"""
Soccer Bot v2 - Season-based poll automation with webhook + DB scheduling
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    PicklePersistence,
    PollAnswerHandler,
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

LOCATION_NAME, LOCATION_LINK, GAME_DAY, GAME_TIME_START, GAME_TIME_END, START_DATE, DURATION, MAX_PLAYERS = range(8)

# States for quickpoll conversation
QP_CHOOSE_TYPE, QP_LOCATION_NAME, QP_LOCATION_LINK, QP_DATE, QP_TIME_START, QP_TIME_END, QP_MAX_PLAYERS, QP_DEADLINE, QP_AUTO_TEAMS, QP_NUM_TEAMS = range(100, 110)

# States for late arrivals input
AWAITING_LATE_ARRIVALS_INPUT = 110


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
        conn.commit()
        conn.close()

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

    async def newseason_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data['admin_id'] = update.effective_user.id
        context.user_data['setup_chat_id'] = update.effective_chat.id  # Remember which chat started setup
        logger.info(f"newseason started by user {update.effective_user.id} in chat {update.effective_chat.id}")
        await self.send(update, "🏟️ *New Season Setup*\n\nStep 1/8: Enter *location name*:")
        return LOCATION_NAME

    async def get_location_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info(f"get_location_name called by user {update.effective_user.id}: {update.message.text}")
        context.user_data['location_name'] = update.message.text
        await self.send(update, "Step 2/8: Enter *Google Maps link*:")
        return LOCATION_LINK

    async def get_location_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data['location_link'] = update.message.text
        await self.send(update, "Step 3/8: Enter *game day* (e.g., Thursday):")
        return GAME_DAY

    async def get_game_day(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        day = update.message.text.strip().capitalize()
        valid_days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        if day not in valid_days:
            await self.send(update, f"Invalid day. Choose: {', '.join(valid_days)}")
            return GAME_DAY
        context.user_data['game_day'] = day
        await self.send(update, "Step 4/8: Enter *start time* (e.g., 19:00):")
        return GAME_TIME_START

    async def get_time_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data['start_time'] = update.message.text.strip()
        await self.send(update, "Step 5/8: Enter *end time* (e.g., 21:00):")
        return GAME_TIME_END

    async def get_time_end(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data['end_time'] = update.message.text.strip()
        await self.send(update, "Step 6/8: Enter *first game date* (YYYY-MM-DD):")
        return START_DATE

    async def get_start_date(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            datetime.strptime(update.message.text.strip(), '%Y-%m-%d')
            context.user_data['start_date'] = update.message.text.strip()
        except ValueError:
            await self.send(update, "Invalid format. Use YYYY-MM-DD:")
            return START_DATE
        await self.send(update, "Step 7/8: Enter *duration in weeks* (e.g., 10):")
        return DURATION

    async def get_duration(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            context.user_data['duration'] = int(update.message.text.strip())
        except ValueError:
            await self.send(update, "Enter a number:")
            return DURATION
        await self.send(update, "Step 8/8: Enter *max players* (e.g., 15):")
        return MAX_PLAYERS

    async def get_max_players(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            context.user_data['max_players'] = int(update.message.text.strip())
        except ValueError:
            await self.send(update, "Enter a number:")
            return MAX_PLAYERS
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE season SET active = 0")
        c.execute("UPDATE scheduled_events SET executed = 1 WHERE executed = 0 AND event_type IN ('send_poll', 'update_countdown', 'send_reminder', 'close_poll')")
        c.execute('''INSERT INTO season (location_name, location_link, game_day, start_time, end_time, 
                     start_date, duration_weeks, max_players) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                  (context.user_data['location_name'], context.user_data['location_link'],
                   context.user_data['game_day'], context.user_data['start_time'], context.user_data['end_time'],
                   context.user_data['start_date'], context.user_data['duration'], context.user_data['max_players']))
        season_id = c.lastrowid
        conn.commit()
        conn.close()

        self.schedule_season_polls(season_id)
        
        summary = f"""✅ *Season Created!*
📍 {context.user_data['location_name']}
🗓️ Every {context.user_data['game_day']}
🕐 {context.user_data['start_time']} - {context.user_data['end_time']}
📅 Starts: {context.user_data['start_date']}
⏳ Duration: {context.user_data['duration']} weeks
👥 Max: {context.user_data['max_players']} players

Polls sent 3 days before each game at noon."""
        await self.send(update, summary)
        return ConversationHandler.END

    async def cancel_setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.send(update, "Setup cancelled.")
        return ConversationHandler.END

    def schedule_season_polls(self, season_id: int):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT * FROM season WHERE id = ?", (season_id,))
        season = c.fetchone()
        c.execute("SELECT value FROM settings WHERE key = 'chat_id'")
        chat_result = c.fetchone()
        conn.close()

        if not season or not chat_result:
            return

        chat_id = int(chat_result[0])
        start_date = datetime.strptime(season[6], '%Y-%m-%d')
        duration = season[7]

        for week in range(duration):
            game_date = start_date + timedelta(weeks=week)
            poll_date = game_date - timedelta(days=3)
            poll_date = poll_date.replace(hour=12, minute=0, second=0)
            
            if TZ.localize(poll_date) > datetime.now(TZ):
                self.schedule_event('send_poll', TZ.localize(poll_date), {
                    'season_id': season_id, 'week': week + 1,
                    'game_date': game_date.strftime('%Y-%m-%d'), 'chat_id': chat_id
                })

    async def send_poll(self, season_id: int, week: int, game_date: str, chat_id: int):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT * FROM season WHERE id = ?", (season_id,))
        season = c.fetchone()
        conn.close()

        if not season or not season[10]:
            return

        location_name, location_link = season[1], season[2]
        start_time, end_time = season[4], season[5]
        duration_weeks, max_players = season[7], season[8]

        now = datetime.now(TZ)
        deadline = now + timedelta(hours=48)
        deadline_str = deadline.strftime('%a %b %d at %I:%M %p')

        keyboard = [
            [InlineKeyboardButton("✅ IN (Members)", callback_data=f"vote_{season_id}_{week}_member_in")],
            [InlineKeyboardButton("❌ OUT (Members)", callback_data=f"vote_{season_id}_{week}_member_out")],
            [InlineKeyboardButton("👥 Guest", callback_data=f"vote_{season_id}_{week}_guest")],
            [InlineKeyboardButton("📊 Status", callback_data=f"status_{season_id}_{week}")],
        ]

        game_dt = datetime.strptime(game_date, '%Y-%m-%d')
        msg = f"""⚽ *Soccer @ {location_name}*
📍 [Click for directions]({location_link})
🗓️ {game_dt.strftime('%A %b %d')} | {start_time} - {end_time}
👥 Max: {max_players} | Week {week} of {duration_weeks}

📢 Voting closes: {deadline_str}
⏳ ~48 hours remaining
Miss it = Miss the game. No exceptions."""

        poll_msg = await self.application.bot.send_message(
            chat_id=chat_id, text=msg, reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown', disable_web_page_preview=True
        )

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('INSERT INTO polls (season_id, week_number, game_date, message_id, chat_id, deadline) VALUES (?, ?, ?, ?, ?, ?)',
                  (season_id, week, game_date, poll_msg.message_id, chat_id, deadline.isoformat()))
        poll_id = c.lastrowid
        c.execute("UPDATE season SET current_week = ? WHERE id = ?", (week, season_id))
        conn.commit()
        conn.close()

        self.schedule_poll_updates(poll_id, deadline, chat_id, poll_msg.message_id, season_id, week)

    def schedule_poll_updates(self, poll_id: int, deadline: datetime, chat_id: int, msg_id: int, season_id: int, week: int):
        now = datetime.now(TZ)
        for hours in [24, 12, 6, 2, 1]:
            update_time = deadline - timedelta(hours=hours)
            if update_time > now:
                self.schedule_event('update_countdown', TZ.localize(update_time), {
                    'poll_id': poll_id, 'msg_id': msg_id, 'chat_id': chat_id,
                    'hours_left': hours, 'season_id': season_id, 'week': week
                })
        
        reminder_time = deadline - timedelta(hours=12)
        if reminder_time > now:
            self.schedule_event('send_reminder', TZ.localize(reminder_time), {
                'poll_id': poll_id, 'chat_id': chat_id, 'season_id': season_id, 'week': week
            })
        
        self.schedule_event('close_poll', TZ.localize(deadline), {
            'poll_id': poll_id, 'chat_id': chat_id, 'season_id': season_id, 'week': week
        })

    async def update_countdown(self, poll_id: int, msg_id: int, chat_id: int, hours_left: int):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT p.*, s.* FROM polls p JOIN season s ON p.season_id = s.id WHERE p.id = ?', (poll_id,))
        r = c.fetchone()
        conn.close()
        if not r: return

        game_date, deadline_str = r[3], r[5]
        location_name, location_link = r[10], r[11]
        start_time, end_time = r[13], r[14]
        duration_weeks, max_players, week = r[16], r[17], r[2]
        deadline = datetime.fromisoformat(deadline_str)
        game_dt = datetime.strptime(game_date, '%Y-%m-%d')

        keyboard = [
            [InlineKeyboardButton("✅ IN (Members)", callback_data=f"vote_{r[1]}_{week}_member_in")],
            [InlineKeyboardButton("❌ OUT (Members)", callback_data=f"vote_{r[1]}_{week}_member_out")],
            [InlineKeyboardButton("👥 Guest", callback_data=f"vote_{r[1]}_{week}_guest")],
            [InlineKeyboardButton("📊 Status", callback_data=f"status_{r[1]}_{week}")],
        ]

        msg = f"""⚽ *Soccer @ {location_name}*
📍 [Click for directions]({location_link})
🗓️ {game_dt.strftime('%A %b %d')} | {start_time} - {end_time}
👥 Max: {max_players} | Week {week} of {duration_weeks}

📢 Voting closes: {deadline.strftime('%a %b %d at %I:%M %p')}
⏳ ~{hours_left} hours remaining
Miss it = Miss the game. No exceptions."""

        try:
            await self.application.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=msg,
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown', disable_web_page_preview=True)
        except: pass

    async def send_nonvoter_reminder(self, poll_id: int, chat_id: int):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT username FROM members")
        all_members = {r[0].lower() for r in c.fetchall() if r[0]}
        c.execute("SELECT LOWER(username) FROM votes WHERE poll_id = ?", (poll_id,))
        voted = {r[0] for r in c.fetchall() if r[0]}
        conn.close()

        non_voter_names = sorted(all_members - voted)
        if not non_voter_names:
            return

        mentions_list = [f"@{name.replace('_', chr(92) + '_')}" for name in non_voter_names]
        mentions = ' '.join(mentions_list)
        await self.application.bot.send_message(chat_id=chat_id,
            text=f"⚠️ *12 hours left!* These members haven't voted:\n{mentions}\n\nVote now or you're out!", parse_mode='Markdown')

    async def close_poll(self, poll_id: int, chat_id: int, season_id: int, week: int):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE polls SET closed = 1 WHERE id = ?", (poll_id,))
        c.execute('SELECT username, vote_type FROM votes WHERE poll_id = ? ORDER BY voted_at', (poll_id,))
        votes = c.fetchall()
        c.execute("SELECT max_players, duration_weeks FROM season WHERE id = ?", (season_id,))
        season = c.fetchone()
        conn.commit()
        conn.close()

        max_players = season[0]
        members_in = [v[0] for v in votes if v[1] == 'member_in']
        guests = [v[0] for v in votes if v[1] == 'guest']
        spots_left = max_players - len(members_in)
        selected_guests = guests[:spots_left] if spots_left > 0 else []

        msg = f"🏁 *FINAL LIST - Week {week}*\n\n"
        if members_in:
            safe_members = [m.replace('_', '\\_') for m in members_in]
            msg += f"*Members ({len(members_in)}):*\n" + '\n'.join([f"👤 {m}" for m in safe_members]) + "\n\n"
        if selected_guests:
            safe_guests = [g.replace('_', '\\_') for g in selected_guests]
            msg += f"*Guests ({len(selected_guests)}):*\n" + '\n'.join([f"👥 {g}" for g in safe_guests]) + "\n\n"
        msg += f"*Total: {len(members_in) + len(selected_guests)}/{max_players}*"

        await self.application.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
        if week >= season[1]: await self.prompt_season_end(chat_id)

    async def prompt_season_end(self, chat_id: int):
        keyboard = [[InlineKeyboardButton("🔄 Renew", callback_data="season_renew")],
                    [InlineKeyboardButton("✏️ Modify", callback_data="season_modify")],
                    [InlineKeyboardButton("⏹️ Stop", callback_data="season_stop")]]
        await self.application.bot.send_message(chat_id=chat_id, text="🏁 *Season Ended!* What's next?",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data.split('_')

        if data[0] == 'vote':
            season_id, week = int(data[1]), int(data[2])
            vote_type = '_'.join(data[3:])
            await self.process_vote(query, season_id, week, vote_type)
        elif data[0] == 'status':
            await self.show_status(query, int(data[1]), int(data[2]))
        elif data[0] == 'season':
            await self.handle_season_action(query, data[1])
        elif data[0] == 'qvote':
            # Quick poll vote
            poll_id = int(data[1])
            vote_type = data[2]
            await self.process_quickpoll_vote(query, poll_id, vote_type)
        elif data[0] == 'qstatus':
            # Quick poll status
            poll_id = int(data[1])
            await self.show_quickpoll_status(query, poll_id)

    async def process_vote(self, query, season_id: int, week: int, vote_type: str):
        user = query.from_user
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT id, closed FROM polls WHERE season_id = ? AND week_number = ?", (season_id, week))
        poll = c.fetchone()
        
        if not poll:
            await query.answer("Poll not found!", show_alert=True)
            conn.close()
            return
        if poll[1]:
            await query.answer("Voting is closed!", show_alert=True)
            conn.close()
            return

        c.execute('INSERT OR REPLACE INTO votes (poll_id, user_id, username, vote_type) VALUES (?, ?, ?, ?)',
                  (poll[0], user.id, user.username or user.first_name, vote_type))
        conn.commit()
        conn.close()
        emoji = {'member_in': '✅', 'member_out': '❌', 'guest': '👥'}
        await query.answer(f"{emoji.get(vote_type, '✅')} Vote recorded!")

    async def show_status(self, query, season_id: int, week: int):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT id FROM polls WHERE season_id = ? AND week_number = ?", (season_id, week))
        poll = c.fetchone()
        c.execute("SELECT max_players FROM season WHERE id = ?", (season_id,))
        season = c.fetchone()
        counts = {}
        if poll:
            c.execute("SELECT vote_type, COUNT(*) FROM votes WHERE poll_id = ? GROUP BY vote_type", (poll[0],))
            counts = dict(c.fetchall())
        conn.close()

        members_in = counts.get('member_in', 0)
        members_out = counts.get('member_out', 0)
        guests = counts.get('guest', 0)
        max_p = season[0] if season else 15
        await query.answer(f"📊 IN: {members_in} | OUT: {members_out} | Guests: {guests} | Total: {members_in+guests}/{max_p}", show_alert=True)

    async def handle_season_action(self, query, action: str):
        await query.answer()
        if action == 'stop':
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("UPDATE season SET active = 0")
            conn.commit()
            conn.close()
        await query.message.reply_text("Use /newseason to set up a new season." if action != 'stop' else "✅ Season stopped.")

    async def process_quickpoll_vote(self, query, poll_id: int, vote_type: str):
        """Process a vote on a quick poll"""
        user = query.from_user
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Create votes table for quickpolls if not exists
        c.execute('''CREATE TABLE IF NOT EXISTS quickpoll_votes (
            id INTEGER PRIMARY KEY, poll_id INTEGER, user_id INTEGER, username TEXT, vote_type TEXT,
            voted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(poll_id, user_id))''')
        
        c.execute('INSERT OR REPLACE INTO quickpoll_votes (poll_id, user_id, username, vote_type) VALUES (?, ?, ?, ?)',
                  (poll_id, user.id, user.username or user.first_name, vote_type))
        conn.commit()
        conn.close()
        
        emoji = {'in': '✅', 'out': '❌', 'guest': '👥'}
        label = {'in': 'IN', 'out': 'OUT', 'guest': 'Guest'}
        await query.answer(f"{emoji.get(vote_type, '✅')} You voted {label.get(vote_type, vote_type)} — tap another to change")

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
        guest_count = counts.get('guest', 0)
        
        vote_labels = {'in': '✅ IN', 'out': '❌ OUT', 'guest': '👥 Guest'}
        my_vote_str = vote_labels.get(my_vote[0], 'Unknown') if my_vote else 'Not voted yet'
        
        await query.answer(f"📊 Your vote: {my_vote_str}\n\nIN: {in_count} | OUT: {out_count} | Guests: {guest_count} | Total: {in_count + guest_count}/{max_players}", show_alert=True)

    async def add_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await self.send(update, "Usage: /addmember Name")
            return
        
        username = ' '.join(context.args).lstrip('@')
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO members (username, member_type) VALUES (?, 'member')", (username,))
        conn.commit()
        conn.close()
        safe_username = username.replace('_', '\\_')
        await self.send(update, f"✅ Added {safe_username}")

    async def add_regular(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await self.send(update, "Usage: /addregular Name")
            return
        
        username = ' '.join(context.args).lstrip('@')
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO members (username, member_type) VALUES (?, 'regular')", (username,))
        conn.commit()
        conn.close()
        safe_username = username.replace('_', '\\_')
        await self.send(update, f"✅ Added {safe_username} as a regular (drop-in)")

    async def remove_regular(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await self.send(update, "Usage: /removeregular Name")
            return
        
        username = ' '.join(context.args).lstrip('@')
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM members WHERE username = ? AND member_type = 'regular'", (username,))
        conn.commit()
        conn.close()
        safe_username = username.replace('_', '\\_')
        await self.send(update, f"✅ Removed regular {safe_username}")

    async def remove_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await self.send(update, "Usage: /removemember Name")
            return
        
        username = ' '.join(context.args).lstrip('@')
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM members WHERE username = ? AND member_type = 'member'", (username,))
        conn.commit()
        conn.close()
        safe_username = username.replace('_', '\\_')
        await self.send(update, f"✅ Removed {safe_username}")

    async def list_members(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT username, member_type FROM members")
        all_members = c.fetchall()
        conn.close()
        if not all_members:
            await self.send(update, "No members. Use /addmember Name or /addregular Name")
            return
        
        members = [m[0] for m in all_members if m[1] != 'regular']
        regulars = [m[0] for m in all_members if m[1] == 'regular']
        
        text = ""
        if members:
            safe = [m.replace('_', '\\_') for m in members]
            text += f"*📋 Members ({len(members)}):*\n" + '\n'.join([f"• {m}" for m in safe])
        if regulars:
            safe = [m.replace('_', '\\_') for m in regulars]
            if text:
                text += "\n\n"
            text += f"*🔄 Regulars ({len(regulars)}):*\n" + '\n'.join([f"• {m}" for m in safe])
        
        await self.send(update, text)

    async def addadmin_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add an admin for the current chat: /addadmin @username or /addadmin user_id"""
        # Check admin authorization - must be existing admin to add new admins
        is_admin, current_chat_id = await self.check_admin(update)
        if not is_admin and current_chat_id is not None:  # Allow if no chat set yet (first admin)
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
            await self.send(update, "❌ No chat set. Use /setchat first.")
            return
        
        chat_id = int(chat_result[0])
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
        
        safe_username = username.replace('_', '\\_')
        await self.send(update, f"✅ Added admin: {safe_username} for chat {chat_id}")

    async def removeadmin_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remove an admin from the current chat: /removeadmin @username or /removeadmin user_id"""
        # Check admin authorization
        is_admin, _ = await self.check_admin(update)
        if not is_admin:
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
            await self.send(update, "❌ No chat set. Use /setchat first.")
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
        
        safe_arg = arg.replace('_', '\\_')
        await self.send(update, f"✅ Removed admin: {safe_arg}")

    async def listadmins_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List all admins for the current chat"""
        # Check admin authorization
        is_admin, _ = await self.check_admin(update)
        if not is_admin:
            await self.send(update, "❌ You are not authorized to use this command.")
            return
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = 'chat_id'")
        chat_result = c.fetchone()
        
        if not chat_result:
            conn.close()
            await self.send(update, "❌ No chat set. Use /setchat first.")
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
            await self.send(update, "📋 You're not admin for any groups yet.\n\nUse /setchat in a group to get started!")
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
            
            # Get IN voters and Guests
            c.execute("SELECT user_id, username FROM quickpoll_votes WHERE poll_id = ? AND vote_type = 'in'", (poll_id,))
            in_voters = c.fetchall()
            c.execute("SELECT user_id, username FROM quickpoll_votes WHERE poll_id = ? AND vote_type = 'guest'", (poll_id,))
            guests = c.fetchall()
            
            all_players = in_voters + guests
            
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
            await self.send(update, "❌ No chat set. Use /setchat in your group first.")
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

    async def set_chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set the target chat for polls. 
        - In group: captures chat ID, deletes command instantly, confirms via DM
        - In private: requires manual chat ID as argument"""
        
        user_id = update.effective_user.id
        username = update.effective_user.username or update.effective_user.first_name
        chat_type = update.effective_chat.type
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        if chat_type in ['group', 'supergroup']:
            # Group mode: /setchat GroupName
            chat_id = update.effective_chat.id
            message_id = update.message.message_id
            
            # Get group name from args or use chat title
            if context.args:
                group_name = context.args[0]
            else:
                group_name = update.effective_chat.title or f"Group{abs(chat_id) % 10000}"
            
            # Store in chat_groups table
            c.execute("INSERT OR REPLACE INTO chat_groups (chat_id, group_name) VALUES (?, ?)",
                      (chat_id, group_name))
            
            # Auto-add this user as admin
            c.execute("INSERT OR REPLACE INTO chat_admins (chat_id, user_id, username) VALUES (?, ?, ?)",
                      (chat_id, user_id, username))
            conn.commit()
            conn.close()
            
            # Note: The command message is deleted by the global delete_group_commands handler.
            
            # Send confirmation to user's private DM
            group_name_escaped = self.escape_markdown(group_name)
            try:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text=f"✅ Group '{group_name_escaped}' registered\!\\n📱 ID: `{chat_id}`\\n🔐 You've been added as admin",
                    parse_mode='MarkdownV2'
                )
            except Exception as e:
                logger.warning(f"Could not DM user {user_id}: {e}")
        
        else:
            # Private chat mode: 
            # 1. /setchat GroupName (uses current DM as target)
            # 2. /setchat <chat_id> <group_name> (sets remote group)
            
            if not context.args:
                await self.send(update, "Usage:\n1. /setchat <GroupName> (to register THIS chat)\n2. /setchat <chat_id> <group_name> (to register a remote group)")
                conn.close()
                return

            if len(context.args) == 1:
                # Use current private chat as the target (good for testing)
                chat_id = update.effective_chat.id
                group_name = context.args[0]
            else:
                try:
                    chat_id = int(context.args[0])
                    group_name = context.args[1]
                except (ValueError, IndexError):
                    await self.send(update, "Invalid format. Usage: /setchat <chat_id> <group_name>")
                    conn.close()
                    return
            
            # Store in chat_groups table
            c.execute("INSERT OR REPLACE INTO chat_groups (chat_id, group_name) VALUES (?, ?)",
                      (chat_id, group_name))
            
            # Auto-add this user as admin
            c.execute("INSERT OR REPLACE INTO chat_admins (chat_id, user_id, username) VALUES (?, ?, ?)",
                      (chat_id, user_id, username))
            conn.commit()
            conn.close()
            
            group_name_escaped = self.escape_markdown(group_name)
            await self.application.bot.send_message(
                chat_id=user_id,
                text=f"✅ Group '{group_name_escaped}' registered\!\n📱 ID: `{chat_id}`\n🔐 You've been added as admin",
                parse_mode='MarkdownV2'
            )


    async def status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT * FROM season WHERE active = 1")
        s = c.fetchone()
        conn.close()
        if not s:
            await self.send(update, "No active season. Use /newseason")
            return
        await self.send(update, f"*Season:* {s[1]}\n🗓️ {s[3]} {s[4]}-{s[5]}\n📅 Week {s[9]}/{s[7]}")

    async def testpoll_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send a test poll immediately to the registered chat"""
        # Check admin authorization
        is_admin, chat_id = await self.check_admin(update)
        if not is_admin:
            await self.send(update, "❌ You are not authorized to use this command.")
            return
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT * FROM season WHERE active = 1")
        season = c.fetchone()
        c.execute("SELECT value FROM settings WHERE key = 'chat_id'")
        chat_result = c.fetchone()
        conn.close()

        if not season:
            await self.send(update, "❌ No active season. Use /newseason first.")
            return
        if not chat_result:
            await self.send(update, "❌ No chat set. Use /setchat in your group first.")
            return

        chat_id = int(chat_result[0])
        # Send a test poll for "week 1" with today's date
        test_date = datetime.now(TZ).strftime('%Y-%m-%d')
        await self.send(update, f"📤 Sending test poll to chat {chat_id}...")
        await self.send_poll(season[0], 1, test_date, chat_id)
        await self.send(update, "✅ Test poll sent! Check your group.")

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
        
        # Step 0: Choose poll type
        keyboard = [
            [
                InlineKeyboardButton("✅ Standard (w/ Guests)", callback_data="qp_type:standard"),
                InlineKeyboardButton("⚡ Simple (FCFS)", callback_data="qp_type:simple")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        msg_text = "⚡ *Quick Poll Setup*\n\nFirst, choose the poll type:\n" \
                   "• **Standard**: IN/OUT + Guest option.\n" \
                   "• **Simple**: IN/OUT only (First-Come-First-Serve)."
        
        await self.send(update, msg_text, reply_markup=reply_markup, parse_mode='Markdown')
        return QP_CHOOSE_TYPE

    async def qp_type_response(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        data = query.data
        if data == "qp_type:standard":
             context.user_data['qp']['allow_guests'] = True
             await query.edit_message_text("✅ Selected: **Standard** (Guests allowed)", parse_mode='Markdown')
        elif data == "qp_type:simple":
             context.user_data['qp']['allow_guests'] = False
             await query.edit_message_text("⚡ Selected: **Simple** (Members only, FCFS)", parse_mode='Markdown')
        
        # Proceed to Step 1
        await self.send(update, "Step 1/8: Enter *location name*:")
        return QP_LOCATION_NAME

    async def qp_get_location_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data['qp']['location_name'] = update.message.text
        await self.send(update, "Step 2/8: Enter *Google Maps link*:")
        return QP_LOCATION_LINK

    async def qp_get_location_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data['qp']['location_link'] = update.message.text
        await self.send(update, "Step 3/8: Enter *game date* (e.g., Feb 10 or 2026-02-10):")
        return QP_DATE

    async def qp_get_date(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data['qp']['date'] = update.message.text.strip()
        await self.send(update, "Step 4/8: Enter *start time* (e.g., 7:00 PM):")
        return QP_TIME_START

    async def qp_get_time_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data['qp']['time_start'] = update.message.text.strip()
        await self.send(update, "Step 5/8: Enter *end time* (e.g., 9:00 PM):")
        return QP_TIME_END

    async def qp_get_time_end(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data['qp']['time_end'] = update.message.text.strip()
        await self.send(update, "Step 6/8: Enter *max players* (e.g., 15):")
        return QP_MAX_PLAYERS

    async def qp_get_max_players(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            max_players = int(update.message.text.strip())
        except ValueError:
            await self.send(update, "Please enter a number:")
            return QP_MAX_PLAYERS
        context.user_data['qp']['max_players'] = max_players
        await self.send(update, "Step 7/8: Enter *voting deadline* in hours (e.g., 2), or *skip* for no deadline:")
        return QP_DEADLINE

    async def qp_get_deadline(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip().lower()
        if text in ('skip', 'no', 'n'):
            context.user_data['qp']['deadline_hours'] = None
            context.user_data['qp']['auto_teams'] = False
            context.user_data['qp']['num_teams'] = 0
            return await self._send_quickpoll_final(update, context)
        try:
            hours = float(text)
        except ValueError:
            await self.send(update, "Please enter a number of hours, or *skip* for no deadline:")
            return QP_DEADLINE
        context.user_data['qp']['deadline_hours'] = hours
        await self.send(update, "Step 8: Auto-create teams when voting closes? (*yes* or *no*):")
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
        return await self._send_quickpoll_final(update, context)

    async def _send_quickpoll_final(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Shared logic to send quickpoll and optionally schedule teams"""
        qp = context.user_data['qp']
        
        # Get the registered chat for this admin
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Priority 1: Check chat_admins/chat_groups for this user
        # We need to find which chat this user is currently managing/set context for.
        # For now, since we only support 1 active chat per admin in this flow, we'll pick the most recently added or updated.
        c.execute("""
            SELECT ca.chat_id, cg.group_name 
            FROM chat_admins ca
            JOIN chat_groups cg ON ca.chat_id = cg.chat_id
            WHERE ca.user_id = ?
            ORDER BY ca.added_at DESC
            LIMIT 1
        """, (qp['admin_id'],))
        
        chat_result = c.fetchone()
        
        # Fallback to legacy settings if nothing found (backward compatibility)
        if not chat_result:
             c.execute("SELECT value FROM settings WHERE key = 'chat_id'")
             legacy_res = c.fetchone()
             if legacy_res:
                 chat_result = (int(legacy_res[0]), "Legacy Group")

        conn.close()

        if not chat_result:
            await self.send(update, "❌ No chat set. Use /setchat in your group (or /setchat <Name> here) first.")
            return ConversationHandler.END

        chat_id = chat_result[0]
        
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
            allow_guests=qp.get('allow_guests', True)
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
                             deadline_time, num_teams: int, admin_id: int, allow_guests: bool = True):
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

        # Determine options based on poll type
        options = ["IN ✅", "OUT ❌"]
        if allow_guests:
            options.append("Guest 👥")

        # Send native poll as a reply
        poll_msg = await self.application.bot.send_poll(
            chat_id=chat_id,
            question=f"Are you playing on {game_date}? ⚽",
            options=options,
            is_anonymous=False,
            allows_multiple_answers=False,
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
        c.execute('''INSERT INTO quickpolls (id, location_name, max_players, deadline_time, num_teams, chat_id, admin_id, allow_guests, telegram_poll_id, poll_message_id, game_date, time_start)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (poll_id, location_name, max_players, deadline_iso, num_teams, chat_id, admin_id,
                   int(allow_guests), poll_msg.poll.id, poll_msg.message_id, game_date, time_start))
        
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
            
        return None, "❌ No chat context found. Use /setchat or specify a group name."

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
        """Post the final roster — who's playing today (members + guests with continuous numbering)"""
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        c.execute("SELECT max_players, admin_id, allow_guests, game_date, time_start FROM quickpolls WHERE id = ?", (poll_id,))
        poll = c.fetchone()
        if not poll:
            conn.close()
            return
        max_players = poll[0]
        admin_id = poll[1]
        allow_guests = bool(poll[2]) if poll[2] is not None else True
        game_date = poll[3]
        time_start = poll[4]
        
        # Get IN votes (members) ordered chronologically
        c.execute("""SELECT username FROM quickpoll_votes 
                     WHERE poll_id = ? AND vote_type = 'in' 
                     ORDER BY voted_at ASC""", (poll_id,))
        in_voters = [r[0] for r in c.fetchall()]
        
        # Get Guest votes ordered chronologically
        c.execute("""SELECT username FROM quickpoll_votes 
                     WHERE poll_id = ? AND vote_type = 'guest' 
                     ORDER BY voted_at ASC""", (poll_id,))
        guest_voters = [r[0] for r in c.fetchall()]
        conn.close()
        
        # Cap at max_players total (members first, then guests fill remaining)
        members_in = in_voters[:max_players]
        remaining_slots = max(0, max_players - len(members_in))
        guests_in = guest_voters[:remaining_slots]
        
        total = len(members_in) + len(guests_in)
        
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
        
        # Continuous numbered list (all players together)
        all_players = members_in + guests_in
        for i, name in enumerate(all_players, 1):
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

        target_chat_id, error = await self.resolve_chat_context(update, context)
        if error:
            await self.send(update, error)
            return

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        # Find the most recent poll specifically for this target chat
        c.execute("SELECT id, chat_id, poll_message_id FROM quickpolls WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1", (target_chat_id,))
        poll = c.fetchone()
        conn.close()
        
        if not poll:
            await self.send(update, "❌ No quickpoll found to close.")
            return
        
        poll_id, chat_id, poll_msg_id = poll
        
        # Stop the native poll
        if poll_msg_id:
            try:
                await self.application.bot.stop_poll(chat_id=chat_id, message_id=poll_msg_id)
            except Exception:
                pass
        
        # Post the roster (goes to admin approval unless force_send=True)
        await self.post_roster(poll_id, chat_id)
        await self.send(update, "✅ Poll closed! Check your DMs to approve and post the roster.")

    async def handle_poll_answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle native poll votes — track in DB for team creation"""
        answer = update.poll_answer
        telegram_poll_id = answer.poll_id
        user = answer.user

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT id FROM quickpolls WHERE telegram_poll_id = ?", (telegram_poll_id,))
        result = c.fetchone()

        if not result:
            conn.close()
            return

        poll_id = result[0]
        
        # Check if user is blocked from this poll (was late to previous poll)
        username = user.username or user.first_name
        c.execute("""SELECT 1 FROM late_arrivals 
                     WHERE blocked_from_poll_id = ? AND LOWER(username) = LOWER(?)
                     AND cleared_at IS NULL""",
                  (poll_id, username))
        if c.fetchone():
            # User is blocked - reject their vote
            c.execute('DELETE FROM quickpoll_votes WHERE poll_id = ? AND user_id = ?', (poll_id, user.id))
            conn.commit()
            conn.close()
            
            # Notify user
            try:
                await self.application.bot.send_message(
                    chat_id=user.id,
                    text="⚠️ You arrived late to the previous game and cannot participate in this poll.\nTry to be on time next time! ⏰"
                )
            except Exception as e:
                logger.warning(f"Could not send blocked user notification to {user.id}: {e}")
            return
        
        option_map = {0: 'in', 1: 'out', 2: 'guest'}

        if answer.option_ids:  # User voted
            vote_type = option_map.get(answer.option_ids[0], 'in')
            c.execute('INSERT OR REPLACE INTO quickpoll_votes (poll_id, user_id, username, vote_type) VALUES (?, ?, ?, ?)',
                      (poll_id, user.id, username, vote_type))
        else:  # User retracted vote
            c.execute('DELETE FROM quickpoll_votes WHERE poll_id = ? AND user_id = ?', (poll_id, user.id))

        conn.commit()
        conn.close()

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
        
        # Get guest votes ordered by time
        c.execute("""SELECT user_id, username, voted_at FROM quickpoll_votes 
                     WHERE poll_id = ? AND vote_type = 'guest' 
                     ORDER BY voted_at ASC""", (poll_id,))
        guest_votes = c.fetchall()
        
        # Build final player list (first come first serve up to max)
        final_players = []
        
        # Add members first (up to max)
        for user_id, username, voted_at in in_votes:
            if len(final_players) >= max_players:
                break
            final_players.append({'user_id': user_id, 'username': username})
        
        # Fill remaining slots with guests
        for user_id, username, voted_at in guest_votes:
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
                     WHERE poll_id = ? AND vote_type IN ('in', 'guest')
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
                    if event_type == 'send_poll':
                        await self.send_poll(payload['season_id'], payload['week'], payload['game_date'], payload['chat_id'])
                    elif event_type == 'update_countdown':
                        await self.update_countdown(payload['poll_id'], payload['msg_id'], payload['chat_id'], payload['hours_left'])
                    elif event_type == 'send_reminder':
                        await self.send_nonvoter_reminder(payload['poll_id'], payload['chat_id'])
                    elif event_type == 'close_poll':
                        await self.close_poll(payload['poll_id'], payload['chat_id'], payload['season_id'], payload['week'])
                    elif event_type == 'close_quickpoll':
                        # Auto-close native poll + post roster
                        qp_poll_id = payload['poll_id']
                        qp_chat_id = payload['chat_id']
                        cconn = sqlite3.connect(DB_FILE)
                        cc = cconn.cursor()
                        cc.execute("SELECT poll_message_id FROM quickpolls WHERE id = ?", (qp_poll_id,))
                        qp_row = cc.fetchone()
                        cconn.close()
                        if qp_row and qp_row[0]:
                            try:
                                await self.application.bot.stop_poll(chat_id=qp_chat_id, message_id=qp_row[0])
                            except Exception:
                                pass
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
        await self.process_pending_events()
        asyncio.create_task(self.periodic_event_check())
        logger.info("Bot startup complete.")

    # ===== CANCELLATION COMMANDS =====

    async def cancelquickpoll_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel the most recent quickpoll: /cancelquickpoll"""
        if update.effective_chat.type in ['group', 'supergroup']:
            await self.delete_message_safely(update.effective_chat.id, update.message.message_id)

        target_chat_id, error = await self.resolve_chat_context(update, context)
        if error:
            await self.send(update, error)
            return

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        # Find cancelable poll for this specific chat
        c.execute("SELECT id, chat_id FROM quickpolls WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1", (target_chat_id,))
        poll = c.fetchone()

        if not poll:
            await self.send(update, "❌ No quickpoll found to cancel.")
            conn.close()
            return

        poll_id, chat_id = poll

        # Cancel pending finalize_teams events for this poll
        c.execute("SELECT id, payload FROM scheduled_events WHERE event_type = 'finalize_teams' AND executed = 0")
        for eid, payload_json in c.fetchall():
            payload = json.loads(payload_json)
            if payload.get('poll_id') == poll_id:
                c.execute("UPDATE scheduled_events SET executed = 1 WHERE id = ?", (eid,))

        # Stop the native poll if it exists
        c.execute("SELECT poll_message_id FROM quickpolls WHERE id = ?", (poll_id,))
        res = c.fetchone()
        poll_msg_id = res[0] if res else None
        
        # Determine who to ask for approval (the user who ran the command)
        admin_id = update.effective_user.id
        
        if poll_msg_id:
            try:
                await self.application.bot.stop_poll(chat_id=chat_id, message_id=poll_msg_id)
            except Exception:
                pass
        
        conn.commit()
        conn.close()
        
        # Instead of posting to group immediately, ask admin for approval
        await self.request_approval(
            admin_id, 
            "❌ *Quick poll has been cancelled!*", 
            f"cancel:{poll_id}:{chat_id}",
            "Post cancellation notice to group?"
        )
        
        await self.send(update, "✅ Quick poll cancelled (approval request sent).")

    async def cancelgame_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel a game session in the active season: /cancelgame [week]"""
        # Determine target chat context
        if update.effective_chat.type in ['group', 'supergroup']:
            target_chat_id = update.effective_chat.id
        else:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("""
                SELECT ca.chat_id FROM chat_admins ca
                JOIN chat_groups cg ON ca.chat_id = cg.chat_id
                WHERE ca.user_id = ?
                ORDER BY ca.added_at DESC LIMIT 1
            """, (update.effective_user.id,))
            res = c.fetchone()
            conn.close()
            
            if res:
                target_chat_id = res[0]
            else:
                target_chat_id = None
        
        # We need to find the season for THIS chat.
        # Currently the schema doesn't link season to chat_id explicitly (it assumes 1 season globally).
        # But let's at least try to be consistent if possible, or just warn if no context.
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT id FROM season WHERE active = 1")
        season = c.fetchone()

        if not season:
            await self.send(update, "❌ No active season.")
            conn.close()
            return

        season_id = season[0]

        # Determine which week to cancel
        if context.args and context.args[0].isdigit():
            week = int(context.args[0])
        else:
            # Find the next upcoming game: check open polls first, then pending send_poll events
            week = None
            c.execute("SELECT week_number FROM polls WHERE season_id = ? AND closed = 0 ORDER BY week_number ASC LIMIT 1", (season_id,))
            result = c.fetchone()
            if result:
                week = result[0]
            else:
                # Check pending send_poll events
                c.execute("SELECT id, payload FROM scheduled_events WHERE event_type = 'send_poll' AND executed = 0 ORDER BY fire_time ASC")
                for _, payload_json in c.fetchall():
                    payload = json.loads(payload_json)
                    if payload.get('season_id') == season_id:
                        week = payload['week']
                        break

            if week is None:
                await self.send(update, "❌ No upcoming game found to cancel.")
                conn.close()
                return

        # Cancel all pending events for this week
        c.execute("SELECT id, payload FROM scheduled_events WHERE executed = 0")
        cancelled = 0
        for eid, payload_json in c.fetchall():
            payload = json.loads(payload_json)
            if payload.get('season_id') == season_id and payload.get('week') == week:
                c.execute("UPDATE scheduled_events SET executed = 1 WHERE id = ?", (eid,))
                cancelled += 1

        # Close the poll if one exists
        c.execute("SELECT message_id, chat_id FROM polls WHERE season_id = ? AND week_number = ?", (season_id, week))
        poll = c.fetchone()
        if poll:
            c.execute("UPDATE polls SET closed = 1 WHERE season_id = ? AND week_number = ?", (season_id, week))
            msg_id, poll_chat_id = poll
            try:
                await self.application.bot.edit_message_text(
                    chat_id=poll_chat_id, message_id=msg_id,
                    text="❌ *This game has been cancelled.*",
                    parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([])
                )
            except Exception:
                pass

        conn.commit()
        conn.close()

        # Send cancellation message to the correct group
        if target_chat_id:
            await self.application.bot.send_message(
                chat_id=target_chat_id,
                text=f"⚠️ *Week {week} game has been cancelled!*",
                parse_mode='Markdown'
            )

        await self.send(update, f"✅ Week {week} cancelled. {cancelled} scheduled events removed.")

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
        
        season_handler = ConversationHandler(
            entry_points=[CommandHandler('newseason', self.newseason_start, filters=filters.ChatType.PRIVATE)],
            states={
                LOCATION_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.get_location_name)],
                LOCATION_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.get_location_link)],
                GAME_DAY: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.get_game_day)],
                GAME_TIME_START: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.get_time_start)],
                GAME_TIME_END: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.get_time_end)],
                START_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.get_start_date)],
                DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.get_duration)],
                MAX_PLAYERS: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.get_max_players)],
            },
            fallbacks=[
                CommandHandler('cancel', self.cancel_setup),
                CommandHandler('newseason', self.newseason_start),  # Allow restart mid-conversation
            ],
            allow_reentry=True,
            name='season_setup',
            persistent=True,
        )

        self.application.add_handler(season_handler)
        

        
        # Quick poll handler (no season required)
        quickpoll_handler = ConversationHandler(
            entry_points=[CommandHandler('quickpoll', self.quickpoll_start, filters=filters.ChatType.PRIVATE)],
            states={
                QP_CHOOSE_TYPE: [CallbackQueryHandler(self.qp_type_response)],
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
                CommandHandler('quickpoll',self.quickpoll_start),
            ],
            allow_reentry=True,
            name='quickpoll_setup',
            persistent=True,
        )
        self.application.add_handler(quickpoll_handler)
        
        # Admin commands with private chat filters
        self.application.add_handler(CommandHandler('setchat', self.set_chat))  # Works in both group and private
        self.application.add_handler(CommandHandler('addadmin', self.addadmin_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('removeadmin', self.removeadmin_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('listadmins', self.listadmins_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('listchats', self.listchats_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('addmember', self.add_member, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('removemember', self.remove_member, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('addregular', self.add_regular, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('removeregular', self.remove_regular, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('members', self.list_members, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('status', self.status_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('testpoll', self.testpoll_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('setskill', self.setskill_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('skills', self.skills_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('deleteskill', self.deleteskill_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('viewlate', self.viewlate_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('addlate', self.addlate_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('removelate', self.removelate_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('clearlate', self.clearlate_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('maketeams', self.maketeams_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('closepoll', self.closepoll_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('cancelquickpoll', self.cancelquickpoll_cmd, filters=filters.ChatType.PRIVATE))
        self.application.add_handler(CommandHandler('cancelgame', self.cancelgame_cmd, filters=filters.ChatType.PRIVATE))
        
        # Handler for late arrivals input (captures admin's response to prompt)
        self.application.add_handler(MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            self.handle_late_arrivals_input
        ))
        
        # Public handlers - anyone can use these
        self.application.add_handler(PollAnswerHandler(self.handle_poll_answer))
        self.application.add_handler(CallbackQueryHandler(self.handle_approval_callback, pattern='^(approve|discard):'))
        self.application.add_handler(CallbackQueryHandler(self.handle_callback))

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
