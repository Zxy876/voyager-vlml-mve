"""VLML 同构的 critic（对应原版 voyager/agents/critic.py）。

原版 critic 做什么：把「观察 + Task + Context」给 LLM，让它判 success + 出 critique，
critique 回灌下一轮迭代修正（iterative prompting）。

⚠️ 本文件的关键纠正（用户反复强调，也是旧后端最大的问题）：
  旧实现用 `evaluate_vs_referee` 把 Voyager 的事实集去**比对 VLML0 裁判算出的标准
  答案**，产出的 verdict / covered / missing / score / coverage 全部喂回 Voyager 让它学
  —— 那是「拿 VLML 跑通的答案标准给 voyager 反馈」，违反设计。

  这里改成你说的：**成败由 DB 取数结果来给**
    · 成功 ⇔ 你的方法真的去库里取到了数（至少一次工具调用成功 且 取到行 > 0）
    · 失败 ⇔ 没调通 / 取到空集
  VLML 跑一遍得到的判决、得分、覆盖率 → **只在面板**，不进这里、不进 Voyager。

critique 语言：改成 **SQL 口径**（哪张表、哪个列、口径对不对、SQL 报什么错），
不含参考值、不含覆盖率、不含"标准答案是多少"。
"""

from __future__ import annotations

from typing import Any


class VLMLCriticAgent:
    """VLML 版 critic：以 DB 取证为成败判据，critique 用 SQL 语言。"""

    def __init__(self, min_rows: int = 1) -> None:
        self.min_rows = max(1, int(min_rows))

    # ---------- 成败判据：DB 取证，不比标准答案 ----------
    def check_task_success(
        self, *, observations: list[dict[str, Any]], task: dict[str, Any] | None = None
    ) -> tuple[bool, str]:
        """(success, critique)。

        success = 至少一次工具调用成功 且 取到行数 >= min_rows。
        理由：设计说「不把记住数值当作成功，而是从用方法取出数值的过程开始存技能」——
        判据是**方法有没有真的从 DB 取到数**，不是取到的数等不等于某个标准答案。
        """
        obs = list(observations or [])
        ok_calls = [o for o in obs if o.get("ok")]
        rows = sum(int(o.get("rows") or 0) for o in ok_calls)
        success = bool(ok_calls) and rows >= self.min_rows
        return success, self.render_critique(observations=obs, task=task or {})

    # ---------- critique：SQL 语言的可枚举反馈 ----------
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
    c = VLMLCriticAgent()
    cases = {
        "全失败": [{"ok": False, "tool": "query_sql", "rows": 0, "error": "no such column"}],
        "空集": [{"ok": True, "tool": "query_sql", "rows": 0, "error": ""}],
        "取到数": [{"ok": True, "tool": "query_sql", "rows": 3, "error": ""}],
    }
    for name, obs in cases.items():
        ok, critique = c.check_task_success(observations=obs, task={"task": "数回合"})
        print(f"--- {name} → success={ok}")
        print(critique)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
