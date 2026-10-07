#!/usr/bin/env python3
"""**撤支架考核** —— 学习曲线的真正数据源。

为什么必须单开一个文件
----------------------
练习时给着知识图谱，模型一轮就 100%（实测：清库 + 撤提示 `--hint=none`
仍首轮满分）。那不是"学会了"，是**支架托着它对**。把这种 100% 连成线，
画出来是一条从第一行就贴顶的平线 —— 好看，但假。

支架式教学（scaffolding）的终点本来就是"撤掉支架后仍会做"。所以：

    练习轮：图谱开（给口径、列含义、骨架、典型错法）—— 这是教
    考核轮：图谱关 —— 只剩题干 + 工具目录 + **它自己攒下的技能库**

考核轮的覆盖率才是"学到了多少"的可观测量。它能上升，才叫学习曲线。

伴学这边**没有**对应的实现（它的掌握度是练出来的，不区分"靠帮助做对"
和"靠自己做对" —— 只在 `used_hint` 上打了 0.85 折）。这里把"撤支架"
做成一次**真实的重考**，不是折算。

产物
----
`exam_log.jsonl`：一行一次考核。
    {"at":..., "topic_id":..., "coverage":..., "verdict":...,
     "skills": <当时技能库条数>, "practice_rounds": <累计练习轮次>}

把 `skills` 一起记下来是为了分辨两种上升：
  · 技能库涨了 + 考核涨了 → 真学会了（技能被复用）
  · 技能库没涨但考核涨了 → 只是模型运气好/题干熟了，不算数

跑法
----
    python mve/exam.py --topic max_losing_streak_map      # 考一次
    python mve/exam.py --all                              # 全库考一遍（摸底）
    python mve/exam.py --profile                          # 打印裸考画像（真实水平）
    python mve/exam.py --curve                            # 打印已有考核曲线

**裸考覆盖率 = 这道题的真实水平**，出题器（planner）读的就是这个数 ——
它取代的原本是"练习覆盖率"，而练习是带着图谱做的（实测恒 100%，
那是支架托着的，不是水平）。见 `planner._true_level()`。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

EXAM_LOG = HERE / "exam_log.jsonl"

import vlml_env  # noqa: F401,E402  必须先引导环境
import voyager as voyager_mod  # noqa: E402
from loop_core import evaluate_vs_referee  # noqa: E402
import run_log  # noqa: E402
import skill_store  # noqa: E402
import tasks as tasks_mod  # noqa: E402
import vlml0_referee  # noqa: E402


def _practice_rounds() -> int:
    """累计练习轮次（不含考核）—— 考核点画在 x 轴的哪个位置。"""
    try:
        return len([l for l in run_log.LOG.read_text(
            encoding="utf-8", errors="replace").splitlines() if l.strip()])
    except Exception:
        return 0


def append(rec: dict[str, Any]) -> None:
    with EXAM_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load() -> list[dict[str, Any]]:
    if not EXAM_LOG.exists():
        return []
    return [json.loads(l) for l in EXAM_LOG.read_text(
        encoding="utf-8", errors="replace").splitlines() if l.strip()]


# ---- 裸考画像（真实水平）----
#
# 为什么另立一套「水平」，不用练习日志里的覆盖率：练习是**带着知识图谱**做的，
# 图谱把口径、列含义、结构骨架全喂进 prompt —— 实测给着图谱首轮就 100%，
# 撤掉图谱平均只有 29%。所以练习覆盖率是「支架的高度」，不是「模型的水平」。
# 出题器要匹配的是水平，所以必须读这里。
#
# 等级标签照抄伴学 `knowledge_tracker.py:240-250`，一个字不改：
#   <0.20 未接触 / <0.40 薄弱 / <0.60 进行中 / <0.80 熟练 / ≥0.80 掌握
LEVELS = ((0.20, "未接触"), (0.40, "薄弱"), (0.60, "进行中"),
          (0.80, "熟练"), (1.01, "掌握"))

# 伴学 `get_weak_topics()` 的薄弱线（knowledge_tracker.py:2073：`mastery < 0.60`）
WEAK_LIMIT = 0.60
# 伴学「掌握」线（difficulty_policy / mastery_v2 都用 0.80）
MASTERED_LIMIT = 0.80


def level_of(cov: float) -> str:
    for edge, name in LEVELS:
        if cov < edge:
            return name
    return "掌握"


def profile() -> dict[str, dict[str, Any]]:
    """每题**最近一次**撤支架考核的结果（后写的覆盖先写的）。

    没有记录的题不出现在这里 —— 那是「未接触」，不是「0 分」。
    两者的处置不同：未接触要先摸底，0 分是要练。
    """
    out: dict[str, dict[str, Any]] = {}
    for r in load():
        t = str(r.get("topic_id") or "")
        if not t:
            continue
        rec = dict(r)
        rec["coverage"] = float(rec.get("coverage") or 0.0)
        rec["level"] = level_of(rec["coverage"])
        out[t] = rec
    return out


def true_level(topic_id: str) -> float | None:
    """这道题的真实水平；**没裸考过返回 None**（不是 0.0）。

    None 与 0.0 必须分开：0.0 是「考过，一分没拿」，None 是「还没考」。
    伴学里对应的是「新知识点初始掌握度硬 0.0」
    （`adaptive_learning/mastery_v2.py:240-256`）—— 它那边没有"考没考过"
    这一维，因为每个知识点一建档就是 0.0。MVE 多出这一维是因为考核要真跑
    一次模型，成本高，不能每题都先考一遍。
    """
    p = profile().get(topic_id)
    return None if p is None else float(p["coverage"])


def unplaced() -> list[str]:
    """题库里**还没裸考过**的题 —— 摸底（`--all`）就是要把它们考一遍。"""
    prof = profile()
    return sorted(t for t in tasks_mod.TASKS if t not in prof)


def weakest(limit: float = WEAK_LIMIT) -> list[tuple[str, float]]:
    """照伴学 `get_weak_topics()`（knowledge_tracker.py:2073-2084）。

    两条规矩，都是照抄不是改编：
      1) 只收 `真实水平 < 0.60` 的（伴学是 `mastery < 0.60` 或 false_mastery）
      2) **升序取最低** —— 最弱优先，不是"最接近阈值"优先。
         后者看着更有效率（一步就能推过线），但那是给分数打工：
         最弱的永远排不上，梯度从底下就断了。伴学选的是前者。
    """
    prof = profile()
    rows = [(t, float(p["coverage"])) for t, p in prof.items()
            if t in tasks_mod.TASKS and float(p["coverage"]) < limit]
    rows.sort(key=lambda kv: (kv[1], kv[0]))
    return rows


def exhausted(topic_id: str) -> bool:
    """这道题**练了也没涨** → 该毕业让位，别把算力全砸在死题上。

    判据：考过 ≥2 次，且最近一次不比上一次高。
    同构的是伴学「错题做对了才消除」之外那条 MVE 自有的 `STALE_LIMIT`
    （planner.py:46）—— 那里是「练习没进展就毕业」，这里是「**裸考**没进展
    就毕业」。用裸考更硬：练习是带支架的，涨不涨都说明不了什么。
    """
    rows = [r for r in load() if str(r.get("topic_id") or "") == topic_id]
    if len(rows) < 2:
        return False
    return float(rows[-1].get("coverage") or 0) <= float(rows[-2].get("coverage") or 0)


def print_profile() -> None:
    prof = profile()
    if not prof:
        print("还没有裸考记录。先跑：python mve/exam.py --all（全库摸底）")
        return
    print("=" * 74)
    print("  裸考画像（撤掉知识图谱后的真实水平）")
    print("=" * 74)
    print(f"  {'题':30} {'真实水平':>8}  {'等级':<6}  {'判定':<10} 考核次数")
    print("  " + "-" * 70)
    counts: dict[str, int] = {}
    for r in load():
        counts[str(r.get("topic_id"))] = counts.get(str(r.get("topic_id")), 0) + 1
    for t in sorted(prof, key=lambda k: float(prof[k]["coverage"])):
        p = prof[t]
        print(f"  {t:30} {float(p['coverage']) * 100:>7.0f}%  "
              f"{str(p.get('level') or ''):<6}  {str(p.get('verdict') or ''):<10}"
              f" {counts.get(t, 0)}")
    missing = unplaced()
    if missing:
        print(f"  （还没裸考过：{', '.join(missing)}）")
    cov = [float(p["coverage"]) for p in prof.values()]
    print("  " + "-" * 70)
    print(f"  平均 {sum(cov) / len(cov) * 100:.0f}%   最低 {min(cov) * 100:.0f}%   "
          f"最高 {max(cov) * 100:.0f}%   "
          f"（掌握 ≥80% 的 {sum(1 for c in cov if c >= MASTERED_LIMIT)} 道）")


async def exam(topic_id: str, *, verbose: bool = True,
               kind: str = "practice_exam",
               extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """考一道题：撤掉图谱，只留题干 + 工具 + 自己的技能库。

    `kind` 区分三种考核，事后能对得上曲线是怎么涨的：
      placement     —— 摸底（清库后第一次全库考一遍，那时技能库是空的）
      practice_exam —— 练完这一题之后的重考（曲线上的正式数据点）
      transfer      —— **没练过的题**的重考。它的涨落回答的是：
                       "曲线上升是记住了这道题，还是真学会了能迁移"
    """
    task = tasks_mod.TASKS.get(topic_id)
    if task is None:
        return {"error": f"没有这道题：{topic_id}"}

    prev = voyager_mod.GRAPH_STATE["on"]
    voyager_mod.GRAPH_STATE["on"] = False        # ← 撤支架
    try:
        ref = await vlml0_referee.answer(task)
        v = voyager_mod.LLMVoyager(blind=False, hint_level="none")
        result = await v.run(task)
        facts = result.get("facts") or []
        tool_calls = sum(1 for t in v.trajectory if not str(t.get("error") or ""))
        ev = evaluate_vs_referee(facts, task.rubric, ref.facts,
                                 tool_calls=tool_calls)
        rec = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "topic_id": topic_id,
            "coverage": round(float(ev.coverage), 4),
            "verdict": str(ev.verdict),
            "score": int(ev.score),
            "missing": list(ev.missing_points)[:3],
            "skills": int(skill_store.stats().get("skills") or 0),
            "practice_rounds": _practice_rounds(),
            "reused_skill": bool(result.get("reused_skill")),
        }
        append(rec)
        if verbose:
            mark = "✅" if rec["coverage"] >= 1.0 else (
                "◐" if rec["coverage"] > 0 else "✗")
            print(f"  {mark} 考核 {topic_id:26} 覆盖率 {rec['coverage'] * 100:5.0f}%  "
                  f"判定 {rec['verdict']:9} 技能库 {rec['skills']} 条"
                  + ("（复用程序）" if rec["reused_skill"] else ""))
            if rec["missing"]:
                print(f"      缺失：{rec['missing']}")
        return rec
    finally:
        voyager_mod.GRAPH_STATE["on"] = prev     # 一定还原，别污染练习轮


def curve() -> None:
    rows = load()
    if not rows:
        print("还没有考核记录。先跑 python mve/exam.py --topic <题>")
        return
    KIND_MARK = {"placement": "摸底", "practice_exam": "重考", "transfer": "迁移"}
    print("=" * 78)
    print("  撤支架考核曲线（练习给图谱，考核撤图谱）")
    print("=" * 78)
    print(f"  {'#':>2}  {'类型':<4} {'练习轮次':>8}  {'技能库':>6}  {'覆盖率':>7}  题")
    print("  " + "-" * 72)
    for i, r in enumerate(rows, 1):
        print(f"  {i:>2}  {KIND_MARK.get(str(r.get('kind') or ''), '·'):<4} "
              f"{r.get('practice_rounds', 0):>8}  "
              f"{r.get('skills', 0):>6}  {float(r.get('coverage') or 0) * 100:>6.0f}%  "
              f"{r.get('topic_id')}"
              + ("  ⟲复用程序" if r.get("reused_skill") else ""))
    cov = [float(r.get("coverage") or 0) for r in rows]
    print("  " + "-" * 72)
    print(f"  轨迹：{[f'{c * 100:.0f}%' for c in cov]}")
    if len(cov) >= 2:
        print(f"  首 {cov[0] * 100:.0f}% → 末 {cov[-1] * 100:.0f}%  "
              f"（Δ {(cov[-1] - cov[0]) * 100:+.0f} 个百分点）")


def progress() -> None:
    """**学习进程**视图：把摸底当基线，只看之后的正式数据点涨没涨。

    为什么不能直接用 `curve()`：curve 把每一次考核首尾相接连成一条线，
    而这里的数据点是「不同的题」—— 第 3 个点可能是 pistol（本来就会），
    第 4 个点可能是 corrode（本来 0%）。把它们连起来，线的形状是选题顺序
    决定的，不是学习决定的。
    所以正确画法是：**按题分行**，每行是这道题自己的摸底 → 重考 → 重考。
    """
    rows = load()
    if not rows:
        return
    by_topic: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_topic.setdefault(str(r.get("topic_id") or ""), []).append(r)
    print("=" * 78)
    print("  学习进程（按题分行：摸底 → 练后重考）")
    print("=" * 78)
    print(f"  {'题':28} {'摸底':>6}  →  之后的重考")
    print("  " + "-" * 72)
    deltas: list[float] = []
    for t in sorted(by_topic, key=lambda k: float(by_topic[k][0].get("coverage") or 0)):
        seq = by_topic[t]
        base = float(seq[0].get("coverage") or 0)
        rest = [float(r.get("coverage") or 0) for r in seq[1:]]
        arrow = " → ".join(f"{c * 100:.0f}%" for c in rest) if rest else "（还没重考过）"
        mark = ""
        if rest:
            d = rest[-1] - base
            deltas.append(d)
            mark = ("  ✅ +" if d > 0.001 else ("  ─  持平" if abs(d) <= 0.001
                                               else "  ⚠ ")) + \
                   (f"{d * 100:.0f}pp" if abs(d) > 0.001 else "")
        print(f"  {t:28} {base * 100:>5.0f}%  →  {arrow}{mark}")
    print("  " + "-" * 72)
    if deltas:
        up = sum(1 for d in deltas if d > 0.001)
        print(f"  重考过的 {len(deltas)} 道里 {up} 道涨了 · "
              f"平均 Δ {sum(deltas) / len(deltas) * 100:+.0f} 个百分点")
    # 迁移对照：没练过的题有没有跟着涨 —— 这才是"学会了"而不是"记住了"
    tr = [r for r in rows if str(r.get("kind") or "") == "transfer"]
    if tr:
        seq = [float(r.get("coverage") or 0) for r in tr]
        verdict = ("涨了 → 有迁移" if seq[-1] > seq[0] + 0.001 else
                   "没涨 → 上升的是记忆，不是迁移")
        shown = " → ".join(f"{c * 100:.0f}%" for c in seq)
        print(f"  迁移对照（{len(tr)} 次未练题考核）：{shown}  {verdict}")


def _main() -> int:
    ap = argparse.ArgumentParser(description="撤支架考核：学习曲线的真正数据源")
    ap.add_argument("--topic", default="", help="考哪道题")
    ap.add_argument("--all", action="store_true", help="全库摸底（没考过的考一遍）")
    ap.add_argument("--profile", action="store_true", help="打印裸考画像（真实水平）")
    ap.add_argument("--curve", action="store_true", help="打印考核流水")
    ap.add_argument("--progress", action="store_true", help="打印学习进程（按题分行）")
    ap.add_argument("--clear", action="store_true", help="清空考核记录")
    args = ap.parse_args()

    if args.clear:
        if EXAM_LOG.exists():
            EXAM_LOG.unlink()
        print("已清空考核记录")
        return 0
    if args.curve:
        curve()
        return 0
    if args.profile:
        print_profile()
        return 0
    if args.progress:
        progress()
        return 0

    if args.all:
        # 摸底只考**没裸考过**的题：重跑不会覆盖已有画像，
        # 也避免把"练过之后的重考"和"零基础的摸底"混在同一条曲线里。
        ids = unplaced() or sorted(tasks_mod.TASKS)
        print(f"摸底：{len(ids)} 道题，全部撤图谱考一遍\n")
        for t in ids:
            asyncio.run(exam(t, kind="placement"))
        print()
        print_profile()
        return 0
    if not args.topic:
        ap.print_help()
        return 1
    asyncio.run(exam(args.topic))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
