"""VLML 同构的 action（对应原版 voyager/agents/action.py + voyager.py 的 step/rollout）。

原版：模型照 control_primitives/*.js 写 **JS 代码** → mineflayer 执行 → 观察原样回灌。
这里：模型照 schema 写 **SQL** → VLML MCP 执行 → 观察原样回灌（行列）。

⚠️ 本文件是原版循环的**移植**，不是改写。逐段对应：

    voyager_original/voyager/voyager.py:176-209  reset()
    voyager_original/voyager/voyager.py:214-296  step()
    voyager_original/voyager/voyager.py:298-304  rollout()
    voyager_original/voyager/agents/action.py:78-103   render_system_message
    voyager_original/voyager/agents/action.py:105-202  render_human_message
    voyager_original/voyager/agents/action.py:204-…    process_ai_message

只有三处替换，控制流一行不多：

    JS 代码          → SQL
    env.step(code)   → env.run_sql(sql)
    背包/位置/方块    → 行列/错误

没有额外的分类器、没有额外的 prompt 段落、没有额外的重试规则 ——
原版怎么转，这里就怎么转。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from voyager_vlml.control_primitives import render_primitives  # noqa: E402
from voyager_vlml.env.vlml_mcp import VLMLMCPEnv  # noqa: E402


def _extract_sql(text: str) -> str:
    """从模型输出里抠出 SELECT。

    模型可能回三种形态，都要接住（实测只按 JSON 解析会漏掉裸 SQL，导致
    "模型没给出 SELECT" 空转三轮）：
      1. JSON      {"sql": "SELECT ..."}
      2. 代码围栏  ```sql SELECT ... ```
      3. 裸文本    SELECT ...
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    # 1) JSON（含 ```json 围栏）
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    blob = m.group(1) if m else raw
    s, e = blob.find("{"), blob.rfind("}")
    if s >= 0 and e > s:
        try:
            obj = json.loads(blob[s : e + 1])
            if isinstance(obj, dict) and obj.get("sql"):
                return str(obj["sql"]).strip()
        except json.JSONDecodeError:
            pass
    # 2) ```sql 围栏
    m = re.search(r"```(?:sql)?\s*(SELECT[\s\S]*?)```", raw, re.I)
    if m:
        return m.group(1).strip()
    # 3) 裸 SELECT
    m = re.search(r"(SELECT[\s\S]+?)(?:;|$)", raw, re.I)
    if m:
        return m.group(1).strip()
    return ""


def _render_rows(data: Any, max_rows: int = 3, max_cols: int = 12) -> str:
    """把一次执行的返回渲染成"观察"文本（列名 + 前几行）。

    同构原版 `render_human_message` 里渲染 `inventory` / `voxels` / `position`
    的那几段：模型必须看见**自己这一步到底返回了什么**，否则只能靠记忆重写。

    兼容几种返回形态：{columns,rows} / 列表字典 / 空集 / 报告型 dict。
    """
    if data is None:
        return "None"
    rows: list[Any] = []
    cols: list[str] = []
    saw_rows_key = False
    if isinstance(data, dict):
        for k in ("columns", "cols", "column_names"):
            v = data.get(k)
            if isinstance(v, list) and v and isinstance(v[0], str):
                cols = [str(x) for x in v]
                break
        for k in ("rows", "data", "results", "records", "items"):
            v = data.get(k)
            if isinstance(v, list):
                rows = v
                saw_rows_key = True
                break
        if not saw_rows_key:
            rows = [data]
    elif isinstance(data, list):
        rows = data
    else:
        rows = [data]
    if not rows:
        return "（0 行 / 空集）"
    out: list[str] = []
    head = rows[0]
    if isinstance(head, dict):
        names = cols or [str(k) for k in head.keys()]
        out.append("列：" + "、".join(names[:max_cols]))
        for r in rows[:max_rows]:
            if isinstance(r, dict):
                out.append("行：" + "，".join(
                    f"{k}={str(v)[:24]}" for k, v in list(r.items())[:max_cols]))
            else:
                out.append(f"行：{str(r)[:200]}")
    elif isinstance(head, (list, tuple)):
        if cols:
            out.append("列：" + "、".join(cols[:max_cols]))
        for r in rows[:max_rows]:
            seq = list(r) if isinstance(r, (list, tuple)) else [r]
            if cols and len(cols) >= len(seq):
                out.append("行：" + "，".join(
                    f"{cols[i]}={str(v)[:24]}" for i, v in enumerate(seq[:max_cols])))
            else:
                out.append("行：" + "，".join(str(v)[:24] for v in seq[:max_cols]))
    else:
        out.append("行：" + str(head)[:200])
    if len(rows) > max_rows:
        out.append(f"（共 {len(rows)} 行，只列前 {max_rows} 行）")
    return "\n".join(out)


def _render_grain(g: dict[str, Any]) -> str:
    """把行放大实测渲染成一句话（给模型看，也给 critic 看）。"""
    n, d = g.get("rows"), g.get("distinct")
    if n is None or d is None:
        return ""
    if d and int(n) > int(d):
        ratio = g.get("ratio") or round(int(n) / int(d), 2)
        return (f"Grain check: JOIN 之后 {n} 行，驱动表 `{g.get('driver')}` 只有 {d} 行不同"
                f"（放大 {ratio}×）—— 关联键没有唯一定位右表，行被乘起来了。")
    return f"Grain check: JOIN 之后 {n} 行 = 驱动表 `{g.get('driver')}` 的 {d} 行（无放大）。"


class VLMLActionAgent:
    """原版的 action agent + 主循环，环境换成 SQL。

    用法与原版一致：
        await agent.reset(task=..., context=...)   # 出第一版两条消息
        while True:
            events, done = await agent.step()      # 写码→执行→判→重建消息
            if done: break
    或者直接 `await agent.rollout(task=..., context=...)`。
    """

    def __init__(
        self,
        env: VLMLMCPEnv | None = None,
        *,
        task_max_retries: int = 4,      # 同构原版 voyager.py:30 的默认 4
        temperature: float = 0.3,       # 原版 action_agent_temperature=0，这里留一点抖动
    ) -> None:
        self.env = env or VLMLMCPEnv()
        self.task_max_retries = max(1, int(task_max_retries))
        self.temperature = temperature
        self.critic = None              # 懒加载，避免循环 import

        self.messages: list[dict[str, str]] = []
        self.rollout_num_iter = -1      # 原版用 -1 表示"还没 reset"（voyager.py:215）
        self.task = ""
        self.context = ""
        self.topic_id = ""
        self.hints: list[str] = []
        self.schema = ""
        # 原版有 recorder/self.last_events 存历史；这里同样存着，供上层记账。
        self.observations: list[dict[str, Any]] = []
        self.last_sql = ""
        # critic 判成功时给出的结论句（原版 JSON 里的 reasoning 字段）——
        # 上层拿它当 answer，而不是把 SQL 当答案（见 task_intake._answer_once）。
        self.critic_reasoning = ""
        self.answer = ""
        self.last_success = False      # 最后一轮的判决（critic 两级都过）
        self.last_ok_sql = ""          # 最后一次「可执行、未乘爆、取到真数」的 SQL
        self.last_ok_data = None       # 与之配对的 data（见 reset() 注释）
        # 执行层的工具可达性：本场景抓手条目 + 本题候选表（见 render_grabs）。
        self.grabs: list[dict[str, Any]] = []
        self.focus_tables: list[str] = []
        # ---- 步级闸门（候选A，2026-10-26 用户拍板）----
        # 原版 Voyager 的追问全部在 rollout 内（critic 每步读环境判，critique
        # 回灌下一步，voyager.py:262-268）；原版 Maieutic 的 examiner 也是
        # 一步一判（spec/route.ts:82-100，判词当轮进下一轮输入）。MVE 曾经
        # 把闸门放在 rollout **之后**判一次（追问要等下一轮重跑才注入），
        # 4 步全用在没人告诉它清单缺口的事上。现在闸门进 step()：
        # gate_check 回调由学习循环注入（带当前维度 + env_obs），返回
        # {"passed","questions","origin",...}；origin="failed" = 闸门不可用，
        # 不阻塞（退化成内层 critic 单独判，它自带 env_obs 口径判据）。
        self.gate_check: Callable[["VLMLActionAgent"], dict[str, Any]] | None = None
        self.last_gate_passed: bool | None = None   # None=没跑/不可用
        self.last_gate_critique = ""                # 闸门追问（≤3 条合并）
        self.last_gate_result: dict[str, Any] = {}  # 闸门原始返回（留痕）
        self._gate_asked: list[str] = []            # 本 rollout 已问（防重复）

    # ---------- 执行 ----------
    async def run_sql(self, sql: str) -> dict[str, Any]:
        """执行一条 SQL，回一份可枚举观察（等价原版 env.step）。"""
        return await self.env.run_sql(sql)

    async def observe_grain(self, sql: str) -> dict[str, Any] | None:
        """行放大实测（只在有 JOIN 时做）。环境不支持就返回 None。"""
        fn = getattr(self.env, "check_grain", None)
        if fn is None or "join" not in sql.lower():
            return None
        try:
            return await fn(sql)
        except Exception:
            return None

    def _fanout_rejected(self, sql: str) -> bool:
        """确定性 fan-out 闸（与 `critic_check` 同口径，只回 bool）：JOIN 把驱动表
        行放大且 SQL 在做聚合 → 父表数值按子表行数翻倍，答案必错。

        抽出来单独给 step() 记录「最后一次未乘爆的成功尝试」用，不重复 `critic_check`
        的 LLM 判官部分 —— 消费侧的核心正确性只靠这把确定性闸，不靠 LLM。
        """
        _dbp = (str(getattr(self.env, "db_path", "") or "")
                or str(getattr(self.env, "path", "") or ""))
        if not _dbp or "join" not in (sql or "").lower():
            return False
        try:
            from voyager_vlml.env.vlml_mcp import check_sql_grain, _has_aggregation
            _fg = check_sql_grain(sql, _dbp)
            if not _fg:
                return False
            return bool(_has_aggregation(sql))
        except Exception:
            return False

    async def run_calls(self, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for c in calls:
            out.append(await self.env.call(str(c.get("name")), **(c.get("args") or {})))
        return out

    # ---------- 抓手条目渲染（主库 feature 分支的增量，2026-10-19 实测得来） ----------
    @staticmethod
    def render_grabs(grabs: list[dict[str, Any]] | None,
                     focus_tables: list[str] | None = None) -> str:
        """把当前场景的**抓手条目**渲染成「可用工具清单」（同构 MCP tools/list）。

        为什么要这一块（2026-10-19 服务器实测踩的）：
          学习环第 ② 步 forge 出来的条目，描述里嵌着**实查到的真实表名与列名**
          （见 `mve/plugin_forge.py` 的条目 description）。但执行层只拿到了
          schema 和技能库 —— 抓手条目对写 SQL 的模型**不可见**，于是它凭想象
          join（实测写成 `rounds.round_id = ext_round_economy.game_id`，
          而那张表根本没有 round_id 列）→ Binder Error → 3 轮都没收敛 →
          闭环停在 no_data。工具可达 ≠ 注册表里有条目，得让模型**看见**它。

        `focus_tables` = 本题子目标匹配出来的候选表（`task_decompose.
        related_tables` 实查）。同一轮实测：即便给了抓手，模型仍会挑一张名字
        最眼熟的表（问「下包之后翻盘」去查 games，还编了个 series_id 字面量），
        所以把候选表单独点名。
        """
        items = [g for g in (grabs or []) if str(g.get("name") or "").strip()]
        ft = [t for t in (focus_tables or []) if str(t).strip()]
        if not items and not ft:
            return ""
        out = ""
        if ft:
            out += ("\n\n【本题候选表（按子目标与真实表名实查匹配）】\n  "
                    + "、".join(ft[:10])
                    + "\n回答本题**优先**用这些表；用别的表要能说清为什么。")
        if items:
            lines = []
            for g in items[:24]:
                desc = str(g.get("description") or "").strip().replace("\n", " ")
                lines.append(f"  - {g['name']}: {desc[:220]}")
            out += (
                "\n\n【本场景可用的抓手条目（注册表实查，只能按它们给的表/列取数）】\n"
                + "\n".join(lines)
            )
        out += ("\n写 SQL 的硬规矩：join 只用上面**列名里真实出现**的键；"
                "不确定某张表有哪些列就先 `SELECT * FROM 表 LIMIT 1` 看一行；"
                "不要假设表里有 round_id / id 这类想当然的列；"
                "**不要编造队伍名/赛事号这类字面量**——题目没给的就不筛它。")
        return out

    # ---------- 同构 agents/action.py:78-103 render_system_message ----------
    def render_system_message(
        self, *, skills: list[dict[str, Any]] | None = None
    ) -> str:
        """system = 原语 + 库结构 + 已学会技能（+ 本场景抓手条目）。

        原版 `action.py:94` 是 `load_control_primitives_context(...) + skills`，
        一句解释都没有。这里同样不加解释段。

        抓手条目由 `reset(grabs=..., focus_tables=...)` 存进实例，
        每步重建 system 时一并带上（同原版 tools/list 在整个 rollout 里恒可见）。
        """
        base = (
            f"你是在 {self._data_source_label()} 里取数的分析 agent。"
            "用下面的原语写 **SQL** 完成任务。"
            "\n\n"
            + render_primitives(available=self._available_tools())
        )
        base += self.render_grabs(
            getattr(self, "grabs", None), getattr(self, "focus_tables", None))
        if self.schema:
            base += (
                "\n\n【当前库真实 schema（只能用这里的表名/列名，不要编造）】\n"
                f"{self.schema}\n"
            )
        # 跨一对多聚合的通用正确性规则（非场景硬编码，所有数据域都适用）：
        # 父表一行对应子表多行时，绝不能直接「父 JOIN 子」再 SUM——那会把父表
        # 数值按子表行数翻倍（乘爆，必错）。这与下方 fan-out 闸门互为表里。
        base += (
            "\n\n【跨一对多关系聚合的硬规矩】当父表的一行对应子表多行（一对多"
            "关系），**绝不能直接 `父 JOIN 子表` 后做 SUM/AVG**——"
            "那样父表的数值会被子表行数重复乘，答案必错（fan-out 闸门会直接拒用）。"
            "正确写法二选一：① 先把子表按关联键 `GROUP BY` 聚合成单行，再"
            " `LEFT JOIN` 到父表；② 改用标量子查询 "
            "`SUM(父表.数值列 + (SELECT COALESCE(SUM(子表.数值列),0) FROM 子表 "
            "WHERE 子表.关联键 = 父表.关联键))`。"
            "⚠️ 无论哪种写法，父表自身也要被汇总的列**必须用聚合函数包住**"
            "（如 `SUM(父表.数值列)`），否则要整句按父表关联键 `GROUP BY`，"
            "否则数据库会报 `column must appear in GROUP BY` 错误。"
        )
        blocks: list[str] = []
        for i, s in enumerate(list(skills or [])[:5]):
            code = str(s.get("code") or "").strip()
            name = str(s.get("name") or f"skill_{i+1}")
            if code:
                blocks.append(
                    f"/* === 已学技能 {i+1}：{name} ===\n"
                    f"   说明：{str(s.get('text') or '')[:160]}\n"
                    f"{code[:1200]}\n"
                    f"=== 结束 {name} === */"
                )
        if blocks:
            base += (
                "\n\n【已学会的技能（可复用，按名引用其逻辑）】\n"
                + "\n\n".join(blocks)
                + "\n\n复用规则：若题目要求跨多张源表合计（合成题），必须把上面多条技能的逻辑**组合**进"
                  "同一条查询——分别取各源表的测度再相加/汇总，"
                  "严禁只取其中一条技能就作答（那样会漏维度、给出片面数）。"
            )
        return base

    # ---------- 同构 agents/action.py:105-202 render_human_message ----------
    def render_human_message(
        self, *, events: list[dict[str, Any]], code: str = "",
        task: str = "", context: str = "", critique: str = ""
    ) -> str:
        """human = 上一轮代码 + 执行错误 + 观察 + 任务 + 上下文 + 批评。

        字段顺序照 `action.py:133-200`：Code from the last round → Execution error
        → 观察 → Task → Context → Critique。
        """
        observation = ""
        if code:
            observation += f"Code from the last round:\n{code}\n\n"
        else:
            observation += "Code from the last round: No code in the first round\n\n"

        error_messages = [
            str(e.get("error") or "") for e in events if not e.get("ok")
        ]
        if error_messages:
            observation += "Execution error:\n" + "\n".join(error_messages) + "\n\n"
        else:
            observation += "Execution error: No error\n\n"

        ok_events = [e for e in events if e.get("ok")]
        if ok_events:
            for e in ok_events:
                observation += (
                    f"Observation from {e.get('tool')} "
                    f"({e.get('rows')} rows):\n{_render_rows(e.get('data'))}\n\n"
                )
                g = _render_grain(e.get("grain") or {})
                if g:
                    observation += g + "\n\n"
        else:
            observation += "Observation: None\n\n"

        observation += f"Task: {task}\n\n"
        observation += f"Context: {context}\n\n" if context else "Context: None\n\n"
        observation += f"Critique: {critique}\n\n" if critique else "Critique: None\n\n"
        return observation

    # ---------- 同构 agents/action.py:204-… process_ai_message ----------
    def process_ai_message(self, message: str) -> str:
        """原版用 babel 解析 JS 抽 program_code；这里从模型输出里抽一条 SELECT。"""
        sql = _extract_sql(message)
        return sql if sql.lower().startswith("select") else ""

    def llm(self, messages: list[dict[str, str]]) -> str:
        """同构原版 `self.action_agent.llm(self.messages)`（voyager.py:217）。"""
        from voyager_vlml.llm import chat

        return chat(messages, temperature=self.temperature)

    # ---------- 同构 voyager.py:195 / :256 skill_manager.retrieve_skills ----------
    def retrieve_skills(self, *, query: str) -> list[dict[str, Any]]:
        try:
            import skill_store

            if not (skill_store._load_raw().get("skills") or {}):
                return []
            got = skill_store.retrieve(
                self.topic_id, query, top_k=skill_store.RETRIEVAL_TOP_K)
            keys = [str(s.get("key") or "") for s in got]
            if keys:
                skill_store.mark_used(keys)
            return got
        except Exception as e:  # pragma: no cover
            print(f"（检索技能库失败：{type(e).__name__}: {e}）")
            return []

    # ---------- 同构 agents/action.py 的 summarize_chatlog ----------
    @staticmethod
    def summarize_chatlog(events: list[dict[str, Any]]) -> str:
        """原版把 chat log 摘要成一句；这里摘要"本轮执行成了什么"。"""
        lines: list[str] = []
        for e in events:
            if e.get("ok"):
                lines.append(f"{e.get('tool')} 取到 {e.get('rows')} 行")
            else:
                lines.append(f"{e.get('tool')} 失败：{str(e.get('error') or '')[:120]}")
        return "；".join(lines) or "无执行"

    # ---------- 同构 voyager.py:176-209 reset ----------
    async def reset(
        self, *, task: str, context: str = "",
        skills: list[dict[str, Any]] | None = None, topic_id: str = "",
        hints: list[str] | None = None,
        grabs: list[dict[str, Any]] | None = None,
        focus_tables: list[str] | None = None,
        gate_check: Callable[["VLMLActionAgent"], dict[str, Any]] | None = None,
    ) -> None:
        self.rollout_num_iter = 0
        self.task = task
        self.context = context
        self.topic_id = topic_id
        self.hints = list(hints or [])
        self.grabs = list(grabs or [])
        self.focus_tables = [str(t) for t in (focus_tables or []) if str(t).strip()]
        self.observations = []
        self.last_sql = ""
        self.critic_reasoning = ""
        self.answer = ""
        self.last_success = False
        # 最后一次「可执行、未乘爆、取到真数」的尝试 (SQL, data) —— 消费/学习侧
        # 取数一律用这对，杜绝 data 来自中间某次、code 却是另一次收敛结果的对不上
        # （如 67245 数据 vs 58145 干净 SQL）。观测本身没存产生它的 SQL，必须从
        # 这里配对取，不能拆开拼（见 step() 与 consume.py ③b）。
        self.last_ok_sql = ""
        self.last_ok_data = None
        # 步级闸门状态随 rollout 重置（每个维度一次 rollout 一份账）
        self.gate_check = gate_check
        self.last_gate_passed = None
        self.last_gate_critique = ""
        self.last_gate_result = {}
        self._gate_asked = []
        try:
            self.schema = await self.env.schema_block()
        except Exception:
            self.schema = ""

        # voyager.py:195 —— 开跑前先检索一次技能
        init_skills = skills if skills is not None else self.retrieve_skills(query=context)
        print(f"[Action Agent] 用 {len(list(init_skills or []))} 条已学会配方渲染 system 消息")
        print(f"[Action Agent] 任务: {task[:140]}")

        system_message = self.render_system_message(skills=list(init_skills or []))
        human_message = self.render_human_message(
            events=[], code="", task=task, context=context, critique="")
        self.messages = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": human_message},
        ]

    # ---------- 同构 voyager.py:214-296 step ----------
    async def step(self) -> tuple[list[dict[str, Any]], bool]:
        if self.rollout_num_iter < 0:
            raise ValueError("Agent must be reset before stepping")

        ai_message = self.llm(self.messages)                       # :217
        sql = self.process_ai_message(ai_message)                  # :222
        events: list[dict[str, Any]] = []
        success = False

        if sql:
            self.last_sql = sql
            obs = await self.run_sql(sql)                          # :226 env.step
            if obs.get("ok"):
                grain = await self.observe_grain(sql)
                if grain:
                    # 把「行数有没有被 JOIN 乘起来」并进观察 —— 原版 critic 的观察里，
                    # 末态本身就是答案面（背包里几个木头一目了然）；SQL 世界的对应
                    # 证据只能实测，不给就判不出来（见 vlml_mcp.check_grain）。
                    obs = dict(obs, grain=grain)
            events = [obs]
            self.observations.extend(events)                       # :230 recorder.record
            # 跟踪「最后一次可执行、未乘爆、取到真数」的尝试 —— 消费/学习侧取数
            # 一律用这对 (sql, data)，而不是把 real[-1] 的数据与 last_sql 拆开拼
            # （二者可能来自不同轮次 → 数 SQL 对不上，如 67245 数据 vs 58145 SQL）。
            # 用确定性 fan-out 闸挡乘爆；乘爆的尝试即便 run_sql 成功也不配进来。
            if obs.get("ok") and obs.get("data") is not None \
                    and not self._fanout_rejected(sql):
                self.last_ok_sql = sql
                self.last_ok_data = obs.get("data")
            success, critique = self.critic_check(events)          # :232-238
            # ---- 步级闸门（候选A）：内层判「真取到数 + 口径对」之后，闸门
            # （Maieutic examiner 同构）核「这条维度的清单答到了没有」——
            # 原版 spec/route.ts:82-100 就是一步一判、判词当轮进下一轮输入。
            # 只有内层过（有答案可核）才跑：0 行/真空/语法错时没有可核的
            # 提交物，跑了也是空核（同构「学生交了 spec 才判」）。
            # 闸门不可用（origin=failed）不阻塞：退化成内层 critic 单独判
            # （它自带 env_obs 口径判据，action.py 的 critic_check 里）。
            if success and self.gate_check is not None:
                try:
                    _g = self.gate_check(self) or {}
                except Exception as _ge:
                    _g = {"passed": True, "questions": [], "origin": "failed",
                          "reason": f"{type(_ge).__name__}: {_ge}"}
                self.last_gate_result = dict(_g) if isinstance(_g, dict) else {}
                if str(self.last_gate_result.get("origin") or "") == "failed":
                    self.last_gate_passed = None
                    self.last_gate_critique = (
                        f"闸门不可用（{str(self.last_gate_result.get('reason') or '')[:80]}）"
                        "，本步以内层判定为准")
                else:
                    self.last_gate_passed = bool(self.last_gate_result.get("passed"))
                    _gq = [str(q).strip() for q in
                           (self.last_gate_result.get("questions") or [])
                           if str(q or "").strip()]
                    self.last_gate_critique = " ".join(_gq)[:300]
                    for _q in _gq:                # 已问清单（防下一步重复问）
                        if _q not in self._gate_asked:
                            self._gate_asked.append(_q)
                    if not self.last_gate_passed:
                        # 闸门追问并进 critique 槽（原版同一条通路：
                        # action.py:197-200 的 Critique:）—— 当步注入下一步
                        _ask = ("；".join(_gq))[:300] or "维度清单还有没答到的"
                        critique = (f"{critique}；【清单追问】{_ask}"
                                    if critique else f"【清单追问】{_ask}")
                        success = False
            # :256-270 —— **每步**按本轮执行摘要重检索技能，再重建两条消息
            new_skills = self.retrieve_skills(
                query=self.task + "\n\n" + self.summarize_chatlog(events))
            self.messages = [
                {"role": "system",
                 "content": self.render_system_message(skills=new_skills)},
                {"role": "user",
                 "content": self.render_human_message(
                     events=events, code=sql, task=self.task,
                     context=self.context, critique=critique)},
            ]
            # 展示口径（2026-10-29 用户指出「失败 · 取到 1 行」语序误导）：
            # 先报**做了什么**（取到几行 + 实际 SQL 片段），再报**判成什么**
            # （内层判官/闸门的判词）。取到数 ≠ 判成功 —— 拒的是口径不是取数。
            _verdict = "判闭合" if success else "判未闭合"
            print(f"  [第 {self.rollout_num_iter + 1}/{self.task_max_retries} 次] "
                  f"{self.summarize_chatlog(events)}"
                  + (f" · SQL: {str(self.last_sql or '')[:120]}"
                     if self.last_sql else "")
                  + f" → {_verdict}")
            if not success and critique:
                print(f"    判词：{critique[:200]}")
            if self.gate_check is not None and self.last_gate_result:
                _gs = ("闸门不可用，内层单独判"
                       if self.last_gate_passed is None
                       else ("闸门过" if self.last_gate_passed else "闸门不过"))
                print(f"    闸门：{_gs}"
                      + (f"｜追问：{self.last_gate_critique[:140]}"
                         if (self.last_gate_passed is False
                             and self.last_gate_critique) else ""))
            if success and self.critic_reasoning:
                print(f"    critic 判词：{self.critic_reasoning[:160]}")
        else:
            # :271-274 —— 解析失败：只打印 Trying again，**不重建消息、不加 critique**
            print(f"  [第 {self.rollout_num_iter + 1}/{self.task_max_retries} 次] "
                  f"没给出 SELECT · Trying again!")

        self.rollout_num_iter += 1                                 # :276
        self.last_success = bool(success)
        done = (                                                   # :277-280
            self.rollout_num_iter >= self.task_max_retries or success
        )
        return events, done

    def critic_check(self, events: list[dict[str, Any]]) -> tuple[bool, str]:
        """同构 `voyager.py:232-238`：每步判一次，成败 + critique 回灌下一轮。

        判据两级（见 `agents/critic.py`）：先「真的取到数」，再让 LLM 按原版语义
        判「任务要求满足了没有」（口径）。第二级的 reasoning 是结论句，存进 self.answer。

        唯一 critic（2026-10-24 学习循环同构化）：原版一个 critic 判完即停、
        判词回灌。MVE 原先内层判一次、外层 task_critic 又判一次（双 critic 偏离）
        —— 现在把外层的强口径（环境实查清单：任务假设的表/列/取值必须能指认 +
        答案对数）并档进这里，内层判定即唯一判定；外层只汇总不再重判。
        """
        if self.critic is None:
            from voyager_vlml.agents.critic import VLMLCriticAgent

            self.critic = VLMLCriticAgent()

        # 数据源路径（fan-out 实测 + 环境实查都会用到）
        _dbp = (str(getattr(self.env, "db_path", "") or "")
                or str(getattr(self.env, "path", "") or ""))
        if not _dbp:
            try:
                import db_switch as _dbs
                _dbp = str(getattr(_dbs, "DEFAULT_DB", "") or "")
            except Exception:
                _dbp = ""

        # 确定性 fan-out 闸门（先于 LLM 判官）：JOIN 把驱动表行放大、且 SQL 在做
        # 聚合 → 父表数值按子表行数翻倍，答案必错。用 check_sql_grain 实测 JOIN
        # **原始**翻倍（不受外层聚合把行折叠成 1 行的影响，比事件 grain 更准）；
        # 事件 grain 作回退。命中即拒 + 给「先按关联键预聚合子表再 JOIN」的改写
        # 指导，让学习侧永不存乘爆配方、消费现场写能自纠（critic 失败 → 模型重写）。
        _fan_g = None
        if _dbp and "join" in (self.last_sql or "").lower():
            try:
                from voyager_vlml.env.vlml_mcp import check_sql_grain
                _fan_g = check_sql_grain(self.last_sql or "", _dbp)
            except Exception:
                _fan_g = None
        if not _fan_g:
            for _e in events:
                _g = (_e.get("grain") if isinstance(_e, dict) else None) or {}
                if int(_g.get("distinct") or 0) and int(_g.get("rows") or 0) > \
                        int(_g.get("distinct") or 0):
                    _fan_g = {
                        "driver": _g.get("driver"),
                        "rows": int(_g.get("rows") or 0),
                        "distinct": int(_g.get("distinct") or 0),
                        "ratio": (round(int(_g.get("rows") or 0)
                                        / int(_g.get("distinct") or 0), 2)
                                  if _g.get("distinct") else None),
                    }
                    break
        if _fan_g:
            try:
                from voyager_vlml.env.vlml_mcp import _has_aggregation
                if _has_aggregation(self.last_sql or ""):
                    return (False,
                            f"⚠️ JOIN 把驱动表行放大了 {_fan_g.get('ratio')}×"
                            f"（{_fan_g.get('rows')} 行 vs 驱动表 "
                            f"{_fan_g.get('distinct')} 行），而你在做聚合 —— "
                            f"父表数值会被按子表行数翻倍，答案必错。请先把右表"
                            f"按关联键聚合成单行再 JOIN"
                            f"（如 LEFT JOIN (SELECT 关联键, SUM(数值列) AS t "
                            f"FROM 子表 GROUP BY 关联键) s "
                            f"ON s.关联键=父表.关联键），或改用标量子查询 "
                            f"SUM(父表.数值列 + (SELECT COALESCE(SUM(子表.数值列),0) FROM "
                            f"子表 WHERE 子表.关联键=父表.关联键))")
            except Exception:
                pass

        _ctx = str(self.context or "")
        if _dbp:
            try:
                from curriculum_auto import db_observation as _dbobs
                _obs = str(_dbobs(_dbp) or "")
                if _obs:
                    _ctx += ("\n\n[环境实查（数据源真实的表/列/取值）：SQL 与答案"
                             "假设的任何表、列、取值都必须能在这里指认，指认不了"
                             " = 编造，判未完成并明说缺什么；答案里的每个具体数"
                             "字都必须能从取回数据指认或算出。**特别地：两个数"
                             "值列相乘/相除合成新口径，同样是假设业务参数** —— "
                             "合成出的口径在这份清单里指认不了，就是不可计算，"
                             "判未完成；]：\n"
                             + _obs)
            except Exception:
                pass  # 实查清单拿不到时退回原口径（真空闸仍在）
        d = self.critic.judge(
            observations=events,
            task={"task": self.task, "hints": self.hints},
            context=_ctx,
            sql=self.last_sql,
        )
        self.critic_reasoning = str(d.get("reasoning") or "")
        if d.get("success") and self.critic_reasoning:
            self.answer = self.critic_reasoning
        return bool(d.get("success")), str(d.get("critique") or "")

    # ---------- 同构 voyager.py:298-304 rollout ----------
    async def rollout(
        self, *, task: str, context: str = "",
        skills: list[dict[str, Any]] | None = None, topic_id: str = "",
        hints: list[str] | None = None,
        grabs: list[dict[str, Any]] | None = None,
        focus_tables: list[str] | None = None,
        gate_check: Callable[["VLMLActionAgent"], dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        await self.reset(task=task, context=context, skills=skills,
                         topic_id=topic_id, hints=hints,
                         grabs=grabs, focus_tables=focus_tables,
                         gate_check=gate_check)
        while True:
            _events, done = await self.step()
            if done:
                break
        return self.observations, self.last_sql

    # ---------- 兼容壳（loop.py / consume.py / task_intake.py 按这个签名调） ----------
    async def solve(
        self, question: str, *, max_iters: int = 4,
        hints: list[str] | None = None, skills: list[dict[str, Any]] | None = None,
        topic_id: str = "",
        grabs: list[dict[str, Any]] | None = None,
        focus_tables: list[str] | None = None,
        gate_check: Callable[["VLMLActionAgent"], dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """= 原版 `voyager.inference(sub_goals=[…])` 里那一步：reset → rollout。

        `hints` 不是新通道，它就是原版的 `context`（人给的补充说明），
        走 human message 的 `Context:` 槽。
        `grabs` / `focus_tables` = 本场景抓手条目与本题候选表（见 render_grabs）。
        `gate_check` = 步级闸门回调（候选A，2026-10-26）：内层 critic 每步
        判过之后调它核维度清单，追问当步注入下一步。不传 = 原版行为
        （只有内层 critic 一个判官），消费侧重放等旧调用方零改动。
        """
        self.task_max_retries = max(1, int(max_iters))
        ctx = "\n".join(str(h) for h in (hints or []) if str(h or "").strip())
        return await self.rollout(
            task=question, context=ctx, skills=skills,
            topic_id=topic_id, hints=hints,
            grabs=grabs, focus_tables=focus_tables,
            gate_check=gate_check)

    # ---------- 环境自述 ----------
    def _available_tools(self) -> set[str] | None:
        """当前库真的装载成功的工具名（报告工具按它过滤，不硬编码）。"""
        try:
            names = getattr(self.env, "tool_names", None)
            return set(names) if names else None
        except Exception:
            return None

    def _data_source_label(self) -> str:
        try:
            name = self.env.data_source()
            return str(name) if name else "当前数据库"
        except Exception:
            return "当前数据库"


def _main() -> int:
    """冒烟：跑一条 SQL → 看观察与 human message 长什么样。"""
    import asyncio

    agent = VLMLActionAgent()

    async def go() -> None:
        obs = await agent.run_sql("SELECT COUNT(*) AS n FROM rounds")
        print("观察:", {k: obs[k] for k in ("ok", "tool", "rows", "error")})
        print("数据:", str(obs.get("data"))[:150])
        print("\n--- human message 片段 ---")
        print(agent.render_human_message(
            events=[obs], code="SELECT COUNT(*) AS n FROM rounds",
            task="数回合", context="", critique="已从库里取到 1 行")[:900])

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
