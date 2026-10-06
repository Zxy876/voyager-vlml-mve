#!/usr/bin/env python3
"""行动因果时间线（照猫娘伴学的 root_fact_seq）。

伴学源码里没有「行动因果」这个词，但 docs/认知引擎阶段文档.md:80 有同构实现：

    「attempt、question、control 已统一进入单调的 root_fact_seq，
      同秒事实和迟到 extraction 具有稳定因果顺序」

即：**所有类型的事实进同一条单调递增时间线，只保证顺序，不产生分数。**
　- control fact = 人导入（人出的题、人给的解释）
　- attempt fact = Voyager 的行动（编排、事实集、verdict）
　- referee fact = 裁判的判定
　- narrative fact = 叙事产出

为什么必须单独建这条线：
　掌握度是「评分」，会被 validated_target=False 的题排除；
　但人导入的题虽然不评分，仍然要被出题器看见——
　「人工导入的意图进时间线（因果），不进掌握度（评分）；
　　出题器看学习轨迹，不看单次分数」。
　没有这条线，人导入就等于什么都没发生。

本文件不 import vlml_env，面板可以直接读。
"""

from __future__ import annotations

import json
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Any

LOG = Path(__file__).resolve().parent / "causal_timeline.jsonl"

# 事实种类。control 与 attempt 的区分是这份设计的核心。
CONTROL = "control"        # 人导入：出题、给解释、改范围
ATTEMPT = "attempt"        # Voyager 的行动
REFEREE = "referee"        # 裁判判定
NARRATIVE = "narrative"    # 叙事产出

KIND_LABEL = {
    CONTROL: "人导入",
    ATTEMPT: "Voyager 行动",
    REFEREE: "裁判判定",
    NARRATIVE: "叙事产出",
}


def _next_seq(rows: list[dict[str, Any]]) -> int:
    """单调递增序号。同秒到达的事实靠它保序，不靠时间戳。"""
    highest = 0
    for r in rows:
        try:
            highest = max(highest, int(r.get("seq") or 0))
        except (TypeError, ValueError):
            continue
    return highest + 1


def append(kind: str, *, topic_id: str = "", summary: str = "",
           detail: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = load()
    fact = {
        "seq": _next_seq(rows),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "topic_id": topic_id,
        "summary": summary,
        "detail": detail or {},
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(fact, ensure_ascii=False) + "\n")
    return fact


def load() -> list[dict[str, Any]]:
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.sort(key=lambda r: int(r.get("seq") or 0))
    return out


def for_panel(limit: int = 30) -> list[dict[str, Any]]:
    rows = load()
    return [
        {
            "seq": r.get("seq"),
            "ts": r.get("ts", ""),
            "kind": r.get("kind", ""),
            "kind_label": KIND_LABEL.get(r.get("kind", ""), r.get("kind", "")),
            "topic_id": r.get("topic_id", ""),
            "summary": r.get("summary", ""),
        }
        for r in rows[-limit:]
    ]


def counts_by_topic() -> dict[str, dict[str, int]]:
    """出题器要看的：每题各有多少 control / attempt。"""
    out: dict[str, dict[str, int]] = {}
    for r in load():
        topic = str(r.get("topic_id") or "")
        if not topic:
            continue
        bucket = out.setdefault(topic, {"control": 0, "attempt": 0, "referee": 0})
        kind = str(r.get("kind") or "")
        if kind in bucket:
            bucket[kind] += 1
    return out


def latest_topics_by_kind(kind: str) -> list[str]:
    """按「最近发生」排序的 topic 列表（去重，最新在前）。

    `counts_by_topic()` 只有计数没有时序，出题器用它分不出「人上周导入过」
    和「人刚才导入过」。人导入的意义就在于**当下**的意图，所以必须按时序取。
    """
    rows = [r for r in load() if str(r.get("kind")) == kind and str(r.get("topic_id") or "")]
    rows.sort(key=lambda r: int(r.get("seq") or 0), reverse=True)
    out: list[str] = []
    for r in rows:
        t = str(r["topic_id"])
        if t not in out:
            out.append(t)
    return out


def reset() -> None:
    if LOG.exists():
        LOG.unlink()
    print(f"已清空行动因果时间线：{LOG}")
