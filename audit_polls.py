import sqlite3
from datetime import datetime

DB = "/data/soccer_bot_v2.db"
conn = sqlite3.connect(DB)
c = conn.cursor()

c.execute("SELECT chat_id, group_name FROM chat_groups")
groups = c.fetchall()
print("=== REGISTERED GROUPS ===")
for g in groups:
    print("  chat_id={}  name={}".format(g[0], g[1]))

tuesday_chat = None
for cid, gname in groups:
    if "tuesday" in (gname or "").lower():
        tuesday_chat = cid
        break
print("Target chat_id: {}".format(tuesday_chat))

START = datetime(2026, 3, 1)
END   = datetime(2026, 6, 30, 23, 59, 59)

print("\n=== QUICKPOLLS ===")
if tuesday_chat:
    c.execute(
        "SELECT qp.id, qp.game_date, qp.location_name, qp.closed,"
        " COUNT(CASE WHEN qv.vote_type='in' THEN 1 END)"
        " FROM quickpolls qp"
        " LEFT JOIN quickpoll_votes qv ON qv.poll_id=qp.id"
        " WHERE qp.chat_id=?"
        " GROUP BY qp.id ORDER BY qp.game_date ASC",
        (tuesday_chat,)
    )
else:
    c.execute(
        "SELECT qp.id, qp.game_date, qp.location_name, qp.closed,"
        " COUNT(CASE WHEN qv.vote_type='in' THEN 1 END)"
        " FROM quickpolls qp"
        " LEFT JOIN quickpoll_votes qv ON qv.poll_id=qp.id"
        " GROUP BY qp.id ORDER BY qp.game_date ASC"
    )

rows = c.fetchall()
print("{:<12} {:<32} {:>4} {:>6} {:>6} {:<8} {:>5}".format(
    "Date", "Location", "IN", "Guests", "Total", "Status", "ID"))
print("-" * 76)
for pid, gdate, loc, closed, in_count in rows:
    try:
        d = datetime.strptime(gdate, "%Y-%m-%d")
        if not (START <= d <= END):
            continue
    except Exception:
        continue
    c2 = conn.cursor()
    c2.execute(
        "SELECT COUNT(*) FROM quickpoll_guests WHERE poll_id=? AND confirmed=1",
        (pid,)
    )
    guests = c2.fetchone()[0]
    total = in_count + guests
    status = "CLOSED" if closed else "OPEN"
    loc_s = (loc or "Unknown")[:30]
    print("{:<12} {:<32} {:>4} {:>6} {:>6} {:<8} {:>5}".format(
        gdate, loc_s, in_count, guests, total, status, pid))

print("\n=== SEASON POLLS ===")
try:
    if tuesday_chat:
        c.execute(
            "SELECT p.id, p.game_date, p.closed,"
            " COUNT(CASE WHEN v.vote_type='member_in' THEN 1 END),"
            " COUNT(CASE WHEN v.vote_type='guest' THEN 1 END)"
            " FROM polls p"
            " LEFT JOIN votes v ON v.poll_id=p.id"
            " WHERE p.chat_id=?"
            " GROUP BY p.id ORDER BY p.game_date ASC",
            (tuesday_chat,)
        )
    else:
        c.execute(
            "SELECT p.id, p.game_date, p.closed,"
            " COUNT(CASE WHEN v.vote_type='member_in' THEN 1 END),"
            " COUNT(CASE WHEN v.vote_type='guest' THEN 1 END)"
            " FROM polls p"
            " LEFT JOIN votes v ON v.poll_id=p.id"
            " GROUP BY p.id ORDER BY p.game_date ASC"
        )
    srows = c.fetchall()
    if not srows:
        print("  (none in range)")
    else:
        print("{:<12} {:>9} {:>6} {:>6} {:<8} {:>5}".format(
            "Date", "member_in", "guests", "total", "Status", "ID"))
        print("-" * 52)
        for pid, gdate, closed, mi, gc in srows:
            try:
                d = datetime.strptime(gdate, "%Y-%m-%d")
                if not (START <= d <= END):
                    continue
            except Exception:
                continue
            status = "CLOSED" if closed else "OPEN"
            print("{:<12} {:>9} {:>6} {:>6} {:<8} {:>5}".format(
                gdate, mi, gc, mi + gc, status, pid))
except Exception as e:
    print("  Error: {}".format(e))

conn.close()
print("\nDone.")
