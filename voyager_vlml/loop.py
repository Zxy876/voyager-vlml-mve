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

# ---- 停止信号（同构原版 voyager.py:_stop_requested / run_voyager_learn.py:request_stop）----
# 原版：面板「停止」→ SIGTERM → request_stop() → 主循环**在当前任务结束后**退出，
# 不硬杀（技能已按任务落盘）。
# ⚠️ 学习循环本来就是**不带停**的（原版 `while True` + 只有 iteration 上限），
#    停止是外部动作，不是循环内部的终止条件。
_STOP = {"requested": False}
_MAX_ITERATIONS = 0      # 不停模式下的总题数上限（0 = 不限；原版 --max-iterations 默认 160）

# ---- 循环自己的状态文件（与面板状态文件**分开**）----
# 用户原话：「循环与面板的运行状态写在两个不同的状态文件里」。
# 为什么必须分开（实测踩的耦合）：面板状态（mve/pilot_state.json）是**人的操作
# 记录**（谁点了启动、点了停止、pid 多少）；循环状态是**循环自己的心跳**（跑到
# 第几轮、这轮工具成没成、连续几轮没拿到证据）。混在一个文件里会有两种互相
# 污染的写法：
#   · 循环每轮重写状态文件 → 人点「停止」写的 stop_requested 位可能被覆盖回去；
#   · 面板为了显示 pid 重写文件 → 把循环刚写的"这轮没取到数"抹掉。
# 分家之后：面板**只读**循环状态，两个文件的写入方各只有一个。
LOOP_STATE = _MVE_DIR / "loop_state.json"

# 运行期新生成的图谱节点落盘处 —— **与消费侧同一份文件**
# （消费侧登记的是 `consume_<hash8>`，这里登记的是 `runtime_<hash8>`）。
# 为什么不各存一份：掌握度的分母是「图谱节点 + 这里登记的能力项」，
# 分两个文件迟早会出现两边口径对不上的账。
RUNTIME_TOPICS = _MVE_DIR / "runtime_topics.json"


def _load_runtime_topics() -> dict[str, Any]:
    try:
        import json as _json
        if RUNTIME_TOPICS.exists():
            obj = _json.loads(RUNTIME_TOPICS.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    return {}


def _save_runtime_topic(node: dict[str, Any]) -> None:
    """把一个运行期生成的图谱节点**持久化**。

    ⚠️ 为什么必须落盘（2026-10-13 服务器实跑 12 轮后发现）：
      以前 `_generate_new_task` 只往内存里的 `curr.graph.topics[tid]` 塞，
      进程一退就没了。结果是：技能库拿到了 14 条新配方、MasteryStore 记了 34 个
      completed，但**图谱节点一个都没留下** —— 重开之后 `select_next` 看不到
      这些已完成节点，于是又 exhausted、又去生成新题、又重新验一遍同样的题。
      用户设计里「任务跑通 → 图谱节点改为已完成（**不重复规划**）」就落空了：
      节点没存，等于每次都得重新规划。
    """
    try:
        import json as _json
        reg = _load_runtime_topics()
        tid = str(node.get("id") or "")
        if not tid:
            return
        reg[tid] = node
        RUNTIME_TOPICS.write_text(
            _json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # pragma: no cover
        print(f"  （运行期节点落盘失败：{type(e).__name__}: {e}）")


def _inject_runtime_topics(curr: Any) -> int:
    """把落盘的运行期节点装回课程的内存图谱（重开后不必重造同一批题）。"""
    nodes = [v for k, v in _load_runtime_topics().items()
             if isinstance(v, dict) and str(k).startswith("runtime_")]
    n = 0
    for node in nodes:
        tid = str(node.get("id") or "")
        if tid and tid not in curr.graph.topics:
            curr.graph.topics[tid] = node
            n += 1
    return n


def _write_loop_state(**kw: Any) -> None:
    """把循环心跳写到 `mve/loop_state.json`（只有循环写，面板只读）。"""
    import json as _json
    import time as _time

    cur: dict[str, Any] = {}
    try:
        cur = _json.loads(LOOP_STATE.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    cur.update(kw)
    cur["updated_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        LOOP_STATE.write_text(
            _json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # pragma: no cover
        pass


# 错误分诊的关键词表。**顺序有意义**：基础设施类先判，因为它看起来也像
# "does not exist"（`database does not exist` vs `column "x" does not exist`）。
_INFRA_ERRORS = (
    "cannot open database", "database does not exist", "io error",
    "catalog error", "permission denied", "no module named",
    "未装载的工具", "connection", "failed to open", "file not found",
)
_SQL_ERRORS = (
    "parser error", "syntax error", "binder error", "conversion error",
    "not implemented", "transactioncontext", "invalid input",
    "referenced column", "does not exist", "ambiguous reference",
)


def _is_infra_error(err: str) -> bool:
    e = (err or "").lower()
    return any(m in e for m in _INFRA_ERRORS)


def _is_sql_error(err: str) -> bool:
    e = (err or "").lower()
    return any(m in e for m in _SQL_ERRORS)


def _classify_no_evidence(observations: list[dict[str, Any]]) -> str:
    """这轮为什么没拿到证据 —— 让观察者能分清「它没学会」和「链路根本没跑通」。

    ⚠️ 用户明确要求的可区分性（原话：「面板要用可区分信息让观察者分辨
      '它没学会' 和 '链路根本没跑通'」）。四种结果的处置完全不同：
        · `linkage`   —— 一次工具都没调成功，且错误是**基础设施类**（打不开库、
                         工具没装载、模块缺失）。改配置/依赖能修，跟"学没学会"无关。
        · `sql_error` —— 工具**跑起来了**，是它写的 SQL 有问题（Parser/Binder
                         错误）。这是**它**的水平问题，不是链路。
                         ⚠️ 这一档是 2026-10-13 补的：之前把「SQL 语法/列名写错」
                         一律归成 `linkage`，于是面板让人去查"库在不在、工具装没装"
                         —— 查半天查的是根本没错的地方。
        · `empty`     —— SQL 跑通了但取到 0 行：题面在库里没数据，或过滤条件过严。
        · `unproven`  —— 取到数了但判据没过（critic 判定为没取到有效证据）。
    不停循环**不会因为任何一种而停下** —— 这里只负责如实标出来。
    """
    obs = observations or []
    if not obs:
        return "linkage"          # 连一次调用都没发出去 = 环境根本没转起来
    ok_n = sum(1 for o in obs if o.get("ok"))
    if ok_n <= 0:
        errs = [str(o.get("error") or "") for o in obs]
        if any(_is_infra_error(e) for e in errs):
            return "linkage"
        if any(_is_sql_error(e) for e in errs):
            return "sql_error"
        return "linkage"          # 工具失败但认不出原因 → 按最坏情况当链路问题
    rows = sum(int(o.get("rows") or 0) for o in obs if o.get("ok"))
    if rows <= 0:
        return "empty"
    return "unproven"


def _install_stop_handler() -> None:
    import signal

    def _handler(signum, _frame):
        _STOP["requested"] = True
        print(f"\n（收到信号 {signum}：跑完当前这道题就停）", flush=True)

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except Exception:  # pragma: no cover
            pass


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
            node = {
                "id": tid,
                "name": name,
                "chapter": "运行期生成",
                "depth": 9,          # 排在种子之后
                "difficulty": 0.5,
                "skills": skills,
                "examples": [{"prompt": question}],
                "prerequisites": [],  # 无前置 → 立即可选
                "source": "practice",
            }
            curr.graph.topics[tid] = node
            _save_runtime_topic(node)   # 落盘：不落盘重开就得重造同一批题
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
    if curr is None:
        curr = VLMLCurriculumAgent(scope=scope)
        n = _inject_runtime_topics(curr)
        if n:
            print(f"  （从上次的运行期节点里装回 {n} 个，不重造同一批题）")
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
                # ⚠️ 字段必须与正常返回**完全对齐**。2026-10-13 服务器实跑踩到：
                #   跑满课程后连续生成新题都验不过 → 走进这条早退 → 返回里漏了
                #   `progress`/`critique` → run_rounds 打印时 `r['progress']`
                #   KeyError → **整个不停循环当场崩掉**。不停循环里"空转一圈"
                #   是设计内的常态（设计：整轮不结束），它绝不能成为致命路径。
                return {
                    "topic_id": "", "reason": "generated_but_unverifiable",
                    "success": False, "question": "", "sql": "",
                    "skill_key": "", "generated": True, "observations": [],
                    "critique": "", "progress": curr.progress,
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
    """连跑 n 轮（共享 env 与课程状态）。

    ⚠️ **`n <= 0` = 不停**（同构原版 Voyager 的 `while True`）：
      一直跑到人点「停止」，或跑满 `--max-iterations`。
      原版就是这个语义 —— `voyager_original/voyager/voyager.py:326` 的主循环
      **没有"跑 N 轮就结束"这一说**，只有 iteration 上限（`--max-iterations`，
      默认 160）和停止信号（SIGTERM → 当前任务跑完后退出）。

      以前这里 n 是硬上限，面板还写死 `rounds:3` → 一次启动只跑 3 道题就退出，
      29 个节点的图谱根本跑不完，人看到的就是「三轮就结束了」。
      那不是 Voyager 该有的行为：**学习循环是不带停的**。

    停止是**优雅停**：收到信号后把手头这道题跑完再退出（照原版
    "exiting learn loop after current task"），不硬杀 —— 技能已按任务落盘。
    """
    env = VLMLMCPEnv()
    curr = VLMLCurriculumAgent(scope=scope)
    # 把上次运行期生成的节点装回内存图谱 —— 它们已经落盘且多半已标记完成，
    # 装回来才能「不重复规划」；不装的话每次重开都要重新生成、重新验一遍。
    _n_inj = _inject_runtime_topics(curr)
    if _n_inj:
        print(f"（装回 {_n_inj} 个上次运行期生成的图谱节点）")
    out: list[dict[str, Any]] = []
    i = 0
    # 连续「没拿到证据」的计数。**只观测，不终止**：设计原文是
    # 「一直跳过且技能进度没涨时，整轮不会结束；只有停止能改变循环节奏」。
    # 所以它唯一的作用是把状态写进 loop_state.json，让面板把
    # 「它没学会」和「链路没跑通」区分给人看（见 _classify_no_evidence）。
    streak = 0
    last_progress = curr.progress
    since_progress = 0
    while True:
        if n > 0 and i >= n:
            break
        if _STOP["requested"]:
            print("\n（收到停止信号：跑完当前这道题就停，不硬杀）")
            break
        if n <= 0 and _MAX_ITERATIONS > 0 and i >= _MAX_ITERATIONS:
            print(f"\n（跑满 --max-iterations {_MAX_ITERATIONS}，停）")
            break
        r = await run_one_round(
            scope=scope,
            use_llm=use_llm,
            max_iters=max_iters,
            curr=curr,
            env=env,
        )
        r["round"] = i + 1
        out.append(r)
        i += 1

        # ---- 写循环心跳（面板只读这个文件，不写）----
        obs = r.get("observations") or []
        ok_calls = [o for o in obs if o.get("ok")]
        rows = sum(int(o.get("rows") or 0) for o in ok_calls)
        if r.get("success"):
            streak = 0
        else:
            streak += 1
        if int(r.get("progress") or 0) > last_progress:
            last_progress = int(r.get("progress") or 0)
            since_progress = 0
        else:
            since_progress += 1
        _write_loop_state(
            running=not _STOP["requested"],
            continuous=(n <= 0),
            round=i,
            topic=str(r.get("topic_id") or ""),
            reason=str(r.get("reason") or ""),
            generated=bool(r.get("generated")),
            success=bool(r.get("success")),
            progress=last_progress,
            tool_calls=len(obs),
            tool_ok=len(ok_calls),
            tool_fail=len(obs) - len(ok_calls),
            rows=rows,
            last_error=next(
                (str(o.get("error") or "") for o in reversed(obs) if not o.get("ok")),
                ""),
            no_evidence_streak=streak,
            no_evidence_kind=(_classify_no_evidence(obs) if not r.get("success") else ""),
            rounds_since_progress=since_progress,
        )

        # 一律用 .get()：不停循环里"这轮什么都没跑成"是设计内的常态，
        # 任何一条早退路径缺字段都不该把整个循环打死（实测踩过 KeyError 崩循环）。
        tag = f"第 {i}/{n} 轮" if n > 0 else f"第 {i} 轮（不停）"
        empty_round = not str(r.get("topic_id") or "")
        print(
            f"[{tag}] {r.get('topic_id') or '（本轮没跑出题）'} · reason={r.get('reason')}"
            f"{' (新生成)' if r.get('generated') else ''}"
            f" · 成功={r.get('success')} · 进度={r.get('progress')}"
            + (f" · ⚠️ 连续 {streak} 轮没拿到证据"
               f"（{_classify_no_evidence(obs)}）"
               if (streak >= 3 and not empty_round) else "")
        )
    _write_loop_state(running=False, round=i, progress=last_progress)
    return out


def _main() -> int:
    ap = argparse.ArgumentParser(description="VLML 同构学习循环 · 一轮（自己出题取数）")
    ap.add_argument("--topic", default="", help="钉住某个知识点（不给则走课程选题）")
    ap.add_argument("--no-llm", action="store_true", help="不出题不写 SQL（仅演示骨架）")
    ap.add_argument("--iters", type=int, default=3, help="写 SQL 的迭代修正轮数")
    ap.add_argument("--rounds", type=int, default=1,
                    help="连跑几轮；**0 = 不停**（同构原版 while True，只有点停止才退出）")
    ap.add_argument("--max-iterations", type=int, default=0,
                    help="不停模式下的总题数上限（0 = 不限；原版 --max-iterations 默认 160）")
    ap.add_argument("--unstale", nargs="?", const="__all__", default="",
                    help="清掉「放弃换题」标记并重置 attempts（不给值=全清，给值=清这一题）")
    a = ap.parse_args()

    # 不停模式必须装信号处理器：面板「停止」发的是 SIGTERM，
    # 我们要的是「跑完当前这道题再退」，不是被硬杀在半路。
    global _MAX_ITERATIONS
    if a.rounds == 0:
        _install_stop_handler()
        _MAX_ITERATIONS = max(0, int(a.max_iterations or 0))

    if a.unstale:
        # 课程被 stale 堵死时的清理口子（实测：8 个 stale 就能让 29 个节点全不可选）
        import topic_graph
        st = topic_graph.MasteryStore()
        tid = "" if a.unstale == "__all__" else str(a.unstale)
        touched = st.unstale(tid)
        print(f"已清理 stale / attempts：{len(touched)} 题"
              + (f" → {touched[:15]}" if touched else ""))
        return 0

    # ⚠️ `rounds=0` 也必须走 run_rounds（0 = 不停），只有 `rounds==1` 才走单轮路径。
    #    以前写成 `if a.rounds > 1`，于是面板传 0 想"不停"时会被当成单轮跑完就退
    #    —— 正是「三轮就结束了」那一类问题的入口版本。
    if a.rounds != 1:
        rs = asyncio.run(
            run_rounds(a.rounds, use_llm=not a.no_llm, max_iters=a.iters)
        )
        ok = sum(1 for r in rs if r.get("success"))
        gen = sum(1 for r in rs if r.get("generated"))
        # 「生成了但验题没过」必须与「跑了但失败」分开数：前者是**出题**的问题
        # （题面在库里答不了），后者才是**它**的问题。混成一个数就没法定位。
        bad = sum(1 for r in rs
                  if r.get("reason") == "generated_but_unverifiable")
        why_stop = "（收到停止信号，跑完手头这道题就退）" if _STOP["requested"] else ""
        print("=== 多轮汇总 ===")
        print(f"轮数 {len(rs)} · 成功 {ok} · 生成新任务 {gen}"
              f"（其中验题没过 {bad}）· 末轮进度 "
              f"{rs[-1].get('progress') if rs else '（一轮没跑）'}{why_stop}")
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
