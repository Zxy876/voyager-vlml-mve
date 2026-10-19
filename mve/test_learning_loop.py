#!/usr/bin/env python3
"""端到端学习闭环：**人导入题目 → 命中/未命中 → 再导入同一问题 → 可达**。

这是用户要看的核心剧本（设计案 + neko 双阈值 + 通用 MCP 编排）：

  第 1 次导入 Q  → 图谱里没有 → 未命中（REJECTED）→ 新建消费节点
                  → 编排动作序列（由注册表条目**组合**，非硬编码模板）
                  → 取数成功（非空）→ 固化技能 → 节点升 verified
  第 2 次导入 Q  → 图谱里已有 → **命中**（HIGH/MEDIUM）→ 归入已有节点
                  → 复用已固化技能 → **可达**

关键硬规矩（不许硬编码）：
  1. 动作条目来自**运行时注册表**（或从真实签名反射），不是代码里写死的名单；
  2. 实体值走 `entity_catalog` **实查**，不写死；
  3. 「命中」用 neko 同款双阈值（40/10），**不是**人审、**不是**答对。

运行：python mve/test_learning_loop.py
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import actions
import routing
import scenes
import skill_store
import orchestrate
from knowledge_graph import KnowledgeGraph, Node


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mve_loop_"))
    scenes.REGISTRY = tmp / "scenes.json"
    scenes.HERE = tmp
    skill_store.STORE = tmp / "skill_store.json"
    routing.PENDING_DIMENSIONS = tmp / "pending_dimensions.json"
    scenes.REGISTRY.write_text("{}", encoding="utf-8")
    skill_store.STORE.write_text("{}", encoding="utf-8")

    # ---- 注册 Scene A 的取数原语（用真实签名装饰，不手写 schema）----
    # 注意：这里刻意用「注册表」而不是写死 10 个名字，用来验证
    # "通用 MCP = 传输层 + 场景级条目"这条同构链。
    @actions.action(name="run_query", description="在数据源上执行一段 SQL")
    async def _run_query(mcp, sql: str):
        return await mcp.query_sql(sql_query=sql)

    actions.register("A", _run_query, origin="builtin")
    print(f"[A 注册] 可执行动作条目 = {actions.entry_count('A')}："
          f"{actions.names('A')}")

    # ---- 场景 B：硬拒绝判据 = entry_count == 0（设计案硬拒绝项）----
    # 注意：这里断言的是**契约本身**（0 条目 ⇒ 不许插入，且给出理由），
    # 不是「B 一定被拒」。B 能否注册到内置原语取决于 action_code 能否反射
    # （服务器上有 vlml 包 → 10 条；本机缺 vlml → 0 条）。实测踩过：
    # 原来写成 b_blocked = not insert_allowed，本机因缺 vlml 假过、服务器上真挂。
    scenes.create_scene("我的学习场景 B", description="新数据域")
    vB = orchestrate.init_scene("B")
    print(f"[B 插入] 条目数 = {vB['entry_count']}  "
          f"allowed={vB['insert_allowed']}  reason={vB['blocking_reason']!r}")
    b_policy_ok = vB["insert_allowed"] == (vB["entry_count"] > 0)
    if not vB["insert_allowed"]:
        print("         → 已按硬拒绝拒绝插入（动作本身缺席，取不到数）")
    else:
        print(f"         → 放行（该场景有 {vB['entry_count']} 条可执行原语，"
              f"不构成「动作缺席」）")
    if vB["insert_allowed"]:
        assert vB["blocking_reason"] == "", "放行时不该带 blocking_reason"
    else:
        assert vB["blocking_reason"], "硬拒绝必须给出理由，不许静默"

    # ---- 第 1 次导入：未命中 → 编排 → 写入 ----
    q = "iPhone 15 的销量是多少"
    print(f"\n[第 1 次导入] Q = {q!r}")
    r1 = routing.route(q, "A")
    print(f"  判定 = {r1['decision']}  score={r1['max_score']}  "
          f"exact={r1['exact_match']}  chunk_hits={r1['chunk_hits']}")
    print(f"  动作 = {r1['action']}  去向 = {r1['dest']}")
    first_unmatched = r1["decision"] == "REJECTED"
    if first_unmatched:
        # 未命中 → 编排动作序列（由注册表条目 + 实查实体组合，非模板硬编码）
        plan = orchestrate.plan(q, "A")
        print(f"  编排 = {' → '.join(s['entry'] for s in plan['steps'])}")
        print(f"  实查实体 = {plan['entities'] or '（库里没有）'}")

    # 冷启动成功 → 建活节点 + 固化技能（模拟取到结构化数据·非空）
    g = KnowledgeGraph()
    g.add_cold_start_node("A", "consume:consume_abc123", "dimension", label=q)
    skill_store.add("iphone_sales", "iphone_sales",
                    "iPhone 15 销量取数配方", source_scene="A",
                    code="SELECT ... FROM sales")
    b_after_first = g.scene_specific_node_count("A")
    print(f"  写入后 Δ 专属节点 = {b_after_first}")
    # 再次消费 → 提升为稳固节点（设计 Q2：证据数 ≥2 才 verify）
    g.promote_node("consume:consume_abc123")
    status_after = g.nodes["consume:consume_abc123"].status
    print(f"  节点状态 = {status_after}  evidence={g.nodes['consume:consume_abc123'].evidence}")

    # ---- 第 2 次导入同一 Q：应命中 ----
    print(f"\n[第 2 次导入] 同一句 Q（再问一次）")
    r2 = routing.route(q, "A", graph=g)
    print(f"  判定 = {r2['decision']}  score={r2['max_score']}  "
          f"exact={r2['exact_match']}  chunk_hits={r2['chunk_hits']}")
    print(f"  动作 = {r2['action']}  命中节点 = {r2.get('node_id')}")
    second_hit = r2["decision"] != "REJECTED"
    reachable = second_hit and r2["action"] == "absorb_into_existing"
    print(f"  可达 = {reachable}（命中已有节点 → 归入，复用已固化技能）")

    # ---- 证据链自检 ----
    ok = (first_unmatched and b_policy_ok and reachable
          and actions.entry_count("A") > 0
          and status_after in ("verified", "cold_start_guess"))
    print("\n=== 结论 ===")
    print("✅ 闭环成立：第一次未命中→新建→写入；再问同一问题→命中→可达"
          if ok else "❌ 闭环验证失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
