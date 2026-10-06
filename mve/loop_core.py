#!/usr/bin/env python3
"""MVE 核心：事实集 → 覆盖率评分 → 掌握度。

三块全部照抄纸面设计与猫娘伴学的阈值，行号依据见各函数 docstring。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable


# --------------------------------------------------------------------------
# 1. 事实（fact）
# --------------------------------------------------------------------------
# 纸面设计：fact = {subject, dimension, value, unit, base, scope, source}
# diff 单元 = (subject, dimension) 对


def fact_key(f: dict[str, Any]) -> tuple[str, str]:
    """事实的比对键：subject 规范化 + dimension。"""
    s = f.get("subject") or {}
    if isinstance(s, dict):
        subj = "|".join(f"{k}={s[k]}" for k in sorted(s) if s[k] is not None)
    else:
        subj = str(s)
    return (subj, str(f.get("dimension")))


def norm_subject(value: Any) -> dict[str, Any]:
    """把模型返回的 subject 归一化成交叉键字典。

    实测模型会返回字符串（如 "series=2843069/map=Corrode"）而不是对象，
    直接进 fact_key 虽然不崩，但后续面板渲染和比对都会走样。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        # 尝试解析 "k=v/k=v" 形态；解析不出来就整个当一个标签
        parts = [p for p in text.split("/") if "=" in p]
        if parts:
            out: dict[str, Any] = {}
            for p in parts:
                k, _, v = p.partition("=")
                if k.strip():
                    out[k.strip()] = v.strip()
            return out
        return {"label": text}
    return {}


def make_fact(
    *,
    subject: dict[str, Any],
    dimension: str,
    value: Any,
    unit: str = "raw",
    base: int | None = None,
    scope: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "subject": norm_subject(subject),
        "dimension": dimension,
        "value": value,
        "unit": unit,
        "base": base,
        "scope": scope or {},
        "source": source or {},
    }


def from_sql_result(
    rows: dict[str, Any],
    *,
    subject_keys: Iterable[str],
    dimension_key: str,
    value_key: str,
    base_key: str | None = None,
    tool: str = "execute_custom_sql",
    sql: str = "",
    fixed_subject: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """把 execute_custom_sql 的结果集展开成事实：一行一列 = 一个事实。

    这是「任意 SQL 都能吃下」的关键——只记结果，不记这列属于哪张表。
    """
    cols = list(rows.get("columns") or [])
    out: list[dict[str, Any]] = []
    for row in rows.get("rows") or []:
        rec = dict(zip(cols, row))
        subject = dict(fixed_subject or {})
        for k in subject_keys:
            if k in rec and rec[k] is not None:
                subject[k] = rec[k]
        out.append(make_fact(
            subject=subject,
            dimension=str(rec.get(dimension_key)),
            value=rec.get(value_key),
            base=int(rec[base_key]) if base_key and rec.get(base_key) is not None else None,
            source={"tool": tool, "args": {"sql": sql}},
        ))
    return out


def from_key_metrics(
    metrics: dict[str, Any],
    *,
    subject: dict[str, Any],
    prefix: str = "",
    tool: str = "",
    scope: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """把 VLML 报告的 key_metrics 展开成事实。

    关键：VLML 自己就用 {"num":..,"denom":..} 组织数值，
    所以 fact.base 直接取 denom，不用自算。
    """
    out: list[dict[str, Any]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if "num" in node and "denom" in node:
                out.append(make_fact(
                    subject=subject,
                    dimension=path,
                    value=node.get("num"),
                    base=node.get("denom"),
                    unit="count",
                    scope=dict(scope or {}),
                    source={"tool": tool, "section": "key_metrics"},
                ))
                return
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            out.append(make_fact(
                subject=subject,
                dimension=path,
                value=node,
                unit="raw",
                scope=dict(scope or {}),
                source={"tool": tool, "section": "key_metrics"},
            ))

    walk(metrics, prefix)
    return out


# --------------------------------------------------------------------------
# 2. 评分：覆盖率比对 + verdict
# --------------------------------------------------------------------------


@dataclass
class AnswerSpec:
    """服务端私有的确定性答案配方。

    照伴学 NumericToleranceEvaluator 的契约（deterministic_evaluators.py:158-167）：
    expected 与 tolerance **只从服务端私有 answer_spec 读**，绝不从学习者答案读，
    也绝不用模型生成的参考答案替换（tutor_llm_agent_answer_evaluate.py:82-84）。

    为什么必须有它：最初我把标准答案写成 rubric 里的常量（25.0 / 8 / 7），
    那是我自己手写的 = 我在当裁判。后来改成让 LLM 裁判跑一遍，结果它读报告
    数错了（说连败 5、起始 R2，真值是 8、R7）——LLM 会编。
    所以能用 SQL 算的必须 SQL 算，这块是确定性的，不经过任何模型。
    """

    sql: str = ""                     # 确定性 SQL，必须以 SELECT 开头（不支持 CTE）
    value_column: int = 0
    base_column: int | None = None
    numeric_tolerance: float = 0.0
    closed_world: bool = True

    # ---- 工具路径：与 sql 二选一 ----
    # 有些指标是 VLML 工具**内部聚合**出来的（pistol / eco / confidence 标签），
    # 没有对应的单条 SQL。硬要用 SQL 复现，等于我照着工具逻辑手写一遍 ——
    # 那又变成「我在当裁判」。所以这里直接调工具、按路径取值：
    # 标准答案仍然由服务端（原版 VLML）独立产出，不经模型、不经人手。
    tool: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    value_path: str = ""              # 点分路径，如 key_metrics.economy.pistol.num
    base_path: str = ""               # 分母的路径
    percent: bool = False             # True → value = round(num/denom*100, 1)


def dig(obj: Any, path: str) -> Any:
    """按点分路径从工具返回里取值。取不到返回 None（绝不猜、绝不兜底）。"""
    cur = obj
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        elif isinstance(cur, (list, tuple)) and part.isdigit():
            i = int(part)
            if i >= len(cur):
                return None
            cur = cur[i]
        else:
            return None
    return cur


@dataclass
class RubricPoint:
    """rubric 的一个评分点。期望一个 (subject, dimension) 被覆盖。"""

    point: str
    subject: dict[str, Any]
    dimension: str
    weight: float
    min_base: int = 20          # 题目自声明的样本门槛（纸面设计：写进 rubric 而非全局写死）
    expected_value: float | None = None
    tolerance: float = 0.05
    # 服务端私有：Voyager 看不到，只有裁判（VLML0）用它算标准答案
    answer_spec: AnswerSpec | None = None


@dataclass
class Evaluation:
    verdict: str                 # correct / partial / wrong / dont_know
    score: int                   # 0-100
    coverage: float              # 0-1
    covered_points: list[str]
    missing_points: list[str]
    rejected_low_base: list[str]
    evidence_status: str         # collected / below_threshold / none
    facts_count: int
    judge: str = "referee"       # referee（VLML0 裁判）| rubric（旧的自带答案，已弃用）
    unjudgeable: list[str] = field(default_factory=list)   # 裁判也没答案的评分点
    no_tool_calls: bool = False  # True = 本轮没调成功任何工具，事实无来源


def evaluate_vs_referee(
    facts: list[dict[str, Any]],
    rubric: list[RubricPoint],
    referee_facts: list[dict[str, Any]],
    *,
    tool_calls: int = -1,
) -> Evaluation:
    """Voyager 的事实集 vs VLML0 裁判的事实集。

    这才是「掌握程度」的算法：标准答案由原版 VLML 独立跑出来，
    不是出题人手写、也不是模型生成的参考答案。

    确定性层先跑（照伴学 AssessmentEngine.try_assess 的次序）：
      0. **工具调用闸**：一次工具都没调成功 → 判无证据，不计入掌握度。
         实测踩过：模型在工具全失败时会凭空编出 5 条"事实"，
         维度还对得上（因为题干里写了），值全是编的。不设这道闸，
         "编造"会拿到 partial 分。
      1. base 过闸 —— 分母不够，数值对也不算数
      2. (subject, dimension) 命中
      3. 值容差比对（容差来自服务端 answer_spec）
    判不了的点进 unjudgeable，不静默算对也不静默算错。
    """
    voyager_index: dict[tuple[str, str], dict[str, Any]] = {}
    for f in facts:
        voyager_index[fact_key(f)] = f

    ref_index: dict[tuple[str, str], dict[str, Any]] = {}
    for f in referee_facts or []:
        ref_index[fact_key(f)] = f

    covered: list[str] = []
    missing: list[str] = []
    rejected: list[str] = []
    unjudgeable: list[str] = []
    got = 0.0
    total = sum(p.weight for p in rubric) or 1.0
    no_evidence = tool_calls == 0

    for p in rubric:
        key = fact_key({"subject": p.subject, "dimension": p.dimension})
        ref = ref_index.get(key)
        if ref is None:
            # 裁判自己也没答出来 —— 这题出得有问题，不能算 Voyager 错
            unjudgeable.append(p.point)
            continue
        got_fact = voyager_index.get(key)
        if got_fact is None:
            missing.append(p.point)
            continue

        base = got_fact.get("base")
        if base is not None and base < p.min_base:
            rejected.append(f"{p.point}(base={base}<{p.min_base})")
            missing.append(p.point)
            continue

        # 值比对：与裁判的值比，容差取评分点声明或 answer_spec
        ref_value = ref.get("value")
        got_value = got_fact.get("value")
        # **交白卷 ≠ 答错**。实测这一条占大头：模型没看到这个数就填 null，
        # 之前会被当成一条"答案"去比对，记成"值不可比"或"值不符 0≠59" ——
        # 那等于把"没取到"粉饰成"取到了但取错"，掩盖了真正的病（取证不全）。
        if got_value is None:
            missing.append(f"{p.point}(未取到值：模型交了 null)")
            continue
        tol = p.tolerance if p.answer_spec is None else p.answer_spec.numeric_tolerance
        try:
            if abs(float(got_value) - float(ref_value)) > tol:
                missing.append(f"{p.point}(值不符: {got_value}≠{ref_value})")
                continue
        except (TypeError, ValueError):
            if str(got_value) != str(ref_value):
                # **类型相同 ≠ 形态问题**。实测 `confidence`：got="insufficient"、
                # ref="moderate"，两边都是 str，float() 转不动才落到这个分支，
                # 于是被记成"值不可比" —— 但那是**值不对**（挑错了枚举），
                # 不是"形态对不上"。两者补救动作不同：
                #   形态不对 → 对齐答案形态；值不对 → 重新判断/换口径。
                # 所以这里必须分开写，否则反馈里 next_action 会指错方向。
                if type(got_value) is type(ref_value):
                    missing.append(f"{p.point}(值不符: {got_value}≠{ref_value})")
                else:
                    # 把两边的**类型与原型**写进日志：只写"值不可比"等于什么都没说，
                    # 人没法判断是模型抄错了形态、还是裁判的值本来就不是数字。
                    missing.append(
                        f"{p.point}(值不可比: got={got_value!r}<{type(got_value).__name__}>"
                        f"≠ref={ref_value!r}<{type(ref_value).__name__}>)"
                    )
                continue

        covered.append(p.point)
        got += p.weight

    coverage = got / total

    # 一次工具都没调成功 → 手里的事实没有来源，整轮判无证据。
    # 照伴学 ui_api.py:41："没有证据 = unassessed + None，不是 0%"。
    if no_evidence:
        return Evaluation(
            verdict="dont_know", score=0, coverage=0.0,
            covered_points=[], missing_points=[p.point for p in rubric],
            rejected_low_base=rejected, evidence_status="none",
            facts_count=len(facts), judge="referee",
            unjudgeable=unjudgeable,
            no_tool_calls=True,
        )

    if not facts:
        evidence = "none"
    elif not covered and (rejected or missing):
        evidence = "below_threshold"
    elif covered:
        evidence = "collected"
    else:
        evidence = "below_threshold"

    if evidence == "none":
        verdict = "dont_know"
    elif coverage >= 0.95:
        verdict = "correct"
    elif coverage > 0:
        verdict = "partial"
    else:
        verdict = "wrong"

    return Evaluation(
        verdict=verdict,
        score=int(round(coverage * 100)),
        coverage=round(coverage, 4),
        covered_points=covered,
        missing_points=missing,
        rejected_low_base=rejected,
        evidence_status=evidence,
        facts_count=len(facts),
        judge="referee",
        unjudgeable=unjudgeable,
    )


def evaluate(
    facts: list[dict[str, Any]],
    rubric: list[RubricPoint],
) -> Evaluation:
    """旧路径：拿 rubric 自带的 expected_value 比。

    ⚠️ 已弃用。expected_value 是出题人手写/模型生成的，不是服务端独立跑出来的，
    用它算掌握度是假阳性来源。保留仅为对照实验；正式链路走 evaluate_vs_referee。
    """
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for f in facts:
        index[fact_key(f)] = f

    covered: list[str] = []
    missing: list[str] = []
    rejected: list[str] = []
    got = 0.0
    total = sum(p.weight for p in rubric) or 1.0

    for p in rubric:
        key = fact_key({"subject": p.subject, "dimension": p.dimension})
        f = index.get(key)
        if f is None:
            missing.append(p.point)
            continue
        base = f.get("base")
        if base is not None and base < p.min_base:
            rejected.append(f"{p.point}(base={base}<{p.min_base})")
            missing.append(p.point)
            continue
        if p.expected_value is not None:
            try:
                if abs(float(f["value"]) - p.expected_value) > p.tolerance:
                    missing.append(f"{p.point}(值不符)")
                    continue
            except (TypeError, ValueError):
                missing.append(f"{p.point}(值不可比)")
                continue
        covered.append(p.point)
        got += p.weight

    coverage = got / total

    if not facts:
        evidence = "none"
    elif not covered and rejected:
        evidence = "below_threshold"
    elif covered:
        evidence = "collected"
    else:
        evidence = "below_threshold"

    if evidence == "none":
        verdict = "dont_know"
    elif coverage >= 0.95:
        verdict = "correct"
    elif coverage > 0:
        verdict = "partial"
    else:
        verdict = "wrong"

    return Evaluation(
        verdict=verdict,
        score=int(round(coverage * 100)),
        coverage=round(coverage, 4),
        covered_points=covered,
        missing_points=missing,
        rejected_low_base=rejected,
        evidence_status=evidence,
        facts_count=len(facts),
        judge="rubric",
    )


# --------------------------------------------------------------------------
# 3. 掌握度 —— V2 模型（mve/mastery_model.py，照伴学 mastery_v2.py）
# --------------------------------------------------------------------------
#
# 旧版这里照的是伴学 **V1**（knowledge_tracker.py:252-286）。V1 有两处硬伤，
# 伴学自己在 V2 里修掉了，我们跟着换：
#   1. `confidence = 1-exp(-attempts/5)` 只数作答次数 → V2 改成按证据权重之和算
#   2. `recency = 1.0` 写死（V1:266）→ V2 引入半衰期 60 天的时间衰减
# V2 另外带来：每条证据有权重（评价可信度 × 作答可靠性 × 时间衰减）、
# 加权平均代替简单平均、有具体分数就用分数（不再退化成 verdict 三档）。
# 旧 V1 公式已删除，需要对照见 `掌握度调研-猫娘伴学对照.md`。

import mastery_model  # noqa: E402
from datetime import datetime  # noqa: E402

from mastery_model import (  # noqa: E402
    DEFAULT_POLICY,
    MASTERY_INSUFFICIENT_EVIDENCE,
    MASTERY_MASTERED,
    MASTERY_PROGRESSING,
    MasteryEvidence,
    MasteryPolicy,
    MasterySnapshot,
    calculate_mastery,
)

MASTERY_THRESHOLD = DEFAULT_POLICY.mastered_threshold
MASTERY_MIN_ATTEMPTS = DEFAULT_POLICY.mastery_min_attempts
UNRESOLVED_WRONG_CAP = DEFAULT_POLICY.unresolved_wrong_mastery_cap


def update_mastery(
    topic_id: str,
    evidence: list[MasteryEvidence],
    *,
    unresolved_wrong_count: int = 0,
    has_active_wrong_question: bool = False,
    as_of: datetime | str | None = None,
    policy: MasteryPolicy | None = None,
) -> MasterySnapshot:
    """掌握度投影（薄封装，本体在 mastery_model.calculate_mastery）。

    `evidence` 是这道题的**全部**历史证据（跨进程从 run_log 重建），
    不是"本轮一条" —— V2 的加权与时间衰减必须看到完整序列才算得对。
    """
    return calculate_mastery(
        topic_id,
        evidence,
        unresolved_wrong_count=unresolved_wrong_count,
        has_active_wrong_question=has_active_wrong_question,
        as_of=as_of,
        policy=policy or DEFAULT_POLICY,
    )



# --------------------------------------------------------------------------
# 4. 出题器
# --------------------------------------------------------------------------


@dataclass
class Task:
    """一个 blocking 任务（= 伴学的一道练习题）。"""

    topic_id: str
    question: str
    rubric: list[RubricPoint]
    difficulty: int = 2
    validated_target: bool = True      # 人工导入的题为 False → 不计掌握度
    min_base: int = 20
    # 这道题要用到哪些 MCP 工具。出题器据此做**工具覆盖**：
    # 某个工具一直没被用过 → 优先出需要它的题，否则覆盖性永远只是纸面论证。
    requires_tools: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "question": self.question,
            "difficulty": self.difficulty,
            "validated_target": self.validated_target,
            "requires_tools": list(self.requires_tools),
            "key_points": [p.point for p in self.rubric],
            "rubric": {p.point: p.weight for p in self.rubric},
        }


def to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
