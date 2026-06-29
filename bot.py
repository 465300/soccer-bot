"""
Soccer Bot v2 - Season-based poll automation with webhook + DB scheduling
"""

import os
import csv
import io
import html
import json
import asyncio
import logging
import traceback
from datetime import datetime, timedelta
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    PicklePersistence,
    ApplicationHandlerStop,
    ChatMemberHandler,
    filters,
)
import sqlite3
import pytz

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Use /data for Fly.io persistent volume, otherwise local
DATA_DIR = '/data' if os.path.exists('/data') else '.'
DB_FILE = os.path.join(DATA_DIR, 'soccer_bot_v2.db')
PERSISTENCE_FILE = os.path.join(DATA_DIR, 'bot_persistence.pickle')
TZ = pytz.timezone('America/Chicago')

# Webhook config
WEBHOOK_URL = os.getenv('WEBHOOK_URL', f"https://{os.getenv('FLY_APP_NAME', 'soccer-telegram-bot')}.fly.dev")
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '')
WEBHOOK_PORT = int(os.getenv('PORT', '8080'))

# States for quickpoll conversation
QP_GROUP_SELECT, QP_LOCATION_NAME, QP_LOCATION_LINK, QP_DATE, QP_TIME_START, QP_TIME_END, QP_MAX_PLAYERS, QP_DEADLINE, QP_AUTO_TEAMS, QP_NUM_TEAMS = range(100, 110)

# States for late arrivals input
AWAITING_LATE_ARRIVALS_INPUT = 110

# Pre-fill check state for quickpoll repeat
QP_REPEAT_CHECK = 111

# State for field cost input in quickpoll wizard
QP_FIELD_RATE = 112

# States for wallet conversations (custom top-up amount, cash-out)
TOPUP_CUSTOM_AMOUNT = 120
CASHOUT_AMOUNT, CASHOUT_HANDLE = 121, 122

# Cancel quickpoll with reason
CANCEL_QP_REASON = 123
CANCEL_QP_GROUP  = 124

# ===== Payment / wallet config =====
VENMO_HANDLE = '@chico-leo'  # Venmo handle players pay to for top-ups
VOTE_COST = 10.00      # charged per IN vote, refunded on switch to OUT
WALLET_FLOOR = 10.00   # minimum balance required to vote IN
TOPUP_MIN = 20.00      # minimum custom top-up amount

# UX-3: how many hours before the deadline to auto-nudge non-voters.
# Only intervals that still land in the future are scheduled, so short
# deadlines naturally skip the earlier nudges.
NUDGE_INTERVALS_HOURS = (24, 12, 2)

# Super-admin controls: only this user can manage admin lifecycle
_raw_super_admin_id = os.getenv('SUPER_ADMIN_ID', '').strip()
SUPER_ADMIN_ID = int(_raw_super_admin_id) if _raw_super_admin_id.isdigit() else 0

# Role-based command routing for private chats
PLAYER_COMMANDS = {'wallet', 'topup', 'cashout', 'cancel'}
ADMIN_COMMANDS = {
    'quickpoll', 'cancelquickpoll', 'closepoll', 'refreshpoll', 'maketeams',
    'setskill', 'skills', 'deleteskill',
    'viewlate', 'addlate', 'removelate', 'clearlate', 'listchats',
    'sendvenmolink', 'waive', 'initchats',
    'addplayer', 'removeplayer', 'nudge',
    'addmember', 'removemember', 'members',
    'pollreport',
    # Admins get everything except the money commands below.
    'addadmin', 'removeadmin', 'listadmins', 'wallethistory',
    'switchgroup', 'mygroups',
}
# Money commands stay super-admin-only (super admin's personal Venmo account).
SUPER_ADMIN_ONLY_COMMANDS = {'voidpayment', 'deletepayment', 'adjustbalance'}


class SoccerBotV2:
    def __init__(self, token: str):
        self.token = token
        self.application = None
        self._processing = False
        self._pending_teams: dict[str, str] = {}  # key -> prebuilt teams message text
        self._pending_cancels: dict[str, str] = {}  # key -> cancel group message text
        self._pending_late_arrivals: dict[int, dict] = {}  # admin_id -> {poll_id, chat_id, players_list}
        self._refresh_tasks: dict[int, asyncio.Task] = {}  # poll_id -> pending debounced card-refresh task
        self._pending_guest_add: dict[int, dict] = {}  # user_id -> {poll_id}
        self._pending_guest_remove: dict[int, dict] = {}  # user_id -> {poll_id, guests: [(id, name), ...]}
        self._cqpg_pending: dict[int, tuple] = {}  # user_id -> (poll_id, chat_id, group_name) from group picker
        self._pending_pollreport: dict[int, dict] = {}  # user_id -> {chat_id, group_name} waiting for date input
        self.init_database()

    async def send(self, update: Update, text: str, **kwargs):
        """Send message WITHOUT replying - uses direct API call and forwards kwargs.
        If a parse_mode (Markdown/HTML) send fails because the text has a broken
        entity, retry once as plain text so the user still gets the message
        instead of the command silently doing nothing."""
        try:
            await self.application.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                **kwargs
            )
        except Exception as e:
            if 'parse_mode' in kwargs and "parse entities" in str(e).lower():
                logger.warning(f"Markdown/HTML send failed ({e}); retrying as plain text.")
                fallback = {k: v for k, v in kwargs.items() if k != 'parse_mode'}
                try:
                    await self.application.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=text,
                        **fallback
                    )
                    return
                except Exception as e2:
                    logger.error(f"Plain-text retry also failed: {e2}")
            logger.error(f"Error sending message: {e}")

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Catch-all for unhandled exceptions in any handler. Without this,
        python-telegram-bot logs nothing visible and the user just sees the
        command do nothing (silent failure). Logs the full traceback and DMs
        the super admin so problems surface immediately."""
        logger.error("Unhandled exception while processing an update:", exc_info=context.error)
        tb = "".join(traceback.format_exception(
            type(context.error), context.error, getattr(context.error, '__traceback__', None)))

        # Best-effort: who triggered it and with what input
        where = ""
        try:
            if isinstance(update, Update) and update.effective_user:
                u = update.effective_user
                if update.message and update.message.text:
                    trigger = update.message.text
                elif update.callback_query:
                    trigger = f"[callback] {update.callback_query.data}"
                else:
                    trigger = "(no text)"
                where = f"user @{u.username or u.id} (id={u.id})\ninput: {trigger}\n\n"
        except Exception:
            pass

        if SUPER_ADMIN_ID:
            try:
                head = f"⚠️ Bot error\n{where}{type(context.error).__name__}: {context.error}"
                detail = html.escape(tb[-2500:])  # tail of traceback, within Telegram's 4096 cap
                await self.application.bot.send_message(
                    chat_id=SUPER_ADMIN_ID,
                    text=f"{head}\n\n<pre>{detail}</pre>",
                    parse_mode='HTML')
            except Exception as e:
                logger.error(f"Failed to DM super admin about error: {e}")

    def is_super_admin(self, user_id: int) -> bool:
        return bool(SUPER_ADMIN_ID and user_id == SUPER_ADMIN_ID)

    def is_admin_any_chat(self, user_id: int, username: str = None) -> bool:
        """Check if a user is admin in any registered chat (for private-command permissions)."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        if user_id:
            c.execute("SELECT 1 FROM chat_admins WHERE user_id = ? LIMIT 1", (user_id,))
            if c.fetchone():
                conn.close()
                return True
        if username:
            clean = username.lstrip('@')
            c.execute("SELECT 1 FROM chat_admins WHERE LOWER(username) = LOWER(?) LIMIT 1", (clean,))
            hit = c.fetchone() is not None
            conn.close()
            return hit
        conn.close()
        return False

    def get_admin_target_chat(self, user_id: int) -> tuple:
        """Return (chat_id, group_name) from the admin's saved active group, or (None, None)."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""SELECT aac.chat_id, cg.group_name
                     FROM admin_active_chat aac
                     JOIN chat_groups cg ON aac.chat_id = cg.chat_id
                     WHERE aac.user_id = ?""", (user_id,))
        row = c.fetchone()
        conn.close()
        if row:
            return row[0], row[1]
        # Auto-select if only one group exists
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT chat_id, group_name FROM chat_groups")
        groups = c.fetchall()
        conn.close()
        if len(groups) == 1:
            return groups[0][0], groups[0][1]
        return None, None

    def set_admin_target_chat(self, user_id: int, chat_id: int):
        """Save the admin's active group choice."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO admin_active_chat (user_id, chat_id) VALUES (?, ?)", (user_id, chat_id))
        conn.commit()
        conn.close()

    def _group_header(self, group_name: str) -> str:
        return f"📍 {group_name} · /switchgroup to change\n\n"

    def _command_menus(self):
        """The three role-based Telegram command menus. Admins get everything
        the super admin does EXCEPT the money commands (super-only)."""
        player_cmds = [
            BotCommand('start', 'Get started / see what I can do'),
            BotCommand('wallet', 'Check your balance and recent activity'),
            BotCommand('topup', 'Add funds to join games ($10/game)'),
            BotCommand('cashout', 'Withdraw your balance to Venmo'),
            BotCommand('cancel', 'Cancel whatever you\'re doing right now'),
        ]
        admin_ops_cmds = [
            BotCommand('quickpoll', 'Set up a game poll for your group'),
            BotCommand('closepoll', 'Close voting and post the final player list'),
            BotCommand('refreshpoll', 'Push latest buttons to an existing poll card'),
            BotCommand('addplayer', 'Force-add a player to the latest game — /addplayer @user [reason]'),
            BotCommand('removeplayer', 'Force-remove a player from the latest game — /removeplayer @user'),
            BotCommand('nudge', 'Ping members who haven\'t voted yet — /nudge [poll_id]'),
            BotCommand('addmember', 'Add players to the nudge roster — /addmember @user …'),
            BotCommand('removemember', 'Remove players from the roster — /removemember @user …'),
            BotCommand('members', 'Show the nudge roster'),
            BotCommand('cancelquickpoll', 'Cancel a poll and refund everyone'),
            BotCommand('maketeams', 'Split players into balanced skill-based teams'),
            BotCommand('setskill', 'Set a player\'s skill rating — /setskill Name 1-10'),
            BotCommand('skills', 'See all player skill ratings'),
            BotCommand('deleteskill', 'Remove a player\'s skill rating'),
            BotCommand('viewlate', 'See who was marked late for a poll'),
            BotCommand('addlate', 'Mark a player as late — /addlate poll_id username'),
            BotCommand('removelate', 'Undo a late mark — /removelate poll_id username'),
            BotCommand('clearlate', 'Clear all late flags for a poll — /clearlate poll_id'),
            BotCommand('switchgroup', 'Choose which group to target with commands'),
            BotCommand('mygroups', 'See all groups you manage'),
            BotCommand('listchats', 'See all the groups you manage'),
            BotCommand('sendvenmolink', 'Push the top-up card to a player — /sendvenmolink @user'),
            BotCommand('waive', 'One-game wallet bypass for a player — /waive @user'),
            BotCommand('initchats', 'Broadcast wallet setup invite to all your groups'),
            BotCommand('addadmin', 'Give someone admin access — /addadmin @username'),
            BotCommand('removeadmin', 'Revoke admin access — /removeadmin @username'),
            BotCommand('listadmins', 'See all admins for a group'),
            BotCommand('wallethistory', 'Full transaction history for a player — /wallethistory @user'),
            BotCommand('pollreport', 'Game attendance CSV — /pollreport 2026-05 or /pollreport 2026-05-01 2026-06-30'),
        ]
        super_cmds = [
            BotCommand('voidpayment', 'Reverse a payment — /voidpayment <id>'),
            BotCommand('deletepayment', 'Delete a payment record — /deletepayment <id>'),
            BotCommand('adjustbalance', 'Adjust wallet balance — /adjustbalance @user amount'),
        ]
        return player_cmds, admin_ops_cmds, super_cmds

    def _role_for(self, user) -> str:
        """'super' | 'admin' | 'member' for a Telegram user."""
        if not user:
            return 'member'
        if self.is_super_admin(user.id):
            return 'super'
        if self.is_admin_any_chat(user.id, getattr(user, 'username', None)):
            return 'admin'
        return 'member'

    async def sync_user_commands(self, user, force: bool = False):
        """Make sure a user sees the right slash-command menu the moment they
        interact, and back-fill their user_id into chat_admins. Telegram only
        accepts a per-user menu once the user has opened a DM with the bot —
        which they have if they're sending a command/start — so this is where
        admins added by @username (NULL user_id) finally get their menu."""
        if not user:
            return
        linked = 0
        if getattr(user, 'username', None):
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("UPDATE chat_admins SET user_id = ? WHERE user_id IS NULL AND LOWER(username) = LOWER(?)",
                      (user.id, user.username))
            linked = c.rowcount
            conn.commit()
            conn.close()
        player_cmds, admin_ops_cmds, super_cmds = self._command_menus()
        role = self._role_for(user)
        # Push the menu if: we just back-filled their ID, or they're an admin
        # (user_id may have been back-filled via a group vote, leaving linked=0
        # but the per-user admin menu never set), or explicitly forced.
        if not (force or linked or role in ('super', 'admin')):
            return
        # DM the admin their group info the first time we push their menu
        if linked and role in ('super', 'admin'):
            try:
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute("SELECT group_name FROM chat_groups ORDER BY group_name")
                gnames = [r[0] for r in c.fetchall()]
                conn.close()
                if gnames:
                    group_list = ", ".join(self.escape_markdown(g) for g in gnames)
                    await self.application.bot.send_message(
                        chat_id=user.id,
                        text=f"👋 You have admin access to: *{group_list}*\n\nUse /switchgroup to choose which group your commands target.",
                        parse_mode='Markdown')
            except Exception:
                pass
        if role == 'super':
            menu = super_cmds + admin_ops_cmds + player_cmds
        elif role == 'admin':
            menu = admin_ops_cmds + player_cmds
        else:
            menu = player_cmds
        try:
            await self.application.bot.set_my_commands(menu, scope=BotCommandScopeChat(chat_id=user.id))
        except Exception as e:
            logger.warning(f"Could not sync commands for user {user.id}: {e}")

    async def refresh_command_scopes(self):
        """Apply role-based Telegram command menus.
        - Members: player commands only
        - Approved admins: player + admin operations
        - Super admin: all commands (including admin lifecycle)
        """
        player_cmds, admin_ops_cmds, super_cmds = self._command_menus()

        # Baseline visibility: private users see only player commands; groups see none.
        await self.application.bot.set_my_commands(player_cmds, scope=BotCommandScopeAllPrivateChats())
        await self.application.bot.set_my_commands([], scope=BotCommandScopeAllGroupChats())

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = 'scoped_admin_users'")
        old_row = c.fetchone()
        old_scoped = []
        if old_row and old_row[0]:
            try:
                old_scoped = [int(v) for v in json.loads(old_row[0]) if int(v) > 0]
            except Exception:
                old_scoped = []

        c.execute("SELECT DISTINCT user_id FROM chat_admins WHERE user_id IS NOT NULL AND user_id > 0")
        admin_ids = [r[0] for r in c.fetchall()]
        conn.close()

        # Revoke stale per-user admin menus.
        new_scoped_set = set(admin_ids)
        if SUPER_ADMIN_ID:
            new_scoped_set.add(SUPER_ADMIN_ID)
        for uid in old_scoped:
            if uid not in new_scoped_set:
                try:
                    await self.application.bot.set_my_commands(player_cmds, scope=BotCommandScopeChat(chat_id=uid))
                except Exception as e:
                    logger.warning(f"Could not clear scoped commands for user {uid}: {e}")

        admin_menu = admin_ops_cmds + player_cmds
        for uid in admin_ids:
            if SUPER_ADMIN_ID and uid == SUPER_ADMIN_ID:
                continue
            try:
                await self.application.bot.set_my_commands(admin_menu, scope=BotCommandScopeChat(chat_id=uid))
            except Exception as e:
                logger.warning(f"Could not set admin menu for user {uid}: {e}")

        if SUPER_ADMIN_ID:
            try:
                await self.application.bot.set_my_commands(
                    super_cmds + admin_ops_cmds + player_cmds,
                    scope=BotCommandScopeChat(chat_id=SUPER_ADMIN_ID)
                )
            except Exception as e:
                logger.warning(f"Could not set super-admin menu for {SUPER_ADMIN_ID}: {e}")
        else:
            logger.warning("SUPER_ADMIN_ID is not set. Super-admin-only commands remain inaccessible.")

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('scoped_admin_users', ?)",
                  (json.dumps(sorted(list(new_scoped_set))),))
        conn.commit()
        conn.close()

    async def private_command_guard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Central gate for private commands based on role and command type."""
        if update.effective_chat.type != 'private' or not update.message or not update.message.text:
            return
        if not update.message.text.startswith('/'):
            return

        cmd = update.message.text.split()[0].split('@')[0].lstrip('/').lower()
        if not cmd:
            return

        user = update.effective_user
        # Back-fill user_id for @username-added admins and push their menu the
        # first time we learn their ID — this is what makes a newly-added
        # admin actually SEE /quickpoll etc.
        await self.sync_user_commands(user)
        role = self._role_for(user)

        if cmd in SUPER_ADMIN_ONLY_COMMANDS and role != 'super':
            await self.send(update, "❌ Not allowed.")
            raise ApplicationHandlerStop
        if cmd in ADMIN_COMMANDS and role not in ('super', 'admin'):
            await self.send(update, "❌ Not allowed.")
            raise ApplicationHandlerStop

    def init_database(self):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
        c.execute('''CREATE TABLE IF NOT EXISTS season (
            id INTEGER PRIMARY KEY, location_name TEXT, location_link TEXT, game_day TEXT,
            start_time TEXT, end_time TEXT, start_date TEXT, duration_weeks INTEGER,
            max_players INTEGER, current_week INTEGER DEFAULT 1, active INTEGER DEFAULT 1)''')
        c.execute('''CREATE TABLE IF NOT EXISTS members (
            username TEXT PRIMARY KEY COLLATE NOCASE, first_name TEXT, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        # Clean up duplicate members
        try:
            c.execute("SELECT rowid, username FROM members")
            all_members = c.fetchall()
            seen = set()
            for rowid, username in all_members:
                clean_name = username.lower() if username else ''
                if clean_name in seen:
                    c.execute("DELETE FROM members WHERE rowid = ?", (rowid,))
                else:
                    seen.add(clean_name)
            # Migrate from old schema if needed
            c.execute("SELECT user_id FROM members LIMIT 1")
            c.execute("CREATE TABLE IF NOT EXISTS members_new (username TEXT PRIMARY KEY COLLATE NOCASE, first_name TEXT, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            c.execute("INSERT OR IGNORE INTO members_new (username, first_name, added_at) SELECT username, first_name, added_at FROM members")
            c.execute("DROP TABLE members")
            c.execute("ALTER TABLE members_new RENAME TO members")
        except:
            pass
        c.execute('''CREATE TABLE IF NOT EXISTS polls (
            id INTEGER PRIMARY KEY, season_id INTEGER, week_number INTEGER, game_date TEXT,
            message_id INTEGER, chat_id INTEGER, deadline TEXT, closed INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY, poll_id INTEGER, user_id INTEGER, username TEXT, vote_type TEXT,
            voted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(poll_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS skills (
            username TEXT PRIMARY KEY COLLATE NOCASE, skill_rating INTEGER DEFAULT 5,
            last_activity TIMESTAMP)''')
        
        # Clean up duplicates using Python (simpler and more compatible)
        try:
            c.execute("SELECT rowid, username, skill_rating FROM skills")
            all_rows = c.fetchall()
            seen = {}
            to_delete = []
            for rowid, username, rating in all_rows:
                clean_name = username.strip('"').strip("'").lower()
                if clean_name in seen:
                    # Keep the one with higher rating
                    if rating > seen[clean_name][1]:
                        to_delete.append(seen[clean_name][0])
                        seen[clean_name] = (rowid, rating)
                    else:
                        to_delete.append(rowid)
                else:
                    seen[clean_name] = (rowid, rating)
            for rid in to_delete:
                c.execute("DELETE FROM skills WHERE rowid = ?", (rid,))
            # Update usernames to remove quotes
            c.execute("UPDATE skills SET username = TRIM(TRIM(username, '\"'), '''') WHERE username LIKE '\"%' OR username LIKE '''%'")
        except:
            pass
        
        # Drop old table if it has user_id column (migration)
        try:
            c.execute("SELECT user_id FROM skills LIMIT 1")
            c.execute("CREATE TABLE IF NOT EXISTS skills_new (username TEXT PRIMARY KEY COLLATE NOCASE, skill_rating INTEGER DEFAULT 5, last_activity TIMESTAMP)")
            c.execute("INSERT OR REPLACE INTO skills_new (username, skill_rating, last_activity) SELECT TRIM(TRIM(username, '\"'), ''''), skill_rating, last_activity FROM skills")
            c.execute("DROP TABLE skills")
            c.execute("ALTER TABLE skills_new RENAME TO skills")
        except:
            pass
        c.execute('''CREATE TABLE IF NOT EXISTS quickpolls (
            id INTEGER PRIMARY KEY, location_name TEXT, max_players INTEGER,
            deadline_time TIMESTAMP, num_teams INTEGER DEFAULT 2, chat_id INTEGER,
            admin_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS quickpoll_votes (
            id INTEGER PRIMARY KEY, poll_id INTEGER, user_id INTEGER, username TEXT, vote_type TEXT,
            voted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(poll_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS scheduled_events (
            id INTEGER PRIMARY KEY, event_type TEXT NOT NULL, fire_time TEXT NOT NULL,
            payload TEXT NOT NULL, executed INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        # Add columns for native poll support
        try:
            c.execute("ALTER TABLE quickpolls ADD COLUMN telegram_poll_id TEXT")
        except:
            pass
        try:
            c.execute("ALTER TABLE quickpolls ADD COLUMN poll_message_id INTEGER")
        except:
            pass
        try:
            c.execute("ALTER TABLE quickpolls ADD COLUMN admin_id INTEGER")
        except:
            pass
        try:
            c.execute("ALTER TABLE quickpolls ADD COLUMN allow_guests INTEGER DEFAULT 1")
        except:
            pass
        try:
            c.execute("ALTER TABLE quickpolls ADD COLUMN game_date TEXT")
        except:
            pass
        try:
            c.execute("ALTER TABLE quickpolls ADD COLUMN time_start TEXT")
        except:
            pass
        # Quickpoll pre-fill: store location_link and time_end for repeat use
        try:
            c.execute("ALTER TABLE quickpolls ADD COLUMN location_link TEXT")
        except:
            pass
        try:
            c.execute("ALTER TABLE quickpolls ADD COLUMN time_end TEXT")
        except:
            pass
        # UX-2: closed flag — drives the live-roster "🔒 closed" banner and
        # the admin-only override on buttons after a poll ends
        try:
            c.execute("ALTER TABLE quickpolls ADD COLUMN closed INTEGER DEFAULT 0")
        except:
            pass
        # Path B: field cost paid by admin for each session
        try:
            c.execute("ALTER TABLE quickpolls ADD COLUMN field_rate REAL")
        except:
            pass
        # Add member_type column for regular/drop-in players
        try:
            c.execute("ALTER TABLE members ADD COLUMN member_type TEXT DEFAULT 'member'")
        except:
            pass
        # Chat admins table for per-group admin management
        c.execute('''CREATE TABLE IF NOT EXISTS chat_admins (
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            added_by INTEGER,
            PRIMARY KEY (chat_id, user_id))''')
        try:
            c.execute("ALTER TABLE chat_admins ADD COLUMN added_by INTEGER")
        except:
            pass
        # Migration: purge rows where chat_id is positive (DM/private context, not a group)
        c.execute("DELETE FROM chat_admins WHERE chat_id > 0")
        # Migration: ensure super-admin is in chat_admins for all existing groups
        if SUPER_ADMIN_ID:
            c.execute("""INSERT OR IGNORE INTO chat_admins (chat_id, user_id, username)
                         SELECT chat_id, ?, NULL FROM chat_groups""", (SUPER_ADMIN_ID,))
        # Chat groups table for named multi-group management
        c.execute('''CREATE TABLE IF NOT EXISTS chat_groups (
            chat_id INTEGER PRIMARY KEY,
            group_name TEXT UNIQUE,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        # Late arrivals table - track who was late for each poll
        c.execute('''CREATE TABLE IF NOT EXISTS late_arrivals (
            id INTEGER PRIMARY KEY,
            poll_id INTEGER,
            blocked_from_poll_id INTEGER,
            user_id INTEGER,
            username TEXT NOT NULL,
            is_member INTEGER DEFAULT 1,
            added_by_admin_id INTEGER,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            cleared_at TIMESTAMP,
            UNIQUE(poll_id, username))''')
        # Wallets table - track player wallet balances for payment eligibility
        c.execute('''CREATE TABLE IF NOT EXISTS wallets (
            user_id INTEGER,
            username TEXT PRIMARY KEY COLLATE NOCASE,
            balance DECIMAL(10,2) DEFAULT 0.00,
            first_paid INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        # Payment confirmations table - audit trail for all payment flows
        c.execute('''CREATE TABLE IF NOT EXISTS payment_confirmations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT COLLATE NOCASE,
            amount DECIMAL(10,2),
            payment_date TEXT,
            confirmed_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'pending',
            notes TEXT)''')
        # Waivers table - one-game eligibility bypass granted by admin
        c.execute('''CREATE TABLE IF NOT EXISTS waivers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT COLLATE NOCASE,
            granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            used INTEGER DEFAULT 0,
            used_at TIMESTAMP,
            granted_by TEXT)''')
        try:
            c.execute("ALTER TABLE waivers ADD COLUMN granted_by TEXT")
        except:
            pass
        # Active group selection per admin — persists across restarts
        c.execute('''CREATE TABLE IF NOT EXISTS admin_active_chat (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER)''')
        # UX-4: guest (+1) system — inviter brings named guest(s) to a quickpoll
        # confirmed: 0=waitlisted, 1=confirmed+charged, 2=pending_payment (inviter short)
        c.execute('''CREATE TABLE IF NOT EXISTS quickpoll_guests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id INTEGER NOT NULL,
            member_user_id INTEGER,
            member_username TEXT COLLATE NOCASE,
            guest_name TEXT NOT NULL,
            confirmed INTEGER DEFAULT 0,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        # Display-name-only members (no Telegram username, can't be @-pinged)
        try:
            c.execute("ALTER TABLE members ADD COLUMN is_display_name INTEGER DEFAULT 0")
        except:
            pass
        # Cleanup: close quickpolls with past or non-ISO game dates (legacy test polls)
        today = datetime.now(TZ).date()
        c.execute("SELECT id, game_date FROM quickpolls WHERE closed = 0")
        for pid, gd in c.fetchall():
            if not gd:
                continue
            try:
                # Real active polls use ISO format (YYYY-MM-DD) from the wizard.
                # Anything that fails to parse as ISO is old test data — close it.
                game_d = datetime.strptime(gd.strip(), '%Y-%m-%d').date()
                if game_d < today:
                    c.execute("UPDATE quickpolls SET closed = 1 WHERE id = ?", (pid,))
            except ValueError:
                # Non-ISO date (e.g. 'May 30') — legacy/test poll, close it
                c.execute("UPDATE quickpolls SET closed = 1 WHERE id = ?", (pid,))
        conn.commit()
        conn.close()

        # Drop legacy season tables (season feature removed).
        # NOTE: 'members' is NOT dropped — it's the standalone nudge roster
        # (read by get_nonvoters), unrelated to the season feature. Wiping it
        # every boot is what broke /nudge.
        conn2 = sqlite3.connect(DB_FILE)
        c2 = conn2.cursor()
        for tbl in ('votes', 'polls', 'season'):
            try:
                c2.execute(f"DROP TABLE IF EXISTS {tbl}")
            except Exception:
                pass
        conn2.commit()
        conn2.close()

    def is_admin(self, user_id: int, chat_id: int, username: str = None) -> bool:
        """Check if user is an admin for the specified chat (by user_id or username).
        When matched by username, back-fills user_id for faster future lookups."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        result = None
        # Primary check: by numeric user_id
        if user_id:
            c.execute("SELECT 1 FROM chat_admins WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            result = c.fetchone()
        # Fallback: by username (handles newly-added admins whose user_id isn't known yet)
        if not result and username:
            clean = username.lstrip('@')
            c.execute("SELECT 1 FROM chat_admins WHERE chat_id = ? AND LOWER(username) = LOWER(?)", (chat_id, clean))
            result = c.fetchone()
            if result and user_id:
                # Back-fill the real user_id so future lookups are by ID
                c.execute("UPDATE chat_admins SET user_id = ? WHERE chat_id = ? AND LOWER(username) = LOWER(?)",
                          (user_id, chat_id, clean))
                conn.commit()
        conn.close()
        return result is not None

    async def delete_message_safely(self, chat_id: int, message_id: int):
        """Delete a message without raising errors, but log details if it fails"""
        try:
            await self.application.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            logger.warning(f"Could not delete message {message_id} in {chat_id}: {e}")

    def parse_game_datetime(self, game_date_str: str, time_start_str: str) -> datetime | None:
        """Try to parse flexible date/time strings into a datetime object.
        Returns None if parsing fails.
        Tries common formats: MM/DD/YYYY, MM-DD-YYYY, YYYY-MM-DD, etc.
        Times: HH:MM, H:MM, HH:MM AM/PM, etc."""
        if not game_date_str or not time_start_str:
            return None
            
        # Common date formats to try
        date_formats = [
            '%m/%d/%Y', '%m/%d/%y', '%m-%d-%Y', '%m-%d-%y',
            '%Y-%m-%d', '%Y/%m/%d', '%d/%m/%Y', '%d-%m-%Y',
            '%B %d, %Y', '%b %d, %Y', '%B %d, %y', '%b %d, %y',
            '%A %B %d, %Y', '%A %b %d, %Y', '%A %B %d', '%A %b %d',  # With day-of-week
            '%B %d', '%b %d',  # Month name + day (no year)
            '%m/%d', '%m-%d'  # No year (numeric)
        ]
        
        # Common time formats to try
        time_formats = [
            '%H:%M', '%H:%M:%S', '%I:%M %p', '%I:%M:%S %p',
            '%I %p', '%H', '%I:%M%p'  # No separators
        ]
        
        date_obj = None
        time_obj = None
        
        # Try parsing date
        for fmt in date_formats:
            try:
                parsed = datetime.strptime(game_date_str.strip(), fmt)
                date_obj = parsed.date()
                # If no year was parsed, use current year
                if '%Y' not in fmt and '%y' not in fmt:
                    today = datetime.now(TZ).date()
                    date_obj = date_obj.replace(year=today.year)
                    # If parsed date is in the past, try next year
                    if date_obj < today:
                        date_obj = date_obj.replace(year=today.year + 1)
                break
            except ValueError:
                continue
        
        # Try parsing time
        for fmt in time_formats:
            try:
                parsed = datetime.strptime(time_start_str.strip(), fmt)
                time_obj = parsed.time()
                break
            except ValueError:
                continue
        
        # If we got both date and time, combine them
        if date_obj and time_obj:
            try:
                dt = datetime.combine(date_obj, time_obj)
                if dt.tzinfo is None:
                    dt = TZ.localize(dt)
                return dt
            except Exception:
                return None
        
        logger.warning(f"Could not parse game_date '{game_date_str}' with time_start '{time_start_str}'")
        return None

    def schedule_late_arrivals_events(self, poll_id: int, chat_id: int, admin_id: int, 
                                       game_start_time: datetime):
        """Schedule prompt and announce events for late arrivals."""
        # Schedule prompt at game_start_time - 5 minutes
        prompt_time = game_start_time - timedelta(minutes=5)
        self.schedule_event('prompt_late_arrivals', prompt_time, {
            'poll_id': poll_id, 'chat_id': chat_id, 'admin_id': admin_id
        })
        
        # Schedule announcement at game_start_time + 2 hours
        announce_time = game_start_time + timedelta(hours=2)
        self.schedule_event('announce_late_arrivals', announce_time, {
            'poll_id': poll_id, 'chat_id': chat_id, 'admin_id': admin_id
        })

    def schedule_nudge_events(self, poll_id: int, chat_id: int, deadline_time: datetime):
        """UX-3: schedule non-voter nudges before the deadline. Only intervals
        that still fall in the future are scheduled (short deadlines skip the
        earlier ones)."""
        now = datetime.now(TZ)
        for hours_before in NUDGE_INTERVALS_HOURS:
            fire_time = deadline_time - timedelta(hours=hours_before)
            if fire_time > now:
                self.schedule_event('nudge_nonvoters', fire_time, {
                    'poll_id': poll_id, 'chat_id': chat_id
                })

    async def check_admin(self, update: Update) -> tuple[bool, int | None]:
        """Check if user is admin for current chat. Returns (is_admin, chat_id)"""
        user_id = update.effective_user.id
        username = update.effective_user.username

        # Get current configured chat_id
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = 'chat_id'")
        chat_result = c.fetchone()
        conn.close()

        if not chat_result:
            return (False, None)

        chat_id = int(chat_result[0])
        return (self.is_admin(user_id, chat_id, username), chat_id)

    async def get_target_group(self, update: Update, group_name_arg: str = None) -> tuple[int | None, str | None]:
        """Smart group selection: returns (chat_id, group_name) or (None, None) if invalid.
        - If group_name provided: validates admin access and returns that group
        - If not provided and admin manages 1 group: auto-returns that group
        - If not provided and admin manages 2+ groups: returns (None, None) for interactive selection"""
        
        user_id = update.effective_user.id
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Get all groups where user is admin
        c.execute("""SELECT cg.chat_id, cg.group_name 
                     FROM chat_groups cg
                     JOIN chat_admins ca ON cg.chat_id = ca.chat_id
                     WHERE ca.user_id = ?""", (user_id,))
        admin_groups = c.fetchall()
        conn.close()
        
        if not admin_groups:
            return (None, None)
        
        # If group name specified, find and validate it
        if group_name_arg:
            for chat_id, group_name in admin_groups:
                if group_name.lower() == group_name_arg.lower():
                    return (chat_id, group_name)
            # Group specified but not found or not authorized
            return (None, None)
        
        # No group specified - auto-detect
        if len(admin_groups) == 1:
            # Only one group - use it automatically
            return admin_groups[0]
        
        # Multiple groups - need interactive selection
        return (None, None)

    def get_wallet(self, username: str) -> dict | None:
        """Fetch wallet record by username. Returns dict or None if not found."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT user_id, username, balance, first_paid, created_at, updated_at FROM wallets WHERE LOWER(username) = LOWER(?)", (username,))
        row = c.fetchone()
        conn.close()
        if not row:
            return None
        return {
            'user_id': row[0],
            'username': row[1],
            'balance': float(row[2]),
            'first_paid': bool(row[3]),
            'created_at': row[4],
            'updated_at': row[5]
        }

    def check_wallet_eligible(self, username: str) -> tuple[bool, str]:
        """Check if wallet is eligible for voting. Returns (eligible, reason)."""
        wallet = self.get_wallet(username)
        if not wallet:
            return (False, "You don't have a wallet yet.")
        if not wallet['first_paid']:
            return (False, "No confirmed payment on file.")
        if wallet['balance'] <= WALLET_FLOOR:
            return (False, "Balance insufficient.")
        return (True, "Eligible")

    def credit_wallet(self, username: str, amount: float, reason: str = "topup") -> bool:
        """Add funds to wallet, mark first_paid if this is first confirmation, log to payment_confirmations."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        now = datetime.now(TZ).isoformat()

        # Get or create wallet
        wallet = self.get_wallet(username)
        if not wallet:
            c.execute("INSERT INTO wallets (username, balance, first_paid) VALUES (?, ?, ?)",
                     (username, amount, 1))
        else:
            new_balance = wallet['balance'] + amount
            c.execute("UPDATE wallets SET balance = ?, first_paid = 1, updated_at = ? WHERE LOWER(username) = LOWER(?)",
                     (new_balance, now, username))

        # Log to payment_confirmations
        c.execute("""INSERT INTO payment_confirmations
                    (username, amount, payment_date, confirmed_date, status, notes)
                    VALUES (?, ?, ?, ?, 'confirmed', ?)""",
                 (username, amount, now, now, reason))
        conn.commit()
        conn.close()
        return True

    def deduct_wallet(self, username: str, amount: float, reason: str = "vote_cost") -> bool:
        """Subtract funds from wallet. Returns False if insufficient balance."""
        wallet = self.get_wallet(username)
        if not wallet or wallet['balance'] < amount:
            return False

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        now = datetime.now(TZ).isoformat()
        new_balance = wallet['balance'] - amount

        c.execute("UPDATE wallets SET balance = ?, updated_at = ? WHERE LOWER(username) = LOWER(?)",
                 (new_balance, now, username))

        # Log deduction as audit record (negative amount conceptually)
        c.execute("""INSERT INTO payment_confirmations
                    (username, amount, confirmed_date, status, notes)
                    VALUES (?, ?, ?, 'confirmed', ?)""",
                 (username, -amount, now, reason))
        conn.commit()
        conn.close()
        return True

    def get_payment_history(self, username: str, limit: int = 10) -> list[dict]:
        """Fetch payment history for user, newest first."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""SELECT id, amount, payment_date, confirmed_date, status, notes
                    FROM payment_confirmations
                    WHERE LOWER(username) = LOWER(?)
                      AND (notes IS NULL OR notes NOT LIKE 'waiver:%')
                    ORDER BY confirmed_date DESC LIMIT ?""",
                 (username, limit))
        rows = c.fetchall()
        conn.close()

        return [
            {
                'id': row[0],
                'amount': float(row[1]),
                'payment_date': row[2],
                'confirmed_date': row[3],
                'status': row[4],
                'notes': row[5]
            }
            for row in rows
        ]

    # ===== WALLET: PLAYER COMMANDS =====

    def low_balance_text(self, balance: float) -> str:
        """Nudge message shown privately after a charge leaves a wallet at/under the floor."""
        return (
            f"⚠️ *Low wallet balance — ${balance:.2f}*\n\n"
            f"Balance at ${WALLET_FLOOR:.0f} minimum. Top up to keep voting.\n\n"
            "DM me /topup to add funds."
        )

    def build_topup_card(self) -> tuple:
        """Build the top-up amount-selection message (text, keyboard). Reused by /topup and the gate."""
        text = (
            "💳 *Top up your wallet*\n\n"
            "Add funds via Venmo to join games — cost per game may vary due to dynamic player count.\n\n"
            "Recommend *$50*: top up once, play several games, done."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("$50", callback_data="topup:50"),
             InlineKeyboardButton("$100", callback_data="topup:100")],
            [InlineKeyboardButton("Custom", callback_data="topup:custom")],
        ])
        return text, keyboard

    def build_venmo_card(self, amount: float) -> tuple:
        """Build the Venmo payment card with a direct deep link button."""
        venmo_handle_clean = VENMO_HANDLE.lstrip('@')
        venmo_url = (
            f"https://venmo.com/u/{venmo_handle_clean}"
            f"?txn=pay&amount={amount:.2f}&note=Soccer%20game"
        )
        text = (
            f"💵 *Pay ${amount:.2f} via Venmo*\n\n"
            "Tap below to open Venmo — amount is pre-filled. "
            "Come back and confirm once sent."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💸 Pay ${amount:.2f} on Venmo →", url=venmo_url)],
            [InlineKeyboardButton("✅ I've Paid", callback_data=f"ctopup:{amount}")],
        ])
        return text, keyboard

    async def topup_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/topup — show the wallet top-up options privately."""
        text, keyboard = self.build_topup_card()
        await self.send(update, text, reply_markup=keyboard, parse_mode='Markdown')

    async def send_topup_prompt(self, user_id: int, reason: str = ""):
        """DM a user the top-up card — used by the wallet gate when a vote is blocked."""
        text, keyboard = self.build_topup_card()
        if reason:
            text = f"🚫 {reason}\n\n" + text
        try:
            await self.application.bot.send_message(
                chat_id=user_id, text=text, reply_markup=keyboard, parse_mode='Markdown')
        except Exception as e:
            logger.warning(f"Could not DM top-up prompt to {user_id}: {e}")

    async def handle_topup_callback(self, query, arg: str):
        """Handle the $50 / $100 preset buttons on the top-up card."""
        await query.answer()
        try:
            amount = float(arg)
        except ValueError:
            return
        text, keyboard = self.build_venmo_card(amount)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')

    async def confirm_topup(self, query, amount: float):
        """Player tapped 'I've Paid' — submit for super-admin approval (trust-based)."""
        await query.answer()
        user = query.from_user
        username = user.username or user.first_name
        # Super-admin tops up themselves — auto-approve, no confirmation needed
        if SUPER_ADMIN_ID and user.id == SUPER_ADMIN_ID:
            now = datetime.now(TZ).isoformat()
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("""INSERT INTO payment_confirmations
                         (user_id, username, amount, payment_date, confirmed_date, status, notes)
                         VALUES (?, ?, ?, ?, ?, 'pending_topup', 'auto_approved_super_admin')""",
                      (user.id, username, amount, now, now))
            pc_id = c.lastrowid
            conn.commit()
            conn.close()
            self.credit_wallet(username, amount, f"topup_approved:{pc_id}")
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("UPDATE payment_confirmations SET status = 'confirmed', confirmed_date = ? WHERE id = ?",
                      (now, pc_id))
            conn.commit()
            conn.close()
            bal = self.get_wallet(username)['balance']
            await query.edit_message_text(
                f"✅ *${amount:.2f} added to your wallet!*\nNew balance: *${bal:.2f}*",
                parse_mode='Markdown')
            return
        # Guard: already have a pending top-up request?
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT id FROM payment_confirmations WHERE LOWER(username) = LOWER(?) AND status = 'pending_topup'",
                  (username,))
        existing = c.fetchone()
        conn.close()
        if existing:
            await query.edit_message_text(
                "⏳ You already have a top-up pending — waiting for admin confirmation. "
                "You'll be notified once it's approved.",
                parse_mode='Markdown')
            return
        # Insert pending record (not credited yet)
        now = datetime.now(TZ).isoformat()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""INSERT INTO payment_confirmations
                     (user_id, username, amount, payment_date, confirmed_date, status, notes)
                     VALUES (?, ?, ?, ?, ?, 'pending_topup', 'awaiting_admin_approval')""",
                  (user.id, username, amount, now, now))
        pc_id = c.lastrowid
        conn.commit()
        conn.close()
        # DM super-admin
        if SUPER_ADMIN_ID:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"tapprove:{pc_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"treject:{pc_id}"),
            ]])
            try:
                await self.application.bot.send_message(
                    chat_id=SUPER_ADMIN_ID,
                    text=f"💰 @{username} wants to top up *${amount:.2f}*.\nApprove to credit their wallet.",
                    reply_markup=kb,
                    parse_mode='Markdown')
            except Exception as e:
                logger.warning(f"Could not DM super admin for topup approval: {e}")
        await query.edit_message_text(
            f"⏳ *${amount:.2f} top-up submitted* — waiting for admin confirmation.\n\n"
            "You'll be notified as soon as it's approved.",
            parse_mode='Markdown')

    async def topup_approve(self, query, pc_id: int):
        """Super-admin approved a pending top-up — credit the wallet."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT user_id, username, amount, status FROM payment_confirmations WHERE id = ?", (pc_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            await self._safe_answer(query, "Top-up record not found.", show_alert=True)
            return
        user_id, username, amount, status = row
        if status != 'pending_topup':
            await self._safe_answer(query, f"Already processed ({status}).", show_alert=True)
            return
        self.credit_wallet(username, amount, f"topup_approved:{pc_id}")
        # Update the pending record's status (credit_wallet creates a new 'confirmed' row;
        # mark the pending row so it doesn't get double-approved)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE payment_confirmations SET status = 'approved' WHERE id = ?", (pc_id,))
        conn.commit()
        conn.close()
        wallet = self.get_wallet(username)
        balance = wallet['balance'] if wallet else amount
        # DM the player
        if user_id:
            try:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text=f"✅ *${amount:.2f} added* — your balance is now *${balance:.2f}*.\n\n"
                         "You're set for the next few games.",
                    parse_mode='Markdown')
            except Exception as e:
                logger.warning(f"Could not DM topup approval to {user_id}: {e}")
        await self._safe_answer(query, f"✅ Approved — @{username} credited ${amount:.2f}.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    async def topup_reject(self, query, pc_id: int):
        """Super-admin rejected a pending top-up — notify the player."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT user_id, username, amount, status FROM payment_confirmations WHERE id = ?", (pc_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            await self._safe_answer(query, "Top-up record not found.", show_alert=True)
            return
        user_id, username, amount, status = row
        if status != 'pending_topup':
            await self._safe_answer(query, f"Already processed ({status}).", show_alert=True)
            return
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE payment_confirmations SET status = 'rejected' WHERE id = ?", (pc_id,))
        conn.commit()
        conn.close()
        # DM the player
        if user_id:
            try:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ Your *${amount:.2f}* top-up was not confirmed by the admin.\n\n"
                         "If you believe this is an error, reach out. "
                         "You can resubmit anytime via /topup.",
                    parse_mode='Markdown')
            except Exception as e:
                logger.warning(f"Could not DM topup rejection to {user_id}: {e}")
        await self._safe_answer(query, f"❌ Rejected — @{username} notified.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    async def topup_custom_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Entry point for the Custom top-up amount conversation."""
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            f"✏️ *Custom top-up*\n\nType the amount you'd like to add "
            f"(minimum ${TOPUP_MIN:.0f}).\n\nSend /cancel to stop.",
            parse_mode='Markdown')
        return TOPUP_CUSTOM_AMOUNT

    async def topup_custom_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive and validate the custom top-up amount."""
        raw = update.message.text.strip().lstrip('$')
        try:
            amount = round(float(raw), 2)
        except ValueError:
            await self.send(update, "❌ Please send a number, like 75.")
            return TOPUP_CUSTOM_AMOUNT
        if amount < TOPUP_MIN:
            await self.send(update, f"❌ Minimum top-up is ${TOPUP_MIN:.0f}. Send a larger amount.")
            return TOPUP_CUSTOM_AMOUNT
        text, keyboard = self.build_venmo_card(amount)
        await self.send(update, text, reply_markup=keyboard, parse_mode='Markdown')
        return ConversationHandler.END

    async def topup_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel the custom top-up conversation."""
        await self.send(update, "Top-up cancelled.")
        return ConversationHandler.END

    async def wallet_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/wallet — show balance, eligibility, and recent activity privately."""
        user = update.effective_user
        username = user.username or user.first_name
        wallet = self.get_wallet(username)
        if not wallet:
            await self.send(update, "You don't have a wallet yet. Run /topup to get started.")
            return
        eligible, _ = self.check_wallet_eligible(username)
        status = "✅ Eligible to vote in" if eligible else f"⚠️ Top up to vote (need more than ${WALLET_FLOOR:.0f})"
        text = (
            "💰 *Your Wallet*\n\n"
            f"Balance: *${wallet['balance']:.2f}*\n"
            f"Status: {status}\n"
        )
        history = self.get_payment_history(username, 3)
        if history:
            text += "\n*Recent activity:*\n"
            for h in history:
                amt = h['amount']
                sign = "+" if amt >= 0 else "−"
                label = self._txn_label(h['notes'])
                when = self._short_date(h['confirmed_date'])
                text += f"  {sign}${abs(amt):.2f}  {label}  _{when}_\n"
        await self.send(update, text, parse_mode='Markdown')

    def _txn_label(self, notes: str) -> str:
        """Human-readable label for a payment_confirmations.notes value."""
        if not notes:
            return "transaction"
        if notes.startswith("topup"):
            return "top-up"
        if notes.startswith("quickpoll_vote"):
            return "game vote"
        if notes.startswith("quickpoll_refund"):
            return "vote refund"
        if notes.startswith("quickpoll_cancelled"):
            return "game cancelled (refund)"
        if notes.startswith("cashout"):
            return "cash-out"
        return notes

    def _short_date(self, iso_str: str) -> str:
        """Format an ISO timestamp as a short 'Feb 26' date; fall back to the raw value."""
        if not iso_str:
            return ""
        try:
            return datetime.fromisoformat(iso_str).strftime("%b %d")
        except (ValueError, TypeError):
            return str(iso_str)[:10]

    async def cashout_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/cashout — begin a withdrawal request."""
        user = update.effective_user
        username = user.username or user.first_name
        wallet = self.get_wallet(username)
        if not wallet or wallet['balance'] <= 0:
            await self.send(update, "Your wallet is empty — nothing to cash out.")
            return ConversationHandler.END
        context.user_data['cashout_username'] = username
        await self.send(
            update,
            f"💸 *Cash out*\n\nYour balance is *${wallet['balance']:.2f}*.\n\n"
            "How much would you like to withdraw? Send an amount, or /cancel.",
            parse_mode='Markdown')
        return CASHOUT_AMOUNT

    async def cashout_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive and validate the cash-out amount."""
        username = context.user_data.get('cashout_username')
        raw = update.message.text.strip().lstrip('$')
        try:
            amount = round(float(raw), 2)
        except ValueError:
            await self.send(update, "❌ Please send a number, like 40.")
            return CASHOUT_AMOUNT
        wallet = self.get_wallet(username)
        balance = wallet['balance'] if wallet else 0
        if amount <= 0:
            await self.send(update, "❌ Enter an amount greater than zero.")
            return CASHOUT_AMOUNT
        if amount > balance:
            await self.send(update, f"❌ You can't cash out more than your balance (${balance:.2f}).")
            return CASHOUT_AMOUNT
        context.user_data['cashout_amount'] = amount
        await self.send(update, "What's your Venmo handle? (e.g. @your-name)")
        return CASHOUT_HANDLE

    async def cashout_handle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive the Venmo handle, deduct the funds, and notify the admin."""
        username = context.user_data.get('cashout_username')
        amount = context.user_data.get('cashout_amount', 0)
        venmo = update.message.text.strip()
        ok = self.deduct_wallet(username, amount, f"cashout to {venmo}")
        if not ok:
            await self.send(update, "❌ Cash-out failed — your balance changed. Run /cashout again.")
            return ConversationHandler.END
        wallet = self.get_wallet(username)
        balance = wallet['balance'] if wallet else 0
        venmo_md = venmo.replace('_', '\\_').replace('*', '\\*')
        username_md = (username or "").replace('_', '\\_').replace('*', '\\*')
        await self.send(
            update,
            f"✅ *Cash-out confirmed* — ${amount:.2f} to {venmo_md}.\n\n"
            "The money will land in your Venmo account within a few minutes.\n"
            f"Remaining wallet balance: *${balance:.2f}*.",
            parse_mode='Markdown')
        note = (
            "💸 *Cash-out request*\n\n"
            f"Player: {username_md}\n"
            f"Amount: *${amount:.2f}*\n"
            f"Venmo: {venmo_md}\n\n"
            "Send this payment from your Venmo account."
        )
        await self.notify_admins(note)
        context.user_data.pop('cashout_username', None)
        context.user_data.pop('cashout_amount', None)
        return ConversationHandler.END

    async def cashout_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel the cash-out conversation."""
        await self.send(update, "Cash-out cancelled.")
        return ConversationHandler.END

    async def notify_admins(self, text: str):
        """DM every known admin (by user_id). Used for cash-out alerts."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT DISTINCT user_id FROM chat_admins WHERE user_id IS NOT NULL")
        admin_ids = [r[0] for r in c.fetchall()]
        conn.close()
        if not admin_ids:
            logger.warning(f"notify_admins: no admin user_id on record. Message: {text}")
            return
        for aid in admin_ids:
            try:
                await self.application.bot.send_message(
                    chat_id=aid, text=text, parse_mode='Markdown')
            except Exception as e:
                logger.warning(f"Could not notify admin {aid}: {e}")

    # ===== ADMIN ESCAPE HATCHES =====

    async def voidpayment_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/voidpayment <id> — Reverse a payment's financial effect and mark it voided."""
        args = context.args
        if not args or not args[0].isdigit():
            await self.send(update, "Usage: `/voidpayment <payment_id>`\n\nRun /wallet on the player first to find the ID.", parse_mode='Markdown')
            return
        payment_id = int(args[0])
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT id, username, amount, status, notes FROM payment_confirmations WHERE id = ?", (payment_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            await self.send(update, f"❌ No payment found with ID {payment_id}.")
            return
        pid, username, amount, status, notes = row
        if status == 'voided':
            await self.send(update, f"⚠️ Payment #{pid} is already voided.")
            return
        # Reverse the financial effect
        wallet = self.get_wallet(username)
        if not wallet:
            await self.send(update, f"❌ No wallet found for `{username}`. Cannot reverse balance.", parse_mode='Markdown')
            return
        reversal = -float(amount)  # opposite sign
        new_balance = wallet['balance'] + reversal
        now = datetime.now(TZ).isoformat()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE wallets SET balance = ?, updated_at = ? WHERE LOWER(username) = LOWER(?)",
                  (new_balance, now, username))
        c.execute("UPDATE payment_confirmations SET status = 'voided' WHERE id = ?", (payment_id,))
        c.execute("""INSERT INTO payment_confirmations (username, amount, confirmed_date, status, notes)
                     VALUES (?, ?, ?, 'confirmed', ?)""",
                  (username, reversal, now, f"void_of_{payment_id}"))
        conn.commit()
        conn.close()
        direction = "credited" if reversal > 0 else "deducted"
        await self.send(update,
            f"✅ *Payment #{pid} voided*\n\n"
            f"Player: `{username}`\n"
            f"Original amount: ${float(amount):.2f} ({notes or 'no note'})\n"
            f"Reversal: ${abs(reversal):.2f} {direction}\n"
            f"New balance: *${new_balance:.2f}*",
            parse_mode='Markdown')

    async def deletepayment_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/deletepayment <id> — Delete a payment record. No balance change."""
        args = context.args
        if not args or not args[0].isdigit():
            await self.send(update, "Usage: `/deletepayment <payment_id>`\n\nDeletes the audit record only — does *not* change the wallet balance.", parse_mode='Markdown')
            return
        payment_id = int(args[0])
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT id, username, amount, status, notes FROM payment_confirmations WHERE id = ?", (payment_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            await self.send(update, f"❌ No payment found with ID {payment_id}.")
            return
        pid, username, amount, status, notes = row
        c.execute("DELETE FROM payment_confirmations WHERE id = ?", (payment_id,))
        conn.commit()
        conn.close()
        await self.send(update,
            f"🗑️ *Payment #{pid} deleted*\n\n"
            f"Player: `{username}`\n"
            f"Amount: ${float(amount):.2f} | Status: {status}\n"
            f"Note: {notes or '—'}\n\n"
            "Wallet balance was *not* changed.",
            parse_mode='Markdown')

    async def adjustbalance_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/adjustbalance <username> <amount> — Add or subtract from a player's wallet."""
        args = context.args
        if len(args) < 2:
            await self.send(update, "Usage: `/adjustbalance <username> <amount>`\n\nPositive = add funds, negative = remove funds.\nExample: `/adjustbalance @player 20` or `/adjustbalance @player -10`", parse_mode='Markdown')
            return
        raw_username = args[0].lstrip('@')
        try:
            amount = round(float(args[1]), 2)
        except ValueError:
            await self.send(update, "❌ Amount must be a number (e.g. `20` or `-10`).", parse_mode='Markdown')
            return
        if amount == 0:
            await self.send(update, "❌ Amount can't be zero.")
            return
        wallet = self.get_wallet(raw_username)
        old_balance = wallet['balance'] if wallet else 0.0
        new_balance = old_balance + amount
        now = datetime.now(TZ).isoformat()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        if wallet:
            c.execute("UPDATE wallets SET balance = ?, updated_at = ? WHERE LOWER(username) = LOWER(?)",
                      (new_balance, now, raw_username))
        else:
            c.execute("INSERT INTO wallets (username, balance, first_paid) VALUES (?, ?, ?)",
                      (raw_username, new_balance, 0))
        c.execute("""INSERT INTO payment_confirmations (username, amount, confirmed_date, status, notes)
                     VALUES (?, ?, ?, 'confirmed', 'admin_adjustment')""",
                  (raw_username, amount, now))
        conn.commit()
        conn.close()
        sign = "+" if amount > 0 else ""
        await self.send(update,
            f"✅ *Balance adjusted*\n\n"
            f"Player: `{raw_username}`\n"
            f"Adjustment: {sign}${amount:.2f}\n"
            f"Old balance: ${old_balance:.2f} → New balance: *${new_balance:.2f}*",
            parse_mode='Markdown')

    async def sendvenmolink_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/sendvenmolink <username> — DM the top-up card to a player."""
        args = context.args
        if not args:
            await self.send(update, "Usage: `/sendvenmolink <username>`\n\nDMs the top-up card to the player.", parse_mode='Markdown')
            return
        raw_username = args[0].lstrip('@')
        # Look up user_id from wallets table
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT user_id FROM wallets WHERE LOWER(username) = LOWER(?)", (raw_username,))
        row = c.fetchone()
        conn.close()
        user_id = row[0] if row and row[0] else None
        if not user_id:
            await self.send(update,
                f"❌ No Telegram user ID on file for `{raw_username}`.\n\n"
                "They need to have DM'd the bot at least once (e.g. `/start` or `/wallet`) before you can push them a message.",
                parse_mode='Markdown')
            return
        text, keyboard = self.build_topup_card()
        try:
            await self.application.bot.send_message(
                chat_id=user_id, text=text, reply_markup=keyboard, parse_mode='Markdown')
            await self.send(update, f"✅ Top-up card sent to `{raw_username}`.", parse_mode='Markdown')
        except Exception as e:
            await self.send(update, f"❌ Could not DM `{raw_username}`: {e}", parse_mode='Markdown')

    async def wallethistory_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/wallethistory <username> — Show full payment history for a player (admin)."""
        args = context.args
        if not args:
            await self.send(update, "Usage: `/wallethistory <username>`", parse_mode='Markdown')
            return
        raw_username = args[0].lstrip('@')
        wallet = self.get_wallet(raw_username)
        if not wallet:
            await self.send(update, f"❌ No wallet found for `{raw_username}`.", parse_mode='Markdown')
            return
        history = self.get_payment_history(raw_username, limit=50)
        if not history:
            await self.send(update, f"No payment history for `{raw_username}`.", parse_mode='Markdown')
            return
        lines = [f"💳 *Wallet history — {raw_username}*", f"Current balance: *${wallet['balance']:.2f}*\n"]
        for h in history:
            amt = h['amount']
            sign = "+" if amt >= 0 else "−"
            label = self._txn_label(h['notes'])
            when = self._short_date(h['confirmed_date'])
            status = f" _{h['status']}_" if h['status'] != 'confirmed' else ""
            lines.append(f"`#{h['id']}` {sign}${abs(amt):.2f}  {label}{status}  _{when}_")
        await self.send(update, "\n".join(lines), parse_mode='Markdown')

    async def waive_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/waive <username> — Grant a one-game wallet gate bypass (admin)."""
        args = context.args
        if not args:
            await self.send(update, "Usage: `/waive <username>`\n\nGrants the player a one-game bypass — they can vote IN even if their wallet isn't set up or is low.", parse_mode='Markdown')
            return
        raw_username = args[0].lstrip('@')
        granter = update.effective_user.username or update.effective_user.first_name
        now = datetime.now(TZ).isoformat()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        # Check for existing unused waiver
        c.execute("SELECT id FROM waivers WHERE LOWER(username) = LOWER(?) AND used = 0", (raw_username,))
        existing = c.fetchone()
        if existing:
            conn.close()
            await self.send(update,
                f"⚠️ `{raw_username}` already has an unused waiver (#{existing[0]}).\n"
                "It will be used on their next IN vote.",
                parse_mode='Markdown')
            return
        c.execute("INSERT INTO waivers (username, granted_at, granted_by) VALUES (?, ?, ?)", (raw_username, now, granter))
        waiver_id = c.lastrowid
        conn.commit()
        conn.close()
        await self.send(update,
            f"✅ *Waiver granted* — #{waiver_id}\n\n"
            f"Player: `{raw_username}`\n"
            "They can vote IN on the next poll regardless of wallet status.\n"
            "Waiver is consumed automatically when they vote IN.",
            parse_mode='Markdown')

    async def pollreport_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show inline group picker, then accept a date range and deliver a CSV report."""
        user = update.effective_user
        if not (self.is_super_admin(user.id) or self.is_admin_any_chat(user.id, user.username)):
            await self.send(update, "❌ Admin only.")
            return

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""SELECT cg.chat_id, cg.group_name
                     FROM chat_groups cg
                     JOIN chat_admins ca ON cg.chat_id = ca.chat_id
                     WHERE ca.user_id = ?
                     ORDER BY cg.group_name""", (user.id,))
        groups = c.fetchall()
        conn.close()

        if not groups:
            await self.send(update, "❌ No groups found. You must be an admin of a registered group.")
            return

        if len(groups) == 1:
            chat_id, group_name = groups[0]
            self._pending_pollreport[user.id] = {'chat_id': chat_id, 'group_name': group_name}
            await self.send(update,
                f"📊 *Poll Report — {group_name}*\n\n"
                "Enter date range:\n"
                "• `2026-05` → full month\n"
                "• `2026-05-01 2026-06-30` → custom range",
                parse_mode='Markdown')
        else:
            buttons = [[InlineKeyboardButton(name, callback_data=f"prpt_group:{cid}")]
                       for cid, name in groups]
            await self.send(update, "📊 *Poll Report — Select a group:*",
                            parse_mode='Markdown',
                            reply_markup=InlineKeyboardMarkup(buttons))

    async def handle_pollreport_group_callback(self, query, chat_id_str: str):
        """Admin tapped a group button on the poll report group picker."""
        await query.answer()
        chat_id = int(chat_id_str)
        user_id = query.from_user.id
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT group_name FROM chat_groups WHERE chat_id = ?", (chat_id,))
        row = c.fetchone()
        conn.close()
        group_name = row[0] if row else str(chat_id)
        self._pending_pollreport[user_id] = {'chat_id': chat_id, 'group_name': group_name}
        await query.edit_message_text(
            f"📊 *Poll Report — {group_name}*\n\n"
            "Enter date range:\n"
            "• `2026-05` → full month\n"
            "• `2026-05-01 2026-06-30` → custom range",
            parse_mode='Markdown')

    async def _handle_pollreport_date_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process the date range text the admin typed after selecting a group."""
        user_id = update.effective_user.id
        pending = self._pending_pollreport.pop(user_id, None)
        if not pending:
            return
        grp_chat_id = pending['chat_id']
        group_name = pending['group_name']
        parts = update.message.text.strip().split()
        start_date = end_date = None
        USAGE = "Format: `2026-05` for a full month, or `2026-05-01 2026-06-30` for a custom range."

        if len(parts) == 1:
            try:
                dt = datetime.strptime(parts[0], "%Y-%m")
                start_date = dt.replace(day=1)
                end_date = (dt.replace(day=31) if dt.month == 12
                            else dt.replace(month=dt.month + 1, day=1) - timedelta(days=1))
            except ValueError:
                self._pending_pollreport[user_id] = pending
                await self.send(update, f"❌ Couldn't parse that. {USAGE}", parse_mode='Markdown')
                return
        elif len(parts) == 2:
            try:
                start_date = datetime.strptime(parts[0], "%Y-%m-%d")
                end_date = datetime.strptime(parts[1], "%Y-%m-%d")
            except ValueError:
                self._pending_pollreport[user_id] = pending
                await self.send(update, f"❌ Couldn't parse that. {USAGE}", parse_mode='Markdown')
                return
        else:
            self._pending_pollreport[user_id] = pending
            await self.send(update, f"❌ Unexpected format. {USAGE}", parse_mode='Markdown')
            return

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""SELECT q.id, q.game_date, q.location_name, q.max_players, q.field_rate,
                            COUNT(CASE WHEN v.vote_type='in' THEN 1 END) as in_count,
                            COUNT(CASE WHEN v.vote_type='out' THEN 1 END) as out_count
                     FROM quickpolls q
                     LEFT JOIN quickpoll_votes v ON v.poll_id = q.id
                     WHERE q.closed = 1 AND q.chat_id = ?
                     GROUP BY q.id""", (grp_chat_id,))
        rows = c.fetchall()

        DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d", "%b %d", "%B %d", "%B %d, %Y"]

        def parse_game_date(raw):
            if not raw:
                return None
            for fmt in DATE_FORMATS:
                try:
                    d = datetime.strptime(raw.strip(), fmt)
                    if d.year == 1900:
                        d = d.replace(year=datetime.now().year)
                    return d
                except ValueError:
                    continue
            return None

        results = []
        for poll_id, game_date_raw, location, max_players, field_rate, in_count, out_count in rows:
            d = parse_game_date(game_date_raw)
            if not d or not (start_date <= d <= end_date):
                continue
            gc_count = c.execute("""SELECT COUNT(*) FROM quickpoll_guests
                                    WHERE poll_id = ? AND confirmed IN (1, 2)""",
                                 (poll_id,)).fetchone()[0]
            voters = c.execute("""SELECT username FROM quickpoll_votes
                                  WHERE poll_id = ? AND vote_type = 'in'
                                  ORDER BY voted_at ASC""", (poll_id,)).fetchall()
            player_list = "; ".join(r[0] for r in voters)
            rate_pct = f"{round(in_count / max_players * 100)}%" if max_players and in_count else ""
            total_units = in_count + gc_count
            per_player = f"${field_rate / total_units:.2f}" if field_rate and total_units else ""
            results.append({
                "game_date": d.strftime("%Y-%m-%d"),
                "location": location or "",
                "total_in": in_count,
                "total_out": out_count,
                "total_guests": gc_count,
                "participation_rate": rate_pct,
                "field_rate": f"${field_rate:.2f}" if field_rate else "",
                "per_player_charge": per_player,
                "players": player_list,
            })
        conn.close()

        if not results:
            await self.send(update, f"No closed polls found for {group_name} in that period.")
            return

        results.sort(key=lambda r: r["game_date"])
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["game_date", "location", "total_in", "total_out",
                                                  "total_guests", "participation_rate",
                                                  "field_rate", "per_player_charge", "players"])
        writer.writeheader()
        writer.writerows(results)
        csv_bytes = buf.getvalue().encode('utf-8')
        range_label = parts[0].replace('-', '_') if len(parts) == 1 else f"{parts[0]}_to_{parts[1]}"
        filename = f"poll_report_{group_name}_{range_label}.csv"
        await update.message.reply_document(
            document=io.BytesIO(csv_bytes),
            filename=filename,
            caption=f"📊 {group_name} — {len(results)} game{'s' if len(results) != 1 else ''} — {start_date.strftime('%b %d')} to {end_date.strftime('%b %d, %Y')}"
        )

    async def initchats_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/initchats — Broadcast a DM-invite message to all groups you manage."""
        user_id = update.effective_user.id
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""SELECT cg.chat_id, cg.group_name
                     FROM chat_groups cg
                     JOIN chat_admins ca ON cg.chat_id = ca.chat_id
                     WHERE ca.user_id = ?
                     ORDER BY cg.group_name""", (user_id,))
        groups = c.fetchall()
        conn.close()
        if not groups:
            await self.send(update, "❌ No groups found. Add the bot to a group and run /setchat first.")
            return
        bot_info = await self.application.bot.get_me()
        bot_username = bot_info.username
        msg = (
            "👋 *Game wallet setup*\n\n"
            f"To vote IN on game polls, you need a wallet with the bot.\n\n"
            f"📲 DM @{bot_username} and send /start to get set up — takes 30 seconds.\n\n"
            "Each game costs $10, charged when you vote IN and refunded if you switch to OUT before the deadline."
        )
        sent, failed = 0, 0
        for chat_id, group_name in groups:
            try:
                await self.application.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
                sent += 1
            except Exception as e:
                logger.warning(f"initchats: could not send to {group_name} ({chat_id}): {e}")
                failed += 1
        summary = f"✅ Message sent to {sent} group{'s' if sent != 1 else ''}."
        if failed:
            summary += f" ⚠️ Failed to send to {failed} group{'s' if failed != 1 else ''} (check bot permissions)."
        await self.send(update, summary)

    # ── UX-2: live-roster rendering helpers ───────────────────────────────
    def _esc(self, s) -> str:
        """HTML-escape dynamic text (names, locations) for the roster card."""
        return html.escape(str(s if s is not None else ''), quote=False)

    def _circled(self, n: int) -> str:
        """Pretty circled numeral for roster lines (① ② … up to 50)."""
        if 1 <= n <= 20:
            return chr(0x2460 + n - 1)      # ①..⑳
        if 21 <= n <= 35:
            return chr(0x3251 + n - 21)     # ㉑..㉟
        if 36 <= n <= 50:
            return chr(0x32B1 + n - 36)     # ㊱..㊿
        return f"{n}."

    def _capacity_bar(self, current: int, maximum: int, segments: int = 10) -> str:
        """Unicode progress bar ▰▱ scaled to the IN/max ratio."""
        if not maximum or maximum <= 0:
            return ''
        filled = round(segments * min(current / maximum, 1.0))
        if current > 0 and filled == 0:
            filled = 1                       # show at least a sliver once anyone's IN
        if current < maximum and filled == segments:
            filled = segments - 1            # never look full until actually full
        return '▰' * filled + '▱' * (segments - filled)

    def _pretty_date(self, game_date: str) -> str:
        """2026-06-05 → 'Thursday, Jun 05' (falls back to the raw string)."""
        if not game_date:
            return "TBD"
        try:
            return datetime.strptime(game_date, '%Y-%m-%d').strftime('%A, %b %d')
        except (ValueError, TypeError):
            return game_date

    def _mention(self, name, user_id) -> str:
        """Clickable mention that works even for users without a public @handle."""
        disp = self._esc(name or 'player')
        if user_id:
            return f'<a href="tg://user?id={user_id}">{disp}</a>'
        return disp

    def quickpoll_keyboard(self, poll_id: int) -> InlineKeyboardMarkup:
        """IN/OUT + Guest management buttons for a quickpoll."""
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("IN", callback_data=f"qvote_{poll_id}_in"),
            InlineKeyboardButton("OUT", callback_data=f"qvote_{poll_id}_out"),
            InlineKeyboardButton("+1", callback_data=f"qguest_add_{poll_id}"),
            InlineKeyboardButton("🗑 My Guests", callback_data=f"qguest_remove_{poll_id}"),
        ]])

    def render_quickpoll_message(self, poll_id: int, closed: bool = None) -> str:
        """Build the live-roster card (HTML) for a quickpoll. Single source of
        truth, reused on send and on every vote."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""SELECT location_name, location_link, game_date, time_start,
                            time_end, max_players, deadline_time, closed
                     FROM quickpolls WHERE id = ?""", (poll_id,))
        p = c.fetchone()
        if not p:
            conn.close()
            return None
        (location_name, location_link, game_date, time_start, time_end,
         max_players, deadline_iso, closed_flag) = p
        c.execute("""SELECT username, vote_type, user_id FROM quickpoll_votes
                     WHERE poll_id = ? ORDER BY voted_at ASC, id ASC""", (poll_id,))
        rows = c.fetchall()
        c.execute("""SELECT member_username, guest_name, confirmed FROM quickpoll_guests
                     WHERE poll_id = ? ORDER BY added_at ASC""", (poll_id,))
        guest_rows = c.fetchall()
        conn.close()

        # Closed = explicit flag OR deadline already passed (so the card flips
        # to the closed banner even before the scheduled close event fires)
        if closed is None:
            closed = bool(closed_flag)
            if not closed and deadline_iso:
                try:
                    dl = datetime.fromisoformat(deadline_iso)
                    if dl.tzinfo is None:
                        dl = TZ.localize(dl)
                    if datetime.now(TZ) > dl:
                        closed = True
                except (ValueError, TypeError):
                    pass

        # On a closed poll only show guests that were confirmed (1) or spot-held pending
        # payment (2). Waitlisted guests (0) were not awarded a spot and must not appear.
        if closed:
            visible_guests = [(mu, gn) for mu, gn, conf in guest_rows if conf in (1, 2)]
        else:
            visible_guests = [(mu, gn) for mu, gn, conf in guest_rows]

        # Build inviter → [guest_name, ...] map (order-preserving)
        inviter_guests: dict = {}
        for mu, gname in visible_guests:
            inviter_guests.setdefault(mu.lower() if mu else '', []).append(gname)
        total_guests = len(visible_guests)

        ins = [(u, uid) for (u, vt, uid) in rows if vt == 'in']
        outs = [(u, uid) for (u, vt, uid) in rows if vt == 'out']
        in_count, out_count = len(ins), len(outs)
        max_players = max_players or 0

        lines = ["⚽ <b>Soccer Day</b>"]

        if location_link:
            lines.append(f'📍 <a href="{self._esc(location_link)}">{self._esc(location_name)}</a>')
        else:
            lines.append(f"📍 {self._esc(location_name)}")

        time_part = ''
        if time_start and time_end:
            time_part = f" · {self._esc(time_start)}–{self._esc(time_end)}"
        elif time_start:
            time_part = f" · {self._esc(time_start)}"
        lines.append(f"🕕 {self._esc(self._pretty_date(game_date))}{time_part}")

        if closed:
            lines.append("Voting closed — tap IN/OUT to reach an admin")
        elif deadline_iso:
            try:
                dl = datetime.fromisoformat(deadline_iso)
                lines.append(f"⏳ Closes {dl.strftime('%b %d, %I:%M %p')}")
            except (ValueError, TypeError):
                pass

        if max_players:
            bar = self._capacity_bar(in_count, max_players)
            lines.append("")
            lines.append(f"{bar}   {in_count}/{max_players} max")

        lines.append("")
        guest_note = f" · {total_guests} guest{'s' if total_guests != 1 else ''}" if total_guests else ""
        lines.append(f"✅ <b>IN — {in_count}{guest_note}</b>")
        for i, (name, uid) in enumerate(ins, 1):
            lines.append(f"{self._circled(i)}  {self._mention(name, uid)}")
            for gi, gname in enumerate(inviter_guests.get((name or '').lower(), []), 1):
                lines.append(f"   ↳ Guest {gi}: {self._esc(gname)}")

        lines.append("")
        lines.append(f"❌ <b>OUT — {out_count}</b>")
        for i, (name, uid) in enumerate(outs, 1):
            lines.append(f"{self._circled(i)}  {self._mention(name, uid)}")

        lines.append("")
        lines.append("1- Switch to out before deadline = full refund")
        lines.append("2- Post deadline, exceptions may be made. Reach out to <b>ADMINS</b>")
        lines.append("3- Guests waitlisted — confirmed &amp; charged at close if cap's got room")

        return "\n".join(lines)

    async def _safe_answer(self, query, text: str = None, show_alert: bool = False):
        """Answer a callback query, swallowing the 'query is too old / invalid'
        error Telegram raises once a tap has expired (~15s). Without this, a
        stale answer would throw and abort the rest of the vote handler."""
        try:
            if text is None:
                await query.answer()
            else:
                await query.answer(text, show_alert=show_alert)
        except Exception as e:
            logger.debug(f"Callback answer skipped (stale query?): {e}")

    def schedule_quickpoll_refresh(self, poll_id: int, delay: float = 0.4):
        """Debounced, non-blocking roster refresh.

        Votes only mark the card dirty and return immediately — the actual
        edit_message_text happens here, out of band. A burst of rapid IN/OUT
        taps cancels the prior pending refresh and reschedules, so the whole
        burst collapses into a single edit that renders the final state. This
        keeps the (sequential) update queue from stalling on the network edit
        and avoids tripping Telegram's same-message edit flood control."""
        old = self._refresh_tasks.get(poll_id)
        if old and not old.done():
            old.cancel()

        async def _runner():
            try:
                await asyncio.sleep(delay)
                await self.refresh_quickpoll_message(poll_id)
            except asyncio.CancelledError:
                pass
            finally:
                if self._refresh_tasks.get(poll_id) is asyncio.current_task():
                    self._refresh_tasks.pop(poll_id, None)

        self._refresh_tasks[poll_id] = asyncio.create_task(_runner())

    async def refresh_quickpoll_message(self, poll_id: int):
        """Re-render and edit the pinned roster message in place."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT chat_id, poll_message_id FROM quickpolls WHERE id = ?", (poll_id,))
        row = c.fetchone()
        conn.close()
        if not row or not row[1]:
            return
        chat_id, message_id = row
        text = self.render_quickpoll_message(poll_id)
        if not text:
            return
        try:
            await self.application.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
                parse_mode='HTML', disable_web_page_preview=True,
                reply_markup=self.quickpoll_keyboard(poll_id))
        except Exception as e:
            if 'not modified' not in str(e).lower():
                logger.warning(f"Could not refresh quickpoll message {message_id}: {e}")

    async def close_quickpoll_buttons(self, chat_id: int, message_id):
        """Close a quickpoll: mark it closed and flip the card to the closed
        banner. Buttons stay visible — process_quickpoll_vote gates them so only
        admins/super-admin can still adjust the roster after the deadline."""
        if not message_id:
            return
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT id FROM quickpolls WHERE poll_message_id = ?", (message_id,))
        row = c.fetchone()
        if row:
            c.execute("UPDATE quickpolls SET closed = 1 WHERE id = ?", (row[0],))
            conn.commit()
        conn.close()
        if row:
            await self.refresh_quickpoll_message(row[0])
        else:
            # Legacy poll with no stored id — fall back to removing the buttons
            try:
                await self.application.bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=message_id, reply_markup=None)
            except Exception as e:
                logger.warning(f"Could not close quickpoll buttons ({message_id}): {e}")

    def get_group_admins(self, chat_id: int):
        """Return [(username, user_id), ...] for a group's registered admins."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT DISTINCT username, user_id FROM chat_admins WHERE chat_id = ?", (chat_id,))
        rows = c.fetchall()
        conn.close()
        return rows

    async def dm_closed_poll_contact(self, user_id: int, chat_id: int):
        """After a poll closes, any tap (player, admin, or super-admin) records no
        vote — the tapper just gets a DM pointing them to the group's admin(s).
        The roster is only changed via /addplayer and /removeplayer."""
        mentions = []
        for username, uid in self.get_group_admins(chat_id):
            if uid and username:
                mentions.append(f'<a href="tg://user?id={uid}">@{self._esc(username)}</a>')
            elif username:
                mentions.append(f"@{self._esc(username)}")
        if mentions:
            text = (f"Voting's closed for that game. To get in or out, "
                    f"message an admin: {', '.join(mentions)}")
        else:
            text = "Voting's closed for that game. Please contact a group admin to get in or out."
        try:
            await self.application.bot.send_message(
                chat_id=user_id, text=text, parse_mode='HTML',
                disable_web_page_preview=True)
        except Exception as e:
            logger.warning(f"Could not DM closed-poll contact to {user_id}: {e}")

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query

        # Wallet / top-up callbacks (colon-delimited, handled before the '_' split)
        if query.data.startswith('topup:'):
            await self.handle_topup_callback(query, query.data.split(':', 1)[1])
            return
        if query.data.startswith('ctopup:'):
            await self.confirm_topup(query, float(query.data.split(':', 1)[1]))
            return
        if query.data.startswith('sg:'):
            await self.handle_switchgroup_callback(query, query.data.split(':', 1)[1])
            return
        if query.data.startswith('cqpg:'):
            parts = query.data.split(':')  # cqpg:{chat_id}:{poll_id}
            await self.handle_cancelqp_group_callback(query, parts[1], parts[2])
            return
        if query.data.startswith('prpt_group:'):
            await self.handle_pollreport_group_callback(query, query.data.split(':', 1)[1])
            return
        if query.data.startswith('tapprove:'):
            await self.topup_approve(query, int(query.data.split(':', 1)[1]))
            return
        if query.data.startswith('treject:'):
            await self.topup_reject(query, int(query.data.split(':', 1)[1]))
            return
        # Admin guest-remove prompt after /removeplayer
        if query.data.startswith('qgrm:'):
            parts = query.data.split(':')  # qgrm:all:{poll_id}:{username} or qgrm:keep:...
            action, poll_id_s, username = parts[1], parts[2], parts[3]
            if action == 'all':
                await self.admin_remove_guests(query, int(poll_id_s), username)
            else:
                await self._safe_answer(query, "Guests kept on the waitlist.")
            return

        data = query.data.split('_')

        if data[0] == 'qvote':
            # Quick poll vote
            poll_id = int(data[1])
            vote_type = data[2]
            await self.process_quickpoll_vote(query, poll_id, vote_type)
        elif data[0] == 'qguest':
            poll_id = int(data[2])
            action = data[1]
            if action == 'add':
                await self.guest_add_trigger(query, poll_id)
            elif action == 'remove':
                await self.guest_remove_trigger(query, poll_id)
            elif action == 'clearout':
                await self.guest_clear_on_out(query, poll_id)
            elif action == 'keepout':
                await self._safe_answer(query, "Your guests stay on the waitlist.")
        elif data[0] == 'qstatus':
            # Quick poll status
            poll_id = int(data[1])
            await self.show_quickpoll_status(query, poll_id)

    async def process_quickpoll_vote(self, query, poll_id: int, vote_type: str):
        """Process a vote on a quick poll — enforces the wallet gate and per-vote charge."""
        user = query.from_user
        username = user.username or user.first_name

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        # Create votes table for quickpolls if not exists
        c.execute('''CREATE TABLE IF NOT EXISTS quickpoll_votes (
            id INTEGER PRIMARY KEY, poll_id INTEGER, user_id INTEGER, username TEXT, vote_type TEXT,
            voted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(poll_id, user_id))''')

        # Poll must still exist
        c.execute("SELECT deadline_time, max_players, chat_id, closed FROM quickpolls WHERE id = ?", (poll_id,))
        prow = c.fetchone()
        if not prow:
            conn.close()
            await self._safe_answer(query, "This poll no longer exists.", show_alert=True)
            return
        max_players = prow[1]
        poll_chat_id = prow[2]

        # Closed = explicit flag OR deadline passed. Once closed, the buttons stay
        # visible but only admins/super-admin may still adjust the roster (the
        # "someone got stuck in traffic" override). Regular voters are rejected.
        closed = bool(prow[3])
        if not closed and prow[0]:
            try:
                deadline = datetime.fromisoformat(prow[0])
                if deadline.tzinfo is None:
                    deadline = TZ.localize(deadline)
                if datetime.now(TZ) > deadline:
                    closed = True
            except (ValueError, TypeError):
                pass
        if closed:
            # Closed = nobody votes via buttons (players, admins, super-admin all
            # alike). The tapper gets a DM with the group's admin handles; roster
            # changes only happen through /addplayer and /removeplayer.
            conn.close()
            await self._safe_answer(query)
            await self.dm_closed_poll_contact(user.id, poll_chat_id)
            return

        # Late-arrival block — players blocked from this poll cannot vote
        c.execute("""SELECT 1 FROM late_arrivals
                     WHERE blocked_from_poll_id = ? AND LOWER(username) = LOWER(?)
                     AND cleared_at IS NULL""", (poll_id, username))
        if c.fetchone():
            conn.close()
            await self._safe_answer(
                query,
                "⚠️ You arrived late to the previous game and can't join this poll.",
                show_alert=True)
            return

        # Read the existing vote (drives idempotency + refund detection)
        c.execute("SELECT vote_type FROM quickpoll_votes WHERE poll_id = ? AND user_id = ?",
                  (poll_id, user.id))
        erow = c.fetchone()
        old_vote = erow[0] if erow else None

        # No change — do not re-charge (idempotency)
        if old_vote == vote_type:
            conn.close()
            await self._safe_answer(query, f"You already voted {vote_type.upper()}.")
            return

        # Switching INTO 'in' — enforce the capacity cap, then the wallet gate
        active_waiver_id = None
        if vote_type == 'in':
            c.execute("SELECT COUNT(*) FROM quickpoll_votes WHERE poll_id = ? AND vote_type = 'in'", (poll_id,))
            in_count = c.fetchone()[0]
            if max_players and in_count >= max_players:
                conn.close()
                await self._safe_answer(
                    query,
                    f"⚽ This game is full — all {max_players} spots are taken.",
                    show_alert=True)
                return
            # Check for an active admin-granted waiver before enforcing the wallet gate
            waiver_conn = sqlite3.connect(DB_FILE)
            wc = waiver_conn.cursor()
            wc.execute("SELECT id FROM waivers WHERE LOWER(username) = LOWER(?) AND used = 0 LIMIT 1", (username,))
            waiver_row = wc.fetchone()
            waiver_conn.close()
            if waiver_row:
                active_waiver_id = waiver_row[0]
            else:
                active_waiver_id = None
                eligible, reason = self.check_wallet_eligible(username)
                if not eligible:
                    conn.close()
                    await self._safe_answer(query, reason, show_alert=True)
                    await self.send_topup_prompt(user.id, reason)
                    return

        # Record the vote
        c.execute('INSERT OR REPLACE INTO quickpoll_votes (poll_id, user_id, username, vote_type) VALUES (?, ?, ?, ?)',
                  (poll_id, user.id, username, vote_type))
        conn.commit()
        conn.close()

        # Path B: no charge at vote time — charges happen at close based on
        # field_rate / total headcount. Waiver is still consumed for audit.
        waiver_used = False
        if old_vote != 'in' and vote_type == 'in' and active_waiver_id:
            now = datetime.now(TZ).isoformat()
            wconn = sqlite3.connect(DB_FILE)
            wc2 = wconn.cursor()
            wc2.execute("UPDATE waivers SET used = 1, used_at = ? WHERE id = ?", (now, active_waiver_id))
            wc2.execute("SELECT granted_by FROM waivers WHERE id = ?", (active_waiver_id,))
            grow = wc2.fetchone()
            granted_by = grow[0] if grow and grow[0] else "unknown"
            wc2.execute("SELECT location_name, game_date FROM quickpolls WHERE id = ?", (poll_id,))
            qrow = wc2.fetchone()
            loc = qrow[0] if qrow else "?"
            gdate = qrow[1] if qrow and qrow[1] else "?"
            audit_note = f"waiver:#{poll_id} {gdate} @ {loc} · granted by @{granted_by}"
            wc2.execute("""INSERT INTO payment_confirmations
                          (username, amount, confirmed_date, status, notes)
                          VALUES (?, 0, ?, 'waived', ?)""",
                       (username, now, audit_note))
            wconn.commit()
            wconn.close()
            waiver_used = True

        # Confirmation popup
        if vote_type == 'in':
            if waiver_used:
                await self._safe_answer(query, "✅ You're IN — waiver noted. You'll be charged at close based on the final headcount.")
            else:
                await self._safe_answer(query, "✅ You're IN — you'll be charged at close based on the final headcount.")
        else:
            await self._safe_answer(query, "❌ You're OUT.")

        # UX-4: when switching OUT, prompt about existing guests (fire-and-forget DM)
        if old_vote == 'in' and vote_type == 'out':
            gconn = sqlite3.connect(DB_FILE)
            gc = gconn.cursor()
            gc.execute("SELECT id, guest_name FROM quickpoll_guests WHERE poll_id = ? AND member_user_id = ?",
                       (poll_id, user.id))
            guests = gc.fetchall()
            gconn.close()
            if guests:
                n = len(guests)
                names = ', '.join(g[1] for g in guests)
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("Yes, remove all", callback_data=f"qguest_clearout_{poll_id}"),
                    InlineKeyboardButton("Keep on waitlist", callback_data=f"qguest_keepout_{poll_id}"),
                ]])
                try:
                    await self.application.bot.send_message(
                        chat_id=user.id,
                        text=(f"You voted OUT. You have {n} guest{'s' if n > 1 else ''} on this poll: {names}.\n\n"
                              "Remove them too?"),
                        reply_markup=kb)
                except Exception as e:
                    logger.warning(f"Could not DM guest-remove prompt to {user.id}: {e}")

        # UX-2: redraw the pinned roster card with the new vote.
        # Debounced + non-blocking so rapid IN/OUT toggling doesn't stall the
        # (sequential) update queue or trip Telegram's edit flood control.
        self.schedule_quickpoll_refresh(poll_id)

    async def show_quickpoll_status(self, query, poll_id: int):
        """Show status of a quick poll"""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        c.execute("SELECT max_players FROM quickpolls WHERE id = ?", (poll_id,))
        poll = c.fetchone()
        max_players = poll[0] if poll else 15
        
        c.execute("SELECT vote_type, COUNT(*) FROM quickpoll_votes WHERE poll_id = ? GROUP BY vote_type", (poll_id,))
        counts = dict(c.fetchall())
        
        # Look up this user's vote
        c.execute("SELECT vote_type FROM quickpoll_votes WHERE poll_id = ? AND user_id = ?", (poll_id, query.from_user.id))
        my_vote = c.fetchone()
        conn.close()
        
        in_count = counts.get('in', 0)
        out_count = counts.get('out', 0)

        vote_labels = {'in': '✅ IN', 'out': '❌ OUT'}
        my_vote_str = vote_labels.get(my_vote[0], 'Unknown') if my_vote else 'Not voted yet'

        await query.answer(f"📊 Your vote: {my_vote_str}\n\nIN: {in_count} | OUT: {out_count} | Total: {in_count}/{max_players}", show_alert=True)

    # ── UX-4: Guest system ─────────────────────────────────────────────────

    async def guest_add_trigger(self, query, poll_id: int):
        """➕ Guest button tapped — gate check, wallet eligibility, then DM the user."""
        user = query.from_user
        username = user.username or user.first_name
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        # Gate: must be IN on this poll
        c.execute("SELECT vote_type FROM quickpoll_votes WHERE poll_id = ? AND user_id = ?",
                  (poll_id, user.id))
        row = c.fetchone()
        if not row or row[0] != 'in':
            conn.close()
            await self._safe_answer(query, "You need to vote IN before adding a guest.", show_alert=True)
            return
        # Wallet eligibility: balance - ($10 × (existing_guests + 1)) >= WALLET_FLOOR
        c.execute("SELECT COUNT(*) FROM quickpoll_guests WHERE poll_id = ? AND member_user_id = ?",
                  (poll_id, user.id))
        existing = c.fetchone()[0]
        conn.close()
        wallet = self.get_wallet(username)
        balance = wallet['balance'] if wallet else 0
        if balance - (VOTE_COST * (existing + 1)) < WALLET_FLOOR:
            msg = f"💳 Your balance won't cover another guest — top up to add guests."
            await self._safe_answer(query, msg, show_alert=True)
            await self.send_topup_prompt(user.id, msg)
            return
        # Store pending state and DM the user
        self._pending_guest_add[user.id] = {'poll_id': poll_id}
        await self._safe_answer(query)
        try:
            await self.application.bot.send_message(
                chat_id=user.id,
                text="Enter guest name(s) — separate multiple with commas (e.g. Marco, Sarah, Alex).\nOr /cancel to abort.")
        except Exception as e:
            logger.warning(f"Could not DM guest-add prompt to {user.id}: {e}")
            self._pending_guest_add.pop(user.id, None)

    async def guest_remove_trigger(self, query, poll_id: int):
        """🗑 My Guests button tapped — list guests and ask which to remove."""
        user = query.from_user
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT vote_type FROM quickpoll_votes WHERE poll_id = ? AND user_id = ?",
                  (poll_id, user.id))
        vote_row = c.fetchone()
        if not vote_row or vote_row[0] != 'in':
            conn.close()
            await self._safe_answer(query, "You need to be IN to manage your guests.", show_alert=True)
            return
        c.execute("SELECT id, guest_name FROM quickpoll_guests WHERE poll_id = ? AND member_user_id = ? ORDER BY added_at ASC",
                  (poll_id, user.id))
        guests = c.fetchall()
        conn.close()
        if not guests:
            await self._safe_answer(query, "You have no guests on this poll.", show_alert=True)
            return
        self._pending_guest_remove[user.id] = {'poll_id': poll_id, 'guests': list(guests)}
        await self._safe_answer(query)
        lines = ["Your guests on this poll:"]
        for i, (gid, gname) in enumerate(guests, 1):
            lines.append(f"{i}. {gname}")
        lines.append("\nReply with the number to remove (or /cancel to abort).")
        try:
            await self.application.bot.send_message(chat_id=user.id, text="\n".join(lines))
        except Exception as e:
            logger.warning(f"Could not DM guest-remove list to {user.id}: {e}")
            self._pending_guest_remove.pop(user.id, None)

    async def _handle_guest_add_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Private DM reply after +1 — parse comma-separated names, wallet-check for N, insert all."""
        user = update.effective_user
        pending = self._pending_guest_add.pop(user.id, None)
        if not pending:
            return
        poll_id = pending['poll_id']
        raw = update.message.text.strip()
        names = [n.strip() for n in raw.split(',') if n.strip()]
        if not names:
            await self.send(update, "❌ No names found. Try again or /cancel to abort.")
            self._pending_guest_add[user.id] = pending  # put back
            return
        bad = [n for n in names if len(n) > 60]
        if bad:
            await self.send(update, f"❌ Name too long (max 60 chars): {bad[0]}. Try again or /cancel to abort.")
            self._pending_guest_add[user.id] = pending  # put back
            return
        username = user.username or user.first_name
        # Re-check wallet eligibility for this many new guests
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM quickpoll_guests WHERE poll_id = ? AND member_user_id = ?",
                  (poll_id, user.id))
        existing = c.fetchone()[0]
        conn.close()
        wallet = self.get_wallet(username)
        balance = wallet['balance'] if wallet else 0
        if balance - (VOTE_COST * (existing + len(names))) < WALLET_FLOOR:
            await self.send(update, f"💳 Your balance won't cover {len(names)} guest(s) — top up and try again.")
            return
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        for name in names:
            c.execute("""INSERT INTO quickpoll_guests (poll_id, member_user_id, member_username, guest_name)
                         VALUES (?, ?, ?, ?)""", (poll_id, user.id, username, name))
        conn.commit()
        conn.close()
        if len(names) == 1:
            await self.send(update, f"✅ \"{names[0]}\" added as your guest.")
        else:
            listed = "\n".join(f"• {n}" for n in names)
            await self.send(update, f"✅ {len(names)} guests added:\n{listed}")
        self.schedule_quickpoll_refresh(poll_id)

    async def _handle_guest_remove_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Private DM reply after 🗑 My Guests — delete the chosen guest row."""
        user = update.effective_user
        pending = self._pending_guest_remove.pop(user.id, None)
        if not pending:
            return
        poll_id = pending['poll_id']
        guests = pending['guests']  # [(id, name), ...]
        try:
            idx = int(update.message.text.strip()) - 1
            if not (0 <= idx < len(guests)):
                raise ValueError
        except ValueError:
            await self.send(update, f"❌ Reply with a number between 1 and {len(guests)}, or /cancel.")
            self._pending_guest_remove[user.id] = pending  # put back
            return
        gid, gname = guests[idx]
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM quickpoll_guests WHERE id = ?", (gid,))
        conn.commit()
        conn.close()
        await self.send(update, f"✅ \"{gname}\" removed from your guests.")
        self.schedule_quickpoll_refresh(poll_id)

    async def guest_clear_on_out(self, query, poll_id: int):
        """User voted OUT and confirmed removing all their guests."""
        user = query.from_user
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM quickpoll_guests WHERE poll_id = ? AND member_user_id = ?",
                  (poll_id, user.id))
        conn.commit()
        conn.close()
        await self._safe_answer(query, "✅ Your guests have been removed.")
        self.schedule_quickpoll_refresh(poll_id)

    async def admin_remove_guests(self, query, poll_id: int, username: str):
        """Admin confirmed removing all guests belonging to a removed player."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM quickpoll_guests WHERE poll_id = ? AND LOWER(member_username) = LOWER(?)",
                  (poll_id, username))
        deleted = c.rowcount
        conn.commit()
        conn.close()
        await self._safe_answer(query)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await self.application.bot.send_message(
            chat_id=query.from_user.id,
            text=f"✅ {deleted} guest(s) for @{username} removed.")
        self.schedule_quickpoll_refresh(poll_id)

    async def confirm_quickpoll_guests(self, poll_id: int, chat_id: int):
        """At close: confirm guest spots, then charge all IN players based on
        field_rate / total units (Path B — no upfront charge)."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT max_players, field_rate FROM quickpolls WHERE id = ?", (poll_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return
        max_players, field_rate = row
        max_players = max_players or 0

        c.execute("SELECT COUNT(*) FROM quickpoll_votes WHERE poll_id = ? AND vote_type = 'in'", (poll_id,))
        in_count = c.fetchone()[0]
        remaining = max(0, max_players - in_count) if max_players else 999

        # --- Step 1: Allocate guest spots (no money yet) ---
        c.execute("""SELECT id, member_username, member_user_id, guest_name
                     FROM quickpoll_guests WHERE poll_id = ? AND confirmed = 0
                     ORDER BY added_at ASC""", (poll_id,))
        guests = c.fetchall()
        conn.close()

        spots_used = 0
        for gid, member_username, member_user_id, guest_name in guests:
            vc = sqlite3.connect(DB_FILE)
            vcc = vc.cursor()
            vcc.execute("SELECT vote_type FROM quickpoll_votes WHERE poll_id = ? AND LOWER(username) = LOWER(?)",
                        (poll_id, member_username))
            vote_row = vcc.fetchone()
            vc.close()
            if not vote_row or vote_row[0] != 'in':
                dc = sqlite3.connect(DB_FILE)
                dcc = dc.cursor()
                dcc.execute("DELETE FROM quickpoll_guests WHERE id = ?", (gid,))
                dc.commit()
                dc.close()
                continue
            if spots_used >= remaining:
                continue  # No spot — leave waitlisted
            uc = sqlite3.connect(DB_FILE)
            ucc = uc.cursor()
            ucc.execute("UPDATE quickpoll_guests SET confirmed = 1 WHERE id = ?", (gid,))
            uc.commit()
            uc.close()
            spots_used += 1

        # --- Step 2: Charge all IN players ---
        if not field_rate:
            if SUPER_ADMIN_ID:
                try:
                    await self.application.bot.send_message(
                        chat_id=SUPER_ADMIN_ID,
                        text=f"ℹ️ Poll #{poll_id} closed — no field rate was set, so no charges were made.")
                except Exception:
                    pass
            return

        gc = sqlite3.connect(DB_FILE)
        gcc = gc.cursor()
        gcc.execute("SELECT COUNT(*) FROM quickpoll_guests WHERE poll_id = ? AND confirmed IN (1, 2)", (poll_id,))
        total_confirmed_guests = gcc.fetchone()[0]
        gcc.execute("SELECT username, user_id FROM quickpoll_votes WHERE poll_id = ? AND vote_type = 'in'", (poll_id,))
        in_voters = gcc.fetchall()
        gc.close()

        total_units = len(in_voters) + total_confirmed_guests
        if total_units == 0:
            return
        per_unit = round(field_rate / total_units, 2)

        charged_ok = []
        shortfalls = []

        for voter_username, voter_user_id in in_voters:
            # Waiver holders pay nothing at close
            wc = sqlite3.connect(DB_FILE)
            wcc = wc.cursor()
            wcc.execute("""SELECT 1 FROM payment_confirmations
                           WHERE LOWER(username) = LOWER(?) AND notes LIKE ? AND status = 'waived'""",
                        (voter_username, f"waiver:#{poll_id}%"))
            is_waived = wcc.fetchone() is not None
            wc.close()
            if is_waived:
                continue

            gfc = sqlite3.connect(DB_FILE)
            gfcc = gfc.cursor()
            gfcc.execute("""SELECT COUNT(*) FROM quickpoll_guests
                            WHERE poll_id = ? AND confirmed IN (1, 2) AND LOWER(member_username) = LOWER(?)""",
                         (poll_id, voter_username))
            guest_count = gfcc.fetchone()[0]
            gfc.close()

            amount_due = round(per_unit * (1 + guest_count), 2)
            wallet = self.get_wallet(voter_username)
            balance = wallet['balance'] if wallet else 0

            if balance >= amount_due:
                self.deduct_wallet(voter_username, amount_due, f"quickpoll_vote:{poll_id}")
                charged_ok.append((voter_username, amount_due, guest_count))
            else:
                shortfall = round(amount_due - balance, 2)
                shortfalls.append((voter_username, voter_user_id, amount_due, guest_count, shortfall))
                now_iso = datetime.now(TZ).isoformat()
                sc = sqlite3.connect(DB_FILE)
                scc = sc.cursor()
                scc.execute("""INSERT INTO payment_confirmations
                               (username, amount, payment_date, confirmed_date, status, notes)
                               VALUES (?, ?, ?, ?, 'pending', ?)""",
                            (voter_username, -amount_due, now_iso, now_iso, f"quickpoll_vote:{poll_id}:shortfall"))
                sc.commit()
                sc.close()

        # DM players with shortfalls
        for voter_username, voter_user_id, amount_due, guest_count, shortfall in shortfalls:
            if voter_user_id:
                try:
                    guest_note = f" (covering you + {guest_count} guest{'s' if guest_count > 1 else ''})" if guest_count else ""
                    await self.application.bot.send_message(
                        chat_id=voter_user_id,
                        text=(f"⚠️ Your share for today's game was ${amount_due:.2f}{guest_note}, "
                              f"but your wallet only has ${(amount_due - shortfall):.2f}. "
                              f"You're short ${shortfall:.2f}. Please top up to cover the balance."))
                except Exception as e:
                    logger.warning(f"Could not DM shortfall notice to {voter_user_id}: {e}")

        # DM super-admin summary
        if SUPER_ADMIN_ID and (charged_ok or shortfalls):
            lines = [f"💰 Poll #{poll_id} closed — ${per_unit:.2f}/unit ({total_units} units, ${field_rate:.2f} field):"]
            if charged_ok:
                lines.append(f"\n✅ Charged ({len(charged_ok)}):")
                for u, amt, gc_count in charged_ok:
                    g = f" +{gc_count}g" if gc_count else ""
                    lines.append(f"  • @{u}{g}: ${amt:.2f}")
            if shortfalls:
                lines.append(f"\n⚠️ Shortfalls ({len(shortfalls)}):")
                for u, uid, amt, gc_count, sf in shortfalls:
                    g = f" +{gc_count}g" if gc_count else ""
                    lines.append(f"  • @{u}{g}: owes ${amt:.2f} (short ${sf:.2f})")
            try:
                await self.application.bot.send_message(
                    chat_id=SUPER_ADMIN_ID, text="\n".join(lines))
            except Exception as e:
                logger.warning(f"Could not DM admin close summary: {e}")

    async def switchgroup_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show all registered groups as inline buttons; tapping one sets the active group."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT chat_id, group_name FROM chat_groups ORDER BY group_name")
        groups = c.fetchall()
        conn.close()
        if not groups:
            await self.send(update, "❌ No groups registered yet. Add the bot to a group first.")
            return
        if len(groups) == 1:
            self.set_admin_target_chat(update.effective_user.id, groups[0][0])
            await self.send(update, f"✅ Active group: *{groups[0][1]}*", parse_mode='Markdown')
            return
        buttons = [[InlineKeyboardButton(name, callback_data=f"sg:{chat_id}")]
                   for chat_id, name in groups]
        await self.send(update, "Choose your active group:",
                        reply_markup=InlineKeyboardMarkup(buttons))

    async def handle_switchgroup_callback(self, query, chat_id_str: str):
        """Handle inline button tap from /switchgroup."""
        try:
            chat_id = int(chat_id_str)
        except ValueError:
            await query.answer("Invalid group.")
            return
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT group_name FROM chat_groups WHERE chat_id = ?", (chat_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            await query.answer("Group not found.")
            return
        self.set_admin_target_chat(query.from_user.id, chat_id)
        await query.edit_message_text(f"✅ Active group: *{row[0]}*\n\nAll commands now target this group.",
                                      parse_mode='Markdown')
        await query.answer()

    async def mygroups_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List all registered groups, marking the admin's active one."""
        user_id = update.effective_user.id
        active_chat_id, _ = self.get_admin_target_chat(user_id)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT chat_id, group_name FROM chat_groups ORDER BY group_name")
        groups = c.fetchall()
        conn.close()
        if not groups:
            await self.send(update, "❌ No groups registered yet.")
            return
        lines = []
        for cid, name in groups:
            marker = "✅" if cid == active_chat_id else "•"
            lines.append(f"{marker} *{name}*")
        text = "📋 *Your groups:*\n\n" + "\n".join(lines)
        if len(groups) > 1:
            text += "\n\n/switchgroup to change target"
        await self.send(update, text, parse_mode='Markdown')

    async def addadmin_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add an admin across all groups: /addadmin @username or /addadmin user_id"""
        caller = update.effective_user
        if not self._role_for(caller) in ('super', 'admin'):
            await self.send(update, "❌ You are not authorized to use this command.")
            return

        if not context.args:
            await self.send(update, "Usage: /addadmin @username or /addadmin <user_id>")
            return

        arg = context.args[0].lstrip('@')
        try:
            new_admin_id = int(arg)
            username = None
        except ValueError:
            new_admin_id = None
            username = arg

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT chat_id FROM chat_groups")
        all_groups = [r[0] for r in c.fetchall()]
        if not all_groups:
            conn.close()
            await self.send(update, "❌ No groups registered yet. Add the bot to a group first.")
            return

        for gid in all_groups:
            c.execute("INSERT OR IGNORE INTO chat_admins (chat_id, user_id, username, added_by) VALUES (?, ?, ?, ?)",
                      (gid, new_admin_id, username, caller.id))
        conn.commit()
        conn.close()
        await self.refresh_command_scopes()

        safe = (username or str(new_admin_id)).replace('_', '\\_')
        await self.send(update, f"✅ Added admin: {safe} (access to all {len(all_groups)} group(s))")

    async def removeadmin_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remove an admin (from all groups): /removeadmin @username or /removeadmin user_id"""
        caller = update.effective_user
        if not self._role_for(caller) in ('super', 'admin'):
            await self.send(update, "❌ You are not authorized to use this command.")
            return

        if not context.args:
            await self.send(update, "Usage: /removeadmin @username or /removeadmin <user_id>")
            return

        arg = context.args[0].lstrip('@')

        # Super admin can never be removed
        if SUPER_ADMIN_ID:
            if arg == str(SUPER_ADMIN_ID):
                await self.send(update, "❌ You can't remove the super admin.")
                return

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        # Resolve target user_id from arg
        try:
            target_id = int(arg)
            target_username = None
        except ValueError:
            target_id = None
            target_username = arg

        # Block removing super admin by username
        if target_id == SUPER_ADMIN_ID:
            conn.close()
            await self.send(update, "❌ You can't remove the super admin.")
            return

        # Non-super admins can only remove admins they themselves added
        if not self.is_super_admin(caller.id):
            if target_id:
                c.execute("SELECT added_by FROM chat_admins WHERE user_id = ? LIMIT 1", (target_id,))
            else:
                c.execute("SELECT added_by FROM chat_admins WHERE LOWER(username) = LOWER(?) LIMIT 1", (target_username,))
            row = c.fetchone()
            if not row or row[0] != caller.id:
                conn.close()
                await self.send(update, "❌ You can only remove admins that you added.")
                return

        # Delete from all groups
        if target_id:
            c.execute("DELETE FROM chat_admins WHERE user_id = ? AND user_id != ?", (target_id, SUPER_ADMIN_ID or -1))
        else:
            c.execute("DELETE FROM chat_admins WHERE LOWER(username) = LOWER(?) AND (user_id IS NULL OR user_id != ?)",
                      (target_username, SUPER_ADMIN_ID or -1))

        if c.rowcount == 0:
            conn.close()
            safe_arg = arg.replace('_', '\\_')
            await self.send(update, f"❌ No admin found with that username/ID: {safe_arg}")
            return

        conn.commit()
        conn.close()
        await self.refresh_command_scopes()

        safe_arg = arg.replace('_', '\\_')
        await self.send(update, f"✅ Removed admin: {safe_arg}")

    async def listadmins_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List all admins for the current chat"""
        # Any admin can view the admin list.
        if not self._role_for(update.effective_user) in ('super', 'admin'):
            await self.send(update, "❌ You are not authorized to use this command.")
            return
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT DISTINCT user_id, username, added_by FROM chat_admins WHERE user_id != ? OR user_id IS NULL",
                  (SUPER_ADMIN_ID or -1,))
        admins = c.fetchall()
        conn.close()

        if not admins:
            await self.send(update, "No admins set yet. Use /addadmin to add one.")
            return

        text = "*🔐 Admins:*\n"
        for uid, username, added_by in admins:
            label = (username or '').replace('_', '\\_') or '(no username)'
            suffix = f" (ID: {uid})" if uid and uid != 0 else ""
            text += f"• {label}{suffix}\n"

        await self.send(update, text, parse_mode='Markdown')

    async def listchats_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List all groups the user is admin for"""
        user_id = update.effective_user.id
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""SELECT cg.chat_id, cg.group_name
                     FROM chat_groups cg
                     JOIN chat_admins ca ON cg.chat_id = ca.chat_id
                     WHERE ca.user_id = ?
                     ORDER BY cg.group_name""", (user_id,))
        groups = c.fetchall()
        conn.close()
        
        if not groups:
            await self.send(update, "📋 You're not admin for any groups yet. Add the bot to a group to get started.")
            return
        
        text = f"*📋 Your Groups ({len(groups)}):*\n\n"
        for chat_id, group_name in groups:
            text += f"• *{group_name}*\n  ID: `{chat_id}`\n"
        
        await self.send(update, text)

    async def setskill_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set a player's skill rating: /setskill Name 7"""
        if len(context.args) < 2:
            await self.send(update, "Usage: /setskill Name Rating (1-10)")
            return
        
        try:
            rating = int(context.args[-1])
            if not 1 <= rating <= 10: raise ValueError
        except ValueError:
            await self.send(update, "Rating must be a number between 1-10")
            return

        username = ' '.join(context.args[:-1]).lstrip('@')
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO skills (username, skill_rating, last_activity) VALUES (?, ?, ?)',
                  (username, rating, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        safe_username = username.replace('_', '\\_')
        await self.send(update, f"✅ Set {safe_username} to skill {rating}")

    async def skills_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show all rated players: /skills"""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT username, skill_rating FROM skills ORDER BY skill_rating DESC")
        skills = c.fetchall()
        conn.close()
        
        if not skills:
            await self.send(update, "No players rated yet. Use /setskill Name Rating")
            return
            
        msg = "*🏆 Player Ratings:*\n"
        for username, rating in skills:
            stars = '⭐' * rating
            safe_username = username.replace('_', '\\_')
            msg += f"• {safe_username}: {stars} ({rating})\n"
            
        await self.send(update, msg)

    async def deleteskill_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Delete a player's skill rating: /deleteskill Name"""
        if not context.args:
            await self.send(update, "Usage: /deleteskill Name")
            return
            
        username = ' '.join(context.args).lstrip('@')
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM skills WHERE username = ?", (username,))
        deleted = c.rowcount > 0
        conn.commit()
        conn.close()
        
        if deleted:
            safe_username = username.replace('_', '\\_')
            await self.send(update, f"🗑️ Deleted rating for {safe_username}")
        else:
            await self.send(update, f"❌ Player not found")

    # ===== LATE ARRIVALS COMMANDS =====

    async def viewlate_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """View late arrivals for a poll: /viewlate [poll_id]"""
        if not context.args:
            # Show most recent
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT id FROM late_arrivals WHERE cleared_at IS NULL ORDER BY added_at DESC LIMIT 1")
            result = c.fetchone()
            conn.close()
            if not result:
                await self.send(update, "❌ No active late arrivals found.")
                return
            poll_id = result[0]
        else:
            try:
                poll_id = int(context.args[0])
            except ValueError:
                await self.send(update, "Usage: /viewlate [poll_id]")
                return
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""SELECT username, blocked_from_poll_id, cleared_at 
                     FROM late_arrivals WHERE poll_id = ? ORDER BY added_at ASC""",
                  (poll_id,))
        late_arrivals = c.fetchall()
        conn.close()
        
        if not late_arrivals:
            await self.send(update, f"📋 No late arrivals for poll {poll_id}")
            return
        
        msg = f"⏰ *Late arrivals for poll {poll_id}:*\n\n"
        for username, blocked_poll_id, cleared_at in late_arrivals:
            safe_name = username.replace('_', '\\_')
            status = "cleared" if cleared_at else ("next poll" if blocked_poll_id else "pending")
            msg += f"• @{safe_name} ({status})\n"
        
        await self.send(update, msg, parse_mode='Markdown')

    async def addlate_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add a late arrival: /addlate poll_id username"""
        if len(context.args) < 2:
            await self.send(update, "Usage: /addlate poll_id username")
            return
        
        try:
            poll_id = int(context.args[0])
        except ValueError:
            await self.send(update, "First argument must be poll_id (number)")
            return
        
        username = ' '.join(context.args[1:]).lstrip('@')
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        try:
            c.execute("""INSERT INTO late_arrivals (poll_id, user_id, username, added_by_admin_id, added_at)
                         VALUES (?, ?, ?, ?, ?)""",
                      (poll_id, None, username, update.effective_user.id, datetime.now(TZ).isoformat()))
            conn.commit()
            safe_name = username.replace('_', '\\_')
            await self.send(update, f"✅ Added @{safe_name} to late arrivals for poll {poll_id}")
        except sqlite3.IntegrityError:
            safe_name = username.replace('_', '\\_')
            await self.send(update, f"⚠️ @{safe_name} already in late arrivals for poll {poll_id}")
        finally:
            conn.close()

    async def removelate_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remove a late arrival: /removelate poll_id username"""
        if len(context.args) < 2:
            await self.send(update, "Usage: /removelate poll_id username")
            return
        
        try:
            poll_id = int(context.args[0])
        except ValueError:
            await self.send(update, "First argument must be poll_id (number)")
            return
        
        username = ' '.join(context.args[1:]).lstrip('@')
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM late_arrivals WHERE poll_id = ? AND username = ?", (poll_id, username))
        deleted = c.rowcount > 0
        conn.commit()
        conn.close()
        
        if deleted:
            safe_name = username.replace('_', '\\_')
            await self.send(update, f"✅ Removed @{safe_name} from late arrivals for poll {poll_id}")
        else:
            safe_name = username.replace('_', '\\_')
            await self.send(update, f"❌ @{safe_name} not found in poll {poll_id}")

    async def clearlate_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mark all late arrivals for a poll as cleared: /clearlate poll_id"""
        if not context.args:
            await self.send(update, "Usage: /clearlate poll_id")
            return
        
        try:
            poll_id = int(context.args[0])
        except ValueError:
            await self.send(update, "Usage: /clearlate poll_id")
            return
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""UPDATE late_arrivals SET cleared_at = ? WHERE poll_id = ? AND cleared_at IS NULL""",
                  (datetime.now(TZ).isoformat(), poll_id))
        updated = c.rowcount
        conn.commit()
        conn.close()
        
        await self.send(update, f"✅ Cleared {updated} late arrival records for poll {poll_id}")

    async def maketeams_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Create balanced teams: /maketeams [all] (uses poll settings)"""
        # Check admin authorization
        is_admin, _ = await self.check_admin(update)
        if not is_admin:
            await self.send(update, "❌ You are not authorized to use this command.")
            return
        
        override_num = None
        use_all = False
        
        # Parse arguments
        for arg in context.args:
            if arg.isdigit():
                n = int(arg)
                if n >= 2: override_num = n
            elif arg.lower() == 'all':
                use_all = True
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        players_with_skills = []
        poll_num_teams = 2 # Default
        
        if use_all:
            # Source 1: All rated players
            c.execute("SELECT username, skill_rating FROM skills ORDER BY skill_rating DESC")
            rated_players = c.fetchall()
            players_with_skills = [{'username': username, 'skill': rating} for username, rating in rated_players]
            
            if not players_with_skills:
                await self.send(update, "❌ No rated players found. Use `/setskill` first.", parse_mode='Markdown')
                conn.close()
                return

        else:
            # Source 2: Latest Quickpoll (Default)
            # Try to get num_teams from the poll
            try:
                c.execute("SELECT id, num_teams FROM quickpolls ORDER BY created_at DESC LIMIT 1")
                poll = c.fetchone()
            except sqlite3.OperationalError:
                # Fallback if num_teams column missing (old schema)
                c.execute("SELECT id FROM quickpolls ORDER BY created_at DESC LIMIT 1")
                row = c.fetchone()
                poll = (row[0], 2) if row else None
            
            if not poll:
                await self.send(update, "❌ No quickpoll found. Use `/quickpoll` to start one, or `/maketeams all`.", parse_mode='Markdown')
                conn.close()
                return
            
            poll_id = poll[0]
            poll_num_teams = poll[1]
            
            # Get IN voters
            c.execute("SELECT user_id, username FROM quickpoll_votes WHERE poll_id = ? AND vote_type = 'in'", (poll_id,))
            in_voters = c.fetchall()

            all_players = in_voters
            
            if not all_players:
                await self.send(update, "❌ No players voted IN yet. Use `/maketeams all`.", parse_mode='Markdown')
                conn.close()
                return

            # Fetch skills for these voters
            for _, username in all_players:
                c.execute("SELECT skill_rating FROM skills WHERE LOWER(username) = LOWER(?)", (username,))
                skill = c.fetchone()
                rating = skill[0] if skill else 3
                players_with_skills.append({'username': username, 'skill': rating})
        
        conn.close()
        
        # Use override if provided, else use poll setting (or default 2)
        num_teams = override_num if override_num else poll_num_teams
        
        if len(players_with_skills) < num_teams:
            await self.send(update, f"❌ Not enough players ({len(players_with_skills)}) for {num_teams} teams.")
            return
        
        # Balance teams using greedy algorithm
        teams = self.balance_teams(players_with_skills, num_teams)

        chat_id, group_name = self.get_admin_target_chat(update.effective_user.id)
        if not chat_id:
            await self.send(update, "❌ No active group set. Run /switchgroup first.")
            return
        
        # Build and send message
        msg = "⚽ *Teams for Today's Game*\n\n"
        for i, team in enumerate(teams, 1):
            total_skill = sum(p['skill'] for p in team)
            msg += f"🏆 *Team {i}* (skill: {total_skill})\n"
            for p in team:
                stars = '⭐' * p['skill']
                safe_name = p['username'].replace('_', '\\_')
                msg += f"• @{safe_name} {stars}\n"
            msg += "\n"
        
        msg += "Good luck! 🎉"
        
        await self.application.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
        await self.send(update, "✅ Teams posted to the group!")

    def balance_teams(self, players: list, num_teams: int) -> list:
        """Greedy algorithm: sort by skill, assign each to lowest-total team"""
        # Sort players by skill (highest first)
        sorted_players = sorted(players, key=lambda p: p['skill'], reverse=True)
        
        # Initialize teams
        teams = [[] for _ in range(num_teams)]
        team_totals = [0] * num_teams
        
        # Assign each player to the team with lowest total skill
        for player in sorted_players:
            min_idx = team_totals.index(min(team_totals))
            teams[min_idx].append(player)
            team_totals[min_idx] += player['skill']
        
        return teams

    def escape_markdown(self, text: str) -> str:
        """Helper to escape Markdown special characters"""
        escape_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in escape_chars:
            text = text.replace(char, f"\\{char}")
        return text

    async def handle_bot_added_to_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Fires when the bot's membership status changes in a chat.
        Auto-registers the group when the bot is added."""
        result = update.my_chat_member
        if not result:
            return
        new_status = result.new_chat_member.status
        if new_status not in ('member', 'administrator'):
            return
        chat = result.chat
        if chat.type not in ('group', 'supergroup'):
            return

        chat_id = chat.id
        group_name = chat.title or f"Group{abs(chat_id) % 10000}"

        added_by = result.from_user  # person who added the bot

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO chat_groups (chat_id, group_name) VALUES (?, ?)",
                  (chat_id, group_name))
        # Auto-register super-admin as admin for this group
        if SUPER_ADMIN_ID:
            c.execute("INSERT OR IGNORE INTO chat_admins (chat_id, user_id, username) VALUES (?, ?, ?)",
                      (chat_id, SUPER_ADMIN_ID, None))
        conn.commit()
        conn.close()

        # DM the super-admin
        if SUPER_ADMIN_ID:
            try:
                await context.bot.send_message(
                    chat_id=SUPER_ADMIN_ID,
                    text=f"✅ Auto-registered group *{self.escape_markdown(group_name)}* (`{chat_id}`)",
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.warning(f"Could not DM super-admin on group join: {e}")

    async def set_chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Super-admin escape hatch: /setchat <chat_id> <GroupName>"""
        if not self.is_super_admin(update.effective_user.id):
            await self.send(update, "❌ Not allowed.")
            return

        if not context.args or len(context.args) < 2:
            await self.send(update, "Usage: /setchat <chat_id> <GroupName>")
            return

        try:
            chat_id = int(context.args[0])
            group_name = ' '.join(context.args[1:])
        except ValueError:
            await self.send(update, "Invalid chat_id. Usage: /setchat <chat_id> <GroupName>")
            return

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO chat_groups (chat_id, group_name) VALUES (?, ?)",
                  (chat_id, group_name))
        conn.commit()
        conn.close()

        group_name_escaped = self.escape_markdown(group_name)
        await self.send(update, f"✅ Group '{group_name_escaped}' registered\n📱 ID: `{chat_id}`")


    # ===== QUICK POLL (no season required) =====
    
    async def quickpoll_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start quick poll setup"""
        # Check admin authorization
        is_admin, chat_id = await self.check_admin(update)
        if not is_admin:
            await self.send(update, "❌ You are not authorized to use this command.")
            return ConversationHandler.END

        context.user_data['qp'] = {}
        context.user_data['qp']['admin_id'] = update.effective_user.id

        # Get list of groups this admin manages
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            SELECT DISTINCT cg.chat_id, cg.group_name
            FROM chat_admins ca
            JOIN chat_groups cg ON ca.chat_id = cg.chat_id
            WHERE ca.user_id = ?
            ORDER BY cg.group_name
        """, (update.effective_user.id,))
        groups = c.fetchall()
        conn.close()

        if not groups:
            await self.send(update, "❌ No groups registered yet. Add the bot to a group first.")
            return ConversationHandler.END

        # Store groups for next step
        context.user_data['qp']['available_groups'] = groups

        # Ask user to pick a group
        group_list = "\n".join([f"{i+1}. {self.escape_markdown(name)}" for i, (_, name) in enumerate(groups)])
        await self.send(update, f"⚡ *Quick Poll Setup*\n\nStep 1/10: Which group?\n\n{group_list}\n\nReply with the *number*:", parse_mode='Markdown')
        return QP_GROUP_SELECT

    async def qp_get_group_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Get group selection from user"""
        try:
            choice = int(update.message.text.strip()) - 1
            groups = context.user_data['qp']['available_groups']
            if choice < 0 or choice >= len(groups):
                await self.send(update, f"❌ Invalid choice. Pick 1–{len(groups)}.")
                return QP_GROUP_SELECT

            selected_chat_id, selected_name = groups[choice]
            context.user_data['qp']['target_chat_id'] = selected_chat_id
            context.user_data['qp']['target_group_name'] = selected_name
            self.set_admin_target_chat(update.effective_user.id, selected_chat_id)

        except ValueError:
            await self.send(update, "❌ Please enter a valid number.")
            return QP_GROUP_SELECT

        # Check for a previous poll on this chat to offer pre-fill
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""SELECT location_name, location_link, game_date, time_start, time_end,
                            max_players, num_teams
                       FROM quickpolls WHERE chat_id = ?
                       ORDER BY created_at DESC LIMIT 1""", (selected_chat_id,))
        prev = c.fetchone()
        conn.close()

        if prev:
            context.user_data['qp']['prev'] = {
                'location_name': prev[0],
                'location_link': prev[1],
                'date': prev[2],
                'time_start': prev[3],
                'time_end': prev[4],
                'max_players': prev[5],
                'num_teams': prev[6],
            }
            # Suggest next date (+7 days from last game)
            try:
                from datetime import timedelta as _td
                next_date = (datetime.strptime(prev[2], '%Y-%m-%d') + _td(days=7)).strftime('%Y-%m-%d')
            except Exception:
                next_date = prev[2]
            context.user_data['qp']['prev']['next_date'] = next_date

            keyboard = [
                [InlineKeyboardButton("📋 Reuse last poll (same details)", callback_data="qp_use_last")],
                [InlineKeyboardButton("✏️ Start from last poll (edit fields)", callback_data="qp_edit_last")],
                [InlineKeyboardButton("🆕 Fresh start", callback_data="qp_fresh")],
            ]
            summary = (
                f"📌 *Last poll for {self.escape_markdown(selected_name)}:*\n"
                f"📍 {self.escape_markdown(str(prev[0]))}\n"
                f"🗓 {prev[2]} | {prev[3]}–{prev[4]}\n"
                f"👥 Max: {prev[5]}\n\n"
                f"What do you want to do?"
            )
            await self.send(update, summary, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            return QP_REPEAT_CHECK

        await self.send(update, "Step 2/9: Enter *location name*:", parse_mode='Markdown')
        return QP_LOCATION_NAME

    async def qp_repeat_use_last(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Pre-fill all fields from prev poll, advance date by 7 days, go straight to final."""
        await update.callback_query.answer()
        prev = context.user_data['qp'].get('prev', {})
        qp = context.user_data['qp']
        qp['location_name'] = prev['location_name']
        qp['location_link'] = prev['location_link']
        qp['date'] = prev.get('next_date', prev['date'])
        qp['time_start'] = prev['time_start']
        qp['time_end'] = prev['time_end']
        qp['max_players'] = prev['max_players']
        qp['num_teams'] = prev['num_teams']
        # We still need deadline and auto_teams — ask for deadline next
        await self.send(update, f"✅ Fields pre-filled. Date set to *{qp['date']}*.\n\nStep: How many hours until the deadline? (e.g., 24):", parse_mode='Markdown')
        return QP_DEADLINE

    async def qp_repeat_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Pre-fill fields from prev but let admin edit them one by one."""
        await update.callback_query.answer()
        prev = context.user_data['qp'].get('prev', {})
        qp = context.user_data['qp']
        qp['location_name'] = prev['location_name']
        qp['location_link'] = prev['location_link']
        qp['date'] = prev.get('next_date', prev['date'])
        qp['time_start'] = prev['time_start']
        qp['time_end'] = prev['time_end']
        qp['max_players'] = prev['max_players']
        qp['num_teams'] = prev['num_teams']
        qp['edit_mode'] = True
        await self.send(
            update,
            f"✏️ *Edit mode* — send a new value or *.* to keep the current one.\n\n"
            f"Step 2/9: Location name\nCurrent: *{prev['location_name']}*",
            parse_mode='Markdown'
        )
        return QP_LOCATION_NAME

    async def qp_repeat_fresh(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ignore prev poll data, start fresh."""
        await update.callback_query.answer()
        context.user_data['qp'].pop('prev', None)
        context.user_data['qp'].pop('edit_mode', None)
        await self.send(update, "Step 2/9: Enter *location name*:", parse_mode='Markdown')
        return QP_LOCATION_NAME

    async def qp_get_location_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        qp = context.user_data['qp']
        text = update.message.text.strip()
        if text != '.' or 'location_name' not in qp:
            qp['location_name'] = text
        if qp.get('edit_mode'):
            cur = qp.get('location_link', '')
            await self.send(update, f"Step 3/9: Google Maps link\nCurrent: {cur}\n\nSend new value or *.* to keep:", parse_mode='Markdown')
        else:
            await self.send(update, "Step 3/9: Enter *Google Maps link*:", parse_mode='Markdown')
        return QP_LOCATION_LINK

    async def qp_get_location_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        qp = context.user_data['qp']
        text = update.message.text.strip()
        if text != '.' or 'location_link' not in qp:
            qp['location_link'] = text
        if qp.get('edit_mode'):
            cur = qp.get('date', '')
            await self.send(update, f"Step 4/9: Game date\nCurrent: {cur}\n\nSend new date (YYYY-MM-DD) or *.* to keep:", parse_mode='Markdown')
        else:
            await self.send(update, "Step 4/9: Enter *game date* (YYYY-MM-DD):", parse_mode='Markdown')
        return QP_DATE

    async def qp_get_date(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        qp = context.user_data['qp']
        text = update.message.text.strip()
        if text != '.' or 'date' not in qp:
            qp['date'] = text
        if qp.get('edit_mode'):
            cur = qp.get('time_start', '')
            await self.send(update, f"Step 5/9: Start time\nCurrent: {cur}\n\nSend new time (HH:MM) or *.* to keep:", parse_mode='Markdown')
        else:
            await self.send(update, "Step 5/9: Enter *start time* (HH:MM):", parse_mode='Markdown')
        return QP_TIME_START

    async def qp_get_time_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        qp = context.user_data['qp']
        text = update.message.text.strip()
        if text != '.' or 'time_start' not in qp:
            qp['time_start'] = text
        if qp.get('edit_mode'):
            cur = qp.get('time_end', '')
            await self.send(update, f"Step 6/9: End time\nCurrent: {cur}\n\nSend new time (HH:MM) or *.* to keep:", parse_mode='Markdown')
        else:
            await self.send(update, "Step 6/9: Enter *end time* (HH:MM):", parse_mode='Markdown')
        return QP_TIME_END

    async def qp_get_time_end(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        qp = context.user_data['qp']
        text = update.message.text.strip()
        if text != '.' or 'time_end' not in qp:
            qp['time_end'] = text
        if qp.get('edit_mode'):
            cur = qp.get('max_players', '')
            await self.send(update, f"Step 7/9: Max players\nCurrent: {cur}\n\nSend new number or *.* to keep:", parse_mode='Markdown')
        else:
            await self.send(update, "Step 7/9: Enter *max players* (number):", parse_mode='Markdown')
        return QP_MAX_PLAYERS

    async def qp_get_max_players(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        qp = context.user_data['qp']
        text = update.message.text.strip()
        if text != '.' or 'max_players' not in qp:
            try:
                qp['max_players'] = int(text)
            except ValueError:
                await self.send(update, "Please enter a number:")
                return QP_MAX_PLAYERS
        await self.send(update, "Step 8/10: Enter *voting deadline* in hours (e.g., 24), or *skip* for no deadline:", parse_mode='Markdown')
        return QP_DEADLINE

    async def qp_get_deadline(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip().lower()
        if text in ('skip', 'no', 'n'):
            context.user_data['qp']['deadline_hours'] = None
            context.user_data['qp']['auto_teams'] = False
            context.user_data['qp']['num_teams'] = context.user_data['qp'].get('num_teams', 0)
            await self.send(update, "Last step: What did you pay for the field? (e.g. 82, or *skip*):", parse_mode='Markdown')
            return QP_FIELD_RATE
        try:
            hours = float(text)
        except ValueError:
            await self.send(update, "Please enter a number of hours, or *skip* for no deadline:", parse_mode='Markdown')
            return QP_DEADLINE
        context.user_data['qp']['deadline_hours'] = hours
        await self.send(update, "Step 9/10: Auto-create teams when voting closes? (*yes* or *no*):", parse_mode='Markdown')
        return QP_AUTO_TEAMS

    async def qp_get_auto_teams(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        answer = update.message.text.strip().lower()
        if answer in ('yes', 'y'):
            context.user_data['qp']['auto_teams'] = True
            await self.send(update, "How many teams? (e.g., 2):")
            return QP_NUM_TEAMS
        elif answer in ('no', 'n'):
            context.user_data['qp']['auto_teams'] = False
            context.user_data['qp']['num_teams'] = 0
            await self.send(update, "Last step: What did you pay for the field? (e.g. 82, or *skip*):", parse_mode='Markdown')
            return QP_FIELD_RATE
        else:
            await self.send(update, "Please answer *yes* or *no*:")
            return QP_AUTO_TEAMS

    async def qp_get_num_teams(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            num_teams = int(update.message.text.strip())
            if num_teams < 2:
                num_teams = 2
        except ValueError:
            await self.send(update, "Please enter a number (min 2):")
            return QP_NUM_TEAMS
        
        context.user_data['qp']['num_teams'] = num_teams
        await self.send(update, "Last step: What did you pay for the field? (e.g. 82, or *skip*):", parse_mode='Markdown')
        return QP_FIELD_RATE

    async def qp_get_field_rate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip().lower()
        if text in ('skip', 'no', 'n', '0'):
            context.user_data['qp']['field_rate'] = None
        else:
            try:
                rate = float(text.lstrip('$'))
                if rate < 0:
                    raise ValueError
                context.user_data['qp']['field_rate'] = rate
            except ValueError:
                await self.send(update, "Please enter a dollar amount (e.g. 82), or *skip*:", parse_mode='Markdown')
                return QP_FIELD_RATE
        return await self._send_quickpoll_final(update, context)

    async def _send_quickpoll_final(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Shared logic to send quickpoll and optionally schedule teams"""
        qp = context.user_data['qp']
        
        # Use the target_chat_id selected in the wizard
        chat_id = qp.get('target_chat_id')
        
        if not chat_id:
            await self.send(update, "❌ No group selected. Restart with /quickpoll.")
            return ConversationHandler.END
        
        # Calculate deadline time (None if skipped)
        deadline_hours = qp.get('deadline_hours')
        deadline_time = datetime.now(TZ) + timedelta(hours=deadline_hours) if deadline_hours is not None else None
        
        # Send the quick poll
        poll_id = await self.send_quickpoll(
            chat_id=chat_id,
            location_name=qp['location_name'],
            location_link=qp['location_link'],
            game_date=qp['date'],
            time_start=qp['time_start'],
            time_end=qp['time_end'],
            max_players=qp['max_players'],
            deadline_time=deadline_time,
            num_teams=qp.get('num_teams', 0),
            admin_id=qp['admin_id'],
            field_rate=qp.get('field_rate')
        )
        
        if deadline_time:
            deadline_str = deadline_time.strftime('%I:%M %p')
            
            # Always schedule auto-close + roster at deadline
            self.schedule_event('close_quickpoll', deadline_time, {
                'poll_id': poll_id, 'chat_id': chat_id
            })

            # Schedule non-voter nudges at -24h / -12h / -2h (UX-3)
            self.schedule_nudge_events(poll_id, chat_id, deadline_time)

            # Schedule late arrivals events (prompt at game_start - 5min, announce at game_start + 2hrs)
            game_start_time = self.parse_game_datetime(qp['date'], qp['time_start'])
            if game_start_time:
                self.schedule_late_arrivals_events(poll_id, chat_id, qp['admin_id'], game_start_time)
            
            if qp.get('auto_teams'):
                # Schedule team selection at deadline
                self.schedule_event('finalize_teams', deadline_time, {
                    'poll_id': poll_id, 'chat_id': chat_id, 'admin_id': qp['admin_id']
                })
                await self.send(update, f"✅ Quick poll sent!\n⏳ Teams will be created at {deadline_str}")
            else:
                await self.send(update, f"✅ Quick poll sent!\n⏳ Voting closes at {deadline_str}\n💡 Use /maketeams to create teams manually")
        else:
            await self.send(update, f"✅ Quick poll sent! — No deadline\n💡 Use /closepoll to close voting and post roster")
        
        return ConversationHandler.END

    async def qp_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.send(update, "Quick poll cancelled.")
        return ConversationHandler.END

    async def send_quickpoll(self, chat_id: int, location_name: str, location_link: str, 
                             game_date: str, time_start: str, time_end: str, max_players: int,
                             deadline_time, num_teams: int, admin_id: int, field_rate: float = None):
        """Send a quick poll using native Telegram poll with reply-to trick"""
        
        poll_id = int(datetime.now().timestamp())

        # Persist the poll first so the live-roster renderer can read it
        # (poll_message_id is back-filled once the roster message is sent)
        deadline_iso = deadline_time.isoformat() if deadline_time else None
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''INSERT INTO quickpolls (id, location_name, location_link, max_players, deadline_time, num_teams, chat_id, admin_id, telegram_poll_id, poll_message_id, game_date, time_start, time_end, field_rate)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (poll_id, location_name, location_link, max_players, deadline_iso, num_teams, chat_id, admin_id,
                   None, None, game_date, time_start, time_end, field_rate))

        # Link any pending late arrivals from previous polls to this new poll
        # (auto-link for next poll feature)
        c.execute("""UPDATE late_arrivals SET blocked_from_poll_id = ?
                     WHERE blocked_from_poll_id IS NULL AND cleared_at IS NULL""",
                  (poll_id,))

        conn.commit()
        conn.close()

        # Send the live-roster message (UX-2) — starts empty, edited on every vote
        poll_msg = await self.application.bot.send_message(
            chat_id=chat_id,
            text=self.render_quickpoll_message(poll_id),
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=self.quickpoll_keyboard(poll_id),
        )

        # Back-fill the message id so future votes can edit it in place
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE quickpolls SET poll_message_id = ? WHERE id = ?",
                  (poll_msg.message_id, poll_id))
        conn.commit()
        conn.close()

        # Auto-pin the poll
        try:
            await self.application.bot.pin_chat_message(
                chat_id=chat_id, message_id=poll_msg.message_id, disable_notification=True
            )
        except Exception as e:
            logger.warning(f"Could not pin poll: {e}")

        return poll_id

    async def resolve_chat_context(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Determines the target chat ID based on:
        1. Context args (group name or ID)
        2. Current chat (if group)
        3. Most recently managed chat (if private)
        Returns: (chat_id, error_message)
        """
        user_id = update.effective_user.id
        
        # 1. Check for explicit argument override
        if context.args:
            identifier = context.args[0]
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            
            # Try by Name first
            c.execute("SELECT chat_id FROM chat_groups WHERE group_name = ?", (identifier,))
            res = c.fetchone()
            
            if not res:
                # Try by ID
                try:
                    chat_id_arg = int(identifier)
                    c.execute("SELECT chat_id FROM chat_groups WHERE chat_id = ?", (chat_id_arg,))
                    res = c.fetchone()
                except ValueError:
                    pass
            
            conn.close()
            
            if res:
                return res[0], None
            else:
                return None, f"❌ Group '{identifier}' not found in your managed groups."
        
        # 2. Check if in Group
        if update.effective_chat.type in ['group', 'supergroup']:
            return update.effective_chat.id, None
            
        # 3. Private context — use admin's saved active group
        chat_id, _ = self.get_admin_target_chat(user_id)
        if chat_id:
            return chat_id, None

        return None, "❌ No active group set. Run /switchgroup to choose a group."

    async def handle_late_arrivals_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle admin's response to late arrivals prompt — also handles UX-4 guest name/number replies."""
        user_id = update.effective_user.id

        # Poll report date input
        if user_id in self._pending_pollreport:
            await self._handle_pollreport_date_input(update, context)
            return
        # UX-4: guest add state
        if user_id in self._pending_guest_add:
            await self._handle_guest_add_reply(update, context)
            return
        # UX-4: guest remove state
        if user_id in self._pending_guest_remove:
            await self._handle_guest_remove_reply(update, context)
            return

        # Check if this admin has a pending late arrivals prompt
        if user_id not in self._pending_late_arrivals:
            return
        
        pending = self._pending_late_arrivals[user_id]
        poll_id = pending['poll_id']
        chat_id = pending['chat_id']
        players_list = pending['players']
        
        response = update.message.text.strip().lower()
        
        # Parse response
        late_arrivals = []
        if response != 'skip':
            try:
                # Parse comma-separated numbers
                indices = [int(x.strip()) - 1 for x in response.split(',')]
                # Map to actual player names
                for idx in indices:
                    if 0 <= idx < len(players_list):
                        late_arrivals.append(players_list[idx])
            except (ValueError, IndexError):
                await self.send(update, "❌ Invalid format. Please reply with numbers like `1,3,5` or `skip`.")
                return
        
        # Save to database
        if late_arrivals:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            
            for username in late_arrivals:
                try:
                    c.execute("""INSERT INTO late_arrivals 
                                 (poll_id, user_id, username, is_member, added_by_admin_id, added_at)
                                 VALUES (?, ?, ?, ?, ?, ?)""",
                              (poll_id, None, username, 1, user_id, datetime.now(TZ).isoformat()))
                except sqlite3.IntegrityError:
                    # Already exists, update it
                    c.execute("""UPDATE late_arrivals SET added_at = ? WHERE poll_id = ? AND username = ?""",
                              (datetime.now(TZ).isoformat(), poll_id, username))
            
            conn.commit()
            conn.close()
            
            await self.send(update, f"✅ Recorded {len(late_arrivals)} late arrivals. Announcement will post in ~2 hours.")
        else:
            await self.send(update, "✅ No late arrivals recorded.")
        
        # Clean up state
        del self._pending_late_arrivals[user_id]

    # ===== APPROVAL WORKFLOW =====

    async def request_approval(self, admin_id: int, text: str, callback_data: str, context_text: str = "Post this to the group?") -> bool:
        """Send a message to admin with a confirmation button to post to group.
        Returns True if the DM was sent successfully, False otherwise."""
        keyboard = [
            [
                InlineKeyboardButton("✅ Post to Group", callback_data=f"approve:{callback_data}"),
                InlineKeyboardButton("❌ Discard", callback_data=f"discard:{callback_data}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await self.application.bot.send_message(
                chat_id=admin_id,
                text=f"📝 *Review Message*\n\n{text}\n\n❓ *{context_text}*",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send approval request to {admin_id}: {e}")
            return False

    async def handle_approval_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle approval buttons: approve:action:poll_id:chat_id or discard:action:poll_id:chat_id"""
        query = update.callback_query
        await query.answer()

        data = query.data.split(':')
        decision = data[0]  # approve / discard
        action = data[1]    # roster / teams / cancel / announce_late / ...
        
        # safely parse remaining args
        try:
            poll_id = int(data[2])
            chat_id = int(data[3])
        except (IndexError, ValueError):
            await query.edit_message_text("❌ Error processing request.")
            return

        if decision == 'discard':
            if action == 'announce_late':
                # Remove from DB if discarded
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute("""UPDATE late_arrivals SET cleared_at = ? 
                             WHERE blocked_from_poll_id = ? AND cleared_at IS NULL""",
                          (datetime.now(TZ).isoformat(), poll_id))
                conn.commit()
                conn.close()
            await query.edit_message_text(f"❌ Action '{action}' discarded.")
            return

        await query.edit_message_text(f"✅ Action '{action}' approved. Posting...")

        if action == 'roster':
            pass  # Roster posting removed — live card is the roster

        elif action == 'teams':
            # Post the prebuilt teams message stored in _pending_teams
            teams_key = f"{poll_id}:{chat_id}"
            teams_msg = self._pending_teams.pop(teams_key, None)
            if teams_msg:
                await self.application.bot.send_message(chat_id=chat_id, text=teams_msg, parse_mode='Markdown')
            else:
                await self.application.bot.send_message(chat_id=chat_id, text="⚠️ Teams message expired. Use /maketeams to regenerate.")

        elif action == 'cancel':
            cancel_key = f"{poll_id}:{chat_id}"
            cancel_msg = self._pending_cancels.pop(cancel_key, "❌ *Game cancelled.*")
            await self.application.bot.send_message(
                chat_id=chat_id,
                text=cancel_msg,
                parse_mode='Markdown'
            )
        
        elif action == 'announce_late':
            # Get and post the late arrivals announcement
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("""SELECT username FROM late_arrivals 
                         WHERE blocked_from_poll_id = ? AND cleared_at IS NULL
                         ORDER BY added_at ASC""", (poll_id,))
            late_players = [row[0] for row in c.fetchall()]
            conn.close()
            
            if late_players:
                msg = "⚠️ *You will sit out next game:*\n\n"
                for i, username in enumerate(late_players, 1):
                    safe_name = username.replace('_', '\\_')
                    msg += f"{i}. @{safe_name}\n"
                
                await self.application.bot.send_message(
                    chat_id=chat_id, 
                    text=msg, 
                    parse_mode='Markdown'
                )

    async def post_roster(self, poll_id: int, chat_id: int, force_send: bool = False):
        """Post the final roster — everyone who voted IN (the cap is enforced at voting time)"""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        c.execute("SELECT max_players, admin_id, game_date, time_start FROM quickpolls WHERE id = ?", (poll_id,))
        poll = c.fetchone()
        if not poll:
            conn.close()
            return
        max_players = poll[0]
        admin_id = poll[1]
        game_date = poll[2]
        time_start = poll[3]

        # Get IN votes ordered chronologically
        c.execute("""SELECT username FROM quickpoll_votes
                     WHERE poll_id = ? AND vote_type = 'in'
                     ORDER BY voted_at ASC""", (poll_id,))
        players_in = [r[0] for r in c.fetchall()]
        conn.close()

        total = len(players_in)
        
        if total == 0:
            msg = "📋 No players voted IN for this game."
            if force_send:
                await self.application.bot.send_message(chat_id=chat_id, text=msg)
            elif admin_id:
                dm_sent = await self.request_approval(admin_id, msg, f"roster:{poll_id}:{chat_id}")
                if not dm_sent:
                    await self.application.bot.send_message(chat_id=chat_id, text=msg)
            else:
                await self.application.bot.send_message(chat_id=chat_id, text=msg)
            return
        
        # Parse game date and time for display
        game_date_display = game_date if game_date else "Today"
        try:
            # Try to parse and format as "Thursday Feb 26"
            from datetime import datetime as dt
            parsed_date = dt.strptime(game_date, '%Y-%m-%d')
            game_date_display = parsed_date.strftime('%A %b %d')
        except:
            pass
        
        time_display = time_start if time_start else "TBD"
        header = f"✅ *You are playing tonight on {game_date_display} at {time_display}*\n⏰ *Be on time or sit out next week.*\n\n"
        text = header
        
        # Continuous numbered list of everyone who's IN
        for i, name in enumerate(players_in, 1):
            safe = name.replace('_', '\\_')
            text += f"{i}. {safe}\n"

        # Confirmed guests section
        gc = sqlite3.connect(DB_FILE)
        gcc = gc.cursor()
        gcc.execute("""SELECT member_username, guest_name FROM quickpoll_guests
                       WHERE poll_id = ? AND confirmed = 1 ORDER BY added_at ASC""", (poll_id,))
        confirmed_guests = gcc.fetchall()
        gc.close()
        if confirmed_guests:
            text += f"\n👥 *+1 Guests ({len(confirmed_guests)} confirmed)*\n"
            for i, (mu, gname) in enumerate(confirmed_guests, 1):
                safe_mu = mu.replace('_', '\\_') if mu else '?'
                safe_gn = gname.replace('_', '\\_')
                text += f'{i}. @{safe_mu} brought "{safe_gn}"\n'
        
        if force_send:
            await self.application.bot.send_message(
                chat_id=chat_id, text=text, parse_mode='Markdown'
            )
        elif admin_id:
            dm_sent = await self.request_approval(admin_id, text, f"roster:{poll_id}:{chat_id}")
            if not dm_sent:
                # Admin DM failed (bot not started in private) — post directly to group
                await self.application.bot.send_message(chat_id=chat_id, text=text, parse_mode='Markdown')
        else:
            await self.application.bot.send_message(chat_id=chat_id, text=text, parse_mode='Markdown')

    async def closepoll_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Manually close the most recent quickpoll and post roster: /closepoll"""
        if update.effective_chat.type in ['group', 'supergroup']:
            # Delete command message in group
            await self.delete_message_safely(update.effective_chat.id, update.message.message_id)

        user_id = update.effective_user.id
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        if update.effective_chat.type in ['group', 'supergroup']:
            c.execute("SELECT id, chat_id, poll_message_id FROM quickpolls WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1", (update.effective_chat.id,))
        else:
            c.execute("""
                SELECT qp.id, qp.chat_id, qp.poll_message_id FROM quickpolls qp
                JOIN chat_admins ca ON qp.chat_id = ca.chat_id
                WHERE ca.user_id = ?
                ORDER BY qp.created_at DESC LIMIT 1
            """, (user_id,))
        poll = c.fetchone()
        conn.close()

        if not poll:
            await self.send(update, "❌ No quickpoll found to close.")
            return
        
        poll_id, chat_id, poll_msg_id = poll

        # Disable the poll buttons
        await self.close_quickpoll_buttons(chat_id, poll_msg_id)

        # Confirm guests (charge inviters for spots, notify short wallets)
        await self.confirm_quickpoll_guests(poll_id, chat_id)
        # Live card IS the roster — no separate roster post needed
        await self.send(update, "✅ Poll closed.")

    async def refreshpoll_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Push the current keyboard (incl. +1 / My Guests) to the latest live
        quickpoll message: /refreshpoll"""
        user_id = update.effective_user.id
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            SELECT qp.id FROM quickpolls qp
            JOIN chat_admins ca ON qp.chat_id = ca.chat_id
            WHERE ca.user_id = ? AND qp.closed = 0
            ORDER BY qp.created_at DESC LIMIT 1
        """, (user_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            await self.send(update, "❌ No open quickpoll found.")
            return
        poll_id = row[0]
        await self.refresh_quickpoll_message(poll_id)
        await self.send(update, "✅ Poll card refreshed.")

    # ── UX-2: admin override — add/remove any player after the deadline ────
    def resolve_user_id(self, username: str):
        """Best-effort username→user_id (for a clickable mention). Looks at
        wallets first, then any prior vote. None if we've never seen them."""
        clean = (username or '').lstrip('@')
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT user_id FROM wallets WHERE LOWER(username) = LOWER(?) AND user_id IS NOT NULL", (clean,))
        row = c.fetchone()
        if not row:
            c.execute("""SELECT user_id FROM quickpoll_votes
                         WHERE LOWER(username) = LOWER(?) AND user_id IS NOT NULL
                         ORDER BY id DESC LIMIT 1""", (clean,))
            row = c.fetchone()
        conn.close()
        return row[0] if row else None

    def latest_poll_for_admin(self, user_id: int):
        """The most recent quickpoll in any chat this user administers.
        Returns (poll_id, chat_id, max_players) or None."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""SELECT qp.id, qp.chat_id, qp.max_players FROM quickpolls qp
                     JOIN chat_admins ca ON qp.chat_id = ca.chat_id
                     WHERE ca.user_id = ?
                     ORDER BY qp.created_at DESC LIMIT 1""", (user_id,))
        row = c.fetchone()
        conn.close()
        return row

    async def addmember_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add players to the nudge roster. @username for pingable; plain name for no-username players.
        Separate multiple entries with commas: /addmember Ali Tarkhani, @user2"""
        if not context.args:
            await self.send(update, "Usage: /addmember @username or /addmember Full Name\nMultiple: /addmember Ali Tarkhani, @user2, Soheil D")
            return
        # Join all args then split on commas so multi-word names work
        entries = [e.strip().strip('"').strip("'") for e in ' '.join(context.args).split(',')]
        added_ping, added_display, existing = [], [], []
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        for raw in entries:
            is_display = not raw.startswith('@')
            name = raw.lstrip('@').strip()
            if not name:
                continue
            c.execute("SELECT 1 FROM members WHERE LOWER(username) = LOWER(?)", (name,))
            if c.fetchone():
                existing.append((name, is_display))
            else:
                c.execute("INSERT INTO members (username, first_name, is_display_name) VALUES (?, ?, ?)",
                          (name, name, 1 if is_display else 0))
                (added_display if is_display else added_ping).append(name)
        conn.commit()
        conn.close()
        lines = []
        if added_ping:
            lines.append("✅ Added: " + ", ".join(f"@{n}" for n in added_ping))
        if added_display:
            lines.append("✅ Added (no username — won't be pinged): " + ", ".join(added_display))
        if existing:
            lines.append("ℹ️ Already on roster: " + ", ".join(
                n if dn else f"@{n}" for n, dn in existing))
        await self.send(update, "\n".join(lines) or "Nothing to add.")

    async def removemember_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remove players from the nudge roster. Comma-separated for multiple: /removemember Ali Tarkhani, @user2"""
        if not context.args:
            await self.send(update, "Usage: /removemember @username or /removemember Full Name\nMultiple: /removemember Ali Tarkhani, @user2")
            return
        entries = [e.strip().strip('"').strip("'") for e in ' '.join(context.args).split(',')]
        removed, missing = [], []
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        for raw in entries:
            name = raw.lstrip('@').strip()
            if not name:
                continue
            c.execute("DELETE FROM members WHERE LOWER(username) = LOWER(?)", (name,))
            (removed if c.rowcount else missing).append(name)
        conn.commit()
        conn.close()
        lines = []
        if removed:
            lines.append("✅ Removed: " + ", ".join(removed))
        if missing:
            lines.append("ℹ️ Not on roster: " + ", ".join(missing))
        await self.send(update, "\n".join(lines) or "Nothing to remove.")

    async def members_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the nudge roster."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT username, is_display_name FROM members ORDER BY LOWER(username)")
        rows = [(r[0], r[1] or 0) for r in c.fetchall() if r[0]]
        conn.close()
        if not rows:
            await self.send(update, "ℹ️ The nudge roster is empty. Use /addmember to add players.")
            return
        body = "\n".join(
            f"{i}. {n} (no username)" if dn else f"{i}. @{n}"
            for i, (n, dn) in enumerate(rows, 1)
        )
        await self.send(update, f"👥 Nudge roster ({len(rows)}):\n{body}")

    def get_nonvoters(self, poll_id: int):
        """UX-3: members who have NOT cast any vote (IN or OUT) on this poll.
        Returns list of (username, is_display_name)."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        try:
            c.execute("SELECT username, is_display_name FROM members")
            members = [(r[0], r[1] or 0) for r in c.fetchall() if r[0]]
        except sqlite3.OperationalError:
            members = []
        c.execute("SELECT username FROM quickpoll_votes WHERE poll_id = ?", (poll_id,))
        voted = {r[0].lower() for r in c.fetchall() if r[0]}
        conn.close()
        return [(name, dn) for name, dn in members if name.lower() not in voted]

    async def _send_nudge_message(self, chat_id: int, usernames):
        """Tag a list of non-voters in the group, chunked to stay under Telegram's limit.
        usernames is a list of (name, is_display_name) tuples."""
        header = "⏳ <b>Game's coming up — we haven't heard from you!</b>\nTap <b>IN</b> or <b>OUT</b> on the poll above 👆"
        CHUNK = 30
        for i in range(0, len(usernames), CHUNK):
            batch = usernames[i:i + CHUNK]
            pinged = [f"@{self._esc(name)}" for name, is_dn in batch if not is_dn]
            display = [self._esc(name) for name, is_dn in batch if is_dn]
            parts = []
            if pinged:
                parts.append(' '.join(pinged))
            if display:
                parts.append(', '.join(display))
            mentions = '\n\n'.join(parts)
            text = f"{header}\n\n{mentions}" if i == 0 else mentions
            try:
                await self.application.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode='HTML',
                    disable_web_page_preview=True)
            except Exception as e:
                logger.error(f"Nudge send failed for chat {chat_id}: {e}")

    async def nudge_nonvoters(self, poll_id: int, chat_id: int, respect_closed: bool = True) -> int:
        """UX-3: tag members who haven't voted at all. Scheduled nudges pass
        respect_closed=True so a closed/cancelled poll is a no-op; manual
        /nudge passes respect_closed=False. Returns how many were tagged."""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT closed FROM quickpolls WHERE id = ?", (poll_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            logger.info(f"Nudge skipped: poll {poll_id} no longer exists")
            return 0
        if respect_closed and row[0]:
            logger.info(f"Nudge skipped: poll {poll_id} is closed")
            return 0
        nonvoters = self.get_nonvoters(poll_id)
        if not nonvoters:
            return 0
        await self._send_nudge_message(chat_id, nonvoters)
        return len(nonvoters)

    async def nudge_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin command: fire a non-voter nudge immediately. /nudge [poll_id]
        — with no arg, targets the admin's most recent poll. No usage limit."""
        user = update.effective_user
        if context.args:
            try:
                poll_id = int(context.args[0])
            except ValueError:
                await self.send(update, "Usage: /nudge [poll_id]")
                return
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("""SELECT qp.chat_id FROM quickpolls qp
                         JOIN chat_admins ca ON qp.chat_id = ca.chat_id
                         WHERE qp.id = ? AND ca.user_id = ?""", (poll_id, user.id))
            row = c.fetchone()
            conn.close()
            if not row:
                await self.send(update, "❌ No poll with that ID in a group you manage.")
                return
            chat_id = row[0]
        else:
            poll = self.latest_poll_for_admin(user.id)
            if not poll:
                await self.send(update, "❌ No recent poll found to nudge.")
                return
            poll_id, chat_id = poll[0], poll[1]

        count = await self.nudge_nonvoters(poll_id, chat_id, respect_closed=False)
        if count:
            await self.send(update, f"✅ Nudged {count} non-voter(s) in the group.")
        else:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM members")
            has_members = c.fetchone()[0] > 0
            conn.close()
            if has_members:
                await self.send(update, "🎉 Everyone's already voted — no one to nudge.")
            else:
                await self.send(update, "ℹ️ No members on file to nudge. Add some with /addmember.")

    async def notify_super_admin_override(self, admin, target: str, poll_id: int, reason: str):
        """DM the super-admin the full picture when a force-add skipped the
        charge (low/no balance). The initiating admin never sees these details."""
        if not SUPER_ADMIN_ID:
            return
        wallet = self.get_wallet(target)
        if wallet:
            wstate = f"balance ${wallet['balance']:.2f}, first\\_paid={'yes' if wallet['first_paid'] else 'no'}"
        else:
            wstate = "no wallet on file"
        admin_name = (f"@{admin.username}" if admin.username else (admin.first_name or str(admin.id))).replace('_', '\\_')
        safe_target = target.replace('_', '\\_')
        when = datetime.now(TZ).strftime('%b %d, %Y %I:%M %p')
        text = (
            f"🛡️ *Admin override — charge skipped*\n\n"
            f"*Admin:* {admin_name} (`{admin.id}`)\n"
            f"*Player added:* @{safe_target}\n"
            f"*Game:* poll #{poll_id}\n"
            f"*When:* {when}\n"
            f"*Reason:* {reason or '—'}\n"
            f"*Wallet:* {wstate}\n\n"
            f"⚠️ ${VOTE_COST:.0f} was *not* charged (insufficient balance). Player added to the roster anyway."
        )
        try:
            await self.application.bot.send_message(chat_id=SUPER_ADMIN_ID, text=text, parse_mode='Markdown')
        except Exception as e:
            logger.warning(f"Could not DM super-admin override notice: {e}")

    async def addplayer_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin override: force-add a player IN to the latest poll, even after
        it closes. Charges $10 like a normal vote; if the wallet can't cover it,
        the player is added anyway and the super-admin is notified."""
        user = update.effective_user
        if not context.args:
            await self.send(update, "Usage: /addplayer @username [reason]")
            return
        target = context.args[0].lstrip('@')
        reason = ' '.join(context.args[1:]).strip()
        poll = self.latest_poll_for_admin(user.id)
        if not poll:
            await self.send(update, "❌ No recent poll found to adjust.")
            return
        poll_id = poll[0]

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT id, vote_type FROM quickpoll_votes WHERE poll_id = ? AND LOWER(username) = LOWER(?)",
                  (poll_id, target))
        erow = c.fetchone()
        if erow and erow[1] == 'in':
            conn.close()
            await self.send(update, f"ℹ️ @{target} is already IN for this game.")
            return
        target_uid = self.resolve_user_id(target)
        if erow:
            c.execute("UPDATE quickpoll_votes SET vote_type = 'in', user_id = COALESCE(user_id, ?) WHERE id = ?",
                      (target_uid, erow[0]))
        else:
            c.execute("INSERT INTO quickpoll_votes (poll_id, user_id, username, vote_type) VALUES (?, ?, ?, 'in')",
                      (poll_id, target_uid, target))
        conn.commit()
        conn.close()

        # Charge like a normal IN vote; skip (and escalate) if balance can't cover
        charged = self.deduct_wallet(target, VOTE_COST, f"quickpoll_vote:{poll_id}")
        if not charged:
            await self.notify_super_admin_override(user, target, poll_id, reason)

        await self.refresh_quickpoll_message(poll_id)
        await self.send(update, f"✅ @{target} added IN.")

    async def removeplayer_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin override: force-remove a player from the latest poll, even
        after it closes. Refunds the $10 if they had been charged."""
        user = update.effective_user
        if not context.args:
            await self.send(update, "Usage: /removeplayer @username [reason]")
            return
        target = context.args[0].lstrip('@')
        poll = self.latest_poll_for_admin(user.id)
        if not poll:
            await self.send(update, "❌ No recent poll found to adjust.")
            return
        poll_id = poll[0]

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT id, vote_type FROM quickpoll_votes WHERE poll_id = ? AND LOWER(username) = LOWER(?)",
                  (poll_id, target))
        erow = c.fetchone()
        if not erow:
            conn.close()
            await self.send(update, f"ℹ️ @{target} isn't on this poll.")
            return
        was_in = erow[1] == 'in'
        c.execute("DELETE FROM quickpoll_votes WHERE id = ?", (erow[0],))
        conn.commit()
        conn.close()

        # Refund only if a charge for this poll actually exists
        if was_in:
            rconn = sqlite3.connect(DB_FILE)
            rc = rconn.cursor()
            rc.execute("""SELECT ABS(amount) FROM payment_confirmations
                          WHERE LOWER(username) = LOWER(?) AND notes LIKE ? AND amount < 0
                          ORDER BY id DESC LIMIT 1""",
                       (target, f"quickpoll_vote:{poll_id}%"))
            charge_row = rc.fetchone()
            rconn.close()
            if charge_row:
                self.credit_wallet(target, charge_row[0], f"quickpoll_refund:{poll_id}")

        await self.refresh_quickpoll_message(poll_id)

        # Check if removed player had guests — prompt admin to decide
        gc = sqlite3.connect(DB_FILE)
        gcc = gc.cursor()
        gcc.execute("SELECT id, guest_name FROM quickpoll_guests WHERE poll_id = ? AND LOWER(member_username) = LOWER(?)",
                    (poll_id, target))
        guest_rows = gcc.fetchall()
        gc.close()
        if guest_rows:
            n = len(guest_rows)
            names = ', '.join(g[1] for g in guest_rows)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("Yes, remove all", callback_data=f"qgrm:all:{poll_id}:{target}"),
                InlineKeyboardButton("Keep on waitlist", callback_data=f"qgrm:keep:{poll_id}:{target}"),
            ]])
            await self.send(update,
                            f"✅ @{target} removed. They had {n} guest{'s' if n > 1 else ''}: {names}.\n\nRemove their guests too?",
                            reply_markup=kb)
        else:
            await self.send(update, f"✅ @{target} removed.")

    async def finalize_teams(self, poll_id: int, chat_id: int, admin_id: int):
        """Called at deadline - creates balanced teams and posts to group"""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Get poll info
        c.execute("SELECT max_players, num_teams FROM quickpolls WHERE id = ?", (poll_id,))
        poll = c.fetchone()
        if not poll:
            conn.close()
            return
        
        max_players, num_teams = poll
        
        # Get all IN votes ordered by time (first come first serve)
        c.execute("""SELECT user_id, username, voted_at FROM quickpoll_votes 
                     WHERE poll_id = ? AND vote_type = 'in' 
                     ORDER BY voted_at ASC""", (poll_id,))
        in_votes = c.fetchall()
        
        # Build final player list (first come first serve up to max)
        final_players = []
        for user_id, username, voted_at in in_votes:
            if len(final_players) >= max_players:
                break
            final_players.append({'user_id': user_id, 'username': username})
        
        if len(final_players) < 2:
            await self.application.bot.send_message(
                chat_id=chat_id, text="❌ Not enough players to create teams."
            )
            conn.close()
            return
        
        # Check for unrated players and notify admin
        unrated_players = []
        six_months_ago = datetime.now() - timedelta(days=180)
        
        for player in final_players:
            c.execute("SELECT skill_rating, last_activity FROM skills WHERE username = ?", (player['username'],))
            skill = c.fetchone()
            
            if skill:
                player['skill'] = skill[0]
                # Check if inactive (no activity in 6 months)
                if skill[1]:
                    last_active = datetime.fromisoformat(skill[1]) if isinstance(skill[1], str) else skill[1]
                    if last_active < six_months_ago:
                        unrated_players.append(player['username'])
            else:
                player['skill'] = 5  # Default
                unrated_players.append(player['username'])
            
            # Update last_activity
            c.execute('INSERT OR REPLACE INTO skills (username, skill_rating, last_activity) VALUES (?, ?, ?)',
                      (player['username'], player['skill'], datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        
        # Notify admin about unrated players
        if unrated_players:
            safe_unrated = [u.replace('_', '\\_') for u in unrated_players]
            unrated_msg = "⚠️ *Assign scores to:*\n" + '\n'.join([f"• @{u}" for u in safe_unrated])
            try:
                await self.application.bot.send_message(chat_id=admin_id, text=unrated_msg, parse_mode='Markdown')
            except: pass  # Admin might have blocked bot
        
        # Balance teams using greedy algorithm
        teams = self.balance_teams(final_players, num_teams)
        
        # Build and send message
        msg = "⚽ *Teams for Today's Game*\n\n"
        for i, team in enumerate(teams, 1):
            total_skill = sum(p['skill'] for p in team)
            msg += f"🏆 *Team {i}* (skill: {total_skill})\n"
            for p in team:
                safe_username = p['username'].replace('_', '\\_')
                stars = '⭐' * min(p['skill'], 10)
                msg += f"• @{safe_username} {stars}\n"
            msg += "\n"
        
        msg += "Good luck! 🎉"
        
        # Store the prebuilt teams message keyed by poll_id so the approval handler can post it
        teams_key = f"{poll_id}:{chat_id}"
        self._pending_teams[teams_key] = msg

        await self.request_approval(
            admin_id,
            msg,
            f"teams:{poll_id}:{chat_id}",
            "Teams created. Post to group?"
        )

    async def prompt_late_arrivals(self, poll_id: int, chat_id: int, admin_id: int):
        """Prompt admin to enter names of players who arrived late (5 min before game start)"""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Get IN voters from the quickpoll
        c.execute("""SELECT username FROM quickpoll_votes
                     WHERE poll_id = ? AND vote_type = 'in'
                     ORDER BY voted_at ASC""", (poll_id,))
        in_players = [row[0] for row in c.fetchall()]
        conn.close()
        
        if not in_players:
            logger.info(f"No IN voters for poll {poll_id}, skipping late arrivals prompt")
            return
        
        # Store state for when admin replies
        self._pending_late_arrivals[admin_id] = {
            'poll_id': poll_id,
            'chat_id': chat_id,
            'players': in_players
        }
        
        # Build numbered list
        roster_msg = "⏰ *Who arrived late and should sit out?*\n\nToday's players:\n"
        for i, username in enumerate(in_players, 1):
            safe_name = username.replace('_', '\\_')
            roster_msg += f"{i}. @{safe_name}\n"
        
        roster_msg += "\n📝 *Reply with comma-separated numbers* (e.g., `1,3,5`)\nOr reply `skip` if everyone was on time."
        
        # Send to admin
        try:
            await self.application.bot.send_message(
                chat_id=admin_id,
                text=roster_msg,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Failed to send late arrivals prompt to admin {admin_id}: {e}")

    async def announce_late_arrivals(self, poll_id: int, chat_id: int, admin_id: int):
        """Post announcement of who was late (2 hours after game start)"""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Get late arrivals for this poll
        c.execute("""SELECT username FROM late_arrivals 
                     WHERE poll_id = ? AND cleared_at IS NULL
                     ORDER BY added_at ASC""", (poll_id,))
        late_players = [row[0] for row in c.fetchall()]
        conn.close()
        
        # If no late arrivals, nothing to announce
        if not late_players:
            logger.info(f"No late arrivals for poll {poll_id}, skipping announcement")
            return
        
        # Build announcement message
        msg = "⚠️ *You will sit out next game:*\n\n"
        for i, username in enumerate(late_players, 1):
            safe_name = username.replace('_', '\\_')
            msg += f"{i}. @{safe_name}\n"
        
        # Request approval before posting
        await self.request_approval(
            admin_id,
            msg,
            f"announce_late:{poll_id}:{chat_id}",
            "Post announcement to group?"
        )

    # ===== DB-BASED SCHEDULING =====

    def schedule_event(self, event_type: str, fire_time, payload: dict):
        """Insert a scheduled event into the DB"""
        ft = fire_time.isoformat() if isinstance(fire_time, datetime) else fire_time
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO scheduled_events (event_type, fire_time, payload) VALUES (?, ?, ?)",
                  (event_type, ft, json.dumps(payload)))
        conn.commit()
        conn.close()

    async def process_pending_events(self):
        """Check for and execute all overdue scheduled events"""
        if self._processing:
            return
        self._processing = True
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            now = datetime.now(TZ).isoformat()
            c.execute("SELECT id, event_type, payload FROM scheduled_events WHERE fire_time <= ? AND executed = 0 ORDER BY fire_time ASC", (now,))
            events = c.fetchall()
            conn.close()

            for event_id, event_type, payload_json in events:
                payload = json.loads(payload_json)
                try:
                    if event_type == 'close_quickpoll':
                        # Auto-close native poll + post roster
                        qp_poll_id = payload['poll_id']
                        qp_chat_id = payload['chat_id']
                        cconn = sqlite3.connect(DB_FILE)
                        cc = cconn.cursor()
                        cc.execute("SELECT poll_message_id FROM quickpolls WHERE id = ?", (qp_poll_id,))
                        qp_row = cc.fetchone()
                        cconn.close()
                        if qp_row and qp_row[0]:
                            await self.close_quickpoll_buttons(qp_chat_id, qp_row[0])
                        await self.confirm_quickpoll_guests(qp_poll_id, qp_chat_id)
                        # Live card IS the roster — no separate roster post needed
                    elif event_type == 'finalize_teams':
                        await self.finalize_teams(payload['poll_id'], payload['chat_id'], payload['admin_id'])
                    elif event_type == 'prompt_late_arrivals':
                        await self.prompt_late_arrivals(payload['poll_id'], payload['chat_id'], payload['admin_id'])
                    elif event_type == 'announce_late_arrivals':
                        await self.announce_late_arrivals(payload['poll_id'], payload['chat_id'], payload['admin_id'])
                    elif event_type == 'nudge_nonvoters':
                        await self.nudge_nonvoters(payload['poll_id'], payload['chat_id'])
                except Exception as e:
                    logger.error(f"Error processing event {event_id} ({event_type}): {e}")

                # Mark as executed regardless of success/failure
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute("UPDATE scheduled_events SET executed = 1 WHERE id = ?", (event_id,))
                conn.commit()
                conn.close()
        finally:
            self._processing = False

    async def periodic_event_check(self):
        """Background task to check for pending events every 5 minutes"""
        while True:
            await asyncio.sleep(300)
            try:
                await self.process_pending_events()
            except Exception as e:
                logger.error(f"Periodic event check error: {e}")

    async def on_startup(self, application):
        """Post-init hook: process pending events and start background checker"""
        logger.info("Bot starting up, processing pending events...")
        await self.refresh_command_scopes()
        await self.process_pending_events()
        self._periodic_task = asyncio.create_task(self.periodic_event_check())
        logger.info("Bot startup complete.")

    async def on_shutdown(self, application):
        """Post-stop hook: cancel the background checker before the loop closes."""
        task = getattr(self, '_periodic_task', None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("Bot shutdown complete.")

    # ===== CANCELLATION COMMANDS =====

    async def cancelquickpoll_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Step 1: If admin manages multiple groups with polls, show picker first."""
        if update.effective_chat.type in ['group', 'supergroup']:
            await self.delete_message_safely(update.effective_chat.id, update.message.message_id)

        user_id = update.effective_user.id
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        if update.effective_chat.type in ['group', 'supergroup']:
            # Run from inside a group — target that group directly
            c.execute("SELECT id, chat_id FROM quickpolls WHERE chat_id = ? AND closed = 0 ORDER BY created_at DESC LIMIT 1", (update.effective_chat.id,))
            poll = c.fetchone()
            conn.close()
            if not poll:
                await self.send(update, "❌ No open quickpoll found in this group.")
                return ConversationHandler.END
            context.user_data['cancel_qp_poll_id'] = poll[0]
            context.user_data['cancel_qp_chat_id'] = poll[1]
            await self.send(update, "What's the reason for cancelling?\n\nSend your reason or /skip to cancel without one.")
            return CANCEL_QP_REASON
        # Run from private DM — find all groups this admin manages that have an open poll
        c.execute("""
            SELECT cg.chat_id, cg.group_name, qp.id, qp.location_name, qp.game_date, qp.time_start
            FROM chat_groups cg
            JOIN quickpolls qp ON qp.chat_id = cg.chat_id
                AND qp.id = (SELECT MAX(id) FROM quickpolls WHERE chat_id = cg.chat_id AND closed = 0)
            LEFT JOIN chat_admins ca ON ca.chat_id = cg.chat_id AND ca.user_id = ?
            WHERE qp.closed = 0
              AND (ca.user_id IS NOT NULL OR ? = ?)
            ORDER BY cg.group_name
        """, (user_id, user_id, SUPER_ADMIN_ID))
        groups = c.fetchall()  # [(chat_id, group_name, poll_id, location, game_date, time_start), ...]
        conn.close()

        # Filter in Python — SQL date comparison is unreliable when game_date
        # may be stored in non-ISO formats like 'May 30'
        _today = datetime.now(TZ).date()
        def _is_upcoming(gd):
            if not gd:
                return True  # no date = include (let admin decide)
            try:
                return datetime.strptime(gd.strip(), '%Y-%m-%d').date() >= _today
            except ValueError:
                return False  # unparseable = legacy test poll, exclude
        groups = [r for r in groups if _is_upcoming(r[4])]

        if not groups:
            await self.send(update, "❌ No open quickpolls found in any of your groups.")
            return ConversationHandler.END

        if len(groups) == 1:
            chat_id, group_name, poll_id, location, game_date, time_start = groups[0]
            context.user_data['cancel_qp_poll_id'] = poll_id
            context.user_data['cancel_qp_chat_id'] = chat_id
            date_str = self._pretty_date(game_date) if game_date else 'TBD'
            time_str = f" · {time_start}" if time_start else ""
            await self.send(update, f"Cancelling poll in *{group_name}*:\n📍 {location} — {date_str}{time_str}\n\nWhat's the reason? Send it or /skip.", parse_mode='Markdown')
            return CANCEL_QP_REASON

        # Multiple groups — show inline group picker with poll details
        lines = ["Which group's poll do you want to cancel?\n"]
        buttons = []
        for chat_id, group_name, poll_id, location, game_date, time_start in groups:
            date_str = self._pretty_date(game_date) if game_date else 'TBD'
            time_str = f" · {time_start}" if time_start else ""
            label = f"{group_name}: {location} — {date_str}{time_str}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"cqpg:{chat_id}:{poll_id}")])
        await self.send(update, "Which group's poll do you want to cancel?",
                        reply_markup=InlineKeyboardMarkup(buttons))
        return CANCEL_QP_GROUP

    async def cancel_qp_group_pick(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Step 1b: in CANCEL_QP_GROUP state. If group was already picked via inline button,
        treat this text as the cancellation reason and execute. Otherwise prompt to tap."""
        user = update.effective_user
        pending = self._cqpg_pending.pop(user.id, None)
        if pending:
            poll_id, chat_id, _ = pending
            context.user_data['cancel_qp_poll_id'] = poll_id
            context.user_data['cancel_qp_chat_id'] = chat_id
            reason = update.message.text.strip()
            await self._execute_cancel_quickpoll(update, context, reason)
            return ConversationHandler.END
        await self.send(update, "Please tap one of the group buttons above, or /cancel to abort.")
        return CANCEL_QP_GROUP

    async def cancel_qp_group_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Step 1b /skip: group already picked via button — execute with no reason."""
        user = update.effective_user
        pending = self._cqpg_pending.pop(user.id, None)
        if pending:
            poll_id, chat_id, _ = pending
            context.user_data['cancel_qp_poll_id'] = poll_id
            context.user_data['cancel_qp_chat_id'] = chat_id
        await self._execute_cancel_quickpoll(update, context, "No reason given.")
        return ConversationHandler.END

    async def handle_cancelqp_group_callback(self, query, chat_id_str: str, poll_id_str: str):
        """Inline button tap on the group picker for /cancelquickpoll."""
        try:
            chat_id = int(chat_id_str)
            poll_id = int(poll_id_str)
        except ValueError:
            await query.answer("Invalid selection.")
            return
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT group_name FROM chat_groups WHERE chat_id = ?", (chat_id,))
        row = c.fetchone()
        c.execute("SELECT location_name, game_date, time_start FROM quickpolls WHERE id = ?", (poll_id,))
        prow = c.fetchone()
        conn.close()
        group_name = row[0] if row else str(chat_id)
        if prow:
            date_str = self._pretty_date(prow[1]) if prow[1] else 'TBD'
            time_str = f" · {prow[2]}" if prow[2] else ""
            poll_detail = f"\n📍 {prow[0]} — {date_str}{time_str}"
        else:
            poll_detail = ""
        self._cqpg_pending[query.from_user.id] = (poll_id, chat_id, group_name)
        await query.answer()
        await query.edit_message_text(
            f"Cancelling poll in *{group_name}*{poll_detail}\n\nWhat's the reason? Reply here or /skip.",
            parse_mode='Markdown')

    async def cancel_qp_reason(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Step 2a: Got a reason — execute cancellation."""
        # If we got here from the group-picker callback, pull poll/chat from side-channel
        pending = self._cqpg_pending.pop(update.effective_user.id, None)
        if pending:
            poll_id, chat_id, _ = pending
            context.user_data['cancel_qp_poll_id'] = poll_id
            context.user_data['cancel_qp_chat_id'] = chat_id
        reason = update.message.text.strip()
        await self._execute_cancel_quickpoll(update, context, reason)
        return ConversationHandler.END

    async def cancel_qp_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Step 2b: Admin skipped the reason — cancel with no reason given."""
        pending = self._cqpg_pending.pop(update.effective_user.id, None)
        if pending:
            poll_id, chat_id, _ = pending
            context.user_data['cancel_qp_poll_id'] = poll_id
            context.user_data['cancel_qp_chat_id'] = chat_id
        await self._execute_cancel_quickpoll(update, context, "No reason given.")
        return ConversationHandler.END

    async def _execute_cancel_quickpoll(self, update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str):
        """Internal: run the actual quickpoll cancellation after reason is collected."""
        poll_id = context.user_data.pop('cancel_qp_poll_id', None)
        chat_id = context.user_data.pop('cancel_qp_chat_id', None)
        if not poll_id or not chat_id:
            await self.send(update, "❌ Could not find poll to cancel.")
            return

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        # Cancel pending deadline events for this poll
        c.execute("SELECT id, payload FROM scheduled_events WHERE event_type IN ('finalize_teams', 'close_quickpoll', 'nudge_nonvoters') AND executed = 0")
        for eid, payload_json in c.fetchall():
            payload = json.loads(payload_json)
            if payload.get('poll_id') == poll_id:
                c.execute("UPDATE scheduled_events SET executed = 1 WHERE id = ?", (eid,))

        # Snapshot OUT voters for audit trail
        c.execute("SELECT username FROM quickpoll_votes WHERE poll_id = ? AND vote_type = 'out'", (poll_id,))
        out_voters = [r[0] for r in c.fetchall()]
        now_iso = datetime.now(TZ).isoformat()
        for ou in out_voters:
            c.execute("""INSERT INTO payment_confirmations
                         (username, amount, payment_date, confirmed_date, status, notes)
                         VALUES (?, 0, ?, ?, 'confirmed', ?)""",
                      (ou, now_iso, now_iso, f'quickpoll_cancelled_out:{poll_id}'))

        # Grab message ID to close buttons
        c.execute("SELECT poll_message_id FROM quickpolls WHERE id = ?", (poll_id,))
        res = c.fetchone()
        poll_msg_id = res[0] if res else None

        # Clear votes and guests; mark closed
        c.execute("DELETE FROM quickpoll_votes WHERE poll_id = ?", (poll_id,))
        c.execute("DELETE FROM quickpoll_guests WHERE poll_id = ?", (poll_id,))
        c.execute("UPDATE quickpolls SET closed = 1 WHERE id = ?", (poll_id,))
        conn.commit()
        conn.close()

        admin_id = update.effective_user.id
        await self.close_quickpoll_buttons(chat_id, poll_msg_id)

        group_text = f"❌ *Game cancelled.*\n\n📢 {reason}"
        self._pending_cancels[f"{poll_id}:{chat_id}"] = group_text
        await self.request_approval(
            admin_id,
            group_text,
            f"cancel:{poll_id}:{chat_id}",
            "Post cancellation notice to group?"
        )

        await self.send(update, "✅ Quick poll cancelled.")

    async def start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Warm welcome message with role-aware guidance."""
        user = update.effective_user
        # Pressing Start always (re)pushes this user's slash-command menu — the
        # reliable way a newly-added admin gets their commands to show up.
        await self.sync_user_commands(user, force=True)
        if self.is_super_admin(user.id):
            role = 'super'
        elif self.is_admin_any_chat(user.id, user.username):
            role = 'admin'
        else:
            role = 'player'

        greeting = "سلام گل گلاب\\! بذار بهت بگم چطوری میتونم در خدمتت باشم 🙌\n\n"
        if role == 'super':
            body = (
                "*👑 Group Management:*\n"
                "/addadmin — Give someone admin access\n"
                "/removeadmin — Revoke admin access\n"
                "/listadmins — See all admins for a group\n"
                "/listchats — See all groups you manage\n\n"
                "*🗳 Game Operations:*\n"
                "/quickpoll — Create a game poll for your group\n"
                "/closepoll — Close voting early and send the final lineup for approval\n"
                "/cancelquickpoll — Cancel a poll and refund everyone automatically\n"
                "/maketeams — Split voted\\-in players into balanced teams\n"
                "/setskill, /skills, /deleteskill — Manage skill ratings for fair team splits\n\n"
                "*💰 Your Wallet:*\n"
                "/wallet — Check your balance and recent activity\n"
                "/topup — Add funds to join games\n"
                "/cashout — Withdraw to Venmo\n\n"
                "Just send /quickpoll to get started\\."
            )
        elif role == 'admin':
            body = (
                "*🗳 Game Operations:*\n"
                "/quickpoll — Create a game poll for your group\n"
                "/closepoll — Close voting early and send the final lineup for approval\n"
                "/cancelquickpoll — Cancel a poll and refund everyone automatically\n"
                "/maketeams — Split voted\\-in players into balanced teams\n"
                "/setskill, /skills, /deleteskill — Manage skill ratings for fair team splits\n\n"
                "*💰 Your Wallet:*\n"
                "/wallet — Check your balance and recent activity\n"
                "/topup — Add funds to join games\n"
                "/cashout — Withdraw to Venmo\n\n"
                "Just send /quickpoll to get started\\."
            )
        else:
            body = (
                "💰 /wallet — Check your balance and recent game activity\\.\n"
                "💳 /topup — Add funds to your wallet so you can vote in on games\\. Each game costs $10\\.\n"
                "💸 /cashout — Withdraw your balance back to Venmo anytime\\.\n\n"
                "When there's a game poll in your group, tap *IN* to join — $10 is deducted from your wallet\\. Switch to *OUT* before the deadline to get it back\\."
            )
        await self.send(update, greeting + body, parse_mode='MarkdownV2')

    async def unknown_message_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Warm reply for unexpected messages in private chat."""
        user_id = update.effective_user.id
        # Clear any pending guest states if user sends /cancel or any unrecognised text
        if update.message and update.message.text and update.message.text.strip().lower() == '/cancel':
            cleared = False
            if self._pending_guest_add.pop(user_id, None):
                cleared = True
            if self._pending_guest_remove.pop(user_id, None):
                cleared = True
            if cleared:
                await self.send(update, "Cancelled.")
                return
        import random
        responses = [
            "Hmm, not sure what to do with that one\\! Try /wallet to check your balance or /topup to add funds\\.",
            "That one went over my head\\! Here's what I'm good at: /wallet, /topup, /cashout — give one of those a go\\.",
            "Not quite my language, but I've got your back for the important stuff\\. Start with /wallet to see where things stand\\.",
        ]
        await self.send(update, random.choice(responses), parse_mode='MarkdownV2')

    async def delete_group_commands(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Delete any command messages sent in groups to keep bot invisible"""
        if update.effective_chat.type in ['group', 'supergroup'] and update.message and update.message.text:
            if update.message.text.startswith('/'):
                await self.delete_message_safely(update.effective_chat.id, update.message.message_id)
    
    def run(self):
        persistence = PicklePersistence(filepath=PERSISTENCE_FILE)
        self.application = Application.builder().token(self.token).persistence(persistence).post_init(self.on_startup).post_stop(self.on_shutdown).build()

        # Surface unhandled exceptions instead of failing silently.
        self.application.add_error_handler(self.error_handler)

        # Group command deletion handler (must be first to delete commands before processing)
        self.application.add_handler(MessageHandler(
            filters.ChatType.GROUPS & filters.Regex(r'^/'),
            self.delete_group_commands
        ), group=-1)

        # Private command authorization guard before command handlers.
        self.application.add_handler(MessageHandler(
            filters.ChatType.PRIVATE & filters.COMMAND,
            self.private_command_guard
        ), group=-1)

        # Quick poll conversation handler
        quickpoll_handler = ConversationHandler(
            entry_points=[CommandHandler('quickpoll', self.quickpoll_start, filters=filters.ChatType.PRIVATE)],
            states={
                QP_GROUP_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.qp_get_group_select)],
                QP_REPEAT_CHECK: [
                    CallbackQueryHandler(self.qp_repeat_use_last, pattern='^qp_use_last$'),
                    CallbackQueryHandler(self.qp_repeat_edit, pattern='^qp_edit_last$'),
                    CallbackQueryHandler(self.qp_repeat_fresh, pattern='^qp_fresh$'),
                ],
                QP_LOCATION_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.qp_get_location_name)],
                QP_LOCATION_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.qp_get_location_link)],
                QP_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.qp_get_date)],
                QP_TIME_START: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.qp_get_time_start)],
                QP_TIME_END: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.qp_get_time_end)],
                QP_MAX_PLAYERS: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.qp_get_max_players)],
                QP_DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.qp_get_deadline)],
                QP_AUTO_TEAMS: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.qp_get_auto_teams)],
                QP_NUM_TEAMS: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.qp_get_num_teams)],
                QP_FIELD_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.qp_get_field_rate)],
            },
            fallbacks=[
                CommandHandler('cancel', self.qp_cancel),
                CommandHandler('quickpoll', self.quickpoll_start),
            ],
            allow_reentry=True,
            name='quickpoll_setup',
            persistent=True,
        )
        self.application.add_handler(quickpoll_handler)

        # Cancel quickpoll conversation (asks for reason first)
        cancelquickpoll_handler = ConversationHandler(
            entry_points=[CommandHandler('cancelquickpoll', self.cancelquickpoll_cmd, filters=filters.ChatType.PRIVATE)],
            states={
                CANCEL_QP_GROUP: [
                    CommandHandler('skip', self.cancel_qp_group_skip),
                    MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.cancel_qp_group_pick),
                ],
                CANCEL_QP_REASON: [
                    CommandHandler('skip', self.cancel_qp_skip),
                    MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.cancel_qp_reason),
                ],
            },
            fallbacks=[CommandHandler('cancel', self.cancel_qp_skip)],
            allow_reentry=True,
            name='cancel_quickpoll',
            persistent=True,
        )
        self.application.add_handler(cancelquickpoll_handler)

        # Wallet conversations: custom top-up amount + cash-out
        topup_custom_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.topup_custom_start, pattern='^topup:custom$')],
            states={
                TOPUP_CUSTOM_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.topup_custom_amount)],
            },
            fallbacks=[CommandHandler('cancel', self.topup_cancel)],
        )
        self.application.add_handler(topup_custom_handler)

        cashout_handler = ConversationHandler(
            entry_points=[CommandHandler('cashout', self.cashout_start, filters=filters.ChatType.PRIVATE)],
            states={
                CASHOUT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.cashout_amount)],
                CASHOUT_HANDLE: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.cashout_handle)],
            },
            fallbacks=[CommandHandler('cancel', self.cashout_cancel)],
        )
        self.application.add_handler(cashout_handler)

        # Standalone commands
        self.application.add_handler(CommandHandler('start', self.start_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('setchat', self.set_chat, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(ChatMemberHandler(self.handle_bot_added_to_group, ChatMemberHandler.MY_CHAT_MEMBER))
        self.application.add_handler(CommandHandler('switchgroup', self.switchgroup_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('mygroups', self.mygroups_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('addadmin', self.addadmin_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('removeadmin', self.removeadmin_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('listadmins', self.listadmins_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('listchats', self.listchats_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('setskill', self.setskill_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('skills', self.skills_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('deleteskill', self.deleteskill_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('viewlate', self.viewlate_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('addlate', self.addlate_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('removelate', self.removelate_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('clearlate', self.clearlate_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('maketeams', self.maketeams_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('closepoll', self.closepoll_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('refreshpoll', self.refreshpoll_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('addplayer', self.addplayer_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('removeplayer', self.removeplayer_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('nudge', self.nudge_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('addmember', self.addmember_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('removemember', self.removemember_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('members', self.members_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('wallet', self.wallet_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('topup', self.topup_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('voidpayment', self.voidpayment_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('deletepayment', self.deletepayment_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('adjustbalance', self.adjustbalance_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('sendvenmolink', self.sendvenmolink_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('wallethistory', self.wallethistory_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('waive', self.waive_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('initchats', self.initchats_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('pollreport', self.pollreport_cmd, filters=filters.ChatType.PRIVATE))

        # Approval callbacks (approve/discard buttons on admin DMs)
        self.application.add_handler(CallbackQueryHandler(self.handle_approval_callback, pattern='^(approve|discard):'))
        # All other inline callbacks (votes, status, etc.)
        self.application.add_handler(CallbackQueryHandler(self.handle_callback))

        # Late arrivals input (admin responding to bot prompt in private)
        self.application.add_handler(MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            self.handle_late_arrivals_input
        ))

        # Unknown message fallback for private chats
        self.application.add_handler(MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT,
            self.unknown_message_handler
        ))

        # Clean up group commands (delete them to reduce spam)
        self.application.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.COMMAND, self.delete_group_commands), group=1)

        logger.info("Bot starting with webhook mode...")
        self.application.run_webhook(
            listen="0.0.0.0",
            port=WEBHOOK_PORT,
            url_path="/webhook",
            webhook_url=f"{WEBHOOK_URL}/webhook",
            secret_token=WEBHOOK_SECRET if WEBHOOK_SECRET else None,
            allowed_updates=Update.ALL_TYPES,
        )

if __name__ == '__main__':
    BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    if not BOT_TOKEN:
        print("Set TELEGRAM_BOT_TOKEN")
        exit(1)
    SoccerBotV2(BOT_TOKEN).run()
