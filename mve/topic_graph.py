#!/usr/bin/env python3
"""MVE 的教学图谱 + 科学选题器（移植自猫娘伴学的 store_topics / graph_read_model）。

为什么是这一层（不是 knowledge_graph.py）
----------------------------------------
`knowledge_graph.py` 是**判分侧**子图：节点是 数据维度/工具/表，只为
answer_spec 判分服务（"这个维度该调哪个工具"）。

本文件是**教学侧**图谱：节点是伴学 `build_vlml_seed.py` 产出的 29 个 VLML
知识点（你亲手加的那个图谱），每个带 depth / difficulty / prerequisites
（含 required_mastery 门槛）/ related / skills / typical_misconceptions /
examples。它回答"下一步该学/练哪个知识点"。

选题逻辑（伴学的"科学"核心，已取证）
------------------------------------
1. **前置门控**：一个知识点只有在其所有前置的掌握度 ≥ required_mastery 时才
   可被推送（伴学 `required_mastery` 门控，knowledge_tracker.py:2066 同款思路）。
   没达标就推，是"不科学"的典型——学员在没地基的指标上打转。
2. **薄弱优先**：掌握度越低越优先；从未练过（None）视为最薄弱，优先开新坑。
3. **难度梯度**：在可选题里挑离全局目标难度最近的（伴学 `_by_gradient`）。
4. **scope 过滤**：练习范围（stage/subject/chapter）像伴学 list_topics 一样筛。

掌握度存在 `topic_mastery.jsonl`（与 TASKS 的覆盖率解耦——知识点 ≠ 考题）。
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SEED_PATH = HERE / "static" / "knowledge_seeds" / "valorant.json"
MASTERY_PATH = HERE / "topic_mastery.jsonl"

DEFAULT_REQUIRED_MASTERY = 0.5


# ---------------------------------------------------------------------------
# 掌握度存储（按知识点，独立于一题一判的覆盖率）
# ---------------------------------------------------------------------------
class MasteryStore:
    """每个知识点一行：{topic_id, mastery, attempts, correct, last_at}。

    掌握度 = 近期正确率的指数滑动平均（与伴学 mastery_v2 同思路，简单版）。
    """

    def __init__(self, path: Path = MASTERY_PATH):
        self.path = Path(path)
        self._rows: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row.get("topic_id"), str):
                self._rows[row["topic_id"]] = row

    def get(self, topic_id: str) -> dict[str, Any] | None:
        return self._rows.get(topic_id)

    def mastery_of(self, topic_id: str) -> float | None:
        row = self._rows.get(topic_id)
        return float(row["mastery"]) if row and row.get("mastery") is not None else None

    def all(self) -> dict[str, dict[str, Any]]:
        return dict(self._rows)

    def update(self, topic_id: str, *, score: float, commit: bool = True) -> dict[str, Any]:
        """score ∈ [0,1]：本次作答得分。更新滑动平均掌握度。"""
        row = self._rows.get(topic_id) or {
            "topic_id": topic_id, "mastery": None, "attempts": 0,
            "correct": 0, "last_at": 0,
        }
        row["attempts"] = int(row.get("attempts", 0)) + 1
        row["correct"] = int(row.get("correct", 0)) + (1 if score >= 0.6 else 0)
        prev = row.get("mastery")
        if prev is None:
            row["mastery"] = float(score)
        else:
            # 指数滑动平均：新样本权重 0.4
            row["mastery"] = round(prev * 0.6 + score * 0.4, 4)
        row["last_at"] = int(time.time())
        self._rows[topic_id] = row
        if commit:
            self._persist()
        return row

    def _persist(self) -> None:
        lines = [json.dumps(r, ensure_ascii=False) for r in self._rows.values()]
        self.path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


# ---------------------------------------------------------------------------
# 教学图谱
# ---------------------------------------------------------------------------
class TopicGraph:
    def __init__(self, seed_path: Path = SEED_PATH, mastery: MasteryStore | None = None):
        self.seed_path = Path(seed_path)
        self.topics: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []  # {from,to,relation,required_mastery}
        self.mastery = mastery or MasteryStore()
        self.load()

    def load(self) -> None:
        if not self.seed_path.exists():
            return
        payload = json.loads(self.seed_path.read_text(encoding="utf-8"))
        for t in payload.get("topics", []):
            self.topics[t["id"]] = t
        for t in self.topics.values():
            for pre in t.get("prerequisites", []) or []:
                req = float(pre.get("required_mastery", DEFAULT_REQUIRED_MASTERY))
                self.edges.append({
                    "from": str(pre.get("id", "")), "to": t["id"],
                    "relation": "prerequisite", "required_mastery": req,
                })
            for rel in t.get("related", []) or []:
                self.edges.append({
                    "from": str(rel.get("id", "")), "to": t["id"],
                    "relation": str(rel.get("relation", "related")),
                    "required_mastery": None,
                })

    # ---- scope / 过滤 ----
    @staticmethod
    def _scope_val(field: str, value: object) -> str:
        v = "" if value is None else str(value)
        if field in ("stage", "subject", "course_family"):
            return v.lower().replace("-", "_").replace(" ", "_")
        return v.strip()

    def scope_matches(self, topic: dict[str, Any], scope: dict[str, Any] | None) -> bool:
        if not scope:
            return True
        for field in ("stage", "subject", "course_family", "chapter", "unit"):
            expected = self._scope_val(field, scope.get(field))
            if not expected:
                continue
            if self._scope_val(field, topic.get(field)) != expected:
                return False
        return True

    def topics_in_scope(self, scope: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return [t for t in self.topics.values() if self.scope_matches(t, scope)]

    # ---- 前置门控 ----
    def prerequisites_of(self, topic_id: str) -> list[dict[str, Any]]:
        return [e for e in self.edges if e["to"] == topic_id and e["relation"] == "prerequisite"]

    def prereq_ok(self, topic: dict[str, Any]) -> bool:
        for e in self.prerequisites_of(topic["id"]):
            m = self.mastery.mastery_of(e["from"])
            if m is None or m < float(e.get("required_mastery", DEFAULT_REQUIRED_MASTERY)):
                return False
        return True

    def blocked_by(self, topic: dict[str, Any]) -> list[str]:
        """返回没达标的前置 id 列表（用于给用户解释为什么这道题还没解锁）。"""
        out = []
        for e in self.prerequisites_of(topic["id"]):
            m = self.mastery.mastery_of(e["from"])
            if m is None or m < float(e.get("required_mastery", DEFAULT_REQUIRED_MASTERY)):
                out.append(e["from"])
        return out

    # ---- 全局目标难度（照伴学 _global_target：已掌握越多，目标越高）----
    def global_target(self) -> tuple[int, str]:
        rows = self.mastery.all()
        known = [r for r in rows.values() if r.get("mastery") is not None]
        total = len(self.topics)
        if not known:
            return 1, "尚未开始任何知识点 → 目标难度 1（从地基打起）"
        mastered = sum(1 for r in known if float(r["mastery"]) >= 0.8)
        avg = sum(float(r["mastery"]) for r in known) / len(known)
        target = min(4, max(1, 1 + mastered))
        return target, (
            f"已练 {len(known)}/{total} 知识点 · 平均掌握 {avg:.0%} · "
            f"已掌握 {mastered} 个 → 目标难度 {target}"
        )

    # ---- 核心：选下一题 ----
    def select_next(
        self, *, scope: dict[str, Any] | None = None, explicit: str | None = None
    ) -> dict[str, Any]:
        """返回 {topic_id, reason, explanation, blocked, hint, difficulty_target}。

        排序键（伴学 weak-first + _by_gradient 同构）：
            (掌握度越低越前, 离目标难度越近越前, depth, id)
        """
        if explicit and explicit in self.topics:
            t = self.topics[explicit]
            return {
                "topic_id": explicit, "blocked": False, "hint": "",
                "difficulty_target": self.global_target()[0],
                "reason": "explicit", "explanation": f"已钉住知识点 {explicit}",
            }

        pool = self.topics_in_scope(scope)
        selectable = [t for t in pool if self.prereq_ok(t)]
        target, why_target = self.global_target()

        if not selectable:
            # 全被前置卡住 → 退回无前置的根知识点（绝不让用户卡死）
            roots = [t for t in pool if not self.prerequisites_of(t["id"])]
            selectable = roots or pool
            note = "（部分题前置未达标，先推无前置的根知识点）" if roots else "（范围里无可选题，退回全量）"
        else:
            note = ""

        def key(t: dict[str, Any]) -> tuple:
            m = self.mastery.mastery_of(t["id"])
            mval = -1.0 if m is None else float(m)
            diff = float(t.get("difficulty", 0.5))
            # 未练过的（mval=-1）全部排前面；其中按 depth 升序（地基优先），
            # 再按离目标难度就近；已练过的按掌握度升序（薄弱优先）。
            return (mval, int(t.get("depth", 1)), abs(diff - target), t["id"])

        chosen = min(selectable, key=key)
        cid = chosen["id"]
        m = self.mastery.mastery_of(cid)
        mtxt = "未练过" if m is None else f"{m:.0%}"
        blocked = self.blocked_by(chosen)
        return {
            "topic_id": cid,
            "reason": "weak_first" if (m is None or m < 0.8) else "review",
            "explanation": (
                f"{cid}：当前掌握 {mtxt}，难度 {chosen.get('difficulty')}，"
                f"离目标 {target} 最近{('；前置未达标：' + ','.join(blocked)) if blocked else ''}"
                f"{note}｜{why_target}"
            ),
            "blocked": bool(blocked),
            "hint": self._hint_for(chosen),
            "difficulty_target": target,
        }

    def _hint_for(self, topic: dict[str, Any]) -> str:
        """把知识点的误区/例题浓缩成给模型的提示（避免它踩已知坑）。"""
        mis = topic.get("typical_misconceptions") or []
        ex = topic.get("examples") or []
        parts = []
        if mis:
            parts.append("常见误区：" + "；".join(mis[:3]))
        if ex:
            first = ex[0]
            if isinstance(first, dict) and first.get("answer_outline"):
                parts.append("拿表回答示意：" + " → ".join(first["answer_outline"][:3]))
        return "｜".join(parts)

    # ---- 给面板用：四层结构 + 边 + 掌握度叠加 ----
    def to_graph_payload(self) -> dict[str, Any]:
        target, _ = self.global_target()
        nodes = []
        for t in self.topics.values():
            m = self.mastery.mastery_of(t["id"])
            nodes.append({
                "id": t["id"], "name": t.get("name", t["id"]),
                "stage": t.get("stage", ""), "subject": t.get("subject", ""),
                "chapter": t.get("chapter", ""), "depth": int(t.get("depth", 1)),
                "difficulty": float(t.get("difficulty", 0.5)),
                "mastery": (None if m is None else round(m, 3)),
                "blocked": bool(self.blocked_by(t)),
                "misconceptions": len(t.get("typical_misconceptions") or []),
                "examples": len(t.get("examples") or []),
            })
        edges = [{"from": e["from"], "to": e["to"], "relation": e["relation"]}
                 for e in self.edges]
        layers = defaultdict(set)
        for n in nodes:
            layers[("stage", n["stage"])].add(n["id"])
            layers[("subject", n["subject"])].add(n["id"])
            layers[("chapter", n["chapter"])].add(n["id"])
        return {
            "schema_version": 3,
            "difficulty_target": target,
            "target": target,
            "nodes": nodes, "edges": edges,
            "layers": {f"{k[0]}:{k[1]}": sorted(v) for k, v in layers.items()},
            "total": len(nodes),
        }


_GRAPH: TopicGraph | None = None


def get_graph() -> TopicGraph:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = TopicGraph()
    return _GRAPH
