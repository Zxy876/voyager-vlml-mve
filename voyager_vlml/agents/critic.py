"""VLML 同构的 critic（对应原版 `voyager/agents/critic.py`）。

原版 critic 做什么（`voyager_original/voyager/agents/critic.py`）：
    · `render_system_message`   :26-28  ← `prompts/critic.txt`
    · `render_human_message`    :30-80  ← 观察（末态）+ Task + Context
    · `ai_check_task_success`   :94-117 ← LLM → JSON {reasoning, success, critique}
                                          解析失败重试 max_retries=5 次
    · `check_task_success`      :119-141← 组装两条消息，auto 走 LLM
    · `voyager.py:232-238`              ← **每一步**判一次，成功即停
    · `prompts/critic.txt:3`            ← "Exceeding the task requirements is also
                                          considered a success while failing to meet
                                          them requires you to provide critique"
  一句话：原版判的是**「任务要求有没有被满足」**（语义 + 口径），不是「执行有没有报错」。

⚠️ 旧实现只判「取到数」，判不出口径（2026-10-22 实测）：
   `vlml_first_blood` 那条成功配方写的是 `WHERE map_name='Split' AND round_number=1`
   ——把「第一回合」当「首血」，取自 2850 行的 `ext_player_round_stats`，取到数 →
   判成功 → 入库。下次复用即错。**「跑通 ≠ 口径对」原样复现。**

现在两级（2026-10-23）：
    第一级 确定性闸门：真的从库里取到数（ok 调用 + 行数达标 + 结果非真空）。
            ← 原版没有这一级，因为 Minecraft 的观察里不存在"空表返回 1 行 (NULL,0)"
              这种形态；这是 SQL 独有的骗局，必须留（见 is_vacuous 的说明）。
    第二级 LLM 口径检验：把「SQL + 报错 + 结果行 + Task + Context」交给模型，
           问原版同一个问题 —— 任务要求满足了没有。返回 reasoning / success / critique。
    两级都过才算成功。第二级跑不成（网络/解析全失败）→ 退回第一级并如实标注，
    不让一次接口抖动把整轮判死。

critique 语言：仍是 **SQL 口径**（哪张表、哪个列、口径对不对、SQL 报什么错），
不含参考值、不含覆盖率、不含"标准答案是多少"。
"""

from __future__ import annotations

import os
from typing import Any, Iterator


# ---------------------------------------------------------------------------
# 第一级：真空结果检测（聚合查询会「假装取到了数据」）
# ---------------------------------------------------------------------------
#
# 2026-10-14 实测踩的坑（用户报「练习线能学会、消费线答不出」的那道题）：
#   技能库里 vlml_fb_conversion 的配方查 `agg_team_round_summary` —— 该表在
#   rib 库上 **0 行**。但 SQL 是纯聚合（无 GROUP BY）：
#       SELECT AVG(...) AS avg_round_win_rate, COUNT(*) AS sample_size FROM ...
#   空表上聚合**照样返回 1 行**：(NULL, 0)。于是 rows=1 ≥ min_rows=1 →
#   练习线记 success=True → 坏配方被固化进技能库；消费线复用它 →
#   回答「胜率 0%、样本量 0」（真实答案：100%、样本量 7）。
#
# 这一级是 SQL 特有的补丁（原版没有：Minecraft 的观察不会"假装"成功）。
# 它只判「有没有数」，判「数对不对」交给第二级 LLM（原版就是 LLM 判的）。

def _iter_value_rows(data: Any) -> Iterator[list[Any]]:
    """从工具返回里尽量取出「行」（每行是值序列）。兼容几种返回形态。"""
    if data is None:
        return
    if isinstance(data, dict):
        rows = None
        for key in ("rows", "data", "results", "records", "items"):
            v = data.get(key)
            if isinstance(v, list):
                rows = v
                break
        if rows is None:
            # 没有行状字段 → 把这个 dict 本身当成一行（报告类工具返回
            # {"summary": "..."} 这类，值本身就是内容）
            rows = [data]
        for r in rows:
            if isinstance(r, (list, tuple)):
                yield list(r)
            elif isinstance(r, dict):
                yield list(r.values())
            else:
                yield [r]
    elif isinstance(data, list):
        for r in data:
            if isinstance(r, (list, tuple)):
                yield list(r)
            elif isinstance(r, dict):
                yield list(r.values())
            else:
                yield [r]
    else:
        yield [data]


def _is_zeroish(v: Any) -> bool:
    """这个值算不算「空」：NULL、数值 0、空串算；布尔值（True/False）不算。"""
    if v is None:
        return True
    if isinstance(v, bool):          # 注意：Python 里 bool 是 int 子类，必须先挡
        return False
    if isinstance(v, (int, float)):
        return v == 0
    if isinstance(v, str):
        return v.strip() == ""
    return False


def is_vacuous(data: Any) -> bool:
    """结果里没有任何真实值（全 NULL，或除了 0 之外全 NULL）。

    一行真实数据都没有（空列表 / None）同样算真空。
    """
    saw_row = False
    for row in _iter_value_rows(data):
        saw_row = True
        if any(not _is_zeroish(v) for v in row):
            return False
    return True


def has_real_data(observation: dict[str, Any]) -> bool:
    """一次工具观察是否真的取到了数（ok 且结果非真空）。"""
    if not isinstance(observation, dict) or not observation.get("ok"):
        return False
    return not is_vacuous(observation.get("data"))


class VLMLCriticAgent:
    """VLML 版 critic：先确定性闸门，再按原版语义让 LLM 判口径。

    用法与原版一致：
        success, critique = critic.check_task_success(observations=..., task={...})
    需要拿到「结论句」时用 `judge()`，它多回一个 reasoning（原版 JSON 的同一个字段）。
    """

    def __init__(
        self,
        min_rows: int = 1,
        *,
        mode: str = "auto",
        use_llm: bool | None = None,
        max_retries: int = 5,       # 同构原版 `critic.py:94` 的 max_retries=5
    ) -> None:
        if mode != "auto":
            # 原版还有 manual（人在终端敲 y/n，`critic.py:82-92`）；MVE 是无人值守
            # 学习循环，没人给它敲键盘 —— 如实不支持，不假装支持。
            raise ValueError(f"MVE 只支持 auto 模式（原版 manual 要人敲键盘）：{mode}")
        self.min_rows = max(1, int(min_rows))
        self.mode = mode
        if use_llm is None:
            use_llm = os.getenv("MVE_CRITIC_LLM", "1").strip() != "0"
        self.use_llm = bool(use_llm)
        self.max_retries = max(0, int(max_retries))
        # 最近一次的判决细节（给上层取"结论句"、给日志看走了哪一级）
        self.last_gate = ""
        self.last_reasoning = ""
        self.last_critique = ""

    # ---------- 第一级：确定性闸门 ----------
    def gate(self, observations: list[dict[str, Any]]) -> tuple[bool, str]:
        """(是否够格进第二级, 闸门名)。

        闸门名：no_call（没调用）/ no_rows（0 行）/ vacuous（全空值）/ pass
        """
        obs = list(observations or [])
        ok_calls = [o for o in obs if o.get("ok")]
        if not obs:
            return False, "no_call"
        if not ok_calls:
            return False, "no_call"
        rows = sum(int(o.get("rows") or 0) for o in ok_calls)
        if rows < self.min_rows:
            return False, "no_rows"
        if not any(has_real_data(o) for o in ok_calls):
            return False, "vacuous"
        return True, "pass"

    # ---------- 同构 agents/critic.py:26-28 render_system_message ----------
    def render_system_message(self) -> str:
        from voyager_vlml.prompts import load_prompt

        return load_prompt("critic")

    # ---------- 同构 agents/critic.py:30-80 render_human_message ----------
    def render_human_message(
        self, *, events: list[dict[str, Any]], task: str,
        context: str = "", sql: str = "",
    ) -> str:
        """观察 + SQL + Task + Context。

        原版这段给的是 Minecraft 的末态（biome/背包/血量…）：那些就是"我做成了什么"的
        全部证据。SQL 世界的末态只有几行数字，**口径只能从 SQL 本身看出来**
        （过滤了哪个列、按什么分组）—— 所以这里比原版多给一段 `SQL:`。
        这是字段映射（背包 → 行列、代码 → SQL），不是新增规则。
        """
        from voyager_vlml.agents.action import _render_rows   # 懒加载，避免循环 import

        lines: list[str] = []
        lines.append(f"SQL: {sql.strip() or '（本轮没有执行 SQL）'}\n")
        errs = [str(e.get("error") or "调用失败") for e in events if not e.get("ok")]
        lines.append("Execution error: " + ("\n".join(errs) if errs else "No error") + "\n")
        ok_events = [e for e in events if e.get("ok")]
        if ok_events:
            for e in ok_events:
                lines.append(
                    f"Result ({e.get('rows')} rows):\n{_render_rows(e.get('data'))}\n")
        else:
            lines.append("Result: None\n")
        lines.append(f"Task: {task}\n")
        lines.append(f"Context: {context}\n" if context else "Context: None\n")
        return "\n".join(lines)

    # ---------- 同构 agents/critic.py:94-117 ai_check_task_success ----------
    def ai_check_task_success(
        self, messages: list[dict[str, str]], max_retries: int | None = None
    ) -> tuple[bool, str, str]:
        """(success, critique, reasoning)。解析不了就重来，最多 max_retries 次。

        原版靠 `fix_and_parse_json` 宽容解析 + 递归重试；这里用 `llm.chat_json`
        （同样是「剥围栏 + 取最外层大括号」的宽容解析）+ 同一个重试次数。
        """
        n = self.max_retries if max_retries is None else int(max_retries)
        if n == 0:
            print("Critic：回复解析不了的次数用尽 —— 判失败（同原版返回 False）。")
            return False, "", ""

        from voyager_vlml.llm import chat_json

        obj = chat_json(messages, temperature=0)
        if not isinstance(obj, dict) or obj.get("success") not in (True, False):
            # 原版这里 `assert response["success"] in [True, False]` 失败 → 递归重试
            print("Critic：回复不是 {reasoning, success, critique} 形状 · Trying again!")
            return self.ai_check_task_success(messages, n - 1)
        return (
            bool(obj["success"]),
            str(obj.get("critique") or ""),
            str(obj.get("reasoning") or ""),
        )

    # ---------- 同构 agents/critic.py:119-141 check_task_success（两级） ----------
    def judge(
        self, *, observations: list[dict[str, Any]], task: dict[str, Any] | None = None,
        context: str = "", sql: str = "",
    ) -> dict[str, Any]:
        """返回 {"success","critique","reasoning","gate"}。

        gate：no_call / no_rows / vacuous（第一级拦下）
              llm（两级都过）/ llm_failed（第二级没跑成，退回第一级）
              llm_off（关掉了第二级，等价旧行为）
        """
        task = task or {}
        task_text = str(task.get("task") or "")
        ctx = context or "\n".join(str(h) for h in (task.get("hints") or []) if h)

        passed, gate = self.gate(observations)
        self.last_gate = gate
        if not passed:
            crit = self.render_critique(observations=observations, task=task)
            self.last_critique, self.last_reasoning = crit, ""
            return {"success": False, "critique": crit, "reasoning": "", "gate": gate}

        if not self.use_llm:
            crit = self.render_critique(observations=observations, task=task)
            self.last_gate, self.last_critique = "llm_off", crit
            return {"success": True, "critique": crit, "reasoning": "", "gate": "llm_off"}

        messages = [
            {"role": "system", "content": self.render_system_message()},
            {"role": "user", "content": self.render_human_message(
                events=observations, task=task_text, context=ctx, sql=sql)},
        ]
        try:
            ok, critique, reasoning = self.ai_check_task_success(messages)
        except Exception as e:      # 接口抖动不该把整轮判死
            print(f"Critic：口径检验没跑成（{type(e).__name__}: {e}）→ 退回第一级判据")
            crit = self.render_critique(observations=observations, task=task)
            note = f"（口径检验没跑成：{type(e).__name__}；这次只按「取到数」判）"
            self.last_gate, self.last_critique = "llm_failed", note + crit
            return {"success": True, "critique": note + crit, "reasoning": "",
                    "gate": "llm_failed"}

        self.last_gate = "llm"
        self.last_reasoning = reasoning
        self.last_critique = critique
        return {"success": bool(ok), "critique": critique, "reasoning": reasoning,
                "gate": "llm"}

    def check_task_success(
        self, *, observations: list[dict[str, Any]], task: dict[str, Any] | None = None,
        sql: str = "",
    ) -> tuple[bool, str]:
        """兼容壳：(success, critique)。需要结论句时用 `judge()`。"""
        d = self.judge(observations=observations, task=task, sql=sql)
        return bool(d["success"]), str(d["critique"])

    # ---------- critique：SQL 语言的可枚举反馈（第一级拦下时用） ----------
    def render_critique(
        self, *, observations: list[dict[str, Any]], task: dict[str, Any]
    ) -> str:
        """回给 Voyager 的话：工具成败、取到几行、SQL 报什么错、口径提示。

        刻意**不含**：VLML 判决、得分、覆盖率、参考答案数值。
        """
        lines: list[str] = []
        if task.get("task"):
            lines.append(f"任务：{task['task']}")

        ok_calls = [o for o in observations if o.get("ok")]
        rows = sum(int(o.get("rows") or 0) for o in ok_calls)

        if not observations:
            lines.append("本轮没有任何工具调用 —— 链路没跑通（不是「没学会」）。")
            lines.append("下一步：先用 get_database_info() 摸清库里有哪些表，再写 SELECT。")
            return "\n".join(lines)

        # 逐条回报「工具调用成功还是失败 / 取到几行」
        lines.append("取数过程（可枚举）：")
        for o in observations:
            tool = o.get("tool", "?")
            if o.get("ok"):
                lines.append(f"  ✓ {tool}：取到 {o.get('rows', 0)} 行")
            else:
                lines.append(f"  ✗ {tool}：{o.get('error') or '调用失败'}")

        if not ok_calls:
            lines.append("所有取数都失败了 —— 链路没跑通。检查表名/列名是否在库里、SQL 是否可执行。")
        elif not any(has_real_data(o) for o in ok_calls):
            lines.append(
                f"取到了调用但结果里**没有任何真实值**（全 NULL / 除了 0 之外全 NULL，"
                f"共 {rows} 行）—— 这通常是查了**空表**或过滤条件太窄："
                f"聚合查询在空表上也会返回 1 行 (NULL, 0)，别被行数骗了。"
                f"换一张 schema 里标注了行数的表，或放宽 WHERE 条件重试。")
        elif rows < self.min_rows:
            lines.append(f"取到了调用但结果为空（0 行）—— 口径或过滤条件可能不对，换 WHERE 条件重试。")
        else:
            lines.append(f"已从库里取到 {rows} 行 —— 方法跑通了。")

        # SQL 语言的口径提示（不是答案）
        hints = task.get("hints") or []
        for h in hints[:4]:
            lines.append(f"口径提示：{h}")
        return "\n".join(lines)


def _main() -> int:
    """冒烟：三种情形的判据（全失败 / 空集 / 取到数）。"""
    c = VLMLCriticAgent(use_llm=False)
    cases = {
        "全失败": [{"ok": False, "tool": "query_sql", "rows": 0, "error": "no such column"}],
        "空集": [{"ok": True, "tool": "query_sql", "rows": 0, "error": ""}],
        "取到数": [{"ok": True, "tool": "query_sql", "rows": 3, "error": "",
                    "data": {"columns": ["n"], "rows": [[3]]}}],
    }
    for name, obs in cases.items():
        ok, critique = c.check_task_success(observations=obs, task={"task": "数回合"})
        print(f"--- {name} → success={ok} gate={c.last_gate}")
        print(critique)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
