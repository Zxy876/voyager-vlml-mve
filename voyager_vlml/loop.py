"""VLML 同构学习循环的最小驱动（⑤ 冒烟用，面板后置）。

闭环（真正自己出题取数）：
  curriculum.propose_next_task()   ← 里程碑树选题（种子 → 树干，已完成排除）
      ↓
  question.generate(topic)         ← LLM 按里程碑节点**出题**（只出题面，不给答案）
      ↓
  action.solve(question)           ← LLM **写 SQL** → VLML MCP(DB/RIB/VLGG) 执行
                                     失败则带 critique 迭代重写（最多 3 轮）
      ↓
  critic.check_task_success()      ← **成败 = 有没有真的从 DB 取到数**（不比标准答案）
      ↓
  curriculum.update_exploration_progress()  ← 跑通→解锁里程碑 + 挂 SQL 技能指针
                                             否则记失败/跳过（带原因）

⚠️ 整条链里没有 VLML 判决、没有覆盖率、没有参考答案 —— 那些是给人看的，只在面板。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from voyager_vlml.agents.action import VLMLActionAgent  # noqa: E402
from voyager_vlml.agents.critic import VLMLCriticAgent  # noqa: E402
from voyager_vlml.agents.curriculum import VLMLCurriculumAgent  # noqa: E402
from voyager_vlml.agents.question import VLMLQuestionAgent  # noqa: E402
from voyager_vlml.env.vlml_mcp import VLMLMCPEnv  # noqa: E402


async def run_one_round(
    scope: dict[str, Any] | None = None,
    topic_id: str | None = None,
    use_llm: bool = True,
    max_iters: int = 3,
) -> dict[str, Any]:
    """跑一轮：里程碑选题 → LLM 出题 → LLM 写 SQL 取数 → DB 取证判据 → 改写图谱。"""
    env = VLMLMCPEnv()
    curr = VLMLCurriculumAgent(scope=scope)
    act = VLMLActionAgent(env=env)
    crit = VLMLCriticAgent()

    # ---- 选题（里程碑树）----
    if topic_id:
        tid, reason = topic_id, "explicit"
    else:
        nxt = curr.propose_next_task()
        tid, reason = nxt["topic_id"], nxt["reason"]
        if not tid:
            return {"topic_id": "", "exhausted": True, "reason": nxt["reason"]}
    topic = curr.graph.topics.get(tid) or {}

    # ---- 出题（LLM；只出题面，不给参考答案）----
    question = ""
    if use_llm:
        try:
            question = await VLMLQuestionAgent(env=env).generate(topic) or ""
        except Exception as e:
            print(f"  （出题失败：{type(e).__name__}: {e}）")
    if not question:
        # 兜底：用知识点自带的问法示例，保证链路仍可跑
        ex = topic.get("examples") or []
        question = (
            str(ex[0].get("prompt") or "") if ex and isinstance(ex[0], dict) else ""
        ) or f"按「{topic.get('name') or tid}」这个知识点，从库里取一个数。"

    # ---- 写 SQL + 取数（LLM + VLML MCP，带迭代修正）----
    observations, sql = await act.solve(
        question, max_iters=max_iters, hints=list(topic.get("skills") or [])[:3]
    )

    # ---- 判成败：是否真的从 DB 取到数 ----
    success, critique = crit.check_task_success(
        observations=observations,
        task={"task": question, "hints": list(topic.get("skills") or [])[:3]},
    )

    # ---- 存技能：可复用 SQL，主键 = 图谱问题类型 topic_id ----
    skill_key = ""
    if success and sql:
        try:
            import skill_store

            _, skill_key = skill_store.add(
                tid,
                tid,  # 主键 = 索引到的图谱问题类型（设计）
                f"自己写 SQL 取数跑通：{question[:60]}",
                code=sql,
                blueprint={"source": "practice", "question": question},
                source="practice",
            )
        except Exception as e:  # pragma: no cover
            print(f"  （存技能失败：{type(e).__name__}: {e}）")

    curr.update_exploration_progress(
        {
            "topic_id": tid,
            "success": success,
            "verdict": "ok" if success else "no_evidence",
            "reason": "已从库取到数" if success else "未从库取到数（链路/口径问题）",
            "skill_key": skill_key,
        }
    )
    return {
        "topic_id": tid,
        "reason": reason,
        "question": question,
        "sql": sql,
        "observations": observations,
        "success": success,
        "critique": critique,
        "skill_key": skill_key,
        "progress": curr.progress,
    }


def _main() -> int:
    ap = argparse.ArgumentParser(description="VLML 同构学习循环 · 一轮（自己出题取数）")
    ap.add_argument("--topic", default="", help="钉住某个知识点（不给则走课程选题）")
    ap.add_argument("--no-llm", action="store_true", help="不出题不写 SQL（仅演示骨架）")
    ap.add_argument("--iters", type=int, default=3, help="写 SQL 的迭代修正轮数")
    a = ap.parse_args()

    r = asyncio.run(
        run_one_round(
            topic_id=a.topic or None, use_llm=not a.no_llm, max_iters=a.iters
        )
    )
    print("=== 一轮闭环（自己出题取数）===")
    print("选题     :", r.get("topic_id"), "| reason:", r.get("reason"))
    print("题面     :", r.get("question"))
    print("SQL      :", r.get("sql"))
    for o in r.get("observations") or []:
        print("  观察   :", {k: o[k] for k in ("ok", "tool", "rows", "error")})
    print("成功     :", r.get("success"), "（判据 = 是否真的从 DB 取到数）")
    print("技能     :", r.get("skill_key") or "（未存）")
    print("进度     :", r.get("progress"), "个里程碑已解锁")
    if r.get("critique"):
        print("--- 反馈（SQL 语言，无答案/覆盖率）---")
        print(r["critique"])
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
