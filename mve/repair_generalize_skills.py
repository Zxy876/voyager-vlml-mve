#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把技能库里「带旧题数值」的 SQL 配方批量泛化成槽位模板。

为什么（2026-10-23 用户定：「不要把数值写死，而是把算法积累成技能」）：
    入库泛化（learn_milestone_skills / consume._absorb 调
    generalize_sql_recipe）上线**之前**的历史配方是为单题写的查询，实体值
    写死在 SQL 里 —— 原样重放就是旧题的答案（实测 consume_f4e54ccd 带
    ['09','2026']，对「技术部门请假总天数」重放必错）。本脚本把历史 SQL
    配方逐条过 `generalize_sql_recipe` 泛化成 :slot 模板，补齐存量。

口径：
    · 只动 SELECT/WITH 开头的 SQL 配方；本机 Python 配方（RESULT 脚本）不碰；
    · 泛化失败 / LLM 挂 / 产出不像 SQL → 原样保留（泛化是增强不是闸门）；
    · 真改前整份 skill_store.json 备份到 mve/_backup_<ts>_generalize/。

用法：
    python mve/repair_generalize_skills.py --dry-run   # 只看要改什么
    python mve/repair_generalize_skills.py             # 真改（先自动备份）
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

_MVE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _MVE_DIR.parent
for _p in (str(_MVE_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="把历史 SQL 配方泛化成槽位模板（存算法不存数值）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不落盘")
    a = ap.parse_args(argv)

    import skill_store
    from voyager_vlml.consume import generalize_sql_recipe

    raw = skill_store._load_raw()          # noqa: SLF001（同 repair_skill_keys）
    skills: dict[str, dict] = raw.get("skills") or {}
    changed: list[tuple[str, str, str]] = []
    kept = 0
    for key, s in list(skills.items()):
        code = str((s or {}).get("code") or "")
        if not code.strip().lower().startswith(("select", "with")):
            continue                        # 非 SQL 配方不归这里管
        q = str(((s or {}).get("blueprint") or {}).get("question")
                or (s or {}).get("text") or "")
        new = generalize_sql_recipe(code, q)
        if str(new or "").strip() == code.strip():
            kept += 1
            print(f"  = {key}：无变化")
            continue
        changed.append((key, code, new))
        print(f"  ~ {key}")
        print(f"    旧：{code[:140]}")
        print(f"    新：{str(new)[:140]}")
    print(f"\n共 SQL 配方 {changed.__len__() + kept} 条："
          f"可泛化 {len(changed)} 条 / 无变化 {kept} 条")
    if not changed:
        return 0
    if a.dry_run:
        print("[dry-run] 库未改动")
        return 0
    backup = _MVE_DIR / f"_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}_generalize"
    backup.mkdir(parents=True)
    src = Path(skill_store.__file__).with_name("skill_store.json")
    shutil.copy2(src, backup / src.name)
    print(f"已备份 → {backup}")
    for key, _, new in changed:
        skills[key]["code"] = new
    skill_store._save(raw)                 # noqa: SLF001
    print(f"已写回 {len(changed)} 条模板化配方")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
