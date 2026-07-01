"""
/pollreport report test suite.

Exercises the rebuilt _render_pollreport, which emits ONE combined CSV
(key metrics + game-level summary + player-level report + legend + worked
example) plus a compact monospace Telegram caption with at-a-glance visuals.

Everything is derived from payment_confirmations (the authoritative ledger),
never from the unreliable quickpoll_votes/quickpoll_guests tables. Reports are
clamped to REPORT_START (May 1, 2026) onward.

Run:  python test_pollreport.py
"""
import asyncio
import csv
import io
import os
import sqlite3
import tempfile
from datetime import datetime

# Point the bot at a temp DB BEFORE importing so init_database() builds it there.
_tmp = tempfile.mkdtemp()
import bot as botmod
botmod.DB_FILE = os.path.join(_tmp, 'test.db')

GROUP = -100999

# ---- fakes -----------------------------------------------------------------

class FakeBot:
    def __init__(self):
        self.messages = []      # {chat_id, text}
        self.documents = []     # {chat_id, filename, caption, content(str)}

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append({'chat_id': chat_id, 'text': text})

    async def send_document(self, chat_id, document, caption=None, **kwargs):
        raw = getattr(document, 'input_file_content', None)
        content = raw.decode('utf-8-sig') if isinstance(raw, (bytes, bytearray)) else ''
        self.documents.append({
            'chat_id': chat_id,
            'filename': getattr(document, 'filename', None),
            'caption': caption,
            'content': content,
        })

class FakeApp:
    def __init__(self, fbot):
        self.bot = fbot

# ---- harness ---------------------------------------------------------------

_results = []

def check(name, cond, detail=''):
    _results.append((name, bool(cond), detail))
    mark = 'PASS' if cond else 'FAIL'
    line = f"[{mark}] {name}"
    if detail and not cond:
        line += f"  -> {detail}"
    print(line)

def new_bot():
    b = botmod.SoccerBotV2(token='x')
    fbot = FakeBot()
    b.application = FakeApp(fbot)
    return b, fbot

def conn():
    return sqlite3.connect(botmod.DB_FILE)

def seed_group(chat_id=GROUP, name='TuesdaySoccer'):
    c = conn(); c.execute(
        "INSERT OR IGNORE INTO chat_groups (chat_id, group_name) VALUES (?, ?)",
        (chat_id, name)); c.commit(); c.close()

def seed_wallet(username, balance=0.0, first_paid=1, user_id=None):
    c = conn(); c.execute(
        "INSERT OR IGNORE INTO wallets (username, user_id, balance, first_paid) "
        "VALUES (?, ?, ?, ?)", (username, user_id, balance, first_paid)); c.commit(); c.close()

def seed_poll(pid, field_rate, game_date, location, closed, chat_id=GROUP, max_players=16):
    c = conn(); c.execute(
        "INSERT INTO quickpolls (id, location_name, field_rate, game_date, closed, "
        "chat_id, admin_id, max_players) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
        (pid, location, field_rate, game_date, closed, chat_id, max_players))
    c.commit(); c.close()

def seed_txn(username, amount, notes, date='2026-06-23', status='confirmed', user_id=None):
    c = conn(); c.execute(
        "INSERT INTO payment_confirmations (username, user_id, amount, notes, status, "
        "payment_date, confirmed_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (username, user_id, amount, notes, status, date, date)); c.commit(); c.close()

# ---- scenario --------------------------------------------------------------

def build_scenario():
    seed_group()
    for u, bal in [('alice', 45), ('bob', 10), ('carol', 80), ('dave', -5), ('eve', 50)]:
        seed_wallet(u, bal)

    # Poll #1 — played. field 50, 4 IN, carol brought 1 guest (pays 2 shares).
    seed_poll(1, 50.0, '2026-06-23', 'Round Rock', closed=1)
    seed_txn('alice', -10, 'quickpoll_vote:1')
    seed_txn('bob',   -10, 'quickpoll_vote:1')
    seed_txn('carol', -20, 'quickpoll_vote:1')   # 2 units -> 1 guest
    seed_txn('dave',  -10, 'quickpoll_vote:1')

    # Poll #2 — open (in range, no charges, no field rate).
    seed_poll(2, None, '2026-06-28', 'Brushy Creek', closed=0)

    # Poll #3 — canceled (closed, no vote charges, has a cancel snapshot row).
    seed_poll(3, None, '2026-06-30', 'Robin Bledsoe', closed=1)
    seed_txn('alice', 0, 'quickpoll_cancelled_out:3', date='2026-06-30')

    # Wallet cash-flow (all in-window June dates).
    seed_txn('alice', 50, 'topup', date='2026-06-20')
    seed_txn('alice', 5,  'quickpoll_refund:1', date='2026-06-24')
    seed_txn('bob',   50, 'topup', date='2026-06-20')
    seed_txn('bob',  -30, 'cashout', date='2026-06-25')
    seed_txn('carol', 100, 'topup', date='2026-06-19')
    seed_txn('dave',  5,  'admin_adjustment', date='2026-06-21')
    seed_txn('eve',   50, 'topup', date='2026-06-18')
    # A waiver row that must be EXCLUDED from player cash-flow.
    seed_txn('eve',   99, 'waiver:1', date='2026-06-18')

# ---- helpers ---------------------------------------------------------------

def parse_csv(content):
    return list(csv.reader(io.StringIO(content)))

def find_row(rows, first_cell):
    for i, r in enumerate(rows):
        if r and r[0] == first_cell:
            return i, r
    return -1, None

def section_table(rows, marker):
    """Return (header, data_rows, totals_row) for a '=== marker ===' section:
    header is the first row after the marker; data rows follow until 'TOTALS'."""
    idx, _ = find_row(rows, marker)
    header = rows[idx + 1]
    data, totals = [], None
    for r in rows[idx + 2:]:
        if not r:
            break
        if r[0] == 'TOTALS':
            totals = r
            break
        data.append(r)
    return header, data, totals

def kv(rows, key):
    _, r = find_row(rows, key)
    return r[1] if r else None

# ---- tests -----------------------------------------------------------------

async def run():
    b, fbot = new_bot()
    build_scenario()

    # All-time report (clamped to May 1, 2026 → today).
    await b._render_pollreport(send_chat_id=777, group_chat_id=GROUP,
                               group_name='TuesdaySoccer', rng=(None, None))

    check('one document sent', len(fbot.documents) == 1, f"got {len(fbot.documents)}")
    doc = fbot.documents[0]
    check('filename shape', (doc['filename'] or '').startswith('TuesdaySoccer_Report_'),
          doc['filename'])
    rows = parse_csv(doc['content'])

    # ---- title + metrics ---------------------------------------------------
    check('title row', 'ACCOUNTING REPORT' in rows[0][0], rows[0][0])
    check('metrics games', kv(rows, 'Games (played / canceled / total)') == '1 / 1 / 3',
          kv(rows, 'Games (played / canceled / total)'))
    check('metrics field', kv(rows, 'Total field cost') == '$50.00',
          kv(rows, 'Total field cost'))
    check('metrics collected', kv(rows, 'Total collected') == '$50.00',
          kv(rows, 'Total collected'))
    check('metrics NET', kv(rows, 'NET (collected − field cost)') == '$0.00',
          kv(rows, 'NET (collected − field cost)'))
    check('metrics avg players', kv(rows, 'Avg players / game (played)') == '4.0',
          kv(rows, 'Avg players / game (played)'))
    check('metrics sum balances', kv(rows, 'Sum of current balances (all-time)') == '$180.00',
          kv(rows, 'Sum of current balances (all-time)'))
    check('metrics debtors', kv(rows, 'Players in debt (balance < 0)') == '1 (owes $5.00)',
          kv(rows, 'Players in debt (balance < 0)'))

    # ---- game table --------------------------------------------------------
    ghdr, gdata, gtot = section_table(rows, '=== GAME LEVEL SUMMARY ===')
    check('game header', ghdr[:3] == ['date', 'location', 'status'], str(ghdr))
    gi = {n: i for i, n in enumerate(ghdr)}
    _, g1 = find_row(rows, '2026-06-23')
    check('game1 played', g1[gi['status']] == 'played', str(g1))
    check('game1 field $50.00', g1[gi['field_cost']] == '$50.00', str(g1))
    check('game1 per_share $10.00', g1[gi['per_share']] == '$10.00', str(g1))
    check('game1 in 4', g1[gi['in_players']] == '4', str(g1))
    check('game1 guests 1', g1[gi['guests']] == '1', str(g1))
    check('game1 collected $50.00', g1[gi['collected']] == '$50.00', str(g1))
    check('game1 surplus $0.00', g1[gi['surplus']] == '$0.00', str(g1))
    check('game1 no_shows 1', g1[gi['no_shows']] == '1', str(g1))
    check('game1 in_list has all 4',
          all(u in g1[gi['in_players_list']] for u in ('alice', 'bob', 'carol', 'dave')),
          g1[gi['in_players_list']])
    check('game1 guests_brought carol', 'carol (+1)' in g1[gi['guests_brought_by']],
          g1[gi['guests_brought_by']])
    _, g3 = find_row(rows, '2026-06-30')
    check('game3 canceled em-dash', g3[gi['status']] == 'canceled'
          and g3[gi['collected']] == '—', str(g3))
    _, g2 = find_row(rows, '2026-06-28')
    check('game2 open', g2[gi['status']] == 'open', str(g2))
    check('game TOTALS field $50.00', gtot[gi['field_cost']] == '$50.00', str(gtot))

    # ---- player table ------------------------------------------------------
    phdr, pdata, ptot = section_table(rows, '=== PLAYER LEVEL REPORT ===')
    check('player header',
          phdr[:4] == ['username', 'games_played', 'games', 'own_share'], str(phdr))
    pi = {n: i for i, n in enumerate(phdr)}
    _, alice = find_row(rows, 'alice')
    check('alice games_played 1', alice[pi['games_played']] == '1', str(alice))
    check('alice games list Jun 23', 'Jun 23' in alice[pi['games']], str(alice))
    check('alice field_paid $10.00', alice[pi['field_paid']] == '$10.00', str(alice))
    check('alice venmo $50.00', alice[pi['venmo_in']] == '$50.00', str(alice))
    check('alice refunds $5.00', alice[pi['refunds']] == '$5.00', str(alice))
    check('alice net +$45.00', alice[pi['net_period']] == '+$45.00', str(alice))
    check('alice balance $45.00', alice[pi['balance_all_time']] == '$45.00', str(alice))
    check('alice eligible yes', alice[pi['eligible_to_play']] == 'yes', str(alice))

    _, carol = find_row(rows, 'carol')
    check('carol guests_brought 1', carol[pi['guests_brought']] == '1', str(carol))
    check('carol guest_cost $10.00', carol[pi['guest_cost']] == '$10.00', str(carol))
    check('carol own_share $10.00', carol[pi['own_share']] == '$10.00', str(carol))
    check('carol field_paid $20.00', carol[pi['field_paid']] == '$20.00', str(carol))

    _, bob = find_row(rows, 'bob')
    check('bob cashouts $30.00', bob[pi['cashouts']] == '$30.00', str(bob))
    check('bob net +$10.00', bob[pi['net_period']] == '+$10.00', str(bob))
    check('bob eligible no (balance == floor)', bob[pi['eligible_to_play']] == 'no', str(bob))

    _, dave = find_row(rows, 'dave')
    check('dave other_adj $5.00', dave[pi['other_adjustments']] == '$5.00', str(dave))
    check('dave net -$5.00', dave[pi['net_period']] == '-$5.00', str(dave))
    check('dave balance -$5.00', dave[pi['balance_all_time']] == '-$5.00', str(dave))

    _, eve = find_row(rows, 'eve')
    check('eve games_played 0', eve[pi['games_played']] == '0', str(eve))
    check('eve venmo $50.00 (waiver excluded)', eve[pi['venmo_in']] == '$50.00', str(eve))

    # ---- legend + example --------------------------------------------------
    check('legend present', find_row(rows, '=== LEGEND & NOTES ===')[0] != -1)
    check('legend net_period note', find_row(rows, 'net_period')[0] != -1)
    check('legend balance note', find_row(rows, 'balance_all_time')[0] != -1)
    check('example present', find_row(rows, '=== WORKED EXAMPLE ===')[0] != -1)
    joined = "\n".join(",".join(r) for r in rows)
    check('example uses Ali Nazem', 'Ali Nazem' in joined, '')

    # ---- caption -----------------------------------------------------------
    cap = doc['caption'] or ''
    check('caption is <pre>', cap.startswith('<pre>') and cap.endswith('</pre>'), cap[:40])
    check('caption group name', 'TUESDAYSOCCER' in cap, cap[:60])
    check('caption NET covered', 'NET' in cap and 'covered' in cap, cap)
    check('caption games line', '1 played' in cap and 'canceled' in cap, cap)
    check('caption owes dave', 'dave' in cap and '-$5.00' in cap, cap)
    check('caption most active', 'Most active' in cap, cap)
    check('caption has a bar glyph', '█' in cap, cap)
    check('caption has a sparkline glyph', any(g in cap for g in '▁▂▃▄▅▆▇█'), cap)

    # ---- May-1 floor + empty period ---------------------------------------
    fbot.documents.clear(); fbot.messages.clear()
    # A May window: the June data is out of range -> no activity.
    await b._render_pollreport(777, GROUP, 'TuesdaySoccer',
                               (datetime(2026, 5, 1), datetime(2026, 5, 31)))
    check('empty period: no docs', len(fbot.documents) == 0, f"{len(fbot.documents)} docs")
    check('empty period: message with month label',
          any('May 2026' in msg['text'] for msg in fbot.messages), str(fbot.messages))

    # A pre-May window is clamped to May 1 -> also no activity, no crash.
    fbot.documents.clear(); fbot.messages.clear()
    await b._render_pollreport(777, GROUP, 'TuesdaySoccer',
                               (datetime(2026, 1, 1), datetime(2026, 4, 30)))
    check('pre-May floor: no docs', len(fbot.documents) == 0, f"{len(fbot.documents)} docs")

    # ---- summary -----------------------------------------------------------
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"\n{passed}/{total} checks passed")
    return passed == total


if __name__ == '__main__':
    ok = asyncio.run(run())
    raise SystemExit(0 if ok else 1)
