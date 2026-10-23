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
    @staticmethod
    def _nested_error(res: Any) -> str:
        """从工具返回里挖出**嵌套的 error**。

        ⚠️ 实测踩过：VLML 工具失败时**不抛异常**，而是返回
        `{"error": "Query execution failed: Binder Error: ..."}`（顶层直接带
        error 键）。这里原来只用 try/except，于是错误响应被判成
        `ok=True, rows=1` —— SQL 明明炸了，闭环却当成"取到了 1 行"：
          · 练习侧：假成功 → 错误的 SQL 被当技能存进库；
          · 消费侧：取到的是错误对象，事实整理器看到的是 error 不是数据
            （它返回空数组是对的 —— "不许错配"的约束生效了），
            于是"工具调通了、取到 1 行"，人却得不到任何回答。
        """
        if not isinstance(res, dict):
            return ""
        err = res.get("error")
        if not err:
            inner = res.get("result") or res.get("data")
            if isinstance(inner, dict):
                err = inner.get("error")
        return str(err or "")

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
        err = self._nested_error(res)
        if err:
            return {"ok": False, "tool": name, "rows": 0,
                    "error": err[:300], "data": res}
        return {"ok": True, "tool": name, "rows": _rows_of(res),
                "error": "", "data": res}

    async def run_sql(self, sql: str) -> dict[str, Any]:
        """执行一条 SQL（模型写的取数代码在这里跑）。"""
        return await self.call("query_sql", sql_query=sql)

    async def table_row_counts(self) -> dict[str, int]:
        """每张表的行数（一次 UNION ALL 查询）。

        抽出来单独给两处用：`schema_block` 渲染要用，`action.solve` 校验
        hints 有没有指向空表也要用 —— 以前这段只在 schema_block 内部，
        算出来的行数只变成了字符串，别处拿不到结构化的。
        """
        obs = await self.run_sql(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema='main' GROUP BY table_name ORDER BY table_name"
        )
        if not obs.get("ok"):
            return {}
        data = obs.get("data") or {}
        rows = data.get("rows") if isinstance(data, dict) else data
        names = [str(r[0]) for r in (rows or []) if r]
        if not names:
            return {}
        union = " UNION ALL ".join(
            f"SELECT '{t}' AS t, COUNT(*) AS n FROM \"{t}\"" for t in names)
        try:
            obs2 = await self.run_sql(union)
        except Exception:
            return {}
        if not obs2.get("ok"):
            return {}
        d2 = obs2.get("data") or {}
        r2 = d2.get("rows") if isinstance(d2, dict) else d2
        out: dict[str, int] = {}
        for r in r2 or []:
            if len(r) >= 2:
                out[str(r[0])] = int(r[1] or 0)
        return out

    async def schema_block(self, max_cols: int = 14, max_tables: int = 24) -> str:
        """实查当前库的表与列，渲染成给模型看的 schema 文本块。

        照伴学 entity_catalog 的「实查不写死」原则：出题与写 SQL 都只能引用
        这里真实存在的表名/列名，避免模型编造。
        """
        obs = await self.run_sql(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='main' ORDER BY table_name, ordinal_position"
        )
        if not obs.get("ok"):
            return ""
        data = obs.get("data") or {}
        rows = data.get("rows") if isinstance(data, dict) else data
        cols: dict[str, list[str]] = {}
        types: dict[str, dict[str, str]] = {}
        for r in rows or []:
            if len(r) >= 2:
                t = str(r[0])
                cols.setdefault(t, []).append(str(r[1]))
                if len(r) >= 3:
                    types.setdefault(t, {})[str(r[1])] = _short_type(str(r[2]))

        # ⚠️ 必须把**每张表有多少行**一起给模型（2026-10-13 实测踩的坑）：
        #   当前库（rib）`agg_player_round_stats` 是 **0 行**，真实数据全在
        #   `ext_player_round_stats`（2850 行）。只给表名列名的话，模型看着
        #   agg_* 表名更"正规"就去查它 → 取回 0 行 → 报「没取到数」→
        #   人看到的是「Voyager 回答不出来」，而数据明明在隔壁表里。
        #   所以空表要**明写**出来：查它只会得 0 行，别去。
        counts: dict[str, int] = await self.table_row_counts()
        names = [t for t, _ in list(cols.items())[:max_tables]]

        # ⚠️ 必须给**一行真实数据**（2026-10-13 第 4 个坑）：
        #   只给列名，模型就会猜列里装的是什么值 —— 实测写出
        #   `WHERE round_won = 'win'`、`= 'team'`（把布尔列当成字符串比）；
        #   `side` 究竟是 attack/defense 还是 T/CT，光看列名也只能猜。
        #   VLML 自带的列级口径（`column_definitions.yaml`）**只覆盖 3 张表**，
        #   指望不上。给一行样例 = 让值自己说话，比写一百句口径说明管用。
        #   只取行数最多的 8 张（schema 有长度上限，宁可少而准，别把样例挤没了）。
        samples: dict[str, str] = {}
        for t in sorted([x for x in names if counts.get(x)],
                        key=lambda x: -counts.get(x, 0))[:8]:
            try:
                o = await self.run_sql(f'SELECT * FROM "{t}" LIMIT 1')
                if not o.get("ok"):
                    continue
                dd = o.get("data") or {}
                rr = dd.get("rows") if isinstance(dd, dict) else dd
                if not rr:
                    continue
                first = rr[0]
                if not isinstance(first, (list, tuple)):
                    continue
                pairs = []
                for name, v in zip(cols.get(t) or [], first):
                    sv = "" if v is None else str(v)
                    pairs.append(f"{name}={sv[:16] or 'NULL'}")
                if pairs:
                    samples[t] = "，".join(pairs[:8])
            except Exception:
                continue

        def _line(t: str, c: list[str]) -> str:
            ty = types.get(t) or {}
            head = "，".join(
                f"{n}" + (f"::{ty[n]}" if n in ty else "") for n in c[:max_cols])
            n = counts.get(t)
            if len(c) > max_cols and n != 0:
                # ⚠️ 有数据的表：截断必须**自报家数并把列名补齐**（2026-10-23 实测的根因）。
                #   `ext_player_round_stats` 有 18 列，max_cols=14 把
                #   alive / first_kill / source / ingested_at 全截掉了，而 `first_kill`
                #   是这张库里**唯一**的首血数据来源 → 模型看不见它，只能信 hints 里
                #   那张 0 行的 agg 表 → 连跑 3 轮全空、被误判成"学不会"。
                #   剩余列不给类型（省字符），但列名一个都不能少 —— 名字就是模型能查的东西。
                head += (f"，…另有 {len(c) - max_cols} 列："
                         + "、".join(c[max_cols:]))
            elif len(c) > max_cols:
                # 空表：列名全列出来没意义（又不会查它），只报总数。
                # 实测 `agg_player_round_stats` 有 123 列，全列出来纯烧字符。
                head += f"，…共 {len(c)} 列"
            if n is None:
                base = f"- {t}: {head}"
            elif n == 0:
                base = f"- {t}（**0 行 · 空表，查它只会得 0 行，别查**）: {head}"
            else:
                base = f"- {t}（{n} 行）: {head}"
            smp = samples.get(t)
            return base + (f"\n    样例：{smp}" if smp else "")

        # 有数据的表排前面、空表沉到最后：schema 文本有长度上限（max_tables），
        # 真被截断时宁可丢空表，也不能把唯一有数据的 ext_* 表截没了
        # （它们字母序靠后，不排序就会被先截掉）。
        ordered = sorted(cols.items(), key=lambda kv: -(counts.get(kv[0]) or 0))
        return "\n".join(_line(t, c) for t, c in ordered[:max_tables])


def _short_type(t: str) -> str:
    """把 DuckDB 的类型名压短，省 schema 的字符预算。

    为什么必须带类型（2026-10-13 实测）：只给列名，模型就猜列里装什么值
    —— 写出 `WHERE round_won = 'win'`、`= 'team'`，把布尔列当字符串比。
    带上 `::bool` 之后这类猜测当场消失。压短是为了在同样的字数里多塞
    几张表：schema 文本块有长度上限，类型名写全了就会挤掉后面的表。
    """
    s = (t or "").strip().upper()
    if not s:
        return ""
    if s.startswith("VARCHAR") or s.startswith("CHAR") or s == "TEXT":
        return "str"
    if s in {"BOOLEAN", "BOOL"}:
        return "bool"
    if any(s.startswith(p) for p in ("DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL")):
        return "float"
    if s.endswith("INT"):  # BIGINT / SMALLINT / UTINYINT / HUGEINT … 整数族兜底
        return "int"
    if s.startswith("TIMESTAMP"):
        return "ts"
    if s == "DATE":
        return "date"
    if s == "TIME":
        return "time"
    if s.startswith("BLOB") or s.startswith("BYTEA"):
        return "blob"
    return s.lower()[:12]


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
