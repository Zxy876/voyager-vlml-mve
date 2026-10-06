#!/usr/bin/env python3
"""合成数据生成器 · 为 MVE 造一个字段与 VLML 真表一致的小型假数据集。

设计原则：
- 不硬编码任何"答案数字"。指标全部由 VLML 自带的 transformations SQL 从 base_events 算出。
- 只在事件层植入**模式**（谁拿首血、首血后谁赢），让 agg 表算出来的数字自带故事。
- 这样 MCP 工具返回的每一个数字都有数据支撑，符合 VLML「不猜测、只查询」的原则。

植入的模式（对应真实 Cloud9 vs NRG 那场的结构）：
- Corrode 图上 Cloud9 首血转换率崩盘 → 惨败
- OXY 首死次数偏高
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

DB_PATH = Path(__file__).resolve().parents[1] / "vlml" / "data" / "vlml_events.duckdb"

SERIES_ID = "2843069"
TOURNAMENT_ID = "vct-americas-2025-stage-2"
TOURNAMENT_NAME = "VCT Americas 2025 Stage 2"
TOURNAMENT_YEAR = 2025
REGION = "americas"
C9, NRG = "Cloud9", "NRG"

ROSTERS = {
    C9: [("OXY", "Neon"), ("neT", "Viper"), ("mitch", "Skye"), ("Xeppaa", "Fade"), ("v1c", "Killjoy")],
    NRG: [("mada", "Raze"), ("brawk", "Omen"), ("skuba", "Cypher"), ("Ethan", "Sova"), ("s0m", "Breach")],
}

# 首死权重：OXY 最高（植入"entry fragger 太暴露"这个模式）
FD_WEIGHTS = {C9: [6.0, 2.0, 2.0, 1.5, 1.0], NRG: [1.0, 1.5, 1.5, 2.0, 1.5]}
# 拿首血权重（mada/Ethan 是 NRG 的突破手）
FB_WEIGHTS = {C9: [3.0, 1.5, 1.0, 2.0, 1.0], NRG: [3.0, 2.0, 1.0, 2.5, 1.0]}

WEAPONS = [("Vandal", "rifle"), ("Phantom", "rifle"), ("Sheriff", "pistol"),
           ("Operator", "sniper"), ("Spectre", "smg"), ("Classic", "pistol")]

# 每张图：Cloud9 与 NRG 的「拿到首血后赢下该回合」的概率
# 三张图的故事：Haven 势均力敌 → Corrode 崩盘 → Lotus 小负
GAMES = [
    {"game_id": "g-haven", "map": "Haven", "conv": {C9: 0.76, NRG: 0.60}, "fb_p_c9": 0.52},
    {"game_id": "g-corrode", "map": "Corrode", "conv": {C9: 0.28, NRG: 0.88}, "fb_p_c9": 0.32},
    {"game_id": "g-lotus", "map": "Lotus", "conv": {C9: 0.55, NRG: 0.80}, "fb_p_c9": 0.42},
]

MAX_ROUNDS = 30


def pick(roster: list[tuple[str, str]], weights: list[float]) -> tuple[str, str]:
    return random.choices(roster, weights=weights, k=1)[0]


def other(team: str) -> str:
    return NRG if team == C9 else C9


def main() -> None:
    random.seed(20261005)
    con = duckdb.connect(str(DB_PATH))

    # 清掉旧合成数据，保证可重复运行
    for t in ["base_events", "rounds", "games", "series",
              "agg_player_round_stats", "agg_player_game_stats", "agg_player_series_stats",
              "agg_team_round_stats", "agg_team_game_stats", "agg_player_daily_stats",
              "agg_tournament_stats", "agg_first_blood_stats", "agg_post_plant_stats",
              "agg_team_round_summary", "agg_team_map_stats", "agg_team_series_stats",
              "agg_player_win_shares"]:
        con.execute(f"DELETE FROM {t}")
    print("已清空旧数据")

    base_time = datetime(2025, 8, 29, 18, 0, 0)
    events: list[tuple] = []
    round_rows: list[tuple] = []
    game_rows: list[tuple] = []
    event_seq = 0
    clock = base_time

    total_wins = {C9: 0, NRG: 0}

    for gi, g in enumerate(GAMES, start=1):
        gid = g["game_id"]
        score = {C9: 0, NRG: 0}
        rnd = 0

        while max(score.values()) < 13 and rnd < MAX_ROUNDS:
            rnd += 1
            rid = f"{gid}-r{rnd:02d}"
            clock = clock + timedelta(seconds=random.randint(45, 110))

            # 谁拿首血
            fb_team = C9 if random.random() < g["fb_p_c9"] else NRG
            fd_team = other(fb_team)

            # 拿首血的一方按该图的转换率赢下回合
            won = random.random() < g["conv"][fb_team]
            winner = fb_team if won else fd_team
            loser = other(winner)
            score[winner] += 1

            # 攻防方（简化：交替）
            atk = C9 if rnd % 2 == 1 else NRG
            def_ = other(atk)

            end_reason = random.choices(
                ["eliminated", "defused", "detonated", "time"],
                weights=[0.62, 0.18, 0.16, 0.04],
            )[0]

            round_rows.append((
                rid, SERIES_ID, gid, rnd, g["map"],
                clock, clock + timedelta(seconds=random.randint(60, 100)),
                random.randint(60, 100),
                winner, loser, end_reason,
                TOURNAMENT_NAME, TOURNAMENT_YEAR, datetime.now(),
            ))

            def mk_event(*, etype, actor, actor_team, target=None, target_team=None,
                         is_kill=False, is_fb=False, is_plant=False, is_defuse=False,
                         damage=None, ts=None):
                nonlocal event_seq
                event_seq += 1
                an, aa = actor
                tn, ta = target if target else (None, None)
                weapon, wtype = random.choice(WEAPONS)
                events.append((
                    f"e{event_seq:07d}", ts or clock, SERIES_ID, gid, rid, etype,
                    f"p_{an}", an, actor_team, aa,
                    f"p_{tn}" if tn else None, tn, target_team, ta,
                    "defense" if target_team == def_ else "attack" if target_team else None,
                    "attack" if actor_team == atk else "defense",
                    round(random.uniform(-5000, 5000), 1), round(random.uniform(-5000, 5000), 1),
                    round(random.uniform(-5000, 5000), 1) if tn else None,
                    round(random.uniform(-5000, 5000), 1) if tn else None,
                    "kill" if is_kill else ("plant" if is_plant else "defuse"),
                    None, None, None,
                    TOURNAMENT_NAME, TOURNAMENT_YEAR, g["map"],
                    is_kill, bool(tn) and is_kill, False, is_fb,
                    is_plant, is_defuse, False, False, False, False, False, False,
                    damage, random.randint(2000, 4000), random.randint(1500, 3500),
                    random.randint(12000, 24000), random.randint(9000, 21000),
                    weapon, wtype, random.random() < 0.35, random.random() < 0.05,
                    random.choice(["head", "body", "legs"]),
                    json.dumps({"synthetic": True}),
                ))

            # 首血事件
            fb_actor = pick(ROSTERS[fb_team], FB_WEIGHTS[fb_team])
            fd_target = pick(ROSTERS[fd_team], FD_WEIGHTS[fd_team])
            mk_event(etype="kill", actor=fb_actor, actor_team=fb_team,
                     target=fd_target, target_team=fd_team,
                     is_kill=True, is_fb=True, damage=150.0)

            # 后续击杀
            for _ in range(random.randint(2, 5)):
                if random.random() < 0.5:
                    a_team, t_team = winner, loser
                else:
                    a_team, t_team = loser, winner
                mk_event(etype="kill", actor=pick(ROSTERS[a_team], FB_WEIGHTS[a_team]),
                         actor_team=a_team, target=pick(ROSTERS[t_team], FD_WEIGHTS[t_team]),
                         target_team=t_team, is_kill=True, damage=float(random.randint(80, 160)))

            # 下包 / 拆包
            if end_reason in ("defused", "detonated", "time") or random.random() < 0.35:
                mk_event(etype="plant", actor=pick(ROSTERS[atk], FB_WEIGHTS[atk]),
                         actor_team=atk, is_plant=True)
                if end_reason == "defused":
                    mk_event(etype="defuse", actor=pick(ROSTERS[def_], FB_WEIGHTS[def_]),
                             actor_team=def_, is_defuse=True)

        winner_map = C9 if score[C9] > score[NRG] else NRG
        game_rows.append((
            gid, SERIES_ID, gi, g["map"], C9, NRG, winner_map,
            rnd * 95, rnd,
        ))
        total_wins[C9] += score[C9]
        total_wins[NRG] += score[NRG]
        print(f"  {g['map']:8s}  Cloud9 {score[C9]:2d} - {score[NRG]:2d} NRG   ({rnd} 回合)")

    series_winner = C9 if total_wins[C9] > total_wins[NRG] else NRG
    con.execute(
        "INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?)",
        [SERIES_ID, TOURNAMENT_ID, TOURNAMENT_NAME, TOURNAMENT_YEAR, REGION,
         C9, NRG, series_winner, base_time, datetime.now()],
    )
    con.executemany("INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?)", game_rows)
    con.executemany("INSERT INTO rounds VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", round_rows)
    placeholders = ",".join(["?"] * len(events[0]))
    con.executemany(f"INSERT INTO base_events VALUES ({placeholders})", events)
    con.commit()

    print()
    print(f"series      : {len(game_rows)} 场图写入（总比分 C9 {total_wins[C9]} - {total_wins[NRG]} NRG，胜者 {series_winner}）")
    print(f"rounds      : {len(round_rows)} 行")
    print(f"base_events : {len(events)} 行")
    print()
    print("下一步：跑 transformations 生成 agg 表")
    print("  python database/scripts/orchestration/run_transformations.py")


if __name__ == "__main__":
    main()
