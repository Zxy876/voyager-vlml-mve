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
   ⚠️ 这个数**不回灌**任何 prompt：不进 action 的迭代修正、不进 critic、
   不进 curriculum 的选题（选题仍是有序课程，不读分数）。
   只落 `coach_log.jsonl` 给面板 —— 与练习侧 `loop.py` 里 coverage=None 呼应：
   练习不判分，消费才判分，判给人看。

判分怎么判（2026-10-13 用户定的判法，取代旧的"按键比对事实"）：

    用户原话：「整理事实这个事情本来就是交给 LLM 的，如果在消费侧，
    直接比对 voyager 和 VLML 的方法和做法，算掌握度和曲线，进能力库，
    然后让 LLM 产出叙事洞察就好啦」

    ① Voyager 用它已积累的能力取数（可能复用配方）
    ② 裁判（VLML）**独立**取一遍：只取数，不整理
    ③ `_method_compare`：把两边的 SQL + 取到的结果摊开，让 LLM 判
       是不是同一口径、Voyager 的做法有没有硬伤 → **一致度即覆盖率**
    ④ `_narrate`：LLM 基于取到的数（+ 比对结论）产出叙事洞察
    ⑤ `_absorb`：答完了 → 这题进能力库 → 掌握

    旧判法要求两边各自整理成 {subject, dimension, value} 再按键比对，
    而"整理"这步最脆：答案是一张表时压不进单值事实 → 两边都交白卷 →
    覆盖率落空 + 回答为空（实测取到 85 行却显示"回答不出来"）。
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
from voyager_vlml.agents.critic import has_real_data  # noqa: E402
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

def _method_compare(question: str, v_sql: str, v_obs: list[dict[str, Any]],
                    r_sql: str, r_obs: list[dict[str, Any]]) -> dict[str, Any]:
    """**直接比对 Voyager 和 VLML 的方法与做法**（用户 2026-10-13 定的判法）。

    为什么不再走「两边各自整理成 {subject, dimension, value} 再按键比对」：
      实测踩过 —— 问「每个回合中各个角色的平均击杀数」，Voyager 的 SQL 完全正确、
      取到 85 行，但答案是一张表，压不进单值事实 → 整理器交白卷 → 回答为空 →
      人只看到「Voyager 回答不出来」。数据是有的，病在"非要塞进结构化事实"。

    所以这里把**两边的方法（SQL）和做法（取到的东西）**摊开让 LLM 判：
      它俩在算同一个量吗？口径一致吗？Voyager 的做法有没有硬伤
      （除零、列名错、粒度错、方向反）？给出 0-1 的一致度。
    LLM 天生擅长这种语义比对，不需要先把结果压成键值。

    返回 {agreement, same_metric, voyager_sound, reason, diff}
    """
    def _brief(obs: list[dict[str, Any]]) -> str:
        return json.dumps(
            [o.get("data") for o in obs if o.get("ok")],
            ensure_ascii=False, default=str)[:3500]
    def _nrow(obs: list[dict[str, Any]]) -> int:
        return sum(int(o.get("rows") or 0) for o in obs if o.get("ok"))

    payload = (
        f"这句话：{question}\n\n"
        f"【Voyager 的方法】SQL：\n{v_sql or '（无）'}\n"
        f"取到 {_nrow(v_obs)} 行，结果（截断）：\n{_brief(v_obs) or '（没取到）'}\n\n"
        f"【VLML 的方法】SQL：\n{r_sql or '（无）'}\n"
        f"取到 {_nrow(r_obs)} 行，结果（截断）：\n{_brief(r_obs) or '（没取到）'}"
    )
    out = chat_json(
        [
            {"role": "system",
             "content": ("你是数据分析裁判。比较**两个做法**在算什么、怎么算的，"
                         "判断它们是不是同一口径、Voyager 的做法有没有硬伤。\n"
                         "硬伤指：除零（x/0 得 NaN）、列名错、粒度错（要每回合却给全场合计）、"
                         "方向反（连败说成连胜）、口径错（队伍整体说成某个选手）。\n"
                         "⚠️ 两个判据**互相独立**，按这个顺序判，别混：\n"
                         "  第一步 voyager_sound：**只看 Voyager 自己那条 SQL**有没有硬伤"
                         "（列名对不对、粒度对不对、有没有 x/0、方向对不对）。\n"
                         "  第二步 same_metric：两边算的是不是同一个量。\n"
                         "  第三步 agreement：口径一致度（不是「谁对谁错」分）。\n"
                         "    **两边口径不同 ≠ Voyager 有硬伤** —— 实测踩过："
                         "问「每个回合各个角色的平均击杀数」，Voyager 算得完全正确，"
                         "是裁判自己多算了 deaths 算成 K/D，结果 Voyager 被判"
                         "「做法有硬伤」，面板上显示的是冤枉它。\n"
                         "    裁判跑偏时：same_metric=false，voyager_sound 仍按第一步判，"
                         "并在 diff 里写清「是裁判多算了什么/少算了什么」。\n"
                         "    reason 里必须写「Voyager 无硬伤」或「Voyager 有硬伤：…」，"
                         "与 voyager_sound 保持一致（别自相矛盾）。\n"
                         "⚠️ 两边都取到 0 行时，SQL 再像也**没有数可比** —— "
                         "这时 agreement 必须给 0，same_metric=false，"
                         "reason 写明「两边都没取到数，无从比对」。别把两个空结果"
                         "判成「口径一致」 —— 实测踩过：Cloud9/Corrode 在当前库里没有数据，"
                         "两边各取回 0 行，方法长得一样，就被判成了 100% 一致。"
                         "那等于给「两边都没取到数」发满分，比报错危险得多。\n"
                         "只返回 JSON："
                         '{"agreement": 0到1的小数, "same_metric": true/false,'
                         ' "voyager_sound": true/false, "reason": "一句话判断依据",'
                         ' "diff": "两边做法的具体差别（没有就写空串）"}')},
            {"role": "user", "content": payload},
        ],
        temperature=0.0,
    )
    try:
        ag = float(out.get("agreement") or 0.0)
    except (TypeError, ValueError):
        ag = 0.0
    reason = str(out.get("reason") or "")
    sound = bool(out.get("voyager_sound"))
    # 一致性兜底：模型会出现"reason 写『Voyager 无硬伤』但 voyager_sound=false"
    # 的自相矛盾（实测踩到）。布尔位会直接决定面板上"做法站得住/有硬伤"的红绿标，
    # 与它自己给的理由打架时，以**它写的理由**为准 —— 理由是它真正的判断，
    # 布尔位只是在两边口径不同的情境下被带偏了。
    if not sound and reason and ("硬伤" in reason):
        for neg in ("无硬伤", "没有硬伤", "没硬伤", "无 硬伤"):
            if neg in reason and f"有{neg}" not in reason:
                sound = True
                break
    return {
        "agreement": max(0.0, min(1.0, ag)),
        "same_metric": bool(out.get("same_metric")),
        "voyager_sound": sound,
        "reason": reason,
        "diff": str(out.get("diff") or ""),
    }


def _narrate(question: str, obs: list[dict[str, Any]],
             compare: dict[str, Any] | None = None) -> str:
    """基于**取到的数**产出叙事洞察（答案是一张表也照样能说清楚）。"""
    payload = json.dumps(
        [o.get("data") for o in obs if o.get("ok")],
        ensure_ascii=False, default=str)[:9000]
    extra = ""
    if compare:
        extra = (
            f"\n\n另外，VLML 独立取过一遍，比对结论：一致度 {compare['agreement']:.0%}，"
            f"{'做法站得住' if compare.get('voyager_sound') else '做法有硬伤'}；"
            f"{compare.get('reason') or ''}"
            f"{('；差别：' + compare['diff']) if compare.get('diff') else ''}\n"
            "回答里照实说，别替它遮掩硬伤。"
        )
    return chat(
        [
            {"role": "system",
             "content": ("你是 Valorant 电竞数据分析师。用工具从数据库取到的**原始数据**"
                         "回答人的问题，并给出一两条洞察。\n"
                         "只用给出来的数，不许补充数据里没有的东西。\n"
                         "⚠️ 结果是多行的（可能几百行）时**不要逐行罗列**："
                         "说清维度与量级、点出最高/最低/异常的几个，再给一条洞察。\n"
                         "⚠️ 整段控制在 250 字以内 —— 实测踩过：给 1334 行让它讲，"
                         "它逐地图逐回合铺开，输出被截断在半句上，人读到的是一段残话。")},
            {"role": "user",
             "content": f"人的问题：{question}\n\n工具取到的原始数据：\n{payload}{extra}"},
        ],
        temperature=0.3,
        max_tokens=900,
    )


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
    # ⚠️ 降级判据必须含**真空结果**（2026-10-14 实测坑）：纯聚合 SQL 在空表上
    #   返回 1 行 (NULL, 0) —— ok=True、rows=1，旧判据 `any(o.get("ok"))` 会
    #   当成"复用成功"，于是坏配方被当成能力用掉，答出「0% / 样本量 0」。
    #   原版同构点：critic 判的是"任务到底做成没有"，不是"代码跑没跑起来"。
    if not any(has_real_data(o) for o in observations):
        # 配方没复用成（库里没有 / 用不上 / 回填后跑不通 / 回填后取到真空值）
        # → 现场写，如实标注
        if sql:
            print(f"  （配方回填后没取到真数，降级为现场写 SQL：{note[:60]}）")
        reused, sql = False, ""
        act = VLMLActionAgent(env=env)
        observations, sql = await act.solve(question, max_iters=3)

    ok_calls = [o for o in observations if o.get("ok")]
    # 真的取到了数（ok 且非真空）—— 判分 / 回答 / 沉淀都只认这个，
    # 披露字段（trajectory / tool_calls）仍用 ok_calls，让人看见实际发生了什么。
    real_calls = [o for o in ok_calls if has_real_data(o)]
    rows = sum(int(o.get("rows") or 0) for o in ok_calls)
    real_rows = sum(int(o.get("rows") or 0) for o in real_calls)
    trajectory = [str(o.get("tool") or "") for o in ok_calls]

    # ---- ④ 整理事实（**面板披露「取到了哪些数」用，不再是判分的前提**）----
    # ⚠️ 2026-10-13 改判法（用户原话）：「整理事实这个事情本来就是交给 LLM 的，
    #   如果在消费侧，直接比对 voyager 和 VLML 的方法和做法，算掌握度和曲线，
    #   进能力库，然后让 LLM 产出叙事洞察就好啦」。
    #   旧判法 = 两边各自整理成 {subject, dimension, value} 再**按键比对**，
    #   而整理这步恰恰最脆：答案是一张表时压不进单值事实 → 整理器交白卷 →
    #   覆盖率落空 + 回答为空（实测取到 85 行却「回答不出来」）。
    #   现在：事实照旧整理（审计线索给人看），判分改看 ⑤ 的**方法与做法比对**。
    out = chat_json(
        [
            {"role": "system", "content": _extract_system()},
            {"role": "user",
             "content": _extract_user(question, observations, dims or None,
                                      subs or None)},
        ],
        temperature=0.1,
    ) if real_calls else {"facts": []}

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

    # ---- ⑤ 裁判（VLML）独立取一遍数：**只取数，不整理** ----
    # 裁判侧以前还要再走一遍「整理成事实」，那一遍是纯浪费且是失败源
    # （详见 ④ 的注释）。现在它只负责把数取回来，比对交给 `_method_compare`。
    ref_points = derived
    if not ref_points and task is not None:
        # 归上了有 rubric 的题 → 用**它的**评分点给裁判当"要给出哪些量"
        # （比从这句话重新派生更准，那是题库里既有的标准）。
        ref_points = [
            {"point": str(getattr(p, "point", "") or getattr(p, "dimension", "")),
             "dimension": str(getattr(p, "dimension", "")),
             "subject": dict(getattr(p, "subject", None) or {})}
            for p in (task.rubric or [])
        ]
    ref_obs: list[dict[str, Any]] = []
    ref_sql: str = ""
    ref_traj: list[str] = []
    # 裁判闸同样只认真数：Voyager 侧是真空结果时，拿它去"比"没有意义 ——
    # 两个空结果会被判成口径一致（见 ⑥ 注释里的实测坑）。
    if real_calls:
        ref_obs, ref_sql, ref_traj = await _referee_observations(
            question, ref_points, env=env, schema=schema)

    # ---- ⑥ 直接比对两边的**方法与做法**（判分就从这里出）----
    # ⚠️ 硬前提：**两边都得真的取到数**。实测踩过：Cloud9/Corrode 在当前库里没有数据，
    # Voyager 取回 0 行、裁判也取回 0 行，两条 SQL 长得几乎一样 → 被判成 100% 一致。
    # 那等于给"两边都没取到数"发满分 —— 比报错危险得多（面板上是绿油油的 100%）。
    # 所以任何一路 0 行就不判，如实留空。
    # ⚠️ 2026-10-14 再补一刀：「0 行」要连**真空行**一起算 —— 空表上的纯聚合返回
    #   1 行 (NULL, 0)，行数是 1 但不是数（见 critic.is_vacuous）。裁判侧同理：
    #   两边都查了空表时，真空行会让"两路都没数"伪装成"两路都有数"。
    ref_rows = sum(int(o.get("rows") or 0) for o in ref_obs if o.get("ok"))
    ref_real_rows = sum(
        int(o.get("rows") or 0) for o in ref_obs
        if o.get("ok") and has_real_data(o))
    compare: dict[str, Any] = {}
    if ref_obs and real_rows > 0 and ref_real_rows > 0:
        try:
            compare = _method_compare(question, sql, observations, ref_sql, ref_obs)
        except Exception as e:  # pragma: no cover
            print(f"  （方法比对失败：{type(e).__name__}: {e}）")
    elif ref_obs:
        print(f"  （不判分：Voyager 取到 {real_rows} 真行 / VLML 取到 "
              f"{ref_real_rows} 真行，有一路没取到真数）")

    # ---- ⑦ 用取到的数回答：LLM 产出**叙事洞察** ----
    # 回答不再依赖 facts 非空 —— 取到了数就该说话，压不进单值事实不叫"没答出来"。
    # ⚠️ 但"取到数"必须是**真数**：真空结果（1 行 (NULL, 0)）上没有叙事可言，
    #   基于它写出来的叙事是编造（实测答出「胜率 0%、样本量 0」，真实是 100%、n=7）。
    answer, base_note = "", ""
    if real_calls and real_rows:
        try:
            answer = _narrate(question, observations, compare or None) or ""
            if answer:
                base_note = ("（叙事洞察：LLM 基于取到的原始数据产出"
                             + ("，并照实说了与 VLML 做法比对的结论" if compare else "") + "）")
        except Exception as e:  # pragma: no cover
            print(f"  （叙事洞察失败：{type(e).__name__}: {e}）")
    if not answer and facts:
        try:
            answer = _answer(question, facts, observations) or ""
        except Exception as e:  # pragma: no cover
            print(f"  （组织回答失败：{type(e).__name__}: {e}）")
    if not answer and real_calls and real_rows:
        # 兜底：叙事没出就用原始数据平铺回答。取到了数却不给回答是最糟的失败。
        try:
            answer = _answer_raw(question, observations) or ""
            if answer:
                base_note = "（用原始数据直接回答）"
        except Exception as e:  # pragma: no cover
            print(f"  （用原始数据回答失败：{type(e).__name__}: {e}）")

    # 归不上已有题 → 拿这句话（+刚取到的事实）生成一道新题。
    # ⚠️ 必须在"进能力库"**之前**：答完了就得有归属，否则这道题明明答出来了，
    # 却因为没有 topic_id 进不了能力库 —— 掌握就变成了"只有归得上题的才算"，
    # 那是把两件事（答没答出来 vs 归不归得上）混成了一个条件。
    # 判据放宽到「答出来了」（facts 或 answer 任一非空）：答案是一张表时压不进 facts，
    # 但那也是答出来了 —— 不能因为没有结构化事实就没归属，进而进不了能力库。
    if not topic_id and auto_task and (facts or answer):
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
    # ⚠️ "取到数"同样只认真数：真空结果上的"答完了"是假象，把它沉淀进能力库
    #   等于把坏配方洗成技能（实测 fb_conversion_analysis 就是这么来的）。
    absorbed = _absorb(question, sql, topic_id) if (answer and real_calls) else {}
    m_tot = int(absorbed.get("mastered_total") or 0)
    a_tot = int(absorbed.get("ability_total") or 0)
    if absorbed.get("mastered") and not topic_id:
        # 归不上已有题但答出来了 → 它有了自己的归属（按这句话登记的能力项）。
        # 归属必须回写，否则面板上这题永远显示"未归入任何题"，人读到的是
        # "它没学会"，而真相是"它答出来了，只是这句话不在旧题库里"。
        topic_id = str(absorbed.get("topic_id") or "")
        hit = f"{hit}（这句话不在旧题库里，按它自己登记为能力项）" if hit else \
              "（这句话不在旧题库里，按它自己登记为能力项）"

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
        # 真空披露：rows 里有多少行是**真数**（ok 调用在空表纯聚合上会返回
        # 1 行 (NULL, 0) —— rows=1 但不是数）。real_rows < rows 即命中了真空。
        "real_rows": real_rows,
        "vacuous": bool(ok_calls) and not real_calls,
        "sql": sql,
        "errors": [str(o.get("error") or "") for o in observations if not o.get("ok")],
        "facts": facts,
        "facts_count": len(facts),
        "answer": answer,
        "answer_note": base_note,
        # —— 裁判（VLML）独立取的那一路，与 Voyager 那一路并列披露 ——
        "referee_sql": ref_sql,
        "referee_rows": ref_rows,
        "referee_trajectory": ref_traj,
        # —— 方法与做法的比对结论（判分的依据，见 ⑥）——
        "method_compare": compare or None,
        "method_reason": str(compare.get("reason") or ""),
        "method_diff": str(compare.get("diff") or ""),
        "same_metric": bool(compare.get("same_metric")),
        "voyager_sound": bool(compare.get("voyager_sound")),
        # 两边算的不是同一个量 → 这一问**没真正判到**（多数情况是裁判自己跑偏）。
        # 这时覆盖率是"口径一致度 0"，不是"Voyager 答错" —— 面板必须分开展示，
        # 否则一个红彤彤的 0% 会把"没判到"说成"能力不行"。
        "judged": bool(compare.get("same_metric")),
        # —— 覆盖率/掌握度（消费侧才算，见 ⑥）——
        "coverage": None,
        "score": None,
        "verdict": "",
        "covered_points": [],
        "missing_points": [],
        "unjudgeable": [],
        # 硬尺子（只有归上有 rubric 的题时才跑）附加，与主尺子并列给面板看
        "coverage_referee": None,
        "verdict_referee": "",
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
    # ⚠️ 只认真数：ok 调用全是真空结果（1 行 (NULL, 0)）时，facts 没有真实来源，
    #   跟"一次都没调通"是同一回事 —— 不能拿真空行算覆盖率（实测会算出绿油油的 100%）。
    if not real_calls:
        base["skipped"] = ("没有取到真实数据（调用失败，或结果全是真空值："
                           "空表上的纯聚合返回 1 行 (NULL, 0)），事实无来源，"
                           "不计覆盖率（避免给编造打分）")
        _log(base)
        return base

    pts = [str(p.get("point") or p.get("dimension")) for p in (ref_points or [])]
    base["derived_points"] = pts

    if compare:
        # 主尺子：把两边的方法（SQL）与做法（取到的东西）摊开让 LLM 判
        # —— 不再要求两边先把结果压成 {subject, dimension, value} 单值事实。
        # 一致度即覆盖率：它量的是"Voyager 这一路的做法，跟 VLML 独立那一路
        # 是不是同一口径、有没有硬伤"。
        base.update({
            "coverage": float(compare["agreement"]),
            "score": int(round(float(compare["agreement"]) * 100)),
            "verdict": ("两边口径一致" if compare["same_metric"]
                        else "两边算的不是同一个量"),
            # 比的是做法不是键，所以"答出/缺"说的是**评分点有没有被同一口径覆盖**
            "covered_points": pts if compare["same_metric"] else [],
            "missing_points": [] if compare["same_metric"] else pts,
            "unjudgeable": (
                ([] if compare["voyager_sound"]
                 else ["Voyager 侧做法有硬伤：" + (compare.get("reason") or "")])
                + ([] if compare["same_metric"]
                   else ["两边算的不是同一个量，这一问没真正判到："
                         + (compare.get("diff") or compare.get("reason") or "")])),
            "judge": "method_compare",
        })
    elif not ref_traj:
        base["skipped"] = ("裁判（VLML）这一路没有取到真实数据（调用失败，或结果全是"
                           "真空值：空表上的纯聚合返回 1 行 (NULL, 0)）→ "
                           "没有第二路做法可比，覆盖率无从计算（如实留空，不拿 0 分冒充）")
    elif rows <= 0 or ref_rows <= 0:
        # 两路都得真的取到数才判 —— 否则两个空结果会被判成"口径一致 100%"
        # （实测：Cloud9/Corrode 在当前库没数据，两边 0 行，一样的方法，判了满分）。
        # ⚠️ 真空行也算"没取到数"：空表纯聚合的 1 行 (NULL, 0) 不是数。
        base["skipped"] = (
            f"有一路没取到数（Voyager {real_rows} 真行 / VLML {ref_real_rows} 真行）→ "
            "没有数可比，覆盖率如实留空。**两边都是 0 行时 SQL 再像也不能判一致** —— "
            "那等于给『两边都没取到数』发满分。")
    else:
        base["skipped"] = ("裁判（VLML）取到数了，但两边的做法比对没出结论 → "
                           "覆盖率如实留空，不拿 0 分冒充")

    # 技能有效性反馈（技能库自己维护，不是给 Voyager 的反馈）
    if reused and base.get("coverage") is not None:
        try:
            import skill_store
            skill_store.mark_result(
                [str(s.get("key") or "") for s in skills[:2]],
                bool(base["coverage"] >= 0.6))
        except Exception:  # pragma: no cover
            pass

    # ---- 硬尺子：只在这句话**归上了有 rubric 的题**时才额外跑一次 ----
    # 旧题库的 rubric 是既有的标准评分点，比从这句话派生的更硬。
    # 它与上面的主尺子**并列**给面板看，不互相折算。
    # ⚠️ 这里**不写** MasteryStore.update(score=coverage)：
    #   掌握与覆盖率是两个东西（用户 2026-10-13）—— 掌握 = 答完了、进能力库
    #   （上面 _absorb 已经做了），覆盖率只是这次答得全不全的质量分。
    #   把 coverage 写进 mastery，等于又用分数去定义掌握，两套语义打架。
    if task is not None:
        try:
            import vlml0_referee
            from loop_core import evaluate_vs_referee
            ref = await vlml0_referee.answer(task)
            ev = evaluate_vs_referee(
                facts, list(task.rubric or []), list(ref.facts or []),
                tool_calls=len(ok_calls),
            )
            base.update({
                "coverage_referee": float(ev.coverage),
                "verdict_referee": str(ev.verdict),
                "judge": (f"{base.get('judge') or ''}+referee".strip("+")
                          or "referee"),
            })
        except Exception as e:  # pragma: no cover
            base["referee_skipped"] = (
                f"硬尺子未计算：{type(e).__name__}: {str(e)[:120]}")

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
      · 裁判侧：走 VLML 自己的工具链独立取一遍（_referee_observations）
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


async def _referee_observations(question: str,
                                 points: list[dict[str, Any]],
                                 env: Any = None,
                                 schema: str = ""
                                 ) -> tuple[list[dict[str, Any]], str, list[str]]:
    """裁判（VLML）**独立**取一遍数 —— **只取数，不整理成事实**。

    ⚠️ 2026-10-13 改判法（用户原话）：「整理事实这个事情本来就是交给 LLM 的，
       如果在消费侧，直接比对 voyager 和 VLML 的方法和做法，算掌握度和曲线，
       进能力库，然后让 LLM 产出叙事洞察就好啦」。

    以前裁判侧取完数还要再走一遍「整理成 {subject, dimension, value}」，
    那一遍既多花两次 LLM 调用，又是**主要失败源**：答案是一张表时压不进
    单值事实 → 裁判侧交白卷 → 覆盖率直接落空（明明两边都取到了数）。
    现在裁判只负责把数取回来，比对交给 `_method_compare` 直接看两边的
    方法与做法 —— LLM 天生擅长这种语义比对，不需要先把结果压成键值。

    返回 (observations, sql, trajectory)。取不到就返回空（调用方如实留空，不编造）。
    """
    dims = [str(p.get("dimension")) for p in (points or [])]
    subs = [dict(p.get("subject") or {}) for p in (points or [])]

    # 裁判取数走**同一套迭代修正机制**（solve：写 SQL → 执行 → 失败带 SQL critique
    # 重写 3 轮）。之前手写的"带错重试"循环实测 3 轮都改不对列名歧义 ——
    # critique 机制是练习侧验证过能纠错的，别重造一个弱化版。
    want = "；".join(
        f"dimension=`{d}`" + (f"，subject={json.dumps(s, ensure_ascii=False)}" if s else "")
        for d, s in zip(dims, subs)
    )
    q2 = (f"（裁判·独立取标准答案）{question}\n"
          + (f"要给出的量（dimension 逐字用这些名字，值从库里算）：{want}\n" if want else "")
          + "⚠️ 比值/除法必须写 NULLIF(x, 0)（如 AVG(kills)/NULLIF(AVG(deaths),0)）—— "
            "DuckDB 里 x/0 得 NaN 不报错，取回来是个没法比的数；"
            "且优先在聚合层做除法（先 AVG 再相除），不要对逐行比值再 MAX。\n"
            "⚠️ 多表 JOIN 时列名必须带表名前缀（两表常有同名列，裸写会 Ambiguous）。\n"
            # 2026-10-14：schema 中部虽已标注空表，实测 glm-4-flash 仍会
            # 3 轮迭代都查 agg_team_round_summary（0 行）—— 空表纯聚合返回
            # 1 行 (NULL, 0)，看着像取到数其实没有。在 user message 里再强调
            # 一遍（不放答案，只放选表纪律，裁判的独立性不受影响）。
            "⚠️ schema 里标注「0 行 · 空表」的表**查了只会得到 (NULL, 0)**，"
            "优先选标注了行数的表；若算出来的比值全列是 NULL，说明查错了表。")
    if env is not None:
        from voyager_vlml.agents.action import VLMLActionAgent
        act = VLMLActionAgent(env=env)
        observations, sql2 = await act.solve(q2, max_iters=3)
        ok_obs = [o for o in observations if o.get("ok")]
        # ⚠️ 2026-10-14：ok ≠ 取到数。空表纯聚合返回 1 行 (NULL, 0)，
        #   ok=True 但是真空 —— 重新打包时不能把真空洗成"成功观察"，
        #   否则 ⑥ 的 ref_real_rows 判据被绕过，两个真空结果又会被判成口径一致。
        #   （实测：裁判 3 轮迭代都查了 schema 里标注"0 行别查"的
        #   agg_team_round_summary，最后一条 ok 观察正是 (None, 0)。）
        #   另：solve 取到真数即 break，所以真数观察若存在必是最后一条，
        #   与返回的 sql2 配套；不存在即 3 轮都没取到真数，如实返回空。
        real_obs = [o for o in ok_obs if has_real_data(o)]
        if not real_obs:
            return [], sql2, []
        last = real_obs[-1]
        return ([{"tool": "query_sql", "args": {"sql_query": sql2},
                  "data": last.get("data"), "rows": last.get("rows") or 0,
                  "error": None, "ok": True}], sql2, ["query_sql"])

    # 兜底：env 不可用时走旧 VLML 编排（工具注册表 + 数据集上下文）。
    # ⚠️ 实测踩过：不注入实查 schema，裁判会犯和 Voyager 一样的**列名幻觉** ——
    # 写 `base_events.player_name`，而真实列是 actor_player_name / target_player_name
    # → 三条调用全 Binder Error → 裁判侧一条数都取不到 → 覆盖率直接落空。
    # 所以这里必须把库里真实存在的表/列给它（照 entity_catalog 的"实查不写死"）。
    try:
        from coach import _execute, _plan_prompt
    except Exception as e:  # pragma: no cover
        print(f"  （裁判工具链不可用：{type(e).__name__}: {e}）")
        return [], "", []
    plan_user = _plan_prompt(q2)
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
        return [], "", []
    return ([{"tool": o.get("tool"), "args": o.get("args"),
              "data": o.get("result"), "error": o.get("error"),
              "ok": not o.get("error")} for o in obs],
            "", list(traj))


_RUNTIME_TOPICS_FILE = _MVE_DIR / "runtime_topics.json"


def _runtime_topics() -> dict[str, Any]:
    """消费侧**答出来但归不上图谱节点**的题，落在这里（能力集合的新增项）。"""
    try:
        if _RUNTIME_TOPICS_FILE.exists():
            return json.loads(_RUNTIME_TOPICS_FILE.read_text(encoding="utf-8"))
    except Exception:  # pragma: no cover
        pass
    return {}


def _runtime_topic_id(question: str) -> str:
    """答出来了、但归不上任何已有题 → 建一个**稳定**的能力 id。

    为什么必须建（用户的定义）：「我问你一句，Voyager 答完了，之后这题就到他的
    能力里了；这就是掌握！」—— **归不上题 ≠ 没答出来**。
    不建的话，掌握就变成了"只有归得上旧题库的才算"，那是把两件事
    （答没答出来 vs 归不归得上）混成了一个条件。
    实测（2026-10-13）：问「每个回合中各个角色的平均击杀数」，Voyager 取到 484 行、
    回答也完整产出了，面板却显示「没答出来，未进能力库」—— 就是被这个条件卡住的。

    稳定 = 同一句话永远得到同一个 id（问题文本的哈希），所以问第二遍不会
    再建一个新节点把分母灌水（掌握度分母 = 图谱节点 + 这里登记的能力项）。
    """
    import hashlib
    h = hashlib.md5(str(question or "").strip().encode("utf-8")).hexdigest()[:8]
    tid = f"consume_{h}"
    reg = _runtime_topics()
    if tid not in reg:
        reg[tid] = {"id": tid, "name": str(question or "")[:60],
                    "question": str(question or ""), "source": "consume"}
        try:
            _RUNTIME_TOPICS_FILE.write_text(
                json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:  # pragma: no cover
            print(f"  （登记新能力项失败：{type(e).__name__}: {e}）")
    return tid


def _coach_log_path() -> Path:
    """消费线的账本路径（与 `coach._log_import` 写的是同一个文件）。"""
    try:
        from coach import COACH_LOG
        return Path(COACH_LOG)
    except Exception:  # pragma: no cover
        return _MVE_DIR / "coach_log.jsonl"


def _consume_book() -> set[str]:
    """**消费侧那本能力账** —— 原版 `curriculum.py:135` 的 `completed_tasks` 的同构物。

    取 `coach_log.jsonl` 里 `mode=voyager_consume` 且 `mastered` 为真的去重 topic_id。

    ⚠️ 为什么**不**去数 `MasteryStore`（`topic_mastery.jsonl`）里 completed 的条数：
      那上面的 `completed` 是个**去重位**（「这节点规划过了，`select_next` 别再规划」），
      练习循环（`curriculum.py:296`）和旧后端（`run_mve.py:463`）也都在写。
      拿它当"掌握"，练习线那 29 条 `runtime_*` 就混进了分子 —— 服务器实测
      `累计 58 / 45 题（掌握度 129%）`，**分子比分母大**。
      根因与拆法见 `topic_graph.MasteryStore.mark_completed` 的 docstring。

    照原版 Voyager：
      `voyager/agents/curriculum.py:135` 学习曲线 = `len(self.completed_tasks)`
      —— 那是**学习循环自己那本账**，不是全局共享的 mastery 文件；
      `:339` 只在成功时 append，**失败不计入**；原版压根没有"考试"。
    所以：**学习时不计入**。消费线自己守住这本账，练习跑通只推进课程规划，
    不进能力集合 —— 能力只从人真的问了、并且答出来了那侧长。
    """
    p = _coach_log_path()
    book: set[str] = set()
    if not p.exists():
        return book
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(r.get("mode") or "") != "voyager_consume":
                continue
            # mastered 为假 = 没取到数 / 没产出回答 → 照原版**失败不计入**
            if not r.get("mastered"):
                continue
            tid = str(r.get("topic_id") or "").strip()
            if tid:
                book.add(tid)
    except Exception:  # pragma: no cover
        pass
    return book


def _ability_universe() -> set[str]:
    """能力全集（掌握度的**分母**）：图谱节点 ∪ **消费侧**登记的能力项。

    ⚠️ 为什么要把练习线的 `runtime_*` 排掉：
      `runtime_topics.json` 是练习线（`loop.py:_save_runtime_topic`）与消费线
      （本文件 `_runtime_topic_id`）**共用的同一个文件**，服务器上 16 项里
      **12 项是 `source="practice"`**。练习线登记的题是"课程打算练的"，
      不是"人问过的"，算进分母只会稀释掌握度 —— 而且那些题消费侧大概率
      永远不答，等于分母里躺着 12 道永远拿不到的分。

    ⚠️ 为什么分母要含**还没掌握**的图谱节点：
      分母是"这门课总共多少题"，分子是"其中会了几道" —— 这样掌握度才是
      "啃下来多少"，而不是"答过的里面对了多少"（后者会虚假地贴近 100%）。
    """
    ids: set[str] = set()
    try:
        import topic_graph
        ids |= set(topic_graph.TopicGraph().topics.keys())
    except Exception:  # pragma: no cover
        pass
    for k, v in _runtime_topics().items():
        k = str(k)
        if k.startswith("consume_") or str((v or {}).get("source") or "") == "consume":
            ids.add(k)
    return ids


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

    ⚠️ 归不上已有题时**照样进能力库**（`_runtime_topic_id` 给它一个稳定 id），
    不能因为归不上就判"没掌握" —— 那等于把"答没答出来"和"归不归得上"
    两个条件捆在一起（实测吃过这次亏）。
    """
    out: dict[str, Any] = {"mastered": False, "skill_action": "",
                           "mastered_total": 0, "ability_total": 0, "topic_id": ""}
    if not sql:
        return out
    tid = topic_id or _runtime_topic_id(question)
    out["topic_id"] = tid
    try:
        import topic_graph
        # 仍然标 completed —— 但那是它**原本**的意思：「这节点规划过了，别再规划」
        # （人已经问过并且答出来的题，课程不必再练一遍）。带 source="consume"
        # 标明这次是消费侧标的，将来能审计；语义拆法见 mark_completed 的 docstring。
        topic_graph.MasteryStore().mark_completed(tid, source="consume")
        out["mastered"] = True
        # 分子分母**同源**：都只从消费侧这一套集合算，不再掺练习线的账。
        # 以前分子数 MasteryStore 全部 completed（58，含练习线 29 条 runtime_*）、
        # 分母数「图谱 29 + runtime_topics 16」（含练习线 12 条 practice 项），
        # 两套集合口径不一致 → 实跑打出 129%（分子比分母大）。
        book = _consume_book() | {tid}
        # 分母再并上 book：外部传进来的 topic_id 可能既不在图谱也不在登记册，
        # 并一下保证 mastered ≤ ability 恒成立，百分比不会越过 100%。
        universe = _ability_universe() | book
        out["mastered_total"] = len(book)
        out["ability_total"] = len(universe)
    except Exception as e:  # pragma: no cover
        print(f"  （标记掌握失败：{type(e).__name__}: {e}）")
        return out

    try:
        import skill_store
        have = [k for k in (skill_store._load_raw().get("skills") or {})
                if str(k).startswith(f"{tid}::")]
        if have:
            skill_store.mark_result(have, True)
            out["skill_action"] = "已有配方，标记为用过且有效"
        else:
            _, key = skill_store.add(
                tid, tid,
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
        if r.get("method_reason"):
            print("  比对依据:", r.get("method_reason"))
        if r.get("method_diff"):
            print("  两边差别:", r.get("method_diff"))
        print("  答出:", r.get("covered_points"))
        print("  缺  :", r.get("missing_points"))
        if r.get("unjudgeable"):
            print("  硬伤:", r.get("unjudgeable"))
        if r.get("coverage_referee") is not None:
            print(f"  硬尺子（题库 rubric）覆盖率 {r['coverage_referee']:.0%}"
                  f" · {r.get('verdict_referee')}")
        elif r.get("referee_skipped"):
            print("  硬尺子:", r.get("referee_skipped"))
        if r.get("judge") == "method_compare":
            print("  （主尺子 = 两边方法与做法的比对：不要求答案压成单值事实，"
                  "答案是一张表也能判）")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
