"""
Admin-access test suite — covers the "admins can do everything except money"
fix and the menu-sync that makes newly-added (@username, NULL user_id) admins
actually see their commands.

Run:  python test_admin_access.py
"""
import asyncio
import os
import sqlite3
import tempfile
from types import SimpleNamespace

_tmp = tempfile.mkdtemp()
import bot as botmod
botmod.DB_FILE = os.path.join(_tmp, 'test.db')

from telegram.ext import ApplicationHandlerStop

SUPER = 5517943591

class FakeBot:
    def __init__(self):
        self.sent = []
        self.menus = []  # list of (chat_id, [command names])

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({'chat_id': chat_id, 'text': text, 'kwargs': kwargs})

    async def set_my_commands(self, commands, scope=None, **kwargs):
        chat_id = getattr(scope, 'chat_id', None)
        self.menus.append((chat_id, [c.command for c in commands]))

class FakeApp:
    def __init__(self, fbot):
        self.bot = fbot

_results = []
def check(name, cond, detail=''):
    _results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not cond else ''))

def new_bot():
    b = botmod.SoccerBotV2(token='x')
    botmod.SUPER_ADMIN_ID = SUPER  # set AFTER init (avoids fresh-DB chat_groups migration)
    fbot = FakeBot()
    b.application = FakeApp(fbot)
    return b, fbot

def reset_db():
    conn = sqlite3.connect(botmod.DB_FILE); c = conn.cursor()
    for t in ('chat_admins', 'chat_groups', 'settings'):
        c.execute(f"DELETE FROM {t}")
    conn.commit(); conn.close()

def seed_admin(chat_id, user_id, username):
    conn = sqlite3.connect(botmod.DB_FILE); c = conn.cursor()
    c.execute("INSERT INTO chat_admins (chat_id, user_id, username) VALUES (?, ?, ?)", (chat_id, user_id, username))
    conn.commit(); conn.close()

def set_settings_chat(chat_id):
    conn = sqlite3.connect(botmod.DB_FILE); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('chat_id', ?)", (str(chat_id),))
    conn.commit(); conn.close()

def get_admins(chat_id):
    conn = sqlite3.connect(botmod.DB_FILE); c = conn.cursor()
    c.execute("SELECT user_id, username FROM chat_admins WHERE chat_id = ?", (chat_id,))
    rows = c.fetchall(); conn.close()
    return rows

def user(uid, username=None):
    return SimpleNamespace(id=uid, username=username, first_name='X')

def update_for(uid, username, text):
    return SimpleNamespace(
        effective_user=user(uid, username),
        effective_chat=SimpleNamespace(id=uid, type='private'),
        message=SimpleNamespace(message_id=1, text=text),
    )
def ctx(*args):
    return SimpleNamespace(args=list(args), user_data={})

# ---- menu composition ------------------------------------------------------

def t_menus():
    b, _ = new_bot()
    player, admin_ops, sup = b._command_menus()
    admin_names = {c.command for c in admin_ops}
    sup_names = {c.command for c in sup}
    check('M1 admin menu has quickpoll', 'quickpoll' in admin_names)
    check('M2 admin menu has nudge', 'nudge' in admin_names)
    check('M3 admin menu has addadmin/removeadmin/listadmins',
          {'addadmin', 'removeadmin', 'listadmins'} <= admin_names, admin_names)
    check('M4 admin menu has wallethistory', 'wallethistory' in admin_names)
    check('M5 admin menu EXCLUDES money cmds',
          not ({'voidpayment', 'deletepayment', 'adjustbalance'} & admin_names), admin_names)
    check('M6 super menu = money cmds only',
          sup_names == {'voidpayment', 'deletepayment', 'adjustbalance'}, sup_names)

def t_constants():
    check('C1 money cmds super-only', botmod.SUPER_ADMIN_ONLY_COMMANDS == {'voidpayment', 'deletepayment', 'adjustbalance'},
          botmod.SUPER_ADMIN_ONLY_COMMANDS)
    check('C2 admin cmds include admin mgmt',
          {'addadmin', 'removeadmin', 'listadmins', 'wallethistory'} <= botmod.ADMIN_COMMANDS)
    check('C3 admin cmds exclude money',
          not ({'voidpayment', 'deletepayment', 'adjustbalance'} & botmod.ADMIN_COMMANDS))

# ---- role detection --------------------------------------------------------

def t_roles():
    reset_db(); b, _ = new_bot()
    seed_admin(-100, 222, 'bob')          # admin by id
    seed_admin(-100, None, 'carol')       # admin by username, NULL id
    check('R1 super', b._role_for(user(SUPER, 'boss')) == 'super')
    check('R2 admin by id', b._role_for(user(222, 'bob')) == 'admin')
    check('R3 admin by username (NULL id)', b._role_for(user(333, 'carol')) == 'admin')
    check('R4 member', b._role_for(user(999, 'rando')) == 'member')

# ---- menu sync / back-fill -------------------------------------------------

async def t_sync():
    # S1 back-fills NULL user_id by username + pushes admin menu
    reset_db(); b, fbot = new_bot()
    seed_admin(-100, None, 'carol')
    await b.sync_user_commands(user(333, 'carol'))
    rows = get_admins(-100)
    check('S1 user_id back-filled', rows == [(333, 'carol')], rows)
    check('S1 menu pushed to user', fbot.menus and fbot.menus[-1][0] == 333, fbot.menus)
    check('S1 admin menu pushed', 'quickpoll' in fbot.menus[-1][1] and 'addadmin' in fbot.menus[-1][1])

    # S2 no push when nothing newly linked and not forced (already has id)
    reset_db(); b, fbot = new_bot()
    seed_admin(-100, 222, 'bob')
    await b.sync_user_commands(user(222, 'bob'))
    check('S2 no redundant push', fbot.menus == [], fbot.menus)

    # S3 force pushes even when already linked
    reset_db(); b, fbot = new_bot()
    seed_admin(-100, 222, 'bob')
    await b.sync_user_commands(user(222, 'bob'), force=True)
    check('S3 force pushes admin menu', fbot.menus and 'quickpoll' in fbot.menus[-1][1])

    # S4 member force-push gets player-only menu
    reset_db(); b, fbot = new_bot()
    await b.sync_user_commands(user(999, 'rando'), force=True)
    check('S4 member gets player menu', fbot.menus and fbot.menus[-1][1] == ['start', 'wallet', 'topup', 'cashout', 'cancel'],
          fbot.menus[-1][1] if fbot.menus else None)

    # S5 super force-push includes money cmds
    reset_db(); b, fbot = new_bot()
    await b.sync_user_commands(user(SUPER, 'boss'), force=True)
    check('S5 super menu has money cmds', fbot.menus and {'voidpayment', 'adjustbalance'} <= set(fbot.menus[-1][1]))

# ---- guard authorization ---------------------------------------------------

async def run_guard(b, upd):
    try:
        await b.private_command_guard(upd, ctx())
        return 'allowed'
    except ApplicationHandlerStop:
        return 'blocked'

async def t_guard():
    # G1 admin (NULL id) can run /quickpoll  + gets back-filled & menu
    reset_db(); b, fbot = new_bot()
    seed_admin(-100, None, 'carol')
    res = await run_guard(b, update_for(333, 'carol', '/quickpoll'))
    check('G1 admin allowed /quickpoll', res == 'allowed', res)
    check('G1 admin back-filled via guard', get_admins(-100) == [(333, 'carol')], get_admins(-100))

    # G2 admin can now run /addadmin (newly opened)
    reset_db(); b, fbot = new_bot()
    seed_admin(-100, 222, 'bob')
    check('G2 admin allowed /addadmin', await run_guard(b, update_for(222, 'bob', '/addadmin @x')) == 'allowed')
    check('G2 admin allowed /listadmins', await run_guard(b, update_for(222, 'bob', '/listadmins')) == 'allowed')
    check('G2 admin allowed /wallethistory', await run_guard(b, update_for(222, 'bob', '/wallethistory @x')) == 'allowed')

    # G3 admin BLOCKED from money commands
    reset_db(); b, fbot = new_bot()
    seed_admin(-100, 222, 'bob')
    check('G3 admin blocked /adjustbalance', await run_guard(b, update_for(222, 'bob', '/adjustbalance @x 5')) == 'blocked')
    check('G3 admin blocked /voidpayment', await run_guard(b, update_for(222, 'bob', '/voidpayment 1')) == 'blocked')

    # G4 member blocked from admin commands, allowed player commands
    reset_db(); b, fbot = new_bot()
    check('G4 member blocked /quickpoll', await run_guard(b, update_for(999, 'rando', '/quickpoll')) == 'blocked')
    check('G4 member allowed /wallet', await run_guard(b, update_for(999, 'rando', '/wallet')) == 'allowed')

    # G5 super allowed money command
    reset_db(); b, fbot = new_bot()
    check('G5 super allowed /adjustbalance', await run_guard(b, update_for(SUPER, 'boss', '/adjustbalance @x 5')) == 'allowed')

# ---- removeadmin super protection + listadmins crash fix -------------------

async def t_removeadmin_protection():
    # P1 admin cannot remove super by ID
    reset_db(); b, fbot = new_bot()
    set_settings_chat(-100)
    seed_admin(-100, SUPER, None)
    seed_admin(-100, 222, 'bob')
    await b.removeadmin_cmd(update_for(222, 'bob', f'/removeadmin {SUPER}'), ctx(str(SUPER)))
    still = [r for r in get_admins(-100) if r[0] == SUPER]
    check('P1 super not removed by admin', still and fbot.sent[-1]['text'].startswith('❌'), fbot.sent[-1]['text'])

    # P2 admin cannot remove super by username
    reset_db(); b, fbot = new_bot()
    set_settings_chat(-100)
    seed_admin(-100, SUPER, 'boss')
    seed_admin(-100, 222, 'bob')
    await b.removeadmin_cmd(update_for(222, 'bob', '/removeadmin @boss'), ctx('boss'))
    still = [r for r in get_admins(-100) if r[0] == SUPER]
    check('P2 super not removed by username', len(still) == 1, get_admins(-100))

    # P3 super CAN remove a normal admin
    reset_db(); b, fbot = new_bot()
    set_settings_chat(-100)
    seed_admin(-100, SUPER, 'boss')
    seed_admin(-100, 222, 'bob')
    await b.removeadmin_cmd(update_for(SUPER, 'boss', '/removeadmin @bob'), ctx('bob'))
    check('P3 super removed bob', not any(r[1] == 'bob' for r in get_admins(-100)), get_admins(-100))

async def t_listadmins_nullname():
    # L1 listadmins does not crash on a NULL-username row (the real prod bug)
    reset_db(); b, fbot = new_bot()
    set_settings_chat(-100)
    seed_admin(-100, SUPER, None)   # super row with NULL username
    seed_admin(-100, None, 'AminMghd')
    try:
        await b.listadmins_cmd(update_for(SUPER, 'boss', '/listadmins'), ctx())
        sent = fbot.sent[-1]['text'] if fbot.sent else ''
        check('L1 listadmins no crash + lists rows', 'Admins for chat' in sent and 'AminMghd' in sent, sent)
        check('L1 null username shown safely', '(no username)' in sent, sent)
    except Exception as e:
        check('L1 listadmins no crash + lists rows', False, repr(e))

def main():
    botmod.SoccerBotV2(token='x')  # build schema
    t_menus()
    t_constants()
    t_roles()
    asyncio.run(t_sync())
    asyncio.run(t_guard())
    asyncio.run(t_removeadmin_protection())
    asyncio.run(t_listadmins_nullname())
    total = len(_results); passed = sum(1 for _, ok, _ in _results if ok)
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
