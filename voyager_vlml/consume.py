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
import os
import re
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MVE_DIR = _REPO_ROOT / "mve"
for _p in (str(_REPO_ROOT), str(_MVE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _consume_timeout(n_skills: int) -> float:
    """消费侧重放已学技能的墙钟预算（秒）。

    为什么不能沿用 30s 默认值（2026-10-21 Scene G 实测）：每条技能都是一次
    **真实的联网/MCP 取数**（一次 web_search 5~20s），命中的题一次要串行合成
    5 条技能 —— 30s 把整次重放判死：trace 里 5 条技能全进了、一条都没跑回来，
    面板显示「工具 0 次 · 0 行 / 取到真空」，掌握度（只认消费侧成功取数）
    自然一次都不涨。预算 = 30s 基础 + 每条技能 25s，封顶 180s；
    env `MVE_CONSUME_TIMEOUT` 可整体覆盖（下限仍 30s）。
    """
    env = os.environ.get("MVE_CONSUME_TIMEOUT")
    if env:
        try:
            return max(30.0, float(env))
        except ValueError:
            pass
    return min(180.0, 30.0 + 25.0 * max(0, int(n_skills or 0)))

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


def _retrieve_skills(question: str, topic_hint: str = "",
                     scene_id: str = "") -> list[dict[str, Any]]:
    """取已积累的技能（可复用 SQL 配方），top_k 照原仓库 retrieval_top_k=5。"""
    try:
        import skill_store
        all_s = list((skill_store._load_raw().get("skills") or {}).values())
    except Exception as e:  # pragma: no cover
        print(f"  （技能库不可用：{type(e).__name__}: {e}）")
        return []
    if not all_s:
        return []
    topic = str(topic_hint or "").strip() or _pick_ability_topic(question, all_s)
    out = skill_store.retrieve(topic, question, top_k=5,
                               scene_id=scene_id)
    # 归题结果在本场景没有技能 → 再按词面兜底一次（照原版：检索不到就现场写，
    # 但**先试过**再放弃）。两次都空才真空。
    if not out:
        _t2 = _pick_ability_topic(question, all_s)
        if _t2 and _t2 != topic:
            out = skill_store.retrieve(_t2, question, top_k=5, scene_id=scene_id)
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

_BUILTIN_CALLS = {"print", "len", "str", "int", "float", "list", "dict", "tuple",
                  "set", "sorted", "sum", "min", "max", "any", "all", "range",
                  "enumerate", "zip", "round", "abs", "isinstance", "json"}


def _tools_in_code(code: str, scene_id: str = "") -> list[str]:
    """从**跑过的代码**里认出实际调用了哪些抓手（本机原语 / 场景条目 / mcp_*）。

    为什么不能写死 `["local_python"]`（2026-10-20 实测）：复用一条联网配方时，
    它明明调了 `mcp_neko_web_search` 取到数，披露栏却写「调用 0 次」——
    **披露失真比不披露更坏**，人会以为答案是模型编的。这里按注册表里的抓手名
    认，认不出的标识符不算（避免把 `json.dumps` 当工具）。
    """
    if not code:
        return []
    names: set[str] = set()
    try:
        import local_agent
        names |= set(local_agent.primitive_names())
    except Exception:
        pass
    if scene_id:
        try:
            import actions
            names |= {str(n) for n in actions.names(scene_id)}
        except Exception:
            pass
    out: list[str] = []
    for ident in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", code):
        if ident in _BUILTIN_CALLS or ident in out:
            continue
        # 合成出来的调用也算：`skill_def()` 是**已学技能**被调用了（原版
        # `smeltFiveRawIron` 里调 `craftFurnace` 就是这一类）。
        if ident not in names and not ident.startswith(("mcp_", "skill_")):
            continue
        out.append(ident)
    return out


async def consume(
    question: str,
    *,
    topic_hint: str = "",
    auto_task: bool = True,
    env: VLMLMCPEnv | None = None,
) -> dict[str, Any]:
    # ⚠️ 环境**延迟创建**：下面按场景后果判断要不要 VLML。非 VLML 场景
    # （用户 2026-10-19：「别的场景下，voyager 的学习和 VLML 没有关系」）
    # 连 `VLMLMCPEnv()` 都不该构造 —— 它的构造会引导 vlml_env（chdir + 打
    # duckdb 补丁 + 指向 rib），那已经是「碰 VLML」了。

    # 当前场景：人导入的问题归属哪个命名空间（1→n 泛化）。
    # 取不到就按默认 A —— 但**必须取**，否则整条消费线没有场景归属，
    # 切回 A 时无法证明「这条记录属于谁」。
    _scene_id = "A"
    try:
        import os as _os
        # 命令行必须能指定场景（2026-10-20 实测踩坑：`MVE_SCENE=D` 被忽略，
        # 人导入的记录写进了面板**当前**场景 F，而技能属于 D → 场景隔离把
        # 技能全挡掉，明明学过却"用到 0 条技能"，两次导入也永远对不上批次）。
        # 优先级：环境变量（CLI/脚本显式指定）> 面板当前场景 > A。
        _env_sid = str(_os.environ.get("MVE_SCENE") or "").strip()
        import scenes as _sc
        _scene_id = _env_sid or str(_sc.current_scene_id() or "A")
    except Exception:
        pass

    # ---- ① 归题（找评分点）----
    topic_id, hit, task = _infer(question)

    # ---- ② 命中判定（照 neko material_topic_mapper.py:204-215 的双阈值）----
    # 「命中」= 相似度分数阈值，**不是**人审核通过、**也不是**「答对且能对上」。
    #   · max_score >= 40 且 exact_match 且 meaningful_match → 高置信
    #   · max_score >= 10 且重复命中 >= 2                    → 中置信
    #   · 否则                                                → 未命中 → 新建节点
    # 人审是匹配**之后**的下游动作，不参与命中判定；匹配与答案对错无关。
    route_info: dict[str, Any] = {}
    try:
        import routing as _rt
        route_info = _rt.route(question, _scene_id)
    except Exception:
        route_info = {"decision": "REJECTED", "max_score": 0,
                      "matched_on": "路由层不可用"}
    # 未命中 → **先落待审区**（设计案 §3.1，同构 neko candidate_knowledge_items）：
    # 不挂活图谱、不丢弃，冷启动解题成功才升活节点；被拒项留审计、不删。
    # ⚠️ 之前这条没接线 —— 面板场景卡的「待审 N」徽章因此恒为 0，人导入了
    # 域外问题却在任何地方都看不到痕迹（2026-10-18 实测特斯拉问题踩中）。
    # 学习触发结果（未命中才有）：面板导入历史据此显示「已启动后台学习」。
    learn_kick: dict[str, Any] = {"kicked": False, "why": ""}
    miss_info: dict[str, Any] = {}
    try:
        if str(route_info.get("decision") or "") == "REJECTED":
            # 用户闭环前两步（2026-10-19 澄清）：
            #   人导入（未命中）→ **待审区登记 + 建图谱节点** → 启动学习
            # on_miss 一次做完前两步（同一套账，消费侧与学习侧不各写一份）。
            try:
                import learn_orchestrate as _lo
                miss_info = _lo.on_miss(
                    question, _scene_id,
                    reason=(f"双阈值未命中（score={route_info.get('max_score', 0)}，"
                            f"matched_on={route_info.get('matched_on') or '—'}）"))
            except Exception as _me:
                miss_info = {"reason": f"{type(_me).__name__}: {_me}"}
            # 第三步（2026-10-19 用户改设计）：**不再自动后台学**。原来未命中
            # 就 kick_learning 抢跑 —— 学习在人看不见的后台跑完，运行日志
            # （run_log/轨迹页）一条都不落，人还以为没学。现在学习由人自己在
            # 驾驶舱点「▶ 启动 Voyager」触发（非 VLML 场景跑 scene_learn.py，
            # 每学一题落一行 run_log）；这里只做完前两步（待审区 + 图谱建点），
            # 并在导入历史里如实说明"等人工触发"。
            learn_kick = {"kicked": False, "manual": True,
                          "why": "已登记待审区+图谱节点 —— 学习等你在驾驶舱点"
                                 "「▶ 启动 Voyager」触发（会落运行日志）"}
    except Exception:
        pass                                     # 待审区写不进去不能拦消费
    # ---- ②.5 场景后端（数据源）----
    # 非 VLML 场景**不做 VLML 取数**：那条路连的是 rib/GRID，在别的场景里
    # 拿它回答就是上一次实测看到的「问世界模型，答出一串 Valorant 战队」。
    # 这里只做：命中判定 + 建点 + 触发学习（学习按本场景自己的数据源走）。
    _scene_kind, _scene_why, _scene_dir = "", "", ""
    try:
        import scene_profile as _sp
        _prof = _sp.profile(_scene_id)
        _scene_kind = str(_prof.get("kind") or "")
        _scene_why = str(_prof.get("why") or "")
        _scene_dir = str(_prof.get("scene_dir") or "")
    except Exception as e:
        _scene_kind = ""
        _scene_why = f"场景后端判定失败：{type(e).__name__}: {e}"
    _non_vlml_why = ""
    if _scene_kind and _scene_kind != "vlml":
        _non_vlml_why = (
            f"当前场景不是 VLML 数据域（{_scene_why}）：消费侧不做 VLML 取数，"
            f"只做命中判定与触发学习（学习按本场景自己的数据源走）")

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
    # ⚠️ 检索键必须是**本题归题结果**（2026-10-20 实测根因）：原来这里传的是
    # 函数入参 `topic_hint`（默认空），`_pick_ability_topic` 于是按词面**随便
    # 挑最像的** —— 问「正念是什么」时挑中了「正念练习的常见误区」那批技能
    # （consume_2461737d），本题自己那批（consume_ca2125be）一条都没进 prompt。
    # 表现就是：图谱已经高置信命中、技能库里明明有 6 条，消费却报"用到 0 条"。
    # 同原版 `retrieve_skills(query=self.context)` —— context 里就是当前这道题。
    skills = _retrieve_skills(question, topic_id or topic_hint, _scene_id)
    reused_list = [{"topic": s.get("topic"), "name": s.get("name"),
                    "key": s.get("key")} for s in skills]

    # ---- ③ 取数：优先复用配方，没有就现场写 ----
    # 非 VLML 场景：不连 VLML、不读 rib。
    # 「本机」场景（普通用户的默认形态）：用本机只读原语作答；取不到就如实说。
    if _non_vlml_why and _scene_kind in ("local", "scene"):
        import local_agent as _la
        # kind=scene（场景目录里自己的 .duckdb）与 kind=local 共用这条消费链：
        # 同构 Scene A 的「SQL 重放 → 没有才现场写」，数据面换成 SceneDBEnv
        # （只读场景库）。此前没接 scene 形态（2026-10-22 数学场景实测：
        # 路由 HIGH_CONFIDENCE 命中却整段跳过，answer 恒空 —— 泛化最后一公里）。
        _env = None
        if _scene_kind == "scene":
            from scene_db import SceneDBEnv
            _env = SceneDBEnv(str((_prof or {}).get("db_path") or ""))
        else:
            # 本机原语注册成条目（origin=local / group=本机能力）——同构猫娘：
            # 插件（能力来源）+ 猫爪总开关（这一组的开关）+ 开关下面的条目。
            # 注册了才在执行层的抓手清单里出现；开关关掉就注入不到命名空间里。
            try:
                import actions as _act
                _act.register_local(_scene_id, _scene_dir)
                _act.register_net(_scene_id)
            except Exception:
                pass
            # 远端 MCP 工具必须先挂上：复用配方时 `_ns_base` 只注入**注册表里已有**
            # 的 mcp 条目，而它们在 learn 那边是 connect_all + expose 才注册的。
            # consume 是新进程 —— 不接这一步，重放 `mcp_xxx` 就是 NameError
            # （2026-10-20 实测：命中 3 条配方，3 条全 NameError → "用到 0 条技能"）。
            mcp_ready = await _la.ensure_mcp_tools(_scene_id)
        if _env is not None:
            mcp_ready = {"skipped": "场景库形态（query_sql 只读本场景 duckdb），消费不接 MCP"}
        # ③a 复用优先（同构 Voyager：retrieve → 重放 → 取不到才写新代码）。
        # 只认「本机形态」的配方（代码里有 RESULT、能 run_saved 重放）；
        # VLML 的 SQL 配方在本机场景重放不了 —— 那是另一个数据源的东西，
        # 跨后端复用就等于场景隔离破功。
        data = None
        code = ""
        obs: list[dict[str, Any]] = []
        reused_skill: dict[str, Any] | None = None
        reuse_errors: list[str] = []
        for s in skills:
            sc = str(s.get("code") or "")
            if _scene_kind == "scene":
                # ---- 场景库形态的配方重放（SQL 配方，SceneDBEnv 只读执行）----
                if not sc.strip().lower().startswith(("select", "with")):
                    continue
                import re as _re_lit
                _lits = set(_re_lit.findall(r"\d+(?:\.\d+)?", sc))
                _qnums = set(_re_lit.findall(r"\d+(?:\.\d+)?", question))
                if _lits and len(_lits - _qnums) >= 2:
                    # 计算式配方里的数字是**上一次那道题**的：对新数字重放
                    # 必然算出旧题答案 —— 跳过，走 ③b 让模型按方法写新 SQL。
                    reuse_errors.append(
                        f"配方 {s.get('name')} 是带旧题数字的计算式"
                        f"（{sorted(_lits)[:6]}），对本题重放必错 —— 改现场写")
                    continue
                _r = await _env.run_sql(sc)
                if not _r.get("ok"):
                    reuse_errors.append(
                        f"重放 {s.get('name')} 执行失败："
                        f"{str(_r.get('error') or '')[:120]}")
                    continue
                _d = _r.get("data") or {}
                if not _d.get("rows"):
                    reuse_errors.append(f"重放 {s.get('name')} 取到 0 行")
                    continue
                data, code, reused_skill = _d, sc, dict(s)
                obs = [{"ok": True, "tool": "query_sql", "rows": _r.get("rows"),
                        "data": _d, "data_sources": ["query_sql"]}]
                break
            if "RESULT" not in sc:
                continue
            try:
                # 必须走 async 版：同步 run_saved 会在事件循环线程里阻塞等
                # 自己提交给同一 loop 的 MCP 调用 → 60s 超时 → 配方被误判失效
                # （2026-10-20 实测：明明命中 3 条配方，却"用到 0 条技能"）。
                # 墙钟同样要给足：单条技能 = 一次真实联网取数（2026-10-21）。
                val = await _la.run_saved_async(
                    sc, _scene_dir, scene_id=_scene_id,
                    timeout=_consume_timeout(1))
            except Exception as e:
                reuse_errors.append(f"重放 {s.get('name')} 抛错：{e}")
                continue
            # ⚠️ 重放结果必须过真空判据：`{"error": ...}` / 空值**不算答出来**。
            # 实测（2026-10-19 截图「命中 1 · 有效率 0%」）：命中后复用一条
            # 历史错误配方，它把 RESULT=error 当答案交了上去 —— 命中了、
            # 掌握度也有了、就是没回答问题。所以这里与 critic 同一口径：
            # error/空 → 这条配方当失败，换下一条；全失败就现场重写。
            data_try, err_try = _la._payload(val)
            if data_try is None:
                reuse_errors.append(f"重放 {s.get('name')} 没取到数：{err_try}")
                continue
            data, code, reused_skill = data_try, sc, dict(s)
            obs = [{"ok": True, "tool": "local_python", "rows": 1,
                    "data": data_try}]
            break
        # ③b 没有可复用的配方 → 现场写
        if reused_skill is None:
            # 已学技能**必须进执行层让模型调用合成**（原版 action.py:94
            # `+ skills`）：命中（HIGH_CONFIDENCE）的题尤其如此 —— 图谱里
            # 已经学过它，却还去"现场现做"，等于技能库白建（用户 2026-10-20
            # 「所有命中的题目都要用到已经学到的技能」）。
            if _scene_kind == "scene":
                # 场景库形态的现场写：同一个 VLMLActionAgent（写 SQL prompt +
                # 迭代修正），数据面只连场景自己的库（与学习侧 SceneDBEnv
                # 同一环境，learn_orchestrate.py:583 同款）。
                from voyager_vlml.agents.action import VLMLActionAgent
                _agent = VLMLActionAgent(env=_env)
                obs, code = await _agent.solve(
                    question, skills=skills,
                    hints=[str(s.get("need") or "") for s in
                           ((route_info or {}).get("subtasks") or [])] or None)
            else:
                obs, code = await _la.solve(
                    question, _scene_dir, scene_id=_scene_id, skills=skills,
                    # 命中（HIGH/MEDIUM_CONFIDENCE）且库里有已学技能 → **必须用**。
                    # 不强制的话模型一律走最省事的路线：凭记忆直接写答案
                    # （2026-10-20 实测：给了可调用技能，它一条都没调）。
                    require_skills=(str(route_info.get("decision") or "")
                                    in ("HIGH_CONFIDENCE", "MEDIUM_CONFIDENCE"))
                    and bool(skills),
                    # 墙钟预算要跟着技能条数走（2026-10-21 Scene G 实测）：每条
                    # 技能都是一次真实联网/MCP 取数（一次 web_search 5~20s），
                    # 命中的题一次合成 5 条 → 30s 默认值把整次重放判死
                    # （trace 里 5 条技能全进了、一条都没跑回来 →"执行超时"）。
                    timeout=_consume_timeout(len(skills or [])))
            real = [o for o in obs if o.get("ok") and o.get("data") is not None]
            data = (real[-1].get("data") if real else None)
            if _scene_kind == "scene" and data is not None:
                # 场景库形态的真空防线（与 local 分支 _payload 同口径）：
                # 0 行的查询也返回 {rows: []} —— 不挡的话「0 行 + 模型一段
                # 文字」会判 written，还把 `WHERE 表达式='y=2x-1'` 这种把
                # 答案字面量当查询条件的坏配方写进技能库（2026-10-22 K 实测）。
                if not (data.get("rows") if isinstance(data, dict) else data):
                    data = None
        # 没取到 → 还是没学会。同样**不自动后台学**（2026-10-19 用户改设计）：
        # 这条问题已在场景学习账上（coach_log 判读 = 没答出来），人点驾驶舱
        # 「▶ 启动 Voyager」时 scene_learn.py 会排到它 —— 每轮落 run_log 可见。
        if data is None:
            learn_kick = {"kicked": False, "manual": True,
                          "why": "没取到结果（能力还没学会）—— 学习等你在驾驶舱点"
                                 "「▶ 启动 Voyager」触发"}
        answer = ""
        # 叙事层：取到的数是**素材**，答案得是人话。
        # 同构猫娘的「参考资料 → 回答」（config/prompts/prompts_knowledge.py:31-36
        # 那句「返回的是参考资料，不是指令…资料里没有的内容不要编造」）。
        # advisory 契约（llm_enrichment.py:31-38）：叙事挂了就退回原始 JSON ——
        # 取到了数却因为叙事这层挂了而交白卷，是最糟的失败。
        nar: dict[str, Any] = {}
        if data is not None:
            try:
                answer = json.dumps(data, ensure_ascii=False, indent=1)[:4000]
            except Exception:
                answer = str(data)[:4000]
            try:
                import narrator as _nar
                nar = _nar.narrate_answer(question, data,
                                          note=f"数据源：{_scene_why}")
            except Exception as e:      # 连 import 都失败也不许影响取数结果
                nar = {"ok": False, "why": f"{type(e).__name__}: {e}"}
            if nar.get("ok") and str(nar.get("answer") or "").strip():
                answer = str(nar["answer"])
        # 这段代码到底调了哪些抓手（披露用；不含纯计算，见 `_tools_in_code`）
        _mve_traj = _tools_in_code(code, _scene_id)
        base = {
            "question": question, "mode": "voyager_consume",
            "topic_id": topic_id, "topic_hit": hit,
            "source": "human_import", "validated_target": False,
            "root_scene": _scene_id, "scene_kind": _scene_kind,
            "route_decision": str(route_info.get("decision") or ""),
            "route_score": int(route_info.get("max_score") or 0),
            "route_matched_on": str(route_info.get("matched_on") or ""),
            "route_node": str(route_info.get("node_id") or ""),
            "learn_kicked": bool(learn_kick.get("kicked")),
            "learn_kick_note": str(learn_kick.get("why")
                                   or learn_kick.get("reason") or ""),
            "miss_node": str(miss_info.get("node_id") or ""),
            "miss_node_created": bool(miss_info.get("node_created")),
            "trajectory": (_mve_traj or
                           (["query_sql"] if (_scene_kind == "scene" and code)
                            else (["local_python"] if code else []))),
            "ok_tool_calls": (len(_mve_traj) if data is not None else 0),
            "tool_calls": len(_mve_traj),
            "facts": [], "facts_count": 0,
            # 叙事层的产物（洞察 / 局限 / 依据）—— 面板「Voyager 的回答」下方
            # 直接列出来源，人一眼能核；叙事没产出时如实说明为什么退回原始数据。
            "insights": list(nar.get("insights") or []),
            "caveats": list(nar.get("caveats") or []),
            "sources": list(nar.get("sources") or []),
            "narrative_ok": bool(nar.get("ok")),
            "narrative_cited": nar.get("cited"),
            "narrative_skipped": ("" if nar.get("ok")
                                  else str(nar.get("why") or "")[:200]),
            "errors": [str(o.get("error") or "") for o in obs if not o.get("ok")],
            "answer": answer, "sql": code,
            "rows": sum(int(o.get("rows") or 0) for o in obs if o.get("ok")),
            "used_skills": ([{"topic": reused_skill.get("topic"),
                              "name": reused_skill.get("name"),
                              "key": reused_skill.get("key")}]
                            if reused_skill else []),
            "reused": bool(reused_skill), "graph_link": {},
            # 复用阶段被真空判据挡下的每一条，都留下证据（不静默跳过）
            "reuse_errors": reuse_errors,
            # 远端工具接入情况（exposed = 这次能用上的 mcp_*；没连上如实带原因）
            "mcp_tools": mcp_ready,
            "judgment_result": "written" if data is not None else "fetched_empty",
            "skipped": ("" if data is not None else
                        (("场景数据源里没有能回答它的数据（" + _scene_why + "）"
                          "—— 往场景目录里放数据 / 调整库内容后重试")
                         if _scene_kind == "scene" else
                         "本机工作区里没有能回答它的数据（" + _scene_why + "）"
                         "——「本机」= 应用跑在用户自己电脑上的那一档：只读应用"
                         "工作区/场景目录，不联网、也不翻操作者的私人文件；"
                         "这类问题需要外部数据源")),
        }
        # ⑤ 记进能力库的闸门：**只有真取到数才记账**。
        # 没取到数（data is None）时绝不能把这段代码写进技能库 —— 否则下一问
        # 命中它、重放出 RESULT=error，闭环就永远停在「命中但不回答」。
        # 这是原版 Voyager 的铁律（learn_orchestrate / 执行层 critic 同一判据）。
        #
        # 复用已有配方也要记 —— 但记的不是「再存一条技能」（`_absorb` 自己
        # 判重：已有配方就 mark_result 用过且有效），而是**这题到能力里了**
        # （用户 2026-10-13 的定义：问一句、答完了，这题就进能力库）。
        # 实测（2026-10-19）：只在「现场新写」时调 `_absorb`，命中复用那条
        # 路径的 mastered 永远是 False —— 面板上「掌握度」怎么答都不涨。
        abs_info: dict[str, Any] = {}
        if data is not None:
            try:
                # 只把**本次真正复用的那一条**记成"用过且有效"（used_keys）。
                # 不传的话 _absorb 会按 topic 前缀扫出同题下全部技能集体 +1，
                # 于是"有效率"（ok/hits）冲到 150%/300%（2026-10-20 截图）。
                _used = ([str(reused_skill.get("key"))]
                         if reused_skill and reused_skill.get("key") else None)
                abs_info = _absorb(question, code, "", scene_id=_scene_id,
                                   used_keys=_used)
            except Exception:
                abs_info = {}
        base.update({
            # topic_id 用 `_absorb` **解析后**的那个（归不上已有题时它给了一个
            # 稳定的 consume_<md5>）：面板的进步曲线就是按这一列累计
            # 「答出来了 +1」，写空串的话 mastered 永远是 True 但曲线不涨。
            "topic_id": str(abs_info.get("topic_id") or base.get("topic_id") or ""),
            "mastered": bool(abs_info.get("mastered")),
            "skill_action": str(abs_info.get("skill_action") or ""),
            "node_status": str(abs_info.get("node_status") or ""),
            "cold_start": bool(abs_info.get("cold_start")),
            "mastered_total": int(abs_info.get("mastered_total") or 0),
            "ability_total": abs_info.get("ability_total"),
            # 复用分支原本没有 mastery 键 → CLI/面板按 `mastery or 0` 读成 0%，
            # 明明「累计 2/39」却显示掌握度 0%（2026-10-20 实测）。与主路径
            # 同一口径：已掌握 / 该掌握的总量（不是覆盖率的折算）。
            "mastery": (round(int(abs_info.get("mastered_total") or 0)
                              / int(abs_info.get("ability_total") or 0), 4)
                        if int(abs_info.get("ability_total") or 0) else None),
        })
        # ---- 「这一分是哪次导入给的」要能被解释（用户 2026-10-20）----
        # 用户要的是：第二次导入同一问题时，掌握度上升不仅发生，还能说清
        # 涨的那一分来自**上一次导入**长出来的技能/节点。
        #   import_count  = 这个问题一共导入过几次（1 = 这是第一次）
        #   reused_import = 这次真正复用的技能属于哪一批导入
        #   reuse_explain = 一句人话，面板/CLI 直接显示
        try:
            import coach as _coach_e
            _h = _coach_e.import_history(question, _scene_id)
            _cnt = int(_h.get("count") or 0)
            _bp = (reused_skill or {}).get("blueprint") or {}
            _from_imp = str(_bp.get("import_id") or "")
            base["import_count"] = _cnt
            base["reused_import"] = _from_imp
            if reused_skill and _cnt >= 1:
                _where = (f"第 {_cnt} 次导入复用了 {_from_imp or '之前导入'} "
                          f"学到的配方 `{reused_skill.get('key')}`"
                          if _cnt > 1 else
                          f"第 1 次导入，命中已有配方 `{reused_skill.get('key')}`"
                          f"（来自 {_from_imp or '本场景既有技能'}）")
                base["reuse_explain"] = (
                    _where + f"；掌握累计 {base.get('mastered_total')}/"
                    f"{base.get('ability_total')}")
            elif _cnt > 1:
                base["reuse_explain"] = (
                    f"第 {_cnt} 次导入同一问题（之前导入过 {_cnt - 1} 次）")
        except Exception:
            pass
        _log(base)
        return base

    # 其它非 VLML 后端（配了自己的库，但消费侧还没接）：整段跳过，如实记账。
    if _non_vlml_why:
        base = {
            "question": question, "mode": "voyager_consume",
            "topic_id": topic_id, "topic_hit": hit,
            "source": "human_import", "validated_target": False,
            "root_scene": _scene_id, "scene_kind": _scene_kind,
            "route_decision": str(route_info.get("decision") or ""),
            "route_score": int(route_info.get("max_score") or 0),
            "route_matched_on": str(route_info.get("matched_on") or ""),
            "route_node": str(route_info.get("node_id") or ""),
            "learn_kicked": bool(learn_kick.get("kicked")),
            "learn_kick_note": str(learn_kick.get("why")
                                   or learn_kick.get("reason") or ""),
            "miss_node": str(miss_info.get("node_id") or ""),
            "miss_node_created": bool(miss_info.get("node_created")),
            "trajectory": [], "facts": [], "facts_count": 0,
            "insights": [], "caveats": [], "errors": [],
            "answer": "", "sql": "", "rows": 0,
            "used_skills": [], "reused": False,
            "graph_link": {}, "judgment_result": "failed",
            "skipped": _non_vlml_why,
        }
        _log(base)
        return base

    env = env or VLMLMCPEnv()
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
        # 抓手条目同构 MCP tools/list 一并交给模型（含 forge 出来的锻造条目，
        # 其描述里带着实查到的真实列名）。不给它，模型就凭 schema 瞎猜 join 键：
        # 2026-10-19 服务器实测写成 `rounds.round_id = ext_round_economy.game_id`
        # （那张表没有 round_id 列）→ Binder Error → 3 轮不收敛 → 报"没取到数"。
        grabs: list[dict[str, Any]] = []
        try:
            import actions as _act2
            grabs = [{"name": e.get("name", ""),
                      "description": str(e.get("description") or "")}
                     for e in _act2.entries(_scene_id)]
        except Exception:
            grabs = []
        observations, sql = await act.solve(question, max_iters=3, grabs=grabs)

    ok_calls = [o for o in observations if o.get("ok")]
    # 真的取到了数（ok 且非真空）—— 判分 / 回答 / 沉淀都只认这个，
    # 披露字段（trajectory / tool_calls）仍用 ok_calls，让人看见实际发生了什么。
    real_calls = [o for o in ok_calls if has_real_data(o)]
    rows = sum(int(o.get("rows") or 0) for o in ok_calls)
    real_rows = sum(int(o.get("rows") or 0) for o in real_calls)
    trajectory = [str(o.get("tool") or "") for o in ok_calls]

    # 命中但**没取到数** = 还是没学会。同样**不自动后台学**（2026-10-19 用户改
    # 设计）：「命中但没取到真数」要学没错，但学习由人在驾驶舱点「▶ 启动
    # Voyager」触发（VLML 场景跑主循环、其他场景跑 scene_learn.py），每轮落
    # run_log —— 自动抢跑的轮次人看不见、也不落账，才有截图里「轨迹 0 轮」。
    # 原版铁律不变：任务做成（取到值）才固化；没做成就留在学习账上继续学。
    if not real_calls:
        learn_kick = {"kicked": False, "manual": True,
                      "why": "命中但没取到真数（能力还没学会）—— 学习等你在"
                             "驾驶舱点「▶ 启动 Voyager」触发"}

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
        # —— 1→n 泛化：场景归属 + 冷启动标记（补 ⑤ 缺口，使记录可按场景隔离）——
        "root_scene": str(absorbed.get("root_scene") or _scene_id),
        "cold_start": bool(absorbed.get("cold_start")),
        "node_status": str(absorbed.get("node_status") or ""),
        # —— 路由命中判定（双阈值，非人审）——
        "route_decision": str(route_info.get("decision") or ""),
        "route_score": int(route_info.get("max_score") or 0),
        "route_matched_on": str(route_info.get("matched_on") or ""),
        "route_node": str(route_info.get("node_id") or ""),
        # 是否已启动后台学习（learn_orchestrate.kick_learning）—— 面板导入历史
        # 据此显示「已启动后台学习」。两条触发路径都要落：未命中（陌生问题）
        # 和命中但没取到真数（认识这句话、还不会回答它）。
        "learn_kicked": bool(learn_kick.get("kicked")),
        "learn_kick_note": str(learn_kick.get("why")
                               or learn_kick.get("reason") or ""),
        # 未命中时建的图谱节点（闭环第 2 步的可见证据）：node_id + 是否新建
        "miss_node": str(miss_info.get("node_id") or ""),
        "miss_node_created": bool(miss_info.get("node_created")),
        # 判定结果枚举（设计《判定结果状态与写入证明规格》）：写入图谱的充分条件。
        #   written              —— 成功写入图谱（节点已存在/已验证，已提交终态）
        #   insufficient_evidence —— 冷启动单次证据（节点 cold_start_guess，待巩固）
        #   fetched_empty        —— 取到真空结果，未写图（= neko no_data）
        #   failed               —— 答完但写图/标掌握失败
        "judgment_result": (
            "fetched_empty" if not real_calls else
            ("insufficient_evidence"
             if (absorbed.get("mastered")
                 and absorbed.get("node_status") == "cold_start_guess")
             else "written" if absorbed.get("mastered")
             else "failed")),
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


def _runtime_topic_id(question: str, scene_id: str = "") -> str:
    """答出来了、但归不上任何已有题 → 建一个**稳定**的能力 id。

    为什么必须建（用户的定义）：「我问你一句，Voyager 答完了，之后这题就到他的
    能力里了；这就是掌握！」—— **归不上题 ≠ 没答出来**。
    不建的话，掌握就变成了"只有归得上旧题库的才算"，那是把两件事
    （答没答出来 vs 归不归得上）混成了一个条件。
    实测（2026-10-13）：问「每个回合中各个角色的平均击杀数」，Voyager 取到 484 行、
    回答也完整产出了，面板却显示「没答出来，未进能力库」—— 就是被这个条件卡住的。

    稳定 = 同一句话永远得到同一个 id（问题文本的哈希），所以问第二遍不会
    再建一个新节点把分母灌水（掌握度分母 = 图谱节点 + 这里登记的能力项）。

    `scene_id`：1→n 泛化（补 ⑤ 缺口）—— 这条消费记录打上所属场景命名空间标签，
    使「切回 A 时 A 的消费记录数不变」可被按场景隔离统计（不再混在全局池里）。
    """
    import hashlib
    h = hashlib.md5(str(question or "").strip().encode("utf-8")).hexdigest()[:8]
    tid = f"consume_{h}"
    reg = _runtime_topics()
    if tid not in reg:
        reg[tid] = {"id": tid, "name": str(question or "")[:60],
                    "question": str(question or ""), "source": "consume",
                    "root_scene": str(scene_id or "")}
        try:
            _RUNTIME_TOPICS_FILE.write_text(
                json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:  # pragma: no cover
            print(f"  （登记新能力项失败：{type(e).__name__}: {e}）")
    else:
        # 已存在则补打场景标签（旧记录升级，不破坏既有）
        if scene_id and not reg[tid].get("root_scene"):
            reg[tid]["root_scene"] = str(scene_id)
            try:
                _RUNTIME_TOPICS_FILE.write_text(
                    json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:  # pragma: no cover
                pass
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


# 配方 = **真的去取数**的代码（原版 skill 就是一次成功 rollout 的 program_code，
# 里面调的是 control primitive）。认这几种取数动作：本机只读原语 / 联网原语 /
# MCP 工具 / SQL 查询。纯赋值（RESULT = 一段文字）不算 —— 那是模型记忆，
# 不是可复用方法。
_RECIPE_CALL = re.compile(
    r"\b(list_files|read_file|read_json|read_csv|query_sql"
    r"|web_search|web_fetch|http_get|fetch_url)\s*\("
    r"|\bmcp_[A-Za-z0-9_]+\s*\("
    # SQL 配方：SELECT…FROM 之外，**纯计算式 SELECT**（`SELECT (6-2)/(3-1) AS k
    # … LIMIT 1`，无 FROM —— 2026-10-22 数学场景实测被这条漏判成"知识型答案"
    # 而拒绝入库，技能库 0 涨，闭环断在第一环）也是真取数：
    # SceneDBEnv.run_sql 的合法形态。
    r"|\bSELECT\b[\s\S]{0,400}?(?:\bFROM\b|\bLIMIT\b)"
    r"|\bduckdb\b",
    re.I,
)


def _looks_like_recipe(code: str) -> bool:
    """这段代码是不是「可执行取数配方」，而不是模型写的一段答案。"""
    return bool(_RECIPE_CALL.search(str(code or "")))


def _absorb(question: str, sql: str, topic_id: str,
            *, scene_id: str = "", answer: str = "",
            used_keys: list[str] | None = None) -> dict[str, Any]:
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
                           "mastered_total": 0, "ability_total": 0, "topic_id": "",
                           "cold_start": False, "root_scene": "", "node_status": ""}
    # 知识型任务（怎么养育/如何理解…）没有取数 SQL，但有答案 —— 照样算"答出来了"，
    # 建节点 + 标掌握；只是不写技能库（技能存的是**可执行配方**，答案本身不是配方）。
    if not sql and not str(answer or "").strip():
        return out
    # 1→n 泛化：当前场景（冷启动节点与技能都归属它）。
    # ⚠️ 必须先解析再使用：`_scene_id` 在下面有赋值，Python 把它当本函数局部
    # 变量 —— 若先读后写（旧代码顺序），topic_id 为空（归不上题，恰好是
    # 学习闭环 learn_orchestrate 每次的走法）时读局部变量会直接
    # UnboundLocalError。2026-10-19 修：消费侧一直没炸只因历史上的成功消费
    # 都归上了有 rubric 的题（or 短路，根本读不到这行）。
    # `scene_id` 显式传入时以它为准：后台学习跑在**独立线程**里，这期间人可能
    # 已经切了场景 —— 跟着「当前场景」走会把 A 学到的东西记到 B 的头上
    # （用户 2026-10-19：「学习时场景隔离，只学习当前场景」）。
    _scene_id = str(scene_id or "").strip()
    if not _scene_id:
        try:
            from scenes import current_scene_id as _cur
            _scene_id = _cur()
        except Exception:
            _scene_id = "A"
    tid = topic_id or _runtime_topic_id(question, _scene_id)
    out["topic_id"] = tid
    out["root_scene"] = _scene_id
    # 冷启动登记：在图谱上建/升级一枚带场景归属的节点，并标记 cold_start
    try:
        import cold_start
        _info = cold_start.ensure_cold_start_node(tid, question, _scene_id)
        out["cold_start"] = bool(_info.get("cold_start"))
        out["node_status"] = str(_info.get("status") or "")
    except Exception as e:  # pragma: no cover
        print(f"  （冷启动图谱登记失败：{type(e).__name__}: {e}）")
    try:
        import topic_graph
        # 仍然标 completed —— 但那是它**原本**的意思：「这节点规划过了，别再规划」
        # （人已经问过并且答出来的题，课程不必再练一遍）。带 source="consume"
        # 标明这次是消费侧标的，将来能审计；语义拆法见 mark_completed 的 docstring。
        topic_graph.MasteryStore().mark_completed(
            tid, source="consume", root_scene=_scene_id)
        out["mastered"] = True
        # 刷新当前场景的 updated_at 指纹（消费是一次对该场景的活动，B 的活动只写 B，
        # 证明切回 A 时 A 的活动时间戳没被覆盖）。
        try:
            import scenes as _sc
            _sc.touch_scene(_scene_id)
        except Exception:  # pragma: no cover
            pass
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

    if not str(sql or "").strip():
        out["skill_action"] = "知识型答案（无可执行配方，不写技能库）"
        return out
    if not _looks_like_recipe(sql):
        # 原版技能 = 一次**成功 rollout 的 program_code**，那是真的调了
        # control primitive 的代码（voyager.py:250-283 成功才 add_new_skill）。
        # 纯 `RESULT = {"answer": "…"}` 是模型凭训练记忆写的答案，不是配方：
        # 存进去下次就会被当"可复用配方"重放，等于把记忆固化成能力。
        # 2026-10-19 实测：Scene D 问「什么是阿斯伯格」，消费层写的正是这种代码，
        # 被存成 consume_e81eea3e 技能 —— 假配方就这么进了库。
        out["skill_action"] = ("知识型答案（代码里没有任何取数动作，不是配方，"
                               "不写技能库）")
        return out
    try:
        import skill_store
        # 记账对象只能是**这次真的用上的那条**（used_keys）。旧实现按
        # `f"{tid}::"` 前缀把同题下所有技能全捞出来集体 +1 —— 而 hits 只涨
        # 被检索注入的那条，分子分母不同源 → 面板「有效率 150%/300%」
        # （2026-10-20 截图实测；同一题下堆了 22 条技能时尤其明显）。
        have = ([k for k in (used_keys or [])
                 if k in (skill_store._load_raw().get("skills") or {})]
                if used_keys else
                [k for k in (skill_store._load_raw().get("skills") or {})
                 if str(k).startswith(f"{tid}::")])
        if have:
            skill_store.mark_result(have, True)
            out["skill_action"] = ("已有配方，标记为用过且有效"
                                   + ("" if used_keys else "（按同题全量记账）"))
        else:
            _, key = skill_store.add(
                tid, tid,
                f"消费时答出来的取数方法：{question[:60]}",
                code=sql,
                blueprint={"source": "consume", "question": question},
                source="consume",
                source_scene=_scene_id,
            )
            out["skill_action"] = f"新配方入库：{key}" if key else "（内容太短未入库）"
            # 确保技能归属当前场景（兜底：即便上面的 source_scene 没传也补上）
            try:
                import cold_start
                cold_start.mark_skill_scene(tid, _scene_id)
            except Exception:
                pass
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
    # 与 scene_learn.py --scene 对齐：命令行不指定就跟着面板当前场景走
    ap.add_argument("--scene", default="", help="场景 id（缺省用面板当前场景）")
    a = ap.parse_args()
    if a.scene:
        import os
        os.environ["MVE_SCENE"] = str(a.scene)
    q = " ".join(a.question).strip() or "Cloud9 在哪张图上崩得最厉害？"
    r = asyncio.run(consume(q))
    print("=== 消费结果 ===")
    print("问     :", r.get("question"))
    print("归题   :", r.get("topic_id") or "（未归上）", "|", r.get("topic_hit"))
    # 复用分支的返回字典没有 skills_found / tool_calls 这两个键（只有
    # used_skills / ok_tool_calls）→ 直接 .get 会打出 "None 条 / 调用 None 次"。
    # 这里统一按「用到了几条技能 / 真正成功的调用次数」容错取。
    print("用能力 :", "复用配方" if r.get("reused") else "现场现做",
          f"（用到 {len(r.get('used_skills') or [])} 条技能）", r.get("reuse_note") or "")
    if r.get("reuse_explain"):
        # 掌握度涨的那一分是哪次导入给的 —— 不解释就等于让人"信我"
        print("解释   :", r.get("reuse_explain"))
    if r.get("reuse_errors"):
        # 配方为什么没复用上，必须看得见（否则又是"命中却用不上"的黑箱）
        print("复用失败:", "；".join(str(x)[:70] for x in r["reuse_errors"][:3]))
    _mcp = r.get("mcp_tools") or {}
    if _mcp:
        _exp = _mcp.get("exposed") or []
        print("远端工具:", (f"{len(_exp)} 个（{'、'.join(str(x) for x in _exp[:3])}…）"
                          if _exp else
                          "0 个 —— " + str(_mcp.get("error")
                                          or _mcp.get("reason")
                                          or _mcp.get("why") or "没配 MCP server")))
    print("工具   :", r.get("trajectory") or "—",
          f"| 调用 {r.get('ok_tool_calls') or r.get('tool_calls') or 0} 次，"
          f"取到 {r.get('rows') or 0} 行")
    print("SQL    :", str(r.get("sql") or "").replace("\n", " ")[:160])
    print("事实   :", len(r.get("facts") or []), "条")
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
