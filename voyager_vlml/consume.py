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
数据里的 NaN / null 一律当「取不到」，不许写进事实（NaN 比值 = 除零的产物，
拿它当真值判比对必错）。取不到就留空数组，不要编。"""


# ---------------------------------------------------------------------------
# ⑤ 用取到的数回答这句话
# ---------------------------------------------------------------------------

def _answer_raw(question: str, obs: list[dict[str, Any]]) -> str:
    """取到数、但整理不出结构化事实时，用**原始数据**直接回答。

    ⚠️ 实测踩过：问「每个回合中各个角色的平均击杀数」，SQL 完全正确、取到 85 行，
    但答案是一张表（round × agent），压不进 `{subject, dimension, value}` 单值事实
    → 整理器返回空 → 回答为空 → 人看到「Voyager 回答不出来」。
    **取到了数却不给回答，是最糟的失败**：数据明明在那儿。
    所以这里兜底：把原始行喂给模型组织成答案，宁可少一层结构，也不能交白卷。
    """
    payload = json.dumps(
        [o.get("data") for o in obs if o.get("ok")],
        ensure_ascii=False, default=str)[:9000]
    return chat(
        [
            {"role": "system",
             "content": ("你是 Valorant 电竞数据分析师。下面是工具从数据库里取到的原始结果，"
                         "用它回答人的问题。只用给出来的数，不许补充数据里没有的东西。"
                         "结果是多行的就说清楚维度和前几行，别只丢一句「有多个」。"
                         "两到四句话。")},
            {"role": "user",
             "content": f"人的问题：{question}\n\n工具取到的原始数据：\n{payload}"},
        ],
        temperature=0.3,
        max_tokens=600,
    )


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

    # ---- 评分点（dimension 名）必须在**取数之前**定下来，且两边共用同一份 ----
    # ⚠️ 实测踩过：Voyager 侧因为没有 rubric 就自由命名成 avg_kd_ratio，
    #   裁判侧用的是另派生的 player —— 两边 dimension 对不上，覆盖率恒 0。
    #   那不是"答错了"，是"两个事实集没对齐"，必须挡掉。
    derived: list[dict[str, Any]] = []
    if task is not None:
        dims = [str(p.dimension) for p in (task.rubric or [])]
        subs = [dict(p.subject or {}) for p in (task.rubric or [])]
    else:
        derived = _derive_dims(question)
        dims = [str(p.get("dimension")) for p in derived]
        subs = [dict(p.get("subject") or {}) for p in derived]

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
    out = chat_json(
        [
            {"role": "system", "content": _extract_system()},
            {"role": "user",
             "content": _extract_user(question, observations, dims or None,
                                      subs or None)},
        ],
        temperature=0.1,
    ) if ok_calls else {"facts": []}
    # ⚠️ 实测踩过：工具明明取到了数（1 行），LLM 整理却返回空数组 —— 取到数交白卷，
    # 人看到"没回答"，掌握也不涨。数据是真的在那儿，所以**重试一次**：
    # 把"你上次返回了空数组"和原始数据再喂一遍，并明确"有数据就必须给出事实"。
    if ok_calls and not (out.get("facts") or []):
        rows_preview = json.dumps(
            [o.get("data") for o in ok_calls], ensure_ascii=False, default=str)[:3000]
        out = chat_json(
            [
                {"role": "system", "content": _extract_system()},
                {"role": "user",
                 "content": (_extract_user(question, observations, dims or None,
                                           subs or None)
                             + "\n\n⚠️ 你刚才返回了空数组，但工具明明取到了数据：\n"
                             + rows_preview
                             + "\n\n数据里有数就必须整理出事实（dimension 按上面的名字写，"
                               "值抄数据里的，不许编）；数据确实答不了这一问才允许留空。")},
            ],
            temperature=0.1,
        )

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
    answer, base_note = "", ""
    if facts:
        try:
            answer = _answer(question, facts, observations) or ""
        except Exception as e:  # pragma: no cover
            print(f"  （组织回答失败：{type(e).__name__}: {e}）")
    if not answer and ok_calls and rows:
        # 取到数但整理不出结构化事实（答案是一张表时常见）→ 用原始数据照样回答。
        # 取到了数却不给回答是最糟的失败：数据明明在那儿，人却以为它答不出来。
        try:
            answer = _answer_raw(question, observations) or ""
            if answer:
                base_note = "（用原始数据直接回答：这一问的答案是一张表，压不进单值事实）"
        except Exception as e:  # pragma: no cover
            print(f"  （用原始数据回答失败：{type(e).__name__}: {e}）")

    # 归不上已有题 → 拿这句话（+刚取到的事实）生成一道新题。
    # ⚠️ 必须在"进能力库"**之前**：答完了就得有归属，否则这道题明明答出来了，
    # 却因为没有 topic_id 进不了能力库 —— 掌握就变成了"只有归得上题的才算"，
    # 那是把两件事（答没答出来 vs 归不归得上）混成了一个条件。
    if not topic_id and auto_task and facts:
        try:
            from coach import _task_from_text
            new_id, why = await _task_from_text(question, facts)
            if new_id:
                from tasks import TASKS
                task = TASKS.get(new_id)
                topic_id, hit = new_id, f"{hit}{why}"
        except Exception as e:  # pragma: no cover
            print(f"  （生成新题失败：{type(e).__name__}: {e}）")

    # ---- 答完了 → 这题进能力库 → 掌握（判据是"答没答出来"，不是覆盖率）----
    # 判据是"答没答出来"（取到数 + 给出了回答），**不是**有没有整理成结构化事实 ——
    # 答案是一张表时压不进 facts，但那也是答出来了，照样进能力库。
    absorbed = _absorb(question, sql, topic_id) if (answer and ok_calls) else {}
    m_tot = int(absorbed.get("mastered_total") or 0)
    a_tot = int(absorbed.get("ability_total") or 0)

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
        "answer_note": base_note,
        # —— 覆盖率/掌握度（消费侧才算，见 ⑥）——
        "coverage": None,
        "score": None,
        "verdict": "",
        "covered_points": [],
        "missing_points": [],
        "unjudgeable": [],
        # —— 掌握 = 能力集合的增长（"答完了这题就到他的能力里了"）——
        "mastered": bool(absorbed.get("mastered")),
        "skill_action": str(absorbed.get("skill_action") or ""),
        "mastered_total": m_tot,
        "ability_total": a_tot,
        # 掌握度 = 已掌握 / 该掌握的总量（不是覆盖率的折算）
        "mastery": (round(m_tot / a_tot, 4) if a_tot else None),
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

    if task is None:
        # 没有现成 rubric（人的自由提问大多归不上旧题库）→ 从**这句话本身**派生
        # 评分点，让 VLML 工具链独立取一遍当尺子。这样"消费算覆盖率"才落得下去，
        # 而不是一归不上题就整体落空。判据标注成软尺子，面板上要能看出软硬。
        points = derived
        if not points:
            base["skipped"] = ("这句话既归不到有评分点的题上，也抽不出该答哪几个量 → "
                               "覆盖率无从计算（如实留空，不编造数字）")
            _log(base)
            return base
        ref_facts, ref_traj = await _referee_facts(
            question, points, env=env, schema=schema)
        base["referee_trajectory"] = ref_traj
        base["derived_points"] = [str(p.get("point") or p.get("dimension"))
                                  for p in points]
        if not ref_facts:
            # 两种失败必须分开说：排查方向完全不同
            #   · 工具都没调通 → 是链路/schema 问题
            #   · 调通了但整理不出事实 → 是评分点（dimension）对不上数据
            base["skipped"] = (
                "裁判侧没取到可比的事实："
                + ("工具调用全部失败" if not ref_traj
                   else "工具调通了，但整理不出评分点对应的事实（评分点与数据对不上）")
                + " → 没有真值可比，覆盖率无从计算（如实留空，不拿 0 分冒充）")
            _log(base)
            return base
        try:
            from loop_core import RubricPoint, evaluate_vs_referee
            rubric = [
                RubricPoint(
                    point=str(p.get("point") or p.get("dimension")),
                    subject=dict(p.get("subject") or {}),
                    dimension=str(p.get("dimension")),
                    weight=1.0, min_base=0, tolerance=0.05,
                )
                for p in points
            ]
            ev = evaluate_vs_referee(facts, rubric, ref_facts,
                                     tool_calls=len(ok_calls))
            base.update({
                "coverage": float(ev.coverage),
                "score": int(ev.score),
                "verdict": str(ev.verdict),
                "covered_points": list(ev.covered_points),
                "missing_points": list(ev.missing_points),
                "unjudgeable": list(ev.unjudgeable),
                # ⚠️ 软尺子：评分点是从这句话派生的（不是题库里既有的 rubric），
                # 真值由 VLML 工具链从 DB 独立取 —— 值仍是 DB 真值，
                # 但"该答哪几个量"是模型提的。面板必须标出来，别和 referee 混。
                "judge": "derived_dims",
            })
        except Exception as e:  # pragma: no cover
            base["skipped"] = f"比对失败，覆盖率未计算：{type(e).__name__}: {str(e)[:120]}"
        if reused and base.get("coverage") is not None:
            try:
                import skill_store
                skill_store.mark_result(
                    [str(s.get("key") or "") for s in skills[:2]],
                    bool(base["coverage"] >= 0.6))
            except Exception:  # pragma: no cover
                pass
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
        # ⚠️ 这里**不写** MasteryStore.update(score=coverage)。
        # 掌握与覆盖率是两个东西（用户 2026-10-13）：掌握 = 答完了、进能力库
        # （上面 _absorb 已经做了）；覆盖率只是这次答得全不全的质量分。
        # 把 coverage 写进 mastery 字段，等于又用分数去定义掌握 —— 两套语义打架。
        pass
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


def _derive_dims(question: str) -> list[dict[str, Any]]:
    """从这句话本身抽出"这一问该答出哪几个量"（只给名字，不给值）。

    为什么需要它：覆盖率要拿 Voyager 的事实集跟裁判的事实集比，比对键是
    (subject, dimension)。归得上旧题库时，dimension 由 rubric 给；
    归不上时（人的自由提问大多归不上）就没有 rubric → 覆盖率算不出来，
    "从消费开始算覆盖率"这条就落空了。

    所以这里让 LLM 从**这句话**派生评分点。⚠️ 派生的只是**名字**
    （该答哪几个量），**不是值**——值仍由两边各自从 DB 取：
      · Voyager 侧：用它已积累的能力取（本文件 ③）
      · 裁判侧：走 VLML 自己的工具链独立取一遍（_referee_facts）
    两个值都是 DB 真值，比对的是两个独立取数结果，不是"模型编的参考答案"。
    """
    out = chat_json(
        [
            {"role": "system",
             "content": ("从一句话里抽出「要回答它，得给出哪几个量」。只给**名字与对象**，"
                         "不给数值。2 到 4 个。只返回 JSON："
                         "{\"points\":[{\"point\":\"中文说明\",\"dimension\":\"英文维度名\","
                         "\"subject\":{\"team\":\"...\",\"map\":\"...\"}}]}\n\n"
                         "⚠️ 三个硬约束（每条都实测踩过）：\n"
                         "  1. dimension 必须是**英文 snake_case 的数值指标名** —— "
                         "kd_ratio / total_damage / usage_rate / win_rate / kast_pct 这种。\n"
                         "     **不许写中文**（实测写成「K/D 比最高」→ 裁判照它写的 SQL "
                         "只取了选手名、没取比值 → 一条事实都整理不出来 → 覆盖率落空）。\n"
                         "     **不许带 最高/最低/最多 这类排序词** —— 那是问法不是指标；"
                         "问「谁最高」时 dimension 就写那个指标本身（kd_ratio）。\n"
                         "  2. subject = **说的是谁**（team / map / player 的真实取值，"
                         "如 {\"map\":\"Corrode\"}），不要把「K/D 比最高」这种描述塞进来，"
                         "也不要把图名塞进 team 字段（实测串过一次）。\n"
                         "  3. 每个 point 都要是**能用 SQL 算出来的一个数**，"
                         "不是一段描述。")},
            {"role": "user", "content": question},
        ],
        temperature=0.0,
    )
    # 硬校验：dimension 必须是纯 ASCII 的数值指标名。
    # 中文 dimension（如「K/D 比最高」）会让两边都写不出可比的事实 ——
    # 宁可判"这一问抽不出可比的量"（如实留空），也不要拿一个对不齐的名字去比出 0 分。
    pts: list[dict[str, Any]] = []
    for p in (out.get("points") or []):
        if not isinstance(p, dict):
            continue
        d = str(p.get("dimension") or "").strip()
        if not d or not d.isascii():
            continue
        s = dict(p.get("subject") or {})
        s = {k: v for k, v in s.items() if v not in (None, "", "?")}
        pts.append({"point": str(p.get("point") or d), "dimension": d, "subject": s})
    return pts[:4]


async def _referee_by_sql(question: str, dims: list[str],
                          subs: list[dict[str, Any]], env: Any,
                          schema: str) -> list[dict[str, Any]]:
    """裁判侧取数：独立写一条 SQL（与 Voyager 侧不同提示、不同调用），执行一次。
    带一轮错误回灌重试。返回观察列表（成功才有值）。"""
    want = "\n".join(
        f"- dimension 必须逐字写成 `{d}`"
        f"{'，subject 写成 ' + json.dumps(s, ensure_ascii=False) if s else ''}"
        for d, s in zip(dims, subs)
    )
    sysmsg = ("你是原版 VLML 分析引擎（裁判）。为这句话写**一条 SELECT** 取标准答案。"
              "只输出 SQL 本体，不要解释。\n"
              "⚠️ 多表 JOIN 时列名必须带表名前缀（两表常有同名列，如 player_name，"
              "裸写会被数据库判 Ambiguous reference 直接报错）。")
    user = (f"这句话：{question}\n\n要给出的量（只给名字，值你自己从库里算）：\n{want}"
            f"\n\n当前库真实存在的表与列（只能引用这里的名字）：\n{schema[:6000]}")
    last_err = ""
    for _ in range(3):
        sql = chat(
            [{"role": "system", "content": sysmsg},
             {"role": "user", "content": user + (f"\n\n上一轮报错：{last_err}" if last_err else "")}],
            temperature=0.1, max_tokens=800,
        )
        sql = _strip_sql(sql)
        if not sql:
            continue
        obs = await env.run_sql(sql)
        if obs.get("ok"):
            return [{"tool": "query_sql", "args": {"sql_query": sql},
                     "data": obs.get("data"), "error": None}]
        last_err = str(obs.get("error") or "")[:300]
    return []


def _strip_sql(text: str) -> str:
    """从模型输出里抠出一条 SELECT（它可能带围栏/前导说明）。"""
    t = str(text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        for tag in ("sql", "SQL"):
            if t.startswith(tag):
                t = t[len(tag):]
        t = t.strip()
    i = t.upper().find("SELECT")
    if i < 0:
        return ""
    t = t[i:].strip().rstrip(";").strip()
    return t if t.upper().startswith("SELECT") else ""


def _facts_from_obs(obs: list[dict[str, Any]], dims: list[str],
                    subs: list[dict[str, Any]], question: str) -> list[dict]:
    """观察 → 事实（裁判侧）。整理为空时重试一次（工具取到了数就不能交白卷）。"""
    from loop_core import make_fact

    def _once(extra: str = "") -> list[dict]:
        out = chat_json(
            [
                {"role": "system", "content": _extract_system()},
                {"role": "user",
                 "content": _extract_user(question, obs, dims, subs) + extra},
            ],
            temperature=0.1,
        )
        facts: list[dict] = []
        for f in (out.get("facts") or []):
            if not isinstance(f, dict) or "dimension" not in f:
                continue
            facts.append(make_fact(
                subject=f.get("subject") or {},
                dimension=str(f.get("dimension")),
                value=f.get("value"), unit=str(f.get("unit") or "raw"),
                base=f.get("base"), source={"tool": "query_sql", "args": {}},
            ))
        return facts

    facts = _once()
    if not facts:
        rows_preview = json.dumps(
            [o.get("data") for o in obs], ensure_ascii=False, default=str)[:3000]
        facts = _once("\n\n⚠️ 你刚才返回了空数组，但工具明明取到了数据：\n"
                      + rows_preview
                      + "\n\n有数就必须整理出事实；数据确实答不了才允许留空。")
    return facts


async def _referee_facts(question: str,
                         points: list[dict[str, Any]],
                         env: Any = None,
                         schema: str = "") -> tuple[list[dict], list[str]]:
    """让 **VLML 自己的工具链独立取一遍数**（裁判侧），不去碰 Voyager 的取数结果。

    走的是 `coach` 那套原版 VLML 编排（工具注册表 + 数据集上下文），
    与 Voyager 侧的 env 是两条独立通路 —— 正因如此，两者的值可比。
    """
    try:
        from coach import _execute, _plan_prompt
        from loop_core import make_fact
    except Exception as e:  # pragma: no cover
        print(f"  （裁判工具链不可用：{type(e).__name__}: {e}）")
        return [], []
    dims = [str(p.get("dimension")) for p in points]
    subs = [dict(p.get("subject") or {}) for p in points]

    # 裁判取数走**同一套迭代修正机制**（solve：写 SQL → 执行 → 失败带 SQL critique
    # 重写 3 轮）。之前手写的"带错重试"循环实测 3 轮都改不对列名歧义 ——
    # critique 机制是练习侧验证过能纠错的，别重造一个弱化版。
    if env is not None:
        from voyager_vlml.agents.action import VLMLActionAgent
        act = VLMLActionAgent(env=env)
        want = "；".join(
            f"dimension=`{d}`" + (f"，subject={json.dumps(s, ensure_ascii=False)}" if s else "")
            for d, s in zip(dims, subs)
        )
        q2 = (f"（裁判·独立取标准答案）{question}\n"
              f"要给出的量（dimension 逐字用这些名字，值从库里算）：{want}\n"
              "⚠️ 比值/除法必须写 NULLIF(x, 0)（如 AVG(kills)/NULLIF(AVG(deaths),0)）—— "
              "DuckDB 里 x/0 得 NaN 不报错，取回来是个没法比的数；"
              "且优先在聚合层做除法（先 AVG 再相除），不要对逐行比值再 MAX。")
        observations, sql2 = await act.solve(q2, max_iters=3)
        ok_obs = [o for o in observations if o.get("ok")]
        if ok_obs:
            obs = [{"tool": "query_sql", "args": {"sql_query": sql2},
                    "data": ok_obs[-1].get("data"), "error": None}]
            return _facts_from_obs(obs, dims, subs, question), ["query_sql"]
        return [], []
    # 兜底：旧 VLML 编排
    # ⚠️ 实测踩过：不注入实查 schema，裁判会犯和 Voyager 一样的**列名幻觉** ——
    # 写 `base_events.player_name`，而真实列是 actor_player_name / target_player_name
    # → 三条调用全 Binder Error → 裁判侧一条数都取不到 → 覆盖率直接落空。
    # 所以这里必须把库里真实存在的表/列给它（照 entity_catalog 的"实查不写死"）。
    plan_user = _plan_prompt(question)
    if schema:
        plan_user += ("\n\n当前库真实存在的表与列（**只能引用这里出现的名字**，"
                      "尤其是列名，写错会直接报错）：\n" + schema[:6000])
    plan = chat_json(
        [
            {"role": "system", "content": "你是原版 VLML 分析引擎。只取数，不做主观推断。"},
            {"role": "user", "content": plan_user},
        ],
        temperature=0.1,
    )
    obs, traj, errors = await _execute(plan.get("calls") or [])
    if not traj and errors:
        # 一次带错重试：把工具报错原样回灌，让它换正确的列名重写
        plan2 = chat_json(
            [
                {"role": "system", "content": "你是原版 VLML 分析引擎。只取数，不做主观推断。"},
                {"role": "user",
                 "content": (plan_user + "\n\n上一轮全部调用都报错了：\n"
                             + "\n".join(str(e)[:300] for e in errors[:3])
                             + "\n\n照上面的报错改对列名/表名，重新规划。")},
            ],
            temperature=0.1,
        )
        obs, traj, _ = await _execute(plan2.get("calls") or [])
    if not traj:
        return [], []
    out = chat_json(
        [
            {"role": "system", "content": _extract_system()},
            {"role": "user",
             "content": _extract_user(question, [
                 {"tool": o.get("tool"), "args": o.get("args"),
                  "data": o.get("result"), "error": o.get("error")} for o in obs],
                 dims, subs)},
        ],
        temperature=0.1,
    )
    facts: list[dict] = []
    for f in (out.get("facts") or []):
        if not isinstance(f, dict) or "dimension" not in f:
            continue
        facts.append(make_fact(
            subject=f.get("subject") or {},
            dimension=str(f.get("dimension")),
            value=f.get("value"), unit=str(f.get("unit") or "raw"),
            base=f.get("base"), source={"tool": (traj or ["?"])[0], "args": {}},
        ))
    return facts, traj


def _absorb(question: str, sql: str, topic_id: str) -> dict[str, Any]:
    """答完了 → **这题就到他的能力里了 → 这就是掌握**（用户 2026-10-13 定）。

    掌握不是覆盖率折算出来的一个分数，而是**能力集合的增长**：
      「我问你一句，Voyager 答完了，之后这题就到他的能力里了」。

    所以这里做两件事：
      1. 把这题标记为**已掌握**（`MasteryStore.mark_completed`）；
      2. 这次的取数 SQL 存成**可复用配方**进技能库（这题本来没有配方时）。
         存的是**计算方法**，不是这次算出来的数值 —— 下次遇到同一类问法就能复用。

    ⚠️ 判据是**有没有真的答出来**（调通工具 + 取到数 + 产出回答），
    **不是覆盖率**。覆盖率算不算、算出来多少，都不影响"这题进不进能力库" ——
    否则就是把 VLML 的判分塞回了学习判定，正是设计禁止的。
    """
    out: dict[str, Any] = {"mastered": False, "skill_action": "",
                           "mastered_total": 0, "ability_total": 0}
    if not topic_id or not sql:
        return out
    try:
        import topic_graph
        st = topic_graph.MasteryStore()
        st.mark_completed(topic_id)
        out["mastered"] = True
        out["mastered_total"] = sum(
            1 for r in st.all().values() if r.get("completed"))
        try:
            out["ability_total"] = len(topic_graph.TopicGraph().topics)
        except Exception:
            out["ability_total"] = 0
    except Exception as e:  # pragma: no cover
        print(f"  （标记掌握失败：{type(e).__name__}: {e}）")
        return out

    try:
        import skill_store
        have = [k for k in (skill_store._load_raw().get("skills") or {})
                if str(k).startswith(f"{topic_id}::")]
        if have:
            skill_store.mark_result(have, True)
            out["skill_action"] = "已有配方，标记为用过且有效"
        else:
            _, key = skill_store.add(
                topic_id, topic_id,
                f"消费时答出来的取数方法：{question[:60]}",
                code=sql,
                blueprint={"source": "consume", "question": question},
                source="consume",
            )
            out["skill_action"] = f"新配方入库：{key}" if key else "（内容太短未入库）"
    except Exception as e:  # pragma: no cover
        print(f"  （存配方失败：{type(e).__name__}: {e}）")
    return out


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
    # 掌握与覆盖率**是两个东西**，分开打印：
    #   掌握 = 答完了 → 这题进能力库（能力集合增长）
    #   覆盖率 = 这次答得全不全（质量分，只给人看）
    print("--- 掌握（能力集合）---")
    if r.get("mastered"):
        print(f"已掌握 · 累计 {r.get('mastered_total')} / {r.get('ability_total')} 题"
              f"（掌握度 {float(r.get('mastery') or 0):.0%}）")
        print("  ", r.get("skill_action") or "")
    else:
        print("没答出来，未进能力库。" + (r.get("skipped") or ""))
    print("--- 覆盖率（质量分，只给人看，不进 Voyager 反馈）---")
    if r.get("coverage") is None:
        print("未计算：", r.get("skipped") or "（无评分点）")
    else:
        print(f"覆盖率 {r['coverage']:.0%} · 得分 {r['score']} · 判读 {r['verdict']}"
              f" · 尺子={r.get('judge')}")
        print("  答出:", r.get("covered_points"))
        print("  缺  :", r.get("missing_points"))
        if r.get("unjudgeable"):
            print("  裁判也没答出来:", r.get("unjudgeable"))
        if r.get("judge") == "derived_dims":
            print("  ⚠️ 软尺子：评分点是从这句话派生的，真值由 VLML 工具链从 DB 独立取；"
                  "值仍是 DB 真值，但『该答哪几个量』是模型提的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
