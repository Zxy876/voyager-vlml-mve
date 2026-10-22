#!/usr/bin/env python3
"""任务完成判定（Voyager critic 的同构）：LLM 看「执行观察」判任务达没达成。

原版取证（voyager_original/voyager/agents/critic.py）
----------------------------------------------------
  :47-110 `render_human_message`：把环境观察 + `Task: {task}` + `Context: {context}`
           拼成人类消息；**只要 events 里出现 `onError` 就 return None**（直接判不了）
  :112-135 `ai_check_task_success(max_retries=5)`：LLM 输出
           `{"reasoning","success","critique"}`，`json` 解析失败**递归重试 5 次**，
           5 次都失败 → (False, "")
  :137-... `check_task_success`：messages[1] is None → (False, "")（即出过错就不算成）
  prompts/critic.txt：判定口径 —— "Exceeding the task requirements is also considered
           a success while failing to meet them requires you to provide critique"；
           失败必须给 critique 帮它改进（这就是迭代提示的来源）

MVE 同构
--------
  events/观察      → 执行层的 observations（local_agent / VLMLActionAgent 产出：
                     data / data_sources / rows / sql / error）+ answer 文本
  onError → None   → 执行层报错（没有答案）→ 直接判 False，不叫 LLM
  数据型任务        → 题面本身要求具体数据（文件/表/多少/排名/统计/最新…）却
                     一次数据源都没调 → 直接判 False（真空防线；见 needs_data）
  知识型任务        → 「怎么养育」「如何理解」这类不需要取数的问题 → **交给 LLM
                     critic 判**（原版本来就是 LLM 判，不要求环境里有"数据"）
  Task/Context     → 同一句任务与 context
  输出契约不变      → {"reasoning","success","critique"}，解析失败重试 5 次

⚠️ 为什么不能一刀切「必须取数」：2026-10-19 实测，人导入「阿斯伯格孩子怎么
养育」，本机没有数据也不该有 —— 旧判据连判 4 次 no_data，还把问题当"域外"引导
换场景。原版 Voyager 的 critic 判的是**任务目标有没有达成**，不是"有没有查库"。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 题面里出现这些词 = 这题要的是**具体数据**（文件/库/统计/时效），
# 没取到真数就是没完成。反之（养育/方法/解释类）= 知识型，交给 LLM 判。
_DATA_HINTS = re.compile(
    r"(\.json|\.csv|\.db|\.duckdb|\.xlsx|\.parquet|\.txt|文件|目录|表|库|"
    r"多少|几个|几次|几成|排名|排行|统计|平均|总和|合计|最高|最低|最大|最小|"
    r"增长|下降|比例|占比|分布|趋势|频率|行数|记录|条目|字段|列名|"
    r"sql|SQL|查询|检索|数据库|"
    r"最新|最近|今年|今天|现在|当前|实时|热门|推荐|榜单|star|stars)"
)


def needs_data(question: str) -> bool:
    """这题是不是「必须取数才算完成」的数据型任务（原版没有这一档，MVE 的
    真空防线需要它：不区分题型就要求取数 = 把知识型问答全判死）。"""
    return bool(_DATA_HINTS.search(str(question or "")))

_SYSTEM = """你是 Voyager 的 critic：评估一个助手有没有完成给定的任务。

我会给你：
- 任务 Task 与上下文 Context（完成标准写在里面）
- 执行观察：它写了什么代码、调了哪些数据源、取回了什么数据、最终答案是什么、有没有报错

判定口径（照原版：只看任务目标有没有达成）：
- 任务要求达到或超过 → success=true；没达到 → success=false，并且必须给 critique
  告诉它差在哪、下一步怎么改（critique 会被拼进它的下一次尝试，别写空话）。
- 两类任务要分开看：
  · **数据型**任务（题面要求具体文件/库/统计/排名/时效事实）：必须有**真实取回
    的数据**支撑。只给结论、没取数、取回空表或与任务无关 → success=false。
  · **知识型**任务（怎么养育/如何理解/为什么、方法或解释类）：不要求取数。
    答案切题、有依据、不自相矛盾、不编造具体数字/来源 → success=true；
    空话套话、答非所问、编造事实 → success=false 并指出缺什么。
- 只看任务目标，不看代码写得漂不漂亮。
- 有「必须回答的维度清单」时（Maieutic spec gate），判定由 `maieutic_gate`
  单独做（只核清单 + 带追问），本模块不重复判一遍 —— 别让 critic 又判质量
  又判清单。

只输出 JSON（能被 json.loads 解析，不许注释/单引号）：
{"reasoning": "...", "success": true/false, "critique": "..."}
reasoning / critique 一律用中文写。
"""


def _evidence_text(result: dict[str, Any], limit: int = 1600) -> str:
    """把一轮学习结果渲染成原版那种"观察"文本（含答案，知识型任务靠它判）。"""
    ex = result.get("execution") or {}
    lines: list[str] = []
    lines.append(f"执行后端: {result.get('execution_backend') or (result.get('backend') or {}).get('kind') or '未知'}")
    lines.append(f"状态: {result.get('status') or ''}"
                 + (f"（{result.get('why')}）" if result.get("why") else ""))
    lines.append(f"数据源调用: {ex.get('data_sources') if ex.get('data_sources') is not None else '（未记录）'}")
    lines.append(f"取数: calls={ex.get('calls')} 真取到={ex.get('real_calls')} rows={ex.get('rows')} ok={ex.get('ok')}")
    sql = str(ex.get("sql") or "").strip()
    if sql:
        lines.append("执行内容（前 800 字）:\n" + sql[:800])
    ans = str(result.get("answer") or "").strip()
    if ans:
        lines.append("最终答案（RESULT）:\n" + ans[:800])
    # 观察明细（每轮返回什么、调了哪个数据源）—— 知识型任务没有数据源，
    # 但也要让 critic 看见模型到底交了什么。
    obs = result.get("observations") or []
    if obs:
        lines.append("各轮观察: " + json.dumps(obs[-3:], ensure_ascii=False)[:600])
    ab = result.get("absorbed") or {}
    if ab:
        lines.append(f"入库: topic={ab.get('topic_id')} mastered={ab.get('mastered')}")
    txt = "\n".join(str(x) for x in lines)
    return txt[:limit]


def _error_payload(answer: str) -> str:
    """答案是「报错/错误页」而不是内容 —— 返回原因，否则空串。

    2026-10-22 场景 B 实测：模型 `web_search → mcp_ddg_fetch_content` 后把
    `Client error '403 Forbidden' for url 'https://www.zhihu.com/...'` 原样
    塞进 RESULT，闸门照样判「闭合」，差点把错误页写成技能（真空成功新形态：
    有内容、但是报错文本）。原版 critic 的口径是 `onError → 不算成`，这里
    在进 LLM 之前先挡一道 —— 必须**换下一个结果/换关键词重试**。
    """
    a = str(answer or "").strip()
    if not a:
        return ""
    low = a.lower()
    _marks = ("client error", "http error", "urlerror", "timed out",
              "could not access", "could not resolve", "connection refused",
              "name or service not known", "no search results",
              "没有找到相关", "抓取失败", "请求失败")
    hit = next((m for m in _marks if m in low), "")
    # JSON 形态：{"error": ...} / {"answer": {"error": ...}}
    try:
        j = json.loads(a)
        seen = json.dumps(j, ensure_ascii=False).lower()
        if '"error"' in seen or "'error'" in seen:
            hit = hit or "error 字段"
    except Exception:
        pass
    if not hit:
        return ""
    # 报错里夹着正文的情况（长页面尾部带 error）：内容够多就不算纯报错
    body = re.sub(r"https?://\S+", "", a)
    body = re.sub(r"(?i)(client error|http error|forbidden|urlerror|"
                  r"timed out|check:|more information)[^\n]{0,80}", "", body)
    return hit if len(body.strip()) < 120 else ""


def check_task_success(task: str, context: str, result: dict[str, Any], *,
                       llm=None, max_retries: int = 5,
                       dimensions: list[dict[str, Any]] | None = None
                       ) -> tuple[bool, str]:
    """返回 (success, critique)。照原版：出错 → (False, "")；其余交 LLM 判。

    唯一的硬门槛是**数据型任务的真空**（题面要数据、却一个数据源都没调），
    知识型任务不再要求取数 —— 否则「阿斯伯格孩子怎么养育」这种问题永远判死。

    `dimensions` = Maieutic 追问骨架出的维度清单（{"id","question","why"}）。
    给了 → **交给 `maieutic_gate` 判闸门**（照 spec-examiner 的闭合口径：
    passed = gaps_still_open 为空；不判整体质量，只带回最多 3 条追问）。
    """
    ex = result.get("execution") or {}
    real = int(ex.get("real_calls") or 0)
    answer = str(result.get("answer") or "").strip()
    # 原版 critic.py:137-... `messages[1] is None → (False, "")` 的同构：
    # 执行层连答案都没产出（报错/空结果）才直接失败，不叫 LLM。
    if not answer and not real:
        return False, str(result.get("why") or "执行层没有产出答案（报错/空结果）")[:300]
    _bad = _error_payload(answer)
    if _bad:
        return False, (f"取回的是报错（{_bad}），不是内容 —— 换搜索结果里的下一个"
                       "URL，或换关键词重试；不要把错误当答案")[:300]
    if needs_data(task) and real <= 0:
        return False, ("这题要具体数据（文件/库/统计/时效），但一次数据源都没调、"
                       "没有真实取数 —— 不算完成")[:300]
    if dimensions:
        try:
            import maieutic_gate
            g = maieutic_gate.check(task, answer, dimensions, llm=llm,
                                    critique=str(context or ""))
        except Exception as e:
            g = {"origin": "failed", "reason": f"{type(e).__name__}: {e}"}
        if g.get("origin") != "failed":
            result["critic_gate"] = {"addressed": list(g.get("addressed") or []),
                                     "open": list(g.get("open") or []),
                                     "questions": list(g.get("questions") or []),
                                     "emergent": list(g.get("emergent") or [])}
            questions = list(g.get("questions") or [])
            # critique = Maieutic 的追问（原版把 critique 拼进下一次 rollout 的
            # prompt —— 迭代提示的同一条通路）
            return (not bool(g.get("open"))), " ".join(questions).strip()[:300]
    return ai_check_task_success(task, context, result, llm=llm,
                                max_retries=max_retries)


def ai_check_task_success(task: str, context: str, result: dict[str, Any], *,
                          llm=None, max_retries: int = 5
                          ) -> tuple[bool, str]:
    """原版 critic.py:112-135 的同构：解析失败递归重试，5 次都失败 → (False, "")。

    只判「任务目标达没达成」。维度清单的闭合交给 `maieutic_gate`，不在这里重判。
    """
    if max_retries <= 0:
        return False, ""
    user = (f"Task: {task}\n\nContext: {context or 'None'}\n\n"
            f"执行观察：\n{_evidence_text(result)}\n\n请按系统要求输出 JSON。")
    msgs = [{"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user}]
    try:
        if llm is not None:
            text = str(llm(msgs) or "")
        else:
            from voyager_vlml.llm import chat
            text = str(chat(msgs, temperature=0) or "")
    except Exception:
        return False, ""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return ai_check_task_success(task, context, result, llm=llm,
                                     max_retries=max_retries - 1)
    try:
        obj = json.loads(m.group(0))
        success = obj.get("success")
        if not isinstance(success, bool):
            raise ValueError("success 不是 bool")
    except Exception:
        return ai_check_task_success(task, context, result, llm=llm,
                                     max_retries=max_retries - 1)
    return bool(success), str(obj.get("critique") or "")
