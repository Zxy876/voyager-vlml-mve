#!/usr/bin/env python3
"""MVE 的知识图谱：维度 / 工具 / 表 三层，以及它们之间的边。

MVE 的知识图谱是什么
--------------------
用户问：「MVE 的知识图谱是什么？解决什么问题？内容依据其实就是 VLML 的
数据建模表。」

对照猫娘伴学（`knowledge_graph_index.py` / `knowledge_graph_edges.py`）：

  伴学的图谱 = 学科知识点之间的关系网
    节点 = 知识点，节点上带 `skills` / `question_types` /
           **`typical_misconceptions`**（`knowledge_graph_index.py:99-112`）
    边   = 10 种语义关系（`knowledge_graph_edges.py:22-35`）：
           prerequisite / procedure_step / confusable / analogy / application /
           extends / co_occurs / supports / next / nearby
    用途 = `build_relevant_subgraph()`（`knowledge_graph_index.py:220`）拿到
           「这个知识点周围是什么」，供判题、讲解、出题使用

  MVE 的图谱 = 数据维度与取数手段之间的关系网
    节点 = 维度（dimension）/ 工具（tool）/ 表（table）
    边   = produced_by（维度由哪个工具出）
           derived_from（维度由哪张表算出）   ← VLML 数据建模表在这里进入
           confusable（易混维度）
           co_occurs（同一题一起出现）
           procedure_step（编排顺序）
    用途 = **回答「这个维度该调哪个工具」**

解决什么问题（这是建它的唯一理由）
----------------------------------
上一轮做的 `feedback.py` 已经能说清"错在哪一类"：

    eco_win_rate 不在观测里（我交了 null），不是算错 —— 要先补调一个能出
    这个维度的工具。其中我没调过的是 pattern_detection_report。

但那句"其中我没调过的是 X"是**事后反推**出来的 —— 它拿裁判的成功轨迹
（`ref_plan`）和我的轨迹做差集。这有两个毛病：

1. 裁判没跑过（或缓存里没有 `ref_plan`）时，这句话就没了 —— 离线重算
   历史日志时 `裁判编排：（无）` 就是这个情况；
2. 它说的是"裁判调了而我没有"，不是"这个维度**本来就该**由它出"。
   万一裁判多调了一个无关工具，这个差集就会指错方向。

图谱把这件事变成**事前可查**：
    eco_win_rate --produced_by--> pattern_detection_report

来源必须是**声明或实测**，不能是模型编的：
- `produced_by`：读 `RubricPoint.answer_spec.tool`（`loop_core.py:185`），
  这是服务端配好的取数配方，不是猜的；
- `derived_from`：解析 `answer_spec.sql` 里的表名，并与 VLML 的
  `DATA_DICTIONARY.json` 对账（认不出的表不写）；
- `confusable`：同一题里"同后缀不同前缀"的维度（pistol_win_rate /
  eco_win_rate —— 实测模型确实混过这两者）。

跑法
----
    python mve/knowledge_graph.py --build          # 重建并落盘
    python mve/knowledge_graph.py --who eco_win_rate
    python mve/knowledge_graph.py --topic pistol_eco_pattern
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

GRAPH_PATH = HERE / "knowledge_graph.json"
# VLML 的数据建模表：schema 17 张 + transformations 13 张（派生动 schema 的 agg_*）
DATA_DICT = (HERE.parent / "vlml" / "database" / "DATA_DICTIONARY.json")

# 落盘格式版本。**改了节点的声明字段就必须 +1** —— 否则已经存在的
# knowledge_graph.json 永远"看起来没过期"（指纹只覆盖 tasks.py），新声明就
# 一辈子进不了图。这是"持久化"最容易踩的坑：写了代码，图里还是旧的。
SCHEMA_VERSION = 2      # 2 = 表节点带 VLML 建模声明，维度节点带伴学式声明

# --------------------------------------------------------------------------
# 边的关系类型（比伴学的 10 种少：只保留 MVE 真的用得上的）
# --------------------------------------------------------------------------
PRODUCED_BY = "produced_by"      # 维度 ← 工具
DERIVED_FROM = "derived_from"    # 维度/表 ← 表
CONFUSABLE = "confusable"        # 易混维度（对称）
CO_OCCURS = "co_occurs"          # 同题共现（对称）
PROCEDURE_STEP = "procedure_step"  # 编排顺序
# 新增：VLML 的四层结构（工具 → 洞察 → SQL → 表）
READS = "reads"                  # 洞察 ← 表
COMPOSES = "composes"            # 工具 ← 洞察（这个 section 由它产出）
FROM_SECTION = "from_section"    # 维度 ← 工具的某个 section
# 伴学 10 种关系里的 `application`（`knowledge_graph_edges.py:31`）：
# 知识点 → 应用场景，`FOCUSED_RELATION_DIRECTION` 规定焦点在 **from** 端。
# MVE 用它记「这个洞察/工具在那张表上被实际应用过一次」—— 人导入的题就是
# 这个"应用场景"，而**题本身不进图**（不建节点）。
APPLICATION = "application"

SYMMETRIC = frozenset({CONFUSABLE, CO_OCCURS})

# 阶段名（数据流分层）照伴学的 stage；从 vlml_schema 拿，避免两处各写一份。
# 这个模块只 import 标准库，不连库，可以安全地在顶部导入。
try:
    # 这个模块只 import 标准库 + 读文件，不连库，可以安全地在顶部导入。
    import vlml_schema
    from vlml_schema import STAGE_LABEL, STAGE_ORDER  # noqa: F401
except Exception:                                    # pragma: no cover
    vlml_schema = None                               # type: ignore[assignment]
    STAGE_LABEL, STAGE_ORDER = {}, []

_RE_FROM = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


@dataclass
class GraphBudget:
    """子图进 prompt 的预算 —— 照伴学 `SubgraphBudget`。

    伴学：focus_topics=3, max_depth=2, max_nodes=20, max_edges=30，
          外加 relation_limits 逐关系限流。
    这里同构：焦点维度数 / 节点数 / 边数 / 每个节点最多展示多少列。
    目的都一样 —— 库可以涨，prompt 不能跟着涨。
    """
    max_focus: int = 8          # 焦点维度（本题的评分点，按权重取前 N）
    max_nodes: int = 20
    max_edges: int = 30
    max_cols: int = 14          # 每张表最多展示多少列
    max_confusable: int = 3     # 每个焦点维度最多带几个易混维度
    max_insights: int = 3       # 一个 section 最多展开几个洞察（多了淹没口径行）
    include_confusable: bool = True


# --------------------------------------------------------------------------
# 节点与边
# --------------------------------------------------------------------------
def _merge_detail(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """合并两个 detail：列表取并集、字典按 key 并集、标量保留已有的。"""
    out = dict(old)
    for k, v in (new or {}).items():
        if v is None or v == "" or v == []:
            continue
        if isinstance(v, list):
            cur = list(out.get(k) or [])
            for item in v:
                if item not in cur:
                    cur.append(item)
            out[k] = cur
        elif isinstance(v, dict):
            cur = dict(out.get(k) or {})
            for dk, dv in v.items():
                if isinstance(dv, list) and isinstance(cur.get(dk), list):
                    for item in dv:
                        if item not in cur[dk]:
                            cur[dk].append(item)
                elif dk not in cur:
                    cur[dk] = dv
            out[k] = cur
        elif k not in out or out.get(k) in (None, "", []):
            out[k] = v
    return out


@dataclass
class Node:
    id: str
    kind: str                      # dimension / tool / table
    label: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label,
                "detail": self.detail}


@dataclass
class Edge:
    src: str
    dst: str
    relation: str
    # 来源必须可追溯：声明式（rubric 配的）还是实测（跑出来的）
    origin: str = "declared"       # declared / observed
    reason: str = ""
    confidence: float = 1.0

    def key(self) -> tuple[str, str, str]:
        a, b = (self.src, self.dst)
        if self.relation in SYMMETRIC:
            a, b = sorted((a, b))
        return (a, b, self.relation)

    def to_dict(self) -> dict[str, Any]:
        return {"from": self.src, "to": self.dst, "relation": self.relation,
                "origin": self.origin, "reason": self.reason,
                "confidence": round(self.confidence, 3)}


class KnowledgeGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self._by_key: dict[tuple[str, str, str], Edge] = {}
        self._out: dict[str, list[Edge]] = {}
        self._in: dict[str, list[Edge]] = {}

    # ---- 构建 ----
    def add_node(self, node: Node) -> None:
        """合并式写入 —— 同一个节点会被多个评分点/多道题重复遇到。

        原来是直接覆盖，实测后果：`map_fb_conv` 同时出现在
        fb_conversion_analysis 和 corrode_collapse，`map_rounds` 在一道题里有
        三个评分点（Corrode/Haven/Lotus）—— 后者把前者整个盖掉，图谱上只剩
        最后一个评分点的信息。多题共用的维度恰恰是最该记全的。
        """
        old = self.nodes.get(node.id)
        if old is None:
            self.nodes[node.id] = node
            return
        if not old.label and node.label:
            old.label = node.label
        old.detail = _merge_detail(old.detail, node.detail)

    def add_edge(self, edge: Edge) -> None:
        # 两端必须都在图里 —— 悬挂边会让查询结果变成噪音
        if edge.src not in self.nodes or edge.dst not in self.nodes:
            return
        k = edge.key()
        if k in self._by_key:
            old = self._by_key[k]
            # 实测证据可以升级声明式边，但反过来不行
            if edge.origin == "observed" and old.origin == "declared":
                self.edges.remove(old)
                old = edge
                self._by_key[k] = old
                self.edges.append(old)
            return
        self._by_key[k] = edge
        self.edges.append(edge)
        self._out.setdefault(edge.src, []).append(edge)
        self._in.setdefault(edge.dst, []).append(edge)

    # ---- 查询 ----
    def who_produces(self, dimension: str) -> list[str]:
        """这个维度由哪些工具产出（这就是 feedback 缺的那条边）。"""
        out = []
        for e in self._in.get(f"dim:{dimension}", []):
            if e.relation == PRODUCED_BY and e.src.startswith("tool:"):
                out.append(e.src[len("tool:"):])
        return sorted(set(out))

    def tables_for(self, dimension: str) -> list[str]:
        """这个维度是从哪些表算出来的（VLML 建模表在这里起作用）。"""
        out = []
        for e in self._in.get(f"dim:{dimension}", []):
            if e.relation == DERIVED_FROM and e.src.startswith("table:"):
                out.append(e.src[len("table:"):])
        return sorted(set(out))

    def yields(self, tool: str) -> list[str]:
        """这个工具能出哪些维度。"""
        out = []
        for e in self._out.get(f"tool:{tool}", []):
            if e.relation == PRODUCED_BY and e.dst.startswith("dim:"):
                out.append(e.dst[len("dim:"):])
        return sorted(set(out))

    def confusable_with(self, dimension: str) -> list[str]:
        out = []
        for e in self._out.get(f"dim:{dimension}", []):
            if e.relation == CONFUSABLE and e.dst.startswith("dim:"):
                out.append(e.dst[len("dim:"):])
        for e in self._in.get(f"dim:{dimension}", []):
            if e.relation == CONFUSABLE and e.src.startswith("dim:"):
                out.append(e.src[len("dim:"):])
        return sorted(set(out))

    def dimensions(self) -> list[str]:
        return sorted(n.id[len("dim:"):] for n in self.nodes.values()
                      if n.kind == "dimension")

    # ---- 阶段：照伴学 KnowledgeGraphIndex.stage_to_ids ----
    # 伴学按 stage（primary / junior_high / senior_high）建索引 ——
    # "学什么之前必须先会什么"。MVE 的同构物是**数据流顺序**：
    # 算什么之前必须先有什么（raw → meta → agg → insight → tool → dimension）。
    def stage_index(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for n in self.nodes.values():
            st = str(n.detail.get("stage") or "").strip()
            if st:
                out.setdefault(st, []).append(n.id)
        for v in out.values():
            v.sort()
        return out

    def upstream_tables(self, table: str, depth: int = 2) -> list[str]:
        """这张表的上游（它的数据从哪来）—— 回答"22 张表之间的关系"。"""
        out: list[str] = []
        frontier = [f"table:{table}"]
        for _ in range(max(1, depth)):
            nxt: list[str] = []
            for nid in frontier:
                for e in self._in.get(nid, []):
                    if e.relation == DERIVED_FROM and e.src.startswith("table:"):
                        if e.src not in out and e.src != f"table:{table}":
                            out.append(e.src)
                            nxt.append(e.src)
            frontier = nxt
            if not frontier:
                break
        return sorted(out)

    def tools(self) -> list[str]:
        return sorted(n.id[len("tool:"):] for n in self.nodes.values()
                      if n.kind == "tool")

    def tables(self) -> list[str]:
        return sorted(n.id[len("table:"):] for n in self.nodes.values()
                      if n.kind == "table")

    def subgraph_for(self, dimension: str) -> dict[str, Any]:
        """照伴学 `build_relevant_subgraph`：给一个焦点，返回它周围的结构。"""
        focus = f"dim:{dimension}"
        if focus not in self.nodes:
            return {"focus": dimension, "nodes": [], "edges": [], "found": False}
        ids = {focus}
        for e in list(self._in.get(focus, [])) + list(self._out.get(focus, [])):
            ids.add(e.src)
            ids.add(e.dst)
        return {
            "focus": dimension,
            "found": True,
            "produced_by": self.who_produces(dimension),
            "derived_from": self.tables_for(dimension),
            "confusable": self.confusable_with(dimension),
            "nodes": [self.nodes[i].to_dict() for i in sorted(ids) if i in self.nodes],
            "edges": [e.to_dict() for e in self.edges
                      if e.src in ids and e.dst in ids],
        }

    # ---- 给模型的上下文：control_primitives 的等价物 ----
    # ------------------------------------------------------------------
    def layering_rule(self, dimension: str) -> dict[str, Any] | None:
        """从骨架里抽出「哪一层 WHERE 才允许出现某一列」——用于**硬校验**。

        为什么需要它：把骨架、致命错法、自检规则都写进 prompt，模型仍五次
        写出一字不差的错法（把 `losing_team_name` 提前到最内层 WHERE）。
        提示打不过强先验，那就换成**约束** —— 原版 Voyager 也是这么做的：
        `process_ai_message` 用 babel 对代码做 AST 断言（必须是 async 函数、
        参数必须叫 bot），不合法直接 retry，而不是"提醒模型注意"。
        MVE 的同构物就是这条 SQL 分层断言。

        判据来自图谱骨架（`recipe`），不硬编码 —— 换一道题照样生效。
        """
        node = self.nodes.get(f"dim:{dimension}")
        if node is None:
            return None
        recipe = str(node.detail.get("recipe") or "")
        if "PARTITION BY" not in recipe.upper():
            return None
        # 骨架里 `) t WHERE X='?'` 这种"外层 WHERE"的列
        outer = re.findall(r"\)\s*\w+\s+WHERE\s+([A-Za-z_][\w]*)\s*=", recipe)
        if not outer:
            return None
        rule = {"outer_only": sorted(set(outer)),
                "note": str(node.detail.get("recipe") or "")[:200]}
        # 分段计算必须**分组**：骨架里的 `GROUP BY (rn - grp)`。
        # 实测紧接着就踩了这条 —— 拦下 WHERE 位置之后，它改的时候把 GROUP BY
        # 一起删了，于是所有输的回合又合成一整段（还是 13）。
        mg = re.search(r"GROUP\s+BY\s*\(([^)]*)\)", recipe, re.IGNORECASE)
        if mg:
            ids = re.findall(r"[A-Za-z_]\w*", mg.group(1) or "")
            if ids:
                rule["group_by_ids"] = sorted(set(ids))
                rule["group_by_expr"] = mg.group(1).strip()
        return rule

    def dims_of(self, topic_id: str) -> list[str]:
        """这道题涉及哪些维度（按权重降序）。"""
        return sorted(
            (n.id[len("dim:"):] for n in self.nodes.values()
             if n.kind == "dimension"
             and topic_id in (n.detail.get("topics")
                              or ([n.detail["topic_id"]]
                                  if n.detail.get("topic_id") else []))),
            key=lambda d: -float((self.nodes[f"dim:{d}"].detail.get("weight")
                                  or 0)),
        )
    # 原版 Voyager 把 control_primitives 的**源码**拼进 system message
    # （voyager/agents/action.py:render_system_message）：模型不只看到"有哪些
    # 函数"，还看到每个函数**怎么实现的**。MVE 里同构的那份东西就是子图渲染：
    # 不只说"有 query_sql"，还说清"这个指标怎么算、读哪张表、用哪些列、
    # 别人通常错在哪"。
    #
    # 预算照伴学 `SubgraphBudget`（knowledge_graph_index.py 附近）：
    #   focus_topics=3, max_depth=2, max_nodes=20, max_edges=30
    # 全图塞进 prompt 会稀释注意力，也会把无关维度（易混的）一起喂进来。
    # ------------------------------------------------------------------
    def subgraph_for_topic(self, topic_id: str,
                           budget: "GraphBudget | None" = None) -> dict[str, Any]:
        """照伴学 build_relevant_subgraph：给一道题，返回它周围的结构。"""
        b = budget or GraphBudget()
        focus = sorted(
            (n for n in self.nodes.values()
             if n.kind == "dimension"
             and topic_id in (n.detail.get("topics")
                              or ([n.detail["topic_id"]]
                                  if n.detail.get("topic_id") else []))),
            key=lambda n: -float(n.detail.get("weight") or 0),
        )[:b.max_focus]

        if not focus:
            return {"topic": topic_id, "found": False, "nodes": [], "edges": []}

        ids: list[str] = []
        for n in focus:
            ids.append(n.id)
            # 产出它的工具 + 它来自的表（depth=1）
            ids += [f"tool:{t}" for t in self.who_produces(n.id[len("dim:"):])]
            ids += [f"table:{t}" for t in self.tables_for(n.id[len("dim:"):])]
            # 易混维度（depth=1，但只取直接相连的）
            if b.include_confusable:
                ids += [f"dim:{c}" for c in
                        self.confusable_with(n.id[len("dim:"):])][:b.max_confusable]

        # 去重保序 + 截断
        seen, ordered = set(), []
        for i in ids:
            if i in self.nodes and i not in seen:
                seen.add(i)
                ordered.append(i)
        ordered = ordered[:b.max_nodes]

        edges = [e.to_dict() for e in self.edges
                 if e.src in seen and e.dst in seen][:b.max_edges]
        return {
            "topic": topic_id,
            "found": True,
            "focus_dims": [n.id[len("dim:"):] for n in focus],
            "nodes": [self.nodes[i].to_dict() for i in ordered],
            "edges": edges,
        }

    # ---- 种子层：任意题干都能配上图谱（照伴学 match_topics） ----
    def facts(self) -> list[dict[str, Any]]:
        """图谱里的「指标全集」。与题目无关，只由 VLML 结构决定。"""
        try:
            import graph_topics
        except Exception:
            return []
        out = []
        for n in self.nodes.values():
            if n.kind != "fact":
                continue
            d = dict(n.detail)
            d["id"] = n.id
            d["label"] = n.label
            d["aliases"] = [a for a in (d.get("aliases") or [])
                            if isinstance(a, str)]
            out.append(d)
        return out

    def subgraph_for_query(self, query: str, *, mode: str = "plan",
                           limit: int = 3) -> dict[str, Any]:
        """题干 → 焦点事实 → 一跳子图（逐关系限流 + 认方向 + 丢弃计数）。

        这是「图谱为所有题目所用」的入口：题目**不需要**先被出过、也不需要在
        图谱里有对应维度，只要题干文本能匹配上种子事实就能出子图。伴学靠
        同一招给任意新题配图（`build_knowledge_guidance_payload`）。
        """
        try:
            import graph_topics
        except Exception:
            return {"query": query, "found": False, "reason": "graph_topics 不可用"}
        matches = graph_topics.match_facts(self.facts(), query=query, limit=limit)
        if not matches:
            return {"query": query, "found": False,
                    "reason": "题干没匹配到任何指标（seed 层无命中）"}
        contexts = []
        for m in matches:
            ctx = graph_topics.focused_context(self, m["id"], query=query,
                                               mode=mode)
            ctx["score"] = m.get("score", 0)
            ctx["matched_terms"] = m.get("matched_terms") or []
            contexts.append(ctx)
        return {
            "query": query,
            "found": True,
            "mode": mode,
            "matches": matches,
            "contexts": contexts,
            "seed_facts": len(self.facts()),
        }

    def render_for_query(self, query: str, *, mode: str = "plan",
                         limit: int = 3) -> str:
        """把「题干匹配出来的子图」渲染成给模型的文本。

        与 `render_for_prompt` 的分工：
        - `render_for_prompt(topic_id)` —— 出过的题，走 rubric 派生维度（信息最全）
        - `render_for_query(题干)`      —— **任意题**，走种子事实（保证有东西可给）
        两者可以并存：后者是兜底，也是"图谱真的为所有题目所用"的证明。
        """
        sub = self.subgraph_for_query(query, mode=mode, limit=limit)
        if not sub.get("found"):
            return ""
        bucket_title = {
            "produced_by": "用什么工具出",
            "section": "取返回里的哪一段",
            "composes": "由哪些计算单元拼成",
            "reads": "这些单元读哪些表",
            "derived_from": "数据来自哪些表",
            "confusions": "容易和什么混",
            "review_with": "常和什么一起出现",
        }
        lines = ["【知识图谱 · 按题干匹配】下面是从 VLML 结构推出的口径，"
                 "优先于你的猜测；'?' 是脱敏占位符，真实取值必须你自己查。"]
        for ctx in sub["contexts"]:
            if not ctx.get("focus", {}).get("label"):
                continue
            lines.append(f"· {ctx['focus']['label']}（匹配度 {ctx.get('score', 0)}）")
            for bucket, title in bucket_title.items():
                vals = ctx.get(bucket) or []
                if vals:
                    lines.append(f"   {title}：{'、'.join(vals[:6])}")
            d = (ctx.get("summary") or {}).get("diagnostics") or {}
            dropped = sum(int(v or 0) for v in d.values())
            if dropped:
                lines.append(f"   （已按 {ctx.get('mode')} 模式裁掉 {dropped} 条边："
                             + "、".join(f"{k}={v}" for k, v in d.items() if v)
                             + "）")
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    def render_for_prompt(self, topic_id: str,
                          budget: "GraphBudget | None" = None,
                          *, with_recipe: bool = True,
                          with_section: bool = True,
                          with_stage: bool = True,
                          with_insight: bool = True) -> str:
        """把子图渲染成给模型的文本 —— 这就是 MVE 的 control_primitives。"""
        b = budget or GraphBudget()
        sub = self.subgraph_for_topic(topic_id, b)
        if not sub.get("found"):
            # 维度层里没有这道题的焦点（运行期题的维度**不建节点**，只落边）
            # → 别返回空串，那等于把支架整个撤掉。支架信息就在边上。
            # 实测：不兜底时 `plant_success_rate_lotus` 的图谱提示长度为 0。
            return _render_from_links(topic_id, self)

        by_id = {n["id"]: n for n in sub["nodes"]}
        lines: list[str] = []
        # 焦点维度用到的列 → 展示表结构时排在前面（否则会被列数预算截掉；
        # 实测 fb_team_won 就被 14 列的预算挤没了，而它正是要 AVG 的那一列）
        # 按表分开记：同名列（series_id）张冠李戴会让人写出 Binder Error。
        priority_cols: dict[str, list[str]] = {}
        for dim in sub["focus_dims"]:
            node = by_id.get(f"dim:{dim}")
            if node is None:
                continue
            d = node["detail"]
            head = f"· 维度 {dim}（权重 {d.get('weight')}）"
            lines.append(head)
            by_topic = (d.get("points_by_topic") or {}).get(topic_id) or []
            pts = by_topic or d.get("points") or (
                [d["point"]] if d.get("point") else [])
            if pts:
                lines.append(f"   要拿什么：{'；'.join(str(p) for p in pts[:3])}")
            if d.get("semantics"):
                lines.append(f"   怎么算：{d['semantics']}")
            vcs = d.get("value_columns") or []
            if vcs:
                lines.append("   取结果的第 "
                             + "、".join(str(v) for v in vcs)
                             + " 列（从 0 开始数）")
            if with_recipe and d.get("recipe"):
                lines.append(f"   结构骨架：{d['recipe']}")
                lines.append("   ⚠ 骨架里 WHERE 的**位置**就是口径：内层先编号、"
                             "外层再筛。不要把所有过滤条件合并到最内层 —— "
                             "合并了段就断了/连错了。")
                lines.append(f"   自检：{SELF_CHECK_NOTE}")
            tool = d.get("tool")
            if tool:
                tnode = by_id.get(f"tool:{tool}") or {}
                req = tnode.get("detail", {}).get("required_params") or []
                hint = tnode.get("detail", {}).get("param_hint") or ""
                lines.append(
                    f"   取数工具：{tool}（按 {d.get('value_path')} 取值）"
                    + (f"；**必填参数**：{', '.join(req)}" if req else "")
                    + (f"；注意：{hint}" if hint else ""))
            if d.get("tables"):
                # 必须写破"表名 ≠ 工具名"：实测模型把图谱里的
                # `来自表：agg_first_blood_stats` 直接写成了 calls[].tool，
                # 连撞三次计划校验，整轮一个工具都没调成。
                lines.append(f"   来自表（要用 query_sql 自己查，**表名不是工具名**）："
                             f"{'、'.join(d['tables'])}")
            if d.get("tool") == "query_sql" and not d.get("tables"):
                lines.append("   取数工具：query_sql")
            cols = d.get("columns") or []
            if cols:
                lines.append(f"   用到的列：{', '.join(cols[:b.max_cols])}")
            for tbl, cs in (d.get("columns_by_table") or {}).items():
                cur = priority_cols.setdefault(tbl, [])
                for c in cs:
                    if c not in cur:
                        cur.append(c)
            if d.get("typical_errors"):
                lines.append("   典型错法："
                            + "；".join(str(e) for e in d["typical_errors"][:4]))
            # 单位：伴学知识点自带 unit，MVE 以前没有 —— 模型于是把
            # 「转换率」算成小数还是百分数全靠运气（实测交过 0.55 和 55 两种）。
            if d.get("unit"):
                lines.append(f"   单位：{d['unit']}"
                             + ("（百分数，不是小数）" if d["unit"] == "%" else ""))
            if d.get("skills"):
                lines.append(f"   要用到的 SQL 技能：{'、'.join(d['skills'])}")

        # 表结构：只列焦点维度真正读的表（写 SQL 时最需要的一份）
        # 带上**它在数据流的哪一层、上游是谁** —— 这是"22 张表之间的关系"，
        # 之前图谱里只有表名和列名，看不出 agg_player_game_stats 是从
        # agg_player_round_stats 再聚合出来的。
        tbl_nodes = [n for n in sub["nodes"] if n["kind"] == "table"]
        for t in tbl_nodes:
            cols = list(t["detail"].get("columns") or [])
            if not cols:
                continue
            ordered = [c for c in priority_cols.get(t["label"], []) if c in cols]
            ordered += [c for c in cols if c not in ordered]
            stage = str(t["detail"].get("stage") or "")
            up = [u[len("table:"):] for u in
                  self.upstream_tables(t["label"], depth=2)]
            lines.append(
                f"· 表 {t['label']}"
                + (f"［{STAGE_LABEL.get(stage, stage)}］" if (stage and with_stage)
                   else "")
                + f"（{t['detail'].get('grain') or ''}"
                + (f"；建模粒度 {gdoc}" if (gdoc := t["detail"].get("grain_doc")) else "")
                + (f"；主键 {', '.join(pk)}" if (pk := t["detail"].get("pk") or []) else "")
                + f"，{t['detail'].get('rows', 0)} 行）"
                + (f" 上游：{'、'.join(up)}" if (up and with_stage) else "")
                + f"\n   列：{', '.join(ordered[:b.max_cols])}"
                + (f" …共 {t['detail'].get('n_columns', 0)} 列"
                   if t["detail"].get("n_columns", 0) > b.max_cols else ""))
            # 列**含义**：VLML 建模文档写的口径。以前只有列名，模型只能按常识
            # 猜（fb_player 猜成 player_name、map 猜成别的），撞了三次才改对。
            desc = t["detail"].get("column_desc") or {}
            if desc:
                named = [c for c in ordered[:b.max_cols] if c in desc]
                if named:
                    named = named[:b.max_cols]
                    lines.append("   列的含义："
                                 + "；".join(
                                     f"{c}={str((desc[c] or {}).get('desc') or '')[:60]}"
                                     for c in named[:6]))
            if t["detail"].get("purpose"):
                lines.append(f"   这张表是干什么的：{t['detail']['purpose']}")

        # 取哪一段：value_path 的段首就是工具的 section（洞察目录）。
        # 缺了这层，模型知道调 match_analysis_report，却不知道值在
        # key_metrics 而不是 team_comparison 里。
        for dim in sub["focus_dims"]:
            if not with_section:
                break
            node = by_id.get(f"dim:{dim}")
            if node is None:
                continue
            sec = node["detail"].get("section")
            declared = str(node["detail"].get("tool") or "")
            if not sec:
                continue
            # FROM_SECTION 边可能不在子图里（那条边的工具未必是 produced_by
            # 的工具），所以直接查全图；并且**只认它自己声明的那个工具** ——
            # 不同工具的 section 可能同名（key_metrics 就有两份），
            # 不认主会指到错误的工具上。
            tools = sorted({
                e.src[len("tool:"):]
                for e in self._in.get(f"dim:{dim}", [])
                if e.relation == FROM_SECTION and e.src.startswith("tool:")
                and (not declared or e.src == f"tool:{declared}")
            })
            if tools:
                lines.append(f"· {dim} 取自 {'、'.join(tools)} 的 `{sec}` 段"
                             f"（不是整包返回里的任意位置）")
                if with_insight:
                    lines.extend(self._insight_lines(dim, sec, tools, b))

        confusables = [e for e in sub["edges"] if e["relation"] == CONFUSABLE]
        if confusables:
            pairs = sorted({tuple(sorted((e["from"][4:], e["to"][4:])))
                            for e in confusables})
            lines.append("· 易混维度："
                         + "；".join(f"{a} ↔ {b}" for a, b in pairs[:6]))

        if not lines:
            return ""
        return ("【知识图谱 · 本题相关子图】下面的口径来自服务端配方，"
                "优先于你的猜测；其中 '?' 是脱敏占位符，真实取值必须你自己查。\n"
                + "\n".join(lines))

    def _insight_lines(self, dim: str, sec: str, tools: list[str],
                       b: "GraphBudget") -> list[str]:
        """洞察层：这一段背后是哪些 SQL、它们读哪些表。

        为什么要有这一段：`key_metrics` 段实际是 5 个洞察拼出来的
        （team_round_metrics / team_economy_pistol / …），每个读不同的表。
        不知道这层，模型只能"调工具 + 在返回里翻"，翻错了就交 null。
        """
        out: list[str] = []
        for tool in tools[:1]:
            node = self.nodes.get(f"tool:{tool}")
            if node is None:
                continue
            si = (node.detail.get("section_insights") or {}).get(sec) or []
            if not si:
                continue
            parts = []
            for name in si[:b.max_insights]:
                inode = self.nodes.get(f"insight:{name}")
                if inode is None:
                    parts.append(name)
                    continue
                purpose = str(inode.detail.get("purpose") or "")
                # tables 里存的可能是裸表名，也可能带 "table:" 前缀 —— 实测
                # 直接切 len("table:")=6 会把 agg_player_round_stats 切成
                # "ayer_round_stats"，所以必须先看有没有前缀。
                tbls = [t[6:] if t.startswith("table:") else t
                        for t in inode.detail.get("tables") or []]
                tbls = [t for t in tbls if t]
                parts.append(f"{name}"
                             + (f"（{purpose}）" if purpose else "")
                             + (f"→读 {','.join(tbls[:3])}" if tbls else ""))
            if parts:
                out.append(f"   `{sec}` 段由 {len(si)} 个洞察拼成："
                           + "；".join(parts))
        return out

    # ---- 持久化 ----
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
            "summary": {
                "dimensions": len(self.dimensions()),
                "tools": len(self.tools()),
                "tables": len(self.tables()),
                "edges": len(self.edges),
                # 缓存过期判据：见 `load()` 里的 `_stale`。
                # 两个指纹缺一不可：题变了要重建（tasks.py），
                # VLML 建模文档变了也要重建（声明来自文档，不是来自题）。
                "task_fingerprint": _task_fingerprint(),
                "model_fingerprint": _model_fingerprint(),
            },
        }

    def save(self, path: Path = GRAPH_PATH) -> None:
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1),
                        encoding="utf-8")

    @classmethod
    def load(cls, path: Path = GRAPH_PATH) -> "KnowledgeGraph":
        g = cls()
        if not path.exists():
            return g
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        # 过期守卫：图谱是 `build()` 的**落盘缓存**。加了一道新题却不重建，
        # 图谱里就永远没有那个维度 —— 实测 `kast_adr_check` 就是这样：
        # 图谱里没有 kast_pct / kd_ratio，提示整段为空，模型连着三轮在错分支
        # 上重试。缓存必须自己知道过期。
        #
        # 三个判据，缺一个都会漏：
        #   题指纹   —— 加题/改题（tasks.py 的 answer_spec 变了）
        #   建模指纹 —— VLML 的建模文档改了（表节点与维度节点的声明来自文档）
        #   格式版本 —— 节点的声明字段本身改了（题没变、文档也没变，但**代码**变了）
        if _stale(raw):
            try:
                g = build()
                g.save(path)
                return g
            except Exception:
                pass          # 重建失败就退回旧图，总比没有强
        for n in raw.get("nodes") or []:
            g.add_node(Node(id=n["id"], kind=n["kind"],
                            label=n.get("label", ""), detail=n.get("detail") or {}))
        for e in raw.get("edges") or []:
            g.add_edge(Edge(src=e["from"], dst=e["to"], relation=e["relation"],
                            origin=e.get("origin", "declared"),
                            reason=e.get("reason", ""),
                            confidence=float(e.get("confidence") or 1.0)))
        return g


# --------------------------------------------------------------------------
# 构建
# --------------------------------------------------------------------------
def _task_fingerprint() -> str:
    """tasks.py 里「题目 → 维度 → 声明路径」的指纹。

    图谱的输入就是这个指纹覆盖的东西；指纹变了说明加/改了题，缓存必须重建。
    """
    try:
        from tasks import TASKS
    except Exception:
        return ""
    parts = []
    for topic_id in sorted(TASKS):
        for p in (getattr(TASKS[topic_id], "rubric", None) or []):
            s = getattr(p, "answer_spec", None)
            parts.append(
                f"{topic_id}|{p.dimension}|"
                f"{getattr(s, 'tool', '') or ''}:{getattr(s, 'value_path', '') or ''}"
                f":{getattr(s, 'sql', '') or ''}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _model_fingerprint() -> str:
    """VLML 四份建模文档的指纹（`vlml_schema.model_specs()`）。

    表节点与维度节点的声明是从这些文档解析出来的 —— 文档改了，图就得重建。
    """
    try:
        return str(vlml_schema.model_specs().get("fingerprint") or "")
    except Exception:
        return ""


def _stale(raw: dict[str, Any]) -> bool:
    """三判据：格式版本 / 题指纹 / 建模文档指纹。"""
    if int(raw.get("schema_version") or 0) != SCHEMA_VERSION:
        return True
    summary = raw.get("summary") or {}
    fp = _task_fingerprint()
    if fp and str(summary.get("task_fingerprint") or "") != fp:
        return True
    mf = _model_fingerprint()
    # 建模指纹为空（文档读不到）时不判过期 —— 否则每次 load 都重建，慢且吵
    if mf and str(summary.get("model_fingerprint") or "") != mf:
        return True
    return False


def _create_table_columns(text: str) -> dict[str, list[str]]:
    """从 CREATE TABLE 语句里解析列名（schema/*.sql 是唯一权威来源之一）。

    为什么需要它：VLML 的 `DATA_DICTIONARY.json` 只有 13 张表，**不含派生表**
    （agg_first_blood_stats 就不在里面）。而 fb_conversion_analysis 四个评分点
    全读这张表 —— 缺了它，图谱就给不出列名，Voyager 只能猜（实测猜成
    `fb_conv` / `round_number` 这类不存在的列）。

    只取"CREATE TABLE 名字（"到配对的 ");" 之间的第一段，逐行取首个标识符，
    跳过注释行与约束行（PRIMARY/FOREIGN/UNIQUE/CHECK/CONSTRAINT）。
    """
    out: dict[str, list[str]] = {}
    for m in re.finditer(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)\s*\(",
            text, re.IGNORECASE):
        name, start = m.group(1), m.end()
        depth, i = 1, start
        while i < len(text) and depth > 0:
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        body = text[start:i - 1] if depth == 0 else text[start:start + 2000]
        cols: list[str] = []
        for raw in body.split("\n"):
            line = raw.split("--")[0].strip().rstrip(",")
            if not line:
                continue
            head = line.split()[0].strip('"`[]')
            if head.upper() in {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK",
                                "CONSTRAINT", "INDEX"}:
                continue
            if not re.fullmatch(r"[A-Za-z_][\w]*", head or ""):
                continue
            if head not in cols:
                cols.append(head)
        if cols:
            out[name] = cols
    return out


def _known_tables() -> dict[str, dict[str, Any]]:
    """VLML 的数据建模表 → {grain, columns, pk, source}。认不出的表名不进图。

    三个来源都要扫，只扫一个会漏：
    - `DATA_DICTIONARY.json`：13 张，带 grain/字段说明，但**不含派生表**；
    - `database/schema/*.sql`：17 张建表语句（含 agg_first_blood_stats），
      用 `_create_table_columns` 解析列名；
    - `database/transformations/*.sql`：13 张派生转换，文件名就是表名
      （`01_` 这类序号前缀要剥掉），至少能让表名进图。

    实测漏掉的后果：fb_conversion_analysis 的四个评分点全读
    `agg_first_blood_stats`，而它不在数据字典里 → derived_from 边全丢，
    图谱里只剩 1 张表，Voyager 写 SQL 时没有任何列名可依。

    返回结构从「表名 → 说明字符串」升级成「表名 → 结构字典」：
    图谱要能回答"这张表有哪些列"，光有 grain 不够。
    """
    out: dict[str, dict[str, Any]] = {}
    if DATA_DICT.exists():
        try:
            rows = json.loads(DATA_DICT.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            rows = []
        for row in rows if isinstance(rows, list) else []:
            name = str(row.get("table") or "").strip()
            if not name:
                continue
            cols = row.get("columns")
            col_names = [str(c) for c in cols] if isinstance(cols, dict) else []
            out[name] = {
                "grain": str(row.get("grain") or "").strip()
                         or f"{len(col_names)} 列",
                "columns": col_names,
                "pk": [str(p) for p in (row.get("pk") or [])],
                "source": "DATA_DICTIONARY.json",
            }

    for sub in ("schema", "transformations"):
        for path in sorted((DATA_DICT.parent / sub).glob("*.sql")):
            name = re.sub(r"^\d+_", "", path.stem)
            if not name:
                continue
            parsed: dict[str, list[str]] = {}
            try:
                if sub == "schema":
                    # 实测服务器上的 schema/*.sql 有一个是 GBK（0xa3 开头，
                    # 中文注释），本地副本恰好全是 UTF-8 所以一直没暴露。
                    # 列名全是 ASCII，坏字节换掉不影响解析结果。
                    parsed = _create_table_columns(
                        path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                parsed = {}
            cols = parsed.get(name) or []
            if name not in out:
                out[name] = {
                    "grain": (f"{sub} 定义（{path.name}）" if not cols
                              else f"{len(cols)} 列 · {sub} 定义"),
                    "columns": cols,
                    "pk": [],
                    "source": f"{sub}/{path.name}",
                }
            elif not out[name]["columns"] and cols:
                # 数据字典里有 grain 但没列（或列为空）→ 用建表语句补上
                out[name]["columns"] = cols
                out[name]["source"] += f" + {sub}/{path.name}"
    return out


def _tables_in_sql(sql: str, known: dict[str, Any]) -> list[str]:
    """从 SQL 里抽表名，只保留数据字典认得的表（防止把 CTE 名当表）。"""
    found = []
    for name in _RE_FROM.findall(sql or ""):
        if name.lower() in {"select", "where", "group", "order", "limit"}:
            continue
        if name in known:
            found.append(name)
    return sorted(set(found))


# --------------------------------------------------------------------------
# 指标语义：把 answer_spec 变成"这个指标怎么算/从哪取"
# --------------------------------------------------------------------------
# 为什么必须有这一段（这是本轮的核心改动）：
#
#   TOOL_CATALOG 只给了工具签名和几张表的列名，却没说 **指标本身是什么**。
#   实测 corrode_collapse 第 2 轮：模型照着点名调了 query_sql，却把
#   `fb_conv`（要算的指标）当成列名写进 SELECT，把 `round_number` 当成
#   agg_first_blood_stats 的列 —— 它知道要查什么，不知道这个数是怎么来的。
#
#   原版 Voyager 怎么解决同类问题？它把 control_primitives 的**源码**拼进
#   system message —— 模型能看到每个技能函数的实现，而不是只有函数名。
#   MVE 的同构物就是这里：把每个维度的「计算式 / 取值路径 / 用哪些列」交底。
#
#   泄题边界：SQL 里的**字面量一律抹成 '?'**。给口径、给结构，不给答案。
#   （WHERE series_id='?' AND map_name='?' 只说明"要按这两列过滤"，
#     至于具体哪张图、哪个队，得它自己查。）
# --------------------------------------------------------------------------

_LITERAL_RE = re.compile(r"'[^']*'")
_WS_RE = re.compile(r"\s+")


def _desensitize(sql: str) -> str:
    """抹掉 SQL 里的字符串字面量 —— 给口径，不给数值。"""
    return _LITERAL_RE.sub("'?'", sql or "")


def _sql_semantics(sql: str) -> str:
    """最外层 SELECT 的表达式（含别名），脱敏后截断。"""
    m = re.search(r"\bselect\b(.*?)\bfrom\b", sql or "", re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    expr = _WS_RE.sub(" ", _desensitize(m.group(1))).strip().strip(",")
    return expr[:200]


def _sql_recipe(sql: str, limit: int = 700) -> str:
    """整条 SQL 的脱敏骨架 —— 复杂指标（窗口函数/嵌套）才需要它。

    `semantics` 只给最外层 SELECT，遇到 gaps-and-islands 这种写法
    （max_losing_streak 用 ROW_NUMBER() OVER (PARTITION BY ...) 分组）
    就看不出结构了 —— 那正是 18 轮全 0% 卡住的地方。整条骨架给出去，
    模型才知道"连败要用窗口函数分段，不是 COUNT 一下"。

    limit 为什么是 700 而不是 400：实测 400 会把骨架尾巴截成
    `... ORDER BY cnt DESC, st…` —— 给了半条 SQL 比不给更糟，模型只能
    自己补后半段。骨架要么完整给，要么不给。
    """
    if not sql:
        return ""
    flat = _WS_RE.sub(" ", _desensitize(sql)).strip()
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _has_structure(sql: str) -> bool:
    """这条 SQL 有没有"结构"（嵌套子查询 / 窗口函数）？没有就只给 semantics。"""
    return bool(re.search(r"\bover\s*\(|\bfrom\s*\(|\brow_number\b|\brank\s*\(|"
                          r"\bdense_rank\b|\blag\s*\(|\blead\s*\(",
                          sql or "", re.IGNORECASE))


def _used_columns(sql: str, tables: list[str],
                  known: dict[str, Any]) -> dict[str, list[str]]:
    """这条 SQL 在**每张表上**真正用到哪些列。

    不靠正则猜标识符（猜出来的列会带进函数别名和关键字），而是拿已知列名单
    逐个 `\b` 匹配。列名单来自 DATA_DICTIONARY / 建表语句，都是可追溯的。

    返回「表 → 列」而不是一列扁平列表：两张表可能有同名列（series_id），
    混在一起会把 A 表的列算到 B 表头上，模型照着写就 Binder Error。
    """
    out: dict[str, list[str]] = {}
    for t in tables:
        cols = (known.get(t) or {}).get("columns") or []
        hits = [c for c in cols
                if re.search(r"\b" + re.escape(c) + r"\b", sql or "")]
        if hits:
            out[t] = hits[:24]
    return out


# --------------------------------------------------------------------------
# 典型错法：照伴学 knowledge_seeds 里的 `typical_misconceptions`
# --------------------------------------------------------------------------
# 伴学怎么写（`static/knowledge_seeds/history.json`）：
#   "typical_misconceptions": ["把郡县制与分封制混为一谈", "只写措施不说明作用"]
# 特点：①**人工维护的种子**，不是模型生成的；②写的是"错法"，不是"正确答案"；
#      ③在出题、讲解、认知诊断里都会被读（`targeted_question_contract.py:27-29`）。
#
# MVE 的同构项就是这里：每个维度"模型实际踩过的坑"。前四条来自实测日志
# （53 轮诊断：max_losing_streak 算出 14/13 而真值是 8、start_round 算出 1
#   而真值是 7），其余来自 LOCAL_SQL_NOTES 里记录的三类幻觉。
#
# 这不是"答案"，是"陷阱清单" —— 给它看，等于伴学把常见错误列在知识点上。
# --------------------------------------------------------------------------
DIM_PITFALLS: dict[str, list[str]] = {
    "max_losing_streak": [
        # 致命顺序按**实测命中率**排，不是按"通用→具体"排。
        # 渲染只取前 N 条，把最致命的排在后面等于没写 —— 实测这条排第 4 位
        # 时被 [:3] 切掉，模型写出一字不差的旧错法。
        # 现象：算出 13，真值 8。它照抄了骨架，却把队伍过滤**提前**到最内层
        # WHERE —— 那时只剩 Cloud9 输的回合，编号自然连续，整段被当成一块。
        # 顺序是口径的一部分：内层只按 series+map 过滤、先编号，外层才筛队伍。
        "把 losing_team_name 的过滤提前到最内层 WHERE —— 必须先对**全部回合**"
        "编号（内层只过滤 series_id + map_name），再在外层筛目标队输的回合；"
        "提前筛会把所有输的回合连成一整段",
        "把「全场一共输了多少回合」当成最长连败（要数**连续**的一段）",
        "跨图累加：回合号按全 series 排序，没先按 map_name 分开算",
        "按半场（第 12 回合）重置 —— 口径不重置",
    ],
    "streak_start_round": [
        "用 MIN(round_number) 取起点 —— 那拿到的是全场最小回合号，"
        "不是**最长那一段**的起点",
        "同上：队伍过滤提前到内层会让段长算错，起点跟着错",
        "取了另一段（较短那段）的起点",
    ],
    "conversion.fb_conv": [
        "把首血次数当转换率（转换率 = 赢了的首血 / 首血总数）",
        "漏乘 100（题目要求 0-100 的百分比，如 25.0）",
        "把 fb_team_won 当布尔字符串比较，而不是 AVG 它",
    ],
    "map_fb_conv": [
        "用全场数据算分图转换率（必须加 map_name 过滤）",
        "分母用成全 series 的首血数",
    ],
    "opening_duels.fb": [
        "拿全库首血数当这支队的（必须按 fb_team 过滤）",
    ],
    "map_rounds": [
        "三张图求和时重复计数（rounds 表一行 = 一个回合，按 map_name 分组数）",
    ],
    "map_win_rate": [
        "用 games 表的胜负算回合胜率（胜率是**回合级**的，要用 rounds 表）",
        "漏乘 100",
    ],
    "total_rounds": [
        "把两张图的回合数相加重复计数",
        "用 games 表数回合（games 一行 = 一张图）",
    ],
    "rounds_won": [
        "用 games.winning_team_name 数回合（那是整图胜者，不是回合数）",
    ],
    "pistol_win_rate": [
        "和 eco_win_rate 混用（同一个 key_metrics.economy 下的两个不同键）",
        "自己按回合硬算（工具内部已有聚合口径，直接取 key_metrics）",
    ],
    "eco_win_rate": [
        "和 pistol_win_rate 混用",
    ],
    "pattern_rounds": [
        "拿全 series 回合数当样本数（要取工具返回的 scope.rounds）",
    ],
    "pattern_confidence": [
        "自己编一个置信度标签 —— 必须原样取工具返回的 scope.confidence",
        "把 confidence 当成数值（它是 moderate / strong / weak / insufficient）",
    ],
}


# 带**内联注释**的骨架：自动抽取的骨架只能显示字符串，说不出"哪一层该有什么"。
#
# 为什么需要手写版：实测把完整骨架 + "WHERE 位置是口径"的一般警告都给了，
# 模型仍然四次写出一字不差的错法 —— 把 `losing_team_name='Cloud9'` 提前到
# 最内层 WHERE。它读到了骨架，但把三层 WHERE 当成"三个等价的过滤条件"合并了。
# 一般警告打不过这个先验；只有在**出错的那一行上**写注释才拦得住。
#
# 与 DIM_PITFALLS 同源：都是人工维护的种子，不是模型生成的。
DIM_RECIPE_NOTES: dict[str, str] = {
    "max_losing_streak":
        "SELECT cnt AS max_streak, start_round FROM ("
        " SELECT COUNT(*) AS cnt, MIN(round_number) AS start_round FROM ("
        "  SELECT round_number, losing_team_name,"
        " ROW_NUMBER() OVER (ORDER BY round_number) AS rn,"
        " ROW_NUMBER() OVER (PARTITION BY losing_team_name ORDER BY round_number) AS grp"
        "  FROM rounds WHERE series_id='?' AND map_name='?'"
        "  /* ↑ 内层：只许 series_id + map_name。写 losing_team_name 就全错 */"
        " ) t WHERE losing_team_name='?'"
        " /* ↑ 外层：队伍过滤只能在这里，编号之后 */"
        " GROUP BY (rn - grp)"
        ") x ORDER BY cnt DESC, start_round ASC LIMIT 1",
    "streak_start_round":
        "同上（同一条 SQL，取第 1 列）：内层 WHERE 只有 series_id + map_name，"
        "losing_team_name 必须在外层 ) t WHERE 之后",
}

# 自检规则：给一条**它自己能判定**的检查，而不是一句"别写错"。
# 光警告"不要合并"没有可操作的判据；有了这条，它能在写完 SQL 后自己比对。
SELF_CHECK_NOTE = (
    "写完后自检：最内层 WHERE（FROM 之后那个）里只允许出现 series_id、map_name；"
    "如果那里出现了要筛的队伍列（losing_team_name / winning_team_name），"
    "删掉它——它属于外层。自检办法：先单独 COUNT 一下该队在这张图输了多少回合，"
    "若你的 max_streak 正好等于这个数，说明你没分段。"
)


def _required_params(tool: str) -> list[str]:
    """这个工具**必填**哪些参数（没写默认值、也不是 *args/**kwargs 的）。

    为什么图谱要记这个：实测 pistol_eco_pattern 那道题，模型确实照图谱调了
    `pattern_detection_report`，但**一个参数都没传** —— 工具返回空 pattern，
    于是手枪局胜率交了 null、样本数 0、置信度 insufficient，覆盖率 0%。
    图谱给了"调哪个工具、取哪条路径"，却没说"不传 team_name 就什么都拿不到"。

    参数表从函数签名抽（可追溯），不是手写。
    """
    import inspect

    try:
        import vlml_env
    except Exception:
        return []
    fn = getattr(vlml_env, tool, None)
    if fn is None and tool == "query_sql":
        fn = getattr(vlml_env, "execute_custom_sql", None)
    if fn is None:
        return []
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return []
    out = []
    for p in sig.parameters.values():
        if p.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          inspect.Parameter.KEYWORD_ONLY):
            continue
        if p.default is inspect.Parameter.empty:
            out.append(p.name)
    return out


# --------------------------------------------------------------------------
# 参数的"语义必要性"：签名抓不到，只能人工标注
# --------------------------------------------------------------------------
# `_required_params()` 抽的是**语法必填**（没有默认值的参数）。但真正坑人的
# 是那些有默认值、不传也不报错、可结果为空的参数：
#   pattern_detection_report(team_name=None, player_name=None, ...) 全都有默认值，
#   实测模型一个参数都不传 → 返回空 pattern → 手枪局胜率交 null、样本数 0、
#   置信度 insufficient → 覆盖率 0%，而它明明"照图谱调对了工具"。
# 这条只能像 DIM_PITFALLS 一样人工写，来源标注为 seed。
# --------------------------------------------------------------------------
TOOL_PARAM_HINTS: dict[str, str] = {
    "pattern_detection_report":
        "至少要传 team_name（或 player_name）才会有有效 scope；"
        "不传 → scope.rounds=0、confidence=insufficient，四个评分点全空。"
        "series_ids 要传**数组** ['2843069']，传字符串会被逐字符拆开"
        "（实测 → rounds=0）",
    "query_sql":
        "sql_query 必填，且必须以 SELECT 开头（不支持 CTE）",
    "match_rounds_report":
        "数据集大时必须用 round_start/round_end 分页，否则会很慢或截断",
}

# 同上，但这是**可机检**的那部分：这些参数至少要传一个，否则结果必为空。
# 有了它，校验层就能在调用发出之前拦下（而不是等裁判说"你交了 null"）。
# 实测：pattern_detection_report() 不传参 → scope=null；
#       传 team_name='Cloud9' → scope={rounds:59, confidence:moderate}（真值）。
TOOL_NEEDS_ANY_OF: dict[str, list[str]] = {
    "pattern_detection_report": ["team_name", "player_name"],
}

# 参数**类型**的坑：语法上不报错，语义上全错。
# 实测：series_ids 传字符串 '2843069' → 被当成逐字符拆开 →
#   scope 变成 {series: 7, rounds: 0, confidence: insufficient}，四个评分点全空；
#   传数组 ['2843069'] → {series: 1, rounds: 59, confidence: moderate}（真值）。
# 模型写 JSON 时习惯给字符串，这条必须在调用前拦下。
TOOL_PARAM_TYPES: dict[str, dict[str, str]] = {
    "pattern_detection_report": {"series_ids": "list"},
}


def build(*, observe: bool = False, with_schema: bool = True) -> KnowledgeGraph:
    """从 tasks 的 rubric + VLML 数据字典 + VLML 源码结构构建图谱。

    observe=True 时会额外跑一遍裁判，用**实测**的编排顺序补 procedure_step
    边（慢，且要联网/连库），默认只用声明式信息。
    with_schema=True 时把 VLML 的 21 张表 / 46 个洞察 / 7 个工具一起进图
    （实查 information_schema + 解析源码；连不上库时自动退回文件解析）。
    """
    import vlml_env  # noqa: F401  引导环境
    from tasks import TASKS

    g = KnowledgeGraph()
    known_tables = _known_tables()

    # ---- VLML 的真实结构：21 张表 + 46 个洞察 + 7 个工具 ----
    # 之前只从**我们自己的 rubric SQL** 反推，结果图谱里只有 2 张表 ——
    # 等于"出过题的才进图"，VLML 真正的建模一概没进。
    # 实查 information_schema + 解析源码（表/派生/洞察/工具）补齐四层。
    schema: dict[str, Any] = {}
    if with_schema:
        try:
            import vlml_schema
            schema = asyncio.run(vlml_schema.discover())
        except Exception as e:                      # 连不上库就退回文件解析
            print(f"（VLML 结构发现失败，退回文件解析：{type(e).__name__}: {e}）")
            schema = {}
    if schema.get("tables"):
        known_tables = schema["tables"]             # 实查的列比文件解析更准

    # --- 工具节点：vlml_env 暴露的报告工具 ---
    tool_names = [
        "match_summary_report", "match_analysis_report", "match_economy_report",
        "match_players_report", "match_rounds_report", "pattern_detection_report",
        "player_profile_report", "scouting_report", "query_sql",
    ]
    for t in tool_names:
        detail: dict[str, Any] = {"required_params": _required_params(t)}
        hint = TOOL_PARAM_HINTS.get(t)
        if hint:
            detail["param_hint"] = hint
            detail["param_hint_origin"] = "seed"
        need = TOOL_NEEDS_ANY_OF.get(t)
        if need:
            detail["needs_any_of"] = list(need)
        types = TOOL_PARAM_TYPES.get(t)
        if types:
            detail["param_types"] = dict(types)
        g.add_node(Node(id=f"tool:{t}", kind="tool", label=t, detail=detail))

    # `requires_tools` 是**题级**声明（"这道题要用到这些工具"），不是维度级的
    # "由它产出"。之前照样建 produced_by，结果 query_sql 因为是每道题的
    # requires_tools 而出现在所有维度的产出者里 —— who_produces 就废了。
    # 正确做法：记在工具节点的 detail 上，不建边。
    for topic_id, task in TASKS.items():
        for tool in getattr(task, "requires_tools", []) or []:
            nid = f"tool:{tool}"
            if nid not in g.nodes:
                g.add_node(Node(id=nid, kind="tool", label=tool))
            node = g.nodes[nid]
            req = list(node.detail.get("required_by") or [])
            if topic_id not in req:
                req.append(topic_id)
            node.detail["required_by"] = sorted(req)

    # --- 维度节点 + produced_by / derived_from / co_occurs / confusable ---
    #
    # ⚠️ 运行期题（generated_tasks.json 采纳的）的维度**不建节点**。
    # 照伴学 `knowledge_tracker.py:1548/1555`：
    #     topic_id = self._ensure_topic(...)          # 先解析，解析不到才建
    #     is_known_topic = bool(self.store.get_topic(topic_id))
    #     qa_topic_id = topic_id if is_known_topic else ""     ← 新建的不进答题记录
    # 即伴学运行期新建的知识点是**空壳（source=runtime，声明全空）+ 不进掌握度
    # + 进待审候选队列**（`upsert_candidate`, evidence=MENTIONED）。
    # MVE 更严：连空壳都不建 —— 维度层的权威来源是**种子题的 rubric**，
    # 运行期的新名字一律走**边**（`link_import`），不占节点位。
    # 不这么做的话，每采纳一道新题就多一个维度节点（实测 15 → 16），
    # 等于"题目派生图谱"，正好是伴学的反面。
    try:
        from tasks import RUNTIME_TOPICS
        runtime_topics = set(RUNTIME_TOPICS)
    except Exception:                                        # pragma: no cover
        runtime_topics = set()
    pending: dict[str, dict[str, Any]] = {}

    dims_by_topic: dict[str, list[str]] = {}
    for topic_id, task in TASKS.items():
        dim_here: list[str] = []
        for p in task.rubric:
            dim = str(p.dimension)
            if not dim:
                continue
            if topic_id in runtime_topics:
                resolved = _resolve_existing_dim(dim, g)
                if resolved:
                    dim = resolved                  # 复用已有维度（同构 _resolve_topic_id）
                else:
                    # 不建节点，只记待审候选 —— 面板可查，人来决定要不要转正
                    spec0 = getattr(p, "answer_spec", None)
                    sql0 = str(getattr(spec0, "sql", "") or "")
                    pending[dim] = {
                        "dimension": dim,
                        "topic_id": topic_id,
                        "question": str(getattr(task, "question", ""))[:300],
                        "point": str(getattr(p, "point", "") or ""),
                        "nearest": _nearest_dim(dim, g),
                        "sql": sql0[:4000],
                        "tables": (_tables_in_sql(sql0, known_tables)
                                   if sql0 else []),
                        "at": datetime.now().isoformat(timespec="seconds"),
                        "source": "runtime",
                    }
                    continue
            dim_here.append(dim)

            # 维度节点：把"这个指标是什么、怎么算、用哪些列"一次写全
            detail: dict[str, Any] = {
                "topic_id": topic_id,
                "topics": [topic_id],
                "point": p.point,
                "points": [p.point],
                # 同一维度会被多道题共用（map_fb_conv 同时属于两道题）。
                # 渲染子图时只显示**当前这道题**的评分点，否则会把别题的
                # subject 一起带进来（实测：corrode 题里冒出 Haven 的评分点）。
                "points_by_topic": {topic_id: [p.point]},
                "weight": p.weight,
            }
            spec = getattr(p, "answer_spec", None)
            if spec is not None:
                if spec.sql:
                    tbls = _tables_in_sql(spec.sql, known_tables)
                    detail["tables"] = tbls
                    cols_map = _used_columns(spec.sql, tbls, known_tables)
                    detail["columns_by_table"] = cols_map
                    detail["columns"] = sorted(
                        {c for v in cols_map.values() for c in v})
                    detail["semantics"] = _sql_semantics(spec.sql)
                    if _has_structure(spec.sql):
                        detail["recipe"] = _sql_recipe(spec.sql)
                        # 有手写注释版就用手写版 —— 它能标出"哪一层该有什么"
                        note = DIM_RECIPE_NOTES.get(dim)
                        if note:
                            detail["recipe"] = note
                            detail["recipe_origin"] = "annotated"
                    detail["tolerance"] = spec.numeric_tolerance
                    # 同一条 SQL 会被多个评分点共用，各自取不同的列
                    # （max_losing_streak 取第 0 列、streak_start_round 取第 1 列）。
                    # 不说清"取第几列"，模型会拿错。
                    detail["value_columns"] = [spec.value_column]
                    if spec.base_column is not None:
                        detail["base_columns"] = [spec.base_column]
                elif spec.tool:
                    # 工具内部聚合的指标（pistol/eco/confidence）：没有 SQL，
                    # 语义就写在"从返回结构的哪条路径取值"上。
                    detail["tool"] = spec.tool
                    detail["value_path"] = spec.value_path
                    detail["semantics"] = (
                        f"由 {spec.tool} 内部聚合，按点分路径取值："
                        f"{spec.value_path}"
                        + ("（百分比：num/denom*100）" if spec.percent else "")
                    )
            pitfalls = DIM_PITFALLS.get(dim)
            if pitfalls:
                detail["typical_errors"] = list(pitfalls)
                detail["typical_errors_origin"] = "seed"

            g.add_node(Node(id=f"dim:{dim}", kind="dimension", label=dim,
                            detail=detail))

            if spec is None:
                continue
            if spec.tool:
                # 声明式：服务端配好的取数配方（loop_core.py:185）
                if f"tool:{spec.tool}" not in g.nodes:
                    g.add_node(Node(id=f"tool:{spec.tool}", kind="tool",
                                    label=spec.tool))
                g.add_edge(Edge(
                    src=f"tool:{spec.tool}", dst=f"dim:{dim}",
                    relation=PRODUCED_BY, origin="declared",
                    reason=f"{topic_id} 的 rubric 声明：按 {spec.value_path} 取值",
                    confidence=1.0))
            elif spec.sql:
                for tbl in _tables_in_sql(spec.sql, known_tables):
                    info = known_tables.get(tbl) or {}
                    g.add_node(Node(id=f"table:{tbl}", kind="table", label=tbl,
                                    detail={"grain": info.get("grain", ""),
                                            "columns": list(info.get("columns")
                                                            or [])[:24],
                                            "pk": list(info.get("pk") or []),
                                            "source": info.get("source", "")}))
                    g.add_edge(Edge(
                        src=f"table:{tbl}", dst=f"dim:{dim}",
                        relation=DERIVED_FROM, origin="declared",
                        reason=f"{topic_id} 的确定性 SQL 读这张表",
                        confidence=1.0))
        dims_by_topic[topic_id] = sorted(set(dim_here))

        # co_occurs：同一题一起出现的维度（对称）
        uniq = sorted(set(dim_here))
        for i, a in enumerate(uniq):
            for b in uniq[i + 1:]:
                g.add_edge(Edge(src=f"dim:{a}", dst=f"dim:{b}",
                                relation=CO_OCCURS, origin="declared",
                                reason=f"同题 {topic_id} 要求一起覆盖",
                                confidence=0.9))
        # confusable：同后缀不同前缀的维度（pistol_win_rate / eco_win_rate）
        for a in uniq:
            for b in uniq:
                if a >= b:
                    continue
                sa, sb = a.split("_"), b.split("_")
                if len(sa) < 2 or len(sb) < 2:
                    continue
                if sa[1:] == sb[1:] and sa[0] != sb[0]:
                    g.add_edge(Edge(src=f"dim:{a}", dst=f"dim:{b}",
                                    relation=CONFUSABLE, origin="declared",
                                    reason="同后缀不同前缀，易混",
                                    confidence=0.8))

    # --- 工具节点反查：这个工具能出哪些维度 ---
    # who_produces 是"维度 → 工具"，这里补反向索引，渲染时直接读 detail。
    for e in g.edges:
        if (e.relation == PRODUCED_BY and e.src.startswith("tool:")
                and e.dst.startswith("dim:")):
            node = g.nodes.get(e.src)
            if node is None:
                continue
            cur = list(node.detail.get("yields") or [])
            dim = e.dst[len("dim:"):]
            if dim not in cur:
                cur.append(dim)
            node.detail["yields"] = sorted(cur)

    if schema:
        _add_vlml_schema(g, schema)
        _add_seed_facts(g, schema)

    # VLML 的建模声明：**读文件**，连不上库也在。放在 schema 之后是因为
    # 它要往已经建好的表节点上补声明（实查的列名 + 文档的口径合起来才完整）。
    model: dict[str, Any] = {}
    if vlml_schema is not None:
        try:
            model = vlml_schema.model_specs()
        except Exception as e:        # 文档读不到就只缺声明，不能整图崩掉
            print(f"（VLML 建模声明解析失败：{type(e).__name__}: {e}）")
    if model:
        _add_model_declarations(g, model)
    _add_dimension_declarations(g, model)

    if observe:
        asyncio.run(_observe_procedure(g))
    _merge_imported_links(g)
    # 运行期题带进来的新维度名：登记为待审候选，不进维度层
    n_pending = _save_pending_dimensions(pending)
    if n_pending:
        print(f"（运行期题的新维度 {n_pending} 个 → 待审候选，未建节点："
              f"{'、'.join(sorted(pending))[:120]}）")
    return g


# --------------------------------------------------------------------------
# 人导入的问题 → 图谱**边**（不是节点）
# --------------------------------------------------------------------------
# ⚠️ 这一段的建谱依据被**推翻重建过一次**，改之前先读这段注释。
#
# 【错的做法（v1，已废）】匹配不到就往图谱里加一个 `dim:` **维度节点**。
#   依据是从 `TASKS[].rubric` 反推（tasks.py:361「出过题的才进图」）——
#   这个依据本身就是错的：它让维度层永远只能装下"已经出过题"的方向，
#   人问的新方向天然无处落脚，于是只能靠造节点续命。
#
# 【对的依据（v2，现在）】**VLML 的表展开到工具集那层，图谱是完全覆盖的**。
#   实测：46 个 `insight` 节点 = VLML 的 `tools/sql/*.sql`，每个都声明了
#   `tables`（读哪些表）和 `reports`（被哪些报告工具 composes）—— 表 × 工具集
#   这一层没有缺口。所以收录判据不是"图谱里有没有同名的维度"，而是
#   **「VLML 能不能用表内覆盖的工具集完成这个工作」**：
#     人问的题 → VLML 真跑了一次取数 → 拿到真实 SQL / 工具 / 表
#     → 查哪些 insight 覆盖了这些表（且被这个工具用）
#     → 命中就是"工具集覆盖得到" → **收录**
#
# 【落法】既然覆盖层不需要补，就**不建节点，建边**（用户原话）：
#     伴学 `MaterialTopicMapper` 映射不上时交回 UI 让人选节点，从不为材料
#     新建知识点；MVE 同理 —— 人的题是一个**应用场景**，场景挂在边上，
#     不占节点位。
#
#   实际落的三类边（全部 origin=observed，因为都来自真实取数）：
#     insight:I --application--> table:T    这个洞察在这张表上被用过一次
#     tool:X    --composes-----> insight:I  该工具这次确实用了这个洞察（补全）
#     insight:A --co_occurs----> insight:B  同一道题要求这两个洞察一起用（对称）
#   退化情形（纯 query_sql 直查表，没有 insight 覆盖）：
#     tool:X    --application--> table:T    按**表级**收录，置信度下调到 0.8
#
# 同构关系：
#   伴学  MaterialTopicMapper → 映射到已有知识点 / 映射不上交回人选，**不建节点**
#   MVE   link_import()       → 判工具集覆盖 / 覆盖就**建边**，不建节点
IMPORTED_LINKS = HERE / "imported_links.json"
IMPORTED_DIMS = HERE / "imported_dimensions.json"   # v1 遗留，仅用于一次性迁移


def load_imported_links() -> list[dict[str, Any]]:
    """人导入落的**边**（持久化，build 时并回图谱）。

    v1 的 `imported_dimensions.json`（造节点的旧机制）在这里**一次性迁移**
    成边记录，然后把旧文件改名收尾 —— 老数据不丢，但不再走建节点那条路。
    """
    try:
        raw = json.loads(IMPORTED_LINKS.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else []
    except Exception:
        items = []
    if IMPORTED_DIMS.exists():
        try:
            old = json.loads(IMPORTED_DIMS.read_text(encoding="utf-8"))
        except Exception:
            old = []
        if isinstance(old, list) and old:
            have = {(str(it.get("question") or ""), str(it.get("dimension") or ""))
                    for it in items}
            for o in old:
                if not isinstance(o, dict):
                    continue
                k = (str(o.get("question") or ""), str(o.get("dimension") or ""))
                if k in have:
                    continue
                known = _known_tables()
                sql = str(o.get("sql") or "")
                tbls = _tables_in_sql(sql, known) if sql else []
                cov = coverage_of(tables=tbls, tool=str(o.get("tool") or ""))
                items.append({
                    "question": str(o.get("question") or "")[:300],
                    "dimension": str(o.get("dimension") or ""),
                    "topic_id": "",
                    "sql": sql[:4000],
                    "tool": str(o.get("tool") or ""),
                    "tables": tbls,
                    "covered": bool(cov["covered"]),
                    "cover_level": cov["level"],
                    "insights": cov["insights"],
                    "subject": dict(o.get("subject") or {}),
                    "value": o.get("value"),
                    "linked_at": str(o.get("adopted_at") or ""),
                    "origin": "human_import(migrated_from_v1_dimension)",
                })
                have.add(k)
            try:
                IMPORTED_LINKS.write_text(
                    json.dumps(items, ensure_ascii=False, indent=1),
                    encoding="utf-8")
                IMPORTED_DIMS.rename(HERE / "imported_dimensions.json.v1")
            except Exception:
                pass
    return items


def _insight_index(g: KnowledgeGraph) -> list[tuple[str, set[str], set[str]]]:
    """(insight 节点 id, 它读的表, 用它的报告工具) —— 工具集覆盖的依据。"""
    out: list[tuple[str, set[str], set[str]]] = []
    for nid, node in g.nodes.items():
        if not nid.startswith("insight:"):
            continue
        det = node.detail or {}
        out.append((nid,
                    {str(t) for t in (det.get("tables") or [])},
                    {str(r) for r in (det.get("reports") or [])}))
    return out


def coverage_of(*, tables: list[str] | set[str], tool: str = "",
                g: KnowledgeGraph | None = None,
                max_hits: int = 3) -> dict[str, Any]:
    """**收录判据**：VLML 能不能用「表内覆盖的工具集」完成这个工作？

    ⚠️ `max_hits` 是必须的，不是优化。实测：一次问「回合胜率」用到
    `rounds` + `games` 两张**主干表**，46 个 insight 里 16 个都读它们 ——
    不限流的话一次导入就落 32 条 application + 16 条 composes +
    C(16,2)=120 条 co_occurs = **168 条边**，直接把图谱淹掉，也爆掉伴学
    `GraphBudget.max_edges=30` 的预算。

    所以这里取的是**最小覆盖集**：按「这次用到的表被它覆盖了多少」排序，
    只留最靠前的 `max_hits` 个 —— 这才是"哪个工具集完成了这个工作"的
    精确答案，而不是"哪些工具碰过这些表"。

    返回值里的 `level`：
      - `insight` —— 有 insight 覆盖了这次取数读到的表（最理想，工具集覆盖）
      - `table`   —— 没有 insight 覆盖，但表本身在图里（一般是用 query_sql
                     直查；工具集 = 通用 SQL，也算覆盖得到，置信度下调）
      - `none`    —— 连表都不在图谱里 → **覆盖不到，不收录**，如实交回
                     （伴学同款：映射不上不硬造，交回 UI 让人处理）
    """
    try:
        g = g or KnowledgeGraph.load()
    except Exception:
        return {"covered": False, "level": "none", "insights": [],
                "insights_total": 0, "tables": []}
    tset = {str(t) for t in (tables or []) if t}
    idx = _insight_index(g)
    tool = str(tool or "").strip()

    scored: list[tuple[float, int, str]] = []
    for nid, itabs, ireps in idx:
        ov = itabs & tset
        if not ov:
            continue
        # 覆盖度 = 这次的表被它覆盖的比例；调用的工具确实声明了这个洞察则加权
        s = len(ov) / max(1, len(tset)) + (0.5 if (tool and tool in ireps) else 0.0)
        scored.append((s, len(ov), nid))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))

    if scored:
        top = [nid for _, _, nid in scored[:max(1, max_hits)]]
        return {"covered": True, "level": "insight",
                "insights": sorted(n[len("insight:"):] for n in top),
                "insights_total": len(scored),
                "tables": sorted(tset)}
    known = {str(t) for t in g.tables()}
    if tset and tset <= known:
        return {"covered": True, "level": "table", "insights": [],
                "insights_total": 0, "tables": sorted(tset)}
    return {"covered": False, "level": "none", "insights": [],
            "insights_total": 0, "tables": sorted(tset)}


# --------------------------------------------------------------------------
# 运行期题的维度 → 待审候选（伴学 `upsert_candidate` 的同构物，**不建节点**）
# --------------------------------------------------------------------------
PENDING_DIMS = HERE / "pending_dimensions.json"


def load_pending_dimensions() -> list[dict[str, Any]]:
    """运行期题带进来、但没解析到已有维度的维度名 —— 只登记，不进维度层。"""
    try:
        raw = json.loads(PENDING_DIMS.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def _norm_dim(d: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(d or "").lower())


def _dim_tokens(d: str) -> set[str]:
    return {t for t in re.split(r"[^a-zA-Z0-9]+", str(d or "").lower()) if len(t) >= 2}


def _resolve_existing_dim(dim: str, g: KnowledgeGraph) -> str:
    """运行期题的维度名先解析到**已有**维度 —— 伴学 `_resolve_topic_id` 的同构。

    只认两档硬匹配：完全同名 / 归一化同名（去标点与大小写）。
    **不做模糊匹配**：把两个不同的量并成一个，比多留一个待审候选
    代价大得多 —— 那会直接污染掌握度与出题口径。
    """
    if f"dim:{dim}" in g.nodes:
        return dim
    norm = _norm_dim(dim)
    for d in g.dimensions():
        if _norm_dim(d) == norm:
            return d
    return ""


def _nearest_dim(dim: str, g: KnowledgeGraph) -> str:
    """给待审候选一个"最像的已有维度"建议 —— 只用于提示，不自动合并。

    照伴学 `_suggest_dims`（只说"换一个维度"模型换不对，要给候选）。
    """
    toks = _dim_tokens(dim)
    if not toks:
        return ""
    best, score = "", 0.0
    for d in g.dimensions():
        t = _dim_tokens(d)
        if not t:
            continue
        s = len(toks & t) / len(toks | t)
        if s > score:
            best, score = d, s
    return best if score >= 0.34 else ""


def _render_from_links(topic_id: str, g: "KnowledgeGraph") -> str:
    """维度层没有这道题的焦点时，用**边上的载荷**渲染提示。

    运行期题的维度不建节点（照伴学：运行期知识点不进权威层），所以
    `subgraph_for_topic` 会 `found=False`。但支架信息并没有丢 —— 它在
    `imported_links.json` / `pending_dimensions.json` 里：那次真实取数的
    SQL 能解析出表、列、口径、结构骨架。不给这段，模型做题时就是裸奔。

    实测：不兜底时新题的图谱提示长度为 0（等于支架被撤），出了题却练不了。
    """
    recs: list[dict[str, Any]] = []
    try:
        recs += [r for r in load_imported_links()
                 if str(r.get("topic_id") or "").strip() == str(topic_id)]
    except Exception:
        pass
    try:
        recs += [r for r in load_pending_dimensions()
                 if str(r.get("topic_id") or "").strip() == str(topic_id)]
    except Exception:
        pass
    if not recs:
        return ""

    b = GraphBudget()
    lines: list[str] = []
    seen: set[str] = set()
    for r in recs:
        dim = str(r.get("dimension") or "").strip()
        if not dim or dim in seen:
            continue
        seen.add(dim)
        sql = str(r.get("sql") or "")
        sh = sql_shape(sql, list(r.get("tables") or []))
        lines.append(f"· 维度 {dim}（运行期题带来的新维度，**未进维度层**，"
                     "口径来自它入库时真实跑通的取数）")
        if r.get("point"):
            lines.append(f"   要拿什么：{r['point']}")
        if sh["tables"]:
            lines.append("   来自表（要用 query_sql 自己查，"
                         "**表名不是工具名**）：" + "、".join(sh["tables"]))
        if sh["columns"]:
            lines.append("   用到的列：" + ", ".join(sh["columns"][:b.max_cols]))
        if sh["semantics"]:
            lines.append(f"   怎么算：{sh['semantics']}")
        if sh["recipe"]:
            lines.append(f"   结构骨架：{sh['recipe']}")
            lines.append(f"   自检：{SELF_CHECK_NOTE}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _save_pending_dimensions(pending: dict[str, dict[str, Any]]) -> int:
    """待审候选落盘。仍"运行期且未解析"的才留着，其余自然消失。"""
    if not pending:
        return 0
    try:
        old = {str(r.get("dimension") or ""): r for r in load_pending_dimensions()}
    except Exception:
        old = {}
    old.update(pending)
    try:
        PENDING_DIMS.write_text(
            json.dumps(sorted(old.values(), key=lambda r: str(r.get("dimension"))),
                       ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        return 0
    return len(pending)


def sql_shape(sql: str, tables: list[str] | None = None) -> dict[str, Any]:
    """从一段**真实跑过的** SQL 解析出「出题形状」：表 / 列 / 口径 / 骨架 / 难度。

    人导入的题不建节点，所以出题器没有 `dim:` 节点可翻 —— 它翻的是**边**，
    而边上的信息就是这份 shape。数据来源同样是实测：解析那段真跑过的 SQL，
    不是模型写的。
    """
    known = _known_tables()
    txt = str(sql or "")
    tbls = list(tables or []) or _tables_in_sql(txt, known)
    cols_map = _used_columns(txt, tbls, known) if (txt and tbls) else {}
    return {
        "tables": tbls,
        "columns_by_table": cols_map,
        "columns": sorted({c for v in cols_map.values() for c in v}),
        "semantics": _sql_semantics(txt) if txt else "",
        "recipe": _sql_recipe(txt) if txt else "",
        "difficulty": _sql_difficulty(txt) if txt else 2,
        "skills": _skills_of(txt),
    }


def link_import(*, question: str, dimension: str = "", sql: str = "",
                tool: str = "", tables: list[str] | None = None,
                topic_id: str = "", value: Any = None,
                subject: dict[str, Any] | None = None) -> dict[str, Any]:
    """把人导入的问题**收进图谱的边上**，不建任何节点。返回落盘记录。

    判据（见 `coverage_of`）：VLML 用真实取数读到的表，有没有被图谱里
    「表内覆盖的工具集」（insight）覆盖到。覆盖得到 → 收录 → 建边。
    覆盖不到 → `adopted=False` 如实返回，**绝不静默造一个节点顶上**。

    边的来源必须是实测（origin="observed"）：这些表/洞察不是声明出来的，
    是这次取数**真的**读到了、真的用了。
    """
    known = _known_tables()
    tbls = list(tables or []) or (_tables_in_sql(sql, known) if sql else [])
    cov = coverage_of(tables=tbls, tool=tool)
    rec: dict[str, Any] = {
        "question": str(question or "")[:300],
        "dimension": str(dimension or "").strip(),
        "topic_id": str(topic_id or "").strip(),
        "sql": str(sql or "")[:4000],
        "tool": str(tool or "").strip(),
        "tables": sorted(set(tbls)),
        "covered": bool(cov["covered"]),
        "cover_level": str(cov["level"]),
        "insights": list(cov["insights"]),
        "insights_total": int(cov.get("insights_total") or 0),
        "subject": dict(subject or {}),
        "value": value,
        "linked_at": datetime.now().isoformat(timespec="seconds"),
        "origin": "human_import",
    }
    if not rec["covered"]:
        rec["adopted"] = False
        rec["reason"] = ("工具集覆盖不到：这次取数读的表 "
                         f"{sorted(set(tbls)) or '（没解析出表）'} "
                         "既不在任何 insight 覆盖范围里、也不全在图内 "
                         "→ 不收录，也不建节点")
        return rec

    items = [it for it in load_imported_links()
             if not (str(it.get("question") or "") == rec["question"]
                     and str(it.get("dimension") or "") == rec["dimension"])]
    items.append(rec)
    try:
        IMPORTED_LINKS.write_text(
            json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass

    # 光落盘不够：**当前这张图里也必须立刻有这些边**。出题器读的是
    # `KnowledgeGraph.load()` 出来的对象，不是 json 文件。指纹没变 → 下次
    # load 不重建 → 补丁留得住；指纹一变，build() 里的
    # `_merge_imported_links` 会从 json 重新并边，不会丢。
    try:
        g = KnowledgeGraph.load()
        n = _merge_imported_links(g)
        g.save()
        rec["edges_added"] = n
    except Exception:
        pass                                                 # 打补丁失败不影响落盘
    rec["adopted"] = True
    return rec


def _merge_imported_links(g: KnowledgeGraph) -> int:
    """build 时把人导入落的**边**并回图 —— 不建一个节点。

    为什么要持久化而不是只在导入时改一次图：图谱是 `build()` 的产物，
    指纹一变（加了新题、VLML 文档变了）就整体重建，直接在图对象上打的
    补丁会被冲掉。落盘成 `imported_links.json`，与
    `question_gen.generated_tasks.json` 是同一种持久化思路。
    """
    added = 0
    for rec in load_imported_links():
        # **覆盖要在合并时重算，不能信落盘时的旧结论**。实测：图谱重建前
        # 服务器上 insight 层是空的，一条人导入被判成 table 级；重建后
        # insight 补齐了，可那条记录还写着 table 级 —— 边就永远停在低置信上。
        # 判据的输入（真实 SQL / 工具 / 表）都存在记录里，重算是免费的。
        tbls0 = [str(t) for t in (rec.get("tables") or [])]
        cov = coverage_of(tables=tbls0, tool=str(rec.get("tool") or ""), g=g)
        if not cov["covered"]:
            continue                     # 覆盖不到的不进图（伴学：映射不上不硬造）
        dim = str(rec.get("dimension") or "").strip()
        q = str(rec.get("question") or "")[:40]
        tag = f"人导入「{q}」"
        dim_tag = f"（维度 {dim}）" if dim else ""
        ins = [f"insight:{i}" for i in (cov.get("insights") or [])
               if f"insight:{i}" in g.nodes]
        tbls = [f"table:{t}" for t in (cov.get("tables") or [])
                if f"table:{t}" in g.nodes]
        tool = str(rec.get("tool") or "").strip()
        tool_id = f"tool:{tool}" if tool else ""
        tset = set(tbls)

        # 1) application：这个洞察在这张表上被实际应用过一次
        #    （伴学 `application` 的焦点在 from 端 —— from 是洞察，没反）
        for i in ins:
            node = g.nodes.get(i)
            itset = {f"table:{t}" for t in ((node.detail or {}).get("tables") or [])}
            for t in sorted(itset & tset):
                g.add_edge(Edge(src=i, dst=t, relation=APPLICATION,
                                origin="observed",
                                reason=f"{tag} 实测：这个洞察在该表上被用到{dim_tag}",
                                confidence=0.9))
            # 2) composes：这个工具这次确实用了这个洞察（补声明里没写的）
            if tool_id:
                g.add_edge(Edge(src=tool_id, dst=i, relation=COMPOSES,
                                origin="observed",
                                reason=f"{tag} 实测：{tool} 这次用了这个洞察",
                                confidence=0.9))

        # 3) co_occurs（对称）：同一道题要求这两个洞察一起用 —— 伴学语义
        #    「同题共现」，MVE 原来只用在维度上，这里同构地用到洞察层
        for a in range(len(ins)):
            for b in range(a + 1, len(ins)):
                g.add_edge(Edge(src=ins[a], dst=ins[b], relation=CO_OCCURS,
                                origin="observed",
                                reason=f"{tag} 实测：这两个洞察一起被用到",
                                confidence=0.9))

        # 4) 退化情形：没有 insight 覆盖（多为 query_sql 直查）→ 按表级收录
        if not ins and tool_id:
            for t in sorted(tset):
                g.add_edge(Edge(src=tool_id, dst=t, relation=APPLICATION,
                                origin="observed",
                                reason=(f"{tag} 实测：{tool} 直查该表"
                                        "（无 insight 覆盖，按表级收录）" + dim_tag),
                                confidence=0.8))
        added += 1
    return added


# v1 的名字，留着做兼容壳：任何还在调 `adopt_dimension` 的地方自动走新逻辑
# （建边，不建节点）。参数沿用 v1 的命名，行为以 `link_import` 为准。
def adopt_dimension(*, dimension: str = "", sql: str = "", tool: str = "",
                    question: str = "", subject: dict[str, Any] | None = None,
                    value: Any = None, **_: Any) -> dict[str, Any]:
    """**已废弃**：v1 的「建维度节点」。现在一律走 `link_import` 建边。"""
    if not str(dimension or "").strip():
        return {}
    return link_import(question=question, dimension=dimension, sql=sql,
                       tool=tool, subject=subject, value=value)


_GRAIN_ZH = {"round": "回合", "game": "图", "series": "series", "player": "选手",
             "team": "队伍", "map": "地图", "date": "日", "tournament": "赛事",
             "entity_type": "实体类型", "entity": "实体", "factor": "因子"}


def _zh_grain(doc: str) -> str:
    """把建模文档写的粒度（`(round_id, team_name)`）翻成中文行粒度。

    `_grain_of_table()` 只认 `agg_{主体}_{粒度}_stats` 这种命名，
    7 张派生表名字不合约定就一律退化成"聚合表"三个字 —— 而
    agg_first_blood_stats 恰恰是**一回合一行**，说成"聚合表"，模型就会
    再 JOIN 一次 rounds，行数翻倍。文档里有精确粒度，用它。
    """
    toks = re.findall(r"[a-z_][a-z_0-9]*", (doc or "").lower())
    if not toks:
        return "", 0
    keys = [t[:-3] if t.endswith("_id") else
            (t[:-5] if t.endswith("_name") else t) for t in toks]
    # "One row per round" 这类英文散文不是键列表 —— 认不全就别硬翻，
    # 否则会翻出「每（one × row × per × 回合）一行」这种鬼话（实测）。
    if any(k not in _GRAIN_ZH for k in keys):
        return "", 0
    zh = [_GRAIN_ZH[k] for k in keys]
    return (f"每{zh[0]}一行" if len(zh) == 1
            else "每（" + " × ".join(zh) + "）一行"), len(zh)


def _add_model_declarations(g: KnowledgeGraph, specs: dict[str, Any]) -> None:
    """把 VLML 的**建模声明**写进表节点 —— 伴学 knowledge_seeds 的同构物。

    伴学的 457 个知识点节点自带 19 个字段（difficulty / question_types /
    typical_misconceptions / prerequisites / unit / depth / …），出题器读节点
    声明来定题型与口径，模型只写题面。VLML 里同层的"作者声明"就是
    `database/` 下四份建模文档：

        DATA_MODEL.md          粒度 Grain + 用途 Use cases + 主干血缘
        DERIVED_TABLES.md      7 张派生表的粒度/上游/关键列/示例查询
        column_definitions.yaml 列级口径
        DATA_DICTIONARY.json   主键 / 列类型 / **指标公式**（sum/denom）

    之前这些一概没进图：表节点只有"列名 + 行数 + 中文粒度"，`pk` 全是空
    数组（实测 `table:rounds` 的 pk=[] ，而文档白纸黑字写着 round_id），
    派生表连粒度都只写"聚合表"三个字。出题时模型只能猜列的含义 ——
    这正是它把 `fb_player` 写成 `player_name` 的根源。
    """
    tbl_specs = specs.get("tables") or {}
    if not tbl_specs:
        return
    declared_by = ("VLML 建模文档：DATA_MODEL.md / DERIVED_TABLES.md / "
                   "metadata/column_definitions.yaml / DATA_DICTIONARY.json")

    # 第一遍：先把节点建齐（上游表可能排在下游表后面，边要等两端都在）
    for name, s in sorted(tbl_specs.items()):
        nid = f"table:{name}"
        if nid not in g.nodes:
            g.add_node(Node(id=nid, kind="table", label=name, detail={}))
        node = g.nodes[nid]
        grain_doc = s.get("grain") or ""
        # 命名约定推不出来（"聚合表"/"参考/字典表"）就换成文档写的精确粒度；
        # 文档粒度有三个以上键时（如 agg_team_map_stats 是 队伍×地图×赛事）
        # 约定版也表达不了，同样以文档为准。
        zh_grain, n_keys = _zh_grain(grain_doc)
        if zh_grain and (node.detail.get("grain") in ("", None, "聚合表", "参考/字典表")
                         or n_keys >= 3):
            node.detail["grain"] = zh_grain
        node.detail.update({
            "purpose": s.get("purpose") or node.detail.get("purpose") or "",
            "grain_doc": grain_doc,             # 建模文档写的精确粒度
            "pk": list(s.get("pk") or node.detail.get("pk") or []),
            "column_desc": dict(s.get("column_desc") or {}),
            "metrics": dict(s.get("metrics") or {}),
            "layer": s.get("layer") or node.detail.get("layer") or "",
            "parent": s.get("parent") or "",
            "use_cases": s.get("use_cases") or "",
            "example_sql": s.get("example_sql") or "",
            "declared_by": declared_by,
        })

    # 第二遍：建模文档声明的上游（DERIVED_TABLES 的 Source + 主干父表）。
    # 为什么要单独建一遍：`discover()` 的 lineage 来自实查 transformations，
    # 断库就没了；这条来自文档，永远在。origin 标 "model" 以示区别。
    for name, s in sorted(tbl_specs.items()):
        for up in s.get("upstream") or []:
            if f"table:{up}" not in g.nodes:
                continue
            g.add_edge(Edge(src=f"table:{up}", dst=f"table:{name}",
                            relation=DERIVED_FROM, origin="model",
                            reason=f"建模文档声明：{name} 由 {up} 算出",
                            confidence=1.0))


# --------------------------------------------------------------------------
# 维度节点的伴学式声明
# --------------------------------------------------------------------------
# 伴学节点 19 个字段里，能在这里**真填**的 12 个；剩下 7 个（aliases /
# curriculum_tags / curriculum_version / exam_region / exam_type 等）在
# MVE 里没有同构物 —— 不编、不填。填了假声明比不填更危险。
def _sql_difficulty(sql: str) -> int:
    """这条 SQL 的难度档（1-4），判据可数，不靠模型自评。

    与 `question_gen.DIFFICULTY_RUBRIC` 同一套分档（1 单表聚合 / 2 分组 /
    3 JOIN 或条件分支 / 4 窗口函数或嵌套子查询）。图谱自带难度，出题器
    就不用各处重新数一遍关键字了 —— 伴学的 difficulty 也是长在节点上的。
    """
    low = (sql or "").lower()
    if "over (" in low or "over(" in low or "from (select" in low:
        return 4
    if " join " in low or "case when" in low or " having " in low:
        return 3
    if "group by" in low:
        return 2
    return 1


def _unit_of(dim: str, d: dict[str, Any]) -> str:
    """这个指标的单位（伴学 `unit` 的同构物）。按命名与口径文本推，可查。

    判据顺序有讲究：`max_losing_streak` 里的 "losing_**s**treak" 含 `_s`，
    先判时间就会把它错标成"秒"（实测就是这么错的）。先判连败/回合。
    """
    text = " ".join(str(d.get(k) or "") for k in ("point", "semantics")).lower()
    if ("pct" in dim or "rate" in dim or "conv" in dim or "kast" in dim
            or "adr" in dim or "%" in text or "率" in text):
        return "%"
    if "ratio" in dim or "share" in dim or "比值" in text:
        return "比值"
    if "streak" in dim or "rounds" in dim or "回合" in text:
        return "回合"
    if dim.endswith("_s") or "time" in dim or "delay" in dim or "秒" in text:
        return "秒"
    return "计数"


def _skills_of(sql: str) -> list[str]:
    low = (sql or "").lower()
    out = []
    if "over (" in low or "over(" in low or "row_number" in low:
        out.append("窗口函数")
    if "from (select" in low or "from(select" in low:
        out.append("嵌套子查询")
    if " join " in low:
        out.append("多表 JOIN")
    if "case when" in low or "coalesce(" in low:
        out.append("条件分支")
    if "group by" in low:
        out.append("分组聚合")
    if any(k in low for k in ("count(", "avg(", "sum(", "max(", "min(")):
        out.append("聚合函数")
    return out


LAYER_LABEL = {"core": "主干表", "derived": "派生表", "agg": "聚合表",
               "ref": "参考/字典表"}


def _table_depth(g: KnowledgeGraph, name: str, seen: set[str] | None = None) -> int:
    """这张表在数据流里被推导了几层（伴学 `depth` 的同构物）。"""
    seen = seen or set()
    nid = f"table:{name}"
    if nid in seen:
        return 0
    seen.add(nid)
    ups = [e.src[len("table:"):]
           for e in g._in.get(nid, []) if e.relation == DERIVED_FROM]
    ups = [u for u in ups if u != name]
    if not ups:
        return 0
    return 1 + max(_table_depth(g, u, seen) for u in ups)


def _add_dimension_declarations(g: KnowledgeGraph, specs: dict[str, Any]) -> None:
    """给 15 个维度节点补上伴学式的声明字段。

    此前这些字段**只存在于 detail 里由 tasks.py 临时派生的那几项**
    （tables / columns / semantics），而且没有任何一项是"教学声明"——
    没有难度、没有单位、没有先修、没有知识点级的典型错法。出题器要挑
    "上一档更难的点"时无从下手，只能自己数 SQL 关键字（还数错了）。
    """
    tbl_specs = specs.get("tables") or {}
    # 难度要从**完整 SQL** 数，不能从 detail 里的 `semantics` 数 ——
    # semantics 只是 SELECT 投影（"ROUND(AVG(fb_team_won)*100,1) AS conv"），
    # 里面没有 GROUP BY / JOIN，于是明明要分组的题被判成 1 档（实测
    # map_fb_conv 就是这样，出题器按"最简单"把它排在了最后）。
    sql_by_dim: dict[str, list[str]] = {}
    try:
        from tasks import TASKS
        for _tid, _t in TASKS.items():
            for _p in getattr(_t, "rubric", None) or []:
                _s = getattr(_p, "answer_spec", None)
                if _s is not None and getattr(_s, "sql", ""):
                    sql_by_dim.setdefault(str(_p.dimension), []).append(
                        _desensitize(str(_s.sql)))
    except Exception:
        sql_by_dim = {}

    for dim in sorted(g.dimensions()):
        node = g.nodes.get(f"dim:{dim}")
        if node is None:
            continue
        d = node.detail
        tables = list(d.get("tables") or [])
        recipe = str(d.get("recipe") or d.get("semantics") or "")
        # 只认 `recipe`（真 SQL 骨架）。`semantics` 对工具路径来说是
        # 一句中文说明，拿它数关键字会凭空得到 1 档 —— 工具维度应当是 0。
        sqls = sql_by_dim.get(dim) or ([d["recipe"]] if d.get("recipe") else [])

        # --- difficulty：图谱自带，出题器不再各数各的 ---
        # 工具路径没有 SQL 可数，标 0（不参与 1-4 分档），不假装是 1 档。
        d["difficulty"] = (max((_sql_difficulty(s) for s in sqls), default=0)
                           if sqls else 0)
        # --- unit / skills / question_types ---
        d["unit"] = _unit_of(dim, d)
        skills = _skills_of(" \n ".join(sqls))
        if skills:
            d["skills"] = skills
        d["question_types"] = (["sql_recipe"] if d.get("tables")
                               else ["tool_recipe"])

        # --- chapter / depth：这张维度站在数据流的哪一层 ---
        primary = tables[0] if tables else ""
        layer = str((tbl_specs.get(primary) or {}).get("layer") or "")
        d["chapter"] = (LAYER_LABEL.get(layer, layer) if tables
                        else "工具内聚合（不出 SQL）")
        d["depth"] = (1 + max((_table_depth(g, t) for t in tables), default=0)
                      if tables else 0)

        # --- prerequisites：先修 = 上游表（伴学 prerequisites 的同构物）---
        ups: list[str] = []
        for t in tables:
            # `upstream_tables` 返回的是**节点 id**，不是表名 —— 忘了剥前缀
            # 就会写出 "table:table:base_events"（实测就是这样）。
            for u in g.upstream_tables(t, depth=2):
                u = u[len("table:"):] if u.startswith("table:") else u
                if u != t and u not in ups:
                    ups.append(u)
        # 派生表本身也是"先会它的上游才谈得上用它"；再加一层建模文档声明的
        for t in tables:
            for u in (tbl_specs.get(t) or {}).get("upstream") or []:
                if u != t and u not in ups:
                    ups.append(u)
        d["prerequisites"] = [
            {"id": f"table:{u}", "relation": "prerequisite",
             "reason": f"{dim} 读的表 {primary or '（工具）'} 由 {u} 算出"
                       + (f"（{(tbl_specs.get(u) or {}).get('grain') or ''}）"
                          if (tbl_specs.get(u) or {}).get("grain") else "")}
            for u in ups[:4]]

        # --- related：易混 / 共现（图里本来就有边，这里做成节点可读的）---
        rel: list[dict[str, str]] = []
        for e in g._out.get(f"dim:{dim}", []) + g._in.get(f"dim:{dim}", []):
            other = e.dst if e.src == f"dim:{dim}" else e.src
            if not other.startswith("dim:") or other == f"dim:{dim}":
                continue
            if e.relation == CONFUSABLE:
                rel.append({"id": other, "relation": "confusable"})
            elif e.relation == CO_OCCURS:
                rel.append({"id": other, "relation": "co_occurs"})
        seen_rel: set[str] = set()
        rel = [r for r in rel if not (r["id"] in seen_rel or seen_rel.add(r["id"]))]
        if rel:
            d["related"] = rel[:6]

        # --- typical_misconceptions：种子错法 + 建模文档推出来的错法 ---
        misc = list(d.get("typical_errors") or [])
        for t in tables:
            s = tbl_specs.get(t) or {}
            grain = str(s.get("grain") or "")
            if s.get("layer") == "derived" and "round" in grain:
                misc.append(
                    f"{t} 已经是「{grain}」一行（主键 {','.join(s.get('pk') or []) or '见文档'}），"
                    "不要再 JOIN rounds 去重 —— 会放大行数")
            for m, formula in list((s.get("metrics") or {}).items())[:1]:
                misc.append(f"{t}.{m} 是「{formula}」算出来的，"
                            "不能直接 AVG 那个同名列")
            break
        if misc:
            d["typical_misconceptions"] = list(dict.fromkeys(misc))[:5]

        # --- examples：口径示例（伴学 examples 的同构物）---
        ex: list[str] = []
        if d.get("recipe"):
            ex.append(str(d["recipe"])[:400])
        elif d.get("semantics"):
            ex.append(str(d["semantics"])[:200])
        for t in tables[:1]:
            s = tbl_specs.get(t) or {}
            if s.get("example_sql"):
                ex.append(str(s["example_sql"])[:300])
        if ex:
            d["examples"] = ex[:2]
        d["name"] = dim
        d["subject"] = "vlml0"
        d["declared_by"] = ("VLML 建模文档 + tasks.py 的 answer_spec"
                            "（难度/单位/先修由文档推，口径由 answer_spec 定）")


def _add_vlml_schema(g: KnowledgeGraph, schema: dict[str, Any]) -> None:
    """把 VLML 的四层结构写进图：表 / 派生关系 / 洞察 / 工具 section。

    全部来自**实查与源码解析**（`vlml_schema.py`），没有一条是手写的。
    """
    import vlml_schema

    # --- 表：21 张，带 stage / grain / 列 / 行数 ---
    for name, info in sorted((schema.get("tables") or {}).items()):
        g.add_node(Node(
            id=f"table:{name}", kind="table", label=name,
            detail={
                "stage": info.get("stage", ""),
                "grain": info.get("grain", ""),
                "columns": list(info.get("columns") or [])[:32],
                "n_columns": len(info.get("columns") or []),
                "rows": info.get("rows", 0),
                "source": info.get("source", ""),
            }))

    # --- 派生：agg 表 ← 上游表（数据流的上游 = 伴学 prerequisite 的同构物）---
    for target, ups in sorted((schema.get("lineage") or {}).items()):
        for up in ups:
            g.add_edge(Edge(
                src=f"table:{up}", dst=f"table:{target}",
                relation=DERIVED_FROM, origin="declared",
                reason=f"transformations 定义：{target} 由 {up} 算出",
                confidence=1.0))

    # --- 洞察：46 个 SQL 单元 ---
    for name, info in sorted((schema.get("insights") or {}).items()):
        g.add_node(Node(
            id=f"insight:{name}", kind="insight", label=name,
            detail={
                "stage": vlml_schema.STAGE_INSIGHT,
                "tables": info.get("tables") or [],
                "reports": info.get("reports") or [],
                "purpose": info.get("purpose", ""),
                "file": info.get("file", ""),
            }))
        for t in info.get("tables") or []:
            g.add_edge(Edge(
                src=f"table:{t}", dst=f"insight:{name}",
                relation=READS, origin="declared",
                reason=f"{info.get('file', name)} 的 FROM/JOIN",
                confidence=1.0))
        for r in info.get("reports") or []:
            if f"tool:{r}" in g.nodes:
                g.add_edge(Edge(
                    src=f"tool:{r}", dst=f"insight:{name}",
                    relation=COMPOSES, origin="declared",
                    reason=f"{r} 通过 load_sql 使用它", confidence=0.9))

    # --- 工具：补 section（洞察目录）---
    for name, info in sorted((schema.get("tools") or {}).items()):
        nid = f"tool:{name}"
        if nid not in g.nodes:
            g.add_node(Node(id=nid, kind="tool", label=name))
        node = g.nodes[nid]
        node.detail["stage"] = vlml_schema.STAGE_TOOL
        node.detail["sections"] = info.get("sections") or []
        # section → 由哪些洞察 SQL 组成（洞察层要能进 prompt 就靠这一张表）
        si = (schema.get("section_insights") or {}).get(name) or {}
        if si:
            node.detail["section_insights"] = si
        desc = info.get("section_desc") or {}
        if desc:
            node.detail["section_desc"] = dict(list(desc.items())[:12])

    # --- 维度：标出它取工具的哪一段（value_path 的第一段就是 section）---
    # 这是"知道调哪个工具、不知道值从哪一段来"的那一层。
    for node in list(g.nodes.values()):
        if node.kind != "dimension":
            continue
        node.detail["stage"] = vlml_schema.STAGE_DIMENSION
        path = str(node.detail.get("value_path") or "")
        head = path.split(".")[0] if path else ""
        if not head:
            continue
        node.detail["section"] = head
        # 找哪些工具的 section 清单里有这一段（可能有多个工具都出这一段）
        for tnode in g.nodes.values():
            if tnode.kind != "tool":
                continue
            if head in (tnode.detail.get("sections") or []):
                g.add_edge(Edge(
                    src=tnode.id, dst=node.id,
                    relation=FROM_SECTION, origin="declared",
                    reason=f"value_path 段首 `{head}` = {tnode.label} 的一个 section",
                    confidence=0.8))


def _add_seed_facts(g: KnowledgeGraph, schema: dict[str, Any]) -> None:
    """种子层：**独立于题目**的指标全集（伴学 topics 的同构物）。

    为什么必须有它：`build()` 里的维度节点全部来自 `TASKS[].rubric`——
    出过题的才进图（实测只有 13 个维度，对应 5 道题）。新题进来，图谱里
    没有它的任何节点，`subgraph_for_topic` 直接返回 found=False。
    伴学不是这么做的：它的 82 个知识点种子先于题目存在，题目只用
    `match_topics(query)` 去匹配焦点，所以**任何题**都能配上图谱。
    这里照搬：从 VLML 发现的四层结构抽出所有可问的事实（洞察 / 工具段 / 表），
    不依赖任何一道题。
    """
    try:
        import graph_topics
    except Exception:                                   # pragma: no cover
        return
    facts = graph_topics.build_seed_facts(schema)
    for n in graph_topics.seed_facts_to_nodes(facts):
        g.add_node(Node(id=n["id"], kind=n["kind"], label=n["label"],
                        detail=n["detail"]))
    # 事实 → 它的来源（工具 / 段 / 洞察 / 表）：让 focused_context 能归桶
    for f in facts:
        fid = str(f.get("id") or "").strip()
        if not fid or fid not in g.nodes:
            continue
        tool = str(f.get("unit") or "").strip()
        if tool and f"tool:{tool}" in g.nodes:
            g.add_edge(Edge(src=f"tool:{tool}", dst=fid,
                            relation=PRODUCED_BY, origin="declared",
                            reason="VLML 源码：该工具产出这个事实",
                            confidence=0.9))
        for i in f.get("insights") or []:
            if f"insight:{i}" in g.nodes:
                g.add_edge(Edge(src=f"insight:{i}", dst=fid,
                                relation=COMPOSES, origin="declared",
                                reason=f"{i} 是它的计算单元", confidence=0.9))
        for t in f.get("tables") or []:
            if f"table:{t}" in g.nodes:
                g.add_edge(Edge(src=f"table:{t}", dst=fid,
                                relation=DERIVED_FROM, origin="declared",
                                reason=f"数据取自 {t}", confidence=0.9))
    g.detail_seed_facts = len(facts)    # type: ignore[attr-defined]


async def _observe_procedure(g: KnowledgeGraph) -> None:
    """跑一遍裁判，用实测的编排顺序补 procedure_step 边。

    声明式信息能回答"哪个工具出哪个维度"，但回答不了"先调哪个再调哪个"——
    那个顺序只有真跑一遍才知道（裁判的 trajectory 就是实测顺序）。
    """
    import vlml0_referee
    from tasks import TASKS

    for topic_id, task in TASKS.items():
        try:
            ref = await vlml0_referee.answer(task)
        except Exception:
            continue
        traj = [str(t) for t in (getattr(ref, "trajectory", None) or [])
                if not str(t).startswith("<")]
        for i in range(len(traj) - 1):
            a, b = traj[i], traj[i + 1]
            if a == b:
                continue
            if f"tool:{a}" not in g.nodes:
                g.add_node(Node(id=f"tool:{a}", kind="tool", label=a))
            if f"tool:{b}" not in g.nodes:
                g.add_node(Node(id=f"tool:{b}", kind="tool", label=b))
            g.add_edge(Edge(src=f"tool:{a}", dst=f"tool:{b}",
                            relation=PROCEDURE_STEP, origin="observed",
                            reason=f"{topic_id}：裁判实测的编排顺序",
                            confidence=0.95))
        # 实测的 produced_by：裁判 fact 的 source.tool 就是真正取到它的工具
        for f in (getattr(ref, "facts", None) or []):
            src = (f.get("source") or {}).get("tool") or ""
            if not src.startswith("vlml0_referee::"):
                continue
            tool = src.split("::", 1)[1]
            dim = str(f.get("dimension") or "")
            if not dim:
                continue
            if f"tool:{tool}" not in g.nodes:
                g.add_node(Node(id=f"tool:{tool}", kind="tool", label=tool))
            if f"dim:{dim}" not in g.nodes:
                g.add_node(Node(id=f"dim:{dim}", kind="dimension", label=dim))
            g.add_edge(Edge(src=f"tool:{tool}", dst=f"dim:{dim}",
                            relation=PRODUCED_BY, origin="observed",
                            reason=f"{topic_id}：裁判实测取到",
                            confidence=1.0))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _main() -> int:
    ap = argparse.ArgumentParser(description="MVE 知识图谱：维度/工具/表")
    ap.add_argument("--build", action="store_true", help="重建图谱并落盘")
    ap.add_argument("--observe", action="store_true",
                    help="--build 时额外跑裁判补实测边（慢）")
    ap.add_argument("--who", default="", help="查这个维度由哪个工具出")
    ap.add_argument("--topic", default="", help="列出这道题涉及的维度及其来源")
    ap.add_argument("--render", default="",
                    help="把这道题的子图渲染成给模型的文本（control_primitives 等价物）")
    ap.add_argument("--no-recipe", action="store_true",
                    help="--render 时不给 SQL 结构骨架（只给语义与列）")
    ap.add_argument("--stats", action="store_true", help="只看统计")
    args = ap.parse_args()

    if args.build:
        g = build(observe=args.observe)
        g.save()
        s = g.to_dict()["summary"]
        print(f"图谱已写入 {GRAPH_PATH}")
        print(f"  维度 {s['dimensions']} · 工具 {s['tools']} · "
              f"表 {s['tables']} · 边 {s['edges']}")
        for d in g.dimensions():
            prod = g.who_produces(d)
            tbl = g.tables_for(d)
            src = (f"工具 {','.join(prod)}" if prod else
                   (f"表 {','.join(tbl)}" if tbl else "（无来源）"))
            print(f"    {d:<28} ← {src}")
        return 0

    g = KnowledgeGraph.load()
    if not g.nodes:
        print("图谱不存在，先跑 --build")
        return 1

    if args.stats:
        print(json.dumps(g.to_dict()["summary"], ensure_ascii=False))
        return 0

    if args.who:
        sub = g.subgraph_for(args.who)
        if not sub.get("found"):
            print(f"图谱里没有维度 {args.who}")
            return 1
        print(f"维度        : {args.who}")
        print(f"  由工具产出: {', '.join(sub['produced_by']) or '（无）'}")
        print(f"  由表算出  : {', '.join(sub['derived_from']) or '（无）'}")
        print(f"  易混维度  : {', '.join(sub['confusable']) or '（无）'}")
        return 0

    if args.render:
        text = g.render_for_prompt(args.render, with_recipe=not args.no_recipe)
        if not text:
            print(f"图谱里没有题目 {args.render} 的维度，先跑 --build")
            return 1
        print(text)
        return 0

    if args.topic:
        import vlml_env  # noqa: F401
        from tasks import TASKS
        task = TASKS.get(args.topic)
        if task is None:
            print(f"未知题目 {args.topic}")
            return 1
        print(f"题目        : {args.topic}")
        print(f"  声明工具  : {', '.join(task.requires_tools) or '（无）'}")
        for p in task.rubric:
            d = str(p.dimension)
            prod = g.who_produces(d)
            tbl = g.tables_for(d)
            src = (f"工具 {','.join(prod)}" if prod else
                   (f"表 {','.join(tbl)}" if tbl else "（无来源）"))
            print(f"  {d:<28} ← {src}")
        return 0

    print("用法：--build / --who <维度> / --topic <题目> / --stats")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
