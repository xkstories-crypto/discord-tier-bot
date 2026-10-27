"""DBの中身を読みやすく出す。Botを止めずに実行できる。

    python dump.py
"""

import sqlite3
import sys

con = sqlite3.connect("file:tier.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row


def show(title, rows, cols):
    print(f"\n=== {title} ({len(rows)}件) ===")
    if not rows:
        print("  （なし）")
        return
    widths = [max(len(c), *(len(str(r[c])) for r in rows)) for c in cols]
    print("  " + "  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("  " + "  ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))


q = con.execute

show("参加者", q("SELECT * FROM members").fetchall(),
     ["user_id", "joined_at", "left_at", "added_by"])

show("判決（ポイントの記録）",
     q("SELECT id, season, target_id, points, yes, no, unanimous, created_at "
       "FROM convictions ORDER BY id").fetchall(),
     ["id", "season", "target_id", "points", "yes", "no", "unanimous",
      "created_at"])

show("殿堂", q("SELECT * FROM hall_of_fame").fetchall(),
     ["conviction_id", "season", "target_id", "yes", "created_at"])

show("投票（直近20件）",
     q("SELECT * FROM polls ORDER BY created_at DESC LIMIT 20").fetchall(),
     ["message_id", "target_id", "reporter_id", "status", "deadline"])

# 今シーズンの集計
season = q("SELECT MAX(season) s FROM convictions").fetchone()["s"]
if season:
    rows = q("""SELECT target_id, SUM(points) pt, COUNT(*) cases
                FROM convictions WHERE season=? AND points > 0
                GROUP BY target_id ORDER BY pt DESC""", (season,)).fetchall()
    show(f"ティア表 {season}", rows, ["target_id", "pt", "cases"])

hb = q("SELECT last_at FROM heartbeat WHERE id=1").fetchone()
print(f"\n最終ハートビート: {hb['last_at'] if hb else '（なし）'}")
