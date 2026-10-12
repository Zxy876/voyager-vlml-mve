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

# 进度档位：0 = 新手（只看 base 表），1 = 已掌握基础层（放开 ext 表）
TIER_BASE = 0
TIER_EXT = 1

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
    name = Path(p).stem if p else "vlml_events(默认)"
    if "vlr" in name:
        return f"{name}（vlr.gg 源：回合级 + 聚合级，无 ext 层）"
    if "rib" in name:
        return f"{name}（rib.gg 源：含首血/经济 ext 层）"
    return f"{name}（默认 GRID 切片）"


def _db_path() -> str:
    try:
        obj = json.loads(DB_CONFIG.read_text(encoding="utf-8"))
        p = str((obj or {}).get("db_path") or "")
    except Exception:
        p = ""
    if not p:
        return str(HERE.parent / "vlml" / "data" / "vlml_events.duckdb")
    return p


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


def mastered_skills() -> dict[str, Any]:
    """chests 等价物：技能库图谱——已掌握的知识点（=已学会的"配方"）。"""
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


def scene_injection(topic_id: str = "", *, progress_tier: int | None = None) -> str:
    """生成给模型 prompt 的「场景注入」文本块（渐进披露，按 progress_tier 收放）。

    progress_tier 不传则按掌握度自动判：有任何知识点掌握≥0.8 → 进 ext 档，
    否则 base 档。这样"基础层没掌握就别碰 ext"是结构性的，不靠说教。
    """
    if progress_tier is None:
        try:
            from topic_graph import MasteryStore
            ms = MasteryStore()
            any_mastered = any(
                (r.get("mastery") is not None and float(r["mastery"]) >= 0.8)
                for r in ms.all().values())
            progress_tier = TIER_EXT if any_mastered else TIER_BASE
        except Exception:
            progress_tier = TIER_BASE

    ds = data_source_identity()
    tabs = available_tables(progress_tier)
    inv = entity_fillables()
    skills = mastered_skills()
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
    opts = inv.get("options") or {}
    if opts:
        parts = []
        for k, v in opts.items():
            parts.append(f"{k}∈{{{', '.join(v)}}}") if v else None
        if parts:
            lines.append("· 实体可填值（inventory · 目录预览）：" + "；".join(parts))
    if skills["mastered_topics"]:
        lines.append(
            f"· 已掌握配方（chests · 已学会）：{', '.join(skills['mastered_topics'])}"
            f"（技能库共 {skills['skill_count']} 条，可复用其取数思路）")
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
