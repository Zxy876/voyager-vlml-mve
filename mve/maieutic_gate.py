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
import time
from typing import Any, Callable

GATE_SYSTEM = """你是产婆术（Maieutic）的**闸门检查员**。一个助手刚交了它对某个问题的回答，
你要做的是：拿「必须回答的维度清单」逐条核，看它有没有对每条**做出实质回答**。

你只做这一件事 —— 不要评价答案写得好不好、结构漂不漂亮、有没有多余内容，
也不要总结、不要改写、不要给答案。

判定口径：
- 一条维度被"答到" = 答案里有**具体的、可核对的内容**在回应它。
  只是提到了这个词（"也要注意输入为空的情况"）**不算**；
  给出具体说法（"输入为空时返回 0"）才算。
- **答案的形态不限定**：一个数、一张表、一条判断、一句口径、一份清单都算答到。
  不要因为它交的不是数值 / 不是原样材料就判未答到；只看「有没有正面回应 +
  能不能在材料上核对」。反过来，形态再漂亮、内容空泛的也不算答到。
判定对象是**当前整篇作答的全文** —— 也就是助手这个 session 一路累积下来的
过程记录（下面「它交上来的回答」整段给出：每次尝试调了什么工具、跑了什么代码、
**实际拿到什么结果**、它自己说了什么结论、上一轮你判了什么）。
- **判定落点是它交上来的那句回答，不是它跑过的那条命令。**
  这条照 spec-examiner 的原话：维度被"答到" = 回答里针对它做出了**具体的
  承诺**（"也要处理输入为空的情况"**不算**；"输入为空时返回 0"**才算**）。
  执行记录、SQL、材料账是**核对依据**：回答里做出的承诺能在材料上指认 → 算数；
  回答里根本没有这个承诺、只是执行记录里出现过 → **不算**答到。
  **执行过 ≠ 作答过**：跑了一条带 WHERE 的查询，不等于交出了"按什么口径筛"
  这句回答。
- 判之前把这条维度的问句原样对着它交的回答念一遍：这句话正面回应问句了吗？
  · 问"多少" → 交一个数就是回应；
  · 问"按什么口径筛 / 用哪一列 / 怎么判断" → 回答里只有数、只有结果行、
    只有"取到了 N 行"，就是**没回应**，判未答到（它该把那句口径写出来）；
  · 问"有哪些 / 列出" → 回答里指认"在哪一段材料"即可，不必逐条抄写。
- 反过来，记录里只有"要查/将查"而没有实际结果、或者结果与维度不搭边的，
  判未答到。
- **Phase1 的边界（原版 maieutic：代码编辑器 Phase2 才解锁）**：这一轮助手
  **不能执行取数**。所以凡是"只能靠真跑一次才知道"的事实 —— 某表多少行、
  某列非空多少行、两张表 join 的命中率、某个答案的具体数值 —— **都不在
  Phase1 的作答义务内**：
  · 它写清了「查哪张表/哪一列、按什么条件、分子分母怎么算」= 对这条维度
    **已答到**（这正是 spec 该有的东西：方法承诺，Phase2 才去执行）；
  · 它只写「我还差一次执行」而没有任何方法/口径 = **未答到**，追问要它补
    方法与口径（"用什么列、什么条件、怎么算"），**不要**要它给具体数值。
  ⚠️ 不得因为"缺具体数值"把维度判未答，也不得在 `questions` 里要求它给数值。
  2026-11-13 实测：exercise_pool / id_answer_join 两维连问 14 轮，它每轮都
  只能答"需要 Phase2 执行"，永远闭合不了 —— 这是口径错，不是学生没答。
- 上一轮你判过、问过的，都在这份记录里；**别重复问同一句** ——
  它这几轮如果在原地打转，就把追问换成缺口本身（缺哪一步、该换什么口径）。
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
- ★★ 2026-11-15（同构助产士原版）—— 「库里没这个量」是**清单外发现**，不是缺口：
  用户口径：「仍然有除助产士闭合标准（缺口序列）以外的待填键（比如取数这个动作）
  要做，我们要割掉它」「result 相当于提交按钮，不提交是拿不到追问和判词的」。
  助产士原版的 `emergentGaps` **明确不阻塞闭合**（`spec-examiner.ts:18-22`：
  「不阻塞闭合：清单满足即 passed」）—— 语义正是「清单外的额外发现，不影响
  这份 spec 算不算完整」。
  ⇒ 所以当某个量在环境实查清单里**根本不存在**（没有费率列、没有这个口径、
    这个表查不到这个值）时：
    · 它是**环境事实**，不是「学生没答」—— 追问它一万次也追不出来
      （实测 2026-11-13：exercise_pool / id_answer_join 两维连问 14 轮，
       每轮只能答「需要 Phase2 执行」，永远闭合不了）；
    · **不要**把它写进 `questions`（那是「还没答到的缺口」）；
    · **改写进 `emergent_gaps`**（清单外发现，不阻塞闭合），让上层如实记
      「这个量在环境里不可得」并交给人看；
    · 只有「它承诺了一个口径但那个口径在清单里指认不了」才是真的未答到 ——
      那时判未答到 + 追问它改口径（换个说得通的口径），**不是**追问它取数。
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
    import os as _os
    # 判官模型可单配（2026-10-24 实测：glm-4-flash 当 judge，同一份带环境观察的
    # 输入 3 次全放行一个明显错误的答案 —— 判官不能省模型钱；出题/执行层可继续
    # 用 flash）。闸门是**维度闭合的决定性判官**，2026-10-26 实测：flash 闸门
    # 把 base_salary×overtime_hours 费率幻觉判成「已答到」（env_obs + 点名判据
    # 都在 prompt 里也没用）—— 毒配方再次入库。env MVE_CRITIC_MODEL 一设即切。
    #
    # ★ 2026-11-11 生产实证（整篇判定连续 6 次「输出解析不出 JSON」）：
    #   glm-4.6 默认开思维链，思考段与正文**共用 max_tokens**。整篇状态 = 全
    #   session 过程记录 + 环境实查清单（16k 字）+ 材料（18k 字）时，思考把
    #   2048 全吃光 → content 为空 → chat() 回退到 reasoning_content（纯思考
    #   散文，没有 JSON）→ 解析必然失败。gate_raw.log 里存的就是那段散文。
    #   ⇒ 这个调用是「照清单打勾并输出 JSON」，与出题器同类：关思考 + 给足
    #   max_tokens。要恢复思考设 MVE_GATE_THINKING=1。
    model = _os.getenv("MVE_CRITIC_MODEL", "")
    _no_think = _os.getenv("MVE_GATE_THINKING", "") not in ("1", "true", "yes")
    kw: dict[str, Any] = {"temperature": 0, "model": model, "max_tokens": 1500}
    if _no_think:
        kw["thinking"] = {"type": "disabled"}
    try:
        from llm_client import chat as _chat          # 面板/场景学习进程
    except Exception:
        try:
            from mve.llm_client import chat as _chat
        except Exception:
            from voyager_vlml.llm import chat as _chat
            kw.pop("thinking", None)                  # 老实现不认这个参数
    return _chat(messages, **kw)


def _parse(text: str) -> dict[str, Any]:
    raw = str(text or "")
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.S)
    if m:
        raw = m.group(1)
    # ⚠️ 不要用 find("{") … rfind("}")：判官在 questions 里引用材料/代码时，
    # 正文也会出现花括号，首尾一夹就成了非法 JSON —— 2026-11-11 实测整篇判定
    # 连续 6 次「输出解析不出 JSON」，而同一份 prompt 离线跑就正常（差在它引了
    # 哪段材料）。这里改成按花括号配平逐个试（跳过字符串内的括号），并且
    # `strict=False` —— 允许字符串里有裸换行/制表符（模型把过程记录里的换行
    # 原样抄进 JSON 时正是这种形态）。
    for start in [i for i, ch in enumerate(raw) if ch == "{"]:
        depth, end, in_str, esc = 0, -1, False, False
        for j in range(start, len(raw)):
            ch = raw[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end < 0:
            continue
        try:
            data = json.loads(raw[start:end + 1], strict=False)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def _dump_raw(text: str) -> None:
    """解析失败时把原文落一份（定位「解析不出 JSON」用，最多留 200KB）。"""
    try:
        from pathlib import Path
        p = Path(__file__).with_name("gate_raw.log")
        if p.exists() and p.stat().st_size > 200_000:
            return
        with p.open("a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"len={len(str(text or ''))} =====\n")
            f.write(str(text or "")[:6000] + "\n")
    except Exception:
        pass


def _material_block(evidence: Any, limit: int = 3, per: int = 6000) -> str:
    """材料清单（同构助产士的 gathered）：只列**取成功**的条目，带来源。

    ⚠️ 2026-11-06 生产实证（Scene R）：原来每条只给**前 1200 字**，而
    sherlock.py 的 `class Sherlock…` 正好在第 1234 字节 —— 闸门每轮都判
    「源码断在 class Sh、远未达到 34664 字节」，维度连败 10+ 轮。
    长条目的完整性看不出来不是内容问题，是**视野问题**：给头 + 尾 + 总长，
    尾部（如 `if __name__ == "__main__"`）才是「拿全了没有」的证据。
    另外观测条目正文常在 `data` 里（没有 `text`），一并兜底。
    """
    items = [e for e in (evidence or []) if isinstance(e, dict) and e.get("ok")]
    if not items:
        return "  （没有取到任何材料）"
    out = []
    for e in items[:limit]:
        src = e.get("source") or e.get("tool") or "?"
        raw = e.get("text")
        if not raw:
            _d = e.get("data")
            if isinstance(_d, dict) and isinstance(_d.get("text"), str):
                raw = _d["text"]
            elif _d is not None:
                try:
                    raw = json.dumps(_d, ensure_ascii=False)
                except Exception:
                    raw = str(_d)
        text = " ".join(str(raw or "").split())
        n = len(text)
        if n > per:
            head = int(per * 0.6)
            tail = per - head - 40
            text = (text[:head] + f" …（中略 {n - head - tail} 字）… "
                    + text[-tail:])
        out.append(f"  - 来源：{src}（共 {n} 字）\n    内容：{text}")
    if len(items) > limit:
        out.append(f"  （另有 {len(items) - limit} 条材料未列出）")
    return "\n".join(out)


def render(question: str, answer: str,
           dimensions: list[dict[str, Any]], *, critique: str = "",
           evidence: Any = None, env_obs: str = "",
           prior_questions: list[str] | None = None,
           sql: str = "",
           prior_iterations: list[dict[str, Any]] | None = None) -> str:
    """照 spec-examiner 的 user 模板：任务 + 维度清单（含内部理由）+ 它交上来的回答。

    `sql` = 本轮实际执行的 SQL（2026-10-29 补，DeepSeek「判定以材料为准」同构）：
    不给 SQL，闸门看不见「计算过程」，只能对着一行结果反复追问「请提供计算
    过程和结果」—— 过程明明就在 SQL 里（过滤了哪列、按什么聚合），它却看不见，
    循环空转。给 SQL = 过程成为可核对的材料。

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
    _pi = [x for x in (prior_iterations or []) if isinstance(x, dict)]
    iters_block = ""
    if _pi:
        _lines = []
        _acc_ad: list[str] = []
        for _i, _it in enumerate(_pi[-4:], 1):
            _qs = "；".join(str(q)[:120] for q in (_it.get("questions") or []))
            _op = ", ".join(str(x) for x in (_it.get("gaps_identified") or []))
            _ad = ", ".join(str(x) for x in (_it.get("gaps_addressed") or []))
            for _x in (_it.get("gaps_addressed") or []):
                if str(_x) not in _acc_ad:
                    _acc_ad.append(str(_x))
            _lines.append(
                f"  Round {_i}: 它当时交的全文："
                f"{str(_it.get('spec_text') or '')[:600]}\n"
                f"             你问了：{_qs or '（无）'}\n"
                f"             当时还开着：[{_op or '（无）'}]"
                f"｜当轮答到：[{_ad or '（无）'}]")
        _acc_block = ""
        if _acc_ad:
            _acc_block = (
                "\n  已累计答到的维度：[" + ", ".join(_acc_ad) + "]\n"
                "  ★ 这些维度**一旦答到就不再变回未答**（原版 Phase1 的累计账："
                "instructorConfiguredDimensionsAddressed 是每轮 gapsAddressed "
                "的并集）：即使它本轮全文里没再写那段，也不要把它算进 "
                "gaps_still_open、也不要再追问它；只追问真正还开着的。\n")
        iters_block = ("\nPRIOR ITERATIONS（同一道题的历史提交，按轮序 —— "
                       "别重复问已经问过的；看它这几轮是不是在原地打转）：\n"
                       + "\n".join(_lines) + _acc_block + "\n")
    env_block = (f"\n环境实查清单（数据源真实的表/列/取值，实查不手写 —— "
                 f"答案的计算口径必须能在这里指认）：\n{env_obs}\n"
                 if str(env_obs or "").strip() else "")
    sql_block = (f"\n本轮实际执行的 SQL（**只作核对口径用，不能替代回答** —— "
                 f"它跑过什么不等于它答了什么）：\n"
                 f"{str(sql or '').strip()[:800]}\n"
                 if str(sql or "").strip() else "")
    return (f"任务：{question}\n\n"
            f"必须回答的维度清单：\n{dim_block or '  （无）'}\n{prior}"
            f"{asked_block}{iters_block}{env_block}{sql_block}\n"
            f"助手取到的材料（执行层剪裁，失败项已剔除）：\n"
            f"{_material_block(evidence)}\n\n"
            f"\n它这个 session 的完整过程记录（每次尝试：调用的工具、代码、"
            f"实际结果、它说的结论、判定；旧条目不会被改写）：\n\"\"\"\n"
            f"{str(answer or '')[:6000]}\n\"\"\"\n\n"
            f"逐条核。只输出 JSON。")


def check(question: str, answer: str, dimensions: list[dict[str, Any]], *,
          llm: Callable | None = None, critique: str = "",
          max_retries: int = 2, evidence: Any = None,
          env_obs: str = "",
          prior_questions: list[str] | None = None,
          sql: str = "",
          prior_iterations: list[dict[str, Any]] | None = None,
          material_required: bool = True) -> dict[str, Any]:
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
            _sys = GATE_SYSTEM
            if not material_required:
                # 整篇模式（原版 Phase1）：只判正文里有没有承诺，不看材料。
                _sys += ("\n\n【本轮**没有**材料账，也不要向材料账判】"
                         "判定只看它交上来的这版正文：正文里针对该维度写出"
                         "具体承诺 → 答到；正文里没有 → 未答到。"
                         "**不要**因为「它没执行 / 没取到材料 / 材料为空」就判"
                         "未答到 —— 那是后面阶段的事；也不要让追问去要材料，"
                         "追问要的是「把这句话写进正文」。"
                         "**还有**：只能靠执行才知道的数值（表有多少行、非空多少行、"
                         "两表 join 命中率、某个答案的数值），它写清「查哪张表/哪列、"
                         "什么条件、分子分母怎么算」就算**答到**；追问只让它补方法，"
                         "**不许**要求它给出具体数值（Phase1 没有执行通道）。")
            text = call([{"role": "system", "content": _sys},
                         {"role": "user", "content": render(
                             question, answer, dims, critique=critique,
                             evidence=evidence, env_obs=env_obs,
                             prior_questions=prior_questions,
                             sql=sql,
                             prior_iterations=prior_iterations)}])
        except Exception as e:
            last = f"LLM 调用失败：{type(e).__name__}: {e}"
            continue
        data = _parse(text)
        if not data:
            last = "输出解析不出 JSON"
            _dump_raw(text)
            continue
        passed = data.get("passed")
        if not isinstance(passed, bool):
            last = "passed 不是 bool"
            _dump_raw(text)
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
