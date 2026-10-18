#!/usr/bin/env python3
"""MVE 1→n 泛化：冷启动期登记（设计 B 冷启动期 + Q2 节点视觉区分）。

设计定位
--------
冷启动期是「第二个场景真的插进来了」的**发生机制**：用户在 Scene B 的消费侧
导入一个问题，系统判定它**归不上现有图谱**（相关性阈值以下）→ 新建一个待验证
节点 → Voyager 现场解题 → 答出来就：
  1. 在知识图谱上建一个 `root_scene=B`、`status=cold_start_guess`、`evidence=1`
     的节点（这就是 Δ 场景专属节点数 +1 的来源，见 knowledge_graph）；
  2. 把这次的取数 SQL 存成技能，并打 `source_scene=B`（见 skill_store）；
  3. 按 `consume_<md5>` 登记进 runtime_topics.json（分母 +1，见 consume._absorb）。

区别于「稳固节点」的是**可信度不是计数**（设计 B 修正）：冷启动成功的题立即计入
掌握度，但在 UI 上标为「单次证据支撑」，再次被消费线命中成功才升级为 verified
（设计 Q2 的转化机制 → promote_node）。

本模块只负责第 1 点的**图谱侧登记封装**，并给出 `cold_start` 标记给消费线与 UI。
第 2、3 点由 consume._absorb 负责（它已存在，本模块被它调用）。

为什么独立成模块：让冷启动登记可以被**不依赖大模型/数据库**的单元测试覆盖，
而不是只能靠真跑 LLM 验证（用户要求实测取证）。
"""

from __future__ import annotations

from typing import Any


def ensure_cold_start_node(
    tid: str,
    question: str,
    scene_id: str | None = None,
    *,
    graph: Any = None,
) -> dict[str, Any]:
    """确保某条消费侧答出来的能力项在知识图谱上有一枚带场景归属的节点。

    参数
    ----
    tid        —— 能力项 id。以 `consume_` 开头的是冷启动新建项；归上已有题的
                 （来自 tasks.py）节点本就存在，不在此新建，也不算冷启动。
    question   —— 原始问句（取前 60 字当节点 label）。
    scene_id   —— 当前场景，缺省取 scenes.current_scene_id()。
    graph      —— 注入的 KnowledgeGraph（测试用）；缺省走 KnowledgeGraph.load()。

    返回
    ----
    {"cold_start": bool, "node_id": str, "root_scene": str, "status": str}
      · cold_start=True  → 这是一枚**新建**的冷启动猜测节点（evidence=1）。
      · cold_start=False → 节点已存在：若此前是 cold_start_guess，则本次成功
        命中触发 promote（evidence++，达阈值升 verified）；返回其当前 status。
    """
    # 延迟导入，避免 consume.py ↔ 本模块顶层的循环依赖，也避免重型依赖在导入期触发
    if scene_id is None:
        from scenes import current_scene_id
        scene_id = current_scene_id()
    from knowledge_graph import KnowledgeGraph

    if not str(tid).startswith("consume_"):
        # 归上现有题的：节点由 build() 生成，不属于冷启动新建
        return {"cold_start": False, "node_id": f"topic:{tid}",
                "root_scene": scene_id, "status": "existing"}

    node_id = f"consume:{tid}"
    g = graph if graph is not None else KnowledgeGraph.load()
    existing = g.nodes.get(node_id)
    if existing is None:
        g.add_cold_start_node(scene_id, node_id, "dimension",
                              label=str(question or tid)[:60])
        out = {"cold_start": True, "node_id": node_id,
               "root_scene": scene_id, "status": "cold_start_guess"}
    else:
        # 再次命中：仅当仍是冷启动猜测时才升级证据（设计 Q2 转化机制），
        # 已是 verified 的不重复累加 evidence。
        if existing.status == "cold_start_guess":
            g.promote_node(node_id)
        out = {"cold_start": False, "node_id": node_id,
               "root_scene": existing.root_scene,
               "status": g.nodes[node_id].status}
    if graph is None:
        g.save()
    return out


def mark_skill_scene(tid: str, scene_id: str) -> int:
    """把某能力项名下的技能打上 source_scene（设计：技能全局共享但记归属）。

    返回被打标的技能条数。供 _absorb 在存完配方后补标场景。旧条目若没有
    source_scene 字段（预存在技能库），补成当前场景；已有归属的不覆盖。
    """
    import skill_store
    data = skill_store._load_raw()
    n = 0
    for k, s in data.get("skills", {}).items():
        if not str(k).startswith(f"{tid}::"):
            continue
        if str(s.get("source_scene", "default")) in ("default",):
            s["source_scene"] = str(scene_id or "default")
            n += 1
    if n:
        skill_store._save(data)
    return n
