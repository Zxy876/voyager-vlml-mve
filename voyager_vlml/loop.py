"""VLML 同构学习循环的最小驱动（⑤ 冒烟用，面板后置）。

闭环：
  curriculum.propose_next_task()   ← 里程碑树选题（种子 → 树干，已完成排除）
      ↓
  action.run_sql(...)              ← 在 VLML MCP(DB/RIB/VLGG) 里跑 SQL 取数
      ↓
  critic.check_task_success()      ← **成败 = 有没有真的从 DB 取到数**（不比标准答案）
      ↓
  curriculum.update_exploration_progress()  ← 跑通→解锁里程碑+挂技能；否则记失败/跳过

⚠️ 整条链里没有 VLML 判决、没有覆盖率、没有参考答案 —— 那是给人看的，只在面板。
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from voyager_vlml.agents.action import VLMLActionAgent  # noqa: E402
from voyager_vlml.agents.critic import VLMLCriticAgent  # noqa: E402
from voyager_vlml.agents.curriculum import VLMLCurriculumAgent  # noqa: E402


def _gran_table(name: str) -> str:
    """从知识点名的括号取粒度标记（rounds / games / series / base_events…）。
    与 open_quiz._gran_table 同一套数据驱动做法：标记本身就是表名。"""
    m = re.search(r"[（(]([^（）()]+)[）)]", name or "")
    return m.group(1).strip() if m else ""


async def run_one_round(
    scope: dict[str, Any] | None = None, topic_id: str | None = None
) -> dict[str, Any]:
    """跑一轮：选题 → 取数 → 判据 → 改写图谱。

    topic_id 给定时钉住该知识点（冒烟/调试用）；不给则走课程的里程碑选题。
    """
    curr = VLMLCurriculumAgent(scope=scope)
    act = VLMLActionAgent()
    crit = VLMLCriticAgent()

    if topic_id:
        nxt = {
            "task": f"拿下里程碑 {topic_id}",
            "context": curr._topic_context(topic_id),
            "topic_id": topic_id,
            "exhausted": False,
            "reason": "explicit",
        }
    else:
        nxt = curr.propose_next_task()
    tid = nxt["topic_id"]
    if not tid:
        return {"topic_id": "", "exhausted": True, "reason": nxt["reason"]}

    topic = curr.graph.topics.get(tid) or {}
    table = _gran_table(str(topic.get("name") or ""))
    sql = f"SELECT COUNT(*) AS n FROM {table}" if table else ""

    observations: list[dict[str, Any]] = []
    if sql:
        observations.append(await act.run_sql(sql))

    success, critique = crit.check_task_success(
        observations=observations,
        task={"task": nxt["task"], "hints": list(topic.get("skills") or [])[:3]},
    )

    curr.update_exploration_progress(
        {
            "topic_id": tid,
            "success": success,
            "verdict": "ok" if success else "no_evidence",
            "reason": "已从库取到数" if success else "未从库取到数（链路/口径问题）",
            "skill_key": f"{tid}::count_{table}" if (success and table) else "",
        }
    )
    return {
        "topic_id": tid,
        "exhausted": False,
        "reason": nxt["reason"],
        "sql": sql,
        "observations": observations,
        "success": success,
        "progress": curr.progress,
    }


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="VLML 同构学习循环 · 一轮冒烟")
    ap.add_argument("--topic", default="", help="钉住某个知识点（不给则走课程选题）")
    a = ap.parse_args()
    r = asyncio.run(run_one_round(topic_id=a.topic or None))
    print("=== 一轮闭环 ===")
    print("选题     :", r.get("topic_id"), "| reason:", r.get("reason"))
    print("SQL      :", r.get("sql"))
    for o in r.get("observations") or []:
        print("  观察   :", {k: o[k] for k in ("ok", "tool", "rows", "error")})
    print("成功     :", r.get("success"), "（判据 = 是否真的从 DB 取到数）")
    print("进度     :", r.get("progress"), "个里程碑已解锁")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
