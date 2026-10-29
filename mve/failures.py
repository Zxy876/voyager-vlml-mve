#!/usr/bin/env python3
"""失败经验沉淀 —— R1 判词回灌 + 技能库失败侧（2026-10-29 用户拍板「搬」）。

用户原话的痛点：「现在我每问 MVE 一个问题就要来找你 debug」。诊断：失败分
三层 —— 执行层已有内生反馈（真空防线/对题判官退回重写环），但**理解层**
（指代/口径/路线选错）与**答案质量层**（答了但答不到点上）的失败完全
不可见：判官判词只落在当轮，跨轮即丢 —— 同类问题下轮规划员对上次犯的错
一无所知，同一个坑反复踩，人被迫当「错误信号回路」。

同构依据：
  · 原版 Voyager：环境反馈即时+机械可读 → self-verify → 就地重试 → 技能
    沉淀，人不在环里。但 MVE 环境只给数据不给「对不对」，**判官判词是唯一
    的环境反馈** —— 原版闭环只覆盖执行层，理解/质量层是 MVE 多出来的新
    地层，原版没有现成答案。
  · DeepSeek R1：RLVR 可验证判据自动打分，训练时把 verifier 信号回灌成
    策略。MVE 没有训练环，对应物 = **verifier 判词进检索记忆**：判不过的
    理由按场景归档，下次消费规划时检索注入「上次犯过 X 错」。

设计（刻意轻量）：
  · 账本 `failure_ledger.json` 按 scene_id 分桶，条目 {q, why, stage, ts}；
    (q, why) 精确去重；每场景封顶 MAX_PER_SCENE（新的挤旧的）—— 失败账是
    「近期踩过的坑」，不是审计日志。
  · 检索用**词面重叠**（字符 bigram jaccard），不上向量库：场景内条目量
    小（几十条），embedding 调用要 key 还要降级，不值。注入的目的不是
    精确匹配，是把「这个场景近期犯过的错」放进规划员视野。
  · ⚠️ 与技能库的边界：技能库存**成功**配方（怎么算对）；失败账存**失败**
    判词（怎么错的）。判对了不记 —— 修正后的成功由技能线照旧收账。

⚠️ 失败账是**增强**不是**依赖**：任何一步挂都不许影响消费本身 ——
全部 try/except 兜住、损坏文件改名留证后重建（同 revisit 纪律）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "failure_ledger.json"
MAX_PER_SCENE = 50       # 每场景最多留多少条（新的挤旧的）

_LOCK = __import__("threading").Lock()


def _load() -> dict[str, list[dict[str, Any]]]:
    """读账本；missing → 空，broken → 改名留证后重建（不静默吞）。"""
    if not LEDGER.exists():
        return {}
    try:
        j = json.loads(LEDGER.read_text(encoding="utf-8"))
        return j if isinstance(j, dict) else {}
    except Exception as e:
        try:
            os.replace(LEDGER, LEDGER.with_suffix(
                f".broken.{int(time.time())}.json"))
            print(f"[failures] 账本损坏已改名留证：{type(e).__name__}: {e}",
                  flush=True)
        except Exception:
            pass
        return {}


def _save(data: dict[str, list[dict[str, Any]]]) -> None:
    """原子写（tmp + os.replace；scenes.json 损坏留证的教训同源）。"""
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, LEDGER)


def record(scene_id: str, question: str, why: str, stage: str) -> bool:
    """记一条失败判词。返回是否真的记了（去重/空值 → False）。

    stage：plan（理解层：路线/口径选错）| fit（对题判官退回）|
           compose（组合没取到真数）| rewrite（重写后仍不对题）。
    """
    q = " ".join(str(question or "").split())
    w = " ".join(str(why or "").split())[:300]
    if not q or not w:
        return False
    with _LOCK:
        data = _load()
        bucket = data.setdefault(str(scene_id or "?"), [])
        if any(e.get("q") == q and e.get("why") == w for e in bucket):
            return False
        bucket.append({"q": q, "why": w, "stage": str(stage or "?"),
                       "ts": int(time.time())})
        data[str(scene_id or "?")] = bucket[-MAX_PER_SCENE:]
        try:
            _save(data)
            return True
        except Exception:
            return False


def _bigrams(s: str) -> set[str]:
    s = "".join(ch for ch in str(s or "").lower() if ch.isalnum())
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s} if s else set()


def recall(scene_id: str, question: str,
           limit: int = 3) -> list[dict[str, Any]]:
    """按词面重叠召回该场景相关的失败经验（不含 score<0.1 的）。"""
    q_bi = _bigrams(question)
    if not q_bi:
        return []
    with _LOCK:
        bucket = list(_load().get(str(scene_id or "?")) or [])
    scored: list[tuple[float, dict[str, Any]]] = []
    for e in bucket:
        e_bi = _bigrams(e.get("q") or "")
        if not e_bi:
            continue
        inter = len(q_bi & e_bi)
        union = len(q_bi | e_bi)
        score = inter / union if union else 0.0
        if score >= 0.1:
            scored.append((score, e))
    scored.sort(key=lambda t: (-t[0], -t[1].get("ts", 0)))
    out = []
    for score, e in scored[:max(1, int(limit or 3))]:
        out.append({**e, "score": round(score, 3)})
    return out


def recall_block(scene_id: str, question: str, limit: int = 3) -> str:
    """注入规划/组合上下文的文本块；没有相关失败 → 空串（不挂废话）。"""
    items = recall(scene_id, question, limit)
    if not items:
        return ""
    lines = [f"-（{e['stage']}）问过「{e['q'][:60]}」犯过的错：{e['why'][:160]}"
             for e in items]
    return ("⚠️ 本场景近期类似问题的失败判词（别再犯，但判词也可能是误判，"
            "以库实貌为准）：\n" + "\n".join(lines))


def clear(scene_id: str | None = None) -> int:
    """清账（面板全清/单场景 reset 用）；返回清掉的条数。"""
    with _LOCK:
        data = _load()
        if scene_id is None:
            n = sum(len(v) for v in data.values())
            data = {}
        else:
            n = len(data.pop(str(scene_id), []))
        if n:
            _save(data)
        return n
