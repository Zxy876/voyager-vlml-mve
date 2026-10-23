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
from typing import Any

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

    # ---------- 执行 ----------
    async def run_sql(self, sql: str) -> dict[str, Any]:
        """执行一条 SQL，回一份可枚举观察（等价原版 env.step）。"""
        return await self.env.run_sql(sql)

    async def run_calls(self, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for c in calls:
            out.append(await self.env.call(str(c.get("name")), **(c.get("args") or {})))
        return out

    # ---------- 同构 agents/action.py:78-103 render_system_message ----------
    def render_system_message(
        self, *, skills: list[dict[str, Any]] | None = None
    ) -> str:
        """system = 原语 + 库结构 + 已学会技能。

        原版 `action.py:94` 是 `load_control_primitives_context(...) + skills`，
        一句解释都没有。这里同样不加解释段。
        """
        base = (
            f"你是在 {self._data_source_label()} 里取数的分析 agent。"
            "用下面的原语写 **SQL** 完成任务。"
            "\n\n"
            + render_primitives(available=self._available_tools())
        )
        if self.schema:
            base += (
                "\n\n【当前库真实 schema（只能用这里的表名/列名，不要编造）】\n"
                f"{self.schema}\n"
            )
        blocks: list[str] = []
        for s in list(skills or [])[:5]:
            code = str(s.get("code") or "").strip()
            if code:
                blocks.append(code[:1200])
        if blocks:
            base += "\n\n" + "\n\n".join(blocks)
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
    ) -> None:
        self.rollout_num_iter = 0
        self.task = task
        self.context = context
        self.topic_id = topic_id
        self.hints = list(hints or [])
        self.observations = []
        self.last_sql = ""
        self.critic_reasoning = ""
        self.answer = ""
        self.last_success = False
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
            events = [obs]
            self.observations.extend(events)                       # :230 recorder.record
            success, critique = self.critic_check(events)          # :232-238
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
            print(f"  [第 {self.rollout_num_iter + 1}/{self.task_max_retries} 次] "
                  f"{'成功' if success else '失败'} · {self.summarize_chatlog(events)}")
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
        """
        if self.critic is None:
            from voyager_vlml.agents.critic import VLMLCriticAgent

            self.critic = VLMLCriticAgent()
        d = self.critic.judge(
            observations=events,
            task={"task": self.task, "hints": self.hints},
            context=self.context,
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
    ) -> tuple[list[dict[str, Any]], str]:
        await self.reset(task=task, context=context, skills=skills,
                         topic_id=topic_id, hints=hints)
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
    ) -> tuple[list[dict[str, Any]], str]:
        """= 原版 `voyager.inference(sub_goals=[…])` 里那一步：reset → rollout。

        `hints` 不是新通道，它就是原版的 `context`（人给的补充说明），
        走 human message 的 `Context:` 槽。
        """
        self.task_max_retries = max(1, int(max_iters))
        ctx = "\n".join(str(h) for h in (hints or []) if str(h or "").strip())
        return await self.rollout(
            task=question, context=ctx, skills=skills,
            topic_id=topic_id, hints=hints)

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
