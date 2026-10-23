#!/usr/bin/env python3
"""外部 task 闭环：人导入 task → 消费态试答 → 失败记账 → 拆子目标（= 课程）→ 重试。

为什么单独一个模块
------------------
`ac45d72` 这版 MVE 的课程是**自发的**：`VLMLCurriculumAgent.propose_next_task()`
走里程碑树，学到哪算哪，没有"外面给的目标"。而泛化场景（算工资、客户数据……）
恰恰是先有一个**给定的 task**，课程是围绕它拆出来的。

本模块补的就是这一环，且只补这一环：

    人导入 task
        ↓  coach.explain(voyager=True)   ← 复用已学技能先答一次（消费态）
    成功 → 结束（能力已够，不用学）
    失败 → 记进 failed_tasks.json + 拆子目标
        ↓  VLMLCurriculumAgent.propose_next_task() 读它（同构原版把
           Failed tasks 拼进 curriculum prompt，见 voyager_original/voyager/
           prompts/curriculum.txt:22-23 + agents/curriculum.py:181-208）
    学一轮 → 每个子目标跑通就存一条 SQL 技能
        ↓  retry(task_id)  ← 才是验收点：复用学会的技能把原 task 答出来

同构关系（原版 → 这里）
-----------------------
    completed_tasks / failed_tasks（`curriculum.py:332-347`）  → 本模块的账本
    `clean_up_tasks` 完成后从 failed 剔除（`:349-370`）        → `mark_task_result`
    `decompose_task`（`:372`，LLM 拆 sub_goals）                → `subtopics_of`（图谱前置，不新造拆解器）
    `inference(task=...)` 人给任务入口（`voyager.py:403`）      → CLI `ask` / `retry`

数据源（用户 2026-10-23 定的判定）
----------------------------------
学习方向（VLML 的几个 MCP）不变，换的是数据源与阶段状态；但**数据源必须有**：
成功判据是「真取到数」，没有数据源时每一轮都取不到，学不到任何技能（`skill_store.add`
只在 success 后调）。所以本模块**不会**在没有数据源时假装能学 —— 取不到就如实记账。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

TASKS_PATH = _HERE / "failed_tasks.json"
MAX_SUBTOPICS = 6


# ---------------------------------------------------------------- 账本
def _load() -> list[dict[str, Any]]:
    if not TASKS_PATH.exists():
        return []
    try:
        data = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _save(rows: list[dict[str, Any]]) -> None:
    TASKS_PATH.write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _new_id() -> str:
    return f"t{int(time.time()) % 1000000:06d}"


def list_tasks(status: str = "") -> list[dict[str, Any]]:
    rows = _load()
    if status:
        rows = [r for r in rows if str(r.get("status")) == status]
    return rows


# ---------------------------------------------------------------- 拆子目标
def _graph():
    import topic_graph
    return topic_graph.get_graph()


def subtopics_of(question: str, topic_id: str = "") -> list[str]:
    """失败 task → 课程（子目标 id 列表）。

    优先级：调用方给的 `topic_id`（= `coach.explain` 归题/生成后的结果）→
    `tasks.infer_topic`（手写 hint + 自动 hint）→ `graph_topics.match_facts`
    （题干 → 指标评分）→ 兜底取有序课程的第一个可学节点。

    为什么会有"兜底"：泛化场景的常态是**问题落在图谱之外**（新域、新口径）。
    这时如实记 `why`，先让学习循环有事做；能不能解决，最终由 `retry` 证明，
    不靠这里假装匹配上了。

    前置也进子目标（`prerequisites`）—— 原版 `decompose_task` 拆 sub_goals 的角色，
    这里用图谱已有的前置边代替 LLM 拆解（可复现、不烧调用）。
    """
    try:
        g = _graph()
    except Exception:
        return []
    ids: list[str] = []

    tid = str(topic_id or "")
    if not tid:
        try:
            from tasks import infer_topic
            tid, _ = infer_topic(question)
        except Exception:
            tid = ""

    if tid and tid in g.topics:
        for p in (g.topics[tid].get("prerequisites") or []):
            pid = p if isinstance(p, str) else str((p or {}).get("id") or "")
            # 只有前置本身还没跑通时，它才是一条要学的子目标
            if pid and pid in g.topics and not g.mastery.is_completed(pid):
                ids.append(pid)
        ids.append(tid)

    if not ids:
        try:
            import graph_topics
            facts = [{"id": t["id"], "label": t.get("name") or t["id"]}
                     for t in g.topics.values()]
            ids = [m.get("id") or "" for m in
                   graph_topics.match_facts(facts, query=question, limit=3)]
        except Exception:
            ids = []

    if not ids:
        # 兜底：按有序课程拿第一个可学节点（前置已达成、未完成、未 stale）。
        try:
            sel = g.select_next()
            ids = [str(sel.get("topic_id") or "")]
        except Exception:
            ids = []

    return [i for i in dict.fromkeys(ids) if i][:MAX_SUBTOPICS]


def pending_subtopics(task: dict[str, Any]) -> list[str]:
    """这条 task 还没学会的子目标（已完成/已 stale 的剔掉）。

    与 `clean_up_tasks`（原版 `:349-370`）同一口径：**只做精确 `not in` 判定**，
    不算相似度（用户踩过：Jaccard≥0.8 会把它题的完成状态误判过来）。
    """
    try:
        g = _graph()
    except Exception:
        return []
    done = set(task.get("done_subtopics") or [])
    out: list[str] = []
    for tid in (task.get("subtopics") or []):
        if tid in done or tid not in g.topics:
            continue
        if g.mastery.is_completed(tid) or g.mastery.is_stale(tid):
            continue
        out.append(tid)
    return out


def next_focus() -> dict[str, Any] | None:
    """给课程规划用：最早一条 pending task 的第一个可学子目标。

    返回 {"task_id", "topic_id", "question", "why"} 或 None。
    同构原版 curriculum prompt 里的 `Failed tasks that are too hard: ...`
    —— 规划时看得见失败清单，才会围着它出下一个任务。
    """
    for task in _load():
        if str(task.get("status")) != "pending":
            continue
        subs = pending_subtopics(task)
        if subs:
            return {"task_id": str(task.get("task_id") or ""),
                    "topic_id": subs[0],
                    "question": str(task.get("question") or ""),
                    "why": f"外部 task 未解决（{len(subs)} 条子目标待学）"}
    return None


# ---------------------------------------------------------------- 记账
def _answer_once(question: str, *, voyager: bool = True) -> dict[str, Any]:
    """消费态答一次：**用已经学到的东西**答，不触发学习。"""
    import coach
    return asyncio.run(coach.explain(question, voyager=voyager))


def _ok(res: dict[str, Any]) -> tuple[bool, str]:
    """成功判据：真取到数（有工具轨迹 + 有事实）。

    ⚠️ 与学习循环同一条尺子（`voyager_vlml/loop.py:472-476` 的
    "成败 = 有没有真的从 DB 取到数"）：不能只看 answer 非空 —— 没有工具轨迹
    的"回答"是模型凭记忆写的，那种成功会把失败信号吃掉，课程就永远不会启动。
    """
    traj = res.get("trajectory") or []
    facts = res.get("facts") or []
    if not traj:
        return False, str(res.get("skipped") or "没有产生任何工具调用（取不到数）")
    if not facts:
        return False, "调了工具但没整理出事实（口径/字段对不上）"
    return True, ""


def ask(question: str, *, voyager: bool = True,
        topic_id: str = "") -> dict[str, Any]:
    """人导入一条 task，先答一次；答不出就记账并拆课。

    返回 {"task_id","ok","reason","subtopics","answer","status"}。
    """
    q = str(question or "").strip()
    if not q:
        return {"ok": False, "reason": "空问题", "task_id": "", "subtopics": []}
    res = _answer_once(q, voyager=voyager)
    ok, why = _ok(res)
    # 用 explain 归题/生成后的 topic_id 拆课：泛化场景的问题常落在图谱之外，
    # explain 会先把它种成节点，这里才拆得出子目标。
    subs = [] if ok else subtopics_of(q, str(res.get("topic_id") or topic_id or ""))
    row = {
        "task_id": _new_id(),
        "question": q,
        "topic_id": str(res.get("topic_id") or topic_id or ""),
        "created_at": _now(),
        "updated_at": _now(),
        "tries": 1,
        "status": "solved" if ok else "pending",
        "subtopics": subs,
        "done_subtopics": [],
        "last_reason": "" if ok else why,
        "answer": str(res.get("answer") or "")[:2000],
    }
    rows = _load()
    rows.append(row)
    _save(rows)
    return {"task_id": row["task_id"], "ok": ok, "reason": why,
            "subtopics": subs, "answer": row["answer"], "status": row["status"],
            "facts": len(res.get("facts") or []),
            "trajectory": list(res.get("trajectory") or [])[:6]}


def mark_subtopic_done(topic_id: str) -> int:
    """子目标跑通后回写（由 `update_exploration_progress` 调）。

    返回被更新的 task 条数；某条 task 的子目标全完成 → `ready_for_retry=True`
    （**不自动置 solved**：成功必须由 retry 真的答出来证明）。
    """
    tid = str(topic_id or "")
    if not tid:
        return 0
    rows = _load()
    n = 0
    for r in rows:
        if str(r.get("status")) != "pending":
            continue
        if tid not in (r.get("subtopics") or []):
            continue
        done = [x for x in (r.get("done_subtopics") or [])]
        if tid not in done:
            done.append(tid)
        r["done_subtopics"] = done
        left = [x for x in (r.get("subtopics") or []) if x not in done]
        r["ready_for_retry"] = not left
        r["updated_at"] = _now()
        n += 1
    if n:
        _save(rows)
    return n


def retry(task_id: str, *, voyager: bool = True) -> dict[str, Any]:
    """重试一条（失败的）task —— 验收点：这次用的是**学会的技能**。"""
    rows = _load()
    row = next((r for r in rows
                if str(r.get("task_id")) == str(task_id)), None)
    if row is None:
        return {"ok": False, "reason": f"没有这条 task：{task_id}"}
    q = str(row.get("question") or "")
    res = _answer_once(q, voyager=voyager)
    ok, why = _ok(res)
    row["tries"] = int(row.get("tries") or 0) + 1
    row["updated_at"] = _now()
    row["status"] = "solved" if ok else "pending"
    row["last_reason"] = "" if ok else why
    row["answer"] = str(res.get("answer") or row.get("answer") or "")[:2000]
    vctx = res.get("voyager_context") or {}
    row["reused_skills"] = [s.get("name", "") for s in (vctx.get("skills") or [])]
    _save(rows)
    return {"task_id": task_id, "ok": ok, "reason": why, "tries": row["tries"],
            "reused_skills": row["reused_skills"],
            "answer": row["answer"], "facts": len(res.get("facts") or []),
            "trajectory": list(res.get("trajectory") or [])[:6]}


# ---------------------------------------------------------------- CLI
def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def _main() -> int:
    ap = argparse.ArgumentParser(description="外部 task 闭环（导入 / 重试 / 列表）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask", help="人导入一条 task，先答一次（失败就记账拆课）")
    a.add_argument("question", nargs="+")
    a.add_argument("--no-voyager", action="store_true",
                   help="只用 VLML 编排答（默认用 Voyager 已积累的技能）")

    r = sub.add_parser("retry", help="重试一条失败 task（验收用）")
    r.add_argument("task_id")

    ls = sub.add_parser("list", help="列出账本")
    ls.add_argument("--status", default="")

    s = sub.add_parser("sub", help="看一条 task 拆出的子目标")
    s.add_argument("task_id")

    args = ap.parse_args()
    if args.cmd == "ask":
        _print(ask(" ".join(args.question), voyager=not args.no_voyager))
    elif args.cmd == "retry":
        _print(retry(args.task_id))
    elif args.cmd == "list":
        rows = list_tasks(args.status)
        _print([{k: r.get(k) for k in
                 ("task_id", "status", "tries", "question", "ready_for_retry",
                  "done_subtopics", "subtopics", "last_reason")} for r in rows])
    elif args.cmd == "sub":
        row = next((r for r in _load()
                    if str(r.get("task_id")) == args.task_id), None)
        if row is None:
            _print({"ok": False, "reason": f"没有这条 task：{args.task_id}"})
        else:
            _print({"task_id": args.task_id,
                    "subtopics": row.get("subtopics") or [],
                    "done": row.get("done_subtopics") or [],
                    "pending": pending_subtopics(row)})
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
