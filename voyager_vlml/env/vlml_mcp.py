"""VLML MCP 环境：把 VLML 的 MCP 工具装进 Voyager 的执行环境。

用户指令③：「在 voyager 环境里安装 VLMLMCP 依赖，（DB，RIB，VLGG）」

做什么：
  · 引导 VLML 运行环境（mve/vlml_env.py：cwd + mkdir 补丁 + 可切换 db_path）
  · 把 VLML 的 MCP 工具暴露成环境可调用的 action space：
      - SQL 原语：execute_custom_sql（query_sql）、get_database_info
      - 报告工具：match_summary / match_rounds / match_players / match_economy /
                 match_analysis / scouting / player_profile / pattern_detection
  · 执行后回一份**可枚举的观察**：{ok, tool, rows, error, data}
    这正是设计里「回给 Voyager 的环境反馈」：工具调用成功还是失败、这次取到几行。

⚠️ 这里**不产 VLML 判决、不给标准答案**：环境只负责「你去取数，取到什么我如实回报」。
   判分（VLML 跑一遍的结果）只在面板，不进这个环境反馈。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MVE_DIR = _REPO_ROOT / "mve"
if str(_MVE_DIR) not in sys.path:
    sys.path.insert(0, str(_MVE_DIR))


def _bootstrap_vlml() -> None:
    """引导 VLML 运行环境（cwd / 沙箱 mkdir 补丁 / 可切换数据库）。"""
    import vlml_env  # noqa: F401  # 副作用即引导


class VLMLMCPEnv:
    """VLML MCP 环境。接口刻意做小：call(name, args) / run_sql(sql)。"""

    def __init__(self) -> None:
        _bootstrap_vlml()
        self._tools: dict[str, Any] = {}
        self._load_tools()

    # ---------- 工具装载 ----------
    def _load_tools(self) -> None:
        """装载 VLML MCP 工具（SQL 原语 + 报告工具）。失败的工具记为不可用，不拖垮环境。"""
        specs = [
            # (对外名字, 模块, 函数)
            ("query_sql", "vlml.tools.db_query_tools", "execute_custom_sql"),
            ("get_database_info", "vlml.tools.db_query_tools", "get_database_info"),
            ("match_summary_report", "vlml.tools.reports.match_analysis", "match_summary_report"),
            ("match_rounds_report", "vlml.tools.reports.match_analysis", "match_rounds_report"),
            ("match_players_report", "vlml.tools.reports.match_analysis", "match_players_report"),
            ("match_economy_report", "vlml.tools.reports.match_analysis", "match_economy_report"),
            ("match_analysis_report", "vlml.tools.reports.match_analysis", "match_analysis_report"),
            ("scouting_report", "vlml.tools.reports.scouting", "scouting_report"),
            ("player_profile_report", "vlml.tools.reports.player_profile", "player_profile_report"),
            ("pattern_detection_report", "vlml.tools.reports.pattern_detection", "pattern_detection_report"),
        ]
        import importlib

        for name, mod, fn in specs:
            try:
                self._tools[name] = getattr(importlib.import_module(mod), fn)
            except Exception as e:  # pragma: no cover
                print(f"（VLML MCP 工具 {name} 装载失败：{type(e).__name__}: {e}）")

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def has(self, name: str) -> bool:
        return name in self._tools

    # ---------- 数据源（DB / RIB / VLGG） ----------
    def data_source(self) -> str:
        """当前库对应的数据源。按库文件名判（mve 既有约定：vlml_vlr→vlr.gg /
        vlml_rib→rib.gg，其余→GRID/DB）。"""
        try:
            import vlml_env

            path = str(getattr(vlml_env, "ACTIVE_DB_PATH", "") or "")
        except Exception:
            path = ""
        name = os.path.basename(path).lower()
        if "rib" in name:
            return "rib.gg"
        if "vlr" in name:
            return "vlr.gg"
        return "GRID/DB"

    # ---------- 执行 ----------
    async def call(self, name: str, **args: Any) -> dict[str, Any]:
        """调一个 VLML MCP 工具，回一份可枚举的观察。

        返回 {ok, tool, rows, error, data}：
          ok    —— 工具调用成功还是失败（设计里的反馈项①）
          rows  —— 这次取到几行（设计里的反馈项②）
        """
        fn = self._tools.get(name)
        if fn is None:
            return {"ok": False, "tool": name, "rows": 0,
                    "error": f"未装载的工具 {name}", "data": None}
        try:
            res = await fn(**args)
        except Exception as e:
            return {"ok": False, "tool": name, "rows": 0,
                    "error": f"{type(e).__name__}: {e}", "data": None}
        return {"ok": True, "tool": name, "rows": _rows_of(res),
                "error": "", "data": res}

    async def run_sql(self, sql: str) -> dict[str, Any]:
        """执行一条 SQL（模型写的取数代码在这里跑）。"""
        return await self.call("query_sql", sql_query=sql)


def _rows_of(res: Any) -> int:
    """从工具返回里数「取到几行」，尽量兼容几种返回形态。"""
    if res is None:
        return 0
    if isinstance(res, dict):
        for key in ("rows", "data", "results", "records", "items"):
            v = res.get(key)
            if isinstance(v, list):
                return len(v)
        # 有些报告工具把结果放在嵌套里
        for v in res.values():
            if isinstance(v, list):
                return len(v)
        return 1 if res else 0
    if isinstance(res, list):
        return len(res)
    return 0


def _main() -> int:
    """冒烟：列出装载的工具 + 数据源 + 跑一条 SQL 看观察。"""
    import asyncio

    env = VLMLMCPEnv()
    print("数据源 :", env.data_source())
    print("已装载 :", env.tool_names)

    async def go() -> None:
        obs = await env.run_sql("SELECT COUNT(*) AS n FROM rounds")
        print("SQL 观察:", {k: obs[k] for k in ("ok", "tool", "rows", "error")})
        if obs["ok"]:
            print("返回片段:", str(obs["data"])[:200])

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
