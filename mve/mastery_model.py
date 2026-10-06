#!/usr/bin/env python3
"""掌握度模型 V2 —— 照猫娘伴学 `adaptive_learning/mastery_v2.py` 移植。

为什么换掉旧的：旧版照的是伴学的 **V1**（`knowledge_tracker.py:252-286`），
而 V1 有两处硬伤，伴学自己都已经在 V2 里修掉了：

  1. `confidence = 1-exp(-attempts/5)` —— **只数作答次数**，
     刷次数就能把掌握度推高（Voyager 一晚上几十轮，比人快得多）。
     V2 改成按**证据权重之和**算（mastery_v2.py:278）。
  2. `recency = 1.0` —— V1 里**写死**（knowledge_tracker.py:266），
     即"三个月前的满分和刚才的满分等价"。V2 引入 `time_decay`，
     半衰期 60 天（mastery_v2.py:43、:416-424）。

V2 还有三样 V1 没有的：
  3. **每条证据有权重** = 评价可信度 × 作答可靠性 × 时间衰减（:335）。
     于是"LLM 判的"不如"SQL 算的"可信、"答得太省"的要打折。
  4. **加权平均**代替简单平均，accuracy / variance / quality 全按权重算。
  5. **可重建**：`as_of` 显式传入、policy 版本化、attempt_id 去重 ——
     同一批事实，增量累积与一次性重算必须得到同一个数（:208-221）。

伴学自己把 V2 标为 **shadow**（mastery_v2.py:1-7）：线上仍是 V1，
V2 并行跑用于对账。我们直接把它转正 —— 因为 MVE 没有"线上用户"要兼容。

两处 MVE 特有适配（伴学没有对应物，按同构映射）：
  - `response_time_ms`（人答题耗时）→ **`tool_calls`**（Voyager 成功调了几次工具）。
    语义相同：都是"这次作答投入了多少"，太快/太省就不可靠。
  - `evaluator_confidence`（评判者可信度）→ **裁判事实的确定性占比**。
    伴学只有"默认 0.75"；MVE 的裁判事实有明确来源
    （`vlml0_referee.deterministic` / `vlml0_referee::工具` / `vlml0_referee.llm`），
    按确定性事实占比映射到 [0.6, 0.95]。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

MODEL_VERSION = "mve-mastery-v2-1"

MASTERY_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
MASTERY_PROGRESSING = "progressing"
MASTERY_MASTERED = "mastered"


@dataclass(frozen=True)
class MasteryPolicy:
    """全部系数集中在一个不可变对象上 —— 照伴学 mastery_v2.py:21-95。

    集中在这里的意义（伴学原文）：历史重建可审计，且调用方无法偷偷改模型行为。
    """

    model_version: str = MODEL_VERSION
    correct_default_score: float = 1.0
    partial_default_score: float = 0.5
    wrong_default_score: float = 0.0
    difficulty_modifier_min: float = 0.9
    difficulty_modifier_max: float = 1.1
    hint_used_modifier: float = 0.85
    hint_not_used_modifier: float = 1.0
    hint_unknown_modifier: float = 1.0
    evaluator_confidence_default: float = 0.75
    response_time_min_reliability: float = 0.6
    response_time_suspicious_ms: int = 1_000
    response_time_full_reliability_ms: int = 5_000
    # MVE 同构项：工具调用深度（伴学用 response_time_ms，我们没有"作答耗时"）
    depth_min_reliability: float = 0.6
    depth_full_calls: int = 2
    time_decay_half_life_days: float = 60.0
    # ---- MVE：按**会话**衰减（伴学是按天，见下方注释）----
    # 伴学的时间单位是天（:43），因为它的学习者是"隔天回来复习一次"。
    # Voyager 不是：它一晚上能跑几十轮，按天算 recency 恒等于 1.000 —— 等于没衰减。
    # 但反过来，同一段连续实验里第 1 轮和第 40 轮也不该互相衰减（那是同一场练习）。
    # 所以按**会话**切：一段连续操作 = 一个会话，隔够 gap 才算下一个。
    time_decay_basis: str = "session"        # "session" | "day"
    time_decay_half_life_sessions: float = 3.0
    session_gap_hours: float = 2.0
    consistency_floor: float = 0.7
    consistency_span: float = 0.3
    confidence_evidence_scale: float = 4.0
    confidence_floor: float = 0.5
    confidence_span: float = 0.5
    mastered_threshold: float = 0.8
    unresolved_wrong_mastery_cap: float = 0.79
    rounding_digits: int = 6
    # 三态门槛（伴学 practice_outcome.py:14）
    mastery_min_attempts: int = 3

    def __post_init__(self) -> None:
        unit_fields = (
            self.correct_default_score, self.partial_default_score,
            self.wrong_default_score, self.hint_used_modifier,
            self.hint_not_used_modifier, self.hint_unknown_modifier,
            self.evaluator_confidence_default, self.response_time_min_reliability,
            self.depth_min_reliability, self.consistency_floor,
            self.consistency_span, self.confidence_floor, self.confidence_span,
            self.mastered_threshold, self.unresolved_wrong_mastery_cap,
        )
        if not self.model_version.strip():
            raise ValueError("model_version is required")
        if any(not math.isfinite(v) or not 0.0 <= v <= 1.0 for v in unit_fields):
            raise ValueError("mastery policy probabilities must be finite values in [0, 1]")
        if not 0.0 <= self.difficulty_modifier_min <= self.difficulty_modifier_max:
            raise ValueError("difficulty modifiers must be ordered and non-negative")
        if not math.isfinite(self.time_decay_half_life_days) or self.time_decay_half_life_days <= 0:
            raise ValueError("time_decay_half_life_days must be positive")
        if not math.isfinite(self.confidence_evidence_scale) or self.confidence_evidence_scale <= 0:
            raise ValueError("confidence_evidence_scale must be positive")
        if self.consistency_floor + self.consistency_span > 1.0:
            raise ValueError("consistency factor must not exceed 1")
        if self.confidence_floor + self.confidence_span > 1.0:
            raise ValueError("confidence factor must not exceed 1")
        # 伴学为此专门加了断言（mastery_v2.py:92）：封顶必须低于毕业线，
        # 否则"有没消化的错题不许毕业"这条规矩形同虚设。
        if self.unresolved_wrong_mastery_cap >= self.mastered_threshold:
            raise ValueError("unresolved wrong mastery cap must be below the mastered threshold")


DEFAULT_POLICY = MasteryPolicy()


@dataclass(frozen=True)
class MasteryEvidence:
    """一条不可变的评估事实（照伴学 mastery_v2.py:101-118）。"""

    attempt_id: str
    verdict: str
    score: float | int | None
    difficulty: float | int | None
    used_hint: bool | None
    response_time_ms: int | None
    evaluator_confidence: float | None
    submitted_at: datetime | str
    # MVE 特有：这次作答成功调了几次工具（伴学用 response_time_ms 表达同一件事）
    tool_calls: int | None = None

    def __post_init__(self) -> None:
        if not str(self.attempt_id or "").strip():
            raise ValueError("attempt_id is required")
        if self.used_hint is not None and not isinstance(self.used_hint, bool):
            raise TypeError("used_hint must be bool or None")


@dataclass(frozen=True)
class MasterySnapshot:
    topic_id: str
    mastery: float
    accuracy: float
    recency: float
    consistency: float
    confidence: float
    evidence_count: int
    unresolved_wrong_count: int
    level: str            # 五档（MVE 沿用伴学 V1 的档位名，面板要用）
    status: str           # 三态（practice_outcome.py:7-9）
    flags: tuple[str, ...] = ()
    mastery_model_version: str = MODEL_VERSION
    computed_at: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "topic_id": self.topic_id,
            "mastery": self.mastery,
            "accuracy": self.accuracy,
            "recency": self.recency,
            "consistency": self.consistency,
            "confidence": self.confidence,
            "evidence_count": self.evidence_count,
            "unresolved_wrong_count": self.unresolved_wrong_count,
            "level": self.level,
            "status": self.status,
            "flags": list(self.flags),
            "mastery_model_version": self.mastery_model_version,
            "computed_at": self.computed_at,
        }


@dataclass(frozen=True)
class _Prepared:
    submitted_at: datetime
    normalized_score: float
    attempt_quality: float
    evidence_weight: float
    time_decay: float
    session_index: int = 0


def assign_sessions(times: Iterable[datetime], gap_hours: float) -> list[int]:
    """把一串时刻切成会话：相邻间隔 > gap 就开一个新会话。

    只依赖时间戳 → **可重建**（不需要额外的"开始/结束会话"状态）。
    这也是不让人手工开关会话的原因：手工开关会留下状态，而 V2 的全部价值
    就在于"同一批事实随时能重算出同一个数"。
    """
    ordered = sorted(t for t in times if isinstance(t, datetime))
    out: list[int] = []
    idx = 0
    prev: datetime | None = None
    for t in ordered:
        if prev is not None and (t - prev).total_seconds() > gap_hours * 3600.0:
            idx += 1
        out.append(idx)
        prev = t
    return out


def calculate_mastery(
    topic_id: str,
    evidence: Iterable[MasteryEvidence],
    *,
    unresolved_wrong_count: int = 0,
    has_active_wrong_question: bool = False,
    as_of: datetime | str | None = None,
    policy: MasteryPolicy = DEFAULT_POLICY,
) -> MasterySnapshot:
    """从一个题的全部事实投影出掌握度（照伴学 mastery_v2.py:208-303）。

    `as_of` 显式传入而不是读时钟 —— 伴学原文：这样重建才是确定的。
    """
    resolved_topic = str(topic_id or "").strip()
    if not resolved_topic:
        raise ValueError("topic_id is required")
    if isinstance(unresolved_wrong_count, bool):
        raise TypeError("unresolved_wrong_count must be an integer")
    wrong_count = max(0, int(unresolved_wrong_count))
    projection_time = _coerce_datetime(as_of) if as_of is not None else datetime.now(timezone.utc)

    unique = _deduplicate(evidence)
    ordered = sorted(unique, key=lambda e: (_coerce_datetime(e.submitted_at, default=projection_time),
                                            e.attempt_id))
    # 会话划分：同一段连续操作内的证据互不衰减，隔够 gap 才进下一个会话。
    sessions = assign_sessions(
        [_coerce_datetime(e.submitted_at, default=projection_time) for e in ordered],
        policy.session_gap_hours,
    )
    current_session = sessions[-1] if sessions else 0
    if ordered:
        last_ts = _coerce_datetime(ordered[-1].submitted_at, default=projection_time)
        if (projection_time - last_ts).total_seconds() > policy.session_gap_hours * 3600.0:
            current_session += 1

    prepared = [
        _prepare(item, as_of=projection_time, policy=policy,
                 session_age=current_session - sessions[i])
        for i, item in enumerate(ordered)
    ]
    evidence_count = len(prepared)
    flags: list[str] = []

    if not prepared:
        flags.append("no_evidence")
        return MasterySnapshot(
            topic_id=resolved_topic, mastery=0.0, accuracy=0.0, recency=0.0,
            consistency=0.0, confidence=0.0, evidence_count=0,
            unresolved_wrong_count=wrong_count, level=_level(0.0),
            status=MASTERY_INSUFFICIENT_EVIDENCE, flags=tuple(flags),
            mastery_model_version=policy.model_version,
            computed_at=_format_datetime(projection_time),
        )

    total_weight = sum(p.evidence_weight for p in prepared)
    if total_weight > 0.0:
        accuracy = sum(p.normalized_score * p.evidence_weight for p in prepared) / total_weight
        weighted_quality = sum(p.attempt_quality * p.evidence_weight for p in prepared) / total_weight
        variance = sum(
            p.evidence_weight * (p.normalized_score - accuracy) ** 2 for p in prepared
        ) / total_weight
    else:
        accuracy = 0.0
        weighted_quality = 0.0
        variance = 0.25
        flags.append("zero_confidence_evidence")

    consistency = _clamp(1.0 - 2.0 * math.sqrt(max(0.0, variance)))
    recency = sum(p.time_decay for p in prepared) / evidence_count
    confidence = 1.0 - math.exp(-total_weight / policy.confidence_evidence_scale)
    consistency_factor = policy.consistency_floor + policy.consistency_span * consistency
    confidence_factor = policy.confidence_floor + policy.confidence_span * confidence
    mastery = _clamp(weighted_quality * consistency_factor * confidence_factor)

    if wrong_count > 0:
        mastery = min(mastery, policy.unresolved_wrong_mastery_cap)
        flags.append("unresolved_wrong_cap")

    rounded = _round_unit(mastery, policy)
    # 三态照伴学 practice_outcome.py:41-45。
    # 注意伴学在这有个坑：V2 snapshot 没有 `attempts` 字段，而 practice_outcome
    # 读的正是它 → attempts=0 < 3 → 对 V2 会恒判 insufficient_evidence。
    # 我们用 evidence_count 填这个位置（语义一致：有几条证据）。
    if evidence_count < policy.mastery_min_attempts:
        status = MASTERY_INSUFFICIENT_EVIDENCE
    elif rounded >= policy.mastered_threshold and not (
        wrong_count > 0 or has_active_wrong_question
    ):
        status = MASTERY_MASTERED
    else:
        status = MASTERY_PROGRESSING

    return MasterySnapshot(
        topic_id=resolved_topic,
        mastery=rounded,
        accuracy=_round_unit(accuracy, policy),
        recency=_round_unit(recency, policy),
        consistency=_round_unit(consistency, policy),
        confidence=_round_unit(confidence, policy),
        evidence_count=evidence_count,
        unresolved_wrong_count=wrong_count,
        level=_level(rounded),
        status=status,
        flags=tuple(flags),
        mastery_model_version=policy.model_version,
        computed_at=_format_datetime(projection_time),
    )


def _level(mastery: float) -> str:
    """五档，照伴学 knowledge_tracker.py:240-250。"""
    v = _clamp(float(mastery or 0.0))
    if v < 0.20:
        return "未接触"
    if v < 0.40:
        return "薄弱"
    if v < 0.60:
        return "进行中"
    if v < 0.80:
        return "熟练"
    return "掌握"


def _deduplicate(evidence: Iterable[MasteryEvidence]) -> tuple[MasteryEvidence, ...]:
    """同一 attempt_id 重复投递：事实一致就忽略，冲突就报错（伴学:306-315）。"""
    unique: dict[str, MasteryEvidence] = {}
    for item in evidence:
        prev = unique.get(item.attempt_id)
        if prev is not None and prev != item:
            raise ValueError(f"conflicting facts for attempt_id: {item.attempt_id}")
        unique[item.attempt_id] = item
    return tuple(unique.values())


def _prepare(
    item: MasteryEvidence,
    *,
    as_of: datetime,
    policy: MasteryPolicy,
    session_age: int = 0,
) -> _Prepared:
    submitted_at = _coerce_datetime(item.submitted_at, default=as_of)
    normalized_score = _normalized_score(item.verdict, item.score, policy)
    difficulty_modifier = _difficulty_modifier(item.difficulty, policy)
    hint_modifier = _hint_modifier(item.used_hint, policy)
    attempt_quality = _clamp(normalized_score * difficulty_modifier * hint_modifier)
    evaluator_confidence = _finite_unit(
        item.evaluator_confidence, default=policy.evaluator_confidence_default
    )
    # 伴学是 response_time_reliability × （没有 depth 项）；
    # MVE 用工具调用深度做同一件事：只调一个聚合报告就交卷 → 不可靠。
    depth_reliability = _depth_reliability(item.tool_calls, policy)
    time_decay = _decay(submitted_at, as_of=as_of, policy=policy, session_age=session_age)
    evidence_weight = _clamp(evaluator_confidence * depth_reliability * time_decay)
    return _Prepared(
        submitted_at=submitted_at,
        normalized_score=normalized_score,
        attempt_quality=attempt_quality,
        evidence_weight=evidence_weight,
        time_decay=time_decay,
        session_index=session_age,
    )


def _normalized_score(verdict: str, score: float | int | None, policy: MasteryPolicy) -> float:
    """照伴学 mastery_v2.py:346-368。

    与 V1 的关键差别：**有具体分数就用分数**（V1 只认 verdict 的三档）。
    MVE 的 score 是覆盖率（0-100），所以 45% 的 partial 不会和 75% 的
    partial 拿同一个 0.6 —— 这一条直接消掉了 V1 的信息损失。
    """
    normalized_verdict = str(verdict or "").strip().lower()
    fallback = {
        "correct": policy.correct_default_score,
        "partial": policy.partial_default_score,
        "wrong": policy.wrong_default_score,
        "dont_know": policy.wrong_default_score,
    }.get(normalized_verdict, policy.wrong_default_score)
    if normalized_verdict in {"wrong", "dont_know"}:
        return policy.wrong_default_score
    if isinstance(score, bool) or score is None:
        return fallback
    try:
        numeric = float(score)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if not math.isfinite(numeric):
        return fallback
    return _clamp(numeric / 100.0)


def _difficulty_modifier(difficulty: float | int | None, policy: MasteryPolicy) -> float:
    """伴学 mastery_v2.py:371-388，题目难度用服务端 1..5 档。"""
    if isinstance(difficulty, bool) or difficulty is None:
        normalized = 0.5
    else:
        try:
            numeric = float(difficulty)
        except (TypeError, ValueError, OverflowError):
            numeric = 3.0
        if not math.isfinite(numeric):
            numeric = 3.0
        normalized = _clamp((numeric - 1.0) / 4.0)
    return policy.difficulty_modifier_min + (
        policy.difficulty_modifier_max - policy.difficulty_modifier_min
    ) * normalized


def _hint_modifier(used_hint: bool | None, policy: MasteryPolicy) -> float:
    if used_hint is None:
        return policy.hint_unknown_modifier
    return policy.hint_used_modifier if used_hint else policy.hint_not_used_modifier


def _response_time_reliability(response_time_ms: int | None, policy: MasteryPolicy) -> float:
    """照伴学 mastery_v2.py:399-413（MVE 目前不传作答耗时，保留以对齐结构）。"""
    if isinstance(response_time_ms, bool) or response_time_ms is None or response_time_ms < 0:
        return 1.0
    if response_time_ms <= policy.response_time_suspicious_ms:
        return policy.response_time_min_reliability
    if response_time_ms >= policy.response_time_full_reliability_ms:
        return 1.0
    span = policy.response_time_full_reliability_ms - policy.response_time_suspicious_ms
    progress = (response_time_ms - policy.response_time_suspicious_ms) / span
    return policy.response_time_min_reliability + (
        1.0 - policy.response_time_min_reliability
    ) * progress


def _depth_reliability(tool_calls: int | None, policy: MasteryPolicy) -> float:
    """`_response_time_reliability` 的同构版：把"作答耗时"换成"工具调用深度"。

    只成功调了 1 次工具（例如只拿一个聚合报告就报四个数）→ 不可靠，
    照伴学对"1 秒交卷"的处理取同一个下限 0.6。
    """
    if isinstance(tool_calls, bool) or tool_calls is None or tool_calls < 0:
        return 1.0
    if tool_calls <= 1:
        return policy.depth_min_reliability
    return 1.0


def _decay(
    submitted_at: datetime,
    *,
    as_of: datetime,
    policy: MasteryPolicy,
    session_age: int = 0,
) -> float:
    """时间衰减的分派：MVE 默认按**会话**，伴学口径按天（可切回）。

    按会话：`2^(-会话年龄 / 半衰期会话数)`。同一会话内 = 1（不衰减），
    隔 3 个会话 = 0.5。间隔多久算一个会话由 `session_gap_hours` 定。

    用 `2.0 ** x` 而不是 `math.exp2`：后者是 Python 3.11 才加的，
    实测在 3.10 的解释器上直接 `AttributeError: module 'math' has no
    attribute 'exp2'`，整轮在 update_mastery 处崩掉 —— 掌握度模型不该
    依赖解释器版本。
    """
    if policy.time_decay_basis == "day":
        return _time_decay(submitted_at, as_of=as_of, policy=policy)
    return _clamp(2.0 ** (-max(0, session_age)
                          / policy.time_decay_half_life_sessions))


def _time_decay(submitted_at: datetime, *, as_of: datetime, policy: MasteryPolicy) -> float:
    """伴学 mastery_v2.py:416-424：半衰期 60 天（人的复习节奏）。

    对 MVE 基本不起作用 —— Voyager 一晚上几十轮，60 天≈零衰减，
    这也是默认走 `time_decay_basis = "session"` 的原因。
    """
    age_days = max(0.0, (as_of - submitted_at).total_seconds()) / 86_400.0
    return _clamp(math.exp(-math.log(2.0) * age_days / policy.time_decay_half_life_days))


def _finite_unit(value: float | int | None, *, default: float) -> float:
    if isinstance(value, bool) or value is None:
        return _clamp(default)
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return _clamp(default)
    return _clamp(numeric) if math.isfinite(numeric) else _clamp(default)


def _clamp(value: float) -> float:
    return 0.0 if not math.isfinite(value) else min(1.0, max(0.0, value))


def _round_unit(value: float, policy: MasteryPolicy) -> float:
    return round(_clamp(value), policy.rounding_digits)


def _coerce_datetime(value: datetime | str, *, default: datetime | None = None) -> datetime:
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            if default is None:
                raise ValueError("datetime value is required")
            return default
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            if default is None:
                raise
            return default
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def evidence_from_row(row: dict, *, default_difficulty: float = 3.0) -> MasteryEvidence:
    """从 run_log 的一行还原一条证据 —— 跨进程重建用。

    证据必须能从日志**完整**重建：否则每次跑都是新进程，
    权重/时间衰减就永远只看到当前这一轮。
    """
    ts = str(row.get("ts") or "")
    round_no = row.get("round")
    return MasteryEvidence(
        attempt_id=f"{ts}#{round_no}",
        verdict=str(row.get("verdict") or ""),
        score=row.get("score"),
        difficulty=row.get("difficulty", default_difficulty),
        used_hint=None,
        response_time_ms=None,
        evaluator_confidence=row.get("evaluator_confidence"),
        submitted_at=ts,
        tool_calls=row.get("tool_calls"),
    )
