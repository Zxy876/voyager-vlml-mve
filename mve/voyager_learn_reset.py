#!/usr/bin/env python3
"""Voyager 清库 / 清记忆（镜像 mve/reset_all.py，但针对 Voyager 的检查点目录）。

和 MVE 面板「清空学习状态」同款心智：先备份到 voyager_backup_<时间戳> 再删，
不让任何键钮直接毁灭数据。

两类清除（对应 MVE 的清库 / 清记忆）：
  清库   —— 删 ckpt/skill/：Voyager 自动课程学到的 JS 技能库
           （skills.json + code/ + vectordb）。删了 = 模型「忘掉」所有已掌握的手法。
  清记忆 —— 删 ckpt/curriculum/ + ckpt/events/：
           curriculum = 课程进度（completed/failed 任务、QA 缓存、探索向量库）；
           events    = 事件历史（EventRecorder 每任务写的 json）。
           删了 = 模型「忘了学到哪了 / 试过什么」，下次从头探索，但技能库还在。

为什么分开：清库 = 忘技能；清记忆 = 忘进度。MVE 面板里「停止 / 清库 / 清记忆」
是三个独立控件，Voyager 这边也照做 —— 人可能只想清进度重跑课程（保留已学技能），
也可能想彻底重来（连技能一起清）。

用法：
    python mve/voyager_learn_reset.py skill              # 清库（先备份）
    python mve/voyager_learn_reset.py memory             # 清记忆（先备份）
    python mve/voyager_learn_reset.py skill  --dry-run   # 只看会清什么
    python mve/voyager_learn_reset.py memory --no-backup # 不备份直接删
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

from voyager_paths import VOYAGER_DIR, SKILL_DIR, CURRICULUM_DIR, EVENTS_DIR


# 每个清除目标：路径 + 人话说明
TARGETS = {
    "skill": [("skill", SKILL_DIR, "已学 JS 技能库（skills.json + code/ + vectordb）")],
    "memory": [
        ("curriculum", CURRICULUM_DIR, "课程进度（completed/failed 任务 + QA 缓存 + 探索向量库）"),
        ("events", EVENTS_DIR, "事件历史（每任务写的 json）"),
    ],
}


def _backup(targets: list[tuple[str, Path, str]]) -> str | None:
    existing = [(name, path, desc) for name, path, desc in targets if path.exists()]
    if not existing:
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = VOYAGER_DIR / f"voyager_backup_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    for _name, path, _desc in existing:
        # 保留 ckpt 下的相对结构：voyager_backup_xxx/skill、.../curriculum、.../events
        rel = path.name
        (dest / rel).mkdir(parents=True, exist_ok=True)
        for item in path.iterdir():
            shutil.copy2(item, dest / rel / item.name) if item.is_file() else \
                shutil.copytree(item, dest / rel / item.name)
    return str(dest)


def _clear_one(which: str) -> dict:
    targets = TARGETS.get(which)
    if not targets:
        return {"ok": False, "error": f"未知目标 {which}"}
    existing = [(name, path, desc) for name, path, desc in targets if path.exists()]
    if not existing:
        return {"ok": True, "cleared": [], "backup": None,
                "note": "本来就没有可清的东西"}
    # 备份（除非 --no-backup）
    backup = None
    if "--no-backup" not in sys.argv:
        backup = _backup(targets)
    for _name, path, _desc in existing:
        shutil.rmtree(path)
    cleared = [f"{name}（{desc}）" for name, _p, desc in existing]
    return {"ok": True, "cleared": cleared, "backup": backup}


def clear_skill() -> dict:
    return _clear_one("skill")


def clear_memory() -> dict:
    return _clear_one("memory")


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] not in ("skill", "memory"):
        print(__doc__)
        print("用法：python mve/voyager_learn_reset.py {skill|memory} [--dry-run] [--no-backup]")
        return 2
    which = argv[0]
    dry = "--dry-run" in argv
    targets = TARGETS[which]
    existing = [(name, path, desc) for name, path, desc in targets if path.exists()]
    label = "清库（技能库）" if which == "skill" else "清记忆（课程进度 + 事件）"
    print("=" * 68)
    print(f"  Voyager {label}" + ("（DRY RUN，不会真的删）" if dry else ""))
    print("=" * 68)
    for name, path, desc in existing:
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        print(f"  {name:12s} {size:>12,} B   {desc}")
    if not existing:
        print("\n  没有可清的东西。")
        return 0
    if dry:
        return 0
    res = _clear_one(which)
    if res.get("backup"):
        print(f"\n  已备份到：{res['backup']}")
    for c in res.get("cleared", []):
        print(f"  已清：{c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
