"""VLML 同构的课程 Agent（原版 voyager/agents/curriculum.py 的同构替换）。

原版做了什么（取证 voyager_original/voyager/agents/curriculum.py）：
  · `render_observation`  拼 MC 出生地信息：biome / time / nearby_blocks /
    other_blocks / nearby_entities / health / hunger / position / equipment /
    inventory / chests / completed_tasks / failed_tasks
  · `curriculum_observations` + `default_warmup` 做**渐进披露**（进度到几才解锁哪几项）
  · `propose_next_task`  首任务硬编码 `"Mine 1 wood log"`（种子里程碑），之后 LLM 据此提
  · `progress` = len(completed_tasks)
  · `update_exploration_progress` 成功→completed_tasks.append，失败→failed_tasks.append

本文件逐项同构到 VLML（用户指令①）：
  biome             → 数据源（rib / vlgg / DB）
  nearby_blocks     → 可见表（按进度档解锁，同构 base/ext 分层）
  other_blocks      → 其它见过的表
  nearby_entities   → 实体可填值 —— **刻意不进上下文**（那是一份答案候选清单，
                      会把重心从"把取数做通"拉到"枚举取值凑答案"），只在面板看
  inventory         → 当前实体锚点（series / team / map）
  chests            → 已学会技能（可复用 SQL 配方）
  completed_tasks   → 已解锁里程碑（图谱节点已完成）
  failed_tasks      → 失败 / 跳过（带原因）
  "Mine 1 wood log" → **VLML 种子里程碑**（树干起点、无前置）
  新增              → 里程碑树（seed / trunk / keystone）+ 随机 (表,列) 最小单位

⚠️ 设计硬约束（用户反复强调，本文件必须守住）：
  · **不拿 VLML 跑通的标准答案给 Voyager 反馈**。本文件只负责「根据环境状态提下一个
    里程碑任务、并在任务有结果后改写图谱节点状态」，成败判据来自 DB 取证
    （见 agents/critic.py），VLML 的判决/覆盖率只进面板。
  · 评分 / 掌握度**不读推进**：选题是有序课程（里程碑树 + 章节序 + 深度），不读分数。
  · 跑通 → 节点标记完成（不重复规划）；重试 ≥3 仍不跑通 → stale 跳过（带原因）。
"""

from __future__ import annotations

import os
import random
import sys
from typing import Any

# —— 让本包能 import 仓库里的 mve/（话题图 / 场景 / 技能库都是 VLML 侧已有实现）——
_MVE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "mve",
)
if _MVE_DIR not in sys.path:
    sys.path.insert(0, _MVE_DIR)


def _mve(mod: str) -> Any:
    """惰性导入 mve 模块（mve 里有些模块连库，不要在 import 期就触发）。"""
    import importlib

    return importlib.import_module(mod)


class VLMLCurriculumAgent:
    """VLML 版课程 Agent。接口对齐原版，方便上层循环照搬。"""

    def __init__(
        self,
        ckpt_dir: str = "ckpt_vlml",
        mode: str = "auto",
        scope: dict[str, Any] | None = None,
    ) -> None:
        assert mode in ("auto", "manual"), f"mode {mode} not supported"
        self.mode = mode
        self.ckpt_dir = ckpt_dir
        self.scope = scope or {}
        topic_graph = _mve("topic_graph")
        self.graph = topic_graph.TopicGraph()
        # ⚠️ 必须共用 TopicGraph 的同一个 MasteryStore 实例。
        # 实测踩过：这里另外 new 一个 MasteryStore，于是 mark_completed 写 A、
        # 而 select_next 查的是 TopicGraph 自己的 B → 永远看不到"已完成"，
        # 同一道题被反复选中（进度读 A 却显示已解锁，两处状态分裂）。
        self.mastery = self.graph.mastery

    # ---------- 同构的观察字段（对应原版 curriculum_observations） ----------
    @property
    def curriculum_observations(self) -> list[str]:
        return [
            "context",
            "data_source",      # biome
            "visible_tables",   # nearby_blocks
            "other_tables",     # other_blocks
            # ⚠️ 这里**没有** entity_values。它原本对着 MC 的 nearby_entities
            #   （"眼前有什么"），但落到 VLML 上它变成的是
            #   「series/team/map 在库里各有哪些取值」—— 那是一份**答案候选清单**。
            #   用户明确把它划到只进面板：「当前实体在库里实际存在哪些取值…
            #   不允许进反馈，因为会影响 voyager 完成任务的重心」。
            #   要看它走 scene_context.panel_only()（面板），不走这里（上下文）。
            "anchor_entities",  # inventory（只留**当前**锚点，不给候选值清单）
            "skills",           # chests
            "milestone_tree",   # 新增：MC 里程碑树同构
            "random_unit",      # 新增：出生随机最小单位 (表,列)
            "completed_milestones",  # completed_tasks
            "failures_skips",        # failed_tasks
        ]

    @property
    def default_warmup(self) -> dict[str, int]:
        """渐进披露阈值：进度到几才把该字段放进上下文（照原版 default_warmup）。"""
        return {
            "context": 3,
            "data_source": 0,
            "visible_tables": 0,
            "other_tables": 2,
            "anchor_entities": 0,
            "skills": 1,
            "milestone_tree": 0,
            "random_unit": 0,
            "completed_milestones": 0,
            "failures_skips": 0,
        }

    # ---------- 进度：同构原版 progress = len(completed_tasks) ----------
    @property
    def progress(self) -> int:
        """已解锁里程碑数（= 图谱里已完成的节点数）。"""
        try:
            return sum(
                1
                for tid in self.graph.topics
                if self.mastery.is_completed(tid)
            )
        except Exception:
            return 0

    # ---------- 观察渲染 ----------
    def render_observation(self, *, topic_id: str = "") -> dict[str, str]:
        """拼 VLML 出生地信息（MC 同构）。

        复用 mve/scene_context —— 它已经是「初始场景（数据世界）」的同构实现，
        （数据源 / 可见表 / 当前实体 / 实体可填值 / 已掌握配方 / 里程碑树 /
        随机最小单位）。这里按原版那样拆成可渐进披露的字段。
        """
        scene = _mve("scene_context")
        obs: dict[str, str] = {k: "" for k in self.curriculum_observations}

        # 里程碑树（seed / trunk / keystone / 已解锁）—— 新增，MC 里没有显式这一项
        try:
            mt = scene.milestone_tree() or {}
        except Exception:
            mt = {}
        if mt.get("trunk"):
            tree = [f"进度 {mt.get('progress', '')}"]
            tree.append(f"种子（须先拿下，对应 MC 的 Mine 1 wood log）：{mt.get('seed')}")
            tree.append("树干链：" + " → ".join(mt.get("trunk") or []))
            if mt.get("keystone"):
                tree.append(
                    f"树干最粗处（keystone，经过路径最多）：{mt['keystone']}"
                    f"（betweenness {mt.get('keystone_score')}）")
            if mt.get("completed_list"):
                tree.append("已解锁里程碑：" + ", ".join(mt["completed_list"]))
            obs["milestone_tree"] = "里程碑树：" + "\n  ".join(tree) + "\n\n"

        # 完整出生地块（数据源/可见表/实体/技能…）——原版各字段在 mve 里已整块渲染
        try:
            block = scene.scene_injection(topic_id) or ""
        except Exception:
            block = ""
        obs["context"] = block + "\n\n" if block else ""

        # chests → 已学会技能（可复用 SQL 配方）
        try:
            learned = scene.learned_skills() or []
        except Exception:
            learned = []
        if learned:
            lines = []
            for s in learned[:8]:
                code = (s.get("code") or "").strip().replace("\n", " ")
                lines.append(f"  - [{s.get('topic')}] {s.get('name')}：{code[:120]}")
            obs["skills"] = "已学会技能（可复用 SQL 配方）：\n" + "\n".join(lines) + "\n\n"
        else:
            obs["skills"] = "已学会技能：无\n\n"

        # completed_tasks → 已解锁里程碑
        try:
            unlocked = scene.unlocked_milestones() or []
        except Exception:
            unlocked = []
        obs["completed_milestones"] = (
            "已解锁里程碑：" + (", ".join(unlocked) if unlocked else "无") + "\n\n"
        )

        # failed_tasks → 失败 / 跳过（带原因）
        try:
            fails = scene.failure_history() or []
            skips = scene.skip_history() or []
        except Exception:
            fails, skips = [], []
        parts = []
        for f in fails[:5]:
            parts.append(f"  失败 {f.get('topic')}（{f.get('verdict')}）：{f.get('reason')}")
        for s in skips[:5]:
            parts.append(f"  跳过 {s.get('topic')}：{s.get('reason')}")
        obs["failures_skips"] = (
            "失败 / 跳过（带原因）：\n" + "\n".join(parts) + "\n\n"
            if parts
            else "失败 / 跳过：无\n\n"
        )

        # 随机最小单位 (表,列) —— 出生随机、可迁移到新库
        try:
            unit = scene.random_unit() or {}
        except Exception:
            unit = {}
        if unit.get("table"):
            obs["random_unit"] = (
                f"本次随机出生最小单位（表,列）：{unit['table']}."
                f"{unit.get('column') or '?'}（{unit.get('desc') or '含义/单位未登记'}）\n\n"
            )
        return obs

    def render_human_message(self, *, topic_id: str = "") -> str:
        """照原版 render_human_message：按 warmup 渐进披露拼成一段文本。"""
        obs = self.render_observation(topic_id=topic_id)
        warm = self.default_warmup
        content = ""
        for key in self.curriculum_observations:
            if self.progress >= warm.get(key, 0):
                include = True if warm.get(key, 0) == 0 else random.random() < 0.8
                if include:
                    content += obs.get(key, "")
        return content

    # ---------- 提下一个任务 ----------
    def propose_next_task(self) -> dict[str, Any]:
        """返回 {task, context, topic_id, exhausted, reason}。

        同构原版：progress==0 → 种子里程碑（对应 `"Mine 1 wood log"`）。
        之后走**有序课程**：topic_graph.select_next（里程碑树干预排序 + 章节序 + 深度，
        已完成排除、stale 跳过、前置门控）。范围内无可拿下任务 → exhausted=True，
        由调用方「不断生成新任务」（设计：整轮不结束）。
        """
        scene = _mve("scene_context")
        try:
            mt = scene.milestone_tree() or {}
        except Exception:
            mt = {}
        seed = mt.get("seed") or ""

        # ---- 外部 task 优先（同构原版 curriculum.txt:22-23 的 Failed tasks 清单）----
        # 人导入的 task 没答出来 → 它拆出的子目标就是**这一轮的课程**。
        # 顺序上有意放在种子**之前**：种子（MC 的 "Mine 1 wood log"）是 VLML 世界的
        # 出生地握手，泛化世界没有种子；而外部 task 是给定目标，必须最先被照顾。
        # 没有外部 task（或子目标全学完）时 `next_focus()` 返回 None，行为与本改造前一致。
        try:
            import task_intake
            focus = task_intake.next_focus()
        except Exception:  # pragma: no cover  模块缺失/账本损坏 → 退回原课程
            focus = None
        if focus and str(focus.get("topic_id") or ""):
            ftid = str(focus["topic_id"])
            ftopic = self.graph.topics.get(ftid) or {}
            return {
                "task": f"为外部任务补子目标 {ftid}"
                        f"（{(ftopic or {}).get('name') or ftid}）",
                "context": (f"人导入的任务：{focus.get('question') or ''}\n"
                            f"（{focus.get('why') or ''}）\n"
                            + self._topic_context(ftid)),
                "topic_id": ftid,
                "exhausted": False,
                "reason": "external_task",
            }

        if self.progress == 0 and seed:
            topic = self.graph.topics.get(seed) or {}
            return {
                "task": f"拿下种子里程碑 {seed}（{(topic or {}).get('name') or seed}）",
                "context": self._topic_context(seed),
                "topic_id": seed,
                "exhausted": False,
                "reason": "seed_milestone",
            }

        sel = self.graph.select_next(scope=self.scope or None)
        tid = str(sel.get("topic_id") or "")
        if sel.get("exhausted") or not tid:
            return {
                "task": "",
                "context": "",
                "topic_id": "",
                "exhausted": True,
                "reason": str(sel.get("reason") or "exhausted"),
            }
        topic = self.graph.topics.get(tid) or {}
        return {
            "task": f"拿下里程碑 {tid}（{(topic or {}).get('name') or tid}）",
            "context": self._topic_context(tid),
            "topic_id": tid,
            "exhausted": False,
            "reason": str(sel.get("reason") or ""),
        }

    def _topic_context(self, topic_id: str) -> str:
        """任务上下文 = 该知识点的技能要求 + 问法示例（照原版 get_task_context）。"""
        t = self.graph.topics.get(topic_id) or {}
        skills = "；".join(str(s) for s in (t.get("skills") or [])[:4])
        ex = ""
        examples = t.get("examples") or []
        if examples and isinstance(examples[0], dict):
            ex = str(examples[0].get("prompt") or "")
        return f"技能要求：{skills or '（无）'}\n问法示例：{ex or '（无）'}"

    # ---------- 任务有结果后改写图谱（同构 update_exploration_progress） ----------
    def update_exploration_progress(self, info: dict[str, Any]) -> None:
        """info = {topic_id, success, reason, skill_key, verdict}

        成功 → 节点标记完成（**解锁里程碑**，不重复规划）+ 挂技能指针。
        失败 → 记失败（带 verdict/reason）+ 记跳过（带原因）。
        原版是 append 进 completed_tasks / failed_tasks；这里落成图谱节点状态，
        正是「技能库与图谱同一份存储、每节点积累技能」。
        """
        tid = str(info.get("topic_id") or "")
        if not tid:
            return
        try:
            if info.get("success"):
                # ⚠️ source="practice"：这是**去重位**（别再规划这道题），
                #   不代表能力。照原版 Voyager（curriculum.py:339）学习时不计入
                #   —— 能力只从消费那侧长（consume.py 传 source="consume"）。
                self.mastery.mark_completed(tid, source="practice")
                skill_key = str(info.get("skill_key") or "")
                if skill_key:
                    self.mastery.add_skill(tid, skill_key)
            else:
                self.mastery.add_failure(
                    tid,
                    str(info.get("verdict") or "failed"),
                    str(info.get("reason") or "任务未跑通"),
                )
                # 跳过只在**真的放弃**时记（重试 ≥3 → stale），不是每失败一次都记。
                # 否则上下文里的「跳过（带原因）」会把一次普通失败说成已放弃，
                # 观察者分不清「还在重试」和「已经换题」。
                if self.mastery.is_stale(tid):
                    self.mastery.add_skip(
                        tid,
                        f"重试 {int((self.mastery.get(tid) or {}).get('attempts', 0))} "
                        f"次仍未跑通 → 放弃换题：{info.get('reason') or ''}",
                    )

            # 外部 task 账：**成功失败都要回写**。
            # 同构原版 `learn()`（voyager.py:372）每轮结束无条件调
            # `update_exploration_progress(info)` —— 成功 append completed_tasks，
            # 失败 append failed_tasks。这里失败不另记一份账（`mastery.attempts`
            # 就是那份账，练够次数据 stale），但**必须重算 ready_for_retry**：
            # 一条学不动的子目标不该把整条外部 task 永久钉死在 pending
            # （原版学不动就 "Skipping to next task"，从不等所有任务成功）。
            try:
                import task_intake
                task_intake.note_subtopic_result(tid, bool(info.get("success")))
            except Exception:  # pragma: no cover
                pass
        except Exception as e:  # pragma: no cover
            print(f"（课程 Agent 改写图谱失败：{type(e).__name__}: {e}）")


def _main() -> int:
    """冒烟：打印 VLML 同构的出生地观察 + 第一个任务（应 = 种子里程碑）。"""
    agent = VLMLCurriculumAgent()
    print("=== progress:", agent.progress, "===")
    print("=== 出生地观察（VLML 同构）===")
    print(agent.render_human_message()[:1600])
    nxt = agent.propose_next_task()
    print("=== 下一个任务 ===")
    print("task     :", nxt["task"])
    print("topic_id :", nxt["topic_id"])
    print("reason   :", nxt["reason"], "| exhausted:", nxt["exhausted"])
    print("context  :", nxt["context"][:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
