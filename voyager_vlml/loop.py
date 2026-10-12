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


async def _generate_new_task(curr: Any, env: Any) -> str:
    """范围内已无可拿下的任务 → **生成一个新任务**（设计：整轮不结束，不断生成新任务）。

    topic_graph 没有运行期登记接口，这里往 `curr.graph.topics` 里加一个内存节点
    （无前置 → 立即可选），掌握度仍落在 MasteryStore 里，重启后进度不丢。
    """
    from voyager_vlml.llm import chat_json

    schema = ""
    try:
        schema = await env.schema_block()
    except Exception:
        schema = ""

    out = chat_json(
        [
            {
                "role": "system",
                "content": (
                    "你是 Valorant 电竞数据分析的课程设计者。当前库的知识点都被拿下或"
                    "暂时拿不下了，请**新提一个**还能用 SQL 从库里取数回答的知识点。"
                    "只返回 JSON：{\"name\": \"知识点名（含粒度，如 回合粒度（rounds））\","
                    " \"question\": \"一个具体题面\", \"skills\": [\"一两条取数要点\"]}"
                ),
            },
            {
                "role": "user",
                "content": (
                    "当前库 schema：\n"
                    f"{schema}\n\n"
                    "已有的知识点不要重复。请只返回 JSON。"
                ),
            },
        ],
        temperature=0.7,
    )
    name = str(out.get("name") or "").strip() or "运行期新任务"
    question = str(out.get("question") or "").strip()
    skills = [str(s) for s in (out.get("skills") or [])][:2]

    n = 1
    while f"runtime_{n}" in curr.graph.topics:
        n += 1
    tid = f"runtime_{n}"
    curr.graph.topics[tid] = {
        "id": tid,
        "name": name,
        "chapter": "运行期生成",
        "depth": 9,          # 排在种子之后
        "difficulty": 0.5,
        "skills": skills,
        "examples": [{"prompt": question}] if question else [],
        "prerequisites": [],  # 无前置 → 立即可选
    }
    return tid


async def run_one_round(
    scope: dict[str, Any] | None = None,
    topic_id: str | None = None,
    use_llm: bool = True,
    max_iters: int = 3,
    curr: Any = None,
    env: Any = None,
) -> dict[str, Any]:
    """跑一轮：里程碑选题 → LLM 出题 → LLM 写 SQL 取数 → DB 取证判据 → 改写图谱。"""
    env = env or VLMLMCPEnv()
    curr = curr or VLMLCurriculumAgent(scope=scope)
    act = VLMLActionAgent(env=env)
    crit = VLMLCriticAgent()

    # ---- 选题（里程碑树）----
    generated = False
    if topic_id:
        tid, reason = topic_id, "explicit"
    else:
        nxt = curr.propose_next_task()
        tid, reason = nxt["topic_id"], nxt["reason"]
        if not tid:
            # 范围内已无可拿下的任务 → 生成新任务（设计：整轮不结束，不断生成新任务）
            tid = await _generate_new_task(curr, env)
            reason = "generated_new_task"
            generated = True
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

    # 落一行 run_log，字段对齐面板读取（面板后置，但先保持数据连续性）。
    # ⚠️ 不编造 coverage / score：那是 VLML 判分的事，新后端不判分，如实写 None，
    #    judge 标成 db_evidence 以示区别。
    try:
        import run_log

        ok_calls = [o for o in observations if o.get("ok")]
        run_log.append(
            {
                "mode": "curriculum",
                "topic_id": tid,
                "task": tid,
                "question": question,
                "round": 1,
                "hint": "",
                "coverage": None,
                "score": None,
                "verdict": "ok" if success else "no_evidence",
                "success": success,
                "done": bool(success),
                "evidence_status": "collected" if success else "none",
                "judge": "db_evidence",
                "no_tool_calls": not ok_calls,
                "feedback": {},
                "tool_calls": len(observations),
                "errors": [
                    str(o.get("error") or "")
                    for o in observations
                    if not o.get("ok")
                ],
                "covered": [],
                "missing": [] if success else [question],
                "narrative": "",
                "generated_task": generated,
                "sql": sql,
                "skill_key": skill_key,
            }
        )
    except Exception as e:  # pragma: no cover
        print(f"  （写 run_log 失败：{type(e).__name__}: {e}）")
    return {
        "topic_id": tid,
        "reason": reason,
        "generated": generated,
        "question": question,
        "sql": sql,
        "observations": observations,
        "success": success,
        "critique": critique,
        "skill_key": skill_key,
        "progress": curr.progress,
    }


async def run_rounds(
    n: int,
    *,
    scope: dict[str, Any] | None = None,
    use_llm: bool = True,
    max_iters: int = 3,
) -> list[dict[str, Any]]:
    """连跑 n 轮（共享 env 与课程状态；设计：一直跑到你点停止，整轮不结束）。"""
    env = VLMLMCPEnv()
    curr = VLMLCurriculumAgent(scope=scope)
    out: list[dict[str, Any]] = []
    for i in range(n):
        r = await run_one_round(
            scope=scope,
            use_llm=use_llm,
            max_iters=max_iters,
            curr=curr,
            env=env,
        )
        r["round"] = i + 1
        out.append(r)
        print(
            f"[第 {i+1}/{n} 轮] {r['topic_id']} · reason={r['reason']}"
            f"{' (新生成)' if r.get('generated') else ''}"
            f" · 成功={r['success']} · 进度={r['progress']}"
        )
    return out


def _main() -> int:
    ap = argparse.ArgumentParser(description="VLML 同构学习循环 · 一轮（自己出题取数）")
    ap.add_argument("--topic", default="", help="钉住某个知识点（不给则走课程选题）")
    ap.add_argument("--no-llm", action="store_true", help="不出题不写 SQL（仅演示骨架）")
    ap.add_argument("--iters", type=int, default=3, help="写 SQL 的迭代修正轮数")
    ap.add_argument("--rounds", type=int, default=1, help="连跑几轮（>1 时共享状态）")
    a = ap.parse_args()

    if a.rounds > 1:
        rs = asyncio.run(
            run_rounds(a.rounds, use_llm=not a.no_llm, max_iters=a.iters)
        )
        ok = sum(1 for r in rs if r.get("success"))
        gen = sum(1 for r in rs if r.get("generated"))
        print("=== 多轮汇总 ===")
        print(f"轮数 {len(rs)} · 成功 {ok} · 生成新任务 {gen} · 末轮进度 {rs[-1].get('progress')}")
        for r in rs:
            print(f"  {r['round']}. {r['topic_id']} [{r['reason']}] 成功={r['success']}"
                  f" 技能={r.get('skill_key') or '—'}")
            print(f"     题面：{r.get('question')}")
            print(f"     SQL ：{(r.get('sql') or '').replace(chr(10), ' ')[:110]}")
        return 0

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
