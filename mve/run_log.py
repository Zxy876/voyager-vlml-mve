#!/usr/bin/env python3
"""运行日志：每一轮落一行 JSONL。

面板的进步曲线读的就是这个文件。分开写的原因：
- 主循环只管跑，不关心持久化格式
- 面板只管读，不依赖主循环还在不在跑

一条记录 = 一轮（round）的完整可观测状态。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

LOG = Path(__file__).resolve().parent / "run_log.jsonl"


def append(record: dict[str, Any]) -> None:
    record.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_all() -> list[dict[str, Any]]:
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
    return out


def attempted_counts() -> dict[str, int]:
    """每题做过几次（照伴学 ordered_scope_topics 的 attempted 键）。"""
    out: dict[str, int] = {}
    for r in load_all():
        t = str(r.get("topic_id") or "")
        if t:
            out[t] = out.get(t, 0) + 1
    return out


def failed_counts() -> dict[str, int]:
    """每题错（wrong/dont_know）几次 —— 伴学的 retry_wrong_questions 就挑这些。"""
    out: dict[str, int] = {}
    for r in load_all():
        t = str(r.get("topic_id") or "")
        if t and str(r.get("verdict") or "") in ("wrong", "dont_know"):
            out[t] = out.get(t, 0) + 1
    return out


def unresolved_wrongs(topic_id: str) -> int:
    """这道题**还没消化**的错题数（照伴学 mastery_v2 的 unresolved_wrong_count）。

    语义：做错就累加，做对就清零。伴学拿它做两件事
    （adaptive_learning/mastery_v2.py:283-288）：
        if unresolved_wrong_count > 0:
            mastery = min(mastery, policy.unresolved_wrong_mastery_cap)  # =0.79
            flags.append("unresolved_wrong_cap")
        mastered = resolved_wrong_count == 0 and mastery >= threshold
    即：**有没消化的错题就不许判"掌握"** —— 这正是"判错是信号而不是失败"
    在评分侧的样子：不惩罚你错，但错题不清零就不给你毕业。
    """
    n = 0
    for r in load_all():
        if str(r.get("topic_id") or "") != topic_id:
            continue
        v = str(r.get("verdict") or "")
        if v == "correct":
            n = 0
        elif v in ("wrong", "partial", "dont_know"):
            n += 1
    return n


def evidence_rows(topic_id: str) -> list[Any]:
    """这道题的**全部**历史证据，供 V2 掌握度模型跨进程重建。

    V2 的加权与时间衰减要求看到完整序列：只喂当前这一轮，
    权重和永远只有一条，confidence 就退化成常数。
    跳过 evidence_status == "none" 的轮 —— 与主循环「没拿到证据
    不计入掌握度序列」的规矩一致。
    """
    import mastery_model  # 局部导入：避免与上层模块形成循环

    out: list[Any] = []
    for r in load_all():
        if str(r.get("topic_id") or "") != topic_id:
            continue
        if str(r.get("evidence_status") or "") == "none":
            continue
        try:
            out.append(mastery_model.evidence_from_row(r))
        except Exception:
            continue
    return out


def recent_scores(topic_id: str, limit: int = 9) -> list[float]:
    """这道题的历史得分序列（供主循环跨进程续接掌握度序列）。

    只收 evidence_status != "none" 的轮 —— 与主循环「未拿到证据不计入
    掌握度序列」的规矩一致。分数用与 run_mve 的 history 相同的 verdict
    映射（correct=1.0 / partial=0.6 / 其他=0.0），不用 score/100：
    序列内部口径必须一致，variance / consistency 才有意义。
    """
    out: list[float] = []
    for r in load_all():
        if str(r.get("topic_id") or "") != topic_id:
            continue
        if str(r.get("evidence_status") or "") == "none":
            continue
        v = str(r.get("verdict") or "")
        out.append({"correct": 1.0, "partial": 0.6}.get(v, 0.0))
    return out[-limit:]


def last_missing(topic_id: str) -> list[str]:
    """这道题最近一次判错的缺口评分点 —— 跨运行恢复 failed_dims 用。"""
    out: list[str] = []
    for r in load_all():
        if str(r.get("topic_id") or "") != topic_id:
            continue
        if str(r.get("verdict") or "") == "correct":
            out = []
        else:
            out = [str(x) for x in (r.get("missing") or [])]
    return out


def reset() -> None:
    if LOG.exists():
        LOG.unlink()
    print(f"已清空运行日志：{LOG}")
