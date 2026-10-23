#!/usr/bin/env python3
"""取证：口径检验（critic 第二级）能不能分辨「取到数但口径错」。

三条固定输入，直接调 critic，不经过出题/写码模型（避免模型抖动干扰取证）：

    A 口径对的：ext_player_round_stats.first_kill + rounds.winning_team_name → 9/13 ≈ 69.2%
    B 口径错的：round_number = 1 当「首血」（历史坏配方，曾判成功入库）
    C 空表：agg_first_blood_stats（0 行，纯聚合返回 1 行 (NULL,0)）→ 第一级就该拦下

期望：A success=True 且 reasoning 带出比例；B success=False 且 critique 点出该看哪一列；
C 走 gate=vacuous，**不调 LLM**。

跑法（服务器，rib 库）：/root/mve/.venv/bin/python mve/_probe_critic.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from voyager_vlml.agents.critic import VLMLCriticAgent  # noqa: E402
from voyager_vlml.env.vlml_mcp import VLMLMCPEnv  # noqa: E402

TASK = "Karmine Corp 在 Sunset 上先拿到首杀的回合里赢了多少比例"
HINTS = [
    "转换率 = 首血方赢下的回合 / 首血方拿到首血的回合",
    "必须同时给出样本量（COUNT(*)），否则看不出这个率稳不稳",
]

SQL_OK = """SELECT
    COUNT(CASE WHEN r.winning_team_name = 'Karmine Corp' THEN 1 END) AS wins,
    COUNT(*) AS total
FROM ext_player_round_stats e
JOIN rounds r ON e.game_id = r.game_id AND e.round_number = r.round_number
WHERE e.team_name = 'Karmine Corp'
  AND e.map_name = 'Sunset'
  AND e.first_kill = TRUE"""

# 2026-10-23 实测：只连 game_id、不连 round_number → rounds 每场 22 行全乘上来，
# 13 个回合变成 286 行。这是「取到数、口径看着对、数却是错的」最阴的一种。
SQL_FANOUT = """SELECT
    COUNT(CASE WHEN r.winning_team_name = 'Karmine Corp' THEN 1 END) AS wins,
    COUNT(*) AS total
FROM ext_player_round_stats e
JOIN rounds r ON e.game_id = r.game_id
WHERE e.team_name = 'Karmine Corp'
  AND e.map_name = 'Sunset'
  AND e.first_kill = TRUE"""

SQL_BAD = """SELECT team_name, COUNT(*) AS first_blood_count
FROM ext_player_round_stats
WHERE map_name = 'Split' AND round_number = 1
GROUP BY team_name
ORDER BY first_blood_count DESC
LIMIT 1"""

SQL_EMPTY = """SELECT ROUND(AVG(CASE WHEN fb_team = 'Karmine Corp' AND fb_team_won THEN 1 ELSE 0 END) * 100, 2) AS win_pct,
       COUNT(*) AS n
FROM agg_first_blood_stats
WHERE map_name = 'Sunset' AND fb_team = 'Karmine Corp'"""


async def _run(env: VLMLMCPEnv, sql: str) -> dict:
    """执行 + 行放大实测（同 action.step 的做法：JOIN 才测）。"""
    obs = await env.run_sql(sql)
    if obs.get("ok"):
        g = await env.check_grain(sql)
        if g:
            obs = dict(obs, grain=g)
    return obs


def _show(tag: str, sql: str, obs: dict, d: dict) -> None:
    print(f"\n===== {tag} =====")
    print("SQL:", " ".join(sql.split())[:150])
    print(f"取数: ok={obs.get('ok')} rows={obs.get('rows')} err={str(obs.get('error') or '')[:90]}")
    g = obs.get("grain")
    print(f"粒度: {g}" if g else "粒度: （无 JOIN / 没测到）")
    print(f"判决: success={d['success']} gate={d['gate']}")
    print(f"reasoning: {d['reasoning'][:220]}")
    print(f"critique : {d['critique'][:220]}")


async def main() -> int:
    env = VLMLMCPEnv()
    print("数据源:", env.data_source())
    c = VLMLCriticAgent()

    for tag, sql in (("A1 正确 JOIN（game_id+round_number）", SQL_OK),
                     ("A2 fan-out JOIN（只连 game_id）", SQL_FANOUT),
                     ("B 口径错（第一回合当首血）", SQL_BAD),
                     ("C 空表", SQL_EMPTY)):
        obs = await _run(env, sql)
        d = c.judge(observations=[obs], task={"task": TASK, "hints": HINTS},
                    context="\n".join(HINTS), sql=sql)
        _show(tag, sql, obs, d)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
