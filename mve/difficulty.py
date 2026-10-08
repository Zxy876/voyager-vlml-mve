#!/usr/bin/env python3
"""出题难度自适应（照猫娘伴学 `difficulty_policy.py`）。

伴学怎么做
----------
`DifficultyPolicy.select()` 是**纯函数**，只吃服务端私有数据，输出 2/3/4：

    combined = (种子难度归一 + 掌握度) / 2
    level    = <0.35 → 2,  <0.65 → 3,  否则 → 4
    blockers  存在        → 强制 2
    reason=retry         → -1（且不再叠加连错，一次最多降一档）
    最近两题连对          → +1
    最近两题连错          → -1
    证据不足（attempts<3 / confidence<0.6 / low_confidence flag）→ 封顶 3
    最后 clamp 到 [2, 4]

关键：伴学的题目是**按 (知识点, 难度) 生成**的 —— 难度是出题的**参数**，
不是题目自带的常量。82 个种子知识点 × 3 档难度 = 梯度。

MVE 为什么不能直接照抄
----------------------
MVE 的 6 道题是**硬编码**的：每题 = 一个知识点 × 一个写死的难度（1/2/2/3/3/4）。
没有「同一知识点的更难版本」，所以伴学那句 `retry → difficulty -= 1`
在 MVE 里**无处落地** —— 错题重试时没法换一道更简单的同知识点题。

而且题目不能让 LLM 自由生成：判定靠 `answer_spec`（服务端配方），
新生成的题没有 answer_spec，裁判就无从独立算出标准答案 —— 那是硬边界。

于是同构关系要换一维：伴学调的是**题目难度**，MVE 能调的是**支架档位**
（scaffolding —— 从扶到放）：

    伴学：同一知识点，难度 2 → 3 → 4（题变难）
    MVE ：同一道题，支架 full → partial → none（题不变，扶得越来越少）

支架收放只改 **prompt 给多少**，不改 `answer_spec`，所以评分点不变、
裁判照样判、掌握度照样算 —— 这是它能成立的原因。

三档支架
--------
    full    —— 给 ★ 声明口径路径；critic 回灌时列出真实候选路径
    partial —— 给聚焦后的结构文档，但**不给 ★**；critic 只说"取空了"，不给候选
    none    —— 结构文档不聚焦（全量截断）；不给 ★，critic 只说"取空了"

判据（照伴学，逐条同构）
------------------------
    证据不足 / 掌握度低  → full（先扶起来）
    连对两题            → 收一档支架（不再需要扶）
    连错两题            → 加一档支架
    wrong_retry         → full（伴学是降难度，MVE 难度不可变，所以加支架）
"""

from __future__ import annotations

from typing import Any, Sequence

HINT_FULL = "full"
HINT_PARTIAL = "partial"
HINT_NONE = "none"
HINT_ORDER = [HINT_NONE, HINT_PARTIAL, HINT_FULL]   # 索引越大 = 扶得越多

HINT_LABEL = {
    HINT_FULL: "给足支架（★ 声明口径 + 候选路径回灌）",
    HINT_PARTIAL: "半扶（只给结构文档，不给口径路径）",
    HINT_NONE: "放开（不聚焦、不给口径、不给候选）",
}

# 伴学是 2..4；MVE 的题集是 1..4，所以这里归一到 1..4
MIN_DIFFICULTY = 1
MAX_DIFFICULTY = 4

LOW_EVIDENCE_ATTEMPTS = 3
LOW_CONFIDENCE = 0.6


def _unit(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return min(1.0, max(0.0, v))


def seed_unit(difficulty: Any) -> float:
    """题目写死的难度 1..4 → 0..1。"""
    d = _unit(difficulty, default=0.4) * MAX_DIFFICULTY
    if d < 1:
        d = 1.0
    return (d - MIN_DIFFICULTY) / (MAX_DIFFICULTY - MIN_DIFFICULTY)


def _level(combined: float) -> int:
    if combined < 0.30:
        return 1
    if combined < 0.55:
        return 2
    if combined < 0.78:
        return 3
    return 4


def _verdicts(recent: Any) -> tuple[str, ...]:
    if not isinstance(recent, Sequence) or isinstance(recent, (str, bytes)):
        return ()
    out = []
    for item in recent:
        if isinstance(item, dict):
            item = item.get("verdict") or ""
        v = str(item or "").strip().lower()
        if v:
            out.append(v)
    return tuple(out)


def _streak(verdicts: tuple[str, ...], want: set[str]) -> bool:
    return len(verdicts) >= 2 and verdicts[-1] in want and verdicts[-2] in want


def select(
    seed_difficulty: Any,
    *,
    mastery: Any = 0.0,
    coverage: Any = None,
    attempts: Any = 0,
    confidence: Any = 0.0,
    flags: Sequence[Any] = (),
    recent_verdicts: Any = (),
    reason: str = "",
    blockers: Sequence[Any] = (),
    prev_hint: Any = "",
) -> dict[str, Any]:
    """算出这题该出到什么难度、给多少支架。返回 {difficulty, hint, why}。"""
    m = _unit(mastery, default=0.0)
    combined = (seed_unit(seed_difficulty) + m) / 2.0
    difficulty = _level(combined)
    why = [f"种子难度 {seed_difficulty} 与掌握度 {m:.2f} 平均 → 难度 {difficulty}"]

    if any(True for _ in (blockers or ())):
        difficulty = MIN_DIFFICULTY
        why.append(f"有未解决的前置阻塞 → 压到 {MIN_DIFFICULTY}")

    hits = HINT_ORDER.index(HINT_PARTIAL)      # 默认半扶
    norm = str(reason or "").strip().lower()
    is_retry = norm in {"retry", "wrong_retry"}

    if is_retry:
        difficulty -= 1
        why.append("错题重试 → 难度 -1")

    verdicts = _verdicts(recent_verdicts)
    if not is_retry:
        if _streak(verdicts, {"correct"}):
            difficulty += 1
            hits -= 1
            why.append("最近连对两题 → 难度 +1、支架收一档")
        elif _streak(verdicts, {"wrong", "dont_know"}):
            difficulty -= 1
            hits += 1
            why.append("最近连错两题 → 难度 -1、支架加一档")

    try:
        att = max(0, int(attempts or 0))
    except (TypeError, ValueError):
        att = 0
    conf = _unit(confidence, default=0.0)
    low_flag = any(str(f or "").strip().lower() == "low_confidence" for f in (flags or ()))
    low_evidence = att < LOW_EVIDENCE_ATTEMPTS or conf < LOW_CONFIDENCE or low_flag
    # ---- 支架判据（优先级从强到弱）----
    #
    # 主判据是**覆盖率**（会不会做），不是掌握度：掌握度低可能只是"证据还少"
    # （V2 的 confidence 会随证据条数被拉低），那不等于"还不会做"。
    # 用掌握度当主判据会出现"明明做得全对却被收掉支架"这种假阳性。
    if is_retry or norm in {"all_stalled", "blocked_diagnostic"}:
        hits = HINT_ORDER.index(HINT_FULL)
        why.append("错题 · 停滞 · 无证据 → 支架给足")
    elif low_evidence:
        hits = HINT_ORDER.index(HINT_FULL)
        why.append(f"证据不足（练 {att} 次 · 置信 {conf:.2f}）→ 难度封顶 3、支架给足")
    else:
        cov = _unit(coverage, default=0.0)
        if cov >= 1.0:
            hits = HINT_ORDER.index(HINT_NONE)
            why.append(f"覆盖率 {cov:.0%} 已满分 → 放开（技能已入库，下一轮直接跑程序）")
        elif cov >= 0.6:
            hits = HINT_ORDER.index(HINT_PARTIAL)
            why.append(f"覆盖率 {cov:.0%} → 半扶")
        else:
            hits = HINT_ORDER.index(HINT_FULL)
            why.append(f"覆盖率 {cov:.0%} 偏低 → 支架给足")

    # 掌握度只用来**收**，不用来放：会了就别再扶
    if m >= 0.80:
        hits = min(hits, HINT_ORDER.index(HINT_NONE))
        why.append(f"掌握度 {m:.2f} 已达标 → 放开")
    elif m >= 0.60:
        hits = min(hits, HINT_ORDER.index(HINT_PARTIAL))
        why.append(f"掌握度 {m:.2f} → 半扶")

    difficulty = min(MAX_DIFFICULTY, max(MIN_DIFFICULTY, difficulty))
    hits = min(len(HINT_ORDER) - 1, max(0, hits))
    # ---- 档位平滑：**每轮最多收/放一档**（照伴学） ----
    #
    # 伴学原文（`difficulty_policy.py:117-120`）：
    #     "A retry already has its one-step decrease.  Do not stack a recent
    #      wrong streak onto it: a retry must never drop more than one level."
    # 连对 → 难度 +1 也一样是一档。**梯度是走出来的，不是跳出来的。**
    #
    # 没有这条约束的实测后果：判据是"本轮覆盖率"，full 档带图谱一做就 100%
    # → 直接跳到 none → 一撤图谱就崩回 0% → 连错又跳回 full —— 档位在
    # full↔none 之间振荡，**partial 档从来没被练过**。这就是面板上
    # 「摸底 16% → 练后重考 16%（持平）→ 结业 0%」的机制：模型从头到尾
    # 只体验过"全给"和"全撤"两种世界，渐进水平无从谈起。
    ph = str(prev_hint or "").strip().lower()
    if ph in HINT_ORDER:
        pi = HINT_ORDER.index(ph)
        if hits > pi + 1:
            hits = pi + 1
            why.append(f"上一轮支架是 {ph}，每轮最多收一档 → {HINT_ORDER[hits]}")
        elif hits < pi - 1:
            hits = pi - 1
            why.append(f"上一轮支架是 {ph}，每轮最多放一档 → {HINT_ORDER[hits]}")
    return {"difficulty": difficulty, "hint": HINT_ORDER[hits], "why": "；".join(why)}


def hint_allows(hint: str, what: str) -> bool:
    """支架档位允许给什么。`what` ∈ {declared, candidates, focus}。"""
    h = str(hint or HINT_PARTIAL)
    table = {
        "declared": {HINT_FULL},
        "candidates": {HINT_FULL},
        "focus": {HINT_FULL, HINT_PARTIAL},
    }
    return h in table.get(what, set())
