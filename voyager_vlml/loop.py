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

# run_log / skill_store 住在 `mve/` 下（面板读的就是那两个文件）。
# 驾驶舱从**仓库根**拉起本脚本（cwd=仓库根），sys.path[0] 是脚本所在目录，
# 不是 mve/ —— 不加这一段，`import run_log` 会被下面那层 except 静默吞掉，
# 表现是「题跑通了、技能也存了，但面板曲线一条不动」。
_MVE_DIR = _REPO_ROOT / "mve"
if str(_MVE_DIR) not in sys.path:
    sys.path.insert(0, str(_MVE_DIR))

from voyager_vlml.agents.action import VLMLActionAgent  # noqa: E402
from voyager_vlml.agents.critic import VLMLCriticAgent  # noqa: E402
from voyager_vlml.agents.curriculum import VLMLCurriculumAgent  # noqa: E402
from voyager_vlml.agents.question import VLMLQuestionAgent  # noqa: E402
from voyager_vlml.env.vlml_mcp import VLMLMCPEnv  # noqa: E402


async def _generate_new_task(curr: Any, env: Any, act: Any = None,
                             max_tries: int = 3, max_iters: int = 3
                             ) -> tuple[str, str, list[dict[str, Any]], str]:
    """生成一个新任务，**并且先验证它真的能从库里取到数**。

    返回 (topic_id, 题面, 验题时取到的观察, 验题用的 SQL)。取不到就换一个再生成，
    全都取不到返回空串 —— 由调用方如实报"这轮没生成出可做的题"。

    ⚠️ 为什么必须先验题（2026-10-13 用户贴的日志）：连跑 3 轮，3 轮都是
      generated_new_task，其中 2 轮失败。失败原因**不是模型不会写 SQL**，
      是题面本身在库里答不了：
        · 「每个回合哪款武器被使用得最多」→ 去 JOIN `weapon_types`（库里没这张表）
        · 「各地图平均得分率」→ 用 `team_score_before / enemy_score_before`（列不存在）
      旧题库出题有七道验题闸（`question_gen`），而**运行期生成的新题一道都没有** ——
      生成完直接拿去跑，跑不通记一次失败 → attempts+1 → 3 次就 stale →
      课程里可选节点越来越少 → 又去生成新题。这是恶性循环，必须从源头掐掉。

    ⚠️ 为什么 id 要按题面哈希（同一个日志挖出来的第二个 bug）：
      旧写法是 `runtime_{n}` 递增，而 n 只在**内存**的 `graph.topics` 里数 ——
      进程一重启就回到 `runtime_1`。于是「问武器」和「问平均伤害」两道完全不同的题
      共用同一个 id，失败账互相叠加（实测 `runtime_1` attempts=5、fails=5），
      新题一出生就背着上一道题的账，凑够 3 次立刻 stale。
      改成 `runtime_<md5(题面)[:8]>`：同题同 id、异题异 id（与消费侧
      `consume_<hash8>` 同一套做法）。
    """
    import hashlib

    from voyager_vlml.agents.action import VLMLActionAgent
    from voyager_vlml.agents.critic import VLMLCriticAgent
    from voyager_vlml.llm import chat_json

    act = act or VLMLActionAgent(env=env)
    crit = VLMLCriticAgent()

    schema = ""
    try:
        schema = await env.schema_block()
    except Exception:
        schema = ""

    tried: list[str] = []
    for _ in range(max(1, int(max_tries))):
        out = chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "你是 Valorant 电竞数据分析的课程设计者。当前库的知识点都被拿下或"
                        "暂时拿不下了，请**新提一个**还能用 SQL 从库里取数回答的知识点。\n"
                        "⚠️ 只提**库里真有数据**的知识点：只能引用下面 schema 里出现过的"
                        "表名与列名，不许凭印象编表名/列名（实测编过 `weapon_types`、"
                        "`team_score_before`，题面看着合理但库里根本没有）。\n"
                        "只返回 JSON：{\"name\": \"知识点名（含粒度，如 回合粒度（rounds））\","
                        " \"question\": \"一个具体题面\", \"skills\": [\"一两条取数要点\"]}"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "当前库 schema：\n"
                        f"{schema}\n\n"
                        "已有的知识点不要重复。\n"
                        + ("下面这些题面刚试过，库里取不到数，**不要**再提类似的：\n- "
                           + "\n- ".join(tried) + "\n" if tried else "")
                        + "请只返回 JSON。"
                    ),
                },
            ],
            temperature=0.7,
        )
        name = str(out.get("name") or "").strip() or "运行期新任务"
        question = str(out.get("question") or "").strip()
        skills = [str(s) for s in (out.get("skills") or [])][:2]
        if not question:
            continue

        tid = "runtime_" + hashlib.md5(question.encode("utf-8")).hexdigest()[:8]
        if tid in curr.graph.topics:
            # 同一道题以前生成过 → 复用它的节点，不重复验、也不重复建
            return tid, question, [], ""

        # —— 验题就当正式那趟跑（同一个 agent、同样的迭代次数）——
        # ⚠️ 别用更严的标准筛题：一开始写成 `max_iters=1`（只迭代一次就判生死），
        #    结果连出 3 题全被刷掉（"能学会但第一次写不对"的题被误杀）。
        #    验题必须与正式跑同标准，否则等于自杀式筛选。
        #    而且跑通就直接复用这趟结果，不重复烧一次 LLM + DB。
        # 取不到数 → 题面不合格，换一个再来；**不记失败**（它还没进课程）
        obs, sql = await act.solve(
            question, max_iters=max_iters, hints=skills[:3])
        ok, _why = crit.check_task_success(
            observations=obs, task={"task": question, "hints": skills})
        if ok:
            curr.graph.topics[tid] = {
                "id": tid,
                "name": name,
                "chapter": "运行期生成",
                "depth": 9,          # 排在种子之后
                "difficulty": 0.5,
                "skills": skills,
                "examples": [{"prompt": question}],
                "prerequisites": [],  # 无前置 → 立即可选
            }
            return tid, question, list(obs or []), str(sql or "")
        tried.append(question)
        print(f"  （新题验题没取到数，换一个：{question[:50]}）")

    # 连续几次都生成不出能从库里取到数的题 —— 如实返回空，不硬凑一道去跑
    return "", "", [], []


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
    gen_question = ""
    pre_obs: list[dict[str, Any]] = []
    pre_sql = ""
    if topic_id:
        tid, reason = topic_id, "explicit"
    else:
        nxt = curr.propose_next_task()
        tid, reason = nxt["topic_id"], nxt["reason"]
        if not tid:
            # 范围内已无可拿下的任务 → 生成一个**并先验题**（设计：整轮不结束）
            tid, gen_question, pre_obs, pre_sql = await _generate_new_task(
                curr, env, act=act, max_iters=max_iters)
            reason, generated = "generated_new_task", True
            if not tid:
                # 连续几次生成的新题都从库里取不到数 —— 如实报，不硬跑一道充数。
                # 以前的写法是"生成完就去跑"，跑不通记一次失败，失败会喂 stale，
                # stale 会把课程越堵越死（实测 3 轮全 generated_new_task、2 轮失败）。
                print("  （连续生成的新题都从库里取不到数，本轮不跑，也不记失败）")
                return {
                    "topic_id": "", "reason": "generated_but_unverifiable",
                    "success": False, "question": "", "sql": "",
                    "skill_key": "", "generated": True, "observations": [],
                }

    topic = curr.graph.topics.get(tid) or {}

    # ---- 出题（LLM；只出题面，不给参考答案）----
    # ⚠️ 运行期新题是**生成时就带着题面一起来的**，且已经验过能从库里取到数，
    #    不再走出题器（出了反而可能把验过的题面换成另一个取不到数的说法）。
    question = gen_question
    if not question and use_llm:
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
    # 验题那一趟已经取到数了 → 直接复用，不重复烧一次 LLM + DB。
    if pre_obs:
        observations, sql = pre_obs, pre_sql
    else:
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
    ap.add_argument("--unstale", nargs="?", const="__all__", default="",
                    help="清掉「放弃换题」标记并重置 attempts（不给值=全清，给值=清这一题）")
    a = ap.parse_args()

    if a.unstale:
        # 课程被 stale 堵死时的清理口子（实测：8 个 stale 就能让 29 个节点全不可选）
        import topic_graph
        st = topic_graph.MasteryStore()
        tid = "" if a.unstale == "__all__" else str(a.unstale)
        touched = st.unstale(tid)
        print(f"已清理 stale / attempts：{len(touched)} 题"
              + (f" → {touched[:15]}" if touched else ""))
        return 0

    if a.rounds > 1:
        rs = asyncio.run(
            run_rounds(a.rounds, use_llm=not a.no_llm, max_iters=a.iters)
        )
        ok = sum(1 for r in rs if r.get("success"))
        gen = sum(1 for r in rs if r.get("generated"))
        # 「生成了但验题没过」必须与「跑了但失败」分开数：前者是**出题**的问题
        # （题面在库里答不了），后者才是**它**的问题。混成一个数就没法定位。
        bad = sum(1 for r in rs
                  if r.get("reason") == "generated_but_unverifiable")
        print("=== 多轮汇总 ===")
        print(f"轮数 {len(rs)} · 成功 {ok} · 生成新任务 {gen}"
              f"（其中验题没过 {bad}）· 末轮进度 {rs[-1].get('progress')}")
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
