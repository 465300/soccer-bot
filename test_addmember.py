"""
Smoke test for the nudge-roster commands (/addmember, /removemember, /members)
and the fix that stops init_database from wiping the members table.

Run:  python test_addmember.py
"""
import asyncio
import os
import sqlite3
import tempfile
from types import SimpleNamespace

# Point the bot at a temp DB BEFORE importing so init_database() builds it there.
_tmp = tempfile.mkdtemp()
import bot as botmod
botmod.DB_FILE = os.path.join(_tmp, 'test.db')

_results = []

class FakeBot:
    def __init__(self):
        self.sent = []
    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({'chat_id': chat_id, 'text': text, 'kwargs': kwargs})
    async def pin_chat_message(self, **kwargs):
        pass

class FakeApp:
    def __init__(self, fbot):
        self.bot = fbot

def make_update(user_id=1, username='admin'):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username=username, first_name='Adminy'),
        effective_chat=SimpleNamespace(id=user_id, type='private'),
        message=SimpleNamespace(message_id=1, text='/x'),
    )

def make_ctx(*args):
    return SimpleNamespace(args=list(args), user_data={})

def check(name, cond, detail=''):
    _results.append((name, bool(cond)))
    mark = 'PASS' if cond else 'FAIL'
    print(f"[{mark}] {name}" + (f"  -- {detail}" if detail and not cond else ''))

def members_in_db():
    conn = sqlite3.connect(botmod.DB_FILE); c = conn.cursor()
    try:
        rows = [r[0] for r in c.execute("SELECT username FROM members ORDER BY LOWER(username)").fetchall()]
    except sqlite3.OperationalError:
        rows = None  # table missing
    conn.close()
    return rows

def seed_poll(poll_id, chat_id):
    conn = sqlite3.connect(botmod.DB_FILE); c = conn.cursor()
    c.execute("""INSERT INTO quickpolls (id, location_name, max_players, chat_id, admin_id, closed)
                 VALUES (?, 'Park', 16, ?, 1, 0)""", (poll_id, chat_id))
    conn.commit(); conn.close()

def seed_vote(poll_id, username, uid):
    conn = sqlite3.connect(botmod.DB_FILE); c = conn.cursor()
    c.execute("INSERT INTO quickpoll_votes (poll_id, user_id, username, vote_type) VALUES (?,?,?, 'in')",
              (poll_id, uid, username))
    conn.commit(); conn.close()

async def main():
    # F1: a fresh init_database must NOT drop the members table anymore.
    b = botmod.SoccerBotV2(token='x')
    fbot = FakeBot(); b.application = FakeApp(fbot)
    check('F1 members table survives init', members_in_db() is not None,
          'init_database still drops members — fix line ~508')

    # A1: addmember adds, strips @, is case-insensitive on dupes
    await b.addmember_cmd(make_update(), make_ctx('@Alice', 'bob'))
    check('A1 added two', members_in_db() == ['Alice', 'bob'], str(members_in_db()))

    # A2: re-adding (different case) does not duplicate
    fbot.sent.clear()
    await b.addmember_cmd(make_update(), make_ctx('alice', 'Carol'))
    check('A2 no dup, adds new', sorted([m.lower() for m in members_in_db()]) == ['alice', 'bob', 'carol'])
    check('A2 reports existing', 'Already on the roster' in fbot.sent[-1]['text'])

    # A3: no args -> usage
    fbot.sent.clear()
    await b.addmember_cmd(make_update(), make_ctx())
    check('A3 usage on no args', 'Usage' in fbot.sent[-1]['text'])

    # R1: removemember removes present, reports missing
    fbot.sent.clear()
    await b.removemember_cmd(make_update(), make_ctx('@bob', 'ghost'))
    check('R1 bob removed', 'bob' not in [m.lower() for m in members_in_db()])
    check('R1 reports missing', 'Not on the roster' in fbot.sent[-1]['text'])

    # M1: members lists current roster
    fbot.sent.clear()
    await b.members_cmd(make_update(), make_ctx())
    txt = fbot.sent[-1]['text']
    check('M1 lists roster', '@Alice' in txt and '@Carol' in txt and 'roster (2)' in txt, txt)

    # N1: end-to-end nudge tags only non-voters from the roster
    seed_poll(100, chat_id=-555)
    seed_vote(100, 'Alice', uid=11)          # Alice voted; Carol did not
    fbot.sent.clear()
    count = await b.nudge_nonvoters(100, -555, respect_closed=False)
    sent_text = fbot.sent[-1]['text'] if fbot.sent else ''
    check('N1 nudged 1 non-voter', count == 1, f'count={count}')
    check('N1 tagged Carol not Alice', '@Carol' in sent_text and '@Alice' not in sent_text, sent_text)
    check('N1 posted to group', fbot.sent and fbot.sent[-1]['chat_id'] == -555)

    # summary
    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    print("\n" + "=" * 50)
    print(f"{passed}/{total} checks passed")
    print("ALL GREEN" if passed == total else "SOME FAILED")
    return passed == total

if __name__ == '__main__':
    ok = asyncio.run(main())
    raise SystemExit(0 if ok else 1)
