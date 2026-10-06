#!/usr/bin/env python3
"""出题器 + 练习范围（照猫娘伴学的 practice_scope）。

伴学的两条机制，这里是同构实现：

1. PracticeScope（practice_scope.py:12）：mode = explicit_scope | explicit_topic
   —— 面板上既能选一个范围（多题轮着来），也能钉住一道题。

2. ordered_scope_topics（practice_scope.py:248-268）：下一题怎么挑
   排序键 = (attempted, depth, difficulty, id)
   —— 没做过的排前面，做过的排后面，同档按难度升序。
   再加上 filter_question_params_to_scope 里的 retry_wrong_questions 优先：
   错题排最前。

本文件不 import vlml_env（避免面板启动时把整个 VLML 拉起来）。
"""

from __future__ import annotations

import re
from typing import Any

from loop_core import AnswerSpec, RubricPoint, Task  # noqa: E402

SERIES = "2843069"
C9 = "Cloud9"


def _fb_sql(where: str) -> str:
    """首血类指标的确定性 SQL：返回 (转换率%, 分母)。"""
    return (
        "SELECT ROUND(AVG(fb_team_won)*100, 1) AS conv, COUNT(*) AS n "
        f"FROM agg_first_blood_stats WHERE {where}"
    )


def _streak_sql(map_name: str, team: str = C9) -> str:
    """最长连败的确定性 SQL（gaps-and-islands）。

    实测踩过的两个坑，顺序不能反：
    1. 过滤必须放在编号**之后**——先对全序列编号，再筛目标队输的那些回合，
       否则整段被当成一整块（实测算出 13 而不是 8）。
    2. 起始回合要取「最长那一段」的起点，不能直接 MIN——
       否则拿到的是全组最小值（实测算出 R1 而不是 R7）。
    """
    return (
        "SELECT cnt AS max_streak, start_round FROM ("
        "  SELECT COUNT(*) AS cnt, MIN(round_number) AS start_round FROM ("
        "    SELECT round_number, losing_team_name,"
        "           ROW_NUMBER() OVER (ORDER BY round_number) AS rn,"
        "           ROW_NUMBER() OVER (PARTITION BY losing_team_name ORDER BY round_number) AS grp"
        f"    FROM rounds WHERE series_id='{SERIES}' AND map_name='{map_name}'"
        "  ) t"
        f"  WHERE losing_team_name='{team}'"
        "  GROUP BY (rn - grp)"
        ") x ORDER BY cnt DESC, start_round ASC LIMIT 1"
    )


# ---------------------------------------------------------------------------
# 题目注册表
# ---------------------------------------------------------------------------

TASK = Task(
    topic_id="fb_conversion_analysis",
    question="Cloud9 输掉这个 series，首血转换上出了什么问题？",
    difficulty=3,
    min_base=20,
    requires_tools=["match_summary_report", "query_sql"],
    rubric=[
        RubricPoint(
            point="整体首血次数",
            subject={"series": SERIES, "team": C9},
            dimension="opening_duels.fb",
            weight=25,
            min_base=1,
            answer_spec=AnswerSpec(
                sql="SELECT COUNT(*) AS n, (SELECT COUNT(*) FROM agg_first_blood_stats) AS total "
                    f"FROM agg_first_blood_stats WHERE fb_team='{C9}'",
                value_column=0, base_column=1, numeric_tolerance=0,
            ),
        ),
        RubricPoint(
            point="整体首血转换率",
            subject={"series": SERIES, "team": C9},
            dimension="conversion.fb_conv",
            weight=25,
            min_base=20,
            answer_spec=AnswerSpec(
                sql=_fb_sql(f"fb_team='{C9}'"),
                value_column=0, base_column=1, numeric_tolerance=0.5,
            ),
        ),
        RubricPoint(
            point="Corrode 图首血转换率",
            subject={"series": SERIES, "map": "Corrode", "team": C9},
            dimension="map_fb_conv",
            weight=30,
            min_base=3,
            answer_spec=AnswerSpec(
                sql=_fb_sql(f"map_name='Corrode' AND fb_team='{C9}'"),
                value_column=0, base_column=1, numeric_tolerance=0.5,
            ),
        ),
        RubricPoint(
            point="Haven 图首血转换率",
            subject={"series": SERIES, "map": "Haven", "team": C9},
            dimension="map_fb_conv",
            weight=20,
            min_base=3,
            answer_spec=AnswerSpec(
                sql=_fb_sql(f"map_name='Haven' AND fb_team='{C9}'"),
                value_column=0, base_column=1, numeric_tolerance=0.5,
            ),
        ),
    ],
)


TASK_HARD = Task(
    # 口径必须写进题干：实测 18 轮全 0%，模型算出 max_losing_streak=14/13、裁判是 8，
    # streak_start_round 算出 1/6、裁判是 7 —— 它调了工具也调对了工具，
    # 错的是**连败怎么算**。而裁判的口径藏在 _streak_sql 里（gaps-and-islands），
    # 题干一个字没提，模型只能猜。猜不中的题，跑多少轮都是 0%。
    topic_id="corrode_collapse",
    question="Cloud9 在这场 series 里，单张图上最长的一段连续丢分发生在哪张图？"
             "这一段一共有多少回合、从哪个回合号开始？"
             "（连败口径：**只在同一张图内**按回合号顺序计算，"
             "中间只要赢下一回合就算断开；不按半场重置，"
             "也不是「全场一共输了多少回合」。）"
             "另外给出 Cloud9 在这张图上的首血转换率。",
    difficulty=4,
    min_base=1,
    requires_tools=["query_sql"],
    rubric=[
        RubricPoint(
            point="失分最严重那张图的首血转换率（subject 里指出是哪张图）",
            subject={"series": SERIES, "map": "Corrode", "team": C9},
            dimension="map_fb_conv",
            weight=20,
            min_base=3,
            answer_spec=AnswerSpec(
                sql=_fb_sql(f"map_name='Corrode' AND fb_team='{C9}'"),
                value_column=0, base_column=1, numeric_tolerance=0.5,
            ),
        ),
        RubricPoint(
            point="最长连败回合数（同图内按回合号连续，赢一回合即断开，不按半场重置）",
            subject={"series": SERIES, "map": "Corrode", "team": C9},
            dimension="max_losing_streak",
            weight=40,
            min_base=1,
            answer_spec=AnswerSpec(
                sql=_streak_sql("Corrode"),
                value_column=0, numeric_tolerance=0,
            ),
        ),
        RubricPoint(
            point="连败起始回合号（最长那一段的第一个回合号，不是全场最小回合号）",
            subject={"series": SERIES, "map": "Corrode", "team": C9},
            dimension="streak_start_round",
            weight=40,
            min_base=1,
            answer_spec=AnswerSpec(
                sql=_streak_sql("Corrode"),
                value_column=1, numeric_tolerance=0,
            ),
        ),
    ],
)


# 第三题：介于两者之间——只考验「按图下钻」，不需要窗口函数
TASK_MID = Task(
    topic_id="map_rounds_split",
    question="三张图各自打了多少回合？Cloud9 在每张图上的胜率分别是多少？",
    difficulty=2,
    min_base=1,
    requires_tools=["query_sql"],
    rubric=[
        RubricPoint(
            point="Corrode 回合数",
            subject={"series": SERIES, "map": "Corrode"},
            dimension="map_rounds",
            weight=20,
            min_base=1,
            answer_spec=AnswerSpec(
                sql=f"SELECT COUNT(*) FROM rounds WHERE series_id='{SERIES}' AND map_name='Corrode'",
                value_column=0, numeric_tolerance=0,
            ),
        ),
        RubricPoint(
            point="Haven 回合数",
            subject={"series": SERIES, "map": "Haven"},
            dimension="map_rounds",
            weight=20,
            min_base=1,
            answer_spec=AnswerSpec(
                sql=f"SELECT COUNT(*) FROM rounds WHERE series_id='{SERIES}' AND map_name='Haven'",
                value_column=0, numeric_tolerance=0,
            ),
        ),
        RubricPoint(
            point="Lotus 回合数",
            subject={"series": SERIES, "map": "Lotus"},
            dimension="map_rounds",
            weight=20,
            min_base=1,
            answer_spec=AnswerSpec(
                sql=f"SELECT COUNT(*) FROM rounds WHERE series_id='{SERIES}' AND map_name='Lotus'",
                value_column=0, numeric_tolerance=0,
            ),
        ),
        RubricPoint(
            point="Corrode 上 Cloud9 的胜率",
            subject={"series": SERIES, "map": "Corrode", "team": C9},
            dimension="map_win_rate",
            weight=20,
            min_base=1,
            answer_spec=AnswerSpec(
                sql="SELECT ROUND(AVG(CASE WHEN winning_team_name='%s' THEN 1.0 ELSE 0.0 END)*100, 1), COUNT(*) "
                    f"FROM rounds WHERE series_id='{SERIES}' AND map_name='Corrode'" % C9,
                value_column=0, base_column=1, numeric_tolerance=0.5,
            ),
        ),
        RubricPoint(
            point="Haven 上 Cloud9 的胜率",
            subject={"series": SERIES, "map": "Haven", "team": C9},
            dimension="map_win_rate",
            weight=20,
            min_base=1,
            answer_spec=AnswerSpec(
                sql="SELECT ROUND(AVG(CASE WHEN winning_team_name='%s' THEN 1.0 ELSE 0.0 END)*100, 1), COUNT(*) "
                    f"FROM rounds WHERE series_id='{SERIES}' AND map_name='Haven'" % C9,
                value_column=0, base_column=1, numeric_tolerance=0.5,
            ),
        ),
    ],
)


# 第四题：唯一能考出「用没用 pattern_detection_report」的题
#
# 设计要点：pistol / eco 这些指标用 query_sql 从 agg_team_round_stats 也能算出来，
# 所以它们**区分不出** Voyager 到底用了哪个工具。真正只有 pattern_detection_report
# 会给的是 `scope.confidence`（置信度标签，阈值 100/50/20）—— 于是把它设成权重
# 最高的评分点：拿到它 = 确实用了这个工具，拿不到 = 绕过去了。
TASK_PATTERN = Task(
    topic_id="pistol_eco_pattern",
    question="Cloud9 的手枪局（pistol round）和 eco 局表现怎么样？"
             "另外，就现有数据量而言，这个结论的置信度是多少？",
    difficulty=3,
    min_base=1,
    requires_tools=["pattern_detection_report"],
    rubric=[
        RubricPoint(
            point="手枪局胜率（0-100 的百分比数值，如 50.0）",
            subject={"series": SERIES, "team": C9},
            dimension="pistol_win_rate",
            weight=25,
            min_base=1,
            answer_spec=AnswerSpec(
                tool="pattern_detection_report",
                tool_args={"team_name": C9},
                value_path="key_metrics.economy.pistol.num",
                base_path="key_metrics.economy.pistol.denom",
                percent=True, numeric_tolerance=0.5,
            ),
        ),
        RubricPoint(
            point="eco 局胜率（0-100 的百分比数值，如 42.9）",
            subject={"series": SERIES, "team": C9},
            dimension="eco_win_rate",
            weight=20,
            min_base=1,
            answer_spec=AnswerSpec(
                tool="pattern_detection_report",
                tool_args={"team_name": C9},
                value_path="key_metrics.economy.eco.num",
                base_path="key_metrics.economy.eco.denom",
                percent=True, numeric_tolerance=0.5,
            ),
        ),
        RubricPoint(
            point="支撑这个结论的样本回合数（整数）",
            subject={"series": SERIES, "team": C9},
            dimension="pattern_rounds",
            weight=25,
            min_base=1,
            answer_spec=AnswerSpec(
                tool="pattern_detection_report",
                tool_args={"team_name": C9},
                value_path="scope.rounds",
                numeric_tolerance=0,
            ),
        ),
        RubricPoint(
            point="置信度标签（只填 moderate / strong / weak / insufficient 之一，原样取自工具返回的 scope.confidence）",
            subject={"series": SERIES, "team": C9},
            dimension="pattern_confidence",
            weight=30,
            min_base=1,
            answer_spec=AnswerSpec(
                tool="pattern_detection_report",
                tool_args={"team_name": C9},
                value_path="scope.confidence",
                numeric_tolerance=0,
            ),
        ),
    ],
)


# 第一题：难度 1 —— 难度阶梯的起点
#
# 为什么要专门注册一道难度 1 的题：实测 45 轮里 corrode_collapse（难度 4）
# 错 12 次、覆盖率全 0%，planner 的 wrong_retry 又是无限优先，别的题永远轮不到
# —— 难度阶梯断在最底下，Voyager 一直在"没有成功经验"的题上打转。
# 这道题只需要一次单表聚合就能全对，作用是让 Voyager 拿到**第一个 correct**，
# 掌握度序列从 None 变成有值，之后的渐进才有起点。
#
# 注意：人导入**不能创建新题**（导入的只是指向注册题的意图，control fact 里
# 存的是 topic_id），所以难度阶梯必须从题目注册表这边补。
TASK_EASY = Task(
    topic_id="series_totals",
    question="Cloud9 这场比赛总共打了多少个回合？其中赢下了多少个回合？",
    difficulty=1,
    min_base=1,
    requires_tools=["query_sql"],
    rubric=[
        RubricPoint(
            point="全场总回合数（一个整数）",
            subject={"series": SERIES},
            dimension="total_rounds",
            weight=50,
            min_base=1,
            answer_spec=AnswerSpec(
                sql=f"SELECT COUNT(*) FROM rounds WHERE series_id='{SERIES}'",
                value_column=0, numeric_tolerance=0,
            ),
        ),
        RubricPoint(
            point="Cloud9 赢下的回合数（一个整数）",
            subject={"series": SERIES, "team": C9},
            dimension="rounds_won",
            weight=50,
            min_base=1,
            answer_spec=AnswerSpec(
                sql="SELECT SUM(CASE WHEN winning_team_name='%s' THEN 1 ELSE 0 END) "
                    f"FROM rounds WHERE series_id='{SERIES}'" % C9,
                value_column=0, numeric_tolerance=0,
            ),
        ),
    ],
)


# --------------------------------------------------------------------------
# 「从未出过的新题」—— 用来验证种子层
# --------------------------------------------------------------------------
# 存在的唯一理由：图谱的维度节点全部从 TASKS[].rubric 派生，所以**出过题的
# 才进图**。这一道题**故意不重建图谱**（它的维度 kast_pct / adr_value 不在
# knowledge_graph.json 里），于是老路径 render_for_prompt(topic_id) 返回空串，
# 只有种子层（按题干匹配 212 个独立于题目的事实）能给它图谱。
# 它是"图谱到底是不是所有题都能用"的判别题：没有它，任何新题都拿不到图谱，
# 而旧题本来就饱和（100%），根本测不出来。
TASK_NEW = Task(
    topic_id="kast_adr_check",
    question="Cloud9 这场比赛的 KAST 和 K/D 各是多少？"
             "（口径：**队伍整体**，取 match_analysis_report 的 team 段，"
             "不是 opponent，也不是某个选手。）",
    difficulty=2,
    min_base=1,
    requires_tools=["match_analysis_report"],
    rubric=[
        RubricPoint(
            point="KAST（0-100 的百分比，如 100.0）",
            subject={"series": SERIES, "team": C9},
            dimension="kast_pct",
            weight=50,
            min_base=1,
            # percent 口径照 pistol 那道题：**必须指到 .num**，另配 base_path
            # 指到 .denom。第一版我写成 `...kast`（少了 .num），裁判取到的是
            # 整个 {num,denom} 字典，于是 got=100.0 与 ref={...} 永远不可比 ——
            # 两组 A/B 都是 0%，但错的是题不是模型（模型答的 100.0 是对的）。
            answer_spec=AnswerSpec(
                tool="match_analysis_report",
                tool_args={"series_id": SERIES},
                value_path="key_metrics.team.consistency.kast.num",
                base_path="key_metrics.team.consistency.kast.denom",
                percent=True, numeric_tolerance=0.5,
            ),
        ),
        RubricPoint(
            point="K/D（比值，如 1.24）",
            subject={"series": SERIES, "team": C9},
            dimension="kd_ratio",
            weight=50,
            min_base=1,
            # 标量指标（不是 num/denom 对）：直接指到它，不要 percent。
            answer_spec=AnswerSpec(
                tool="match_analysis_report",
                tool_args={"series_id": SERIES},
                value_path="key_metrics.team.consistency.kd",
                percent=False, numeric_tolerance=0.05,
            ),
        ),
    ],
)


TASKS: dict[str, Task] = {
    TASK_EASY.topic_id: TASK_EASY,
    TASK_MID.topic_id: TASK_MID,
    TASK.topic_id: TASK,
    TASK_HARD.topic_id: TASK_HARD,
    TASK_PATTERN.topic_id: TASK_PATTERN,
    TASK_NEW.topic_id: TASK_NEW,
}


def _merge_generated() -> int:
    """把 `question_gen` 验过的题并入 TASKS。

    为什么必须在这里合：图谱的维度节点全部从 `TASKS[].rubric` 派生
    （tasks.py:360），不合进来的话，自动生成的题既进不了掌握度、也进不了图谱，
    等于生成了个寂寞。

    默认 `validated_target=False` —— 照伴学：只有 validated 的题才计分，
    **生成 ≠ 生效**，先要能被裁判跑通并被确认口径。
    """
    try:
        import question_gen
    except Exception:                                        # pragma: no cover
        return 0
    added = 0
    for rec in question_gen.load_generated():
        topic = str(rec.get("topic_id") or "").strip()
        if not topic or topic in TASKS:
            continue
        try:
            task = question_gen.to_task(rec)
        except Exception:                                    # pragma: no cover
            continue
        if not task.rubric:
            continue
        TASKS[topic] = task
        added += 1
    return added


GENERATED_COUNT = _merge_generated()


# ---------------------------------------------------------------------------
# 练习范围与下一题（照 practice_scope.ordered_scope_topics）
# ---------------------------------------------------------------------------

def next_topic(
    *,
    attempted: dict[str, int],
    failed: dict[str, int],
    current: str | None = None,
    mode: str = "explicit_scope",
    only: list[str] | None = None,
) -> str:
    """挑下一题。

    排序照伴学 ordered_scope_topics（practice_scope.py:260-267）：
        (已做过?, 错题?, 难度, id)
    另加伴学 filter_question_params_to_scope 的规矩：错题（retry_wrong_questions）优先。

    only —— 练习范围（practice_scope.py）。人在知识图谱上点一个维度把它设成范围，
    出题器就只在这个范围里出题；给了 only 就**不**退化成全库（范围里实在没题了
    才退回全库，那说明范围本身失效，practice_scope 已经标 invalidated 了）。
    """
    if mode == "explicit_topic" and current in TASKS:
        return current

    def key(topic_id: str) -> tuple:
        task = TASKS[topic_id]
        done = topic_id in attempted
        wrong = topic_id in failed
        return (
            not wrong,       # 错题优先（retry_wrong_questions）
            done,            # 没做过的优先（ordered_scope_topics 的 attempted 键）
            task.difficulty,  # 难度升序
            topic_id,
        )

    pool = [t for t in (only or []) if t in TASKS] if only else None
    if pool:
        return min(pool, key=key)
    return min(TASKS, key=key)


def task_brief(task: Task) -> dict[str, Any]:
    """面板上题目卡片的字段（不泄露 answer_spec —— 那是服务端私有）。"""
    return {
        "topic_id": task.topic_id,
        "question": task.question,
        "difficulty": task.difficulty,
        "key_points": [p.point for p in task.rubric],
        "weights": {p.point: p.weight for p in task.rubric},
        "dims": sorted({p.dimension for p in task.rubric}),
        "requires_tools": list(task.requires_tools or []),
        "has_answer_spec": sum(1 for p in task.rubric if p.answer_spec is not None),
    }


def all_briefs() -> list[dict[str, Any]]:
    return [task_brief(t) for t in TASKS.values()]


# ---------------------------------------------------------------------------
# 人导入的问题归到哪一题
# ---------------------------------------------------------------------------
#
# 为什么必须有这一步：出题器的 weak_topic / human_focus 分支是按 topic_id 读
# 因果时间线的（`causal_timeline.counts_by_topic()` 会跳过 topic_id 为空的行）。
# 人从面板上随手打一句话，不可能要求他先背下 topic_id —— 于是必须把自然语言
# 归到某一题上。归不上就诚实地留空，并在面板上写明「这条不驱动出题」，
# 而不是悄悄塞一个默认值。
#
# 打分用「命中词的长度」而不是「命中个数」：长词（首血转换、置信度）比
# 短词（图、胜率）更能说明归属，避免问「三张图胜率」被归到 pistol 题。
TOPIC_HINTS: dict[str, tuple[str, ...]] = {
    "series_totals": (
        "总共打了多少回合", "一共多少回合", "总共多少回合", "总共打了", "全场",
        "赢下了多少回合", "赢了多少回合", "赢下多少", "总回合", "total_rounds",
    ),
    "pistol_eco_pattern": (
        "手枪", "手枪局", "pistol", "eco", "强起", "经济局", "半买",
        "置信度", "置信", "confidence", "样本量够不够",
    ),
    "corrode_collapse": (
        "崩盘", "崩", "连败", "连丢", "最长", "起始回合", "第几回合开始",
        "最难看", "输得最惨", "streak", "一口气",
    ),
    "fb_conversion_analysis": (
        "首血", "首杀", "转换率", "转换", "fb_conv", "first blood",
        "开局对枪", "拿到首血之后",
    ),
    "map_rounds_split": (
        "三张图", "每张图", "各打了多少回合", "回合数", "图上的胜率",
        "map_rounds", "map_win_rate", "分图",
    ),
}


def _auto_hints() -> dict[str, tuple[str, ...]]:
    """从 TASKS **自动派生**归题关键词 —— 手写表 `TOPIC_HINTS` 的兜底。

    为什么必须自动：`kast_adr_check` 是后加的题，手写表里没有它 ——
    实测「Cloud9 这场比赛的 KAST 是多少」直接归不上，于是这条 control fact
    的 topic_id 为空，出题器 `latest_topics_by_kind()` 跳过它，
    人导入的意图就这么断了。**每加一道题补一次手写表，迟早还会再漏一次。**

    派生的三源（都是服务端既有的硬事实，不需要人工维护）：
      1. `rubric.dimension` 的英文词干：kast_pct → kast / pct
      2. `rubric.point` 里的大写缩写：KAST、ADR
      3. `rubric.point` 里的比值写法：K/D
    """
    out: dict[str, set[str]] = {}
    for tid, task in TASKS.items():
        words = out.setdefault(tid, set())
        for p in task.rubric:
            for w in re.split(r"[^A-Za-z0-9]+", str(p.dimension) or ""):
                if len(w) >= 3:
                    words.add(w.lower())
            for abbr in re.findall(r"[A-Z]{2,}", str(p.point) or ""):
                words.add(abbr.lower())
            for ratio in re.findall(r"[A-Za-z]/[A-Za-z]", str(p.point) or ""):
                words.add(ratio.lower())
    return {k: tuple(sorted(v)) for k, v in out.items()}


AUTO_HINTS: dict[str, tuple[str, ...]] = _auto_hints()


def infer_topic(question: str) -> tuple[str, str]:
    """把一句自然语言归到最可能的那道题。返回 (topic_id, 命中的词)。

    归不上返回 ("", "") —— 调用方必须处理这个分支，不能默认第一题。

    两级：先查人工精修的 `TOPIC_HINTS`，再查从 TASKS 自动派生的
    `AUTO_HINTS`（后加的题没补手写表时靠它兜住）。
    """
    q = (question or "").lower()
    if not q.strip():
        return "", ""
    best_topic, best_hit, best_score = "", "", 0
    # 手写表优先（人工精修过的更准）；自动派生的词短且泛（map / win / rate），
    # 只作兜底，且要求 ≥3 字符 —— 长度闸**只管自动派生**，
    # 手写的中文单字（「崩」）也是有效关键词，不能被它滤掉。
    for source, weight, minlen in ((TOPIC_HINTS, 0, 0), (AUTO_HINTS, -2, 3)):
        for topic, hints in source.items():
            if topic not in TASKS:
                continue
            for h in hints:
                hl = h.lower()
                if len(h) < minlen or hl not in q:
                    continue
                # 英文词加一点权重：它是精确的维度名，比中文字面更可靠
                score = len(h) + (3 if h.isascii() else 0) + weight
                if score > best_score:
                    best_topic, best_hit, best_score = topic, h, score
        if best_topic:
            break
    return best_topic, best_hit
