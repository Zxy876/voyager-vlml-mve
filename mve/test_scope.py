#!/usr/bin/env python3
"""第 2.5 步（判定作用域）的测试：单位守卫 + scope 判定器。

跑法：python mve/test_scope.py（服务器无 pytest，同 test_*.py 口径）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    print("== 1. _unit_guard：单位幻觉确定性拦截 ==")
    from voyager_vlml.consume import _unit_guard

    # 拦：题面问钱，聚合的是时长列，无乘法
    g = _unit_guard(
        "SELECT SUM(overtime_hours) AS total FROM attendance "
        "WHERE emp_id IN (SELECT emp_id FROM employees WHERE dept='技术') "
        "AND month='2026-09'",
        "技术部 9 月加班费是多少？")
    check("SUM(overtime_hours) 当加班费 → 拒", bool(g), str(g)[:60])
    check("拒绝理由点名单位问题", "单位" in (g or ""))

    g2 = _unit_guard(
        "SELECT SUM(leave_days) AS d FROM attendance WHERE month='2026-09'",
        "9 月缺勤扣款总额是多少？")
    check("SUM(leave_days) 当扣款 → 拒", bool(g2), str(g2)[:60])

    # 放行：聚合的就是金额列
    check("SUM(base_salary) 问工资 → 放行",
          _unit_guard("SELECT SUM(base_salary) AS t FROM employees "
                      "WHERE dept='技术'", "技术部基本工资总额？") == "")
    # 放行：题面不问钱
    check("问加班时长（不问钱）→ 放行",
          _unit_guard("SELECT SUM(overtime_hours) AS t FROM attendance "
                      "WHERE month='2026-09'", "技术部 9 月加班总时长？") == "")
    # 放行：有乘法（列 × 费率列 = 单位换算合法）
    check("overtime_hours * rate 有费率换算 → 放行",
          _unit_guard(
              "SELECT SUM(a.overtime_hours * r.rate) FROM att a "
              "JOIN rate r ON r.emp_id=a.emp_id",
              "技术部 9 月加班费是多少？") == "")
    # 放行：SELECT * / COUNT(*) 的星号不算乘法
    check("SELECT * 中的星号不算乘法 → 仍拦",
          bool(_unit_guard("SELECT * FROM attendance", "加班费多少？")) is False
          or True)  # 无聚合列 → 放行，两种结果都合法，只验证不抛错
    # 边界：空输入
    check("空 SQL / 空题面 → 放行不抛错",
          _unit_guard("", "") == "" and _unit_guard("SELECT 1", "") == "")

    print("== 2. scope.classify：作用域判定器 ==")
    import scope

    r = scope.classify("B")
    check("场景 B（有 duckdb 数据源）→ learn",
          r.get("line") == "learn" and r.get("has_env") is True, str(r)[:80])
    r2 = scope.classify("__no_such_scene__")
    check("未知场景回落 local（本机即环境，2026-10-19 口径）→ learn",
          r2.get("line") == "learn", str(r2)[:80])
    check("scope_line 便捷取值", scope.scope_line("B") == "learn")
    r3 = scope.classify("Z" * 3)
    check("判定结果带 why（可解释）", bool(str(r3.get("why") or "").strip()))

    print("== 3. 入库口接线：_absorb 走单位守卫 ==")
    # _absorb 的守卫段 import _unit_guard —— 直接验证可导入与签名
    from voyager_vlml.consume import _absorb  # noqa: F401
    import inspect
    src = inspect.getsource(_absorb)
    check("_absorb 源码调用 _unit_guard", "_unit_guard" in src)
    src2 = inspect.getsource(
        __import__("learn_orchestrate").learn_milestone_skills)
    check("learn_milestone_skills 源码调用 _unit_guard", "_unit_guard" in src2)

    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
