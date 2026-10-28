#!/usr/bin/env python3
"""作用域判定器（2026-10-28 路线图第 2.5 步，用户拍板）。

回答的问题：「这句话归哪条供给线」——不是「会不会答」，是**该走哪条线答**。

口径（作用域两分，别用一个定义另一个）
----------
- **作用域跟消费架构走，验证能力跟环境走**（两个独立维度）。
- 分型权威 = 环境（`scene_profile.kind`），不是题面词面 ——
  「知识型/数据型」词面分类已于 2026-10-28 拍板砍掉（task_critic.py
  docstring 自认「原版没有这一档」）。**指认不了环境 = 不可答**
  （curriculum_auto.py 环境指认口径）。

三条供给线
----------
- **learn（学习线 / 技能线）**：环境可指认（kind ∈ vlml/scene/local）且
  答案**可复现** —— 重放同库同配方得同数。学一次、反复兑现；可验证、
  可入库（r 择优）、计入学习曲线。
- **tool（工具线）**：答案**时变**（依赖库外状态：联网/实时数据）。
  不需学、不经学习侧；答案标 claim（有真值但未知），**不入技能库、
  不计入学习曲线**（工具功劳不能算到学习头上 —— 2026-10-28 拍板）。
  ⚠️ 当前 MVE 无联网能力 → 工具线尚无供给，判定结果如实报
  unanswerable（第 4 步 DeepSeek 消费同构接入联网后激活）。
- **inference**：无环境可指认 → 模型推的、无来源。r 上限 0.1
  （reward.INFERENCE_CAP，ABC 落地）、消费侧标「未验证」、不入库。

时变怎么判（不回词面分类）
----------
不看「今天/最新/现在」这类词 —— 那是刚被砍掉的词面分类换个马甲。
权威判据是**重放**：学习线的一切答案必须能重放复现（同库同配方同数）。
依赖库外状态的问题，环境指认阶段就指认不了数据来源 → 落不进学习线；
接入工具线后由工具当场取、用完即弃（claim，不沉淀）。
"""

from __future__ import annotations

from typing import Any

__all__ = ["classify", "scope_line"]


def classify(scene_id: str) -> dict[str, Any]:
    """判定一个场景/问题域的供给线。

    返回 {"line": "learn"|"tool"|"inference", "has_env": bool,
          "kind": str, "why": str}
    """
    sid = str(scene_id or "").strip() or "A"
    try:
        import reward
        has_env = bool(reward.scene_has_env(sid))
    except Exception as e:  # pragma: no cover
        return {"line": "inference", "has_env": False, "kind": "missing",
                "why": f"环境判定失败（{type(e).__name__}）—— 如实降级 inference"}
    kind = ""
    try:
        from scene_profile import profile
        kind = str((profile(sid) or {}).get("kind") or "")
    except Exception:
        pass
    if not has_env:
        return {"line": "inference", "has_env": False, "kind": kind,
                "why": "环境指认不了（无可指认数据源/工具）—— 产物是 inference，"
                       "标未验证、不入库、不计入学习曲线"}
    # 有环境 → 学习线（可复现、可重放验证）。时变问题在环境指认阶段就
    # 拿不到数据来源，落不进这里；工具线待第 4 步联网接入后在此分叉。
    return {"line": "learn", "has_env": True, "kind": kind,
            "why": f"环境可指认（kind={kind or 'local'}）且答案可重放复现 —— "
                   "学习线：学一次、反复兑现"}


def scope_line(scene_id: str) -> str:
    """便捷取线名（learn / tool / inference）。"""
    return str(classify(scene_id).get("line") or "inference")
