#!/usr/bin/env python3
"""数据源里**实际有哪些实体**（赛事 / 团队 / 地图 / 选手）—— 实查，不手写。

为什么要有这个模块
------------------
「学科」层要绑具体实体（赛事号 / 团队名 / 选手名），那就必须知道库里到底
有哪些值可填。手写一张清单是最容易腐坏的东西：换一次库就全错。所以这里是
**连库实查**，查不到就返回空并说明原因，绝不猜。

为什么不在 `knowledge_graph.py` 里做
------------------------------------
图谱必须保持"只 import 标准库、不连库"的轻量性质（面板要秒开）。连库的活
放这里，落盘成 `entity_catalog.json`，图谱和其它模块只读缓存。

跑法
----
    python mve/entity_catalog.py            # 刷新并打印
    python mve/entity_catalog.py --json     # 只输出 JSON
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DB_CONFIG = HERE / "db_config.json"
CATALOG = HERE / "entity_catalog.json"

# 每张表怎么取 (实体类别, 取值列, 附带展示列)
#
# ⚠️ `player` 有**两个候选表**，按顺序试到有值为止：
#   `agg_player_series_stats` 是 VLML 从**逐事件**派生的（GRID 源有）；
#   `ext_player_game_stats` 是 vlr.gg / rib.gg 给的**聚合级**统计。
#   接了公开源之后没有 kill 事件，前者就是空的 —— 这时要退到后者，
#   否则"选手"这一栏会显示 0 个，而实际上选手数据是有的（只是粒度不同）。
#
# rib.gg 那两张（`ext_player_round_stats` / `ext_player_side_stats`）只在
# `vlml_rib.duckdb` 里存在，放在最后当兜底：库里没有这张表会记 warning，
# 不影响前面命中。同一类别**先到先得**，所以顺序不能乱。
PROBES: list[tuple[str, str, str, list[str]]] = [
    # 类别       表名                         取值列          附带
    ("series", "series", "series_id", ["tournament_name"]),
    ("team", "agg_team_series_stats", "team_name", []),
    ("team", "ext_game_economy", "team_name", []),
    ("player", "agg_player_series_stats", "player_name", []),
    ("player", "ext_player_game_stats", "player_name", []),
    ("player", "ext_player_round_stats", "player_name", []),
    ("map", "games", "map_name", []),
]

KIND_LABEL = {"series": "赛事", "team": "团队", "map": "地图",
              "player": "选手"}


def _db_path() -> Path:
    """当前连的是哪个库 —— `db_config.json` 可切换（见 vlml_env.py:52）。"""
    try:
        cfg = json.loads(DB_CONFIG.read_text(encoding="utf-8"))
        p = str((cfg or {}).get("db_path") or "").strip()
        if p:
            return Path(p)
    except Exception:
        pass
    return ROOT / "vlml" / "data" / "vlml_events.duckdb"


def refresh() -> dict:
    """连库实查所有实体类别的可选值。失败就返回带 error 的空壳，不猜。"""
    path = _db_path()
    out: dict = {"db_path": str(path), "entities": {}, "counts": {}}
    try:
        import duckdb
    except Exception as exc:
        out["error"] = f"duckdb 不可用：{type(exc).__name__}: {exc}"[:200]
        return out
    if not path.exists():
        out["error"] = f"库文件不存在：{path}"
        return out
    try:
        con = duckdb.connect(str(path), read_only=True)
    except Exception as exc:
        out["error"] = f"连库失败：{type(exc).__name__}: {exc}"[:200]
        return out
    try:
        for kind, table, col, extra in PROBES:
            # 同一类别有多个候选表时，**先到先得**：前一个表里查到了就不换
            # （见 PROBES 里 player 的两档回退）。
            if out["entities"].get(kind):
                continue
            try:
                cols = ", ".join([col, *extra])
                rows = con.execute(
                    f'SELECT DISTINCT {cols} FROM "{table}" '
                    f'WHERE "{col}" IS NOT NULL ORDER BY 1').fetchall()
            except Exception as exc:
                out.setdefault("warnings", []).append(
                    f"{table}.{col} 查不到：{type(exc).__name__}: {str(exc)[:80]}")
                continue
            items = []
            seen: set[str] = set()
            for r in rows:
                v = str(r[0])
                # DISTINCT 带上附带列时同一个值会出现多次（实测：队伍 12 个
                # 被查成 16 个、地图 21 张查成 7 张的重复）—— 按取值去重。
                if not v or v in seen:
                    continue
                seen.add(v)
                d = {"value": v}
                for i, e in enumerate(extra, start=1):
                    if i < len(r) and r[i] is not None:
                        d[e] = str(r[i])
                items.append(d)
            out["entities"][kind] = items
            out["counts"][kind] = len(items)
            out.setdefault("sources", {})[kind] = table
    finally:
        con.close()
    return out


def catalog(*, force: bool = False) -> dict:
    """读缓存；没有或 `force` 就先刷新。"""
    if not force and CATALOG.exists():
        try:
            cached = json.loads(CATALOG.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("entities"):
                return cached
        except Exception:
            pass
    data = refresh()
    try:
        CATALOG.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    except Exception:
        pass
    return data


def options(kind: str) -> list[str]:
    """某个学科类别下**可填入的具体值**（给 UI 下拉 / 出题器用）。"""
    return [str(it.get("value") or "")
            for it in (catalog().get("entities") or {}).get(kind, [])
            if it.get("value")]


def _main() -> int:
    as_json = "--json" in sys.argv
    data = catalog(force=True)
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    if data.get("error"):
        print(f"❌ {data['error']}")
        return 1
    print(f"数据源：{data['db_path']}")
    for kind, items in (data.get("entities") or {}).items():
        vals = [str(it.get("value")) for it in items]
        print(f"\n{KIND_LABEL.get(kind, kind)}（{kind}）· {len(vals)} 个")
        print("  " + ("、".join(vals) if vals else "（无）"))
    for w in data.get("warnings") or []:
        print(f"⚠ {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
