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
# Voyager 课程机制（替换/增强 MVE 出题器，2026-10-12 落地）
# ---------------------------------------------------------------------------
# 1) 放弃机制（堵 MC 161 轮死循环）：一个知识点练够 MAX_ATTEMPTS_PER_TOPIC 次
#    （初试 + 重试，设计指定 = 3）仍没跑通 → 标记 stale，select_next 强制跳过换题。
#    绝不在一个知识点上无限打转（MC 实测 161 轮不换题=从不干净失败）。
#    ⚠️ 设计硬约束：放弃判定只看「次数够不够」，不读掌握度/覆盖率
#    （评分/掌握度只进面板展示，绝不被任何推进条件读到）。
MAX_ATTEMPTS_PER_TOPIC = 3
STALE_MASTERY = 0.6   # 仅留作面板"是否达标"展示，不再参与放弃/选题判定

# 2) 跨域基线（用户指定：技能/动作锁定 VLML 10 工具/语义层，不写裸 SQL）。
#    这些知识点依赖 ext_* 表（首血 / 经济局），换到没有 ext 表的库（vlml_vlr）
#    时不可答 → 出题器把它们降权，等换到有 ext 表的库（vlml_rib）再放开。
EXT_TOPICS = {
    "vlml_first_blood", "vlml_fb_conversion", "vlml_post_plant", "vlml_economy",
    "vlml_trade", "vlml_opening_duel", "vlml_grain_trap", "vlml_sample_size",
}


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
                row.setdefault("completed", False)
                self._rows[row["topic_id"]] = row

    def get(self, topic_id: str) -> dict[str, Any] | None:
        return self._rows.get(topic_id)

    def mastery_of(self, topic_id: str) -> float | None:
        row = self._rows.get(topic_id)
        return float(row["mastery"]) if row and row.get("mastery") is not None else None

    def all(self) -> dict[str, dict[str, Any]]:
        return dict(self._rows)

    def update(self, topic_id: str, *, score: float, commit: bool = True) -> dict[str, Any]:
        """score ∈ [0,1]：本次作答得分。更新滑动平均掌握度。

        额外维护两个 Voyager 课程要用的字段：
          streak_wrong —— 连续答错次数（连错触发加支架，见 difficulty.py）；
          stale       —— 放弃标记：练够次数仍不达标 → 出题器强制换题
                        （堵 MC 161 轮死循环：从不干净失败=永不换题）。
        """
        row = self._rows.get(topic_id) or {
            "topic_id": topic_id, "mastery": None, "attempts": 0,
            "correct": 0, "last_at": 0, "streak_wrong": 0, "stale": False,
            "completed": False,
        }
        row["attempts"] = int(row.get("attempts", 0)) + 1
        row["correct"] = int(row.get("correct", 0)) + (1 if score >= 0.6 else 0)
        # 连错计数：答对清零，答错 +1（difficulty.py 的连错两题加支架读它）
        row["streak_wrong"] = 0 if score >= 0.6 else int(row.get("streak_wrong", 0)) + 1
        prev = row.get("mastery")
        if prev is None:
            row["mastery"] = float(score)
        else:
            # 指数滑动平均：新样本权重 0.4
            row["mastery"] = round(prev * 0.6 + score * 0.4, 4)
        row["last_at"] = int(time.time())
        # 放弃机制：练够 MAX_ATTEMPTS_PER_TOPIC 次 → 标记 stale（设计指定 = 3）。
        # ⚠️ 设计硬约束：放弃判定**只看次数**，不读掌握度/覆盖率——
        # 评分结果不被任何推进条件读到（决定跳过、下一题、整轮结束都不读它）。
        # "刚好最后一次蒙对"不会刷掉标记：次数到了就是到了。
        row["stale"] = bool(row["attempts"] >= MAX_ATTEMPTS_PER_TOPIC)
        self._rows[topic_id] = row
        if commit:
            self._persist()
        return row

    def is_stale(self, topic_id: str) -> bool:
        return bool((self._rows.get(topic_id) or {}).get("stale"))

    def mark_completed(self, topic_id: str, *, commit: bool = True) -> dict[str, Any]:
        """任务跑通（ev.success）→ 图谱当前节点标记完成（设计：不重复规划）。

        与 mastery（展示用）解耦：completed 是布尔「做没做过」，mastery 是
        「做得怎么样」（只进面板）。select_next 据此排除已完成的节点，不重规划。
        """
        row = self._rows.get(topic_id) or {
            "topic_id": topic_id, "mastery": None, "attempts": 0,
            "correct": 0, "last_at": 0, "streak_wrong": 0, "stale": False,
            "completed": False,
        }
        row["completed"] = True
        self._rows[topic_id] = row
        if commit:
            self._persist()
        return row

    def is_completed(self, topic_id: str) -> bool:
        return bool((self._rows.get(topic_id) or {}).get("completed"))

    def streak_of(self, topic_id: str) -> int:
        return int((self._rows.get(topic_id) or {}).get("streak_wrong", 0))

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
        """前置门控：前置节点**已完成**（任务跑通）才解锁。

        ⚠️ 设计硬约束：门控只看「前置节点是否 completed（布尔）」，
        不读掌握度/覆盖率——评分结果不被任何推进条件读到。
        有序课程（比赛→队伍→队员）由 completion 驱动，不是由分数驱动。
        """
        for e in self.prerequisites_of(topic["id"]):
            if not self.mastery.is_completed(e["from"]):
                return False
        return True

    def blocked_by(self, topic: dict[str, Any]) -> list[str]:
        """返回没完成的前置 id 列表（用于解释为什么这道题还没解锁）。"""
        out = []
        for e in self.prerequisites_of(topic["id"]):
            if not self.mastery.is_completed(e["from"]):
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

    # ---- 课程顺序（设计：有序规划课程，比赛→队伍→队员）----
    def _chapter_rank(self, topic: dict[str, Any]) -> int:
        """章节在种子里的出现顺序 = 课程顺序（不读掌握度/覆盖率）。"""
        if not hasattr(self, "_chapter_order"):
            order: dict[str, int] = {}
            n = 0
            for t in self.topics.values():
                ch = str(t.get("chapter") or "")
                if ch not in order:
                    order[ch] = n
                    n += 1
            self._chapter_order = order
        return self._chapter_order.get(str(topic.get("chapter") or ""), 999)

    # ---- 核心：选下一题（Voyager 课程驱动）----
    def select_next(
        self, *, scope: dict[str, Any] | None = None, explicit: str | None = None,
        ext_available: bool | None = None,
    ) -> dict[str, Any]:
        """返回 {topic_id, reason, explanation, blocked, hint, difficulty_target,
        novelty, stale_skipped, ext_deprioritized, exhausted}。

        ⚠️ 设计硬约束（评分/掌握度不被任何推进条件读）：
          · 选下一题 = **有序课程**（章节顺序 × 深度），**不读掌握度/覆盖率**。
          · 已完成节点（任务跑通）直接排除——「不重复规划」。
          · 放弃机制：练够 3 次仍没跑通 → stale，强制跳过换题。
          · 前置门控：前置节点 completed 才解锁（由 completion 驱动，非分数）。
          · 范围内无「可拿下」任务（全完成 / 全卡死）→ 返回 exhausted=True，
            由调用方「不断生成新任务」继续（设计：整轮不结束）。
        """
        if explicit and explicit in self.topics:
            t = self.topics[explicit]
            return {
                "topic_id": explicit, "blocked": False, "hint": "",
                "difficulty_target": self.global_target()[0],
                "reason": "explicit", "explanation": f"已钉住知识点 {explicit}",
                "novelty": 0.0, "stale_skipped": 0, "ext_deprioritized": False,
                "exhausted": False,
            }

        if ext_available is None:
            ext_available = ext_tables_present()

        pool = self.topics_in_scope(scope)

        # —— 候选 = 在范围内、未标记完成、前置已完成、未 stale ——
        stale_ids = [t["id"] for t in pool if self.mastery.is_stale(t["id"])]
        selectable = [
            t for t in pool
            if not self.mastery.is_completed(t["id"])
            and not self.mastery.is_stale(t["id"])
            and self.prereq_ok(t)
        ]

        if not selectable:
            # 范围内没有「可拿下」的任务 → 信号 exhausted，由调用方生成新任务。
            done_n = sum(1 for t in pool if self.mastery.is_completed(t["id"]))
            blocked_n = sum(
                1 for t in pool
                if not self.mastery.is_completed(t["id"])
                and not self.mastery.is_stale(t["id"])
                and not self.prereq_ok(t))
            return {
                "topic_id": "", "blocked": False, "hint": "",
                "difficulty_target": self.global_target()[0],
                "reason": "exhausted",
                "explanation": (
                    f"范围内无「可拿下」任务（完成 {done_n} / 卡死 {len(stale_ids)} "
                    f"/ 前置未达成 {blocked_n}）→ 应由调用方生成新任务"),
                "novelty": 0.0, "stale_skipped": len(stale_ids),
                "ext_deprioritized": False, "exhausted": True,
            }

        # —— 有序课程：章节顺序 × 深度（不读掌握度/覆盖率）——
        # 缺 ext 表时，依赖 ext 的知识点降权到末尾（跨域基线：技能锁定工具/
        # 语义层，裸 ext 查询在缺表库上不可答），但仍排在已完成节点之后。
        ext_blocked = {t["id"] for t in selectable
                       if (not ext_available) and t["id"] in EXT_TOPICS}

        def key(t: dict[str, Any]) -> tuple:
            ext_pen = 1 if t["id"] in ext_blocked else 0
            return (ext_pen, self._chapter_rank(t), int(t.get("depth", 1)), t["id"])

        chosen = min(selectable, key=key)
        cid = chosen["id"]
        m = self.mastery.mastery_of(cid)
        mtxt = "未练过" if m is None else f"{m:.0%}"
        blocked = self.blocked_by(chosen)
        # 新奇分仅作展示（不驱动选题）：从未练过 = 1.0，练过但掌握度低 = 中等。
        novelty = 1.0 if m is None else max(0.0, 0.6 - (m or 0))
        target, why_target = self.global_target()
        return {
            "topic_id": cid,
            "reason": "curriculum_order",
            "explanation": (
                f"{cid}：有序课程（章节 {chosen.get('chapter')} · 深度 "
                f"{chosen.get('depth')}）｜当前掌握 {mtxt}（仅展示，不驱动选题）"
                f"{('；前置未达成：' + ','.join(blocked)) if blocked else ''}"
                f"｜{why_target}"
            ),
            "blocked": bool(blocked),
            "hint": self._hint_for(chosen),
            "difficulty_target": target,
            "novelty": round(novelty, 2),
            "stale_skipped": len(stale_ids),
            "ext_deprioritized": bool(cid in ext_blocked),
            "exhausted": False,
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
        # 每个节点的技能数（设计：技能库与图谱按 topic_id 一一对应、面板同屏展示
        # 里程碑与累计技能——"同一份存储"在节点粒度落地）。
        skill_counts: dict[str, int] = {}
        try:
            import skill_store
            for s in skill_store.all_skills():
                tid = str(s.get("topic") or "")
                if tid:
                    skill_counts[tid] = skill_counts.get(tid, 0) + 1
        except Exception:
            skill_counts = {}
        nodes = []
        for t in self.topics.values():
            m = self.mastery.mastery_of(t["id"])
            nodes.append({
                "id": t["id"], "name": t.get("name", t["id"]),
                "stage": t.get("stage", ""), "subject": t.get("subject", ""),
                "chapter": t.get("chapter", ""), "depth": int(t.get("depth", 1)),
                "difficulty": float(t.get("difficulty", 0.5)),
                "mastery": (None if m is None else round(m, 3)),
                "completed": bool(self.mastery.is_completed(t["id"])),
                "blocked": bool(self.blocked_by(t)),
                "stale": bool(self.mastery.is_stale(t["id"])),
                "ext_layer": bool(t["id"] in EXT_TOPICS),
                "skill_count": int(skill_counts.get(t["id"], 0)),
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


def ext_tables_present(db_path: str | None = None) -> bool:
    """当前库有没有 ext_* 表（首血/经济局等 rib.gg 专属层）。

    这是「场景条件化」的判据：没有 ext 表时，依赖 ext 的知识点（EXT_TOPICS）
    在出题器里被降权，因为裸 ext 查询在缺表库上必然跑不出数。换到 vlml_rib
    这类带 ext 表的库就放开 —— 技能仍只描述「调哪个工具/取哪类事实」，不写
    裸 SQL，所以换库只是"该层的题有没有数据"，技能本身可迁移。
    """
    try:
        import json as _json
        import duckdb
        p = db_path
        if not p:
            cfg = HERE / "db_config.json"
            try:
                obj = _json.loads(cfg.read_text(encoding="utf-8"))
                p = (obj or {}).get("db_path") or ""
            except Exception:
                p = ""
        if not p:
            return False
        from pathlib import Path as _P
        if not _P(p).exists():
            return False
        con = duckdb.connect(str(p), read_only=True)
        try:
            tabs = [r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='main'").fetchall()]
            return any(str(t).startswith("ext_") for t in tabs)
        finally:
            con.close()
    except Exception:
        return False


def get_graph() -> TopicGraph:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = TopicGraph()
    return _GRAPH
