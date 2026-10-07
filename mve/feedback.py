#!/usr/bin/env python3
"""判题反馈结构（照猫娘伴学 `tutor_llm_agent_answer_evaluate.py:96-131`）。

为什么要有这个模块
------------------
用户原话：「反馈写回 Voyager 学习部分的内容没有做好，没有提供可取的有用的价值。」

之前 `voyager.learn()` 产出的 critique 是硬编码模板，判对判错各一句，
每轮只把维度名换一下：

    判对：「我的事实集覆盖了裁判的全部评分点，编排方式有效。」
    判错：「我以为这样编排就能覆盖全部评分点，但裁判比对后判定 wrong；
          漏掉的维度是 eco_win_rate。下次应当针对这些维度补一次下钻。」

**"下次应当针对这些维度补一次下钻"这句话是可执行价值的下限** —— 它没说：
- 这个维度是**没取到**（观测里根本没有）还是**取到了但取错**；
- 该补哪个工具；
- 已调的工具里有没有白调的。

而"没取到"和"取错"的补救动作**完全不同**：前者要补工具，后者要改口径。
把两者混成一句"补一次下钻"，模型就永远在错误的方向上重试 —— 这正是
`取证通路-原版Voyager对照.md` 里实测到的病（got=None 被当成答去比对）。

伴学怎么做
----------
伴学判完题回传的是**一包结构**，不是一句评语：
    verdict / score / error_type / feedback / next_action /
    covered_points / missing_points / misconceptions / step_feedback /
    related_topics / reference_answer / confidence

其中三件事是价值所在：
1. `error_type` —— 错在哪一类，决定补救方向；
2. `next_action` —— 下一步**具体**做什么；
3. `misconceptions` —— 记下"我以为 X"这个错误假设，而不只是"我错了"。

本模块的映射
------------
MVE 没有数学题的"错误类型"，但有自己这一套（全部来自 `loop_core.py`
判定时的实测后缀，不是编的）：

    unmeasured        模型交了 null —— 这个维度**不在观测里**（取证不全）
    missing_dimension 评分点对应的事实根本没交
    wrong_value       取到了但数不对（口径 / 过滤条件错）
    value_form        类型不可比（数值 vs 枚举）
    low_base          样本量不足被裁判拒收
    no_evidence       完全没有证据

`error_type → next_action` 是本模块的核心表：**每条都要能直接执行**。

另外：伴学判完会把标准答案 `reference_answer` 回传，因为它要的是"学会"。
MVE 的考核通道刻意不给数值（否则掌握度不可比），只给**编排序列**
（对应 `reference_plan`）—— 给做法，不给答案。

与旁路学习的分工
----------------
`bypass_learn.py` 提炼的技能带 `defect` / `strategy`（粗粒度：解决哪类缺陷）。
本模块的 `error_type` 是细粒度（这一轮具体错在哪）。两者用
`ERROR_TO_DEFECT` 对齐成同一套词汇，否则技能库里会出现两套叫法。
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# 词汇表：error_type（本轮错在哪，细）
# --------------------------------------------------------------------------
ERROR_TYPES: dict[str, str] = {
    "unmeasured": "没取到值（模型交了 null）—— 这个维度不在观测里，取证不全",
    "missing_dimension": "漏维度 —— 评分点对应的事实根本没交",
    "wrong_value": "值不符 —— 取到了但数不对（口径/过滤条件错）",
    "value_form": "值形态不可比 —— 类型对不上（数值 vs 枚举/字符串）",
    "low_base": "样本量不足，被裁判拒收",
    "no_evidence": "完全没有证据",
}

# --------------------------------------------------------------------------
# 词汇表：defect / strategy（技能库 blueprint 用，粗；与 bypass_learn 同一套）
# --------------------------------------------------------------------------
DEFECTS: dict[str, str] = {
    "missing_dimension": "漏维度",
    "wrong_granularity": "粒度/口径不对",
    "tool_uncovered": "工具没被用到",
    "value_form": "数值形态不对",
}
STRATEGIES: dict[str, str] = {
    "drill_down": "下钻一层",
    "switch_tool": "换工具",
    "combine_tools": "组合多工具",
    "read_scope": "读报告的 scope 元数据",
}

# 细 → 粗：保证 feedback 和 bypass_learn 说的是同一套缺陷
ERROR_TO_DEFECT: dict[str, str] = {
    "unmeasured": "tool_uncovered",
    "missing_dimension": "missing_dimension",
    "wrong_value": "wrong_granularity",
    "value_form": "value_form",
    "low_base": "wrong_granularity",
    "no_evidence": "tool_uncovered",
}

# 严重度：取最严重的那条当主 error_type（unmeasured 最该先修 —— 它意味着
# 后面的比对根本没发生，先补工具才谈得上对不对）
ERROR_SEVERITY: dict[str, int] = {
    "no_evidence": 5,
    "unmeasured": 4,
    "missing_dimension": 3,
    "wrong_value": 2,
    "value_form": 1,
    "low_base": 0,
}

# --------------------------------------------------------------------------
# 解析：missing 条目里带的原因后缀（loop_core.py 判定时刻进去的）
# --------------------------------------------------------------------------
_RE_UNMEASURED = re.compile(r"^(?P<point>.+?)\(未取到值[：:]?\s*(?P<got>[^)]*)\)\s*$")
_RE_WRONG = re.compile(r"^(?P<point>.+?)\(值不符[：:]?\s*(?P<got>.*?)[≠](?P<ref>.*?)\)\s*$")
_RE_FORM = re.compile(
    r"^(?P<point>.+?)\(值不可比[：:]?\s*got=(?P<got>.*?)<(?P<gottype>[A-Za-z_]+)>"
    r"[≠]ref=(?P<ref>.*?)<(?P<reftype>[A-Za-z_]+)>\)\s*$"
)
_RE_LOWBASE = re.compile(r"^(?P<point>.+?)\(样本不足.*\)\s*$")


@dataclass
class MissingItem:
    """一条缺失评分点 + 它为什么缺失。"""
    point: str
    dimension: str = ""
    error_type: str = "missing_dimension"
    got: str = ""
    ref: str = ""
    got_type: str = ""
    ref_type: str = ""

    @property
    def defect(self) -> str:
        return ERROR_TO_DEFECT.get(self.error_type, "missing_dimension")

    def to_dict(self) -> dict[str, Any]:
        # producers：图谱里"这个维度由哪个工具产出"。带上它，Voyager 才能判断
        # "点名的工具我试过没有" —— 没这条它只能瞎猜要不要求助。
        producers: list[str] = []
        g = graph()
        if g is not None:
            try:
                producers = list(g.who_produces(self.dimension or self.point))
            except Exception:
                producers = []
        return {
            "point": self.point, "dimension": self.dimension,
            "error_type": self.error_type, "defect": self.defect,
            "got": self.got, "ref": self.ref,
            "got_type": self.got_type, "ref_type": self.ref_type,
            "producers": producers,
        }


def parse_missing(entry: str, *, dim_by_point: dict[str, str] | None = None) -> MissingItem:
    """把 `xxx(值不符: 0≠59)` 这种条目拆成结构化的一条。

    后缀是判定时刻进去的（`loop_core.py:305/310/316`），所以这里是**读**不是猜。
    """
    e = str(entry or "").strip()
    dim_by_point = dim_by_point or {}

    for rx, etype in ((_RE_UNMEASURED, "unmeasured"),
                      (_RE_FORM, "value_form"),
                      (_RE_WRONG, "wrong_value"),
                      (_RE_LOWBASE, "low_base")):
        m = rx.match(e)
        if m:
            point = m.group("point").strip()
            item = MissingItem(point=point, dimension=dim_by_point.get(point, point),
                               error_type=etype)
            if etype == "unmeasured":
                item.got = (m.group("got") or "").strip() or "null"
            elif etype == "wrong_value":
                item.got = (m.group("got") or "").strip()
                item.ref = (m.group("ref") or "").strip()
            elif etype == "value_form":
                item.got = (m.group("got") or "").strip()
                item.ref = (m.group("ref") or "").strip()
                item.got_type = m.group("gottype")
                item.ref_type = m.group("reftype")
            return item

    point = e.split("(")[0].strip()
    return MissingItem(point=point, dimension=dim_by_point.get(point, point),
                       error_type="missing_dimension")


# --------------------------------------------------------------------------
# 知识图谱（lazy）：维度 → 由哪个工具产出
# --------------------------------------------------------------------------
_GRAPH: Any = None          # None=未加载 / False=不可用 / KnowledgeGraph=已加载


def graph() -> Any | None:
    """取知识图谱。读的是落盘的 json，**不 import vlml_env** ——
    离线重算（--topic 回放历史日志）时也必须能用。"""
    global _GRAPH
    if _GRAPH is None:
        try:
            import knowledge_graph
            _GRAPH = knowledge_graph.KnowledgeGraph.load()
        except Exception:
            _GRAPH = False
    return _GRAPH or None


def reset_graph() -> None:
    global _GRAPH
    _GRAPH = None


# --------------------------------------------------------------------------
# error_type → next_action（本模块的核心表：每条都要能直接执行）
# --------------------------------------------------------------------------
def _sql_recipe(dim: str, subject: dict[str, Any] | None = None) -> str:
    """SQL 路径维度的「该查什么」—— 图谱里存着，反馈却一直没给出去。

    **这是「跑再多轮也学不会」的根因**：
    `map_fb_conv`（Haven 图首血转换率）23 轮一次都没取到、
    `map_win_rate`（图上胜率）43 轮只取到 5 次。这两条都是 **SQL 路径**
    维度 —— 图谱 `who_produces` 为空，于是反馈里 `producers=[]`，
    模型每轮拿到的都是同一句「先确认哪个工具能出它，再补调」，
    **一句可执行的信息都没有**，所以第 43 轮和第 1 轮拿到的一样多。

    而图谱里其实存着完整的教学信息（`knowledge_graph.py:1258-1289`）：
    `tables` / `columns_by_table` / `semantics` / `typical_errors` ——
    事前渲染进了图谱提示，事后反馈一个字没用。这里补上。

    给的是**结构不是答案**（跟 `action_code` 那条路同一个边界）：
    表名、列名、易错点、口径形态；不含任何数值。
    """
    g = graph()
    if g is None:
        return ""
    node = g.nodes.get(f"dim:{dim}")
    det = dict(node.detail) if node else {}
    tables = [str(t) for t in (det.get("tables") or []) if str(t)]
    if not tables:
        return ""
    cols_map = det.get("columns_by_table") or {}
    cols = [str(c) for c in (det.get("columns") or [])]
    parts = [f"{dim} 是 **SQL 口径**（没有工具直接出它，得自己写 SQL）"]
    for t in tables[:2]:
        cs = [str(c) for c in (cols_map.get(t) or cols)]
        parts.append(f"表 `{t}`" + (f"，相关列 {'、'.join(cs[:8])}" if cs else ""))
    errs = [str(e) for e in (det.get("typical_errors") or [])][:2]
    if errs:
        parts.append("易错点：" + "；".join(errs))
    if isinstance(subject, dict) and subject:
        # subject 的每个键都必须在 WHERE 里出现 —— 这正是「漏查某张图」的解药
        keys = "、".join(f"{k}={v}" for k, v in subject.items() if v not in (None, ""))
        if keys:
            parts.append(f"这个评分点的范围是 {{{keys}}} —— WHERE 必须逐个带上，"
                         "不能只查整体再往下推算")
    sem = str(det.get("semantics") or "")
    if sem:
        parts.append(f"口径形态：{sem}")
    return "；".join(parts) + "。"


def _next_action(item: MissingItem, *, ref_plan: list[str] | None = None,
                 my_tools: list[str] | None = None,
                 subject: dict[str, Any] | None = None) -> str:
    dim = item.dimension or item.point
    plan = " → ".join(ref_plan or [])
    tools = "、".join(my_tools or []) or "（没调任何工具）"

    if item.error_type == "unmeasured":
        # 这是最该被修的一类：维度压根不在观测里，补口径没用，得补工具。
        #
        # 该补**哪个**工具，两个来源，优先图谱：
        #   1. 知识图谱的 produced_by（声明式：`answer_spec.tool` 配好的取数
        #      配方）—— 事前可查，裁判没跑过时也答得出来；
        #   2. 裁判轨迹差集（事后反推）—— 只在图谱没这条边时才用，且它说的是
        #      "裁判调了而我没有"，不是"本来就该由它出"。
        g = graph()
        producers: list[str] = []
        if g is not None:
            try:
                producers = [t for t in g.who_produces(dim) if t not in (my_tools or [])]
            except Exception:
                producers = []
        if producers:
            return (f"{dim} 不在观测里（我交了 null），不是算错 —— "
                    f"它**由 {'、'.join(producers)} 产出**（知识图谱声明），"
                    f"我这次没调这个工具。")
        gap = [t for t in (ref_plan or []) if t not in (my_tools or [])]
        if gap:
            return (f"{dim} 不在观测里（我交了 null），不是算错 —— "
                    f"要先补调一个能出它的工具；裁判调过而我没调的是 {'、'.join(gap)}"
                    + (f"（裁判完整编排 {plan}）。" if plan else "。"))
        return (f"{dim} 不在观测里（我交了 null），不是算错 —— "
                f"当前调的是 {tools}，它们不出这个维度，要换工具。")

    if item.error_type == "missing_dimension":
        g = graph()
        if g is not None:
            try:
                producers = [t for t in g.who_produces(dim)
                             if t not in (my_tools or [])]
                if producers:
                    return (f"{dim} 连 null 都没交 —— 它**由 {'、'.join(producers)} "
                            f"产出**（知识图谱声明），补调它。")
            except Exception:
                pass
        # 图谱说不出生产者 = 这是 SQL 路径维度。以前走到这里只给一句
        # 「先确认哪个工具能出它」—— 对 SQL 维度是句空话，实测 43 轮没变过。
        recipe = _sql_recipe(dim, subject)
        if recipe:
            return recipe
        tail = f"裁判的标准编排是 {plan}。" if plan else ""
        return f"{dim} 没有任何事实，连 null 都没交 —— 先确认哪个工具能出它，再补调。{tail}"

    if item.error_type == "wrong_value":
        return (f"{dim} 取到了但数不对（{item.got or '?'} ≠ {item.ref or '?'}）—— "
                f"这是口径/过滤条件的问题（时间范围、地图、队伍、回合类型），"
                f"**不是补工具能解决的**，回看调用参数。")

    if item.error_type == "value_form":
        return (f"{dim} 形态对不上（我给的是 {item.got or '?'}<{item.got_type}>，"
                f"裁判是 {item.ref or '?'}<{item.ref_type}>）—— "
                f"对齐答案形态（数值 vs 枚举/字符串），不是数值算错。")

    if item.error_type == "low_base":
        return f"{dim} 样本量不足被拒收 —— 放宽过滤条件或换更大口径再取。"

    return f"{dim}：整轮没有拿到任何证据（当前工具 {tools}），先重建取证通路。"


def _strategy_for(defect: str, *, ref_plan: list[str] | None = None,
                  my_tools: list[str] | None = None) -> str:
    """缺陷 → 修补策略（与 bypass_learn 同一套四个）."""
    if defect == "tool_uncovered":
        # 裁判用了多个工具而我只用了一个 → 组合；否则换工具
        if ref_plan and my_tools and len([t for t in ref_plan if t not in my_tools]) >= 1 \
                and len(set(ref_plan)) > 1:
            return "combine_tools"
        return "switch_tool"
    if defect == "missing_dimension":
        return "drill_down"
    if defect == "value_form":
        return "read_scope"
    return "drill_down"


# --------------------------------------------------------------------------
# 反馈包
# --------------------------------------------------------------------------
@dataclass
class Feedback:
    """一包判题反馈（字段对齐伴学 `answer_evaluate` 的返回）。"""
    verdict: str = ""
    score: int = 0
    coverage: float = 0.0
    error_type: str = ""                       # 主错误类型（最严重的）
    error_types: list[str] = field(default_factory=list)
    feedback: str = ""                         # 人话评语
    next_action: str = ""                      # 可执行的下一步
    lesson: str = ""                           # 可固化的做法（只在判对时非空）
    covered_points: list[str] = field(default_factory=list)
    missing_points: list[str] = field(default_factory=list)   # 干净的评分点名
    missing_detail: list[dict[str, Any]] = field(default_factory=list)
    misconceptions: list[str] = field(default_factory=list)   # "我以为 X"
    step_feedback: list[dict[str, Any]] = field(default_factory=list)
    related_topics: list[str] = field(default_factory=list)   # 供出题器选下一题
    reference_plan: list[str] = field(default_factory=list)   # 编排示范（不给数值）
    my_plan: list[str] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict, "score": self.score,
            "coverage": round(self.coverage, 4),
            "error_type": self.error_type, "error_types": self.error_types,
            "feedback": self.feedback, "next_action": self.next_action,
            "lesson": self.lesson,
            "covered_points": self.covered_points,
            "missing_points": self.missing_points,
            "missing_detail": self.missing_detail,
            "misconceptions": self.misconceptions,
            "step_feedback": self.step_feedback,
            "related_topics": self.related_topics,
            "reference_plan": self.reference_plan, "my_plan": self.my_plan,
            "confidence": round(self.confidence, 4),
        }


def build_feedback(
    *,
    verdict: str = "",
    score: int = 0,
    coverage: float = 0.0,
    covered: list[str] | None = None,
    missing: list[str] | None = None,
    rejected_low_base: list[str] | None = None,
    dim_by_point: dict[str, str] | None = None,
    ref_plan: list[str] | None = None,
    my_plan: list[str] | None = None,
    confidence: float = 0.0,
    subj_by_point: dict[str, dict[str, Any]] | None = None,
) -> Feedback:
    """从判定结果组装一包反馈。纯函数：同样的输入永远给同样的输出。

    `subj_by_point`：评分点 → subject。SQL 路径维度漏掉时，要靠它说清
    「WHERE 必须带上 map=Haven 这种过滤」—— 没有它反馈就是空话。
    """
    subj_by_point = subj_by_point or {}
    covered = [str(c) for c in (covered or [])]
    missing_raw = [str(m) for m in (missing or [])]
    dim_by_point = dim_by_point or {}
    ref_plan = [str(t) for t in (ref_plan or []) if not str(t).startswith("<")]
    my_plan = [str(t) for t in (my_plan or []) if not str(t).startswith("<")]

    items = [parse_missing(m, dim_by_point=dim_by_point) for m in missing_raw]
    # 样本不足被拒收的单独补进来 —— 判定把它们放 rejected_low_base，不在 missing 里
    for p in (rejected_low_base or []):
        point = str(p).split("(")[0].strip()
        items.append(MissingItem(point=point, dimension=dim_by_point.get(point, point),
                                 error_type="low_base"))

    fb = Feedback(
        verdict=str(verdict), score=int(score), coverage=float(coverage),
        covered_points=covered,
        missing_points=[i.point for i in items],
        missing_detail=[i.to_dict() for i in items],
        reference_plan=ref_plan, my_plan=my_plan, confidence=float(confidence),
    )

    if not items:
        fb.error_type = ""
        fb.error_types = []
        fb.feedback = f"覆盖了裁判的全部 {len(covered)} 个评分点，编排方式有效。"
        fb.next_action = ("这套编排可以固化 —— 把它按『工具名+按什么维度拆分』"
                          "写进技能库，下次同类题直接复用。")
        # lesson 必须是**这套具体编排**，不能是"先取整体报告再下钻"那种模板 ——
        # 模板写进技能库等于什么都没写（它就是这么来的：voyager.py:1048 的模板）。
        dims = sorted({dim_by_point.get(c, c) for c in covered})
        if my_plan:
            fb.lesson = (
                f"编排 {' → '.join(my_plan)}：先取 {my_plan[0]} 拿总盘"
                + (f"，再用 {'、'.join(my_plan[1:])} 补齐" if len(my_plan) > 1 else "")
                + f"。覆盖 {len(covered)} 个评分点"
                + (f"（{'、'.join(dims)}）" if dims else "")
                + "。")
        else:
            fb.lesson = f"未调工具即覆盖 {len(covered)} 个评分点（可疑，检查是否用了缓存事实）。"
        fb.step_feedback = _step_feedback(my_plan, ref_plan, [])
        fb.related_topics = dims
        return fb

    # 主错误类型 = 最严重的那条
    ranked = sorted(items, key=lambda i: -ERROR_SEVERITY.get(i.error_type, 0))
    fb.error_types = sorted({i.error_type for i in items},
                            key=lambda t: -ERROR_SEVERITY.get(t, 0))
    fb.error_type = ranked[0].error_type

    # next_action：按严重度排，最多给两条（给多了等于没给）
    tops = ranked[:2]
    fb.next_action = " ".join(
        _next_action(i, ref_plan=ref_plan, my_tools=my_plan,
                     subject=subj_by_point.get(i.point)) for i in tops
    )

    # misconceptions：把"我以为这样编排就能覆盖"这个错误假设挑明
    unmeasured = [i for i in items if i.error_type == "unmeasured"]
    if unmeasured:
        tools = "、".join(my_plan) or "（没调任何工具）"
        dims = "、".join(i.dimension or i.point for i in unmeasured)
        fb.misconceptions.append(
            f"我调了 {tools} 就以为能覆盖 {dims} —— 实际这些维度不在这些工具的输出里。"
            f"（已调用过但仍取不到，说明不是忘调，是这两个工具不出这个维度）")
    wrong = [i for i in items if i.error_type == "wrong_value"]
    if wrong:
        fb.misconceptions.append(
            "我以为取到的数就是裁判口径下的数 —— 实际过滤条件/范围不一致："
            + "、".join(f"{i.dimension or i.point}({i.got}≠{i.ref})" for i in wrong))
    form = [i for i in items if i.error_type == "value_form"]
    if form:
        fb.misconceptions.append(
            "我以为答案就是这个形态 —— 实际裁判要的是另一套："
            + "、".join(f"{i.dimension or i.point}({i.got_type}≠{i.ref_type})" for i in form))

    n_by_type: dict[str, int] = {}
    for i in items:
        n_by_type[i.error_type] = n_by_type.get(i.error_type, 0) + 1
    fb.feedback = (
        f"判定 {verdict}（覆盖率 {coverage:.0%}）："
        + "；".join(f"{ERROR_TYPES.get(t, t)} ×{n}" for t, n in
                   sorted(n_by_type.items(), key=lambda kv: -kv[1]))
        + f"。已覆盖 {len(covered)} 个评分点。"
    )

    fb.step_feedback = _step_feedback(my_plan, ref_plan, items)
    fb.related_topics = sorted({i.dimension or i.point for i in items})
    return fb


def _step_feedback(my_plan: list[str], ref_plan: list[str],
                   items: list[MissingItem]) -> list[dict[str, Any]]:
    """逐步反馈：每个工具这一步到底起了什么作用。

    伴学的 `step_feedback` 是逐步骤说"这一步对不对"。MVE 的"步骤"就是工具调用，
    能说的是：调了它之后，哪些维度进来了、哪些还是没进来、以及哪些工具压根没调。
    """
    out: list[dict[str, Any]] = []
    covered_dims = set()
    for t in my_plan:
        out.append({"tool": t, "used": True, "note": ""})
    for t in ref_plan:
        if t not in my_plan:
            out.append({"tool": t, "used": False,
                        "note": "裁判调了它，我没调 —— 缺失的维度很可能从这里出"})

    # 把"调了但没出"标到具体工具上：只标最后一个（前面的不能确定）
    dims_missing = {i.dimension or i.point for i in items
                    if i.error_type in ("unmeasured", "missing_dimension")}
    if out and dims_missing:
        last_used = [o for o in out if o["used"]]
        if last_used:
            last_used[-1]["note"] = (
                f"调了它，但 {'、'.join(sorted(dims_missing))} 仍未取到 "
                f"—— 这些维度不由它出，需要换/加工具")
    _ = covered_dims
    return out


def blueprint_for(fb: Feedback) -> dict[str, Any] | None:
    """把反馈包压成技能库能带的 blueprint（缺陷 → 策略 → 动作）。

    与 `bypass_learn.py` 产出的 blueprint 同构，只是 `source` 不同：
    旁路学的是裁判的做法（`bypass`），这里是自己判对后固化下来的（`practice`）。
    """
    if not fb.error_type and not fb.missing_detail:
        # 判对：固化的是"这套编排有效"
        return {
            "defect": "", "strategy": "combine_tools" if len(fb.my_plan) > 1 else "drill_down",
            "trajectory": fb.my_plan,
            "dimensions": [d for d in fb.related_topics],
            "source": "practice",
        }
    defect = ERROR_TO_DEFECT.get(fb.error_type, "missing_dimension")
    return {
        "defect": defect,
        "strategy": _strategy_for(defect, ref_plan=fb.reference_plan, my_tools=fb.my_plan),
        "trajectory": fb.reference_plan or fb.my_plan,
        "dimensions": [d.get("dimension") for d in fb.missing_detail],
        "source": "practice",
    }


def render_critique(fb: Feedback) -> str:
    """把反馈包渲染成给模型的 critique 文本（进下一轮 prompt）。

    照 fork 的 critic 形态：**第一人称自我反思**。但不再是一句模板 ——
    带上 error_type 的归因和 next_action 的具体动作。
    """
    if not fb.error_type:
        return (f"我的事实集覆盖了裁判的全部 {len(fb.covered_points)} 个评分点，"
                f"编排方式有效。{fb.next_action}")
    head = f"我以为这样编排就能覆盖全部评分点，但裁判比对后判定 {fb.verdict}。"
    body = fb.feedback
    mis = (" ".join(f"错误假设：{m}" for m in fb.misconceptions)) if fb.misconceptions else ""
    act = f"下一步：{fb.next_action}"
    return " ".join(x for x in (head, body, mis, act) if x)


# --------------------------------------------------------------------------
# 离线重算：从 run_log 的一行重建反馈（用于验证"结构说得对不对"）
# --------------------------------------------------------------------------
def dim_map_for(topic_id: str) -> dict[str, str]:
    """评分点原文 → dimension。

    离线重算必须补这个映射：run_log 里存的 missing 是**评分点原文**
    （例如「eco 局胜率（0-100 的百分比数值，如 42.9）」），直接拿来当维度名
    根本没法读；真正的维度名在 rubric 里（`eco_win_rate`）。
    """
    try:
        from tasks import TASKS
    except Exception:
        return {}
    task = TASKS.get(str(topic_id or ""))
    if task is None:
        return {}
    return {p.point: p.dimension for p in getattr(task, "rubric", [])}


def from_row(row: dict[str, Any]) -> Feedback:
    """用 run_log 的一行重建反馈包 —— 不重跑模型，只重算结构。"""
    topic = str(row.get("topic_id") or "")
    return build_feedback(
        verdict=str(row.get("verdict") or ""),
        score=int(row.get("score") or 0),
        coverage=float(row.get("coverage") or 0.0),
        covered=[str(c) for c in (row.get("covered") or [])],
        missing=[str(m) for m in (row.get("missing") or [])],
        rejected_low_base=[str(p) for p in (row.get("rejected_low_base") or [])],
        dim_by_point=dim_map_for(topic),
        ref_plan=[str(t) for t in (row.get("ref_plan") or [])],
        my_plan=[str(t) for t in (row.get("trajectory") or [])],
        confidence=float(row.get("evaluator_confidence") or 0.0),
    )


def _main() -> int:
    ap = argparse.ArgumentParser(description="判题反馈结构：离线重算 + 回放")
    ap.add_argument("--topic", default="", help="只看这道题；不传则看全部")
    ap.add_argument("--limit", type=int, default=5, help="回放最近几条")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = ap.parse_args()

    import run_log
    rows = run_log.load_all()
    if args.topic:
        rows = [r for r in rows if str(r.get("topic_id") or "") == args.topic]
    rows = rows[-args.limit:]
    if not rows:
        print("没有可回放的日志")
        return 1

    for r in rows:
        fb = from_row(r)
        if args.json:
            print(json.dumps(fb.to_dict(), ensure_ascii=False))
            continue
        print(f"[{r.get('ts')}] {r.get('topic_id')} · {fb.verdict} · "
              f"覆盖率 {fb.coverage:.0%}")
        print(f"  判据      : {fb.feedback}")
        if fb.error_type:
            print(f"  错误类型  : {fb.error_type}  （{ERROR_TYPES.get(fb.error_type, '')}）")
        print(f"  下一步    : {fb.next_action}")
        for m in fb.misconceptions:
            print(f"  错误假设  : {m}")
        print(f"  我的编排  : {' → '.join(fb.my_plan) or '（无）'}")
        print(f"  裁判编排  : {' → '.join(fb.reference_plan) or '（无）'}")
        for s in fb.step_feedback:
            flag = "✓调了" if s["used"] else "✗没调"
            print(f"  步骤      : {flag} {s['tool']}"
                  + (f" —— {s['note']}" if s["note"] else ""))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
