#!/usr/bin/env python3
"""上下文矩阵问答测试（与「在 DeepSeek 上问问题」同构）。

用户 2026-10-28 三次拍板的落点：
  ① 「不允许硬编码」—— 不写死业务口径、不针对某道题加分支；
  ② 「测试方法应该和在 DeepSeek 上问报表、让它分析报表同构」；
  ③ 「在 DeepSeek 上问什么问题、带不带图和文件（上下文）应该这样，而不是
     让他只解决这一个问题，写死解法」。

DeepSeek 的形态就是：**随便问一句，带不带上下文都行** ——
带文件/数据就按它答；不带就按模型知识答，并说清这是推断、不是查到的数。
本文件照这个形态组织：

  维度 A｜上下文：**不带** ／ **带真实场景库**（MVE 的上下文=文件/库；图暂不支持，
                 不支持就如实说，不假装能看图）
  维度 B｜问句：合计、部门口径、单项汇总、以及一句和环境无关的知识题

**真值不写死常量**：每个用例自带一条「口径 SQL」（与被测实现无关），
跑测试时现场查到真值再比 —— 期望值来自库，不来自我的记忆。
（2026-10-28 实测教训：我凭记忆写 83120 当期望，漏了 active 过滤；
 系统答 57120 才是对的，测试反而挂错人。）

跑法（服务器上跑；本地缺 duckdb/httpx 会如实报跑不了，不假过）：
    python mve/test_report_qa.py
    python mve/test_report_qa.py --only 津贴
    python mve/test_report_qa.py --ctx none      # 只跑不带上下文的
"""

from __future__ import annotations

import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_HERE, _ROOT, os.path.join(_ROOT, "voyager_vlml")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


# ---- 问句 × 上下文矩阵 ------------------------------------------------------
# ctx = 场景 id（""=不带上下文）；truth = 独立口径 SQL（现场查真值）。
# 口径写在用例里 = 这题「按什么算」是测试声明的事实，不是实现里的隐藏规则。
CASES = [
    {"ctx": "B", "q": "九月份公司一共要支付多少钱",
     "truth": """SELECT (SELECT SUM(base_salary) FROM employees WHERE active)
                      + (SELECT SUM(amount) FROM allowance WHERE month = '2026-09')""",
     "note": "口径：在职基本工资 + 9 月全部津贴"},
    {"ctx": "B", "q": "技术部九月份工资发多少钱",
     "truth": """SELECT (SELECT SUM(base_salary) FROM employees
                         WHERE dept = '技术' AND active)
                      + (SELECT SUM(amount) FROM allowance
                         WHERE month = '2026-09'
                           AND emp_id IN (SELECT emp_id FROM employees
                                          WHERE dept = '技术'))""",
     "note": "口径：技术部在职基本工资 + 该部门 9 月津贴（离职不计）"},
    {"ctx": "B", "q": "九月份公司一共发了多少津贴",
     "truth": "SELECT SUM(amount) FROM allowance WHERE month = '2026-09'",
     "note": "口径：9 月津贴合计"},
    {"ctx": "B", "q": "技术部九月份一共发了多少津贴",
     "truth": """SELECT SUM(amount) FROM allowance WHERE month = '2026-09'
                  AND emp_id IN (SELECT emp_id FROM employees WHERE dept = '技术')""",
     "note": "口径：技术部 9 月津贴合计"},
    # 不带上下文：这里**没有**真值可比 —— 要验的恰恰是「不许编一个数出来」
    {"ctx": "", "q": "什么是强化学习", "truth": None,
     "note": "无环境的知识题：要有回复、要说清是推断，不许假装查到了数据"},
    # 带**上传文件**（不是库）：DeepSeek 客户端的形态 —— 文件内容进上下文,
    # 问文件里的事实，答案必须来自文件而不是凭记忆。
    {"ctx": "E", "q": "九月说明这份文件里，技术部九月的津贴合计是多少",
     "upload": ("九月说明.md",
                "技术部九月说明\n\n本月技术部在职 3 人，九月津贴合计 2120 元。\n"
                "（备注：公司整体九月津贴合计 3600 元，含财务 500、销售 980。）\n"
                .encode("utf-8")),
     "truth_const": 2120.0, "truth_from": "上传的文件内容",
     "note": "文件即上下文：真值来自文件正文（不是库、不是记忆）"},
]


def _db_of(scene: str) -> str:
    """场景对应的数据源（不写死路径：走 scene_profile 自己的解析）。"""
    os.environ["MVE_SCENE"] = str(scene)
    import scene_profile as _sp
    return str((_sp.profile(str(scene)) or {}).get("db_path") or "")


def _truth(db_path: str, sql: str) -> float:
    import duckdb
    con = duckdb.connect(db_path, read_only=True)
    try:
        row = con.execute(sql).fetchone()
    finally:
        con.close()
    return float(row[0])


def _import_consume():
    try:
        from voyager_vlml import consume as C  # noqa: F401
    except Exception:
        import consume as C  # noqa: F401
    return C


def run_case(C, case: dict, idx: int) -> None:
    import asyncio

    q = case["q"]
    ctx = str(case.get("ctx") or "")
    tag = f"上下文={'场景 ' + ctx if ctx else '无'}"
    print(f"\n== 用例 {idx}｜{tag}｜{q} ==")
    os.environ["MVE_SCENE"] = ctx or "A"
    if case.get("upload"):
        # 先把文件「交给模型」（落进上下文），再问 —— DeepSeek 客户端的顺序
        _nm, _blob = case["upload"]
        try:
            import upload as _u
            _ur = _u.ingest(ctx, _nm, _blob)
            check(f"文件已进上下文（{_nm}）", bool(_ur.get("ok")),
                  str(_ur.get("why") or _ur.get("kind") or "")[:80])
        except Exception as e:
            check(f"文件已进上下文（{_nm}）", False, f"{type(e).__name__}: {e}")
    expect = None
    if case.get("truth"):
        try:
            expect = _truth(_db_of(ctx), case["truth"])
        except Exception as e:
            check("取到真值（先查库再比）", False,
                  f"{type(e).__name__}: {e}")
            return
        print(f"  真值（现场查库）：{expect:,.2f}　{case.get('note') or ''}")
    elif case.get("truth_const") is not None:
        expect = float(case["truth_const"])
        print(f"  真值（{case.get('truth_from') or '用例给定'}）：{expect:,.2f}"
              f"　{case.get('note') or ''}")
    try:
        r = asyncio.run(C.consume(q))
    except Exception:
        check(f"跑通（{q}）", False, traceback.format_exc(limit=2)[-300:])
        return
    ans = str(r.get("answer") or "").strip()
    jr = str(r.get("judgment_result") or "")
    print(f"  答案：{ans[:200]}")
    print(f"  判读：{jr}｜技能：{[s.get('name') for s in (r.get('used_skills') or [])]}")

    # ① 句句有回复（R1 契约：答不了也要有交代）
    if not ans:
        check("有回复", False, "空答案")
        return
    if ans.lstrip().startswith("{") and '"rows"' in ans:
        check("有回复且是人话", False, "退化成原始 JSON 平铺")
        return
    check("有回复", True, ans[:60])

    if expect is None:
        # ② 不带上下文：不许把模型知识说成查到的数据
        check("没有假装查到数据（不给 written）", jr != "written",
              f"判读={jr}")
        try:
            import narrator as _n
            big = [x for x in _n.numbers_in_text(ans) if x >= 100]
        except Exception:
            big = []
        check("没有编造出金额/统计数字", not big, f"出现了 {big[:5]}")
        return

    # ③ 带上下文：答到真值
    try:
        import narrator as _n
        hit = _n.mentions_value(ans, expect)
    except Exception:
        hit = f"{expect:.2f}" in ans.replace(",", "")
    check(f"答到真值 {expect:,.2f}", hit, ans[:120])
    if case.get("truth_const") is not None:
        # 文件上下文作答：口径是「按文件答、说清来源」，不是判 written
        check("标注了来源（按上传的文件作答）", "上传的文件" in ans, ans[-60:])
    else:
        # 交付口径：真答出来了，不是「未验证/没学过」糊过去
        check("判读为 written（真答出来）", jr == "written",
              jr + "｜" + str(r.get("critic_note") or r.get("skipped") or "")[:90])


def main() -> int:
    argv = sys.argv[1:]
    only = argv[argv.index("--only") + 1] if "--only" in argv else ""
    ctx_f = argv[argv.index("--ctx") + 1] if "--ctx" in argv else None
    try:
        C = _import_consume()
    except Exception:
        print("❌ 消费模块加载失败（本地缺 duckdb/httpx？到服务器上跑）：")
        print(traceback.format_exc(limit=2)[-400:])
        return 2
    cases = [c for c in CASES if not only or only in c["q"]]
    if ctx_f is not None:
        cases = [c for c in cases
                 if (("none" if not c["ctx"] else c["ctx"]) == ctx_f)]
    print(f"== 上下文矩阵问答（与「在 DeepSeek 上问问题」同构）"
          f"｜{len(cases)} 个用例 ==")
    for i, c in enumerate(cases, 1):
        run_case(C, c, i)
    print(f"\n== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
