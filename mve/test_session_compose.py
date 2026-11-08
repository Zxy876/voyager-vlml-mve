#!/usr/bin/env python3
"""会话（= 组合了多条技能的「过程」）三件事的回归测试。

跑法：python mve/test_session_compose.py（本地可跑，不依赖 duckdb / LLM）。

背景（用户 2026-11-09 定的方向）：
  **把索引技能变成索引过程（会话）。**
  · 原版 maieutic：一个会话 = 为同一个目标走完的一整条学习链，跨轮
    **累积并集**（`sessions.ts:263-267`）；检索会话 = 拿到为这个问题
    积累的**全部**方法。
  · 原版 Voyager：一条技能 = 一段**可执行程序**（`skill.py:89` 存
    `program_code`）；技能之间互相可见（`index.js:256` `programs + code`
    同一作用域 eval），所以「组合多条技能」就写在这段程序里。

MVE 之前的三处断裂（这份测试逐个钉死）：
  ① 会话技能存的是**裸 SQL**（`SELECT COUNT(*) ...`）→ 一调就
     `SyntaxError: Invalid star expression`（`*` 被当 Python 星号表达式）
     → 召回有（hits 高）但一条都跑不起来。
  ② 技能重放时的 ns 里**没有兄弟** → 组合型技能必然 NameError
     （实测 `consume_7c45c9b6` 调 `skill_web_docs_2026_activity()`
     → `name ... is not defined`）。
  ③ 消费侧检索命中会话后**不把它的组成技能带出来** → 拿到的还是碎片
     配方，组合不全 → 消费不到（用户痛点 1/2 同源）。
"""

import os
import sys
import tempfile

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
    import local_agent
    from learn_orchestrate import _session_program
    from voyager_vlml.consume import _expand_session_skills, _is_session_skill

    tmp = tempfile.mkdtemp()

    # ---- 1. 会话程序是**组合调用**，不是裸 SQL ----
    print("== 1. 会话程序形态（组合调用 + RESULT，不是裸 SQL） ==")
    prog = _session_program([("d_1", "skill_web_facts_row_count"),
                              ("d_2", "skill_web_facts_first_item")],
                             "SELECT COUNT(*) AS total_rows FROM web_facts")
    check("调用了第一条兄弟技能", "skill_web_facts_row_count()" in prog)
    check("调用了第二条兄弟技能", "skill_web_facts_first_item()" in prog)
    check("有 RESULT 汇总", "RESULT = {" in prog)
    check("不是裸 SQL（不以 SELECT 开头）",
          not prog.strip().upper().startswith(("SELECT", "WITH")),
          prog.splitlines()[0][:50])
    check("整体取数仍在（组合 SQL 保留）",
          "query_sql(sql=" in prog and "web_facts" in prog)
    # 旧形态的病根：`SELECT COUNT(*)` 当 Python 代码 → 星号表达式语法错
    check("裸 SQL 会炸的那处不再出现（SQL 在字符串里，不在语句层）",
          "COUNT(*)" not in prog.split("query_sql(")[0])

    # ---- 2. 会话程序真能跑出 RESULT ----
    print("== 2. 会话程序真执行（兄弟调得到、RESULT 有真值） ==")
    skill_a = {"name": "dim_a",
               "code": "r = skill_dim_b()\nRESULT = {\"a\": r}"}
    skill_b = {"name": "dim_b",
               "code": "RESULT = {\"b\": 42}"}
    fns = local_agent._build_skill_fns([skill_a, skill_b], tmp, "",
                                        tracer=[])
    check("兄弟技能互相看得见（两个函数都造出来了）",
          "skill_dim_a" in fns and "skill_dim_b" in fns,
          ", ".join(sorted(fns.keys())))
    ns = dict(fns)
    box = local_agent._run_restricted(
        "d_1 = skill_dim_a()\n"
        "d_2 = skill_dim_b()\n"
        "RESULT = {\"dims\": {\"a\": d_1, \"b\": d_2}}", ns, timeout=20)
    check("会话程序执行成功", bool(box.get("ok")), str(box.get("error") or "")[:120])
    res = ns.get("RESULT") or {}
    check("RESULT 拿到兄弟的结果",
          isinstance(res, dict)
          and (res.get("dims") or {}).get("a") == {"a": {"b": 42}}
          and (res.get("dims") or {}).get("b") == {"b": 42},
          str(res)[:120])

    # ---- 3. 技能重放绑**同一个库**（db_path 透传） ----
    print("== 3. db_path 透传（技能里的 query_sql 查同一个库） ==")
    _seen = {"db": None}

    def _fake_query_sql(scene_dir, sql, db_path=""):
        _seen["db"] = db_path
        return {"rows": [[1]], "columns": ["x"]}

    _orig_q = local_agent.query_sql
    local_agent.query_sql = _fake_query_sql
    try:
        fn = local_agent._make_skill_fn(
            {"name": "q_one",
             "code": "r = query_sql(sql=\"SELECT 1\")\nRESULT = r"},
            tmp, "", None, db_path="/tmp/some_scene.duckdb")
        fn()
    finally:
        local_agent.query_sql = _orig_q
    check("技能里的 query_sql 绑的是传入的 db_path",
          _seen["db"] == "/tmp/some_scene.duckdb",
          f"实际绑了 {_seen['db']!r}")

    # ---- 4. 消费侧：会话命中 → 组成技能被带出 ----
    print("== 4. 消费侧按会话检索 → 带出全部组成技能 ==")
    import skill_store
    _raw = {"skills": {
        "T1::milestone": {
            "topic": "T1", "name": "milestone", "text": "会话",
            "code": "RESULT = {}", "source_scene": "L",
            "blueprint": {"from": "maieutic_milestone_composite",
                          "dims": ["T1::dim_x", "T1::dim_y"]}},
        "T1::dim_x": {"topic": "T1", "name": "dim_x", "text": "维度X",
                      "code": "RESULT = 1", "source_scene": "L"},
        "T1::dim_y": {"topic": "T1", "name": "dim_y", "text": "维度Y",
                      "code": "RESULT = 2", "source_scene": "L"},
        # 同名维度散在别的 topic 下 —— 不能被误带进来（拿错技能 = 组合出错的数）
        "T2::dim_x": {"topic": "T2", "name": "dim_x", "text": "别的题",
                      "code": "RESULT = 9", "source_scene": "L"},
    }}
    _orig_load = skill_store._load_raw
    skill_store._load_raw = lambda: _raw
    try:
        _hit = [{"key": "T1::milestone", "topic": "T1", "name": "milestone",
                 "text": "会话", "code": "RESULT = {}", "source_scene": "L",
                 "blueprint": _raw["skills"]["T1::milestone"]["blueprint"]}]
        check("会话能被识别", _is_session_skill(_hit[0]))
        out = _expand_session_skills(_hit, "L")
        names = [str(s.get("name") or "") for s in out]
        check("会话排最前（材料清单按前 N 条截断，不前置会被挤掉）",
              names and names[0] == "milestone", ", ".join(names))
        check("组成技能 dim_x 被带出", "dim_x" in names, ", ".join(names))
        check("组成技能 dim_y 被带出", "dim_y" in names, ", ".join(names))
        check("组成技能带出了来源会话（可追溯）",
              any(str(s.get("from_session") or "") == "T1::milestone"
                  for s in out))
        # 老条目：dims 存裸 dim_id（维度 id 不是全局唯一）
        _old = {"skills": {
            "T1::milestone": {
                "topic": "T1", "name": "milestone", "text": "老会话",
                "code": "RESULT = {}", "source_scene": "L",
                "blueprint": {"from": "maieutic_milestone_composite",
                              "dims": ["dim_x"]}},
            "T1::dim_x": {"topic": "T1", "name": "dim_x", "text": "维度X",
                          "code": "RESULT = 1", "source_scene": "L"},
            "T2::dim_x": {"topic": "T2", "name": "dim_x", "text": "别的题",
                          "code": "RESULT = 9", "source_scene": "L"}}}
        skill_store._load_raw = lambda: _old
        out2 = _expand_session_skills(
            [{"key": "T1::milestone", "topic": "T1", "name": "milestone",
              "text": "老会话", "code": "RESULT = {}", "source_scene": "L",
              "blueprint": _old["skills"]["T1::milestone"]["blueprint"]}], "L")
        names2 = [str(s.get("name") or "") for s in out2]
        check("老条目（裸 dim_id）也能带出组成技能", "dim_x" in names2,
              ", ".join(names2))
        check("同名维度不跨 topic 误取",
              all(str(s.get("topic") or "") == "T1" for s in out2),
              ", ".join("%s/%s" % (s.get("topic"), s.get("name")) for s in out2))
    finally:
        skill_store._load_raw = _orig_load

    # ---- 5. 会话 code 里放**真值**，原样重放跑得通 ----
    # ⚠️ 2026-11-09 实测踩过的坑：会话 code 里放**泛化后的槽位模板**
    # （`WHERE "GitHub" = :status`）→ 原样重放报
    # `ParserException: syntax error at or near ":"`（槽位没实例化）。
    # MVE 的维度技能本来就没被泛化（code 里写死 'CLAIMED'/'ZXY876'，
    # 靠消费侧回填层按当前问句换实体）—— 会话必须跟维度技能同一套形态：
    # code 里放真值，泛化模板另存 blueprint 供回填层参考。
    print("== 5. 会话 code 用真值 → 原样重放跑得通 ==")
    _orig_q2 = local_agent.query_sql
    local_agent.query_sql = (lambda scene_dir, sql, db_path="": {
        "rows": [["https://www.github.com/torvalds"]], "columns": ["source_url"]})
    try:
        _p2 = _session_program([("d_1", "skill_github_claimed_first_source_url")],
                               "SELECT source_url FROM web_facts "
                               "WHERE \"GitHub\" = 'CLAIMED' LIMIT 1")
        _ns2 = local_agent._ns_base(tmp, "", db_path="/tmp/x.duckdb")
        _fn2 = local_agent._make_skill_fn(
            {"name": "github_claimed_first_source_url",
             "code": "r = query_sql(sql=\"SELECT source_url FROM web_facts "
                     "WHERE \\\"GitHub\\\" = 'CLAIMED' LIMIT 1\")\nRESULT = r"},
            tmp, "", None, db_path="/tmp/x.duckdb")
        _ns2[_fn2.__name__] = _fn2
        _box2 = local_agent._run_restricted(_p2, _ns2, timeout=20)
        check("会话程序原样执行成功（真值 SQL）", bool(_box2.get("ok")),
              str(_box2.get("error") or "")[:140])
        _r2 = _ns2.get("RESULT") or {}
        check("拿到真实数据",
              isinstance(_r2, dict)
              and "torvalds" in str(_r2.get("overall") or ""),
              str(_r2.get("overall"))[:120])
        check("兄弟结果也进 RESULT",
              isinstance(_r2, dict)
              and "torvalds" in str(_r2.get(
                  "github_claimed_first_source_url") or ""),
              str(_r2)[:160])
        # 设计决定：**槽位模板不许进 code**。code 里放真值 → 原样重放跑得通；
        # 放模板 → 槽位没实例化 → DuckDB 报
        # `ParserException: syntax error at or near ":"`（2026-11-09 实测）。
        # 守住这条：迁移脚本必须能把「含槽位的会话」认出来并重建。
        from migrate_session_code import _is_old_form
        check("含槽位的会话被认出来（迁移脚本会重建它）",
              _is_old_form('r = query_sql(sql="SELECT x WHERE y = :status")'))
        check("真值会话不被误判（不会反复重建）",
              not _is_old_form(_p2),
              _p2.splitlines()[-2][:80] if _p2 else "")
    finally:
        local_agent.query_sql = _orig_q2

    # ---- 6. 没有会话时，行为不变（不凭空多带技能） ----
    print("== 6. 没有会话 → 列表原样返回（不改变既有检索行为） ==")
    _plain = [{"key": "T1::dim_x", "topic": "T1", "name": "dim_x"}]
    check("非会话列表原样返回",
          _expand_session_skills(_plain, "L") == _plain)

    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
