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

    def data_source(self) -> str:
        """本环境的数据来源（自报）。`learn_orchestrate` 用它填观察的
        `data_sources` —— 不报的话，取到真数也会被判成「知识型：一次数据源
        都没调」（2026-10-23 实测：工资题取到 3 行，库里写 evidence=knowledge）。"""
        try:
            return f"scene_db:{Path(self.db_path).name}"
        except Exception:
            return "scene_db"

    async def schema_block(self, max_cols: int = 20,
                           max_tables: int = 40) -> str:
        """表 → 列（+ 行数 + 低基数列真实取值），实查该场景库。形状照
        VLMLMCPEnv.schema_block。

        ⚠️ 必须带**真实取值**：执行层照这个写 SQL，光给列名它会猜字面量
        （实测编出 dept='技术部'、month='2023-09'，而库里是 '技术'/'2026-09'，
        整条 SQL 落空）。低基数字符串/布尔列把实查 distinct 值列出来，模型才
        写得对字面量 —— 与 curriculum_auto.db_observation 同一把尺子。
        """
        try:
            rows = await asyncio.to_thread(self._schema_rows)
        except Exception:
            return ""
        cols: dict[str, list[str]] = {}
        types: dict[str, str] = {}
        for r in rows:
            if len(r) >= 3:
                t, c, dt = str(r[0]), str(r[1]), str(r[2]).upper()
                cols.setdefault(t, []).append(c)
                types[f"{t}.{c}"] = dt
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
            # ---- pivot 宽表折叠（2026-11-08）----
            # 实查 L 场景 web_facts：908 列，前 900 多列是「平台名 / 平台名 http
            # 状态码」成对出现的稀疏列（每行只有一个平台有值），真正通用的列
            # （item/source_url/正文/来源）排在最后。旧实现只给 `cs[:20]` →
            # 模型**根本看不见 `正文`**，于是拿平台宽列拼 SQL，写出
            # `GitHub http 状态码` 这种裸标识符（Parser Error at "http"）和
            # 库里不存在的 `Twitter http 状态码`（Binder Error）—— 两轮组合
            # 全败，重问已闭合的题必然 fetched_empty（2026-11-07 实测）。
            # 判据是确定性的：若列 X 与「X + 分隔符 + 后缀」同时存在，这组列
            # 就是 pivot 出来的稀疏维度，折叠成一句说明；其余列（真正的字段）
            # 全量列出 —— 与具体平台名/后缀无关，不写死任何业务词。
            cset = set(cs)
            pivot_child: dict[str, str] = {}
            for c in cs:
                # 父名必须是**另一个已存在的列**，且 c = 父名 + 分隔符 + 后缀
                # （`1337x http 状态码` 的父是 `1337x`；不能反过来 rsplit，
                #   那样父名会变成 `1337x http`，一个都不成组 —— 实测过）。
                for d in cset:
                    if d == c or not c.startswith(d):
                        continue
                    if c[len(d):len(d) + 1] in (" ", "_", "/", "-"):
                        pivot_child[c] = d
                        break
            pivot_base = set(pivot_child.values())
            folded = set(pivot_child) | pivot_base
            rest = [c for c in cs if c not in folded]
            if not rest:                     # 整表都是成对列 → 至少保住前几列
                rest = cs[:max_cols]
                folded = set(cs) - set(rest)
            # 折叠后剩下的才是**真正的字段**（item/source_url/正文/来源…），
            # 全量给；只有真的还很宽（>120 列）才截断。
            shown = rest if len(rest) <= 120 else rest[:max(max_cols, 24)]
            line = head + "：" + "，".join(shown)
            if len(rest) > len(shown):
                line += "…"
            if folded:
                _ex = sorted(pivot_base)[:3]
                line += (f"（另有 {len(pivot_base)} 组互斥稀疏列："
                         f"「{'/'.join(_ex)}」各配一列「… + 后缀」状态列；"
                         f"一行里同组只有一列有值 —— 它们不是可加的维度，"
                         f"要平台清单请对本表的文本列做解析或 SELECT DISTINCT，"
                         f"**不要假设每个 X 都有 X+后缀 那一列**）")
            lines.append(line)
            # 低基数字符串/布尔列：把实查 distinct 值列出来，模型照着写字面量
            # （dept 取值是「技术」不是「技术部」，month 是「2026-09」不是「2023-09」）。
            for c in shown:
                dt = types.get(f"{t}.{c}", "")
                if dt not in ("VARCHAR", "TEXT", "STRING", "BOOLEAN"):
                    continue
                try:
                    data = await self.run_sql(
                        f'SELECT DISTINCT "{c}" FROM "{t}" LIMIT 13')
                    flat = [r[0] for r in ((data.get("data") or {}).get("rows") or [])
                            if r and r[0] is not None]
                    if flat and len(flat) <= 12:
                        lines.append(
                            f"    · {t}.{c} 真实取值: "
                            + "、".join(str(x) for x in flat))
                except Exception:
                    pass
        return "\n".join(lines)
