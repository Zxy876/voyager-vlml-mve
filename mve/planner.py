#!/usr/bin/env python3
"""自适应出题器（照猫娘伴学 planner.select_practice_selection）。

伴学的优先级链（planner.py:303 的 docstring 写得很直白）：

    retry > due_review > weak_topic > blocked_diagnostic > recommended > default

每条选择都带 `reason` + `explanation` —— **出题器必须说清为什么是这题**，
不能是个黑箱排序。这是「自适应」与「静态排序」的分界线：
我上一版的 next_topic() 只有排序没有理由，那不算自适应。

另加伴学的 readiness 闸门（planner.py:209 apply_readiness_policy）：
没有拿到证据的题不能推进 —— 对应助产士第四轮
「生成新的问题生成不了；重复（需要拿到证据（记忆来自适应出题给voyager））」。

还有一个必须继承的规矩：
　人导入的题 validated_target=False，**不进掌握度**，但写进行动因果时间线。
　出题器读的是时间线（因果）+ 掌握度（评分）两者，不是只看分数。
"""

from __future__ import annotations

from typing import Any

import causal_timeline  # noqa: E402
import run_log  # noqa: E402
from tasks import TASKS  # noqa: E402

# 复习间隔（照 FSRS 的思路：掌握了也要回头练，否则会退）
REVIEW_AFTER_ATTEMPTS = 4

# 错题重试冷却（照伴学 store_qa.py:322 的 retrying 状态：两次重试间隔 ≥30 分钟）。
# MVE 一次运行只有几分钟，按时间冷却没有意义；等价的量是**中间隔了几次别的题**。
# 没有这道闸的实测后果：corrode_collapse 错 12 次、覆盖率 0%，wrong_retry 无限优先，
# 其他题永远轮不到 —— 难度阶梯从底下就断了，掌握度永远不涨。
RETRY_COOLDOWN = 2

# 错题的**毕业**条件：同一题连着这么多次覆盖率一字不差（且没满分），
# 说明再练也是重复 —— 必须让位，否则就是"两道死题无限交替"。
#
# 实测（53 轮日志）：series_totals 连着 23 次停在 50%、corrode_collapse 连着 18 次
# 停在 0%，两道题吃掉 79% 的轮次，而会做的题（pistol 100%、map 100%）只拿到 8 轮。
# 冷却闸只是把"死磕一道"改造成"两道交替"，并没有给错题一个出口 ——
# 出口必须是"没进展就毕业"。覆盖率一变（哪怕只涨一点）就重新进错题池。
STALE_LIMIT = 3


def _mastery_by_topic() -> dict[str, dict[str, Any]]:
    """每题最近的掌握度 + 是否答错过 + 冷却进度。"""
    rows = run_log.load_all()
    out: dict[str, dict[str, Any]] = {}
    for i, r in enumerate(rows):
        topic = str(r.get("topic_id") or "")
        if not topic:
            continue
        rec = out.setdefault(topic, {
            "attempts": 0, "wrongs": 0, "last_coverage": 0.0,
            "last_mastery": None, "evidence_none": 0,
            "last_seen": -1, "cooling": 0, "cov_seq": [],
        })
        rec["attempts"] += 1
        if str(r.get("verdict")) in ("wrong", "dont_know"):
            rec["wrongs"] += 1
        if str(r.get("evidence_status")) == "none":
            rec["evidence_none"] += 1
        rec["last_evidence"] = str(r.get("evidence_status") or "")
        if r.get("coverage") is not None:
            rec["last_coverage"] = float(r["coverage"])
            rec["cov_seq"].append(round(float(r["coverage"]), 4))
        if r.get("mastery") is not None:
            rec["last_mastery"] = float(r["mastery"])
        rec["last_seen"] = i
    # 冷却进度：最后一次做这题之后，做了几次**别的**题
    for topic, rec in out.items():
        if rec["last_seen"] < 0:
            continue
        rec["cooling"] = sum(
            1 for r in rows[rec["last_seen"] + 1:]
            if str(r.get("topic_id") or "") != topic
        )
        # 停滞长度：从最近一次往前，覆盖率一字不差的连续次数
        seq = rec["cov_seq"]
        tail = seq[-1] if seq else None
        run = 0
        for v in reversed(seq):
            if v == tail:
                run += 1
            else:
                break
        rec["stale_run"] = run
        rec["stalled"] = run >= STALE_LIMIT and rec["last_coverage"] < 1.0
    return out


def _used_tools() -> set[str]:
    """Voyager 到目前为止真正调成功过的工具（从运行日志的轨迹里统计）。"""
    used: set[str] = set()
    for r in run_log.load_all():
        for t in (r.get("trajectory") or []):
            if isinstance(t, str) and t and not t.startswith("<"):
                used.add(t)
    return used


def select_next(*, explicit_topic_id: str = "") -> dict[str, Any]:
    """挑下一题，并说明为什么。

    返回 {topic_id, reason, explanation, blocked}
    """
    mastery = _mastery_by_topic()

    # 0) 人钉住一道题（伴学 explicit_topic 模式）
    if explicit_topic_id and explicit_topic_id in TASKS:
        return {
            "topic_id": explicit_topic_id,
            "reason": "explicit_topic",
            "explanation": "面板上指定钉住这道题，出题器不介入",
            "blocked": False,
        }

    # 1) wrong_retry：有未消化的错题（伴学 retry_wrong_question 优先级最高）
    #    但有冷却闸：刚重试过、中间还没做过别的题的，先让位 —— 否则就是无限
    #    硬磕同一道难题（实测 corrode_collapse 连错 12 次，别的题永远轮不到）。
    # 停滞的题不算"可重试"：连着 STALE_LIMIT 次覆盖率一字不差，再排它只是重复。
    # 伴学里对应的是错题重做**做对了才消除**；这里多做一条"没进展就毕业"，
    # 否则错题池只增不减，算力全砸在死题上（实测 79%）。
    wrongs = [
        (t, m) for t, m in mastery.items()
        if t in TASKS and m["wrongs"] > 0 and m["last_coverage"] < 1.0
        and m["cooling"] >= RETRY_COOLDOWN and not m["stalled"]
    ]
    if wrongs:
        # 错得最多次、且覆盖率最低的优先
        topic, m = min(wrongs, key=lambda kv: (kv[1]["last_coverage"], -kv[1]["wrongs"]))
        return {
            "topic_id": topic,
            "reason": "wrong_retry",
            "explanation": f"这道题错过 {m['wrongs']} 次、最近覆盖率只有 "
                           f"{m['last_coverage']:.0%}，先把它补上再推进"
                           f"（冷却 {m['cooling']}/{RETRY_COOLDOWN} 已过）",
            "blocked": False,
        }

    stalled = [(t, m) for t, m in mastery.items() if t in TASKS and m["stalled"]]

    # 2) due_review：练够了但还没满分 → 回头复习（照 FSRS）
    due = [
        (t, m) for t, m in mastery.items()
        if t in TASKS and m["attempts"] >= REVIEW_AFTER_ATTEMPTS
        and m["wrongs"] == 0 and m["last_coverage"] < 1.0
        and not m["stalled"]
    ]
    if due:
        topic, m = min(due, key=lambda kv: kv[1]["last_coverage"])
        return {
            "topic_id": topic,
            "reason": "due_review",
            "explanation": f"已练 {m['attempts']} 次但覆盖率停在 "
                           f"{m['last_coverage']:.0%}，该回头复习而不是开新题",
            "blocked": False,
        }

    # 3) human_focus：人最近在某一题上导入过问题（control fact 有时间线里）
    #    —— 这正是「人导入 → 影响自适应出题」的落点。
    #
    #    用 latest_topics_by_kind 而不是 counts_by_topic：人导入的意义在于
    #    **当下的意图**，必须是最近那条，不能是历史上导入最多那条。
    #
    #    两种情形分别措辞：
    #      a) Voyager 根本没练过 → 按人导入的方向开题
    #      b) 练过但没满分     → 仍然回到这题（人的意图优先于"推进新题"）
    #
    #    但 (b) 同样要过冷却闸 —— 不然 wrong_retry 刚让位，这里又把同一道
    #    0% 的难题顶回去，死锁只是换了个分支（实测就是这样的）。
    #    伴学里"人导入过"也从来不是豁免重试冷却的理由。
    for topic in causal_timeline.latest_topics_by_kind(causal_timeline.CONTROL):
        if topic not in TASKS:
            continue
        m = mastery.get(topic)
        if m is None:
            return {
                "topic_id": topic,
                "reason": "human_focus",
                "explanation": f"人最近在「{TASKS[topic].question[:28]}…」上导入过问题"
                               f"（已进因果时间线），但 Voyager 还没正式练过 —— "
                               f"按人导入的方向开题",
                "blocked": False,
            }
        if m["last_coverage"] < 1.0 and m["cooling"] >= RETRY_COOLDOWN:
            return {
                "topic_id": topic,
                "reason": "human_focus",
                "explanation": f"人最近导入的问题落在这道题上，而它还只做到 "
                               f"{m['last_coverage']:.0%} —— 先顺着人的意图把它补满，"
                               f"而不是去开新题（冷却 {m['cooling']}/{RETRY_COOLDOWN} 已过）",
                "blocked": False,
            }
        # 冷却中（刚试过还没隔开）或已满分 → 看人更早导入的下一条
        continue

    # 3.5) tool_coverage：有工具**一次都没被用过** → 出一道必须用到它的题。
    #    覆盖性不能只停留在"结构论证"（工具进了 TOOL_REGISTRY 就算覆盖），
    #    得真被调过才算。这条是新增的，伴学里没有直接对应 —— 它对应的是
    #    "weak_topic 优先补薄弱点"这同一条原则，只是薄弱点从知识点换成了工具。
    used = _used_tools()

    def _never_used(topic_id: str) -> bool:
        req = set(TASKS[topic_id].requires_tools or [])
        return bool(req) and not (req & used)

    # 停滞的题**不能**从这条分支进：实测它把已经连着 23 次停在 50% 的
    # series_totals 又选了回来 —— 死锁只是从 wrong_retry 换到了 tool_coverage
    # （同样的错误之前在 human_focus 分支犯过一次，见上面 :146-148 的注释）。
    fresh = [t for t in TASKS if _never_used(t) and t not in mastery
             and not (mastery.get(t) or {}).get("stalled")]
    if not fresh:
        # 练过也没关系：只要工具还没被用上，覆盖性就还没达成
        fresh = [t for t in TASKS if _never_used(t)
                 and not (mastery.get(t) or {}).get("stalled")]
    if fresh:
        topic = min(fresh, key=lambda t: (TASKS[t].difficulty, t))
        gap = sorted(set(TASKS[topic].requires_tools) - used)
        return {
            "topic_id": topic,
            "reason": "tool_coverage",
            "explanation": f"工具 {', '.join(gap)} 到现在一次都没被用过 —— "
                           f"出一道非用到它不可的题（本题的关键评分点"
                           f"只有这个工具给得出）",
            "blocked": False,
        }

    # 4) recommended：按难度推进最没练过的
    #    （这就是难度阶梯的走法：wrong_retry 被冷却让位后，先补没练过的最低难度）
    untouched = [t for t in TASKS if t not in mastery]
    if untouched:
        topic = min(untouched, key=lambda t: (TASKS[t].difficulty, t))
        return {
            "topic_id": topic,
            "reason": "recommended",
            "explanation": f"这道还没练过，难度 {TASKS[topic].difficulty} 最低，从它开始",
            "blocked": False,
        }

    # 5) default：都练过了 → 挑覆盖率最低的。
    #    但**只挑还没停滞的**：停滞题从这里再进来，就等于"毕业规则白加了"
    #    —— 死锁只是从 wrong_retry 换到了 default（实测确实如此）。
    pushable = [
        (t, m) for t, m in mastery.items()
        if t in TASKS and m["last_coverage"] < 1.0 and not m["stalled"]
    ]
    if pushable:
        topic, m = min(pushable, key=lambda kv: kv[1]["last_coverage"])
        return {
            "topic_id": topic,
            "reason": "default",
            "explanation": f"都练过了，挑覆盖率最低、且还没停滞的这道"
                           f"（{m['last_coverage']:.0%}）",
            "blocked": False,
        }

    # 5.5) all_stalled：能推进的题一个都没有了 —— 如实说清，别假装还在自适应。
    #     这时候该做的是补题目口径 / 换题，而不是继续硬跑同一道。
    if stalled:
        topic, m = min(stalled, key=lambda kv: (kv[1]["last_coverage"], -kv[1]["stale_run"]))
        return {
            "topic_id": topic,
            "reason": "all_stalled",
            "explanation": f"没有可推进的题了：这道连着 {m['stale_run']} 次停在 "
                           f"{m['last_coverage']:.0%}（其余未满分的题也都停滞）—— "
                           f"再排它只是重复，建议先补题目口径或换题",
            "blocked": True,
        }

    # 6) blocked_diagnostic：兜底（助产士第四轮：拿不到证据就不生成新题）
    #    位置必须垫底 —— 伴学 apply_readiness_policy（planner.py:235）里它是
    #    retry/due/weak **全为空**时才出现的 fallback，不是高优先级分支。
    #    我原来放在 recommended 前面，等于又造了一个死锁（刚修完两个，又造一个）。
    #    判据是"**最近一次**没拿到证据"，不是历史累计 —— corrode_collapse
    #    13 轮里只 none 过 1 次，累计判据会把它永久卡死在这里。
    no_evidence = [
        (t, m) for t, m in mastery.items()
        if t in TASKS and m.get("last_evidence") == "none"
    ]
    if no_evidence:
        topic, m = min(no_evidence, key=lambda kv: -kv[1]["evidence_none"])
        return {
            "topic_id": topic,
            "reason": "blocked_diagnostic",
            "explanation": f"没有别的可推进的题了，而这道题最近一次没拿到证据"
                           f"（历史共 {m['evidence_none']} 次）—— 重复它直到拿到",
            "blocked": True,
        }

    topic = min(TASKS, key=lambda t: (TASKS[t].difficulty, t))
    return {
        "topic_id": topic,
        "reason": "cold_start",
        "explanation": "还没有任何练习记录，从最简单的开始",
        "blocked": False,
    }


REASON_LABEL = {
    "explicit_topic": "钉住指定",
    "wrong_retry": "错题重试",
    "due_review": "到期复习",
    "human_focus": "人导入方向",
    "weak_topic": "人导入方向",
    "tool_coverage": "补工具覆盖",
    "blocked_diagnostic": "拿不到证据·重复",
    "all_stalled": "全部停滞·重复",
    "recommended": "推进新题",
    "default": "补最弱的",
    "cold_start": "冷启动",
}


def next_brief() -> dict[str, Any]:
    """给面板用的推荐卡。"""
    sel = select_next()
    task = TASKS.get(sel["topic_id"])
    return {
        **sel,
        "reason_label": REASON_LABEL.get(sel["reason"], sel["reason"]),
        "question": task.question if task else "",
        "difficulty": task.difficulty if task else 0,
        "cmd": f"python mve/run_mve.py --llm --topic {sel['topic_id']}",
    }
