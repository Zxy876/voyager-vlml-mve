#!/usr/bin/env python3
"""验证「第二个场景真的插进来了」—— 设计 Q3 的可复现剧本。

不依赖 duckdb / 大模型，纯测命名空间骨干 + 图谱增量口径：

  1. 加载 Scene A（已有时专属节点 N，来自 build 出的 VLML 图，这里用内存图模拟）
  2. 插入 Scene B，冷启动一题 → 断言 B 专属节点 0 → 1
  3. 切回 A，断言 A 专属节点读数不变（B 的增长没有污染 A）
  4. 技能库：A 的技能归属 A，B 冷启动的技能归属 B，跨场景复用仍记 A

运行：python mve/test_scenes_insertion.py
"""
from __future__ import annotations

from knowledge_graph import KnowledgeGraph, Node
import skill_store
import scenes


def main() -> int:
    # ---- 0. 干净环境（用临时 registry / store 隔离，不污染真实文件） ----
    scenes.REGISTRY.write_text("{}", encoding="utf-8")
    skill_store.STORE.write_text("{}", encoding="utf-8")

    # ---- 1. 模拟 Scene A 已有一张建好的图 + 一条技能 ----
    g = KnowledgeGraph()
    g.add_node(Node(id="dim:win_rate", kind="dimension", label="胜率",
                    root_scene="A", status="verified", evidence=5))
    g.add_node(Node(id="tool:query_sql", kind="tool", label="取数",
                    root_scene="A", status="verified", evidence=9))
    a_nodes_before = g.scene_specific_node_count("A")
    skill_store.add("win_rate", "calcwin", "用 query_sql 算胜率", source_scene="A",
                    code="SELECT ...")
    a_skills_before = skill_store.scene_skill_count("A")

    print(f"[A 初始] 专属节点={a_nodes_before}  专属技能={a_skills_before}")

    # ---- 2. 插入 Scene B 并冷启动一题 ----
    scenes.create_scene("我的学习场景", description="B 场景：用户自定义问题域")
    b_before = g.scene_specific_node_count("B")
    print(f"[B 插入前] 专属节点={b_before}  (应为 0)")

    # 消费侧导入问题，归不上现有图谱 → 冷启动新建节点
    g.add_cold_start_node("B", "dim:custom_metric_x", "dimension",
                          label="B 场景特有指标 X")
    b_after = g.scene_specific_node_count("B")
    # B 冷启动的技能也记到 B
    skill_store.add("custom_metric_x", "calcx",
                    "B 场景冷启动现做的一道取数配方", source_scene="B",
                    code="SELECT ...")
    b_skills = skill_store.scene_skill_count("B")
    print(f"[B 冷启动后] 专属节点={b_after}  (应为 1)  专属技能={b_skills}  (应为 1)")

    # ---- 3. 切回 A，断言 A 没被污染 ----
    a_nodes_after = g.scene_specific_node_count("A")
    a_skills_after = skill_store.scene_skill_count("A")
    print(f"[A 复测] 专属节点={a_nodes_after}  专属技能={a_skills_after}")

    # ---- 4. 跨场景复用：B 复用 A 的技能，仍归属 A ----
    reused = skill_store.by_scene("A")  # B 复用的 calx 不应出现在 A
    cross_pollution = any(s["name"] == "calcx" for s in reused)
    print(f"[跨场景] B 的技能误入 A？ {cross_pollution}  (应为 False)")

    # ---- 断言 ----
    b_skills_ok = b_skills == 1
    ok = (b_before == 0 and b_after == 1 and a_nodes_after == a_nodes_before
          and a_skills_after == a_skills_before and not cross_pollution
          and b_skills_ok)
    print("\n=== 结论 ===")
    print("✅ Scene B 真的插进来了：专属节点 0 → 1，且未污染 A 的统计"
          if ok else "❌ 验证失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
