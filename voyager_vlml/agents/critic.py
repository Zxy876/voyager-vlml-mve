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

from typing import Any, Iterator


# ---------------------------------------------------------------------------
# 真空结果检测：聚合查询会「假装取到了数据」
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
# 原版 Voyager 为什么没这个坑：它的 critic 是 **LLM 看 events 判「任务到底
# 做成没有」**（`critic.py` 的 check_task_success 收 events 让模型判）。
# 我们这边 critic 刻意不用 LLM（可枚举反馈），就必须把「取到数」的定义
# 从「工具 ok 且返回行数 ≥ 1」收紧到「**至少有一个真实值**」——
# 全 NULL、或除了 0 之外全 NULL 的结果，等于没取到数。

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
    """VLML 版 critic：以 DB 取证为成败判据，critique 用 SQL 语言。"""

    def __init__(self, min_rows: int = 1) -> None:
        self.min_rows = max(1, int(min_rows))

    # ---------- 成败判据：DB 取证，不比标准答案 ----------
    def check_task_success(
        self, *, observations: list[dict[str, Any]], task: dict[str, Any] | None = None
    ) -> tuple[bool, str]:
        """(success, critique)。

        success = 至少一次工具调用成功 且 取到行数 >= min_rows 且 **取到了真实值**。
        理由：设计说「不把记住数值当作成功，而是从用方法取出数值的过程开始存技能」——
        判据是**方法有没有真的从 DB 取到数**，不是取到的数等不等于某个标准答案。

        ⚠️ 第三条「真实值」是 2026-10-14 补的：纯聚合 SQL 在空表上返回
        1 行 (NULL, 0)，行数判据会被它骗过（详见 is_vacuous 的 docstring）。
        """
        obs = list(observations or [])
        ok_calls = [o for o in obs if o.get("ok")]
        rows = sum(int(o.get("rows") or 0) for o in ok_calls)
        real = any(has_real_data(o) for o in ok_calls)
        success = bool(ok_calls) and rows >= self.min_rows and real
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
