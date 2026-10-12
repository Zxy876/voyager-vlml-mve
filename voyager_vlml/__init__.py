"""voyager_vlml —— 原版 Voyager 学习循环的 VLML 同构版。

用户指令（2026-10-12）：当前 mve/ 后端问题太大（反馈骨架建在「比对 VLML 标准
答案」上，违反设计），改为**从原版 Voyager 重建**，按五步同构：

  ① MC 初始化携带的信息 → 我们的 VLML 同构版里程碑        （本文件同级的 agents/curriculum.py）
  ② control_primitives 的 JS 代码 → SQL 原语               （control_primitives/）
  ③ 在 voyager 环境里安装 VLML MCP 依赖（DB / RIB / VLGG）   （env/）
  ④ 迭代修正机制 prompt 里的语言 → SQL                      （prompts/ + agents/critic.py）
  ⑤ 面板后置（本轮只验证后端闭环）

同构对照表（原版 MC 字段 → VLML）：
  biome             → 数据源（rib / vlgg / DB）
  nearby_blocks     → 可见表（按进度档解锁）
  other_blocks      → 其它见过的表
  nearby_entities   → 实体可填值（库里真实取值范围）
  inventory         → 当前实体锚点（series / team / map）
  chests            → 已学会技能（可复用 SQL 配方）
  completed_tasks   → 已解锁里程碑（图谱节点已完成）
  failed_tasks      → 失败 / 跳过（带原因）
  "Mine 1 wood log" → VLML 种子里程碑（树干起点、无前置）
  —— 新增：里程碑树（seed / trunk / keystone）+ 随机 (表,列) 最小单位 ——
"""

__all__ = ["agents"]
