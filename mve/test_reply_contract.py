#!/usr/bin/env python3
"""句句有回复契约的测试（2026-10-28 用户截图两事故的回归线）。

事故 ①：「什么是强化学习」句句无回复；
事故 ②：「技术部九月份工资发多少钱」被答成津贴 2120 且判 written/掌握✓
        （数据全真、无幻觉，但答非所问 —— 用户原话「有局限性，答案也不对」）。

抄 R1 的解法：最终结论 rule-based 对账（口径守卫）+ 答不了就换路
（rejection sampling 同构：拒了重采样下一技能）+ 每句必有交代。

跑法：python mve/test_reply_contract.py（服务器无 pytest，同 test_*.py 口径）。
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "voyager_vlml"))

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
    os.chdir(os.path.join(os.path.dirname(_HERE), "voyager_vlml"))
    import consume as C

    print("== 1. 口径守卫：真数 ≠ 对题 ==")
    g = C._scope_guard("技术部九月份工资发多少钱",
                       "SELECT SUM(amount) AS total_allowance FROM allowance "
                       "WHERE kind IN ('餐补','交通') AND month='2026-09'")
    check("题面问工资、配方算津贴 → 拒", bool(g), str(g)[:50])
    check("拒绝原因点明口径", "答非所问" in g and "工资" in g)
    check("基薪+津贴的组合 SQL → 放行", not C._scope_guard(
        "技术部九月份工资发多少钱",
        "SELECT SUM(e.base_salary) + (SELECT SUM(a.amount) FROM allowance a) "
        "FROM employees e WHERE e.dept='技术'"))
    check("餐补/补贴题配 allowance → 放行", not C._scope_guard(
        "技术部9月的餐补和交通补贴总额",
        "SELECT SUM(amount) FROM allowance WHERE kind IN ('餐补','交通')"))
    check("加班费配 overtime 列 → 放行", not C._scope_guard(
        "技术部9月加班费发了多少", "SELECT SUM(total_ot_paid) FROM payroll"))
    check("题面无钱词 → 不误伤", not C._scope_guard(
        "列出所有员工姓名", "SELECT name FROM employees"))
    check("「一共要支付多少钱」配 allowance-only → 拒", bool(C._scope_guard(
        "九月份公司一共要支付多少钱", "SELECT SUM(amount) FROM allowance")))

    print("== 2. 数据题判定（决定答不出时走哪条路） ==")
    check("「工资发多少」是数据题", C._is_data_question("技术部九月份工资发多少钱"))
    check("「什么是强化学习」不是数据题", not C._is_data_question("什么是强化学习"))
    check("「平均/排名/哪些」是数据题",
          C._is_data_question("各部门平均工资排名") and
          C._is_data_question("哪些员工有津贴"))

    print("== 3. inference 兜底（句句有回复的最后一层） ==")
    orig = C.chat_json
    try:
        C.chat_json = lambda *a, **k: {"answer": "强化学习是智能体通过与环境"
                                        "交互试错来学习策略的机器学习方法。",
                                        "grounded": True}
        inf = C._inference_answer("什么是强化学习")
        check("模型能答 → used + 有答案", inf.get("used") and "强化学习" in inf["answer"])
        C.chat_json = lambda *a, **k: {"answer": "我不知道", "grounded": False}
        inf2 = C._inference_answer("明天股价多少")
        check("模型自评不知 → 如实不答（不编）",
              not inf2.get("used") and "如实不答" in inf2["note"])
        def _boom(*a, **k):
            raise RuntimeError("llm down")
        C.chat_json = _boom
        inf3 = C._inference_answer("什么是强化学习")
        check("LLM 挂了 → 留 note 不抛", not inf3.get("used") and "失败" in inf3["note"])
    finally:
        C.chat_json = orig

    print("== 4. 消费主流程接线 ==")
    src = open("consume.py", encoding="utf-8").read()
    check("复用逐条过口径守卫", "_scope_guard(question, _sql)" in src)
    check("整段 SQL 再兜一道口径守卫", "_scope_guard(question, code)" in src)
    check("口径拒因进重采样反馈（_fails）",
          src.index("_scope_guard(question, _sql)") < src.index('feedback=_fb'))
    check("复用逐条也过单位守卫", "_unit_guard(_sql, question)" in src)
    check("答不出数据题 → 自动 kick_learning",
          "kick_learning(" in src and "_is_data_question(question)" in src)
    check("kick 注明同构语义（答不了就进入学习）", "进入学习" in src)
    check("非数据题先 inference 后工具线",
          src.index("_inference_answer(question)") < src.index("_tool_line_fallback(question)"))
    check("内部数据题不联网（_internal_hit 闸）", "_internal_hit" in src)
    check("base 带 inference_used", '"inference_used": bool(inference_used)' in src)
    check("兜底的兜底也句句有回复（判官结论入正文）",
          "判官结论：" in src and "不编数" in src)

    print("== 5. 落盘与披露链（白名单惯犯第 7 次预防） ==")
    coach_src = open(os.path.join(os.path.dirname(_HERE), "mve", "coach.py"),
                     encoding="utf-8").read()
    check("coach 白名单透传 inference_used", '"inference_used"' in coach_src)
    dash_src = open(os.path.join(os.path.dirname(_HERE), "mve", "dashboard.py"),
                    encoding="utf-8").read()
    check("x_mve 带 learn_kicked / inference_used",
          '"learn_kicked": bool' in dash_src and '"inference_used": bool' in dash_src)
    check("没答出来的回复带判官结论与学习状态",
          "判官结论" in dash_src and "已自动拉起后台学习" in dash_src)
    chat_src = open(os.path.join(os.path.dirname(_HERE), "mve", "chat_page.py"),
                    encoding="utf-8").read()
    check("思考轨迹写「已自动拉起后台学习」", "已自动拉起后台学习" in chat_src)
    check("标签行有「已派学习」「模型推断」", "已派学习" in chat_src and "模型推断" in chat_src)

    print("== 6. 回复降级链的顺序（思考轨迹） ==")
    import chat_page as CP
    r = CP.reasoning_text(dict(critic_note="口径不符：题面问工资、配方算津贴",
                               learn_kicked=True,
                               learn_kick_note="已自动拉起后台学习"),
                          scene_id="B", scene_label="工资")
    check("判官局限写进思考轨迹", "核对没过" in r and "口径不符" in r)
    check("已派学习写进思考轨迹", "已自动拉起后台学习" in r)

    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
