# Soccer Telegram Bot

Automates soccer group management — polls, scheduling, team balancing, and member tracking — for Telegram groups.

## What It Does

- **Season management** — multi-week schedules with automated weekly polls, countdown updates, non-voter reminders, and roster posting at deadline
- **Quickpolls** — one-off game polls with auto team balancing at deadline
- **Member & regular roster** — track season members vs drop-in regulars
- **Skill ratings + balanced teams** — rate players 1–10, auto-generate balanced teams
- **Multi-group support** — one bot managing multiple Telegram groups
- **Admin system** — per-group admin management via DM

## Stack

- Python 3, `python-telegram-bot==21.10`
- SQLite (`soccer_bot_v2.db`) on a persistent Fly.io volume
- Deployed on Fly.io (webhook mode, port 8080, 256MB RAM)
- Scheduled events engine — DB-driven, fires every 5 minutes

## Deploy

```powershell
git pull origin master
flyctl deploy
```

## Admin Commands

All commands are admin-only, run via private DM — except `/setchat` which runs in the group.

### Season
| Command | Description |
|---|---|
| `/newseason` | 8-step wizard: location, Maps link, day, times, start date, weeks, max players |
| `/status` | Show active season info |
| `/testpoll` | Send test poll immediately |
| `/cancelgame [week]` | Cancel a game, post notice to group |

### Quickpolls
| Command | Description |
|---|---|
| `/quickpoll` | 10-step wizard: type, location, date, times, max players, deadline, teams |
| `/closepoll` | Manually close latest quickpoll |
| `/cancelquickpoll` | Cancel latest quickpoll |

### Members
| Command | Description |
|---|---|
| `/addmember Name` | Add season member |
| `/removemember Name` | Remove season member |
| `/addregular Name` | Add drop-in regular |
| `/removeregular Name` | Remove drop-in regular |
| `/members` | List all members and regulars |

### Skills & Teams
| Command | Description |
|---|---|
| `/setskill Name Rating` | Set skill rating (1–10) |
| `/skills` | List all rated players |
| `/deleteskill Name` | Remove skill rating |
| `/maketeams [all] [N]` | Build N balanced teams from poll voters or all rated players |

### Groups & Admins
| Command | Description |
|---|---|
| `/setchat [GroupName]` | Register group (run inside group) |
| `/addadmin @user or ID` | Add group admin |
| `/removeadmin @user or ID` | Remove group admin |
| `/listadmins` | List admins |
| `/listchats` | List all managed groups |

## Environment Variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather — set in Fly.io secrets |
