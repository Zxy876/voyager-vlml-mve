#!/usr/bin/env python3
"""把 vlr.gg 接进 VLML：抓 → 落成 GRID 形态 jsonl → 跑原管线入库。

用法
----
    # 1) 看看现在站点上有哪些赛事（不需要 key）
    python mve/vlml_source/ingest.py --discover

    # 2) 抓一个赛事的前 8 场，落成 jsonl（不入库）
    python mve/vlml_source/ingest.py --event 2977 --max 8

    # 3) 抓 + 入库到**新库**（不动 GRID 切片 vlml_events.duckdb）
    python mve/vlml_source/ingest.py --event 2977 --max 8 \
        --db vlml/data/vlml_vlr.duckdb

    # 4) 只入库（jsonl 已经落好了）
    python mve/vlml_source/ingest.py --only-pipeline \
        --db vlml/data/vlml_vlr.duckdb

为什么默认入库到**新库**
------------------------
GRID 切片里是有逐事件流的（kill/伤害/技能/坐标），接 vlr 之后这些没有。
两个源的数据粒度不同，混在一张表里会让"这列为什么一半是空"说不清。
所以默认 `vlml_vlr.duckdb`，用 `mve/db_config.json` 或者面板上的
「切换数据库」在两个源之间切 —— **旧库保持原样，随时能切回去**。

落的表
------
VLML 原管线（`run_pipeline`）：series / games / rounds / base_events + agg_* 派生表
本脚本额外建一张：
    ext_player_game_stats —— vlr 给的**聚合级**选手统计（K/D/A、ACS、ADR、
    KAST、HS%、FK/FD）。它**不进 base_events**：那些数字是整图的汇总，
    不是逐事件，塞进事件流会被下游当成"某一次击杀"来读。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
MVE = HERE.parent
ROOT = MVE.parent
sys.path.insert(0, str(HERE))

import vlr_client as vc                                   # noqa: E402
import vlr_to_grid as vtg                                 # noqa: E402

SCRIPTS = ROOT / "vlml" / "database" / "scripts"
DEFAULT_DB = ROOT / "vlml" / "data" / "vlml_vlr.duckdb"

EXT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ext_player_game_stats (
    series_id    VARCHAR,
    game_id      VARCHAR,
    map_name     VARCHAR,
    player_name  VARCHAR,
    team_name    VARCHAR,
    rating       DOUBLE,
    acs          DOUBLE,
    kills        INTEGER,
    deaths       INTEGER,
    assists      INTEGER,
    kast         DOUBLE,
    adr          DOUBLE,
    hs_pct       DOUBLE,
    fk           INTEGER,
    fd           INTEGER,
    source       VARCHAR,
    ingested_at  TIMESTAMP
)
"""


def _num(v: str) -> float | None:
    s = str(v or "").strip().replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _int(v: str) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


def write_ext_stats(matches: list[dict], db_path: Path) -> int:
    """vlr 的聚合级选手统计 → `ext_player_game_stats`。返回写入行数。"""
    import duckdb

    rows: list[tuple] = []
    for m in matches:
        series_id = str(m.get("series_id") or "")
        teams = list(m.get("teams") or [])
        for g in (m.get("games") or []):
            gid = str(g.get("game_id") or "")
            for p in (m.get("player_stats") or []):
                if str(p.get("map_name") or "") != str(g.get("map_name") or ""):
                    continue
                rows.append((
                    series_id, gid, str(p.get("map_name") or ""),
                    str(p.get("player_name") or ""),
                    vtg._team_of(str(p.get("team_tag") or ""), teams),
                    _num(p.get("rating")), _num(p.get("acs")),
                    _int(p.get("kills")), _int(p.get("deaths")),
                    _int(p.get("assists")), _num(p.get("kast")),
                    _num(p.get("adr")), _num(p.get("hs_pct")),
                    _int(p.get("fk")), _int(p.get("fd")),
                    "vlr.gg", datetime.now(),
                ))
    if not rows:
        return 0
    con = duckdb.connect(str(db_path))
    try:
        con.execute(EXT_TABLE_SQL)
        con.execute(f"DELETE FROM ext_player_game_stats "
                    f"WHERE series_id IN ({','.join('?' * len({r[0] for r in rows}))})",
                    [*{r[0] for r in rows}])
        con.executemany(
            "INSERT INTO ext_player_game_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
    finally:
        con.close()
    return len(rows)


def run_pipeline(db_path: Path, year: int | None = None) -> None:
    """跑 VLML 原管线：建表 → 载 jsonl → 跑派生变换 → 校验。

    一行不改地复用 `database/scripts/orchestration/run_pipeline.py` ——
    这正是"换数据源只换下载层"的意义。
    """
    sys.path.insert(0, str(SCRIPTS))
    from orchestration.run_pipeline import run_pipeline as _run
    _run(db_path=str(db_path), year=year)


def main() -> int:
    ap = argparse.ArgumentParser(description="把 vlr.gg 接进 VLML")
    ap.add_argument("--discover", action="store_true",
                    help="列出站点上的赛事（不抓比赛）")
    ap.add_argument("--event", action="append", default=[],
                    help="赛事 id，可多次给（如 --event 2977 --event 2976）")
    ap.add_argument("--max", type=int, default=8,
                    help="每个赛事最多抓几场（默认 8）")
    ap.add_argument("--db", default=str(DEFAULT_DB),
                    help=f"入库到哪个库（默认 {DEFAULT_DB}）")
    ap.add_argument("--raw-dir", default=str(vtg.RAW_DEFAULT),
                    help="jsonl 落盘根目录")
    ap.add_argument("--sleep", type=float, default=vc.DEFAULT_SLEEP,
                    help=f"每次请求间隔秒数（默认 {vc.DEFAULT_SLEEP}，别调太快）")
    ap.add_argument("--only-pipeline", action="store_true",
                    help="不抓，只把已落盘的 jsonl 入库")
    ap.add_argument("--no-pipeline", action="store_true",
                    help="只抓/落盘，不入库")
    args = ap.parse_args()

    raw_dir = Path(args.raw_dir)

    if args.discover or not (args.event or args.only_pipeline):
        print("站点上的赛事（前 20 个）：")
        for e in vc.list_events(sleep=args.sleep)[:20]:
            print(f"  {e['event_id']:>6}  {e['slug']}")
        if not args.event and not args.only_pipeline:
            print("\n给一个 --event <id> 再跑（比如 --event 2977）")
            return 0

    matches: list[dict] = []
    if not args.only_pipeline:
        paths: list[str] = []
        for eid in args.event:
            found = vc.list_matches(eid, limit=args.max, sleep=args.sleep)
            print(f"赛事 {eid}：{len(found)} 场")
            paths += [m["path"] for m in found]
        for i, p in enumerate(paths, 1):
            try:
                m = vc.fetch_match(p, sleep=args.sleep)
            except vc.FetchError as exc:
                print(f"  [{i}/{len(paths)}] ❌ {p} → {exc}")
                continue
            if not m.get("games"):
                print(f"  [{i}/{len(paths)}] ⏭️  {p} 没有回合数据（可能未打/未公布），跳过")
                continue
            out = vtg.write_jsonl(m, raw_dir=raw_dir)
            rounds = sum(len(g.get("rounds") or []) for g in m["games"])
            print(f"  [{i}/{len(paths)}] ✅ {m['teams'][0]} vs {m['teams'][1]} "
                  f"· {len(m['games'])} 图 / {rounds} 回合 → {out.name}")
            matches.append(m)

    if args.no_pipeline:
        return 0

    db_path = Path(args.db)
    print(f"\n入库到 {db_path} …")
    run_pipeline(db_path)
    n = write_ext_stats(matches, db_path) if matches else 0
    if n:
        print(f"选手聚合统计写入 ext_player_game_stats：{n} 行")
    print("\n接完了。下一步：")
    print(f"  1) 刷新学科可填值：python mve/entity_catalog.py")
    print(f"  2) 同步进登记册：  python mve/subjects.py --sync")
    print(f"  3) 面板切到这个库（或写 mve/db_config.json 的 db_path）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
