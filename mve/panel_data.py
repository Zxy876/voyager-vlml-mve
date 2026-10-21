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
import threading
import time
from pathlib import Path
from typing import Any

import run_log  # noqa: E402
import causal_timeline  # noqa: E402
import skill_store  # noqa: E402
import practice_scope  # noqa: E402

ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# build_state 的单飞 + 短缓存（2026-10-21 黑客松面板挂死取证后补）
#
# 事故形态：前端每 4 秒轮询 /api/state，每次 build_state 里 probe_env 都要
# `asyncio.run(get_database_info())` 开一个 duckdb 连接。第一个请求一慢
# （内存吃紧 / duckdb 争锁），后续轮询**并发堆线程**——ThreadingHTTPServer
# 来者不拒，几十个线程各自开 duckdb、各自算一遍完整 state，内存冲到 3.2G
# 开始换页 → 更慢 → 更多堆积。接口永远不返回，浏览器侧就是
# "Unexpected end of JSON input"（进程被 py-spy 实锤：几十个线程卡在
# probe_env / _load_raw，77 个 duckdb fd 同时开着）。
#
# 治法：同一时刻只允许一个 build 在跑（单飞），其余请求直接给**上一份结果**；
# 算完的缓存 3 秒内直接复用。数据是日志聚合，3 秒内的旧值人眼看不出差别。
# ---------------------------------------------------------------------------
_STATE_CACHE: dict[str, Any] = {"at": 0.0, "value": None}
_STATE_LOCK = threading.Lock()
_STATE_TTL = 3.0

# 单飞的闸门：同一时刻只允许一个 build 在算，其余请求等它算完拿同一份。
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: dict[str, Any] = {"on": False, "done": threading.Event()}

# probe_env 每次都要新开一个 duckdb 连接（vlml db/manager.py 即开即关），
# 而 /api/state 一个 build 里要调两次；换库/切场景之外没必要那么勤。15s 足够。
_ENV_CACHE: dict[str, Any] = {"at": 0.0, "value": None}
_ENV_TTL = 15.0


def build_state_invalidate() -> None:
    """改动了账目（学习落盘/清库/切场景）之后叫一声，让下一轮强制重算。"""
    with _STATE_LOCK:
        _STATE_CACHE["at"] = 0.0
        _ENV_CACHE["at"] = 0.0


def _task_library() -> list[dict[str, Any]]:
    """题目清单（不 import vlml_env，面板启动不拉 VLML）。"""
    try:
        import tasks
        return tasks.all_briefs()
    except Exception as e:
        return [{"topic_id": "?", "question": f"题目注册表加载失败：{e}",
                 "difficulty": 0, "key_points": [], "weights": {},
                 "dims": [], "has_answer_spec": 0}]


def _mc_thinking_view() -> dict[str, Any]:
    """MC 思路学习入库情况：4 条域无关规划思维（topic=mc-thinking）是否已进共享库。

    面板「技能库」区据此显示「MC 思维入库 N 条」，让"思维积累入库"可见、可证。
    """
    items = [s for s in skill_store.all_skills() if s.get("topic") == "mc-thinking"]
    skill_md = ROOT / "thought_store" / "SKILL.md"
    return {
        "count": len(items),
        "items": [
            {"name": s.get("name", ""), "text": (s.get("text", "") or "")[:90],
             "hits": s.get("hits", 0), "ok": s.get("ok", 0),
             "versions": s.get("versions", 1)}
            for s in items
        ],
        "skill_md": str(skill_md) if skill_md.exists() else "",
    }


def _referee_view(topic_id: str) -> dict[str, Any]:
    """裁判（VLML0）的标准答案视图 —— 双层隔离（2026-10-12 纠偏）。

      · `facts` / `narrative` = 客观裁判（DB 基准），Voyager 比对与面板判分都用它；
      · `judge` = 给人看的 VLML 视角（叙事 + LLM 估算），**仅供参考、不参与判分**。

    只显示已经有了的（缓存命中）；没有就标 pending，不现场跑——
    面板是读视图，不该在渲染时触发一次 LLM 编排。
    """
    if not topic_id:
        return {"state": "pending", "facts": []}
    try:
        import vlml0_referee
        ans = vlml0_referee.cached(topic_id)
        judge = vlml0_referee.cached_judge(topic_id)
    except Exception as e:
        return {"state": "unavailable", "reason": f"{type(e).__name__}: {e}"[:160], "facts": []}
    if ans is None:
        return {"state": "pending", "facts": []}

    def _fmt(f: dict) -> dict:
        return {
            "subject": "/".join(f"{k}={v}" for k, v in (f.get("subject") or {}).items()),
            "dimension": f.get("dimension"),
            "value": f.get("value"),
            "base": f.get("base"),
            "source": (f.get("source") or {}).get("tool", ""),
        }

    judge_view: dict[str, Any] = {}
    if judge:
        judge_view = {
            "narrative": judge.get("narrative", ""),
            "llm_facts": [_fmt(f) for f in (judge.get("llm_facts") or [])],
        }
    return {
        "state": "ready" if ans.ok else "empty",
        "facts": [_fmt(f) for f in ans.facts],
        "trajectory": ans.trajectory,
        "narrative": ans.narrative,
        "judge": judge_view,
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

    为什么面板上必须另画这一条：以前那条 `coverage_track`（已下架）读的是 run_log，
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
                "hint": "还没有裸考记录：python mve/exam.py --all（全库摸底）",
                # 教学图谱（伴学出题/图谱移植）：无数据时也展示知识点图谱骨架
                "teaching": _teaching_payload()}

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
            # 迁移成功率：未练题独立考核里真正「独立做对」的占比。
            # 考核记录没有 success 布尔，coverage≥1.0 就是它的达成形态
            # （全部评分点覆盖且值正确）。这是判据区最硬的信号。
            "ok_count": sum(1 for c in tseq if c >= 1.0),
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


def _human_imports(limit: int = 10,
                   scene_id: str | None = None) -> list[dict[str, Any]]:
    """人导入的历史（直接读文件，不 import coach —— 那条会拉起整个 VLML）。

    助产士第二轮的原话：伴学面板里「学习者可以导入题干和内容以换得系统对此的
    记忆和解释」，我们与之同构，只是记忆与解释的提供方换成 VLML。

    1→n：`scene_id` 非空时只返回该场景的导入记录（先过滤再取末 N 条——
    反过来会让别的场景的新记录把本场景的挤出末 N 条）。旧记录没有
    `root_scene` 字段（1→n 改造前的导入）按默认场景 `A` 计，不丢历史。
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
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if scene_id is not None and str(rec.get("root_scene") or "A") != str(scene_id):
            continue
        out.append(rec)
    return out[-limit:]


def _ability_universe_size() -> int:
    """能力全集的大小（掌握度的**分母**）：图谱节点 + **消费侧**登记的能力项。

    ⚠️ 为什么排掉练习线的 `runtime_*`：`runtime_topics.json` 是练习线
    （`voyager_vlml/loop.py:_save_runtime_topic`）与消费线
    （`voyager_vlml/consume.py:_runtime_topic_id`）**共用的同一个文件**，
    线上 16 项里 **12 项是 `source="practice"`**。练习线登记的题是
    "课程打算练的"，不是"人问过的"，算进分母只会稀释掌握度 —— 而且消费侧
    大概率永远不会答那些题，等于分母里躺着 12 道拿不到的分。

    与 `consume._ability_universe()` 同口径（那里是集合、这里是大小）。
    """
    n = 0
    try:
        import topic_graph
        n = len(topic_graph.TopicGraph().topics)
    except Exception:
        n = 0
    try:
        p = ROOT / "runtime_topics.json"
        if p.exists():
            reg = json.loads(p.read_text(encoding="utf-8"))
            n += sum(1 for k, v in (reg or {}).items()
                     if str(k).startswith("consume_")
                     or str((v or {}).get("source") or "") == "consume")
    except Exception:
        pass
    return n


def _consume_curve(limit: int = 40,
                   scene_id: str | None = None) -> list[dict[str, Any]]:
    """**消费进步曲线** —— 用户 2026-10-13 定：「测 Voyager 的进步曲线和掌握度
    全在消费这测就好了」。

    为什么是消费而不是练习：练习（run_log）不判分 —— VLML 的尺子不能进 Voyager
    的反馈环，所以那条线只有「取没取到数」，画出来的曲线是"链路通不通"，
    不是"水平高不高"。消费（人问一句 → Voyager 用已积累的能力答）才是**交付**：
    人真的要一个答案，这时候用 VLML0 裁判独立跑一遍量出覆盖率，才是真实的进步曲线。

    ⚠️ **只有消费侧算掌握度**（用户 2026-10-18 再次钉死）：本函数只读
    `coach_log.jsonl` 里 `mode=voyager_consume` 的记录 —— 练习线（run_log /
    MasteryStore）一条都不读。练习跑通只推进课程规划，不算能力变大
    （照原版 Voyager `curriculum.py:339` 只在成功时 append）。

    曲线画的是**累计已掌握数**（`mastered_total`）—— 掌握不是覆盖率折算的分数，
    而是「答完了 → 这题进能力库 → 掌握」（用户 2026-10-13 定义）。
    所以进步 = 能力集合在变大，y 轴是"会做的题有几道"（原版口径
    `curriculum.py:135 len(completed_tasks)`）。

    ⚠️ `mastered_total` **在这里重算**，不信记录里当时算的那个数：口径一改，
    历史记录里写死的旧数不会自己更新。重算规则就是原版那句：答出来了 +1，
    失败不计。

    ⚠️ **场景隔离**（1→n）：`scene_id` 非空时只保留该场景的记录 —— 切到
    Scene B 时曲线必须只画 B 的导入历史，A 的历史混进来就会把"B 从零开始学"
    画成"接在 A 的尾声上"，Δ 掌握数虚高。旧记录没有 `root_scene` 字段
    （1→n 改造前的导入）按默认场景 `A` 计（与 consume() 的 `_scene_id="A"`
    一致），不丢历史。
    ⚠️ 场景模式先多读再过滤：全局取末 N 条再过滤，会因 A 的记录太多把 B 的
    早期记录挤出末 N 条（曲线前段凭空消失）。

    覆盖率另记一条（这次答得全不全的质量分，不是掌握度）。老的人导入
    （纯 VLML 解释）没有这些字段，一律跳过 —— 不能拿 None 补 0，那会把
    "没测"画成"测得 0 分"。
    """
    out: list[dict[str, Any]] = []
    # y 轴用累计掌握数（原版口径），`universe` 只作"总共多少题"的参照显示。
    # 场景模式不给 universe —— 全局分母对单场景没有意义（B 才导入 3 题，
    # 拿 A 的 360 题当分母，掌握度恒 ≈0，读起来像"没学会"）。
    universe = _ability_universe_size() if scene_id is None else None
    # 场景模式多读一些再过滤，避免末 N 条被别的场景挤占
    # （过滤口径统一在 _human_imports(scene_id=…)，不在这里重复一遍）
    recs = [r for r in _human_imports(limit=limit * 10 if scene_id else limit,
                                      scene_id=scene_id)
            if r.get("mode") == "voyager_consume"]
    recs = recs[-limit:]
    book: set[str] = set()
    for i, rec in enumerate(recs, start=1):
        # 累计：只有**答出来了**才进能力集合（失败不计入，照原版 :339）
        if rec.get("mastered") and str(rec.get("topic_id") or "").strip():
            book.add(str(rec.get("topic_id")).strip())
        cov = rec.get("coverage")
        out.append({
            "i": i,
            "ts": rec.get("ts", ""),
            "question": str(rec.get("question") or "")[:80],
            "topic_id": str(rec.get("topic_id") or ""),
            "coverage": float(cov) if cov is not None else None,
            "score": rec.get("score"),
            "verdict": str(rec.get("verdict") or ""),
            # 掌握（能力集合）—— 在这**重算**，不信记录里写死的旧口径数
            "mastered": bool(rec.get("mastered")),
            "mastered_total": len(book),
            "ability_total": (max(universe, len(book))
                              if universe is not None else None),
            "skill_action": str(rec.get("skill_action") or ""),
            "reused": bool(rec.get("reused")),
            # —— 1→n 场景归属 + 命中判定 + 判定结果（曲线点上的证据）——
            "root_scene": str(rec.get("root_scene") or "A"),
            "route_decision": str(rec.get("route_decision") or ""),
            "route_score": int(rec.get("route_score") or 0),
            "judgment_result": str(rec.get("judgment_result") or ""),
            "node_status": str(rec.get("node_status") or ""),
            "cold_start": bool(rec.get("cold_start")),
            # —— 方法与做法的比对结论（判分的依据，得让人看见"凭什么判这个分"）——
            "judge": str(rec.get("judge") or ""),
            "method_reason": str(rec.get("method_reason") or ""),
            "method_diff": str(rec.get("method_diff") or ""),
            "same_metric": bool(rec.get("same_metric")),
            "voyager_sound": bool(rec.get("voyager_sound")),
            "judged": bool(rec.get("judged")),
            "referee_rows": int(rec.get("referee_rows") or 0),
            "coverage_referee": (float(rec["coverage_referee"])
                                 if rec.get("coverage_referee") is not None else None),
            "verdict_referee": str(rec.get("verdict_referee") or ""),
            "rows": int(rec.get("rows") or 0),
        })
    return out


def curve_summary(scene_id: str) -> dict[str, Any]:
    """单场景学习曲线摘要（1→n）——给场景卡当「曲线上升」的数字证据。

    只读消费侧（`_consume_curve(scene_id=…)`，口径与主面板学习曲线完全一致，
    不另造一把尺子）：
      · `imports`       —— 本场景导入过几次
      · `high/medium/rejected` —— 命中判定分布（双阈值，非人审）
      · `written/...`   —— 判定结果分布（written/insufficient_evidence/
                          fetched_empty/failed，见《判定结果状态与写入证明规格》）
      · `mastered_first` / `mastered_last` / `delta` —— 曲线首末读数，
                        delta>0 即「曲线上升」
      · `reachable`     —— 可达证据：同一问题被导入 ≥2 次且后一次高置信命中
                        （用户要的「再导入该同一问题时，可达」）
      · `reach_evidence`—— 人话版证据（哪句话、第几次导入后命中）
    """
    try:
        cc = _consume_curve(limit=60, scene_id=scene_id)
    except Exception:
        cc = []
    dec: dict[str, int] = {}
    jud: dict[str, int] = {}
    for c in cc:
        if c.get("route_decision"):
            dec[c["route_decision"]] = dec.get(c["route_decision"], 0) + 1
        if c.get("judgment_result"):
            jud[c["judgment_result"]] = jud.get(c["judgment_result"], 0) + 1
    first = cc[0]["mastered_total"] if cc else 0
    last = cc[-1]["mastered_total"] if cc else 0
    # 可达证据：同一句话导入多次，且后一次是高置信命中
    seen: dict[str, int] = {}
    reach = 0
    evidence = ""
    for c in cc:
        q = str(c.get("question") or "").strip()
        if not q:
            continue
        seen[q] = seen.get(q, 0) + 1
        if seen[q] >= 2 and str(c.get("route_decision") or "").startswith("HIGH"):
            reach += 1
            if not evidence:
                evidence = f"「{q[:40]}」第 {seen[q]} 次导入 → 高置信命中"
    return {
        "imports": len(cc),
        "high": dec.get("HIGH_CONFIDENCE", 0),
        "medium": dec.get("MEDIUM_CONFIDENCE", 0),
        "rejected": dec.get("REJECTED", 0),
        "written": jud.get("written", 0),
        "insufficient_evidence": jud.get("insufficient_evidence", 0),
        "fetched_empty": jud.get("fetched_empty", 0),
        "failed": jud.get("failed", 0),
        "mastered_first": first,
        "mastered_last": last,
        "delta": last - first,
        "reachable": reach > 0,
        "reach_evidence": evidence,
        "reused": sum(1 for c in cc if c.get("reused")),
    }


def _db_view() -> dict[str, Any]:
    """当前指向哪个库（不 import vlml_env，避免面板启动就拉起整个 VLML）。"""
    try:
        import db_switch
        return {"path": db_switch.current_path(), "is_default": db_switch.is_default()}
    except Exception as e:
        return {"path": "", "is_default": True,
                "error": f"{type(e).__name__}: {e}"[:160]}


def _causal_view(limit: int = 25,
                 scene_id: str | None = None) -> dict[str, Any]:
    """行动因果时间线（control / attempt / referee / narrative 同一条单调序列）。

    面板要显示它是因为：人导入**不进掌握度**，如果面板只有掌握度视图，
    人就会觉得自己导入的东西石沉大海。这条线是「导入确实发生了」的唯一证据。

    1→n：`scene_id` 非空时只返回该场景的事实（切场景后这一页只显本场景的
    因果）。旧事实没有 `scene` 键（1→n 改造前写的）按默认场景 A 计。
    """
    try:
        rows = (causal_timeline.for_scene(scene_id, limit) if scene_id is not None
                else causal_timeline.for_panel(limit))
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


def _scene_ctx() -> dict[str, Any]:
    """当前场景上下文（1→n，2026-10-18）：面板所有页签按它过滤。

    用户指令：「切了场景之后应该这几个页面的内容都关于那个场景」——
    build_state 里 imports / consume_curve / causal / series / attempted /
    failed / skill_detail 全部按这里的 id 过滤。
    返回 `{id, name, data_source, is_default}`，scenes 模块不可用时退化为
    默认 A（绝不拖垮面板）。
    """
    try:
        import scenes as _sc
        sid = str(_sc.current_scene_id() or "A")
        for s in _sc.list_scenes():
            if str(s.get("id")) == sid:
                return {
                    "id": sid,
                    "name": str(s.get("name") or sid),
                    "data_source": str(s.get("data_source") or ""),
                    "is_default": sid == "A",
                }
        return {"id": sid, "name": sid, "data_source": "", "is_default": sid == "A"}
    except Exception:
        return {"id": "A", "name": "A", "data_source": "", "is_default": True}


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


def _voyager_view() -> dict[str, Any]:
    """Voyager 真实学习驾驶舱状态（面板只读，不持有进程句柄）。

    与 _pilot_view 同构：状态写在 voyager_pilot_state.json，进度由
    run_voyager_learn.py 每任务写 voyager_progress.json。这里只读文件，
    并附上技能库 / 课程进度 / 事件目录的**存在性**——让人一眼看清"现在库里有什么"。
    """
    try:
        import voyager_pilot
        from voyager_paths import SKILL_DIR, CURRICULUM_DIR, EVENTS_DIR
        st = voyager_pilot.read()
        return {
            **st,
            "tail": voyager_pilot.tail(80),
            "skill_dir_exists": SKILL_DIR.exists(),
            "curriculum_dir_exists": CURRICULUM_DIR.exists(),
            "events_dir_exists": EVENTS_DIR.exists(),
        }
    except Exception as e:
        return {"running": False, "tail": "", "why": f"Voyager 驾驶舱不可用：{type(e).__name__}: {e}"[:160]}


def _next_recommend() -> dict[str, Any]:
    """出题器当前推荐（带理由）。人导入之后这条应该跟着变。"""
    try:
        import planner
        return planner.next_brief()
    except Exception as e:
        return {"topic_id": "", "reason": "unavailable",
                "reason_label": "不可用", "explanation": f"{type(e).__name__}: {e}"[:160],
                "question": "", "difficulty": 0}


def probe_env(force: bool = False) -> dict[str, Any]:
    """探测 VLML 是否可用。

    助产士第三轮的要求：MCP 连不上时面板必须说清楚，不能静默显示空。
    这里不做重试，只报状态 + 原因，由人决定下一步。

    2026-10-21 起带 15s 缓存：每次探测都要新开 duckdb 连接，面板轮询下
    这是最贵的一次系统调用（事故里 77 个 fd 全卡在这一步）。
    """
    if not force:
        with _STATE_LOCK:
            if (_ENV_CACHE["value"] is not None
                    and time.time() - _ENV_CACHE["at"] < _ENV_TTL):
                return _ENV_CACHE["value"]
    result = _probe_env_uncached()
    with _STATE_LOCK:
        _ENV_CACHE["value"] = result
        _ENV_CACHE["at"] = time.time()
    return result


def _probe_env_uncached() -> dict[str, Any]:
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


def _scene_cache_key() -> str:
    """缓存必须按场景分桶 —— 否则切了场景前端还在读上一份（用户 2026-10-18
    「切了场景之后应该这几个页面的内容都关于那个场景」的最直接违反）。
    test_scene_isolation 就是靠这条守着的。"""
    try:
        import scenes as _sc
        return str(_sc.current_scene_id() or "A")
    except Exception:
        return "A"


def build_state() -> dict[str, Any]:
    """聚合面板全部视图（单飞 + 3s 缓存，为什么必须：见文件头的事故记录）。

    1→n：全页签按当前场景过滤（用户 2026-10-18「切了场景之后应该这几个
    页面的内容都关于那个场景」）；练习线 run_log 同理。旧记录无 scene 键 →
    for_scene 归 A，与 consume() / coach.explain() 的默认口径一致。
    """
    key = _scene_cache_key()
    with _STATE_LOCK:
        if (_STATE_CACHE["value"] is not None
                and _STATE_CACHE.get("key") == key
                and time.time() - _STATE_CACHE["at"] < _STATE_TTL):
            return _STATE_CACHE["value"]
    with _INFLIGHT_LOCK:
        if _INFLIGHT["on"]:
            ev, mine = _INFLIGHT["done"], False
        else:
            _INFLIGHT["on"] = True
            _INFLIGHT["done"] = ev = threading.Event()
            mine = True
    if not mine:
        # 已经有一个 build 在跑：**等它算完拿同一份**（最多 30s），而不是
        # 各自再开一个 duckdb 连接 —— 事故里 77 个 fd 就是这么堆出来的。
        ev.wait(timeout=30.0)
        with _STATE_LOCK:
            cached = _STATE_CACHE["value"]
            cached_key = _STATE_CACHE.get("key")
        if cached is not None and cached_key == key:
            return cached
        # 等了 30s 还一份都没有（首个请求就算崩了），或刚切了场景而那一份
        # 是别人场景的：给**合法 JSON**，前端至少能显示"正在聚合"，
        # 不再出现 Unexpected end of JSON input。
        return {"ok": False, "pending": True,
                "reasons": ["面板正在聚合数据，请稍后刷新"], "scene": {}}
    try:
        state = _build_state_uncached()
    except Exception as e:  # 单飞闸门必须放行，否则整个面板永久 500
        with _INFLIGHT_LOCK:
            _INFLIGHT["on"] = False
            ev.set()
        return {"ok": False, "pending": False,
                "reasons": [f"聚合失败：{type(e).__name__}: {e}"[:200]], "scene": {}}
    with _INFLIGHT_LOCK:
        _INFLIGHT["on"] = False
        ev.set()
    with _STATE_LOCK:
        _STATE_CACHE["value"] = state
        _STATE_CACHE["key"] = key
        _STATE_CACHE["at"] = time.time()
    return state


def _build_state_uncached() -> dict[str, Any]:
    # 1→n：全页签按当前场景过滤（用户 2026-10-18：「切了场景之后应该这几个
    # 页面的内容都关于那个场景」）。练习线 run_log 同理 —— B 的练习轮不该
    # 混进 A 的轨迹页。旧记录无 scene 键 → for_scene 归 A，与 consume() /
    # coach.explain() 的默认口径一致，不丢历史。
    scene_ctx = _scene_ctx()
    sid = scene_ctx["id"]
    rows = run_log.for_scene(sid)
    memory = skill_store.load()
    # 防膨胀/防幻觉能不能自证，就看这两个数
    skill_stats = skill_store.stats()
    # 排序：**最有效（ok 多）的排最前**，其次被注入次数多的，完全没用过的排最后。
    #
    # 不排序的话这列表是插入序 —— 复用链接上之前 25 条全 0/0 看不出差别；
    # 接上之后（实测 hits 9 / ok 5），真正被复用且帮上忙的那 2 条会淹在
    # 31 条的最底下，人根本看不见「哪些经验是真在用的」。
    # hits / ok 的语义（skill_store.py:313 / :327）：
    #   hits = 被检索注入过几次 prompt；ok = 注入后这一轮判成功几次。
    #   「hits 涨、ok 不涨」= 注入了但没帮上，是要它沉底的信号。
    _detail = [
        {
            "key": s.get("key", ""),
            "topic": s.get("topic", ""),
            "name": s.get("name", ""),
            "text": s.get("text", ""),
            "hits": int(s.get("hits") or 0),
            "ok": int(s.get("ok") or 0),
            # 1→n 场景归属：技能卡上直接标「来自 Scene X」（溯源并入技能页，
            # 不再单开页面）。字段引入前入库的老技能显示 default。
            "source_scene": str(s.get("source_scene") or "default"),
            # 技能归属（2026-10-20 用户定）：一次人导入 = 一个批次，这批学习长出
            # 的维度/里程碑/技能都记同一个 `import_id`。面板上直接标出来，
            # 「这条技能是哪次导入、哪个里程碑长的」才可回看（Obsidian 式反链的
            # 可见形态）；字段引入前的技能没有 → 空串，前端不显示。
            "import_id": str((s.get("blueprint") or {}).get("import_id") or ""),
            "milestone": str((s.get("blueprint") or {}).get("milestone") or ""),
            "dimension": str((s.get("blueprint") or {}).get("dimension") or ""),
            "versions": s.get("versions", 1),
            "rate": (
                round(int(s.get("ok") or 0) / int(s.get("hits") or 1), 3)
                if int(s.get("hits") or 0) else None
            ),
        }
        # 1→n 场景过滤：技能是**跨场景复用资产**，所以口径是「本场景 + 无
        # 归属的老技能（default）」——切到 B 时老技能仍在（它们本来就可能
        # 被 B 复用），但 A 专属的技能不混进来。
        for s in skill_store.all_skills()
        if str(s.get("source_scene") or "default") in (sid, "default")
    ]
    _detail.sort(key=lambda x: (-x["ok"], -x["hits"]))
    skill_detail = _detail

    # 人导入通路：这两块在「还没跑过」时也要显示 —— 它正是让人开始的地方
    # 1→n：都按当前场景取（切到 B 时人导入页只显 B 导入过的问题）。
    imports = _human_imports(scene_id=sid)
    # 反链（Obsidian 式）：每条导入记录标出「这批长出了几条技能」。
    # 技能归属不在导入记录上另存一份账（那会两本账打架），而是按
    # `blueprint.import_id` 反查 —— 派生索引，删技能它自动跟着变。
    _grown: dict[str, int] = {}
    for _s in skill_store.all_skills():
        _iid = str((_s.get("blueprint") or {}).get("import_id") or "")
        if _iid:
            _grown[_iid] = _grown.get(_iid, 0) + 1
    for _im in imports:
        _im["skills_grown"] = _grown.get(str(_im.get("import_id") or ""), 0)
    recommend = _next_recommend()
    causal = _causal_view(scene_id=sid)
    pilot_view = _pilot_view()
    db = _db_view()
    # 练习范围（知识图谱页上点「练习此知识点」设的）。面板只读，不推断。
    scope = practice_scope.get_scope()

    if not rows:
        return {
            "has_data": False,
            # 1→n：当前场景上下文（前端场景栏/横幅/空态说明都要读它）
            "scene": scene_ctx,
            "env": probe_env(),
            "db": db,
            "pilot": pilot_view,
            "voyager": _voyager_view(),
            "practice_scope": scope,
            "imports": imports,
            # 消费进步曲线：run_log 为空（比如刚清库）也要照常给 ——
            # 进步是在消费这条线上测的，练习没跑过不影响它。
            # 1→n：按当前场景取（B 冷启动时曲线为空是事实，不是 bug）。
            "consume_curve": _consume_curve(scene_id=sid),
            "recommend": recommend,
            "causal": causal,
            "tasks": _task_library(),
            "skills": memory,
            "skill_stats": skill_stats,
            "skill_detail": skill_detail,
            "mc_thinking": _mc_thinking_view(),
            # 摸底/考核走的是 exam_log.jsonl，**不写** run_log.jsonl（有意分开：
            # run_log 是带图谱的练习覆盖率，恒 100%，画出来是假平线）。
            # 所以"run_log 为空"≠"没有任何数据" —— 摸底跑完了一样要给看。
            # 漏掉这一条的实测后果：线上摸底跑完 7 道题，总览页仍显示
            # 「还没有任何运行记录」，真实水平曲线根本不出现。
            "exam_curve": _exam_view(),
            # 导航上的「轨迹 N 轮」读它；不给就是 "undefined 轮"
            "total_rounds": 0,
            "topic_id": "",
            "hint": (f"Scene {sid} 还没有练习记录。摸底/考核不入 run_log"
                     f"（走 exam_log）——有裸考数据时下面会照常显示。"),
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
            # VLML 观测：全部评分点达成（考核尺子）。旧日志没有这一列 → False
            "success": bool(r.get("success")),
            # 判据层布尔：任务完成（Voyager 尺子：跑通编排 + 拿到数 + 没翻车，
            # 不数覆盖点）。旧日志没有这一列 → False。
            "done": bool(r.get("done")),
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
    # 判据必须用判据层的 success（self-verification 布尔），不用覆盖率，
    # 更不用 mastery —— 掌握度只做独立评估，不当学习判据。
    mas_warning = (
        "mastery 的 confidence = 1-exp(-证据权重和/4)，反复作答仍会把它推高；"
        "它只做独立评估。判据看判据区的 success（全部评分点达成）。"
        if len(mas) >= 2 else None
    )

    # ⚠️ 「编排轨迹是否变复杂」这一路（traj_sizes / tool_sets / new_tools）已**下架**：
    #   它读的 run_log.trajectory 只由已冻结的旧后端 mve/run_mve.py 写入，
    #   新后端 voyager_vlml/loop.py 不把工具序列落进 run_log —— 线上 36 条记录里
    #   恒为空，算出来永远是 [0,0,...] 和 []。面板上那张「编排轨迹」表随之下架。
    #   「这轮调了哪些工具、成败、取到几行」改由 mve/loop_state.json 里的
    #   循环心跳提供（tool_calls / tool_ok / tool_fail / rows），是真实在产的。
    last = series[-1]
    prev_mastery = series[-2]["mastery"] if len(series) >= 2 else None

    attempted = run_log.attempted_counts(sid)
    failed = run_log.failed_counts(sid)
    topic_id = str(rows[-1].get("topic_id") or "")

    return {
        "has_data": True,
        # 1→n：当前场景上下文（id/name/data_source/is_default）。
        # 前端场景栏、图谱横幅、各页签空态说明都读它；切场景时 state 重建，
        # 下面的 imports/consume_curve/causal/series/attempted/failed/
        # skill_detail 已全部按它过滤。
        "scene": scene_ctx,
        "env": probe_env(),
        "db": db,
        "pilot": pilot_view,
        "voyager": _voyager_view(),
        "practice_scope": scope,
        "question": rows[-1].get("question", ""),
        "topic_id": topic_id,
        # 出题器：照伴学 practice_scope 的选题排序
        "referee": _referee_view(topic_id),
        "tasks": _task_library(),
        # 人导入通路：导入过什么 + 出题器因此改成推哪题
        "imports": imports,
        # 消费进步曲线：进步与掌握度**在消费这条线上测**（练习不判分）。
        # 1→n：按当前场景取 —— B 的曲线只画 B 的导入历史（否则"B 从零开始"
        # 会被画成"接在 A 的尾声上"，Δ 掌握数虚高）。
        "consume_curve": _consume_curve(scene_id=sid),
        "recommend": recommend,
        "causal": causal,
        "attempted": attempted,
        "failed": failed,
        "total_rounds": len(series),
        "series": series,
        "reading": reading,
        "reading_text": reading_text,
        "mastery_warning": mas_warning,
        # ── 已下架的轨迹键（按后端索引，2026-10-10）──────────────────────
        # 下面这几个顶层键原本是给面板画折线用的，现在**一个消费方都没有**
        # （dashboard 直接在客户端从 series 现算，不再要服务端喂轨迹）：
        #     runs / coverage_track / done_track / success_track /
        #     mastery_track / trajectory_sizes / new_tools
        # 其中 coverage_track / mastery_track 在新后端上恒为空数组（练习线不判分），
        # trajectory_sizes / new_tools 恒为 0 / []（new 后端不落 trajectory）。
        # 留着它们就是给面板挂一排"0"和空数组，看着像"它没学会"，其实无人在产。
        # 需要时从 series 现算即可，别再往 state 里塞。
        "last_done": bool(series[-1]["done"]),
        # VLML 观测：最近一次是否全部评分点达成（考核尺子）
        "last_success": bool(series[-1]["success"]),
        "skills": memory,
        # 防膨胀：写入次数 vs 实际条目（bloat=1 表示每次写入都成了新条目，即没挡住）
        "skill_stats": skill_stats,
        "skill_detail": skill_detail,
        "mc_thinking": _mc_thinking_view(),
        # 防幻觉：被溯源校验拦下的编造数字
        "hallucination_total": hallucination_total,
        # 注意：新后端（练习线）**不写 mastery**，所以这里通常是 None。
        # 以前无 mastering 值时返回「掌握度 None」，导出 md 上就挂着一行噪音。
        "mastery_delta": (
            f"掌握度 {_pct(prev_mastery)} -> {_pct(last['mastery'])}"
            if prev_mastery is not None and last["mastery"] is not None
            else (f"掌握度 {_pct(last['mastery'])}" if last["mastery"] is not None else None)
        ),
        # 真实水平曲线（撤支架考核）—— 学习曲线该看的那一列
        # （上面判读已经算过一次，这里复用，别再读一遍 exam_log）
        "exam_curve": exam_curve,
        # 教学图谱（伴学出题/图谱移植）：前置门控 + 薄弱优先 + 掌握度叠加。
        # 与判分侧 /api/graph 并存：一个管"下一步学什么"，一个管"维度↔工具"。
        # 惰性加载 + 异常兜底：topic_graph 读 JSON 种子，挂了也不拖垮整个面板。
        "teaching": _teaching_payload(),
    }


def _teaching_payload() -> dict:
    """教学图谱 payload（惰性加载）。失败返回空壳，自适应页签退化为「知识点图谱」。"""
    try:
        import topic_graph
        return topic_graph.get_graph().to_graph_payload()
    except Exception as e:  # 绝不能因为教学图谱挂了拖垮整个面板
        return {"schema_version": 3, "difficulty_target": 1, "nodes": [],
                "edges": [], "layers": {}, "total": 0,
                "error": f"{type(e).__name__}: {e}"[:200]}


def as_json() -> str:
    return json.dumps(build_state(), ensure_ascii=False, indent=2)


def scenes_view() -> dict[str, Any]:
    """场景命名空间视图（1→n 泛化）：列出所有场景 + 当前场景 + 每场景专属节点数
    与降级标记、状态指纹。

    专属节点数 = `root_scene == 该场景` 且 `status != deleted` 且非根占位（见
    knowledge_graph.scene_specific_node_count）—— 这就是设计 Q3 的
    **Δ 场景专属节点数**在面板上的数据来源。插入 Scene B 之前为 0，冷启动一题
    成功后变 1，切回 A 读数不变（证明 B 没污染 A）。

    每个场景额外返回：
      · `exclusive_nodes` —— Δ 场景专属节点数（证明量）
      · `graph_sparsity` —— `seeding`（种子态·节点随消费增长）/`dense`
      · `entity_catalog` —— `missing`（实体目录缺失·橙色告警）/`present`
      · `fingerprint`    —— 状态指纹（续跑点/上次活动/消费数/状态 hash），
                            切回 A 后可逐项核对「没被覆盖」
      · `action_count`   —— 本场景注册的可执行动作条数（0 = 硬拒绝：不许插入）
      · `pending_nodes`  —— 本场景待审区条目数（未命中且未人审）
    """
    try:
        import scenes as _sc
        scene_list = _sc.list_scenes()
        current = _sc.current_scene_id()
        scene_ids = [s["id"] for s in scene_list]
    except Exception:
        scene_list, current, scene_ids = [], "A", []
    exclusive: dict[str, int] = {}
    sparsity: dict[str, str] = {}
    catalog: dict[str, str] = {}
    fp: dict[str, Any] = {}
    acts: dict[str, int] = {}
    pending: dict[str, int] = {}
    curve: dict[str, Any] = {}
    groups: dict[str, list[dict[str, Any]]] = {}
    backend: dict[str, str] = {}
    try:
        import knowledge_graph as kg
        import scenes as _sc
        import actions as _acts
        import routing as _rt
        import scene_profile as _sp
        # 热路径兜底：注册表是进程内存态，重启后就丢（实测 action_count 全 0）。
        # 这里在取数前先 restore + 反射补齐，与面板启动自检同一套逻辑，
        # 避免「明明能跑的场景显示 0 个可执行动作」的假告警。
        _acts.restore()
        for sid in [s["id"] for s in scene_list]:
            prof = _sp.profile(sid)
            backend[sid] = str(prof.get("kind") or "")
            if prof.get("kind") == _sp.LOCAL:
                # 本机场景：本机原语**始终**作为一组条目在（不是"没条目才补"）——
                # 否则一个已经学过技能的场景只显示「学到的技能」组，人在面板上
                # 看不到「本机能力」这组开关，等于总开关下面少了一格。
                if not any(e.get("origin") == "local"
                           for e in _acts.entries(sid)):
                    _acts.register_local(sid, str(prof.get("scene_dir") or ""))
                if not any(e.get("origin") == "net" for e in _acts.entries(sid)):
                    _acts.register_net(sid)
            elif _acts.entry_count(sid) == 0:
                # VLML 场景走反射补齐（浅克隆没有 vlml/ 时如实 0 条，不编清单）
                _acts.register_builtin_actions(sid)
        g = kg.KnowledgeGraph.load()
        for sid in scene_ids:
            exclusive[sid] = g.scene_specific_node_count(sid)
            sparsity[sid] = _sc.graph_sparsity(sid)
            catalog[sid] = _sc.entity_catalog_status(sid)
            fp[sid] = _sc.scene_state_fingerprint(sid)
            acts[sid] = _acts.entry_count(sid)
            # 猫爪总开关：每组一个开关 + 组下面的条目（同构 neko 插件 toggle）
            groups[sid] = _acts.groups(sid)
            try:
                pending[sid] = len(_rt.pending_for_scene(sid))
            except Exception:
                pending[sid] = 0
            curve[sid] = curve_summary(sid)
    except Exception:
        exclusive = {sid: 0 for sid in scene_ids}
        sparsity = {sid: "seeding" for sid in scene_ids}
        catalog = {sid: "present" for sid in scene_ids}
        fp = {sid: {} for sid in scene_ids}
        acts = {sid: 0 for sid in scene_ids}
        pending = {sid: 0 for sid in scene_ids}
        curve = {sid: {} for sid in scene_ids}
        groups = {sid: [] for sid in scene_ids}
        backend = {sid: "" for sid in scene_ids}
    return {"scenes": scene_list, "current": current,
            "exclusive_nodes": exclusive, "graph_sparsity": sparsity,
            "entity_catalog": catalog, "fingerprint": fp,
            "action_count": acts, "pending_nodes": pending,
            "curve_summary": curve,
            # 猫爪总开关（同构 neko）：每场景的能力组 + 组内条目 + 开关状态；
            # scene_backend 说明这场景的数据源是哪一档（vlml / local / scene）。
            "groups": groups, "scene_backend": backend}


# ⚠️ 跨场景技能溯源（skill_traceability）已并入技能页（2026-10-18 去重）：
#   /scenes 独立页撤掉后，它的唯一消费方没了。归属场景 + 复用健康度改由
#   `build_state().skill_detail` 直接供（source_scene 字段 + hits/ok 前端
#   按同一判据算 ok/stale/unused），不再单开一个 API 和一份重复遍历。


if __name__ == "__main__":
    print(as_json())
