"""修复 6 条真空配方（走 skill_store.add 官方通路，保留 hits/ok 历史）。

修复策略：
- 能验证的换已验证 SQL（fb_conversion×2 / map_pool / adr）
- 死实体（Cloud9 已不在 rib 库）的修**模式 bug 与口径 bug**，实体字面量保留
  （消费线 _refill_sql 会按问句回填；库里没这支队时新闸门会诚实降级）
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "mve"))     # skill_store 在 mve/ 子目录

import duckdb
import skill_store
from voyager_vlml.agents.critic import is_vacuous

cfg = json.loads((ROOT / "mve" / "db_config.json").read_text(encoding="utf-8"))
con = duckdb.connect(cfg["db_path"], read_only=True)


def run(sql):
    cur = con.execute(sql)
    return [list(r) for r in cur.fetchall()]


# (topic, name, 新 SQL, 期望)
FIXES = [
    ("vlml_fb_conversion", "vlml_fb_conversion", """
SELECT ROUND(AVG(CASE WHEN r.winning_team_name = 'G2 Esports' THEN 1 ELSE 0 END) * 100, 1) AS avg_round_win_rate,
       COUNT(*) AS sample_size
FROM ext_player_round_stats e
JOIN rounds r ON e.game_id = r.game_id AND e.round_number = r.round_number
WHERE e.team_name = 'G2 Esports' AND r.map_name = 'Split' AND e.first_kill = 1
""", "真数 100.0/7"),
    ("fb_conversion_analysis", "fb_conversion_analysis", """
SELECT ROUND(AVG(CASE WHEN r.winning_team_name = 'G2 Esports' THEN 1 ELSE 0 END) * 100, 1) AS avg_round_win_rate,
       COUNT(*) AS sample_size
FROM ext_player_round_stats e
JOIN rounds r ON e.game_id = r.game_id AND e.round_number = r.round_number
WHERE e.team_name = 'G2 Esports' AND r.map_name = 'Split' AND e.first_kill = 1
""", "真数 100.0/7"),
    ("vlml_map_pool", "vlml_map_pool", """
SELECT ROUND(SUM(CASE WHEN winning_team_name = 'Team Liquid' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS win_rate_pct,
       COUNT(*) AS total_games
FROM games
WHERE (team1_name = 'Team Liquid' OR team2_name = 'Team Liquid') AND map_name = 'Abyss'
""", "真数 0.0/1（TL 在 Abyss 只打过 1 场且输了，诚实答案）"),
    ("vlml_adr", "vlml_adr", """
SELECT ROUND(AVG(adr), 2) AS avg_adr, COUNT(*) AS sample_rows
FROM ext_player_side_stats
WHERE team_name = 'G2 Esports' AND side = 'defense'
""", "真数 133.2/10"),
    ("series_totals", "series_totals", """
SELECT COUNT(*) AS total_rounds,
       SUM(CASE WHEN winning_team_name = 'Cloud9' THEN 1 ELSE 0 END) AS rounds_won
FROM rounds
WHERE game_id IN (SELECT game_id FROM games WHERE team1_name = 'Cloud9' OR team2_name = 'Cloud9')
""", "模式修复：= → IN（原写法多场次会报错）；Cloud9 不在 rib 库，真空属预期"),
    ("vlml_side_conversion", "vlml_side_conversion", """
SELECT COUNT(DISTINCT CASE WHEN e.side = 'attack' THEN e.game_id || '#' || e.round_number END) AS attack_rounds,
       COUNT(DISTINCT CASE WHEN e.side = 'attack' AND r.winning_team_name = e.team_name
                           THEN e.game_id || '#' || e.round_number END) AS attack_rounds_won,
       ROUND(COUNT(DISTINCT CASE WHEN e.side = 'attack' AND r.winning_team_name = e.team_name
                                 THEN e.game_id || '#' || e.round_number END) * 100.0
             / NULLIF(COUNT(DISTINCT CASE WHEN e.side = 'attack'
                                           THEN e.game_id || '#' || e.round_number END), 0), 1) AS attack_conversion_pct
FROM ext_player_round_stats e
JOIN rounds r ON e.game_id = r.game_id AND e.round_number = r.round_number
WHERE e.team_name = 'Cloud9'
""", """口径修复：原 SQL 的 attack_side_conversions 就是 attack_rounds（假指标）；
且逐选手行数会 5 倍虚胖，改 DISTINCT 回合；Cloud9 不在 rib 库，真空属预期"""),
]

raw = json.loads((ROOT / "mve" / "skill_store.json").read_text(encoding="utf-8"))
old = raw["skills"]

print("== 修复前逐条实跑验证 ==")
for topic, name, sql, expect in FIXES:
    key = f"{topic}::{name}"
    try:
        rows = run(sql)
        vac = is_vacuous(rows)
        status = "真空" if vac else "真数"
        print(f"  [{status}] {key}: {rows[:2]}  —— 期望：{expect}")
    except Exception as e:
        print(f"  [报错] {key}: {str(e)[:100]}")

print("\n== 写入技能库（add 官方通路）==")
for topic, name, sql, expect in FIXES:
    key = f"{topic}::{name}"
    if key not in old:
        print(f"  !! {key} 不在库里，跳过")
        continue
    s = old[key]
    action, k = skill_store.add(
        topic, name, s.get("text") or "",
        source=str(s.get("source") or "practice"),
        blueprint=s.get("blueprint"),
        code=sql.strip(),
    )
    print(f"  {action:9s} {k}（hits={s.get('hits')} ok={s.get('ok')} 保留）")

print("\n== 修复后复跑审计 ==")
after = json.loads((ROOT / "mve" / "skill_store.json").read_text(encoding="utf-8"))
for topic, name, sql, expect in FIXES:
    key = f"{topic}::{name}"
    s = after["skills"][key]
    rows = run(s["code"])
    vac = is_vacuous(rows)
    print(f"  [{'真空' if vac else '真数'}] {key}: versions={s['versions']} {rows[:2]}")
