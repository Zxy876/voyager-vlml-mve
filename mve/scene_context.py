#!/usr/bin/env python3
"""MVE 等价「渐进披露初始场景注入」（Task 2，2026-10-12 落地）。

把 Minecraft 的「初始场景注入」（curriculum.py:142 render_observation 取
biome / nearby_blocks / inventory / chests；warm_up :95-109 渐进披露）翻译成
MVE 的对应物：

  MC 元素               MVE 等价物                      来源
  ──────────────────────────────────────────────────────────────────────
  biome（世界）         数据源身份（vlml_vlr / vlml_rib / 默认库）  db_config
  nearby_blocks（眼前） 当前可见的「表 + VLML 10 工具」（base 层恒给，
                         ext_* 层进度够了才给）                        information_schema
  inventory（背包）     实体可填值（锚点 series/team/map + 目录可选项）
                        + 已掌握的知识点                               anchor / entity_catalog / 掌握度
  chests（箱内存货）    技能库图谱（已学会的技能/配方）                 skill_store / 掌握度

关键：这是「场景层」的渐进披露，**独立于** voyager.py graph_context 的「支架层」
（full/partial/none）。支架层管"给多少答案骨架"，场景层管"允许看到哪些数据/工具"。
两层正交：一个新手可能场景全开（看得全库）但支架全给（答案扶到底）；一个老手
可能场景只给 base 表但支架全撤（自己下钻 ext）。

为什么必须做（MC 教训）
------------------------
MC 的 warm_up 是"随进度放开更多世界状态"——一开始就给全图会让智能体无从下手、
也测不出它到底会不会用基础工具。MVE 此前把所有表一股脑塞进 prompt，模型跳过
基础层直接裸写 ext 查询 → 在缺表库上必然 0 分。场景层渐进披露让"基础层没掌握
就别碰 ext"成为**结构性约束**，而不是靠 prompt 说教。

用法
----
    from scene_context import scene_injection
    text = scene_injection(topic_id, progress_tier=0)   # 给模型 prompt 用
    # 或命令行看当前库两档场景：
    python mve/scene_context.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DB_CONFIG = HERE / "db_config.json"

# VLML 10 工具 = 跨域基线（用户指定：技能/动作锁定语义层，不写裸 SQL）。
# 换库只是"该层题有没有数据"，工具集恒定 → 技能可迁移。
SEMANTIC_TOOLS: list[str] = [
    "match_analysis_report", "match_summary_report", "match_players_report",
    "match_rounds_report", "match_economy_report", "player_profile_report",
    "scouting_report", "pattern_detection_report",
    "query_sql", "get_database_info",
]

# 进度档位：0 = 新手（只看 base 表），1 = 已解锁基础层（放开 ext 表）
TIER_BASE = 0
TIER_EXT = 1
# 升到 ext 档需要已解锁几个里程碑。用**里程碑数**而不是掌握度，是因为
# 掌握度是 VLML 的评分结果 —— 评分只进面板，不能当披露/推进的门槛
# （用户原话：「评分结果不被任何推进条件读到」）。
TIER_EXT_UNLOCKS = 3

# VLML 已知表（静态回退用）：duckdb 不在时也能给出有意义的"场景"，
# 不必每次出题都实查库（也避免慢查询）。实查到时以实查为准。
KNOWN_BASE_TABLES = [
    "series", "games", "rounds", "base_events",
    "agg_team_series_stats", "agg_player_series_stats", "ext_player_game_stats",
]
KNOWN_EXT_TABLES = [
    "ext_player_round_stats", "ext_round_economy",
    "ext_game_economy", "ext_player_side_stats",
]


def data_source_identity() -> str:
    """biome 等价物：当前用的是哪个数据源（库文件名决定）。"""
    try:
        obj = json.loads(DB_CONFIG.read_text(encoding="utf-8"))
        p = str((obj or {}).get("db_path") or "")
    except Exception:
        p = ""
    # db_path 为空时用的是**默认库**（db_switch.DEFAULT_DB，当前 rib.gg）——
    # 标签要跟着默认库走，不能再写死 "vlml_events(默认)"（2026-10-13 起因：
    # 默认库改成 rib 后，这个标签会指着一个没在用的库）。
    name = Path(p).stem if p else Path(_default_db()).stem
    if "vlr" in name:
        return f"{name}（vlr.gg 源：回合级 + 聚合级，无 ext 层）"
    if "rib" in name:
        return f"{name}（rib.gg 源：含首血/经济 ext 层）"
    return f"{name}（默认库）" if not p else f"{name}（GRID 单场切片）"


def _default_db() -> str:
    """默认库路径（唯一定义在 db_switch，当前 rib.gg）。"""
    from db_switch import DEFAULT_DB
    return str(DEFAULT_DB)


def _db_path() -> str:
    """当前库路径。**相对路径按仓库根解析**（与 vlml_env._resolve_db_path 同口径）。

    ⚠️ 为什么不原样返回配置值：vlml_env 会把 cwd 切到 `vlml/`，于是相对路径
       在它那儿会被拼两遍。两边口径必须一致，否则面板显示"已切到 rib 库"、
       实际连的还是默认库 —— 2026-10-13 在服务器上就是这么栽的。
    """
    try:
        obj = json.loads(DB_CONFIG.read_text(encoding="utf-8"))
        p = str((obj or {}).get("db_path") or "")
    except Exception:
        p = ""
    if not p:
        return _default_db()
    return str(((HERE.parent / Path(p).expanduser()).resolve())
               if not Path(p).expanduser().is_absolute()
               else Path(p).expanduser())


def available_tables(progress_tier: int = TIER_BASE) -> dict[str, list[str]]:
    """nearby_blocks 等价物：当前可见的表（按 base / ext 分层）。

    progress_tier < 1 时只返回 base 表；>= 1 时才把 ext_* 表一并返回。
    这是"渐进披露"的核心——新手看不到 ext 层，就无从裸写 ext 查询。

    优先实查库（duckdb 在时）；不在则以静态已知表清单回退（按数据源身份
    决定是否含 ext 层），保证任何环境都有有意义的场景描述。
    """
    out: dict[str, list[str]] = {"base": [], "ext": [], "source": "static"}
    live: list[str] = []
    try:
        import duckdb
        from pathlib import Path as _P
        p = _db_path()
        if _P(p).exists():
            con = duckdb.connect(p, read_only=True)
            try:
                live = [str(r[0]) for r in con.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='main' ORDER BY 1").fetchall()]
            finally:
                con.close()
    except Exception:
        live = []
    if live:
        out["source"] = "live"
        for t in live:
            (out["ext"] if t.startswith("ext_") else out["base"]).append(t)
    else:
        # 静态回退：vlr/默认库没有 ext 层，rib 库有
        name = Path(_db_path()).stem
        has_ext = "rib" in name
        out["base"] = list(KNOWN_BASE_TABLES)
        out["ext"] = list(KNOWN_EXT_TABLES) if has_ext else []
    if progress_tier < TIER_EXT:
        out["ext"] = []  # 新手档：隐藏 ext 层
    return out


def entity_fillables() -> dict[str, list[str]]:
    """inventory 等价物：实体可填值——锚点（当前那场/队/图）+ 目录可选项。"""
    inv: dict[str, list[str]] = {"anchor": [], "options": {}}
    try:
        import anchor
        a = anchor.get()
        inv["anchor"] = [f"series={a.get('series','')}", f"team={a.get('team','')}",
                         f"map={a.get('map','')}"]
    except Exception:
        pass
    try:
        import entity_catalog
        for kind in ("series", "team", "map", "player"):
            vals = entity_catalog.options(kind)
            if vals:
                inv["options"][kind] = vals[:12]  # 下拉预览，截前 12
    except Exception:
        pass
    return inv


def _skill_count() -> int:
    """技能库里现有几条配方（**不含任何评分**）。"""
    try:
        import skill_store
        return len(skill_store.load())
    except Exception:
        return 0


def panel_only() -> dict[str, Any]:
    """**只给人看、绝不进模型上下文**的那一半。

    用户划的线很明确：
      进运行时上下文的 —— 解锁里程碑 / 学会技能 / 任务失败 / 任务跳过（带原因）
                          + 环境反馈（工具成没成、取到几行、图谱变了什么）
      只进面板的       —— ① 当前实体在库里实际存在哪些取值
                          ② VLML 跑一遍得到的判定结果（得分 / 覆盖率）
    为什么 ① 不能进上下文：那是一份答案候选清单，会把它的重心从"把取数做通"
    拉到"枚举取值凑答案"。
    为什么 ② 不能进上下文：那是尺子，不是反馈。尺子进了反馈环，它就会去讨好
    尺子（照覆盖率调 SQL），而不是完成任务 —— 用户原话：「会影响 voyager
    完成任务的重心」。

    面板从这里取数，别自己去 MasteryStore / entity_catalog 里翻。
    """
    out: dict[str, Any] = {
        "entity_values": {}, "mastery": {}, "skill_count": _skill_count(),
    }
    try:
        import entity_catalog
        for kind in ("series", "team", "map", "player"):
            vals = entity_catalog.options(kind)
            if vals:
                out["entity_values"][kind] = vals
    except Exception:
        pass
    try:
        from topic_graph import MasteryStore
        ms = MasteryStore()
        out["mastery"] = {
            tid: {"mastery": r.get("mastery"), "completed": ms.is_completed(tid),
                  "stale": ms.is_stale(tid), "attempts": r.get("attempts", 0)}
            for tid, r in ms.all().items()
        }
    except Exception:
        pass
    return out


def mastered_skills() -> dict[str, Any]:
    """chests 等价物：技能库图谱——已掌握的知识点（=已学会的"配方"）。

    ⚠️ **面板专用**：这里读的是掌握度（≥0.8），属「评分结果」，
    按约定只进面板（`panel_only()`），不进 `scene_injection()`。
    """
    out: dict[str, Any] = {"mastered_topics": [], "skill_count": 0}
    try:
        from topic_graph import MasteryStore
        ms = MasteryStore()
        for tid, row in ms.all().items():
            m = row.get("mastery")
            if m is not None and float(m) >= 0.8:
                out["mastered_topics"].append(tid)
    except Exception:
        pass
    try:
        import skill_store
        out["skill_count"] = len(skill_store.load())
    except Exception:
        pass
    return out


def unlocked_milestones() -> list[str]:
    """里程碑等价物：已**完成**（任务跑通）的知识点 = 已解锁的图谱节点。进上下文。

    同构原版 curriculum.render_observation 把 completed_tasks 写进 human message
    —— MVE 的「已完成」就是 topic_graph 的 completed 标记（任务跑通），标进上下文
    让 agent 知道「哪些里程碑已经拿下了，不用再绕」。这是布尔 completed，**不是
    掌握度阈值**——评分/掌握度只进面板，不在此处驱动任何逻辑。
    """
    try:
        from topic_graph import MasteryStore
        ms = MasteryStore()
        return [tid for tid, r in ms.all().items() if ms.is_completed(tid)]
    except Exception:
        return []


def milestone_tree() -> dict[str, Any]:
    """MC 节点里程碑树（进入上下文）。

    同构原版 Voyager 的「里程碑 = 已完成的任务」+ MC 的「必须先砍树」科技树：
      · seed    —— 树干起点（无前置），对应 MC 的 Mine 1 wood log；MVE 里是
                  基础粒度知识点，且出生注入会随机给一个最小单位 (表,列) 作为
                  真正的「第一块木头」。
      · trunk   —— 最长前置路径（设计原文「经过节点最多的路径上的节点就是里程碑」）。
      · milestone —— trunk 上的节点集合。
      · keystone —— betweenness 最高的节点（「经过路径最多的节点」= 树干最粗处）。
    返回结构含每个里程碑的 completed 状态，供面板/上下文展示进度。
    """
    out: dict[str, Any] = {
        "seed": None, "trunk": [], "milestones": [], "keystone": None,
        "completed_n": 0, "total_n": 0, "progress": "0/0",
    }
    try:
        from topic_graph import MasteryStore, TopicGraph
        ms = MasteryStore()
        g = TopicGraph()
        info = g.milestone_info()
        trunk = info["trunk"]
        milestones = set(info["milestones"])
        done = [t for t in trunk if ms.is_completed(t)]
        out["seed"] = info["seed"]
        out["trunk"] = trunk
        out["milestones"] = sorted(milestones)
        out["keystone"] = info["keystone"]
        out["keystone_score"] = info.get("keystone_score")
        out["completed_n"] = len(done)
        out["total_n"] = len(trunk)
        out["progress"] = f"{len(done)}/{len(trunk)}"
        out["completed_list"] = done
    except Exception:
        pass
    return out


def skip_history(limit: int = 8) -> list[dict[str, Any]]:
    """跳过任务（带原因）等价物：把 stale-skip / 放弃 写进上下文（设计⑨：跳过任务
    带原因进运行时上下文）。读循环日志无关的节点状态（MasteryStore 的 skips）。
    """
    out: list[dict[str, Any]] = []
    try:
        from topic_graph import MasteryStore
        ms = MasteryStore()
        for tid, r in ms.all().items():
            for s in (r.get("skips") or [])[-limit:]:
                out.append({"topic": tid, "reason": s.get("reason", "")})
    except Exception:
        return []
    return out[-limit:]


def learned_skills(limit: int = 5) -> list[dict[str, Any]]:
    """学会技能等价物：技能库里已固化的取数配方（SQL 源码）。进上下文，供复用。

    同构原版 SkillManager —— chests 里的存货下次能直接调。只列 top（按
    ok/hits 有效性排序），避免 prompt 膨胀。设计⑦要求技能存的是「可复用代码
    （SQL）」，不是一两句思路，所以这里给的是 code 片段，不是自然语言。
    """
    try:
        import skill_store
        rows = skill_store.all_skills()
        rows.sort(key=lambda s: -(int(s.get("ok") or 0)
                                  / max(int(s.get("hits") or 1), 1)))
        return [{"topic": s.get("topic", ""), "name": s.get("name", ""),
                 "code": str(s.get("code") or s.get("text") or "")}
                for s in rows[:limit]]
    except Exception:
        return []


def failure_history(limit: int = 8) -> list[dict[str, Any]]:
    """任务失败 / 跳过（带原因）等价物：把 failed tasks 写进上下文（同构原版
    curriculum.render_observation 的 failed_tasks）。

    设计⑨明确：任务失败、跳过任务（原因…）要进运行时上下文。这里读的是循环日志
    run_log（循环写的那份），不是面板状态文件——面板只读它、不回写上下文。
    注意：只给「漏了哪些维度（枚举）」和 verdict，**不给覆盖率数值**（约定⑧）。

    ⚠️ 必须**去重**（2026-10-13 实测）：同一个知识点连着栽 7 次，这里就会吐
      7 行一模一样的 `vlml_first_blood（wrong）：wrong`。那不是"历史"，是噪声
      —— 白占上下文预算，而且「带原因」变得名不副实（`wrong` 不是原因）。
      去重按 (知识点, verdict, 原因) 三元组，保留**最近**那一条并记上次数，
      「栽过几次」这个信息比重复七遍有用。
    """
    out: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    try:
        import run_log
        for r in run_log.load_all():
            v = str(r.get("verdict") or "")
            if v in ("wrong", "dont_know") or r.get("done") is False:
                tid = r.get("topic_id") or r.get("task") or ""
                miss = [str(m) for m in (r.get("missing") or [])]
                reason = "、".join(m.split("(")[0] for m in miss[:3]) or v
                key = (tid, v, reason)
                if key in seen:
                    seen[key]["times"] = int(seen[key].get("times") or 1) + 1
                    continue
                seen[key] = {"topic": tid, "verdict": v, "reason": reason, "times": 1}
        out = list(seen.values())
    except Exception:
        return []
    return out[-limit:]


def random_unit() -> dict[str, Any]:
    """最小单位 = (表, 列)，随机挑一个，带表名 + 列含义与单位。

    设计⑩：初始注入要「随机给其中一个最小单位」，像 MC 出生环境不写死、可迁移到
    新世界。先把这个最小单位（一张表的一个列）的取数做通，再向对应里程碑
    （树干最粗、经过路径最多的节点）推进。列含义/单位从 knowledge_graph 的
    table 节点 column_desc 取，取不到就只给列名。
    """
    try:
        tabs = available_tables(TIER_BASE).get("base") or list(KNOWN_BASE_TABLES)
        if not tabs:
            return {}
        tbl = random.choice(tabs)
        cols: dict[str, str] = {}
        try:
            import knowledge_graph
            g = knowledge_graph.KnowledgeGraph.load()
            node = g.nodes.get(f"table:{tbl}") if g is not None else None
            if node:
                cols = dict((node.detail or {}).get("column_desc") or {})
        except Exception:
            cols = {}
        col = random.choice(list(cols.keys())) if cols else ""
        raw = cols.get(col, "") if cols else ""
        # knowledge_graph 的 column_desc 值可能是 dict（含 desc/type/flag），归一化。
        if isinstance(raw, dict):
            raw = raw.get("desc") or raw.get("type") or ""
        return {"table": tbl, "column": col, "desc": str(raw)}
    except Exception:
        return {}


def scene_injection(topic_id: str = "", *, progress_tier: int | None = None) -> str:
    """生成给模型 prompt 的「场景注入」文本块（渐进披露，按 progress_tier 收放）。

    progress_tier 不传则按**已解锁里程碑数**自动判（≥ TIER_EXT_UNLOCKS → ext 档），
    这样"基础层没拿下就别碰 ext"是结构性的，不靠说教。

    ⚠️ 以前这里读的是「有任何知识点掌握度 ≥0.8」—— 那是把 VLML 的**评分结果**
       当成披露条件，与设计相悖（用户原话：「评分结果不被任何推进条件读到」）。
       改成里程碑计数：里程碑是布尔的（这一题跑通了 / 没跑通），不含任何评分。
    """
    if progress_tier is None:
        try:
            progress_tier = TIER_EXT if len(unlocked_milestones()) >= TIER_EXT_UNLOCKS \
                else TIER_BASE
        except Exception:
            progress_tier = TIER_BASE

    ds = data_source_identity()
    tabs = available_tables(progress_tier)
    inv = entity_fillables()
    # ⚠️ 刻意**不读掌握度**：chests 该装的是"已经学会的取数配方"（skill_store 里的
    #    SQL），不是"评分 ≥0.8 的知识点"。用掌握度当门槛，等于把 VLML 的评分结果
    #   偷偷读进上下文 —— 而设计规定评分只进面板、不被任何推进/披露条件读到
    #   （用户原话：「评分结果不被任何推进条件读到」）。
    skills = {"skill_count": _skill_count()}
    src_note = "（静态已知 schema）" if tabs.get("source") != "live" else "（实查）"

    lines: list[str] = []
    lines.append("【初始场景（数据世界）】")
    lines.append(f"· 数据源（biome）：{ds}")
    lines.append(
        f"· 可用工具（语义层，恒可用，不要写裸 SQL 之外的取数方式）："
        + "、".join(SEMANTIC_TOOLS))
    base_block = "、".join(tabs["base"]) if tabs["base"] else "（无可读 base 表）"
    lines.append(f"· 可见表（nearby_blocks · base 层{src_note}）：{base_block}")
    if progress_tier >= TIER_EXT:
        ext_block = "、".join(tabs["ext"]) if tabs["ext"] else "（本库无 ext 层）"
        lines.append(f"· 可见表（nearby_blocks · ext 层，已解锁）：{ext_block}")
    else:
        # 新手档：明确告诉模型 ext 层被收起，别碰
        lines.append("· 可见表（ext 层）：**本档未解锁**——base 层没掌握前不要写 ext_* 查询")
    if inv["anchor"]:
        lines.append("· 当前实体（inventory · 锚点，出题/作答围绕这些，不要编造）："
                     + "；".join(inv["anchor"]))
    # ⚠️ **不给** `inv["options"]`（「series/team/map 在库里各有哪些取值」）。
    #    用户原话：「当前实体在库里实际存在哪些取值…不允许进反馈，因为会影响
    #    voyager 完成任务的重心」。理由很实在：那是一份**答案候选清单**，
    #    等于把选择题的选项一并递过去 —— 它会去"枚举取值凑答案"，而不是
    #    "把这一题的取数做通"。要看有哪些取值，去面板（panel_only()）。
    if skills.get("skill_count"):
        lines.append(
            f"· 已学会技能（chests）：技能库共 {skills['skill_count']} 条配方，"
            f"下面列出可直接复用的 SQL 思路")

    # —— 同构会话新增（设计⑨⑩）：进运行时上下文的「学习状态」字段 ——
    # 这些和上面 biome/nearby_blocks/inventory/chests 正交：上面是「世界长啥样」，
    # 下面是「我学到哪了 / 栽在哪了」。VLML 判决与覆盖率数值**不在这里**（约定⑧，
    # 它们只进面板 run_log，不进上下文）。
    # MC 节点里程碑树（设计：把 VLML 的表理成树状图，树干最粗 = 里程碑）：
    mt = milestone_tree()
    if mt.get("trunk"):
        lines.append(
            f"· MC 里程碑树（树干=最长前置路径，节点最多路径上的节点即里程碑）："
            f"进度 {mt['progress']}")
        lines.append(f"    种子（须先拿下，对应 MC 的 Mine 1 wood log）：{mt['seed']}")
        lines.append(
            f"    树干链：{' → '.join(mt['trunk'])}")
        if mt.get("keystone"):
            lines.append(
                f"    树干最粗处（keystone，经过路径最多）：{mt['keystone']}"
                f"（betweenness {mt.get('keystone_score')}）")
        if mt.get("completed_list"):
            lines.append(f"    已解锁里程碑：{', '.join(mt['completed_list'])}")
    ms_topics = unlocked_milestones()
    if ms_topics:
        lines.append(f"· 已解锁里程碑（图谱节点·已完成）：{', '.join(ms_topics)}")
    learned = learned_skills()
    if learned:
        lines.append("· 已学会技能（chests·可复用取数配方，照抄其 SQL 思路）：")
        for s in learned:
            code = (s["code"] or "").strip().replace("\n", " ")
            snippet = (code[:160] + "…") if len(code) > 160 else code
            lines.append(f"    - [{s['topic']}] {s['name']}：{snippet}")
    fails = failure_history()
    if fails:
        lines.append("· 历史失败（带原因，避免重蹈）：")
        for f in fails:
            times = int(f.get("times") or 1)
            lines.append(
                f"    - {f['topic']}（{f['verdict']}）：{f['reason']}"
                + (f" ×{times}" if times > 1 else ""))
    skips = skip_history()
    if skips:
        lines.append("· 历史跳过（带原因，避免重复绕路）：")
        for s in skips:
            lines.append(f"    - {s['topic']}：{s['reason']}")
    unit = random_unit()
    if unit.get("table"):
        lines.append(
            f"· 本次随机出生最小单位（表,列）：{unit['table']}"
            f".{unit['column'] or '?'}（{unit['desc'] or '列含义/单位未登记'}）")
        lines.append("    —— 先把这个最小单位的取数做通，再向对应里程碑推进。")

    if topic_id:
        lines.append(f"· 本次聚焦知识点：{topic_id}")
    return "\n".join(lines)


def _main() -> int:
    print("=== MVE 初始场景注入（两档渐进披露）===\n")
    for tier in (TIER_BASE, TIER_EXT):
        label = "新手档（base 层）" if tier == TIER_BASE else "解锁档（含 ext 层）"
        print(f"--- 进度档 {tier}：{label} ---")
        print(scene_injection(progress_tier=tier))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
