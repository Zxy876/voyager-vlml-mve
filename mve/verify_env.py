#!/usr/bin/env python3
"""一键验证环境：合成数据是否还在、agg 表是否有数、MCP 工具能否返回数据。

用法：
    python mve/verify_env.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VLML = ROOT / "vlml"
# VLML 用相对路径解析 data/vlml_events.duckdb，必须把 cwd 切到仓库根
os.chdir(VLML)
sys.path.insert(0, str(VLML / "src"))

# 沙箱环境会对「已存在目录」的 mkdir 抛 PermissionError(EEXIST)，
# 而 vlml/db/manager.py 每次建连接都会 mkdir(data, exist_ok=True)。
# 这里在 MVE 侧打补丁，不动仓库源码：目录已存在就直接跳过。
_orig_mkdir = Path.mkdir


def _safe_mkdir(self, *args, **kwargs):  # type: ignore[no-untyped-def]
    try:
        if self.exists() and self.is_dir():
            return None
    except OSError:
        pass
    return _orig_mkdir(self, *args, **kwargs)


Path.mkdir = _safe_mkdir  # type: ignore[assignment]

import duckdb  # noqa: E402

from vlml.tools.db_query_tools import execute_custom_sql, get_database_info  # noqa: E402
from vlml.tools.reports.match_analysis import match_summary_report  # noqa: E402

DB = VLML / "data" / "vlml_events.duckdb"
SERIES = "2843069"

ok = True


def mark(cond: bool, label: str, detail: str = "") -> None:
    global ok
    ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def main() -> int:
    print("=" * 64)
    print("  VLML 合成环境验证")
    print("=" * 64)

    if not DB.exists():
        print(f"  数据库不存在: {DB}")
        return 1
    con = duckdb.connect(str(DB), read_only=True)

    print("\n--- 核心表 ---")
    for t in ["series", "games", "rounds", "base_events"]:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        mark(n > 0, f"{t:14s}", f"{n} 行")

    print("\n--- 聚合表（transformations 产物）---")
    agg_tables = [
        "agg_player_round_stats", "agg_player_game_stats", "agg_player_series_stats",
        "agg_team_round_stats", "agg_team_game_stats", "agg_player_daily_stats",
        "agg_tournament_stats", "agg_first_blood_stats", "agg_post_plant_stats",
        "agg_team_round_summary", "agg_team_map_stats", "agg_team_series_stats",
        "agg_player_win_shares",
    ]
    empty = []
    for t in agg_tables:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        if n == 0:
            empty.append(t)
    mark(not empty, f"{len(agg_tables)} 张 agg 表", "全部有数据" if not empty else f"空表: {empty}")

    print("\n--- 植入的模式是否成立 ---")
    rows = con.execute("""
        SELECT map_name, fb_team, COUNT(*) fb, SUM(fb_team_won) won,
               ROUND(AVG(fb_team_won)*100, 1) conv
        FROM agg_first_blood_stats GROUP BY 1, 2 ORDER BY 1, 2
    """).fetchall()
    for m, team, fb, won, conv in rows:
        print(f"       {m:8s} {team:8s} 首血 {fb:2d} → 赢 {won:2d} = {conv}%")
    corrode = {(m, t): c for m, t, fb, w, c in rows}
    mark(
        corrode.get(("Corrode", "Cloud9"), 100) < 40,
        "Corrode 上 Cloud9 首血转换率崩盘",
        f"{corrode.get(('Corrode', 'Cloud9'))}%",
    )
    fd = con.execute("""
        SELECT fd_player, COUNT(*) c FROM agg_first_blood_stats
        WHERE fd_team = 'Cloud9' GROUP BY 1 ORDER BY 2 DESC LIMIT 1
    """).fetchone()
    mark(fd[0] == "OXY", f"Cloud9 首死最多的人是 OXY", f"{fd[0]} {fd[1]} 次")

    print("\n--- MCP 工具 ---")

    async def check_tools():
        d = await get_database_info()
        mark("available_tables" in d, "get_database_info", f"{len(d.get('available_tables', []))} 张表")
        r = await execute_custom_sql("SELECT map_name, COUNT(*) AS n FROM rounds GROUP BY 1 ORDER BY 1")
        mark(r.get("row_count", 0) == 3, "execute_custom_sql", json.dumps(r["rows"], ensure_ascii=False))
        s = await match_summary_report(series_id=SERIES, team_name="Cloud9")
        km = (s.get("key_metrics") or {}).get("team", {})
        mark("opening_duels" in km, "match_summary_report", f"sections={len(s)}")
        od = km.get("opening_duels", {})
        print(f"       opening_duels: {json.dumps(od, ensure_ascii=False)}")
        # VLML 自带 num/denom —— 这是 fact.base 的数据来源
        mark(
            "num" in json.dumps(od),
            "key_metrics 自带 num/denom",
            "→ fact.base 可直接取 denom，不用自算",
        )

    asyncio.run(check_tools())

    print("\n" + "=" * 64)
    print("  ✅ 环境可用，可以开始跑 MVE" if ok else "  ❌ 有问题，见上")
    print("=" * 64)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
