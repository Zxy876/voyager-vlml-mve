"""消费通路：人写一句想问的话 → **Voyager 用它已积累的能力回答** → 披露 VLML 解析 → 算覆盖率/掌握度。

为什么单独开这条（与练习闭环 `loop.py` 的区别）：

    练习（loop.py）          消费（本文件）
    ─────────────────        ────────────────────────
    里程碑选题                人出题（一句话）
    成败 = 有没有取到数        成败 = 有没有**答对**（覆盖率）
    **不算覆盖率/掌握度**      **从这里开始算覆盖率/掌握度**
    判据不进 VLML 尺子        判据就是 VLML0 裁判（但只给人看）

用户定的边界（2026-10-13）：「学习这块已经做好了，消费能力这块咱也得做……
从这块开始来计算覆盖率和掌握度最好了」。

⚠️ 三条硬约束（本文件必须守住）：

1. **用已积累的能力回答，不是现编**。
   检索技能库里已固化的 SQL 配方 → 把**当前这句话里的实体值回填进配方** → 执行。
   用户原话：「复用时当前这道题里的实体值回填进配方；不把『记住数值』当作成功，
   而是从用方法取出数值的过程开始存技能。**回填不存在的，只有更快的计算**」。
   所以这里回填的是**计算**（配方的算法结构），不是上一次的数值。
   库里没有可用配方时，才降级到现场写 SQL，并**如实标注 reused=False**
   （"这题还没学会，是现做的"——不能假装用了能力）。

2. **VLML 的解析照旧披露**：调了哪些工具、取到哪些事实，原样给面板看。
   这是给人看的审计线索（"它是怎么答出来的"）。

3. **覆盖率/掌握度只进面板，绝不进 Voyager 反馈**。
   覆盖率由 VLML0 裁判独立跑出标准答案事实集，与 Voyager 的事实集比对得出。
   ⚠️ 这个数**不回灌**任何 prompt：不进 action 的迭代修正、不进 critic、
   不进 curriculum 的选题（选题仍是有序课程，不读分数）。
   只落 `coach_log.jsonl` 给面板 —— 与练习侧 `loop.py` 里 coverage=None 呼应：
   练习不判分，消费才判分，判给人看。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MVE_DIR = _REPO_ROOT / "mve"
for _p in (str(_REPO_ROOT), str(_MVE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from voyager_vlml.agents.action import VLMLActionAgent  # noqa: E402
from voyager_vlml.env.vlml_mcp import VLMLMCPEnv  # noqa: E402
from voyager_vlml.llm import chat, chat_json  # noqa: E402


# ---------------------------------------------------------------------------
# ① 这句话归到哪道题（为了拿到评分点 —— 覆盖率要按评分点算）
# ---------------------------------------------------------------------------

def _infer(question: str) -> tuple[str, str, Any]:
    """归题 → (topic_id, hit, task)。

    归的是**旧题库** `TASKS`（它有 rubric + answer_spec，是覆盖率能算出来的前提）。
    注意：这与练习侧的图谱节点（`vlml_*` / `runtime_*`）**不是一套**。
    两套并存是有意的：图谱管"学什么"（里程碑推进），题库管"答得对不对"（评分点）。
    归不上 → task 为 None，覆盖率无从计算（如实留空，不编造）。
    """
    try:
        from tasks import TASKS, infer_topic
    except Exception as e:  # pragma: no cover
        print(f"  （题库不可用：{type(e).__name__}: {e}）")
        return "", "", None
    tid, hit = infer_topic(question)
    return tid, hit, TASKS.get(tid)


def _same_question(question: str, task: Any) -> tuple[bool, str]:
    """这句话与这道题，问的是**不是同一个问题**（不是"有没有同一个词"）。

    为什么必须有这道校验（实测踩到）：
      人问「哪位选手在 Corrode 这张图上的 K/D 比最高？」
      → `infer_topic` 靠 "k/d" 这个词归到 `kast_adr_check`
      → 但那道题问的是「Cloud9 这场比赛**队伍整体**的 KAST 和 K/D」
        （rubric subject = {series, team:NRG}，要的是队伍口径，不是某个选手）
      → 拿它的 rubric 去判 Voyager 那条（subject = {player_name, map_name}）
      → 键对不上，覆盖率恒 0。

    那不是「Voyager 答错了」，是**尺子拿错了**。不校验就直接判 0，
    面板上会显示覆盖率 0%，人读到的是"它能力不行"，真实情况却是
    "我们拿了一道别的题的标准答案来量它"。这种假性失败必须挡掉：
    不同题 → 不套用它的 rubric，如实留空，交给生成新题那条路。
    """
    try:
        out = chat_json(
            [
                {"role": "system",
                 "content": ("判断两句话问的是不是**同一个问题**。看口径与对象："
                             "队伍整体 ≠ 某个选手，全场 ≠ 某一张图，一个指标 ≠ 两个指标。"
                             "只返回 JSON：{\"same\": true/false, \"why\": \"一句话理由\"}")},
                {"role": "user",
                 "content": (f"甲（人问的）：{question}\n\n"
                             f"乙（题库里的题）：{getattr(task, 'question', '')}")},
            ],
            temperature=0.0,
        )
        return bool(out.get("same")), str(out.get("why") or "")
    except Exception as e:  # pragma: no cover
        return False, f"校验失败：{type(e).__name__}"


# ---------------------------------------------------------------------------
# ② 这句话该消费哪些已积累的能力（找配方）
# ---------------------------------------------------------------------------

def _pick_ability_topic(question: str, skills: list[dict[str, Any]]) -> str:
    """在**技能库实际有的 topic** 里挑一个最像这句话的。

    为什么不能直接用归题结果：归题归的是旧题库（series_totals 等），
    而技能库里的配方挂在 `vlml_*` / `runtime_*` 上 —— 两套 ID 不通，
    用归题结果去 `skill_store.retrieve` 会一条都检索不到（实测：技能库
    3 条配方全是 runtime_*，而 infer_topic 只会返回旧题库 ID）。
    所以能力归属走**词面匹配**，看的是"这句话和哪个已学配方最像"。
    """
    try:
        from skill_store import similarity
    except Exception:  # pragma: no cover
        return ""
    best, best_s = "", 0.0
    for s in skills:
        t = str(s.get("topic") or "")
        if not t:
            continue
        blob = f"{s.get('name', '')} {s.get('text', '')} {s.get('code', '')}"
        sc = similarity(question, blob)
        if sc > best_s:
            best, best_s = t, sc
    return best


def _retrieve_skills(question: str, topic_hint: str = "") -> list[dict[str, Any]]:
    """取已积累的技能（可复用 SQL 配方），top_k 照原仓库 retrieval_top_k=5。"""
    try:
        import skill_store
        all_s = list((skill_store._load_raw().get("skills") or {}).values())
    except Exception as e:  # pragma: no cover
        print(f"  （技能库不可用：{type(e).__name__}: {e}）")
        return []
    if not all_s:
        return []
    topic = topic_hint or _pick_ability_topic(question, all_s)
    out = skill_store.retrieve(topic, question, top_k=5)
    try:
        skill_store.mark_used([str(s.get("key") or "") for s in out])
    except Exception:  # pragma: no cover
        pass
    return out


# ---------------------------------------------------------------------------
# ③ 复用配方：把当前这句话的实体值回填进配方的**计算**
# ---------------------------------------------------------------------------

_REFILL_SYSTEM = """你是 Valorant 电竞数据分析师。你手里有**以前学会的取数配方（SQL）**，
现在要用它回答一个**新的问法**。

规则（违反任何一条都不算复用成功）：

0. **先判断配方算的量，与这句话问的量，是不是同一个量。**
   不是同一个量 → 直接返回 {"reused": false, "sql": "", "note": "原因"}，
   不要硬改一个出来。
   ⚠️ 实测踩过：问「Cloud9 在哪张图上崩得最厉害？」（要的是连败/崩盘），
   库里只有一条「平均 K/D」配方，模型照样把 'Cloud9' 填进去产出一条 K/D 查询，
   还自称"复用了配方" —— 结果取到的是 K/D 比，被下游当成"崩盘率"讲了出来。
   **数字错还能看出来，量错了是方向错，比报错危险得多。**
   换实体值（Cloud9→NRG）算复用；换个完全不同的量算不上复用。

1. 复用的是配方的**计算方法**（用了哪些表、怎么聚合、怎么筛选），
   **不是**它上次算出来的数值 —— 数值必须重新算。
2. 把**当前这句话里的实体值**（队伍名 / 图名 / 赛事 / 选手）填进配方对应的位置。
   配方里写死的字面量要换成当前这句话指的那个值。
3. 当前这句话没给实体（比如问"哪支队伍最高"）→ 保留配方的分组结构，别自己造一个值。
4. 只输出一条 SELECT。不要 INSERT/UPDATE/DROP，不要多语句。

只返回一个 JSON：{"sql": "你的 SELECT", "reused": true/false,
"note": "一句话说明你复用了配方的哪部分、把什么值填了进去（没复用就说明为什么用不上）"}"""


def _refill_sql(question: str, skills: list[dict[str, Any]], schema: str) -> dict[str, Any]:
    """让模型把配方改造成当前这句话的取数 SQL（回填 = 改造计算，不是抄数值）。"""
    recipes = "\n\n".join(
        f"【配方 {i+1}｜topic={s.get('topic')}｜{s.get('name') or ''}】\n"
        f"说明：{str(s.get('text') or '')[:200]}\n"
        f"SQL：\n{str(s.get('code') or '')[:900]}"
        for i, s in enumerate(skills[:5])
    )
    user = (
        f"当前这句话：\n{question}\n\n"
        f"已学会的配方：\n{recipes}\n\n"
        f"当前库真实存在的表与列（只能引用这里出现的名字）：\n{schema[:6000]}\n\n"
        "按规则产出回答这句话的 SELECT。"
    )
    out = chat_json(
        [
            {"role": "system", "content": _REFILL_SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
    )
    sql = str(out.get("sql") or "").strip()
    return {
        "sql": sql,
        "reused": bool(out.get("reused")) and bool(sql),
        "note": str(out.get("note") or ""),
    }


# ---------------------------------------------------------------------------
# ④ 事实整理（给覆盖率比对用）
# ---------------------------------------------------------------------------

def _extract_system() -> str:
    return ("你是严谨的数据整理器。把工具返回的真实数据整理成事实数组，只输出 JSON，"
            "绝不编造数据里没有的数字。")


def _extract_user(question: str, obs: list[dict[str, Any]],
                  dims: list[str] | None = None,
                  subjects: list[dict[str, Any]] | None = None) -> str:
    payload = json.dumps(
        [{"tool": o.get("tool"), "args": o.get("args"),
          "result": o.get("data"), "error": o.get("error")} for o in obs],
        ensure_ascii=False, default=str,
    )[:14000]
    hint = ""
    if dims:
        # ⚠️ 只给**维度名**，不给值 —— 名字是"该交哪几项作业"，值仍要 Voyager 自己算。
        # 两边 dimension 必须同名字，否则事实集按 (subject, dimension) 对不上键，
        # 覆盖率恒为 0（那不是"答错了"，是"没法比"）。
        hint = (
            "\n\n这一问期望覆盖的维度（**只给名字，值你自己从数据里算**）：\n"
            + "\n".join(
                f"- dimension 必须逐字写成 `{d}`"
                f"{'，subject 写成 ' + json.dumps(s, ensure_ascii=False) if s else ''}"
                for d, s in zip(dims, subjects or [None] * len(dims))
            )
            + "\n\n⚠️ 硬约束：上面这些维度，工具返回的数据里**算不出来就一条都别写**，"
            "留空数组。**绝对不许把别的数套到这个维度名上** —— 实测踩过：问的是"
            "崩盘（要连败），取到的其实是 K/D 比，模型照样写成"
            "`map_fb_conv = 1.146`，下游把它讲成「崩盘率 1.146」。"
            "维度名与值不匹配 = 编造，比留空坏得多。宁可少写，不许错配。"
        )
    return f"""工具返回的真实数据：

{payload}

这句话问的是：
{question}
{hint}

请整理成事实数组，每个事实：
{{"subject": {{...}}, "dimension": "...", "value": 数值,
  "unit": "percent|count|ratio|raw", "base": 分母}}

subject 至少要能定位到谁：涉及队伍带 "team"，涉及某张图带 "map"（用真实名字）。
dimension 要能看出方向：写 max_losing_streak 这种，不要写 round_streak ——
方向反了比报错更危险。

只输出 JSON：{{"facts":[...]}}
取不到就留空数组，不要编。"""


# ---------------------------------------------------------------------------
# ⑤ 用取到的数回答这句话
# ---------------------------------------------------------------------------

def _answer(question: str, facts: list[dict[str, Any]],
            obs: list[dict[str, Any]]) -> str:
    if not facts:
        return ""
    payload = json.dumps(facts, ensure_ascii=False, default=str)[:6000]
    return chat(
        [
            {"role": "system",
             "content": ("你是 Valorant 电竞数据分析师。用**已经取到的数**回答人的问题。"
                         "只用给出来的数，不许补充任何数据里没有的推断。"
                         "说清楚数字是多少、是哪个对象的、样本多大。两到四句话。")},
            {"role": "user",
             "content": f"人的问题：{question}\n\n已取到的事实：\n{payload}"},
        ],
        temperature=0.3,
        max_tokens=600,
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

async def consume(
    question: str,
    *,
    topic_hint: str = "",
    auto_task: bool = True,
    env: VLMLMCPEnv | None = None,
) -> dict[str, Any]:
    env = env or VLMLMCPEnv()

    # ---- ① 归题（找评分点）----
    topic_id, hit, task = _infer(question)
    if topic_hint:
        hit = f"{hit}（指定）" if hit else "（指定）"
    if task is not None:
        # 归题靠词面，会归到"有同一个词但不是同一个问题"的题上。
        # 尺子拿错 → 覆盖率恒 0，那是假性失败，不能当能力差报给人。
        same, why = _same_question(question, task)
        if not same:
            hit = f"{hit}（归到的题与这句话不是同一个问题：{why}）"
            task = None

    # ---- ② 找已积累的能力（配方）----
    skills = _retrieve_skills(question, topic_hint)
    reused_list = [{"topic": s.get("topic"), "name": s.get("name"),
                    "key": s.get("key")} for s in skills]

    # ---- ③ 取数：优先复用配方，没有就现场写 ----
    schema = ""
    try:
        schema = await env.schema_block()
    except Exception:
        schema = ""

    observations: list[dict[str, Any]] = []
    sql, reused, note = "", False, ""
    if skills:
        r = _refill_sql(question, skills, schema)
        # 模型自己判定"配方算的量与这句话问的不是同一个量" → **不执行它**，
        # 直接现场写。宁可现做，也不许拿一个张冠李戴的 SQL 去取数。
        if r["reused"] and r["sql"]:
            sql, reused, note = r["sql"], True, r["note"]
            observations.append(await env.run_sql(sql))
        else:
            note = r["note"] or "（配方与这句话问的不是同一个量）"
            print(f"  （未复用配方：{note[:100]}）")
    if not any(o.get("ok") for o in observations):
        # 配方没复用成（库里没有 / 用不上 / 回填后跑不通）→ 现场写，如实标注
        if sql:
            print(f"  （配方回填后没取到数，降级为现场写 SQL：{note[:60]}）")
        reused, sql = False, ""
        act = VLMLActionAgent(env=env)
        observations, sql = await act.solve(question, max_iters=3)

    ok_calls = [o for o in observations if o.get("ok")]
    rows = sum(int(o.get("rows") or 0) for o in ok_calls)
    trajectory = [str(o.get("tool") or "") for o in ok_calls]

    # ---- ④ 整理事实 ----
    dims: list[str] = []
    subs: list[dict[str, Any]] = []
    if task is not None:
        for p in (task.rubric or []):
            dims.append(str(p.dimension))
            subs.append(dict(p.subject or {}))
    out = chat_json(
        [
            {"role": "system", "content": _extract_system()},
            {"role": "user",
             "content": _extract_user(question, observations, dims or None,
                                      subs or None)},
        ],
        temperature=0.1,
    ) if ok_calls else {"facts": []}

    facts: list[dict[str, Any]] = []
    try:
        from loop_core import make_fact
        for f in (out.get("facts") or []):
            if not isinstance(f, dict) or "subject" not in f or "dimension" not in f:
                continue
            facts.append(make_fact(
                subject=f.get("subject") or {},
                dimension=str(f.get("dimension")),
                value=f.get("value"),
                unit=str(f.get("unit") or "raw"),
                base=f.get("base"),
                source={"tool": (trajectory or ["query_sql"])[0], "args": {}},
            ))
    except Exception as e:  # pragma: no cover
        print(f"  （事实整理失败：{type(e).__name__}: {e}）")

    # ---- ⑤ 用取到的数回答 ----
    answer = ""
    if facts:
        try:
            answer = _answer(question, facts, observations) or ""
        except Exception as e:  # pragma: no cover
            print(f"  （组织回答失败：{type(e).__name__}: {e}）")

    base: dict[str, Any] = {
        "question": question,
        "topic_id": topic_id,
        "topic_hit": hit,
        "mode": "voyager_consume",
        "source": "human_import",
        "validated_target": False,     # 人导入 ≠ 出题器产出（伴学判定链）
        # —— 能力消费情况：用了哪些已学配方，还是现做的 ——
        "reused": bool(reused),
        "reuse_note": note,
        "used_skills": reused_list,
        "skills_found": len(skills),
        # —— VLML 的解析（照旧披露：调了哪些工具、取到哪些事实）——
        "trajectory": trajectory,
        "tool_calls": len(observations),
        "ok_tool_calls": len(ok_calls),
        "rows": rows,
        "sql": sql,
        "errors": [str(o.get("error") or "") for o in observations if not o.get("ok")],
        "facts": facts,
        "facts_count": len(facts),
        "answer": answer,
        # —— 覆盖率/掌握度（消费侧才算，见 ⑥）——
        "coverage": None,
        "score": None,
        "verdict": "",
        "covered_points": [],
        "missing_points": [],
        "unjudgeable": [],
        "mastery": None,
        "judge": "",
        "skipped": "",
    }

    # ---- ⑥ 覆盖率 / 掌握度 ----
    # ⚠️ 只在**消费**这条线上算。练习（loop.py）不判分，写 coverage=None。
    # ⚠️ 算出来只落盘给面板，**绝不回灌** Voyager 的任何 prompt。
    if not ok_calls:
        base["skipped"] = "一次工具都没调成功，事实无来源，不计覆盖率（避免给编造打分）"
        _log(base)
        return base

    if task is None and auto_task:
        # 归不上已有题 → 拿这句话（+刚取到的事实）生成一道新题，它自带 rubric
        try:
            from coach import _task_from_text
            new_id, why = await _task_from_text(question, facts)
            if new_id:
                from tasks import TASKS
                task = TASKS.get(new_id)
                topic_id, hit = new_id, f"{hit}{why}"
                base["topic_id"], base["topic_hit"] = topic_id, hit
        except Exception as e:  # pragma: no cover
            print(f"  （生成评分点失败：{type(e).__name__}: {e}）")

    if task is None:
        base["skipped"] = ("这句话归不到有评分点的题上，也没有生成出新题 → "
                           "覆盖率无从计算（如实留空，不编造数字）")
        _log(base)
        return base

    try:
        import vlml0_referee
        from loop_core import evaluate_vs_referee
        ref = await vlml0_referee.answer(task)
        ev = evaluate_vs_referee(
            facts, list(task.rubric or []), list(ref.facts or []),
            tool_calls=len(ok_calls),
        )
        base.update({
            "coverage": float(ev.coverage),
            "score": int(ev.score),
            "verdict": str(ev.verdict),
            "covered_points": list(ev.covered_points),
            "missing_points": list(ev.missing_points),
            "unjudgeable": list(ev.unjudgeable),
            "judge": str(ev.judge),
        })
        # 掌握度：按题累计（指数滑动平均）。
        # ⚠️ count_attempt=False —— 消费不该消耗练习的尝试预算：
        # 练习侧 stale 是按「练了几次」判的，消费三次同一题就把练习题目标记成
        # 放弃换题，那是把两条线的账本混了。
        try:
            import topic_graph
            row = topic_graph.MasteryStore().update(
                topic_id, score=float(ev.coverage), count_attempt=False)
            base["mastery"] = row.get("mastery")
        except Exception as e:  # pragma: no cover
            print(f"  （写掌握度失败：{type(e).__name__}: {e}）")
        # 技能有效性反馈（技能库自身维护，不是给 Voyager 的反馈）
        if reused:
            try:
                import skill_store
                skill_store.mark_result(
                    [str(s.get("key") or "") for s in skills[:2]],
                    bool(ev.coverage >= 0.6),
                )
            except Exception:  # pragma: no cover
                pass
    except Exception as e:  # pragma: no cover
        base["skipped"] = f"裁判不可用，覆盖率未计算：{type(e).__name__}: {str(e)[:120]}"

    _log(base)
    return base


def _log(result: dict[str, Any]) -> None:
    """落一条人导入消费记录（面板读 `coach_log.jsonl` 显示）。

    与 `coach._log_import` 写同一个文件、同一套字段，面板的人导入分区不用改就能显示；
    新增的 coverage/score/verdict/mastery/reused 字段老记录没有 → 面板要容错。
    """
    try:
        from coach import _log_import
        _log_import({
            **result,
            "topic_matched": bool(result.get("topic_id")),
            "coverage": result.get("coverage"),
            "score": result.get("score"),
            "verdict": result.get("verdict", ""),
            "mastery": result.get("mastery"),
            "reused": bool(result.get("reused")),
            "used_skills": result.get("used_skills") or [],
            "answer": result.get("answer", ""),
            "ok_tool_calls": result.get("ok_tool_calls", 0),
            "rows": result.get("rows", 0),
        })
    except Exception as e:  # pragma: no cover
        print(f"  （写人导入日志失败：{type(e).__name__}: {e}）")


def _main() -> int:
    ap = argparse.ArgumentParser(description="消费：人问一句，Voyager 用已积累的能力回答")
    ap.add_argument("question", nargs="*", help="你想问的话")
    a = ap.parse_args()
    q = " ".join(a.question).strip() or "Cloud9 在哪张图上崩得最厉害？"
    r = asyncio.run(consume(q))
    print("=== 消费结果 ===")
    print("问     :", r.get("question"))
    print("归题   :", r.get("topic_id") or "（未归上）", "|", r.get("topic_hit"))
    print("用能力 :", "复用配方" if r.get("reused") else "现场现做",
          f"（库里找到 {r.get('skills_found')} 条）", r.get("reuse_note") or "")
    print("工具   :", r.get("trajectory"), f"| 调用 {r.get('tool_calls')} 次，取到 {r.get('rows')} 行")
    print("SQL    :", str(r.get("sql") or "").replace("\n", " ")[:160])
    print("事实   :", r.get("facts_count"), "条")
    for f in (r.get("facts") or [])[:6]:
        subj = "/".join(f"{k}={v}" for k, v in (f.get("subject") or {}).items())
        print(f"   {subj} · {f.get('dimension')} = {f.get('value')} (base={f.get('base')})")
    print("--- 回答 ---")
    print(r.get("answer") or "（没取到数，不产出回答）")
    print("--- 覆盖率 / 掌握度（只给人看，不进 Voyager 反馈）---")
    if r.get("coverage") is None:
        print("未计算：", r.get("skipped") or "（无评分点）")
    else:
        print(f"覆盖率 {r['coverage']:.0%} · 得分 {r['score']} · 判读 {r['verdict']}")
        print("  答出:", r.get("covered_points"))
        print("  缺  :", r.get("missing_points"))
        if r.get("unjudgeable"):
            print("  裁判也没答出来:", r.get("unjudgeable"))
        print("  掌握度:", r.get("mastery"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
