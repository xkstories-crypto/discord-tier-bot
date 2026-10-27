"""集計のテスト。設計書の検算表がそのまま入っている。

    python test_tally.py     # pytest なしでも動く
    pytest -q                # 入れているならこちらでも
"""

from tally import Config, tally, PASSED, REJECTED, FALSE_REPORT, NO_QUORUM

CFG = Config()

REPORTER = "reporter"
TARGET = "target"


def make_votes(yes: int, no: int) -> dict:
    """Yes n人 / No m人 の投票を作る。通報者・対象者とは別人にする。"""
    votes = {f"y{i}": True for i in range(yes)}
    votes.update({f"n{i}": False for i in range(no)})
    return votes


def run(yes, no, **kw):
    return tally(make_votes(yes, no), REPORTER, TARGET, CFG, **kw)


# ---- 設計書の検算表 ----

def test_深夜のやらかし():
    r = run(4, 0)
    assert (r.status, r.points) == (PASSED, 4)
    assert not r.unanimous          # 8票未満なので満場一致ボーナスはつかない


def test_夕方の失言():
    assert run(10, 2).points == 10


def test_僅差():
    assert run(7, 6).points == 7


def test_全会一致は殿堂入り():
    r = run(9, 0)
    assert r.unanimous
    assert r.points == 11           # 9 + ボーナス2


def test_大炎上は上限で止まる():
    assert run(20, 1).points == 15


def test_スベった通報は通報者に返ってくる():
    r = run(4, 16)
    assert r.status == FALSE_REPORT
    assert r.points == 0
    assert r.reporter_points == 1


def test_深夜に2人だけなら不成立():
    r = run(2, 0)
    assert r.status == NO_QUORUM
    assert r.points == 0


# ---- 境界と抜け穴 ----

def test_同数なら否決():
    assert run(3, 3).status == REJECTED


def test_僅差の否決では通報者にペナルティが出ない():
    # 4:5 は否決だが言いがかりとまでは言えない
    assert run(4, 5).status == REJECTED


def test_言いがかり判定は票が少ないと発動しない():
    # No が3倍以上でも、4票しかないので通常の否決どまり
    assert run(1, 3).status == REJECTED


def test_自分で自分に投票しても数えない():
    votes = make_votes(3, 0)
    votes[TARGET] = False           # 対象者が自分を庇う
    votes[REPORTER] = True          # 通報者が自分で賛成する
    r = tally(votes, REPORTER, TARGET, CFG)
    assert (r.yes, r.no) == (3, 0)  # どちらも無視される
    assert r.points == 3


def test_連続逓減():
    # Noが1人いるので満場一致ボーナスはつかない。素の10ptが半分になる
    assert run(10, 1, repeats_24h=1).points == 5
    assert run(10, 1, repeats_24h=2).points == 3   # ceil(2.5)


def test_逓減は満場一致ボーナスを含めた額にかかる():
    assert run(10, 0).points == 12                  # 10 + ボーナス2
    assert run(10, 0, repeats_24h=1).points == 6    # 12の半分


def test_逓減しても1pt案件は消えない():
    assert run(3, 0, repeats_24h=1).points == 2    # ceil(1.5)


def test_1日の上限で頭打ちになる():
    assert run(10, 0, gained_today=20).points == 5
    assert run(10, 0, gained_today=25).points == 0


def test_repeat_mult_1なら連続逓減なし():
    cfg = Config(quorum=1, repeat_mult=1.0)
    votes = make_votes(6, 0)
    for rep in (0, 1, 5):
        r = tally(votes, REPORTER, TARGET, cfg, repeats_24h=rep)
        assert r.points == 6, rep


def test_daily_cap_0なら上限なし():
    cfg = Config(quorum=1, daily_cap=0)
    votes = {str(i): True for i in range(10)}
    r = tally(votes, reporter_id="r", target_id="t", cfg=cfg, gained_today=999)
    assert r.points == 12  # Yes10 + 満場一致ボーナス2。上限で削られない


def test_満場一致は8票から():
    assert not run(7, 0).unanimous
    assert run(8, 0).unanimous


# ---- pytest がなくても動かせるようにしておく ----

if __name__ == "__main__":
    import sys, traceback

    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception:
            failed += 1
            print(f"  FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
