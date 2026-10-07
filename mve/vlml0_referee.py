#!/usr/bin/env python3
"""VLML0 —— 裁判。原版 VLML，无 Voyager 参与。

为什么必须有它：
    上一版我把标准答案写成 rubric 里的常量（expected_value=25.0 / 8 / 7），
    那是我自己用 SQL 核算完手写的 —— 等于**我在当裁判**。这违反伴学的硬契约：

        tutor_llm_agent_answer_evaluate.py:82-84
        "The evaluator may explain its verdict, but it must never replace the
         server-held expected answer with a model-generated reference answer."

    即：标准答案必须由服务端独立产出，不能是模型给的，也不能是人手写的。
    在 MVE 里，"服务端"就是原版 VLML —— 它不读 Voyager 的技能库、不读 Voyager
    的轨迹，只拿同一道题和同一套 MCP 工具，自己编排、自己取数。

VLML0 与 VLML1（Voyager 的场）的区别：
    VLML0  裁判。允许知道 rubric 的全部维度提示（它是出题方，本来就知道答案长什么样）
    VLML1  Voyager 的场。blind 模式下只给自然语言题干，维度要自己想到
    两者共用同一份 TOOL_CATALOG、同一套工具分派、同一个数据库 —— 只有"知不知道
    评分点"不同。这样比对才公平：差多少就是 Voyager 的编排能力差多少。

产物按 topic_id 落盘缓存：同一道题只跑一次。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import vlml_env  # noqa: F401  先引导环境

from loop_core import dig, fact_key, make_fact  # noqa: E402
from llm_client import chat_json  # noqa: E402
from voyager import LOCAL_SQL_NOTES, TOOL_CATALOG, TOOL_REGISTRY, VLML_USAGE_TIPS  # noqa: E402

STORE = Path(__file__).resolve().parent / "referee_store.json"


@dataclass
class RefereeAnswer:
    """裁判给出的标准答案：事实集 + 叙事（叙事不参与比对）。"""

    topic_id: str
    facts: list[dict[str, Any]]
    narrative: str = ""
    trajectory: list[str] = field(default_factory=list)
    ok: bool = True
    error: str = ""

    @property
    def keys(self) -> set[tuple[str, str]]:
        """标准答案覆盖了哪些 (subject, dimension)。"""
        return {fact_key(f) for f in self.facts}

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "facts": self.facts,
            "narrative": self.narrative,
            "trajectory": self.trajectory,
            "ok": self.ok,
            "error": self.error,
        }


def _load_store() -> dict[str, Any]:
    if not STORE.exists():
        return {}
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_store(data: dict[str, Any]) -> None:
    STORE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def spec_fingerprint(task: Any) -> str:
    """这道题的**取数配方**指纹 —— 缓存过期判据。

    为什么必须加它：缓存原来只按 `topic_id` 存，而生成的题会被**重新生成**
    （同 topic_id、换配方）。实测 `max_losing_streak_map` 重出之后，裁判
    仍然拿着缓存里的旧真值 `'Corrode'`（旧配方的 value_column 指到了
    map_name）去比对新答案 —— 模型交的 `3` **是对的**，却被判"值不可比"，
    连跑 5 轮 0%。题变了而裁判不知道，跟图谱缓存那个坑是同一个病。
    """
    import hashlib
    parts = [str(getattr(task, "topic_id", ""))]
    for p in (getattr(task, "rubric", None) or []):
        s = getattr(p, "answer_spec", None)
        parts.append(f"{p.dimension}|{p.subject}|"
                     f"{getattr(s, 'sql', '') or ''}"
                     f"{getattr(s, 'tool', '') or ''}{getattr(s, 'value_path', '') or ''}"
                     f"{getattr(s, 'value_column', '')}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def cached(topic_id: str, fingerprint: str = "") -> RefereeAnswer | None:
    raw = _load_store().get(topic_id)
    if not raw:
        return None
    # 配方变了 → 这份缓存是**旧题的答案**，不能再用
    if fingerprint and str(raw.get("spec_fingerprint") or "") != fingerprint:
        return None
    return RefereeAnswer(
        topic_id=raw.get("topic_id", topic_id),
        facts=raw.get("facts") or [],
        narrative=raw.get("narrative", ""),
        trajectory=raw.get("trajectory") or [],
        ok=bool(raw.get("ok", True)),
        error=raw.get("error", ""),
    )


def reset() -> None:
    if STORE.exists():
        STORE.unlink()
    print(f"已清空裁判缓存：{STORE}")


# ---------------------------------------------------------------------------
# 裁判的编排：与 Voyager 同一套工具，但提示全给
# ---------------------------------------------------------------------------

REFEREE_PLAN_SCHEMA = """只输出一个 JSON 对象：
{"thought": "一句话说明你打算怎么查",
 "calls": [{"tool": "工具名", "args": {"参数名": "参数值"}}]}
最多 3 次调用。你要给出能支撑全部评分点的查询，不要省。"""


def _plan_prompt(task: Any) -> str:
    points = "\n".join(
        f"- {p.point}｜dimension 必须写成 `{p.dimension}`｜"
        f"subject 必须写成 {json.dumps(p.subject, ensure_ascii=False)}"
        for p in task.rubric
    )
    return f"""你是原版 VLML 分析引擎（裁判）。请为下面这道题取出**标准答案**所需的数据。

题目：
{task.question}

必须覆盖的评分点（每个都要有对应数据）：
{points}

{TOOL_CATALOG}

VLML 自己的用法提示：
{VLML_USAGE_TIPS}

{LOCAL_SQL_NOTES}

{REFEREE_PLAN_SCHEMA}"""


def _extract_prompt(task: Any, obs: list[dict[str, Any]]) -> str:
    points = "\n".join(
        f"- {p.point}｜dimension=`{p.dimension}`｜subject={json.dumps(p.subject, ensure_ascii=False)}"
        for p in task.rubric
    )
    payload = json.dumps(
        [{"tool": o["tool"], "args": o.get("args"),
          "result": o.get("result"), "error": o.get("error")} for o in obs],
        ensure_ascii=False,
    )[:14000]
    return f"""工具返回的真实数据：

{payload}

评分点：
{points}

请整理成事实数组，每个事实：
{{"subject": {{...}}, "dimension": "...", "value": 数值,
  "unit": "percent|count|ratio|raw", "base": 分母}}

要求：
- 每个评分点至少一条事实，dimension 与 subject 必须与评分点**逐字一致**
- value 必须是数据里真实存在的数，不许推算、不许编
- base 取分母（百分比必填）

只输出 JSON：{{"facts":[...], "narrative":"一句话结论"}}"""


async def _execute(calls: list[dict[str, Any]],
                   limit: int = 3) -> tuple[list[dict[str, Any]], list[str]]:
    """跑调用。limit 必须由**评分点数**决定 —— 写死 3 会让多于 3 个评分点的题
    永远凑不齐标准答案（与 voyager._execute 同一个坑）。"""
    obs: list[dict[str, Any]] = []
    traj: list[str] = []
    for call in (calls or [])[:limit]:
        tool = str(call.get("tool", ""))
        args = dict(call.get("args") or {})
        fn = TOOL_REGISTRY.get(tool)
        if fn is None:
            obs.append({"tool": tool, "error": f"未知工具 {tool}"})
            continue
        try:
            if fn is vlml_env.execute_custom_sql:
                sql = str(args.pop("sql_query", "") or args.pop("sql", ""))
                if not sql.lower().lstrip().startswith("select"):
                    obs.append({"tool": tool, "error": "只允许 SELECT"})
                    continue
                res = await fn(sql)
                args = {"sql_query": sql}
            else:
                res = await fn(**args)
            obs.append({"tool": tool, "args": args, "result": res})
            traj.append(tool)
        except Exception as e:
            obs.append({"tool": tool, "error": f"{type(e).__name__}: {e}"})
    return obs, traj


async def _tool_facts(task: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """确定性层 · 工具路径版：直接调 VLML 工具，按路径取标准答案。

    与 _deterministic_facts（SQL 版）互补。凡是指标由工具内部聚合、没有
    等价 SQL 的评分点，走这里 —— 避免"我照着工具逻辑手写 SQL"变相当裁判。
    """
    facts: list[dict[str, Any]] = []
    done: list[str] = []
    for p in task.rubric:
        spec = getattr(p, "answer_spec", None)
        if spec is None or not spec.tool:
            continue
        fn = TOOL_REGISTRY.get(spec.tool)
        if fn is None:
            print(f"  [裁判·工具层] {p.point} 未知工具 {spec.tool}")
            continue
        try:
            res = await fn(**dict(spec.tool_args))
        except Exception as e:
            print(f"  [裁判·工具层] {p.point} 调用失败：{type(e).__name__}: {e}")
            continue
        if isinstance(res, dict) and res.get("error"):
            print(f"  [裁判·工具层] {p.point} 工具报错：{str(res['error'])[:120]}")
            continue
        value = dig(res, spec.value_path)
        base = dig(res, spec.base_path) if spec.base_path else None
        if value is None:
            print(f"  [裁判·工具层] {p.point} 路径 {spec.value_path} 取不到值")
            continue
        if spec.percent and isinstance(base, (int, float)) and base:
            value = round(float(value) / float(base) * 100, 1)
        facts.append(make_fact(
            subject=dict(p.subject),
            dimension=p.dimension,
            value=value,
            unit="percent" if spec.percent else "raw",
            base=int(base) if isinstance(base, (int, float)) else None,
            source={"tool": f"vlml0_referee::{spec.tool}", "args": dict(spec.tool_args)},
        ))
        done.append(p.point)
    return facts, done


async def _deterministic_facts(task: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """确定性层：能用 SQL 算的绝不问模型。

    LLM 读报告会数错（实测把连败 8 说成 5、起始 R7 说成 R2），
    所以凡是有 answer_spec 的评分点，一律 SQL 直算。
    """
    facts: list[dict[str, Any]] = []
    done: list[str] = []
    for p in task.rubric:
        spec = getattr(p, "answer_spec", None)
        # 工具路径型的评分点交给 _tool_facts，不在这里重复处理
        if spec is None or spec.tool:
            continue
        try:
            res = await vlml_env.execute_custom_sql(spec.sql)
        except Exception as e:
            print(f"  [裁判·确定性] {p.point} SQL 失败：{type(e).__name__}: {e}")
            continue
        if res.get("error"):
            print(f"  [裁判·确定性] {p.point} SQL 错误：{res['error'][:120]}")
            continue
        rows = res.get("rows") or []
        if not rows:
            print(f"  [裁判·确定性] {p.point} SQL 返回空")
            continue
        row = rows[0]
        try:
            value = row[spec.value_column]
        except (IndexError, TypeError):
            continue
        base = None
        if spec.base_column is not None:
            try:
                base = int(row[spec.base_column])
            except (IndexError, TypeError, ValueError):
                base = None
        facts.append(make_fact(
            subject=dict(p.subject),
            dimension=p.dimension,
            value=value,
            unit="percent" if "percent" in str(p.dimension) or "conv" in str(p.dimension) else "count",
            base=base,
            source={"tool": "vlml0_referee.deterministic", "args": {"sql": spec.sql[:120]}},
        ))
        done.append(p.point)
    return facts, done


async def answer(task: Any, *, force: bool = False) -> RefereeAnswer:
    """异步跑原版 VLML 拿标准答案。同一 topic_id 只跑一次（除非 force）。

    次序照伴学 AssessmentEngine：确定性层先跑，缺口才交给 LLM。
    """
    fp = spec_fingerprint(task)
    if not force:
        hit = cached(task.topic_id, fp)
        if hit is not None:
            return hit

    # ---- ① 确定性层 ----（SQL 直算 + 工具路径取值，两条互补）
    det_facts, det_done = await _deterministic_facts(task)
    tool_facts, tool_done = await _tool_facts(task)
    det_facts = det_facts + tool_facts
    det_keys = {fact_key(f) for f in det_facts}

    # ---- ② LLM 层 ----
    # 需要 LLM 补事实的评分点（确定性层没覆盖到的）
    remaining = [
        p for p in task.rubric
        if fact_key({"subject": p.subject, "dimension": p.dimension}) not in det_keys
    ]
    # 关键：即使确定性层已经覆盖了全部评分点，**这一趟编排照样要跑**。
    # 因为它产出的 trajectory 是给 Voyager 看的「编排示范」——
    # 伴学答完题后会回传 reference_answer（tutor_llm_agent_answer_evaluate.py:84），
    # 人类学习者就是靠看标准解法进步的。我们同理，但只给工具序列、不给数值。
    sub = _SubTask(task, remaining or list(task.rubric))
    plan = chat_json([
        {"role": "system", "content": "你是原版 VLML 分析引擎。你只取数，不做主观推断。"},
        {"role": "user", "content": _plan_prompt(sub)},
    ])
    obs, traj = await _execute(plan.get("calls") or [],
                               limit=max(3, min(8, len(task.rubric or []))))

    llm_facts: list[dict[str, Any]] = []
    narrative = ""
    if remaining:
        out = chat_json(
            [
                {"role": "system", "content": "你是严谨的数据整理器，只输出 JSON，绝不编造数字。"},
                {"role": "user", "content": _extract_prompt(sub, obs)},
            ],
        )
        for f in (out.get("facts") or []):
            if not isinstance(f, dict) or "subject" not in f or "dimension" not in f:
                continue
            llm_facts.append(make_fact(
                subject=f.get("subject") or {},
                dimension=str(f.get("dimension")),
                value=f.get("value"),
                unit=str(f.get("unit") or "raw"),
                base=f.get("base"),
                source={"tool": "vlml0_referee.llm", "args": {}},
            ))
        narrative = str(out.get("narrative", ""))

    # 确定性层产出的优先（它不会错），LLM 层只补缺
    facts = det_facts + [
        f for f in llm_facts
        if fact_key(f) not in det_keys
    ]

    ans = RefereeAnswer(
        topic_id=task.topic_id,
        facts=facts,
        narrative=narrative or f"确定性层覆盖 {len(det_facts)} 条，LLM 层补充 {len(facts) - len(det_facts)} 条。",
        trajectory=(["<deterministic-sql>"] if det_facts else []) + traj,
        ok=bool(facts),
        error="" if facts else "裁判没有产出任何事实",
    )

    store = _load_store()
    rec = ans.as_dict()
    rec["spec_fingerprint"] = fp        # 配方换了就得重跑（见 spec_fingerprint）
    store[task.topic_id] = rec
    _save_store(store)
    return ans


class _SubTask:
    """只保留剩余评分点的任务视图，供 LLM 层使用。"""

    def __init__(self, task: Any, rubric: list[Any]) -> None:
        self.topic_id = task.topic_id
        self.question = task.question
        self.rubric = rubric
        self.difficulty = getattr(task, "difficulty", 2)


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from run_mve import TASK, TASK_HARD

    task = TASK_HARD if "--hard" in sys.argv else TASK
    res = asyncio.run(answer(task, force="--force" in sys.argv))
    print(json.dumps(res.as_dict(), ensure_ascii=False, indent=2)[:3000])
