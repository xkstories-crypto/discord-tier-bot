"""上位3名の決め方のテスト。同点の扱いがここの肝。

    python test_roles.py
"""

from roles import top_ranks, crowned_nick


def rows(*pairs):
    return [{"target_id": uid, "pt": pt} for uid, pt in pairs]


def test_素直に3人():
    r = top_ranks(rows(("a", 30), ("b", 20), ("c", 10), ("d", 5)))
    assert [x.user_ids for x in r] == [["a"], ["b"], ["c"]]


def test_3位が同点なら2人とも3位():
    r = top_ranks(rows(("a", 30), ("b", 20), ("c", 10), ("d", 10), ("e", 5)))
    assert len(r) == 3
    assert r[2].user_ids == ["c", "d"]


def test_1位が同点でも2位3位はずれない():
    r = top_ranks(rows(("a", 30), ("b", 30), ("c", 20), ("d", 10)))
    assert r[0].user_ids == ["a", "b"]
    assert r[1].user_ids == ["c"]
    assert r[2].user_ids == ["d"]


def test_全員同点なら1位に全員入って2位以下は無い():
    r = top_ranks(rows(("a", 10), ("b", 10), ("c", 10)))
    assert len(r) == 1
    assert r[0].user_ids == ["a", "b", "c"]


def test_人が足りなければ段が減る():
    assert len(top_ranks(rows(("a", 10), ("b", 5)))) == 2
    assert top_ranks(rows()) == []


def test_4位以降は同点でも拾わない():
    r = top_ranks(rows(("a", 30), ("b", 20), ("c", 10), ("d", 9), ("e", 9)))
    assert [x.user_ids for x in r] == [["a"], ["b"], ["c"]]


def test_王冠は名前の末尾に付く():
    assert crowned_nick("たろう", "👑") == "たろう 👑"


def test_王冠は二重に付かない():
    assert crowned_nick("たろう 👑", "👑") == "たろう 👑"
    assert crowned_nick("たろう👑👑", "👑") == "たろう 👑"


def test_32文字を超えたら名前側を削る():
    n = crowned_nick("あ" * 40, "👑")
    assert len(n) <= 32
    assert n.endswith(" 👑")


if __name__ == "__main__":
    ok = fail = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
                ok += 1
            except AssertionError:
                print(f"  FAIL {name}")
                fail += 1
    print(f"\n{ok} passed, {fail} failed")
    raise SystemExit(1 if fail else 0)
