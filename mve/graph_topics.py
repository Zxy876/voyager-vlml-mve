#!/usr/bin/env python3
"""图谱的「种子层」——**独立于题目**的指标全集 + 文本匹配 + 语义桶压缩。

照猫娘伴学 `knowledge_graph_guidance.py` / `knowledge_graph_index.py` 移植。
为什么要移植：伴学的图谱是**先于题目存在的知识结构**（82 个知识点种子），
题目只是用 `match_topics(query=题干)` 去匹配焦点，所以任何题都能用；
而 MVE 之前的图谱是**题目 rubric 的投影**——维度节点从 `TASKS[].rubric`
派生，只有出过的那 5 道题有维度（实测 13 个）。新题进来匹配不到任何东西，
图谱就退化成"这 5 道题的备忘"。

伴学的三段式，这里是同构实现：
    topics（全量种子）  →  match_topics(query)  →  SubgraphBudget 裁剪
    →  _build_focused_model_context（压成语义桶）
    fact（全量指标）    →  match_facts(query)   →  GraphBudget+relation_limits
    →  focused_context（压成语义桶）

几个必须照搬的约束（都是从伴学源码里读出来的，不是我编的）：
1. `raw_seed_included: False`（knowledge_graph_guidance.py:999）——
   绝不把 18 字段原始种子整包喂给模型，只给 label 列表。
   MVE 的同构物：绝不把 answer_spec 的 SQL 原文喂进去（已经是脱敏骨架）。
2. 只认**一跳直接边**，且认方向（:1162-1169）；丢弃要计数
   （diagnostics: self_relation / nonincident / direction_mismatch）。
3. `relation_limits` 逐关系限流（knowledge_graph_index.py:121-134）——
   防止稠密的某一种关系把其它关系饿死。
4. `response_mode` 分流（:1302-1310）——不同环节取不同的关系子集。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# 边关系 → 语义桶（照伴学 _build_focused_model_context 的 9 个桶）
# --------------------------------------------------------------------------
PRODUCED_BY = "produced_by"      # 维度/事实 ← 工具
DERIVED_FROM = "derived_from"    # 维度/表 ← 表
READS = "reads"                  # 洞察 ← 表
COMPOSES = "composes"            # 工具 ← 洞察
FROM_SECTION = "from_section"    # 维度 ← 工具的某一段
CONFUSABLE = "confusable"
CO_OCCURS = "co_occurs"

BUCKET_OF = {
    PRODUCED_BY: "produced_by",
    DERIVED_FROM: "derived_from",
    READS: "reads",
    COMPOSES: "composes",
    FROM_SECTION: "section",
    CONFUSABLE: "confusions",
    CO_OCCURS: "review_with",
}

# 边的**规范方向**：src→dst，焦点节点在该关系的哪一端。
# 照伴学 `_FOCUSED_RELATION_DIRECTION` 的思路——"prerequisite 是 incoming"，
# 这里"事实被工具产出"也是 incoming（边从 tool 指向 fact）。
FOCUSED_RELATION_DIRECTION = {
    PRODUCED_BY: "incoming",
    DERIVED_FROM: "incoming",
    READS: "incoming",
    COMPOSES: "incoming",
    FROM_SECTION: "incoming",
}
SYMMETRIC_RELATIONS = frozenset({CONFUSABLE, CO_OCCURS})

# 逐关系限流（照伴学 SubgraphBudget.relation_limits 的 10 种形态）
RELATION_LIMITS: dict[str, int] = {
    PRODUCED_BY: 4,
    FROM_SECTION: 4,
    COMPOSES: 6,
    READS: 6,
    DERIVED_FROM: 5,
    CONFUSABLE: 3,
    CO_OCCURS: 3,
}

# 关系优先级（照伴学 RELATION_PRIORITY：越小越先说）
RELATION_PRIORITY = {
    PRODUCED_BY: 0,
    FROM_SECTION: 1,
    COMPOSES: 2,
    READS: 3,
    DERIVED_FROM: 4,
    CONFUSABLE: 5,
    CO_OCCURS: 6,
}

PRIORITY_SCORE = {"core": 0, "useful": 1, "optional": 2}

# --------------------------------------------------------------------------
# response_mode 分流（照伴学 :1302-1310）
# --------------------------------------------------------------------------
# 伴学按"用户在干什么"分流：problem_solving / general_explanation /
# general_discussion / unknown，各取不同关系子集。
# MVE 的同构物是**当前环节**：
#   plan    —— 让模型写取数计划：口径 + 工具 + 段 + 易混（全量）
#   explain —— 讲解/求助（bypass_learn）：只要结构与上游，不要易混（会诱导）
#   judge   —— 判分：只要取值路径与口径
#   minimal —— 兜底
MODE_ALLOWED: dict[str, set[str]] = {
    "plan": {PRODUCED_BY, FROM_SECTION, COMPOSES, READS, DERIVED_FROM,
             CONFUSABLE, CO_OCCURS},
    "explain": {PRODUCED_BY, FROM_SECTION, COMPOSES, READS, DERIVED_FROM},
    "judge": {PRODUCED_BY, FROM_SECTION, DERIVED_FROM},
    "minimal": {PRODUCED_BY},
}
DEFAULT_MODE = "plan"

# --------------------------------------------------------------------------
# 通用词（照伴学 GENERIC_QUERY_TERMS）
# --------------------------------------------------------------------------
# 题干里"帮我算一下""是多少""分析"这类词到处都是，不去掉会让所有 fact 都得分。
GENERIC_TERMS = {
    # 英文
    "what", "how", "many", "much", "the", "this", "that", "team", "match",
    "series", "game", "games", "round", "rounds", "map", "maps", "player",
    "players", "report", "analysis", "analyze", "analyse", "compute",
    "calculate", "get", "find", "show", "give", "tell", "please", "help",
    "question", "problem", "value", "number", "count", "total", "rate",
    "percent", "percentage", "average", "avg", "mean",
    # 中文
    "多少", "几个", "什么", "怎么", "如何", "为什么", "哪些", "这个", "那个",
    "比赛", "数据", "分析", "计算", "查询", "统计", "一下", "请问", "帮我",
    "问题", "情况", "表现", "结果", "数值", "比例", "胜率",
}

# 中文术语 → 英文指标词。
# 来源标注 seed：伴学的 aliases 也是种子里手写的（知识点的别名本来就是人给的），
# 这里同理。它只做**别名**，不碰任何取值，所以不构成"我当裁判"。
# 注意：只放"术语→指标"的映射，绝不放数值。
TERM_SEED: dict[str, list[str]] = {
    "手枪局": ["pistol", "team_economy_pistol"],
    "手枪": ["pistol", "team_economy_pistol"],
    "eco局": ["eco", "team_economy_eco"],
    "经济": ["economy", "team_economy_pistol", "team_economy_eco"],
    "首血": ["fb", "first_blood", "opening_duels", "agg_first_blood_stats"],
    "首杀": ["fb", "first_blood", "opening_duels"],
    "首死": ["fd", "opening_duels"],
    "转换率": ["conv", "conversion", "fb_conv"],
    "转化": ["conv", "conversion"],
    "连败": ["streak", "max_losing_streak"],
    "连胜": ["streak"],
    "回合": ["rounds", "round"],
    "残局": ["clutch", "clutches", "team_impact_metrics"],
    "多杀": ["multikills", "team_impact_metrics"],
    "kast": ["kast", "team_consistency_metrics"],
    "adr": ["adr", "team_consistency_metrics"],
    "评分": ["rating", "impact"],
    "下半场": ["half_breakdown"],
    "加时": ["half_breakdown"],
    "经济曲线": ["economy_context"],
    "进攻": ["attack_patterns"],
    "战术": ["attack_patterns"],
    "时间线": ["round_timeline"],
    "地图": ["map", "map_zones", "agg_team_map_stats"],
    "选手": ["player", "player_performance", "agg_player_round_stats"],
    "队伍对比": ["team_comparison"],
    "关键指标": ["key_metrics"],
    "亮点": ["highlight_rounds"],
    "回合情境": ["round_situations"],
}


def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _snake_parts(name: str) -> list[str]:
    """`team_economy_pistol` → ['team', 'economy', 'pistol']。"""
    return [p for p in re.split(r"[^a-z0-9]+", _text(name).lower()) if p]


def _tokens(query: str, *, strip_generic: bool = True) -> list[str]:
    """题干分词：英文按 [a-z0-9]+，中文按 2-4 字 N-gram，并做术语映射。"""
    raw = _text(query).lower()
    if not raw:
        return []
    out: set[str] = set()
    for tok in re.findall(r"[a-z0-9_]+", raw):
        for part in _snake_parts(tok):
            if len(part) >= 2:
                out.add(part)
    # 中文：先替换术语，再对剩余片段取 2-4 gram
    cjk = "".join(ch if "\u4e00" <= ch <= "\u9fff" else " " for ch in raw)
    for term, mapped in TERM_SEED.items():
        if term in cjk:
            out.update(mapped)
            cjk = cjk.replace(term, " ")
    for frag in cjk.split():
        if len(frag) < 2:
            continue
        out.add(frag)
        for size in (2, 3, 4):
            for i in range(0, len(frag) - size + 1):
                out.add(frag[i:i + size])
    if strip_generic:
        out = {t for t in out if t not in GENERIC_TERMS}
    return sorted(out, key=lambda x: (-len(x), x))


def _aliases_of(fact: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("id", "label", "unit", "chapter", "section"):
        v = _text(fact.get(key)).lower()
        if v:
            out.append(v)
            out.extend(_snake_parts(v))
    out.extend(_text(a).lower() for a in (fact.get("aliases") or []))
    out.extend(_text(a).lower() for a in (fact.get("insights") or []))
    for t in (fact.get("tables") or []):
        out.extend(_snake_parts(_text(t)))
    seen, uniq = set(), []
    for a in out:
        if a and a not in seen:
            seen.add(a)
            uniq.append(a)
    return uniq


def _search_text(fact: dict[str, Any]) -> str:
    parts = [_text(fact.get(k)) for k in
             ("id", "label", "unit", "chapter", "section", "tool")]
    parts += [_text(a) for a in (fact.get("aliases") or [])]
    parts += [_text(a) for a in (fact.get("insights") or [])]
    parts += [_text(a) for a in (fact.get("tables") or [])]
    return " ".join(p for p in parts if p).lower()


# --------------------------------------------------------------------------
# 焦点匹配（照伴学 match_topics）
# --------------------------------------------------------------------------
def match_facts(facts: list[dict[str, Any]], *, topic_id: str = "",
                query: str = "", limit: int = 5) -> list[dict[str, Any]]:
    """题干 → 焦点指标。

    照伴学 `match_topics`（knowledge_graph_guidance.py:481）的评分形态：
      - topic_id 精确命中 → score 100（:491-500）
      - 完整 label/alias 被题干覆盖 → +100（:534-544）
      - label 出现在 terms → +40；alias → +36；前缀 → +18；包含 → +10；
        haystack → +3
    存在理由：题目**不需要预先声明**它考哪些指标。伴学靠这个给任意新题配图，
    MVE 之前没有它，所以图谱只对出过的题有效。
    """
    limit = max(1, int(limit or 5))
    by_id = {_text(f.get("id")): f for f in facts if _text(f.get("id"))}
    key = _text(topic_id)
    if key and key in by_id:
        return [{"id": key, "label": _text(by_id[key].get("label")) or key,
                 "score": 100, "match": "topic_id"}]

    text = query or topic_id
    terms = _tokens(text)
    if not terms:
        return []
    # 题干里出现**完整表名**（"agg_team_game_stats"）是最强的一种证据：
    # 实测问"这张表的粒度"时，引用该表的洞察会凭零散 token 得分压过表本身。
    compact_q = re.sub(r"[^a-z0-9]+", "", text.lower())
    scored: list[dict[str, Any]] = []
    for fact in facts:
        fid = _text(fact.get("id"))
        if not fid:
            continue
        label = _text(fact.get("label")).lower()
        aliases = _aliases_of(fact)
        haystack = _search_text(fact)
        score = 0
        matched: list[str] = []
        for tname in (fact.get("tables") or []):
            ct = re.sub(r"[^a-z0-9]+", "", _text(tname).lower())
            if ct and len(ct) >= 6 and ct in compact_q:
                score += 150
                matched.append(_text(tname))
        # 完整标签被覆盖：最强的证据
        label_parts = _snake_parts(label) or ([label] if label else [])
        if label_parts and set(label_parts).issubset(set(terms)):
            score += 100
            matched.append(label)
        if label and label in terms:
            score += 40
            matched.append(label)
        for alias in aliases:
            if alias in terms:
                score += 36
                matched.append(alias)
        for term in terms:
            if term == fid.lower():
                score += 20
            elif term in aliases:
                score += 18
            elif any(term in a for a in aliases):
                score += 8 if len(term) >= 3 else 5
            elif label.startswith(term):
                score += 18
            elif term in label:
                score += 10
            elif term in haystack:
                score += 3
            else:
                continue
            matched.append(term)
        if score:
            scored.append({"id": fid, "label": _text(fact.get("label")) or fid,
                           "score": score, "match": "query",
                           "matched_terms": list(dict.fromkeys(matched))[:6]})
    return sorted(scored,
                  key=lambda i: (-int(i["score"]), len(_text(i["label"])),
                                 _text(i["label"])))[:limit]


# --------------------------------------------------------------------------
# 边排序（照伴学 _edge_sort_key）
# --------------------------------------------------------------------------
def edge_sort_key(query: str, edge: Any) -> tuple:
    relation = _text(getattr(edge, "relation", ""))
    priority = PRIORITY_SCORE.get("core", 0)
    overlap = 0
    if query:
        hay = " ".join(_text(x) for x in
                       (getattr(edge, "src", ""), getattr(edge, "dst", ""),
                        getattr(edge, "reason", ""))).lower()
        overlap = sum(1 for t in _tokens(query) if t and t in hay)
    return (RELATION_PRIORITY.get(relation, 99), priority,
            -float(getattr(edge, "confidence", 1.0) or 0.0), -overlap,
            _text(getattr(edge, "src", "")), _text(getattr(edge, "dst", "")))


# --------------------------------------------------------------------------
# 语义桶压缩（照伴学 _build_focused_model_context）
# --------------------------------------------------------------------------
@dataclass
class FocusBudget:
    """照伴学 SubgraphBudget；这里只留 MVE 真的用得上的字段。"""
    max_focus: int = 3
    max_nodes: int = 20
    max_edges: int = 30
    relation_limits: dict[str, int] = field(
        default_factory=lambda: dict(RELATION_LIMITS))


def focused_context(graph: Any, selected_id: str, *, query: str = "",
                    mode: str = DEFAULT_MODE,
                    budget: FocusBudget | None = None) -> dict[str, Any]:
    """只取焦点的**一跳直接边**，认方向，按关系归桶，丢弃要计数。

    伴学的原话（:1073-1079）：「The graph UI may include a multi-hop retrieved
    subgraph. Reusing its relation groups here previously made edges between
    two neighbouring topics look like direct relations of selected_id.」
    即：多跳子图里两个邻居之间的边，会被误当成焦点的直接关系。
    MVE 同构风险一模一样（agg 表的上游上游不是事实的直接来源），所以这里
    严格只读 incident 边。
    """
    b = budget or FocusBudget()
    allowed = MODE_ALLOWED.get(_text(mode).lower(), MODE_ALLOWED[DEFAULT_MODE])
    diag = {"self_relation_dropped": 0, "nonincident_edge_dropped": 0,
            "direction_mismatch_dropped": 0, "mode_filtered": 0}

    ctx: dict[str, Any] = {
        "mode": _text(mode).lower() or DEFAULT_MODE,
        "query": _text(query),
        "focus": {"id": selected_id,
                  "label": _text(getattr(graph.nodes.get(selected_id), "label", ""))
                  if selected_id in getattr(graph, "nodes", {}) else ""},
    }
    for bucket in set(BUCKET_OF.values()):
        ctx[bucket] = []
    ctx["summary"] = {"raw_seed_included": False, "mode": ctx["mode"],
                      "diagnostics": diag}

    if selected_id not in getattr(graph, "nodes", {}):
        return ctx

    indexed = ([*graph._in.get(selected_id, []),
                *graph._out.get(selected_id, [])])
    buckets: dict[str, list[tuple[str, str]]] = {
        k: [] for k in set(BUCKET_OF.values())}
    seen: set[tuple[str, str, str]] = set()
    for edge in indexed:
        src, dst = _text(edge.src), _text(edge.dst)
        rel = _text(edge.relation)
        if (src, dst, rel) in seen:
            continue
        seen.add((src, dst, rel))
        if rel not in BUCKET_OF:
            continue
        if rel not in allowed:
            diag["mode_filtered"] += 1
            continue
        if src == selected_id and dst == selected_id:
            diag["self_relation_dropped"] += 1
            continue
        if selected_id not in {src, dst}:
            diag["nonincident_edge_dropped"] += 1
            continue
        other = dst if src == selected_id else src
        if not other or other == selected_id:
            diag["self_relation_dropped"] += 1
            continue
        if other not in graph.nodes:
            diag["nonincident_edge_dropped"] += 1
            continue
        incoming = (dst == selected_id)
        expect = FOCUSED_RELATION_DIRECTION.get(rel)
        if rel not in SYMMETRIC_RELATIONS:
            if expect == "incoming" and not incoming:
                diag["direction_mismatch_dropped"] += 1
                continue
            if expect == "outgoing" and incoming:
                diag["direction_mismatch_dropped"] += 1
                continue
        buckets[BUCKET_OF[rel]].append(
            (other, _text(graph.nodes[other].label) or other))

    for key, items in buckets.items():
        ordered = sorted(items, key=lambda i: (i[1].casefold(), i[0]))
        vals, seen_labels = [], set()
        for _oid, lab in ordered:
            if not lab or lab in seen_labels:
                continue
            seen_labels.add(lab)
            vals.append(lab)
            if len(vals) >= b.relation_limits.get(
                    _rel_of_bucket(key), b.max_nodes):
                break
        ctx[key] = vals
    ctx["summary"]["diagnostics"] = diag
    return ctx


def _rel_of_bucket(bucket: str) -> str:
    for rel, bk in BUCKET_OF.items():
        if bk == bucket:
            return rel
    return bucket


# --------------------------------------------------------------------------
# 种子层：把 VLML 结构变成「指标全集」（不依赖题目）
# --------------------------------------------------------------------------
def build_seed_facts(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """从 VLML 发现的四层结构里，抽出所有**可以问出来的事实**。

    与题目的关系是零：不管有没有出过题，这些事实都在。伴学的 82 个知识点
    种子也是这个性质（先于题目存在），题目只是引用它们。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(fact: dict[str, Any]) -> None:
        fid = _text(fact.get("id"))
        if not fid or fid in seen:
            return
        seen.add(fid)
        out.append(fact)

    section_insights = schema.get("section_insights") or {}

    # 1) 洞察级事实：46 个洞察各有 purpose（"Pistol round performance"）
    for name, info in sorted((schema.get("insights") or {}).items()):
        purpose = _text(info.get("purpose"))
        if not purpose:
            continue
        aliases = list(_snake_parts(name))
        aliases += [p.lower() for p in _snake_parts(purpose)]
        cjk = "".join(ch if "\u4e00" <= ch <= "\u9fff" else " "
                      for ch in purpose)
        for term, mapped in TERM_SEED.items():
            if any(m in aliases for m in mapped):
                aliases.append(term)
        add({
            "id": f"fact:insight:{name}",
            "label": purpose,
            "subject": "valorant",
            "stage": "insight",
            "unit": (info.get("reports") or [""])[0],
            "chapter": "",
            "insights": [name],
            "tables": list(info.get("tables") or []),
            "aliases": sorted(set(aliases)),
            "origin": "vlml_source",
        })

    # 2) 段级事实：工具 × section（答案真正落地的位置）
    for tool, sections in sorted((schema.get("tools") or {}).items()):
        for sec in (sections.get("sections") or []):
            ins = (section_insights.get(tool) or {}).get(sec) or []
            tbls: list[str] = []
            for i in ins:
                node = (schema.get("insights") or {}).get(i) or {}
                for t in node.get("tables") or []:
                    if t not in tbls:
                        tbls.append(t)
            aliases = list(_snake_parts(sec)) + list(_snake_parts(tool))
            for term, mapped in TERM_SEED.items():
                if any(m in aliases for m in mapped):
                    aliases.append(term)
            add({
                "id": f"fact:section:{tool}.{sec}",
                "label": f"{sec}（{tool}）",
                "subject": "valorant",
                "stage": "tool",
                "unit": tool,
                "chapter": sec,
                "tool": tool,
                "section": sec,
                "insights": list(ins),
                "tables": tbls,
                "aliases": sorted(set(aliases)),
                "origin": "vlml_source",
            })

    # 3) 表级事实：一张表本身也是可以问的东西（"这张表有多少行/什么粒度"）
    for name, info in sorted((schema.get("tables") or {}).items()):
        aliases = list(_snake_parts(name))
        for term, mapped in TERM_SEED.items():
            if any(m in aliases for m in mapped):
                aliases.append(term)
        add({
            "id": f"fact:table:{name}",
            "label": f"{name}（{info.get('grain') or '表'}）",
            "subject": "valorant",
            "stage": _text(info.get("stage")),
            "unit": "",
            "chapter": "",
            "tables": [name],
            "aliases": sorted(set(aliases)),
            "origin": "information_schema",
        })
    return out


def seed_facts_to_nodes(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把种子事实转成图谱节点（kind='fact'）。"""
    nodes = []
    for f in facts:
        nodes.append({
            "id": _text(f.get("id")),
            "kind": "fact",
            "label": _text(f.get("label")) or _text(f.get("id")),
            "detail": dict(f),
        })
    return nodes
