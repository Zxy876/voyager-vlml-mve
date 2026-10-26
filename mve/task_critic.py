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
- **答案对数**（原版 critic.txt 的例题口径：拿背包里 `'oak_log':2` 对照
  "Mine 3 wood logs"）：答案里的每个具体数字都必须能从「观察」里的**真实
  行值**指认或算出来；对不上、指认不了 → success=false，并在 critique 里
  指出差在哪。
- 有「必须回答的维度清单」时（Maieutic spec gate），判定由 `maieutic_gate`
  单独做（只核清单 + 带追问），本模块不重复判一遍 —— 别让 critic 又判质量
  又判清单。

只输出 JSON（能被 json.loads 解析，不许注释/单引号）：
{"reasoning": "...", "success": true/false, "critique": "..."}
reasoning / critique 一律用中文写。
"""


def _evidence_text(result: dict[str, Any], limit: int = 2600) -> str:
    """把一轮学习结果渲染成原版那种"观察"文本（含答案，知识型任务靠它判）。

    原版防幻觉的核心（voyager_original/voyager/agents/critic.py:30-80）：
    判官看到的是**环境真实末态**——背包里每个物品的真实数量
    （critic.txt 例题就是拿 `{'oak_log':2}` 对照 "Mine 3 wood logs"），
    而不是执行者的转述。SQL 同构 = 取回的**真实行值**。
    2026-10-24 取证：这里原来只给调用计数 + obs 截断 JSON，判官看不到行值，
    只能凭叙事合理性猜 —— glm-4-plus 漏放 2 条错配方的残余随机性正是从
    这里来的（内层 VLMLCriticAgent 一直有行：agents/critic.py:194）。
    修法：每个观察渲染真实行（复用 _render_rows）+ 明示报错（原版
    critic.py:42-45 onError 对判官可见的同构）。
    """
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
    # 观察明细：每轮调了哪个数据源、返回的**真实行值**、报了什么错。
    # 知识型任务没有数据源，但也要让 critic 看见模型到底交了什么。
    obs = result.get("observations") or []
    if obs:
        try:
            from voyager_vlml.agents.action import _render_rows as _rr
        except Exception:
            def _rr(data: Any, max_rows: int = 4, max_cols: int = 12) -> str:
                return json.dumps(data, ensure_ascii=False)[:400]
        for i, o in enumerate(obs[-3:]):
            ok = bool(o.get("ok"))
            lines.append(f"观察{i+1}: tool={o.get('tool') or '未知'} ok={ok} rows={o.get('rows')}")
            if ok and o.get("data") is not None:
                lines.append(_rr(o.get("data"), max_rows=4))
            elif not ok:
                lines.append("报错: " + str(o.get("error") or "调用失败")[:150])
    else:
        lines.append("各轮观察: （无）")
    ab = result.get("absorbed") or {}
    if ab:
        lines.append(f"入库: topic={ab.get('topic_id')} mastered={ab.get('mastered')}")
    txt = "\n".join(str(x) for x in lines)
    return txt[:limit]


def check_task_success(task: str, context: str, result: dict[str, Any], *,
                       llm=None, max_retries: int = 5,
                       dimensions: list[dict[str, Any]] | None = None,
                       env_obs: str = "",
                       prior_questions: list[str] | None = None
                       ) -> tuple[bool, str]:
    """返回 (success, critique)。照原版：出错 → (False, "")；其余交 LLM 判。

    唯一的硬门槛是**数据型任务的真空**（题面要数据、却一个数据源都没调），
    知识型任务不再要求取数 —— 否则「阿斯伯格孩子怎么养育」这种问题永远判死。

    `dimensions` = Maieutic 追问骨架出的维度清单（{"id","question","why"}）。
    给了 → **交给 `maieutic_gate` 判闸门**（照 spec-examiner 的闭合口径：
    passed = gaps_still_open 为空；不判整体质量，只带回最多 3 条追问）。

    `prior_questions` = 本次闭合已问过的追问 → 透传闸门防重复问（同构
    spec-examiner 的 priorIterations；步级闸门每步都判，不给历史会重复问）。
    """
    ex = result.get("execution") or {}
    real = int(ex.get("real_calls") or 0)
    answer = str(result.get("answer") or "").strip()
    # 原版 critic.py:137-... `messages[1] is None → (False, "")` 的同构：
    # 执行层连答案都没产出（报错/空结果）才直接失败，不叫 LLM。
    if not answer and not real:
        return False, str(result.get("why") or "执行层没有产出答案（报错/空结果）")[:300]
    # 材料账（同构 maieutic 的 `gathered`／`report` 两本账）：evidence 由执行层
    # 按**实际返回**剪裁 —— 403/timeout/空返回一律 ok=False，天然进不了材料。
    # 所以这里判的是「有没有材料、材料答没答到」，不需要按错误字符串去猜
    # （用户 2026-10-22：「同构了就不用防 403 和异常」）。
    ev = result.get("evidence")
    if isinstance(ev, list):
        ok_items = [e for e in ev if isinstance(e, dict) and e.get("ok")]
        if not ok_items:
            fails = [str(e.get("error") or "").strip()[:60]
                     for e in ev if isinstance(e, dict) and not e.get("ok")]
            why = "；".join([f for f in fails if f][:3]) or "没有调用任何工具"
            return False, (f"一次都没取到内容（{why}）—— 换关键词或换来源重来；"
                           "不许拿报错或记忆当答案")[:300]
    if needs_data(task) and real <= 0:
        # 区分「一次都没调」与「调了但全是空值」——这两件事要模型改的方向完全不同。
        # 2026-10-23 工资场景实测：4 次调用各返回 1 行、SUM 全是 NULL（LEFT JOIN
        # 没匹配 + `+ al.amount` 让 NULL 传染），判词却写「一次数据源都没调」，
        # 模型拿着这句反馈无从下手，4 轮原地打转。原版 critic 的职责是
        # 「失败必须给能改进的 critique」（prompts/critic.txt），所以这里要说真话。
        calls = int(ex.get("calls") or 0)
        rows = int(ex.get("rows") or 0)
        if calls > 0:
            errs = [str(o.get("error") or "").strip()[:80]
                    for o in (result.get("observations") or [])
                    if not o.get("ok")]
            tail = ("；报错：" + "；".join(e for e in errs if e)[:120]) if errs else ""
            return False, (
                f"确实调了 {calls} 次数据源、取回 {rows} 行，但**每一行都是空值"
                f"（NULL/0）**，所以不算取到数{tail}。"
                "常见原因：LEFT JOIN 没匹配上；或对可能为 NULL 的列直接做加法/SUM"
                "（NULL 会传染整行）。先 `SELECT * FROM 表 LIMIT 1` 看清列与值，"
                "再对可空的列套 COALESCE，或把右表先按键聚合成一行再 join。"
            )[:400]
        return False, ("这题要具体数据（文件/库/统计/时效），但一次数据源都没调、"
                       "没有真实取数 —— 不算完成")[:300]
    if dimensions:
        try:
            import maieutic_gate
            g = maieutic_gate.check(task, answer, dimensions, llm=llm,
                                    critique=str(context or ""),
                                    evidence=result.get("evidence"),
                                    env_obs=str(env_obs or ""),
                                    prior_questions=prior_questions)
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
            import os as _os
            # 判官模型可单配（2026-10-24 实测：glm-4-flash 当 judge，同一份
            # 带环境观察的输入 3 次全放行一个明显错误的答案 —— 判官不能省
            # 模型钱；出题/执行层可继续用 flash）。env MVE_CRITIC_MODEL
            # 一设即切，不设回落全局模型，行为不变。
            text = str(chat(msgs, temperature=0,
                            model=_os.getenv("MVE_CRITIC_MODEL", "")) or "")
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
