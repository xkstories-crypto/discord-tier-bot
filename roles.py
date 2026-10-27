"""上位3名へのロール付与。

ティア表の順位を Discord のロールに反映する。ここも **点数計算には触らない**。
順位の決め方（同点は全員）だけは純粋関数として切り出し、テストで固める。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Rank:
    index: int        # 0 = 1位
    points: int
    user_ids: list    # 同点なら複数人


def top_ranks(rows, depth: int = 3) -> list:
    """ランキング行から上位 depth 段ぶんを返す。

    rows  : db.ranking() の戻り（pt降順。pt>0 のみ）
    同点は同じ順位にまとめる。3位が2人なら 3位のロールを2人が持つ。
    段が足りなければ（参加者が2人しかいない等）その段は存在しない。
    """
    groups = []
    for row in rows:
        pt = row["pt"]
        uid = str(row["target_id"])
        if groups and groups[-1][0] == pt:
            groups[-1][1].append(uid)
        else:
            if len(groups) == depth:
                break
            groups.append((pt, [uid]))

    return [Rank(i, pt, uids) for i, (pt, uids) in enumerate(groups)]


NICK_MAX = 32      # Discordのニックネーム上限


def crowned_nick(base: str, emoji: str) -> str:
    """末尾に王冠を足す。32文字を超えるぶんは名前側を削る。

    すでに王冠が付いている名前を渡されても二重に付かないようにする
    （元の名前の控えが何かの拍子に消えたときの保険）。
    """
    suffix = " " + emoji
    base = base.rstrip()
    while base.endswith(emoji):
        base = base[: -len(emoji)].rstrip()
    room = NICK_MAX - len(suffix)
    return base[:room].rstrip() + suffix
