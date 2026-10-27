"""SQLite まわり。スキーマと、Botから呼ぶクエリだけを持つ。

方針:
  - ポイントの合計テーブルを作らない。convictions から毎回集計する
    → 合計がズレるという事故が構造的に起きない
  - メッセージ本文を保存しない。message_id だけ持ち、表示時にDiscordから取る
  - シーズンは "2026-09" のような文字列。月が変われば自動で切り替わる
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

DB_PATH = "tier.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    user_id   TEXT PRIMARY KEY,
    joined_at TEXT NOT NULL,
    left_at   TEXT,
    added_by  TEXT
);

CREATE TABLE IF NOT EXISTS polls (
    message_id        TEXT PRIMARY KEY,
    channel_id        TEXT NOT NULL,
    thread_id         TEXT,
    target_message_id TEXT NOT NULL,
    target_id         TEXT NOT NULL,
    reporter_id       TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    deadline          TEXT NOT NULL,
    extended_sec      INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_polls_status ON polls(status);
CREATE INDEX IF NOT EXISTS idx_polls_target_msg ON polls(target_message_id);

CREATE TABLE IF NOT EXISTS votes (
    message_id TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    choice     INTEGER NOT NULL,
    voted_at   TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id)
);

CREATE TABLE IF NOT EXISTS convictions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    season      TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    reporter_id TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    yes         INTEGER NOT NULL,
    no          INTEGER NOT NULL,
    points      INTEGER NOT NULL,
    unanimous   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_target ON convictions(target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conv_season ON convictions(season);

CREATE TABLE IF NOT EXISTS hall_of_fame (
    conviction_id INTEGER PRIMARY KEY,
    season        TEXT NOT NULL,
    target_id     TEXT NOT NULL,
    yes           INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeat (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    last_at TEXT NOT NULL
);
"""

_conn: sqlite3.Connection = None
_tz = timezone(timedelta(hours=9))
_monthly_reset = True
_expire_days = 0          # 時効（日）。0 なら時効なし
_expire_manual = True     # 手動調整（/adjust）にも時効をかけるか


# ---------- 基本 ----------

def connect(path: str = DB_PATH, tz_offset_hours: int = 9) -> sqlite3.Connection:
    global _conn, _tz
    _tz = timezone(timedelta(hours=tz_offset_hours))
    _conn = sqlite3.connect(path, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")     # クラッシュ耐性
    _conn.execute("PRAGMA synchronous=NORMAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(SCHEMA)
    _migrate()
    return _conn


def _migrate():
    """既存のDBを壊さずに列を足す。"""
    cols = {r["name"] for r in _conn.execute("PRAGMA table_info(convictions)")}
    if "note" not in cols:
        _conn.execute("ALTER TABLE convictions ADD COLUMN note TEXT")

    cols = {r["name"] for r in _conn.execute("PRAGMA table_info(members)")}
    if "display_name" not in cols:
        _conn.execute("ALTER TABLE members ADD COLUMN display_name TEXT")


def set_name(user_id, name: str):
    """表示名を覚えておく。IDだけだと集計を見ても誰だか分からないため。"""
    _conn.execute(
        "UPDATE members SET display_name=? WHERE user_id=?",
        (name, str(user_id)),
    )


def name_of(user_id) -> str:
    row = _conn.execute(
        "SELECT display_name FROM members WHERE user_id=?", (str(user_id),)
    ).fetchone()
    return (row["display_name"] if row and row["display_name"] else str(user_id))


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


def season(at: datetime = None) -> str:
    """現地時間の年月。記録には常にこれを入れる。"""
    return (at or now()).astimezone(_tz).strftime("%Y-%m")


def set_monthly_reset(on: bool):
    """月次リセットの切り替え。

    オフにしても記録の season 列はそのまま埋め続ける。
    変わるのは集計の範囲だけなので、あとでオンに戻せば月別に戻る。
    """
    global _monthly_reset
    _monthly_reset = bool(on)


def monthly_reset() -> bool:
    return _monthly_reset


def set_expire_days(days: int, manual_too: bool = True):
    """時効の設定。0 なら時効なし（ずっと残る）。

    記録そのものは消さない。集計から外すだけなので、
    0 に戻せば過去のぶんもそのまま戻ってくる。

    manual_too=False にすると /adjust で入れたぶんだけ時効にかからない。
    引き継いだ基準値を残したいときに使う。
    """
    global _expire_days, _expire_manual
    _expire_days = max(0, int(days or 0))
    _expire_manual = bool(manual_too)


def expire_days() -> int:
    return _expire_days


def expire_cutoff() -> datetime:
    """これより古い記録は集計に入らない。"""
    return now() - timedelta(days=_expire_days)


def scope_label() -> str:
    """表示用。集計している範囲を一言で表す。"""
    parts = []
    if _expire_days:
        parts.append(f"直近{_expire_days}日")
    if _monthly_reset:
        parts.append(season())
    return " ・ ".join(parts)


def _scope():
    """集計の範囲。(SQLの条件, パラメータ) を返す。

    ここが集計の唯一の入口。ランキングも合計も全部これを通る。
    """
    conds, params = [], []
    if _monthly_reset:
        conds.append("season = ?")
        params.append(season())
    if _expire_days:
        if _expire_manual:
            conds.append("created_at >= ?")
        else:
            conds.append("(created_at >= ? OR message_id = 'manual')")
        params.append(iso(expire_cutoff()))
    return (" AND ".join(conds) or "1 = 1"), params


# ---------- 名簿 ----------

def join(user_id: str, added_by: str = "self") -> bool:
    """参加させる。もともと参加中なら False。"""
    row = _conn.execute(
        "SELECT left_at FROM members WHERE user_id=?", (user_id,)
    ).fetchone()
    if row is not None and row["left_at"] is None:
        return False
    if row is None:
        _conn.execute(
            "INSERT INTO members (user_id, joined_at, added_by) VALUES (?,?,?)",
            (user_id, iso(now()), added_by),
        )
    else:
        # 再参加。ポイントはそのまま復活する
        _conn.execute(
            "UPDATE members SET left_at=NULL, joined_at=?, added_by=? WHERE user_id=?",
            (iso(now()), added_by, user_id),
        )
    return True


def leave(user_id: str) -> bool:
    """抜ける。ポイントは消さない（消すと逃げ得になる）。"""
    cur = _conn.execute(
        "UPDATE members SET left_at=? WHERE user_id=? AND left_at IS NULL",
        (iso(now()), user_id),
    )
    return cur.rowcount > 0


def is_member(user_id: str) -> bool:
    row = _conn.execute(
        "SELECT 1 FROM members WHERE user_id=? AND left_at IS NULL", (user_id,)
    ).fetchone()
    return row is not None


def member_ids() -> list:
    rows = _conn.execute(
        "SELECT user_id FROM members WHERE left_at IS NULL ORDER BY joined_at"
    ).fetchall()
    return [r["user_id"] for r in rows]


# ---------- 投票 ----------

def create_poll(message_id, channel_id, thread_id, target_message_id,
                target_id, reporter_id, deadline):
    _conn.execute(
        """INSERT INTO polls
           (message_id, channel_id, thread_id, target_message_id, target_id,
            reporter_id, created_at, deadline, extended_sec, status)
           VALUES (?,?,?,?,?,?,?,?,0,'open')""",
        (str(message_id), str(channel_id), str(thread_id) if thread_id else None,
         str(target_message_id), str(target_id), str(reporter_id),
         iso(now()), iso(deadline)),
    )


def get_poll(message_id):
    return _conn.execute(
        "SELECT * FROM polls WHERE message_id=?", (str(message_id),)
    ).fetchone()


def open_polls() -> list:
    return _conn.execute(
        "SELECT * FROM polls WHERE status='open'"
    ).fetchall()


def has_open_poll_for(target_message_id) -> bool:
    row = _conn.execute(
        "SELECT 1 FROM polls WHERE target_message_id=? AND status='open'",
        (str(target_message_id),),
    ).fetchone()
    return row is not None


def claim_poll(message_id) -> bool:
    """締め処理の入口。アトミックに1回しか成功しない＝二重集計が起きない。"""
    cur = _conn.execute(
        "UPDATE polls SET status='closing' WHERE message_id=? AND status='open'",
        (str(message_id),),
    )
    return cur.rowcount > 0


def finish_poll(message_id, status: str):
    _conn.execute(
        "UPDATE polls SET status=? WHERE message_id=?", (status, str(message_id))
    )


def extend_poll(message_id, new_deadline: datetime, extended_sec: int):
    _conn.execute(
        "UPDATE polls SET deadline=?, extended_sec=? WHERE message_id=?",
        (iso(new_deadline), extended_sec, str(message_id)),
    )


def cast_vote(message_id, user_id, choice: bool):
    """押し直しは INSERT OR REPLACE で自然に上書きされる。"""
    _conn.execute(
        "INSERT OR REPLACE INTO votes VALUES (?,?,?,?)",
        (str(message_id), str(user_id), 1 if choice else 0, iso(now())),
    )


def votes_of(message_id) -> dict:
    rows = _conn.execute(
        "SELECT user_id, choice FROM votes WHERE message_id=?", (str(message_id),)
    ).fetchall()
    return {r["user_id"]: bool(r["choice"]) for r in rows}


def vote_count(message_id) -> int:
    return _conn.execute(
        "SELECT COUNT(*) c FROM votes WHERE message_id=?", (str(message_id),)
    ).fetchone()["c"]


# ---------- 判決 ----------

def record_conviction(target_id, reporter_id, message_id, yes, no, points,
                      unanimous: bool) -> int:
    cur = _conn.execute(
        """INSERT INTO convictions
           (season, target_id, reporter_id, message_id, yes, no, points,
            unanimous, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (season(), str(target_id), str(reporter_id), str(message_id),
         yes, no, points, 1 if unanimous else 0, iso(now())),
    )
    cid = cur.lastrowid
    if unanimous:
        _conn.execute(
            "INSERT OR IGNORE INTO hall_of_fame VALUES (?,?,?,?,?)",
            (cid, season(), str(target_id), yes, iso(now())),
        )
    return cid


def adjust_points(target_id, by_id, points: int, note: str) -> int:
    """手動でポイントを足し引きする。基準値の設定と、誤爆の取り消しに使う。

    投票と同じ convictions に1行積むだけ。合計は常に集計で出すので整合が崩れない。
    message_id='manual' で印を付け、連続逓減の判定からは除外する。
    """
    _conn.execute(
        """INSERT INTO convictions
           (season, target_id, reporter_id, message_id, yes, no, points,
            unanimous, created_at, note)
           VALUES (?,?,?,'manual',0,0,?,0,?,?)""",
        (season(), str(target_id), str(by_id), points, iso(now()), note),
    )
    return total_points(target_id)


def total_points(target_id) -> int:
    where, params = _scope()
    row = _conn.execute(
        f"""SELECT COALESCE(SUM(points), 0) pt FROM convictions
            WHERE {where} AND target_id=?""",
        params + [str(target_id)],
    ).fetchone()
    return row["pt"]


def repeats_24h(target_id) -> int:
    cutoff = iso(now() - timedelta(hours=24))
    return _conn.execute(
        """SELECT COUNT(*) c FROM convictions
           WHERE target_id=? AND points > 0 AND message_id <> 'manual'
             AND created_at > ?""",
        (str(target_id), cutoff),
    ).fetchone()["c"]


def gained_today(target_id) -> int:
    midnight = now().astimezone(_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return _conn.execute(
        """SELECT COALESCE(SUM(points), 0) s FROM convictions
           WHERE target_id=? AND created_at >= ?""",
        (str(target_id), iso(midnight)),
    ).fetchone()["s"]


def ranking(limit: int = 15) -> list:
    """今シーズンのティア表。合計は持たず毎回集計する。"""
    where, params = _scope()
    return _conn.execute(
        f"""SELECT target_id,
                   SUM(points) pt,
                   SUM(CASE WHEN message_id <> 'manual' AND points > 0
                            THEN 1 ELSE 0 END) cases
            FROM convictions
            WHERE {where}
            GROUP BY target_id
            HAVING pt > 0
            ORDER BY pt DESC, cases DESC
            LIMIT ?""",
        params + [limit],
    ).fetchall()


def hall_of_fame(limit: int = 10) -> list:
    return _conn.execute(
        "SELECT * FROM hall_of_fame ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()


# ---------- ハートビート ----------

def summary() -> dict:
    """convictions から作る集計。ここから作られるものは全部“写し”で、正はDB。"""
    where, params = _scope()

    agg = _conn.execute(
        f"""SELECT target_id,
                   SUM(points) pt,
                   SUM(CASE WHEN message_id <> 'manual' AND points > 0
                            THEN 1 ELSE 0 END) cases,
                   SUM(CASE WHEN message_id =  'manual' THEN points ELSE 0 END) manual_pt,
                   SUM(CASE WHEN message_id <> 'manual' THEN points ELSE 0 END) voted_pt,
                   MAX(created_at) last_at
            FROM convictions
            WHERE {where}
            GROUP BY target_id""",
        params,
    ).fetchall()
    by_id = {r["target_id"]: r for r in agg}

    people = []
    for m in _conn.execute(
        "SELECT user_id, display_name, left_at FROM members ORDER BY joined_at"
    ).fetchall():
        r = by_id.pop(m["user_id"], None)
        people.append({
            "user_id": m["user_id"],
            "name": m["display_name"] or m["user_id"],
            "points": r["pt"] if r else 0,
            "cases": r["cases"] if r else 0,
            "manual_points": r["manual_pt"] if r else 0,
            "voted_points": r["voted_pt"] if r else 0,
            "last_at": r["last_at"] if r else None,
            "active": m["left_at"] is None,
        })
    # 名簿から抜けた後もポイントが残っている人
    for uid, r in by_id.items():
        people.append({
            "user_id": uid, "name": name_of(uid), "points": r["pt"],
            "cases": r["cases"], "manual_points": r["manual_pt"],
            "voted_points": r["voted_pt"], "last_at": r["last_at"],
            "active": False,
        })

    people.sort(key=lambda p: (-p["points"], -p["cases"], p["name"]))
    ranked = [p for p in people if p["points"] > 0]
    for i, p in enumerate(ranked, 1):
        p["rank"] = i

    recent = [
        {
            "at": r["created_at"],
            "target": name_of(r["target_id"]),
            "points": r["points"],
            "yes": r["yes"], "no": r["no"],
            "kind": "manual" if r["message_id"] == "manual" else "vote",
            "unanimous": bool(r["unanimous"]),
            "note": r["note"],
        }
        for r in _conn.execute(
            f"SELECT * FROM convictions WHERE {where} "
            "ORDER BY id DESC LIMIT 50", params).fetchall()
    ]

    hof = [
        {"season": r["season"], "name": name_of(r["target_id"]),
         "yes": r["yes"], "at": r["created_at"]}
        for r in _conn.execute(
            "SELECT * FROM hall_of_fame ORDER BY created_at DESC").fetchall()
    ]

    seasons = [
        {"season": r["season"], "points": r["pt"], "cases": r["c"]}
        for r in _conn.execute(
            "SELECT season, SUM(points) pt, COUNT(*) c FROM convictions "
            "GROUP BY season ORDER BY season DESC").fetchall()
    ]

    return {
        "generated_at": iso(now()),
        "season": scope_label(),
        "monthly_reset": _monthly_reset,
        "source": "tier.db の convictions から生成した写し。編集しても反映されません。",
        "totals": {
            "members": sum(1 for p in people if p["active"]),
            "ranked": len(ranked),
            "points": sum(p["points"] for p in people),
            "cases": sum(p["cases"] for p in people),
        },
        # 一目で見る用。名前 → ポイント だけ
        "points": {p["name"]: p["points"] for p in people},
        "ranking": ranked,
        "everyone": people,
        "recent": recent,
        "hall_of_fame": hof,
        "seasons": seasons,
    }


def beat():
    _conn.execute(
        "INSERT INTO heartbeat (id, last_at) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET last_at=excluded.last_at",
        (iso(now()),),
    )


def last_heartbeat():
    row = _conn.execute("SELECT last_at FROM heartbeat WHERE id=1").fetchone()
    return parse(row["last_at"]) if row else None
