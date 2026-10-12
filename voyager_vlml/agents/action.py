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
    ) -> str:
        """让模型按题面写一条 SELECT；迭代修正时带上一轮的 critique 与 SQL 重写。

        只收 SELECT（不接受其它语句），拿不到就返回空串由调用方判失败。
        """
        from voyager_vlml.llm import chat

        system = self.render_system_message()
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
    ) -> tuple[list[dict[str, Any]], str]:
        """出题之后真正取数：写 SQL → VLML MCP 执行 → 失败带 critique 重写（迭代修正）。

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
        for _ in range(max_iters):
            sql = self.write_sql(
                question, schema=schema, critique=critique, last_sql=sql
            )
            if not sql:
                observations.append(
                    {"ok": False, "tool": "write_sql", "rows": 0,
                     "error": "模型没给出 SELECT 语句"}
                )
                critique = "上一轮没给出 SELECT。请只输出一条以 SELECT 开头的查询。"
                continue
            obs = await self.run_sql(sql)
            observations.append(obs)
            ok, critique = critic.check_task_success(
                observations=[obs],
                task={"task": question, "hints": list(hints or [])},
            )
            if ok:
                break
        return observations, sql

    # ---------- 迭代修正的 prompt（语言 = SQL） ----------
    def render_system_message(self) -> str:
        """写码阶段的 system：可用取数原语 + 库里的坑（同构原版把 JS 原语拼进来）。"""
        return (
            "你是在 VLML 电竞数据库里取数的分析 agent。用下面的原语写 **SQL** 完成任务。"
            "\n\n"
            + render_primitives()
        )

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
