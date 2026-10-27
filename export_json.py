"""summary.json と tier.txt を作り直す。Botを止めずに実行できる。

    python export_json.py

どちらも tier.db から作る出力。消しても壊れないし、編集しても反映されない。
正は常に tier.db。
"""

import json
import os

import db
import report


def write_atomic(path: str, body: str):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


with open("config.json", encoding="utf-8") as f:
    conf = json.load(f)

db.connect(tz_offset_hours=conf.get("timezone_offset_hours", 9))
db.set_monthly_reset(conf.get("monthly_reset", True))

data = db.summary()

write_atomic("summary.json", json.dumps(data, ensure_ascii=False, indent=2))
write_atomic("tier.txt", report.text(data))

print(report.text(data))
print("summary.json と tier.txt を書きました。")
