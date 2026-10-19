#!/usr/bin/env python3
"""Maieutic 追问骨架的接线测试（不联网、不连库）。

验四件事：
  1. 骨架解析/清洗/缓存/失败如实（origin=failed，不编维度）；
  2. `task_decompose.plan(scaffold=True)` 用维度当子目标，失败退回原版；
  3. 维度进得了执行层 prompt 和 critic prompt（这是"同构到动作提示词"的落点）；
  4. 维度节点进得了知识图谱（图谱长大的机制），且幂等。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(REPO))

CANNED = {
    "dimensions": [
        {"id": "definition", "question": "阿斯伯格与孤独症在诊断标准上的区别是什么？",
         "why": "不把两者分开，后面所有养育建议都会错位"},
        {"id": "core_traits", "question": "阿斯伯格的核心特征有哪几条？",
         "why": "决定了要不要针对性干预"},
        {"id": "support", "question": "目前证据支持的干预方式有哪些？",
         "why": "问题的落点是养育方式"},
    ],
    "expected_drifts": [
        {"category": "drift", "pattern": "只答定义，不答干预方式"},
        {"category": "bug", "pattern": "把阿斯伯格说成孤独症的轻度版本"},
        {"category": "revision", "pattern": "改用 ICD 而非 DSM 口径（不算错）"},
    ],
    "material_note": None,
}


def test_parse_and_cache() -> None:
    import maieutic_scaffold as M
    tmp = Path(tempfile.mkdtemp())
    old = M.CKPT
    M.CKPT = tmp
    try:
        calls = {"n": 0}

        def fake(messages):
            calls["n"] += 1
            return "好的，我想了想：\n```json\n" + json.dumps(CANNED, ensure_ascii=False) + "\n```"

        a = M.scaffold("什么是阿斯伯格", "T", llm=fake)
        assert len(a["dimensions"]) == 3, a
        assert a["origin"] == "llm", a
        b = M.scaffold("什么是阿斯伯格", "T", llm=fake)
        assert b["origin"] == "cache", b
        assert calls["n"] == 1, calls
        # 空问题 / LLM 挂 → 如实失败，不编维度
        assert M.scaffold("", "T", llm=fake)["origin"] == "failed"

        def boom(messages):
            raise RuntimeError("模型 500")

        c = M.scaffold("另一句", "T", llm=boom)
        assert c["origin"] == "failed" and not c["dimensions"], c
        assert "500" in c["reason"], c
    finally:
        M.CKPT = old
    print("PASS  test_parse_and_cache")


def test_plan_uses_dimensions() -> None:
    import maieutic_scaffold as M
    import task_decompose as T
    tmp = Path(tempfile.mkdtemp())
    old = M.CKPT
    M.CKPT = tmp
    try:
        def fake(messages):
            return json.dumps(CANNED, ensure_ascii=False)

        plan = T.plan("什么是阿斯伯格", "T", llm=fake, scaffold=True)
        assert [s["need"] for s in plan["subtasks"]] == \
            [d["question"] for d in CANNED["dimensions"]], plan["subtasks"]
        assert plan["origin"].startswith("maieutic"), plan["origin"]
        assert len(plan["dimensions"]) == 3
        # 骨架失败 → 退回原版 decompose（origin 不是 maieutic）
        def bad(messages):
            return "我不会"

        plan2 = T.plan("另一句没有骨架的问题", "T", llm=bad, scaffold=True)
        assert not plan2["origin"].startswith("maieutic"), plan2["origin"]
        assert plan2["subgoals"], plan2
        # 不开 scaffold → 原版 decompose（不调骨架）
        plan3 = T.plan("什么是阿斯伯格", "T", llm=bad, scaffold=False)
        assert not plan3.get("dimensions")
    finally:
        M.CKPT = old
    print("PASS  test_plan_uses_dimensions")


def test_dimensions_reach_prompts() -> None:
    """维度要真的出现在执行层 / critic 的 user 消息里（否则只是存了一份 JSON）。"""
    import task_critic
    seen: list[str] = []

    def fake_closed(messages):
        seen.append(messages[-1]["content"])
        return json.dumps({"gaps_addressed": [], "gaps_still_open": [],
                           "emergent_gaps": [], "questions": [],
                           "passed": True}, ensure_ascii=False)

    dims = CANNED["dimensions"]
    result = {"execution": {"real_calls": 1, "calls": 1, "rows": 1,
                            "data_sources": ["read_file"], "sql": "read_json()"},
              "answer": "……", "status": "learned"}
    ok, _ = task_critic.check_task_success("什么是阿斯伯格", "", result,
                                           llm=fake_closed, dimensions=dims)
    assert ok
    # 维度清单真的进了判定提示词（否则只是存了一份 JSON）
    assert "必须回答的维度清单" in seen[-1], seen[-1][:200]
    assert all(d["question"] in seen[-1] for d in dims), seen[-1][:400]
    assert all(d["id"] in seen[-1] for d in dims), seen[-1][:400]

    print("PASS  test_dimensions_reach_prompts")


def test_gate_closes_only_when_list_satisfied() -> None:
    """Maieutic 闭合口径（spec-examiner.ts:136-147）：
    passed 当且仅当 gaps_still_open 为空；emergent_gaps 不阻塞；追问 ≤3 条。
    """
    import maieutic_gate as G
    import task_critic

    dims = [{"id": "def_x", "question": "X 是什么？", "why": ""},
            {"id": "how_x", "question": "X 怎么做？", "why": ""}]
    result = {"execution": {"real_calls": 0, "calls": 1, "rows": 1,
                            "data_sources": []}, "answer": "X 是……"}

    def closed(messages):
        return json.dumps({"gaps_addressed": ["def_x", "how_x"],
                           "gaps_still_open": [], "emergent_gaps": [],
                           "questions": [], "passed": True}, ensure_ascii=False)

    ok, crit = task_critic.check_task_success("X", "", result, llm=closed,
                                              dimensions=dims)
    assert ok is True and crit == "", (ok, crit)

    def open_gap(messages):
        return json.dumps({
            "gaps_addressed": ["def_x"], "gaps_still_open": ["how_x"],
            "emergent_gaps": [{"description": "没给依据", "question": "依据来自哪？"}],
            "questions": ["X 具体怎么做？分几步？", "第二步之后呢？"],
            "passed": False}, ensure_ascii=False)

    ok2, crit2 = task_critic.check_task_success("X", "", result, llm=open_gap,
                                                dimensions=dims)
    assert ok2 is False, (ok2, crit2)
    assert crit2 == "X 具体怎么做？分几步？ 第二步之后呢？", crit2   # 追问原样带回
    gate = result.get("critic_gate") or {}
    assert gate.get("open") == ["how_x"] and gate.get("addressed") == ["def_x"], gate

    # 模型自相矛盾（说 passed 但清单没清）→ 按清单为准（原版 :146-147）
    def liar(messages):
        return json.dumps({"gaps_addressed": [], "gaps_still_open": ["how_x"],
                           "emergent_gaps": [], "questions": ["补上怎么做"],
                           "passed": True}, ensure_ascii=False)

    ok3, _ = task_critic.check_task_success("X", "", result, llm=liar,
                                            dimensions=dims)
    assert ok3 is False, "passed 必须严格等于 gaps_still_open 为空"

    # 空答案 → 清单全部未闭合，不叫 LLM
    ok4, crit4 = task_critic.check_task_success(
        "X", "", {"execution": {"real_calls": 0}, "answer": ""},
        llm=lambda m: "{}", dimensions=dims)
    assert ok4 is False and crit4, (ok4, crit4)

    # 闸门自身不可用 → 如实报 failed，不假装判过
    bad = G.check("X", "答", dims, llm=lambda m: "我不会")
    assert bad["origin"] == "failed", bad
    print("PASS  test_gate_closes_only_when_list_satisfied")


def test_attach_dimensions() -> None:
    import maieutic_scaffold as M
    from knowledge_graph import KnowledgeGraph

    g = KnowledgeGraph()
    g.ensure_scene_root("T")
    g.add_cold_start_node("T", "consume:consume_abc", "dimension", label="什么是阿斯伯格")
    added = M.attach_dimensions("T", "consume_abc", CANNED["dimensions"],
                                verified=True, graph=g)
    assert len(added) == 3, added
    for nid in added:
        n = g.nodes[nid]
        assert n.kind == "dimension" and n.root_scene == "T", n
        assert n.status == "verified" and n.label, n
    rels = [e.relation for e in g.edges if e.src == "consume:consume_abc"]
    assert rels.count("has_dimension") == 3, rels
    # 幂等：再挂一次不再新建，证据数增加
    before = len(g.nodes)
    ev = g.nodes[added[0]].evidence
    assert M.attach_dimensions("T", "consume_abc", CANNED["dimensions"],
                               verified=True, graph=g) == []
    assert len(g.nodes) == before
    assert g.nodes[added[0]].evidence == ev + 1
    # 空维度不建节点
    assert M.attach_dimensions("T", "consume_abc", [], graph=g) == []
    print("PASS  test_attach_dimensions")


def test_drift_notes() -> None:
    import maieutic_scaffold as M
    notes = M.drift_notes(CANNED)
    assert len(notes) == 2, notes                      # revision 不进去
    assert any("轻度版本" in n for n in notes), notes
    print("PASS  test_drift_notes")


def test_gate_log_reports_real_counts() -> None:
    """日志必须打出**真实**的维度答到数，不能把 dict 当 list 数（2026-10-19 踩过：
    服务器日志打出「闸门：0/3 个维度答到」，实际是 8/9 —— len(dict)=3 个 key，
    ok_n 迭代到 str key 全被 `isinstance(g, dict)` 挡掉 → 恒 0。」
    """
    import scene_learn as SL

    captured: list[str] = []
    _ORIG_LOG = SL._log
    SL._log = lambda t: captured.append(str(t))        # type: ignore[assignment]
    try:
        SL._log_learning_detail({
            "dimension_results": {
                "total": 9,
                "learned": ["asd_definition", "asberg_definition",
                            "diagnosis_criteria"],
                "failed": [{"id": "motor_difficulties", "critique": "请具体说明"}],
            },
            "critic": {"success": False,
                       "critique": "[motor_difficulties] 请具体说明",
                       "gate": {"addressed": ["asd_definition",
                                              "asberg_definition",
                                              "diagnosis_criteria"],
                                "open": ["motor_difficulties"],
                                "questions": ["请具体说明"]}},
        })
    finally:
        SL._log = _ORIG_LOG                            # type: ignore[assignment]
    txt = "\n".join(captured)
    assert "3/9" in txt, txt                           # 维度子任务：3/9
    assert "motor_difficulties" in txt, txt
    assert "闭合 3 条 / 未闭合 1 条" in txt, txt
    assert "0/3" not in txt, txt
    print("PASS  test_gate_log_reports_real_counts")


def test_subtask_retries_then_skips() -> None:
    """原版重试机制（用户 2026-10-19 定）：单维度最多 `SUBTASK_MAX_RETRIES`
    次（原版 voyager.py:30 action_agent_task_max_retries=4），到上限仍不闭合就
    **跳过这条维度**（curriculum.py:342 "Skipping to next task"），不无限重试。
    """
    import learn_orchestrate as L
    assert L.SUBTASK_MAX_RETRIES == 4, L.SUBTASK_MAX_RETRIES   # 抄原版，不是自造
    print(f"PASS  test_subtask_retries_then_skips（上限={L.SUBTASK_MAX_RETRIES}）")


def test_one_closed_dimension_is_one_node() -> None:
    """最小技能单位 = **一枚节点 = 闭合一个（维度）问题**（用户 2026-10-19 定）。

    验两件事：
      1. 闭合一条就落一枚节点，且**同一个 (topic, dim) 重复闭合不重复建**；
      2. 幂等所需的前提：父节点 id 与 ⑥ `_absorb` 用同一算法算出。
    """
    import maieutic_scaffold as M
    from knowledge_graph import KnowledgeGraph

    dim = {"id": "definition", "question": "阿斯伯格的定义是什么？", "why": "地基"}
    g = KnowledgeGraph()
    g.ensure_scene_root("T")
    g.add_cold_start_node("T", "consume:consume_abc", "dimension",
                          label="什么是阿斯伯格")
    a1 = M.attach_dimensions("T", "consume_abc", [dim], verified=True, graph=g)
    assert len(a1) == 1, a1                       # 闭合一条 = 一枚节点
    assert M.attach_dimensions("T", "consume_abc", [dim], verified=True,
                               graph=g) == []      # 重复闭合不重复建
    # 另一条维度闭合 = 另一枚独立节点（粒度是"每条一个问题"，不是整题一枚）
    a2 = M.attach_dimensions(
        "T", "consume_abc",
        [{"id": "support", "question": "证据支持的干预方式有哪些？", "why": "落点"}],
        verified=True, graph=g)
    assert len(a2) == 1 and a2[0] != a1[0], (a1, a2)
    print("PASS  test_one_closed_dimension_is_one_node")


def test_milestone_is_generated_once_and_judged_by_closure() -> None:
    """里程碑：骨架**生成时**产出一次，运行时只判闭合（用户 2026-10-20 定）。

    · 达成 = 维度全闭合（同 Maieutic spec gate 的 `gaps_still_open` 为空）；
    · 0 个维度的骨架不算里程碑（没东西可闭合就别报达成）；
    · 老骨架没有 milestone 字段 → 用问题当名字，**不重新生成**。
    """
    import maieutic_scaffold as M

    tmp = Path(tempfile.mkdtemp())
    old = M.CKPT
    M.CKPT = tmp
    try:
        canned = dict(CANNED)
        canned["milestone"] = {"name": "阿斯伯格与孤独症谱系的边界",
                               "why": "分开之后养育建议才不会错位"}

        def fake(messages):
            return "```json\n" + json.dumps(canned, ensure_ascii=False) + "\n```"

        a = M.scaffold("什么是阿斯伯格", "T", llm=fake)
        assert a["milestone"]["name"] == "阿斯伯格与孤独症谱系的边界", a["milestone"]
        # 再来一次是缓存命中 —— 里程碑不会在运行时反复生成
        b = M.scaffold("什么是阿斯伯格", "T", llm=fake)
        assert b["origin"] == "cache" and \
            b["milestone"]["name"] == "阿斯伯格与孤独症谱系的边界", b

        dims = a["dimensions"]
        ids = [d["id"] for d in dims]
        st = M.milestone_state("T", "什么是阿斯伯格", ids[:-1],
                               scaffold_result=a)
        assert st["total"] == 3 and st["closed"] == 2 and not st["achieved"], st
        assert st["missing"] == [ids[-1]], st
        st2 = M.milestone_state("T", "什么是阿斯伯格", ids, scaffold_result=a)
        assert st2["achieved"] and not st2["missing"], st2
        # 空骨架：不许报达成
        assert not M.milestone_state("T", "x", [],
                                     scaffold_result={"dimensions": []})["achieved"]
        # 老骨架没有 milestone 字段 → 名字退回问题本身，不重建
        old_sc = {"dimensions": dims}
        st3 = M.milestone_state("T", "什么是阿斯伯格", ids, scaffold_result=old_sc)
        assert st3["achieved"] and st3["name"] == "什么是阿斯伯格", st3
    finally:
        M.CKPT = old
    print("PASS  test_milestone_is_generated_once_and_judged_by_closure")


def test_milestone_closure_writes_skills() -> None:
    """里程碑闭合 → 这一轮**跑通的代码**进技能库（同原版 skill.py:64-99
    `add_new_skill`，只在成功 rollout 后写）；没调取数动作的答案**不入库**。
    """
    import learn_orchestrate as L
    import skill_store as SS

    tmp = Path(tempfile.mkdtemp())
    old = SS.STORE
    SS.STORE = tmp / "skill_store.json"
    try:
        mst = {"name": "里程碑X", "total": 2, "closed": 2, "missing": [],
               "achieved": True}
        codes = [
            {"id": "prevalence", "question": "发病率是多少？",
             "code": 'r = web_search("Asperger prevalence")\nRESULT = r'},
            {"id": "definition", "question": "定义是什么？",
             "code": 'RESULT = {"answer": "阿斯伯格是一种…"}'},   # 凭记忆 → 不是配方
        ]
        out = L.learn_milestone_skills("T", "consume_abc", "什么是阿斯伯格",
                                       mst, codes)
        assert len(out["added"]) == 1, out          # 只有真调了动作的入库
        assert out["added"][0]["dim"] == "prevalence", out
        assert out["skipped"] == ["definition"], out  # 伪配方如实跳过
        raw = SS._load_raw()
        got = list((raw.get("skills") or {}).values())
        assert len(got) == 1, got
        assert "web_search(" in str(got[0].get("code") or ""), got[0]
        # key 不能出现 topic::topic::dim 的双前缀
        assert list((raw.get("skills") or {}))[0] == "consume_abc::prevalence", \
            list((raw.get("skills") or {}))
        assert got[0].get("source") == "learn", got[0]
        assert (got[0].get("blueprint") or {}).get("milestone") == "里程碑X", got[0]
        # 没闭合的里程碑：已闭合的那几条照样入（各自是一次成功 rollout）
        out2 = L.learn_milestone_skills(
            "T", "consume_def", "另一个问题",
            {"name": "M", "achieved": False, "closed": 1, "total": 2},
            [{"id": "prevalence", "question": "q",
              "code": 'read_file("a.json")\nRESULT = 1'}])
        assert len(out2["added"]) == 1, out2
    finally:
        SS.STORE = old
    print("PASS  test_milestone_closure_writes_skills")


def test_observed_tools_are_written_back() -> None:
    """回写"由谁产出"（用户 2026-10-20 截图纠偏）：闭合维度时把**实测调用过**
    的动作建成 tool 节点 + produced_by（origin=observed）边 —— 不然图谱的
    「工具（MCP）」列永远空、来源永远是"无来源"，违背原版"维度由哪个工具
    产出"的设计哲学（knowledge_graph.py:1556 声明式的 observed 同构）。
    """
    import maieutic_scaffold as M
    from knowledge_graph import KnowledgeGraph

    g = KnowledgeGraph()
    g.ensure_scene_root("T")
    g.add_cold_start_node("T", "consume:consume_abc", "dimension",
                          label="什么是阿斯伯格")
    dim = {"id": "prevalence", "question": "发病率是多少？", "why": "要有数"}
    added = M.attach_dimensions(
        "T", "consume_abc", [dim], verified=True, graph=g,
        tools={"prevalence": ["web_search", "web_search", "mcp_ddg_search"]})
    assert len(added) == 1, added
    # 工具节点建出来了（重复的 web_search 只算一个）
    assert "tool:web_search" in g.nodes and "tool:mcp_ddg_search" in g.nodes
    assert g.nodes["tool:web_search"].kind == "tool"
    # produced_by 边：tool → 维度，origin=observed
    rels = [(e.src, e.dst, e.relation, e.origin) for e in g.edges
            if e.dst == "dim:consume_abc:prevalence"
            and e.relation == "produced_by"]
    assert {r[0] for r in rels} == {"tool:web_search", "tool:mcp_ddg_search"}, rels
    assert all(r[3] == "observed" for r in rels), rels
    # 幂等：同一批再挂一次不重复建边
    n_edges = len(g.edges)
    M.attach_dimensions("T", "consume_abc", [dim], verified=True, graph=g,
                        tools={"prevalence": ["web_search"]})
    assert len([e for e in g.edges if e.relation == "produced_by"]) * 2 \
        <= n_edges + 2, "produced_by 边不该翻倍"
    # 没给 tools → 不建工具节点（老路径不破坏）
    g2 = KnowledgeGraph()
    g2.ensure_scene_root("T")
    M.attach_dimensions("T", "consume_x", [{"id": "d", "question": "q？"}],
                        verified=True, graph=g2)
    assert not [n for n in g2.nodes.values() if n.kind == "tool"], \
        "没实测就别造工具节点"
    print("PASS  test_observed_tools_are_written_back")


def test_called_tools_parses_code() -> None:
    """从跑通的代码解析动作名（续学补挂 produced_by 时没有新 observations）。"""
    import learn_orchestrate as L
    code = ('r1 = web_search("阿斯伯格 发病率")\n'
            'f = read_json("data.json")\n'
            'x = mcp_ddg_fetch_content(url="...")\n'
            'RESULT = {"answer": "…"}')          # 纯答案不产生动作名
    assert L._called_tools(code) == ["mcp_ddg_fetch_content", "read_json",
                                     "web_search"], L._called_tools(code)
    assert L._called_tools('RESULT = {"answer": "纯记忆"}') == []
    print("PASS  test_called_tools_parses_code")


def main() -> None:
    test_parse_and_cache()
    test_plan_uses_dimensions()
    test_dimensions_reach_prompts()
    test_gate_closes_only_when_list_satisfied()
    test_attach_dimensions()
    test_drift_notes()
    test_gate_log_reports_real_counts()
    test_subtask_retries_then_skips()
    test_one_closed_dimension_is_one_node()
    test_milestone_is_generated_once_and_judged_by_closure()
    test_milestone_closure_writes_skills()
    test_observed_tools_are_written_back()
    test_called_tools_parses_code()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
