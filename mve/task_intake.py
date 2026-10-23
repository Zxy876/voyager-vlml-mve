#!/usr/bin/env python3
"""外部 task 闭环：人导入 task → 消费态试答 → 失败记账 → 拆子目标（= 课程）→ 重试。

为什么单独一个模块
------------------
`ac45d72` 这版 MVE 的课程是**自发的**：`VLMLCurriculumAgent.propose_next_task()`
走里程碑树，学到哪算哪，没有"外面给的目标"。而泛化场景（算工资、客户数据……）
恰恰是先有一个**给定的 task**，课程是围绕它拆出来的。

本模块补的就是这一环，且只补这一环：

    人导入 task
        ↓  Voyager 用自己的技能库答一次（消费态；`VLMLActionAgent.solve`，
           技能库空 → 直接判失败，不烧调用）
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
_ROOT = _HERE.parent
# 两个都要：`mve/` 给 topic_graph/skill_store，仓库根给 voyager_vlml。
# 只加 `_HERE` 时，当 CLI 直接跑会 ModuleNotFoundError: voyager_vlml。
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

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
    """列出账本。`ready_for_retry` **读时现算**，不用账本里的快照。

    为什么现算：它取决于"还有没有能学的子目标"，而那是 mastery 的实时状态
    （练够次数 → stale → 该子目标自动退出 pending）。写进账本就会和 mastery
    对不上 —— 实测旧账本里存的是 false，可实际 pending 已经空了、就该重试了。
    项目铁律：状态一律反查，不重复记账。
    """
    rows = _load()
    for r in rows:
        if str(r.get("status")) == "pending":
            r["ready_for_retry"] = _ready_now(r)
    if status:
        rows = [r for r in rows if str(r.get("status")) == status]
    return rows


# ---------------------------------------------------------------- 拆子目标
def _graph():
    import topic_graph
    return topic_graph.get_graph()


def resolve_topic(question: str, topic_id: str = "") -> str:
    """这条题归到哪个知识点（= 技能检索的键）。

    ⚠️ 检索键必须是**本题的归题结果**：`skill_store.retrieve(topic, …)` 里
    `topic` 完全相等才给 3.0 的同题分；跨题只靠词面相似度过 0.12 的门槛。
    实测这道题：`sim=0.021` → 传空串/传别的 topic 都召回不到，只有传
    `vlml_first_blood` 才拿得到那条配方。所以归题结果必须落到账本上。
    """
    tid = str(topic_id or "")
    if tid:
        return tid
    try:
        from tasks import infer_topic
        tid, _ = infer_topic(question)
    except Exception:
        tid = ""
    return str(tid or "")


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

    tid = resolve_topic(question, topic_id)

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


def skipped_subtopics(task: dict[str, Any]) -> list[str]:
    """这条 task 里**当前学不动**的子目标（= 原版 failed_tasks 的等价物）。

    原版 `update_exploration_progress`（`curriculum.py:340-344`）失败就把任务塞进
    `failed_tasks`，并打印 "Skipping to next task"；下一轮 curriculum 的 human message
    里带着 `Failed tasks that are too hard: …`（`curriculum.txt:23`），选课器据此避开。

    这里**不另存一份账**：直接从 mastery 反查 `stale`（练够 MAX_ATTEMPTS_PER_TOPIC 次
    仍没跑通）。项目铁律：状态一律反查，不重复记账（两本账必然对不上）。
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
        if g.mastery.is_completed(tid):
            continue
        if g.mastery.is_stale(tid):
            out.append(tid)
    return out


def next_focus() -> dict[str, Any] | None:
    """给课程规划用：最早一条 pending task 的第一个可学子目标。

    返回 {"task_id", "topic_id", "question", "why"} 或 None。
    同构原版 curriculum prompt 里的 `Failed tasks that are too hard: ...`
    —— 规划时看得见失败清单，才会围着它出下一个任务。

    挑哪一条：用**图谱那套课程序**（`graph.curriculum_key`，与 `select_next` 同一个键），
    不另造排序。学不动的（stale）已被 `pending_subtopics` 排除，自然换下一条 ——
    这正是原版 `update_exploration_progress` 失败即 "Skipping to next task" 的行为。
    """
    for task in _load():
        if str(task.get("status")) != "pending":
            continue
        subs = pending_subtopics(task)
        if not subs:
            continue
        try:
            g = _graph()
            ms = g.trunk_milestones()
            subs = sorted(
                subs,
                key=lambda tid: g.curriculum_key(
                    g.topics.get(tid) or {"id": tid}, milestones=ms))
        except Exception:
            pass
        why = f"外部 task 未解决（{len(subs)} 条子目标待学"
        skipped = skipped_subtopics(task)
        if skipped:
            why += f"，已放弃 {len(skipped)} 条太难：{'、'.join(skipped[:3])}"
        why += "）"
        return {"task_id": str(task.get("task_id") or ""),
                "topic_id": subs[0],
                "question": str(task.get("question") or ""),
                "why": why}
    return None


# ---------------------------------------------------------------- 记账
def _answer_once(question: str, *, topic_id: str = "",
                 subtopics: list[str] | None = None,
                 use_skills: bool = True) -> dict[str, Any]:
    """消费态答一次：**用 Voyager 自己的技能库**答（不是让 VLML 专家代答）。

    为什么不用 `coach.explain`
    --------------------------
    `coach.explain` 走的是 VLML 编排（外部专家）：库里有的数它都能取，几乎不会失败。
    而这条闭环要证明的是「**Voyager 学会了**」，不是「VLML 会查」。所以消费态必须是
    Voyager 自己写 SQL —— 它答不出，才是学习信号。

    检索范围 = 本题的归题结果 + 它的子目标。
    为什么不能只用一个 topic：技能是按「学它的那个节点」存的，而一条外部 task 要的
    往往是几个节点（子目标）合起来。实测：传空 topic 时 `sim=0.021 < 0.12` 门槛，
    库里明明有那条配方也召回不到 → retry 直接判"还没学过"，复用链等于断的。

    技能库为空时**直接判失败、不烧一次调用**：一条配方都没有，写出来的 SQL 只能是
    现场凭记忆编的（那正是这套学习要消灭的东西）。这条也正是验收线「初始 task 必 fail」
    的机制来源：新场景技能库是空的。
    """
    skills: list[dict[str, Any]] = []
    if use_skills:
        try:
            import skill_store
            keys = [topic_id] + [str(t) for t in (subtopics or [])]
            seen: set[str] = set()
            for t in dict.fromkeys([k for k in keys if k]):
                for s in skill_store.retrieve(t, question, top_k=4):
                    k = str(s.get("key") or "")
                    if k and k not in seen:
                        seen.add(k)
                        skills.append(s)
        except Exception:
            skills = []
        if not skills:
            return {"ok": False, "trajectory": [], "rows": 0, "sql": "",
                    "skills": [],
                    "skipped": "技能库里没有与它相关的配方（还没学过）"}

    from voyager_vlml.agents.action import VLMLActionAgent

    # ⚠️ 消费态**不传**图谱节点的 hints。试过传（口径提示走原版 Context 槽），
    #    结果那条提示正好指着 0 行的 `agg_first_blood_stats`（"粒度是 round_id /
    #    字段 fb_team"）→ 模型乖乖去查空表 → 纯聚合返回 1 行 (NULL,0) → 判真空失败。
    #    hints 是"学的时候该往哪看"，不是"答的时候必须这么看"；答的时候靠 schema
    #    （已标"0 行·别查"）+ 学到的配方，口径由 critic 判，不靠 hints 灌。
    act = VLMLActionAgent()
    obs, sql = asyncio.run(act.solve(question, max_iters=3, skills=skills))
    # 成败与结论都用 **agent 自己那一趟的最后一轮判决**，不再另起一个 critic 重判：
    # 重判既多烧一次 LLM，还可能跟前一步的 critic 意见相反（同一条结果两个判词）。
    ok = bool(getattr(act, "last_success", False))
    crit = str(getattr(getattr(act, "critic", None), "last_critique", "") or "")
    reasoning = str(getattr(act, "answer", "") or "")
    return {
        "ok": ok,
        "trajectory": [str(o.get("tool") or "") for o in obs if o.get("ok")],
        "rows": sum(int(o.get("rows") or 0) for o in obs if o.get("ok")),
        "sql": sql,
        "skills": [{"name": s.get("key") or s.get("name") or ""} for s in skills],
        # 答案优先用 critic 的 reasoning（原版 JSON 的同一个字段，判成功时它写着结论句），
        # 没有才退回 SQL —— 以前这里恒为 SQL，于是"取到 13 行"永远变不成"69.2%"这句话。
        "answer": (reasoning or sql)[:400],
        "answer_source": "critic_reasoning" if reasoning else "sql",
        "skipped": "" if ok else str(crit or "没取到数"),
    }


def _ok(res: dict[str, Any]) -> tuple[bool, str]:
    """成功判据：真取到数。

    ⚠️ 与学习循环同一条尺子（`voyager_vlml/loop.py:472-476` 的
    "成败 = 有没有真的从 DB 取到数"）：不能只看 answer 非空 —— 没有工具轨迹
    的"回答"是模型凭记忆写的，那种成功会把失败信号吃掉，课程就永远不会启动。
    """
    if "ok" in res:                      # Voyager 通路（critic 已经判过）
        return bool(res.get("ok")), str(res.get("skipped") or "")
    # 兼容 coach.explain 的返回（有轨迹 + 有事实才算）
    traj = res.get("trajectory") or []
    facts = res.get("facts") or []
    if not traj:
        return False, str(res.get("skipped") or "没有产生任何工具调用（取不到数）")
    if not facts:
        return False, "调了工具但没整理出事实（口径/字段对不上）"
    return True, ""


def ask(question: str, *, use_skills: bool = True,
        topic_id: str = "") -> dict[str, Any]:
    """人导入一条 task，先答一次；答不出就记账并拆课。

    返回 {"task_id","ok","reason","subtopics","answer","status"}。
    """
    q = str(question or "").strip()
    if not q:
        return {"ok": False, "reason": "空问题", "task_id": "", "subtopics": []}
    # 先归题再答：归题结果既是技能检索的键，也是拆课的依据（拆课本身不烧 LLM）。
    tid = resolve_topic(q, topic_id)
    subs = subtopics_of(q, tid)
    res = _answer_once(q, topic_id=tid, subtopics=subs, use_skills=use_skills)
    ok, why = _ok(res)
    if ok:
        subs = []          # 答出来了就不用学，别留一份"待学"账
    row = {
        "task_id": _new_id(),
        "question": q,
        "topic_id": tid,
        "created_at": _now(),
        "updated_at": _now(),
        "tries": 1,
        "status": "solved" if ok else "pending",
        "subtopics": subs,
        "done_subtopics": [],
        # 拆不出子目标 = 没有能学的了，直接交给人重试（原版：学不了就跳过）
        "ready_for_retry": not subs,
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


def _ready_now(row: dict[str, Any]) -> bool:
    """「没有还能学的子目标了」→ 该交给人去 retry。

    同构原版 `learn()`（`voyager.py:326-385`）：**它从不等所有任务成功**。
    某个任务失败就 `failed_tasks.append` + 打印 "Skipping to next task"（`:340-344`），
    学习继续往下走 —— 原版根本不存在"必须全部 completed 才允许继续"这道门。

    所以这里的门槛是「pending 为空」，不是「subtopics 全在 done 里」。
    后者会让一条学不动的子目标（stale）把整条外部 task 永久钉死在 pending：
    实测 `vlml_fb_conversion` 挂在 0 行的 `agg_first_blood_stats` 上，
    学 3 次 stale 后再也不被选中，但 `done` 里没有它 → `retry` 永远不可达。
    """
    return not pending_subtopics(row)


def note_subtopic_result(topic_id: str, success: bool) -> int:
    """子目标跑完一轮后记账 —— 同构原版 `update_exploration_progress`（`:332-347`）。

    成功 → 记进 `done_subtopics`（原版 `completed_tasks.append`）；
    失败 → **不写任何"失败账"**：失败本身就是 `mastery.attempts+1`（原版
           `failed_tasks.append` 的等价物），练够次数据就 stale，
           `pending_subtopics` 自然把它排除 —— 那就是"跳过"。
    两条路都要重算 `ready_for_retry`，否则"全失败的 task"永远等不到人重试
    （原版每轮结束都无条件调 `update_exploration_progress(info)`，成功失败都记账）。

    返回被更新的 task 条数。
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
        if success:
            done = list(r.get("done_subtopics") or [])
            if tid not in done:
                done.append(tid)
            r["done_subtopics"] = done
        r["ready_for_retry"] = _ready_now(r)
        r["updated_at"] = _now()
        n += 1
    if n:
        _save(rows)
    return n


def mark_subtopic_done(topic_id: str) -> int:
    """兼容壳：子目标跑通后回写（等价 `note_subtopic_result(tid, True)`）。"""
    return note_subtopic_result(topic_id, True)


def retry(task_id: str, *, use_skills: bool = True) -> dict[str, Any]:
    """重试一条（失败的）task —— 验收点：这次用的是**学会的技能**。"""
    rows = _load()
    row = next((r for r in rows
                if str(r.get("task_id")) == str(task_id)), None)
    if row is None:
        return {"ok": False, "reason": f"没有这条 task：{task_id}"}
    q = str(row.get("question") or "")
    res = _answer_once(q, topic_id=str(row.get("topic_id") or ""),
                       subtopics=list(row.get("subtopics") or []),
                       use_skills=use_skills)
    ok, why = _ok(res)
    row["tries"] = int(row.get("tries") or 0) + 1
    row["updated_at"] = _now()
    row["status"] = "solved" if ok else "pending"
    row["last_reason"] = "" if ok else why
    row["answer"] = str(res.get("answer") or row.get("answer") or "")[:2000]
    row["reused_skills"] = [str(s.get("name") or "") for s in (res.get("skills") or [])]
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
    a.add_argument("--no-skills", action="store_true",
                   help="不要求复用技能库（现场写 SQL，等于让专家代答一次）")

    r = sub.add_parser("retry", help="重试一条失败 task（验收用）")
    r.add_argument("task_id")

    ls = sub.add_parser("list", help="列出账本")
    ls.add_argument("--status", default="")

    s = sub.add_parser("sub", help="看一条 task 拆出的子目标")
    s.add_argument("task_id")

    args = ap.parse_args()
    if args.cmd == "ask":
        _print(ask(" ".join(args.question), use_skills=not args.no_skills))
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
                    "skipped": skipped_subtopics(row),
                    "pending": pending_subtopics(row),
                    "ready_for_retry": _ready_now(row)})
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
