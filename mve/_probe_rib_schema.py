"""查 rib 库：每张表行数 + 首血相关列到底在哪。"""
import duckdb

c = duckdb.connect("vlml/data/vlml_rib.duckdb", read_only=True)
print("== 表与行数 ==")
tabs = c.execute(
    "select table_name from information_schema.tables where table_schema = 'main' order by 1"
).fetchall()
for (t,) in tabs:
    n = c.execute(f'select count(*) from "{t}"').fetchone()[0]
    print(f"{t:34s} {n}")

print("\n== 含 first / kill / fb 的列 ==")
rows = c.execute(
    "select table_name, column_name from information_schema.columns "
    "where table_schema = 'main' and (lower(column_name) like '%first%' "
    "or lower(column_name) like '%kill%' or lower(column_name) like '%fb%') "
    "order by 1,2"
).fetchall()
for r in rows:
    print(f"{r[0]:34s} {r[1]}")

print("\n== ext_player_round_stats 列 ==")
print([r[0] for r in c.execute(
    "select column_name from information_schema.columns "
    "where table_name = 'ext_player_round_stats' order by ordinal_position"
).fetchall()])

print("\n== ext_player_round_stats 样例 1 行 ==")
cols = [r[0] for r in c.execute(
    "select column_name from information_schema.columns "
    "where table_name = 'ext_player_round_stats' order by ordinal_position"
).fetchall()]
row = c.execute("select * from ext_player_round_stats limit 1").fetchone()
for k, v in zip(cols, row):
    print(f"  {k:28s} = {v}")
