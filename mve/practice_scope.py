#!/usr/bin/env python3
"""练习范围：把知识图谱上的一个维度钉成「接下来练什么」。

为什么要有它：
    面板上原本让人在驾驶舱里挑「模式 / 轮数 / 选题」，这些键钮本质是让人替出题器做
    决定。伴学的做法（onboarding.md:82）是相反的——**人在知识图谱上点一个知识点，
    把它设成练习范围，出题器只在范围里出题**。于是界面上只剩下开始 / 停止。

形态照伴学 `study_set_practice_scope`（entry_practice_scope_entries.py:117-173）：
  · 存的是 canonical `scope_key`，**读的时候重新校验**——图谱重建后旧维度可能已经
    不存在了，这时返回 invalidated + 原因，而不是拿旧值继续出题
    （伴学对应的错误码就是 PRACTICE_SCOPE_INVALIDATED）
  · `scope_revision` 单调递增：范围每改一次加一，用来判断"出题器有没有跟上最新范围"
  · 校验失败不抛穿：面板只读视图，一个失效的范围不该让 /api/state 整个 500

范围 → 题的映射以**题目注册表**为准，不以图谱节点为准：
    维度节点的 detail.topic_id 是图谱构建时"先到先得"记下的第一个题，
    而 map_fb_conv 这类维度跨两道题（fb_conversion_analysis 和 corrode_collapse 都要它）。
    所以真正的 topic 列表从 tasks 的 rubric 对账得来，图谱只负责**校验维度存在**。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SCOPE_FILE = ROOT / "practice_scope.json"
GRAPH_FILE = ROOT / "knowledge_graph.json"


class PracticeScopeError(Exception):
    """范围不可用。code 照伴学：PRACTICE_SCOPE_INVALIDATED。"""

    def __init__(self, message: str, code: str = "PRACTICE_SCOPE_INVALIDATED"):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# 只读来源（都不 import vlml_env —— 面板秒开，离线也能查）
# ---------------------------------------------------------------------------

def graph_dims() -> dict[str, dict[str, Any]]:
    """图谱里的维度 -> {topic_id, point, weight}。图没建过就返回空。"""
    if not GRAPH_FILE.exists():
        return {}
    try:
        g = json.loads(GRAPH_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for n in g.get("nodes") or []:
        if n.get("kind") != "dimension":
            continue
        d = n.get("detail") or {}
        out[str(n.get("label") or "")] = {
            "topic_id": str(d.get("topic_id") or ""),
            "point": str(d.get("point") or ""),
            "weight": d.get("weight"),
        }
    return out


def topics_for_dim(dim: str) -> list[str]:
    """覆盖这个维度的题（按题目注册表的 rubric 对账）。

    为什么不用图谱节点的 topic_id：见模块头 —— 那是"第一个题"，不是"所有题"。
    """
    try:
        import tasks
    except Exception:
        return []
    return sorted({
        t.topic_id for t in tasks.TASKS.values()
        if any(p.dimension == dim for p in t.rubric)
    })


# ---------------------------------------------------------------------------
# 存 / 读 / 清
# ---------------------------------------------------------------------------

def _read() -> dict[str, Any]:
    if not SCOPE_FILE.exists():
        return {}
    try:
        obj = json.loads(SCOPE_FILE.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write(state: dict[str, Any]) -> None:
    SCOPE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def set_scope(dim: str) -> dict[str, Any]:
    """把一个维度设成练习范围。维度必须在当前图谱里（canonical 校验）。"""
    dim = str(dim or "").strip()
    if not dim:
        raise PracticeScopeError("维度是空的", code="INVALID_PRACTICE_SCOPE")

    dims = graph_dims()
    if dim not in dims:
        raise PracticeScopeError(
            f"维度「{dim}」不在当前知识图谱里（图谱有 {len(dims)} 个维度）"
            + ("；先跑 python mve/knowledge_graph.py --build" if not dims else ""),
        )

    topics = topics_for_dim(dim)
    if not topics:
        raise PracticeScopeError(
            f"维度「{dim}」在图谱里，但没有任何题的 rubric 用到它——练不了",
        )

    prev = _read()
    info = dims[dim]
    state = {
        "active": True,
        "scope_key": f"dim:{dim}",
        "kind": "dimension",
        "label": dim,
        "topics": topics,
        "point": info["point"],
        "weight": info["weight"],
        "scope_revision": int(prev.get("scope_revision") or 0) + 1,
        "set_at": datetime.now().isoformat(timespec="seconds"),
        "source": "graph",
    }
    _write(state)
    return {**state, "ok": True}


def clear_scope() -> dict[str, Any]:
    """清除范围（伴学 onboarding.md:84：范围已掌握就清掉，回到出题器自选）。"""
    prev = _read()
    state = {
        "active": False,
        "scope_key": "",
        "kind": "",
        "label": str(prev.get("label") or ""),
        "topics": [],
        "scope_revision": int(prev.get("scope_revision") or 0) + 1,
        "set_at": "",
        "source": "graph",
    }
    _write(state)
    return {**state, "ok": True, "cleared": str(prev.get("label") or "")}


def get_scope() -> dict[str, Any]:
    """当前范围。存过的维度若已不在图谱里 → active=False + invalidated 原因。"""
    st = _read()
    if not st or not st.get("active"):
        return {"active": False, "label": str(st.get("label") or ""),
                "scope_revision": int(st.get("scope_revision") or 0)}

    dim = str(st.get("label") or "")
    dims = graph_dims()
    if dim not in dims:
        return {
            "active": False, "invalidated": True, "label": dim,
            "scope_key": str(st.get("scope_key") or ""),
            "reason": f"维度「{dim}」已不在当前知识图谱里（图谱重建过？）",
            "scope_revision": int(st.get("scope_revision") or 0),
        }

    # topics 每次重算：题目注册表可能改过，不信任落盘时那份
    topics = topics_for_dim(dim) or list(st.get("topics") or [])
    info = dims[dim]
    return {
        "active": True,
        "scope_key": f"dim:{dim}",
        "kind": "dimension",
        "label": dim,
        "topics": topics,
        "point": info["point"],
        "weight": info["weight"],
        "scope_revision": int(st.get("scope_revision") or 0),
        "set_at": str(st.get("set_at") or ""),
        "source": "graph",
    }


def resolve_topic(*, attempted: dict[str, int] | None = None,
                  failed: dict[str, int] | None = None) -> str:
    """范围内的下一题。没设范围就返回空串（出题器自选）。

    排序完全复用 tasks.next_topic（错题优先 → 没做过优先 → 难度升序），
    只是把候选集从"全部题"收窄成"范围内的题"。
    """
    sc = get_scope()
    if not sc.get("active") or not sc.get("topics"):
        return ""
    try:
        import run_log
        import tasks
        attempted = attempted if attempted is not None else run_log.attempted_counts()
        failed = failed if failed is not None else run_log.failed_counts()
        return tasks.next_topic(attempted=attempted, failed=failed,
                                only=list(sc["topics"]))
    except Exception:
        return str(sc["topics"][0])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> int:
    import sys

    args = sys.argv[1:]
    if not args or args[0] in ("--show", "-s"):
        sc = get_scope()
        print(json.dumps(sc, ensure_ascii=False, indent=2))
        return 0
    if args[0] == "--set" and len(args) >= 2:
        try:
            print(json.dumps(set_scope(args[1]), ensure_ascii=False, indent=2))
        except PracticeScopeError as e:
            print(json.dumps({"ok": False, "code": e.code, "error": str(e)},
                             ensure_ascii=False))
            return 1
        return 0
    if args[0] == "--clear":
        print(json.dumps(clear_scope(), ensure_ascii=False, indent=2))
        return 0
    if args[0] == "--resolve":
        print(resolve_topic())
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
