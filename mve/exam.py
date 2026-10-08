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


def profile_split() -> dict[str, dict[str, Any]]:
    """每题的**摸底基线**和**最新水平**分开返回 —— 不要只留最后一个数。

    `profile()` 只保留最后一次记录，语义没错（"现在是什么水平"），
    但单独用它看画像会出事：练完重考是 100%，它就把摸底的 19% **顶掉**，
    画像看上去像"本来就会"，学习曲线凭空消失。

    实测就是这样：服务器上跑完 7 题摸底（平均 19%）+ 8 次练后重考（全 100%）后，
    `exam.py --profile` 打印出来的是「全部 100% · 平均 100%」，
    而面板上按题分行显示的摸底均值还是 19% —— 同一个 exam_log，
    两个视图给出两个结论。基线必须留着：曲线是「基线 → 现在」，
    只剩"现在"就没有曲线了。

    基线**锚定 kind=="placement" 的第一条**，不是"第一条记录"：
    加了结业考（kind=="final"）之后，一道题的记录里可能既有摸底又有结业考，
    而且顺序不一定是摸底在前（先练后补摸底、或结业考被 --force 重跑时）。
    "第一条 == 摸底"是隐含假设，一旦破了，基线就会变成"练过之后的水平"。
    """
    out: dict[str, dict[str, Any]] = {}
    for r in load():
        t = str(r.get("topic_id") or "")
        if not t:
            continue
        cov = float(r.get("coverage") or 0.0)
        is_placement = str(r.get("kind") or "") == "placement"
        rec = out.get(t)
        if rec is None:
            rec = out[t] = {"topic_id": t, "baseline": cov, "latest": cov,
                            "exams": 0, "kind": "", "verdict": "",
                            "has_placement": False}
        # 基线只认摸底；还没有摸底记录之前先用第一条顶着，摸底一到就换掉
        if is_placement and not rec["has_placement"]:
            rec["baseline"] = cov
            rec["has_placement"] = True
        elif not rec["has_placement"] and rec["exams"] == 0:
            rec["baseline"] = cov
        rec["exams"] += 1
        rec["latest"] = cov
        rec["kind"] = str(r.get("kind") or "")
        rec["verdict"] = str(r.get("verdict") or "")
    for r in out.values():
        r["baseline_level"] = level_of(r["baseline"])
        r["latest_level"] = level_of(r["latest"])
        r["delta_pp"] = round((r["latest"] - r["baseline"]) * 100)
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


def transfer_weakest(limit: float = MASTERED_LIMIT) -> list[tuple[str, float]]:
    """迁移考核失败的知识点 —— **判据层信号**（Voyager 学到哪了）。

    transfer（未练题独立考核）考的是「练了别的题之后，这道**没练过**的题
    能不能独立做对」。覆盖率 < 掌握线 = 技能积累没能迁移到它 = 它正是当前
    最该补的薄弱点。与 `weakest()` 的区别：
      · weakest 读 placement/最新裸考 —— 学习前/后的基线水平
      · transfer_weakest 读 kind=="transfer" —— **学习后的迁移考核**，
        它失败才说明「学了，但没学会怎么用」。

    升序取最低（同 weakest：最弱优先，不挑最接近及格的）。
    """
    rows = [r for r in load() if str(r.get("kind") or "") == "transfer"]
    by: dict[str, float] = {}
    for r in rows:
        t = str(r.get("topic_id") or "")
        if t:
            by[t] = float(r.get("coverage") or 0.0)   # 后写覆盖先写 = 最近一次
    out = [(t, c) for t, c in by.items() if c < limit]
    out.sort(key=lambda kv: (kv[1], kv[0]))
    return out


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
    split = profile_split()
    if not split:
        print("还没有裸考记录。先跑：python mve/exam.py --all（全库摸底）")
        return
    print("=" * 82)
    print("  裸考画像（撤掉知识图谱后的真实水平 · 摸底基线 vs 最新水平）")
    print("=" * 82)
    print(f"  {'题':30} {'摸底':>6} → {'最新':>6}  {'Δ':>7}  {'最新等级':<6} 考核次数")
    print("  " + "-" * 78)
    for t in sorted(split, key=lambda k: (split[k]["baseline"], k)):
        r = split[t]
        d = r["delta_pp"]
        dmark = "—" if d == 0 else (f"+{d}pp" if d > 0 else f"{d}pp")
        print(f"  {t:30} {r['baseline'] * 100:>5.0f}% → {r['latest'] * 100:>5.0f}%  "
              f"{dmark:>7}  {r['latest_level']:<6} {r['exams']}")
    missing = unplaced()
    if missing:
        print(f"  （还没裸考过：{', '.join(missing)}）")
    base = [r["baseline"] for r in split.values()]
    late = [r["latest"] for r in split.values()]
    print("  " + "-" * 78)
    print(f"  摸底均值 {sum(base) / len(base) * 100:.0f}%  →  "
          f"最新均值 {sum(late) / len(late) * 100:.0f}%   "
          f"（Δ {(sum(late) / len(late) - sum(base) / len(base)) * 100:+.0f} 个百分点）")
    print(f"  摸底掌握 ≥80% 的 {sum(1 for c in base if c >= MASTERED_LIMIT)} 道 → "
          f"现在 {sum(1 for c in late if c >= MASTERED_LIMIT)} 道")
    print("  注：「最新」用的是每题最后一次考核。练后重考 100% 会把摸底顶掉，")
    print("      所以必须两列一起看 —— 只有最新那一列就没有学习曲线了。")


def _skills_count() -> int:
    try:
        return int(skill_store.stats().get("skills") or 0)
    except Exception:
        return 0


def placement_blocked(*, force: bool = False) -> str:
    """摸底的前提：**技能库必须是空的**。返回空串表示可以摸，否则返回原因。

    摸底测的是「零基础上的真实水平」。技能库里一旦有程序，考核轮就会重放它们
    —— 考出来的是「练过之后的水平」，不是裸考。而 `profile()` 取的是每题
    **最后一次**记录，所以这种假摸底会把真摸底**覆盖掉**，画像无声无息就废了。

    实测踩过两次：
      1) 第一次摸底时技能库残留 2 条 → max_losing_streak_map 摸出 100%，
         清库后重摸才是 0%（那 8 行记录作废）。
      2) 面板上点摸底又踩一次 → corrode_collapse 的摸底从 0% 被顶成 100%。

    所以这道闸必须是**硬**的：不提供"我记得住就行"的软提示。
    """
    if force:
        return ""
    n = _skills_count()
    if n:
        return (f"技能库里已经有 {n} 条程序 —— 现在考出来的是「练过之后的水平」，"
                f"不是裸考。要摸底先清库（python mve/reset_all.py，或面板上「清库」）")
    return ""


def final_blocked() -> str:
    """结业考的前提反过来：**技能库不能是空的**。返回空串表示可以考。

    摸底（`placement`）测的是「零基础上的真实水平」，所以要求技能库为空；
    结业考（`final`）测的是「学到现在，撤掉图谱还剩多少」——
    **恰恰要带着技能库考**，重放它自己攒下的程序才是这次考试的题意。

    技能库为空时结业考在数值上等于摸底，但它会以 kind="final" 落盘，
    把「这一列到底是基线还是结业」搅浑。所以这里也必须是硬闸。
    """
    n = _skills_count()
    if not n:
        return ("技能库是空的 —— 结业考测的是「学到现在还剩多少」，"
                "没学就没得考。先跑学习单元（python mve/learn.py --units 8），"
                "或先摸底（python mve/exam.py --all）")
    return ""


async def exam(topic_id: str, *, verbose: bool = True,
               kind: str = "practice_exam",
               extra: dict[str, Any] | None = None,
               force: bool = False) -> dict[str, Any]:
    """考一道题：撤掉图谱，只留题干 + 工具 + 自己的技能库。

    `kind` 区分四种考核，事后能对得上曲线是怎么涨的：
      placement     —— 摸底（清库后第一次全库考一遍，那时技能库是空的）
      practice_exam —— 练完这一题之后的重考（曲线上的正式数据点）
      transfer      —— **没练过的题**的重考。它的涨落回答的是：
                       "曲线上升是记住了这道题，还是真学会了能迁移"
      final         —— 结业考。**带着现有技能库**把全库再撤图谱考一遍，
                       回答"学到现在，去掉支架还剩多少"。与摸底的差别不是
                       代码路径，是**前提**：摸底要空库，结业考要有库。
    """
    task = tasks_mod.TASKS.get(topic_id)
    if task is None:
        return {"error": f"没有这道题：{topic_id}"}

    if kind == "placement":
        why = placement_blocked(force=force)
        if why:
            if verbose:
                print(f"  ✗ 摸底中止：{why}")
            return {"error": why, "blocked": True}
    elif kind == "final":
        why = final_blocked()
        if why:
            if verbose:
                print(f"  ✗ 结业考中止：{why}")
            return {"error": why, "blocked": True}

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
    KIND_MARK = {"placement": "摸底", "practice_exam": "重考",
                 "transfer": "迁移", "final": "结业"}
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
    split = profile_split()          # 基线锚定 placement，不靠"第一条"
    MARK = {"placement": "摸底", "practice_exam": "重考",
            "transfer": "迁移", "final": "结业"}
    print("=" * 82)
    print("  学习进程（按题分行：摸底 → 练后重考 → 结业考）")
    print("=" * 82)
    print(f"  {'题':28} {'摸底':>6}  →  之后的考核")
    print("  " + "-" * 76)
    deltas: list[float] = []
    finals: list[float] = []
    bases: list[float] = []
    for t in sorted(by_topic, key=lambda k: split[k]["baseline"]):
        seq = by_topic[t]
        base = split[t]["baseline"]
        bases.append(base)
        # 基线那一条自己不再进箭头（它就是箭头左边的数）
        rest = [r for r in seq
                if not (str(r.get("kind") or "") == "placement"
                        and float(r.get("coverage") or 0) == base)] or seq[1:]
        arrow = " → ".join(
            f"{float(r.get('coverage') or 0) * 100:.0f}%"
            f"·{MARK.get(str(r.get('kind') or ''), '·')}" for r in rest
        ) if rest else "（还没重考过）"
        mark = ""
        if rest:
            last_v = float(rest[-1].get("coverage") or 0)
            d = last_v - base
            deltas.append(d)
            mark = ("  ✅ +" if d > 0.001 else ("  ─  持平" if abs(d) <= 0.001
                                               else "  ⚠ ")) + \
                   (f"{d * 100:.0f}pp" if abs(d) > 0.001 else "")
        fs = [float(r.get("coverage") or 0) for r in seq
              if str(r.get("kind") or "") == "final"]
        if fs:
            finals.append(fs[-1])
        print(f"  {t:28} {base * 100:>5.0f}%  →  {arrow}{mark}")
    print("  " + "-" * 76)
    if deltas:
        up = sum(1 for d in deltas if d > 0.001)
        print(f"  重考过的 {len(deltas)} 道里 {up} 道涨了 · "
              f"平均 Δ {sum(deltas) / len(deltas) * 100:+.0f} 个百分点")
    if finals:
        print(f"  结业考 {len(finals)} 道 · 均值 {sum(finals) / len(finals) * 100:.0f}%"
              + (f"  （摸底均值 {sum(bases) / len(bases) * 100:.0f}% → "
                 f"Δ {(sum(finals) / len(finals) - sum(bases) / len(bases)) * 100:+.0f}pp）"
                 if bases else ""))
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
    ap.add_argument("--final", action="store_true",
                    help="全库结业考：带着现有技能库撤图谱再考一遍（不覆盖摸底基线）")
    ap.add_argument("--profile", action="store_true", help="打印裸考画像（真实水平）")
    ap.add_argument("--curve", action="store_true", help="打印考核流水")
    ap.add_argument("--progress", action="store_true", help="打印学习进程（按题分行）")
    ap.add_argument("--force", action="store_true",
                    help="强制重测（不管有没有摸过底、不管技能库是不是空的）")
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

    if args.final:
        # 结业考：**每道题都考**，不管考没考过 —— 它要的是"现在的全库水平"，
        # 不是"补没考过的"。与 --all 的差别正是这里：--all 只补没摸过的，
        # 且要求技能库为空；--final 全部重考，且要求技能库非空。
        #
        # 落盘 kind="final"，不覆盖摸底基线（`profile_split()` 的基线锚定
        # placement），所以摸底那一列跑完结业考后还在 —— 曲线不会凭空消失。
        why = final_blocked()
        if why:
            print(f"结业考中止：{why}")
            return 1
        ids = sorted(tasks_mod.TASKS)
        print(f"结业考：{len(ids)} 道题，带着现有技能库全部撤图谱考一遍\n")
        for t in ids:
            rec = asyncio.run(exam(t, kind="final"))
            if rec.get("error"):
                print(f"\n结业考中止：{rec['error']}")
                return 1
        print()
        progress()
        return 0

    if args.all:
        # 摸底只考**没裸考过**的题。全摸过了就**不再重考** ——
        # `profile()` 取每题最后一次记录，重考会把"零基础的摸底"覆盖成
        # "练过之后的水平"，画像无声无息就废了（实测踩过，见 placement_blocked）。
        ids = unplaced()
        if not ids and not args.force:
            print("题库里每道题都已经有裸考记录了 —— 不再重考。")
            print("  （重考会用「练过之后的水平」覆盖掉零基础的摸底值）")
            print("  要强制重测：python mve/exam.py --all --force")
            print("  要看现有画像：python mve/exam.py --profile")
            return 0
        if not ids:
            ids = sorted(tasks_mod.TASKS)
        print(f"摸底：{len(ids)} 道题，全部撤图谱考一遍\n")
        for t in ids:
            rec = asyncio.run(exam(t, kind="placement", force=args.force))
            if rec.get("error"):
                print(f"\n摸底中止：{rec['error']}")
                return 1
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
