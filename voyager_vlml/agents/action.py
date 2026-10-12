"""VLML 同构的 action（对应原版 voyager/agents/action.py）。

原版：模型照 control_primitives/*.js 写 **JS 代码** → mineflayer 执行 → 观察原样回灌。
这里：模型照 control_primitives 的 **SQL 原语** 写 **SQL** → VLML MCP 环境执行
（DB / RIB / VLGG）→ 观察原样回灌 {ok, tool, rows, error, data}。

用户指令②「把 js 代码指定 sql」+ ④「迭代修正机制 prompt 里的语言改为 sql」。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from voyager_vlml.control_primitives import render_primitives  # noqa: E402
from voyager_vlml.env.vlml_mcp import VLMLMCPEnv  # noqa: E402


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
