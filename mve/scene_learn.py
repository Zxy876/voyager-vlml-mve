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
    latest_scope: dict[str, str] = {}
    latest_db: dict[str, bool | None] = {}
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
        # 这条题**规划里有没有 db 路**（None = 老记录，没有 plan_routes 字段）。
        # 学习侧的学习单元 = 环境里的一次 rollout；规划没排 db 路 = 这题不需要
        # 环境（联网题 / 概念题 / 聊天），排进待学只会让驾驶舱反复拆库内维度、
        # 白烧轮次（实测 b2a70f4：外部对比题硬拆 5 个库内维度、learned 空；
        # 那时还会轮次攒到 given_up —— 2026-10-29 起 given_up 只作观测、不跳过）。
        # 同一把尺子：规划决定走不走环境（_use_env），
        # 也就决定学不学。老记录保持旧行为（宁可学，不静默丢）。
        _pr = r.get("plan_routes")
        latest_db[q] = (bool((_pr or {}).get("db"))
                        if isinstance(_pr, dict) and _pr else None)
        latest_scope[q] = str(r.get("scope_line") or "")
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
        if latest_db.get(q) is False:
            continue             # 规划里没 db 路：不排学习（见上，含待审区）
        if q and q not in learned_done and q not in todo:
            todo.append(q)
    # 待审区（人导入了、还没解出来的另一本账）
    # ⚠️ 同一把尺子也管这里：待审区的题若已判过「规划里没有 db 路」，同样
    # 不排 —— 否则上一段的过滤被这一段的补录洗掉（服务器实测：三句外部题
    # 就是从这里被加回待学队列的）。
    try:
        import routing
        for item in routing.pending_for_scene(scene_id) or []:
            q = str((item or {}).get("question") or "").strip()
            if latest_db.get(q) is False:
                continue
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
    # 2026-10-19 修：维度即子任务后 critic.gate 是 {addressed/open/questions}，
    # 旧打印按 list 数（len = 3 个 key）→ 打出"0/3"这种假数字。
    dres = r.get("dimension_results")
    if dres:
        _ok = dres.get("learned") or []
        _bad = dres.get("failed") or []
        _log(f"  · 维度子任务：{len(_ok)}/{dres.get('total')} 个答到"
             + (f"（未闭合：{'，'.join(str(f.get('id') or '') for f in _bad)}）"
                if _bad else "（全部闭合）"))
    ms = r.get("milestone_scaffold") or {}
    if ms:
        _name = str(ms.get("name") or "")
        _log(f"  · 里程碑：{_name}（{ms.get('closed')}/{ms.get('total')} 闭合）"
             + ("✅ 达成" if ms.get("achieved")
                else f"未达成（还差：{'，'.join(ms.get('missing') or []) or '？'}）"))
        # 重试多轮仍不闭合的维度：**只作观测**（2026-10-29 按原版 inference 改：
        # 子目标失败不跳过，下一轮还是它，止损只有外层 max_iterations）。
        _gu = ms.get("given_up") or []
        if _gu:
            try:
                import maieutic_scaffold as _msc
                _n_gu = int(getattr(_msc, "GIVE_UP_AFTER_ROUNDS", 3))
            except Exception:
                _n_gu = 3
            _log(f"  · 顽固维度 {len(_gu)} 条（已重试 ≥{_n_gu}"
                 f" 轮仍未闭合，按原版语义继续重试、不跳过）："
                 + "，".join(str(x) for x in _gu))
    sk = r.get("skills") or {}
    if sk:
        _add = sk.get("added") or []
        _skip = sk.get("skipped") or []
        _log(f"  · 技能入库：{len(_add)} 条"
             + (f"（{', '.join(str(a.get('dim')) for a in _add)}）" if _add else "")
             + (f"；跳过 {len(_skip)} 条（没调取数动作，不是配方）" if _skip else "")
             + (f" · {sk['error']}" if sk.get("error") else ""))
    gate = (r.get("critic") or {}).get("gate") or {}
    if isinstance(gate, dict) and (gate.get("addressed") or gate.get("open")):
        _open = gate.get("open") or []
        _log(f"  · 闸门：闭合 {len(gate.get('addressed') or [])} 条"
             f" / 未闭合 {len(_open)} 条"
             + ("（未闭合 = 这轮不算完成，维度 id 留在进度里下轮补）"
                if _open else "（闭合 → 判完成）"))


def _light_up_milestone(scene: str, question: str, db_path: str,
                        max_retries: int) -> bool:
    """phase-3（天授 test/watch 同构）：里程碑前置维度全闭合后，**重试里程碑本身**
    （重跑合成题、组合已学原子技能）直至取到真数为止（用户 2026-10-25：
    「完成前置任务后重试里程碑直至里程碑任务成功为止」）。

    原版 inference() 语义（voyager.py `while progress < len(sub_goals)`）：
    子目标失败**不跳过**，下一轮 while 立刻重试同一子目标、无重试上限 ——
    唯一上限是外层主循环的 max_iter。所以这里一次只重试一遍：
      · 取到真数（成功）→ mark_milestone_achieved 点亮节点，返回 True；
      · 没取到（失败）→ **不点亮**（维度已学的事实照旧，进度账不动），
        返回 False —— 调用方下一轮 while 继续重试同一里程碑，绝不降级
        自由出题。旧版「失败也点亮 + 降级」的假掌握就是这里漏出去的。
    组合复合技能入库（learn_milestone_composite）挂在 learn_orchestrate 的
    achieved 分支，重跑成功的那一轮会自动执行，无需在这里重复。
    """
    fetched = False
    # 跨轮评语缓存（2026-10-26）：原版 inference 的重试发生在**同一个 agent
    # 会话**里，critic 反馈天然可见；MVE 每轮重试是冷启动 —— 不把上一轮的
    # 失败评语带进去，LLM 会用同样的输入重复同样的错误（实测：组合层把
    # 题面词「工资」写进 SQL 被守卫拒 → 整轮 no_data → 10s 后原样重试）。
    _prev = getattr(_light_up_milestone, "_last_crit", "") or ""
    try:
        if _asked_to_stop():
            _log("[场景学习] 收到停止信号，跳过本轮里程碑重试（已学的维度保持）。")
            return False
        t0 = time.time()
        info = _run_one(question, scene, max_retries=max_retries,
                        critique=_prev, milestone_retry=True)
        r0 = info.get("result", {}) or {}
        # 「真取到」口径（2026-10-26 修）：real_calls/rows 挂在 r0["execution"]
        # 嵌套层（learn_orchestrate out["execution"]），顶层键不存在 —— 只读
        # 顶层会永远 fetched=False，哪怕本体 rollout 真取了 4 次数（实测：
        # calls=4 真取到=4 证据=data 却判"未取到真数"，死活不点亮）。
        _exec0 = r0.get("execution") or {}
        fetched = (bool(r0.get("real_calls")) or bool(_exec0.get("real_calls"))
                   or (int(r0.get("rows") or 0) > 0)
                   or (int(_exec0.get("rows") or 0) > 0))
        _log(f"[场景学习] 里程碑重试 → status={r0.get('status')}"
             f"（{time.time() - t0:.0f}s，真取到={fetched}）")
        _log_learning_detail(r0)
        # 失败评语 = critic 评语 + 复合技能组合失败原因（守卫/fan-out/执行
        # 拒因在这里），下一轮重试带进去 —— 重试才有修正的信息，不是原样重摇。
        _parts = [str(((r0.get("critic") or {}).get("critique")) or "")]
        _mcomp = r0.get("milestone_skill") or {}
        if isinstance(_mcomp, dict) and _mcomp.get("skipped"):
            _parts.append("上轮里程碑复合技能组合失败："
                          + str(_mcomp["skipped"]))
        _light_up_milestone._last_crit = "；".join(p for p in _parts if p)[:600]
    except Exception as e:
        _log(f"[场景学习] 里程碑重试异常（按未成功处理，下轮继续）："
             f"{type(e).__name__}: {e}")
    if not fetched:
        _log("[场景学习] 里程碑重试未取到真数 → 本轮不点亮、不解锁，"
             "下一轮继续重试同一里程碑（原版 inference 语义：失败不跳过；"
             "唯一上限是外层 max_iter）。")
        return False
    try:
        import maieutic_scaffold as _ms
        _ms.mark_milestone_achieved(scene, question, fetched=True)
    except Exception as e:
        _log(f"[场景学习] 里程碑点亮写盘失败（按未成功处理，下轮继续）："
             f"{type(e).__name__}: {e}")
        return False
    _log("[场景学习] 里程碑任务成功：重跑取到真数 → 节点点亮变实"
         "（组合复合技能入库由本轮 learn_orchestrate achieved 分支自动完成）。")
    return True


def _run_one(q: str, scene: str, *, context: str = "",
             max_retries: int = 1, critique: str = "",
             preset_dimension: dict | None = None,
             milestone_retry: bool = False) -> dict[str, Any]:
    """一次外层 rollout（原版 voyager.py:298-301，默认**只跑 1 次**）。

    同构修正（2026-10-24）：原版每轮课程对每个任务只 rollout 一次
    （voyager.py:326-330），任务内部的动作级 4 次重试
    （action_agent_task_max_retries，voyager.py:30/:277-279）发生在**执行层
    solve 的 4 步迭代**里 —— 之前这里默认 4 × 内层 4 = 16 次执行，是原版没有
    的双层重试。默认改回 1；失败 → 进 failed 账换下一个任务
    （curriculum.py:330-345 "Skipping to next task"），人导入题由主循环
    沉底重试（那是 manual 语义，见下方主循环注释）。

    每次失败把 critic 的 critique 拼进下一次（:262-268 迭代提示）——
    本函数保留该机制，供 max_retries>1 的显式调用方使用。
    """
    _log(f"\n----- 场景学习 · Scene {scene} · {q} -----")
    critique = str(critique or "")   # 重试题带入上次的失败评语（迭代提示初值）
    info: dict[str, Any] = {"task": q, "success": False, "critique": "",
                            "attempts": 0, "result": {}}
    for i in range(max(1, int(max_retries))):
        t0 = time.time()
        try:
            r = asyncio.run(learn_orchestrate.start_learning(
                q, scene, critique=critique, judge=True, context=context,
                preset_dimension=preset_dimension,
                milestone_retry=milestone_retry))
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
                from reward import scene_has_env as _she
                success, critique2 = task_critic.check_task_success(
                    q, context, r, has_env=_she(scene))
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
    ap.add_argument("--max-retries", type=int, default=1,
                    help="外层 rollout 次数，默认 1（原版 voyager.py:326-330 每轮"
                         "只 rollout 一次，判不过 → 进 failed 换下一个任务）；"
                         "单任务内部的 4 步迭代在执行层 solve 里"
                         "（对应原版 action_agent_task_max_retries=4，"
                         "voyager.py:30/:277-279），两层各管各的，不再相乘。")
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
    tried_this_run: set[str] = set()   # 单次启动内防死循环：同一题无论成败只重学一次
    while True:
        if _asked_to_stop():
            _log("\n[场景学习] 收到停止信号（人在驾驶舱点了停止），当前任务已结束，退出。")
            break
        if iteration >= max_iter:
            _log(f"[场景学习] 达到任务上限 {max_iter}（原版 max_iterations），退出。"
                 "已完成 " + str(len(cur.completed_tasks)) + "，失败 "
                 + str(len(cur.failed_tasks)) + "。")
            break
        # ⚠️ 里程碑维度优先（用户 2026-10-24）：场景有未完成里程碑时，选题源
        # 从「LLM 自由出题」改成「该里程碑的未闭合维度」，逐维度 solve→落技能
        # →全闭合解锁；全部完成后才降级回下面的自由出题。这样自动学习精准长
        # 在目标的前置阶梯上，不再散点（之前跑出「李娜出勤」「销售出勤」这类
        # 与目标「求出九月份总工资」完全无关的题，就是因为驾驶舱从没看过里程碑）。
        try:
            import maieutic_scaffold as _ms
            import scene_profile as _sp
            _tgt = _ms.current_target(scene)
            if _tgt:
                _dbp = str((_sp.profile(scene) or {}).get("db_path") or "")
                _unclosed = _ms.milestone_unclosed(scene, _tgt, db_path=_dbp)
                _cool = _ms.milestone_cooldown(scene, _tgt)
                if _unclosed and _cool > 0:
                    # 整题层节流（原版 learn() 语义，2026-10-31）：上一次整题
                    # rollout 0 闭合 = 这个 task 失败 → 下一轮**换题**，先做
                    # N 道自由出题把前置能力长出来再回来（不是把维度判死）。
                    _log(f"[场景学习] 里程碑「{_tgt[:36]}」上一轮整题 0 闭合"
                         f"（原版 learn()：失败进 failed_tasks、下一轮换题）；"
                         f"先做 {_cool} 道自由出题再回来重试"
                         f"（还剩 {len(_unclosed)} 条未闭合维度，不跳过）。")
                elif _unclosed:
                    _log(f"\n[场景学习] 当前里程碑「{_tgt[:42]}」有 {len(_unclosed)}"
                         f" 个未闭合维度 → 驾驶舱优先闭合它们"
                         f"（而不是自由出题）")
                    _before = set(_ms.load_progress(scene, _tgt))
                    for _d in _unclosed:
                        if _asked_to_stop():
                            break
                        iteration += 1
                        tried_this_run.add(_tgt)
                        _log(f"\n----- 场景学习 · Scene {scene} · 里程碑维度："
                             f"{str(_d.get('question') or '')[:50]} -----")
                        t0 = time.time()
                        info = _run_one(_tgt, scene, max_retries=max_retries,
                                        preset_dimension=_d)
                        _r0 = info.get("result", {}) or {}
                        _log(f"[场景学习] 里程碑维度闭合 → status="
                             f"{_r0.get('status')}（{time.time() - t0:.0f}s）")
                        _log_learning_detail(_r0)
                        # 里程碑维度不进 cur.failed_tasks（那是自由课程的账）；
                        # 进度由 maieutic_scaffold.save_progress 记，断点续学认得。
                    # 整题层记账（原版 update_exploration_progress）：本轮有几条
                    # 新闭合 = 有没有进展。0 闭合 → 这次 rollout 算失败，挂
                    # cooldown、**不 continue**（下一轮换题）；否则继续攻剩下的。
                    _after = set(_ms.load_progress(scene, _tgt))
                    _new = len(_after - _before)
                    _rec = _ms.note_milestone_round(scene, _tgt, new_closed=_new)
                    _log(f"[场景学习] 整题记账：本轮新闭合 {_new} 条"
                         f"（累计轮次 {_rec.get('rounds')}、失败轮 "
                         f"{_rec.get('fail_rounds')}）"
                         + ("" if _new else
                            " → 按原版 learn() 记 failed，下一轮让 curriculum 出新题"))
                    # 维度闭合只是**前置任务完成**，里程碑本身要重跑取到真数才算
                    # 成功（用户 2026-10-25：「完成前置任务后重试里程碑直至里程碑
                    # 任务成功为止」）。重试统一走下方「全闭合」分支，这里不再
                    # 顺手点亮 —— 旧版「失败也点亮 + 降级自由出题」正是缺的
                    # 「直至成功为止」循环语义、假掌握就是从这漏出去的。
                    if _new:
                        continue
                # ---- 前置维度全闭合 → 重试里程碑直至成功（原版 inference 语义）----
                # 子目标失败下一轮 while 立刻重试、无重试上限，唯一上限是外层
                # max_iter；成功（重跑取到真数）才点亮解锁、清锚点降级自由出题。
                _lit = _ms.milestone_lit(scene, _tgt)
                if _lit.get("total_dims") == 0:
                    # 骨架拆不出维度 → 不构成里程碑（与 milestone_state「0 维度
                    # 不报达成」同口径），释放锚点降级自由出题，不空转重试。
                    _log(f"[场景学习] 里程碑「{_tgt[:42]}」骨架无维度（拆解失败/"
                         f"非数据题），释放锚点降级自由出题。")
                    _ms.clear_current_target(scene)
                elif _lit.get("exhausted") and not _lit.get("achieved"):
                    # ⚠️ 2026-10-29 按原版 inference 改：子目标失败**不跳过**。
                    # 这里原来判 failed + 「降级自由出题」，那是替原版发明的取舍
                    # （原版 `while progress < len(sub_goals)` 里失败不推进 progress，
                    # 下一轮照样拿同一条，止损只有外层 max_iterations）。而且它把
                    # 两件事混成一件：「这条维度不可达」和「前置能力还没长出来」
                    # （用户原话：「查 webdoc 可能是我们让它做的里程碑的前置能力」）。
                    # 前置会在反复重试里被技能库补上，判死等于掐断这条学习曲线。
                    # 现在只如实报「这条很顽固」（人看得见），**落在下方自由出题**，
                    # 下一轮 while 再回来看它 —— 不判死、也不原地空转。
                    # 正常走不到这里：`milestone_unclosed` 已不再排除顽固维度，
                    # 只要还 missing 就一定在 `_unclosed` 里被上面的维度循环接走；
                    # 真走到说明骨架与进度账对不上（读缓存失败之类），如实报 + 降级。
                    _gu = _lit.get("given_up") or []
                    _n_gu = getattr(_ms, "GIVE_UP_AFTER_ROUNDS", 3)
                    _log(f"[场景学习] 里程碑「{_tgt[:42]}」这些维度已重试 ≥{_n_gu}"
                         f" 轮仍未闭合（{'，'.join(str(x) for x in _gu) or '？'}）"
                         f" → 按原版 inference 语义继续重试（不跳过、不判死）；"
                         f"本轮先降到自由出题，下一轮回来再攻。")
                elif not _lit.get("achieved"):
                    iteration += 1
                    if _light_up_milestone(scene, _tgt, _dbp, max_retries):
                        # 里程碑任务成功：点亮解锁 → 清锚点 → 本轮直接降级自由出题，
                        # 等下一次人导入立新锚点。
                        _ms.clear_current_target(scene)
                    else:
                        # 未成功：不解锁、不降级，下一轮 while 继续重试同一里程碑
                        continue
                # 成功或已点亮 → 落到下面的自由出题
        except Exception as e:
            _log(f"[场景学习] 里程碑锚点检查失败（降级回自由出题）："
                 f"{type(e).__name__}: {e}")
        # 队列 = 人导入的题（原版 manual 模式的对应物）。
        # ⚠️ 2026-10-24 机制修复：failed 的题**不再被永久屏蔽**——原版 failed
        #   账（curriculum.py:330-345 "Skipping to next task"）约束的是自动
        #   课程别原地打转；人导入的题是 manual 语义，人明确要它学会，失败
        #   过的下一轮要**沉底重试**（新鲜题优先），否则「导入→学→重试」
        #   闭环第 3 步永远走不通（实测工资题失败一次后就此消失）。
        #   单次启动内同一题只重学一次（tried_this_run），不无限打转。
        _qs = [t["question"] for t in scene_questions(scene)]
        fresh = [q for q in _qs
                 if q not in cur.completed_tasks and q not in cur.failed_tasks]
        retries = [q for q in _qs
                   if q in cur.failed_tasks and q not in cur.completed_tasks]
        queued = ([q for q in fresh if q not in tried_this_run]
                  + [q for q in retries if q not in tried_this_run])[:units]
        if not queued and args.curriculum != "queue":
            # 人导入队列空了 → 自动出题（auto 模式，原版 while True 的本义）。
            queued = []
        elif not queued and args.curriculum == "queue":
            _log("[场景学习] 队列空了（queue 模式：只学人导入的题），退出。"
                 "要原版那种不停出题，用 --curriculum auto。")
            break
        try:
            task, context = cur.propose_next_task(
                queue=[t for t in queued], max_retries=5)
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
            # queue 里的题都被账本挡住（理论上不该发生）→ 落到自动出题。
            task, context = cur.propose_next_task(max_retries=5)
            if not task:
                _log("[场景学习] 课程提不出下一个任务"
                     + (f"：{context}" if context else
                        "（工作区没数据 / LLM 不可用 / 连续 5 次解析失败）")
                     + " —— 如实退出，不硬凑。")
                break
        tried_this_run.add(task)
        # 重试题把上次的失败评语带回去当 critique 初值（原版把 critique 拼进
        # 下一次 rollout 的同一条通路）。
        retry_critique = ""
        if task in cur.failed_tasks:
            retry_critique = str(
                (getattr(cur, "_failed_why", {}) or {}).get(task) or "")[:300]
            if retry_critique:
                _log(f"[场景学习] 「{task[:36]}」上次失败过（沉底重试），"
                     f"带着上次的评语重学：{retry_critique[:120]}")
        iteration += 1
        # QA 攻略层（原版 voyager.py:419-424 的同构：rollout 前先
        # get_task_context(task)，qa_llm 出的 tips 拼进 context）。
        # 失败返回空串照常跑 —— 攻略层挂掉不能拦住学习。
        try:
            qa_ctx = cur.get_task_context(task)
        except Exception as e:
            qa_ctx = ""
            _log(f"[场景学习] QA 攻略层失败（照常继续）：{type(e).__name__}: {e}")
        if qa_ctx:
            _log("[场景学习] 攻略 tips 已注入 context（qa agent）：\n"
                 + qa_ctx[:400])
            context = "；".join(x for x in (context, qa_ctx) if x)
        info = _run_one(task, scene, context=context, max_retries=max_retries,
                        critique=retry_critique)
        # 原版 curriculum.update_exploration_progress：成功进 completed、失败进 failed
        cur.update_exploration_progress(info)
        # 整题层节流：做完一道自由出题 → 里程碑的 cooldown 减一（原版「学完
        # 相邻任务再回来」）。到了 0，下一轮 while 才会重新进里程碑维度循环。
        try:
            import maieutic_scaffold as _msc
            _t2 = _msc.current_target(scene)
            if _t2 and _msc.milestone_cooldown(scene, _t2) > 0:
                _left = _msc.tick_milestone_cooldown(scene, _t2)
                _log(f"[场景学习] 自由出题完成一道 → 里程碑重试冷却 -1"
                     f"（还剩 {_left}）")
        except Exception:
            pass
        _log(f"[场景学习] 进度 {iteration}/{max_iter}：已完成 "
             f"{len(cur.completed_tasks)} / 失败 {len(cur.failed_tasks)}"
             + ("（失败→Skipping to next task）" if not info.get("success") else ""))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
