#!/usr/bin/env python3
"""消费侧路由：**「命中」按什么判定**（同构猫娘伴学 `material_topic_mapper`）。

设计案的结论（照 neko 实证，可核查）
------------------------------------
**「命中」= 相似度分数阈值，不是人审核通过，也不是「答对且能对上」。**

出处：伴学插件知识子系统 `material_topic_mapper.py:204-215`，主评分器 `match_topics`
（词法评分器）三级判定：

    max_score >= 40 且 exact_match 且 meaningful_match → HIGH_CONFIDENCE
    max_score >= 10 且 chunk_hits >= 2                 → MEDIUM_CONFIDENCE
    否则                                                → rejected_ids（未命中）

补充（`:225-230`）：即便分数够了，若该 topic 在库里已不存在（`get_topic is None`）
也判 rejected —— **「命中」还要求目标节点真实存在**。

两个必须守住的分界：
  · 人审是匹配**之后**对候选评分的下游动作，不参与「命中」判定；
  · 匹配**与答案对错无关** —— 它只判断"这个问题和哪个已有节点相关"，
    答得对不对是掌握度维度，不是路由维度。

未命中归宿（同构 neko `candidate_knowledge_items`）
--------------------------------------------------
未命中/被拒项**不挂活图谱、也不丢弃**，先进独立待审区（`pending_dimensions.json`，
`status=pending_review`）；冷启动**解题成功**才把它升为活节点
（仿 `update_candidate_score_status` 的 accepted）。被拒项留审计、不删。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PENDING_DIMENSIONS = HERE / "pending_dimensions.json"

# 双阈值（照 neko match_topics:204-215）
HIGH_CONF_MIN = 40        # >= 40 且 exact_match 且 meaningful_match → 高置信命中
MEDIUM_CONF_MIN = 10      # >= 10 且重复命中 >= 2 → 中置信命中
MEDIUM_CHUNK_HITS = 2

# 判定枚举
HIGH = "HIGH_CONFIDENCE"
MEDIUM = "MEDIUM_CONFIDENCE"
REJECTED = "REJECTED"

# 命中类型（同构 neko 的 match_type 命名）
MATCH_EXACT = "material_exact_match"
MATCH_REPEATED = "material_repeated_match"

# 中英混排分词（与 skill_store._tokens 同一套语义）
def _tokens(s: str) -> list[str]:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(s or "").lower())
    out: list[str] = []
    for w in s.split():
        if re.fullmatch(r"[a-z0-9_]+", w):
            out.append(w)
            continue
        if len(w) == 1:
            out.append(w)
            continue
        for i in range(len(w) - 1):
            out.append(w[i:i + 2])
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(s or "").lower())


def _score_one(q_norm: str, q_tokens: list[str], texts: list[str]) -> tuple[int, bool, bool, int]:
    """给**一个节点**打分：返回 (score, exact_match, meaningful_match, chunk_hits)。"""
    if not q_norm or not texts:
        return 0, False, False, 0
    joined = " ".join(_norm(t) for t in texts)
    exact = bool(q_norm) and q_norm in joined
    hits = [t for t in q_tokens if t in joined]
    chunk_hits = len(set(hits))
    if not hits:
        return 0, exact, False, 0
    cover = len(set(hits)) / max(1, len(set(q_tokens)))
    # 覆盖率为主（60），命中词的总长度为辅（长词更说明归属，最多 40）
    len_bonus = min(40, sum(len(t) for t in set(hits)) * 3)
    score = int(cover * 60 + len_bonus)
    return min(100, score), exact, cover >= 0.5, chunk_hits


def _node_texts(node: Any) -> list[str]:
    """节点参与匹配的文本：label + 维度名 + 别名 + 表名。"""
    out: list[str] = [str(getattr(node, "label", "") or ""),
                      str(getattr(node, "id", "") or "")]
    detail = getattr(node, "detail", None) or {}
    if isinstance(detail, dict):
        for k in ("aliases", "dimension", "table", "topic_id", "value_path"):
            v = detail.get(k)
            if isinstance(v, (list, tuple)):
                out += [str(x) for x in v]
            elif v:
                out.append(str(v))
    return [t for t in out if t]


def match(question: str, scene_id: str | None = None, *,
        graph: Any = None) -> dict[str, Any]:
    """消费侧问题 → 命中哪个已有图谱节点。

    返回：:
        {
          "decision": HIGH | MEDIUM | REJECTED,
          "max_score": int, "exact_match": bool, "meaningful_match": bool,
          "chunk_hits": int, "node_id": str, "node_label": str,
          "matched_on": str,
        }
    """
    q = str(question or "").strip()
    if not q:
        return {"decision": REJECTED, "max_score": 0, "exact_match": False,
                "meaningful_match": False, "chunk_hits": 0,
                "node_id": "", "node_label": "", "matched_on": ""}

    if scene_id is None:
        try:
            import scenes
            scene_id = scenes.current_scene_id()
        except Exception:
            scene_id = "A"
    sid = str(scene_id)

    if graph is None:
        try:
            import knowledge_graph as kg
            graph = kg.KnowledgeGraph.load()
        except Exception:
            graph = None
    if graph is None:
        return {"decision": REJECTED, "max_score": 0, "exact_match": False,
                "meaningful_match": False, "chunk_hits": 0,
                "node_id": "", "node_label": "", "matched_on": "图谱不可用"}

    q_norm = _norm(q)
    q_tokens = _tokens(q)
    best = {"decision": REJECTED, "max_score": 0, "exact_match": False,
            "meaningful_match": False, "chunk_hits": 0,
            "node_id": "", "node_label": "", "matched_on": ""}
    for nid, node in (getattr(graph, "nodes", {}) or {}).items():
        # 只在本场景命名空间内匹配（跨场景节点不算本场景的命中）
        if str(getattr(node, "root_scene", "A") or "A") != sid:
            continue
        # 根占位节点不参与（它不是可归的题）
        if str(getattr(node, "kind", "")) == "scene_root":
            continue
        score, exact, meaningful, chunk = _score_one(
            q_norm, q_tokens, _node_texts(node))
        if score > best["max_score"]:
            best = {"max_score": score, "exact_match": exact,
                    "meaningful_match": meaningful, "chunk_hits": chunk,
                    "node_id": str(nid),
                    "node_label": str(getattr(node, "label", "") or nid),
                    "matched_on": "text"}

    # 三级判定（照 neko:204-215）
    if best["max_score"] >= HIGH_CONF_MIN and best["exact_match"] \
            and best["meaningful_match"]:
        best["decision"] = HIGH
        best["matched_on"] = MATCH_EXACT
    elif best["max_score"] >= MEDIUM_CONF_MIN \
            and best["chunk_hits"] >= MEDIUM_CHUNK_HITS:
        best["decision"] = MEDIUM
        best["matched_on"] = MATCH_REPEATED
    else:
        best["decision"] = REJECTED
    return best


def route(question: str, scene_id: str | None = None, *,
            graph: Any = None) -> dict[str, Any]:
    """路由决策：命中 → 归入已有节点；未命中 → 新建节点（冷启动猜测）。

    这是设计案 §3.1 的落地。返回 decision + 归属 + 去向。
    """
    m = match(question, scene_id, graph=graph)
    if m["decision"] == REJECTED:
        m["action"] = "create_new_node"       # 未命中 → 新建图谱节点
        m["dest"] = "pending_dimensions"      # 先落待审区，解题成功才升活节点
    else:
        m["action"] = "absorb_into_existing"  # 命中 → 归入已有节点
        m["dest"] = "knowledge_graph"
    return m


# ---------------------------------------------------------------------------
# 待审区（同构 neko candidate_knowledge_items）
# ---------------------------------------------------------------------------
def _load_pending() -> dict[str, Any]:
    if not PENDING_DIMENSIONS.exists():
        return {}
    try:
        data = json.loads(PENDING_DIMENSIONS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_pending(data: dict[str, Any]) -> None:
    PENDING_DIMENSIONS.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_pending(question: str, scene_id: str, *, node_id: str = "",
                status: str = "pending_review", reason: str = "") -> dict[str, Any]:
    """未命中 → 先进待审区（不直接挂活图谱、不丢弃）。"""
    import hashlib
    import time
    h = hashlib.md5(question.strip().encode("utf-8")).hexdigest()[:8]
    tid = node_id or f"consume_{h}"
    data = _load_pending()
    item = data.get(tid) or {}
    item.update({
        "topic_id": tid,
        "question": question,
        "root_scene": str(scene_id),
        "status": status,
        "reason": reason or "检索图谱未命中（相似度低于双阈值）",
        "created_at": item.get("created_at") or int(time.time()),
        "updated_at": int(time.time()),
    })
    data[tid] = item
    _save_pending(data)
    return item


def update_pending_status(tid: str, status: str, *, note: str = "") -> bool:
    """人审通过 → accepted（升活节点）；拒绝 → rejected（留审计，不删）。"""
    data = _load_pending()
    item = data.get(tid)
    if item is None:
        return False
    item["status"] = str(status)
    item["updated_at"] = int(time.time())
    if note:
        item["review_note"] = note
    data[tid] = item
    _save_pending(data)
    return True


def pending_counts() -> dict[str, int]:
    """按 status 分组计数（仿 neko `candidate_status_counts`）。"""
    data = _load_pending()
    out: dict[str, int] = {}
    for v in data.values():
        if isinstance(v, dict):
            s = str(v.get("status") or "candidate")
        else:
            s = "candidate"
        out[s] = out.get(s, 0) + 1
    return out


def pending_for_scene(scene_id: str) -> list[dict[str, Any]]:
    """某场景名下的待审项。"""
    sid = str(scene_id)
    return [v for v in _load_pending().values()
            if isinstance(v, dict) and str(v.get("root_scene") or "") == sid]
