"""服务器技能库审计：把每条技能的 SQL 对当前库实跑，标出「真空配方」。

真空 = ok 但结果没有任何真实值（空表纯聚合返回 1 行 (NULL, 0) 的形态）。
用法：cd /root/mve && .venv/bin/python audit_skills.py
"""
import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from voyager_vlml.agents.critic import has_real_data, is_vacuous

store_path = ROOT / "mve" / "skill_store.json"
data = json.loads(store_path.read_text(encoding="utf-8"))
skills = data.get("skills") or {}
if isinstance(skills, dict):
    items = list(skills.items())          # key -> record
else:
    items = [(s.get("key") or s.get("id") or "?", s) for s in skills]

cfg = json.loads((ROOT / "mve" / "db_config.json").read_text(encoding="utf-8"))
db_path = cfg.get("db_path") or ""
print(f"库：{db_path}")
print(f"技能数：{len(items)}")
print("=" * 70)

con = duckdb.connect(db_path, read_only=True)
bad, good, err = [], [], []

for key, s in items:
    code = s.get("code") or s.get("sql") or ""
    hits = s.get("hits", 0)
    oks = s.get("ok", 0)
    if not code or not code.strip().upper().startswith(("SELECT", "WITH")):
        err.append((key, "没有 SQL 配方（纯文字经验？）", hits, oks))
        continue
    try:
        cur = con.execute(code)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    except Exception as e:
        err.append((key, f"SQL 报错：{str(e)[:80]}", hits, oks))
        continue
    row_list = [list(r) for r in rows]
    vac = is_vacuous(row_list)
    rec = (key, len(rows), cols, row_list[:2], hits, oks, code)
    (bad if vac else good).append(rec)

print(f"\n【真空配方】{len(bad)} 条 —— 跑得通但取不到真数，复用即答错：")
for key, n, cols, sample, hits, oks, code in bad:
    print(f"  ✗ {key}（hits={hits} ok={oks}）{n} 行 {cols} 样例 {sample}")
    print(f"      SQL: {' '.join(code.split())[:150]}")

print(f"\n【正常配方】{len(good)} 条：")
for key, n, cols, sample, hits, oks, code in good:
    print(f"  ✓ {key}（hits={hits} ok={oks}）{n} 行 样例 {sample}")

print(f"\n【无 SQL / 报错】{len(err)} 条：")
for key, why, hits, oks in err:
    print(f"  ? {key}（hits={hits} ok={oks}）{why}")

print("\n" + "=" * 70)
print(f"合计 {len(items)}：真空 {len(bad)} / 正常 {len(good)} / 其他 {len(err)}")
