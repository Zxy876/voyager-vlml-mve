#!/usr/bin/env python3
"""`reset_all.py --scene X` 的测试（隔离临时目录，不动真实状态）。

验四件事：
  1. **只清目标场景**：该场景的目录/条目/图谱节点全没，其它场景原样留着；
  2. **不动数据源与配置**：scene_data/、scenes.json、action_registry.json
     一个都不许碰（那是人的文件和配置，不是学习成果）；
  3. **空场景字段归默认场景 A**（与 `build_state` 同口径）—— 清 A 才动它们；
  4. `--dry-run` 一个字节都不改；场景 id 不存在 → 拒绝（防止打错字）。

为什么单开这个文件：`reset_all.py` 是全库删除器，以前没有按场景清的口子，
也没有任何测试 —— 加这条路径必须先把"误伤别的场景"这条路堵死。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(REPO))

import reset_all as R                       # noqa: E402
from knowledge_graph import KnowledgeGraph, Node, Edge   # noqa: E402


def _build(tmp: Path) -> None:
    """造一份 A / D 两个场景混在一起的状态。"""
    (tmp / "ckpt_curriculum" / "D").mkdir(parents=True)
    (tmp / "ckpt_curriculum" / "D" / "completed_tasks.json").write_text(
        '["阿斯伯格…"]', encoding="utf-8")
    (tmp / "ckpt_curriculum" / "D" / "failed_tasks.json").write_text('[]',
                                                                    encoding="utf-8")
    (tmp / "ckpt_curriculum" / "B").mkdir(parents=True)
    (tmp / "ckpt_curriculum" / "B" / "completed_tasks.json").write_text(
        '["世界模型…"]', encoding="utf-8")
    (tmp / "ckpt_maieutic" / "D").mkdir(parents=True)
    (tmp / "ckpt_maieutic" / "D" / "abcd1234.json").write_text(
        json.dumps({"dimensions": [{"id": "d1", "question": "定义？"}]},
                   ensure_ascii=False), encoding="utf-8")

    def jl(name: str, rows: list[dict]) -> None:
        (tmp / name).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")

    jl("run_log.jsonl", [{"scene": "D", "q": "阿斯伯格 1"},
                         {"scene": "D", "q": "阿斯伯格 2"},
                         {"scene": "A", "q": "VLML 题"}])
    jl("causal_timeline.jsonl", [{"scene": "D", "act": "web_search"},
                                 {"scene": "A", "act": "query_sql"}])
    jl("coach_log.jsonl", [{"root_scene": "D", "q": "导入的问题"},
                           {"root_scene": "A", "q": "VLML 消费"}])
    jl("topic_mastery.jsonl", [{"root_scene": "D", "topic": "consume_a6e"},
                               {"root_scene": "A", "topic": "vlml_grain_x"}])
    # 一条**没有场景字段**的老记录：口径同 build_state，归 A，清 D 不许动它
    jl("exam_log.jsonl", [{"topic": "无场景字段的裸考"}])
    (tmp / "runtime_topics.json").write_text(json.dumps(
        {"t_d": {"root_scene": "D", "label": "D 的运行期节点"},
         "t_a": {"root_scene": "A", "label": "A 的运行期节点"}},
        ensure_ascii=False), encoding="utf-8")
    (tmp / "pending_dimensions.json").write_text(json.dumps(
        {"p1": {"root_scene": "D", "name": "待审维度"}}, ensure_ascii=False),
        encoding="utf-8")
    # 数据源与配置：清任何场景都不许碰
    (tmp / "scene_data" / "D").mkdir(parents=True)
    (tmp / "scene_data" / "D" / "我的资料.txt").write_text("人的文件",
                                                          encoding="utf-8")
    (tmp / "scenes.json").write_text('{"A":{},"D":{}}', encoding="utf-8")
    (tmp / "action_registry.json").write_text('{"master":true}', encoding="utf-8")

    # 图谱：A 两节点 + D 两节点，边含 A-A / A-D / D-D
    g = KnowledgeGraph()
    g.add_node(Node(id="topic:a1", kind="dimension", label="A 维度",
                    root_scene="A"))
    g.add_node(Node(id="table:rounds", kind="table", label="rounds",
                    root_scene="A"))
    g.add_node(Node(id="dim:t:d1", kind="dimension", label="D 维度 1",
                    root_scene="D"))
    g.add_node(Node(id="dim:t:d2", kind="dimension", label="D 维度 2",
                    root_scene="D"))
    g.add_edge(Edge(src="topic:a1", dst="table:rounds", relation="reads"))
    g.add_edge(Edge(src="dim:t:d1", dst="table:rounds", relation="reads"))
    g.add_edge(Edge(src="dim:t:d1", dst="dim:t:d2", relation="co_occurs"))
    g.save(tmp / "knowledge_graph.json")     # save() 会写对指纹，load 才不过期


def test_clear_scene_only_touches_that_scene() -> None:
    tmp = Path(tempfile.mkdtemp())
    _build(tmp)
    old = R.ROOT
    R.ROOT = tmp
    try:
        assert R._clear_scene("D", dry=False, backup=True) == 0
        # 1. 目录：D 的两个都没了，B 的还在
        assert not (tmp / "ckpt_curriculum" / "D").exists()
        assert not (tmp / "ckpt_maieutic" / "D").exists()
        assert (tmp / "ckpt_curriculum" / "B" / "completed_tasks.json").exists()
        # 2. 条目：只剩 A 的
        def scenes_of(name: str, key: str) -> list[str]:
            p = tmp / name
            if not p.exists():
                return []
            return [str(json.loads(l).get(key) or "")
                    for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

        assert scenes_of("run_log.jsonl", "scene") == ["A"], "run_log 应只剩 A"
        assert scenes_of("causal_timeline.jsonl", "scene") == ["A"]
        assert scenes_of("coach_log.jsonl", "root_scene") == ["A"]
        assert scenes_of("topic_mastery.jsonl", "root_scene") == ["A"]
        assert json.loads((tmp / "runtime_topics.json").read_text(
            encoding="utf-8")).keys() == {"t_a"}, "runtime_topics 应只剩 A 那条"
        # 清空即删文件，不留空壳
        assert not (tmp / "pending_dimensions.json").exists()
        # 3. 图谱：D 的节点和挂在它们上的边全没，A 的原样
        raw = json.loads((tmp / "knowledge_graph.json").read_text(encoding="utf-8"))
        ids = {n["id"] for n in raw["nodes"]}
        assert ids == {"topic:a1", "table:rounds"}, ids
        rels = sorted(e["relation"] for e in raw["edges"])
        assert raw["edges"] and all(
            e["from"] in ids and e["to"] in ids for e in raw["edges"]), raw["edges"]
        assert len(raw["edges"]) == 1, rels          # 只剩那条 A-A 边
        # 4. 数据源与配置一个都没动
        assert (tmp / "scene_data" / "D" / "我的资料.txt").read_text(
            encoding="utf-8") == "人的文件"
        assert json.loads((tmp / "scenes.json").read_text(encoding="utf-8")) == \
            {"A": {}, "D": {}}
        assert (tmp / "action_registry.json").exists()
        # 不带场景字段的记录（exam_log）没被动 —— 它不在按场景清的清单里
        assert (tmp / "exam_log.jsonl").exists()
        # 5. 备份留了
        assert any(p.name.startswith("_backup_") and "sceneD" in p.name
                   for p in tmp.iterdir()), sorted(p.name for p in tmp.iterdir())
    finally:
        R.ROOT = old
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS  test_clear_scene_only_touches_that_scene")


def test_dry_run_changes_nothing() -> None:
    tmp = Path(tempfile.mkdtemp())
    _build(tmp)
    old = R.ROOT
    R.ROOT = tmp
    try:
        before = {p.name: p.read_bytes() for p in tmp.rglob("*") if p.is_file()}
        assert R._clear_scene("D", dry=True, backup=True) == 0
        after = {p.name: p.read_bytes() for p in tmp.rglob("*") if p.is_file()}
        assert before == after, "dry-run 竟然改了东西"
    finally:
        R.ROOT = old
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS  test_dry_run_changes_nothing")


def test_unknown_scene_is_refused() -> None:
    tmp = Path(tempfile.mkdtemp())
    _build(tmp)
    old_root, old_ids, old_argv = R.ROOT, R._scene_ids, sys.argv
    R.ROOT = tmp
    R._scene_ids = lambda: ["A", "B", "D"]
    sys.argv = ["reset_all.py", "--scene", "ZZ"]
    try:
        assert R.main() == 1, "不存在的场景必须拒绝（防打错字）"
        assert (tmp / "ckpt_curriculum" / "D").exists(), "拒绝时不能顺手清掉东西"
    finally:
        R.ROOT = old_root
        R._scene_ids = old_ids
        sys.argv = old_argv
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS  test_unknown_scene_is_refused")


def test_missing_scene_field_belongs_to_default() -> None:
    """旧记录没有场景字段 → 归 A（与 build_state 同口径）：清 D 不动、清 A 才动。"""
    assert R._scene_matches({}, "scene", "A") is True
    assert R._scene_matches({}, "scene", "D") is False
    assert R._scene_matches({"scene": "D"}, "scene", "D") is True
    assert R._scene_matches({"scene": "A"}, "scene", "D") is False
    print("PASS  test_missing_scene_field_belongs_to_default")


def main() -> None:
    test_missing_scene_field_belongs_to_default()
    test_clear_scene_only_touches_that_scene()
    test_dry_run_changes_nothing()
    test_unknown_scene_is_refused()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
