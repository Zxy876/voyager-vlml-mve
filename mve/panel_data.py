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
import re
import statistics
import threading
import time
from pathlib import Path
from typing import Any

import run_log  # noqa: E402
import causal_timeline  # noqa: E402
import skill_store  # noqa: E402
import practice_scope  # noqa: E402

import os
import hashlib
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


_REV_CACHE: dict[str, Any] = {"at": 0.0, "value": ""}


def state_rev() -> str:
    """全库数据指纹（2026-11-18 面板卡顿治理）。

    对 mve 目录下所有文件（跳 __pycache__/.git/node_modules）的
    mtime_ns+size 做 sha1 —— 学习循环（独立进程）和面板（人操作）
    写任何账目，指纹必变。前端轮询 /api/rev，指纹没变就不拉 1MB
    全量 /api/state。1 秒微缓存：多页签同帧轮询只算一次。
    """
    now = time.monotonic()
    if now - _REV_CACHE["at"] < 1.0 and _REV_CACHE["value"]:
        return _REV_CACHE["value"]
    h = hashlib.sha1()
    skip = {"__pycache__", ".git", "node_modules"}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
                h.update(f"{p}|{st.st_mtime_ns}|{st.st_size}\n".encode())
            except OSError:
                pass
    _REV_CACHE["at"] = now
    _REV_CACHE["value"] = h.hexdigest()[:16]
    return _REV_CACHE["value"]


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
            # 三态披露（2.5）：False = 裁判根本没对照上，上面三个 bool 是
            # bool(None)=False 占位、无意义 —— 前端据此显示「未对照」而非「有硬伤」。
            "referee_compared": bool(rec.get("referee_compared")),
            "scope_line": str(rec.get("scope_line") or ""),
            "referee_rows": int(rec.get("referee_rows") or 0),
            "coverage_referee": (float(rec["coverage_referee"])
                                 if rec.get("coverage_referee") is not None else None),
            "verdict_referee": str(rec.get("verdict_referee") or ""),
            "rows": int(rec.get("rows") or 0),
        })
    return out


def _trace_curve(scene_id: str | None = None, limit: int = 24) -> list[dict[str, Any]]:
    """**题内缺口序列** —— 进步的唯一观测账本（同构助产士 Phase1Iteration）。

    2026-11-14 用户拍板：这条线**取代**消费线的 `mastered_total` 曲线。

    为什么换（两条都是实测，不是口径偏好）：
      · 消费线画「累计会做几道题」—— 答出来 +1、失败不减，**单调不减**，
        同一道题做十遍仍然是 1。它记的是"做了多少"，不是"做得多好"；
        而且实测 `coach_log.jsonl` 24 条里 `mastered=True` **0 条**、
        `coverage` 非空 **0 条**，这条线本来就是空的。
      · 缺口序列是同一道题**逐轮**的剩余缺口数：助产士每提交一次 spec
        追加一条 `Phase1Iteration`（`opus/schemas.ts:126`），收敛形状
        （4→1→0）就是进步本身；重跑时段与段可逐轮比对。

    数据源：`ckpt_maieutic/<scene>/req_*.json` 的 `phase1.iterations`，
    经 `maieutic_scaffold.spec_progress` 按 run 分段**现算** —— 不信文件里
    写死的 `progress`（历史文件没有这个字段，口径也变过）。

    返回按最近活动倒序；`points` 直接可画：x=段内轮号，y=当轮剩余缺口数。
    """
    try:
        import maieutic_scaffold as _ms
    except Exception:
        return []
    root = ROOT / "ckpt_maieutic"
    dirs = [root / scene_id] if scene_id else sorted(
        p for p in root.glob("*") if p.is_dir())
    out: list[dict[str, Any]] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("req_*.json")):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            q = str(rec.get("question") or "").strip()
            if not q:
                continue
            # ⚠️ 必须用**这份文件自己的** iterations：`_ms.spec_progress(scene, q)`
            # 按问句解析缓存路径（取"这句话最新的 req"），同一句发布过两次时
            # 会把两份记录都读成最新那份 —— 旧记录的会话凭空消失。
            _its = [x for x in ((rec.get("phase1") or {}).get("iterations") or [])
                    if isinstance(x, dict)]
            try:
                pr = _ms.spec_progress_of(_its)
            except Exception:
                continue
            runs = [r for r in (pr.get("runs") or []) if isinstance(r, dict)]
            if not runs:
                continue
            tail = runs[-1]
            opens = [int(x) for x in (tail.get("opens") or [])]
            out.append({
                "req_id": f.stem[4:] if f.stem.startswith("req_") else f.stem,
                "question": q[:80],
                "root_scene": d.name,
                "runs": runs,
                "n_runs": len(runs),
                # 最近一段：逐轮剩余缺口（前端直接画，不再自己拼）
                "points": [{"x": i + 1, "y": y} for i, y in enumerate(opens)],
                "opens_now": opens[-1] if opens else None,
                "rounds": int(tail.get("rounds") or 0),
                "first_pass_round": tail.get("first_pass_round"),
                "passed": bool(tail.get("passed")),
                "delta": pr.get("delta"),
                "monotonic": bool(pr.get("monotonic")),
                "stuck": [str(x) for x in (pr.get("stuck") or [])],
                "last_ts": str((_its[-1].get("ts") if _its else "") or ""),
            })
    # 最近动过的题排前面（空 ts 的沉底，但别丢）
    out.sort(key=lambda r: (r.get("last_ts") or "", r.get("req_id") or ""),
             reverse=True)
    return out[:limit]


def _quadrants(rec: dict[str, Any], runs: list[dict[str, Any]],
               raw: list[dict[str, Any]], pr: dict[str, Any]
               ) -> dict[str, Any]:
    """四象限 + 一句话概述（同构 `CohortNarrativeOutput`）。

    助产士字段（`src_lib_opus_schemas.ts:391-398`）：
      `narrative` / `solution_techniques` / `common_drifts` /
      `strengths` / `difficulties` / `provisional`
    触发条件照抄：**至少一个会话完成**才出 Summary；不足 3 段标 provisional。

    抽成独立函数是因为有两个消费者（原「能力模块」卡 + 新的练习详情页），
    两处必须同一把尺子 —— 各算一遍就会出现「卡上说强项 3 条、点进去 5 条」。
    """
    dims = [str(x.get("id")) for x in (rec.get("dimensions") or [])
            if isinstance(x, dict) and x.get("id")]

    # 缺口账：哪些维度被反复点到
    cnt: dict[str, int] = {}
    for it in raw:
        for g in (it.get("gaps_identified") or []):
            k = str(g)
            cnt[k] = cnt.get(k, 0) + 1
    p3 = rec.get("phase3") or {}
    drifts: list[str] = []
    dv_ids: set[str] = set()
    for dv in (p3.get("divergences") or [])[:6]:
        if not isinstance(dv, dict):
            continue
        did = str(dv.get("divergence_id") or "").strip()
        cls = str(dv.get("final_classification")
                  or dv.get("initial_classification") or "").strip()
        if not did:
            continue
        dv_ids.add(did)
        # 用题面里那句人话，不要只给英文维度 id（原版也是人话条目）
        ask = str(dv.get("student_facing_question") or "").strip()
        if ask:
            ask = ask[:36] + ("…" if len(ask) > 36 else "")
        drifts.append(f"[{cls or '—'}] {did}" + (f" —— {ask}" if ask else ""))

    # ⚠️ Phase1 缺口账与 Phase3 分歧账口径不同：一个维度可能在 Phase1
    # 从没进过 gaps_identified，却在这里被判 bug（代码没实现）。
    # 不排除就会同一条同时出现在「强项」和「常见偏差」里，自相矛盾。
    never = [x for x in dims if x not in cnt and x not in dv_ids]
    tech: list[str] = []
    for r in runs:
        seq = "→".join(str(x) for x in (r.get("opens") or [])) or "—"
        if r.get("passed"):
            tech.append(f"第 {r['run']} 段：{r.get('rounds')} 轮通过"
                        f"（逐轮剩余缺口 {seq}）")
        else:
            tech.append(f"第 {r['run']} 段：{r.get('rounds')} 轮仍未通过"
                        f"（逐轮剩余缺口 {seq}）")
    strengths: list[str] = []
    if never:
        strengths.append("一次到位（从未进缺口）：" + "、".join(never[:5]))
    if pr.get("monotonic"):
        strengths.append("缺口逐轮单调收缩，没有回退")
    diffs: list[str] = []
    hard = sorted(((k, v) for k, v in cnt.items() if v >= 2),
                  key=lambda x: -x[1])
    if hard:
        diffs.append("反复卡住：" + "、".join(
            f"{k}（{v} 轮）" for k, v in hard[:4]))
    if pr.get("stuck"):
        diffs.append("末轮仍未闭合：" + "、".join(
            str(x) for x in (pr.get("stuck") or [])[:5]))
    tail = runs[-1] if runs else {}
    if tail and not tail.get("passed"):
        diffs.append(f"最近一段未通过（{tail.get('rounds')} 轮止步）")

    done = sum(1 for r in runs if r.get("passed"))
    prov = done < 3 or len(runs) < 3
    head = (f"共 {len(runs)} 段会话、{done} 段通过；"
            f"最近一段 {tail.get('rounds')} 轮"
            f"{'通过' if tail.get('passed') else '未通过'}")
    ops = (tail.get("opens") or []) if tail else []
    if ops:
        head += f"，剩余缺口 {ops[0]} → {ops[-1]}"
    if drifts:
        head += f"；Phase3 记了 {len(drifts)} 条分歧"
    head += "。"
    # 全段一轮过、缺口恒 0：这题没产生过追问轨迹（直接命中/跳过），
    # 不说明的话四象限会读成"全都一次到位"，那是假象。
    if runs and all((not (r.get("opens") or [])) or (r.get("opens") or [0])[0] == 0
                    for r in runs) and all(r.get("rounds") == 1 for r in runs):
        head += "每段都一轮通过、缺口恒为 0 —— 这题没有产生追问轨迹，四象限只反映终态。"
    if prov:
        head += "样本不足 3 段会话，下面的模式都是暂定的。"
    return {
        "narrative": head,
        "solution_techniques": tech[:6],
        "common_drifts": drifts,
        "strengths": strengths[:5],
        "difficulties": diffs[:5],
        "provisional": bool(prov),
        "completed": done,
        "rounds": int((tail or {}).get("rounds") or 0),
        "passed": bool((tail or {}).get("passed")),
        "opens_now": (ops[-1] if ops else None),
    }


def _cohort_narrative(scene_id: str | None = None, limit: int = 12) -> list[dict[str, Any]]:
    """每道题的「能力模块」四象限（`_quadrants` 的对外视图）。

    只收**至少一段会话通过**的题 —— 原版规矩：没有完成就没有 Summary。
    """
    try:
        import maieutic_scaffold as _ms
    except Exception:
        return []
    root = ROOT / "ckpt_maieutic"
    dirs = [root / scene_id] if scene_id else sorted(
        p for p in root.glob("*") if p.is_dir())
    out: list[dict[str, Any]] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("req_*.json")):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            q = str(rec.get("question") or "").strip()
            if not q:
                continue
            # 同 _trace_curve：按文件读，不按问句解析（见那里的注释）
            raw = [x for x in ((rec.get("phase1") or {}).get("iterations") or [])
                   if isinstance(x, dict)]
            try:
                pr = _ms.spec_progress_of(raw)
            except Exception:
                continue
            runs = [r for r in (pr.get("runs") or []) if isinstance(r, dict)]
            if not runs or not any(r.get("passed") for r in runs):
                continue
            quad = _quadrants(rec, runs, raw, pr)
            out.append({
                "req_id": f.stem[4:] if f.stem.startswith("req_") else f.stem,
                "question": q[:80],
                "root_scene": d.name,
                **quad,
                "sessions": len(runs),
                "delta": pr.get("delta"),
                "monotonic": bool(pr.get("monotonic")),
                "points": [{"x": i + 1, "y": y}
                           for i, y in enumerate((runs[-1].get("opens") or []))],
                "runs": runs,
                "intended": [str(x.get("pattern") or "")
                             for x in (rec.get("expected_drifts") or [])
                             if isinstance(x, dict) and x.get("pattern")][:6],
            })
    out.sort(key=lambda r: (r.get("req_id") or ""), reverse=True)
    return out[:limit]


def _norm_q(s: str) -> str:
    """题面归一化：只留字/数字，去掉标点与空白差异。

    同一句话在 `coach_log.jsonl` 与 `req_*.json` 里常差一个句号、少一个空格
    （实测："计算 64 的立方根，并写出其算式。" vs "计算64的立方根"），
    不归一化就匹配不上，回答会被算成"没有"。
    """
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(s or "")).lower()


def _coach_answers() -> list[dict[str, Any]]:
    """旧消费线（`mode=voyager_consume`）留在 `coach_log.jsonl` 的口头作答。

    为什么单独取（2026-11-14 用户：「现在在哪里看得见 Voyager 的回答呢」）：
      Voyager 的作答有两种形态，分落两处 ——
        · 学习闭环（`scene_learn`）：交的是**程序**，落在 req 记录的
          `phase2.finalCode`，详情页「Phase 2」那块能看到；
        · 旧消费线（`voyager_consume`）：答的是**人话**（"64的立方根是4，
          算式为 ∛64 = 4"），只落在 `coach_log.jsonl` 的 `answer` 字段。
      把「交给 Voyager 回答」的按钮解绑之后，第二种在面板上**再没有任何
      入口** —— 它还在盘上（20 条有 answer），只是没人读。
    这里只做**只读索引**：把它归到练习详情页，标清"不属于学习账"，
    不参与任何进步 / 掌握口径（那两条线看的是缺口序列与 phase3 分歧）。
    """
    f = ROOT / "coach_log.jsonl"
    if not f.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        ans = str(d.get("answer") or "").strip()
        if not ans:
            continue
        out.append({
            "q": str(d.get("question") or ""),
            "qkey": _norm_q(d.get("question")),
            "ts": str(d.get("ts") or ""),
            "mode": str(d.get("mode") or ""),
            "answer": ans[:6000],
            "sql": str(d.get("sql") or "")[:1500],
            "rows": d.get("rows"),
            "caveats": [str(x)[:300] for x in (d.get("caveats") or [])][:4],
            "narrative": str(d.get("narrative") or "")[:1200],
            "import_id": str(d.get("import_id") or ""),
            "root_scene": str(d.get("root_scene") or ""),
        })
    return out


def _answers_for(q: str, scene: str, pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把口头作答归到某道题上。精确优先，其次长前缀互相包含（题面被截断时）。"""
    k = _norm_q(q)
    if not k:
        return []
    hit = [x for x in pool
           if x.get("qkey") == k
           and (not x.get("root_scene") or x["root_scene"] == scene)]
    if hit:
        return hit[-6:]
    soft = [x for x in pool
            if x.get("qkey") and len(x["qkey"]) >= 8 and x["qkey"] in k
            and (not x.get("root_scene") or x["root_scene"] == scene)]
    return soft[-6:]


def _exercise_index(scene_id: str | None = None,
                    limit: int = 80) -> list[dict[str, Any]]:
    """**练习索引** —— 同构助产士 Instructor · Exercises 的卡片 + 详情。

    为什么要有这一层（2026-11-14 用户：「概览应该是可以点击的 exercise」）：
      之前概览只有一张「能力模块」卡，靠下拉切题 —— 那是**详情**的样子，
      不是**列表**的样子。原版列表页每张卡是一道 Exercise，头部三个数：
      `Sessions 1 / 3`、`Spec iterations 4 median / max 4`、`Divergences`；
      点进去才是每段会话的跑法与整份 Summary。

    这里把两者一次算齐（N 很小，不做分页/懒加载）：
      · 卡面：sessions / completed / 轮数中位数与最大 / 分歧分类 / 首轮最常漏的维度
      · 详情：每段会话（run）的逐轮缺口、首过轮、Phase2 交码与对账、
              Phase3 分歧逐条、四象限
    一个 req = 一道 Exercise（原版 Exercise.id 的同构物，`_cache_path` 已按
    req 分文件）；没有 req 的历史题按题面文件各自成卡。
    """
    try:
        import maieutic_scaffold as _ms
    except Exception:
        return []
    # 口头作答（旧消费线）整表读一次，循环里只做匹配 —— 不复读文件
    try:
        _ans_pool = _coach_answers()
    except Exception:
        _ans_pool = []
    root = ROOT / "ckpt_maieutic"
    dirs = [root / scene_id] if scene_id else sorted(
        p for p in root.glob("*") if p.is_dir())
    out: list[dict[str, Any]] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("req_*.json")):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            q = str(rec.get("question") or "").strip()
            if not q:
                continue
            # 全部从**这份文件**读（q 只是用来定位文件名，不代表"这句话的最新账"）：
            # phase1 / phase2 / phase3 / exercise 元信息都在这一个 json 里。
            raw = [x for x in ((rec.get("phase1") or {}).get("iterations") or [])
                   if isinstance(x, dict)]
            try:
                pr = _ms.spec_progress_of(raw)
                srun = _ms.runs_from_iterations(raw)
            except Exception:
                continue
            runs = [r for r in (pr.get("runs") or []) if isinstance(r, dict)]
            # ⚠️ spec_progress 的 `rounds` 是**轮数**（int），不是轮记录列表；
            # 要看"每段第一轮漏了哪几个维度"，得拿 runs_from_iterations 里那段
            # 真正的 rounds 数组。混用会在 rounds[0] 上炸成 int 下标。
            seg_rounds: dict[int, list[dict[str, Any]]] = {
                int(x.get("run") or 0): [y for y in (x.get("rounds") or [])
                                         if isinstance(y, dict)]
                for x in (srun or []) if isinstance(x, dict)}
            p2 = rec.get("phase2") if isinstance(rec.get("phase2"), dict) else {}
            meta = rec.get("exercise") if isinstance(rec.get("exercise"), dict) else {}
            p3 = rec.get("phase3") or {}
            dims = [str(x.get("id")) for x in (rec.get("dimensions") or [])
                    if isinstance(x, dict) and x.get("id")]
            done_milestone = bool(rec.get("milestone_achieved"))

            # ── 每段会话：P1 轮次 → P2 交码 → 是否完成 ──
            # 「完成」照原版：这一段跑到过 Phase3/4（spec 冻结 + 交过码）。
            # 历史记录没有 phase2（本版才落盘）→ 用 milestone_achieved 兜底，
            # 并在卡上如实标 `phase2_recorded=false`，不假装记过。
            # ⚠️ `phase2` 是**单槽位**（只存最新一遍），重做会覆盖上一遍 →
            #   第 1 遍明明交过码却判"没做完"。save_phase2 现在把被覆盖的那份
            #   推进 phase2_history，这里按 run 回填（按 run 取，不按时间猜）。
            _p2_hist: dict[int, dict[str, Any]] = {}
            for _x in (rec.get("phase2_history") or []):
                if isinstance(_x, dict):
                    _p2_hist[int(_x.get("run") or 0)] = _x
            if isinstance(p2, dict) and p2:
                _p2_hist[int(p2.get("run") or 0)] = p2
            rows: list[dict[str, Any]] = []
            for r in runs:
                _p2r = _p2_hist.get(int(r["run"])) or (
                    p2 if (int(p2.get("run") or 0) in (0, r["run"])) else {})
                _p2_here = bool(_p2r) and (int(_p2r.get("run") or 0) == r["run"]
                                           or r is runs[-1])
                completed = bool(r.get("passed")) and (
                    bool((_p2r or {}).get("submitted")) or done_milestone
                    or bool(p3.get("divergences") is not None and r is runs[-1]))
                rows.append({
                    "run": r["run"],
                    "rounds": r["rounds"],
                    "opens": r.get("opens") or [],
                    "closed": r.get("closed") or [],
                    "passed": bool(r.get("passed")),
                    "first_pass_round": r.get("first_pass_round"),
                    "completed": completed,
                    "phase2": (_p2r if _p2_here else {}),
                    # 这一遍的"起点"（2026-11-15）：第 1 遍必然 0 条配方、无底稿，
                    # 它就是基线；后面各遍带了几条配方、是不是从上一版代码起步，
                    # 决定 Δ 能不能读成"变强了"。老记录没这两项 → 空列表 + 空串。
                    "skills": list((_p2r or {}).get("skills") or []),
                    "skills_known": ("skills" in (_p2r or {})),
                    "draft_from": str((_p2r or {}).get("draft_from") or ""),
                })

            completed_runs = [x for x in rows if x["completed"]]
            n_done = len(completed_runs)
            iters = [int(x["rounds"]) for x in completed_runs]
            med = (int(statistics.median(iters)) if iters else None)
            mx = (max(iters) if iters else None)

            # ── 首轮最常漏的维度（原版卡面 "dimensions most often missed
            #    on the first round"）—— 只看每段的**第一轮**，不混终态 ──
            missed: dict[str, int] = {}
            for r in runs:
                rounds = seg_rounds.get(int(r["run"])) or []
                if not rounds:
                    continue
                for g in (rounds[0].get("gaps_identified") or []):
                    missed[str(g)] = missed.get(str(g), 0) + 1
            first_missed = sorted(missed.items(), key=lambda x: (-x[1], x[0]))[:6]

            # ── 这份 Phase3 账对的是不是**最新那次 Phase2 提交**（2026-11-19）──
            #   Phase2 重跑会覆盖 open_items；Phase3 若超时/被杀，req 里残留的
            #   是上一轮的问答（实测：phase2 三条新批评 + phase3 两条旧问答，
            #   `tool_path_changed` 新旧同名，看起来像"逐条问过"）。面板必须能
            #   把这两种情形分开，否则读账的人（含 AI）会把旧问答当本轮回应。
            _p3_run = int(p3.get("phase2_run") or 0)
            _p2_run = int((p2 or {}).get("run") or 0)
            _p3_stale = False
            if p3.get("divergences") and p2:
                if _p3_run and _p2_run and _p3_run != _p2_run:
                    _p3_stale = True
                elif (not p2.get("verified")
                      and str(p3.get("started_at") or "")
                      and str(p2.get("submitted_at") or "")
                      and str(p3["started_at"]) < str(p2["submitted_at"])):
                    # 老账不带 run 标识时的退化判据：Phase3 起跑早于本轮提交，
                    # 且本轮提交没过 → 这份对账不可能是本轮的。
                    _p3_stale = True

            # ── 分歧分类（原版卡面 "Divergences: none flagged / drift n…"）──
            dv: list[dict[str, Any]] = []
            dcount: dict[str, int] = {}
            for d0 in (p3.get("divergences") or []):
                if not isinstance(d0, dict):
                    continue
                cls = str(d0.get("final_classification")
                          or d0.get("initial_classification") or "").strip()
                dcount[cls] = dcount.get(cls, 0) + 1
                dv.append({
                    "id": str(d0.get("divergence_id") or "")[:60],
                    "cls": cls,
                    "question": str(d0.get("student_facing_question") or "")[:200],
                    "spec": str(d0.get("evidence_from_spec") or "")[:400],
                    "code": str(d0.get("evidence_from_code") or "")[:400],
                    "alignment": str(d0.get("alignment") or ""),
                    "response": str(d0.get("student_response") or "")[:600],
                })

            quad = _quadrants(rec, runs, raw, pr) if runs else {}
            tail = rows[-1] if rows else {}
            out.append({
                "qid": (f.stem[4:] if f.stem.startswith("req_") else f.stem),
                # ⚠️ qid 是 `{req_id}_{dbhash8}`（见 req_registry.skeleton_path:514），
                # 直接拿它当 req_id 去调 _cache_path 会拼出
                # `req_{req_id}_{dbhash}_{dbhash}.json` —— 另一个不存在的文件，
                # 于是"再做一遍"清的是空气（reset 返回 missing），账却看着正常。
                # 这里单独给一个干净的 req_id，只剥最后一段 8 位 hash。
                "req_id": ((f.stem[4:].rsplit("_", 1)[0]
                            if re.fullmatch(r"[0-9a-f]{8}",
                                            f.stem[4:].rsplit("_", 1)[-1] or "")
                            else f.stem[4:])
                           if f.stem.startswith("req_") else f.stem),
                "title": str(meta.get("title") or q)[:80],
                "question": q,
                "unit": str(meta.get("unit") or ""),
                "language": str(meta.get("target_language") or ""),
                "root_scene": d.name,
                "published_at": str(meta.get("published_at") or ""),
                # —— 卡面三个数（原版：Sessions / Spec iterations / Divergences）——
                "sessions": len(runs),
                "completed": n_done,
                "pct": (round(n_done * 100 / len(runs)) if runs else 0),
                "iter_median": med,
                "iter_max": mx,
                "iter_values": iters,
                "div_total": len(dv),
                "div_counts": dcount,
                # 这份对账是否覆盖本轮提交（false = 账上这些问答属于上一轮）
                "p3_stale": _p3_stale,
                "p3_timed_out": bool(p3.get("timed_out")),
                "p3_answered": int(p3.get("answered") or 0),
                "p3_error": str(p3.get("error") or ""),
                "first_missed": [{"id": k, "n": v} for k, v in first_missed],
                "provisional": bool(n_done < 3),
                "llm_ready": bool(dims),
                "dims": dims,
                "milestone": (rec.get("milestone") or {}).get("name") or "",
                "milestone_achieved": done_milestone,
                "expected_drifts": [
                    {"category": str(x.get("category") or ""),
                     "pattern": str(x.get("pattern") or "")}
                    for x in (rec.get("expected_drifts") or [])
                    if isinstance(x, dict)][:6],
                # —— 详情：一条会话账 P1 → P2 → P3 → 里程碑 ——
                "runs": rows,
                "phase1": {
                    # 从 runs_from_iterations 展开，不自取 iterations 的 run/round：
                    # 历史记录里这两个字段**根本没写**（全 0），直接读会显示成
                    # "第 0 段 · 第 0 轮"，六段会话糊成一条。
                    # runs_from_iterations 按"通过后再出现记录即新段"还原段号，
                    # 轮号用段内序号兜底。
                    "rounds": [
                        {"run": int(_r.get("run") or 0),
                         "round": int(_it.get("round") or 0) or (_ri + 1),
                         "questions": [str(y) for y in (_it.get("questions") or [])],
                         "gaps": [str(y) for y in (_it.get("gaps_identified") or [])],
                         "addressed": [str(y) for y in (_it.get("gaps_addressed") or [])],
                         "passed": bool(_it.get("passed")),
                         # ★ Voyager 的**作答正文**：每一轮它交的整篇 spec
                         #   （"已知 / 64 的立方根是 4，因为 4×4×4=64 / 代入…"）。
                         #   以前面板只摆判官问了什么，从不摆它答了什么 ——
                         #   用户问「在哪看得见 Voyager 的回答」多半指这一层。
                         "spec": str(_it.get("spec_text") or "")[:6000],
                         "ts": str(_it.get("ts") or "")}
                        for _r in (srun or [])
                        for _ri, _it in enumerate(_r.get("rounds") or [])][-40:],
                    "addressed": sorted({
                        str(y) for x in raw for y in (x.get("gaps_addressed") or [])}),
                    "open_now": [str(y) for y in (raw[-1].get("gaps_identified") or [])]
                                if raw else [],
                },
                "phase2": p2 or {},
                "phase2_recorded": bool(p2),
                # Voyager 的**口头作答**（旧消费线留在 coach_log 的 answer）。
                # 与 phase2.finalCode 是两种形态，别混：一个交程序，一个说话。
                "answers": _answers_for(q, d.name, _ans_pool),
                "phase3": {"divergences": dv,
                           "revision_choice": str(p3.get("revision_choice") or ""),
                           "counts": dict(p3.get("counts") or {}),
                           "skipped": [str(x)[:200] for x in (p3.get("skipped") or [])][:4]},
                "quad": quad,
                "points": [{"x": i + 1, "y": y}
                           for i, y in enumerate((tail.get("opens") or []))],
                "opens_now": (tail.get("opens") or [None])[-1] if tail else None,
                "passed_last": bool(tail.get("passed")),
                # spec_progress 不产 last_ts（它只算形状）；活动时间取最后一轮的 ts。
                # 之前这里读 pr["last_ts"] 恒 None → 排序与"谁更新"全失效。
                "last_ts": str((raw[-1].get("ts") if raw else "") or ""),
                "delta": pr.get("delta"),
                "monotonic": bool(pr.get("monotonic")),
                # 「再做一遍」的台账 —— 每次重做前清了哪些背包项（见
                # maieutic_scaffold.reset_practice_state）。面板据此把这一段
                # 标成「第 N 遍 · 从零」，而不是"接着上一遍继续"。
                "redos": [x for x in (rec.get("redos") or [])
                          if isinstance(x, dict)][-10:],
            })
    # ── 按题面归并（同一句被发布多次 = 同一道练习的多份派发）──────────
    # 实测（Scene T）：同一句点了 8 次「启动学习」→ 8 个 req、8 张卡片，
    # 内容全一样、会话全是 0。原版一道 Exercise 只有一行，重复发布不会
    # 多出一张卡；MVE 的 req 是"每次派发一份骨架"的粒度，比 Exercise 细一层。
    # 列表按 (场景, 题面) 归并，`reqs` 记派发了几份，点进去仍能看到每份的账。
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for e in out:
        k = (str(e.get("root_scene") or ""), str(e.get("question") or ""))
        g = merged.get(k)
        if g is None:
            e = dict(e)
            e["reqs"] = 1
            e["req_ids"] = [e["qid"]]
            merged[k] = e
            continue
        # 会话与完成数相加；P2/P3/四象限取**最新那份派发**（它才是当前的账）
        g["sessions"] += int(e.get("sessions") or 0)
        g["completed"] += int(e.get("completed") or 0)
        g["reqs"] += 1
        g["req_ids"].append(e["qid"])
        # 重做台账按派发顺序拼（带上 req，才知道是哪一份被重做过）
        for _rd in (e.get("redos") or []):
            _rd = dict(_rd)
            _rd["req"] = e["qid"]
            g.setdefault("redos", []).append(_rd)
        for r in (e.get("runs") or []):
            r = dict(r)
            r["req"] = e["qid"]
            g["runs"].append(r)
        # 标题/单元：哪份有教师起的名字就用哪份（题面兜底出来的不算"起过名"）
        for f in ("title", "unit", "language", "published_at"):
            _gv, _ev = str(g.get(f) or ""), str(e.get(f) or "")
            _g_default = (f == "title" and _gv == str(g.get("question") or ""))
            if _ev and (not _gv or _g_default):
                g[f] = e[f]
        if (e.get("last_ts") or "") >= (g.get("last_ts") or ""):
            for f in ("qid", "req_id", "last_ts", "phase2", "phase2_recorded", "phase3",
                      "quad", "delta", "monotonic", "answers",
                      "expected_drifts", "milestone",
                      "milestone_achieved", "dims", "llm_ready", "points",
                      "opens_now", "passed_last", "phase1"):
                if f in e:
                    g[f] = e[f]
        # 首轮最常漏的维度：多份派发的次数相加（口径一致，都是"每段第一轮"）
        acc = {x["id"]: x["n"] for x in (g.get("first_missed") or [])}
        for x in (e.get("first_missed") or []):
            acc[x["id"]] = acc.get(x["id"], 0) + x["n"]
        g["first_missed"] = [{"id": k2, "n": v2} for k2, v2 in
                             sorted(acc.items(), key=lambda y: (-y[1], y[0]))[:6]]
        # 分歧逐条相加（同一份题的分歧来自不同派发，不重复计同一 id）
        seen = {d["id"] for d in (g["phase3"].get("divergences") or [])} \
            if isinstance(g.get("phase3"), dict) else set()
        for d0 in ((e.get("phase3") or {}).get("divergences") or []):
            if d0.get("id") not in seen and isinstance(g.get("phase3"), dict):
                g["phase3"]["divergences"].append(d0)
                seen.add(d0.get("id"))
    out = list(merged.values())
    for e in out:
        # 归并后重算派生量：完成率 / 轮数中位数与最大 / 分歧条数与分类
        e["pct"] = (round(e["completed"] * 100 / e["sessions"])
                    if e["sessions"] else 0)
        iters = [int(r.get("rounds") or 0) for r in (e.get("runs") or [])
                 if r.get("completed")]
        e["iter_values"] = iters
        e["iter_median"] = (int(statistics.median(iters)) if iters else None)
        e["iter_max"] = (max(iters) if iters else None)
        dv = (e.get("phase3") or {}).get("divergences") or []
        e["div_total"] = len(dv)
        cnt: dict[str, int] = {}
        for d0 in dv:
            cnt[str(d0.get("cls") or "")] = cnt.get(str(d0.get("cls") or ""), 0) + 1
        e["div_counts"] = cnt
        e["provisional"] = bool(e["completed"] < 3)
        # ★ 归并后重算跨段 delta（2026-11-15）：原来直接沿用**单份派发**的
        #   pr["delta"]，同一句被发布多次时，delta 说的是"那份里的最后两段"，
        #   而不是"这道题的最后两段" —— 第 2 遍与第 1 遍的对比会指向错的段。
        _rr = [r for r in (e.get("runs") or []) if isinstance(r, dict)]
        if len(_rr) >= 2:
            _a, _b = _rr[-2], _rr[-1]
            _fa, _fb = _a.get("first_pass_round"), _b.get("first_pass_round")
            e["delta"] = {
                "rounds": int(_b.get("rounds") or 0) - int(_a.get("rounds") or 0),
                "first_pass_round": (None if (_fa is None or _fb is None)
                                     else int(_fb) - int(_fa)),
                "from_run": _a.get("run"), "to_run": _b.get("run"),
            }
        else:
            e["delta"] = None
    out.sort(key=lambda r: (r.get("last_ts") or "", r.get("qid") or ""),
             reverse=True)
    return out[:limit]


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
    # —— 题内缺口序列（2026-11-14 起是进步的主口径，取代 mastered_* 曲线）——
    # 场景栏只需要"最近这道题还剩几个缺口"，不拉全部轨迹。
    try:
        _tc = _trace_curve(scene_id=scene_id, limit=1)
    except Exception:
        _tc = []
    _t = _tc[0] if _tc else {}
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
        # 进步主口径：最近一题的收敛状态（None = 这场景还没有题内轨迹）
        "trace_open": _t.get("opens_now"),
        "trace_rounds": _t.get("rounds"),
        "trace_runs": _t.get("n_runs"),
        "trace_passed": _t.get("passed"),
        "trace_question": str(_t.get("question") or "")[:40],
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


def _sessions_view(scene_id: str) -> list[dict[str, Any]]:
    """需求 · 会话视图（2026-11-09 第三刀 E）。

    用户指令：「其实会话应该是可以复用技能页面的」
    —— 所以**不新开页面**，会话的视图挂在技能页里。

    这个函数只做**聚合**，不做任何推断：
    - 需求身份：`req_registry`（人导入那一刻分配，与问句文本无关）
    - 会话内容：`session_recall.recall`（按 req id **精确**召回）
    - 会话阶段与事件流：`session_state`（照抄原版 Session / SessionEvent）
    - 会话包含哪些技能：按 `blueprint.req_id` **精确**反查技能库

    ⚠️ **不猜**：
      - 找不到会话文件 → 如实说「还没拆骨架」，不拿别的需求的会话顶替
      - 老技能没有 `blueprint.req_id` → 不按 milestone 标题猜（可重名），
        如实标「未归属」
    """
    try:
        import req_registry as _rr
        import session_recall as _sr
    except Exception:
        return []
    try:
        reqs = _rr.list_reqs(scene_id)
    except Exception:
        return []

    # 技能库按 req_id 建索引（精确连接）。一次全表扫 + 建索引，
    # 不为每个需求单独扫一遍技能库。
    #
    # ★ 一对多（2026-11-11 用户：「按现在的会话详情页对技能一对多就好啦」）：
    #   一个会话列出它学成的**全部**技能。两条连接并用、缺一不可：
    #     ① `blueprint.req_ids`（列表）—— 这个会话确实写过/学过它（精确）；
    #     ② 同一个 **topic**（= learn_<md5(问句)>）—— 同一道题再派时技能常一字
    #        未变、走 skipped 分支，历史条目只有旧的单值 req_id，靠 ② 才不至于
    #        在新会话里漏掉（实测 learn_9b8c863d::example_count 停在旧 req）。
    #   老条目没有 req_ids → 单值 req_id 兜底（不按标题猜，标题可重名）。
    import hashlib as _hl

    def _topic_of(q: str) -> str:
        return "learn_" + _hl.md5(str(q or "").strip().encode("utf-8")).hexdigest()[:8]

    by_req: dict[str, list[dict[str, Any]]] = {}
    by_topic: dict[str, list[dict[str, Any]]] = {}
    unowned = 0
    try:
        for s in skill_store.all_skills():
            bp = s.get("blueprint") or {}
            rids = [str(x) for x in (bp.get("req_ids") or []) if str(x or "").strip()]
            if not rids:
                one = str(bp.get("req_id") or "").strip()
                rids = [one] if one else []
            topic = str(s.get("topic") or "")
            if not rids and not topic.startswith("learn_"):
                unowned += 1
                continue
            row = {
                "key": str(s.get("key") or ""),
                "name": str(s.get("name") or ""),
                # dimension 非空 = 维度配方（会话的构件）；空 = 会话级（task 级）
                "dimension": str(bp.get("dimension") or ""),
                "question": str(bp.get("question") or ""),
                "source": str(s.get("source") or ""),
                "versions": int(s.get("versions") or 1),
                "sessions": len(rids),
                "hits": int(s.get("hits") or 0),
                "ok": int(s.get("ok") or 0),
                "runs": int(s.get("runs") or 0),
                # 技能本体（跑通的那段程序/SQL）—— 详情页可展开看，默认折叠
                "code": str(s.get("code") or "")[:4000],
            }
            for rid in rids:
                by_req.setdefault(rid, []).append(dict(row))
            if topic.startswith("learn_"):
                by_topic.setdefault(topic, []).append(dict(row))
    except Exception:
        by_req, by_topic = {}, {}

    def _skills_of(rec: dict[str, Any]) -> list[dict[str, Any]]:
        """这个会话学成的技能（**一对多**）：精确归属 ∪ 同 topic。

        - 精确归属：`blueprint.req_ids` / 单值 `req_id` 里有这个 req
        - 同 topic：`learn_<md5(问句)>` 相同 —— 同一道题的不同派发共享同一批
          技能（同题再派时技能常走 skipped 分支，归属字段不会刷新）
        去重按 key；**会话级（task 级）排前面**，维度配方排后面，详情页按这两组渲染。
        """
        rid = str(rec.get("req_id") or "")
        _qs = [str(rec.get("question") or "")] + [
            str(x) for x in (rec.get("known_questions") or [])]
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in by_req.get(rid, []):
            _k = str(row.get("key") or "")
            if _k and _k not in seen:
                seen.add(_k)
                rows.append(row)
        for q in _qs:
            for row in by_topic.get(_topic_of(q), []):
                _k = str(row.get("key") or "")
                if _k and _k not in seen:
                    seen.add(_k)
                    rows.append(row)
        rows.sort(key=lambda r: (1 if r.get("dimension") else 0,
                                 str(r.get("dimension") or "")))
        return rows

    out: list[dict[str, Any]] = []
    for rec in reqs:
        rid = str(rec.get("req_id") or "")
        if not rid:
            continue
        item: dict[str, Any] = {
            "req_id": rid,
            "question": str(rec.get("question") or ""),
            "known_questions": [str(x) for x in (rec.get("known_questions") or [])],
            "created_at": float(rec.get("created_at") or 0),
            "has_session": False,
            "phase": 0,
            "phase_name": "未开始",
            "archived": False,
            "completed_at": 0.0,
            "dims": [],
            "closed": 0,
            "total": 0,
            "verified": {},
            "unclosed": [],
            "skills": _skills_of(rec),
            "events": [],
            "why": "",
            # 维度级学习账（2026-11-10）：学习侧闭合过程的只读投影。
            # 面板据此展示「每维试了几轮 / 判官说什么 / 验出什么」，
            # 不必再靠消费侧问一句来窥视学习结果。
            "dim_detail": [],
            "rounds": {},
            "is_target": False,
        }
        try:
            sess = _sr.recall(rid, scene_id)
        except Exception as e:
            item["why"] = f"会话读取失败：{type(e).__name__}: {e}"
            out.append(item)
            continue
        if sess.get("found"):
            item["has_session"] = True
            sm = sess.get("session") or {}
            item["phase"] = int(sm.get("phase") or 0)
            item["phase_name"] = str(sm.get("phase_name") or "未开始")
            item["archived"] = bool(sm.get("archived"))
            item["completed_at"] = float(sm.get("completed_at") or 0)
            item["events"] = [e for e in (sm.get("events") or [])
                              if isinstance(e, dict)]
            item["dims"] = sess.get("dims") or []
            item["total"] = len(item["dims"])
            item["closed"] = len(sess.get("learned") or [])
            try:
                item["verified"] = _sr.verified_answers(sess)
            except Exception:
                item["verified"] = {}
            try:
                item["unclosed"] = _sr.unclosed_dims(sess)
            except Exception:
                item["unclosed"] = []
            # 维度级明细：骨架维度 × 闭合状态 × 过程账 × 已验结果。
            # 四份数据都已在 sess 里（dims / learned / iterations / codes），
            # 这里只做对齐拼接，不推断、不补默认值假象（没试过的就是 rounds=0）。
            _codes = sess.get("codes") or {}
            _iters = sess.get("iterations") or {}
            _learned = {str(x) for x in (sess.get("learned") or [])}
            _dd: list[dict[str, Any]] = []
            for _d in item["dims"]:
                _did = str(_d.get("id") or "")
                _c = _codes.get(_did) or {}
                _it = _iters.get(_did) or {}
                _dd.append({
                    "id": _did,
                    "question": str(_d.get("question") or ""),
                    "why": str(_d.get("why") or ""),
                    "landing": str(_d.get("landing") or ""),
                    "closed": _did in _learned,
                    "rounds": int(_it.get("rounds") or 0),
                    "asked": str(_it.get("asked") or ""),
                    "gap": str(_it.get("gap") or ""),
                    "passed": bool(_it.get("passed")),
                    "r": (float(_c.get("r", -1.0)) if _c else None),
                    "how": str(_c.get("how") or ""),
                    "answer": str(_c.get("answer") or ""),
                    "code": str(_c.get("code") or ""),
                    "tools": list(_c.get("tools") or []),
                })
            item["dim_detail"] = _dd
            item["rounds"] = dict(sess.get("rounds") or {})
        else:
            item["why"] = str(sess.get("why") or "还没有会话记录")
        out.append(item)
    # 当前学习锚点（maieutic_scaffold 的 current_target）—— 消费侧每问一句
    # 都会立锚点，学习侧下一轮优先攻它。面板标出来，人才知道「它现在在学哪句」。
    _tgt_q = ""
    try:
        import maieutic_scaffold as _msc
        _tgt_q = str(_msc.current_target(scene_id) or "")
    except Exception:
        _tgt_q = ""
    if _tgt_q:
        for _it in out:
            if str(_it.get("question") or "") == _tgt_q:
                _it["is_target"] = True
    return out


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
    # 学习轨迹账（2026-11-18）：轨迹页/概览判据区改读 learn_trace.jsonl。
    # run_log 是原版练习线的账（coverage/score 恒空、里程碑点亮了还记
    # failed），新后端的真实指标（Phase1/Phase2/取数/里程碑/证据门）在
    # learn_trace 里。
    try:
        import learn_trace as _lt
        _trace = _lt.for_scene(sid)
    except Exception:
        _trace = []
    # 概览判据区 KPI（2026-11-18 换账本）：全部从 learn_trace 现算，
    # 不再读 run_log 的 success/行数（那是原版练习线的账）。
    #   entered = 这轮真进了执行（Phase2 交付 / 调过取数 / 真取到）
    #   fetched = 真取到数
    #   lit     = 里程碑点亮（真做出来了的最硬信号）
    try:
        _entered_n = sum(1 for _r in _trace
                         if _r.get("phase2_submitted")
                         or int(_r.get("fetch_calls") or 0) > 0
                         or _r.get("fetch_ok"))
        _trace_kpi = {
            "rounds": len(_trace),
            "entered": _entered_n,
            "fetched": sum(1 for _r in _trace if _r.get("fetch_ok")),
            "lit": sum(1 for _r in _trace if _r.get("lit")),
            "last": dict(_trace[-1]) if _trace else None,
        }
    except Exception:
        _trace_kpi = {"rounds": 0, "entered": 0, "fetched": 0, "lit": 0, "last": None}
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
            # 配方本体（2026-11-10 用户定：零碎技能人直接看，不走召回不占消费侧
            # 额度 —— 卡片展开即可读 code，不用去翻库）。
            "code": str(s.get("code") or ""),
            "hits": int(s.get("hits") or 0),
            "ok": int(s.get("ok") or 0),
            # runs = 注入后真进了执行的轮数（2026-11-18 分母修正：
            # 真取到率 = ok/runs；hits 只作注入次数读数）
            "runs": int(s.get("runs") or 0),
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
                round(int(s.get("ok") or 0) / int(s.get("runs") or 1), 3)
                if int(s.get("runs") or 0) else None
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
            # 题内缺口序列：进步的主曲线（取代上面那条累计掌握数，2026-11-14）
            "trace_curve": _trace_curve(scene_id=sid),
            # 能力模块四象限（同构助产士 CohortNarrativeOutput，2026-11-14）
            "cohort": _cohort_narrative(scene_id=sid),
            # 练习索引（同构助产士 Instructor · Exercises）：概览页的卡片 + 详情
            # 全从这一份读，不再二次计算（见 _exercise_index 的注释）。
            "exercises": _exercise_index(scene_id=sid),
            "recommend": recommend,
            "causal": causal,
            "tasks": _task_library(),
            "skills": memory,
            "skill_stats": skill_stats,
            "skill_detail": skill_detail,
            "mc_thinking": _mc_thinking_view(),
            # 需求 · 会话（第三刀 E）：**不新开页面**，视图挂在技能页里。
            # 这里只聚合，不推断 —— 见 _sessions_view 的注释。
            "sessions": _sessions_view(scene_id=sid),
            # 摸底/考核走的是 exam_log.jsonl，**不写** run_log.jsonl（有意分开：
            # run_log 是带图谱的练习覆盖率，恒 100%，画出来是假平线）。
            # 所以"run_log 为空"≠"没有任何数据" —— 摸底跑完了一样要给看。
            # 漏掉这一条的实测后果：线上摸底跑完 7 道题，总览页仍显示
            # 「还没有任何运行记录」，真实水平曲线根本不出现。
            "exam_curve": _exam_view(),
            # 导航上的「轨迹 N 轮」读它；不给就是 "undefined 轮"
            "total_rounds": 0,
            "trace_kpi": _trace_kpi,
            "topic_id": "",
            "hint": ("还没有练习记录。摸底/考核不入 run_log"
                     "（走 exam_log）——有裸考数据时下面会照常显示。"),
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
        # 题内缺口序列：进步的主曲线（取代累计掌握数，2026-11-14）
        "trace_curve": _trace_curve(scene_id=sid),
        # 能力模块四象限（同构助产士 CohortNarrativeOutput，2026-11-14）
        "cohort": _cohort_narrative(scene_id=sid),
        "exercises": _exercise_index(scene_id=sid),
        "recommend": recommend,
        "causal": causal,
        "attempted": attempted,
        "failed": failed,
        "total_rounds": len(series),
        "series": series,
        # 学习轨迹账（MVE 现账，2026-11-18）：轨迹页/判据区读它
        "trace": _trace,
        "trace_total": len(_trace),
        # 概览判据区（2026-11-18 换账本）：当前题判据/真取到率/累计轮次读它
        "trace_kpi": _trace_kpi,
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
        # 需求 · 会话（第三刀 E）：技能页里的会话区块读它。
        "sessions": _sessions_view(scene_id=sid),
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
    # 背包 + 材料（2026-11-18 用户拍板「只有一个 voyager 背包，内容物是跑出来的」）：
    # 面板不再让人选/新建场景，所以这里直接把当前工作区的背包与材料根摆出来。
    # 口径与 curriculum_auto 完全一致（同一份账，不另算一套）：
    #   Inventory (used/36)，格 = 一次会话，容量是常量（curriculum.py:205）。
    inventory = {"used": 0, "cap": 36, "slots": []}
    material = ""
    try:
        import curriculum_auto as _ca_inv
        import scene_profile as _sp_inv
        _prof_inv = _sp_inv.profile(current) or {}
        _ag_inv = _ca_inv.CurriculumAgent(
            current, scene_dir=str(_prof_inv.get("scene_dir") or ""))
        inventory = {"used": _ag_inv.inventory_used,
                     "cap": getattr(_ca_inv, "INV_CAP", 36),
                     "slots": [str((v or {}).get("q") or "")[:80]
                               for v in list(_ag_inv.slots.values())[-6:]]}
        material = str(_ag_inv._material_root() or "")
    except Exception:
        pass

    return {"scenes": scene_list, "current": current,
            "exclusive_nodes": exclusive, "graph_sparsity": sparsity,
            "entity_catalog": catalog, "fingerprint": fp,
            "action_count": acts, "pending_nodes": pending,
            "curve_summary": curve,
            # 猫爪总开关（同构 neko）：每场景的能力组 + 组内条目 + 开关状态；
            # scene_backend 说明这场景的数据源是哪一档（vlml / local / scene）。
            "groups": groups, "scene_backend": backend,
            # 单工作区视图：背包 + 材料（面板顶栏只渲染这两个）
            "inventory": inventory, "material": material}


# ⚠️ 跨场景技能溯源（skill_traceability）已并入技能页（2026-10-18 去重）：
#   /scenes 独立页撤掉后，它的唯一消费方没了。归属场景 + 复用健康度改由
#   `build_state().skill_detail` 直接供（source_scene 字段 + hits/ok 前端
#   按同一判据算 ok/stale/unused），不再单开一个 API 和一份重复遍历。


if __name__ == "__main__":
    print(as_json())
