#!/usr/bin/env python3
"""句句有回复契约的测试（2026-10-28 用户截图两事故的回归线）。

事故 ①：「什么是强化学习」句句无回复；
事故 ②：「技术部九月份工资发多少钱」被答成津贴 2120 且判 written/掌握✓
        （数据全真、无幻觉，但答非所问 —— 用户原话「有局限性，答案也不对」）。

抄 R1 的解法：答不了就换路（kick 学习）+ 每句必有交代 + **看全貌判对题**。
⚠️ 2026-10-28 用户拍板：「不能这样写死守卫解决，而是…让 agent 看到全貌」——
第一版曾用 `_scope_guard`/`_subject_guard`/`_completeness_guard` 三张业务词表
硬拦，已全部删除；现在由 `_fits_judge`（题面 + SQL + 结果 + 整库全貌 + 技能库
全库目录 → 判主体/口径/完整性）承担，词表写死**不许回归**（断言见第 4 节）。

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

    print("== 1. 全貌判官 _fits_judge：真数 ≠ 对题 ==")
    orig = C.chat_json
    seen: list[list[dict]] = []

    def _stub_ok(msgs, **k):
        seen.append(msgs)
        return {"ok": True, "why": "口径一致"}

    try:
        C.chat_json = _stub_ok
        r = C._fits_judge(
            "技术部九月份工资发多少钱",
            "SELECT SUM(base_salary) AS total FROM employees WHERE dept='技术'",
            {"rows": [{"total": 55000}], "columns": ["total"]},
            "/root/mve/scene_data/B/payroll.duckdb")
        check("判官判对 → ok", r["ok"] is True, r["why"][:30])
        msgs = seen[-1]
        sys_p = msgs[0]["content"]
        user_p = msgs[1]["content"]
        check("判官只问三件事（主体/口径/完整性）",
              "主体" in sys_p and "口径" in sys_p and "完整性" in sys_p)
        check("判官看得到实际 SQL", "SUM(base_salary)" in user_p)
        check("判官看得到真实取回的行", "55000" in user_p)
        check("判官拿到库全貌（表/列/取值）", "库全貌" in user_p)
        check("完整性只准依据全貌（不许用常识补表）",
              "missing 只准从白名单里挑" in sys_p
              and "别用行业常识补全貌里没有的东西" in sys_p)
        check("判官拿到数值列白名单（缺口的唯一合法候选集）",
              "数值列白名单" in user_p)

        # 多技能编排：逐条渲染，别让判官把拼起来的 SQL 误读成「同一条少算了」
        C._fits_judge("技术部九月份总共要发多少钱", "", {},
                      "", items=[
                          {"name": "tech_dept_base_salaries",
                           "sql": "SELECT SUM(base_salary) FROM employees "
                                  "WHERE dept='技术'",
                           "data": {"rows": [{"t": 55000}],
                                    "columns": ["t"]}},
                          {"name": "tech_dept_allowances",
                           "sql": "SELECT SUM(amount) FROM allowance",
                           "data": {"rows": [{"t": 2120}], "columns": ["t"]}}])
        _u = seen[-1][1]["content"]
        _s = seen[-1][0]["content"]
        check("多条 SQL 逐条渲染（带来源配方名）",
              "SQL 1" in _u and "SQL 2" in _u and "tech_dept_allowances" in _u)
        check("逐条各自配自己的结果", "55000" in _u and "2120" in _u)
        check("告知判官「上层负责合并」，别要求单条答完整题",
              "由上层合并/相加" in _u and "不要要求单条 SQL" in _s)

        C.chat_json = lambda *a, **k: {"ok": False,
                                       "why": "题面问工资总额，SQL 只算了基本工资，缺津贴"}
        r2 = C._fits_judge("技术部九月份总共要发多少钱",
                           "SELECT SUM(base_salary) FROM employees WHERE dept='技术'",
                           {"rows": [{"total": 55000}]}, "")
        check("判官判片面 → 不 ok", r2["ok"] is False)
        check("拒因原样带回（进重填反馈）", "缺津贴" in r2["why"])

        # 完整性缺口的机械裁决：只有 schema 白名单里真有的列才算「缺」
        # （防判官凭常识编「还有奖金/住房补贴没算」把正确答案怼死，2026-10-28）
        _orig_nums = C._numeric_columns
        try:
            C._numeric_columns = lambda p, limit=80: [
                "employees.base_salary", "allowance.amount"]
            C.chat_json = lambda *a, **k: {
                "subject_ok": True, "measure_ok": True,
                "missing": ["bonus.amount", "住房补贴"],
                "why": "没有包含奖金和住房补贴"}
            r4 = C._fits_judge("技术部九月份总共要发多少钱",
                               "SELECT SUM(base_salary) FROM employees", {}, "")
            check("点名的缺口不在库里 → 不采信、如实放行",
                  r4["ok"] is True and "按全貌放行" in r4["why"], r4["why"][:60])
            check("采信的缺口表为空", r4["missing"] == [])
            C.chat_json = lambda *a, **k: {
                "subject_ok": True, "measure_ok": True,
                "missing": ["allowance.amount"],
                "why": "漏了津贴"}
            r5 = C._fits_judge("技术部九月份总共要发多少钱",
                               "SELECT SUM(base_salary) FROM employees", {}, "")
            check("缺口是库里真实数值列 → 采信、退回",
                  r5["ok"] is False and "allowance.amount" in r5["why"])
            C.chat_json = lambda *a, **k: {"subject_ok": False,
                                           "measure_ok": True, "missing": [],
                                           "why": "题面问公司、SQL 只算技术部"}
            r6 = C._fits_judge("九月份公司一共要支付多少钱",
                               "SELECT SUM(base_salary) FROM employees "
                               "WHERE dept='技术'", {}, "")
            check("主体错 → 退回（55000 事故）", r6["ok"] is False)
            C.chat_json = lambda *a, **k: {"ok": True, "why": "老契约"}
            r7 = C._fits_judge("随便", "SELECT 1", {}, "")
            check("老契约（只给 ok/why）仍兼容", r7["ok"] is True)

            # 7950 事故：一轮里 3 条配方 2 条执行报错，判官只看成功的那条就
            # 说过关了 → 交出残缺总数。有失败项时必须判官**明确认可**才放行。
            _F = [{"name": "total_salary", "why": "month 列不存在"},
                  {"name": "overtime_pay", "why": "月份 列不存在"}]

            def _rec(resp):
                def _f(msgs, **k):
                    seen.append(msgs)
                    return resp
                return _f

            C.chat_json = _rec({"subject_ok": True, "measure_ok": True,
                                "missing": [], "why": "口径一致"})
            r8 = C._fits_judge("九月份公司一共要支付多少钱",
                               "SELECT SUM(amount) FROM allowance", {}, "",
                               fails=_F)
            check("有取数失败又没明确认可 → 不放行（7950 事故）",
                  r8["ok"] is False and "取数失败" in r8["why"], r8["why"][:70])
            check("失败项写进判官上下文",
                  "total_salary" in seen[-1][1]["content"])
            C.chat_json = _rec({"subject_ok": True, "measure_ok": True,
                                "missing": [], "failed_ok": True,
                                "why": "失败的是加班费，库里无费率"})
            r9 = C._fits_judge("九月份公司一共要支付多少钱",
                               "SELECT SUM(amount) FROM allowance", {}, "",
                               fails=_F)
            check("判官确认失败项不可算 → 放行", r9["ok"] is True, r9["why"][:60])
        finally:
            C._numeric_columns = _orig_nums

        def _boom(*a, **k):
            raise RuntimeError("llm down")

        C.chat_json = _boom
        r3 = C._fits_judge("随便", "SELECT 1", {"rows": [{"x": 1}]}, "")
        check("判官挂了 → 如实放行（绝不因故障拒掉答案）",
              r3["ok"] is True and "如实放行" in r3["why"])
    finally:
        C.chat_json = orig

    print("== 1.5 全貌供给（替代写死词表的两个来源） ==")
    check("_catalog_block 空库路径 → 空串不抛", C._catalog_block("") == "")
    check("_catalog_block 坏库路径 → 空串不抛（环境观察失败不阻塞）",
          C._catalog_block("/nonexistent/x.duckdb") == "")
    _cat, _all = C._scene_skill_catalog("no-such-scene-xyz")
    check("_scene_skill_catalog 返回 (目录文本, 全库技能表)",
          isinstance(_cat, str) and isinstance(_all, list))

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
    check("执行完先过全貌对题判官",
          "_fits_judge(question, code, data, _dbp" in src
          and "items=_executed" in src)
    check("solve 兜底路径也过判官",
          "整段 SQL 被对题判官退回" in src)
    check("判不过 → 带全库目录重填一轮", "catalog=_cat" in src and "_fb2" in src)
    check("重填用技能库全库（不只命中的几条）",
          "_scene_skill_catalog(_scene_id)" in src and "_all_sk or skills" in src)
    check("重填后仍不过 → 当没答出来（走学习线）",
          "重填后仍不对题" in src and "reused_skill = None" in src)
    check("回填层收得到全库目录",
          "catalog: str = \"\"" in src and "技能库全库目录" in src)
    check("复用逐条仍过单位守卫（数据一致性，非业务口径）",
          "_unit_guard(_sql, question)" in src)
    # ⚠️ 用户 2026-10-28 拍板：「不能这样写死守卫解决」—— 业务词表写死不许回归。
    # 判调用点/定义（docstring 里提到旧名做沿革说明是允许的）。
    _biz_guards = ("_scope_guard", "_subject_guard", "_completeness_guard")
    check("业务词表守卫已彻底删除（不许回归）",
          not any((f"def {g}(" in src or f"{g}(question" in src
                   or f"{g}(_sql" in src) for g in _biz_guards))
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
