"""db.py の動作確認。tier.db には触らず、使い捨ての smoke.db で試す。

    python smoke_db.py
"""

import os
from datetime import timedelta

import db

for f in ("smoke.db", "smoke.db-wal", "smoke.db-shm"):
    if os.path.exists(f):
        os.remove(f)

db.connect("smoke.db")

# ---- 名簿 ----
assert db.join("u1") and not db.join("u1")
assert db.is_member("u1")
assert db.leave("u1") and not db.is_member("u1")
assert db.join("u1")                       # 再参加できる
db.join("u2"); db.join("u3"); db.join("u4")
assert set(db.member_ids()) == {"u1", "u2", "u3", "u4"}

# ---- 投票 ----
deadline = db.now() + timedelta(seconds=300)
db.create_poll(111, 222, 333, 444, "u1", "u2", deadline)
assert db.has_open_poll_for(444)

db.cast_vote(111, "u3", True)
db.cast_vote(111, "u4", True)
db.cast_vote(111, "u3", False)             # 押し直しは上書きになる
assert db.vote_count(111) == 2
assert db.votes_of(111) == {"u3": False, "u4": True}

# ---- 二重集計の防止 ----
assert db.claim_poll(111) is True
assert db.claim_poll(111) is False         # 2回目は必ず失敗する
db.finish_poll(111, "closed")

# ---- 判決 ----
db.record_conviction("u1", "u2", 444, 9, 0, 11, True)
assert db.repeats_24h("u1") == 1
assert db.gained_today("u1") == 11
rows = db.ranking()
assert rows[0]["target_id"] == "u1" and rows[0]["pt"] == 11
assert len(db.hall_of_fame()) == 1

# ---- ハートビート ----
db.beat()
assert db.last_heartbeat() is not None

print("db smoke test: all OK  /  season =", db.season())
