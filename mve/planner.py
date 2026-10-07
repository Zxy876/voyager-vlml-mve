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
import difficulty  # noqa: E402
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

# 连续满分多少次算「这道题已经被拿下」。拿下之后不该再反复考它，
# 而该把它当成"可以往上走一档"的信号 —— 见 `_mastery_by_topic` 里
# `cleared` 那条注释（缺了它就是一个全库满分的死循环）。
CLEAR_RUN = 3


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
        # 卡住了：连续 N 次覆盖率一字不差，**且还没满分** → 再练也是重复。
        rec["stalled"] = run >= STALE_LIMIT and rec["last_coverage"] < 1.0
        # 拿下了：连续 N 次满分 → 该上难度了（此前这一档根本不存在）。
        #
        # 缺了它的后果是一个隐蔽的死循环：全库满分时 stalled 恒为 False
        # （`last_coverage < 1.0` 不满足），于是 `all_stalled` 分支永不触发，
        # 出题器**永远不会去生成更难的题** —— 实测 kast 连对 10 次、
        # pistol 连对 6 次，stale_run 远超阈值却一个都不算停滞。
        # 换言之：「做错了卡住」是信号，「做对了」压根不是信号。
        # 而伴学恰恰相反 —— 掌握一档就推进下一档（`difficulty_policy` +
        # 82 个知识点 × 3 档难度的题库铺开），做对了才是上难度的触发条件。
        rec["cleared"] = run >= CLEAR_RUN and rec["last_coverage"] >= 1.0

    # 难度自适应要用的三个字段（`difficulty.py` 的判据照伴学 difficulty_policy）。
    # 只取**最近一次**的快照：掌握度是序列的投影，用历史累计会把"曾经很好"
    # 当成"现在很好"，那正是伴学用 recent_results 而不是累计的原因。
    for topic, rec in out.items():
        recent = [r for r in rows if str(r.get("topic_id") or "") == topic]
        last = recent[-1] if recent else {}
        rec["confidence"] = float(last.get("mastery_confidence") or 0.0)
        rec["flags"] = list(last.get("flags") or [])
        rec["recent_verdicts"] = [str(r.get("verdict") or "")
                                  for r in recent[-2:]]
    return out


def _has_reusable_program(topic: str) -> bool:
    """技能库里有没有这道题**能直接跑**的程序（有源码 + 有主函数名）。

    这是「能不能收支架」的前提：收支架的意思是"它已经会了，不用扶"，
    而"会了"在本原型里的可观测形态就是**库里有跑通过的程序可以复用**。
    没有程序就收支架不是提高难度，只是让它重犯老病 —— 实测同一道题
    full 能取对（kast={109/109}、kd=1.24），partial / none 会取成 1.0。
    """
    try:
        import skill_store
    except Exception:
        return False
    for s in skill_store.all_skills():
        if str(s.get("topic") or "") != topic:
            continue
        if str(s.get("source") or "") != "practice":
            continue
        if not str(s.get("code") or ""):
            continue
        if not str((s.get("blueprint") or {}).get("program_name") or ""):
            continue
        return True
    return False


def _hint_for(topic: str, rec: dict[str, Any] | None, reason: str) -> dict[str, Any]:
    """这道题该给多少支架（照伴学 difficulty_policy.select 的同构物）。"""
    seed = TASKS[topic].difficulty if topic in TASKS else 2
    d = difficulty.select(
        seed,
        mastery=(rec or {}).get("last_mastery") or 0.0,
        coverage=(rec or {}).get("last_coverage"),
        attempts=(rec or {}).get("attempts") or 0,
        confidence=(rec or {}).get("confidence") or 0.0,
        flags=(rec or {}).get("flags") or (),
        recent_verdicts=(rec or {}).get("recent_verdicts") or (),
        reason=reason,
    )
    # 收支架的前提：库里有能直接跑的程序。没有就按 full 兜底。
    if d["hint"] != difficulty.HINT_FULL and not _has_reusable_program(topic):
        d["hint"] = difficulty.HINT_FULL
        d["why"] += "；但这题还没有可复用的程序 → 支架先不收"
    return d


def _with_hint(sel: dict[str, Any], mastery: dict[str, Any]) -> dict[str, Any]:
    """给一次选择补上「目标难度 + 支架档位 + 为什么」。"""
    rec = mastery.get(sel["topic_id"])
    d = _hint_for(sel["topic_id"], rec, sel["reason"])
    sel["difficulty_target"] = d["difficulty"]
    sel["hint"] = d["hint"]
    sel["hint_label"] = difficulty.HINT_LABEL[d["hint"]]
    sel["difficulty_why"] = d["why"]
    return sel


def _used_tools() -> set[str]:
    """Voyager 到目前为止真正调成功过的工具（从运行日志的轨迹里统计）。"""
    used: set[str] = set()
    for r in run_log.load_all():
        for t in (r.get("trajectory") or []):
            if isinstance(t, str) and t and not t.startswith("<"):
                used.add(t)
    return used


def _global_target(mastery: dict[str, Any]) -> tuple[int, str]:
    """全局目标难度：**会做的题越多，接下来该啃越难的**。

    伴学不需要这一条 —— 它的难度是按 (知识点, 难度) 生成题目时直接算出来的
    （`difficulty_policy.select`）。MVE 的题是硬编码的、每题一个写死的难度，
    所以梯度只能落在"从哪道题往上走"上。

    规则（MVE 自创，伴学没有同构物）：
        目标难度 = 1 + 已满分（覆盖率 100%）的题数，clamp [1, 4]
    一道都没拿下 → 出最基础的；拿下 1 道 → 上难度 2；拿下 3 道 → 上难度 4。
    它把「掌握度」直接翻译成「下一步往哪走」，而不只是排序。
    """
    cleared = sum(1 for t, m in mastery.items()
                  if t in TASKS and float(m.get("last_coverage") or 0) >= 1.0)
    target = min(4, max(1, 1 + cleared))
    return target, f"已拿下 {cleared} 道 → 目标难度 {target}"


def _by_gradient(candidates: list[str], target: int) -> str:
    """在候选里挑**离目标难度最近**的（同难度时取更简单的）。"""
    return min(candidates,
               key=lambda t: (abs(TASKS[t].difficulty - target),
                              TASKS[t].difficulty, t))


def select_next(*, explicit_topic_id: str = "") -> dict[str, Any]:
    """挑下一题，并说明为什么。

    返回 {topic_id, reason, explanation, blocked, hint, difficulty_target}
    """
    mastery = _mastery_by_topic()
    target, why_target = _global_target(mastery)
    sel = _select_raw(explicit_topic_id=explicit_topic_id, mastery=mastery,
                      target=target)
    sel = _with_hint(sel, mastery)
    sel["difficulty_target"] = target
    sel["difficulty_why"] = why_target + "｜" + str(sel.get("difficulty_why") or "")
    return sel


def _sql_dims() -> set[str]:
    """能自己写 SQL 的维度（服务端声明 `by_sql=True`）。

    为什么必须区分：工具路径的维度受工具签名限制（`kast_pct` 只有
    `match_analysis_report` 的队伍级口径），既换不了 subject 粒度，
    也写不出窗口函数 —— 拿它当"难度 4 的新题方向"必然失败。
    实测就是这样：`_new_topic_suggestion` 挑了 kast_pct 当 focus，
    要求「用队员级范围出题」，模型只能硬去查 match_players_report
    （那是工具名不是表），三次全挂在同一个错上。
    """
    try:
        import question_gen
        specs = question_gen._dim_specs()
    except Exception:                                        # pragma: no cover
        return set()
    return {str(d) for d, s in (specs or {}).items()
            if isinstance(s, dict) and s.get("by_sql")}


def _new_topic_suggestion(
    settled: list[tuple[str, dict[str, Any]]],
    *,
    target: int = 2,
    mode: str = "stalled",
) -> dict[str, Any]:
    """照伴学 `weak_topic`：先确定「练什么」，再交给出题器去出题。

    伴学这一步是 `get_weak_topics()`（knowledge_tracker.py:2066）给出薄弱知识点，
    planner 据此定 `selection_reason=weak_topic`；**planner 自己不调 LLM** ——
    生成题面是 entry 层的事（`entry_tutor_question_entries.py:1289
    _generate_question_payload_impl`）。MVE 同构：这里只产出
    「围绕什么出题 + 难度 + 为什么」，一个字都不交给模型。

    为什么必须补这一层：伴学在「没有可推进的题」时会走 `weak_topic` 去**生成**一道
    新题；MVE 原版走到 `all_stalled` 就只会标 blocked 卡住 —— 这正是
    「连着 10 次停在 50%、没有可推进的题了」那个症状的根因。

    **难度必须接梯度**（此前不接，是这条闭环最大的断口）：
      stalled（卡住）→ 新题难度 = max(全局目标, 薄弱题难度) —— 补薄弱点，别出更简单的
      cleared（拿下）→ 新题难度 = min(4, 全局目标 + 1) —— 这是**推进一档**，
                       否则全库满分时永远只会生成同档题，题库涨了但难度不涨。
    此前一律 `int(task.difficulty)` 抄停滞题的种子难度，梯度算出的 `target`
    到这里就断了 —— 于是「自适应梯度」只作用在"选哪道旧题"，进不了"出什么新题"。
    """
    if not settled:
        return {}
    topic, m = min(settled, key=lambda kv: (kv[1]["last_coverage"],
                                            -kv[1]["stale_run"]))
    task = TASKS.get(topic)
    seed = int(getattr(task, "difficulty", 2) or 2)
    if mode == "cleared":
        # 全被拿下 → 往上推一档（clamp 到 4：题库上限，再往上没有口径可出）
        difficulty = min(4, max(1, int(target or 2) + 1))
    else:
        # 卡住了 → 至少不低于全局目标，也不低于薄弱题本身的难度
        difficulty = min(4, max(1, max(int(target or 2), seed)))
    dims = [str(p.dimension) for p in (task.rubric if task else [])]
    # focus 必须是**能自己写 SQL** 的维度：工具维度换不了 subject 粒度，
    # 也写不出难度 3+ 要求的结构（JOIN / 窗口函数）。
    # 此前直接取 dims[0]，于是挑中了 kast_pct（by_tool、无队员级口径）
    # 去出一道难度 4 的题 —— 那个方向在数据里根本无解，三次生成全挂。
    sql_dims = _sql_dims()
    focus = next((d for d in dims if d in sql_dims), "")
    if not focus:
        # 本题全是工具维度 → 从题库里另挑一个 SQL 维度当方向，
        # 否则「出一道难度 {difficulty} 的题」这个要求无从落地。
        focus = next(iter(sorted(sql_dims)), "") or (dims[0] if dims else "")
    # 已有题的 subject 用过哪些键 —— 新题必须换一个键，否则撞「与已有题重复」闸
    used: set[str] = set()
    for t in TASKS.values():
        for p in t.rubric:
            used.update((p.subject or {}).keys())
    if "player" not in used:
        want, what = "player", "队员级"
    elif "map" not in used:
        want, what = "map", "单图"
    else:
        want, what = "", "换个角度"
    return {
        "about": f"围绕「{focus}」这个口径，用{what}的范围出一道新题"
                 + (f"（subject 带上 {want} 键）" if want else "（换一个 subject）"),
        "focus_dimension": focus,
        "subject_key": want,
        "difficulty": difficulty,
        "why": ((f"{topic} 连着 {m['stale_run']} 次满分（全库已被拿下）→ "
                 f"往上推一档，按难度 {difficulty} 出题"
                 f"（全局目标 {target}）") if mode == "cleared" else
                (f"{topic} 连着 {m['stale_run']} 次停在 {m['last_coverage']:.0%}，"
                 f"它考的口径（{focus or '未知'}）就是当前的薄弱点"
                 f" → 按难度 {difficulty} 出（全局目标 {target}）")),
    }


def _select_raw(*, explicit_topic_id: str, mastery: dict[str, Any],
                target: int) -> dict[str, Any]:

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
    # 已被拿下的题（连续满分）：它们不再需要练，但**是上难度的信号**。
    cleared = [(t, m) for t, m in mastery.items() if t in TASKS and m["cleared"]]

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
        topic = _by_gradient(fresh, target)
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
        topic = _by_gradient(untouched, target)
        return {
            "topic_id": topic,
            "reason": "recommended",
            "explanation": f"这道还没练过，难度 {TASKS[topic].difficulty} 离目标难度 {target} 最近 —— 从它往上走",
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
        topic, m = min(pushable, key=lambda kv: (kv[1]["last_coverage"],
                       abs(TASKS[kv[0]].difficulty - target)))
        return {
            "topic_id": topic,
            "reason": "default",
            "explanation": f"都练过了，挑覆盖率最低、且还没停滞的这道"
                           f"（{m['last_coverage']:.0%}）",
            "blocked": False,
        }

    # 5.5) 没有可推进的题了 —— 如实说清，别假装还在自适应。
    #     两种"推不动"，性质相反，措辞与出法都必须分开：
    #
    #       all_stalled  = 卡住了（没满分，再练也是重复）→ 补薄弱点，难度跟着薄弱题走
    #       all_cleared  = 拿下了（全满分）→ **该上难度了**，难度比目标再推一档
    #
    #     后者此前根本不存在：`stalled` 判据要求 `last_coverage < 1.0`，
    #     于是全库满分时出题器只会掉到冷启动兜底，在几道满分的题里来回挑，
    #     永远不去生成更难的题 —— "做对了"压根不是信号。而伴学的做法恰恰
    #     相反：掌握一档就推进下一档（82 个知识点 × 3 档难度铺开的题库）。
    if stalled:
        topic, m = min(stalled, key=lambda kv: (kv[1]["last_coverage"], -kv[1]["stale_run"]))
        return {
            "topic_id": topic,
            "reason": "all_stalled",
            "explanation": f"没有可推进的题了：这道连着 {m['stale_run']} 次停在 "
                           f"{m['last_coverage']:.0%}（其余未满分的题也都停滞）—— "
                           f"再排它只是重复",
            "blocked": True,
            # 伴学在这里走 weak_topic 去**生成**一道新题（entry_tutor_answer_entries
            # .py:174：reason != due_review 就 action=generate_question）。
            # MVE 补齐这一环：planner 只给「练什么」，生成由 entry 层做。
            "suggestion": _new_topic_suggestion(stalled, target=target,
                                                mode="stalled"),
            "action": "generate_question",
        }

    # 5.6) 全被拿下：这是**上难度**的信号，不是"没事可做"。
    #     此前没有这一支，所以题库永远停在 6 道、难度永远上不去。
    #    判据要收紧：必须**全库都满分**才算"该上难度"。只有一两道拿下、
    #    其余还停在低覆盖率时，正确的做法是回去补缺口，不是跳档。
    unfinished = [t for t, m in mastery.items()
                  if t in TASKS and m["last_coverage"] < 1.0]
    if cleared and not unfinished:
        # 拿下次数最多的那道 —— 它最能代表「这个口径已经会了，可以往上走」
        topic, m = max(cleared, key=lambda kv: kv[1]["stale_run"])
        return {
            "topic_id": topic,
            "reason": "all_cleared",
            "explanation": (
                f"这道题连着 {m['stale_run']} 次满分（全库 {len(cleared)} 道已被拿下，"
                f"目标难度 {target}）—— 再考它测不出东西，往上一档出题"),
            "blocked": True,
            "suggestion": _new_topic_suggestion(cleared, target=target,
                                                mode="cleared"),
            "action": "generate_question",
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

    topic = _by_gradient(list(TASKS), target)
    return {
        "topic_id": topic,
        "reason": "cold_start",
        "explanation": f"还没有任何练习记录，从离目标难度 {target} 最近的开始",
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
    "all_stalled": "全部停滞·生成新题",
    "all_cleared": "全被拿下·上一档生成新题",
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
        "difficulty_target": sel.get("difficulty_target"),
        "hint": sel.get("hint", ""),
        "hint_label": sel.get("hint_label", ""),
        "difficulty_why": sel.get("difficulty_why", ""),
        "cmd": f"python mve/run_mve.py --llm --topic {sel['topic_id']}",
    }
