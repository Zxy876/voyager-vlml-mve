#!/usr/bin/env python3
"""面板数据层：把 run_log.jsonl 聚合成面板要显示的视图。

契约全部照猫娘伴学的 ui_api.py：
- 无证据 = unassessed + None，**不是 0%**（ui_api.py:41-47）
- 五档状态 weak/progress/good/mastered + unassessed（ui_api.py:62-73）
- flags 徽标 false_mastery / low_confidence（knowledge_tracker.py:298-299, :274）
- 掌握度变化显示成「掌握度 {before} -> {after}」（i18n zh-CN: ui.practice.mastery_delta_fmt）

VLML 没有前端（仓库内无 html/tsx/jsx），所以面板形态取自伴学，数据取自 MVE。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import run_log  # noqa: E402
import causal_timeline  # noqa: E402
import skill_store  # noqa: E402
import practice_scope  # noqa: E402

ROOT = Path(__file__).resolve().parent


def _task_library() -> list[dict[str, Any]]:
    """题目清单（不 import vlml_env，面板启动不拉 VLML）。"""
    try:
        import tasks
        return tasks.all_briefs()
    except Exception as e:
        return [{"topic_id": "?", "question": f"题目注册表加载失败：{e}",
                 "difficulty": 0, "key_points": [], "weights": {},
                 "dims": [], "has_answer_spec": 0}]


def _referee_view(topic_id: str) -> dict[str, Any]:
    """裁判（VLML0）的标准答案视图。

    只显示已经有了的（缓存命中）；没有就标 pending，不现场跑——
    面板是读视图，不该在渲染时触发一次 LLM 编排。
    """
    if not topic_id:
        return {"state": "pending", "facts": []}
    try:
        import vlml0_referee
        ans = vlml0_referee.cached(topic_id)
    except Exception as e:
        return {"state": "unavailable", "reason": f"{type(e).__name__}: {e}"[:160], "facts": []}
    if ans is None:
        return {"state": "pending", "facts": []}
    return {
        "state": "ready" if ans.ok else "empty",
        "facts": [
            {
                "subject": "/".join(f"{k}={v}" for k, v in (f.get("subject") or {}).items()),
                "dimension": f.get("dimension"),
                "value": f.get("value"),
                "base": f.get("base"),
                "source": (f.get("source") or {}).get("tool", ""),
            }
            for f in ans.facts
        ],
        "trajectory": ans.trajectory,
        "narrative": ans.narrative,
    }

# 五档 UI 状态（照 ui_api.py:62-73 的阈值）
def _ui_status(mastery: float | None, flags: list[str]) -> str:
    if mastery is None:
        return "unassessed"
    if "false_mastery" in flags or mastery < 0.40:
        return "weak"
    if mastery < 0.60:
        return "progress"
    if mastery < 0.80:
        return "good"
    return "mastered"


STATUS_LABEL = {
    "unassessed": "未评估",
    "weak": "薄弱",
    "progress": "进行中",
    "good": "熟练",
    "mastered": "已掌握",
}

FLAG_LABEL = {
    "false_mastery": "假性掌握",
    # V1 的二元 flag（attempts<3）。V2 已改成连续折扣，不再产生它；
    # 保留标签只为**旧日志**还能显示，新的快照不会再带这个。
    "low_confidence": "样本不足（旧 V1）",
    # 伴学 mastery_v2.py:285 —— 有没消化的错题，掌握度被封在 0.79
    "unresolved_wrong_cap": "有未消化错题",
    # V2 新增（mastery_v2.py:241、:274）
    "no_evidence": "没有证据",
    "zero_confidence_evidence": "证据权重为零",
}


def _retention_view() -> dict[str, dict[str, Any]]:
    """保持度（会忘）：每道题此刻还剩多少。

    V2 的投影只回答"现在掌握到什么程度"，这一层回答"过一阵还剩多少"。
    """
    try:
        import mastery_retention
        rows = mastery_retention.STORE.load()
        return {t: mastery_retention.STORE.current(t) for t in rows}
    except Exception:
        return {}


def _pct(v: float | None) -> str | None:
    return None if v is None else f"{v * 100:.0f}%"


def _exam_view() -> dict[str, Any]:
    """**真实水平曲线** —— 撤支架考核（exam_log.jsonl）。

    为什么面板上必须另画这一条：`coverage_track` 读的是 run_log，
    那是**带着知识图谱**练出来的覆盖率 —— 实测给着图谱首轮就 100%，
    连起来是一条从第一行就贴顶的平线。它量的是支架的高度，不是模型的水平。
    撤掉图谱重考出来的数（`exam.py`）才是学习曲线该画的东西。

    画法按**题分行**，不是首尾相连：第 3 个数据点可能是本来就会的题，
    第 4 个是本来 0% 的题，连成线的话形状由选题顺序决定，不由学习决定。
    """
    try:
        import exam
    except Exception:
        return {"has_data": False}
    rows = exam.load()
    if not rows:
        return {"has_data": False,
                "hint": "还没有裸考记录：python mve/exam.py --all（全库摸底）"}

    by_topic: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_topic.setdefault(str(r.get("topic_id") or ""), []).append(r)

    tracks: list[dict[str, Any]] = []
    for t, seq in by_topic.items():
        # 基线**锚定 placement**，不能取"第一条记录" —— 加了结业考（kind=final）
        # 之后，一道题的记录里顺序不再保证摸底在最前（补摸底、--force 重跑
        # 都会打乱）。"第一条 == 摸底"这个隐含假设一旦破了，基线就变成
        # "练过之后的水平"，学习曲线凭空消失。
        pl = [r for r in seq if str(r.get("kind") or "") == "placement"]
        anchor = pl[0] if pl else seq[0]
        base = float(anchor.get("coverage") or 0.0)
        rest = [r for r in seq if r is not anchor]
        rest_cov = [float(r.get("coverage") or 0.0) for r in rest]
        finals = [float(r.get("coverage") or 0.0) for r in seq
                  if str(r.get("kind") or "") == "final"]
        tracks.append({
            "topic_id": t,
            "baseline": base,
            "baseline_pct": _pct(base),
            "has_placement": bool(pl),
            "level": exam.level_of(base),
            "exam_count": len(seq),
            "after": [{"cov": c, "pct": _pct(c),
                       "kind": str(r.get("kind") or "")}
                      for c, r in zip(rest_cov, rest)],
            "delta_pp": round((rest_cov[-1] - base) * 100) if rest_cov else None,
            # 最新水平单独给一列：`profile()` 只留最后一次记录，练后重考 100%
            # 会把摸底顶掉。只有 baseline 那列会让人以为"练了个寂寞"，
            # 只有 latest 那列会让人以为"本来就会" —— 两列必须一起给。
            "latest": rest_cov[-1] if rest_cov else base,
            "latest_pct": _pct(rest_cov[-1] if rest_cov else base),
            "latest_level": exam.level_of(rest_cov[-1] if rest_cov else base),
            # 结业考单独一列：带着技能库全库再考一遍，回答"学到现在还剩多少"
            "final": finals[-1] if finals else None,
            "final_pct": _pct(finals[-1]) if finals else None,
            # 练了裸考也不涨 → 已毕业让位（伴学没有这条，MVE 自有）
            "exhausted": bool(exam.exhausted(t)),
        })
    tracks.sort(key=lambda x: (x["baseline"], x["topic_id"]))

    deltas = [x["delta_pp"] for x in tracks if x["delta_pp"] is not None]
    covs = [float(x["baseline"]) for x in tracks]
    lates = [float(x["latest"]) for x in tracks]
    transfer = [r for r in rows if str(r.get("kind") or "") == "transfer"]
    tseq = [float(r.get("coverage") or 0.0) for r in transfer]
    return {
        "has_data": True,
        "tracks": tracks,
        "avg_baseline": round(sum(covs) / len(covs) * 100) if covs else 0,
        "avg_latest": round(sum(lates) / len(lates) * 100) if lates else 0,
        "avg_delta_pp_all": (round((sum(lates) / len(lates) - sum(covs) / len(covs)) * 100)
                             if covs else 0),
        "retested": len(deltas),
        "rose": sum(1 for d in deltas if d > 0),
        "avg_delta_pp": round(sum(deltas) / len(deltas)) if deltas else None,
        "mastered": sum(1 for c in covs if c >= 0.80),
        "mastered_now": sum(1 for c in lates if c >= 0.80),
        # 结业考：带着现有技能库全库重考。它和「练后重考」的差别是覆盖面 ——
        # 练后重考只考刚练过的那一题，结业考是全库，所以均值才可比。
        "final": _final_view(tracks, covs),
        # 迁移对照：没练过的题涨不涨 —— 分辨「记住了这道题」和「真学会了」
        "transfer": {
            "count": len(tseq),
            "seq": [_pct(c) for c in tseq],
            "rose": bool(len(tseq) >= 2 and tseq[-1] > tseq[0] + 0.001),
        } if tseq else None,
    }


def _final_view(tracks: list[dict[str, Any]],
                baselines: list[float]) -> dict[str, Any] | None:
    """结业考汇总：全库带着技能库撤图谱重考一遍之后的水平。

    为什么必须单列：`latest` 那列是"每题最后一次考核"，练后重考 100% 也在里面，
    但它只覆盖了**练过的**题 —— 拿它当"现在的水平"会把没练过的题漏掉。
    结业考是**全库**，所以它的均值才和摸底均值可比（同一个分母）。
    """
    vals = [float(t["final"]) for t in tracks if t.get("final") is not None]
    if not vals:
        return None
    b = sum(baselines) / len(baselines) if baselines else 0.0
    f = sum(vals) / len(vals)
    return {
        "count": len(vals),
        "avg": round(f * 100),
        "avg_baseline": round(b * 100),
        "delta_pp": round((f - b) * 100),
        "mastered": sum(1 for v in vals if v >= 0.80),
        "covered_all": len(vals) == len(tracks),
    }


def _human_imports(limit: int = 10) -> list[dict[str, Any]]:
    """人导入的历史（直接读文件，不 import coach —— 那条会拉起整个 VLML）。

    助产士第二轮的原话：伴学面板里「学习者可以导入题干和内容以换得系统对此的
    记忆和解释」，我们与之同构，只是记忆与解释的提供方换成 VLML。
    """
    log = ROOT / "coach_log.jsonl"
    if not log.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-limit:]


def _db_view() -> dict[str, Any]:
    """当前指向哪个库（不 import vlml_env，避免面板启动就拉起整个 VLML）。"""
    try:
        import db_switch
        return {"path": db_switch.current_path(), "is_default": db_switch.is_default()}
    except Exception as e:
        return {"path": "", "is_default": True,
                "error": f"{type(e).__name__}: {e}"[:160]}


def _causal_view(limit: int = 25) -> dict[str, Any]:
    """行动因果时间线（control / attempt / referee / narrative 同一条单调序列）。

    面板要显示它是因为：人导入**不进掌握度**，如果面板只有掌握度视图，
    人就会觉得自己导入的东西石沉大海。这条线是「导入确实发生了」的唯一证据。
    """
    try:
        rows = causal_timeline.for_panel(limit)
        counts = causal_timeline.counts_by_topic()
    except Exception as e:
        return {"rows": [], "counts": {}, "reason": f"{type(e).__name__}: {e}"[:160]}
    return {
        "rows": rows,
        "counts": {
            t: {k: v for k, v in c.items() if v}
            for t, c in counts.items()
        },
        "reason": "",
    }


def _pilot_view() -> dict[str, Any]:
    """驾驶舱状态（面板只读，不持有进程句柄）。

    为什么不把进程挂在 dashboard 里：面板一重启句柄就丢了，人点过"启动"
    却查不到在不在跑，比不给按钮更糟。所以状态写在文件里，这里只读文件。
    """
    try:
        import pilot
        st = pilot.read()
        return {**st, "tail": pilot.tail(80)}
    except Exception as e:
        return {"running": False, "tail": "", "why": f"驾驶舱不可用：{type(e).__name__}: {e}"[:160]}


def _next_recommend() -> dict[str, Any]:
    """出题器当前推荐（带理由）。人导入之后这条应该跟着变。"""
    try:
        import planner
        return planner.next_brief()
    except Exception as e:
        return {"topic_id": "", "reason": "unavailable",
                "reason_label": "不可用", "explanation": f"{type(e).__name__}: {e}"[:160],
                "question": "", "difficulty": 0}


def probe_env() -> dict[str, Any]:
    """探测 VLML 是否可用。

    助产士第三轮的要求：MCP 连不上时面板必须说清楚，不能静默显示空。
    这里不做重试，只报状态 + 原因，由人决定下一步。
    """
    try:
        import vlml_env  # noqa: F401  (会 chdir + 打补丁)
        from vlml_env import get_database_info
        import asyncio

        info = asyncio.run(get_database_info())
        if isinstance(info, dict) and info.get("error"):
            return {"ok": False, "state": "degraded",
                    "reason": str(info["error"])[:200]}
        # 返回结构核实过：available_tables(list) / statistics(dict) / usage_tips(list)
        tables = list(info.get("available_tables") or [])
        stats = dict(info.get("statistics") or {})
        return {
            "ok": True,
            "state": "ready",
            "tables": len(tables),
            "events": stats.get("total_events") or stats.get("total_base_events"),
            "series": len(info.get("recent_series") or []),
            "usage_tips": len(info.get("usage_tips") or []),
            "reason": "",
        }
    except Exception as e:  # 任何失败都要能被面板显示出来
        return {"ok": False, "state": "unavailable",
                "reason": f"{type(e).__name__}: {e}"[:200]}


def build_state() -> dict[str, Any]:
    rows = run_log.load_all()
    memory = skill_store.load()
    # 防膨胀/防幻觉能不能自证，就看这两个数
    skill_stats = skill_store.stats()
    skill_detail = [
        {
            "key": s.get("key", ""),
            "topic": s.get("topic", ""),
            "name": s.get("name", ""),
            "text": s.get("text", ""),
            "hits": s.get("hits", 0),
            "ok": s.get("ok", 0),
            "versions": s.get("versions", 1),
        }
        for s in skill_store.all_skills()
    ]

    # 人导入通路：这两块在「还没跑过」时也要显示 —— 它正是让人开始的地方
    imports = _human_imports()
    recommend = _next_recommend()
    causal = _causal_view()
    pilot_view = _pilot_view()
    db = _db_view()
    # 练习范围（知识图谱页上点「练习此知识点」设的）。面板只读，不推断。
    scope = practice_scope.get_scope()

    if not rows:
        return {
            "has_data": False,
            "env": probe_env(),
            "db": db,
            "pilot": pilot_view,
            "practice_scope": scope,
            "imports": imports,
            "recommend": recommend,
            "causal": causal,
            "tasks": _task_library(),
            "skills": memory,
            "skill_stats": skill_stats,
            "skill_detail": skill_detail,
            # 摸底/考核走的是 exam_log.jsonl，**不写** run_log.jsonl（有意分开：
            # run_log 是带图谱的练习覆盖率，恒 100%，画出来是假平线）。
            # 所以"run_log 为空"≠"没有任何数据" —— 摸底跑完了一样要给看。
            # 漏掉这一条的实测后果：线上摸底跑完 7 道题，总览页仍显示
            # 「还没有任何运行记录」，真实水平曲线根本不出现。
            "exam_curve": _exam_view(),
            # 导航上的「轨迹 N 轮」读它；不给就是 "undefined 轮"
            "total_rounds": 0,
            "topic_id": "",
            "hint": ("还没有练习记录。摸底/考核不入 run_log（走 exam_log）——"
                     "有裸考数据时下面会照常显示。"),
        }

    # ---- 全局进度序列（跨运行连续编号）----
    series: list[dict[str, Any]] = []
    for i, r in enumerate(rows, start=1):
        flags = list(r.get("flags") or [])
        m = r.get("mastery")
        series.append({
            "i": i,
            "ts": r.get("ts", ""),
            "mode": r.get("mode", ""),
            # topic_id 必须带上：导出 md 的「掌握度（按题）」和轮次明细都靠它
            # 分组/显示。之前只映射了 "task"（命令行传的 --topic，常为空），
            # 结果导出里题目那一列全是空的。
            "topic_id": str(r.get("topic_id") or r.get("task") or ""),
            "task": r.get("task", ""),
            "round": r.get("round"),
            "coverage": r.get("coverage"),
            "score": r.get("score"),
            "verdict": r.get("verdict", ""),
            "evidence": r.get("evidence_status", ""),
            "mastery": m,
            "mastery_pct": _pct(m),
            "ui_status": _ui_status(m, flags),
            "status_label": STATUS_LABEL[_ui_status(m, flags)],
            "level": r.get("level"),
            "flags": [{"key": f, "label": FLAG_LABEL.get(f, f)} for f in flags],
            # V2 新增：证据条数 / recency（时间衰减后的新鲜度）/ 保持度
            "evidence_count": r.get("mastery_evidence_count"),
            "mastery_recency": r.get("mastery_recency"),
            "retention": _retention_view().get(str(r.get("topic_id") or "")),
            "facts": r.get("facts_count"),
            "judge": r.get("judge", ""),
            # 叙事段（comparable 恒 False，不参与比对）
            "narrative": r.get("narrative", ""),
            "insights": r.get("insights") or [],
            "caveats": r.get("caveats") or [],
            "narrative_comparable": bool(r.get("narrative_comparable", False)),
            "narrative_based_on": r.get("narrative_based_on", 0),
            "no_tool_calls": bool(r.get("no_tool_calls")),
            "unjudgeable": r.get("unjudgeable") or [],
            "referee_facts": r.get("referee_facts"),
            "trajectory": r.get("trajectory") or [],
            "covered": r.get("covered") or [],
            "missing": r.get("missing") or [],
            "rejected": r.get("rejected_low_base") or [],
            "skill_count": r.get("skill_count"),
            # 防幻觉：本轮被溯源校验丢掉的事实
            "hallucinations": r.get("hallucinations") or [],
            "hallucination_count": int(r.get("hallucination_count") or 0),
        })

    hallucination_total = sum(s["hallucination_count"] for s in series)

    # ---- 判读：**撤支架考核优先** ----
    #
    # 这一段原来只看 run_log 的覆盖率轨迹，判出来的话是错的 —— 实测：
    # 服务器跑完 7 题摸底（平均 19%）+ 8 次练后重考（全 100%），
    # 导出的「判读」写的是「首轮即满分：这个任务对模型太简单，测不出学习曲线」。
    # 依据的那 8 个 100% 全是**带着知识图谱**练出来的（实测给着图谱首轮就满分），
    # 量的是支架的高度，不是模型的水平。真实情况是 19% → 100%，涨了 81pp。
    #
    # 所以判读顺序改成：有撤支架考核数据就以它为准，run_log 那条降级为附注。
    cov = [s["coverage"] for s in series if s["coverage"] is not None]
    mas = [s["mastery"] for s in series if s["mastery"] is not None]
    exam_curve = _exam_view()

    if exam_curve.get("has_data") and exam_curve.get("retested"):
        b = exam_curve["avg_baseline"]
        l = exam_curve["avg_latest"]
        n = exam_curve["retested"]
        up = exam_curve["rose"]
        d = exam_curve["avg_delta_pp"] or 0
        # 「+84pp」里的加号会被 export_md 的 escape_markdown 转义成 "\+84pp"，
        # 用「涨/退」两个字代替符号。
        word = "涨" if d > 0 else "退"
        if d > 0:
            reading = "learned"
            reading_text = (
                f"撤支架考核：摸底 {b}% → 练后重考 {l}%（{n} 道重考里 {up} 道涨，"
                f"平均{word} {abs(d)}pp）—— 真实水平上升了。"
            )
        elif d < 0:
            reading = "regressed"
            reading_text = (f"撤支架考核：摸底 {b}% → 练后重考 {l}%（平均 {word} {abs(d)}pp）"
                            f"—— 反而退步，检查技能库是否引入了错误经验。")
        else:
            reading = "flat"
            reading_text = (f"撤支架考核：摸底 {b}% → 练后重考 {l}%（持平）—— "
                            f"练了没涨，学习未发生或任务已饱和。")
    elif exam_curve.get("has_data"):
        reading = "insufficient"
        reading_text = (f"已摸底 {exam_curve['avg_baseline']}%（{len(exam_curve['tracks'])} 道），"
                        f"还没重考过 —— 跑「学习单元」后才有 Δ。")
    elif len(cov) >= 2:
        first_full = cov[0] >= 1.0
        if first_full:
            reading = "too_easy"
            reading_text = ("练习首轮即满分：注意这是**带着知识图谱**跑的，量的是支架不是水平。"
                            "要判学没学会，得跑摸底 + 练后重考（撤支架考核）。")
        elif cov[-1] > cov[0]:
            reading = "learned"
            reading_text = f"覆盖率 {cov[0]:.0%} → {cov[-1]:.0%}：补齐缺失后做得更全了。"
        elif cov[-1] < cov[0]:
            reading = "regressed"
            reading_text = f"覆盖率 {cov[0]:.0%} → {cov[-1]:.0%}：反而退步了，检查技能库是否引入了错误经验。"
        else:
            reading = "flat"
            reading_text = f"覆盖率稳定在 {cov[-1]:.0%}：没有变化，学习未发生或任务已饱和。"
    else:
        reading = "insufficient"
        reading_text = "样本不足（少于 2 次），无法判读进步。"

    # mastery 上升不能当证据：V2 里 confidence 随**证据权重之和**上升，
    # 权重会被时间衰减和评价可信度拉低，但**刷轮次依然能把它推高**。
    mas_warning = (
        "mastery 的 confidence = 1-exp(-证据权重和/4)，反复作答仍会把它推高；"
        "只看它判定「学会了」会得出假阳性。判据必须用覆盖率。"
        if len(mas) >= 2 else None
    )

    # ---- 编排轨迹是否变复杂（「学会编排」的直接证据）----
    traj_sizes = [len(s["trajectory"]) for s in series]
    tool_sets = [set(s["trajectory"]) for s in series]
    new_tools: list[str] = []
    for i in range(1, len(tool_sets)):
        new_tools += sorted(tool_sets[i] - tool_sets[i - 1])

    last = series[-1]
    prev_mastery = series[-2]["mastery"] if len(series) >= 2 else None

    attempted = run_log.attempted_counts()
    failed = run_log.failed_counts()
    topic_id = str(rows[-1].get("topic_id") or "")

    return {
        "has_data": True,
        "env": probe_env(),
        "db": db,
        "pilot": pilot_view,
        "practice_scope": scope,
        "question": rows[-1].get("question", ""),
        "topic_id": topic_id,
        # 出题器：照伴学 practice_scope 的选题排序
        "referee": _referee_view(topic_id),
        "tasks": _task_library(),
        # 人导入通路：导入过什么 + 出题器因此改成推哪题
        "imports": imports,
        "recommend": recommend,
        "causal": causal,
        "attempted": attempted,
        "failed": failed,
        "runs": len({(r.get("ts", "")[:10]) for r in rows}),
        "total_rounds": len(series),
        "series": series,
        "reading": reading,
        "reading_text": reading_text,
        "mastery_warning": mas_warning,
        "coverage_track": [_pct(c) for c in cov],
        "mastery_track": [_pct(m) for m in mas],
        "trajectory_sizes": traj_sizes,
        "new_tools": sorted(set(new_tools)),
        "skills": memory,
        # 防膨胀：写入次数 vs 实际条目（bloat=1 表示每次写入都成了新条目，即没挡住）
        "skill_stats": skill_stats,
        "skill_detail": skill_detail,
        # 防幻觉：被溯源校验拦下的编造数字
        "hallucination_total": hallucination_total,
        "mastery_delta": (
            f"掌握度 {_pct(prev_mastery)} -> {_pct(last['mastery'])}"
            if prev_mastery is not None and last["mastery"] is not None
            else f"掌握度 {_pct(last['mastery'])}"
        ),
        # 真实水平曲线（撤支架考核）—— 学习曲线该看的那一列
        # （上面判读已经算过一次，这里复用，别再读一遍 exam_log）
        "exam_curve": exam_curve,
    }


def as_json() -> str:
    return json.dumps(build_state(), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    print(as_json())
