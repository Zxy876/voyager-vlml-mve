#!/usr/bin/env python3
"""界面交互事件流。

为什么单独建这条流：用户要求导出的 md「一定要具体到界面交互（按了什么键，
然后选了什么难度）」。而现有三条流都不是这个视角：

  run_log.jsonl       —— Voyager 的作答结果（机器视角）
  causal_timeline     —— control / attempt 的因果序列（出题器视角）
  coach_log.jsonl     —— 人导入的问题与解释（内容视角）

**没有一条是"人在界面上按了什么"**。没有它，导出的 md 只能写出"覆盖率 45%"，
写不出"人 02:01 点了启动、选了连续自适应 + 3 轮 + 钉住 pistol_eco_pattern（难度 3）"
—— 而后者才是复现一次实验真正需要的。

事件落盘成 ui_events.jsonl，导出 md 时排在最前面（照伴学 doc_exporter.py 的
`## Recent Interactions`，它是 Overview 之后的第一节）。

难度必须记下来：选题下拉里是 topic_id，但人真正关心的是"选了什么难度"，
所以事件里同时存 topic_id 和 difficulty（"出题器自选"时 difficulty=0 并标注）。
"""

from __future__ import annotations

import json
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "ui_events.jsonl"

# 事件种类（面板上真正会被按下的东西）
PILOT_START = "pilot_start"    # 点「启动 Voyager」
PILOT_STOP = "pilot_stop"      # 点「停止」
IMPORT = "human_import"        # 人导入一条意图
TAB = "tab_switch"             # 切分区
CLEAR_LOG = "clear_log"        # 清空运行日志
EXPORT = "export_md"           # 导出 md
DB_PROBE = "db_probe"          # 点「验货」：换库前先看看这个库里有什么
DB_SWITCH = "db_switch"        # 点「切换」：写 db_config.json（要重启面板才生效）
RESET = "reset_all"            # 点「清空学习状态」
PRACTICE_SCOPE_SET = "practice_scope_set"      # 知识图谱页点「练习此知识点」
PRACTICE_SCOPE_CLEAR = "practice_scope_clear"  # 点「清除范围」

# kind -> 人话（导出 md 里直接显示）
ACTION_LABEL = {
    PILOT_START: "点「▶ 启动 Voyager」",
    PILOT_STOP: "点「■ 停止」",
    IMPORT: "提交一条人导入意图",
    TAB: "切换面板分区",
    CLEAR_LOG: "点「清空日志」",
    EXPORT: "导出 md",
    DB_PROBE: "点「验货」（检查新数据库）",
    DB_SWITCH: "点「切换数据库」",
    RESET: "点「清空学习状态」",
    PRACTICE_SCOPE_SET: "点「练习此知识点」（设练习范围）",
    PRACTICE_SCOPE_CLEAR: "点「清除范围」",
}


def _next_seq(rows: list[dict[str, Any]]) -> int:
    highest = 0
    for r in rows:
        try:
            highest = max(highest, int(r.get("seq") or 0))
        except (TypeError, ValueError):
            continue
    return highest + 1


def append(kind: str, *, detail: dict[str, Any] | None = None,
           result: str = "") -> dict[str, Any]:
    rows = load()
    ev = {
        "seq": _next_seq(rows),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "action": ACTION_LABEL.get(kind, kind),
        "detail": detail or {},
        "result": result,
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def load() -> list[dict[str, Any]]:
    if not LOG.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.sort(key=lambda r: int(r.get("seq") or 0))
    return out


def difficulty_of(topic_id: str) -> int:
    """topic_id -> 难度。出题器自选时不知道，返回 0。"""
    if not topic_id:
        return 0
    try:
        from tasks import TASKS
        return int(TASKS[topic_id].difficulty)
    except Exception:
        return 0


def recent(limit: int = 50) -> list[dict[str, Any]]:
    return load()[-limit:]


def reset() -> None:
    if LOG.exists():
        LOG.unlink()
    print(f"已清空界面交互事件流：{LOG}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "reset":
        reset()
    else:
        for e in recent(30):
            print(f"#{e['seq']} {e['ts'][11:19]} {e['action']:24s} "
                  f"{json.dumps(e['detail'], ensure_ascii=False)}"
                  + (f"  -> {e['result']}" if e.get("result") else ""))
