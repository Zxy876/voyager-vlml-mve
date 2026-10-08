#!/usr/bin/env python3
"""题目锚点：当前库里"代表那一场/那一队/那张图"是谁 —— **从库实查，不写死**。

为什么必须有它
--------------
`tasks.py` / `question_gen.py` / `voyager.py` 里原本写死：

    SERIES = "2843069"      # GRID 切片里那场
    C9     = "Cloud9"

换库（GRID → vlr.gg → rib.gg）之后这两个值在库里**根本不存在**，于是：

    种子题 17 个评分点，在 rib 库上**0 个能跑出数**

实测（2026-10-08）：`series_totals / map_rounds_split / fb_conversion_analysis /
corrode_collapse / lotus_win_rate / max_losing_streak_map` 全部 value=None 或 0，
其中 3 个直接报"SQL 返回 0 行 —— 这道题在本场数据里无解"。

`db_switch.probe()` 早就给出过这条警告（"原题依赖的 series 2843069 不在这个
库里…所有题都查不到数据"），但只是**提示**，没有自动重配 —— 本模块就是把那
一步补上。

怎么挑（不能随便挑，否则题还是跑不出数）
----------------------------------------
- series：**回合数最多**的那场（数据最全，题最可能有解）
- team：那场里**回合数最多**的队
- map：那场里**回合数最多**的图

落盘与失效
----------
结果写进 `db_config.json` 的 `anchor` 字段（和 db_path 一起），换库时由
`db_switch.write()` 重算。读不到就现场连库实查一次。

⚠️ 进程内换库不会自动生效（模块 import 时就把值解析好了）—— 面板换库会重启
服务，够用。想强制刷新就跑 `python mve/anchor.py --refresh`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DB_CONFIG = HERE / "db_config.json"
FALLBACK = {"series": "2843069", "team": "Cloud9", "map": ""}


def _cfg() -> dict[str, Any]:
    try:
        obj = json.loads(DB_CONFIG.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _db_path() -> Path:
    p = str(_cfg().get("db_path") or "").strip()
    if p:
        return Path(p)
    return HERE.parent / "vlml" / "data" / "vlml_events.duckdb"


def pick(db_path: Path | str | None = None,
         prefer: dict[str, str] | None = None) -> dict[str, str]:
    """从库里实查锚点。挑**回合数最多**的那场/队/图，保证题跑得出数。

    `prefer` 是上一轮的锚点：若它在库里**仍然存在**就沿用，避免换库后
    题面无谓地全变（比如重跑管线时）。
    """
    path = Path(db_path) if db_path else _db_path()
    out = dict(FALLBACK)
    prefer = prefer or {}
    try:
        import duckdb
        con = duckdb.connect(str(path), read_only=True)
    except Exception:
        return out
    try:
        # ── series：回合数最多的那场 ──
        try:
            row = con.execute(
                "SELECT CAST(series_id AS VARCHAR) FROM rounds "
                "GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        except Exception:
            row = None
        if not row:                       # 库里没有 rounds：退回 series 表随便挑一个
            try:
                row = con.execute(
                    "SELECT CAST(series_id AS VARCHAR) FROM series LIMIT 1").fetchone()
            except Exception:
                row = None
        if row:
            sid = str(row[0])
            if str(prefer.get("series") or "") in _series_ids(con):
                sid = str(prefer["series"])
            out["series"] = sid

        # ── team / map：都限定在这场比赛里 ──
        sid = out["series"]
        # ⚠️ team 那条 SQL 里有**两个** `?`（winning / losing 各一次），
        # 只传一个参数会抛错并静默退回兜底值（实测挑出 Cloud9 —— rib 库里
        # 根本没这个队）。按占位符个数配参数。
        for key, sql, args in (
            ("team",
             "SELECT t FROM (SELECT winning_team_name AS t FROM rounds "
             "WHERE CAST(series_id AS VARCHAR)=? UNION ALL "
             "SELECT losing_team_name FROM rounds "
             "WHERE CAST(series_id AS VARCHAR)=?) "
             "WHERE t IS NOT NULL AND t<>'' GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1",
             [sid, sid]),
            ("map",
             "SELECT map_name FROM rounds WHERE CAST(series_id AS VARCHAR)=? "
             "AND map_name IS NOT NULL AND map_name<>'' "
             "GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1",
             [sid]),
        ):
            try:
                r = con.execute(sql, args).fetchone()
            except Exception:
                r = None
            if r and str(r[0]):
                out[key] = str(r[0])
            elif prefer.get(key):
                out[key] = str(prefer[key])
    finally:
        try:
            con.close()
        except Exception:
            pass
    return out


def _series_ids(con) -> set[str]:
    try:
        return {str(r[0]) for r in con.execute(
            "SELECT DISTINCT CAST(series_id AS VARCHAR) FROM rounds").fetchall()}
    except Exception:
        return set()


def refresh(db_path: Path | str | None = None) -> dict[str, str]:
    """重算锚点并写回 db_config.json（保留 db_path 等其他字段）。"""
    cfg = _cfg()
    path = Path(db_path) if db_path else _db_path()
    anchor = pick(path, prefer=cfg.get("anchor") or {})
    cfg["anchor"] = anchor
    if db_path:
        cfg["db_path"] = str(path)
    DB_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    return anchor


def get() -> dict[str, str]:
    """当前锚点。优先读配置（快、不连库），读不到就现场实查。"""
    a = _cfg().get("anchor")
    if isinstance(a, dict) and a.get("series") and a.get("team"):
        return {"series": str(a["series"]), "team": str(a["team"]),
                "map": str(a.get("map") or "")}
    return pick()


def series() -> str:
    return get()["series"]


def team() -> str:
    return get()["team"]


def map_name() -> str:
    return get()["map"]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="题目锚点（跟着库走）")
    ap.add_argument("--refresh", action="store_true", help="重算并写回 db_config.json")
    ap.add_argument("--db", default="", help="对哪个库算（默认当前库）")
    a = ap.parse_args()
    if a.refresh:
        r = refresh(a.db or None)
        print(f"锚点已重算并写入 db_config.json：{json.dumps(r, ensure_ascii=False)}")
    else:
        print(json.dumps(get(), ensure_ascii=False))
