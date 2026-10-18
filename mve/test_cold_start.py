#!/usr/bin/env python3
"""冷启动登记单元测试（不依赖 LLM / 数据库）。

验证设计 B 冷启动期 + Q3 Δ 场景专属节点数 + Q2 节点转化机制：
  · 新消费项在 Scene B 建一枚 root_scene=B / status=cold_start_guess / evidence=1 的节点
  · 同一项再次命中 → 升级为 verified（证据数 2）
  · 归上现有题的（非 consume_ 前缀）不新建、不算冷启动
  · Scene A 的专属节点数不被 B 污染
  · 技能打上 source_scene=B
运行：python mve/test_cold_start.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from knowledge_graph import KnowledgeGraph, Node
import cold_start
import skill_store
import scenes


def _fresh_graph() -> KnowledgeGraph:
    g = KnowledgeGraph()
    g.add_node(Node(id="dim:win_rate", kind="dimension", label="胜率",
                    root_scene="A", status="verified", evidence=5))
    return g


def test_cold_start_new_and_promote() -> None:
    g = _fresh_graph()
    # 1) 第一次冷启动新建
    info = cold_start.ensure_cold_start_node("consume_abc123", "B 场景的特有指标", "B", graph=g)
    assert info["cold_start"] is True, info
    n = g.nodes["consume:consume_abc123"]
    assert n.root_scene == "B", n.root_scene
    assert n.status == "cold_start_guess", n.status
    assert n.evidence == 1
    # 2) A 不受影响
    assert g.scene_specific_node_count("A") == 1
    assert g.scene_specific_node_count("B") == 1
    # 3) 再次命中 → 升级 verified
    info2 = cold_start.ensure_cold_start_node("consume_abc123", "B 场景的特有指标", "B", graph=g)
    assert info2["cold_start"] is False, info2
    assert g.nodes["consume:consume_abc123"].status == "verified"
    assert g.nodes["consume:consume_abc123"].evidence == 2


def test_existing_topic_not_cold_start() -> None:
    g = _fresh_graph()
    info = cold_start.ensure_cold_start_node("vlml_first_blood", "已存在题", "A", graph=g)
    assert info["cold_start"] is False
    assert info["status"] == "existing"


def test_skill_scene_tag() -> None:
    # 用临时技能库，避免污染本地 skill_store.json
    tmp = Path(tempfile.mkdtemp()) / "skill_store.json"
    skill_store.STORE = tmp
    skill_store.add("consume_xyz", "consume_xyz",
                    "消费时答出来的取数方法：B 场景问题足够长以入库", code="SELECT 1")
    n = cold_start.mark_skill_scene("consume_xyz", "B")
    assert n == 1, n
    s = [s for s in skill_store.all_skills() if s["key"].startswith("consume_xyz::")][0]
    assert s["source_scene"] == "B", s["source_scene"]


def main() -> int:
    test_cold_start_new_and_promote()
    test_existing_topic_not_cold_start()
    test_skill_scene_tag()
    print("✅ 冷启动登记：新建/升级/非冷启动/技能场景归属 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
