#!/usr/bin/env python3
"""服务器回溯补账：给 4 条特斯拉 coach_log 记录补 root_scene（2026-10-18）。

背景：_log_import 白名单丢字段 bug（已修，见 commit e5b2589）导致这 4 条
2026-10-18 21:18-21:40 的记录没有 root_scene —— 导入时路由信息算过但
落盘时被白名单洗掉。现在按**场景创建时间戳**补：
  ts < scene_C.created_at（21:19:58）→ 归 A（当时 C 还不存在，导入的就是 A）
  ts >= scene_C.created_at           → 归 C

只补 root_scene。route_decision / route_score / judgment_result 当时**从未
落盘**（不是丢失，是没记）—— 现在拿当前图状态反推属于把"现在的读数"写成
"历史的事实"，不做。新记录从 e5b2589 起带全字段。

跑之前先备份 coach_log.jsonl → coach_log.jsonl.bak-<ts>。
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
COACH_LOG = HERE / "coach_log.jsonl"
SCENES = HERE / "scenes.json"


def main() -> int:
    scenes = json.loads(SCENES.read_text(encoding="utf-8"))
    scene_c = scenes.get("scenes", {}).get("C") or {}
    c_created = float(scene_c.get("created_at") or 0)
    c_created_dt = dt.datetime.fromtimestamp(c_created)
    print(f"Scene C created_at = {c_created_dt.isoformat()}")

    rows = [json.loads(l) for l in COACH_LOG.read_text(encoding="utf-8").splitlines()
            if l.strip()]
    changed = 0
    for r in rows:
        if r.get("root_scene"):
            continue
        ts = str(r.get("ts") or "")
        try:
            t = dt.datetime.fromisoformat(ts)
        except ValueError:
            print(f"  !! 无法解析 ts={ts!r}，跳过")
            continue
        scene = "C" if t.timestamp() >= c_created else "A"
        r["root_scene"] = scene
        changed += 1
        print(f"  {ts} | {(r.get('question') or '')[:24]} → root_scene={scene}")

    if not changed:
        print("没有需要补账的记录（都已有 root_scene）")
        return 0

    backup = COACH_LOG.with_suffix(f".jsonl.bak-{int(c_created)}")
    shutil.copy2(COACH_LOG, backup)
    print(f"已备份 → {backup.name}")

    COACH_LOG.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8")
    print(f"补账完成：{changed} 条记录写上 root_scene")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
