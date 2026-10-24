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

环境：一个本机工作区（用户自己的文件）或一个场景自己的 SQL 库。
你要做的：根据「数据源的真实结构」+「已完成任务」+「失败过的任务及其原因」，
规划**下一个**任务（一句自然语言问题），让一个只能读文件/查本地 duckdb 的助手去完成。

硬约束（照原版：任务必须"当前可完成"）：
1. 任务必须能从数据源真取数回答（读文件、数数、排序、求和、求平均、对比……）；
   不要时效性题（最新/今年），不要数据源里没有的外部知识；
2. 已完成的任务不要再出；失败过的任务换一个更简单/不同的角度；
3. 任务要具体（点明表/文件与数据范围），要有可判定的完成标准；
4. 严格按格式输出两行（原版 parse_ai_message 只认 Task: 行）：
Task: <任务，一句话，不要句号结尾>
Context: <一句话说明：用哪个文件/表、算什么、完成标准是什么>

重新规划的规则（2026-10-23 用户实测「同一道必败题反复出」之后加）：
5. 失败过的题是**里程碑**：它值得学，但当前水平达不到。你的职责不是原样或换个
   说法再出它 —— 是规划它**前置的、现在就能做对**的子任务（一次只查一张表、
   只算一个数），学会了再逐步逼近。看「为什么失败」：缺哪块能力，就先补哪块。
6. 题里的字面量（部门名、月份、类型、人名……）必须**逐字**取自观察里列出的
   「真实取值」；清单里没有的说法不要用（写了也查不到，必然 0 行）。
7. 已学会的技能还很少时（看「已学会的技能」计数），只出**单步题**：
   一张表、一个数或一行 —— 原版 "Mine [quantity] [block]" 的同构。
8. 题目只能引用观察里 schema 实查出现的**表名和列名**；数据源里不存在的
   业务参数（费率、汇率、税率、政策比例……）**不许假设** —— 出了这种题，
   助手只能编一个数（实测 2026-10-24：编出"每小时加班费率 150 元"还入库成
   了技能）。库里没有的参数，要么不出这类题，要么把「数据源缺少 X，
   无法计算」本身作为任务。
"""


def db_observation(db_path: str) -> str:
    """实查场景库：表 → 行数 → 列 → 低基数字符串/布尔列的真实取值。

    为什么提成模块级（2026-10-24 用户定「用原 voyager 看环境的方法来抄」）：
    原版所有 agent（curriculum / action / critic）看到的是**同一份实查渲染的
    环境末态**（背包物品×数量、附近方块）—— critic 之所以能验证「挖到铁矿」，
    是因为末态清单里看得见 `iron_ore ×1`。SQL 场景的同构物就是这份清单：
    出题器拿它保证「只出观察面能验证的题」，critic 拿它指认「任务假设的
    表/列/业务参数是否真的存在」（实测 2026-10-24：模型编出"每小时加班费率
    150 元"，critic 没有环境清单可对照，把幻觉当判据放行了）。

    只用 DuckDB 标准语句，不需要任何领域知识；打不开/查不动就返回空串
    （上层如实说"实查失败"，不编造）。
    """
    out = ""
    try:
        import duckdb
        con = duckdb.connect(str(db_path or ""), read_only=True)
        try:
            tables = [r[0] for r in con.execute("show tables").fetchall()]
            lines: list[str] = []
            for t in tables:
                try:
                    cnt = con.execute(f'select count(*) from "{t}"').fetchone()[0]
                    cols = con.execute(f'describe "{t}"').fetchall()
                except Exception:
                    continue
                names = ", ".join(c[0] for c in cols)
                lines.append(f"- 表 {t}（{cnt} 行）：{names}")
                for c in cols:
                    cname, ctype = str(c[0]), str(c[1]).upper()
                    if ctype not in ("VARCHAR", "TEXT", "STRING", "BOOLEAN"):
                        continue
                    try:
                        vals = [r[0] for r in con.execute(
                            f'select distinct "{cname}" from "{t}" '
                            f'limit 13').fetchall()]
                    except Exception:
                        continue
                    vals = [v for v in vals if v is not None]
                    if vals and len(vals) <= 12:
                        lines.append(
                            f"    · {t}.{cname} 真实取值: "
                            + "、".join(str(v) for v in vals))
            out = "\n".join(lines)
        finally:
            con.close()
    except Exception:
        out = ""
    return out


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
                 db_path: str = "",
                 llm=None, state_dir: str | Path | None = None) -> None:
        self.scene_id = str(scene_id)
        self.scene_dir = str(scene_dir or "")
        # 场景自有 SQL 库（kind=scene 时非空）。2026-10-23 实测（Scene P 反复出
        # 「技术部应发工资」这道必败题）的根因之一：出题器的观察面里**没有库的
        # 真实结构** —— 它只看到"工作区有一个 payroll.duckdb 文件"，既不知道有
        # 哪些表，也不知道 dept 的真实取值是「技术」不是「技术部」。原版出题器
        # 的观察面是**完整末态**（背包/附近方块），我们至少要给它 schema + 枚举值。
        self.db_path = str(db_path or "")
        self._db_obs_cache: str | None = None
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
    def local_enabled(self) -> bool:
        """本机取数能力这一组还开着吗（= 猫爪总开关 ∧ 本机组开关）。

        原版没有那么细的开关，对应物是「环境里有没有可用的 control primitive」：
        原版 observation 里能看到 chests / nearby_blocks，是因为 agent 确实能
        采集它们。MVE 里能力关掉后模型根本调不到本机原语，再围绕本机文件出题
        就是出一道**必然失败**的题（用户 2026-10-19 实测）。
        所以「能不能读本机」必须体现在环境观察里，而不是只体现在执行层。
        """
        try:
            import actions
            actions.restore_switches()          # 子进程：读面板拨过的开关
            return bool(actions.group_enabled(self.scene_id, "本机能力"))
        except Exception:
            return True                          # 读不到就按默认开（不假装关）

    def net_enabled(self) -> bool:
        """联网检索能力这组还开着吗（组名与 actions.register_net 一致）。

        为什么课程层要关心它：**知识型学习不依赖本机文件**。工作区空 +
        本机关，只要联网还开着，Voyager 就该继续围绕人问过的主题出更深的
        子问题（Maieutic 追问式拓展）—— 用户 2026-10-19 实测纠偏：
        「本机能力关了之后课程直接退出」是错的，不停（while True）才是原版语义；
        只有**所有取数手段都关了**才谈得上"没得学"。
        """
        try:
            import actions
            actions.restore_switches()
            return bool(actions.group_enabled(self.scene_id, "联网能力"))
        except Exception:
            return True

    def render_observation(self) -> dict[str, str]:
        import local_agent
        local_ok = self.local_enabled()
        net_ok = self.net_enabled()
        # kind=scene（场景自有 duckdb）→ 观察面给**实查的表结构 + 枚举值**，
        # 不给文件清单（那只会让模型把 .duckdb 当文本文件编）。这是原版
        # "nearby_blocks 能看到附近有什么"的同构：出题器必须看得见数据长什么样。
        if self.db_path and local_ok:
            db_obs = self._db_observation()
            if db_obs:
                files = ("这个场景的数据源是一个 SQL 库，实查结构如下"
                         "（表/列/取值全部真实，题里的字面量必须逐字来自「真实取值」）：\n"
                         + db_obs)
            else:
                files = ("这个场景的数据源是一个 SQL 库，但本次实查失败（库打不开？）。"
                         "不要编造表名或列名。")
        elif self.db_path and not local_ok:
            files = ("None（本机取数能力已被关掉，SQL 库读不了 —— 不要出需要查库的题）")
        else:
            inv = (local_agent.inventory(self.scene_dir, limit=12)
                   if (self.scene_dir and local_ok) else [])
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
                files = "工作区文件:\n" + "\n".join(lines)
            elif not local_ok:
                files = ("None（本机能力已被关掉读不到文件，而且工作区里本来也没有"
                         "任何文件 —— 不要出需要读本机文件的题，也**绝对不要编造"
                         "文件名**）")
            else:
                files = ("None（这个场景的工作区里没有任何文件 —— **绝对不要编造"
                         "文件名**）")
        done = "；".join(self.completed_tasks[-20:]) if self.completed_tasks else "None"
        failed = "；".join(self.failed_tasks[-20:]) if self.failed_tasks else "None"
        wo = "；".join(f"{k[:40]}（{v[:80]}）" for k, v in list(self._failed_why.items())[-8:])
        return {
            # 原版 biome 只说「这是什么地形」（plains / forest），**不报路径**。
            # 我上一版写成 "数据源: 本机工作区 /root/mve/scene_data/D"，
            # 实测模型据此编出 room_data.txt（空目录里根本没这个文件），
            # 连出 3 道必失败的题。环境描述只给类别，不给可被脑补成文件名的路径。
            "biome": self._biome_text(local_ok, net_ok) + "\n\n",
            "nearby_blocks": f"{files}\n\n",
            "chests": f"已学会的技能（可复用配方）: {self._skill_count()} 条\n\n",
            "completed_tasks": f"Completed tasks so far: {done}\n\n",
            "failed_tasks": (
                f"Failed tasks that are too hard: {failed}\n\n"
                + (f"为什么失败: {wo}\n\n" if wo else "")
                + ("（这些失败题多半是人导入的目标，是**里程碑**。按规则 5："
                   "围绕它出**前置的、现在就能做对**的子任务，不要原样重出。）\n\n"
                   if self.failed_tasks else "")),
        }

    def _biome_text(self, local_ok: bool, net_ok: bool = True) -> str:
        if not local_ok and not net_ok:
            return "数据源: 本机取数能力与联网能力**都已被关掉** —— 这个场景现在没有任何可用的取数手段。"
        if self.db_path:
            if local_ok:
                return ("数据源: 这个场景自己的 SQL 库（只读，连接由执行层负责）。"
                        "出题范围就是下面实查列出的表和列。")
            return "数据源: 场景 SQL 库存在，但本机取数能力已被关掉，读不了。"
        if not local_ok:
            return ("数据源: 本机取数能力已被关掉；联网检索可用。"
                    "只能出由联网检索回答的知识型问题，不要碰本机文件。")
        return "数据源: 本机工作区（应用工作区 + 场景目录），只读；联网检索也可用"

    def _db_observation(self) -> str:
        """实查场景库：表 → 行数 → 列 → 低基数字符串/布尔列的真实取值。

        只用 DuckDB 标准语句，不需要任何领域知识；打不开/查不动就返回空串
        （上层如实说"实查失败"，不编造）。缓存到实例 —— 一次进程里枚举值
        不会变，不必每轮出题都连库。
        """
        if getattr(self, "_db_obs_cache", None) is None:
            self._db_obs_cache = db_observation(self.db_path)
        return self._db_obs_cache

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
        队列空 → LLM 出题（原版 auto），解析失败重试 max_retries 次。

        环境三态（2026-10-19 用户纠偏后定）：
          · 有文件 → 原版模式：围绕工作区数据出题；
          · 没文件/本机关，但联网开 → **知识型拓展模式**：围绕 Completed tasks
            里人问过的主题出更深的子问题（Maieutic 追问式），由联网检索回答
            ——「不停」是原版 while True 的本义，本机关不等于没得学；
          · 本机 + 联网**都关** → 才是"没有可学的环境"，如实返回空。
        """
        for q in (queue or []):
            q = str(q or "").strip()
            if q and q not in self.completed_tasks and q not in self.failed_tasks:
                return q, "人导入的待学问题（原版 manual 模式对应物）"
        # 原版 :264-291 在交给 LLM 之前先走硬编码分支（inventory 满 → 固定任务）。
        # MVE 的对应物就是这条：先看**有没有任何可用的取数手段**。
        # 2026-10-19 第一版这里把"工作区没文件"也当成没得学，用户实测纠偏：
        # Scene D（阿斯伯格）本机关、联网开着，点「练题」课程直接退 ——
        # 但知识型拓展根本不需要本机文件。
        if not self.local_enabled() and not self.net_enabled():
            return "", ("可学的抓手全被关掉了：本机取数能力和联网能力都处于关闭"
                        "状态。把至少一组能力打开（面板「能力开关」），或导入要学"
                        "的问题后再启动。")
        return self.propose_next_ai_task(max_retries=max_retries)

    def has_environment(self) -> bool:
        """工作区里有没有东西可探索（原版：nearby_blocks 非空才谈得上出任务）。"""
        if not self.scene_dir or not self.local_enabled():
            return False
        try:
            import local_agent
            return bool(local_agent.inventory(self.scene_dir, limit=1))
        except Exception:
            return True          # 探不动就按有环境（不因探测失败而假装没得学）

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
        # 环境模式：没文件（或本机关）时切换到知识型拓展 —— 不然模型只能对着
        # "工作区"这个词编文件名（2026-10-19 实测连出 3 道 room_data.txt）。
        mode = ""
        if not self.has_environment():
            local_ok, net_ok = self.local_enabled(), self.net_enabled()
            mode = ("\n\n本次环境模式（重要）：\n"
                    f"- 本机取数能力：{'开' if local_ok else '关'}；"
                    f"联网检索能力：{'开' if net_ok else '关'}。\n"
                    "- 工作区里**没有任何文件**。所以**禁止**出「读/计算某个文件"
                    "里的数据」这类题 —— 那种文件名都是编出来的，必然失败。\n"
                    "- 你要出的是**知识型问题**：定义/区别/机制/标准/方法/常见"
                    "误解这类，执行层会用联网检索来回答。\n")
            if self.completed_tasks:
                mode += ("- 优先围绕上面 Completed tasks 里已经回答过的主题，出"
                         "**同一主题更深入的子问题**（更细的维度，比如从「是什么」"
                         "深入到「怎么判断/怎么做/有什么争议」），不要漂到不相关"
                         "的领域。\n")
        user = (self.render_human_message() + mode
                + "\n请按系统要求输出 Task: 与 Context: 两行。")
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
                           db_path=str(prof.get("db_path") or ""),
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
