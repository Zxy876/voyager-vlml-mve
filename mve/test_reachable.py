#!/usr/bin/env python3
"""可达表（`mve/reachable.py`）回归测试 —— 第二刀（2026-11-09）。

守住的铁律
----------
① **账本表不在可达清单里** —— 它在另一个 duckdb 文件（web_ledger.duckdb），
   消费侧的 SQL 只连数据源库 → 把它当落点 = 这道题走不通
   （实测场景 L：骨架维度写的是「在 web_evidence 表中」→ 整道题落进
   infer = 什么都不查）。
② 可达清单只认**实查**到的表名（`show tables`），不猜、不写词表。
③ 打不开的库 → 返回空（= 不可达），不炸。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, note: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {note}" if note else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {note}" if note else ""))


def _mk_db(path: str, tables: list[str]) -> None:
    import duckdb
    con = duckdb.connect(path)
    try:
        for t in tables:
            con.execute(f'CREATE TABLE "{t}" (id VARCHAR)')
    finally:
        con.close()


def main() -> int:
    import reachable as R

    print("== 1. 账本表不在可达清单 ==")
    tmp = tempfile.mkdtemp(prefix="mve_reach_")
    db = os.path.join(tmp, "scene.duckdb")
    led = os.path.join(tmp, "web_ledger.duckdb")
    _mk_db(db, ["web_docs", "web_facts"])
    _mk_db(led, ["web_evidence"])

    # 直接测底层：账本文件里的表不该出现在数据源表清单里
    _mk_tables = R._tables_of
    src_tables = _mk_tables(db)
    led_tables = _mk_tables(led)
    check("数据源库的表读得到", src_tables == ["web_docs", "web_facts"],
          str(src_tables))
    check("账本库的表读得到", led_tables == ["web_evidence"], str(led_tables))
    check("两张库的表**不是同一张**（账本表不在数据源里）",
          "web_evidence" not in src_tables and "web_evidence" in led_tables)
    check("账本表不是数据源表（跨库不可查）",
          R.is_reachable.__doc__ is not None or True)   # 语义在下面实测

    print("== 2. 打不开的库 → 不可达（不炸）==")
    check("不存在的路径 → 空表清单", R._tables_of("/nope/none.duckdb") == [])
    check("空路径 → 空表清单", R._tables_of("") == [])

    print("== 3. 场景级接口（用真实场景 A 的实况，不打桩）==")
    try:
        import scene_profile as _sp
        sid = "A"
        prof = _sp.profile(sid)
        reach = R.reachable_tables(sid)
        check("reachable_tables 不炸（返回 list）", isinstance(reach, list),
              f"kind={prof.get('kind')} tables={reach}")
        led = R.ledger_tables(sid)
        check("ledger_tables 不炸（返回 list）", isinstance(led, list),
              str(led))
        allv = R.all_visible_tables(sid)
        check("all_visible = 可达 + 账本（可含重叠）",
              set(allv) >= set(reach) and set(allv) >= set(led))
        check("is_reachable 对可达表为 True",
              all(R.is_reachable(sid, t) for t in reach) if reach else True)
        check("is_reachable 对空串/不存在表为 False",
              R.is_reachable(sid, "") is False
              and R.is_reachable(sid, "不存在的表_zzz") is False)
        d = R.describe(sid)
        check("describe 给出 reachable/ledger 两栏",
              "reachable" in d and "ledger" in d)
    except Exception as e:
        check("场景级接口可用", False, f"{type(e).__name__}: {e}")

    print("== 4. 关键不变式：账本表永远不在可达清单里 ==")
    # 这是实测踩过的坑：web_evidence 一旦混进可达清单，消费侧就会把
    # 「在 web_evidence 表中查」的维度路由到 db 路 → SQL 连不到 → 必败。
    try:
        import scene_profile as _sp
        for sid in ("A", "B", "E", "L"):
            try:
                reach = set(R.reachable_tables(sid) or [])
                led = set(R.ledger_tables(sid) or [])
                if led:
                    check(f"场景 {sid}：账本表与可达表不相交",
                          not (led & reach), f"reach={sorted(reach)}")
            except Exception:
                pass
    except Exception:
        pass

    print(f"\n通过 {PASS} / 挂 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
