#!/usr/bin/env python3
"""取数守卫（形状无关）的回归测试 —— 2026-11-09 换血。

跑法：python mve/test_substance_gate.py（服务器无 pytest，同 test_*.py 口径）。

守住的四条：
  ① MCP 返回的任意 dict（无 rows 键）**算数** —— 第五根因的正解：
     真取到的数不能因为「长得不像 SQL」被丢掉。
  ② 空表纯聚合返回 1 行 (NULL, 0) **仍然算真空** —— 既有铁律不许被换血放松。
  ③ 只有计划没结论（「我打算去查…」）**不算数**（maieutic isPlanOnly 口径）。
  ④ 材料账优先：材料里有真值就认（maieutic「判官看材料账不看自述」）。
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import substance_gate as G  # noqa: E402


def test_mcp_dict_counts_as_real():
    """① MCP 返回的任意 dict（无 rows 键）算数。"""
    # 场景 L 实测的形状：sherlock 返回候选 URL + http 状态，没有 rows 键。
    data = {"github": {"url": "https://github.com/ce", "status": 200},
            "gitlab": {"url": "https://gitlab.com/ce", "status": 404}}
    g = G.grade(data)
    assert g["level"] == "yes", f"MCP dict 应算真值，实际 {g}"
    assert G.took_real_number(g), g
    print("PASS  MCP 任意 dict 算真值（不再被形状闸丢掉）")


def test_empty_aggregate_still_vacuous():
    """② 空表纯聚合 1 行 (NULL, 0) 仍然算真空 —— 铁律不许放松。"""
    g = G.grade({"rows": [(None, 0)]})
    assert g["level"] == "no", f"空聚合必须真空，实际 {g}"
    assert not G.took_real_number(g), g
    # 纯 dict 形状也一样
    g2 = G.grade({"columns": ["a", "b"], "rows": [(None, 0)]})
    assert g2["level"] == "no", g2
    print("PASS  空表纯聚合 (NULL,0) 仍判真空（铁律守住）")


def test_empty_containers_vacuous():
    """空容器 / 全 None / 全空串 = 真空。"""
    for d in ({}, {"rows": []}, {"a": None, "b": ""}, {"a": [], "b": {}},
              None, [], ""):
        g = G.grade(d)
        assert g["level"] == "no", f"{d!r} 应真空，实际 {g}"
    print("PASS  空容器 / 全空值判真空")


def test_plan_only_not_counted():
    """③ 只有计划没结论不算数（maieutic isPlanOnly）。"""
    assert G.is_plan_only("我打算去查询这个用户名在各个平台的注册情况")
    assert G.is_plan_only("我将稍后再检查这些站点")
    # 有结论 → 不是纯计划（照抄原版的 && 结构）
    assert not G.is_plan_only("我打算查一下，答案是它在 GitHub 上被注册了")
    assert not G.is_plan_only("查询结果：GitHub 状态 200")
    # 判据层：纯计划的答案不算取到真数
    g = G.grade({"rows": [[1, 2]]}, answer="我打算去查这些站点")
    assert g["level"] == "no", f"纯计划应不算数，实际 {g}"
    print("PASS  只有计划没结论不算数（maieutic isPlanOnly 口径）")


def test_material_ledger_priority():
    """④ 材料账优先：材料里有真值就认（maieutic「判官看材料账」）。"""
    ev = [{"tool": "mcp_sherlock_lookup_username", "ok": True,
           "text": "site: github.com\nverdict: CLAIMED\nhttp: 200"}]
    # 结果对象本身是空的，但材料账里有真值 → 算数
    g = G.grade(None, evidence=ev)
    assert g["level"] == "yes", f"材料账有真值应算数，实际 {g}"
    # 失败条目不进材料账（照 maieutic _material_block 只取 ok 的）
    ev_bad = [{"tool": "x", "ok": False, "error": "boom"}]
    g2 = G.grade(None, evidence=ev_bad)
    assert g2["level"] == "no", g2
    print("PASS  材料账优先（只取 ok 条目）")


def test_nested_dict_leaves():
    """嵌套 dict 要下探到叶子 —— 不能因为「不是 rows 键」就判真空。"""
    data = {"found": {"github": "https://github.com/ce"}, "count": 0}
    g = G.grade(data)
    assert g["level"] in ("yes", "partial"), f"嵌套 dict 应算取到东西，实际 {g}"
    assert G.took_real_number(g), g
    print("PASS  嵌套 dict 下探叶子判真值")


def test_partial_level():
    """真值与空值混合 → partial（**有进展**，照 maieutic 三档语义）。"""
    g = G.grade({"a": 1, "b": None, "c": ""})
    assert g["level"] == "partial", f"应为 partial，实际 {g}"
    # ⚠️ partial 也算「取到东西了」—— maieutic 的 "partial" 是
    # 「some actual work, but incomplete」，**不是**「没做」。
    # 只有 "no" 才置空。照错这条，MCP 返回里混一个 count:0 就会让整轮
    # 被当成「没取到」→ 第五根因换个形状又回来。
    assert G.took_real_number(g), "partial 是有进展，不该当没取到"
    print("PASS  混合真值/空值判 partial（照 maieutic 三档：partial 算数）")


def test_partial_with_zero_field():
    """MCP 返回里混一个零值字段（count:0）→ 仍是 partial，不是真空。"""
    g = G.grade({"found": {"github": "https://github.com/ce"}, "count": 0})
    assert g["level"] == "partial", g
    assert G.took_real_number(g), "零值字段不等于没数据"
    print("PASS  零值字段不导致误判真空")


def test_bool_is_real_value():
    """bool 是结论不是空值（照抄 critic._is_zeroish 的口径）。"""
    g = G.grade({"claimed": True})
    assert g["level"] == "yes", g
    print("PASS  bool 算真值（不是空）")


def _run():
    for fn in [test_mcp_dict_counts_as_real,
               test_empty_aggregate_still_vacuous,
               test_empty_containers_vacuous,
               test_plan_only_not_counted,
               test_material_ledger_priority,
               test_nested_dict_leaves,
               test_partial_level,
               test_partial_with_zero_field,
               test_bool_is_real_value]:
        fn()
    print("\n全部通过：取数守卫（形状无关）9 项")


if __name__ == "__main__":
    _run()
