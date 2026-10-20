#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修技能主键：key 里的名字必须等于 `blueprint.dimension`。

为什么（2026-10-20 线上取证）：
    `skill_store.add()` 过去会对**同题**的技能做相似度合并，于是里程碑 A 的
    维度 `differences` 被并进了里程碑 B 早就存在的 `::def` 条目（key 不动，
    blueprint 被覆盖）。结果：
        key      = consume_010245c5::def
        blueprint= {milestone: 阿斯伯格综合症概述, dimension: differences}
    面板/CLI 于是报「复用了 …::def」，而它真正干的是"区别"那件事 ——
    披露失真，人没法核（用户要的"两次导入可解释"就断在这里）。

    根因已在 `add(..., exact_only=True)` 修掉（学习通道只认精确同名），
    但历史条目还带着错名字，这个脚本把它们改成对的。

口径（与 exact_only 一致，只许精确）：
    · 只在目标 key **空着**时改名，**绝不合并**两条技能；
    · 目标 key 已被占 → 如实报冲突，跳过（留给人看，不猜谁该留下）；
    · 改名前整份 skill_store.json 备份到 mve/_backup_<ts>_skillkeys/。

用法：
    python mve/repair_skill_keys.py --dry-run     # 只看要改什么
    python mve/repair_skill_keys.py               # 真改（先自动备份）
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="修技能主键与 blueprint.dimension 不一致")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不落盘")
    a = ap.parse_args(argv)

    import skill_store

    raw = skill_store._load_raw()          # noqa: SLF001（同 reset_all 的做法）
    skills: dict[str, dict] = raw.get("skills") or {}
    plan: list[tuple[str, str, dict]] = []
    conflicts: list[tuple[str, str]] = []
    for key, s in list(skills.items()):
        bp = s.get("blueprint") or {}
        dim = str(bp.get("dimension") or "").strip()
        if not dim or "::" not in key:
            continue
        topic, name = key.split("::", 1)
        if name == dim:
            continue
        want = f"{topic}::{dim}"
        if want in skills:
            conflicts.append((key, want))
            continue
        plan.append((key, want, s))

    if not plan and not conflicts:
        print("没有需要修的主键（key 与 blueprint.dimension 全部一致）")
        return 0

    for old, new, s in plan:
        print(f"{'DRY ' if a.dry_run else ''}改名 {old} → {new}"
              f"（里程碑 {((s.get('blueprint') or {}).get('milestone') or '')[:20]}）")
    for old, want in conflicts:
        print(f"冲突 {old}：目标 {want} 已存在 → 跳过（不合并、不猜）")

    if a.dry_run:
        print(f"\n干跑结束：{len(plan)} 条要改名，{len(conflicts)} 条冲突跳过")
        return 0

    if plan:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = Path(skill_store.STORE).parent / f"_backup_{stamp}_skillkeys"
        bak.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_store.STORE, bak / "skill_store.json")
        print(f"备份：{bak / 'skill_store.json'}")

    for old, new, s in plan:
        s["name"] = new.split("::", 1)[1]
        skills[new] = skills.pop(old)
    raw["skills"] = skills
    Path(skill_store.STORE).write_text(
        json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已改名 {len(plan)} 条；冲突跳过 {len(conflicts)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
