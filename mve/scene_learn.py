#!/usr/bin/env python3
"""场景学习循环：**只学当前场景自己的任务**（被 pilot 在非 VLML 场景下拉起）。

用户 2026-10-19 的设计原话：

    【人导入意图】「现在最前沿的世界模型是什么」→ 未命中、取到真空、写入图谱
    → 驾驶舱 ▶ 启动 Voyager【学习】= 开始拆分 **B 场景、B 图谱的任务**，
    并且规划课程 → 用编排（从猫娘搬来的 MCP、插件、联网能力）去达到任务
    （成功依据是取没取到内容）→ 人再次导入 → 命中、给出答案、掌握度变化。

为什么需要这个文件：驾驶舱原来的 `learn` 单元跑的是 `learn.py`（VLML 课程，
vlml_grain_* 那套）—— 在 Scene B 下点「跑学习单元」，跑的却是 VLML 的题，
这正是用户截图问的「为什么在场景 b 下会跑 VLML 的学习」。

所以本文件就是**非 VLML 场景的学习单元**：
  · 任务来源 = 这个场景**自己**的账：coach_log 里本场景没答出来（fetched_empty /
    failed）的导入问题 + 待审区条目；已答出（written）的不重学（同「不再重复出题」）。
  · 执行 = `learn_orchestrate.start_learning(question, scene)`：拆分 B 图谱任务 →
    补缺口 → 本机/联网原语取数（成功判据 = 取没取到内容）→ 入库 → 结晶。
  · 场景显式传参，**不跟「当前场景」漂**（后台线程/子进程里人可能切了场景）。

用法（pilot 拉起）：python mve/scene_learn.py --scene B --units 4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(HERE), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

COACH_LOG = HERE / "coach_log.jsonl"
PILOT_STATE = HERE / "pilot_state.json"


def _asked_to_stop() -> bool:
    """与驾驶舱共用一个停止信号：点「■ 停止」后跑完当前这一单元才退。"""
    try:
        return bool(json.loads(PILOT_STATE.read_text(encoding="utf-8")
                               ).get("stop_requested"))
    except Exception:
        return False


def _log(text: str) -> None:
    print(text, flush=True)


def scene_questions(scene_id: str) -> list[dict[str, Any]]:
    """这个场景**自己**的学习任务清单（去重、新→旧）。

    · 没答出来的（fetched_empty / failed）→ 要学；
    · 已经答出来的（written）→ 不重学（同「不再重复出题」）；
    · 待审区条目（人导入了但还没建到活图谱的）也排进来。

    ⚠️ 「答出来过」看**最近一次**的判读，不是「历史上曾经答出来过」。
    实测（2026-10-19 服务器）：同一句先被联网答出来（written），
    之后关掉联网能力再导入 → fetched_empty（真空、需外部数据源）。
    旧写法因为历史上那条 written 就把它永久判「已掌握」，
    驾驶舱于是显示「待学任务 0 个」—— 明明刚刚才答不出来。
    当前答不出来 = 要学；历史上答出来过不能抵消这一点。
    """
    latest: dict[str, str] = {}
    todo: list[str] = []
    try:
        if COACH_LOG.exists():
            rows = [json.loads(l) for l in
                    COACH_LOG.read_text(encoding="utf-8").splitlines()
                    if l.strip()]
        else:
            rows = []
    except Exception:
        rows = []
    for r in rows:                                   # 旧→新扫一遍：留最后一次判读
        if str(r.get("root_scene") or "") != scene_id:
            continue
        if str(r.get("mode") or "") != "voyager_consume":
            continue
        q = str(r.get("question") or "").strip()
        if not q:
            continue
        latest[q] = str(r.get("judgment_result") or "")   # 后写的覆盖先写的
    done = {q for q, v in latest.items() if v == "written"}
    for r in reversed(rows):                         # 新→旧排任务
        if str(r.get("root_scene") or "") != scene_id:
            continue
        if str(r.get("mode") or "") != "voyager_consume":
            continue
        q = str(r.get("question") or "").strip()
        if q and q not in done and q not in todo:
            todo.append(q)
    # 待审区（人导入了、还没解出来的另一本账）
    try:
        import routing
        for item in routing.pending_for_scene(scene_id) or []:
            q = str((item or {}).get("question") or "").strip()
            if q and q not in done and q not in todo:
                todo.append(q)
    except Exception:
        pass
    return [{"question": q} for q in todo]


def _run_one(q: str, scene: str) -> None:
    """学一道（同构原版 voyager.py:326 那圈 rollout：取任务 → 写代码/取数 → 成功加技能）。
    结果自身会落 run_log（轨迹页可见），不在这里另记。"""
    _log(f"\n----- 场景学习 · Scene {scene} · {q} -----")
    t0 = time.time()
    try:
        r = asyncio.run(learn_orchestrate.start_learning(q, scene))
    except Exception as e:
        r = {"status": "failed", "why": f"{type(e).__name__}: {e}"}
    status = str(r.get("status") or "")
    why = str(r.get("why") or "")
    _log(f"[场景学习] → status={status}（{time.time() - t0:.0f}s）"
         + (f" · {why[:160]}" if why else "")
         + (f" · 后端={((r.get('backend') or {}).get('kind'))}" if r.get("backend") else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", required=True)
    ap.add_argument("--units", type=int, default=4,
                    help="每轮从待学队列最多取几个学（不是总上限——学完一轮会再读队列，"
                         "随你导入随学）。默认 4。")
    ap.add_argument("--question", default="",
                    help="强制重学这一句（即使账上记过 written —— 用于把历史误记的"
                         "『成功』纠正回来）。先单独跑一次，再进入正常循环。")
    args = ap.parse_args()
    scene = args.scene
    units = max(1, min(50, args.units))
    forced = str(args.question).strip()

    import learn_orchestrate

    # 强制重学：先单独跑一次（纠正误记），再进入正常循环。
    if forced:
        _log(f"\n[场景学习] 强制重学这一句（纠正历史误记的 written）：{forced}")
        _run_one(forced, scene)

    # 同构原版 Voyager learn() 的「while True」：每轮从课程树取下一个任务执行，
    # **只有人点「■ 停止」（SIGTERM → _asked_to_stop）或队列真的耗尽才退**——
    # 不自己发明空闲 sleep、不靠计时器掐断、不跑 1 题就收。每轮重读队列，
    # 你跑着的时候新导入的问题会被自动接进来接着学（这就是「随导入随学」）。
    _log(f"\n[场景学习] Scene {scene} · 进入 Voyager 式循环（while True，"
         f"每轮最多 {units} 题，点「■ 停止」才退）· {time.strftime('%Y-%m-%d %H:%M:%S')}")
    while True:
        if _asked_to_stop():
            _log("\n[场景学习] 收到停止信号（人在驾驶舱点了停止），跑完当前题退出。")
            break
        tasks = scene_questions(scene)[:units]
        if not tasks:
            # 队列真空 = 这个场景当前没有没答出来的导入问题了。
            # 同构 Voyager：课程树走完就退出（voyager.py:328 `if not self.env.cheat_n_save(): break`）。
            # 不是故障、不是超时，是「该学的都学完了」；你再导入新问题，重新点
            # 「▶ 启动 Voyager」即可。
            _log("[场景学习] 待学队列空了 —— 当前场景没答出来的导入问题都学完了，循环结束。"
                 "（再导入新问题，点「▶ 启动 Voyager」会接着学。）")
            break
        _log(f"\n[场景学习] 本轮待学 {len(tasks)} 个（队列上限 {units}）")
        for i, t in enumerate(tasks, 1):
            if _asked_to_stop():
                _log(f"\n[场景学习] 收到停止信号，跑完当前题退出（{i - 1}/{len(tasks)}）。")
                break
            _run_one(t["question"], scene)
        # 不 return —— 回到 while 顶，重读队列、随导入随学。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
