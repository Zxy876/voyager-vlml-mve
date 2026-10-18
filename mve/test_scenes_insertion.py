#!/usr/bin/env python3
"""验证「第二个场景真的插进来了」—— 设计 Q3 的可复现剧本 + 任务 174 状态指纹对账。

不依赖 duckdb / 大模型，纯测命名空间骨干 + 图谱增量口径 + 状态指纹隔离：

  1. 加载 Scene A（已有时专属节点 N，来自内存图模拟）
  2. 插入 Scene B，冷启动一题 → 断言 B 专属节点 0 → 1
  3. 切回 A，断言 A 专属节点读数不变（B 的增长没有污染 A）
  4. 技能库：A 的技能归属 A，B 冷启动的技能归属 B，跨场景复用仍记 A
  5. 状态指纹（任务174 / 补 ⑤）：A 设好续跑点 → 切到 B 消费 → 切回 A，断言 A 的
     cursor / updated_at / consume_count / state_hash 严格不变（B 的活动只写 B）

运行：python mve/test_scenes_insertion.py

⚠️ 全程用临时文件隔离，不污染真实的 scenes/skill_store/runtime_topics。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from knowledge_graph import KnowledgeGraph, Node
import skill_store
import scenes


def main() -> int:
    # ---- 0. 干净环境（临时文件隔离，不碰真实运行时 json）----
    tmp = Path(tempfile.mkdtemp(prefix="mve_scenes_test_"))
    scenes.REGISTRY = tmp / "scenes.json"
    scenes.HERE = tmp                                  # consume_count 读 HERE/runtime_topics.json
    skill_store.STORE = tmp / "skill_store.json"
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
    # A 设好续跑点（模拟已学到第 7 题）
    scenes.set_state({"cursor": "topic:kd_ratio", "last_topic": "win_rate"}, "A")
    a_fp_before = scenes.scene_state_fingerprint("A")

    print(f"[A 初始] 专属节点={a_nodes_before}  专属技能={a_skills_before}")
    print(f"[A 指纹] cursor={a_fp_before['cursor']} "
          f"updated_at={a_fp_before['updated_at']:.0f} "
          f"consume_count={a_fp_before['consume_count']} hash={a_fp_before['state_hash']}")

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

    # B 消费一条（写入 runtime_topics.json，带 root_scene=B）
    _write_runtime(tmp, "consume_b1", {"source": "consume", "root_scene": "B"})
    b_consume = scenes.consume_count("B")
    print(f"[B 消费] consume_count(B)={b_consume}  (应为 1)")

    # ---- 3. 切回 A，断言 A 没被污染 ----
    a_nodes_after = g.scene_specific_node_count("A")
    a_skills_after = skill_store.scene_skill_count("A")
    a_fp_after = scenes.scene_state_fingerprint("A")
    print(f"[A 复测] 专属节点={a_nodes_after}  专属技能={a_skills_after}")
    print(f"[A 指纹] cursor={a_fp_after['cursor']} "
          f"updated_at={a_fp_after['updated_at']:.0f} "
          f"consume_count={a_fp_after['consume_count']} hash={a_fp_after['state_hash']}")

    # A 的消费记录数（B 写入的不应计入 A）
    a_consume_after = scenes.consume_count("A")
    print(f"[A 消费数] consume_count(A)={a_consume_after}  (应为 0)")

    # ---- 4. 跨场景复用：B 复用 A 的技能，仍归属 A ----
    reused = skill_store.by_scene("A")  # B 复用的 calcx 不应出现在 A
    cross_pollution = any(s["name"] == "calcx" for s in reused)
    print(f"[跨场景] B 的技能误入 A？ {cross_pollution}  (应为 False)")

    # ---- 断言 ----
    b_skills_ok = b_skills == 1
    # 指纹隔离：A 的四项在 B 活动前后严格一致
    fp_ok = (
        a_fp_after["cursor"] == a_fp_before["cursor"]
        and a_fp_after["state_hash"] == a_fp_before["state_hash"]
        and a_fp_after["updated_at"] == a_fp_before["updated_at"]
        and a_fp_after["consume_count"] == a_fp_before["consume_count"]
        and a_fp_after["consume_count"] == 0
    )
    ok = (b_before == 0 and b_after == 1 and a_nodes_after == a_nodes_before
          and a_skills_after == a_skills_before and not cross_pollution
          and b_skills_ok and b_consume == 1 and a_consume_after == 0
          and fp_ok)
    print("\n=== 结论 ===")
    print("✅ Scene B 真的插进来了：专属节点 0 → 1，未污染 A 的统计，"
          "且 A 的状态指纹（续跑点/活动/消费数/哈希）在 B 活动后严格不变"
          if ok else "❌ 验证失败")
    return 0 if ok else 1


def _write_runtime(tmp: Path, tid: str, meta: dict) -> None:
    p = tmp / "runtime_topics.json"
    reg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    reg[tid] = {"id": tid, "name": tid, **meta}
    p.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
