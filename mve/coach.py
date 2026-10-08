#!/usr/bin/env python3
"""人导入问题 → VLML 解释回答（coach 通路）。

这条通路**不经过 Voyager**：

    人导入问题 → 原版 VLML 编排取数 → LLM 解释 → 记忆（进时间线）
                                              → 反哺出题器

照伴学的两条硬规矩：

1. 「解释输出」的供应方是 VLML + 其系统 LLM，**不是让 Voyager 跑的**
   （助产士第三轮的清单：解释输出 = 替换件）。

2. 人导入的题 `validated_target = False`
   照 `target_binding.py:22-31` 的判定链——只有同时满足
     question_source == "current_question"
     source == "targeted_question"
     target_binding.validation_status == "passed"
     generated_at 非空
     bound_topic_id == 当前选中题
   才是 True（即出题器产出）。人导入的题一条都不满足 → False。

   False 的后果（`practice_outcome.py:58-65`）：
     **能批改出 verdict，但 mastery_status 强制 insufficient_evidence**
   → 「能批改」和「能评掌握度」被一个布尔量切开。

3. 不进掌握度 ≠ 什么都没发生：它写进行动因果时间线（control fact），
   出题器读这条线决定下一题。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import vlml_env  # noqa: F401  先引导环境

import causal_timeline  # noqa: E402
from llm_client import chat_json  # noqa: E402
from loop_core import make_fact  # noqa: E402
import narrator  # noqa: E402
from voyager import LOCAL_SQL_NOTES, TOOL_CATALOG, TOOL_REGISTRY, VLML_USAGE_TIPS  # noqa: E402
from tasks import TASKS, infer_topic  # noqa: E402  (tasks 不拉 VLML，可安全 import)

# 人导入的题永远 validated_target=False —— 这是伴学的判定链，不是可选项
VALIDATED_TARGET_IMPORTED = False

# 服务端持有的数据集上下文。不给它，模型会把图名当 series_id 传
# （实测：问「Corrode 上怎么崩的」→ 它直接传 series_id='Corrode'）。
DATASET_CONTEXT = """
本库里只有一场 series，问任何问题都用它：
  series_id = '2843069'（Cloud9 vs NRG）
  team_name 取 'Cloud9' 或 'NRG'
  map_name 取 'Haven'、'Corrode'、'Lotus' 之一
图名是 map_name，**不是** series_id。

这场 series 的最终结果是 **Cloud9 输了、NRG 赢了**。
实测踩过的坑：不交代胜负方向，模型会把「连续输 5 个回合」解释成
「连续五轮保持胜利」——数字对、方向反，比报错更危险。
所以描述任何连胜/连败时，先确认主语是谁输谁赢再下结论。
"""

PLAN_SCHEMA = """只输出一个 JSON 对象：
{"thought": "一句话说明打算怎么查",
 "calls": [{"tool": "工具名", "args": {"参数名": "参数值"}}]}
最多 3 次调用。"""

# 人导入的历史落盘，面板读它显示"我导入过什么、VLML 怎么答的"
COACH_LOG = Path(__file__).resolve().parent / "coach_log.jsonl"


def recent_imports(limit: int = 10) -> list[dict[str, Any]]:
    """最近几条人导入（面板用；不 import vlml_env，读文件即可）。"""
    if not COACH_LOG.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in COACH_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-limit:]


def _log_import(result: dict[str, Any]) -> None:
    """落一条人导入记录。不存完整 facts（面板只看摘要 + 解释）。"""
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "question": result.get("question", ""),
        "topic_id": result.get("topic_id", ""),
        "topic_matched": result.get("topic_matched", False),
        "topic_hit": result.get("topic_hit", ""),
        # 图谱侧落了什么（建边 / 覆盖层 / 命中洞察数）—— "收录了没有"要可回看
        "graph_link": result.get("graph_link") or {},
        "validated_target": result.get("validated_target", False),
        "trajectory": result.get("trajectory") or [],
        "facts_count": len(result.get("facts") or []),
        "narrative": result.get("narrative", ""),
        "insights": result.get("insights") or [],
        "caveats": result.get("caveats") or [],
        "comparable": result.get("comparable", False),
        "skipped": result.get("skipped", ""),
        "errors": (result.get("errors") or [])[:2],
    }
    with COACH_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _plan_prompt(question: str) -> str:
    return f"""你是原版 VLML 分析引擎。有人向你提了一个问题，请取出回答它所需的数据。

问题：
{question}

{DATASET_CONTEXT}

{TOOL_CATALOG}

VLML 自己的用法提示：
{VLML_USAGE_TIPS}

{LOCAL_SQL_NOTES}

{PLAN_SCHEMA}"""


def _extract_prompt(question: str, obs: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        [{"tool": o.get("tool"), "args": o.get("args"),
          "result": o.get("result"), "error": o.get("error")} for o in obs],
        ensure_ascii=False,
        default=str,   # VLML 的返回里带 datetime，不处理会序列化失败
    )[:14000]
    return f"""工具返回的真实数据：

{payload}

问题：
{question}

请把数据整理成事实数组，每个事实：
{{"subject": {{...}}, "dimension": "...", "value": 数值,
  "unit": "percent|count|ratio|raw", "base": 分母}}

subject 至少要能定位到谁：
  - 涉及队伍就带 "team"，涉及某张图就带 "map"（用真实图名，不要写编号）
dimension 要能看出方向：写 max_losing_streak（最长连败）这种，
  不要写 round_streak 这种看不出是输是赢的名字 —— 方向反了比报错更危险。

只输出 JSON：{{"facts":[...]}}
不要编造数据里没有的数字。"""


async def _execute(calls: list[dict[str, Any]],
                   limit: int = 8) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    # limit 不能写死 3（与 voyager._execute 同一个坑）：评分点多于 3 个时，
    # 第 4 条之后的调用会被静默丢弃，讲解就少一块。
    obs: list[dict[str, Any]] = []
    traj: list[str] = []
    errors: list[str] = []
    for call in (calls or [])[:limit]:
        tool = str(call.get("tool", ""))
        args = dict(call.get("args") or {})
        fn = TOOL_REGISTRY.get(tool)
        if fn is None:
            errors.append(f"未知工具 {tool}")
            continue
        try:
            if fn is vlml_env.execute_custom_sql:
                sql = str(args.pop("sql_query", "") or args.pop("sql", ""))
                if not sql.lower().lstrip().startswith("select"):
                    errors.append(f"{tool}: 只允许 SELECT")
                    continue
                res = await fn(sql)
                args = {"sql_query": sql}
            else:
                res = await fn(**args)
            if isinstance(res, dict) and res.get("error"):
                errors.append(f"{tool}: {res['error']}"[:200])
                obs.append({"tool": tool, "error": res["error"]})
                continue
            obs.append({"tool": tool, "args": args, "result": res})
            traj.append(tool)
        except Exception as e:
            errors.append(f"{tool}: {type(e).__name__}: {e}"[:200])
    return obs, traj, errors


async def _task_from_text(question: str,
                          facts: list[dict[str, Any]] | None = None
                          ) -> tuple[str, str]:
    """归不上已有题时，拿这段文本去生成一道新题。返回 (topic_id, 说明)。

    失败就坦然返回空 —— 出题器那条线断了，但**解释本身照常返回**，
    人看到的内容不受影响。

    `facts` 是 VLML 刚才给出的答案。它是这条链路的关键：

      人问的题归不上 → 但 VLML 答得出（有维度、有值、有取数 SQL）
      → 判「工具集覆盖不覆盖得到」→ 覆盖得到就把这题**收进图谱的边上**
      → 出题器从边上挑方向 → 人的意图进到自适应出题里

    不传 facts 的话，出题点只能由 `_graph_blueprint()` 从**已有**维度节点里
    挑，而已有维度几乎都出过题（图谱的维度节点全部从 TASKS[].rubric 派生），
    于是挑出来的还是旧方向 —— 人导入的意图就这么被吃掉了。
    实测：导入「爆弹成功率」时 VLML 答出 `plant_success_rate`，但 topic_id
    仍为空，出题器毫无反应。

    ⚠️ 收录的落法是**建边、不建节点**（`knowledge_graph.link_import`）：
    图谱在「表 × 工具集（insight）」这层是完全覆盖的，人的题只要 VLML 能用
    那套工具集做出来就够格收录，不需要再造一个维度节点去占位。
    """
    # 1) 先把人问出来的题收进图谱的**边**上（不建节点）
    dims: list[str] = []
    for f in (facts or []):
        d = str((f or {}).get("dimension") or "").strip()
        if d and d not in dims:
            dims.append(d)
    tool = ""
    if facts:
        tool = str((facts[0] or {}).get("tool") or "")
    focus = ""
    covered = False
    for d in dims:
        try:
            import knowledge_graph
            rec = knowledge_graph.link_import(
                question=question,
                dimension=d,
                sql=str((facts[0] or {}).get("sql") or ""),
                tool=tool,
                subject=dict((facts[0] or {}).get("subject") or {}),
                value=(facts[0] or {}).get("value"),
            )
            if not rec.get("adopted"):
                # 覆盖不到就如实说 —— 伴学同款：映射不上不硬造，交回人
                print(f"  [导入] 未收录：{str(rec.get('reason') or '')[:160]}")
                continue
            covered = True
            focus = focus or d
            print(f"  [导入] 图谱新增联系边（不建节点）：{d}"
                  f"｜覆盖层={rec.get('cover_level')}"
                  f"｜洞察 {len(rec.get('insights') or [])} 个"
                  f"｜表 {len(rec.get('tables') or [])} 张")
        except Exception as exc:                             # pragma: no cover
            print(f"  [导入] 入图失败：{type(exc).__name__}: {str(exc)[:120]}")

    # 2) 围绕这个方向出题（题点归服务端，模型只写题面）
    if not covered:
        # 覆盖不到还要硬出题，出题器只会从旧维度里挑一个凑数 —— 不如不出，
        # 免得制造"人导入已生效"的假象。
        return "", "（工具集覆盖不到，未收录进图谱，也不据此出题）"
    try:
        import question_gen
        rec, _ = await question_gen.generate_adopt(
            about=question, difficulty=2, tries=2, do_adopt=True, verbose=False,
            focus_dimension=focus)
    except Exception as exc:                                 # pragma: no cover
        print(f"  [导入] 生成新题失败：{type(exc).__name__}: {str(exc)[:120]}")
        return "", ""
    if not rec:
        return "", ""
    new_id = str(rec.get("topic_id") or "").strip()
    if not new_id:
        return "", ""
    try:
        TASKS[new_id] = question_gen.to_task(rec)
    except Exception as exc:                                 # pragma: no cover
        return "", ""
    # 题号回填进那条**边**：维度不建节点，做题时的图谱提示就靠边上的载荷
    # （`knowledge_graph._render_from_links`）。不回填，新题做题时支架是空的
    # —— 实测 `render_for_prompt` 返回长度 0。
    try:
        import knowledge_graph
        for lk in knowledge_graph.load_imported_links():
            if (str(lk.get("question") or "")[:300] == question[:300]
                    and not str(lk.get("topic_id") or "").strip()):
                knowledge_graph.link_import(
                    question=str(lk.get("question") or ""),
                    dimension=str(lk.get("dimension") or ""),
                    sql=str(lk.get("sql") or ""),
                    tool=str(lk.get("tool") or ""),
                    topic_id=new_id,
                    subject=dict(lk.get("subject") or {}),
                    value=lk.get("value"),
                )
    except Exception:                                        # pragma: no cover
        pass
    return new_id, "（导入文本归不上已有题，已生成新题）"


async def explain(question: str, *, topic_id: str = "",
                  auto_task: bool = True) -> dict[str, Any]:
    """人导入一个问题，由 VLML 解释回答。

    返回结构与 Voyager 的答卷同构（facts + narrative），但：
      - validated_target = False → 不计掌握度
      - 写一条 control fact 进行动因果时间线 → 出题器看得见

    关于 topic_id：不填的话这条 control fact 的 topic_id 就是空的，而出题器
    `latest_topics_by_kind()` 会跳过空 topic —— 等于人导入了但出题器看不见，
    「影响自适应出题」这条线断在这里。所以入口处先把它归到一题上。

    `auto_task`：归**不上**任何已有题时，拿这段文本去**生成一道新题**。

    这一步补的是伴学里「导入学习内容 → MaterialTopicMapper 映射到已有知识点」
    的**反面**：伴学映射不到就让人去图谱里选一个（`baseline_topic_prompt`）；
    MVE 多了 `question_gen`，可以直接把这段文本变成一道题，于是
    「人导入 → 影响出题」不会在归不上时断掉 —— 反而多了一种长新题的方式。

    关于时机：**归不上时不再取数前就急着生成题**。人问的新维度往往是 VLML
    刚答出来的（取数前根本不知道它叫什么），先取数、拿到事实，再用那个维度
    去种图谱节点 + 出题，才对得上人的方向。取数前生成只会让
    `_graph_blueprint()` 从已有维度里挑一个旧的（实测：导入「爆弹成功率」，
    生成的题和 plant 毫无关系）。
    """
    inferred, hit = "", ""
    if topic_id:
        hit = "（面板指定）"
    else:
        inferred, hit = infer_topic(question)
        topic_id = inferred
        # 归不上时不在这里生成题 —— 等取完数拿到维度再说（见 docstring）

    base: dict[str, Any] = {
        "question": question,
        "topic_id": topic_id,
        "topic_matched": bool(topic_id),
        "topic_hit": hit,
        "topic_inferred": bool(inferred),
        "source": "human_import",           # 出题器产出才是 targeted_question
        "validated_target": VALIDATED_TARGET_IMPORTED,
    }

    plan = chat_json([
        {"role": "system", "content": "你是原版 VLML 分析引擎，只取数不做主观推断。"},
        {"role": "user", "content": _plan_prompt(question)},
    ])
    obs, traj, errors = await _execute(plan.get("calls") or [])

    # 人导入这次**真实用过的取数 SQL 与工具** —— 新维度进图谱时要靠它填
    # tables / columns / recipe，没有这三样 `_graph_blueprint()` 挑不到它。
    used_sql, used_tool = "", ""
    for o in obs:
        a = o.get("args") or {}
        if not used_sql and a.get("sql_query"):
            used_sql = str(a.get("sql_query") or "")
            used_tool = str(o.get("tool") or "")
    if not used_tool and traj:
        used_tool = str(traj[0])

    # 工具调用闸（与 Voyager 那条同款）：一次都没调成功就不许产出事实。
    # 实测踩过：工具全报错时模型照样编出 3 条"事实"，值全是它自己想的。
    if not traj:
        result = {
            **base,
            "trajectory": [],
            "errors": errors or ["计划没有产生有效的工具调用"],
            "facts": [],
            "narrative": "",
            "insights": [],
            "caveats": [],
            "comparable": False,
            "skipped": "本轮没有取到任何数据，不产出解释（避免把报错说圆）",
        }
        # 取数失败也要进时间线：人发过这个意图是事实，出题器照样看得见。
        # 只是没有 facts，不能用来出题的证据，只能算方向信号。
        causal_timeline.append(
            causal_timeline.CONTROL,
            topic_id=topic_id,
            summary=f"人导入问题（未取到数据）：{question[:80]}",
            detail={"trajectory": [], "facts": 0, "validated_target": False},
        )
        _log_import(result)
        return result

    out = chat_json([
        {"role": "system", "content": "你是严谨的数据整理器，只输出 JSON，绝不编造数字。"},
        {"role": "user", "content": _extract_prompt(question, obs)},
    ])

    facts = []
    raw_facts: list[dict[str, Any]] = []
    for f in (out.get("facts") or []):
        if not isinstance(f, dict) or "subject" not in f or "dimension" not in f:
            continue
        # 原始事实要带上这次的取数 SQL / 工具：新维度进图谱时靠它们填
        # tables / columns / recipe（没有这三样出题器挑不到新节点）。
        raw_facts.append({**f, "sql": used_sql, "tool": used_tool})
        facts.append(make_fact(
            subject=f.get("subject") or {},
            dimension=str(f.get("dimension")),
            value=f.get("value"),
            unit=str(f.get("unit") or "raw"),
            base=f.get("base"),
            source={"tool": "vlml_coach", "args": {}},
        ))

    # 归不上已有题 → 用 VLML 刚答出的维度种图谱节点 + 围绕它出新题。
    # 放在取数之后：维度名是取数才有的，取数前生成只会绕回旧维度。
    if not topic_id and auto_task:
        topic_id, hit = await _task_from_text(question, raw_facts)
        base["topic_id"] = topic_id
        base["topic_matched"] = bool(topic_id)
        base["topic_hit"] = hit
        # 图谱侧到底落了什么，回给面板看 —— "收录了没有"必须可验证，
        # 不能只在日志里一行字。
        try:
            import knowledge_graph as _kg
            hit_lk = [l for l in _kg.load_imported_links()
                      if str(l.get("question") or "")[:300] == question[:300]]
            if hit_lk:
                lk = hit_lk[-1]
                base["graph_link"] = {
                    "covered": bool(lk.get("covered")),
                    "cover_level": str(lk.get("cover_level") or ""),
                    "insights": list(lk.get("insights") or []),
                    "insights_total": int(lk.get("insights_total") or 0),
                    "tables": list(lk.get("tables") or []),
                }
        except Exception:                                    # pragma: no cover
            pass

    # 解释由 LLM 出（VLML README:97：指标由工具出，洞察由 LLM 出）
    story = narrator.narrate(None, facts, verdict="") if facts else {
        "narrative": "", "insights": [], "caveats": [],
        "comparable": False,
        "skipped": "没取到数据，不产出解释",
    }

    result = {
        **base,
        "trajectory": traj,
        "errors": errors,
        "facts": facts,
        "narrative": story.get("narrative", ""),
        "insights": story.get("insights") or [],
        "caveats": story.get("caveats") or [],
        "comparable": False,
    }

    # 进时间线（因果），不进掌握度（评分）
    causal_timeline.append(
        causal_timeline.CONTROL,
        topic_id=topic_id,
        summary=f"人导入问题：{question[:80]}",
        detail={"trajectory": traj, "facts": len(facts),
                "validated_target": False},
    )
    _log_import(result)
    return result


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "Cloud9 在哪张图上崩得最厉害？"
    res = asyncio.run(explain(q))
    print(json.dumps({k: v for k, v in res.items() if k != "facts"},
                     ensure_ascii=False, indent=2))
    print(f"[facts {len(res['facts'])} 条]")
    for f in res["facts"][:8]:
        subj = "/".join(f"{k}={v}" for k, v in (f.get("subject") or {}).items())
        print(f"  {subj} · {f['dimension']} = {f['value']} (base={f.get('base')})")
