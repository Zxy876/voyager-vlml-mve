#!/usr/bin/env python3
"""消费侧多技能实例化（_refill_sql_multi）单测（2026-10-26 原版同构对齐）。

原版同构证据：voyager.py:195-199（检索技能整批注入）+ action.py:94（programs）。
验证（chat_json 打桩，不联网）：
  ① items 逐条透传（多条 SQL 列表，不再是单条）；
  ② 无 sql / 非法条目被丢弃；
  ③ 超过 5 条截断（检索 top_k=5 口径一致）；
  ④ note 透传。
运行：python mve/test_refill_multi.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(HERE), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

_FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        _FAILS.append(name)


def main() -> int:
    from voyager_vlml.consume import _refill_sql_multi as _multi
    import voyager_vlml.consume as C

    skills = [{"topic": f"learn_x::{i}", "name": f"skill_{i}",
               "text": f"配方{i}", "code": f"SELECT {i}"} for i in range(7)]

    print("== ① 多条 items 逐条透传 ==")
    C.chat_json = lambda *a, **k: {"items": [
        {"name": "skill_0", "sql": "SELECT SUM(base_salary) FROM employees"},
        {"name": "skill_1", "sql": "SELECT SUM(amount) FROM allowance"},
    ], "note": "复用了两条组成配方"}
    r = _multi("九月份公司的总支出", skills, "employees(x) allowance(y)")
    check("两条 SQL", len(r["items"]) == 2, f"n={len(r['items'])}")
    check("名字透传", r["items"][0]["name"] == "skill_0"
          and r["items"][1]["name"] == "skill_1")
    check("note 透传", r["note"] == "复用了两条组成配方")

    print("== ② 脏条目丢弃 ==")
    C.chat_json = lambda *a, **k: {"items": [
        {"name": "a", "sql": "SELECT 1"},
        {"name": "b", "sql": ""},
        "not-a-dict",
        {"name": "c"},
        {"sql": "SELECT 2"},
    ], "note": ""}
    r = _multi("q", skills, "")
    check("只留有 SQL 的条目", len(r["items"]) == 2
          and [i["name"] for i in r["items"]] == ["a", ""],
          f"{[i['name'] for i in r['items']]}")

    print("== ③ 超过 5 条截断 ==")
    C.chat_json = lambda *a, **k: {"items": [
        {"name": f"s{i}", "sql": f"SELECT {i}"} for i in range(9)], "note": ""}
    r = _multi("q", skills, "")
    check("截断到 5", len(r["items"]) == 5, f"n={len(r['items'])}")

    print("== ④ 空响应 ==")
    C.chat_json = lambda *a, **k: {}
    r = _multi("q", skills, "")
    check("items 空、note 空", r["items"] == [] and r["note"] == "")

    if _FAILS:
        print(f"✗ 失败 {len(_FAILS)} 项：{_FAILS}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
