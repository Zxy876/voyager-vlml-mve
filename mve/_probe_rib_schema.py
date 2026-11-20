"""查 rib 库：每张表行数 + 首血相关列到底在哪。"""
import duckdb

c = duckdb.connect("vlml/data/vlml_rib.duckdb", read_only=True)
print("== 表与行数 ==")
tabs = c.execute("show tables").fetchall()
for (t,) in tabs:
    n = c.execute(f'select count(*) from "{t}"').fetchone()[0]
    print(f"{t:34s} {n}")

print("\n== 含 first / kill / fb 的列 ==")
rows = []
for (t,) in c.execute("show tables").fetchall():
    for r in c.execute(f'describe "{t}"').fetchall():
        cl = str(r[0]).lower()
        if "first" in cl or "kill" in cl or "fb" in cl:
            rows.append((t, r[0]))
rows.sort()
for r in rows:
    print(f"{r[0]:34s} {r[1]}")

print("\n== ext_player_round_stats 列 ==")
print([r[0] for r in c.execute("describe ext_player_round_stats").fetchall()])

print("\n== ext_player_round_stats 样例 1 行 ==")
cols = [r[0] for r in c.execute("describe ext_player_round_stats").fetchall()]
row = c.execute("select * from ext_player_round_stats limit 1").fetchone()
for k, v in zip(cols, row):
    print(f"  {k:28s} = {v}")
