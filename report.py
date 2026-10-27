"""集計を人が読む形に整える。

JSONは機械向けなので、開いた瞬間に分かる表も別に書き出す。
全角混じりでも桁が揃うよう、文字幅を数えて詰める。
"""

import unicodedata
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))


def width(s: str) -> int:
    """全角を2、半角を1として数える。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s: str, n: int, right: bool = False) -> str:
    space = " " * max(0, n - width(s))
    return space + s if right else s + space


def jst(iso_str: str) -> str:
    try:
        return datetime.fromisoformat(iso_str).astimezone(JST).strftime(
            "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "-"


NAME_MAX = 20


def clip(s: str, n: int = NAME_MAX) -> str:
    """長すぎる名前で列が広がらないように詰める。"""
    if width(s) <= n:
        return s
    out = ""
    for c in s:
        if width(out + c) > n - 1:
            break
        out += c
    return out + "…"


def text(data: dict) -> str:
    ranked = data["ranking"]
    ranked_ids = {p["user_id"] for p in ranked}
    # ランク外（0pt以下）。マイナスの人もここに出す。消えると気づけない
    others = [p for p in data["everyone"]
              if p["user_id"] not in ranked_ids and p["active"]]
    others.sort(key=lambda p: (p["points"], p["name"]))
    t = data["totals"]

    name_w = min(NAME_MAX,
                 max([8] + [width(clip(p["name"])) for p in data["everyone"]]))
    lines = []

    head = "問題児ティア表"
    if data.get("monthly_reset", True) and data.get("season"):
        head += f"   {data['season']} シーズン"
    lines.append(head)
    lines.append(f"更新: {jst(data['generated_at'])}")
    lines.append("")

    head = (f"  {pad('順位', 6)}{pad('名前', name_w)}  "
            f"{pad('pt', 5, True)}  {pad('件数', 6, True)}  内訳")
    lines.append(head)
    lines.append("  " + "-" * (width(head) - 2))

    if not ranked:
        lines.append("  まだ誰も問題を起こしていません。平和ですね。")
    for p in ranked:
        mark = {1: "1位", 2: "2位", 3: "3位"}.get(p["rank"], f"{p['rank']}位")
        breakdown = []
        if p["voted_points"]:
            breakdown.append(f"投票 {p['voted_points']}")
        if p["manual_points"]:
            breakdown.append(f"手動 {p['manual_points']}")
        lines.append(
            f"  {pad(mark, 6)}{pad(clip(p['name']), name_w)}  "
            f"{pad(str(p['points']), 5, True)}  "
            f"{pad(str(p['cases']), 6, True)}  {' / '.join(breakdown)}"
        )

    if others:
        lines.append("")
        lines.append("  ---- ランク外 ----")
        for p in others:
            lines.append(f"  {pad('', 6)}{pad(clip(p['name']), name_w)}  "
                         f"{pad(str(p['points']), 5, True)}")

    lines.append("")
    lines.append(f"  参加 {t['members']}人 ・ 合計 {t['points']}pt ・ "
                 f"{t['cases']}件")

    if data["recent"]:
        lines.append("")
        lines.append("  最近の判決")
        for r in data["recent"][:10]:
            kind = "手動" if r["kind"] == "manual" else f"{r['yes']}-{r['no']}"
            crown = " 🏆" if r["unanimous"] else ""
            sign = format(r["points"], "+d")      # +3 / -1
            lines.append(f"    {jst(r['at'])}  "
                         f"{pad(clip(r['target']), name_w)}  "
                         f"{pad(sign, 4, True)}  ({kind}){crown}")

    if data["hall_of_fame"]:
        lines.append("")
        lines.append("  殿堂（満場一致・リセットされない）")
        for h in data["hall_of_fame"][:10]:
            lines.append(f"    {h['season']}  {pad(h['name'], name_w)}  "
                         f"全会一致 {h['yes']}票")

    lines.append("")
    return "\n".join(lines)
