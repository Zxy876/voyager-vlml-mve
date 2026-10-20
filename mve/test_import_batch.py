#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「一次导入 = 一个父节点」的归属测试（用户 2026-10-20）。

原话：「每次人导入-运行学习一批内容，那么这批知识图谱上长出来的内容就是那次
人导入的子节点呀，技能的归属也是如此；人话一点就是我希望我第二次导入相同问题
的时候能够用上之前学的东西」——「这样两次导入掌握度上升会直观，且能被解释」。

钉死三件事：
  1. 导入记录有稳定的批次 id（老记录也能按 ts 现算出来，算法幂等）；
  2. 同一个问题导入多次 → 能查到"第几次"、上一批是哪批（严格同句 + 同场景）；
  3. 图谱里这批维度节点挂在 `import:<id>` 下面（换一批导入 → 另一条边），
     所以"这批是哪次导入长的"看得见，第二次导入的复用也看得见。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# coach.py 顶层 `import vlml_env` 会 os.chdir 到 vlml/ 子目录（本 worktree 没检出
# 那个目录）。这里只测导入记账，不需要真环境 → 用空模块顶上，别去碰磁盘。
import types  # noqa: E402


class _StubEnv(types.ModuleType):
    """任意属性都返回一个空函数 —— 只为让 coach/voyager 的顶层 import 过得去。"""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None


sys.modules.setdefault("vlml_env", _StubEnv("vlml_env"))
import coach  # noqa: E402
import maieutic_scaffold as M  # noqa: E402


def test_import_id_is_stable_for_old_records():
    """老记录没有 import_id 字段 → 按 ts 现算，且算法与写入时一致。"""
    rec = {"ts": "2026-10-20T09:30:12", "question": "什么是阿斯伯格"}
    iid = coach.import_id_of(rec)
    assert iid == "imp_20261020T093012", iid
    assert coach.import_id_of(rec) == iid, "同一记录必须稳定"
    # 新记录直接用字段，不再重算
    rec2 = {"ts": "2026-10-20T09:30:12", "import_id": "imp_manual"}
    assert coach.import_id_of(rec2) == "imp_manual"
    print("PASS  test_import_id_is_stable_for_old_records")


def test_import_history_counts_same_question_once_per_batch():
    """同句同场景才算同一次问题的历史；别的句子/别的场景不能混进来。"""
    tmp = Path(tempfile.mkdtemp())
    old = coach.COACH_LOG
    try:
        log = tmp / "coach_log.jsonl"
        rows = [
            {"ts": "2026-10-20T09:00:00", "question": "什么是阿斯伯格", "root_scene": "D"},
            {"ts": "2026-10-20T09:10:00", "question": "别的问句", "root_scene": "D"},
            {"ts": "2026-10-20T09:20:00", "question": "什么是阿斯伯格", "root_scene": "A"},
            {"ts": "2026-10-20T09:30:00", "question": "什么是阿斯伯格", "root_scene": "D"},
        ]
        log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                       encoding="utf-8")
        coach.COACH_LOG = log
        h = coach.import_history("什么是阿斯伯格", "D")
        assert h["count"] == 2, h              # A 场景那条不算
        assert h["import_id"] == "imp_20261020T093000", h   # 最近一批
        assert h["first_id"] == "imp_20261020T090000", h
        # 第一次导入（还没学过）→ count=0，不编批次
        h0 = coach.import_history("从没导入过的问题", "D")
        assert h0["count"] == 0 and h0["import_id"] == "", h0
    finally:
        coach.COACH_LOG = old
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS  test_import_history_counts_same_question_once_per_batch")


def test_dimensions_hang_under_import_node():
    """这批维度节点是那次导入的子节点 —— 换一批导入是另一条边，节点不重复。"""
    from knowledge_graph import KnowledgeGraph

    g = KnowledgeGraph()
    g.ensure_scene_root("T")
    g.add_cold_start_node("T", "consume:consume_abc", "dimension",
                          label="什么是阿斯伯格")
    dim = {"id": "def", "question": "定义是什么？", "why": "先有定义"}

    M.attach_dimensions("T", "consume_abc", [dim], verified=True, graph=g,
                        import_id="imp_20261020T090000",
                        import_label="什么是阿斯伯格")
    imp1 = "import:imp_20261020T090000"
    assert imp1 in g.nodes, list(g.nodes)[:8]
    assert g.nodes[imp1].kind == "import"
    assert g.nodes[imp1].label == "什么是阿斯伯格"
    dim_id = "dim:consume_abc:def"
    e1 = [(e.src, e.dst) for e in g.edges
          if e.relation == "has_dimension" and e.dst == dim_id]
    assert (imp1, dim_id) in e1, e1
    # topic 那层还在（两层并存：这题的知识 / 哪次导入学的）
    assert ("consume:consume_abc", dim_id) in e1, e1

    # 第二次导入（同一维度已存在）：照样挂到**新的**导入节点下，不重复建节点
    n_nodes = len(g.nodes)
    M.attach_dimensions("T", "consume_abc", [dim], verified=True, graph=g,
                        import_id="imp_20261020T100000",
                        import_label="什么是阿斯伯格")
    imp2 = "import:imp_20261020T100000"
    assert imp2 in g.nodes
    e2 = [(e.src, e.dst) for e in g.edges
          if e.relation == "has_dimension" and e.dst == dim_id]
    assert (imp2, dim_id) in e2, e2          # 复用也要挂在这次导入下
    assert len(g.nodes) == n_nodes + 1, "只多一个 import 节点，维度不复制"
    # 没给 import_id → 不造 import 节点（老路径不破坏）
    g2 = KnowledgeGraph()
    g2.ensure_scene_root("T")
    g2.add_cold_start_node("T", "consume:x", "dimension", label="q")
    M.attach_dimensions("T", "consume:x", [{"id": "d", "question": "q？"}],
                        verified=True, graph=g2)
    assert not [n for n in g2.nodes.values() if n.kind == "import"], \
        "没给批次就别造导入节点"
    print("PASS  test_dimensions_hang_under_import_node")


def test_backlinks_and_imports_of():
    """Obsidian 式反链：归属是**图上算出来的**，不另存一张表。

    第二次导入同一问题时 `backlinks / imports_of` 会数出两个 import —— 这就是
    "掌握度为什么涨"的解释（复用第 1 次导入学到的维度）。
    """
    from knowledge_graph import KnowledgeGraph

    g = KnowledgeGraph()
    g.ensure_scene_root("T")
    g.add_cold_start_node("T", "consume:consume_abc", "dimension", label="q")
    dim = {"id": "def", "question": "定义是什么？"}
    M.attach_dimensions("T", "consume_abc", [dim], verified=True, graph=g,
                        import_id="imp_A", import_label="q")
    M.attach_dimensions("T", "consume_abc", [dim], verified=True, graph=g,
                        import_id="imp_B", import_label="q")
    did = "dim:consume_abc:def"
    assert len(g.backlinks(did, "has_dimension")) == 3, \
        g.backlinks(did, "has_dimension")     # topic 层 1 + 两次导入各 1
    assert set(g.sources_of(did, "has_dimension")) == {
        "consume:consume_abc", "import:imp_A", "import:imp_B"}
    imps = M.imports_of("T", "consume_abc", "def", graph=g)
    assert [i["import_id"] for i in imps] == ["imp_A", "imp_B"], imps
    assert all(i["label"] == "q" for i in imps), imps
    # 局部图谱：以维度为中心 1 跳就能看到两次导入 + topic
    lg = g.local_graph(did, depth=1)
    ids = {n["id"] for n in lg["nodes"]}
    assert {"import:imp_A", "import:imp_B", "consume:consume_abc"} <= ids, ids
    assert lg["edges"], "局部图得带边"
    # 没有节点的 id → 空图，不炸
    assert g.local_graph("nope") == {"nodes": [], "edges": []}
    print("PASS  test_backlinks_and_imports_of")


def test_origin_upgrade_keeps_backlink_index_honest():
    """同一 key 的边从 declared 升级为 observed 时，反链索引必须跟着换。

    否则 `backlinks()` 返回的是被丢掉的那个旧对象 —— 反链会报告 origin=
    "declared" 的陈旧证据（Obsidian 式反链全靠这个索引，错一份就等于说假话）。
    """
    from knowledge_graph import Edge, KnowledgeGraph, Node

    g = KnowledgeGraph()
    for nid in ("a", "b"):
        g.add_node(Node(id=nid, kind="dimension", label=nid))
    g.add_edge(Edge(src="a", dst="b", relation="produced_by", origin="declared"))
    g.add_edge(Edge(src="a", dst="b", relation="produced_by", origin="observed"))
    back = g.backlinks("b", "produced_by")
    assert len(back) == 1, back
    assert back[0].origin == "observed", back[0]
    assert g.outlinks("a", "produced_by")[0].origin == "observed"
    print("PASS  test_origin_upgrade_keeps_backlink_index_honest")


if __name__ == "__main__":
    test_import_id_is_stable_for_old_records()
    test_import_history_counts_same_question_once_per_batch()
    test_dimensions_hang_under_import_node()
    test_backlinks_and_imports_of()
    test_origin_upgrade_keeps_backlink_index_honest()
    print("\nALL PASS")
