#!/usr/bin/env python3
"""叙述层数字对账（抄 DeepSeek R1 rule-based 口径）的测试。

核心案例 = 2026-10-28 场景 B 实测事故：83240.5 = 81000 基薪 + 2120 津贴
+ 20.5「加班费」（20.5 其实是 overtime_hours 的小时数）。
跑法：python mve/test_narrative_guard.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    from narrator import verify_answer_numbers

    # 事故数据的形状（多技能编排合并行：金额列 + 时长/数量列混在一张表）
    data = {"rows": [
        {"dept": "技术", "month": "2026-09",
         "total_base_salary": 81000.0, "total_allowance": 2120.0,
         "total_overtime_hours": 20.5, "total_leave_days": 3.0},
    ]}

    print("== 1. 事故复现：83240.5 必须被拦 ==")
    ok, why = verify_answer_numbers(
        "技术部9月应发工资总额为83240.5元，包括基本工资、津贴和加班费。",
        data, sum_auth=True)
    check("83240.5（混入 20.5 小时）→ 拒", not ok, why[:60])
    check("拒绝理由点名指认不了", "指认不了" in why)

    ok2, _ = verify_answer_numbers(
        "技术部9月应发工资总额为83120元（基本工资 81000 + 津贴 2120）。",
        data, sum_auth=True)
    check("83120 = 金额列子集和 → 放行", ok2)

    ok3, _ = verify_answer_numbers(
        "技术部基本工资合计 81000 元。", data, sum_auth=False)
    check("原样引用（verbatim）→ 放行", ok3)

    print("== 2. 非金额相加未授权 → 拒 ==")
    ok4, why4 = verify_answer_numbers(
        "合计 83240.5。", data, sum_auth=False)
    check("sum_auth=False 时混算 → 拒", not ok4, why4[:50])

    print("== 3. 日期与引用编号不算答案数字 ==")
    ok5, _ = verify_answer_numbers(
        "2026-09 技术部基薪 81000 元 [1]。", data, sum_auth=False)
    check("日期 + [1] 引用不误杀", ok5)

    # 季度标记也是时间（2026-10-28 实测：「技术部 2026Q3 的奖金总额为 2000 元」
    # 被 2026 卡死 → 答案退化成原始 JSON 平铺，2000 明明就在数据里）
    _dq = {"rows": [{"来源技能": "bonus_sum", "total_bonus": 2000}]}
    ok5b, why5b = verify_answer_numbers("技术部 2026Q3 的奖金总额为 2000 元。", _dq)
    check("2026Q3 不误杀", ok5b, why5b[:40])
    ok5c, why5c = verify_answer_numbers(
        "技术部 2026 年第三季度奖金合计 2,000.00 元 [1]", _dq)
    check("「2026 年第三季度」不误杀", ok5c, why5c[:40])
    ok5d, _ = verify_answer_numbers("技术部 Q3 奖金 3000 元", _dq)
    check("时间剥掉后真错的数仍然拦（3000）", not ok5d)

    print("== 4. 边界 ==")
    ok6, _ = verify_answer_numbers("没有数字的答案。", data)
    check("无数字 → 放行", ok6)
    ok7, _ = verify_answer_numbers("月薪 99,999 元。", data)
    check("千分位数字对不上 → 拒", not ok7)
    # 金额列键面判定：overtime_hours 的值不许当金额
    data2 = {"rows": [{"total_overtime_hours": 20.5, "pay": 100.0}]}
    ok8, _ = verify_answer_numbers("合计 120.5 元", data2, sum_auth=True)
    check("100（金额）+20.5（小时）拼不出 → 拒", not ok8)

    print("== 4.5 金额语义穿透「值」列表（服务器实测误杀修复）==")
    data3 = {"rows": [{"来源技能": "tech_base_salary", "值": [81000.0]},
                      {"来源技能": "tech_allowance", "值": [2120.0]}]}
    ok9, _ = verify_answer_numbers("合计 83120 元。", data3, sum_auth=True)
    check("81000+2120 经「值」列表 → 83120 放行", ok9)
    ok10, _ = verify_answer_numbers("基薪 81000。", data3, sum_auth=False)
    check("穿透后 verbatim 仍放行", ok10)

    print("== 5. consume 接线 ==")
    import inspect
    import voyager_vlml.consume as C
    src = inspect.getsource(C.consume)
    check("场景分支调用 verify_answer_numbers", "verify_answer_numbers" in src)
    check("有重采样（rejection sampling）", "note=_nar_note2" in src)
    check("有降级平铺", "退回原始数据平铺" in src)

    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
