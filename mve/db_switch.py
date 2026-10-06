#!/usr/bin/env python3
"""数据库切换：把 VLML 指向另一个 .duckdb 文件。

为什么要有这个：题目注册表里的 SERIES / 队伍名是**写死**的（tasks.py 的
SERIES='2843069'、C9='Cloud9'），换一个库这些常量就全失效。
所以本模块不假装"换库即换题"，而是：

  1. 换库后**立即体检**：能打开吗、几张表、几条事件、
     原题目依赖的那场 series 还在不在。
  2. 体检结果如实报给面板，由人决定要不要顺带清档重来。

体检必须做在切换**之前**——切过去才发现是空库/错库，人已经不知道刚才的数据
去哪了。所以流程是：先 probe(path) 验货 → 人确认 → 才写 db_config.json。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "db_config.json"
DEFAULT_DB = ROOT.parent / "vlml" / "data" / "vlml_events.duckdb"


def current_path() -> str:
    try:
        obj = json.loads(CONFIG.read_text(encoding="utf-8"))
        p = str((obj or {}).get("db_path") or "")
    except Exception:
        p = ""
    return p or str(DEFAULT_DB)


def is_default() -> bool:
    try:
        return Path(current_path()).resolve() == DEFAULT_DB.resolve()
    except Exception:
        return True


def write(path: str) -> dict[str, Any]:
    """切换数据库。**只写配置**，不校验 —— 校验走 probe()。"""
    p = str(path or "").strip()
    CONFIG.write_text(
        json.dumps({"db_path": p}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"ok": True, "db_path": p or str(DEFAULT_DB),
            "is_default": not bool(p)}


def probe(path: str) -> dict[str, Any]:
    """验货：这个库能不能用、里面有什么。切换前必须先跑这个。"""
    p = str(path or "").strip() or str(DEFAULT_DB)
    out: dict[str, Any] = {"path": p, "ok": False, "is_default": not bool(path)}
    f = Path(p)
    if not f.exists():
        out["error"] = "文件不存在"
        return out
    if f.suffix.lower() not in (".duckdb", ".db", ".ddb"):
        out["error"] = f"扩展名 {f.suffix} 不像 DuckDB 库（.duckdb / .db / .ddb）"
        return out

    try:
        import duckdb
        con = duckdb.connect(str(f), read_only=True)
    except Exception as e:
        out["error"] = f"打不开：{type(e).__name__}: {e}"[:200]
        return out

    try:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY table_name").fetchall()]
        out["tables"] = tables
        # 事件量：不同 schema 下表名不同，两个都试
        events = None
        for t in ("base_events", "events"):
            if t in tables:
                try:
                    events = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    break
                except Exception:
                    continue
        out["events"] = events

        # 关键的兼容性检查：题目依赖的那场 series 还在不在
        series_ids: list[str] = []
        try:
            from tasks import SERIES
            want = SERIES
        except Exception:
            want = ""
        for t in tables:
            try:
                cols = [r[0] for r in con.execute(
                    f"SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name='{t}'").fetchall()]
                if "series_id" not in cols:
                    continue
                rows = con.execute(
                    f"SELECT DISTINCT CAST(series_id AS VARCHAR) FROM {t} LIMIT 20"
                ).fetchall()
                for r in rows:
                    v = str(r[0])
                    if v and v not in series_ids:
                        series_ids.append(v)
            except Exception:
                continue
        out["series_ids"] = series_ids[:10]
        out["ok"] = bool(tables)
        out["warning"] = ""
        if want and series_ids and want not in series_ids:
            out["warning"] = (
                f"原题依赖的 series {want} 不在这个库里（现有 {', '.join(series_ids[:5])}）"
                " —— 换库后题目里的 series/队伍常量要重配，否则所有题都查不到数据"
            )
        elif want and not series_ids:
            out["warning"] = "没找到任何 series_id 列，无法判断题目的 series 是否还在"
    finally:
        try:
            con.close()
        except Exception:
            pass
    return out


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        print(json.dumps(probe(sys.argv[2] if len(sys.argv) > 2 else ""),
                         ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "set":
        print(json.dumps(write(sys.argv[2] if len(sys.argv) > 2 else ""),
                         ensure_ascii=False))
    else:
        print(json.dumps({"current": current_path(), "is_default": is_default(),
                          "probe": probe("")}, ensure_ascii=False, indent=2))
