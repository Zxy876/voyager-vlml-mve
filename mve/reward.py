#!/usr/bin/env python3
"""标量 reward（2026-10-28 路线图第 2 步 A：补 r）。

定位（别误解）
----------
原版 voyager.py:296 的 reward 恒 0，MVE 与原版同构——所以这不是「修坏掉的
reward」，而是**把判官的判定沉淀成标量**，让两个此前没有信号的机制活起来：
  ① 组内择优入库（同维度多轮候选 / 同名技能重写，r 高者胜）——GRPO 训练时
    行为的学习侧同构；
  ② r 进索引排序（检索优先命中验证过的技能）——消费侧假技能降级。

构成（同构 R1：rule-based 优先、正确性主导、LLM 只兜底）
----------
  has_env=True（有可指认环境：vlml / scene / local）：
    +0.6  环境主裁判：真取到数（real_calls ≥ 1 且 rows > 0，执行层已含
          非空语义——空表聚合 (NULL,0) 骗局不算）
    +0.2  材料账：evidence / observations 全 ok（403/timeout/空返回天然进不来）
    +0.1  有可复现配方（sql 非空）
    +0.1  LLM 判官 success（兜底，可被攻击的权重被压到 0.1）
  has_env=False（无可指认环境 → 产物是 inference）：
    r = 0.1 if judge_success else 0.0   —— 上限 0.1，天然实现「inference
    不高优入库、检索时排后」，不需要「知识型/数据型」分类（2026-10-28 拍板砍掉）。

r ∈ [0, 1]。has_env 的权威来源是 scene_profile 的 kind，不是题面词面。
"""

from __future__ import annotations

from typing import Any

__all__ = ["compute_r", "scene_has_env"]

WEIGHT_REAL_DATA = 0.6     # 环境主裁判：真取到数
WEIGHT_EVIDENCE_OK = 0.2   # 材料账：证据全 ok
WEIGHT_HAS_SQL = 0.1       # 有可复现配方
WEIGHT_JUDGE = 0.1         # LLM 判官兜底（可被攻击，权重压最低）
INFERENCE_CAP = 0.1        # 无环境（inference）的 r 上限


def compute_r(result: dict[str, Any], judge_success: bool,
              has_env: bool) -> float:
    """从一轮学习结果合成标量 reward。

    result = 执行层的产物（同 task_critic.check_task_success 收到的那份）：
      result["execution"] = {real_calls, rows, sql, ...}
      result["evidence"] = [{ok, ...}]（可缺）；缺则看 result["observations"]
      result["answer"] = str
    """
    result = result or {}
    if not has_env:
        return round(INFERENCE_CAP if judge_success else 0.0, 3)

    ex = result.get("execution") or {}
    real = int(ex.get("real_calls") or 0)
    rows = int(ex.get("rows") or 0)
    r = 0.0
    # ① 环境主裁判：真取到数（real_calls 的「真」由执行层保证——空值不算）。
    # ⭐ 真空一票否决全部 rule 信号：没取到数时，材料账/SQL 载体都不该给分
    # —— 它们都是围绕「取到数」才有意义的（空表聚合 (NULL,0) 骗局不过
    # ②③ 骗过 0.3 分）。
    if real >= 1 and rows > 0:
        r += WEIGHT_REAL_DATA
        # ② 材料账：证据全 ok（优先 evidence，缺则回退 observations）
        ev = result.get("evidence")
        items = ev if isinstance(ev, list) else (result.get("observations") or [])
        if items and all(bool(i.get("ok")) for i in items if isinstance(i, dict)):
            r += WEIGHT_EVIDENCE_OK
        # ③ 有可复现配方（技能的物理载体）
        if str(ex.get("sql") or "").strip():
            r += WEIGHT_HAS_SQL
    # ④ LLM 判官兜底
    if judge_success:
        r += WEIGHT_JUDGE
    return round(min(r, 1.0), 3)


def scene_has_env(scene_id: str) -> bool:
    """场景有没有可指认的环境（分型权威，2026-10-28 砍词面分类后的唯一判据）。

    kind ∈ vlml / scene / local → True（local = 本机文件 + 本机原语，也是环境）；
    missing / none / 判定失败 → False（无环境 → 产物是 inference）。
    """
    try:
        from scene_profile import profile
        kind = str((profile(str(scene_id or "")) or {}).get("kind") or "")
        return kind in ("vlml", "scene", "local")
    except Exception:
        return False
