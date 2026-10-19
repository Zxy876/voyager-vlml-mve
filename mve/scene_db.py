#!/usr/bin/env python3
"""场景自有数据源的执行环境（不经过 VLML）。

用户 2026-10-19：「别的场景下，voyager 的学习和 VLML 没有关系」。

VLMLMCPEnv 的 `query_sql` 最终落到 VLML 的 `execute_custom_sql`，而它连的是
`vlml_env.ACTIVE_DB_PATH`（进程级全局，由 db_config 决定 = rib/GRID）。所以
在 Scene B 里学习，实际上一直在读 Scene A 的库 —— 表现是「问世界模型，答案
里列出一串 Valorant 战队」。本模块给非 VLML 场景一个**最小等价物**：

    SceneDBEnv(db_path).run_sql(sql)      → 直连该场景的 duckdb（read_only）
    SceneDBEnv(db_path).schema_block()    → 实查 information_schema 渲染 schema

接口刻意与 VLMLMCPEnv 对齐（run_sql / schema_block 返回同一形状），这样
`VLMLActionAgent(env=SceneDBEnv(...))` 就能直接复用它的写 SQL prompt 与
critic —— 复用的是「写 SQL + 迭代修正」这套通用能力，数据面完全不碰 VLML。

地界（诚实）
-----------
· 只实现 query_sql 这一种原语。VLML 的 8 个报告工具（match_summary 等）是
  VLML 数据域专属，非 VLML 场景没有，也不假装有。
· 只读连接（read_only=True）：学习不写别人的库。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


class SceneDBEnv:
    """把「某个场景自己的 duckdb」包成 Voyager 能用的最小环境。"""

    def __init__(self, db_path: str) -> None:
        p = str(db_path or "").strip()
        if not p:
            raise ValueError("SceneDBEnv 需要一个库路径")
        self.db_path = str(Path(p).expanduser())
        self.tool_names = ["query_sql"]

    # ---------- 执行 ----------
    def _run(self, sql: str) -> dict[str, Any]:
        import duckdb
        con = duckdb.connect(self.db_path, read_only=True)
        try:
            cur = con.execute(sql)
            cols = [d[0] for d in (cur.description or [])]
            rows = cur.fetchall()
            return {"columns": cols, "rows": rows, "row_count": len(rows)}
        finally:
            try:
                con.close()
            except Exception:
                pass

    async def run_sql(self, sql: str) -> dict[str, Any]:
        """同 VLMLMCPEnv.run_sql 的观察形状：{ok, tool, rows, error, data}。"""
        text = str(sql or "").strip()
        if not text:
            return {"ok": False, "tool": "query_sql", "rows": 0,
                    "error": "空 SQL", "data": None}
        if not text.lower().startswith(("select", "with")):
            # 与 VLML 的 execute_custom_sql 同一条安全线：只许读
            return {"ok": False, "tool": "query_sql", "rows": 0,
                    "error": "只允许 SELECT（学习不写别人的库）", "data": None}
        try:
            data = await asyncio.to_thread(self._run, text)
        except Exception as e:
            return {"ok": False, "tool": "query_sql", "rows": 0,
                    "error": f"Query execution failed: {type(e).__name__}: {e}",
                    "data": None}
        return {"ok": True, "tool": "query_sql",
                "rows": int(data.get("row_count") or 0),
                "error": "", "data": data}

    async def call(self, name: str, **args: Any) -> dict[str, Any]:
        """兼容 VLMLMCPEnv.call 的入口（本环境只有 query_sql）。"""
        if name != "query_sql":
            return {"ok": False, "tool": name, "rows": 0,
                    "error": f"本场景没有工具 {name}（非 VLML 场景只有 query_sql）",
                    "data": None}
        return await self.run_sql(str(args.get("sql_query") or ""))

    # ---------- schema 渲染 ----------
    def _schema_rows(self) -> list[tuple]:
        import duckdb
        con = duckdb.connect(self.db_path, read_only=True)
        try:
            return con.execute(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns WHERE table_schema='main' "
                "ORDER BY table_name, ordinal_position").fetchall()
        finally:
            try:
                con.close()
            except Exception:
                pass

    async def schema_block(self, max_cols: int = 20,
                           max_tables: int = 40) -> str:
        """表 → 列（+ 行数），实查该场景库。形状照 VLMLMCPEnv.schema_block。"""
        try:
            rows = await asyncio.to_thread(self._schema_rows)
        except Exception:
            return ""
        cols: dict[str, list[str]] = {}
        for r in rows:
            if len(r) >= 2:
                cols.setdefault(str(r[0]), []).append(str(r[1]))
        if not cols:
            return ""
        counts: dict[str, int] = {}
        for t in list(cols)[:max_tables]:
            try:
                data = await self.run_sql(
                    f'SELECT COUNT(*) AS n FROM "{t}"')
                got = ((data.get("data") or {}).get("rows") or [[0]])[0]
                counts[t] = int(got[0] or 0)
            except Exception:
                counts[t] = 0
        lines = []
        for t, cs in list(cols.items())[:max_tables]:
            n = counts.get(t, 0)
            head = f"- {t}（{n} 行）" + ("：⚠️ 空表，别查它" if n == 0 else "")
            lines.append(head + "：" + "，".join(cs[:max_cols])
                         + ("…" if len(cs) > max_cols else ""))
        return "\n".join(lines)
