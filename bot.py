"""
Soccer Bot v2 - Season-based poll automation with webhook + DB scheduling
"""

import os
import json
import asyncio
import logging
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

# States for wallet conversations (custom top-up amount, cash-out)
TOPUP_CUSTOM_AMOUNT = 120
CASHOUT_AMOUNT, CASHOUT_HANDLE = 121, 122

# Cancel quickpoll with reason
CANCEL_QP_REASON = 123

# ===== Payment / wallet config =====
VENMO_HANDLE = '@chico-leo'  # Venmo handle players pay to for top-ups
VOTE_COST = 10.00      # charged per IN vote, refunded on switch to OUT
WALLET_FLOOR = 10.00   # minimum balance required to vote IN
TOPUP_MIN = 20.00      # minimum custom top-up amount

# Super-admin controls: only this user can manage admin lifecycle
_raw_super_admin_id = os.getenv('SUPER_ADMIN_ID', '').strip()
SUPER_ADMIN_ID = int(_raw_super_admin_id) if _raw_super_admin_id.isdigit() else 0

# Role-based command routing for private chats
PLAYER_COMMANDS = {'wallet', 'topup', 'cashout', 'cancel'}
ADMIN_COMMANDS = {
    'quickpoll', 'cancelquickpoll', 'closepoll', 'maketeams',
    'setskill', 'skills', 'deleteskill',
    'viewlate', 'addlate', 'removelate', 'clearlate', 'listchats'
}
SUPER_ADMIN_ONLY_COMMANDS = {'addadmin', 'removeadmin', 'listadmins'}


class SoccerBotV2:
    def __init__(self, token: str):
        self.token = token
        self.application = None
        self._processing = False
        self._pending_teams: dict[str, str] = {}  # key -> prebuilt teams message text
        self._pending_late_arrivals: dict[int, dict] = {}  # admin_id -> {poll_id, chat_id, players_list}
        self.init_database()

    async def send(self, update: Update, text: str, **kwargs):
        """Send message WITHOUT replying - uses direct API call and forwards kwargs"""
        try:
            await self.application.bot.send_message(
                chat_id=update.effective_chat.id, 
                text=text,
                **kwargs
            )
        except Exception as e:
            logger.error(f"Error sending message: {e}")

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

    async def refresh_command_scopes(self):
        """Apply role-based Telegram command menus.
        - Members: player commands only
        - Approved admins: player + admin operations
        - Super admin: all commands (including admin lifecycle)
        """
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
            BotCommand('cancelquickpoll', 'Cancel a poll and refund everyone'),
            BotCommand('maketeams', 'Split players into balanced skill-based teams'),
            BotCommand('setskill', 'Set a player\'s skill rating — /setskill Name 1-10'),
            BotCommand('skills', 'See all player skill ratings'),
            BotCommand('deleteskill', 'Remove a player\'s skill rating'),
            BotCommand('viewlate', 'See who was marked late for a poll'),
            BotCommand('addlate', 'Mark a player as late — /addlate poll_id username'),
            BotCommand('removelate', 'Undo a late mark — /removelate poll_id username'),
            BotCommand('clearlate', 'Clear all late flags for a poll — /clearlate poll_id'),
            BotCommand('listchats', 'See all the groups you manage'),
        ]
        super_cmds = [
            BotCommand('addadmin', 'Give someone admin access — /addadmin @username'),
            BotCommand('removeadmin', 'Revoke admin access — /removeadmin @username'),
            BotCommand('listadmins', 'See all admins for a group'),
        ]

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
        role = 'member'
        if self.is_super_admin(user.id):
            role = 'super'
        elif self.is_admin_any_chat(user.id, user.username):
            role = 'admin'

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
            PRIMARY KEY (chat_id, user_id))''')
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
        conn.commit()
        conn.close()

        # Drop legacy season tables (season feature removed)
        conn2 = sqlite3.connect(DB_FILE)
        c2 = conn2.cursor()
        for tbl in ('votes', 'polls', 'members', 'season'):
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
        """Player tapped 'I've Paid' — credit the wallet (trust-based) and confirm."""
        await query.answer()
        user = query.from_user
        username = user.username or user.first_name
        self.credit_wallet(username, amount, "topup")
        wallet = self.get_wallet(username)
        balance = wallet['balance'] if wallet else amount
        await query.edit_message_text(
            f"✅ *${amount:.2f} added* — your balance is now *${balance:.2f}*.\n\n"
            "You're set for the next few games. Will nudge you when it's time "
            "to top up again.",
            parse_mode='Markdown')

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

    async def close_quickpoll_buttons(self, chat_id: int, message_id):
        """Remove the in/out/status buttons from a quickpoll message to stop voting."""
        if not message_id:
            return
        try:
            await self.application.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=None)
        except Exception as e:
            logger.warning(f"Could not close quickpoll buttons ({message_id}): {e}")

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query

        # Wallet / top-up callbacks (colon-delimited, handled before the '_' split)
        if query.data.startswith('topup:'):
            await self.handle_topup_callback(query, query.data.split(':', 1)[1])
            return
        if query.data.startswith('ctopup:'):
            await self.confirm_topup(query, float(query.data.split(':', 1)[1]))
            return

        data = query.data.split('_')

        if data[0] == 'qvote':
            # Quick poll vote
            poll_id = int(data[1])
            vote_type = data[2]
            await self.process_quickpoll_vote(query, poll_id, vote_type)
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

        # Poll must still exist and be open
        c.execute("SELECT deadline_time, max_players FROM quickpolls WHERE id = ?", (poll_id,))
        prow = c.fetchone()
        if not prow:
            conn.close()
            await query.answer("This poll no longer exists.", show_alert=True)
            return
        max_players = prow[1]
        if prow[0]:
            try:
                deadline = datetime.fromisoformat(prow[0])
                if deadline.tzinfo is None:
                    deadline = TZ.localize(deadline)
                if datetime.now(TZ) > deadline:
                    conn.close()
                    await query.answer("⏰ Voting has closed for this poll.", show_alert=True)
                    return
            except (ValueError, TypeError):
                pass

        # Late-arrival block — players blocked from this poll cannot vote
        c.execute("""SELECT 1 FROM late_arrivals
                     WHERE blocked_from_poll_id = ? AND LOWER(username) = LOWER(?)
                     AND cleared_at IS NULL""", (poll_id, username))
        if c.fetchone():
            conn.close()
            await query.answer(
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
            await query.answer(f"You already voted {vote_type.upper()}.")
            return

        # Switching INTO 'in' — enforce the capacity cap, then the wallet gate
        if vote_type == 'in':
            c.execute("SELECT COUNT(*) FROM quickpoll_votes WHERE poll_id = ? AND vote_type = 'in'", (poll_id,))
            in_count = c.fetchone()[0]
            if max_players and in_count >= max_players:
                conn.close()
                await query.answer(
                    f"⚽ This game is full — all {max_players} spots are taken.",
                    show_alert=True)
                return
            eligible, reason = self.check_wallet_eligible(username)
            if not eligible:
                conn.close()
                await query.answer(reason, show_alert=True)
                await self.send_topup_prompt(user.id, reason)
                return

        # Record the vote
        c.execute('INSERT OR REPLACE INTO quickpoll_votes (poll_id, user_id, username, vote_type) VALUES (?, ?, ?, ?)',
                  (poll_id, user.id, username, vote_type))
        conn.commit()
        conn.close()

        # Money: charge on entering 'in', refund on leaving 'in'
        charged = False
        if old_vote != 'in' and vote_type == 'in':
            self.deduct_wallet(username, VOTE_COST, f"quickpoll_vote:{poll_id}")
            charged = True
        elif old_vote == 'in' and vote_type != 'in':
            self.credit_wallet(username, VOTE_COST, f"quickpoll_refund:{poll_id}")

        # Confirmation popup
        if vote_type == 'in':
            await query.answer(f"✅ You're IN — ${VOTE_COST:.0f} deducted from your wallet.")
        else:
            note = f" ${VOTE_COST:.0f} refunded." if old_vote == 'in' else ""
            await query.answer(f"❌ You're OUT.{note}")

        # Low-balance nudge after a charge
        if charged:
            wallet = self.get_wallet(username)
            if wallet and wallet['balance'] <= WALLET_FLOOR:
                try:
                    await self.application.bot.send_message(
                        chat_id=user.id,
                        text=self.low_balance_text(wallet['balance']),
                        parse_mode='Markdown')
                except Exception as e:
                    logger.warning(f"Could not send low-balance nudge to {user.id}: {e}")

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

    async def addadmin_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add an admin for the current chat: /addadmin @username or /addadmin user_id"""
        # Super-admin only: admin lifecycle is centrally controlled.
        if not self.is_super_admin(update.effective_user.id):
            await self.send(update, "❌ You are not authorized to use this command.")
            return
        
        if not context.args:
            await self.send(update, "Usage: /addadmin @username or /addadmin <user_id>")
            return
        
        # Get current chat_id from settings
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = 'chat_id'")
        chat_result = c.fetchone()
        
        if not chat_result:
            conn.close()
            await self.send(update, "❌ No chat set. Add the bot to a group first.")
            return
        
        chat_id = int(chat_result[0])

        if chat_id > 0:
            conn.close()
            await self.send(update, "❌ No valid group chat set. Add the bot to a group first.")
            return

        arg = context.args[0].lstrip('@')
        
        # Check if it's a user_id (numeric) or username
        try:
            new_admin_id = int(arg)
            username = arg  # Will use ID as placeholder
        except ValueError:
            # It's a username — store NULL for user_id; is_admin() will resolve by username
            # and back-fill the real ID on first interaction
            new_admin_id = None
            username = arg

        c.execute("INSERT OR REPLACE INTO chat_admins (chat_id, user_id, username) VALUES (?, ?, ?)",
                  (chat_id, new_admin_id, username))
        conn.commit()
        conn.close()
        await self.refresh_command_scopes()
        
        safe_username = username.replace('_', '\\_')
        await self.send(update, f"✅ Added admin: {safe_username} for chat {chat_id}")

    async def removeadmin_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remove an admin from the current chat: /removeadmin @username or /removeadmin user_id"""
        # Super-admin only: admin lifecycle is centrally controlled.
        if not self.is_super_admin(update.effective_user.id):
            await self.send(update, "❌ You are not authorized to use this command.")
            return
        
        if not context.args:
            await self.send(update, "Usage: /removeadmin @username or /removeadmin <user_id>")
            return
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = 'chat_id'")
        chat_result = c.fetchone()
        
        if not chat_result:
            conn.close()
            await self.send(update, "❌ No chat set. Add the bot to a group first.")
            return
        
        chat_id = int(chat_result[0])
        arg = context.args[0].lstrip('@')
        
        # Try both user_id and username
        try:
            user_id = int(arg)
            c.execute("DELETE FROM chat_admins WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        except ValueError:
            c.execute("DELETE FROM chat_admins WHERE chat_id = ? AND username = ?", (chat_id, arg))
        
        conn.commit()
        conn.close()
        await self.refresh_command_scopes()
        
        safe_arg = arg.replace('_', '\\_')
        await self.send(update, f"✅ Removed admin: {safe_arg}")

    async def listadmins_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List all admins for the current chat"""
        # Super-admin only.
        if not self.is_super_admin(update.effective_user.id):
            await self.send(update, "❌ You are not authorized to use this command.")
            return
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = 'chat_id'")
        chat_result = c.fetchone()
        
        if not chat_result:
            conn.close()
            await self.send(update, "❌ No chat set. Add the bot to a group first.")
            return
        
        chat_id = int(chat_result[0])
        c.execute("SELECT user_id, username FROM chat_admins WHERE chat_id = ?", (chat_id,))
        admins = c.fetchall()
        conn.close()
        
        if not admins:
            await self.send(update, f"No admins set for chat {chat_id}. Use /addadmin to add one.")
            return
        
        text = f"*🔐 Admins for chat {chat_id}:*\n"
        for user_id, username in admins:
            safe_username = username.replace('_', '\\_')
            if user_id and user_id != 0:
                text += f"• {safe_username} (ID: {user_id})\n"
            else:
                text += f"• {safe_username}\n"
        
        await self.send(update, text)

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
        
        # Get group chat
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = 'chat_id'")
        chat_result = c.fetchone()
        conn.close()
        
        if not chat_result:
            await self.send(update, "❌ No chat set. Add the bot to a group first.")
            return
        
        chat_id = int(chat_result[0])
        
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
        # Also register whoever added the bot, if different
        if added_by and added_by.id != SUPER_ADMIN_ID:
            c.execute("INSERT OR IGNORE INTO chat_admins (chat_id, user_id, username) VALUES (?, ?, ?)",
                      (chat_id, added_by.id, added_by.username))
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
        group_list = "\n".join([f"{i+1}. {name}" for i, (_, name) in enumerate(groups)])
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
                f"📌 *Last poll for {selected_name}:*\n"
                f"📍 {prev[0]}\n"
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
        await self.send(update, "Step 8/9: Enter *voting deadline* in hours (e.g., 24), or *skip* for no deadline:", parse_mode='Markdown')
        return QP_DEADLINE

    async def qp_get_deadline(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip().lower()
        if text in ('skip', 'no', 'n'):
            context.user_data['qp']['deadline_hours'] = None
            context.user_data['qp']['auto_teams'] = False
            context.user_data['qp']['num_teams'] = context.user_data['qp'].get('num_teams', 0)
            return await self._send_quickpoll_final(update, context)
        try:
            hours = float(text)
        except ValueError:
            await self.send(update, "Please enter a number of hours, or *skip* for no deadline:", parse_mode='Markdown')
            return QP_DEADLINE
        context.user_data['qp']['deadline_hours'] = hours
        await self.send(update, "Step 9/9: Auto-create teams when voting closes? (*yes* or *no*):", parse_mode='Markdown')
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
            return await self._send_quickpoll_final(update, context)
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
        await self.send(update, "✅ Step 10/10: Sending poll...")
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
            admin_id=qp['admin_id']
        )
        
        if deadline_time:
            deadline_str = deadline_time.strftime('%I:%M %p')
            
            # Always schedule auto-close + roster at deadline
            self.schedule_event('close_quickpoll', deadline_time, {
                'poll_id': poll_id, 'chat_id': chat_id
            })
            
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
                             deadline_time, num_teams: int, admin_id: int):
        """Send a quick poll using native Telegram poll with reply-to trick"""
        
        poll_id = int(datetime.now().timestamp())

        msg = f"""⚽ *Soccer session at {location_name}*
📍 [Click for directions]({location_link})
🗓️ {game_date} | {time_start} - {time_end}
👥 Max: {max_players} players"""

        if deadline_time:
            deadline_str = deadline_time.strftime('%b %d at %I:%M %p')
            msg += f"""

⏳ Voting closes: {deadline_str}
❌ Miss it = Miss the game!"""

        # Send info message
        info_msg = await self.application.bot.send_message(
            chat_id=chat_id, text=msg,
            parse_mode='Markdown', disable_web_page_preview=True
        )

        # Send the poll as an inline-button message (reply to the info message)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("in", callback_data=f"qvote_{poll_id}_in"),
            InlineKeyboardButton("out", callback_data=f"qvote_{poll_id}_out"),
            InlineKeyboardButton("status", callback_data=f"qstatus_{poll_id}"),
        ]])
        poll_text = (
            f"⚽ *Are you playing on {game_date}?*\n\n"
            f"Tap *in* or *out* below. Each game costs ${VOTE_COST:.0f} from your "
            "wallet — switch to *out* anytime before the deadline for a full refund.\n\n"
            "Tap *status* to see the current count."
        )
        poll_msg = await self.application.bot.send_message(
            chat_id=chat_id,
            text=poll_text,
            parse_mode='Markdown',
            reply_markup=keyboard,
            reply_to_message_id=info_msg.message_id,
        )

        # Auto-pin the poll
        try:
            await self.application.bot.pin_chat_message(
                chat_id=chat_id, message_id=poll_msg.message_id, disable_notification=True
            )
        except Exception as e:
            logger.warning(f"Could not pin poll: {e}")
        
        # Store poll info
        deadline_iso = deadline_time.isoformat() if deadline_time else None
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''INSERT INTO quickpolls (id, location_name, location_link, max_players, deadline_time, num_teams, chat_id, admin_id, telegram_poll_id, poll_message_id, game_date, time_start, time_end)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (poll_id, location_name, location_link, max_players, deadline_iso, num_teams, chat_id, admin_id,
                   None, poll_msg.message_id, game_date, time_start, time_end))
        
        # Link any pending late arrivals from previous polls to this new poll
        # (auto-link for next poll feature)
        c.execute("""UPDATE late_arrivals SET blocked_from_poll_id = ? 
                     WHERE blocked_from_poll_id IS NULL AND cleared_at IS NULL""",
                  (poll_id,))
        
        conn.commit()
        conn.close()
        
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
            
        # 3. Private context fallback (most recent)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            SELECT ca.chat_id 
            FROM chat_admins ca
            JOIN chat_groups cg ON ca.chat_id = cg.chat_id
            WHERE ca.user_id = ?
            ORDER BY ca.added_at DESC LIMIT 1
        """, (user_id,))
        res = c.fetchone()
        
        # Global fallback (legacy)
        if not res:
             c.execute("SELECT value FROM settings WHERE key = 'chat_id'")
             res = c.fetchone()
             
        conn.close()
        
        if res:
            return int(res[0]), None
            
        return None, "❌ No chat context found. Add the bot to a group first, or specify a group name."

    async def handle_late_arrivals_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle admin's response to late arrivals prompt"""
        user_id = update.effective_user.id
        
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
            await self.post_roster(poll_id, chat_id, force_send=True)

        elif action == 'teams':
            # Post the prebuilt teams message stored in _pending_teams
            teams_key = f"{poll_id}:{chat_id}"
            teams_msg = self._pending_teams.pop(teams_key, None)
            if teams_msg:
                await self.application.bot.send_message(chat_id=chat_id, text=teams_msg, parse_mode='Markdown')
            else:
                await self.application.bot.send_message(chat_id=chat_id, text="⚠️ Teams message expired. Use /maketeams to regenerate.")

        elif action == 'cancel':
            await self.application.bot.send_message(
                chat_id=chat_id,
                text="❌ *Quick poll has been cancelled!*",
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

        # Post the roster (goes to admin approval unless force_send=True)
        await self.post_roster(poll_id, chat_id)
        await self.send(update, "✅ Poll closed! Check your DMs to approve and post the roster.")

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
                        await self.post_roster(qp_poll_id, qp_chat_id)
                    elif event_type == 'finalize_teams':
                        await self.finalize_teams(payload['poll_id'], payload['chat_id'], payload['admin_id'])
                    elif event_type == 'prompt_late_arrivals':
                        await self.prompt_late_arrivals(payload['poll_id'], payload['chat_id'], payload['admin_id'])
                    elif event_type == 'announce_late_arrivals':
                        await self.announce_late_arrivals(payload['poll_id'], payload['chat_id'], payload['admin_id'])
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
        asyncio.create_task(self.periodic_event_check())
        logger.info("Bot startup complete.")

    # ===== CANCELLATION COMMANDS =====

    async def cancelquickpoll_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Step 1: Ask admin for a cancellation reason before proceeding."""
        if update.effective_chat.type in ['group', 'supergroup']:
            await self.delete_message_safely(update.effective_chat.id, update.message.message_id)

        user_id = update.effective_user.id
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        if update.effective_chat.type in ['group', 'supergroup']:
            c.execute("SELECT id, chat_id FROM quickpolls WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1", (update.effective_chat.id,))
        else:
            c.execute("""
                SELECT qp.id, qp.chat_id FROM quickpolls qp
                JOIN chat_admins ca ON qp.chat_id = ca.chat_id
                WHERE ca.user_id = ?
                ORDER BY qp.created_at DESC LIMIT 1
            """, (user_id,))
        poll = c.fetchone()
        conn.close()

        if not poll:
            await self.send(update, "❌ No quickpoll found to cancel.")
            return ConversationHandler.END

        context.user_data['cancel_qp_poll_id'] = poll[0]
        context.user_data['cancel_qp_chat_id'] = poll[1]

        await self.send(update, "What's the reason for cancelling?\n\nSend your reason or /skip to cancel without one.")
        return CANCEL_QP_REASON

    async def cancel_qp_reason(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Step 2a: Got a reason — execute cancellation."""
        reason = update.message.text.strip()
        await self._execute_cancel_quickpoll(update, context, reason)
        return ConversationHandler.END

    async def cancel_qp_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Step 2b: Admin skipped the reason — cancel with no reason given."""
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
        c.execute("SELECT id, payload FROM scheduled_events WHERE event_type IN ('finalize_teams', 'close_quickpoll') AND executed = 0")
        for eid, payload_json in c.fetchall():
            payload = json.loads(payload_json)
            if payload.get('poll_id') == poll_id:
                c.execute("UPDATE scheduled_events SET executed = 1 WHERE id = ?", (eid,))

        # Collect IN voters for refund
        c.execute("SELECT username FROM quickpoll_votes WHERE poll_id = ? AND vote_type = 'in'", (poll_id,))
        in_voters = [r[0] for r in c.fetchall()]

        # Grab message ID to close buttons
        c.execute("SELECT poll_message_id FROM quickpolls WHERE id = ?", (poll_id,))
        res = c.fetchone()
        poll_msg_id = res[0] if res else None

        # Clear votes so refund can't happen twice
        c.execute("DELETE FROM quickpoll_votes WHERE poll_id = ?", (poll_id,))
        conn.commit()
        conn.close()

        admin_id = update.effective_user.id
        await self.close_quickpoll_buttons(chat_id, poll_msg_id)

        # Refund every IN voter
        for voter in in_voters:
            self.credit_wallet(voter, VOTE_COST, f"quickpoll_cancelled:{poll_id}")

        group_text = (
            f"❌ *Game poll cancelled.*\n\n"
            f"📢 Reason: {reason}\n\n"
            f"💸 {len(in_voters)} player(s) have been refunded ${VOTE_COST:.0f} each."
        )
        await self.request_approval(
            admin_id,
            group_text,
            f"cancel:{poll_id}:{chat_id}",
            "Post cancellation notice to group?"
        )

        await self.send(update, f"✅ Quick poll cancelled. {len(in_voters)} player(s) refunded ${VOTE_COST:.0f} each.")

    async def start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Warm welcome message with role-aware guidance."""
        user = update.effective_user
        if self.is_super_admin(user.id):
            role = 'super'
        elif self.is_admin_any_chat(user.id, user.username):
            role = 'admin'
        else:
            role = 'player'

        greeting = "سلام گل گلاب\! بذار بهت بگم چطوری میتونم در خدمتت باشم 🙌\n\n"
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
                "/maketeams — Split voted\-in players into balanced teams\n"
                "/setskill, /skills, /deleteskill — Manage skill ratings for fair team splits\n\n"
                "*💰 Your Wallet:*\n"
                "/wallet — Check your balance and recent activity\n"
                "/topup — Add funds to join games\n"
                "/cashout — Withdraw to Venmo\n\n"
                "Just send /quickpoll to get started\."
            )
        elif role == 'admin':
            body = (
                "*🗳 Game Operations:*\n"
                "/quickpoll — Create a game poll for your group\n"
                "/closepoll — Close voting early and send the final lineup for approval\n"
                "/cancelquickpoll — Cancel a poll and refund everyone automatically\n"
                "/maketeams — Split voted\-in players into balanced teams\n"
                "/setskill, /skills, /deleteskill — Manage skill ratings for fair team splits\n\n"
                "*💰 Your Wallet:*\n"
                "/wallet — Check your balance and recent activity\n"
                "/topup — Add funds to join games\n"
                "/cashout — Withdraw to Venmo\n\n"
                "Just send /quickpoll to get started\."
            )
        else:
            body = (
                "💰 /wallet — Check your balance and recent game activity\.\n"
                "💳 /topup — Add funds to your wallet so you can vote in on games\. Each game costs $10\.\n"
                "💸 /cashout — Withdraw your balance back to Venmo anytime\.\n\n"
                "When there's a game poll in your group, tap *IN* to join — $10 is deducted from your wallet\. Switch to *OUT* before the deadline to get it back\."
            )
        await self.send(update, greeting + body, parse_mode='MarkdownV2')

    async def unknown_message_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Warm reply for unexpected messages in private chat."""
        import random
        responses = [
            "Hmm, not sure what to do with that one\! Try /wallet to check your balance or /topup to add funds\.",
            "That one went over my head\! Here's what I'm good at: /wallet, /topup, /cashout — give one of those a go\.",
            "Not quite my language, but I've got your back for the important stuff\. Start with /wallet to see where things stand\.",
        ]
        await self.send(update, random.choice(responses), parse_mode='MarkdownV2')

    async def delete_group_commands(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Delete any command messages sent in groups to keep bot invisible"""
        if update.effective_chat.type in ['group', 'supergroup'] and update.message and update.message.text:
            if update.message.text.startswith('/'):
                await self.delete_message_safely(update.effective_chat.id, update.message.message_id)
    
    def run(self):
        persistence = PicklePersistence(filepath=PERSISTENCE_FILE)
        self.application = Application.builder().token(self.token).persistence(persistence).post_init(self.on_startup).build()

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
        self.application.add_handler(CommandHandler('wallet', self.wallet_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('topup', self.topup_cmd, filters=filters.ChatType.PRIVATE))

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
