#!/usr/bin/env python3
"""一次性清空 MVE 的全部学习状态。

为什么单开一个文件：`run_mve.py --reset` 只清技能库 + 运行日志 + 裁判缓存，
但「学习状态」还有另外三处散落的地方 —— 行动因果时间线、人导入历史、驾驶舱日志。
只清一部分会留下**对不上的曲线**：比如 run_log 空了但因果时间线还在，
出题器就会照着旧的时间线选题，而掌握度视图里什么都看不到。

清的六处：
  skill_store.json      技能库（Voyager 自己写的教训 / 记忆）
  run_log.jsonl         每轮作答（进步曲线的唯一数据源）
  causal_timeline.jsonl 行动因果（control=人导入 / attempt=Voyager 行动）
  coach_log.jsonl       人导入历史
  referee_store.json    裁判缓存（会重算，留着反而是旧的）
  pilot.log / pilot_state.json  驾驶舱日志与状态

用法：
    python mve/reset_all.py            # 先备份再清
    python mve/reset_all.py --dry-run  # 只看会清什么
    python mve/reset_all.py --no-backup
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

TARGETS = [
    ("skill_store.json", "技能库 / 记忆"),
    ("run_log.jsonl", "运行日志（进步曲线数据源）"),
    ("causal_timeline.jsonl", "行动因果时间线"),
    ("coach_log.jsonl", "人导入历史"),
    ("referee_store.json", "裁判标准答案缓存"),
    # 保持度（baseline + 半衰期）。不清它，清档后"学会了多少、能撑多久"
    # 还留着上一轮的参数 —— 又是一条对不上的曲线。
    ("mastery_retention.jsonl", "掌握度保持度（baseline / 半衰期）"),
    ("pilot.log", "驾驶舱日志"),
    ("pilot_state.json", "驾驶舱状态"),
]


def main() -> int:
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    backup = "--no-backup" not in argv

    print("=" * 68)
    print("  MVE 清档" + ("（DRY RUN，不会真的删）" if dry else ""))
    print("=" * 68)

    existing = [(name, desc) for name, desc in TARGETS if (ROOT / name).exists()]
    for name, desc in existing:
        size = (ROOT / name).stat().st_size
        print(f"  {name:26s} {size:>9,} B   {desc}")
    missing = [n for n, _ in TARGETS if not (ROOT / n).exists()]
    if missing:
        print(f"  （本来就不存在：{', '.join(missing)}）")

    if dry or not existing:
        if not existing:
            print("\n  没有可清的东西。")
        return 0

    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = ROOT / f"_backup_{stamp}"
        dest.mkdir(exist_ok=True)
        for name, _ in existing:
            shutil.copy2(ROOT / name, dest / name)
        print(f"\n  已备份到：{dest}")

    for name, _ in existing:
        (ROOT / name).unlink()
    print(f"\n  已清 {len(existing)} 个文件。现在的状态：零技能、零记录、零因果。")
    print("  下一步：python mve/run_mve.py --llm （或面板上点「启动 Voyager」）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
