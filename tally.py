"""投票の集計。

このモジュールは **状態を持たない**。DBにもDiscordにも触らない。
点数計算のバグは信用を失い、一度失うと誰も使わなくなるので、ここだけはテストで固める。
"""

from dataclasses import dataclass
from math import ceil


@dataclass(frozen=True)
class Config:
    """config.json のうち集計に関わる部分。"""

    quorum: int = 3
    cap_per_case: int = 15
    unanimous_n: int = 8
    unanimous_pt: int = 2
    false_min_n: int = 5
    false_ratio: int = 3
    repeat_mult: float = 0.5
    daily_cap: int = 25

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in fields})


# status の取りうる値
PASSED = "passed"              # 可決。対象者に加点
REJECTED = "rejected"          # 否決。変動なし
FALSE_REPORT = "false_report"  # 言いがかり。通報者に加点
NO_QUORUM = "no_quorum"        # 定足数未満。不成立


@dataclass(frozen=True)
class Result:
    status: str
    points: int           # 対象者に加算するpt
    reporter_points: int  # 通報者に加算するpt（言いがかりのときだけ）
    yes: int
    no: int
    unanimous: bool       # 満場一致（殿堂入り）


def tally(
    votes: dict,
    reporter_id: str,
    target_id: str,
    cfg: Config,
    repeats_24h: int = 0,
    gained_today: int = 0,
) -> Result:
    """投票を集計する。

    votes        : {user_id(str): True=Yes / False=No}
    repeats_24h  : 対象者が直近24時間で可決された回数
    gained_today : 対象者が今日すでに獲得したpt
    """
    # 1) 自演と、通報者の自動賛成を除外する
    votes = {
        uid: v
        for uid, v in votes.items()
        if uid != target_id and uid != reporter_id
    }

    yes = sum(1 for v in votes.values() if v)
    no = len(votes) - yes
    n = yes + no

    # 2) 定足数
    if n < cfg.quorum:
        return Result(NO_QUORUM, 0, 0, yes, no, False)

    # 3) 多数決
    if yes <= no:
        # 明らかな言いがかりのときだけ通報者にペナルティ。
        # 僅差の否決でペナを出すと、グレーな案件を誰も通報しなくなる。
        if n >= cfg.false_min_n and no >= yes * cfg.false_ratio:
            return Result(FALSE_REPORT, 0, 1, yes, no, False)
        return Result(REJECTED, 0, 0, yes, no, False)

    # 4) 獲得pt = Yesを押した人数（上限あり）
    pt = min(yes, cfg.cap_per_case)

    # 5) 満場一致ボーナス
    unanimous = n >= cfg.unanimous_n and no == 0
    if unanimous:
        pt += cfg.unanimous_pt

    # 6) 連続逓減（集中砲火の緩和）。切り上げなので1pt案件が消えない
    if repeats_24h > 0:
        pt = ceil(pt * (cfg.repeat_mult ** repeats_24h))

    # 7) 1日の上限（daily_cap <= 0 なら上限なし）
    if cfg.daily_cap > 0:
        pt = max(0, min(pt, cfg.daily_cap - gained_today))

    return Result(PASSED, pt, 0, yes, no, unanimous)
