#!/usr/bin/env python3
"""vlr.gg 的比赛 → **GRID 形态的 jsonl**，好让 VLML 原有的入库管线直接吃。

为什么绕这道弯
--------------
`vlml/database/scripts/ingestion/` 只认 GRID 的事件流（事件驱动状态机：
`tournament-started-series` → `series-started-game` → `game-started-round`
→ `game-ended-round` → `team-won-game` / `team-won-series`）。与其另写一套
入库器（要自己重做幂等、事务、派生关系），不如把 vlr 的数据**翻译成这套事件**：

- 落盘位置照 GRID 的约定：`data/raw_events/{year}/{tournament}/{series_id}.jsonl`
  （`parsers.py:29` —— **series_id 就是文件名**，year/tournament 取目录名）
- 于是 `load_data.py`（幂等跳过已入库 series）和 `run_pipeline.py` **一行不用改**

能造什么、不能造什么（这条线不能越）
------------------------------------
| 事件 | 造不造 | 依据 |
|---|---|---|
| `tournament-started-series` | ✅ 造 | 赛事名 / 开赛时间 / 两队，都是 vlr 真给的 |
| `series-started-game` | ✅ 造 | 每张图的图名与序号，vlr 真给 |
| `game-started-round` / `game-ended-round` | ✅ 造 | 每回合胜者 + 结束原因，vlr 真给 |
| `team-won-game` / `team-won-series` | ✅ 造 | 比分推出来的 |
| `player-killed-player` / `…-damaged-…` / `player-used-ability` | ❌ **不造** | vlr 没有逐击杀事件。造了就是往 `base_events` 里灌假数据，`is_kill` 之类的布尔会骗人 |

所以入库后 `base_events` 里只有回合级事件，**没有一行 kill** —— 这是数据源的
真实上限，不是 bug。选手的 K/D/A、ACS、ADR、**FK/FD** 是 vlr 给的聚合值，
另落在 `ext_player_game_stats` 表（不塞进事件流，免得被当成逐事件数据）。

⚠️ 回合时间戳：vlr **没有**逐回合时间，这里统一取开赛时间。
别拿 `rounds.started_at` 算节奏/时长 —— 那个粒度不存在。
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any

RAW_DEFAULT = (Path(__file__).resolve().parents[2] / "vlml" / "data"
               / "raw_events")


def _fold(s: str) -> str:
    """去音标 + 转大写，用来把 vlr 的队标签（LEV）对上全名（LEVIATÁN）。"""
    n = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in n if not unicodedata.combining(c)).upper()


def _team_of(tag: str, teams: list[str]) -> str:
    """队标签 → 队全名。对不上就原样返回标签（不硬猜成第一支队）。"""
    t = _fold(tag)
    if not t:
        return ""
    for full in teams:
        f = _fold(full)
        if f == t or f.startswith(t) or t.startswith(f):
            return full
    return tag


def _player_nodes(players: list[dict[str, Any]], team: str) -> list[dict]:
    """把该队的选手挂到 seriesState 的 team 节点上（id 用名字的稳定哈希）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for p in players:
        if _team_of(p.get("team_tag", ""), [team]) != team:
            continue
        name = str(p.get("player_name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"id": f"vlr_player_{abs(hash(name)) % (10 ** 9)}",
                    "name": name, "character": {"name": ""}})
    return out


def build_events(m: dict[str, Any]) -> list[dict[str, Any]]:
    """一场 vlr 比赛 → GRID 形态的事件列表（已按时间排好序）。"""
    series_id = str(m.get("series_id") or "")
    teams = [str(t) for t in (m.get("teams") or [])][:2]
    while len(teams) < 2:
        teams.append("")
    started = str(m.get("started_at") or "")
    t_slug = str(m.get("tournament_slug") or "")
    players = list(m.get("player_stats") or [])

    series_teams = [{"id": f"vlr_team_{_fold(t) or i}", "name": t,
                     "players": _player_nodes(players, t)}
                    for i, t in enumerate(teams)]

    ev: list[dict[str, Any]] = [{
        "id": f"{series_id}_tournament-started-series",
        "type": "tournament-started-series",
        "occurredAt": started,
        "actor": {"state": {"id": t_slug,
                            "name": str(m.get("tournament_name") or t_slug)}},
        "target": {"state": {"startedAt": started,
                             "teams": [{"name": t} for t in teams]}},
        "seriesState": {"teams": series_teams},
    }]

    for g in (m.get("games") or []):
        gid = str(g.get("game_id") or "")
        seq = int(g.get("sequence") or 0)
        if not gid or not seq:
            continue
        ev.append({
            "id": f"{series_id}_game_{seq}_started",
            "type": "series-started-game",
            "occurredAt": started,
            "seriesState": {
                "startedAt": started,
                "teams": series_teams,
                "games": [{"id": gid, "sequenceNumber": seq,
                           "map": {"name": str(g.get("map_name") or "")},
                           "teams": series_teams}],
            },
        })

        for r in (g.get("rounds") or []):
            rseq = int(r.get("round_number") or 0)
            if not rseq:
                continue
            winner = str(r.get("winner") or "")
            ev.append({
                "id": f"{series_id}_game_{seq}_round_{rseq}_start",
                "type": "game-started-round",
                "occurredAt": started,
                "platformGameId": gid,
            })
            # teams[].won 决定 winning/losing；winType 直接给 end_reason
            # （db_loader.infer_end_reason 优先取 winType，不用造 objectives）
            ev.append({
                "id": f"{series_id}_game_{seq}_round_{rseq}_end",
                "type": "game-ended-round",
                "occurredAt": started,
                "platformGameId": gid,
                "actor": {"state": {"id": gid}},
                "target": {"state": {
                    "sequenceNumber": rseq,
                    "winType": str(r.get("end_reason") or ""),
                    "teams": [{"name": t, "won": t == winner} for t in teams],
                }},
            })

        if g.get("winner"):
            ev.append({
                "id": f"{series_id}_game_{seq}_won",
                "type": "team-won-game",
                "occurredAt": started,
                "actor": {"state": {"name": str(g["winner"])}},
                "target": {"state": {"id": gid}},
            })

    if m.get("winner"):
        ev.append({
            "id": f"{series_id}_series_won",
            "type": "team-won-series",
            "occurredAt": started,
            "actor": {"state": {"name": str(m["winner"])}},
        })
    return ev


def write_jsonl(m: dict[str, Any], *, raw_dir: Path | str | None = None
                ) -> Path:
    """落盘成 `raw_events/{year}/{tournament}/{series_id}.jsonl`，返回路径。

    与 GRID 的下载器保持同构：一行一个 wrapper（`parsers.parse_jsonl_file`
    读的就是 `{"occurredAt":…, "events":[…]}`）。
    """
    base = Path(raw_dir) if raw_dir else RAW_DEFAULT
    year = str(int(m.get("year") or 0) or "unknown")
    tour = str(m.get("tournament_slug") or "unknown")
    series_id = str(m.get("series_id") or "")
    if not series_id:
        raise ValueError("这场比赛没有 series_id，落不了盘")
    out = base / year / tour / f"{series_id}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    ev = build_events(m)
    with out.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"occurredAt": str(m.get("started_at") or ""),
                            "events": ev}, ensure_ascii=False) + "\n")
    return out
