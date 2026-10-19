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

    def fake(messages):
        seen.append(messages[-1]["content"])
        return json.dumps({"reasoning": "ok", "success": True, "critique": ""})

    dims = CANNED["dimensions"]
    result = {"execution": {"real_calls": 1, "calls": 1, "rows": 1,
                            "data_sources": ["read_file"], "sql": "read_json()"},
              "answer": "……", "status": "learned"}
    ok, _ = task_critic.check_task_success("什么是阿斯伯格", "", result,
                                           llm=fake, dimensions=dims)
    assert ok
    assert all(d["question"] in seen[-1] for d in dims), seen[-1][:400]
    assert "drift" in seen[-1], seen[-1][:300]

    # ---- 闸门：LLM 说「有一条没答到」→ 就算它 success=true 也必须判否 ----
    def fake_drift(messages):
        return json.dumps({"reasoning": "定义答了，干预没答", "success": True,
                           "critique": "干预方式缺",
                           "dimensions": [
                               {"id": "definition", "answered": True, "why": ""},
                               {"id": "support", "answered": False,
                                "why": "只说'要支持'，没给具体干预方式"}]},
                          ensure_ascii=False)

    ok2, crit2 = task_critic.check_task_success(
        "什么是阿斯伯格", "", result, llm=fake_drift, dimensions=dims)
    assert ok2 is False, (ok2, crit2)
    assert "闸门未过" in crit2 and "support" in crit2, crit2
    assert result.get("critic_gate"), result
    print("PASS  test_dimensions_reach_prompts（含闸门判否）")


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


def main() -> None:
    test_parse_and_cache()
    test_plan_uses_dimensions()
    test_dimensions_reach_prompts()
    test_attach_dimensions()
    test_drift_notes()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
