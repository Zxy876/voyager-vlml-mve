#!/usr/bin/env python3
"""旁路学习：不通过做题，直接观摩裁判（VLML0）的标准解法来积累技能。

为什么要有这条旁路
------------------
人类学习者有两种积累能力的方式：做题（被考核），和不做题的学习（看例题、
看讲解、看标准解法）。猫娘伴学里后者是**能力模块**（`adaptive_learning/
cognitive_catalog.py` 的 blueprint + `cognitive_delivery.py` 的投递）：
它独立于做题，按「认知缺陷 → 修补策略 → 具体动作」把能力送到学习者面前。

MVE 之前只有做题一条路，而做题这条路上有个死结：

    判错时不写技能库 —— 这次的做法已被裁判证明是错的，写进去就是埋雷
    （`voyager.py:1103-1114`，这个决定是对的）

于是学习**只可能发生在判对时**，而判对时写的又是一条模板
（`voyager.py:1048-1050`）。结果：技能库永远只有一两条，还没什么内容。

旁路把这个死结解开：**旁路上学的不是 Voyager 自己的做法，而是裁判的做法** ——
裁判的做法是确定性层算出来的、已经被验证正确的，可以放心固化。

与考核通道的分工（这是伴学给的关键区分）
----------------------------------------
伴学判题后会**把标准答案回传给学习者**（`tutor_llm_agent_answer_evaluate.py:84`
与 `:123` 的 `reference_answer`）—— 因为它的目的是"学会"，不是"考过"。
MVE 的 `learn()` 则刻意不给数值（`voyager.py:1068`），因为它要的是可比的掌握度。

两者都对，只是通道不同：
    考核通道（做题）  —— 不给答案，判覆盖率，进掌握度
    旁路通道（本模块）—— 给标准解法，不判分，只进技能库

跑法
----
    python mve/bypass_learn.py --topic pistol_eco_pattern
    python mve/bypass_learn.py --topic series_totals --force   # 重跑裁判
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import vlml_env  # noqa: F401,E402  必须先引导环境

import causal_timeline  # noqa: E402
import skill_store  # noqa: E402
import vlml0_referee  # noqa: E402
from llm_client import chat_json  # noqa: E402
from tasks import TASKS  # noqa: E402

BYPASS_SOURCE = "bypass"


def _prompt(task: Any, ref: Any, traj: list[str], dims: list[str]) -> str:
    rubric = "\n".join(f"- {p.point}｜dimension=`{p.dimension}`" for p in task.rubric)
    return f"""你是一名分析 agent，正在**观摩**一份标准解法，准备把它变成自己能复用的做法。

题目：{task.question}

需要覆盖的评分点：
{rubric}

标准解法（已被验证正确）用的工具序列：{' → '.join(traj) or '（无）'}
它取到的维度：{', '.join(dims) or '（无）'}

请从中提炼一条**下次遇到同类题目时能用上**的做法，输出 JSON：
{{"name": "技能名，格式固定为『工具名+按什么维度拆分』，例如 match_summary_report按map下钻",
  "lesson": "一到两句话，只写可复用的做法：该调哪个工具、按什么维度拆分、先取哪层再下钻哪层。不要复述题目，不要写具体数值",
  "defect": "这条做法解决的是哪一类缺陷，从这几个里选一个：missing_dimension（漏维度）/ wrong_granularity（粒度不对）/ tool_uncovered（工具没被用到）/ value_form（数值形态不对）",
  "strategy": "修补策略，从这几个里选一个：drill_down（下钻一层）/ switch_tool（换工具）/ combine_tools（组合多工具）/ read_scope（读报告的 scope 元数据）"}}

命名规则很重要：同名技能会被覆盖而不是新增，所以名字要稳定、能概括做法，
不要带轮次、题目原文或具体数值。"""


def _solution_code(task: Any) -> str:
    """把 VLML0 的标准解法变成**可学的源码** —— 这就是原版的 `program_code`。

    原版 `add_new_skill` 存的是 `info["program_code"]`：Voyager 自己写出来的、
    跑通了的那段 JS。MVE 的 Voyager 不产出代码，但这里有一个原版没有的东西：
    **VLML0 的标准解法是确定的、已被验证正确的**（服务端 answer_spec + 裁判
    轨迹）—— 它天然就是"正确的源码"。所以映射关系变成：

        原版：自己写 → 跑通 → 存源码
        MVE ：判错 → 观摩 VLML0 → 存它的解法源码

    脱敏边界（与知识图谱同一条原则：**给口径、给结构，不给答案**）：
      - SQL 走 `_desensitize`，字符串字面量一律抹成 `'?'`；
      - 工具调用的实参值同样抹掉，只留参数名与形态。
    这样模型学到的是"该查哪张表、怎么分层、取第几列 / 取哪条路径"，
    而不是"答案是多少" —— 数值仍然必须它自己查出来。
    """
    try:
        from knowledge_graph import _desensitize
    except Exception:                                   # pragma: no cover
        def _desensitize(s: str) -> str:                # type: ignore[misc]
            return s or ""

    lines: list[str] = []
    for p in getattr(task, "rubric", None) or []:
        spec = getattr(p, "answer_spec", None)
        if spec is None:
            continue
        dim = str(getattr(p, "dimension", "") or "")
        point = str(getattr(p, "point", "") or "")
        sql = str(getattr(spec, "sql", "") or "")
        tool = str(getattr(spec, "tool", "") or "")
        if sql:
            body = _desensitize(sql).strip()
            tail = f"取第 {getattr(spec, 'value_column', 0)} 列"
            base = getattr(spec, "base_column", None)
            if base is not None:
                tail += f"，分母第 {base} 列"
            if getattr(spec, "percent", False):
                tail += "（百分比：num/denom*100）"
            lines.append(f"[{dim}] {point}\n    {body}\n    # {tail}")
        elif tool:
            args = getattr(spec, "tool_args", None) or {}
            sig = ", ".join(f"{k}='?'" for k in (args or {})) or ""
            path = str(getattr(spec, "value_path", "") or "")
            base = str(getattr(spec, "base_path", "") or "")
            if getattr(spec, "percent", False):
                # 百分比口径必须**同时给分子与分母两条路径**。
                # 实测踩过：源码里只写了 `...kast.num`，模型照抄 dig 到 109
                # 就交上来了 —— 它不知道还要除 denom。给一条路径等于让它算一半，
                # 而这个"一半"看起来完全像个正常的数（不像 None 那样能自证）。
                num_path = path or base
                den_path = base or (
                    path[: -len(".num")] + ".denom" if path.endswith(".num") else "")
                lines.append(
                    f"[{dim}] {point}\n    {tool}({sig})\n"
                    f"    # 百分比：取两个数再相除\n"
                    f"    #   num   = dig(p, \"{num_path}\")\n"
                    f"    #   denom = dig(p, \"{den_path}\")\n"
                    f"    #   返回 {{\"num\": num, \"denom\": denom}}（不要自己算好再返回）")
            else:
                # 标量指标。实测踩过一次，必须点破「标量 ≠ num/denom」：
                #   kast 与 kd 同在 key_metrics.team.consistency 下（kast 是
                #   {num,denom}，kd 是标量 1.24），长得太像。此前这里只写一句
                #   「按路径取值」，模型照抄时把上一行 kast 的 num/denom 套路
                #   套到 kd 上，取成 109/109 = 1.0 —— 连着 48 轮停在 50%。
                #   只给路径不足以阻止套错，必须显式写清"不要取 .num/.denom"。
                lines.append(
                    f"[{dim}] {point}\n    {tool}({sig})\n"
                    f"    # 标量：dig 到的就是最终值，不用再算\n"
                    f"    #   value = dig(p, \"{path}\")\n"
                    f"    #   注意：这个指标**不是** num/denom 结构 ——\n"
                    f"    #   不要去取 .num / .denom，也不要拿别的指标的\n"
                    f"    #   num/denom 去除出它，直接返回原值")
    return "\n".join(lines)


async def learn_from_referee(topic_id: str, *, force: bool = False) -> dict[str, Any]:
    """观摩裁判答这道题 → 提炼一条可复用做法 + **存下它的解法源码** → 写进技能库。"""
    if topic_id not in TASKS:
        return {"ok": False, "error": f"未知题目 {topic_id}"}
    task = TASKS[topic_id]

    ref = await vlml0_referee.answer(task, force=force)
    if not getattr(ref, "facts", None):
        return {"ok": False, "error": "裁判没有产出事实，无从观摩"}

    dims = [str(f.get("dimension")) for f in ref.facts]
    # 内部标记不能进技能库：实测 Voyager 会把 "<deterministic-sql>" 当真工具名
    traj = [str(t) for t in (getattr(ref, "trajectory", None) or [])
            if not str(t).startswith("<")]

    out = chat_json(
        [
            {"role": "system", "content": "你是会把标准解法提炼成可复用做法的分析 agent。"},
            {"role": "user", "content": _prompt(task, ref, traj, dims)},
        ],
        temperature=0.2,
    )
    name = str(out.get("name") or "").strip()
    lesson = str(out.get("lesson") or "").strip()
    if not lesson:
        return {"ok": False, "error": "模型没有提炼出做法"}

    blueprint = {
        "defect": str(out.get("defect") or "").strip(),
        "strategy": str(out.get("strategy") or "").strip(),
        "trajectory": traj,
        "dimensions": dims,
        "source": BYPASS_SOURCE,
    }
    code = _solution_code(task)
    action, key = skill_store.add(
        topic_id, name or f"{topic_id}标准编排", lesson,
        source=BYPASS_SOURCE, blueprint=blueprint, code=code,
    )

    causal_timeline.append(
        causal_timeline.CONTROL,
        topic_id=topic_id,
        summary=f"旁路学习：观摩裁判解法 → {action} {name or topic_id}"
                f"（已存解法源码 {len(code)} 字符）",
        detail={"trajectory": traj, "dimensions": dims, "action": action,
                "code_len": len(code),
                # 只记长度不记全文：源码里含服务端配方，落盘即泄题。
                # 要查全文去技能库看，别在日志里复制一份。
                "validated_target": False},
    )
    return {
        "ok": True, "topic_id": topic_id, "action": action, "key": key,
        "name": name, "lesson": lesson, "blueprint": blueprint,
        "code": code, "code_len": len(code),
        "trajectory": traj, "dimensions": dims,
    }


RETRY_SOURCE = "help"           # 判错后求助 → 掌握度按"借助帮助"计

# 讲解的四段结构，直接照伴学 _solution_structure.py:173 的 headings：
#   题目解析 / 解题过程 / 答案 / 举一反三
# 注意第三段是**答案** —— 伴学的讲解里答案是可见的
# （settings 说明写得很直白："仅讲述题目解析、答案和举一反三"）。
#
# 之前我把这里设计成"只讲做法不给数值"，理由是不能泄题。那是把两件事混了：
#   伴学的「显示提示」(ui.button.show_hint → main.js:3444 置 used_hint)
#     —— 做题途中点开，内容是方法/知识点（如"记住：180° 等于 π 弧度"），**不含本题答案**
#   伴学的「讲解」(solution structure，四段含答案)
#     —— 是**学习环节**，先讲透（含答案）再去练
# 伴学的默认路径见 onboarding.md:47「从讲解进入练习」—— 先学（答案可见），再练。
# MVE 只有"做题"一条路，把求助塞进做题中途，于是做成了两头不靠的半截提示：
# 对解题没帮助（模型还是不知道该取什么值/什么口径），又不像讲解那样能学到东西。
# 实测后果：corrode_collapse 求助三次，下一轮反而出现"未调用工具"、覆盖率 0%。
STUDY_SECTIONS = ("题目解析", "解题过程", "答案", "举一反三")


def _is_real_tool(name: str) -> bool:
    """这个工具名是不是 Voyager 真能调的 MCP 工具。

    裁判的标准解法里混着它自己的内部工具（vlml0_referee::deterministic 等），
    那些是裁判算数用的，Voyager 调不到。讲给 Voyager 听之前必须先分清，
    否则它照着去查一个不存在的表。
    """
    try:
        from action_code import TOOL_NAMES
        return str(name) in TOOL_NAMES
    except Exception:
        # 拿不到工具名单时退回：至少排除带前缀的内部名
        return "::" not in str(name) and not str(name).startswith("vlml0")


def _explain_prompt(task: Any, *, traj: list[str], dims_by_tool: dict[str, list[str]],
                    missing: list[str], my_plan: list[str],
                    facts_text: str, graph_text: str = "") -> str:
    mapping = "\n".join(
        f"- {tool} → {', '.join(dims) or '（无）'}" for tool, dims in dims_by_tool.items()
    ) or "（无）"
    # 讲解的口径必须以知识图谱为准 —— 否则会出现"图谱说 A、讲解说 B"，
    # 它下一轮不知道听谁的。图谱里没有的维度才由讲解自由发挥。
    gblock = f"""
【知识图谱 · 本题的口径】讲解必须与下面一致（这是服务端配方，不是参考意见）：
{graph_text}
""" if graph_text else ""
    return f"""你是一名讲解者，正在给一名刚做错这道题的分析 agent **讲一遍标准解法**。

题目：{task.question}

它这次的编排：{' → '.join(my_plan) or '（没调任何工具）'}
它没拿到的维度：{', '.join(missing) or '（无）'}

标准解法的编排：{' → '.join(traj) or '（无）'}
每个工具负责哪些维度：
{mapping}

**重要**：编排里凡是被标成「（裁判内部，不是可调工具）」的都是裁判自己算数用的
内部结构，**它调不到**。讲"解题过程"时必须只提它真能调的工具，
否则它会照着去查一个不存在的表（实测就栽在这：讲解里写了
`vlml0_referee.deterministic`，它下一轮真去 FROM 这张表 → Catalog Error）。

标准解法取到的**真实数值**（裁判确定性层算出来的，已经验证过，可以直接讲）：
{facts_text or '（无）'}
{gblock}
请按四段讲，输出 JSON：
{{"analysis": "题目解析：这道题到底在问什么、评分点之间的依赖关系",
  "process": "解题过程：先调什么、拿到什么、为什么还要再调下一个；尤其讲清**口径**（过滤条件、分组方式、顺序），这正是它做错的地方",
  "answer": "答案：把上面那些数值按评分点组织好，写清楚每个值是多少",
  "transfer": "举一反三：下次遇到同类题，一眼该看什么、先动哪个工具"}}

要求：
- process 里必须讲清**口径**（怎么过滤、按什么分组、按什么排序），不要只写工具名。
- 上面给了知识图谱时，口径、列名、取值路径一律**照图谱讲**，不要另写一套。
- answer 里的数值只能取自上面给定的真实数值，一个都不许自己编。
- 这是**学习环节**，讲透是目的；它下一轮重做时要自己调工具取证，抄不进去。"""


def _graph_text(task: Any) -> str:
    """本题的知识图谱子图 —— 讲解的口径必须与规划时看到的是同一份。

    为什么连讲解也要带：之前图谱只进规划 prompt，讲解靠裁判轨迹自由发挥，
    两者可能给出不同口径（"图谱说用 AVG(fb_team_won)、讲解说用 COUNT"）。
    它下一轮同时看到两份，只能再猜一次。
    """
    try:
        from voyager import graph_context
        # mode="explain"：讲解/求助环节**不喂易混维度**（confusable / co_occurs）。
        # 照伴学的 response_mode 分流（knowledge_graph_guidance.py:1302-1310）——
        # general_explanation 只留 prerequisite/application/supports/extends/confusable，
        # 讲解时给"容易和什么混"会把求助变成诱导；规划时才需要它当护栏。
        return graph_context(task, mode="explain")
    except Exception:
        return ""


async def explain_for_retry(
    task: Any,
    *,
    my_plan: list[str] | None = None,
    missing: list[str] | None = None,
) -> dict[str, Any]:
    """判错后求助：**先讲一遍标准解法（学习环节），再带着它去重做**。

    照伴学的通道分工
    ----------------
    伴学把"学到"和"考过"分成两条路（onboarding.md:47「从讲解进入练习」）：

        讲解（学习环节）—— 四段：题目解析 / 解题过程 / **答案** / 举一反三
                          答案可见，因为目的是学会（`_solution_structure.py:173`）
        练习（考核环节）—— 自己答，答错才给方法提示（不含答案）；
                          点过提示就在掌握度上记一笔 used_hint（0.85 折）

    MVE 的同构：
        学习环节（本函数）—— 给完整讲解，数值取自裁判的确定性事实，可以讲
        重做环节（下一轮）—— prompt 只带做法（编排 + 每工具负责哪些维度），
                          数值不回灌；值还得它自己调工具取

    为什么数值敢给：MVE 有**溯源校验**（`loop_core` 的 hallucination 拦截），
    抄一个没调工具取来的数，事实溯源不通过，覆盖率照样不涨 ——
    抄答案占不到便宜，但能真正学到口径（这正是 corrode_collapse 缺的东西）。
    """
    ref = await vlml0_referee.answer(task)
    if not getattr(ref, "facts", None):
        return {"ok": False, "error": "裁判没有产出事实，无从解释"}

    traj = [str(t) for t in (getattr(ref, "trajectory", None) or [])
            if not str(t).startswith("<")]
    dims_by_tool: dict[str, list[str]] = {}
    for f in ref.facts:
        src = str((f.get("source") or {}).get("tool") or "")
        tool = src.split("::", 1)[1] if "::" in src else (src or "deterministic-sql")
        # 裁判的内部工具（如 vlml0_referee::deterministic）**不是** Voyager 能调的
        # MCP 工具。不标出来，讲解就会把它写成"下一步调它" ——
        # 实测：模型照着去 FROM vlml0_referee.deterministic → Catalog Error，
        # 求助反而把这一轮做空。所以这里显式改名 + prompt 里明令禁止。
        if not _is_real_tool(tool):
            tool = f"{tool}（裁判内部，不是可调工具）"
        dims_by_tool.setdefault(tool, [])
        d = str(f.get("dimension") or "")
        if d and d not in dims_by_tool[tool]:
            dims_by_tool[tool].append(d)

    my_plan = [str(t) for t in (my_plan or [])]
    missing = [str(m) for m in (missing or [])]

    # 讲解要讲的数值：只取裁判确定性层算出来的真值，不取模型编的
    facts_text = "\n".join(
        f"- {f.get('dimension')} = {f.get('value')}"
        + (f"（分母 {f.get('base')}）" if f.get("base") else "")
        for f in ref.facts
    )
    out = chat_json(
        [
            {"role": "system",
             "content": "你是会把标准解法讲透的讲解者：讲清口径，答案只能引用给定真值。"},
            {"role": "user", "content": _explain_prompt(
                task, traj=traj, dims_by_tool=dims_by_tool,
                missing=missing, my_plan=my_plan, facts_text=facts_text,
                graph_text=_graph_text(task))},
        ],
        temperature=0.2,
    )
    study = {
        "analysis": str(out.get("analysis") or "").strip(),
        "process": str(out.get("process") or "").strip(),
        "answer": str(out.get("answer") or "").strip(),
        "transfer": str(out.get("transfer") or "").strip(),
    }
    explain = study["process"] or study["analysis"]
    lesson = study["transfer"]
    if not any(study.values()):
        return {"ok": False, "error": "模型没有产出讲解"}

    causal_timeline.append(
        causal_timeline.REFEREE,
        topic_id=getattr(task, "topic_id", ""),
        summary=f"学习标准解法（VLML0，含答案）：{explain[:52] or study['answer'][:52]}",
        detail={"trajectory": traj, "dims_by_tool": dims_by_tool,
                "my_plan": my_plan, "missing": missing,
                "study": study, "with_answer": True,
                "used_hint": True, "validated_target": False},
    )
    return {
        "ok": True,
        "topic_id": getattr(task, "topic_id", ""),
        # 兼容旧字段：explain / lesson 现在指"解题过程"和"举一反三"
        "explain": explain, "lesson": lesson,
        "study": study, "sections": list(STUDY_SECTIONS),
        "plan": traj, "dims_by_tool": dims_by_tool,
        # 重做时**不**回灌这个：值要它自己取
        "facts": [
            {"dimension": f.get("dimension"), "value": f.get("value"),
             "base": f.get("base")}
            for f in ref.facts
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="旁路学习：观摩裁判解法积累技能")
    ap.add_argument("--topic", required=True, help=f"题目 id，可选：{', '.join(TASKS)}")
    ap.add_argument("--force", action="store_true", help="重跑裁判（忽略缓存）")
    args = ap.parse_args()

    res = asyncio.run(learn_from_referee(args.topic, force=args.force))
    if not res.get("ok"):
        print(f"旁路学习失败：{res.get('error')}")
        return 1
    print(f"题目      : {res['topic_id']}")
    print(f"标准编排  : {' → '.join(res['trajectory']) or '（无）'}")
    print(f"取到维度  : {', '.join(res['dimensions'])}")
    print(f"技能写入  : {res['action']} · {res['name']}")
    print(f"          {res['lesson']}")
    print(f"蓝图      : 缺陷={res['blueprint']['defect']} · 策略={res['blueprint']['strategy']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
