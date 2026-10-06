#!/usr/bin/env python3
"""叙事洞察层：把「已被裁判认可的事实」换成给人看的解释。

这一步在流程的最末端，**只有 Voyager 的事实被裁判判过之后才跑**：

    出题器出题 → Voyager 编排取数 → 事实集 → VLML0 裁判比对
        → 通过的事实 → 【本文件】叙事洞察 → 面板「解释输出区」

为什么必须在比对之后：
    叙事是给用户（教练/分析师）看的最终交付物。如果不先过裁判就把
    Voyager 的全部事实拿去叙事，等于把编错的数字包装成流畅的结论——
    那正是 VLML 宣称要消灭的东西（"无幻觉，无虚构数据"）。
    所以本文件只收 **covered（裁判认可）** 的事实，不收被判值不符的。

为什么要单独一层：
    VLML 自己就把这条边界画好了（README.md:97）：
        "All reports return metrics and evidence only. LLMs should generate insights."
    指标与证据由工具出，洞察由 LLM 出，两者不混。

产出的叙事标记 `comparable: false` —— **不参与比对**。
这条是助产士第二轮定下的契约，机器可读 + 人可读（面板虚线框）双重标记。
"""

from __future__ import annotations

import json
from typing import Any

from llm_client import chat  # noqa: E402

# VLML 自己的报告标准（insights_reference.md）：
#   :130 硬规则 —— 必须给分子分母："75% clutch win rate (3/4)"，不能只给 "75%"
#   :122-127 四档置信度 —— n≥100 Strong / 50-99 Moderate / 20-49 Weak / <20 Insufficient
NARRATOR_SYSTEM = """你是 Valorant 电竞数据分析师（教练视角）。

铁律：
1. 只能使用下面给出的事实，不许引入任何没在数据里出现的数字。
2. 每条结论必须能追溯到一条事实；引用百分比时**必须带上分母**，
   写成「67%（16/24）」，不能只写「67%」。
3. 样本量小的时候必须说明（分母 < 20 标 Weak，< 50 标 Moderate）。
4. 不许预测、不许给战术建议以外的空话。

输出一个 JSON：
{"narrative": "3-5 句连贯的分析，给教练看的",
 "insights": ["一条可执行的洞察", "最多 3 条"],
 "caveats": ["数据的局限，比如样本量小", "可以为空数组"]}"""


def confidence_label(base: int | None, min_base: int = 20) -> str:
    """照 VLML insights_reference.md:122-127 的四档置信度。"""
    if base is None:
        return ""
    if base >= 100:
        return "Strong"
    if base >= 50:
        return "Moderate"
    if base >= min_base:
        return "Weak"
    return "Insufficient"


def _fmt_fact(f: dict[str, Any]) -> str:
    subj = "/".join(f"{k}={v}" for k, v in (f.get("subject") or {}).items())
    base = f.get("base")
    conf = confidence_label(base)
    val = f.get("value")
    unit = f.get("unit") or "raw"
    suffix = "%" if unit == "percent" else ""
    base_txt = f"，分母 {base}" if base is not None else "（无分母）"
    conf_txt = f"，置信度 {conf}" if conf else ""
    return f"- {subj} · {f.get('dimension')} = {val}{suffix}{base_txt}{conf_txt}"


def narrate(
    task: Any,
    verified_facts: list[dict[str, Any]],
    *,
    verdict: str = "",
) -> dict[str, Any]:
    """把已验证事实换成叙事洞察。

    comparable 恒为 False —— 这是契约，不是可选项。
    """
    if not verified_facts:
        return {
            "narrative": "",
            "insights": [],
            "caveats": [],
            "comparable": False,
            "skipped": "本轮没有被裁判认可的事实，不产出叙事（避免把编错的数字说圆）",
        }

    fact_lines = "\n".join(_fmt_fact(f) for f in verified_facts)
    prompt = f"""题目：
{task.question if task else ''}

以下事实**已经通过原版 VLML 裁判的核对**（值都对得上）：

{fact_lines}

本次判定：{verdict}

请基于这些已验证事实，写出给教练看的叙事洞察。只输出 JSON。"""

    raw = chat(
        [
            {"role": "system", "content": NARRATOR_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
    )

    start, end = raw.find("{"), raw.rfind("}")
    payload: dict[str, Any] = {}
    if start >= 0 and end > start:
        try:
            payload = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            payload = {}

    return {
        "narrative": str(payload.get("narrative") or "").strip(),
        "insights": [str(x).strip() for x in (payload.get("insights") or []) if str(x).strip()],
        "caveats": [str(x).strip() for x in (payload.get("caveats") or []) if str(x).strip()],
        # 契约：叙事永远不参与比对。机器可读标记。
        "comparable": False,
        "based_on": len(verified_facts),
        "raw": "" if payload else raw[:400],
    }
