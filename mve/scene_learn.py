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

# 模块级导入：_run_one 是模块级函数，必须能直接取到 learn_orchestrate。
# 原来它写在 main() 局部 → 模块级作用域里没有这个名字 →
#   NameError: name 'learn_orchestrate' is not defined
# 于是 start_learning 一行都没跑，学习主循环（拆分/补缺口/MCP/取数/入库）全空转。
# learn_orchestrate 不反向 import scene_learn（已 grep 确认只在注释里出现），
# 放到顶层没有循环导入风险。
import learn_orchestrate  # noqa: E402


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
    # ⚠️ **不能**拿 coach_log 的 judgment_result=="written" 当"课程已完成"。
    # 那是**消费侧**的判读（人问一句、当场答出来），原版里对应的是"这次答复成功"，
    # 不是 curriculum 的两本账。原版课程账只由 curriculum 自己记
    # （completed_tasks / failed_tasks，curriculum.py:330-365）。
    # 2026-10-19 实测踩过：Scene D 导入「什么是阿斯伯格（孤独症）」→ 消费层
    # 用模型记忆写了段 RESULT={"answer": …} 就判 written → 这句话立刻被踢出
    # 待学队列 → 队列空 → 自动课程围着空目录编出 room_data.txt，用户看到的
    # 就是「课程完全没按我导入的问题来」。
    # 现在课程完成只认下面这本账（学习链路自己落盘的 learned/partial），
    # 与消费侧判读解耦 —— 这就是原版「课程账 vs 执行结果」的分工。
    learned_done: set[str] = set()
    try:
        import run_log as _rl
        for l in _rl.load_all():
            if str(l.get("mode")) != "scene_learn":
                continue
            if str(l.get("scene") or "A") != scene_id:
                continue
            if str(l.get("learn_status") or "") in ("learned", "partial"):
                learned_done.add(str(l.get("question") or "").strip())
    except Exception:
        pass
    for r in reversed(rows):                         # 新→旧排任务
        if str(r.get("root_scene") or "") != scene_id:
            continue
        if str(r.get("mode") or "") != "voyager_consume":
            continue
        q = str(r.get("question") or "").strip()
        if q and q not in learned_done and q not in todo:
            todo.append(q)
    # 待审区（人导入了、还没解出来的另一本账）
    try:
        import routing
        for item in routing.pending_for_scene(scene_id) or []:
            q = str((item or {}).get("question") or "").strip()
            if q and q not in learned_done and q not in todo:
                todo.append(q)
    except Exception:
        pass
    return [{"question": q} for q in todo]


def _log_learning_detail(r: dict[str, Any]) -> None:
    """把一轮学习的「拆解 / 暴露能力 / 取数 / 入库」打到日志 —— 面板运行日志
    才看得见学习主循环（用户 2026-10-19：原来只打 status，看不到课程规划/
    任务拆解/用了什么能力）。失败也照打，方便定位。"""
    if not isinstance(r, dict):
        return
    plan = r.get("plan") or {}
    subs = plan.get("subtasks") or plan.get("subgoals") or []
    if subs:
        needs = [str(s.get("need") or s.get("goal") or "") for s in subs]
        _log("  · 拆解子目标(" + str(len(needs)) + ")：" +
             "；".join(n[:42] for n in needs if n))
    exposed = r.get("exposed") or {}
    if exposed.get("exposed"):
        _log("  · 暴露的远端/MCP·猫娘能力：" +
             "，".join(str(x) for x in exposed["exposed"][:12]))
    elif exposed.get("connected"):
        _log("  · 已连 MCP server：" +
             "，".join(str(c.get("server")) for c in exposed["connected"]))
    elif exposed.get("reason"):
        _log("  · MCP：" + str(exposed["reason"])[:80])
    execu = r.get("execution") or {}
    if execu:
        _log(f"  · 取数：calls={execu.get('calls')} 真取到={execu.get('real_calls')} "
             f"行数={execu.get('rows')} ok={execu.get('ok')} "
             f"证据={execu.get('evidence') or 'unknown'}"
             + ("（知识型：无数据源，成败交 critic 判）"
                if execu.get("evidence") == "knowledge" else ""))
        sql = str(execu.get("sql") or "")
        if sql:
            _log("  · 脚本/SQL(head)：" + sql[:160].replace("\n", " "))
    absorbed = r.get("absorbed") or {}
    if absorbed:
        _log(f"  · 入库：topic={absorbed.get('topic_id')} 掌握={absorbed.get('mastered')} "
             f"动作={absorbed.get('skill_action')}")
    ms = r.get("milestone") or {}
    if ms.get("topic_id"):
        _log("  · 里程碑解锁：" + ("是" if ms.get("unlocked") else "否") +
             f"（{ms.get('topic_id')}）")
    cry = r.get("crystallized") or {}
    if isinstance(cry, dict) and cry.get("ok"):
        _log("  · 技能结晶：" + str(cry.get("entry") or cry.get("name") or "ok"))
    # 闸门（Maieutic spec gate）：逐维度答到没答到 —— 这是"任务完成没有"的硬判据
    gate = (r.get("critic") or {}).get("gate") or []
    if gate:
        ok_n = sum(1 for g in gate if isinstance(g, dict)
                   and g.get("answered") is not False)
        _log(f"  · 闸门：{ok_n}/{len(gate)} 个维度答到"
             + ("（缺的那条判 drift = 这轮不算完成）" if ok_n < len(gate) else ""))


def _run_one(q: str, scene: str, *, context: str = "",
             max_retries: int = 4) -> dict[str, Any]:
    """一次 rollout（原版 voyager.py:298-301）+ 内层重试（voyager.py:214-295）。

    原版机制照抄：
      · 单任务最多 `action_agent_task_max_retries=4` 次（voyager.py:30/:277-279）；
      · 每次失败把 **critic 的 critique 拼进下一次**（:262-268）—— 迭代提示，
        不是"同一段 prompt 重跑"；
      · 4 次仍不过 → 任务进 failed_tasks，课程换下一个（curriculum.py:330-345
        "Skipping to next task"），**不会原地无限重试**。
    """
    _log(f"\n----- 场景学习 · Scene {scene} · {q} -----")
    critique = ""
    info: dict[str, Any] = {"task": q, "success": False, "critique": "",
                            "attempts": 0, "result": {}}
    for i in range(max(1, int(max_retries))):
        t0 = time.time()
        try:
            r = asyncio.run(learn_orchestrate.start_learning(
                q, scene, critique=critique, judge=True, context=context))
        except Exception as e:
            r = {"status": "failed", "why": f"{type(e).__name__}: {e}"}
        status = str(r.get("status") or "")
        why = str(r.get("why") or "")
        _log(f"[场景学习] 第 {i + 1}/{max_retries} 次 → status={status}"
             f"（{time.time() - t0:.0f}s）"
             + (f" · {why[:160]}" if why else "")
             + (f" · 后端={((r.get('backend') or {}).get('kind'))}"
                if r.get("backend") else ""))
        _log_learning_detail(r)
        # 完成判定：learn_orchestrate 里已经调过 critic（judge=True，原版在
        # rollout 判、判过才入库）。这里只取结论；没判过（prepared/unreachable）
        # 才自己再判一次。
        c = r.get("critic") or {}
        if "success" in c:
            success, critique2 = bool(c.get("success")), str(c.get("critique") or "")
        else:
            try:
                import task_critic
                success, critique2 = task_critic.check_task_success(q, context, r)
            except Exception as e:
                success, critique2 = False, f"critic 失败：{type(e).__name__}: {e}"
        info.update(task=q, success=bool(success),
                    critique=str(critique2 or ""), attempts=i + 1, result=r)
        _log("  · critic 判完成：" + ("是" if success else "否")
             + (f" · 评语：{str(critique2)[:160]}"
                if (not success and critique2) else ""))
        if success:
            break
        critique = str(critique2 or why or "")
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", required=True)
    ap.add_argument("--units", type=int, default=4,
                    help="每次从待学队列取多少个候选（兼容保留；原版每轮只学 1 个）。")
    ap.add_argument("--question", default="",
                    help="强制重学这一句（即使账上记过 written —— 用于把历史误记的"
                         "『成功』纠正回来）。先单独跑一次，再进入正常循环。")
    ap.add_argument("--curriculum", choices=["auto", "queue"], default="auto",
                    help="auto=原版 Voyager 行为（人导入的题优先，队列空了自动出题，"
                         "不停；默认）；queue=只学人导入的题，队列空就退。")
    ap.add_argument("--max-retries", type=int, default=4,
                    help="单任务最多重试几次（原版 action_agent_task_max_retries=4）")
    ap.add_argument("--max-iterations", type=int, default=160,
                    help="最多学多少个任务（原版 max_iterations=160）")
    args = ap.parse_args()
    scene = args.scene
    units = max(1, min(50, args.units))
    max_retries = max(1, min(10, args.max_retries))
    max_iter = max(1, int(args.max_iterations))
    forced = str(args.question).strip()

    import curriculum_auto
    cur = curriculum_auto.agent_for(scene)

    # 强制重学：先单独跑一次（纠正误记），再进入正常循环。
    if forced:
        _log(f"\n[场景学习] 强制重学这一句（纠正历史误记的 written）：{forced}")
        info = _run_one(forced, scene, max_retries=max_retries)
        cur.update_exploration_progress(info)

    # 同构原版 voyager.py:326 `while True`：每轮 propose_next_task → rollout →
    # update_exploration_progress（completed/failed 两本账）→ 下一轮。退出只有三种：
    # 人点「■ 停止」、达到 --max-iterations（原版 160）、课程提不出任务（数据源空/LLM 挂）。
    _log(f"\n[场景学习] Scene {scene} · 进入 Voyager 式循环"
         f"（原版 learn()：propose → rollout → 记账；已完成 {len(cur.completed_tasks)}"
         f" / 失败 {len(cur.failed_tasks)}；任务上限 {max_iter}，单任务重试 {max_retries}）"
         f" · {time.strftime('%Y-%m-%d %H:%M:%S')}")
    iteration = 0
    while True:
        if _asked_to_stop():
            _log("\n[场景学习] 收到停止信号（人在驾驶舱点了停止），当前任务已结束，退出。")
            break
        if iteration >= max_iter:
            _log(f"[场景学习] 达到任务上限 {max_iter}（原版 max_iterations），退出。"
                 "已完成 " + str(len(cur.completed_tasks)) + "，失败 "
                 + str(len(cur.failed_tasks)) + "。")
            break
        # 队列 = 人导入的题（原版 manual 模式的对应物）；两本账里的不再出
        queued = [t for t in scene_questions(scene)[:units]
                  if t["question"] not in cur.completed_tasks
                  and t["question"] not in cur.failed_tasks]
        if not queued and args.curriculum != "auto":
            _log("[场景学习] 队列空了（queue 模式：只学人导入的题），退出。"
                 "要原版那种不停出题，用 --curriculum auto。")
            break
        try:
            task, context = cur.propose_next_task(
                queue=[t["question"] for t in queued], max_retries=5)
        except Exception as e:
            if "Max retries reached" in str(e):
                # 原版在这里也是 raise（curriculum.py:294-296）；它不是故障，是
                # "当前数据被学完了、模型提不出不重复的题"。日志要说人话。
                _log("[场景学习] 自动课程连续 5 次提不出可用的新题（"
                     + str(e) + "）—— 这个场景现有数据大概已经学完了"
                     f"（已完成 {len(cur.completed_tasks)} 条）。"
                     "放新数据 / 导入新问题 / 改写问题后再启动，不是故障。")
            else:
                _log(f"[场景学习] 课程规划失败：{type(e).__name__}: {e} —— 退出。")
            break
        if not task:
            _log("[场景学习] 课程提不出下一个任务"
                 + (f"：{context}" if context else
                    "（工作区没数据 / LLM 不可用 / 连续 5 次解析失败）")
                 + " —— 如实退出，不硬凑。")
            break
        iteration += 1
        info = _run_one(task, scene, context=context, max_retries=max_retries)
        # 原版 curriculum.update_exploration_progress：成功进 completed、失败进 failed
        cur.update_exploration_progress(info)
        _log(f"[场景学习] 进度 {iteration}/{max_iter}：已完成 "
             f"{len(cur.completed_tasks)} / 失败 {len(cur.failed_tasks)}"
             + ("（失败→Skipping to next task）" if not info.get("success") else ""))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
