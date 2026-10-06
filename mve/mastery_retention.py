#!/usr/bin/env python3
"""掌握度的**保持度**（会忘）—— 照猫娘伴学 `adaptive_learning/mastery_retention.py`。

V2 的投影回答的是"现在掌握到什么程度"，这一层回答的是"**过一阵还剩多少**"。
伴学原文（:1-5）：这是试验性启发式，不是 FSRS、也不是训练出来的 HLR 模型。

两条式子（伴学 :27-54）：
    current_mastery = baseline × 2^(-days / half_life)
    half_life' = half_life × (1 ± confidence × weight)   # 答对拉长、答错缩短

MVE 适配一处（`used_hint`）：伴学用它判断"这次做对是靠自己还是靠提示"，
没有它半衰期就永远不动（伴学 :42-43 直接原样返回）。Voyager 不吃提示，
但**吃技能库** —— 本轮注入了技能经验 ≈ 靠提示做对，这就是同构项：
`used_hint ≡ 本轮注入过技能库经验`。
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "mastery_retention.jsonl"

MODEL_VERSION = "mve-retention-1"
# 伴学是 7.0 **天**（人的复习周期是"隔天"）。MVE 的时间单位按**会话**走
# —— 与 mastery_model.MasteryPolicy.time_decay_half_life_sessions 保持同一套单位，
# 否则一边按会话衰减、一边按天遗忘，两边对不上账。
INITIAL_HALF_LIFE = 3.0
SESSION_GAP_HOURS = 2.0
ZERO_THRESHOLD = 0.001
ACQUIRE_THRESHOLD = 0.01


def finite_fraction(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a mastery fraction")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError("expected a finite fraction in [0, 1]")
    return result


def current_mastery(baseline: float, half_life: float, elapsed_sessions: float) -> float:
    """伴学 mastery_retention.py:27-34（把"天数"换成"会话数"）。"""
    baseline = finite_fraction(baseline)
    if not math.isfinite(half_life) or half_life <= 0:
        raise ValueError("invalid half life")
    if not math.isfinite(elapsed_sessions) or elapsed_sessions < 0:
        raise ValueError("invalid elapsed sessions")
    value = baseline * math.exp2(-elapsed_sessions / half_life)
    return 0.0 if value < ZERO_THRESHOLD else value


def feedback_half_life(
    half_life: float,
    elapsed_sessions: float,
    verdict: str,
    confidence: float | None,
    used_hint: bool | None,
) -> float:
    """伴学 mastery_retention.py:37-54：答对（且没靠提示）拉长，答错缩短。

    `elapsed_sessions` = 距上次作答跨了几个会话。同一会话内再答一次 → 0，
    半衰期不变（一次练习里的连续尝试不构成"又复习了一遍"）。
    """
    current_mastery(1.0, half_life, elapsed_sessions)
    if confidence is None or used_hint is None:
        return half_life
    confidence = finite_fraction(confidence)
    weight = -math.expm1(-math.log(2) * elapsed_sessions / half_life)
    if verdict == "correct" and used_hint is False:
        result = half_life * (1 + confidence * weight)
    elif verdict in {"wrong", "dont_know"}:
        result = half_life * (1 - 0.5 * confidence * weight)
    else:
        result = half_life
    if not math.isfinite(result) or result <= 0:
        raise ValueError("half life exceeded numeric range")
    return result


def evidence_baseline(scores: list[float]) -> float:
    """伴学 :57-70 —— V1 风格的一致性估计，用来当保持度的 baseline。

    原文注释：这里不做日历加权，因为"时间只在保持度层施加一次"；
    提示折扣由证据收集方处理，不是这个函数的职责。
    """
    values = [finite_fraction(s) for s in scores[-10:]]
    if not values:
        raise ValueError("missing mastery evidence")
    accuracy = sum(values) / len(values)
    variance = sum((s - accuracy) ** 2 for s in values) / len(values)
    consistency = max(0.0, 1 - 2 * math.sqrt(variance))
    support = 1 - math.exp(-len(values) / 5)
    return min(1.0, accuracy * (0.6 + 0.4 * consistency) * (0.55 + 0.45 * support))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce(value: Any, default: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return default or _now()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return default or _now()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class RetentionStore:
    """每道题一行：baseline（学会时的水平）+ half_life（能撑多久）。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or LOG)

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        out: dict[str, dict[str, Any]] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            topic = str(row.get("topic_id") or "")
            if topic:
                out[topic] = row
        return out

    def get(self, topic_id: str) -> dict[str, Any]:
        return self.load().get(str(topic_id or ""), {
            "topic_id": str(topic_id or ""),
            "baseline": 0.0,
            "half_life": INITIAL_HALF_LIFE,
            "updated_at": "",
        })

    def upsert(self, topic_id: str, *, baseline: float, half_life: float,
               updated_at: datetime | None = None,
               session_index: int | None = None) -> dict[str, Any]:
        rows = self.load()
        prev = rows.get(str(topic_id)) or {}
        row = {
            "topic_id": str(topic_id),
            "baseline": round(float(baseline), 6),
            "half_life": round(float(half_life), 6),
            "updated_at": _coerce(updated_at or _now()).isoformat().replace("+00:00", "Z"),
            # 会话序号：只用于算"跨了几个会话"，本身不参与掌握度计算
            "session_index": int(prev.get("session_index") or 0)
            if session_index is None else int(session_index),
        }
        rows[str(topic_id)] = row
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            for r in rows.values():
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return row

    def apply_attempt(
        self,
        topic_id: str,
        *,
        verdict: str,
        scores: list[float],
        confidence: float | None,
        used_hint: bool | None,
        as_of: datetime | str | None = None,
    ) -> dict[str, Any]:
        """一轮结束后更新这道题的保持度参数。"""
        now = _coerce(as_of) if as_of is not None else _now()
        prev = self.get(topic_id)
        # 跨了几个会话：只按"与上次作答的间隔"判断。
        # 局限：中间没有活动就没法知道中间隔了几段，一律记 1 —— 保守估计。
        elapsed_sessions = 0.0
        session_index = int(prev.get("session_index") or 0)
        if prev.get("updated_at"):
            gap_hours = (now - _coerce(prev["updated_at"])).total_seconds() / 3600.0
            if gap_hours > SESSION_GAP_HOURS:
                elapsed_sessions = 1.0
                session_index += 1
        try:
            baseline = evidence_baseline(scores)
        except (ValueError, TypeError):
            baseline = float(prev.get("baseline") or 0.0)
        half_life = float(prev.get("half_life") or INITIAL_HALF_LIFE)
        try:
            half_life = feedback_half_life(
                half_life, elapsed_sessions, str(verdict or ""), confidence, used_hint
            )
        except (ValueError, TypeError):
            pass
        return self.upsert(topic_id, baseline=baseline, half_life=half_life,
                           updated_at=now, session_index=session_index)

    def current(self, topic_id: str, *, as_of: datetime | str | None = None) -> dict[str, Any]:
        """这道题**此刻**还剩多少（含已经忘掉的部分）。"""
        now = _coerce(as_of) if as_of is not None else _now()
        row = self.get(topic_id)
        baseline = float(row.get("baseline") or 0.0)
        half_life = float(row.get("half_life") or INITIAL_HALF_LIFE)
        elapsed = 0.0
        if row.get("updated_at"):
            gap_hours = (now - _coerce(row["updated_at"])).total_seconds() / 3600.0
            elapsed = 1.0 if gap_hours > SESSION_GAP_HOURS else 0.0
        try:
            value = current_mastery(baseline, half_life, elapsed)
        except (ValueError, TypeError):
            value = 0.0
        return {
            "topic_id": str(topic_id),
            "baseline": round(baseline, 4),
            "half_life_sessions": round(half_life, 2),
            "elapsed_sessions": round(elapsed, 3),
            "retained": round(value, 4),
            "session_index": int(row.get("session_index") or 0),
            "updated_at": row.get("updated_at") or "",
        }

    def reset(self) -> None:
        if self.path.exists():
            self.path.unlink()


STORE = RetentionStore()

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "show":
        for topic, row in STORE.load().items():
            cur = STORE.current(topic)
            print(f"{topic}: baseline={cur['baseline']} 半衰期={cur['half_life_sessions']}个会话 "
                  f"已过={cur['elapsed_sessions']}个 → 剩余 {cur['retained']}")
    else:
        print(json.dumps({"rows": list(STORE.load().values())}, ensure_ascii=False, indent=2))
