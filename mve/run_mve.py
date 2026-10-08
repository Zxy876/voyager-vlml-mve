#!/usr/bin/env python3
"""MVE 主循环：出题 → Voyager 取数 → 事实集 → 覆盖率 → verdict → mastery → 学习。

跑法：
    python mve/run_mve.py

要验证的问题：**Voyager 能不能通过积累技能，把同一道题做得更全。**
观测方式：同一道题连考三轮，看覆盖率与掌握度是否随技能积累上升。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import vlml_env  # noqa: F401,E402  必须先引导环境

import causal_timeline  # noqa: E402
import difficulty  # noqa: E402
import bypass_learn  # noqa: E402
import mastery_model  # noqa: E402
import mastery_retention as retention  # noqa: E402
import narrator  # noqa: E402
import planner  # noqa: E402
import run_log  # noqa: E402
import skill_store  # noqa: E402
import vlml0_referee  # noqa: E402
from loop_core import (  # noqa: E402
    AnswerSpec,
    RubricPoint,
    Task,
    evaluate_vs_referee,
    fact_key,
    task_done,
    update_mastery,
)
from voyager import LLMVoyager, ScriptedVoyager  # noqa: E402

from tasks import SERIES, TASKS, TASK, TASK_HARD, TASK_MID, TASK_PATTERN, next_topic  # noqa: E402

# 「任务完成」的判据（照原版 Voyager 的 self-verification）：
#   done = Voyager 自己的尺子：跑通编排（有成功工具调用）+ 拿到数（facts 非空）
#          + 没翻车（无工具错误）—— **不数覆盖点**。
# 用户拍板：学习阶段（存技能 / 停止同题 / 推进换题）不算覆盖点 —— 覆盖点是
# VLML 的尺子，只用于考核（exam transfer/final）与掌握度证据。原版 critic
# 判的就是「环境里可观测的任务目标达成」，不是「覆盖了多少评分点」。


def _stable_skill_name(task: Any) -> str:
    """技能的**稳定主键**（照原版 `program_name`）。

    原版技能的主键是函数名——稳定，所以同名会走 Rewriting 更新同一条。
    MVE 此前用 LLM 每轮生成的一句话当名字，措辞一变就是"新技能"：
    实测同一条经验被写成「按 team 维度下钻」「按 team 维度拆分」「按 team 下钻」三版，
    于是 versions 涨到 49、膨胀率 17.5，而每一版都没被真正更新过（ok=0）。
    名字必须由**题目**决定，不由模型当次的措辞决定。
    """
    return f"{getattr(task, 'topic_id', '') or 'task'}解法"


def _plan_program(topic_id: str, calls: list[dict[str, Any]]) -> tuple[str, str]:
    """把 plan 路径跑通的**调用序列**渲染成一段可读的程序（可 exec 形态）。

    为什么需要它：SQL 类题走 plan 路径，那条路不产出 `program_code`，
    于是"跑通了"对技能库零贡献。它的程序就是这串调用 —— 存下来才能重放。
    渲染成人能读的样子，是为了复盘时能一眼看出"当时到底怎么取的"；
    真正复用走的是 `blueprint.calls`（`voyager._replay_saved`）。
    """
    fn = "".join(ch if (ch.isalnum() or ch == "_") else "_"
                 for ch in str(topic_id)) + "_solve"
    lines = [f"# 自己跑通的编排（{topic_id}）—— 重放即可，口径已由裁判验证",
             f"async def {fn}(mcp):", "    obs = []"]
    for i, c in enumerate(calls):
        args = ", ".join(f"{k}={v!r}" for k, v in (c.get("args") or {}).items())
        lines.append(f"    r{i} = await mcp.{c.get('tool')}({args})")
        lines.append(f"    obs.append(r{i})")
    lines.append("    return {'_obs': obs}")
    return "\n".join(lines), fn


def _successful_calls(voyager: Any) -> list[dict[str, Any]]:
    """本轮**真正调成功了**的调用（只留 tool + args，不带返回值）。

    trajectory 的每项还带着工具的原始返回（`result`）—— 那是整张表，
    存进技能库会把 json 撑到几 MB，而且复用时根本用不到。
    """
    out: list[dict[str, Any]] = []
    for t in (getattr(voyager, "trajectory", None) or []):
        if not isinstance(t, dict) or not t.get("tool"):
            continue
        args = {k: v for k, v in (t.get("args") or {}).items()
                if isinstance(v, (str, int, float, bool))}
        out.append({"tool": str(t["tool"]), "args": args})
    return out


def _deterministic_ratio(ref_facts: list[dict[str, Any]]) -> float:
    """裁判事实里「确定性来源」占多少 —— V2 证据权重的 evaluator_confidence 用它。

    伴学只有"评判者可信度默认 0.75"一个数（它无从区分）；MVE 的裁判事实
    带来源标签，所以能量化：SQL 直算 / 工具路径 = 确定性，llm = 模型补的。
    """
    if not ref_facts:
        return 0.0
    det = 0
    for f in ref_facts:
        tool = str(((f.get("source") or {}).get("tool")) or "")
        if "deterministic" in tool or tool.startswith("vlml0_referee::"):
            det += 1
    return det / len(ref_facts)


async def main() -> None:
    argv = sys.argv[1:]

    # 清档：技能库 + 运行日志一起清。只清一个会留下对不上的曲线。
    if "--reset" in argv:
        skill_store.reset()
        run_log.reset()
        vlml0_referee.reset()
        return

    if "--next" in argv:
        b = planner.next_brief()
        print(f"下一题：{b['question']}")
        print(f"  topic_id  : {b['topic_id']}")
        print(f"  推荐理由  : [{b['reason_label']}] {b['explanation']}")
        print(f"  难度      : 题目 {b['difficulty']} → 目标 {b['difficulty_target']}"
              f"（{b['difficulty_why']}）")
        print(f"  支架档位  : {b['hint']} —— {b['hint_label']}")
        print(f"  跑法      : {b['cmd']}")
        return

    use_llm = "--llm" in argv
    blind = "--blind" in argv

    # 求助策略：判错后 Voyager 自己决定要不要去旁路看解释（默认 auto）。
    #   auto   —— 由模型判断（有没试过的工具就先自己试）
    #   always —— 一判错就给解释（对照用：能看出掌握度被打了多少折）
    #   never  —— 永不求助（纯自学基线）
    help_policy = "auto"
    for pol in ("auto", "always", "never"):
        if f"--help-policy={pol}" in argv:
            help_policy = pol

    # 支架档位覆盖（默认由出题器按掌握度算，见 difficulty.py）。
    # 给它是为了能 A/B：同一道题 full / partial / none 各跑一遍，
    # 支架若真起作用，覆盖率应该随档位下降 —— 不下降说明档位是摆设。
    hint_override = ""
    for lv in (difficulty.HINT_FULL, difficulty.HINT_PARTIAL, difficulty.HINT_NONE):
        if f"--hint={lv}" in argv:
            hint_override = lv

    # 轮数上限。注意这只是**上限**：判对就 break（照 Voyager rollout 的 done=success），
    # 不会硬跑满。面板/驾驶舱用它控制一次跑多久。
    rounds = 3
    if "--rounds" in argv:
        try:
            rounds = max(1, min(10, int(argv[argv.index("--rounds") + 1])))
        except (ValueError, IndexError):
            rounds = 3

    # 选题：照伴学 practice_scope 的两种模式
    #   --topic <id>  → explicit_topic：钉住一道题连考
    #   不给          → explicit_scope：出题器按优先级链自选
    topic_id = None
    if "--topic" in argv:
        topic_id = argv[argv.index("--topic") + 1]
        if topic_id not in TASKS:
            print(f"未知题目 {topic_id}。可选：{', '.join(TASKS)}")
            return
    elif "--hard" in argv:
        topic_id = TASK_HARD.topic_id
    elif "--mid" in argv:
        topic_id = TASK_MID.topic_id
    elif "--pattern" in argv:
        topic_id = TASK_PATTERN.topic_id

    # ---- 自适应出题 ----
    # 照伴学 planner.select_practice_selection 的优先级链：
    #   wrong_retry > due_review > weak_topic(人导入) > blocked_diagnostic > recommended > default
    # 每条都带 reason + explanation。上一版只有排序没有理由，那不算自适应。
    selection = planner.select_next(explicit_topic_id=topic_id or "")

    # ---- 没有可推进的题 → 生成一道新的（伴学 weak_topic → generate_question）----
    # 分工照伴学：planner 只说清「练什么」，**生成题面是 entry 层的活**
    # （entry_tutor_question_entries.py:1289 _generate_question_payload_impl）。
    # 伴学在 selection_reason != due_review 时就是 action=generate_question，
    # MVE 补齐这一环 —— 否则「没有可推进的题」只会卡住（这正是此前
    # 「连着 10 次停在 50%」那个症状的出口）。
    if "--no-gen" not in argv and selection.get("action") == "generate_question":
        sug = selection.get("suggestion") or {}
        if sug:
            print(f"  ⚠ {selection['explanation']}")
            print(f"  → 照伴学 weak_topic，尝试生成一道新题：{sug.get('why', '')}")
            try:
                import question_gen
                rec, _ = await question_gen.generate_adopt(
                    about=str(sug.get("about") or ""),
                    difficulty=int(sug.get("difficulty") or 2),
                    focus_dimension=str(sug.get("focus_dimension") or ""),
                    tries=3, do_adopt=True, verbose=True)
            except Exception as exc:                         # pragma: no cover
                print(f"  ⚠ 自动出题失败（继续用现有题）：{type(exc).__name__}: {exc}")
                rec = None
            if rec:
                new_id = str(rec.get("topic_id") or "")
                # 就地并入 —— planner 读的就是同一个 dict
                TASKS[new_id] = question_gen.to_task(rec)
                print(f"  ✅ 新题已入闱：{new_id}")
                selection = planner.select_next(explicit_topic_id=new_id)
            else:
                print("  ⚠ 三次都没生成出成立的题 —— 继续排现有题（诚实结果，"
                      "不是 bug）")

    task = TASKS[selection["topic_id"]]

    print(f"  出题器    : {task.topic_id}")
    print(f"  推荐理由  : [{planner.REASON_LABEL.get(selection['reason'], selection['reason'])}] "
          f"{selection['explanation']}")
    print(f"  难度梯度  : 题目 {task.difficulty} → 目标 "
          f"{selection.get('difficulty_target')}（{selection.get('difficulty_why','')}）")
    print(f"  支架档位  : {selection.get('hint')} —— {selection.get('hint_label','')}")
    if hint_override:
        print(f"  ⚠ 支架覆盖  : 出题器算的是 {selection.get('hint')}，"
              f"本轮强制 {hint_override}（A/B 用）")
        selection["hint"] = hint_override
        selection["hint_label"] = difficulty.HINT_LABEL[hint_override]
    if selection["blocked"]:
        print("  ⚠ 拿不到证据 —— 按伴学的规矩，此时不出新题，重复这一题直到拿到证据。")
    print()

    voyager: Any = LLMVoyager(blind=blind,
                              hint_level=str(selection.get("hint") or "")) if use_llm \
        else ScriptedVoyager()
    mode = ("LLM+BLIND" if blind else "LLM") if use_llm else "SCRIPTED"
    # 掌握度序列跨进程续接：从 run_log 恢复这道题的历史**证据**。
    # 不恢复的话，「跑一题就停」每次都是新进程，V2 只看到当前这一轮，
    # 权重和永远只有一条 —— 加权与时间衰减都失去意义。
    history: list[float] = run_log.recent_scores(task.topic_id)
    evidence: list[mastery_model.MasteryEvidence] = run_log.evidence_rows(task.topic_id)
    snapshots = []
    coverages: list[float] = []
    successes: list[bool] = []   # VLML 观测：每轮是否全部评分点达成（考核尺子）
    dones: list[bool] = []       # 判据层：每轮任务是否完成（Voyager 尺子，不数覆盖点）

    print("=" * 72)
    print(f"  MVE：Voyager 在 VLML 上做题   [{mode}]  裁判=VLML0(原版)")
    print("=" * 72)
    print(f"\n题目：{task.question}")
    print(f"评分点：{[p.point for p in task.rubric]}")
    print(f"validated_target = {task.validated_target}（True → 计入掌握度）")

    # ---- 裁判异步开跑：与 Voyager 取数重叠，不阻塞 ----
    # 标准答案必须来自原版 VLML（无 Voyager 参与），不能由出题人手写、
    # 也不能用模型生成的参考答案（伴学 tutor_llm_agent_answer_evaluate.py:82-84）。
    ref_task = asyncio.create_task(vlml0_referee.answer(task))
    print("裁判 VLML0 已在后台开跑…\n")

    for round_no in range(1, rounds + 1):
        print("-" * 72)
        print(f"第 {round_no} 轮")
        print("-" * 72)

        # 本轮作答时是否带着求助来的解释 —— 必须在 run() **之前**取：
        # 求助发生在上一轮末尾，这一轮是"带着解释重做"，所以这轮算借助帮助。
        used_hint = bool(getattr(voyager, "used_hint", False))
        if used_hint:
            print("  ℹ️ 本轮带着求助到的解释重做 → 掌握度按「借助帮助」计（0.85 折，"
                  "且答对也不拉长半衰期）")

        result = await voyager.run(task)
        facts = result["facts"]

        # 裁判到这里才必须就绪（前面几轮取数的时间被它白用掉了）
        ref = await ref_task
        if not hasattr(main, "_ref_shown"):
            main._ref_shown = True
            print(f"[裁判 VLML0] 标准答案 {len(ref.facts)} 条，轨迹 {ref.trajectory}")
            for f in ref.facts:
                subj = "/".join(f"{k}={v}" for k, v in (f.get("subject") or {}).items())
                print(f"              {subj} · {f['dimension']} = {f['value']} (base={f.get('base')})")
            print()

        # 只数「真调成功了」的工具调用；失败的不算证据
        tool_calls = sum(1 for t in voyager.trajectory if not str(t.get("error") or ""))
        ev = evaluate_vs_referee(facts, task.rubric, ref.facts, tool_calls=tool_calls)
        # Voyager 尺子：任务完成 = 跑通编排 + 拿到数 + 没翻车（**不数覆盖点**）
        done = task_done(facts, tool_calls=tool_calls, errors=result.get("errors"))

        for e in (result.get("errors") or []):
            print(f"  ⚠ 工具错误  : {e[:150]}")
        for h in (result.get("hallucinations") or []):
            print(f"  🛑 幻觉拦截  : {h[:150]}")
        print(f"  轨迹      : {[t['tool'] for t in voyager.trajectory]}")
        if use_llm:
            print(f"  思考      : {result.get('thought', '')[:90]}")
            st = skill_store.stats()
            print(f"  技能库    : {st['skills']} 条（写入 {st['writes']} 次 → "
                  f"覆盖 {st['rewrites']} · 丢弃 {st['skipped']} · 膨胀率 {st['bloat']}）"
                  f"  本轮注入 {len(getattr(voyager, '_injected_keys', []))}/{st['top_k']} 条")
            for ev_ in getattr(voyager, "skill_events", [])[-2:]:
                print(f"               · 上一轮写入：{ev_['label']} {ev_['key']}")
            for s in skill_store.all_skills():
                print(f"               · {s['text'][:100]}")
        else:
            print(f"  技能      : {result['skill_used']}   "
                  f"技能库={[s.name for s in voyager.skills]}")
        print(f"  事实数    : {ev.facts_count}   evidence={ev.evidence_status}")
        if result.get("reused_skill"):
            # 学到的程序被**跑**了，不是被读了 —— 这是「学会」的可观测形态。
            print(f"  复用技能  : {result['reused_skill']}"
                  f"（直接执行技能库里的程序，本轮未重新生成代码）")
        print(f"  已覆盖    : {ev.covered_points}")
        print(f"  缺失      : {ev.missing_points}")
        if ev.rejected_low_base:
            print(f"  base 过闸 : {ev.rejected_low_base}")
        if ev.unjudgeable:
            print(f"  裁判缺答  : {ev.unjudgeable}（不计入 Voyager 的错）")
        print(f"  verdict   : {ev.verdict}   score={ev.score}   coverage={ev.coverage:.0%}"
              f"   判据={ev.judge}")

        snap = None
        if ev.evidence_status == "none":
            print("  → 本回合未拿到证据：不计入掌握度序列")
        else:
            history.append({"correct": 1.0, "partial": 0.6}.get(ev.verdict, 0.0))
            # 本轮这条证据：**先** append 再投影，V2 要看到完整序列。
            # evaluator_confidence 取裁判事实的确定性占比 —— 伴学只有"默认 0.75"，
            # 而 MVE 的裁判事实有明确来源（deterministic / 工具路径 / llm），
            # 全确定性给 0.95、全 LLM 给 0.6。
            det_ratio = _deterministic_ratio(ref.facts)
            evidence.append(mastery_model.MasteryEvidence(
                attempt_id=f"{datetime.now().isoformat(timespec='seconds')}#{round_no}",
                verdict=ev.verdict,
                score=ev.score,
                difficulty=task.difficulty,
                # used_hint = **本轮作答时用没用解释**（VLML0 求助）。
                # 之前恒传 None，于是 hint 折扣从不生效（`hint_unknown_modifier=1.0`）；
                # 保持度那边又把它映射成"注入过技能库经验"—— 那两个都是错的口径。
                # 伴学的同构项很明确：used_hint 是"这次做对靠没靠帮助"
                # （mastery_v2.py:109 + :391-396），注入技能是正常做题的一部分，
                # 只有**求助**才算借助帮助。
                used_hint=used_hint,
                response_time_ms=None,
                evaluator_confidence=0.6 + 0.35 * det_ratio,
                submitted_at=datetime.now().isoformat(timespec="seconds"),
                tool_calls=tool_calls,
            ))
            # 照伴学 mastery_v2.py:283-285：还没消化的错题会封顶掌握度。
            # 不传这个，mastery 会靠 confidence 项随证据权重单调上升 —— 假阳性。
            unresolved = (0 if ev.verdict == "correct"
                          else run_log.unresolved_wrongs(task.topic_id) + 1)
            snap = update_mastery(
                task.topic_id, evidence,
                unresolved_wrong_count=unresolved,
            )
            snapshots.append(snap)

            # 保持度（会忘）：伴学 mastery_retention.py:46-51 ——
            #   correct 且 **没靠帮助** → 半衰期拉长（学得扎实）
            #   wrong/dont_know       → 半衰期缩短
            #   其他（含 correct 但用了帮助）→ 半衰期**不动**
            # 即"借助帮助答对"：分数打 0.85 折，但不加速遗忘 —— 不是惩罚，是不计全功。
            try:
                retention.STORE.apply_attempt(
                    task.topic_id, verdict=ev.verdict, scores=history,
                    confidence=float(getattr(snap, "confidence", 0.0) or 0.0),
                    used_hint=used_hint,
                )
            except Exception as e:
                print(f"  （保持度未更新：{type(e).__name__}: {e}）")

            print(f"  mastery   : {snap.mastery:.3f}  {snap.level}  "
                  f"status={snap.status}  flags={snap.flags or '—'}"
                  + (f"  （未消化错题 {unresolved} → 封顶 0.79）" if unresolved else ""))
            print(f"  证据权重  : {len(evidence)} 条 · 裁判确定性 {det_ratio:.0%}"
                  f" · 本轮工具调用 {tool_calls}"
                  f" · recency={getattr(snap, 'recency', 0):.3f}")

        # ---- 叙事洞察：只在「有被裁判认可的事实」时才跑 ----
        # 顺序不能反：先比对、后叙事。否则等于把编错的数字包装成流畅结论。
        covered_keys = {
            fact_key({"subject": p.subject, "dimension": p.dimension})
            for p in task.rubric if p.point in ev.covered_points
        }
        verified = [f for f in facts if fact_key(f) in covered_keys]
        story = narrator.narrate(task, verified, verdict=ev.verdict)
        if story.get("narrative"):
            print(f"  叙事洞察  : {story['narrative'][:110]}")
            for ins in story.get("insights") or []:
                print(f"               · {ins[:100]}")
        elif story.get("skipped"):
            print(f"  叙事洞察  : 跳过 —— {story['skipped']}")

        # ---- 裁判判定与叙事都进因果时间线 ----
        # 出题器读的是这条线（因果）+ 掌握度（评分）；人导入的题只进前者。
        causal_timeline.append(
            causal_timeline.REFEREE,
            topic_id=task.topic_id,
            summary=f"裁判判定 {ev.verdict}，覆盖率 {ev.coverage:.0%}",
            detail={"covered": ev.covered_points, "missing": ev.missing_points,
                    "no_tool_calls": ev.no_tool_calls},
        )
        if story.get("narrative"):
            causal_timeline.append(
                causal_timeline.NARRATIVE,
                topic_id=task.topic_id,
                summary=f"产出叙事，基于 {story.get('based_on', 0)} 条已验证事实",
                detail={"insights": story.get("insights") or []},
            )

        # Voyager 的行动也进时间线 —— 出题器要看得见「它做了什么」
        #
        # 必须连 **SQL 原文** 一起存：之前只存了工具名和 verdict，
        # 于是"它到底写错了什么"事后无从复盘。诊断"图谱给了骨架它为什么不照写"
        # 时，缺的就是这一行 —— 看不到 SQL，只能猜它没读还是读了没用。
        causal_timeline.append(
            causal_timeline.ATTEMPT,
            topic_id=task.topic_id,
            summary="Voyager 编排："
                    + (" → ".join(t["tool"] for t in voyager.trajectory) or "（未调用）"),
            detail={"facts": ev.facts_count, "verdict": ev.verdict,
                    "errors": (result.get("errors") or [])[:2],
                    "sqls": [
                        str((a.get("args") or {}).get("sql_query")
                            or (a.get("args") or {}).get("sql") or "")[:400]
                        for a in (getattr(voyager, "attempted", None) or [])
                        if str(a.get("tool", "")) in ("query_sql", "execute_custom_sql")
                    ][:4],
                    # 连同**参数**一起存：只存工具名时，"它调对了工具却没传参"
                    # 这种失败根本看不出来（实测 pistol_eco_pattern 就栽在这）。
                    "calls": [
                        {"tool": str(t.get("tool") or ""),
                         "args": {k: str(v)[:60] for k, v in
                                  (t.get("args") or {}).items()}}
                        for t in (getattr(voyager, "trajectory", None) or [])
                    ][:4]},
        )

        # 反馈必须在落盘**之前**算出来 —— 面板和离线重算读的都是这一包，
        # 不落盘就等于"反馈只在进程里存在过"，事后无法复盘"当时到底说了什么"。
        det_ratio = _deterministic_ratio(ref.facts)
        critique = voyager.learn(
            covered=ev.covered_points, missing=ev.missing_points,
            task=task, referee=ref, verdict=ev.verdict,
            rejected_low_base=list(ev.rejected_low_base),
            confidence=round(0.6 + 0.35 * det_ratio, 4),
        ) or ""     # 脚本基线的 learn 不产出 critique（返回 None），这里兜住
        print(f"  critique  : {critique[:110]}" if critique else "  critique  : （脚本基线，无）")
        fb = getattr(voyager, "last_feedback", None) or {}
        if fb.get("error_type"):
            print(f"  错误类型  : {fb['error_type']}")
        # 学了却没取证：上次看过讲解、本轮一个工具都没调成功 —— 帮助白给了。
        # 实测 corrode_collapse 求助后连着出现「编排（未调用）、事实数 0」，
        # 比不求助更糟。当场点出来，否则人只看到又一个 0%。
        if used_hint and ev.no_tool_calls:
            print("  ⚠ 求助无效：上一轮学了标准解法，本轮却一个工具都没调成功"
                  " —— 这次帮助不计入进步（已落 help_wasted）")
        if fb.get("next_action"):
            print(f"  下一步    : {fb['next_action'][:150]}")

        # ---- 学习 ①：自己跑通了 → 存**自己写的**程序 ----
        # 这是原版 `add_new_skill` 的本体：
        #   原版：自己写 JS → 跑通 → 存 program_code → 下次直接跑
        # 此前 MVE 只存 VLML0 的解法（观摩来的），自己跑通的那段
        # 从来没存过 —— 于是"跑通"这件事对技能库没有任何贡献。
        # 判据照原版：任务完成（Voyager 尺子 = 跑通编排 + 拿到数 + 没翻车，
        # **不算覆盖点** —— 覆盖点是 VLML 的尺子，只用于考核与掌握度证据）
        # 才存；没跑通的存进去等于把错误做法固化下来。
        try:
            prog = str(result.get("program_code") or "")
            if prog and done:
                _saved = skill_store.add(
                    task.topic_id,
                    _stable_skill_name(task),
                    f"自己跑通的编排（覆盖率 {ev.coverage:.0%}）："
                    f"{result.get('program_name') or ''}",
                    code=prog,
                    # 主函数名必须一起存：复用时 `execute(code, name)` 要靠它
                    # 找到入口。只存源码不存函数名，程序取出来也跑不了。
                    blueprint={"source": "practice",
                               "coverage": ev.coverage,
                               "program_name": result.get("program_name") or ""},
                    source="practice")
                print(f"  学会技能  : {_saved[0]} "
                      f"（自己写的程序 {len(prog)} 字符已入库）")
            elif done:
                # ---- plan 路径：没有 program_code，但**跑通了的调用序列就是它的程序** ----
                #
                # 8 道题里 6 道是 SQL 类，走 plan 路径，那条路永远不产出代码 ——
                # 于是这六成的题"跑通了"对技能库零贡献：撤掉图谱重考时只能
                # 从头再写一遍 SQL，然后再犯同一个口径错（实测 corrode_collapse
                # 练完 100%、重考仍 0%，缺的还是同一条 losing_team_name 放错层）。
                # 原版的判据是**跑通了没有**，不是这段东西长什么样。
                calls = _successful_calls(voyager)
                if calls:
                    code, fn = _plan_program(str(task.topic_id), calls)
                    _saved = skill_store.add(
                        task.topic_id,
                        _stable_skill_name(task),
                        f"自己跑通的编排（覆盖率 {ev.coverage:.0%}）："
                        f"{' → '.join(str(c['tool']) for c in calls)}",
                        code=code,
                        blueprint={"source": "practice",
                                   "coverage": ev.coverage,
                                   "program_name": fn,
                                   "calls": calls},
                        source="practice")
                    print(f"  学会技能  : {_saved[0]} "
                          f"（跑通的调用序列 {len(calls)} 步已入库，可重放）")
        except Exception as e:                # pragma: no cover
            print(f"  （存程序失败：{type(e).__name__}: {e}）")


        # ---- 求助闭环：判错后由 Voyager 自己决定要不要去旁路看解释 ----
        # 顺序有讲究：先自己按图谱说的工具名试一次，实在不行才求助。
        # 求助一次就要在掌握度上记一笔（used_hint → 0.85 折），
        # 所以不能一判错就给解释，否则掌握度全是"借助帮助"得来的。
        if fb.get("error_type") and round_no < rounds and help_policy != "never":
            decision = voyager.decide_help(task=task, fb=fb)
            print(f"  求助判断  : {'去旁路看解释' if decision.get('need_help') else '先自己试'}"
                  f" —— {decision.get('reason', '')[:70]}")
            if decision.get("need_help") or help_policy == "always":
                try:
                    pack = await bypass_learn.explain_for_retry(
                        task,
                        my_plan=[t["tool"] for t in voyager.trajectory],
                        missing=[m.get("dimension") or m.get("point")
                                 for m in (fb.get("missing_detail") or [])],
                    )
                    if pack.get("ok"):
                        voyager.accept_help(pack)
                        study = pack.get("study") or {}
                        print(f"  === 学习标准解法（含答案，照伴学讲解四段）===")
                        for i, sec in enumerate(pack.get("sections") or
                                                ["题目解析", "解题过程", "答案", "举一反三"]):
                            key = ["analysis", "process", "answer", "transfer"][i]
                            body = str(study.get(key) or "").replace("\n", " ")
                            print(f"  {sec}：{body[:150]}")
                        print(f"  重做时只带做法（编排 {' → '.join(pack.get('plan') or [])}），数值自己取")

                        # ---- 学习 ②：没跑通 → 观摩 VLML0 的标准解法 ----
                        # 这是原版 `add_new_skill` 的同构物，也是之前的核心：
                        #   原版：自己写 JS → 跑通 → 存 program_code
                        #   MVE ：判错（学习信号）→ 观摩 VLML0 → 存它的解法源码
                        # 判错本身就是信号，不需要另造；而"学到了什么"必须落进
                        # 技能库，否则下一轮拿不到 —— 之前 learn_from_referee
                        # 只在 CLI 里手动跑过，主循环一次都没调，
                        # 于是**旁路学到的东西从来没进过技能库**。
                        try:
                            learned = await bypass_learn.learn_from_referee(
                                task.topic_id)
                            if learned.get("ok"):
                                print(f"  学到技能  : {learned.get('action')} "
                                      f"{learned.get('name')} "
                                      f"（解法源码 {learned.get('code_len')} 字符"
                                      f" → 下一轮进 prompt）")
                            else:
                                print(f"  （未学到：{learned.get('error')}）")
                        except Exception as e:                  # pragma: no cover
                            print(f"  （学习失败：{type(e).__name__}: {e}）")
                    else:
                        print(f"  （讲解未取到：{pack.get('error')}）")
                except Exception as e:
                    print(f"  （求助失败：{type(e).__name__}: {e}）")

        # 落一行日志：面板的进步曲线读的就是这个
        run_log.append({
            "mode": mode,
            "topic_id": task.topic_id,
            "task": topic_id,
            "question": task.question,
            "round": round_no,
            # 本轮的支架档位 —— 档位递进（每轮最多 ±1）要跨进程对比
            # "上一轮用的什么档"，不落盘这个约束就是空谈（见 planner._prev_hint）。
            "hint": str(getattr(voyager, "hint_level", "") or ""),
            "coverage": round(ev.coverage, 4),
            "score": round(ev.score, 4),
            "verdict": ev.verdict,
            "success": bool(ev.success),   # VLML 观测：全部评分点达成（考核尺子）
            "done": bool(done),            # 判据层布尔：任务完成（Voyager 尺子，不数覆盖点）
            "evidence_status": ev.evidence_status,
            "judge": ev.judge,
            "no_tool_calls": ev.no_tool_calls,
            # --- 结构化反馈包（照伴学 answer_evaluate）：判据 + 下一步 + 错误假设 ---
            # 没有它，事后只能看到"覆盖率 25%"，看不到"为什么、下一步该干什么"。
            "feedback": fb,
            # 本轮是否"借助帮助"（用了 VLML0 解释）。落盘才能事后回答
            # "这个掌握度是靠自己拿的还是靠帮助拿的" —— 伴学 used_hint 同构项。
            "used_hint": bool(used_hint),
            # 学了却没取证：上一轮看过讲解，本轮一个工具都没调 → 这次帮助白给。
            # 实测 corrode_collapse 求助后的第 52/53 轮就是这样（编排"（未调用）"、
            # 事实数 0），比不求助更糟。落盘才能事后把这类轮次挑出来看。
            "help_wasted": bool(used_hint and ev.no_tool_calls),
            "help_decision": getattr(voyager, "last_help_decision", None),
            # 裁判的编排序列：离线重算 feedback 时要用（否则"我没调过的工具"算不出来）
            "ref_plan": [str(t) for t in (getattr(ref, "trajectory", None) or [])
                         if not str(t).startswith("<")],
            # --- V2 掌握度要求的三个字段：没有它们，下次进程无法重建证据 ---
            "difficulty": task.difficulty,
            "tool_calls": tool_calls,
            "evaluator_confidence": round(0.6 + 0.35 * det_ratio, 4),
            "errors": (result.get("errors") or [])[:3],
            "unjudgeable": ev.unjudgeable,
            "referee_facts": len(ref.facts),
            "covered": ev.covered_points,
            "missing": ev.missing_points,
            "rejected_low_base": ev.rejected_low_base,
            # 叙事段：comparable 恒为 False，不参与比对
            "narrative": story.get("narrative", ""),
            "insights": story.get("insights") or [],
            "caveats": story.get("caveats") or [],
            "narrative_comparable": story.get("comparable", False),
            "narrative_based_on": story.get("based_on", 0),
            "facts_count": ev.facts_count,
            "trajectory": [t["tool"] for t in voyager.trajectory],
            "skill_count": len(voyager.memory) if use_llm else len(voyager.skills),
            "skill_stats": skill_store.stats() if use_llm else None,
            "hallucinations": (result.get("hallucinations") or [])[:3],
            "hallucination_count": len(result.get("hallucinations") or []),
            "mastery": round(snap.mastery, 4) if snap else None,
            "level": snap.level if snap else None,
            "status": snap.status if snap else None,
            "flags": list(snap.flags) if snap else [],
            "mastery_recency": round(getattr(snap, "recency", 0.0), 4) if snap else None,
            "mastery_confidence": round(getattr(snap, "confidence", 0.0), 4) if snap else None,
            "mastery_evidence_count": getattr(snap, "evidence_count", 0) if snap else 0,
            "mastery_model": getattr(snap, "mastery_model_version", "") if snap else "",
        })

        coverages.append(ev.coverage)
        successes.append(ev.success)
        dones.append(done)
        print()

        # 照 Voyager rollout（voyager.py:269-273）：
        #   done = (rollout_num_iter >= action_agent_task_max_retries or success)
        # **任务完成了就停**。之前是硬跑满 3 轮 —— 做对了还在重做，既浪费，
        # 又会让后面几轮的"失败教训"继续往技能库里写。
        # 判据用 done（Voyager 尺子：跑通编排 + 拿到数 + 没翻车），
        # **不是 VLML 覆盖点**（覆盖率只作考核尺子 / 掌握度证据）。
        if done:
            print("  ✅ 任务完成（Voyager 尺子：跑通 + 拿到数 + 没翻车）"
                  "—— 照 Voyager rollout 的 `done = success`，本题不再重做。")
            break

    print("=" * 72)
    print("  结论")
    print("=" * 72)
    # 判据层（Voyager）：done = 任务完成布尔（跑通编排 + 拿到数 + 没翻车，
    # 不数覆盖点），存技能/停止/推进都只认它。
    # 掌握度（伴学）不能当学习判据：V2 里 confidence 随证据权重之和上升
    # （权重会被时间衰减、评价可信度拉低），但反复作答照样能把它推高，
    # 用它判断「学会了」会得出假阳性 —— 掌握度只做独立评估。
    print(f"  done轨迹  : {[str(d) for d in dones]}  ← 判据层（Voyager 尺子）")
    print(f"  success   : {[str(s) for s in successes]}  ← VLML 观测（全部评分点达成）")
    # 覆盖率降级为观测：达成度的连续读数，喂掌握度证据，不进对错判断
    print(f"  覆盖率观测 : {[f'{c:.0%}' for c in coverages]}  ← 仅作达成度观测")
    if snapshots:
        print(f"  mastery 轨迹: {[f'{s.mastery:.3f}' for s in snapshots]}"
              f"  （注意：它会随证据权重自然上升，不能当学习证据）")
        last = snapshots[-1]
        print(f"  证据/置信   : {getattr(last, 'evidence_count', 0)} 条 · "
              f"recency={getattr(last, 'recency', 0):.3f} · "
              f"confidence={getattr(last, 'confidence', 0):.3f}")
        try:
            cur = retention.STORE.current(task.topic_id)
            print(f"  保持度     : baseline {cur['baseline']:.3f} · 半衰期 "
                  f"{cur['half_life_sessions']:.1f} 个会话 · 现在剩 {cur['retained']:.3f}")
        except Exception:
            pass
    if use_llm:
        st = skill_store.stats()
        print(f"  技能库     : {st['skills']} 条 / 累计写入 {st['writes']} 次 "
              f"（覆盖 {st['rewrites']}、丢弃 {st['skipped']}）")
        # 膨胀率 = 写入次数 / 实际条目。v1 是 15 次写入 → 15 条（1.0，完全没挡住）
        print(f"  膨胀率     : {st['bloat']}  （>1 说明去重生效；=1 说明每次写入都成了新条目）")
        print(f"  检索上限   : 每题最多注入 {st['top_k']} 条进 prompt")
    else:
        print(f"  技能库     : {[s.name for s in voyager.skills]}")
    print()
    # 学习是否发生的判据：done 从 False → True（任务从没完成 → 完成，
    # Voyager 尺子，不数覆盖点）。覆盖率只作观测提示。
    learned = len(dones) >= 2 and (dones[-1] and not dones[0])
    if coverages and coverages[0] >= 1.0:
        print("  ⚠️ 首轮即满分 —— 这个任务对模型太简单，测不出学习曲线。")
        print("     这不说明「学不会」，只说明「不需要学」。要测学习必须上更难的题（--hard）。")
    elif learned:
        print(f"  ✅ done: {dones[0]} → {dones[-1]}"
              f"（覆盖率观测 {coverages[0]:.0%} → {coverages[-1]:.0%}）：")
        print("     Voyager 把教训写进技能库后，同一道题从「跑不通」变成了"
              "「跑通 + 拿到数 + 没翻车」。")
        print("     这是「学会编排」在本原型里的可观测形态（不数覆盖点）。")
    elif use_llm and len(voyager.memory) >= 2 and coverages[-1] <= coverages[0]:
        print("  ⚠️ 技能库有内容但覆盖率没提升 —— 模型读了经验却没改变行为")
    elif not use_llm and any(s.name == "分图下钻" for s in voyager.skills):
        print("  ⚠️ 技能已补上但覆盖率没动 —— 检查技能执行逻辑")
    elif not use_llm:
        print("  ❌ 技能库没有补上「分图下钻」—— 学习逻辑没触发")
    print()
    if use_llm:
        print("  本轮是 LLMVoyager：计划、抽取、反思全部由模型自己完成。")
        print("  success 若随轮次从 False 变 True，说明模型确实从 missing_points 里")
        print("  学会了改变编排，达成了全部评分点（覆盖率只是达成度观测）。")
    else:
        print("  本轮用 ScriptedVoyager（技能库是规则式的），验证的是评分链路。")
        print("  加 --llm 才能测「模型自己能否学会编排」。")


if __name__ == "__main__":
    asyncio.run(main())
