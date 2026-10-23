"""手工验证：题干那个问题在这张库里到底答不答得出来（不经过 LLM）。"""
import duckdb

c = duckdb.connect("vlml/data/vlml_rib.duckdb", read_only=True)

print("== rounds 列 ==")
print([r[0] for r in c.execute(
    "select column_name from information_schema.columns "
    "where table_name = 'rounds' order by ordinal_position").fetchall()])
row = c.execute("select * from rounds limit 1").fetchone()
cols = [r[0] for r in c.execute(
    "select column_name from information_schema.columns "
    "where table_name = 'rounds' order by ordinal_position").fetchall()]
for k, v in zip(cols, row):
    print(f"  {k:26s} = {v}")

print("\n== 各表里 Karmine Corp / Sunset 的分布 ==")
for t, col in [("ext_player_round_stats", "team_name"),
               ("rounds", "map_name")]:
    print(t, col, c.execute(
        f"select count(*) from \"{t}\" where \"{col}\" = "
        + ("'Karmine Corp'" if col == "team_name" else "'Sunset'")).fetchone())

print("\n== 正确口径：Karmine Corp 拿首血的回合里赢了多少 ==")
sql = """
WITH fb AS (
  SELECT DISTINCT e.series_id, e.game_id, e.map_name, e.round_number, e.team_name
  FROM ext_player_round_stats e
  WHERE e.first_kill = TRUE AND e.team_name = 'Karmine Corp'
),
joined AS (
  SELECT f.*, r.winning_team_name
  FROM fb f
  LEFT JOIN rounds r
    ON r.game_id = f.game_id AND r.round_number = f.round_number
)
SELECT
  COUNT(*) AS fb_rounds,
  SUM(CASE WHEN winning_team_name = 'Karmine Corp' THEN 1 ELSE 0 END) AS won_after_fb,
  ROUND(100.0 * SUM(CASE WHEN winning_team_name = 'Karmine Corp' THEN 1 ELSE 0 END)
        / NULLIF(COUNT(*), 0), 1) AS pct
FROM joined
"""
print(c.execute(sql).fetchall())

print("\n== 只看 Sunset ==")
sql2 = sql.replace(
    "WHERE e.first_kill = TRUE AND e.team_name = 'Karmine Corp'",
    "WHERE e.first_kill = TRUE AND e.team_name = 'Karmine Corp' AND e.map_name = 'Sunset'")
print(c.execute(sql2).fetchall())

print("\n== 全库首血分布前 5 队（确认 first_kill 不是全 False）==")
print(c.execute("""
select team_name, count(*) n from ext_player_round_stats
where first_kill = TRUE group by 1 order by n desc limit 5""").fetchall())
