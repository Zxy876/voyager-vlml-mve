#!/usr/bin/env python3
"""Maieutic 的「闸门闭合」移植 —— critic 只做闸门判定，不做整体质量评估。

来源（本机 `~/ maieutic_fork/maieutic`）
---------------------------------------
  `src/lib/opus/prompts/spec-examiner.ts:7-147`  SPEC_EXAMINER_SYSTEM
    · `passed` **当且仅当** `gaps_still_open` 为空 —— 判完成就是判清单，
      不判"写得好不好"。
    · 输出 `gaps_addressed` / `gaps_still_open` / `emergent_gaps` / `questions`
      / `passed`：除了判定，还**带回最多 3 条追问**（这就是"不仅判完成还带回
      提示"），但明确禁止给答案、给实现方案、改写内容。
    · `emergent_gaps`（清单外的发现）**不阻塞闭合** —— 配置清单满足即 passed。
    · 严格口径：提到主题 ≠ 做出承诺（"处理空输入" 不算，"空输入返回 0" 才算）。
    · 问题数量上限 3 条：critic 不该干太多活，也不该把学生/助手淹死。

MVE 同构映射
------------
    学生的 spec           →    执行层交上来的答案（RESULT / answer 文本）
    spec_gate_dimensions  →    maieutic_scaffold 出的维度清单
    追问 questions        →    critic 的 critique（原版把它拼进下一次 rollout
                               的 prompt —— 迭代提示的同一条通路）

为什么单独一个模块
------------------
`task_critic` 保留**原版 critic 的口径**（判任务目标达没达成，输出
reasoning/success/critique）。闸门是 Maieutic 的另一套：只核清单、只带追问。
两件事分开，critic 才不会又要判质量又要判清单（用户：别让 critic 做太多工作）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

GATE_SYSTEM = """你是产婆术（Maieutic）的**闸门检查员**。一个助手刚交了它对某个问题的回答，
你要做的是：拿「必须回答的维度清单」逐条核，看它有没有对每条**做出实质回答**。

你只做这一件事 —— 不要评价答案写得好不好、结构漂不漂亮、有没有多余内容，
也不要总结、不要改写、不要给答案。

判定口径：
- 一条维度被"答到" = 答案里有**具体的、可核对的内容**在回应它。
  只是提到了这个词（"也要注意输入为空的情况"）**不算**；
  给出具体说法（"输入为空时返回 0"）才算。
- `passed` 为 true **当且仅当** `gaps_still_open` 为空。
- 判定**以材料为准**：用户消息里会给你「助手取到的材料」（来自执行层，失败项
  已剔除）。答案只有落在材料上的内容才算答到；材料为空 = 没有任何可取证的
  内容，全部维度都算没答到（别拿"它写了段话"当答到）。
- **口径真实性**（同款严格：提到 ≠ 做出）：用户消息里还会给你「环境实查清单」
  （数据源真实的表/列/取值，实查不手写）。答案的**计算口径**必须能在这份清单里
  指认。**两个数值列相乘/相除合成一个新口径（如 base_salary × overtime_hours
  当作「加班费」）= 假设库里不存在的业务参数（费率/汇率/系数）** —— 库里没有
  该口径的列或费率，这个量就不可计算：该维度判**未答到**（进 gaps_still_open），
  并在 questions 里明说「库里没有这个口径/费率，该量不可计算，缺什么」。
  把月薪当时薪用是语义造假 —— 材料里真取到数字也不算答到。
- 如果你还发现了清单外的遗漏（`emergent_gaps`），照样可以列 —— 但它**不阻塞**
  闭合：清单全部答到就 passed=true，把额外发现当信息带回去即可。

`questions` —— 你唯一的"输出给下一轮"的内容：
- 最多 **3 条**；多了就是让下一轮不知道先改哪条；
- 每条针对一条具体的 `gaps_still_open`；
- 是**追问**，不是答案：指出差在哪、要补什么，**不要替它把答案写出来**
  （写了就等于让它照抄，它什么也没学会）；
- 用中文写，短。

只输出一个 JSON 对象，不要前言、不要 markdown 围栏：
{
  "gaps_addressed": ["<已答到的维度 id>", ...],
  "gaps_still_open": ["<还没答到的维度 id>", ...],
  "emergent_gaps": [{"description": "<清单外发现的遗漏>", "question": "<追问>"}],
  "questions": ["<追问 1>", "<追问 2>"],
  "passed": true | false
}

`passed` 必须严格等于 `gaps_still_open` 是否为空。"""


def _default_llm(messages: list[dict[str, str]]) -> str:
    from voyager_vlml.llm import chat
    import os as _os
    # 判官模型可单配（2026-10-24 实测：glm-4-flash 当 judge，同一份带环境观察的
    # 输入 3 次全放行一个明显错误的答案 —— 判官不能省模型钱；出题/执行层可继续
    # 用 flash）。闸门是**维度闭合的决定性判官**，2026-10-26 实测：flash 闸门
    # 把 base_salary×overtime_hours 费率幻觉判成「已答到」（env_obs + 点名判据
    # 都在 prompt 里也没用）—— 毒配方再次入库。env MVE_CRITIC_MODEL 一设即切。
    return chat(messages, temperature=0,
                model=_os.getenv("MVE_CRITIC_MODEL", ""))


def _parse(text: str) -> dict[str, Any]:
    raw = str(text or "")
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.S)
    if m:
        raw = m.group(1)
    s, e = raw.find("{"), raw.rfind("}")
    if s < 0 or e <= s:
        return {}
    try:
        data = json.loads(raw[s:e + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _material_block(evidence: Any, limit: int = 3, per: int = 1200) -> str:
    """材料清单（同构助产士的 gathered）：只列**取成功**的条目，带来源。"""
    items = [e for e in (evidence or []) if isinstance(e, dict) and e.get("ok")]
    if not items:
        return "  （没有取到任何材料）"
    out = []
    for e in items[:limit]:
        src = e.get("source") or e.get("tool") or "?"
        text = " ".join(str(e.get("text") or "").split())[:per]
        out.append(f"  - 来源：{src}\n    内容：{text}")
    if len(items) > limit:
        out.append(f"  （另有 {len(items) - limit} 条材料未列出）")
    return "\n".join(out)


def render(question: str, answer: str,
           dimensions: list[dict[str, Any]], *, critique: str = "",
           evidence: Any = None, env_obs: str = "",
           prior_questions: list[str] | None = None) -> str:
    """照 spec-examiner 的 user 模板：任务 + 维度清单（含内部理由）+ 它交上来的回答。

    `env_obs` = 环境实查清单（数据源真实的表/列/取值）—— 口径真实性的判据：
    答案的计算口径必须能在清单里指认，指认不了 = 假设业务参数 = 未答到
    （2026-10-26 实测 base_salary×overtime_hours 当加班费穿过旧判据入库成
    164000 假数；闸门只核清单核不出口径幻觉，环境实况必须一起给它）。

    `prior_questions` = 本次闭合过程中**已经问过**的追问（步级闸门用，同构
    spec-examiner.ts:224-235 的 PRIOR ITERATIONS：examiner 每轮都看得见
    自己问过什么，不重复问）。步级化后闸门一步一判，不给它这份历史它会
    每步问同一句 → 模型对着同一句改 4 步，浪费整个 rollout。
    """
    dim_block = "\n".join(
        f"  - id: {str(d.get('id') or '')}\n"
        f"    question: {str(d.get('question') or '')}\n"
        f"    (内部理由，别复述给助手：{str(d.get('why') or '')})"
        for d in (dimensions or []) if isinstance(d, dict))
    prior = (f"\n上一轮你提的追问（它可能照着改了）：\n  - {critique}\n"
             if critique else "")
    asked_block = ""
    _pq = [str(q).strip() for q in (prior_questions or [])
           if str(q or "").strip()]
    if _pq:
        asked_block = ("\n本次闭合过程中你已经问过以下追问（它可能已经照着"
                       "改了，**不要重复问**——改问缺口本身、或换一个角度）：\n"
                       + "\n".join(f"  - {q[:200]}" for q in _pq[:6]) + "\n")
    env_block = (f"\n环境实查清单（数据源真实的表/列/取值，实查不手写 —— "
                 f"答案的计算口径必须能在这里指认）：\n{env_obs}\n"
                 if str(env_obs or "").strip() else "")
    return (f"任务：{question}\n\n"
            f"必须回答的维度清单：\n{dim_block or '  （无）'}\n{prior}"
            f"{asked_block}{env_block}\n"
            f"助手取到的材料（执行层剪裁，失败项已剔除）：\n"
            f"{_material_block(evidence)}\n\n"
            f"它交上来的回答：\n\"\"\"\n{str(answer or '')[:2500]}\n\"\"\"\n\n"
            f"逐条核。只输出 JSON。")


def check(question: str, answer: str, dimensions: list[dict[str, Any]], *,
          llm: Callable | None = None, critique: str = "",
          max_retries: int = 2, evidence: Any = None,
          env_obs: str = "",
          prior_questions: list[str] | None = None) -> dict[str, Any]:
    """闸门判定。返回 {"passed","addressed","open","questions","emergent","origin"}。

    `origin="failed"` = 模型/解析不可用（调用方应退回原版 critic，不假装判过）。

    `env_obs` = 环境实查清单（数据源真实的表/列/取值）→ 进 user 消息，
    口径真实性的判据（列×列合成新口径 = 假设费率 = 未答到）。

    `prior_questions` = 本次闭合已问过的追问（步级闸门防重复问，同构
    spec-examiner 的 priorIterations）。
    """
    out: dict[str, Any] = {"passed": False, "addressed": [], "open": [],
                           "questions": [], "emergent": [],
                           "origin": "failed", "reason": ""}
    dims = [d for d in (dimensions or []) if isinstance(d, dict)
            and str(d.get("question") or "").strip()]
    if not dims:
        out["reason"] = "没有维度清单（不该走闸门）"
        return out
    if not str(answer or "").strip():
        out.update(open=[str(d.get("id") or "") for d in dims],
                   questions=["执行层没有产出任何答案"],
                   origin="llm", reason="")
        return out
    call = llm or _default_llm
    last = ""
    for _ in range(max(1, max_retries) + 1):
        try:
            text = call([{"role": "system", "content": GATE_SYSTEM},
                         {"role": "user", "content": render(
                             question, answer, dims, critique=critique,
                             evidence=evidence, env_obs=env_obs,
                             prior_questions=prior_questions)}])
        except Exception as e:
            last = f"LLM 调用失败：{type(e).__name__}: {e}"
            continue
        data = _parse(text)
        if not data:
            last = "输出解析不出 JSON"
            continue
        passed = data.get("passed")
        if not isinstance(passed, bool):
            last = "passed 不是 bool"
            continue
        addressed = [str(x) for x in (data.get("gaps_addressed") or [])]
        opened = [str(x) for x in (data.get("gaps_still_open") or [])]
        # passed 必须严格等于 gaps_still_open 为空（原版 :146-147 同一条）
        opened = [] if (passed and not opened) else opened
        if passed and opened:
            passed = False
        out.update(passed=bool(passed), addressed=addressed, open=opened,
                   questions=[str(q) for q in (data.get("questions") or [])][:3],
                   emergent=[g for g in (data.get("emergent_gaps") or [])
                             if isinstance(g, dict)][:3],
                   origin="llm", reason="")
        return out
    out["reason"] = last or "闸门判定不可用"
    return out
