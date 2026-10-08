#!/usr/bin/env python3
"""把公开源（vlr.gg / rib.gg）接进 VLML：抓 → 落成 GRID 形态 jsonl → 跑原管线入库。

用法
----
    # 1) 看看各站点上有哪些赛事（不需要 key）
    python mve/vlml_source/ingest.py --discover            # vlr
    python mve/vlml_source/ingest.py --discover --source rib

    # 2) 抓 rib 某赛事的前 3 场，落成 jsonl（不入库）
    python mve/vlml_source/ingest.py --source rib --event 151 --max 3 --no-pipeline

    # 3) 抓 + 入库
    python mve/vlml_source/ingest.py --source rib --event 151 --max 3

    # 4) 只入库（jsonl 已经落好了）
    python mve/vlml_source/ingest.py --source rib --only-pipeline

为什么**一个源一个库**
----------------------
两个源产出的事件子集是一样的（回合级，**都没有 kill 事件**），混在一张表里
不会"某列一半是空" —— 但 `knowledge_graph.data_source()` 是**按库文件名**判源的
（`vlml_vlr` → `vlr.gg`），而面板/图谱要靠它说清"这数据哪来的"。
混库会让这个字段只能二选一，等于丢信息。所以：

    vlml_vlr.duckdb ← vlr.gg
    vlml_rib.duckdb ← rib.gg

在面板上「切换数据库」即可在两源之间切；旧 GRID 库 `vlml_events.duckdb` 原样保留。

⚠️ 因此 raw 目录也必须分开
--------------------------
`load_data.py:47` 把 raw 目录**写死**成 `vlml/data/raw_events`（不可配）。
若两个源的 jsonl 堆在同一棵树里，跑 rib 的库会把 vlr 的 series 一起吃进去。

做法：**每个源一个暂存目录**，`raw_events` 是指向"当前源"的**软链**：

    vlml/data/raw_events       → raw_events_rib   （软链，管线只认这个路径）
    vlml/data/raw_events_vlr/  ← vlr 的 jsonl + _parsed 缓存
    vlml/data/raw_events_rib/  ← rib 的 jsonl + _parsed 缓存

第一次跑会把已有的 `raw_events/`（真实目录）整目录改名为 `raw_events_vlr/`，
并打印提示 —— 都是脚本自己抓下来的可再生成数据，且 `load_data` 按 series_id
幂等，重跑不会重复入库。

落的表
------
VLML 原管线（`run_pipeline`，一行没改）：series / games / rounds / base_events + agg_*

本脚本额外建（**都不进 base_events** —— 那些是聚合/逐回合统计，塞进事件流会被
下游当成"某一次击杀"来读）：

    ext_player_game_stats    两源都有：整图聚合选手统计（K/D/A、ACS、ADR、KAST、FK/FD）
    ext_player_round_stats   rib 独有：每回合 × 每选手（含 first_kill 首血标记）
    ext_round_economy        rib 独有：每回合 × 每队（bank / loadout / buy_tier）
    ext_game_economy         rib 独有：每图经济汇总（手枪局 / eco / 半买 / 全买 胜率）
    ext_player_side_stats    rib 独有：攻防拆分后的选手统计
"""

from __future__ import annotations

import argparse
import json
import shutil
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
import rib_client as rb                                   # noqa: E402

DATA_DIR = ROOT / "vlml" / "data"

# 每个源：客户端 / 默认库 / 显示名 / series_id 前缀
# ⚠️ 前缀的事：rib 的 matchId 是 4 位数（1027），vlr 是 6 位数（706349），
# 同一棵 raw 树里理论上不撞；但两个库分开之后本来就不会混，所以**不加前缀**，
# 免得 series_id 变成 "rib_1027" 这种和站点对不上的怪值。
SOURCES = {
    "vlr": {"mod": vc, "db": "vlml_vlr.duckdb", "label": "vlr.gg", "prefix": ""},
    "rib": {"mod": rb, "db": "vlml_rib.duckdb", "label": "rib.gg", "prefix": ""},
}
DEFAULT_SOURCE = "vlr"

SCRIPTS = ROOT / "vlml" / "database" / "scripts"

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

EXT_ROUND_STATS_SQL = """
CREATE TABLE IF NOT EXISTS ext_player_round_stats (
    series_id    VARCHAR,
    game_id      VARCHAR,
    map_name     VARCHAR,
    round_number INTEGER,
    player_name  VARCHAR,
    team_name    VARCHAR,
    side         VARCHAR,
    agent        VARCHAR,
    kills        INTEGER,
    deaths       INTEGER,
    assists      INTEGER,
    damage       INTEGER,
    acs          INTEGER,
    hs_pct       DOUBLE,
    alive        BOOLEAN,
    first_kill   BOOLEAN,
    source       VARCHAR,
    ingested_at  TIMESTAMP
)
"""

EXT_ROUND_ECON_SQL = """
CREATE TABLE IF NOT EXISTS ext_round_economy (
    series_id    VARCHAR,
    game_id      VARCHAR,
    map_name     VARCHAR,
    round_number INTEGER,
    team_name    VARCHAR,
    bank         INTEGER,
    loadout      INTEGER,
    buy_tier     VARCHAR,
    source       VARCHAR,
    ingested_at  TIMESTAMP
)
"""

EXT_GAME_ECON_SQL = """
CREATE TABLE IF NOT EXISTS ext_game_economy (
    series_id       VARCHAR,
    game_id         VARCHAR,
    map_name        VARCHAR,
    team_name       VARCHAR,
    pistol_won      INTEGER,
    eco_total       INTEGER,
    eco_won         INTEGER,
    semi_eco_total  INTEGER,
    semi_eco_won    INTEGER,
    semi_buy_total  INTEGER,
    semi_buy_won    INTEGER,
    full_buy_total  INTEGER,
    full_buy_won    INTEGER,
    source          VARCHAR,
    ingested_at     TIMESTAMP
)
"""

EXT_SIDE_STATS_SQL = """
CREATE TABLE IF NOT EXISTS ext_player_side_stats (
    series_id    VARCHAR,
    game_id      VARCHAR,
    map_name     VARCHAR,
    side         VARCHAR,
    player_name  VARCHAR,
    team_name    VARCHAR,
    agent        VARCHAR,
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


def _num(v) -> float | None:
    s = str(v if v is not None else "").strip().replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _int(v) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


def _parsed_dir(raw_dir: Path) -> Path:
    """解析结果缓存目录。

    为什么要缓存：选手统计（`ext_player_game_stats` 等）来自**解析结果**，
    而 jsonl 里只有事件。第一次跑 `ingest` 抓完就存一份，之后 `--only-pipeline`
    重入库时不用再抓一遍站点（实测踩过：只跑 pipeline 时选手表是空的）。
    """
    return raw_dir / "_parsed"


def _save_parsed(m: dict, raw_dir: Path) -> None:
    d = _parsed_dir(raw_dir)
    d.mkdir(parents=True, exist_ok=True)
    sid = str(m.get("series_id") or "")
    if sid:
        (d / f"{sid}.json").write_text(
            json.dumps(m, ensure_ascii=False), encoding="utf-8")


def _load_parsed(raw_dir: Path) -> list[dict]:
    d = _parsed_dir(raw_dir)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def stage_dir(source: str) -> Path:
    """当前源的暂存目录，并把 `data/raw_events` 软链指过去。

    `load_data.py:47` 只认 `data/raw_events`，所以要么软链、要么就只能共用
    一棵树。用软链 = 每个源的数据物理隔离，跑哪个库就看到哪个源的文件。
    """
    stage = DATA_DIR / f"raw_events_{source}"
    link = DATA_DIR / "raw_events"

    # 第一次跑：`raw_events` 还是**真实目录**（里面是早先抓的 vlr jsonl）。
    # 整目录改名成 `raw_events_vlr`，再建软链。
    if link.exists() and not link.is_symlink():
        legacy = DATA_DIR / "raw_events_vlr"
        if not legacy.exists():
            print(f"⚠️  第一次按源分目录：把已有的 raw_events/ 改名为 raw_events_vlr/")
            shutil.move(str(link), str(legacy))
        else:
            # 两边都有了（不该发生）：不删用户的东西，直接报错让人看一眼
            raise SystemExit(
                f"❌ {link} 和 {legacy} 都是真实目录，不知道该留哪个。"
                f"手动合并后再跑。")

    stage.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if link.resolve() != stage.resolve():
            link.unlink()
            link.symlink_to(stage.name)
    elif not link.exists():
        link.symlink_to(stage.name)
    return stage


def _game_ids(db_path: Path, sids: list[str]) -> dict[tuple[str, int], str]:
    """(series_id, game_number) → `games.game_id`。

    ⚠️ 实测踩到的大坑：VLML 的 db_loader **自己生成** game_id
    （`{series_id}_game_{n}`，见 games 表：1057_game_1），**不看**我们塞进
    `platformGameId` 的源站 id。于是 ext 表里若存源站那套（rib: `map-392a29c1`
    / vlr: `275080`），跟 games / rounds **一行都 join 不上** —— 两个源都中招。
    落 ext 表时反查 games 表换成 VLML 的 id；查不到再退回拼接（已验证
    `game_number` == 我们建的 `sequence`，map 顺序也一致）。
    """
    import duckdb
    if not sids or not db_path.exists():
        return {}
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            f"SELECT series_id, game_number, game_id FROM games "
            f"WHERE series_id IN ({','.join('?' * len(sids))})", sids).fetchall()
    except Exception:
        return {}
    finally:
        con.close()
    return {(str(a), int(b)): str(c) for a, b, c in rows}


def repair_game_ids(db_path: Path) -> int:
    """把已入库的 ext_* 表的 game_id 修成 `games` 表那套。

    按 (series_id, map_name) 对齐 —— Bo 系列里同一张图不会打两次（地图 veto），
    所以这个键是唯一的。用来修**早先写进去的老数据**（vlr 库就是中招的那个）。
    """
    import duckdb
    if not db_path.exists():
        print(f"❌ 没有这个库：{db_path}")
        return 0
    con = duckdb.connect(str(db_path))
    total = 0
    try:
        tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()
                  if r[0].startswith("ext_")]
        for t in tables:
            cols = [c[0] for c in con.execute(f"DESCRIBE {t}").fetchall()]
            if not {"game_id", "series_id", "map_name"} <= set(cols):
                continue
            n = con.execute(f"""
                UPDATE {t} e SET game_id = g.game_id
                FROM (SELECT DISTINCT series_id, map_name, game_id FROM games) g
                WHERE e.series_id = g.series_id AND e.map_name = g.map_name
                  AND e.game_id <> g.game_id
            """).fetchall()
            changed = con.execute(f"""
                SELECT COUNT(*) FROM {t} e JOIN games g
                  ON e.series_id=g.series_id AND e.map_name=g.map_name
                 WHERE e.game_id = g.game_id
            """).fetchone()[0]
            total_rows = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t}: {changed}/{total_rows} 行 game_id 现在能对上 games 表")
            total += changed
    finally:
        con.close()
    return total


def _write_table(db_path: Path, sql: str, table: str,
                 rows: list[tuple]) -> int:
    """幂等写一张 ext 表：先删这批 series_id 的行，再整批插入。"""
    if not rows:
        return 0
    import duckdb
    sids = sorted({str(r[0]) for r in rows})
    con = duckdb.connect(str(db_path))
    try:
        con.execute(sql)
        con.execute(f"DELETE FROM {table} "
                    f"WHERE series_id IN ({','.join('?' * len(sids))})", sids)
        con.executemany(
            f"INSERT INTO {table} VALUES ({','.join('?' * len(rows[0]))})", rows)
    finally:
        con.close()
    return len(rows)


def write_agg_stats(matches: list[dict], db_path: Path, source: str) -> int:
    """整图聚合选手统计 → `ext_player_game_stats`（vlr / rib 两源共用）。"""
    gids = _game_ids(db_path, sorted({str(m.get("series_id") or "")
                                      for m in matches}))
    rows: list[tuple] = []
    for m in matches:
        series_id = str(m.get("series_id") or "")
        teams = list(m.get("teams") or [])
        for g in (m.get("games") or []):
            seq = int(g.get("sequence") or 0)
            gid = gids.get((series_id, seq)) or f"{series_id}_game_{seq}"
            for p in (m.get("player_stats") or []):
                if str(p.get("map_name") or "") != str(g.get("map_name") or ""):
                    continue
                rows.append((
                    series_id, gid, str(p.get("map_name") or ""),
                    str(p.get("player_name") or ""),
                    # rib 的 team_tag 直接就是队全名；vlr 给的是短标签（LEV），
                    # 靠 _team_of 去音标比对回全名（LEVIATÁN）
                    str(p.get("team_name") or "") or
                    vtg._team_of(str(p.get("team_tag") or ""), teams),
                    _num(p.get("rating")), _num(p.get("acs")),
                    _int(p.get("kills")), _int(p.get("deaths")),
                    _int(p.get("assists")), _num(p.get("kast")),
                    _num(p.get("adr")), _num(p.get("hs_pct")),
                    _int(p.get("fk")), _int(p.get("fd")),
                    source, datetime.now(),
                ))
    return _write_table(db_path, EXT_TABLE_SQL, "ext_player_game_stats", rows)


def write_rib_tables(matches: list[dict], db_path: Path) -> dict[str, int]:
    """rib 独有三张表：逐回合选手 / 逐回合经济 / 每图经济汇总 / 攻防拆分。"""
    now = datetime.now()
    sids = sorted({str(m.get("series_id") or "") for m in matches})
    gids = _game_ids(db_path, sids)
    prs: list[tuple] = []
    econ: list[tuple] = []
    gecon: list[tuple] = []
    side: list[tuple] = []

    for m in matches:
        sid = str(m.get("series_id") or "")
        # rib 的 game_id 是它自己的 map uuid（map-392a29c1），先换成 VLML 那套
        seq_of = {str(g.get("game_id") or ""): int(g.get("sequence") or 0)
                  for g in (m.get("games") or [])}
        def _gid(raw: str) -> str:
            seq = seq_of.get(str(raw or ""), 0)
            return gids.get((sid, seq)) or f"{sid}_game_{seq}"

        for r in (m.get("player_round_stats") or []):
            prs.append((sid, _gid(r.get("game_id")),
                        str(r.get("map_name") or ""), _int(r.get("round_number")),
                        str(r.get("player_name") or ""), str(r.get("team_name") or ""),
                        str(r.get("side") or ""), str(r.get("agent") or ""),
                        _int(r.get("kills")), _int(r.get("deaths")),
                        _int(r.get("assists")), _int(r.get("damage")),
                        _int(r.get("acs")), _num(r.get("hs_pct")),
                        bool(r.get("alive")), bool(r.get("first_kill")),
                        "rib.gg", now))
        for r in (m.get("round_economy") or []):
            econ.append((sid, _gid(r.get("game_id")),
                         str(r.get("map_name") or ""), _int(r.get("round_number")),
                         str(r.get("team_name") or ""), _int(r.get("bank")),
                         _int(r.get("loadout")), str(r.get("buy_tier") or ""),
                         "rib.gg", now))
        for r in (m.get("game_economy") or []):
            gecon.append((sid, _gid(r.get("game_id")),
                          str(r.get("map_name") or ""), str(r.get("team_name") or ""),
                          _int(r.get("pistol_won")), _int(r.get("eco_total")),
                          _int(r.get("eco_won")), _int(r.get("semi_eco_total")),
                          _int(r.get("semi_eco_won")), _int(r.get("semi_buy_total")),
                          _int(r.get("semi_buy_won")), _int(r.get("full_buy_total")),
                          _int(r.get("full_buy_won")), "rib.gg", now))
        for s in (m.get("side_stats") or []):
            side.append((sid, _gid(s.get("game_id")),
                         str(s.get("map_name") or ""), str(s.get("side") or ""),
                         str(s.get("player_name") or ""), str(s.get("team_name") or ""),
                         str(s.get("agent") or ""), _num(s.get("rating")),
                         _num(s.get("acs")), _int(s.get("kills")),
                         _int(s.get("deaths")), _int(s.get("assists")),
                         _num(s.get("kast")), _num(s.get("adr")), _num(s.get("hs_pct")),
                         _int(s.get("fk")), _int(s.get("fd")), "rib.gg", now))

    return {
        "ext_player_round_stats":
            _write_table(db_path, EXT_ROUND_STATS_SQL, "ext_player_round_stats", prs),
        "ext_round_economy":
            _write_table(db_path, EXT_ROUND_ECON_SQL, "ext_round_economy", econ),
        "ext_game_economy":
            _write_table(db_path, EXT_GAME_ECON_SQL, "ext_game_economy", gecon),
        "ext_player_side_stats":
            _write_table(db_path, EXT_SIDE_STATS_SQL, "ext_player_side_stats", side),
    }


def run_pipeline(db_path: Path, year: int | None = None) -> None:
    """跑 VLML 原管线：建表 → 载 jsonl → 跑派生变换 → 校验。

    一行不改地复用 `database/scripts/orchestration/run_pipeline.py` ——
    这正是"换数据源只换下载层"的意义。
    """
    sys.path.insert(0, str(SCRIPTS))
    from orchestration.run_pipeline import run_pipeline as _run
    _run(db_path=str(db_path), year=year)


def _discover(src: dict, sleep: float) -> None:
    print(f"{src['label']} 上的赛事（前 20 个）：")
    if hasattr(src["mod"], "list_events"):
        for e in src["mod"].list_events(sleep=sleep)[:20]:
            print(f"  {e['event_id']:>6}  {e['slug']}")
    else:
        print("  （这个源没有赛事列表页，直接给 --event <id>）")


def main() -> int:
    ap = argparse.ArgumentParser(description="把公开源接进 VLML")
    ap.add_argument("--source", default=DEFAULT_SOURCE, choices=sorted(SOURCES),
                    help=f"用哪个源（默认 {DEFAULT_SOURCE}）")
    ap.add_argument("--discover", action="store_true",
                    help="列出站点上的赛事（不抓比赛）")
    ap.add_argument("--event", action="append", default=[],
                    help="赛事 id，可多次给（如 --event 151 --event 104）")
    ap.add_argument("--match", action="append", default=[],
                    help="直接给比赛路径（如 --match /matches/1027/team-vitality-vs-loud）")
    ap.add_argument("--max", type=int, default=8,
                    help="每个赛事最多抓几场（默认 8）")
    ap.add_argument("--db", default="",
                    help="入库到哪个库（默认按源：vlr→vlml_vlr.duckdb，rib→vlml_rib.duckdb）")
    ap.add_argument("--year", type=int, default=0, help="只入库某一年")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="每次请求间隔秒数（默认用该客户端自己的礼貌值）")
    ap.add_argument("--only-pipeline", action="store_true",
                    help="不抓，只把已落盘的 jsonl 入库")
    ap.add_argument("--no-pipeline", action="store_true",
                    help="只抓/落盘，不入库")
    ap.add_argument("--repair-game-ids", action="store_true",
                    help="把已入库 ext_* 表的 game_id 修成 games 表那套（老数据专用）")
    args = ap.parse_args()

    src = SOURCES[args.source]
    mod = src["mod"]
    sleep = args.sleep or getattr(mod, "DEFAULT_SLEEP", 1.2)
    db_path = Path(args.db) if args.db else DATA_DIR / src["db"]
    raw_dir = stage_dir(args.source)

    if args.repair_game_ids:
        print(f"修 {db_path} 里 ext_* 表的 game_id …")
        repair_game_ids(db_path)
        return 0

    if args.discover or not (args.event or args.match or args.only_pipeline):
        _discover(src, sleep)
        if not (args.event or args.match or args.only_pipeline):
            print(f"\n给一个 --event <id>（或 --match <path>）再跑。"
                  f"当前源：{src['label']}")
            return 0

    matches: list[dict] = []
    if not args.only_pipeline:
        # 显式 --match 必抓；赛事里的比赛要**试到抓满 --max 场已打完的**为止
        # （rib 的赛事页把 upcoming 排前面，取"前 N 场"会一场数据都没有）
        todo: list[tuple[str, bool]] = [(p, True) for p in args.match]
        for eid in args.event:
            found = mod.list_matches(eid, limit=0, sleep=sleep)
            print(f"赛事 {eid}：{len(found)} 场")
            todo += [(m["path"], False) for m in found]

        cand_cap = len(args.match) + max(args.max * 5, args.max + 10)
        todo = todo[:cand_cap]
        got = 0
        for i, (p, must) in enumerate(todo, 1):
            if not must and got >= args.max:
                break
            try:
                m = mod.fetch_match(p, sleep=sleep)
            except getattr(mod, "FetchError", Exception) as exc:
                print(f"  [{i}/{len(todo)}] ❌ {p} → {exc}")
                continue
            status = str(m.get("status") or "")
            if status and status != "completed":
                print(f"  [{i}/{len(todo)}] ⏭️  {p} 状态 {status}（没打完，跳过）")
                continue
            if not m.get("games"):
                print(f"  [{i}/{len(todo)}] ⏭️  {p} 没有回合数据（可能未公布），跳过")
                continue
            out = vtg.write_jsonl(m, raw_dir=raw_dir)
            rounds = sum(len(g.get("rounds") or []) for g in m["games"])
            extra = ""
            if args.source == "rib":
                extra = (f" · 逐回合 {len(m.get('player_round_stats') or [])} 行"
                         f" / 首杀 {sum(1 for r in (m.get('player_round_stats') or [])
                                        if r.get('first_kill'))}")
            print(f"  [{i}/{len(todo)}] ✅ {m['teams'][0]} vs {m['teams'][1]} "
                  f"· {len(m['games'])} 图 / {rounds} 回合{extra} → {out.name}")
            _save_parsed(m, raw_dir)
            matches.append(m)
            got += 1
    else:
        # 没抓就入库：解析结果从缓存读，选手统计才不会丢
        matches = _load_parsed(raw_dir)
        if matches:
            print(f"（从缓存读到 {len(matches)} 场的解析结果，ext 表照样写）")

    if args.no_pipeline:
        return 0

    print(f"\n入库到 {db_path} …")
    run_pipeline(db_path, year=args.year or None)

    n = write_agg_stats(matches, db_path, src["label"]) if matches else 0
    if n:
        print(f"聚合选手统计 ext_player_game_stats：{n} 行")
    if args.source == "rib" and matches:
        for table, cnt in write_rib_tables(matches, db_path).items():
            if cnt:
                print(f"{table}：{cnt} 行")

    print("\n接完了。下一步：")
    print("  1) 刷新学科可填值：python mve/entity_catalog.py")
    print("  2) 同步进登记册：  python mve/subjects.py --sync")
    print(f"  3) 面板切到 {db_path.name}（或写 mve/db_config.json 的 db_path）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
