"""VLML 同构的 action（对应原版 voyager/agents/action.py）。

原版：模型照 control_primitives/*.js 写 **JS 代码** → mineflayer 执行 → 观察原样回灌。
这里：模型照 control_primitives 的 **SQL 原语** 写 **SQL** → VLML MCP 环境执行
（DB / RIB / VLGG）→ 观察原样回灌 {ok, tool, rows, error, data}。

用户指令②「把 js 代码指定 sql」+ ④「迭代修正机制 prompt 里的语言改为 sql」。
"""

from __future__ import annotations

import json
import os
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


class VLMLActionAgent:
    """在 VLML MCP 环境里执行取数动作（SQL）。"""

    def __init__(self, env: VLMLMCPEnv | None = None) -> None:
        self.env = env or VLMLMCPEnv()

    async def run_sql(self, sql: str) -> dict[str, Any]:
        """执行一条 SQL，回一份可枚举观察。"""
        return await self.env.run_sql(sql)

    async def run_calls(self, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按序执行多个取数动作（call = {name, args}），回观察列表。"""
        out: list[dict[str, Any]] = []
        for c in calls:
            out.append(await self.env.call(str(c.get("name")), **(c.get("args") or {})))
        return out

    # ---------- LLM 写 SQL（用户指令：让闭环真正自己出题取数） ----------
    def write_sql(
        self,
        question: str,
        *,
        schema: str = "",
        critique: str = "",
        last_sql: str = "",
        skills: list[dict[str, Any]] | None = None,
    ) -> str:
        """让模型按题面写一条 SELECT；迭代修正时带上一轮的 critique 与 SQL 重写。

        只收 SELECT（不接受其它语句），拿不到就返回空串由调用方判失败。
        """
        from voyager_vlml.llm import chat

        system = self.render_system_message(skills=list(skills or []))
        if schema:
            system += (
                "\n\n【当前库真实 schema（只能用这里的表名/列名，不要编造）】\n"
                f"{schema}\n"
            )
        user = self.render_iteration_prompt(
            task=question,
            critique=critique or "（首轮，直接写一条 SELECT 回答这个问题）",
            last_sql=last_sql,
        )
        text = chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.3,
        )
        sql = _extract_sql(text)
        if not sql.lower().startswith("select"):
            return ""
        return sql

    async def solve(
        self,
        question: str,
        *,
        max_iters: int = 3,
        hints: list[str] | None = None,
        skills: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """出题之后真正取数：写 SQL → VLML MCP 执行 → 失败带 critique 重写（迭代修正）。

        `skills` = 检索到的已学会配方（照原版 voyager.py:195 先 retrieve 再跑）。
        它进 system 区块，所以**迭代的每一轮都带着**，不是只在第一轮给。

        返回 (观察列表, 最后一条 SQL)。观察是**可枚举的**：工具成败 + 取到几行。
        """
        from voyager_vlml.agents.critic import VLMLCriticAgent

        critic = VLMLCriticAgent()
        try:
            schema = await self.env.schema_block()
        except Exception:
            schema = ""

        observations: list[dict[str, Any]] = []
        sql = ""
        critique = ""
        n_skills = len(list(skills or []))
        # 同构原版 voyager.py:196-198 —— 打印注入了多少条已学会配方（原版只打印数量，
        # 不打印整条 system 消息：把 schema + 配方库全打出来会把日志淹没）。
        print(f"[Action Agent] 用 {n_skills} 条已学会配方渲染 system 消息")
        print(f"[Action Agent] 任务: {question[:140]}")
        for i in range(max_iters):
            sql = self.write_sql(
                question, schema=schema, critique=critique, last_sql=sql,
                skills=list(skills or []),
            )
            if not sql:
                observations.append(
                    {"ok": False, "tool": "write_sql", "rows": 0,
                     "error": "模型没给出 SELECT 语句"}
                )
                print(f"  [第 {i + 1}/{max_iters} 次] 没产出 SELECT，重试")
                critique = "上一轮没给出 SELECT。请只输出一条以 SELECT 开头的查询。"
                continue
            # 同构原版 voyager.py:218 —— 打印模型给出的动作（AI 消息）。
            print(f"  [第 {i + 1}/{max_iters} 次] 提案 SQL: {sql[:200]}")
            obs = await self.run_sql(sql)
            observations.append(obs)
            ok, critique = critic.check_task_success(
                observations=[obs],
                task={"task": question, "hints": list(hints or [])},
            )
            if ok:
                print(f"  [第 {i + 1}/{max_iters} 次] 成功 -> 工具={obs.get('tool')} 行数={obs.get('rows')}")
                print(f"[Action Agent] 任务在第 {i + 1} 次尝试成功")
                break
            # 同构原版 voyager.py:293 / 274 —— 展示环境反馈 + critic 判决，然后重试。
            print(f"  [第 {i + 1}/{max_iters} 次] 失败 -> {str(obs.get('error') or '没取回数据')[:160]}")
            print(f"  [critic] {critique[:240]}")
            print("  -> 重试！")
        else:
            print(f"[Action Agent] 任务在 {max_iters} 次尝试后失败（最后 critique: {critique[:240]}）")
        return observations, sql

    # ---------- 迭代修正的 prompt（语言 = SQL） ----------

    # 已学会的技能（可复用 SQL）怎么用的说明。
    #
    # 照原版 `render_system_message(skills=...)`：原版把技能 code 直接拼在
    # control_primitives 后面（`action.py:94` 的 `+ skills`），**一句解释都没有**。
    # 这里必须多这一段，因为两边技能形态不同：
    #   原版技能 = **带参数的函数**（`async function mineBlock(bot, name, count)`），
    #     复用天然就是"换参数调用"，不需要教；
    #   我们的技能 = **一整条写死了字面量的 SQL**（`team_name = 'NRG'`），
    #     是实例不是函数。不写这段，模型会把上次的值原样抄过来，
    #     取到的就是上一题那一队的数 —— 看着像复用，其实是记答案。
    _SKILL_USAGE = """【已学会的取数配方】下面是你以前跑通过、已经存进技能库的 SQL。

怎么用（这几条就是复用的判据）：
1. 复用的是配方的**计算方法** —— 用了哪些表、怎么 join、怎么聚合、怎么筛选。
2. **配方里写死的实体字面量（队伍名 / 图名 / 赛事号 / 选手名）是上一次的值，
   必须换成当前这道题指的那个值。** 抄上一次算出来的数是记答案，不是复用。
3. 当前这道题没指定实体（比如问"哪支队伍最高"）→ 保留配方的分组结构，别自己造值。
4. 配方算的量与当前这道题问的量**不是同一个量** → **不要用**，照题面从零写。
   换队伍名算复用；换个完全不同的量（K/D 比 → 连败数）不算，那是张冠李戴。
5. 只输出一条 SELECT。"""

    def render_system_message(
        self, *, skills: list[dict[str, Any]] | None = None
    ) -> str:
        """写码阶段的 system：取数原语 + 已学会的配方。

        同构原版 `action.py:94`：技能拼在原语后面，占同一个 system 区块，
        迭代修正的每一轮都带着（不是只带第一轮）。
        """
        base = (
            "你是在 VLML 电竞数据库里取数的分析 agent。用下面的原语写 **SQL** 完成任务。"
            "\n\n"
            + render_primitives()
        )
        blocks: list[str] = []
        for i, s in enumerate(list(skills or [])[:5]):
            code = str(s.get("code") or "").strip()
            if not code:
                continue
            blocks.append(
                f"-- 配方 {i + 1}｜topic={s.get('topic') or ''}\n"
                f"-- 上次用它做的是：{str(s.get('text') or '')[:160]}\n"
                f"{code[:1200]}"
            )
        if not blocks:
            return base
        return base + "\n\n" + self._SKILL_USAGE + "\n\n" + "\n\n".join(blocks)

    def render_iteration_prompt(
        self, *, task: str, critique: str, last_sql: str = ""
    ) -> str:
        """迭代修正 prompt：带着上一轮的 critique 重写 SQL（语言 = SQL，不给答案）。"""
        parts = [f"任务：{task}"]
        if last_sql:
            parts.append(f"上一轮你写的 SQL：\n{last_sql}")
        parts.append(f"上一轮的环境反馈：\n{critique}")
        parts.append(
            "请**只改 SQL**（表名/列名/过滤条件/聚合方式），重新给出一条能在当前库执行的 SELECT。"
        )
        return "\n\n".join(parts)


def _main() -> int:
    """冒烟：在真实库上跑一条 SQL，看观察与迭代 prompt。"""
    import asyncio

    agent = VLMLActionAgent()
    sql = "SELECT COUNT(*) AS n FROM games"

    async def go() -> None:
        obs = await agent.run_sql(sql)
        print("观察:", {k: obs[k] for k in ("ok", "tool", "rows", "error")})
        print("数据:", str(obs.get("data"))[:150])
        print("--- 迭代 prompt（SQL 语言）---")
        print(
            agent.render_iteration_prompt(
                task="数一下有几张图",
                critique="  ✗ query_sql：no such column: map",
                last_sql="SELECT COUNT(*) FROM games WHERE map='Lotus'",
            )[:400]
        )

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
