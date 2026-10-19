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
                     data / data_sources / rows / sql / error）
  onError → None   → 没有取到真数（data_sources 为空 或 ok=False）→ 直接判 False，
                     不叫 LLM（比原版更严：真空成功在 MVE 是硬性失败）
  Task/Context     → 同一句任务与 context
  输出契约不变      → {"reasoning","success","critique"}，解析失败重试 5 次

⚠️ 只判「任务目标在观察里是否达成」，**不拿 VLML 判分当完成判据**（用户硬约束）。
"""

from __future__ import annotations

import json
import re
from typing import Any

_SYSTEM = """你是 Voyager 的 critic：评估一个「只读本机工作区」的助手有没有完成给定任务。

我会给你：
- 任务 Task 与上下文 Context（完成标准写在里面）
- 执行观察：它写/跑了什么、调了哪些数据源、取回了什么数据、有没有报错

判定口径（照原版）：
- 任务要求达到或超过 → success=true；没达到 → success=false，并且必须给 critique
  告诉它差在哪、下一步怎么改（critique 会被拼进它的下一次尝试，别写空话）。
- 观察里的数据是**真实取回的**才能算成功；只说了结论、没取数、数据与任务无关、
  或取回的是空表/错误 → success=false。
- 只看任务目标，不看代码写得漂不漂亮。

只输出 JSON（能被 json.loads 解析，不许注释/单引号）：
{"reasoning": "...", "success": true/false, "critique": "..."}
"""


def _evidence_text(result: dict[str, Any], limit: int = 1600) -> str:
    """把一轮学习结果渲染成原版那种"观察"文本。"""
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
    # 观察明细（data）——local_agent 的 observations 没直接进 result，但有 execution 汇总；
    # 若有 absorbed/结晶，说明取到过真值。
    ab = result.get("absorbed") or {}
    if ab:
        lines.append(f"入库: topic={ab.get('topic_id')} mastered={ab.get('mastered')}")
    txt = "\n".join(str(x) for x in lines)
    return txt[:limit]


def check_task_success(task: str, context: str, result: dict[str, Any], *,
                       llm=None, max_retries: int = 5) -> tuple[bool, str]:
    """返回 (success, critique)。原版语义：出错/没取到数 → (False, ""), 不叫 LLM。"""
    ex = result.get("execution") or {}
    real = int(ex.get("real_calls") or 0)
    ok = bool(ex.get("ok"))
    # 原版 critic.py:137-... `messages[1] is None → (False, "")` 的同构：
    # 没有真取到数（真空/报错）就不必叫 LLM 判，直接失败。
    if not ok or real <= 0:
        return False, str(result.get("why") or "没取到真数（执行层失败/空结果）")[:300]
    return ai_check_task_success(task, context, result, llm=llm,
                                max_retries=max_retries)


def ai_check_task_success(task: str, context: str, result: dict[str, Any], *,
                          llm=None, max_retries: int = 5) -> tuple[bool, str]:
    """原版 critic.py:112-135 的同构：解析失败递归重试，5 次都失败 → (False, "")。"""
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
