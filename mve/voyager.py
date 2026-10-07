#!/usr/bin/env python3
"""Voyager agent：在 VLML 上取数，交出事实集。

两种实现：
- ScriptedVoyager：不需要 LLM。用「技能库」驱动——每轮结束后把这次用到的
  工具组合记下来，下轮优先复用有效组合。用来先跑通链路。
- LLMVoyager：需要 LLM key。让模型自己决定调什么工具（真正的“学会编排”）。

两者产出同一种结构：facts[] + narrative{comparable:false}
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys
from typing import Any

from vlml_env import (  # noqa: E402  (先引导环境)
    execute_custom_sql,
    get_database_info,
    match_analysis_report,
    match_economy_report,
    match_players_report,
    match_rounds_report,
    match_summary_report,
    pattern_detection_report,
    player_profile_report,
    scouting_report,
)

import causal_timeline  # noqa: E402
import skill_store  # noqa: E402
import run_log  # noqa: E402

from loop_core import from_key_metrics, from_sql_result, make_fact  # noqa: E402
from llm_client import chat, chat_json  # noqa: E402
import feedback as feedback_mod  # noqa: E402

SERIES = "2843069"
C9, NRG = "Cloud9", "NRG"


class Skill:
    """一条技能：在某种题型下，用哪个工具组合能拿到哪些维度。"""

    def __init__(self, name: str, steps: list[str], yields: list[str]):
        self.name = name
        self.steps = steps          # 工具调用序列（取数组合，不参与评分）
        self.yields = yields        # 这条技能能拿到的 dimension 前缀
        self.used = 0
        self.hit = 0

    @property
    def success_rate(self) -> float:
        return self.hit / self.used if self.used else 0.0

    def __repr__(self) -> str:
        return f"<Skill {self.name} used={self.used} hit={self.hit} rate={self.success_rate:.2f}>"


class ScriptedVoyager:
    """用技能库驱动的 Voyager。行为随技能积累而改变——这就是「学习」的可观测部分。"""

    def __init__(self) -> None:
        self.skills: list[Skill] = []
        self.trajectory: list[dict[str, Any]] = []

    # ---- 技能库 ----
    def _ensure_skill(self, name: str, steps: list[str], yields: list[str]) -> Skill:
        for s in self.skills:
            if s.name == name:
                return s
        s = Skill(name, steps, yields)
        self.skills.append(s)
        return s

    def _pick_skill(self, need_dims: list[str]) -> Skill | None:
        """按「能否覆盖需要的维度」挑技能；没有能覆盖的就返回 None（触发探索）。"""
        best = None
        best_score = 0.0
        for s in self.skills:
            cover = sum(1 for d in need_dims if any(d.startswith(y) for y in s.yields))
            if cover == 0:
                continue
            score = cover + 0.3 * s.success_rate
            if score > best_score:
                best, best_score = s, score
        return best

    # ---- 执行 ----
    async def run(self, task: Any) -> dict[str, Any]:
        """跑一轮：挑技能 → 调工具 → 产出事实集。"""
        need_dims = [p.dimension for p in task.rubric]
        skill = self._pick_skill(need_dims)

        # 没有技能能覆盖 → 探索：先只调一个粗粒度报告（新手行为）
        if skill is None:
            skill = self._ensure_skill(
                name="整体概览",
                steps=["match_summary_report(series, team)"],
                yields=["opening_duels", "conversion", "impact", "consistency"],
            )

        facts: list[dict[str, Any]] = []
        self.trajectory = []

        if any(p.dimension.startswith("map_fb_conv") for p in task.rubric) and \
           not any(s.name == "分图下钻" for s in self.skills):
            # 需要分图维度但还没学会下钻 → 只会整体概览，覆盖不全
            pass

        # 步骤 1：整体概览（所有技能都从这里开始）
        rep = await match_summary_report(series_id=SERIES, team_name=C9)
        self.trajectory.append({"tool": "match_summary_report",
                                "args": {"series_id": SERIES, "team_name": C9}})
        km = (rep.get("key_metrics") or {}).get("team", {})
        facts += from_key_metrics(
            km, subject={"series": SERIES, "team": C9},
            tool="match_summary_report",
            scope={"rounds": 59},
        )
        skill.used += 1

        # 步骤 2：如果技能库里有「分图下钻」且本轮需要，就做完整体后下钻
        drill = next((s for s in self.skills if s.name == "分图下钻"), None)
        if drill is not None:
            rows = await execute_custom_sql(
                "SELECT map_name AS map, fb_team,"
                " COUNT(*) AS fb,"
                " ROUND(AVG(fb_team_won)*100, 1) AS conv_pct"
                " FROM agg_first_blood_stats GROUP BY 1, 2"
            )
            self.trajectory.append({"tool": "execute_custom_sql", "args": {"drill": "map"}})
            facts += [
                make_fact(
                    subject={"series": SERIES, "map": m, "team": t},
                    dimension="map_fb_conv",
                    value=c, unit="percent", base=int(n),
                    scope={"rounds": 59},
                    source={"tool": "execute_custom_sql", "section": "agg_first_blood_stats"},
                )
                for m, t, n, c in (rows.get("rows") or [])
            ]
            drill.used += 1

        return {
            "facts": facts,
            "narrative": {"text": self._narrate(facts), "comparable": False},
            "skill_used": skill.name,
        }

    def _narrate(self, facts: list[dict[str, Any]]) -> str:
        """叙事段：不参与比对，只给人看。"""
        return f"本轮采集 {len(facts)} 条事实，覆盖 {'/'.join(sorted({f['dimension'] for f in facts}))}"

    def learn(self, *, covered: list[str], missing: list[str], task: Any = None,
              referee: Any = None, verdict: str = "",
              rejected_low_base: list[str] | None = None,
              confidence: float = 0.0) -> None:
        """一轮结束后更新技能库：缺哪个 dimension，就补一条能补上它的技能。

        注意：必须按 **dimension** 判断，不能按评分点的中文名——
        名字里没有维度信息（第一版就栽在这里，技能永远学不会）。

        脚本基线**不消费**结构化反馈包（referee / verdict / rejected_low_base /
        confidence）—— 那套是 LLMVoyager 学「写程序 → 跑通 → 存程序」用的；
        这里收下它们只是为了与调用方签名一致（实测跑自动生成的新题时
        因为签名不兼容直接 TypeError 崩了）。
        """
        dim_by_point = {p.point: p.dimension for p in (task.rubric if task else [])}
        miss_dims = [dim_by_point.get(m, m) for m in missing]

        if any(d.startswith("map_") for d in miss_dims) and \
           not any(s.name == "分图下钻" for s in self.skills):
            self._ensure_skill(
                name="分图下钻",
                steps=["match_summary_report", "execute_custom_sql(agg_first_blood_stats by map)"],
                yields=["map_fb_conv"],
            )


# ---------------------------------------------------------------------------
# LLMVoyager：让模型自己决定调用什么工具、自己从缺失里学
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 工具目录：照搬 VLML 的 MCP server（vlml/src/vlml/server.py）。
# 一字不改地沿用它的 docstring —— VLML 就是把「这是什么 + 什么时候用」
# 写在这一行里，靠它引导 LLM 编排。我们对 Voyager 用的是同一份。
# 注意：MCP 对外工具名是 query_sql（Python 实现函数名才是 execute_custom_sql）。
# ---------------------------------------------------------------------------

TOOL_CATALOG = """
可用工具（全部只返回结构化数据，不给结论、不给建议）：

1. match_summary_report(series_id, team_name?, map_name?)
   轻量摘要（metadata, team_comparison, key_metrics, benchmarks）。**先看这个拿全局**。

2. match_players_report(series_id, team_name?, map_name?)
   选手视角（performance, kast_impact_analysis, opening_death_impact, highlight_rounds）。用于选手分析。

3. match_rounds_report(series_id, team_name?, map_name?, round_start?, round_end?)
   逐回合（round_timeline, round_situations, half_breakdown）。**很重，大数据集要用分页**。

4. match_economy_report(series_id, team_name?, map_name?)
   经济与战术（economy_context, attack_patterns）。用于经济级联或可预测性分析。

5. match_analysis_report(series_id, team_name?, map_name?)
   全量 18 段。需要一次拿全时才用。

6. player_profile_report(player_name, series_ids?, last_n_series?, map_name?, agent_name?)
7. scouting_report(team_name, series_ids?, last_n_series?, map_name?)
8. pattern_detection_report(team_name?, player_name?, tournament_name?, series_ids?, min_rounds?)
9. query_sql(sql_query) —— 自定义 SQL，**仅 SELECT**。想按图/按回合下钻就必须用它。
10. get_database_info() —— 拿表清单与用法提示。

常用表（query_sql 用）：
  agg_first_blood_stats(round_id, game_id, series_id, map_name, round_number,
                        fb_team, fd_team, fb_player, fd_player,
                        winning_team_name, fb_team_won, fd_team_won)
  rounds(round_id, series_id, game_id, round_number, map_name,
         winning_team_name, losing_team_name, end_reason)
  games(game_id, series_id, map_name, team1_name, team2_name, winning_team_name)
  base_events(series_id, game_id, round_id, event_type, actor_player_name,
              actor_team_name, target_player_name, is_kill, is_first_blood,
              is_plant, is_defuse, map_name, ...)
"""

# VLML 自己的用法提示，逐字照搬 get_database_info().usage_tips（实测输出，4 条）
VLML_USAGE_TIPS = """
Use agg_player_game_stats for fast player performance queries
Use base_events for detailed event-level analysis
All tables have team_name populated (91% coverage)
Metrics include: K/D, ADR, KAST%, first bloods, multi-kills
"""

# 下面这段**不是** VLML 写的，是我实测补的。
# 原因：VLML 的 usage_tips 里没提 SQL 限制，而实测会踩：
#   WITH x AS (...) SELECT ...  ->  {'error': 'Only SELECT queries are allowed'}
#   SELECT ... FROM (SELECT ...)  ->  正常返回
# 不写进目录，模型会反复撞同一面墙，而且撞了也看不出是自己写错还是工具不行。
LOCAL_SQL_NOTES = """
query_sql 的实测限制（踩过）：
- 语句必须以 SELECT 开头。**不支持 WITH（CTE）**，写了会报 "Only SELECT queries are allowed"。
- 想做多步计算，用嵌套子查询：SELECT ... FROM (SELECT ...) t —— 这个能过。
- 例：算某图的连败这种「连续段」问题，要用嵌套子查询 + ROW_NUMBER() 分段
    （gaps-and-islands），不是把回合取回来自己数。
    具体骨架看上面的【知识图谱 · 本题相关子图】—— 有就照它写。

**拿不准列名就先查，不要猜**（这是最容易产生幻觉的地方 —— 猜出来的列名
一定报 Binder Error，然后你会在下一轮原样再猜一遍）：
    SELECT column_name FROM information_schema.columns WHERE table_name='rounds'
这张表叫什么也能查：SELECT table_name FROM information_schema.columns GROUP BY table_name
注意：不同表里"队伍"的列名不一样（有的是 team_name，有的是 winning_team_name），
写 SQL 前先确认目标表里的实际列名。

已知的工具缺陷（VLML 上游 bug，不是你调错了）：
- match_summary_report / match_analysis_report / match_economy_report / scouting_report
  **传 map_name 会崩溃**（报错 "Referenced table tgs not found"）。
  要按图分析就用 query_sql 自己加 WHERE map_name=... —— 这条永远有效。
"""

PLAN_SCHEMA = """只输出一个 JSON 对象：
{"thought": "一句话说明你打算怎么查",
 "calls": [{"tool": "工具名", "args": {"参数名": "参数值"}}]}
调用条数上限 = 本题评分点的条数（每个评分点一条查询就够）；不要输出解释文字。"""


# ---------------------------------------------------------------------------
# 工具分派表：TOOL_CATALOG 里写了 10 个，这里就必须真能调 10 个。
# 目录与能力不一致是假阳性的来源之一——模型照目录规划了，执行端却说"未知工具"。
# query_sql 是 MCP 对外名，execute_custom_sql 是 Python 实现名，两者都映射到同一个函数。
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 已知缺陷：VLML 的 match_summary_report / match_analysis_report 传 map_name 必崩
#
#   实测：BinderException: Referenced table "tgs" not found! (Candidate: "prs")
#   根因：tools/reports/match_analysis.py:172 把 map_filter 拼成 "AND tgs.map_name = ?"，
#         但同一个 filter 被复用到 tools/sql/team_economy_eco.sql —— 那条 SQL 的子查询
#         只有 prs/r/g 别名，没有 tgs。只在传 map_name 时触发。
#   这是 VLML 上游的 bug，与合成数据无关（不传 map_name 一切正常）。
#
# 处理方式：不改仓库源码，在分派层**显式报错**并引导改用 query_sql。
#   不静默降级成"忽略 map_name"——那样会拿到全图数据冒充分图数据，
#   比报错危险得多（事实会带错 subject）。
# ---------------------------------------------------------------------------
VLML_MAP_NAME_BUG = (
    "该工具传 map_name 会崩溃（VLML 上游 bug：team_economy_eco.sql 引用了不存在的别名 tgs）。"
    "要按图分析请改用 query_sql 自己过滤 map_name。"
)


async def _guarded_report(fn, args: dict[str, Any]):
    if args.get("map_name"):
        return {"error": VLML_MAP_NAME_BUG}
    return await fn(**args)


TOOL_REGISTRY: dict[str, Any] = {
    "match_summary_report": match_summary_report,
    "match_players_report": match_players_report,
    "match_rounds_report": match_rounds_report,
    "match_economy_report": match_economy_report,
    "match_analysis_report": match_analysis_report,
    "player_profile_report": player_profile_report,
    "scouting_report": scouting_report,
    "pattern_detection_report": pattern_detection_report,
    "query_sql": execute_custom_sql,
    "execute_custom_sql": execute_custom_sql,
    "get_database_info": get_database_info,
}

# 这几个报告工具共用同一个 map_filter 拼接逻辑，都会踩同一个 bug
MAP_NAME_BUGGY_TOOLS = {
    "match_summary_report", "match_analysis_report",
    "match_economy_report", "scouting_report",
}


_DB_SELF_DESC: str = ""


async def db_self_description() -> str:
    """照 VLML 自己的做法：把「有什么可用」在写 SQL **之前**交给调用方。

    出处 vlml/src/vlml/tools/db_query_tools.py:272-334 的 get_database_info()：
    它不校验输入，而是**自描述** —— 返回
      - available_tables：9 张表，每张带人类可读的一句描述
      - usage_tips：4 条用法提示
      - sample_recent_stats：真实样例（让调用方看到值长什么样）
      - recent_series
    这才是 VLML 解决"输入不标准"的主力：调用方在动手前就知道格式，
    而不是写完被打回来再猜。

    我之前的做法是反的 —— 靠报错后回灌（schema_probe 查列名、
    参数白名单报"只接受哪些"）。那是**事后补救**，一轮只能试一次。
    两者都要：这里负责事前，那些闸门负责事后。
    """
    global _DB_SELF_DESC
    if _DB_SELF_DESC:
        return _DB_SELF_DESC
    try:
        info = await get_database_info()
    except Exception as e:
        return f"（数据库自描述取不到：{type(e).__name__}: {e}）"
    if not isinstance(info, dict) or info.get("error"):
        return f"（数据库自描述取不到：{str((info or {}).get('error'))[:150]}）"

    tables = "\n".join(f"  - {t}" for t in (info.get("available_tables") or []))
    tips = "\n".join(f"  - {t}" for t in (info.get("usage_tips") or []))
    series = ", ".join(str(x) for x in (info.get("recent_series") or [])[:5])
    sample = (info.get("sample_recent_stats") or [])[:1]
    sample_txt = ""
    if sample:
        sample_txt = "\n样例（看清楚值长什么样）：\n  " + json.dumps(
            sample[0], ensure_ascii=False, default=str)[:300]

    _DB_SELF_DESC = (
        "【数据库自描述 · 由 VLML 自己给出，写 SQL 前先读这段】\n"
        f"可用表：\n{tables}\n\n"
        f"VLML 的用法提示：\n{tips}\n\n"
        f"现有 series：{series or '（无）'}"
        f"{sample_txt}\n\n"
        "拿不准列名就先查，不要猜：\n"
        "  SELECT column_name FROM information_schema.columns WHERE table_name='表名'\n"
        "  SELECT table_name FROM information_schema.columns GROUP BY table_name"
    )
    return _DB_SELF_DESC


def _coerce_value(v: Any) -> Any:
    """把模型给出的值归一到可比对形态。

    实测模型会把百分比写成 "50%"、把置信度写成 "Moderate" —— 内容是对的，
    但 `"50%"` 转不成 float，比对时就变成"值不可比"，等于白做。
    格式差异不该影响评分，所以在这里归一；**数值对不对仍由裁判判**。
    """
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        t = v.strip()
        if t.endswith("%"):                      # "50%" → 50.0
            try:
                return float(t[:-1].strip())
            except ValueError:
                return t
        try:
            return float(t)                      # "59" → 59.0
        except ValueError:
            return t.lower() if t.isascii() else t   # "Moderate" → "moderate"
    return v


def _blueprint_hint(skill: dict[str, Any]) -> str:
    """把技能蓝图的「标准编排序列」也带进 prompt —— 不止一句话。

    实测踩到的问题：只给一句自然语言经验，模型**读了但不改行为** ——
    注入 2 条经验，轨迹仍是单工具、覆盖率反而掉到 30%。原因是那句经验说的是
    "按什么维度拆分"，没说"要调哪几个工具"。

    伴学的能力模块投递的是整个 blueprint（缺陷 → 策略 → 具体动作），
    不是一句话概括（`adaptive_learning/cognitive_delivery.py`）。这里同理：
    把旁路上学到的标准编排序列一并给出去。
    """
    bp = skill.get("blueprint") or {}
    traj = [str(t) for t in (bp.get("trajectory") or [])
            if not str(t).startswith("<")]
    if not traj:
        return ""
    hint = f"｜标准编排：{' → '.join(traj)}"
    if bp.get("strategy"):
        hint += f"（策略：{bp['strategy']}）"
    return hint


def _code_block(skill: dict[str, Any], limit: int = 900) -> str:
    """技能里的**解法源码** —— 照原版 `programs` property 拼进 prompt 的是
    `entry['code']`，不是函数名也不是一句话描述
    （`voyager/agents/skill.py:55`）。

    为什么必须给源码：只给"先取 query_sql 拿总盘"这种话，模型读了但不改行为
    （实测注入 2 条经验、轨迹仍是单工具、覆盖率掉到 30%）。它缺的不是"方向"，
    是"这段 SQL 到底长什么样"—— 哪张表、怎么分层、取第几列。源码给了，
    它才知道该照着写什么。
    """
    code = str(skill.get("code") or "").strip()
    if not code:
        return ""
    src = code if len(code) <= limit else code[:limit].rstrip() + " …"
    indented = "\n".join("      " + ln for ln in src.splitlines())
    return "\n    解法源码：\n" + indented


# ---------------------------------------------------------------------------
# 知识图谱 = control_primitives 的等价物
# ---------------------------------------------------------------------------
# 原版 Voyager 为什么能写出能跑的 JS？不是因为模型会 Mineflayer，而是
# `render_system_message()` 把 **control_primitives 的源码** 拼进了 system
# message —— 模型看得到每个技能函数的实现，而不是只有函数名
# （voyager/agents/action.py:render_system_message）。
#
# MVE 的同构物：把「这个指标怎么算 / 读哪张表 / 用哪些列 / 别人常错在哪」
# 作为**本题相关子图**注入 prompt。之前这一块完全没有 —— 图谱只喂给了面板
# 可视化、取证提示和判错反馈，Voyager 规划时一行都看不到，于是只能猜列名
# （实测 corrode 第 2 轮把 fb_conv、round_number 当列写进 SELECT）。
#
# 与「求助」的分工：图谱是**常驻**上下文（像 control_primitives 一直在 system
# 里），不计入 used_hint、不打掌握度折扣；求助（VLML0 解释）才是伴学意义上
# 的"借助帮助"，要打 0.85 折。两者不能混。
# ---------------------------------------------------------------------------

_GRAPH: Any = None          # None=未加载 / False=不可用 / KnowledgeGraph=已加载

# SQL 结构骨架（gaps-and-islands 这类）给不给 —— 默认给，可用环境变量关掉做 A/B
GRAPH_WITH_RECIPE = os.environ.get("MVE_GRAPH_RECIPE", "1") != "0"

# 图谱注入总开关：MVE_GRAPH=0 时回到"没有 control_primitives"的基线。
# 存在的唯一理由是 A/B —— 想知道图谱到底有没有用，必须能把它关掉跑同一道题，
# 否则"加了图谱之后好了"永远无法证伪（也可能是别的改动带来的）。
GRAPH_ENABLED = os.environ.get("MVE_GRAPH", "1") != "0"
# 可临时关掉（考核用）。为什么不用环境变量：环境变量是**进程级**的，
# 而"练习一轮 → 考核一轮"要在同一个进程里来回切。考核 = 撤掉支架，
# 看模型靠自己积累的技能库还能不能做对 —— 这才是学习曲线该测的东西。
GRAPH_STATE: dict[str, bool] = {"on": GRAPH_ENABLED}

# 图谱里两层的单独开关 —— 存在的唯一理由是 A/B。
# 洞察层（"取工具的哪一段"）和数据分层（表在哪一层、上游是谁）是本轮新加的，
# 必须能单独关掉才能测出它们到底有没有被模型用上，否则只能靠"感觉有用"。
GRAPH_SECTION = os.environ.get("MVE_GRAPH_SECTION", "1") != "0"
GRAPH_STAGE = os.environ.get("MVE_GRAPH_STAGE", "1") != "0"
GRAPH_INSIGHT = os.environ.get("MVE_GRAPH_INSIGHT", "1") != "0"

# 种子层兜底开关：老路径（按 topic_id 取维度）拿不到东西时，用**题干**
# 去匹配独立于题目的 212 个事实。关掉它就能 A/B 出"图谱是否真的对所有题生效"。
GRAPH_SEED_FALLBACK = os.environ.get("MVE_GRAPH_SEED", "1") != "0"

# 代码化取证总开关：MVE_ACTION_CODE=1 时 Voyager 产出**代码**而不是 plan JSON，
# 值由解释器按路径取出 —— 不经过 LLM 誊抄。
# 存在的理由：实测"翻错键"的病根就在誊抄环节。源码里把路径
# `key_metrics.team.consistency.kd` 写得清清楚楚，模型照样交出 kast 的值 1.0
# （kast=109/109）—— 它是在工具返回的巨大 JSON 里**用眼睛翻**的。
# 交给解释器执行路径，就翻不错。
#
# 2026-10-06 默认改为 1（原为 0）。对照实验（同一技能库 / 同一时间 / 同一道题）：
#     代码路径 100%   —— kd_ratio 交出 1.24
#     默认路径 50%   —— kd_ratio 交出 1.0（把 kast 的 num/denom 套到标量上）
# 技能库里已经写明了正确路径，两条路的**输入完全相同**，差的只是"执行 vs 阅读"。
# 这正是原版 Voyager 的本质：技能被 exec，不是被 read。
# 默认打开不影响 SQL 类题 —— `_is_tool_path_task` 按口径源分流，
# answer_spec.sql 仍走原路径（那条有图谱骨架与分层断言，corrode 曾因全走代码掉到 0%）。
ACTION_CODE = os.environ.get("MVE_ACTION_CODE", "1") != "0"

# 只关**断言**（硬校验），保留 prompt 提示 —— A/B 时必须能把两者分开：
# 断言一旦生效就把失败挡住了，提示层的边际效果会被完全掩盖
# （实测 pistol 开不开 section 都是 100%，因为参数断言已经兜住了）。
ASSERT_ENABLED = os.environ.get("MVE_ASSERT", "1") != "0"


def _load_graph() -> Any:
    global _GRAPH
    if _GRAPH is None:
        try:
            import knowledge_graph
            g = knowledge_graph.KnowledgeGraph.load()
            _GRAPH = g if g.nodes else False
        except Exception:
            _GRAPH = False
    return _GRAPH or None


def _apply_auto_fixes(plan: dict[str, Any], task: Any,
                      errors: list[str]) -> None:
    """校验通过后，把能机械修好的 SQL 直接修掉。"""
    for c in plan.get("calls") or []:
        if not isinstance(c, dict):
            continue
        if TOOL_REGISTRY.get(str(c.get("tool"))) is not execute_custom_sql:
            continue
        args = dict(c.get("args") or {})
        key = "sql_query" if "sql_query" in args else "sql"
        sql = str(args.get(key) or "")
        fixed, note = auto_fix_layering(sql, task)
        if note and fixed != sql:
            args[key] = fixed
            c["args"] = args
            errors.append(f"自动修正 SQL：{note}")


def _check_tool_params(tool: str, args: dict[str, Any]) -> str:
    """「至少要传一个」这类参数约束，在调用发出之前拦下。

    为什么需要：实测 pistol_eco_pattern 那道题，模型照图谱调对了工具
    `pattern_detection_report`，但一个参数都没传 —— 工具返回 scope=null，
    四个评分点全空，覆盖率 0%。它"调对了工具"却什么都拿不到。
    提示（param_hint）写进 prompt 不够，照分层断言的先例做成硬校验。
    """
    if not (GRAPH_STATE["on"] and ASSERT_ENABLED):
        return ""
    g = _load_graph()
    if g is None:
        return ""
    node = g.nodes.get(f"tool:{tool}")
    if node is None:
        return ""
    need = list(node.detail.get("needs_any_of") or [])
    if not need:
        return ""
    if any(str(k) in (args or {}) and args.get(k) not in (None, "") for k in need):
        return ""
    if need:
        return (f"{tool} 至少要传 {' 或 '.join(need)} 之一才会有有效结果 —— "
                f"不传返回空（实测 scope=null），这个维度就取不到值了")

    # 参数**类型**：语法上不报错、语义上全错的那类
    # （series_ids 传 '2843069' 会被逐字符拆开 → rounds=0）
    for key, want in (node.detail.get("param_types") or {}).items():
        if key not in (args or {}):
            continue
        val = args.get(key)
        if want == "list" and isinstance(val, str):
            return (f"{tool} 的 {key} 要传**数组** [\"{val}\"]，不能传字符串 —— "
                    f"传字符串会被逐字符拆开，结果变成空集（实测 rounds=0）")
    return ""


def _inner_where(sql: str) -> str:
    """最内层 WHERE 子句（第一个 WHERE 到下一个右括号之间）。

    分段计算（gaps-and-islands）里，"哪一层过滤"就是口径本身：
    内层先对**全部回合**编号，外层才筛队伍。把队伍过滤提前到内层，
    剩下的就只有该队输的回合，编号自然连续，整段被当成一块
    （实测算出 13 而真值是 8）。
    """
    i = (sql or "").upper().find(" WHERE ")
    if i < 0:
        return ""
    rest = sql[i:]
    j = rest.find(")")
    return rest[:j] if j > 0 else rest


def _check_group_order(sql: str, task: Any) -> tuple[list[str], str]:
    """GROUP BY 之后必须确定行序 —— 否则「按第 N 行取数」会把值配错分组。

    实测（`map_rounds_split`）：模型写了一条 `GROUP BY map_name` 查三行，
    然后按行号取数，交上 Corrode/Haven/Lotus = 21/24/14，真值是 14/21/24
    —— **数字全对，只是配错了地图**。这类错误不会报错、不会取空，
    只有裁判比对才能发现，而那时一整轮已经浪费掉。

    最稳的写法是**每个分组一条 SQL**（裁判自己的 answer_spec 就是这么写的：
    `WHERE map_name='Corrode'`），所以驳回时把那条形同口径的骨架一起给出去。
    """
    if not (GRAPH_STATE["on"] and ASSERT_ENABLED):
        return [], ""
    s = (sql or "").upper()
    if "GROUP BY" not in s or "ORDER BY" in s:
        return [], ""
    recipe = ""
    for p in (getattr(task, "rubric", None) or []):
        subj = getattr(p, "subject", None) or {}
        spec = getattr(p, "answer_spec", None)
        if subj.get("map") and getattr(spec, "sql", ""):
            try:
                from knowledge_graph import _sql_recipe
                recipe = _sql_recipe(str(spec.sql))[:400]
            except Exception:
                recipe = ""
            break
    return ([
        "你用了 GROUP BY 但**没有 ORDER BY** —— 结果行的顺序不确定，"
        "按第 N 行取数会把数字配错分组（实测 Corrode/Haven/Lotus 回合数"
        "整体错位：交 21/24/14，真值 14/21/24 —— 数都是对的，只是配错了图）。"
        "改成**每个分组一条 SQL**（WHERE map_name='?'）最稳；"
        "坚持用 GROUP BY 就必须显式 ORDER BY 分组键。",
    ], recipe)


def _check_sql_layering(sql: str, task: Any) -> tuple[list[str], str]:
    """照 process_ai_message 的 AST 断言：不合法就驳回，而不是"提醒一下"。

    原版用 babel 断言 JS 代码结构（必须 async、参数必须叫 bot）；
    这里同构地断言 SQL 的**分层结构**，判据取自知识图谱的 recipe。

    返回 (所有问题, 正确骨架)。**所有问题一起返回**是实测逼出来的：
    一次只说一条时，模型改了 WHERE 就忘了 GROUP BY，下一轮又回到 WHERE，
    三次重试打转 → 整轮作废（覆盖率 0% 的波动就是这么来的）。
    """
    if not (GRAPH_STATE["on"] and ASSERT_ENABLED):
        return [], ""
    if not sql or "PARTITION BY" not in sql.upper():
        return [], ""
    g = _load_graph()
    if g is None:
        return [], ""
    problems: list[str] = []
    recipe = ""
    try:
        for dim in g.dims_of(str(getattr(task, "topic_id", "") or "")):
            rule = g.layering_rule(dim)
            if not rule:
                continue
            recipe = recipe or str(rule.get("note") or "")
            inner = _inner_where(sql)
            for col in rule.get("outer_only") or []:
                if re.search(r"\b" + re.escape(col) + r"\b", inner or ""):
                    problems.append(
                        f"{col} 写在了**最内层** WHERE —— 编号前就筛掉队伍，"
                        f"剩下的回合编号必然连续，整段会被当成一块；"
                        f"{col} 要放到 `) t WHERE` 之后的外层，"
                        f"内层只留 series_id + map_name")
            # 分组不能丢：实测拦下 WHERE 之后它顺手把 GROUP BY 删了，
            # 结果所有输的回合又合成一整段，值还是不对。
            ids = rule.get("group_by_ids") or []
            if ids:
                m = re.search(r"GROUP\s+BY\s+(.+?)(?:\)\s*\w+\s+ORDER|\)\s*\w+$|"
                              r"\bORDER\b|$)", sql, re.IGNORECASE | re.DOTALL)
                gsql = (m.group(1) if m else "")
                if not gsql or not all(
                        re.search(r"\b" + re.escape(i) + r"\b", gsql) for i in ids):
                    problems.append(
                        f"少了分组 `GROUP BY ({rule.get('group_by_expr') or ', '.join(ids)})`"
                        f" —— 不按 (编号差) 分组就只会得到一整段，"
                        f"那正是你算出「全场一共输了多少回合」的原因")
            if problems:
                break
    except Exception:
        return [], ""
    return problems, recipe


def auto_fix_layering(sql: str, task: Any) -> tuple[str, str]:
    """能机械修好的就**直接修**，不要指望模型自己改。

    为什么必须落到代码里：驳回信息写得很清楚（"把 losing_team_name 移到
    `) t WHERE` 之后的外层"），模型连着三次原样重提交 —— 它认为自己的写法
    是对的。重试是靠不住的，能自动修的变换就不要交给它。

    只做一种变换：**外层已有该条件时，删掉内层多余的那一份**。
    不改语义（过滤条件一个不少，只是挪层），也就不存在"把 SQL 改坏"的风险。
    """
    if not (GRAPH_STATE["on"] and ASSERT_ENABLED):
        return sql, ""
    if not sql or "PARTITION BY" not in sql.upper():
        return sql, ""
    g = _load_graph()
    if g is None:
        return sql, ""
    try:
        for dim in g.dims_of(str(getattr(task, "topic_id", "") or "")):
            rule = g.layering_rule(dim)
            if not rule:
                continue
            fixed = sql
            notes: list[str] = []
            for col in rule.get("outer_only") or []:
                i = fixed.upper().find(" WHERE ")
                if i < 0:
                    continue
                j = fixed.find(")", i)
                if j < 0:
                    continue
                inner, tail = fixed[i:j], fixed[j:]
                if not re.search(r"\b" + re.escape(col) + r"\b", inner):
                    continue
                # 外层必须已经有同一条件，否则删了就漏过滤 —— 不修
                if not re.search(r"\b" + re.escape(col) + r"\b", tail):
                    continue
                new_inner = re.sub(
                    r"\s+AND\s+" + re.escape(col)
                    + r"\s*(?:=|<>|!=|IS|IN|LIKE)\s*(?:'[^']*'|\"[^\"]*\"|[\w.()]+)",
                    "", inner, count=1, flags=re.IGNORECASE)
                if new_inner == inner:
                    continue
                fixed = fixed[:i] + new_inner + tail
                notes.append(f"把内层的 {col} 过滤挪到外层（分段必须先编号再筛）")
            if notes:
                return fixed, "；".join(notes)
    except Exception:
        return sql, ""
    return sql, ""


def graph_context(task: Any, *, with_recipe: bool | None = None,
                  mode: str = "plan") -> str:
    """本题相关子图，渲染成给模型的文本；图谱不可用时返回空串（不阻断主流程）。

    两条路，按顺序试：
    1. `render_for_prompt(topic_id)` —— 出过的题，走 rubric 派生的维度，
       信息最全（口径、易混、骨架、参数陷阱）。
    2. `render_for_query(题干)` —— **任意题**，用种子层（212 个独立于题目的
       事实）按题干文本匹配。伴学就是靠这一招让图谱对所有题目生效
       （`build_knowledge_guidance_payload` → `match_topics(query)`）：
       它的 82 个知识点种子先于题目存在，题目只负责匹配焦点。
       没有这条兜底时，MVE 的图谱退化成"出过那几道题的备忘"——
       新题的图谱块是空的。
    """
    if not GRAPH_STATE["on"]:
        return ""
    g = _load_graph()
    if g is None:
        return ""
    topic_id = str(getattr(task, "topic_id", "") or "")
    if not topic_id:
        return ""
    try:
        s = g.render_for_prompt(
            topic_id,
            with_recipe=GRAPH_WITH_RECIPE if with_recipe is None else with_recipe,
            with_section=GRAPH_SECTION,
            with_stage=GRAPH_STAGE,
            with_insight=GRAPH_INSIGHT,
        )
    except Exception as exc:  # pragma: no cover
        # 不能静默吞：实测这里吞过一次 AttributeError，表现是图谱"神秘消失"
        # （prompt 里没有图谱块，但没有任何报错），排查花了很久。
        print(f"[graph] render_for_prompt 失败，本题不带图谱: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        s = ""
    if s or not GRAPH_SEED_FALLBACK:
        return s
    # 出过的题也可能在这里：topic_id 有了但图谱里没有它的维度（新加的题、
    # 或者 rubric 改过维度名）。此时用题干走种子层。
    question = str(getattr(task, "question", "") or "")
    if not question:
        return ""
    try:
        return g.render_for_query(question, mode=mode, limit=3)
    except Exception as exc:  # pragma: no cover
        print(f"[graph] render_for_query 失败: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return ""


def _tools_used_before(topic_id: str) -> set[str]:
    """这道题在**历史轮次**（跨进程）里真正调过的工具。

    为什么必须跨进程：loop 模式每题起一个新 run_mve（新 Voyager 实例），
    只看 `self.trajectory` / `self.attempted` 就只看得到本进程这几轮。
    实测后果：series_totals 连跑 24 轮卡在 50%，反馈每轮点名 query_sql、
    模型每轮只调 match_summary_report → "没试过"永远成立 →
    「先自己试」的纪律让它**一次求助都没发生**（53 轮日志里 series_totals
    没有一条求助记录）。
    """
    if not topic_id:
        return set()
    out: set[str] = set()
    try:
        for r in run_log.load_all():
            if str(r.get("topic_id") or "") != topic_id:
                continue
            for t in (r.get("trajectory") or []):
                # "<deterministic-sql>" 是内部标记，不是真工具名，不能算"试过"
                if isinstance(t, str) and t and not t.startswith("<"):
                    out.add(t)
    except Exception:
        return set()
    return out


class LLMVoyager:
    """真正的 Voyager：模型自己编排工具、自己反思、把教训存进技能库。

    「学会编排」在代码里体现为：self.memory 里的教训会进入下一轮的 prompt，
    从而改变它的调用计划。memory 是空的 → 它大概率只做整体概览；
    有了教训 → 它应该学会下钻。
    """

    def __init__(self, model: str = "", api_key: str = "", blind: bool = False,
                 hint_level: str = "") -> None:
        # 技能库跨运行累积 —— 不累积就看不出进步曲线
        self.memory: list[str] = skill_store.load()
        self.trajectory: list[dict[str, Any]] = []
        self.last_thought = ""
        self.blind = blind                 # True = 规划时不给 dimension/subject 提示
        # 支架档位（出题器按掌握度算出来，见 difficulty.py）：
        #   full    给 ★ 声明口径路径 + critic 回灌真实候选路径
        #   partial 只给聚焦后的结构文档，不给 ★、不给候选
        #   none    不聚焦、不给 ★、不给候选（全放开）
        # 空串 = 未指定 → 按 full 处理（保持旧行为，不回退）
        self.hint_level = str(hint_level or "")
        self.failed_dims: list[str] = []   # 照 fork glm_curator._context()：失败维度喂给下一题
        # 点名的**评分点**（比维度更细）：同一维度可能挂多个评分点，
        # 只说维度名模型会以为自己已经交过。
        self.failed_points: list[str] = []
        self.last_critique = ""
        self.errors: list[str] = []        # 工具执行/计划解析的错误，必须上面板

        # ---- 防幻觉：照 ActionAgent.render_human_message ----
        # 原仓库把「上一轮的 code / Execution error / Critique」拼成 observation 回灌，
        # 缺哪项就显式写 "No code in the first round" / "No error" / "None"。
        # 不给模型留"我上一轮到底干了什么"的想象空间 —— 想象空间就是幻觉的入口。
        self.last_round: dict[str, Any] = {
            "calls": [],     # 上一轮真正发出去的工具调用
            "errors": [],    # 上一轮的执行错误
            "critique": "",  # 上一轮的自我反思
        }
        # ---- 防幻觉：事实溯源 ----
        # 模型抽出来的数值必须在工具返回里真的出现过，否则就是编的。
        self.hallucinations: list[str] = []
        # 本轮被注入 prompt 的技能 key（用于统计技能有效性）
        self._injected_keys: list[str] = []
        # SQL 报错后探到的表结构 —— 下一轮 prompt 的「你现在有什么」
        self.schema_hints: list[str] = []
        # 每一次**尝试过**的调用（含失败的 + SQL 原文），只用于回灌，不算证据
        self.attempted: list[dict[str, Any]] = []
        # VLML 自描述（表清单 + 用法提示 + 样例），写 SQL 前给模型看
        self.db_info: str = ""

        # ---- 求助（旁路解释）----
        # used_hint：本轮作答时用没用 VLML0 的解释。伴学的同构项是"这次做对
        # 靠没靠提示"（mastery_v2.py:109）—— 只有**求助**才算，注入技能库经验
        # 不算（那是正常做题的一部分）。之前把它映射成"注入过技能"是错的。
        self.used_hint: bool = False
        self.help_pack: dict[str, Any] | None = None
        self.last_help_decision: dict[str, Any] | None = None
        self.last_feedback: dict[str, Any] | None = None

    # ---- 观察回灌：照 voyager/agents/action.py:render_human_message ----
    # ------------------------------------------------------------------
    # 求助决策：判错之后，Voyager 自己决定要不要去旁路（VLML0 解释接口）
    # ------------------------------------------------------------------
    def decide_help(self, *, task: Any = None,
                    fb: dict[str, Any] | None = None) -> dict[str, Any]:
        """判错之后决定要不要求助。**不是一判错就求助。**

        反馈里已经带了"这个维度由哪个工具产出"（知识图谱给的声明），
        能自己解决的先自己解决 —— 照着调一次，取不到再说。
        只有下面三种情况才值得花一次解释：
          (a) 已经按提示调了那个工具，仍然取不到；
          (b) 反馈没说清该调什么（图谱里没有这条边）；
          (c) 同一个缺口连续出现，自己试的方向已被证明无效。

        这是伴学"先给提示再给讲解"的同构项：提示（图谱说的工具名）先给，
        讲解（标准解法）后给，不能一上来就讲。
        """
        fb = fb or getattr(self, "last_feedback", None) or {}
        if not fb or not fb.get("error_type"):
            return {"need_help": False, "reason": "没判错，不需要求助"}

        my_plan = [str(t.get("tool")) for t in self.trajectory if t.get("tool")]
        tried = set(my_plan) | {str(t.get("tool")) for t in self.attempted
                                if t.get("tool")}
        # 反馈里点名的工具（图谱给的），看是否已经试过
        suggested: set[str] = set()
        for m in fb.get("missing_detail") or []:
            suggested |= set(m.get("producers") or [])
        gap = sorted(suggested - tried)

        # 「点名但没试过」要分两类，之前把它们混为一谈，等于把求助永久堵死：
        #   still_worth —— 从没试过（本进程 + 历史都没有）→ 值得自己去试
        #   ineffective —— 历史上试过、但那次也没做出来 → 再试一遍大概率还是一样
        # 实测：series_totals 连跑 24 轮卡在 50%，反馈每轮都点名 query_sql，
        #       而模型每轮都只调 match_summary_report → gap 永远非空 →
        #       「先自己试」的纪律让它一次求助都没发生。
        #       （历史记录来自 run_log：loop 模式每题是新进程，光看本进程
        #         的轨迹永远看不到"以前试过"。）
        hist = _tools_used_before(str(getattr(task, "topic_id", "") or ""))
        ineffective = sorted(set(gap) & hist)
        still_worth = sorted(set(gap) - hist)

        # 硬规则先于模型判断：还有"从没试过"的工具时，一律不求助。
        # 实测踩过：把这句交给模型自由发挥，它会说"我已经尝试了所有点名的工具"
        # （其实一个都没试），然后要么错误求助、要么给出自相矛盾的理由。
        # "先自己试"是纪律不该是选项 —— 有没试过的工具就必须先去试。
        if still_worth:
            self.last_help_decision = {
                "need_help": False,
                "reason": f"还有点名但从没试过的工具（{'、'.join(still_worth)}），先自己试",
                "untried_suggested": still_worth,
                "ineffective_before": ineffective,
                "by": "rule",
            }
            return self.last_help_decision

        # 走到这里说明：点名的工具本轮/本进程都试过了，
        # 或者「没试过」的那些历史上已经证明无效 —— 这时才值得花一次解释。

        prompt = f"""你刚做完这道题，裁判判定如下：

判据：{fb.get('feedback', '')}
下一步：{fb.get('next_action', '')}
我这次实际调过的工具：{'、'.join(sorted(tried)) or '（无）'}
反馈点名但**我还没试过**的工具：{'、'.join(gap) or '（无）'}
其中历史上试过、但那次也没做出来（再试一遍大概率一样）：{'、'.join(ineffective) or '（无）'}
缺的维度：{', '.join(fb.get('missing_points') or []) or '（无）'}

现在要决定：**要不要去求助**（调 VLML0 的解释接口，看标准解法怎么编排）。

判断标准（这是关键，别一判错就求助）：
- 如果还有"点名了、且我从没试过"的工具 → **不求助**，先照着调一次。
  自己能解决的必须自己解决，求助一次就要在掌握度上记一笔（按"借助帮助"计分，打 0.85 折）。
- 只有在这些情况下才求助：
  (a) 点名的工具我都试过了，仍然取不到；
  (b) 反馈根本没说清该调什么（没有工具名可用）；
  (c) 没试过的那几个，历史上已经证明试了也没用（本题连续多轮停在同一个覆盖率）；
  (d) 同一个维度的缺口已经连续出现，我自己试的方向被证明无效。

输出 JSON：{{"need_help": true 或 false, "reason": "一句话理由"}}"""

        try:
            out = chat_json(
                [
                    {"role": "system",
                     "content": "你是会先自己尝试、实在不行才求助的分析 agent。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
        except Exception as e:
            return {"need_help": False, "reason": f"决策调用失败：{type(e).__name__}"}

        need = bool(out.get("need_help"))
        reason = str(out.get("reason") or "").strip()
        self.last_help_decision = {"need_help": need, "reason": reason,
                                   "untried_suggested": gap, "by": "model"}
        return self.last_help_decision

    def accept_help(self, pack: dict[str, Any]) -> None:
        """把学习环节拿到的讲解存下来 —— 下一轮 prompt 会带**做法**部分。

        讲解是四段（照伴学 _solution_structure.py:173）：
            题目解析 / 解题过程 / 答案 / 举一反三
        其中"答案"只在**学习那一轮**看过；回灌进下一轮 prompt 的是做法三样
        （编排 + 各工具负责的维度 + 解题过程与口径），数值不回灌。

        为什么要给答案：目的是学会，不是考过（伴学讲解里答案就是可见的）。
        为什么重做时不给：值还得它自己调工具取，否则掌握度是假的。
        """
        self.help_pack = pack
        self.used_hint = True
        study = pack.get("study") or {}
        self._log_skill("help_received", pack.get("topic_id", ""),
                        str(study.get("process") or pack.get("explain")
                            or study.get("transfer") or "")[:160])

    def _render_observation(self) -> str:
        """把上一轮的调用（含 SQL 原文）/ 错误 / 自我反思拼成 observation。

        原仓库的写法（action.py:201-236）：
            Code from the last round: ...   （首轮写 "No code in the first round"）
            Execution error: ...            （无错写 "No error"）
            Critique: ...                   （无则 "None"）
        两个要点，缺一个都会原地打转：
          (a) 回灌的是**代码全文**，不是函数名 —— 看不到自己写了什么就只能重写一遍，
              实测连犯三轮同一个 team_name 错；
          (b) **缺失时必须显式写 None** —— 空字符串会被模型当成"上一轮挺顺利"，
              然后它顺着这个假设继续编。
        """
        calls = self.last_round.get("calls") or []
        errs = self.last_round.get("errors") or []
        # Critique 必须取**当前**的 self.last_critique，不能取 last_round 里的快照：
        # last_round 是上一轮 run() 末尾拍下的，那一刻 learn() 还没跑，critique
        # 恒为空字符串 —— 于是 `_render_observation` 每次都渲染成 "None"。
        # 实测取证（MVE_DEBUG_PROMPT=1 落盘的 prompt）：
        #   「上一轮你的自我反思 Critique：None」
        # 这正是 fb_conversion_analysis 连跑 23 轮停在 80%、Haven 图首血转换率
        # 一次都没取到的直接原因 —— 配方写进反馈了，但从未进过下一轮的眼睛。
        crit = (getattr(self, "last_critique", "") or "").strip() \
            or (self.last_round.get("critique") or "").strip()

        if calls:
            call_lines = []
            for i, c in enumerate(calls, 1):
                arg = (c.get("args") or {})
                sql = str(arg.get("sql_query") or arg.get("sql") or "")
                shown = f" {sql}" if sql else f" {json.dumps(arg, ensure_ascii=False)[:120]}"
                tail = f"→ 失败：{c['error'][:200]}" if c.get("error") else "→ 成功"
                call_lines.append(f"  {i}. {c.get('tool')}{shown}\n     {tail}")
            calls_txt = "\n" + "\n".join(call_lines)
        else:
            calls_txt = " None（这是第一轮）"

        lines = [
            "上一轮你实际发出的调用（失败的也列出，附 SQL 原文）：" + calls_txt,
            "上一轮的执行错误："
            + ("；".join(str(e)[:220] for e in errs) if errs else "None"),
            "上一轮你的自我反思 Critique："
            + (crit if crit else "None"),
            "上一轮裁判判定的缺口维度："
            + ("、".join(self.failed_dims) if self.failed_dims else "None"),
            # 维度名不够 —— map_fb_conv 在本题挂着 Corrode 与 Haven 两个评分点，
            # 只说"缺 map_fb_conv"，模型会以为自己已经交过（它确实交了 Corrode）。
            # 必须**点名评分点**，并说明每个都要单独一条查询。
            "上一轮没覆盖到的评分点（每一个都要单独出一条查询，"
            "不要把两条并成一条）："
            + ("、".join(self.failed_points) if getattr(self, "failed_points", None)
               else "None"),
            # 光说"你错了"没用，要说"你有什么可用"—— 否则它只能再猜一次
            "上一轮写错的表，实际可用的列："
            + ("；".join(self.last_round.get("schema_hints") or [])[:900]
               if self.last_round.get("schema_hints") else "None"),
        ]
        # 学过的标准解法（学习环节给的，讲透了；但**数值不回灌** ——
        # 值还得它自己调工具取，抄不进去，溯源校验会拦）。
        pack = getattr(self, "help_pack", None) or {}
        if pack:
            mapping = "；".join(
                f"{t} → {', '.join(d)}" for t, d in
                (pack.get("dims_by_tool") or {}).items())
            study = pack.get("study") or {}
            lines.append(
                "【我学过的标准解法】（学习环节已讲透，这里只带做法，数值自己取）"
                + f"\n  编排：{' → '.join(pack.get('plan') or []) or '（无）'}"
                + f"\n  各工具负责的维度：{mapping or '（无）'}"
                + (f"\n  题目解析：{study['analysis'][:220]}" if study.get("analysis") else "")
                + (f"\n  解题过程与口径：{study['process'][:400]}" if study.get("process") else "")
                + (f"\n  举一反三：{study['transfer'][:220]}" if study.get("transfer") else "")
                + "\n  （答案在上一轮的学习记录里看过；本轮必须自己调工具把值取出来，"
                  "直接填数会被溯源校验拦下。）")
        return "\n".join(lines)

    # 注：这里原本有个 `_feedback_block()`，但从未被任何地方调用 ——
    # "打分回传"实际上由 `_render_observation` 里的两行承担
    # （「上一轮裁判判定的缺口维度」+「上一轮没覆盖到的评分点」）。
    # 死代码比没有代码更危险：看起来反馈链路是通的，其实没接。已删。

    # ---- 计划 ----
    def _memory_block(self, task: Any) -> str:
        """技能库注入：照 SkillManager.retrieve_skills 只取 retrieval_top_k 条。

        原仓库（skill.py:76-96）：
            k = min(self.vectordb._collection.count(), retrieval_top_k)   # =5
            docs_and_scores = self.vectordb.similarity_search_with_score(query, k=k)
        技能库再大，进 prompt 的永远只有 5 条 —— "库会涨、prompt 不涨"就靠这一句。

        之前是全量塞进去：连跑三轮同题攒出十几条几乎一样的经验，
        它们一起挤在 prompt 里，反而把真正有用的那条淹掉了。
        """
        topic = getattr(task, "topic_id", "") if task else ""
        query = getattr(task, "question", "") if task else ""
        top = skill_store.retrieve(topic, query, top_k=skill_store.RETRIEVAL_TOP_K)
        if not top:
            self._injected_keys = []
            return "（暂无经验）"
        self._injected_keys = [s["key"] for s in top]
        skill_store.mark_used(self._injected_keys)

        mine = [s for s in top if s.get("topic") == topic]
        others = [s for s in top if s.get("topic") != topic]
        out = ""
        if mine:
            out += "本题的经验：\n" + "\n".join(
                f"- {s['text']}" + (f"（已复用 {s['hits']} 次）" if s.get("hits") else "")
                + _blueprint_hint(s)
                + _code_block(s)
                for s in mine
            )
        if others:
            out += ("\n\n其它题目的经验（仅参考，注意是否适用本题）：\n"
                    + "\n".join(f"- {s['text']}" + _blueprint_hint(s)
                                 + _code_block(s) for s in others))
        return out or "（暂无经验）"

    # ---- 计划校验：照 voyager/agents/action.py:process_ai_message ----
    # 原仓库对模型返回的代码做 AST 断言（必须是 async 函数、参数必须叫 bot），
    # 不合法就 retry=3。这里同构：工具名必须在白名单里、参数必须是 dict、
    # SQL 必须是 SELECT。模型最爱编的就是「一个不存在的工具名」——
    # 实测它甚至把内部标记 <deterministic-sql> 当成真工具写了进去。
    def _allowed_params(self, tool: str) -> set[str] | None:
        """工具真正接受的参数名（None = 查不到签名就不校验）。"""
        fn = TOOL_REGISTRY.get(tool)
        if fn is None:
            return None
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return None
        allowed = {
            p.name for p in sig.parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          inspect.Parameter.KEYWORD_ONLY)
        }
        # query_sql 在 MCP 侧叫 sql_query，Python 侧参数名是 sql —— 两个都接
        if tool in ("query_sql", "execute_custom_sql"):
            allowed |= {"sql_query", "sql"}
        return allowed

    def _validate_plan(self, plan: Any, task: Any = None) -> str:
        if not isinstance(plan, dict):
            return "返回的不是 JSON 对象"
        calls = plan.get("calls")
        if not isinstance(calls, list) or not calls:
            return "calls 为空或不是数组"
        # 上限跟着**评分点数**走，不是写死的 3。
        # 实测踩过：写死 3 的时候，`fb_conversion_analysis`（4 个评分点、
        # 每个按图一条查询）和 `map_rounds_split`（5 个）**必然漏条** ——
        # 模型写 4 条就被闸拦下、3 次重试全废在"条数超了"上，最后降级放行 3 条，
        # 于是每张图一条这种最简单的写法根本用不了。
        # 上限是"每个评分点一条查询"，不是"三次机会"。
        cap = max(3, len(getattr(task, "rubric", None) or []))
        if len(calls) > cap:
            return f"calls 超过 {cap} 个（{len(calls)}）—— 本轮最多 {cap} 次调用"
        for i, c in enumerate(calls):
            if not isinstance(c, dict):
                return f"calls[{i}] 不是对象"
            tool = str(c.get("tool") or "").strip()
            if not tool:
                return f"calls[{i}] 缺 tool"
            if tool.startswith("<"):
                return f"calls[{i}] 工具名 {tool} 是内部标记，不是真工具"
            if tool not in TOOL_REGISTRY:
                # 只说"不在清单里"，它会换个名字再猜一次。要顺带说破
                # **它把什么当成了工具名**（实测：图谱给了表名，它就把表名
                # 填进 calls[].tool，连撞三次才停）。
                why = ""
                g = _load_graph()
                if g is not None:
                    if f"table:{tool}" in g.nodes:
                        why = (f" —— {tool} 是**表名**，要用 query_sql 在 SQL 里 "
                               f"FROM 它，不能当工具名")
                    elif f"dim:{tool}" in g.nodes:
                        why = f" —— {tool} 是要填的**维度名**，不是工具"
                    elif f"tool:{tool}" in g.nodes:
                        why = f" —— {tool} 在图谱里但不在本进程的工具注册表"
                return f"calls[{i}] 工具名 {tool} 不在工具清单里{why}"
            args = c.get("args")
            if args is not None and not isinstance(args, dict):
                return f"calls[{i}] args 必须是对象"
            # 参数名白名单：模型爱自造参数（实测写过 dimensions），也爱把
            # team_name 写成 team。等到执行时才 TypeError 就晚了一轮 ——
            # 这里就要驳回，并把「到底接受哪些参数」一并告诉它，
            # 否则它只能再猜一次。
            if isinstance(args, dict) and args:
                allowed = self._allowed_params(tool)
                if allowed:
                    bad = [k for k in args if k not in allowed]
                    if bad:
                        return (f"calls[{i}] {tool} 不接受参数 {', '.join(bad)}；"
                                f"它只接受：{', '.join(sorted(allowed))}")
            # 「至少要传一个」的参数约束（判据来自图谱，不硬编码）。
            # 注意必须在 `args` 判空的 if **外面** —— 一个参数都不传正是
            # 最该被拦下的情况，放进里面就永远轮不到检查。
            missing_param = _check_tool_params(
                tool, args if isinstance(args, dict) else {})
            if missing_param:
                return f"calls[{i}] {missing_param}"
            if TOOL_REGISTRY.get(tool) is execute_custom_sql:
                sql = str((args or {}).get("sql_query")
                          or (args or {}).get("sql") or "").strip()
                if not sql:
                    return f"calls[{i}] query_sql 缺 sql_query"
                if not sql.lower().lstrip().startswith("select"):
                    return f"calls[{i}] query_sql 只允许 SELECT"
                # 口径的**结构**断言（照 process_ai_message 的 AST 断言）：
                # 分段计算里过滤放在哪一层就是口径本身，写错层要在这里拦下，
                # 而不是等裁判说"值不对"—— 那时已经浪费了一整轮。
                # 所有问题**一起**说，并附上正确骨架：一次只说一条会让它在
                # 两个错之间来回打转，三次重试用完就整轮作废。
                bad, recipe = _check_sql_layering(sql, task)
                if not bad:
                    # 分层没问题再看「分组顺序」—— 两者独立，别互相挡住
                    bad, recipe = _check_group_order(sql, task)
                if bad:
                    msg = f"calls[{i}] 口径错误：" + "；".join(bad)
                    if recipe:
                        msg += f"。正确骨架（照这个改，'?' 换成真实值）：{recipe}"
                    return msg
        return ""

    def _plan(self, task: Any, retries: int = 3, call_limit: int = 3) -> dict[str, Any]:
        mem = self._memory_block(task)
        # 知识图谱子图 —— 原版 render_system_message 拼 control_primitives 的位置。
        # 放在工具目录之后：先知道"有哪些工具"，再知道"这个指标怎么用它们算"。
        gctx = graph_context(task)
        gctx_block = f"\n{gctx}\n" if gctx else ""
        if self.blind:
            # 盲测：只给自然语言评分点，不泄露 dimension / subject。
            # 这样测的是「模型能否自己想到要拆到哪个维度」，而不是照抄提示。
            points = "\n".join(f"- {p.point}" for p in task.rubric)
        else:
            points = "\n".join(
                f"- {p.point}｜维度 dimension 必须写成 `{p.dimension}`｜"
                f"subject 必须写成 {json.dumps(p.subject, ensure_ascii=False)}｜权重 {p.weight}"
                for p in task.rubric
            )
        prompt = f"""你是数据分析 agent。用户的问题是：

{task.question}

评分点（每个都要被数据覆盖，否则扣分）：
{points}

{TOOL_CATALOG}{gctx_block}
VLML 自己的用法提示：
{self.db_info}

【工具优先 · query_sql 只是逃生舱】
VLML 的 10 个报告工具已经封装好指标口径。能用报告工具拿到的，就用报告工具，
不要自己写 SQL 硬算。

实测反复出现的三类错误，务必避开（它们都是同一个毛病：
把"你想表达的语义"直接当成数据库里的标识符来写）：
  1. 把维度名当列名 —— pistol_win_rate、pattern_rounds、pattern_confidence
     是**要你填的 dimension**，不是表里的列。
  2. 把工具名当表名 —— pattern_detection_report 是工具，不能写在 FROM 后面。
  3. 猜列名 —— team_name 有的表里有、有的没有，写之前先查
     information_schema.columns 确认。

{LOCAL_SQL_NOTES}

你过去积累的经验（只列出与本题最相关的几条）：
{mem}
{self._render_observation()}
【本轮调用条数】必须出满 {call_limit} 条 —— 每个评分点一条查询。
少一条就有一个评分点永远拿不到事实（执行层只跑前 {call_limit} 条，多出的会被丢弃）。
{PLAN_SCHEMA}"""

        # 调试取证：MVE_DEBUG_PROMPT=1 时把完整 prompt 落到 /tmp。
        # "图谱到底进没进 prompt"只能看原文确认，不能靠猜 —— 实测就在这里
        # 翻过车：代码写了注入，但模型行为没变，必须眼见为实。
        if os.environ.get("MVE_DEBUG_PROMPT"):
            try:
                with open("/tmp/mve_plan_prompt.txt", "w", encoding="utf-8") as f:
                    f.write(prompt)
            except OSError:
                pass

        # 照 process_ai_message 的 retry=3：解析不合法就带着报错再来一次，
        # 而不是静默退化成"没调工具 → 无证据"。
        last_err = ""
        last_plan: dict[str, Any] = {}
        for attempt in range(1, retries + 1):
            try:
                plan = chat_json([
                    {"role": "system", "content": "你是严谨的数据分析 agent，只输出 JSON。"},
                    {"role": "user", "content": prompt},
                ])
            except Exception as e:
                last_err = f"调用失败：{type(e).__name__}: {e}"
                self.errors.append(f"规划失败（第 {attempt} 次）：{last_err}"[:200])
                continue
            err = self._validate_plan(plan, task)
            last_plan = plan if isinstance(plan, dict) else {}
            if not err:
                self.last_thought = str(plan.get("thought", "")) if isinstance(plan, dict) else ""
                _apply_auto_fixes(plan, task, self.errors)
                return plan
            last_err = err
            self.errors.append(f"计划校验失败（第 {attempt}/{retries} 次）：{err}")
            # 把校验结果回灌，让模型自己改 —— 只说"再试一次"它还会犯同样的错
            prompt += (
                f"\n\n【上一版被驳回】{err}。"
                f"工具名必须原样取自上面的工具清单，不得自造。请重新输出。"
            )

        # 重试用尽仍不合法：能修的先修，不要整轮作废。
        # 整轮作废 = 一个工具都不调 = 判"无证据"（dont_know 0%）。
        # 那是在惩罚格式而不是惩罚能力 —— 而且它会把上一轮已经拿到的进展抹平，
        # 这正是覆盖率忽高忽低的来源之一。所以这里一律降级放行：
        # 只丢掉工具名不合法的调用，其余照跑（口径错的 SQL 会拿到错的值，
        # 但至少留下证据，下一轮 critique 能指出"值不符"）。
        calls = last_plan.get("calls")
        if isinstance(calls, list):
            kept = [
                c for c in calls[:call_limit]
                if isinstance(c, dict) and str(c.get("tool", "")) in TOOL_REGISTRY
            ]
            if kept:
                last_plan["calls"] = kept
                _apply_auto_fixes(last_plan, task, self.errors)
                self.errors.append(
                    f"计划校验重试 {retries} 次未收敛，已降级放行 "
                    f"{len(kept)} 个有效调用（丢弃 {len(calls[:call_limit]) - len(kept)} 个）：{last_err}"[:220]
                )
                self.last_thought = str(last_plan.get("thought", ""))
                return last_plan

        self.errors.append(f"规划重试 {retries} 次仍不合法，本轮放弃：{last_err}")
        self.last_thought = ""
        return {}

    # ---- 执行 ----
    # ---- 表结构探测：SQL 报错后的「Inventory」----
    async def _schema_probe(self, sql: str, error: str = "") -> str:
        """SQL 报错时，把失败语句涉及的表**实际有哪些列**、以及**该用什么替代**查出来。

        为什么必须有这一步：实测三轮卡在同一个错 —— 模型一直写 team_name，
        而 rounds 表里根本没有这一列。只把 "Referenced column team_name not found"
        回灌给它是没用的：它知道自己错了，但不知道**有什么是对的**，
        于是只能再猜一次，大概率还是同一个错。

        同构于原仓库 ActionAgent.render_human_message：它不只给
        "Execution error"，还一并给 Inventory / Nearby blocks（你现在有什么）。
        在这里，"你现在有什么" 就是 information_schema 里真实存在的列名。

        光列列名还是不够 —— 实测给了列名它照样写 team_name，
        因为它要的是"按队伍分组"这个语义。所以还要把**替代方案**说出来：
        缺 team_name 就告诉它有 winning_team_name / losing_team_name。
        """
        tables = re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w]*)", sql or "", re.I)
        tables = [t.lower() for t in tables if t.lower() != "select"]
        if not tables:
            return ""
        tbls = sorted(set(tables))[:3]
        in_list = ",".join(f"'{t}'" for t in tbls)
        q = (f"SELECT table_name, column_name FROM information_schema.columns "
             f"WHERE table_name IN ({in_list}) ORDER BY table_name, ordinal_position")
        try:
            res = await execute_custom_sql(q)
        except Exception:
            return ""
        rows = (res or {}).get("rows") if isinstance(res, dict) else None
        if not rows:
            # 查不到列 = 这张表根本不存在（实测模型把**工具名**当成表名写进了
            # FROM：FROM pattern_detection_report）。照 VLML get_database_info 的做法，
            # 这时要给出**可用清单**，而不是只说"你错了"。
            return await self._table_menu(tables)
        by: dict[str, list[str]] = {}
        for t, c in rows:
            by.setdefault(str(t), []).append(str(c))

        out = "；".join(f"{t} 有列 {', '.join(cs[:24])}" for t, cs in by.items())

        # 把"缺哪一列 → 该用哪列"直接说破。列名清单给过，模型照样写错，
        # 因为它想要的是语义（按队伍分组），不是一个列名列表。
        m = re.search(r'Referenced column "([^"]+)" not found', error or "")
        if m:
            missing = m.group(1)
            core = [p for p in missing.split("_") if p not in
                    {"name", "id", "key", "code", "type"} and len(p) > 2]
            alts: list[str] = []
            for cs in by.values():
                for c in cs:
                    if c == missing:
                        continue
                    if any(p in c for p in core) and c not in alts:
                        alts.append(c)
            if alts:
                out += (f"\n注意：这些表里**没有** {missing} 列；"
                        f"要表达同一个意思，用 {', '.join(alts[:8])}。"
                        f"不要再写 {missing}。")
        return out

    async def _table_menu(self, tried: list[str]) -> str:
        """照 VLML get_database_info：给可用表清单，而不是只说"你错了"。"""
        try:
            res = await execute_custom_sql(
                "SELECT table_name FROM information_schema.columns GROUP BY table_name"
            )
        except Exception:
            return ""
        names = sorted({str(r[0]) for r in (res.get("rows") or []) if r})
        if not names:
            return ""
        return (
            f"数据库里**没有** {', '.join(tried) or '这张表'} —— "
            f"工具名不是表名，不能写在 FROM 后面。可用的表只有：{', '.join(names)}"
        )

    def _call_limit(self, task: Any) -> int:
        """本轮最多跑几条调用 —— 必须由**评分点数**决定，不能写死。

        写死 3 的直接后果（实测取证，88 轮日志）：
            map_rounds_split      5 个评分点 → 最多覆盖 3 个 → 长期 60%
            fb_conversion_analysis 4 个评分点 → 最多覆盖 3 个 → 长期 80%
        而工具路径的题一次调用能带回多个维度，3 条够用 → 全 100%。
        于是"SQL 类的题一直学不会"根本不是模型学不会，是**执行层截断了** ——
        反馈写得再准也没用，第四条查询根本没机会发出去。

        上限 8 只是防爆（一次跑几十条 SQL 会拖垮裁判），不是能力上限。
        """
        n = len(getattr(task, "rubric", None) or [])
        return max(3, min(8, n)) if n else 3

    async def _execute(self, plan: dict[str, Any],
                       limit: int = 3) -> list[dict[str, Any]]:
        obs: list[dict[str, Any]] = []
        for call in (plan.get("calls") or [])[:limit]:
            tool = str(call.get("tool", ""))
            args = dict(call.get("args") or {})
            fn = TOOL_REGISTRY.get(tool)
            if fn is None:
                obs.append({"tool": tool, "error": f"未知工具 {tool}"})
                self._note_attempt(tool, args, f"未知工具 {tool}")
                continue
            try:
                # query_sql 的参数名在 MCP 侧是 sql_query；模型也可能写成 sql。都接住。
                if fn is execute_custom_sql:
                    sql = str(args.pop("sql_query", "") or args.pop("sql", "") or
                              args.pop("sql_query", ""))
                    if not sql.lower().lstrip().startswith("select"):
                        obs.append({"tool": tool, "error": "只允许 SELECT"})
                        self.errors.append(f"{tool}: 只允许 SELECT")
                        self._note_attempt(tool, {"sql_query": sql}, "只允许 SELECT")
                        continue
                    res = await fn(sql)
                    args = {"sql_query": sql}
                    if isinstance(res, dict) and res.get("error"):
                        # SQL 挂了 → 顺手把这张表真实的列查出来，喂给下一轮
                        hint = await self._schema_probe(sql, str(res.get("error", "")))
                        if hint:
                            self.schema_hints.append(hint)
                elif tool in MAP_NAME_BUGGY_TOOLS:
                    res = await _guarded_report(fn, args)
                else:
                    res = await fn(**args)
                if isinstance(res, dict) and res.get("error"):
                    self.errors.append(f"{tool}: {res['error']}"[:200])
                    obs.append({"tool": tool, "args": args, "error": res["error"]})
                    self._note_attempt(tool, args, str(res["error"]))
                    continue
                obs.append({"tool": tool, "args": args, "result": res})
                # 日记必须带上**原始返回**：fork 的 _emit_trajectory 落的就是
                # `response`（drift_voyager.py:329-345），判定读的也是它。
                # 之前这里只记 tool+args，把 result 丢了 —— 于是事后无法回答
                # "当时工具到底返回了什么"，只能看 LLM 誊抄后的 facts。
                # 日记必须带上**原始返回**：fork 的 _emit_trajectory 落的就是
                # `response`（drift_voyager.py:329-345），critic 读的也是它。
                # 之前这里只记 tool+args 把 result 丢了 —— 事后无法回答
                # "当时工具到底返回了什么"，只剩 LLM 誊抄过的 facts 可看。
                # 存原始对象（不截断）：trajectory 只在内存里传，落盘时
                # run_mve 只取 tool 名（:311），不会撑爆日志。
                self.trajectory.append({"tool": tool, "args": args, "result": res})
                self._note_attempt(tool, args, "")
            except TypeError as e:  # 参数名/个数不对 —— 报出来让模型下轮改
                obs.append({"tool": tool, "error": f"参数错误 {e}"})
                self.errors.append(f"{tool}: 参数错误 {e}")
                self._note_attempt(tool, args, f"参数错误 {e}")
            except Exception as e:  # 工具挂了不能让整轮崩掉
                obs.append({"tool": tool, "error": f"{type(e).__name__}: {e}"})
                self.errors.append(f"{tool}: {type(e).__name__}: {e}")
                self._note_attempt(tool, args, f"{type(e).__name__}: {e}")
        if not obs:
            # 计划里根本没有调用 —— 必须说出来，不能静默变成"无证据"
            self.errors.append("计划中没有产生任何工具调用（plan.calls 为空或解析失败）")
        return obs

    def _note_attempt(self, tool: str, args: dict[str, Any], error: str) -> None:
        """记下每一次**尝试过**的调用（含失败的和它的 SQL 原文）。

        trajectory 只记成功的调用 —— 于是上一轮的回灌里只有工具名，没有 SQL。
        模型看不到自己上一轮写了什么，只能重新生成一遍，然后原样再犯一次错
        （实测连犯三轮 team_name）。
        原仓库回灌的是 "Code from the last round" 的**全文**，不是函数名。
        """
        self.attempted.append({
            "tool": tool,
            "args": {k: v for k, v in (args or {}).items() if k in
                     {"sql_query", "sql", "series_id", "team_name", "team", "map_name"}},
            "error": (error or "")[:300],
        })

    # ---- 抽取事实 ----
    def _extract(self, task: Any, obs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        points = "\n".join(
            f"- {p.point}｜dimension=`{p.dimension}`｜subject={json.dumps(p.subject, ensure_ascii=False)}"
            for p in task.rubric
        )
        payload = json.dumps(
            [{"tool": o["tool"], "args": o.get("args"),
              "result": o.get("result"), "error": o.get("error")} for o in obs],
            ensure_ascii=False,
            default=str,   # VLML 的返回里带 datetime，不处理会序列化失败
        )[:12000]
        prompt = f"""下面是工具返回的真实数据：

{payload}

评分点：
{points}

请把数据整理成事实数组。每个事实形如：
{{"subject": {{"series":"...","team":"...","map":"..."}},
  "dimension": "维度名，必须与评分点里的 dimension 一致",
  "value": 数值,
  "unit": "percent|count|ratio|raw",
  "base": 分母（整数；若数据里是 {{"num":x,"denom":y}} 就填 y；没有就 null）}}

只输出 JSON：{{"facts":[...], "narrative":"一句话结论"}}
不要编造数据里没有的数字。

**取不到就不要给这条事实**，不要填 0 或 null 占位 —— 占位值会被当成真答案
去和裁判比对（实测模型填 0，比对成"值不符 0≠59"，白丢分）。

注意：工具返回里**除了 key_metrics，顶层字段也要看**（scope / summary / report_type 等）。
样本量、置信度这类元数据就写在 scope 里 —— 实测模型只翻 key_metrics，
于是 scope.rounds 拿不到就填个 0 交差。"""
        try:
            out = chat_json([
                {"role": "system", "content": "你是严谨的数据整理器，只输出 JSON，绝不编造数字。"},
                {"role": "user", "content": prompt},
            ])
        except Exception as e:
            self.errors.append(f"抽取失败：{type(e).__name__}: {e}"[:200])
            out = {}
        facts = []
        for f in (out.get("facts") or []):
            if not isinstance(f, dict):
                continue
            if "subject" not in f or "dimension" not in f:
                continue
            facts.append(make_fact(
                subject=f.get("subject") or {},
                dimension=str(f.get("dimension")),
                value=_coerce_value(f.get("value")),
                unit=str(f.get("unit") or "raw"),
                base=f.get("base"),
                source={"tool": "llm_extract", "args": {}},
            ))
        self._narrative_text = str(out.get("narrative", ""))
        return facts

    # ---- 事实溯源：防幻觉的最后一道闸 ----
    def _ground_facts(
        self, facts: list[dict[str, Any]], obs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """丢掉「工具返回里根本没出现过」的数值。

        这是防幻觉里最硬的一招，对应原仓库 critic 的验证端
        （critic.py:39-42：event 里出现 onError 就直接判失败，不让模型自己圆）。
        prompt 里写"不要编造数字"是软约束 —— 实测模型在工具全失败时照样
        能写出一整套看起来合理的数，只因为 prompt 拦不住。

        判定：数值必须能在工具返回文本里找到（近似相等），
        或者能由返回里的两个数字算出来（如 13/21→61.9，此时 base 也在数据里）。
        """
        if not facts:
            return facts
        blob = json.dumps(
            [o.get("result") for o in obs if o.get("result") is not None],
            ensure_ascii=False, default=str,
        )
        nums: list[float] = []
        for m in re.finditer(r"-?\d+(?:\.\d+)?", blob):
            try:
                nums.append(float(m.group()))
            except ValueError:
                continue
        if not nums:
            # 没有任何数字返回 —— 那所有数值型事实都不可信
            return [f for f in facts if not isinstance(f.get("value"), (int, float))]

        def near(x: float) -> bool:
            return any(abs(x - n) <= max(0.05, abs(n) * 0.005) for n in nums)

        # 退回检查：模型常常算出正确的比值却不填 base（实测 eco 6/14→42.86
        # 被当成编造丢掉，那是**误杀**）。这时要允许「任意两个返回数字相处」——
        # 这道闸要拦的是**完全无来源**的数字，精确验算归裁判做，不归它做。
        pool = sorted(set(nums))[:200]

        def derivable(x: float) -> bool:
            for a in pool:
                for b in pool:
                    if b == 0:
                        continue
                    if abs(a / b * 100 - x) <= 0.15:   # 百分比：6/14→42.86
                        return True
                    if abs(a / b - x) <= 0.005:        # 比值：13/21→0.619
                        return True
            return False

        kept: list[dict[str, Any]] = []
        for f in facts:
            v = f.get("value")
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                kept.append(f)          # 非数值（如文本枚举）不做数值溯源
                continue
            v = float(v)
            if near(v) or near(round(v, 4)):
                kept.append(f)
                continue
            base = f.get("base")
            if isinstance(base, (int, float)) and base:
                # 允许由分母算出：percent 的 v*base/100，ratio 的 v*base
                if near(v * float(base)) or near(v * float(base) / 100.0):
                    kept.append(f)
                    continue
            # base 缺失时不要急着判死 —— 先看它能不能由返回里的两个数字相除得到
            if derivable(v):
                kept.append(f)
                continue
            self.hallucinations.append(
                f"{f.get('dimension')}={v}：工具返回里没有这个数，已丢弃"
            )
        return kept

    # ---- 主入口（与 ScriptedVoyager 同签名）----
    @staticmethod
    def _is_tool_path_task(task: Any) -> bool:
        """这道题是不是"工具路径类"（取值靠按路径 dig，而不是靠自己写 SQL）。

        为什么要分流：实测代码路径对**工具类**是根治（翻错键的病没了），
        对 **SQL 类**反而回退（corrode 原路径 100%、代码路径 0%：模型放弃写
        gaps-and-islands，改调 match_rounds_report，取不到就交 0）。

        这不是调参问题，是**同构边界**：原版 Voyager 的 JS 是"调用 bot 上的
        API"，对应这里调 MCP 工具；而 SQL 是 `query_sql` 的**参数字符串**，
        对应原版 `bot.chat("...")` 的字符串参数 —— 原版从不把 chat 的字符串
        当成程序来学，技能库里也没有"chat 字符串"这一类技能。
        所以：**工具路径类走代码，SQL 类走原来的 plan 路径**（那条路有图谱
        骨架与 `_check_sql_layering` 分层断言，正是 SQL 类需要的护栏）。
        """
        specs = [getattr(p, "answer_spec", None)
                 for p in (getattr(task, "rubric", None) or [])]
        specs = [s for s in specs if s is not None]
        if not specs:
            return False
        return all(bool(getattr(s, "tool", "")) and not getattr(s, "sql", "")
                   for s in specs)

    # ---- 重放自己攒下的程序（plan 路径上的「技能被 exec」）----
    async def _replay_saved(self, task: Any) -> tuple[list[dict], dict] | None:
        """重放技能库里存下来的调用序列；**维度取齐了才采用**。

        为什么 plan 路径也必须有这一份（此前没有，是学习曲线涨不起来的根因）：
        8 道题里 6 道是 SQL 类，走 plan 路径，而那条路**从来不产出
        `program_code`** —— 于是"跑通了"对技能库零贡献，撤掉知识图谱重考时
        只能从头再写一遍 SQL，然后再犯同一个口径错（实测 corrode_collapse
        练完 100%、重考仍 0%，且缺的还是同一条 losing_team_name 放错层）。

        原版 Voyager 的规矩是「自己写 → 跑通 → 存程序 → 下次直接跑」，
        判据是**跑通了没有**，不是这段东西长什么样。此前 `voyager.py:1631-1634`
        那条注释以"原版从不把 chat 字符串当程序学"为由把 SQL 类排除在外 ——
        那是把表面形态当成了同构边界：原版的 `bot.chat()` 发的是自然语言指令，
        而 MVE 的 SQL 是**结构化检索代码**，与"按路径 dig"是同一类东西
        （都是"怎么把数据取出来"），只是写法不同。排除它的代价就是：
        六成的题永远学不会。

        只重放 `practice` 通道（自己跑通过的），且**维度不齐就不采用** ——
        部分正确的程序拿去复用，等于把一个已知缺陷固化成默认行为。
        """
        try:
            import skill_store
        except Exception:                                    # pragma: no cover
            return None
        tid = str(getattr(task, "topic_id", "") or "")
        dims = [str(getattr(p, "dimension", "") or "")
                for p in (getattr(task, "rubric", None) or [])]
        for s in skill_store.all_skills():
            if str(s.get("topic") or "") != tid:
                continue
            if str(s.get("source") or "") != "practice":
                continue
            calls = list((s.get("blueprint") or {}).get("calls") or [])
            if not calls:
                continue
            obs = await self._execute({"calls": calls}, limit=len(calls))
            if not [o for o in obs if o.get("result") is not None]:
                continue
            facts = self._extract(task, obs)
            facts = self._ground_facts(facts, obs)
            got = {str(f.get("dimension")) for f in facts}
            if dims and not all(d in got for d in dims):
                continue                    # 没取全 → 不算数，让模型自己重写
            return facts, s
        return None

    async def _run_with_code(self, task: Any) -> dict[str, Any]:
        """代码化取证：模型写代码 → 解释器执行 → **值由代码产出**。

        照原版 `action.py` 的完整链路（`render_system_message` → 模型出
        ```js → `process_ai_message` 的 AST 断言 → 解释器执行 → 观察原样回灌），
        MVE 侧由 `action_code.py` 实现。与 plan JSON 那条路的根本区别：

            plan JSON 路：模型调工具 → **模型读返回誊写成 facts** → 判定读 facts
            代码路      ：模型写代码 → **解释器按路径取值** → 判定读原值

        伴学的同一条原则（`deterministic_evaluators.py:158-167`）：expected 与
        tolerance 只从服务端私有 answer_spec 读，**绝不从学习者的作答读**。
        誊抄等于让模型再当一次读数器 —— 那正是伴学明确排除掉的环节。
        """
        import action_code

        dims = sorted({str(p.dimension) for p in getattr(task, "rubric", [])
                       if getattr(p, "dimension", "")})
        # 技能库里学到的解法源码要进 system（照原版 `+ skills`）——
        # 不然旁路学到的东西在写代码时一句都看不见。
        skills_text = ""
        top: list[dict[str, Any]] = []
        try:
            top = skill_store.retrieve(getattr(task, "topic_id", ""),
                                       getattr(task, "question", ""),
                                       top_k=skill_store.RETRIEVAL_TOP_K)
            # 注入计数与 mark_used 必须在这里做：此前只有 plan 那条路
            # （`_memory_block`）设置过 `_injected_keys`，代码路径永远是空的，
            # 于是技能的 hits / ok / 有效性加权全是死的 —— 面板上"本轮注入 0/5"
            # 不是没注入，是没记。
            self._injected_keys = [s["key"] for s in top]
            skill_store.mark_used(self._injected_keys)
            blocks = [f"- {s.get('text','')}{_code_block(s)}" for s in top]
            if blocks:
                # 必须写明"这些路径已被验证正确、优先照抄"：不写的话模型会在
                # 工具结构文档里另挑一条长得像的路径（实测挑了
                # `team_comparison.Cloud9...`，真值在 `key_metrics.team...`）。
                skills_text = ("\n\n已学会的解法（**这些路径已被裁判验证正确，"
                               "优先照抄，不要从上面的返回结构里另挑一条**）：\n"
                               + "\n".join(blocks))
        except Exception:
            skills_text = ""
            self._injected_keys = []

        # ---- 先跑库里的程序，跑通就直接采用（原版"技能被 exec"的本体）----
        #
        # 此前技能只被**拼进 prompt 让人读**，不会被运行 —— 这正是
        # "技能库有内容但覆盖率不涨"的根因：模型每轮重写一遍，每次重写都是
        # 一次新的翻错机会。原版 Voyager 是把 `program_code` 取出来直接 exec 的。
        # 只复用 `practice` 通道（自己跑通过的源码）；旁路通道存的是伪代码
        # 注释（`bypass_learn._solution_code`），没有主函数，跑不起来。
        reused = await self._reuse_skill(top, dims)
        if reused is not None:
            out, skill = reused
            self.trajectory = [{"tool": str(t), "error": ""}
                               for t in (out.get("tools") or [])]
            self.attempted = [dict(c) for c in (out.get("calls") or [])]
            facts = self._facts_from_values(task, out.get("values") or {},
                                            out.get("tools") or [])
            self.last_round = {"calls": self.attempted, "errors": [],
                               "critique": "", "schema_hints": [],
                               "code": str(out.get("program_code") or "")}
            return {
                "facts": facts,
                "narrative": {"text": "", "comparable": False},
                "skill_used": f"reuse({skill.get('name','')})",
                "reused_skill": skill.get("name", ""),
                "program_code": str(out.get("program_code") or ""),
                "program_name": str((skill.get("blueprint") or {})
                                    .get("program_name") or ""),
                "trajectory": self.trajectory, "errors": [],
                "hallucinations": [], "attempted": self.attempted,
            }

        prev = getattr(self, "last_round", None) or {}
        out = await action_code.collect(
            task, dims=dims,
            code=str(prev.get("code") or ""),
            error="；".join(str(e) for e in (prev.get("errors") or []))[:400],
            critique=str(prev.get("critique") or ""),
            missing=list(getattr(self, "failed_dims", None) or []),
            skills_text=skills_text,
            hint=str(getattr(self, "hint_level", "") or ""),
        )

        if out.get("error"):
            self.errors.append(str(out["error"]))
            self.last_round = {"calls": [], "errors": self.errors,
                               "critique": "", "schema_hints": [],
                               "code": str(out.get("program_code") or "")}
            return {"facts": [],
                    "narrative": {"text": "", "comparable": False},
                    "skill_used": "action_code(failed)",
                    "trajectory": [], "errors": self.errors,
                    "hallucinations": [], "attempted": []}

        # trajectory 的形态必须与 plan JSON 那条路**一致**（元素是带 "tool" 的
        # dict）：主循环按 `t["tool"]` 读它。代码路径这里如果塞字符串，
        # run_mve.py:179 的 `t.get("error")` 直接 AttributeError 崩整轮。
        self.trajectory = [{"tool": str(t), "error": ""}
                           for t in (out.get("tools") or [])]
        self.attempted = [dict(c) for c in (out.get("calls") or [])]
        facts = self._facts_from_values(task, out.get("values") or {},
                                        out.get("tools") or [])
        self.last_round = {
            "calls": self.attempted, "errors": list(self.errors),
            "critique": self.last_critique, "schema_hints": [],
            "code": str(out.get("program_code") or ""),
        }
        return {
            "facts": facts,
            "narrative": {"text": "", "comparable": False},
            "skill_used": f"action_code({out.get('program_name') or ''})",
            # 自己写的这段源码要带出去：跑通了就由主循环存进技能库 ——
            # 原版是「自己写 → 跑通 → 存 program_code」，不是存别人给的答案。
            "program_code": str(out.get("program_code") or ""),
            "program_name": str(out.get("program_name") or ""),
            # 对外仍报工具名列表（面板/日志用），内部 self.trajectory 才是 dict 形态
            "trajectory": self.trajectory,
            "errors": list(self.errors),
            "hallucinations": [],
            "attempted": self.attempted,
        }

    async def _reuse_skill(self, top: list[dict[str, Any]],
                           dims: list[str]) -> tuple[dict[str, Any], dict] | None:
        """试着直接跑技能库里的程序；跑得通且维度齐全就返回 (结果, 技能)。

        这是原版 `SkillManager.retrieve_skills` → `exec` 那一段的同构物。
        判据用「维度齐不齐」而不是「有没有异常」：程序能跑完但路径取错时
        不会抛异常（dig 取不到就返回 None），只看异常会把错的也当成对的。
        """
        import action_code
        for s in top or []:
            if str(s.get("source") or "") != "practice":
                continue
            code = str(s.get("code") or "")
            name = str((s.get("blueprint") or {}).get("program_name") or "")
            if not code or not name:
                continue
            try:
                out = await action_code.execute(code, name)
            except Exception:
                continue
            vals = out.get("values") or {}
            if dims and all(not action_code.is_empty(vals.get(d)) for d in dims):
                out["program_code"] = code
                out["program_name"] = name
                return out, s
        return None

    def _facts_from_values(self, task: Any, values: dict[str, Any],
                           tools: list[str]) -> list[dict[str, Any]]:
        """代码取到的原值 → facts。**不做任何 LLM 加工**。

        `{num, denom}` 这种形态按 answer_spec 的 percent 口径换算成百分比，
        换算规则来自服务端配方（与裁判用的是同一份口径），不是模型现场算的。
        """
        facts: list[dict[str, Any]] = []
        for p in getattr(task, "rubric", None) or []:
            dim = str(getattr(p, "dimension", "") or "")
            if dim not in values:
                continue
            raw = values[dim]
            if raw is None:
                continue                     # 没取到就是没取到，不兜底
            spec = getattr(p, "answer_spec", None)
            percent = bool(getattr(spec, "percent", False)) if spec else False
            value: Any = raw
            base: int | None = None
            if isinstance(raw, dict):
                num = raw.get("num")
                denom = raw.get("denom")
                if percent and isinstance(num, (int, float)) \
                        and isinstance(denom, (int, float)) and denom:
                    value = round(float(num) / float(denom) * 100, 1)
                    base = int(denom)
                else:
                    value = num if num is not None else raw
                    if isinstance(denom, (int, float)):
                        base = int(denom)
            unit = "percent" if percent else ("ratio" if isinstance(value, float)
                                              else "raw")
            facts.append(make_fact(
                subject=dict(getattr(p, "subject", None) or {}),
                dimension=dim,
                value=value, unit=unit, base=base,
                source={"tool": "action_code",
                        "program": True,
                        "tools": list(tools)},
            ))
        return facts

    async def run(self, task: Any) -> dict[str, Any]:
        self.trajectory = []
        self.errors = []
        self.hallucinations = []
        self.schema_hints = []
        self.attempted = []

        if ACTION_CODE and self._is_tool_path_task(task):
            return await self._run_with_code(task)

        # 照 VLML：动手前先拿到「有什么可用」，而不是写完被打回来再猜
        if not self.db_info:
            self.db_info = await db_self_description()

        # 跨运行恢复缺口：上一轮运行判错的维度，这一轮要接着驱动重做。
        # 缺口记在 run_log（持久化），不记在技能库 —— 技能库只放被裁判认可的做法。
        if not self.failed_dims:
            dim_by_point = {p.point: p.dimension for p in task.rubric}
            last_missing = [str(m) for m in
                            run_log.last_missing(getattr(task, "topic_id", ""))]
            self.failed_dims = sorted({
                dim_by_point.get(m.split("(")[0].strip(), m)
                for m in last_missing
            })
            # 评分点级一起恢复：跨运行时维度级恢复不了"这个维度下哪条没交"
            self.failed_points = sorted(set(last_missing))

        # ---- 先跑库里的程序，跑通就直接采用（原版"技能被 exec"的本体）----
        #
        # 此前技能只被**拼进 prompt 让人读**，不会被运行 —— 这正是
        # "技能库有内容但覆盖率不涨"的根因：模型每轮重写一遍，每次重写都是
        # 一次新的翻错机会。原版 Voyager 是把 `program_code` 取出来直接 exec 的。
        # plan 路径（SQL 类）没有 program_code，它的等价物是存下来的
        # **调用序列** —— 见 `_replay_saved`。
        replayed = await self._replay_saved(task)
        if replayed is not None:
            facts, skill = replayed
            return {
                "facts": facts,
                "narrative": {"text": "", "comparable": False},
                "skill_used": f"replay({skill.get('name', '')})",
                "reused_skill": skill.get("name", ""),
                "thought": "重放技能库里自己跑通过的调用序列（口径已由裁判验证）",
                "errors": list(self.errors),
                "hallucinations": list(self.hallucinations),
            }

        plan = self._plan(task, call_limit=self._call_limit(task))
        obs = await self._execute(plan, limit=self._call_limit(task))
        ok_obs = [o for o in obs if o.get("result") is not None]

        # 照 critic.py:39-42 —— onError 直接判失败。
        # 工具全挂时不进入抽取：让模型对着一串 error 文本"整理事实"，
        # 它只会把错误编成数字。实测过：轨迹为空却产出 5 条事实。
        if not ok_obs:
            facts: list[dict[str, Any]] = []
            self.errors.append(
                "本轮没有任何工具调用成功 —— 按 critic 的 onError 规则直接判失败，"
                "跳过事实抽取（否则模型会对着错误信息编数字）"
            )
        else:
            facts = self._extract(task, obs)
            facts = self._ground_facts(facts, obs)

        # 本轮观察存下来 —— 下一轮的 prompt 要用（照 render_human_message）
        self.last_round = {
            # 用 attempted 而不是 trajectory：失败的调用和它的 SQL 原文也要回灌
            "calls": [dict(a) for a in self.attempted],
            "errors": list(self.errors),
            "critique": self.last_critique,
            "schema_hints": list(self.schema_hints),
        }
        self.schema_hints = []
        return {
            "facts": facts,
            "narrative": {"text": getattr(self, "_narrative_text", ""), "comparable": False},
            "skill_used": f"llm(skills={len(skill_store.all_skills())})",
            "thought": self.last_thought,
            "errors": list(self.errors),
            "hallucinations": list(self.hallucinations),
        }

    # ---- 学习：打分回传，照 fork 的 critic 形态 ----
    # voyager-fork/voyager_disk/drift_voyager.py:412-469 的 _critic()：
    #   返回 (success, critique, signal)，critique 是**第一人称自我反思**
    #   （"我期望…但世界返回…我以为…"），成功 → _synthesize_skill，
    #   失败 → (intent, critique) 进 IntentDiffAnalyzer 落 GapSpec。
    # fork/voyager_disk/glm_curator.py:235-243 的 _context() 又把
    # "已失败任务（缺口类型）" 写进下一题的 prompt —— 这就是"打分回传驱动下一题"。
    def learn(
        self,
        *,
        covered: list[str],
        missing: list[str],
        task: Any = None,
        referee: Any = None,
        verdict: str = "",
        rejected_low_base: list[str] | None = None,
        confidence: float = 0.0,
    ) -> str:
        """返回本轮的 critique（会被写进下一轮的 prompt，也会上面板）。

        结构化反馈包存在 `self.last_feedback`（dict），字段对齐伴学
        `answer_evaluate` 的返回 —— critique 只是它渲染出来的文本视图。
        """
        dim_by_point = {p.point: p.dimension for p in (task.rubric if task else [])}
        topic = getattr(task, "topic_id", "?") if task else "?"

        # 我的编排：只算**成功**的调用。失败的调用进 errors/attempted，
        # 不能算进"我会的编排"，否则 feedback 会以为某个工具已经调过了。
        my_plan = [str(t.get("tool")) for t in self.trajectory if t.get("tool")]
        # 裁判编排：内部标记（<deterministic-sql>）不是真工具，过滤掉 ——
        # 实测 Voyager 会把它们当真工具名写进技能库，然后一直调一个不存在的工具。
        ref_plan = [str(t) for t in (getattr(referee, "trajectory", None) or [])
                    if not str(t).startswith("<")]

        fb = feedback_mod.build_feedback(
            verdict=verdict,
            score=int(round(100 * (len(covered) / max(1, len(covered) + len(missing))))),
            coverage=(len(covered) / max(1, len(covered) + len(missing))),
            covered=covered, missing=missing,
            rejected_low_base=list(rejected_low_base or []),
            dim_by_point=dim_by_point,
            # subject 让 SQL 路径维度的反馈能说清「WHERE 必须带 map=Haven」
            subj_by_point={str(p.point): dict(p.subject or {})
                           for p in (task.rubric if task else [])},
            ref_plan=ref_plan, my_plan=my_plan,
            confidence=float(confidence or 0.0),
        )
        self.last_feedback = fb.to_dict()
        miss_dims = sorted({d.get("dimension") or d.get("point")
                            for d in fb.missing_detail})

        if not missing:
            # 照 curriculum.clean_up_tasks：成功了就把同题的失败记录顶掉。
            # v1 是 append，于是"失败教训"和"成功经验"同时在库里互相打架。
            #
            # lesson 不再是模板（"先取整体报告再下钻 query_sql"—— 那句是写死的，
            # 写进技能库等于什么都没写）：现在是**这套具体编排 + 它覆盖的维度**。
            # 名字必须是**稳定主键**：`{topic}解法`。
            # 此前这里叫 `{topic}编排`，主循环「学习①」存自己跑通的程序时
            # 叫 `{topic}解法` —— 同一个成果落成两条，一条有源码一条没有，
            # 注入时模型可能读到那条没有源码的（那正是"技能是伪代码注释
            # 只被 read"的残留）。统一成一个 key，两条通道覆盖同一条：
            # 没跑通时留文字经验，跑通了由「学习①」把**自己写的源码**补上。
            action, key = skill_store.add(
                topic, f"{topic}解法", fb.lesson,
                source="practice", blueprint=feedback_mod.blueprint_for(fb),
            )
            self._log_skill(action, key)
            self.failed_dims = [d for d in getattr(self, "failed_dims", []) if d not in miss_dims]
            # 全覆盖了：缺口清空（与 failed_dims 同一套语义）。
            # 注意不能写成 `[p for p in ... if p not in miss_pts]` —— 这条分支里
            # missing 恒为空，那个过滤会把旧缺口**全留下**，下一轮继续报缺。
            self.failed_points = []
            self._sync_memory()
            # 技能有效 → 记一笔，检索时会加权；无效技能会自然沉底
            skill_store.mark_result(self._injected_keys, True)
            self.last_critique = feedback_mod.render_critique(fb)
            return self.last_critique

        # 照 Voyager 主循环 voyager.py:351 —— `if info["success"]: add_new_skill(info)`：
        # **失败不写技能库**。判错要消化，但不是靠把"我这次的做法"固化下来 ——
        # 这次的做法已经被裁判证明是错的，写进去就是往库里埋雷。
        # 实测反例：pistol_eco_pattern 攒下"用 match_summary_report 按 eco_win_rate
        # 维度分析"这条错经验后，后面几轮一直被它带偏，覆盖率反而不涨。
        #
        # 那判错靠什么消化？照 Voyager rollout：critique 在**本题内重做**时回灌
        # （last_round → _render_observation），最多重做到做对为止；
        # 跨运行则靠 run_log 里持久化的缺口维度（__init__ 里恢复），
        # 出题器也读它来选错题。缺口会留，做法不留。
        #
        # 之前这里会调一次 LLM 让模型"总结经验"，然后**不写库**（只记一条日志）——
        # 一次纯浪费的调用。现在不调了：结构化反馈直接由判定结果算出，
        # 比模型复述更准（"没取到"和"取错"是判定给的，不是猜的）。
        self._log_skill("not_saved_failure", f"{topic}::{fb.error_type}",
                        f"判 {verdict}（{fb.error_type}），做法未固化"
                        f"（缺口 {'、'.join(miss_dims) or '无'}）")

        # 照 fork：失败要留下"缺口类型"，供下一题的 curriculum 读取
        self.failed_dims = sorted(set(getattr(self, "failed_dims", [])) | set(miss_dims))
        # 评分点级：维度级只能说"哪个维度缺"，说不出"这个维度下哪一条没交"。
        self.failed_points = sorted(
            set(getattr(self, "failed_points", [])) | {str(m) for m in missing})
        self._sync_memory()

        # 错误假设单独留一条给因果时间线：维度级的 failed_dims 只能说"哪个维度缺"，
        # 说不出"我误以为什么"—— 而后者才是出题器该拿来出下一题的东西。
        if fb.misconceptions:
            causal_timeline.append(
                causal_timeline.REFEREE,
                topic_id=topic,
                summary=f"错误假设（{fb.error_type}）：{fb.misconceptions[0]}",
                detail={"error_type": fb.error_type, "error_types": fb.error_types,
                        "misconceptions": fb.misconceptions,
                        "missing_detail": fb.missing_detail,
                        "my_plan": my_plan, "ref_plan": ref_plan,
                        "validated_target": False},
            )

        self.last_critique = feedback_mod.render_critique(fb)
        return self.last_critique

    def _log_skill(self, action: str, key: str, note: str = "") -> None:
        """技能写入结果必须可见 —— 否则"到底拦没拦住膨胀"只能靠猜。"""
        label = {
            "added": "新增技能",
            "rewritten": "覆盖同名技能",
            "skipped": "丢弃（重复/无价值）",
            "not_saved_failure": "判错·做法未固化",
        }.get(action, action)
        self.skill_events = getattr(self, "skill_events", [])
        self.skill_events.append({
            "action": action, "key": key, "label": label, "note": note,
        })

    def _sync_memory(self) -> None:
        """self.memory 只作为展示用的文本视图；真正的存储是 skill_store 的 dict。"""
        self.memory = skill_store.load()
        return self.last_critique
