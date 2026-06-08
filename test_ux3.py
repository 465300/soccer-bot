"""
UX-3 (non-voter nudger) test suite.

Runs bot.py logic against a throwaway SQLite DB with a fake Telegram bot that
records outgoing messages. Covers scheduling, non-voter computation,
nudge dispatch, the /nudge command, chunking, and the scheduler end-to-end.

Run:  python test_ux3.py
"""
import asyncio
import os
import sqlite3
import tempfile
from datetime import timedelta
from types import SimpleNamespace

# Point the bot at a temp DB BEFORE importing so init_database() builds it there.
_tmp = tempfile.mkdtemp()
import bot as botmod
botmod.DB_FILE = os.path.join(_tmp, 'test.db')

TZ = botmod.TZ
now = lambda: botmod.datetime.now(TZ)

# ---- fakes -----------------------------------------------------------------

class FakeBot:
    def __init__(self):
        self.sent = []          # list of dicts: {chat_id, text, kwargs}
        self.raise_on = None    # set to a chat_id to simulate a send failure

    async def send_message(self, chat_id, text, **kwargs):
        if self.raise_on is not None and chat_id == self.raise_on:
            raise RuntimeError("simulated telegram failure")
        self.sent.append({'chat_id': chat_id, 'text': text, 'kwargs': kwargs})

    async def pin_chat_message(self, **kwargs):
        pass

class FakeApp:
    def __init__(self, fbot):
        self.bot = fbot

def make_update(user_id, username='admin', chat_id=None):
    chat_id = chat_id if chat_id is not None else user_id
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username=username, first_name='Adminy'),
        effective_chat=SimpleNamespace(id=chat_id, type='private'),
        message=SimpleNamespace(message_id=1, text='/nudge'),
    )

def make_ctx(*args):
    return SimpleNamespace(args=list(args), user_data={})

# ---- harness ---------------------------------------------------------------

_results = []

def reset_db():
    conn = sqlite3.connect(botmod.DB_FILE)
    c = conn.cursor()
    # Ensure members exists (3.14 sqlite quirk can drop it during init migration)
    c.execute("""CREATE TABLE IF NOT EXISTS members (
        username TEXT PRIMARY KEY COLLATE NOCASE, first_name TEXT,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, member_type TEXT DEFAULT 'member')""")
    for t in ('members', 'quickpoll_votes', 'quickpolls', 'scheduled_events', 'chat_admins'):
        c.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()

def seed_members(*names):
    conn = sqlite3.connect(botmod.DB_FILE)
    c = conn.cursor()
    for n in names:
        c.execute("INSERT OR IGNORE INTO members (username, first_name) VALUES (?, ?)", (n, n))
    conn.commit(); conn.close()

def seed_poll(poll_id, chat_id, closed=0, deadline=None, max_players=16):
    conn = sqlite3.connect(botmod.DB_FILE)
    c = conn.cursor()
    c.execute("""INSERT INTO quickpolls (id, location_name, max_players, chat_id, admin_id, closed, deadline_time)
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
              (poll_id, 'Park', max_players, chat_id, 1, closed, deadline))
    conn.commit(); conn.close()

def seed_vote(poll_id, username, vote_type, user_id=None):
    conn = sqlite3.connect(botmod.DB_FILE)
    c = conn.cursor()
    # unique(poll_id, user_id) — give distinct user_ids to avoid clobbering
    if user_id is None:
        user_id = abs(hash((poll_id, username))) % 10_000_000
    c.execute("INSERT INTO quickpoll_votes (poll_id, user_id, username, vote_type) VALUES (?, ?, ?, ?)",
              (poll_id, user_id, username, vote_type))
    conn.commit(); conn.close()

def seed_admin(chat_id, user_id):
    conn = sqlite3.connect(botmod.DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO chat_admins (chat_id, user_id, username) VALUES (?, ?, ?)",
              (chat_id, user_id, 'admin'))
    conn.commit(); conn.close()

def count_events(event_type='nudge_nonvoters', executed=None):
    conn = sqlite3.connect(botmod.DB_FILE)
    c = conn.cursor()
    q = "SELECT fire_time, executed FROM scheduled_events WHERE event_type = ?"
    c.execute(q, (event_type,))
    rows = c.fetchall()
    conn.close()
    if executed is not None:
        rows = [r for r in rows if r[1] == executed]
    return rows

def new_bot():
    b = botmod.SoccerBotV2(token='x')
    # 3.14 sqlite quirk drops members during init migration — re-ensure it.
    conn = sqlite3.connect(botmod.DB_FILE); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS members (
        username TEXT PRIMARY KEY COLLATE NOCASE, first_name TEXT,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, member_type TEXT DEFAULT 'member')""")
    conn.commit(); conn.close()
    fbot = FakeBot()
    b.application = FakeApp(fbot)
    return b, fbot

def check(name, cond, detail=''):
    _results.append((name, bool(cond), detail))
    mark = 'PASS' if cond else 'FAIL'
    print(f"[{mark}] {name}" + (f"  -- {detail}" if detail and not cond else ''))

# ---- tests -----------------------------------------------------------------

def t_scheduling():
    # T1 long deadline -> 3 nudges
    reset_db(); b, _ = new_bot()
    b.schedule_nudge_events(100, -1, now() + timedelta(hours=48))
    check('T1 long deadline schedules 3 nudges', len(count_events()) == 3, f"got {len(count_events())}")

    # T2 short deadline (3h) -> only -2h is future
    reset_db(); b, _ = new_bot()
    b.schedule_nudge_events(100, -1, now() + timedelta(hours=3))
    check('T2 3h deadline schedules 1 nudge', len(count_events()) == 1, f"got {len(count_events())}")

    # T3 very short deadline (1h) -> none in future
    reset_db(); b, _ = new_bot()
    b.schedule_nudge_events(100, -1, now() + timedelta(hours=1))
    check('T3 1h deadline schedules 0 nudges', len(count_events()) == 0, f"got {len(count_events())}")

    # T2b exactly 12h -> -2h future, -12h is "now" (not strictly >), -24h past => 1
    reset_db(); b, _ = new_bot()
    b.schedule_nudge_events(100, -1, now() + timedelta(hours=12, seconds=30))
    check('T2b 12h deadline schedules 2 nudges', len(count_events()) == 2, f"got {len(count_events())}")

def t_nonvoters():
    reset_db()
    b, _ = new_bot()
    seed_members('alice', 'bob', 'carol', 'dave')
    seed_poll(1, -100)
    seed_vote(1, 'alice', 'in')
    seed_vote(1, 'bob', 'out')
    nv = set(b.get_nonvoters(1))
    check('T4 nonvoters = carol,dave', nv == {'carol', 'dave'}, f"got {nv}")

    # T5 case-insensitive
    reset_db(); b, _ = new_bot()
    seed_members('Alice', 'Bob')
    seed_poll(1, -100)
    seed_vote(1, 'alice', 'in')   # lowercase vote vs capitalized member
    nv = set(b.get_nonvoters(1))
    check('T5 case-insensitive match', nv == {'Bob'}, f"got {nv}")

    # T6 no members
    reset_db(); b, _ = new_bot()
    seed_poll(1, -100)
    check('T6 no members -> []', b.get_nonvoters(1) == [], f"got {b.get_nonvoters(1)}")

    # T7 all voted
    reset_db(); b, _ = new_bot()
    seed_members('alice', 'bob')
    seed_poll(1, -100)
    seed_vote(1, 'alice', 'in'); seed_vote(1, 'bob', 'out')
    check('T7 all voted -> []', b.get_nonvoters(1) == [], f"got {b.get_nonvoters(1)}")

    # T8 OUT also counts as voted (already implied) — explicit single-member OUT
    reset_db(); b, _ = new_bot()
    seed_members('alice')
    seed_poll(1, -100)
    seed_vote(1, 'alice', 'out')
    check('T8 OUT counts as voted', b.get_nonvoters(1) == [], f"got {b.get_nonvoters(1)}")

async def t_nudge_dispatch():
    # T9 open poll with nonvoters -> sends, returns count, header + mentions present
    reset_db(); b, fbot = new_bot()
    seed_members('alice', 'bob', 'carol')
    seed_poll(1, -100, closed=0)
    seed_vote(1, 'alice', 'in')
    n = await b.nudge_nonvoters(1, -100)
    sent_text = fbot.sent[0]['text'] if fbot.sent else ''
    check('T9 returns count 2', n == 2, f"got {n}")
    check('T9 sent one group message', len(fbot.sent) == 1, f"got {len(fbot.sent)}")
    check('T9 message tags both nonvoters', '@bob' in sent_text and '@carol' in sent_text and '@alice' not in sent_text, sent_text)
    check('T9 message has header', 'haven' in sent_text.lower() or 'coming up' in sent_text.lower(), sent_text)
    check('T9 sent to group chat', fbot.sent[0]['chat_id'] == -100)

    # T10 closed poll, respect_closed=True -> no send, 0
    reset_db(); b, fbot = new_bot()
    seed_members('alice', 'bob'); seed_poll(1, -100, closed=1)
    n = await b.nudge_nonvoters(1, -100, respect_closed=True)
    check('T10 closed poll skipped (count 0)', n == 0, f"got {n}")
    check('T10 closed poll no send', len(fbot.sent) == 0, f"got {len(fbot.sent)}")

    # T11 closed poll, manual (respect_closed=False) -> sends
    reset_db(); b, fbot = new_bot()
    seed_members('alice', 'bob'); seed_poll(1, -100, closed=1)
    n = await b.nudge_nonvoters(1, -100, respect_closed=False)
    check('T11 manual overrides closed (count 2)', n == 2, f"got {n}")
    check('T11 manual sent message', len(fbot.sent) == 1, f"got {len(fbot.sent)}")

    # T12 nonexistent poll -> 0, no send
    reset_db(); b, fbot = new_bot()
    seed_members('alice')
    n = await b.nudge_nonvoters(999, -100)
    check('T12 missing poll -> 0', n == 0, f"got {n}")
    check('T12 missing poll no send', len(fbot.sent) == 0)

    # T13 chunking: 65 nonvoters -> 3 messages (30/30/5), header only on first
    reset_db(); b, fbot = new_bot()
    names = [f"user{i}" for i in range(65)]
    seed_members(*names)
    seed_poll(1, -100, closed=0)
    n = await b.nudge_nonvoters(1, -100)
    check('T13 returns 65', n == 65, f"got {n}")
    check('T13 three chunks', len(fbot.sent) == 3, f"got {len(fbot.sent)}")
    if len(fbot.sent) == 3:
        first, second, third = (m['text'] for m in fbot.sent)
        check('T13 header only on first chunk',
              ('coming up' in first.lower()) and ('coming up' not in second.lower()) and ('coming up' not in third.lower()))
        check('T13 chunk sizes 30/30/5',
              first.count('@') == 30 and second.count('@') == 30 and third.count('@') == 5,
              f"{first.count('@')}/{second.count('@')}/{third.count('@')}")

    # T14 send failure is swallowed (no raise)
    reset_db(); b, fbot = new_bot()
    seed_members('alice'); seed_poll(1, -100, closed=0)
    fbot.raise_on = -100
    try:
        n = await b.nudge_nonvoters(1, -100)
        check('T14 send failure swallowed', True)
    except Exception as e:
        check('T14 send failure swallowed', False, repr(e))

async def t_nudge_cmd():
    # T15 no args, latest poll, open -> group msg + admin "Nudged N"
    reset_db(); b, fbot = new_bot()
    seed_members('alice', 'bob'); seed_admin(-100, 500); seed_poll(1, -100, closed=0)
    seed_vote(1, 'alice', 'in')
    await b.nudge_cmd(make_update(500), make_ctx())
    group_msgs = [m for m in fbot.sent if m['chat_id'] == -100]
    admin_msgs = [m for m in fbot.sent if m['chat_id'] == 500]
    check('T15 group nudge sent', len(group_msgs) == 1, f"got {len(group_msgs)}")
    check('T15 admin got confirmation', admin_msgs and 'Nudged 1' in admin_msgs[-1]['text'],
          admin_msgs[-1]['text'] if admin_msgs else 'none')

    # T16 no args, no poll for admin
    reset_db(); b, fbot = new_bot()
    await b.nudge_cmd(make_update(500), make_ctx())
    check('T16 no poll -> error DM', fbot.sent and 'No recent poll' in fbot.sent[-1]['text'],
          fbot.sent[-1]['text'] if fbot.sent else 'none')

    # T17 explicit poll_id admin manages
    reset_db(); b, fbot = new_bot()
    seed_members('alice', 'bob'); seed_admin(-100, 500); seed_poll(7, -100, closed=0)
    await b.nudge_cmd(make_update(500), make_ctx('7'))
    check('T17 explicit poll id works', any(m['chat_id'] == -100 for m in fbot.sent))

    # T18 explicit poll_id NOT managed by this admin
    reset_db(); b, fbot = new_bot()
    seed_members('alice'); seed_admin(-100, 500); seed_poll(7, -100, closed=0)
    seed_admin(-200, 600)  # different admin/chat
    await b.nudge_cmd(make_update(600), make_ctx('7'))  # 600 manages -200, not poll 7's -100
    check('T18 unmanaged poll rejected', fbot.sent and 'No poll with that ID' in fbot.sent[-1]['text'],
          fbot.sent[-1]['text'] if fbot.sent else 'none')

    # T19 invalid (non-int) poll id
    reset_db(); b, fbot = new_bot()
    seed_admin(-100, 500)
    await b.nudge_cmd(make_update(500), make_ctx('abc'))
    check('T19 non-int poll id -> Usage', fbot.sent and 'Usage' in fbot.sent[-1]['text'],
          fbot.sent[-1]['text'] if fbot.sent else 'none')

    # T20 everyone voted -> "already voted"
    reset_db(); b, fbot = new_bot()
    seed_members('alice'); seed_admin(-100, 500); seed_poll(1, -100, closed=0)
    seed_vote(1, 'alice', 'in')
    await b.nudge_cmd(make_update(500), make_ctx())
    check('T20 everyone voted message', fbot.sent and 'already voted' in fbot.sent[-1]['text'],
          fbot.sent[-1]['text'] if fbot.sent else 'none')

    # T21 no members -> "No members on file"
    reset_db(); b, fbot = new_bot()
    seed_admin(-100, 500); seed_poll(1, -100, closed=0)
    await b.nudge_cmd(make_update(500), make_ctx())
    check('T21 no members message', fbot.sent and 'No members on file' in fbot.sent[-1]['text'],
          fbot.sent[-1]['text'] if fbot.sent else 'none')

async def t_scheduler_e2e():
    # T22 due nudge for OPEN poll fires + marks executed
    reset_db(); b, fbot = new_bot()
    seed_members('alice', 'bob'); seed_poll(1, -100, closed=0)
    seed_vote(1, 'alice', 'in')
    past = (now() - timedelta(minutes=1)).isoformat()
    b.schedule_event('nudge_nonvoters', past, {'poll_id': 1, 'chat_id': -100})
    await b.process_pending_events()
    check('T22 open poll nudge fired', any(m['chat_id'] == -100 for m in fbot.sent))
    check('T22 event marked executed', len(count_events(executed=1)) == 1 and len(count_events(executed=0)) == 0)

    # T23 due nudge for CLOSED poll -> no send, still marked executed
    reset_db(); b, fbot = new_bot()
    seed_members('alice', 'bob'); seed_poll(1, -100, closed=1)
    past = (now() - timedelta(minutes=1)).isoformat()
    b.schedule_event('nudge_nonvoters', past, {'poll_id': 1, 'chat_id': -100})
    await b.process_pending_events()
    check('T23 closed poll no send', len(fbot.sent) == 0, f"got {len(fbot.sent)}")
    check('T23 event still marked executed', len(count_events(executed=1)) == 1)

    # T24 cancel cleanup marks pending nudge executed (simulate cancel query)
    reset_db(); b, fbot = new_bot()
    seed_poll(1, -100, closed=0)
    future = (now() + timedelta(hours=5)).isoformat()
    b.schedule_event('nudge_nonvoters', future, {'poll_id': 1, 'chat_id': -100})
    # mimic _execute_cancel_quickpoll cleanup
    conn = sqlite3.connect(botmod.DB_FILE); c = conn.cursor()
    c.execute("SELECT id, payload FROM scheduled_events WHERE event_type IN ('finalize_teams', 'close_quickpoll', 'nudge_nonvoters') AND executed = 0")
    import json
    for eid, pj in c.fetchall():
        if json.loads(pj).get('poll_id') == 1:
            c.execute("UPDATE scheduled_events SET executed = 1 WHERE id = ?", (eid,))
    conn.commit(); conn.close()
    check('T24 cancel cleanup cancels nudge', len(count_events(executed=0)) == 0 and len(count_events(executed=1)) == 1)

def main():
    botmod.SoccerBotV2(token='x')  # build the schema in the temp DB
    t_scheduling()
    t_nonvoters()
    asyncio.run(t_nudge_dispatch())
    asyncio.run(t_nudge_cmd())
    asyncio.run(t_scheduler_e2e())
    total = len(_results)
    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"\n{'='*50}\n{passed}/{total} checks passed")
    failed = [(n, d) for n, ok, d in _results if not ok]
    if failed:
        print("FAILURES:")
        for n, d in failed:
            print(f"  - {n}  {d}")
        raise SystemExit(1)
    print("ALL GREEN")

if __name__ == '__main__':
    main()
