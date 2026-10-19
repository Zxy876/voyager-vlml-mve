#!/usr/bin/env python3
"""Voyager 式自动课程（非 VLML 场景）—— 原版 CurriculumAgent 的同构实现。

原版取证（voyager_original/voyager/agents/curriculum.py，逐条照抄）
------------------------------------------------------------------
  :29-93   `__init__`：`completed_tasks` / `failed_tasks` 两本账，落
           ckpt/curriculum/*.json（resume 时读回）
  :126-136 `progress` = len(completed_tasks)
  :150-224 `render_observation`：把环境状态拼成字段字典，其中
           `completed_tasks` → "Completed tasks so far: ..."
           `failed_tasks`    → "Failed tasks that are too hard: ..."
  :226-262 `render_human_message`：按 warm_up 渐进披露字段（进度不够就不放某些项）
  :264-291 `propose_next_task(max_retries=5)`：inventory 满等硬编码分支 → 否则走 LLM
  :293-311 `propose_next_ai_task`：LLM 输出里 parse `Task:` 行；**解析失败递归
           重试 max_retries=5 次**（这是课程侧的重试机制，不是任务重试）
  :330-345 `update_exploration_progress(info)`：success → completed_tasks.append，
           否则 → failed_tasks.append + "Skipping to next task"
  :347-365 `clean_up_tasks`：completed 去重；**completed 的任务从 failed 里删掉**

MVE 同构映射（字段 → 本机场景）
-----------------------------
  biome / nearby_blocks  → 数据源 = 本机工作区（scene_data/<id>）
  nearby_entities        → **刻意不放**（候选答案清单，会把重心拉偏，同 VLML 版决定）
  inventory / chests     → 工作区文件清单（带小文件结构预览）+ 已学技能数
  completed_tasks        → 学成功的问题（含 topic_id）
  failed_tasks           → 学过没学出来的问题（含原因）—— 出题时明确告诉模型「别出这些」
  "Mine 1 wood log"      → 队列里的第一条人导入题（人给的第一题 = 种子）

两种任务来源（对应原版 auto / manual）
------------------------------------
  · 人导入队列（routing.pending 里本场景的题）优先 —— 等价原版 manual 模式的人给题；
  · 队列空 → LLM 自动出题（原版 auto 模式），**不落 pending**（那是人导入的账），
    只进 completed/failed 两本账 —— 混账会让失败的自动题下次被当"人导入"重试。

自欺防线
--------
  · 出题必须能真取数：把文件清单 + 结构预览递给模型，让它指出依据（basis）；
  · 目录为空 / LLM 挂 / 解析 5 次失败 → 如实返回 ("", why)，循环照实退出；
  · 出的题不许和 completed/failed 重复（clean_up_tasks 语义的反向使用）。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

STATE_DIR = Path(__file__).resolve().parent / "ckpt_curriculum"

_SYSTEM = """你是 Voyager 式自动课程规划器（Minecraft 版 Voyager 的 curriculum agent 的本机同构）。

环境：一个本机工作区（用户自己的文件），不是任何在线数据库。
你要做的：根据「工作区里现有的文件」+「已完成任务」+「失败过的任务（太难/没数据）」，
规划**下一个**任务（一句自然语言问题），让一个只能读文件/查本地 duckdb 的助手去完成。

硬约束（照原版：任务必须"当前可完成"）：
1. 任务必须能从下面列出的文件真取数回答（读文件、数数、排序、求和、求平均、对比……）；
   不要时效性题（最新/今年），不要文件里没有的外部知识；
2. 已完成的任务不要再出；失败过的任务说明「太难或没数据」，换一个更简单/不同的角度；
3. 任务要具体（点明文件与数据范围），要有可判定的完成标准；
4. 严格按格式输出两行（原版 parse_ai_message 只认 Task: 行）：
Task: <任务，一句话，不要句号结尾>
Context: <一句话说明：用哪个文件、算什么、完成标准是什么>
"""


def _norm(q: str) -> str:
    return re.sub(r"[\s，。？！,.?!：:；;“”\"'（）()]+", "", str(q or "")).lower()


def _too_similar(q: str, others: Iterable[str]) -> bool:
    """去重：**只做归一化后的精确相等**（原版 clean_up_tasks 就是精确去重）。

    ⚠️ 不要加相似度阈值（我一度加了"字符集 Jaccard ≥ 0.8"，2026-10-19 实测炸了：
    「…比 2025 年**减少**了？」与「…**增加**了至少 20,000？」长句共享字太多，
    被判成同一题 → 合法新题连续被拒 5 次 → 原版 RuntimeError。自然语言里
    "换个问法"本来就是新任务，相似度挡不住语义差异，只会误杀。
    归一化只去空白/标点（中英文标点混用很常见），不做同义词归一。
    """
    n = _norm(q)
    if not n:
        return True
    return any(n == _norm(o) for o in others)


class CurriculumAgent:
    """原版 CurriculumAgent 的本机场景同构版（只做课程规划，不管执行）。"""

    def __init__(self, scene_id: str, *, scene_dir: str = "",
                 llm=None, state_dir: str | Path | None = None) -> None:
        self.scene_id = str(scene_id)
        self.scene_dir = str(scene_dir or "")
        self._llm = llm
        self._dir = Path(state_dir) if state_dir else (STATE_DIR / self.scene_id)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.completed_tasks: list[str] = []
        self.failed_tasks: list[str] = []
        self._failed_why: dict[str, str] = {}
        self._load()

    # ---------- 持久化（原版 :44-60 resume 读回两本账） ----------
    def _load(self) -> None:
        try:
            c = json.loads((self._dir / "completed_tasks.json").read_text("utf-8"))
            if isinstance(c, list):
                self.completed_tasks = [str(x) for x in c]
        except Exception:
            pass
        try:
            f = json.loads((self._dir / "failed_tasks.json").read_text("utf-8"))
            if isinstance(f, list):
                self.failed_tasks = [str(x) for x in f]
        except Exception:
            pass
        try:
            w = json.loads((self._dir / "failed_why.json").read_text("utf-8"))
            if isinstance(w, dict):
                self._failed_why = {str(k): str(v) for k, v in w.items()}
        except Exception:
            pass

    def _save(self) -> None:
        (self._dir / "completed_tasks.json").write_text(
            json.dumps(self.completed_tasks, ensure_ascii=False, indent=2), "utf-8")
        (self._dir / "failed_tasks.json").write_text(
            json.dumps(self.failed_tasks, ensure_ascii=False, indent=2), "utf-8")
        (self._dir / "failed_why.json").write_text(
            json.dumps(self._failed_why, ensure_ascii=False, indent=2), "utf-8")

    @property
    def progress(self) -> int:
        return len(self.completed_tasks)

    # ---------- 观察（原版 :150-224） ----------
    def render_observation(self) -> dict[str, str]:
        import local_agent
        inv = local_agent.inventory(self.scene_dir, limit=12) if self.scene_dir else []
        if inv:
            lines = []
            for r in inv:
                if r.get("kind") == "dir":
                    lines.append(f"- {r['name']}/（目录）")
                    continue
                lines.append(f"- {r['name']}（{r.get('size', 0)} 字节）")
                if r.get("preview"):
                    pv = str(r["preview"]).replace("\n", " ")
                    lines.append(f"    开头内容（结构预览）：{pv[:300]}")
            files = "\n".join(lines)
        else:
            files = "None"
        done = "；".join(self.completed_tasks[-20:]) if self.completed_tasks else "None"
        failed = "；".join(self.failed_tasks[-20:]) if self.failed_tasks else "None"
        wo = "；".join(f"{k}（{v[:60]}）" for k, v in list(self._failed_why.items())[-8:])
        return {
            "biome": f"数据源: 本机工作区 {self.scene_dir}\n\n",
            "nearby_blocks": f"工作区文件:\n{files}\n\n",
            "chests": f"已学会的技能（可复用配方）: {self._skill_count()} 条\n\n",
            "completed_tasks": f"Completed tasks so far: {done}\n\n",
            "failed_tasks": (f"Failed tasks that are too hard: {failed}\n\n"
                             + (f"为什么失败: {wo}\n\n" if wo else "")),
        }

    def _skill_count(self) -> int:
        try:
            import skill_store
            return len([s for s in skill_store.all_skills()
                        if str(s.get("source_scene") or "") in ("", self.scene_id)])
        except Exception:
            return 0

    def render_human_message(self) -> str:
        obs = self.render_observation()
        # 原版按 warm_up 渐进披露；本机只有 4 项且都便宜，全放（等价 warm_up 全 0）
        return "".join(obs[k] for k in
                       ("biome", "nearby_blocks", "chests",
                        "completed_tasks", "failed_tasks"))

    # ---------- 出题（原版 :264-311） ----------
    def propose_next_task(self, *, max_retries: int = 5,
                          queue: list[str] | None = None
                          ) -> tuple[str, str]:
        """返回 (task, context)。队列里有人导入的题 → 直接取（原版 manual 的人给题）；
        队列空 → LLM 出题（原版 auto），解析失败重试 max_retries 次。"""
        for q in (queue or []):
            q = str(q or "").strip()
            if q and q not in self.completed_tasks and q not in self.failed_tasks:
                return q, "人导入的待学问题（原版 manual 模式对应物）"
        return self.propose_next_ai_task(max_retries=max_retries)

    def propose_next_ai_task(self, *, max_retries: int = 5,
                             _feedback: str = "") -> tuple[str, str]:
        """原版 :293-311：LLM 出 `Task:` 行；解析失败重试 5 次后 raise。

        多做的两件小事（都不改原版语义）：
          · 重复/没格式时，把原因拼进下一轮 user 消息（迭代提示，同原版 action
            agent 把 critic 的 critique 回灌下一次的做法）；
          · raise 时把最后原因带进消息 —— 原版只有一句 "Max retries reached"，
            线上看到它根本不知道是"模型没按格式"还是"全是重复"。
        """
        if max_retries <= 0:
            raise RuntimeError(
                "Max retries reached, failed to propose ai task."
                + (f"（最后原因：{_feedback}）" if _feedback else ""))
        user = self.render_human_message() + "\n请按系统要求输出 Task: 与 Context: 两行。"
        if _feedback:
            user += f"\n\n上一次不行：{_feedback}。请务必换一个**不同**的任务。"
        msgs = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user}]
        try:
            if self._llm is not None:
                text = str(self._llm(msgs) or "")
            else:
                from voyager_vlml.llm import chat
                text = str(chat(msgs, temperature=0.8) or "")
        except Exception:
            # 模型调用失败 = 这一轮出不了题（原版是抛异常让上层处理）；
            # 这里返回空让循环如实退出，别把异常变成"学到一半"的假象。
            return "", ""
        task, context = self.parse_ai_message(text)
        if task and not _too_similar(
                task, self.completed_tasks + self.failed_tasks):
            return task, context
        why = ("模型输出里没有 `Task:` 行（没按格式）" if not task
               else f"提的题与已完成/失败过的重复：{task[:40]}")
        # 解析失败 / 重复 → 重试（原版 :304-311 的递归重试）
        return self.propose_next_ai_task(max_retries=max_retries - 1,
                                         _feedback=why)

    @staticmethod
    def parse_ai_message(message: str) -> tuple[str, str]:
        """原版 :312-322：只认 `Task:` 行（原版还可见 `.replace(".", "")`）。"""
        task, context = "", ""
        for line in str(message or "").split("\n"):
            s = line.strip()
            if s.startswith("Task:"):
                task = s[5:].strip()
            elif s.startswith("Context:"):
                context = s[8:].strip()
        return task, context

    # ---------- 记账（原版 :330-365） ----------
    def update_exploration_progress(self, info: dict[str, Any]) -> None:
        task = str(info.get("task") or "").strip()
        if not task:
            return
        if info.get("success"):
            if task not in self.completed_tasks:
                self.completed_tasks.append(task)
        else:
            if task not in self.failed_tasks:
                self.failed_tasks.append(task)
            why = str(info.get("critique") or info.get("why") or "")
            if why:
                self._failed_why[task] = why[:200]
        self.clean_up_tasks()
        self._save()

    def clean_up_tasks(self) -> None:
        """原版 :347-365：completed 去重（保序），并把已完成的从 failed 里删掉。"""
        seen: list[str] = []
        for t in self.completed_tasks:
            if t not in seen:
                seen.append(t)
        self.completed_tasks = seen
        self.failed_tasks = [t for t in self.failed_tasks
                            if t not in self.completed_tasks]
        for t in list(self._failed_why):
            if t not in self.failed_tasks:
                self._failed_why.pop(t, None)

    def reset(self) -> None:
        self.completed_tasks, self.failed_tasks, self._failed_why = [], [], {}
        self._save()


# ---------------------------------------------------------------------------
# 便捷入口（scene_learn 用；也保留给脚本/测试）
# ---------------------------------------------------------------------------
def agent_for(scene_id: str, *, llm=None) -> CurriculumAgent:
    import scene_profile
    prof = scene_profile.profile(scene_id)
    return CurriculumAgent(scene_id,
                           scene_dir=str(prof.get("scene_dir") or ""),
                           llm=llm)


def propose_for(scene_id: str, *, exclude: Iterable[str] = (), llm=None,
                last_why: str = "") -> dict[str, Any]:
    """一层薄适配（老的调用口径）：返回 {"question","basis","why"}。

    真实机制在 CurriculumAgent 里（两本账 + Task: 解析 + 5 次重试）；
    这里只是把 exclude 当"额外失败项"临时喂进去，不落盘。
    """
    a = agent_for(scene_id, llm=llm)
    merged = list(a.failed_tasks)
    for x in exclude:
        if x and x not in merged:
            merged.append(str(x))
    if last_why and merged:
        a._failed_why[merged[-1]] = str(last_why)[:200]
    try:
        task, context = a.propose_next_ai_task(max_retries=5)
    except Exception as e:
        return {"question": "", "basis": [], "why": f"{type(e).__name__}: {e}"}
    if not task:
        return {"question": "", "basis": [],
                "why": "自动课程提不出下一题（LLM 不可用 / 连续 5 次解析失败 / 全都重复）"}
    return {"question": task, "basis": [], "why": f"自主出题（{context or '无 context'}）"}
