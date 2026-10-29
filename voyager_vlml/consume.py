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

def _critic_env_context(db_path: str) -> str:
    """给 critic 的**环境观察**（原版同构：critic 看的是同一份 env 末态）。

    原版 critic 能验证「挖到铁矿」，是因为背包清单里看得见 iron_ore×1。SQL
    场景的同构物 = 库的实查清单（表/行数/列/真实取值，见 curriculum_auto
    .db_observation，实查不手写）。2026-10-24 实测两类错误只有靠它才拦得住：
      ① 编造参数：库里没有加班费率，模型编「每小时 150 元」；
      ② JOIN 缩小人群：「9 名在职员工的总工资」被 INNER JOIN 一张只有
        1 行记录的津贴表算成 1 个人的（22600），critic 没有群体行数可对照
        就放行了。
    """
    ctx = ""
    try:
        if str(db_path or "").strip():
            from curriculum_auto import db_observation as _dbobs
            obs = _dbobs(db_path)
            if obs:
                ctx = ("[环境实查（数据源真实的表/行数/列/取值）]：\n" + obs
                       + "\n\n判定要求：①任务假设的任何表、列、业务参数（费率/"
                         "汇率/税率/政策数值…）必须能在这份清单里指认，指认不了 "
                         "= 任务在数据源上不可答，判未完成并明说缺什么；"
                         "特别地，两个数值列相乘/相除合成数据源里不存在的新口径，"
                         "同样是假设业务参数 —— 合成出的口径在这份清单里指认不了"
                         "就是不可计算，判未完成；"
                         "②对「总/全部/所有/每个」类题，核对结果覆盖的行数/人数"
                         "是否与清单里该群体的行数一致 —— INNER JOIN 一张稀疏表"
                         "会把人群缩小（实测：大群体的总账被算成只有少数有 "
                         "记录者的），覆盖不符 = 判未完成。")
    except Exception:
        ctx = ""
    return ctx


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
   ⚠️ 实测踩过：问句要的是一个量，库里命中的配方算的是另一个量 ——
   模型把问句里的实体名填进一条
   量纲不同的配方，还自称"复用了配方" —— 结果取到的是配方那个量，被下游
   当成要问的量讲了出来。**数字错还能看出来，量错了是方向错，比报错危险得多。**
   换同类实体值（换一支队伍/一个部门）算复用；换个完全不同的量算不上复用。

1. 复用的是配方的**计算方法**（用了哪些表、怎么聚合、怎么筛选），
   **不是**它上次算出来的数值 —— 数值必须重新算。
2. 把**当前这句话里的实体值**（本题涉及的队伍/部门/月份/图名……）填进配方对应的位置。
   配方里写死的字面量要换成当前这句话指的那个值。
   ⚠️ 原版同构铁律（2026-10-24 实测 10:56 事故后立）：原版 action agent 写代码
   前，环境观察（nearbyBlocks）里只有**真实存在的方块名** —— 它写不出观察里
   没有的实体。这里同理：SQL 里的筛选字面量（部门/月份/队伍/图名…）必须
   **逐字**取自下面环境观察的「真实取值」清单；问句里的口语说法必须映射成
   清单里逐字存在的值（如口语月份 → 清单里的日期写法、口语部门名 → 清单里
   的部门代码）。
   清单里没有的写法一律不许出现在 SQL 里 —— 实测把口语写法（英文月份、
   带俗称的部门名）填进槽位，LEFT JOIN 匹配不上，SUM 出 1 行 NULL，
   技能"命中却答不出"。
   ⚠️ 跨「一对多」JOIN 做聚合是头号陷阱：父表一行会被子表的每一行乘一次
   （父表一行匹配子表 2 行 → 父表数值列被算 2 次），SUM 出来翻倍。正确写法二选一：
     · 先把右表按关联键聚合成单行再 JOIN：
       `LEFT JOIN (SELECT 关联键, SUM(子表数值列) AS v FROM 子表
        WHERE … GROUP BY 关联键) t ON t.关联键 = 父表.关联键`，
       然后 `SUM(父表.数值列 + COALESCE(t.v, 0))`；
     · 或把子表聚合塞进标量子查询：
       `SUM(父表.数值列 + (SELECT COALESCE(SUM(子表.数值列),0) FROM 子表
        WHERE 子表.关联键 = 父表.关联键 …))`。
   绝对不要写 `SUM(父表.数值列 + 子表.数值列) FROM ... JOIN 子表` 这种
   把父表数值列直接带进"带子表 JOIN"的 SUM —— 必乘爆。
3. 当前这句话没给实体（比如问"哪支队伍最高"）→ 保留配方的分组结构，别自己造一个值。
4. 配方可能是**槽位模板**（入库时泛化过的）：`:dept`、`:month` 这种占位符，
   配方头部注释逐个说明了每个槽位该填什么 —— 把每个槽位换成当前这句话指的值，
   注释行可以删掉。
5. 只输出一条 SELECT。不要 INSERT/UPDATE/DROP，不要多语句。

只返回一个 JSON：{"sql": "你的 SELECT", "reused": true/false,
"note": "一句话说明你复用了配方的哪部分、把什么值填了进去（没复用就说明为什么用不上）"}"""


def _refill_sql(question: str, skills: list[dict[str, Any]], schema: str,
                db_path: str = "") -> dict[str, Any]:
    """让模型把配方改造成当前这句话的取数 SQL（回填 = 改造计算，不是抄数值）。

    db_path 非空时把 db_observation（表/行数/列/**真实取值**）一并给模型 ——
    原版同构：action agent 写代码前的环境观察就是实体值的地面真话，实体值
    不许凭问句原文脑补（2026-10-24 实测 'September'/'技术部' 幻觉回填）。
    """
    recipes = "\n\n".join(
        f"【配方 {i+1}｜topic={s.get('topic')}｜{s.get('name') or ''}】\n"
        f"说明：{str(s.get('text') or '')[:200]}\n"
        f"SQL：\n{str(s.get('code') or '')[:900]}"
        for i, s in enumerate(skills[:5])
    )
    # 环境观察（原版「写代码前先看环境」的同构物）：筛选字面量的地面真话。
    ground = ""
    if str(db_path or "").strip():
        try:
            from curriculum_auto import db_observation as _dbobs
            ground = str(_dbobs(db_path) or "")
        except Exception:
            ground = ""
    user = (
        f"当前这句话：\n{question}\n\n"
        f"已学会的配方：\n{recipes}\n\n"
        f"当前库真实存在的表与列（只能引用这里出现的名字）：\n{schema[:6000]}\n\n"
        + (f"[环境观察（数据源真实的取值）——筛选字面量必须逐字取自这里，"
           f"清单里没有的写法不许出现在 SQL 里]：\n{ground[:5000]}\n\n"
           if ground else "")
        + "按规则产出回答这句话的 SELECT。"
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


# 多技能输出契约 = 共享 _REFILL_SYSTEM 的规则 0-5（量纲判断 / 回填不是抄数值 /
# 字面量地面真话 / 防乘爆 / 槽位），只换输出段 —— 单文件单一事实源，规则改动
# 两边同步生效。
_REFILL_MULTI_SYSTEM = (_REFILL_SYSTEM.split("只返回一个 JSON：")[0] + """多条输出契约（原版同构 2026-10-26：消费检索到的**全部**已学技能整批交给
执行层按需组合调用 —— voyager.py:195 retrieve → :199 render_system_message →
action.py:94 programs 注入；此前只挑一条写一条 SQL，合成题（base+津贴+加班）
被单维度配方答掉，实测 3600/264000/133500 三连）：

- **逐条**判断检索到的每条配方：它算的量，要么本身就是这句话问的量，
  要么是这句话问的量的**组成部分** —— 问「总支出」时，基本工资/津贴/加班费
  各条配方都是组成部分，**都要实例化出来**，各自取真数后由叙事层相加组织
  （用户 2026-10-25：「检索到的技能取到数字，给叙事 LLM 相加或者做什么，
  也算能力」）。
- 跟问的量无关的配方不要输出（硬凑 = 张冠李戴，规则 0 同款）。
- 如果某条**复合配方**（一条 SQL 已覆盖整题全部组成部分）命中，就只输出它，
  不必再输出它的组成部分。
- 每条 SQL 必须独立可执行（单条 SELECT）；守卫与执行逐条进行，一条被拒不
  连坐其它条。

只返回一个 JSON：{"items": [{"name": "配方名", "sql": "该配方实例化后的 SELECT"}],
"note": "一句话说明复用了哪些配方、各填了什么值；哪些没用上、为什么"}
其中 name **必须逐字写配方标签「｜」之后的原配方名**（如 total_base_salary），
不要把整个【配方 N｜topic=…｜…】标签抄进来 —— 下游要靠它精确对回配方。""")


def _refill_sql_multi(question: str, skills: list[dict[str, Any]], schema: str,
                      db_path: str = "", feedback: str = "",
                      catalog: str = "", file_context: str = "") -> dict[str, Any]:
    """原版同构的多技能实例化：检索到的每条相关配方都回填成 SQL（不是只挑一条）。

    与 `_refill_sql` 同一地面真话（db_observation 实查取值清单 + schema），
    输出从「一条 SQL」改为「SQL 列表」—— 合成题由多条原子技能各取真数、
    叙事层组织；复合配方命中时只出它。

    `feedback`（2026-10-26）：上一轮回填/执行的报错清单（原版 step 的
    critique——环境报错是地面真话，喂回去 LLM 才修正得了，voyager.py:236-271）。

    `catalog`（2026-10-28 用户拍板「让 agent 看到全貌」）：技能库**全库**目录。
    检索只给命中的 top_k，模型就看不出「问总额 = 基薪配方 + 津贴配方」这种
    组合关系（实测：命中 base_salary 一条 → 只算出 55000，漏 2120 津贴）。
    给了全库目录，组合关系由模型自己看出来，不需要在代码里写死业务词表。
    """
    recipes = "\n\n".join(
        f"【配方 {i+1}｜topic={s.get('topic')}｜{s.get('name') or ''}】\n"
        f"说明：{str(s.get('text') or '')[:200]}\n"
        f"SQL：\n{str(s.get('code') or '')[:900]}"
        for i, s in enumerate(skills[:5])
    )
    ground = ""
    if str(db_path or "").strip():
        try:
            from curriculum_auto import db_observation as _dbobs
            ground = str(_dbobs(db_path) or "")
        except Exception:
            ground = ""
    user = (
        f"当前这句话：\n{question}\n\n"
        # R1 客户端式：用户上传的文件**原文进上下文**（列名/行数/真实前几行）。
        # 用户 2026-10-28：「上传文件的时候要像 R1 客户端一样当作上下文输入
        # （这个可以防一堆暴雷；比写死一堆 prompt 强）」——模型看到真实内容，
        # 就不用猜列名/取值，也不用在代码里列业务词。
        + (f"{file_context}\n\n" if file_context else "")
        + f"已学会的配方：\n{recipes}\n\n"
        + (f"[技能库全库目录 —— 库里已经学会的**全部**方法，上面只列了本题命中"
           f"的几条。判断题面要的量需不需要**组合多条**（题面问合计/总额而"
           f"单一配方只覆盖其中一部分时）：需要就把对应方法也各实例化一条 "
           f"SELECT，交给上层相加]\n"
           f"{catalog[:3000]}\n\n" if catalog else "")
        + f"当前库真实存在的表与列（只能引用这里出现的名字）：\n{schema[:6000]}\n\n"
        + (f"[环境观察（数据源真实的取值）——筛选字面量必须逐字取自这里，"
           f"清单里没有的写法不许出现在 SQL 里]：\n{ground[:5000]}\n\n"
           if ground else "")
        + "按规则把**每条**要用的配方都实例化成独立的 SELECT。"
        + (f"\n\n⚠️ 上一轮这些配方的回填/执行失败了（报错是环境给的地面真话，"
           f"照着修——列名/表名以 schema 为准，不许发明列）：\n{feedback}"
           f"\n\n⚠️ 硬约束：被拒原因是「费率/系数不存在」的配方，说明该量在"
           f"库里**不可计算** —— 直接不输出那条，绝不许去掉系数、换口径、"
           f"换列来凑一个数（那是语义造假，比答不出更糟）。"
           if feedback else "")
    )
    out = chat_json(
        [
            {"role": "system", "content": _REFILL_MULTI_SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
    )
    items: list[dict[str, str]] = []
    for it in (out.get("items") or [])[:5]:
        if not isinstance(it, dict):
            continue
        _sql = str(it.get("sql") or "").strip()
        if _sql:
            items.append({"name": str(it.get("name") or ""), "sql": _sql})
    return {"items": items, "note": str(out.get("note") or "")}


# （_pick_composite_skill 已删 —— 2026-10-26 多技能编排取代「挑一条复合配方」：
#  回填层自己判断是出复合配方还是多条原子配方，见 _refill_sql_multi 的输出契约。）


# ---------------------------------------------------------------------------
# ③' 配方泛化：入库前把「上一题的取数 SQL」改写成**槽位模板**
#     （用户 2026-10-23 定：「不要把数值写死，而是把算法积累成技能」）
# ---------------------------------------------------------------------------

_GENERALIZE_SYSTEM = """你是数据取数方法整理员。手里有一条为某道旧题写好的 SQL 配方，
要把它改写成**可复用的取数方法模板**：把题目专属的实体值替换成命名槽位，
下次换一道同方法的题，只要把新值填进槽位就能用。

规则：
1. 只把**来自题目的实体值**换成槽位：WHERE 里的筛选值（部门名/月份/队伍/
   选手/图名）、比较阈值这类「这次算的是哪个范围」。
   表名、列名、聚合方式（COUNT/SUM/AVG）、JOIN 结构、GROUP BY 的列
   都是**方法本身**，一个字都不许动。
2. 槽位写成 `:语义名`（小写蛇形，如 :dept、:month、:team）。
   ⚠️ 槽位只许出现在**值的位置**（与列比较的右侧 / IN 的元素 / LIMIT 的数字）。
   实测踩过：把槽位塞进列别名（`AS total_:kind`）或写成拼接表达式
   （`hire_date < :month + '01'`）—— 别名坏了输出列名就变了，日期要整体
   一个槽位（`hire_date < :cutoff_date`），不许拆开拼。
3. 列别名（AS 后面的名字）**原样保留**，哪怕它含旧题的词 —— 别名只是输出
   列名，不是筛选值。
4. 输出第一条 SQL 注释逐个槽位写明「该填什么、从问句哪里取」，例如：
   -- slots: dept=部门名，取问句里点名的部门（如"技术"）; cutoff_date=统计截止日（如"2026-10-01"）
5. 输出必须是单条 SELECT 或 WITH 开头的语句，不许 INSERT/UPDATE/DROP。
6. 这条 SQL 里若没有题目专属的实体值（纯结构查询），原样返回，slots 给空数组。

只返回一个 JSON：{"sql": "模板化后的 SQL", "slots": ["dept"],
"note": "一句话说明把哪些值换成了槽位（没有就说原样返回）"}"""


def generalize_sql_recipe(code: str, question: str) -> str:
    """SQL 配方入库前泛化成槽位模板 —— **技能存算法，不存上一题的数值**。

    原版同构：原版技能是**带参数的函数**（skill.py 的 programs 注入执行域，
    实体值由新代码在调用时传入），从不把某次任务的实体写死在技能体里。
    MVE 的 SQL 配方是为单题写的查询，天然带着旧题的筛选值 —— 原样重放就是
    旧题的答案（2026-10-23 实测 consume_f4e54ccd 带 ['09','2026']，对
    「技术部门请假总天数」重放必错）。入库时把实体值换成 :slot，消费侧
    `_refill_sql` 按当前问句填槽实例化（槽位规则见 _REFILL_SYSTEM 规则 4）。

    任何失败（LLM 挂 / 产出不像 SQL）都**原样返回**：泛化是增强不是闸门，
    不许它阻塞学习闭环 —— 入库铁律（成功 rollout 才入库）不受影响。
    """
    sc = str(code or "").strip()
    if not sc.lower().startswith(("select", "with")):
        return code            # 非 SQL（本机 Python 配方）不归这里管
    try:
        out = chat_json(
            [
                {"role": "system", "content": _GENERALIZE_SYSTEM},
                {"role": "user",
                 "content": f"这条配方对应的旧题：\n{str(question or '')[:300]}\n\n"
                            f"SQL：\n{sc[:3000]}"},
            ],
            temperature=0.2,
        )
    except Exception:
        return code
    sql = str(out.get("sql") or "").strip()
    # 模板按规则带 `-- slots: …` 注释头 —— 校验前先剥掉注释行，
    # 否则合格的模板会被这里的 startswith 拒掉、每次都静默回退原样。
    _body = "\n".join(ln for ln in sql.splitlines()
                      if not ln.strip().startswith("--")).strip()
    if not _body.lower().startswith(("select", "with")):
        return code
    # 槽位只许出现在**值的位置**：`:slot` 前面紧贴标识符字符（实测
    # glm-4-flash 写出 `AS total_:kind` 把槽位塞进列别名）= 方法被改坏，
    # 原样返回 —— 坏模板比重放旧值更危险（张冠李戴）。
    import re as _re
    for _m in _re.finditer(r":([a-z_][a-z0-9_]*)", _body, _re.I):
        _i = _m.start()
        if _i > 0 and (_body[_i - 1].isalnum() or _body[_i - 1] == "_"):
            return code
    # 子句级守卫：槽位只许出现在 **WHERE / JOIN ON 的值位置**。
    # 实测（2026-10-24 Scene P）glm-4-flash 会把 SELECT 列表里的列名
    # （`SUM(:base_salary + :amount)`）和 GROUP BY 的分组列（`GROUP BY :emp_id`）
    # 也换成槽位 —— 那是方法本体，换掉等于把算法改坏。粗切子句边界：
    # 第一个顶层 FROM 之前 = SELECT 列表；GROUP BY 起（含 HAVING/ORDER BY）
    # 一概拒绝 —— 保守方向是「不泛化原样入库」，不是「入库坏模板」。
    _slots = list(_re.finditer(r":[a-z_][a-z0-9_]*", _body, _re.I))
    if _slots:
        _up = _body.upper()

        def _pos(m: Any) -> int:
            return m.start() if m else len(_body)

        _from = _pos(_re.search(r"\bFROM\b", _up))
        _group = _pos(_re.search(r"\bGROUP\s+BY\b", _up))
        for _m in _slots:
            if _m.start() < _from or _m.start() >= _group:
                return code
    return sql


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
            "A 量，取到的其实是 B 量，模型照样把 B 的数写进 A 的维度名，"
            "下游把它讲成 A 的答案。"
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

subject 至少要能定位到谁：用本题库的**真实字段与真实取值**来定位
（涉及群体带群体字段、涉及个体带个体字段，字段名逐字取自 schema）。
dimension 要能看出方向：带方向前缀（如 max_ / min_），不要写中性名字 ——
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
                         "Voyager 算的量完全正确，"
                         "是裁判自己把别的量混进来比对，结果 Voyager 被判"
                         "「做法有硬伤」，面板上显示的是冤枉它。\n"
                         "    裁判跑偏时：same_metric=false，voyager_sound 仍按第一步判，"
                         "并在 diff 里写清「是裁判多算了什么/少算了什么」。\n"
                         "    reason 里必须写「Voyager 无硬伤」或「Voyager 有硬伤：…」，"
                         "与 voyager_sound 保持一致（别自相矛盾）。\n"
                         "⚠️ 两边都取到 0 行时，SQL 再像也**没有数可比** —— "
                         "这时 agreement 必须给 0，same_metric=false，"
                         "reason 写明「两边都没取到数，无从比对」。别把两个空结果"
                         "判成「口径一致」 —— 实测踩过：两边的实体在当前库里都没有数据，"
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


def _compose_halves(question: str, db_part: str, web_part: str) -> str:
    """按规划把两半合成一个回答（2026-10-29 用户：「所有问题都应由外部规划
    去完成」——规划不止路由，还要负责合成）。

    铁律：库内数字原样引用、不改不凑；外部信息如实标注来自联网资料；材料
    里没有的绝不补。合成失败 → 返回空串，调用方退回机械拼接（不假装合成过）。
    """
    try:
        cj = chat_json(
            [{"role": "system",
              "content": ("把两半材料合成一个回答。铁律：库内数字原样引用、"
                          "不改不凑；外部信息如实标注来自联网资料；材料里"
                          "没有的绝不补、不算。只返回 JSON：{\"answer\": \"...\"}")},
             {"role": "user",
              "content": (f"问题：{question}\n\n"
                          f"【库内半场（可复现，已验证取到真数）】\n"
                          f"{str(db_part)[:3000]}\n\n"
                          f"【外部半场（联网 claim，未验证）】\n"
                          f"{str(web_part)[:3000]}\n\n"
                          "请对照两边给出结论；看不出结论就如实说数据不足。")}],
            temperature=0.0)
        return str((cj or {}).get("answer") or "").strip()
    except Exception:
        return ""


async def _tool_line_fallback(question: str, context: str = "") -> dict[str, Any]:
    """工具线兜底（第 4 步，DeepSeek 消费同构）：库场景答不出 → 联网取公开
    资料、当场作答、用完即弃。

    同构依据：DeepSeek 客户端消费架构 = 按需拉能力；MVE 库场景此前
    「消费不接 MCP」（consume.py 场景库形态注释）是联网唯一真缺口。

    claim 口径铁律（2026-10-28 拍板）：
      · 联网取回的是 **claim**（有真值但未知）—— 答案必须带「工具线 claim」
        标注，落库后才可升级 fact；
      · **不入技能库、不进能力账、不计入学习曲线**（工具功劳不能算到学习
        头上）—— 调用方保证（critic_ok=False → absorbed={} 恒成立）；
      · 联网失败**不是"答一个像样的"**：没取到内容就如实报 note，绝不编造
        （与 net_tools「取不到就如实抛错」同一条铁律）。
    """
    out: dict[str, Any] = {"used": False, "answer": "", "sources": [], "note": ""}
    try:
        import net_tools
    except Exception as e:
        out["note"] = f"联网原语不可用：{type(e).__name__}: {e}"
        return out
    import asyncio as _aio

    async def _one_shot(qry: str) -> dict[str, Any]:
        """一轮完整的 搜索 → 取正文 → claim 综合。供首答与改写重试共用。"""
        o: dict[str, Any] = {"used": False, "answer": "", "sources": [],
                             "note": ""}
        # ① 搜索（同步函数 → 线程池，不堵事件循环）
        try:
            results = await _aio.to_thread(net_tools.web_search, qry, 5)
        except Exception as e:
            o["note"] = (f"搜索失败（联网失败不是答一个像样的）："
                         f"{type(e).__name__}: {str(e)[:140]}")
            return o
        results = [r for r in (results or [])
                   if str(r.get("url") or "").strip()][:3]
        if not results:
            o["note"] = "搜索没返回任何结果 —— 如实不答"
            return o
        o["sources"] = [{"title": str(r.get("title") or "")[:120],
                         "url": str(r.get("url") or "")} for r in results]
        # ② 取页正文（多源重试，2026-10-29 用户拍板「一定做」）：
        # 反爬/超时是常态，只抓 top2 且各试一次命中率太低（实测同一题
        # 「搜索有结果但正文都没取到」）。现在：最多试 top4，每个失败再
        # 重试一次，凑够 3 页就停 —— 全失败才如实报，绝不编。
        pages: list[dict[str, str]] = []
        tries = 0
        for r in results[:4]:
            if len(pages) >= 3:
                break
            for _attempt in (1, 2):
                tries += 1
                try:
                    pg = await _aio.to_thread(net_tools.web_fetch, str(r["url"]))
                    if str(pg.get("text") or "").strip():
                        pages.append({"title": str(r.get("title") or "")[:120],
                                      "url": str(r.get("url") or ""),
                                      "text": str(pg["text"])[:5000]})
                        break
                except Exception:
                    continue
        if not pages:
            o["note"] = (f"搜索有结果但页面正文都没取到（试了 {tries} 次）"
                         " —— 如实不答")
            return o
        # ③ claim 综合：只准用取回的资料，缺了就说缺
        material = "\n\n".join(
            f"【资料{i+1}】{p['title']}\n{p['url']}\n{p['text']}"
            for i, p in enumerate(pages))
        try:
            cj = chat_json(
                [{"role": "system",
                  "content": ("你根据给出的联网资料回答问题。铁律：**只依据资料**；"
                              "资料里没有的写「资料未提及」；绝不编造数字与事实。"
                              "只返回 JSON："
                              '{"answer": "基于资料的回答", "grounded": true/false}'
                              "（grounded = 资料是否足以回答该问题）")},
                 {"role": "user",
                  # 上传的文件是这次的上下文：先按文件答，联网资料是补充。
                  "content": ((str(context).strip() + "\n\n")
                              if str(context or "").strip() else "")
                             + f"问题：{qry}\n\n联网资料：\n{material[:9000]}"}],
                temperature=0.0)
            ans = str((cj or {}).get("answer") or "").strip()
            if ans and bool((cj or {}).get("grounded", True)):
                o["used"] = True
                o["answer"] = ans
            elif ans:
                o["note"] = "资料不足以回答（grounded=false，如实不答）"
            else:
                o["note"] = "claim 综合没产出回答"
        except Exception as e:
            o["note"] = f"claim 综合失败：{type(e).__name__}: {str(e)[:140]}"
        return o

    out = await _one_shot(question)
    if not out.get("used"):
        # 改写重试（DeepSeek 同构：搜索词也是工具调用的参数，模型自己写）：
        # 题面常带指代与口语（「这家公司…啊」），直接拿去搜命中率低 —— 实测
        # 同一题换通用措辞就能取回正文。改写一轮再来一次；还不行就如实报。
        _q2 = ""
        try:
            _cj2 = chat_json(
                [{"role": "system",
                  "content": ("把这句话改写成适合搜索引擎的通用查询：去掉指代"
                              "（如「这家公司」）与口语语气词，保留要查的主题；"
                              "不是翻译也不是扩写。只返回 JSON："
                              '{"query": "搜索词"}')},
                 {"role": "user", "content": question}],
                temperature=0.0)
            _q2 = str((_cj2 or {}).get("query") or "").strip()
        except Exception:
            _q2 = ""
        if _q2 and _q2 != question:
            _out2 = await _one_shot(_q2)
            if _out2.get("used"):
                out = _out2
    return out


async def consume(
    question: str,
    *,
    topic_hint: str = "",
    auto_task: bool = True,
    env: VLMLMCPEnv | None = None,
    web_allowed: bool | None = None,
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

    # ---- ⓪ 上传文件当上下文（R1 客户端式）----
    # 用户 2026-10-28：「上传文件的时候要像 R1 客户端一样当作上下文输入」。
    # 库里的全貌（db_observation）只给「表/列/取值集合」，给不了**行级**形状；
    # 上传时的真实前几行在这里进上下文 —— 回填层看得到真实列名与取值，
    # 不用猜、也不用在代码里写业务词。
    _file_ctx = ""
    try:
        import upload as _upl
        # ⚠️ **必须把 question 传进去**：文件是「这一次请求的上下文」，不是
        # 永久贴纸。全文动辄几十万字（场景 G 的 17KB PDF 只是小的），一次
        # 全塞进输入会撑爆窗口也稀释注意力 —— DeepSeek 客户端同样只放得下
        # 一个上下文窗口。所以按**这一句话**去文件里挑相关段落（upload._pick_text），
        # 开头永远保留（文档的摘要/目录通常在前面）。
        _file_ctx = str(_upl.context_block(_scene_id, question=question) or "")
    except Exception:
        _file_ctx = ""

    # 本场景数据源里有哪些表（空库 = 还没东西可查）。用来区分两种「没答出来」：
    # 库是空的（先去读上下文/文件）vs 库有数据但技能没覆盖（该去学）。
    _scene_tables: list[str] = []
    try:
        import scene_profile as _sp
        _scene_tables = list(_sp.source_tables(_scene_id) or [])
    except Exception:
        _scene_tables = []

    # ---- ⓪' 信息路规划（斯坦福 Generative Agents 同构：先规划、再对真实
    # 环境做可达性检查；不可达的步骤如实修订，绝不假装走到过）----
    # 用户 2026-10-29 拍板的闭环：LLM 先生成可达性规划 → 按规划逐路执行
    # （db 路走既有消费/学习线：技能复用 → 取数 → 进技能库 → 里程碑重试；
    # web 路执行时另核联网工具可不可达）→ 综合作答。DeepSeek V3.2 同构点：
    # 工具菜单外挂给模型、模型自主规划；MVE 特有的三线记账规则不变。
    _plan: dict[str, Any] = {}
    tool_line: dict[str, Any] = {"used": False, "answer": "", "sources": [],
                                 "note": ""}
    try:
        _plan = _plan_routes(question, scene_id=_scene_id,
                             has_file=bool(_file_ctx),
                             scene_tables=_scene_tables)
    except Exception:
        _plan = {}

    # ---- 规划门控（2026-10-29 用户拍板「所有问题都应由外部规划去完成」）----
    # 走不走环境**只**由规划决定。词表已删（它是硬编码路由：把「待遇好不好」
    # 判成非数据题 → db 半场直接跳过、0 行收场，实测）。规划员不可用
    # （planned=False）→ 兜底也不碰环境（不猜题型），只做模型推断并如实标注。
    _use_env = bool(_plan.get("db")) if _plan.get("planned") else False
    # 对话页联网开关（DeepSeek 同构，2026-10-29）：开关关 = 规划里的 web 步
    # 不执行（计划照旧显示，执行层如实交代）；开关开 = 仍要过可达性检查。
    # None = 没带开关（旧客户端/CLI）→ 按规划执行。
    if web_allowed is False and _plan.get("web"):
        _plan = dict(_plan)
        _plan["web"] = False
        _plan["why"] = (str(_plan.get("why") or "")
                        + "（对话页联网开关关：web 步不执行）")[:200]

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
    inference_used = False        # inference 兜底（句句有回复的最后一层）
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
            # 回灌环境建里程碑锚点（用户 2026-10-25 拍板）对"未命中"同样生效：
            # 见下面路由块之后的通用段（人导入的每句都立为当前里程碑锚点）。
    except Exception:
        pass                                     # 待审区写不进去不能拦消费
    # ---- 回灌环境建里程碑锚点（用户 2026-10-25 拍板）----
    # 人导入的每句都作为本场景**当前里程碑锚点**（对齐原版 inference 把人工给定
    # 任务当里程碑反复重试直到掌握）。驾驶舱（scene_learn 里程碑优先块）启动后
    # 按本句拆维学、未闭合维度优先（"没完成的优先"）。无论是否命中图谱都立锚点
    # —— 命中但还没学成里程碑的（"不存在于里程碑队列"）同样回灌；跨域明显不属
    # 本场景的靠 suggest_new_scene 提示可另建场景承接，这里不阻断消费。
    if str(question or "").strip():
        try:
            import maieutic_scaffold as _ms
            _ms.set_current_target(str(_scene_id or "A"), question)
            miss_info["milestone_target_set"] = True
        except Exception:
            pass
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

    # （①' 合成题判定已并入下方「维度覆盖守卫」——同一套 milestone_dimensions
    # + dims 口径在那边算 _nd；单配方捷径删除后这里不再需要预先分流。）

    # ---- ② 找已积累的能力（配方）----
    # ⚠️ 检索键必须是**本题归题结果**（2026-10-20 实测根因）：原来这里传的是
    # 函数入参 `topic_hint`（默认空），`_pick_ability_topic` 于是按词面**随便
    # 挑最像的** —— 问「正念是什么」时挑中了「正念练习的常见误区」那批技能
    # （consume_2461737d），本题自己那批（consume_ca2125be）一条都没进 prompt。
    # 表现就是：图谱已经高置信命中、技能库里明明有 6 条，消费却报"用到 0 条"。
    # 同原版 `retrieve_skills(query=self.context)` —— context 里就是当前这道题。
    # ⚠️ 2026-10-29 服务器实测（「腾讯校招算法岗起薪」）：规划明明只排了 web 路
    # （外部市场数据，库里没有），却仍去技能库检索，命中一条库内配方
    # `base_salary_comparison` 并**真去实查**（`SELECT ... FROM up_工资明细`），
    # 判官随后按「主体范围错误：题面问腾讯校招，SQL 只查了技术部」退回，
    # 白烧一轮 + 判词写成 fetched_empty。根因：检索不看规划。
    # 规划里没有 db 路 = 这题不该用库内配方 → 不检索、不 mark_used。
    # 有 db 路（含 db+web 混合题）照旧检索。
    _raw_skills = (_retrieve_skills(question, topic_id or topic_hint, _scene_id)
                   if _use_env else [])
    # ⚠️ 消费会话级技能（`consume_<md5>` topic）不是「学习跑过的里程碑」技能，
    # 只是上次消费兜底现做经 `_absorb` 落库的产物。按用户边界（2026-10-25：
    # 消费只编排、不复用消费会话产物、未学不答只标记）—— 必须排除出「已学
    # 里程碑」复用池。否则场景里只剩这类技能时，任何新问法都被 `_pick_ability_topic`
    # 归到它并检回 → `skills` 永远非空 → 未学分支（下方 `else:`）永不触发，
    # 表现正是用户抱怨的「清库了还是现做/复用混乱」。
    skills = [s for s in _raw_skills
              if not str(s.get("topic") or "").startswith("consume_")]
    reused_list = [{"topic": s.get("topic"), "name": s.get("name"),
                    "key": s.get("key")} for s in skills]
    # 维度覆盖守卫的标尺：本条答案实际**复用**的是哪些技能。默认取全部检回技能；
    # 复合复用（③b）成功落 `_comp` 后改挂这条组合技能——守卫只核实「被复用的
    # 组合技能自身覆盖是否自洽」，而不是拿全部维度技能的并集去压它（否则
    # total_salary 这种「2 表组合维度」会被误判为漏了 attendance 那张维度表而
    # 被错杀成 fetched_empty，见 2026-10-25 场景 B 九月份总薪酬复验）。
    _ans_skills = skills

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
        # 多技能编排的执行账（_refill_sql_multi 逐条实例化 + 执行成功的技能）；
        # _used_skills 是它们的技能元数据（面板 used_skills 披露用）。
        _executed: list[dict[str, Any]] = []
        _used_skills: list[dict[str, Any]] = []
        # 合并时算出的「各来源金额」与「合计」（确定性算术；_do_merge 里填）。
        _money_kv: list[tuple[str, float]] = []
        _total: float | None = None
        if _scene_kind == "scene":
            # ---- 场景库形态的配方复用：**回填**，不原样重放（2026-10-23 用户
            # 定「不要把数值写死，把算法积累成技能」）----
            # 原版同构：技能是带参数的函数，实体值是**调用时**传的（skill.py
            # programs 注入执行域）。之前这里直接重放配方原文，配方里带着
            # 上一题的筛选值（实测 consume_f4e54ccd 带 ['09','2026']），重放
            # 出来的就是旧题答案，只能再打「字面量检测」补丁拦 —— 补丁只认
            # 数字，字符串实体（'技术'→'财务'）照样漏。现在统一走 _refill_sql：
            # 模型按当前问句把方法实例化成新 SQL，规则 0 还会拒掉「不是同一个
            # 量」的张冠李戴；与 VLML 分支（本文件 :1060 附近）同一结构。
            _schema = ""
            try:
                _schema = await _env.schema_block()
            except Exception:
                _schema = ""
            # （③a 单配方捷径已删 —— 2026-10-26 对齐原版：检索到的**全部**已学
            # 技能整批实例化、逐条执行，见下方 `if reused_skill is None:` 块；
            # 原子题/合成题同一条编排路径，不再分流。）
        else:
            for s in skills:
                sc = str(s.get("code") or "")
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
                    reuse_errors.append(
                        f"重放 {s.get('name')} 没取到数：{err_try}")
                    continue
                data, code, reused_skill = data_try, sc, dict(s)
                obs = [{"ok": True, "tool": "local_python", "rows": 1,
                        "data": data_try}]
                break
        # ③b 场景库形态：**多技能编排复用**（原版同构 2026-10-26 用户拍板
        # 「检索/组织复用没对齐就改」）。原版消费 = inference → rollout → step：
        # 检索技能**整批**注入执行层（voyager.py:195-199 → action.py:94 programs），
        # agent 按需组合调用，critic 收口 —— 没有单配方捷径。此前只挑一条配方写
        # 一条 SQL，合成题被单维度答掉（实测 3600/264000/133500 三连）。现在：
        # 检索到的全部已学技能整批回填成 SQL 列表（_refill_sql_multi）→ 逐条过
        # 确定性守卫（字面量 + fan-out）→ 逐条执行取真数 → 合并交叙事层相加组织
        # （用户 2026-10-25：「取到数字给叙事 LLM 相加也算能力」）。
        # 边界不变：消费不现做合成 —— 回填全是已学配方的实例化；实例化不出/
        # 守卫拒/执行空 → 如实标记去学，绝不现场写 SQL。
        if reused_skill is None:
            if _scene_kind == "scene":
                _dbp = str((_prof or {}).get("db_path") or "")
                if not _scene_tables and _file_ctx:
                    # 数据源里**一张表都没有**（文档/PDF 类场景）→ 没有可查的东西，
                    # 别写 SQL。文件就是这次请求的上下文，直接读它作答 ——
                    # DeepSeek 客户端拿到一本 PDF 也只会读，不会先要一张表。
                    # （不写 SQL 不是「刻意跳过」：没有表就没有可执行的查询，
                    #   硬跑只会拿一串 "Table xxx does not exist" 当"没答出来"。）
                    reuse_errors.append(
                        f"本场景数据源里还没有表"
                        f"（{Path(_dbp).name if _dbp else '—'}）→ 无 SQL 可写，"
                        f"改按上传的文件作答")
                    obs, code = [], ""
                elif not skills and _file_ctx:
                    # 有上传文件当上下文、但本题还没学过：**先现场答一次**。
                    # R1 客户端拿到文件就能读，不会先要求「学过」；MVE 同理
                    # ——文件已经把表/列/真实取值摆在上下文里，让 agent 直接
                    # 写 SQL 试一次，成不成由后面对题判官与真空防线收口。
                    # （2026-10-28 用户：「上传文件要像 R1 一样当上下文输入，
                    #  这个可以防一堆暴雷」）
                    print("  · 未学但有上传文件上下文 → 现场作答一次", flush=True)
                    obs, code = await _la.solve(
                        question, _scene_dir, scene_id=_scene_id,
                        skills=skills, timeout=_consume_timeout(0),
                        context=_file_ctx)
                    real = [o for o in obs
                            if o.get("ok") and o.get("data") is not None]
                    data = (real[-1].get("data") if real else None)
                    if data is not None:
                        _ans_skills = []
                elif not skills:
                    # 未学（索引不到已学技能）：不合成、不答，标记交给课程规划。
                    # 设计边界（用户 2026-10-25）：消费侧不做合成，只检索复用；
                    # 检索不到说明里程碑没跑过，应由学习侧拆解完成。
                    reuse_errors.append(
                        "未学：索引不到已学技能 → 标记交给课程规划拆解学习")
                    learn_kick = {"kicked": False, "manual": True,
                                  "why": "未学（检索不到已学技能）—— 这道里程碑还没"
                                         "被学习跑过，需驾驶舱 ▶ 启动 Voyager 学完"}
                    obs, code = [], ""
                else:
                    _mr = _refill_sql_multi(question, skills, _schema,
                                            db_path=_dbp,
                                            file_context=_file_ctx)
                    if not _mr["items"]:
                        reuse_errors.append(
                            f"检索到 {len(skills)} 条已学技能但回填层没实例化出任何"
                            f" SQL：{str(_mr['note'])[:140]}")
                    _fails: list[dict[str, str]] = []

                    async def _try_items(_items: list[dict[str, str]],
                                         _round: str) -> None:
                        """逐条守卫 → 执行（原版 step 同构：每条配方 = 一个
                        子目标 rollout，环境报错如实记账）。"""
                        for _it in _items:
                            _sql = _it["sql"]
                            # name 对回：先精确，再宽松（LLM 偶尔把整个
                            # 【配方 N｜topic=…｜原名】标签抄进 name —— 标签里
                            # 含原配方名就算命中，否则 _sk=None 会让 _used_skills
                            # 披露丢条目、used_skills 少报（实测「用到 1 条技能」）。
                            _sk = next((s for s in skills
                                        if str(s.get("name") or "") == _it["name"]),
                                       None)
                            if _sk is None and _it["name"]:
                                _sk = next((s for s in skills
                                            if str(s.get("name") or "")
                                            and str(s.get("name")) in _it["name"]),
                                           None)
                            _label = (str((_sk or {}).get("name") or "")
                                      or _it["name"]
                                      or str((skills[0] if skills else {})
                                             .get("name") or "配方"))
                            # 防重复按**结果签名**判，不按配方名判。
                            # 2026-10-28 那版按配方名挡（防同一配方两个变体
                            # 重复相加），但 2026-10-29 实测它把**合法的另一个
                            # 槽位**也挡了：重填层想补「全公司」口径的
                            # tech_base_salary 实例化，被「该配方已有一条成功
                            # 账」跳过 —— 判官只能拒，整题空手。同一份 SQL =
                            # 同一份结果，才叫重复；不同槽位（技术部 vs 全公司）
                            # 是不同的账，该放行。
                            _sig = " ".join(str(_sql or "").split()).lower()
                            if any(" ".join(str(e.get("sql") or "").split()).lower()
                                   == _sig for e in _executed):
                                reuse_errors.append(
                                    f"{_label} 的这份 SQL 与已有账逐字相同"
                                    f" —— 同一结果不重复计")
                                continue
                            _gwhy = _sql_literal_guard(_sql, _dbp)
                            if _gwhy:
                                reuse_errors.append(
                                    f"{_label} 被字面量守卫拒用（幻觉取值）：{_gwhy[:120]}")
                                _fails.append({"name": _label, "sql": _sql,
                                               "why": _gwhy[:200], "guard": "literal"})
                                continue
                            _fwhy = _fanout_guard(_sql, _dbp)
                            if _fwhy:
                                reuse_errors.append(
                                    f"{_label} 被 fan-out 闸门拒用（JOIN 乘爆）：{_fwhy[:120]}")
                                _fails.append({"name": _label, "sql": _sql,
                                               "why": _fwhy[:200], "guard": "fanout"})
                                continue
                            # 单位守卫已删（2026-10-29）：题面口径/单位语义
                            # 不在代码里判，交给执行后的全貌判官（_fits_judge）。
                            # 费率守卫（确定性）：乘以库里指认不了的常量 =
                            # 假设费率（SUM(overtime_hours)*100 事故，2026-10-28）。
                            _rwhy = _rate_guard(_sql, _dbp)
                            if _rwhy:
                                reuse_errors.append(
                                    f"{_label} 被费率守卫拒用：{_rwhy[:120]}")
                                _fails.append({"name": _label, "sql": _sql,
                                               "why": _rwhy[:200], "guard": "rate"})
                                continue
                            _rr = await _env.run_sql(_sql)
                            _d = _rr.get("data") or {}
                            if _rr.get("ok") and _d.get("rows"):
                                _executed.append({"skill": _sk or {},
                                                  "name": _label,
                                                  "sql": _sql, "data": _d,
                                                  "rows": _rr.get("rows")})
                            else:
                                _why = str(_rr.get("error") or "0 行")[:200]
                                reuse_errors.append(
                                    f"{_label} 回填后没取到真数：{_why[:120]}")
                                _fails.append({"name": _label, "sql": _sql, "why": _why})

                    await _try_items(_mr["items"], "首轮")
                    if not _executed and _fails:
                        # 原版 step 同构（voyager.py:236-271）：执行报错作为
                        # critique 重新 render 进下一轮 human message —— 环境报错
                        # 是地面真话，喂回去 LLM 才修正得了。冷启动单发实测会犯
                        # BinderException（total_salary 配方本身干净，回填 LLM
                        # 画蛇添足给 employees 加了不存在的 month 列）。重试一次
                        # （= 原版 4 步迭代里的一次修正轮，不是新发明）。
                        _fb = "\n".join(f"- {_f['name']}：{_f['why']}"
                                        for _f in _fails[:5])
                        print(f"  · 复用首轮全败（{len(_fails)} 条）→ 报错当 critique "
                              f"重试一轮（原版 step 迭代同构）", flush=True)
                        # 重试必须带上**全貌**（表/列/真实取值）：只给报错
                        # 不给取值，模型会把「津贴」当成 allowance.kind 的取值
                        # 再写一遍（2026-10-28 实测：kind='津贴' 被字面量守卫
                        # 连拒两轮 → 整题真空；库里真实取值是 餐补/交通）。
                        _mr2 = _refill_sql_multi(question, skills, _schema,
                                                 db_path=_dbp, feedback=_fb,
                                                 catalog=_catalog_block(_dbp),
                                                 file_context=_file_ctx)
                        if _mr2["items"]:
                            await _try_items(_mr2["items"], "重试")
                    _last_note: str = str(_mr.get("note") or "")

                    def _do_merge() -> None:
                        """把 _executed 合并成 data/code/obs（判官退回后要重来一遍）。"""
                        nonlocal data, code, obs, reused_skill
                        nonlocal _used_skills, _ans_skills
                        nonlocal _money_kv, _total
                        # 多技能结果合并（原版：agent 组合多条技能的产出交呈现层）：
                        # 每行标注来源技能，叙事层据此相加/组织；`rows` 键保住
                        # 下方真空防线的口径不变。
                        _merged: list[Any] = []
                        for _e in _executed:
                            # list 行还原列名（执行层 run_sql 带 columns）——
                            # 叙述守卫的单位语义靠**列名**活着，丢列名的
                            # 「值」行会让 20.5 小时混进金额相加（2026-10-28）。
                            _cols = [str(c) for c in
                                     ((_e.get("data") or {}).get("columns")
                                      or [])]
                            # 别名不撒谎（2026-10-28 用户截图事故）：配方写
                            # `SUM(overtime_hours) AS overtime_pay` 时，输出列
                            # 名看着是钱、来源列其实是小时 —— 合并时把这种列
                            # 改成「<来源列>（数量/时长，不是钱）」，下游
                            # （叙事对账 _collect_evidence、合计、人眼）就都
                            # 不会再把它当金额。
                            _q = _qty_outputs(str(_e.get("sql") or ""))
                            for _row in (_e["data"].get("rows") or []):
                                if isinstance(_row, dict):
                                    _pair = dict(_row)
                                elif (isinstance(_row, (list, tuple)) and _cols
                                      and len(_row) == len(_cols)):
                                    _pair = dict(zip(_cols, _row))
                                else:
                                    _merged.append({"来源技能": _e["name"],
                                                    "值": _row})
                                    continue
                                _fixed = {_k: _v for _k, _v in _pair.items()
                                          if _k not in _q}
                                for _k, _v in _pair.items():
                                    if _k in _q:
                                        _fixed[f"{_q[_k]}（数量/时长，不是钱）"] = _v
                                _merged.append({"来源技能": _e["name"], **_fixed})
                        # 合计**在这里算好**（确定性算术，不交给叙事 LLM 做加法，
                        # 也不交给它挑「哪几个是钱」）：数据里带一个「合计」，
                        # 叙事照着说就行，对账也认得出这个数。
                        _money_kv = _money_parts(_executed)
                        _total = (round(sum(v for _, v in _money_kv), 2)
                                  if len(_money_kv) > 1 else None)
                        data = {"rows": _merged,
                                "sources": [_e["name"] for _e in _executed]}
                        if _total is not None:
                            data = {"合计（各金额来源相加）": _total, **data}
                        code = "\n;\n".join(_e["sql"] for _e in _executed)
                        _first = next((e["skill"] for e in _executed
                                       if e.get("skill")), None) or skills[0]
                        reused_skill = {**dict(_first), "refill_note": _last_note,
                                        "composed_of": [_e["name"]
                                                        for _e in _executed]}
                        _used_skills = [dict(e["skill"]) for e in _executed
                                        if e.get("skill")]
                        _ans_skills = (_used_skills or skills)
                        obs = [{"ok": True, "tool": "query_sql",
                                "rows": _e["rows"], "data": _e["data"],
                                "data_sources": ["query_sql"]}
                               for _e in _executed]

                    if _executed:
                        _do_merge()
                    else:
                        reuse_errors.append(
                            "已检索到已学技能但复用未取到真数（回填/守卫/执行全败）")
                        learn_kick = {"kicked": False, "manual": True,
                                      "why": "已学技能检索到了但复用未取到真数 —— "
                                             "消费不做合成，需驾驶舱 ▶ 启动 Voyager "
                                             "重学该里程碑（或补缺维度）"}
                        obs, code = [], ""
                    # ---- 作答对题判官（**看全貌**判，不靠写死词表）----
                    # 2026-10-28 用户拍板：「不能这样写死守卫解决，而是…让 agent
                    # 看到全貌」。判官拿到「题面 + 实际 SQL + 取回结果 + 整库全貌
                    # （表/列/真实取值）+ 技能库全库目录」，自己看出「工资 = 基本
                    # 工资 + 津贴」这类合成关系 —— 业务口径不再写进代码。
                    # 判不过 → 带着全貌与拒因重填一轮（原版 step 迭代同构）；仍
                    # 不过就当没答出来（data=None 走学习线），绝不交片面数。
                    # 技能目录在判官**之前**取（2026-10-29 用户拍板「守卫根据
                    # 技能更新」）：判官眼中的「库里可算口径」锚在技能库现状上，
                    # 每学一条技能，可算集合自动扩一圈 —— 守卫不再依赖写死的
                    # 假设，跟原版 Voyager 以技能库/环境现状为判定锚同构。
                    _cat, _all_sk = _scene_skill_catalog(_scene_id)
                    if _executed:
                        _fit = _fits_judge(question, code, data, _dbp,
                                           items=_executed, fails=_fails,
                                           catalog=_cat)
                        if not _fit["ok"]:
                            reuse_errors.append(
                                f"作答被对题判官退回：{_fit['why'][:160]}")
                            print(f"  · 对题判官退回：{_fit['why'][:110]}"
                                  f" → 带全库目录重填一轮", flush=True)
                            _fb_lines = [f"- 上一轮结果被判官退回：{_fit['why']}"]
                            if _fails:
                                # 执行报错也是地面真话，必须一起喂回去：
                                # 只说「缺了津贴」而没说「你写的 month 列不存在」，
                                # 重填会照着同一个错再写一遍（2026-10-28 实测）。
                                _fb_lines.append("- 上一轮这些配方取数失败"
                                                 "（照着改，列名以 schema 为准）：")
                                _fb_lines += [f"  · {f['name']}：{f['why'][:160]}"
                                              for f in _fails[:5]]
                            _fb_lines.append("- 技能库全库目录（可组合多条，"
                                             "别只看命中的）：")
                            _fb_lines.append(f"{_cat or '（空）'}")
                            _fb2 = "\n".join(_fb_lines)
                            try:
                                _mr3 = _refill_sql_multi(
                                    question, (_all_sk or skills), _schema,
                                    db_path=_dbp, feedback=_fb2, catalog=_cat,
                                    file_context=_file_ctx)
                            except Exception as _e3:
                                _mr3 = {"items": [], "note": str(_e3)}
                            if _mr3["items"]:
                                # ⚠️ 判官说的是「缺一部分」，不是「全错」：
                                # 上一轮已取到真数的条目必须留着（清掉=把对的也
                                # 扔了 → 第二轮三条全被字面量守卫拒 → 空手。
                                # 2026-10-28 实测：首轮 基薪+津贴 已执行成功、判官
                                # 只指出缺加班费，清表后整题退回 fetched_empty）。
                                _prev = list(_executed)
                                _prev_sql = {" ".join(str(e["sql"]).split()).lower()
                                             for e in _prev}
                                # 结果内容一样 = 同一条账（换了个写法），不许
                                # 重复计进合并（实测重填出了第二份 allowance →
                                # 判官看到「SQL 3 和 SQL 4 重复计算了餐补」）。
                                _prev_sig = {
                                    json.dumps((e.get("data") or {}).get("rows"),
                                               ensure_ascii=False, sort_keys=True)
                                    for e in _prev}
                                _executed.clear()
                                _fails.clear()
                                _last_note = str(_mr3.get("note") or _last_note)
                                await _try_items(_mr3["items"], "判官退回重填")
                                _executed[:] = _prev + [
                                    e for e in _executed
                                    if " ".join(str(e["sql"]).split()).lower()
                                    not in _prev_sql
                                    and json.dumps(
                                        (e.get("data") or {}).get("rows"),
                                        ensure_ascii=False, sort_keys=True)
                                    not in _prev_sig]
                            if _executed:
                                _do_merge()
                                _fit = _fits_judge(question, code, data, _dbp,
                                                   items=_executed, fails=_fails)
                            if not _fit["ok"]:
                                data = None
                                reused_skill = None
                                reuse_errors.append(
                                    f"重填后仍不对题：{_fit['why'][:160]}")
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
                    timeout=_consume_timeout(len(skills or [])),
                    # 上传的文件就是这次的上下文（DeepSeek 客户端同构）：
                    # 本机/场景 solve 也看得见它，不是只有 SQL 回填层看得见。
                    context=_file_ctx)
                real = [o for o in obs
                        if o.get("ok") and o.get("data") is not None]
                data = (real[-1].get("data") if real else None)
            if _scene_kind == "scene" and data is not None:
                # fan-out 闸门（消费侧确定性拒乘爆兜底）：复用回填出的 SQL 即便
                # 过了上面的字面量守卫，这里用实查行数再兜一道——跨一对多 JOIN
                # 放大且做聚合的乘爆 SQL 绝不交出去（原版 critic 删后的确定性防线）。
                # 注：字面量守卫已在回填后执行前（:1044）逐条过过，复用路径不会再
                # 带幻觉取值；做题兜底已删（2026-10-25），此处无需再为现场写补闸。
                _fwhy = _fanout_guard(code, str((_prof or {}).get("db_path") or ""))
                if _fwhy:
                    data = None
                    reuse_errors.append(
                        f"复用 SQL 被 fan-out 闸门拒用（JOIN 乘爆）：{_fwhy[:140]}")
                # 场景库形态的真空防线（与 local 分支 _payload 同口径）：
                # 0 行的查询也返回 {rows: []} —— 不挡的话「0 行 + 模型一段
                # 文字」会判 written，还把 `WHERE 表达式='y=2x-1'` 这种把
                # 答案字面量当查询条件的坏配方写进技能库（2026-10-22 K 实测）。
                elif not (data.get("rows") if isinstance(data, dict) else data):
                    data = None
                if data is not None and not skills:
                    # 无已学技能 → 现场 solve 出的 SQL：同一把对题判官（看全貌判
                    # 主体/口径/完整性）。这里**不重填**：solve 已用掉内层 3 轮
                    # 迭代，再回填性价比低 —— 直接当没答出来走学习线（宁可空手
                    # 也不交片面数）。有 skills 时上面已判过，不重复花钱。
                    _fit2 = _fits_judge(
                        question, code, data,
                        str((_prof or {}).get("db_path") or ""),
                        catalog=(_scene_skill_catalog(_scene_id)[0]
                                 if _scene_id else ""))
                    if not _fit2["ok"]:
                        data = None
                        reuse_errors.append(
                            f"整段 SQL 被对题判官退回：{_fit2['why'][:140]}")
        # ---- 维度覆盖守卫（合成题被单配方/单表答的确定性闸）----
        # 与 fan-out 闸同风格、同位置：不赌 LLM。本题拆成多维度（dims>=2）却只
        # 覆盖 1 张维度源表 → 合成题被单维度答掉（如"发多少工资"只答了基薪、
        # 漏津贴/加班）。命中即不交片面数：data 置空走下面的学习路由，并标未验证。
        _coverage_why = ""
        if data is not None:
            # 维度数优先用里程碑 scaffold 的权威拆分（导入式里程碑题）；否则回退
            # _derive_dims 的 dims——自由提问无骨架时不误伤原子题。
            _nd = len(dims)
            try:
                import maieutic_scaffold as _ms
                _msd = _ms.milestone_dimensions(_scene_id, question)
                if len(_msd) >= 2:
                    _nd = len(_msd)
            except Exception:
                pass
            # ⚠️ 原来这里还有一条 `dims>=2 却只执行到 1 张源表 → 拦` 的硬规则，
            # 2026-10-28 删掉：维度 ≠ 表。单表多维度题（如上传表里
            # 「技术部(维1) 2026Q3(维2) 的奖金(维3)」全在 up_季度奖金 一张表）
            # 被它误判成「组成部分不齐」，明明一条 SQL 就答全了。真正的判据
            # 是「本题各维度技能所涉源表」覆盖没覆盖（_answer_is_partial 的第
            # 二段：同一个源 = 谈不上组合漏维），加上 _fits_judge 的全貌判官。
            _partial, _cwhy = _answer_is_partial(code, _ans_skills, _nd)
            if _partial:
                _coverage_why = _cwhy
                data = None
        # 没取到 → 还是没学会。同样**不自动后台学**（2026-10-19 用户改设计）：
        # 这条问题已在场景学习账上（coach_log 判读 = 没答出来），人点驾驶舱
        # 「▶ 启动 Voyager」时 scene_learn.py 会排到它 —— 每轮落 run_log 可见。
        if data is None and _use_env:
            # 自动拉起学习（2026-10-28 用户改设计，原 2026-10-19「只人工触发」
            # 作废）：「发送新的对话内容，就顺带拉取一个新的学习流程」—— 同构
            # R1「答不了就进入思考」：MVE 答不了数据题就**进入学习**，回复里
            # 明说「正在学」。kick_learning 自带同题去重，不怕连问。
            try:
                import learn_orchestrate as _lo2
                _kk = _lo2.kick_learning(
                    question, _scene_id,
                    reason="消费没答出来（未学/口径不符/真空）—— 新对话顺带拉起学习")
                learn_kick = {
                    "kicked": bool(_kk.get("kicked")),
                    "manual": not bool(_kk.get("kicked")),
                    "why": ("已自动拉起后台学习（学完再问同一句即可复用）"
                            if _kk.get("kicked") else
                            f"自动学习没拉起：{_kk.get('why') or '未知'}——"
                            "可在驾驶舱 ▶ 启动 Voyager 人工触发")}
            except Exception as _ke:
                learn_kick = {"kicked": False, "manual": True,
                              "why": f"自动学习拉起失败：{type(_ke).__name__}: {_ke}"}
        elif data is None:
            learn_kick = {"kicked": False, "manual": True,
                          "why": ("规划里没有 db 路（工具线题）：联网结果按 claim "
                                  "处理，不入库、不计学习曲线"
                                  if _plan.get("web") else
                                  "规划里没有 db 路（概念/解释类），无需学习流程")}
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
                _nar_note = f"数据源：{_scene_why}"
                if len((reused_skill or {}).get("composed_of") or []) > 1:
                    # 多技能编排：各条目是同一总量的组成部分，授权叙事层相加
                    # （用户 2026-10-25：「检索到的技能取到数字，给叙事 LLM
                    # 相加或者做什么，也算能力」）。
                    _nar_note += ("；下面各「来源技能」条目是这句话问的总量的"
                                  "组成部分，允许相加汇总，逐个来源编号都标上")
                    # 合计**已经算好**（在 _do_merge 里，确定性算术）：这里只
                    # 把结果和「哪些是钱」告诉叙事层，不让它自己挑、自己加。
                    # 2026-10-28 实测：让它自己加 → 三次把合计写成其中一项
                    # （133500 而不是 137100），还把小时数当钱加成 134903.5。
                    if _total is not None:
                        _kv_txt = " + ".join(f"{_v:,.2f}" for _n, _v in _money_kv)
                        _nar_note += (f"；各来源金额相加 = {_total:,.2f}"
                                      f"（{_kv_txt}）—— 题面问的是合计/总额时"
                                      f"**必须**用这个数，不许只报其中一项，"
                                      f"也不许自己另算一个合计")
                    else:
                        _nar_note += ("；数据里没有可相加的金额（各来源不是钱，"
                                      "或只有一项）—— 题面问合计时如实说算不出来")
                _nar_note += ("；⚠️ 值的单位是数量/时长（不是货币）时，绝不许"
                              "把它叙述成某个货币金额 —— 数据源里没有换算"
                              "比率，这笔钱就不存在")
                nar = _nar.narrate_answer(question, data,
                                          note=_nar_note)
            except Exception as e:      # 连 import 都失败也不许影响取数结果
                nar = {"ok": False, "why": f"{type(e).__name__}: {e}"}
            if nar.get("ok") and str(nar.get("answer") or "").strip():
                answer = str(nar["answer"])
                # ---- R1 同构：叙述层数字对账（rule-based，不花 LLM）----
                # R1 不给叙述设判官，只 rule-based 判**最终答案**：数字必须
                # 能从证据逐字指认（boxed 匹配同构）；不过 → 重采样一次
                # （rejection sampling 同构）→ 仍不过就降级原始数据平铺。
                # 83240.5 事故（20.5 小时被叙述成「20.5 元加班费」）死在这关。
                _sum_auth = (len((reused_skill or {}).get("composed_of") or []) > 1
                             or len(_executed) > 1 or len(_used_skills) > 1)
                _ok_n, _why_n = _nar.verify_answer_numbers(
                    answer, data, sum_auth=_sum_auth)
                if not _ok_n:
                    _nar_note2 = (_nar_note
                                  + "；⚠️ 上一版答案里有数据对不上的数字（"
                                  + _why_n[:90] + "）——只准原样引用数据里出现"
                                    "的数字；合计只准相加金额列的值，"
                                    "时长/数量列的值不许当金额")
                    _a2 = ""
                    try:
                        _nar2 = _nar.narrate_answer(question, data,
                                                    note=_nar_note2)
                        _a2 = str(_nar2.get("answer") or "").strip()
                    except Exception:
                        _nar2 = {}
                    if _nar2.get("ok") and _a2:
                        _ok2, _why2 = _nar.verify_answer_numbers(
                            _a2, data, sum_auth=_sum_auth)
                        if _ok2:
                            answer = _a2
                            nar = _nar2
                    if not (answer and _nar.verify_answer_numbers(
                            answer, data, sum_auth=_sum_auth)[0]):
                        # 重采样仍不过 → 降级：原始数据平铺（可核、无修辞），
                        # 叙述作废并留痕 —— 展示层只给可验证的结论（R1：
                        # think 隐藏、只展示 answer 的同构）。
                        try:
                            answer = json.dumps(data, ensure_ascii=False,
                                                indent=1)[:4000]
                        except Exception:
                            answer = str(data)[:4000]
                        try:
                            nar.setdefault("caveats", []).append(
                                "⚠️ 未验证：叙述层数字与数据对不上（"
                                + _why_n[:120] + "），已退回原始数据平铺")
                        except Exception:
                            pass
            # ---- 合计类问句的确定性终判（R1 同构：rule-based 判最终结论）----
            # 叙事层的活儿只剩「把话说顺」：合计对不对，不再由它决定。
            # 2026-10-28 三轮实测它把合计写成其中一项（133500 vs 137100），
            # 还有一轮自己加了小时数（134903.5）—— 代码算好的数它不引用，
            # 那就换成代码拼的那句。数字逐项可指认，谁也没得编。
            if (_total is not None
                    and not _mentions_value(answer, _total)):
                _by_code = _total_answer(_total, _money_kv)
                try:
                    nar["caveats"] = list(nar.get("caveats") or []) + [
                        "合计由代码相加得到（叙事层没引用这个数，已改用代码结论）"]
                    nar["answer"] = _by_code
                except Exception:
                    nar = {"ok": True, "answer": _by_code,
                           "caveats": ["合计由代码相加得到（叙事层没引用它）"]}
                answer = _by_code
        # 这段代码到底调了哪些抓手（披露用；不含纯计算，见 `_tools_in_code`）
        _mve_traj = _tools_in_code(code, _scene_id)
        # 完成判定（2026-10-25 用户拍板）：消费侧 **LLM critic 判定删除**——
        # 消费不走长期学习链路，只是技能库索引重放；每次重放再过一遍判官，
        # 结果就是「一直都不会掌握」。判定收敛为确定性的「取到真数」：
        # 真空仍拦（空 rows / 无结果），语义质量防线回到学习侧入库闸
        # （task_critic + _sql_literal_guard，原样保留）。
        critic_ok = data is not None
        critic_why = ("" if critic_ok
                      else "没取到真数（真空/无结果）——消费侧确定性判据")
        # 维度覆盖守卫：合成题被单表答 → 即便取到了数也标未验证，不交片面数。
        if _coverage_why:
            critic_ok = False
            critic_why = _coverage_why
        # 判未完成 → 答案不许以确定句示人（2026-10-24 用户拍板）：
        # 130600 事故三次同款——账面上 fetched_empty、面板上却是自信数字，
        # 展示层必须跟账本一致：判没答出来就明说「未验证、数字不可信」。
        if answer and data is not None and not (critic_ok
                           and nar.get("answered", True)):
            answer = (str(answer).rstrip()
                      + "（⚠️ 未验证：本轮未通过 critic 判定，此数字不可信）")
        # 未学（检索不到已学技能）：不答、只标记交给课程规划，明确告知人。
        # ⚠️ 有上传文件当上下文时**不写"未学"**：文件就在手里，DeepSeek
        # 客户端拿到文件也照样读（2026-10-28 用户：「上下文是上传文件」）。
        # 让它落到下面的 inference 分支按文件作答，并如实标注来源。
        # ⚠️ 规划里有 web 这一步时**不写"未学"**：这题的一半答案本来就在
        # 场景外（2026-10-29）—— 写"未学"会把「未学」当成库内半场去合成。
        if (data is None and not skills and _use_env
                and not _file_ctx and not _plan.get("web")):
            answer = ("⚠️ 未学：这道题还没学过，检索不到已学配方 —— 不编数。"
                      + ("已自动拉起后台学习，学完再问同一句即可复用。"
                         if learn_kick.get("kicked") else
                         "学习未自动拉起，可在驾驶舱 ▶ 启动 Voyager 人工学。"))
        # ---- 句句有回复（R1 客户端同构，2026-10-28）----
        # 三层，按题型分流（用户截图两事故的收敛：
        #   ①「什么是强化学习」句句无回复；② 工资题被答成津贴还判 written）：
        #   · 内部数据题（题面找数 + 路由命中本场景）答不出 → 绝不联网、绝不
        #     推断 —— 联网搜"技术部工资"只会搜到垃圾，模型推断就是编数；
        #     如实交代判官结论 + 已派学习（上面已 kick）。
        #   · 非数据题（是什么/为什么）→ 先 inference（同 DeepSeek 客户端：
        #     联网默认关，知识题模型直接答，标「模型推断」）；模型自评不知 →
        #     工具线 claim。
        #   · 外部时变数据题（路由没命中本场景的"多少"题，如汇率）→ 工具线。
        tool_line: dict[str, Any] = {}
        _internal_hit = (str(route_info.get("decision") or "")
                         in ("HIGH_CONFIDENCE", "MEDIUM_CONFIDENCE")
                         or bool(skills))
        _no_reply = (data is None and not str(answer or "").strip())
        # ⚠️ DeepSeek 客户端的优先级只有一条：**这次请求带了什么上下文，
        # 就按什么答**。库/技能取到数 → 用库（结构化、可复现，更好）；
        # 库取不到数 → 文件就在上下文里，读它，并如实标注「按你上传的文件
        # 作答（未经库内验证）」。之前把条件收窄成「没有技能才准用文件」，
        # 实测（2026-10-28 截图）：场景 G 文件已注入、但路由命中旧图谱节点
        # → 宁可 fetched_empty 也不读文件 —— 那不是谨慎，是没同构。
        # （残留上传顶替正确答案的问题已在源头解决：过期上传连表清掉。）
        _file_as_source = bool(_file_ctx)
        if (_no_reply and _use_env and not _plan.get("web")
                and _internal_hit and not _file_as_source):
            pass                       # 上面已有「未学/不会」交代 + 已派学习
        elif _no_reply:
            # 有上传文件 → 数据题也先读文件（文件就是上下文）；没文件才只答
            # 知识题。标注分两种：按文件作答 vs 模型推断，别混成一句。
            if _file_as_source or not _use_env:
                _inf = _inference_answer(question, context=_file_ctx)
                if _inf.get("used"):
                    answer = (str(_inf["answer"]).rstrip()
                              + ("（⚠️ 按你上传的文件作答：内容来自文件本身，"
                                 "未经库内验证；不入库、不计入学习曲线）"
                                 if _file_as_source else
                                 "（⚠️ 模型推断：未从环境取数验证；不入库、"
                                 "不计入学习曲线）"))
                    inference_used = True
            if not str(answer or "").strip():
                # 斯坦福 Generative Agents 同构（2026-10-29 拍板）：计划里
                # 有 web 这一步才联网 —— 计划外的动作不执行；规划员挂掉
                # （planned=False）→ 退回旧行为，兜底不会比现状更差。
                if _plan.get("planned") and not _plan.get("web"):
                    tool_line = {"used": False, "answer": "", "sources": [],
                                 "note": "规划里没有联网这一步（计划外的动作不执行）"}
                elif not _web_reachable(_scene_id)[0]:
                    _wn = _web_reachable(_scene_id)[1]
                    tool_line = {"used": False, "answer": "", "sources": [],
                                 "note": ("规划里有联网步骤，但本场景的联网工具当前"
                                          f"不可达（{_wn}）—— 外部信息如实缺失，"
                                          "不编数")}
                else:
                    try:
                        tool_line = await _tool_line_fallback(question, context=_file_ctx)
                    except Exception as e:  # pragma: no cover
                        tool_line = {"used": False, "answer": "", "sources": [],
                                     "note": (f"工具线异常：{type(e).__name__}: "
                                              f"{str(e)[:140]}")}
                    if tool_line.get("answer"):
                        answer = (str(tool_line["answer"]).rstrip()
                                  + "（⚠️ 工具线 claim：来自联网资料，未经库内验证；"
                                    "不进能力账、不计入学习曲线）")
        if _no_reply and not str(answer or "").strip():
            # 兜底的兜底：inference / 工具线都没产出 —— 也必须句句有回复，
            # 判官结论与局限原样交代（用户 2026-10-28：判官可以看到局限性）。
            _lim = (str(critic_why or "")
                    or "；".join(str(x) for x in reuse_errors[-2:])
                    or str((tool_line.get("note") or "")))
            answer = ("这题这轮没答出来 —— 不编数。"
                      + (f"判官结论：{_lim[:220]} " if _lim.strip() else "")
                      + ("已自动拉起后台学习。" if learn_kick.get("kicked")
                         else "可到面板查看判定明细。"))
        # ---- 对比题的 web 半场（2026-10-29 拍板）----
        # db 半场答出了、规划里还有联网这一步 → 执行联网半场补「场景外」
        # 那一半（如「待遇好不好」= 库内 137100 + 外部行情对比）。可达才
        # 执行；不可达如实缺失。claim 只追加在答案尾部：不入库、不计入
        # 学习曲线（时变数据入库即污染，同 75120 教训）。
        if (answer and _plan.get("planned") and _plan.get("web")
                and not tool_line.get("used") and not tool_line.get("answer")):
            _web_ok, _web_names = _web_reachable(_scene_id)
            if _web_ok:
                try:
                    tool_line = await _tool_line_fallback(question,
                                                          context=_file_ctx)
                except Exception as e:  # pragma: no cover
                    tool_line = {"used": False, "answer": "", "sources": [],
                                 "note": (f"工具线异常：{type(e).__name__}: "
                                          f"{str(e)[:140]}")}
                if tool_line.get("answer"):
                    _db_part = str(answer).rstrip()
                    _wpart = str(tool_line["answer"]).rstrip()
                    # 规划负责合成（2026-10-29）：两半都有材料 → 让 LLM 对照
                    # 给出结论；合成失败退回机械拼接（如实分段，不假装合成）。
                    _syn = _compose_halves(question, _db_part, _wpart)
                    if _syn:
                        answer = (_syn + "\n\n—— 口径 ——\n"
                                  "库内半场：已验证取数（可复现）；"
                                  "外部半场：联网 claim（未验证、不入库、"
                                  "不计入学习曲线）")
                    else:
                        answer = (_db_part
                                  + "\n\n—— 外部信息（联网检索） ——\n"
                                  + _wpart
                                  + "（⚠️ 工具线 claim：来自联网资料，未经库内"
                                    "验证；不进能力账、不计入学习曲线）")
            else:
                tool_line = {"used": False, "answer": "", "sources": [],
                             "note": ("规划里有联网步骤，但本场景的联网工具当前"
                                      f"不可达（{_web_names}）—— 外部信息如实"
                                      "缺失，不编数")}
        base = {
            "question": question, "mode": "voyager_consume",
            "topic_id": topic_id, "topic_hit": hit,
            "source": "human_import", "validated_target": False,
            "root_scene": _scene_id, "scene_kind": _scene_kind,
            # 作用域 + 工具线披露（2.5/第 4 步）：场景分支不跑 VLML 裁判对照，
            # referee_compared 恒 False（面板显示「未对照」而非「有硬伤」）。
            "scope_line": ("tool" if (_plan.get("planned") and _plan.get("web")
                               and not _plan.get("db"))
                              else _scope_line(_scene_id)),
            "referee_compared": False,
            "tool_line_used": bool(tool_line.get("used")),
            "tool_sources": (tool_line.get("sources") or [])[:5],
            "tool_line_note": str(tool_line.get("note") or "")[:200],
            # 规划披露（2026-10-29）：这一问规划了哪几路 + 规划员的一句话
            # 理由。规划≠可达，web 实际执行结果看 tool_line_note。
            "plan_routes": {k: bool((_plan or {}).get(k))
                            for k in ("db", "web", "file", "infer")},
            "plan_why": (str((_plan or {}).get("why") or "")[:160]
                         + ("" if _plan.get("planned") is not False
                            else "（规划员不可用：不碰环境、不联网，仅模型推断）"))[:200],
            # inference 兜底（句句有回复）：模型推断层用没用 —— 与工具线
            # claim 同级的披露，面板/对话页据此挂「模型推断」标注。
            "inference_used": bool(inference_used),
            "route_decision": str(route_info.get("decision") or ""),
            "route_score": int(route_info.get("max_score") or 0),
            "route_matched_on": str(route_info.get("matched_on") or ""),
            "route_node": str(route_info.get("node_id") or ""),
            "learn_kicked": bool(learn_kick.get("kicked")),
            "learn_kick_note": str(learn_kick.get("why")
                                   or learn_kick.get("reason") or ""),
            "miss_node": str(miss_info.get("node_id") or ""),
            "miss_node_created": bool(miss_info.get("node_created")),
            "milestone_target_set": bool(miss_info.get("milestone_target_set")),
            "suggest_new_scene": bool(route_info.get("max_score", 0) == 0),
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
            # 多技能编排：把**每条**执行到的已学技能都披露出来（原版同构：
            # 注入的 skills 就是 agent 实际可调用的集合）；单技能复用仍走
            # reused_skill 兜底，口径不变。
            "used_skills": ([{"topic": s.get("topic"), "name": s.get("name"),
                              "key": s.get("key")} for s in _used_skills]
                            if _used_skills else
                            ([{"topic": reused_skill.get("topic"),
                               "name": reused_skill.get("name"),
                               "key": reused_skill.get("key")}]
                             if reused_skill else [])),
            "reused": bool(reused_skill), "graph_link": {},
            # 复用阶段被真空判据挡下的每一条，都留下证据（不静默跳过）
            "reuse_errors": reuse_errors,
            # 远端工具接入情况（exposed = 这次能用上的 mcp_*；没连上如实带原因）
            "mcp_tools": mcp_ready,
            # 有行 ≠ 答出来：critic 判没答出来（含叙事层 answered=false 的情形）
            # 一律 fetched_empty —— 错语义数据不能假解锁（2026-10-23 Scene P）。
            "critic_note": "" if critic_ok else str(critic_why or "")[:300],
            "judgment_result": (
                "written" if (data is not None and critic_ok
                              and nar.get("answered", True))
                else "fetched_empty"),
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
        # 用户 2026-10-25 新边界（消费只编排不合成）：`_absorb` **只记录本次
        # 真正复用的那条已学技能用量**——`reused_skill is None`（现场兜底现做 /
        # 未学）一律不调 `_absorb`，消费侧永不写新技能。否则兜底现做会落
        # `consume_<md5>` 脏账，下次又被检回「现做」，正是此前「清库了还是
        # 现做」的根因。学习侧（learn()）才写技能，消费只调答案出来编排。
        abs_info: dict[str, Any] = {}
        # critic 判没答出来 / answered=false（叙事层自检）→ 不进能力库 ——
        # 有行 ≠ 答出来（2026-10-23 Scene P：fail 的 task 却录入了技能）。
        # 且必须「真复用了一条已学技能」才记录（消费不写新技能）。
        if (data is not None and critic_ok and nar.get("answered", True)
                and reused_skill is not None):
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
                # 多技能编排（2026-10-26）：执行到的每条来源配方都点名，
                # 别再只显示第一条的 key 让人误以为只用了 1 条。
                _comp = list((reused_skill or {}).get("composed_of") or [])
                _desc = ("配方 " + "、".join(f"`{n}`" for n in _comp)
                         if len(_comp) > 1 else
                         f"配方 `{reused_skill.get('key')}`")
                _where = (f"第 {_cnt} 次导入复用了 {_from_imp or '之前导入'} "
                          f"学到的{_desc}"
                          if _cnt > 1 else
                          f"第 1 次导入，命中已有{_desc}"
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
            "milestone_target_set": bool(miss_info.get("milestone_target_set")),
            "suggest_new_scene": bool(route_info.get("max_score", 0) == 0),
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
        import db_switch as _dbs_r
        _def_db = str(getattr(_dbs_r, "DEFAULT_DB", "") or "")
        r = _refill_sql(question, skills, schema, db_path=_def_db)
        # 模型自己判定"配方算的量与这句话问的不是同一个量" → **不执行它**，
        # 直接现场写。宁可现做，也不许拿一个张冠李戴的 SQL 去取数。
        # 回填产物同样过字面量守卫（与场景分支同一把尺子，2026-10-24）。
        _gwhy = _sql_literal_guard(r["sql"], _def_db) if (r["reused"] and r["sql"]) else ""
        if r["reused"] and r["sql"] and not _gwhy:
            sql, reused, note = r["sql"], True, r["note"]
            observations.append(await env.run_sql(sql))
        elif _gwhy:
            note = f"配方回填被字面量守卫拒用：{_gwhy}"
            print(f"  （{note[:100]}）→ 降级现场写")
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
        if not _use_env:
            # 规划没排 db 路（或规划员不可用+词表判知识题）→ 不走环境。
            # act.solve 的 3 轮迭代实测要 80s+，对「什么是强化学习」纯属
            # 浪费；落到下面统一的 inference/工具线兜底，句子必有回复。
            observations, sql = [], ""
        else:
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
            _solve_task = question
            if (_plan.get("planned") and _plan.get("db")
                    and _plan.get("web")):
                # 规划裁剪（2026-10-29 拍板「先规划、再按规划逐路执行」的落实）：
                # 对比题的 db 半场只要求算出**库内**能指认的量，与外部对象的
                # 对比留给联网半场。原题直接进内层 = 判官拿「好不好」对库判
                # 闭合，永远闭不上（实测 3 轮全拒、0 行收场）。
                _solve_task = (question + "\n（规划：这是内部+外部的对比题，"
                               "本轮只做 db 半场 —— 用场景数据源把**库内**能"
                               "指认的量算出来（口径必须能落到真实表/列）；"
                               "与外部对象的对比由联网半场负责，不要求本轮在"
                               "库里找到，不构成拒判理由。）")
            observations, sql = await act.solve(_solve_task, max_iters=3,
                                                grabs=grabs)

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
        # 自动拉起学习（2026-10-28 用户改设计，与场景分支同款）：答不了数据题
        # 就**进入学习**（同构 R1「答不了就进入思考」），回复里明说「正在学」。
        if _use_env:
            try:
                import learn_orchestrate as _lo3
                _kk = _lo3.kick_learning(
                    question, _scene_id,
                    reason="消费没答出来（VLML 侧真空/未学）—— 新对话顺带拉起学习")
                learn_kick = {
                    "kicked": bool(_kk.get("kicked")),
                    "manual": not bool(_kk.get("kicked")),
                    "why": ("已自动拉起后台学习（学完再问同一句即可复用）"
                            if _kk.get("kicked") else
                            f"自动学习没拉起：{_kk.get('why') or '未知'}")}
            except Exception as _ke2:
                learn_kick = {"kicked": False, "manual": True,
                              "why": f"自动学习拉起失败：{type(_ke2).__name__}: {_ke2}"}
        else:
            learn_kick = {"kicked": False, "manual": True,
                          "why": ("规划里没有 db 路（工具线题）：联网结果按 claim "
                                  "处理，不入库、不计学习曲线"
                                  if _plan.get("web") else
                                  "规划里没有 db 路（概念/解释类），无需学习流程")}

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

    # ---- 完成判定（2026-10-25 用户拍板，同场景分支）------------------------
    # 消费侧 LLM critic 判定删除：消费不走长期学习链路，只是技能库索引重放；
    # 每次重放再过一遍判官 → 「一直都不会掌握」。判定收敛为确定性「取到真数」
    # （real_calls = ok 调用里含真值的那些，(NULL,0) 空聚合行不算数）。
    # 学习侧入库闸（task_critic + _sql_literal_guard）原样保留。
    critic_ok = bool(real_calls)
    critic_why = ("" if critic_ok
                  else "取到真空（ok 调用里没有一行真数）——消费侧确定性判据")
    # ---- 2.5 口径防线（2026-10-28）：裁判对照从「仅供参考的披露」升为「硬伤即拦」----
    # compare 只在两边都取到真行时才产出；空 compare 的 voyager_sound 旧代码
    # 写成 bool(None)=False —— 面板「做法有硬伤」其实是「裁判没跑成」，误导
    # （2026-10-28 消费实测 referee_rows=0 时三条信号全 False）。三态修正：
    # referee_compared=False = 未对照（旧 bool 字段此时无意义，面板改看三态）。
    # 裁判判出硬伤（voyager_sound=False）= 有行 ≠ 答出来：答案标「⚠️ 未验证」、
    # 不进能力库、不标 mastered —— 与「真空判未完成」同一条铁律的口径侧执行。
    referee_compared = bool(compare)
    if referee_compared and compare.get("voyager_sound") is False:
        critic_ok = False
        critic_why = ("Voyager 做法有硬伤（裁判对照）："
                      + str(compare.get("reason") or "")[:160]
                      + " —— 有行不等于答出来（口径侧）")
    elif referee_compared and compare.get("same_metric") is False:
        # 两边算的不是同一个量：多数是裁判自己跑偏（2026-10-14 拍板「裁判跑偏
        # ≠ Voyager 有硬伤」）→ 不拦 mastered，但答案带口径存疑披露。
        if answer:
            answer = (str(answer).rstrip()
                      + "（⚠️ 口径存疑：独立对照两边算的不是同一个量，数值请谨慎）")
    # 判未完成 → 答案标注未验证（与场景分支同款，2026-10-24 用户拍板）。
    if answer and not critic_ok:
        answer = (str(answer).rstrip()
                  + "（⚠️ 未验证：本轮未通过 critic 判定，此数字不可信）")

    # ---- 句句有回复（R1 客户端同构，与场景分支同一套三层分流）----
    tool_line: dict[str, Any] = {}
    _no_reply_v = not critic_ok and not str(answer or "").strip()
    if _no_reply_v and _use_env and not _plan.get("web"):
        # 内部数据题答不出：绝不联网、绝不推断（都是编数）—— 如实交代判官
        # 结论 + 学习状态（上面已 auto-kick）。
        _lim_v = str(critic_why or "")
        answer = ("这题这轮没答出来 —— 不编数。"
                  + (f"判官结论：{_lim_v[:220]} " if _lim_v.strip() else "")
                  + ("已自动拉起后台学习，学完再问同一句即可复用。"
                     if learn_kick.get("kicked") else
                     f"{learn_kick.get('why') or ''}"))
    elif _no_reply_v:
        if not _use_env:
            # 规划没排 db 路 → 先 inference（同 DeepSeek 客户端：联网默认关，知识题
            # 模型直接答，标「模型推断」）；模型自评不知 → 工具线 claim。
            _inf_v = _inference_answer(question, context=_file_ctx)
            if _inf_v.get("used"):
                answer = (str(_inf_v["answer"]).rstrip()
                          + "（⚠️ 模型推断：未从环境取数验证；不入库、"
                            "不计入学习曲线）")
                inference_used = True
        if not str(answer or "").strip():
            # 与场景分支同款：计划里有 web 这一步才联网（斯坦福同构）；
            # 规划员挂掉 → 退回旧行为。
            if _plan.get("planned") and not _plan.get("web"):
                tool_line = {"used": False, "answer": "", "sources": [],
                             "note": "规划里没有联网这一步（计划外的动作不执行）"}
            elif not _web_reachable(_scene_id)[0]:
                _wn = _web_reachable(_scene_id)[1]
                tool_line = {"used": False, "answer": "", "sources": [],
                             "note": ("规划里有联网步骤，但本场景的联网工具当前"
                                      f"不可达（{_wn}）—— 外部信息如实缺失，"
                                      "不编数")}
            else:
                try:
                    tool_line = await _tool_line_fallback(question,
                                                          context=_file_ctx)
                except Exception as e:  # pragma: no cover
                    tool_line = {"used": False, "answer": "", "sources": [],
                                 "note": f"工具线异常：{type(e).__name__}: {str(e)[:140]}"}
                if tool_line.get("answer"):
                    answer = (str(tool_line["answer"]).rstrip()
                              + "（⚠️ 工具线 claim：来自联网资料，未经库内验证；"
                                "不进能力账、不计入学习曲线）")

    # ---- 答完了 → 这题进能力库 → 掌握（判据是"答没答出来"，不是覆盖率）----
    # 判据是"答没答出来"（取到数 + 给出了回答 + **critic 判过**），**不是**有没有
    # 整理成结构化事实 —— 答案是一张表时压不进 facts，但那也是答出来了，照样进能力库。
    # ⚠️ "取到数"同样只认真数：真空结果上的"答完了"是假象，把它沉淀进能力库
    #   等于把坏配方洗成技能（实测 fb_conversion_analysis 就是这么来的）。
    absorbed = (_absorb(question, sql, topic_id)
                if (answer and real_calls and critic_ok) else {})
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
        # 三态披露（2.5）：False = 裁判根本没对照上（没跑成/有一路没取到真数），
        # 此时上面的 same_metric/voyager_sound 是 bool(None)=False 的占位，
        # **无意义** —— 面板必须看这个字段再决定信不信那两个 bool。
        "referee_compared": referee_compared,
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
        # 作用域判定（2.5）：这句话归哪条供给线（learn/tool/inference）——
        # 面板与对账用；工具线接入后（第 4 步）据此分流。
        "scope_line": ("tool" if (_plan.get("planned") and _plan.get("web")
                               and not _plan.get("db"))
                              else _scope_line(_scene_id)),
        # 工具线（第 4 步）：这轮有没有走联网兜底、取回了哪些来源、为什么没走成。
        "tool_line_used": bool(tool_line.get("used")),
        "tool_sources": (tool_line.get("sources") or [])[:5],
        "tool_line_note": str(tool_line.get("note") or "")[:200],
        # 规划披露（2026-10-29）：与场景分支同口径。
        "plan_routes": {k: bool((_plan or {}).get(k))
                        for k in ("db", "web", "file", "infer")},
        "plan_why": (str((_plan or {}).get("why") or "")[:160]
                     + ("" if _plan.get("planned") is not False
                        else "（规划员不可用：不碰环境、不联网，仅模型推断）"))[:200],
        # inference 兜底（句句有回复）：与场景分支同口径的披露。
        "inference_used": bool(inference_used),
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
        # ⚠️ 2026-10-23：critic 判没答出来（数据与问题对不上 / 空值 / 答非所问）
        #   按 fetched_empty 处理 —— 有行 ≠ 答出来，这句话不能假解锁。
        #   critique 落盘（critic_note），面板能看到"为什么不算答出来"。
        "critic_note": "" if critic_ok else str(critic_why or "")[:300],
        "judgment_result": (
            "fetched_empty" if (not real_calls or not critic_ok) else
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
                         "total_amount / avg_value / usage_rate 这种（优先取本题库"
                         "真实存在的数值列）。\n"
                         "     **不许写中文**（实测写成中文描述 → 裁判照它写的 SQL "
                         "只取了实体名、没取比值 → 一条事实都整理不出来 → 覆盖率落空）。\n"
                         "     **不许带 最高/最低/最多 这类排序词** —— 那是问法不是指标；"
                         "问「谁最高」时 dimension 就写那个指标本身。\n"
                         "  2. subject = **说的是谁**（本题实体的字段与真实取值，"
                         "如 {\"实体字段\":\"真实取值\"}），不要把「XX 最高」这种描述塞进来，"
                         "也不要把一个实体的值塞进另一个字段（实测串过一次）。\n"
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
          + "⚠️ 比值/除法必须写 NULLIF(x, 0)（如 AVG(数值列)/NULLIF(AVG(另一数值列),0)）—— "
            "DuckDB 里 x/0 得 NaN 不报错，取回来是个没法比的数；"
            "且优先在聚合层做除法（先 AVG 再相除），不要对逐行比值再 MAX。\n"
            "⚠️ 多表 JOIN 时列名必须带表名前缀（两表常有同名列，裸写会 Ambiguous）。\n"
            "⚠️ 跨「一对多」JOIN 做聚合会乘爆：父表一行被子表"
            "每行乘一次（父表一行匹配子表 2 行 → 父表数值列被算 2 次）。正确写法：先把"
            "右表按关联键聚合成单行再 JOIN（LEFT JOIN (SELECT 关联键, SUM(子表数值列)"
            " AS v FROM 子表 WHERE … GROUP BY 关联键) t ON "
            "t.关联键=父表.关联键），再 SUM(父表.数值列 + COALESCE(t.v,0))；或改用"
            "标量子查询 SUM(父表.数值列 + (SELECT COALESCE(SUM(子表.数值列),0) FROM 子表"
            " WHERE 子表.关联键=父表.关联键 …))。绝不要写"
            " SUM(父表.数值列 + 子表.数值列) FROM ... JOIN 子表。\n"
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


# 比较边界日期字面量（2026-10-26）：'2026-09-30' / '2026-09' / '2026/9/30'
_DATE_LIT_RE = re.compile(r"^\d{4}[-/]\d{1,2}([-/]\d{1,2})?$")


def _scope_line(scene_id: str) -> str:
    """作用域判定（mve/scope.py，2.5）。失败如实降级 inference，不假装判过。"""
    try:
        import scope as _scope_mod
        return str(_scope_mod.scope_line(scene_id))
    except Exception:
        return "inference"


def _sql_literal_guard(sql: str, db_path: str) -> str:
    """SQL 配方入库前的确定性守卫：字符串字面量必须能在 db_observation 实查
    清单里指认。返回拒绝原因；'' = 通过。

    原版同构（2026-10-24 落地的环境观察三方共用）：critic 的判据是「任务假设
    的表/列/业务参数必须能在观察里指认」。这里把其中**确定性可查**的部分做成
    代码闸门 —— 实测（Scene B 工资题 10:01）合成配方把 kind 写成 'food'/
    'transportation'（真实取值是 餐补/交通），学习侧判定放行、坏配方入库、
    复用即答 243320.83。LLM 判官有残余随机性，字面量对不对不该赌它。
    费率公式类幻觉（base/8）无法确定性证伪，仍靠判官（MVE_CRITIC_MODEL）。
    """
    if not str(sql or "").strip() or not str(db_path or "").strip():
        return ""
    try:
        from curriculum_auto import db_observation
        obs = str(db_observation(db_path) or "")
    except Exception:
        return ""                      # 实查不了 → 如实放行（判官仍在把关）
    if not obs:
        return ""
    lits = re.findall(r"'([^']*)'", str(sql))
    # 去掉字面量后的 SQL 正文：字面量若兼作标识符/函数参数名（如
    # date_trunc('month', month)），裸词仍出现 → 不算幻觉。
    bare = re.sub(r"'[^']*'", " ", str(sql)).lower()
    for lit in lits:
        t = lit.strip()
        if (not t or len(t) < 2 or "%" in t
                or t.lower() in ("true", "false", "null")):
            continue
        if t in obs or t.lower() in obs.lower():
            continue
        if t.lower() in bare:
            continue
        # 比较边界日期放行（2026-10-26）：`hire_date <= '2026-09-30'` 这类
        # 范围谓词的右值是**查询边界**，语义上允许不在数据取值域内（库里
        # 存的是员工入职日，月末 cutoff 不在任何一行里）——把它当幻觉拦掉
        # 会把 total_base_salary 这种核心配方整条废掉（实测：回填全被拒，
        # 总支出答案缺了 base 大头）。等值过滤（=/IN）的日期仍必须指认：
        # 那才是「假设了一个库里没有的取值」。
        if _DATE_LIT_RE.match(t):
            _boundary = True
            # BETWEEN 的右边界前面是 AND（BETWEEN a AND b），仅在 SQL 确有
            # BETWEEN 时把 AND 也认作边界前缀，避免误放普通等值组合。
            _has_between = bool(re.search(r"\bbetween\b", str(sql), re.I))
            _bounds_re = (r"(<|<=|>=|>|between|and)\s*$" if _has_between
                          else r"(<|<=|>=|>|between)\s*$")
            for _m in re.finditer(re.escape(lit), str(sql)):
                _left = str(sql)[max(0, _m.start() - 10):_m.start()]
                # 截窗尾可能带着字面量的开引号（…<= '2026-…），剥掉再判边界
                _left = _left.rstrip().rstrip("'").rstrip()
                if not re.search(_bounds_re, _left, re.I):
                    _boundary = False
                    break
            if _boundary:
                continue
        return (f"SQL 字面量 '{t}' 在数据源实查清单里指认不了"
                f"（库里没有这个取值）—— 大概率是幻觉取值，拒绝入库")
    return ""


def _rate_guard(sql: str, db_path: str = "") -> str:
    """费率守卫（确定性）：SQL 里**乘以常量** = 假设了一个库里不存在的费率。

    铁律同源（出题器规则 8 / 记忆铁律「库里没有的费率/汇率/税率不许假设」）。
    2026-10-28 实测：`SUM(overtime_hours) * 100 AS total_overtime_pay` 跑通并
    进了「公司一共要支付多少钱」的答案 —— 100 元/小时是编的，库里只有
    overtime_hours（小时数），没有任何费率列。字面量守卫只看 WHERE 里的
    取值，管不到 SELECT 表达式里的乘数，这里补上。

    判据：乘号旁的数字常量，必须能在数据源实查清单里逐字指认；指认不了
    就是假设的费率/系数，拒。0/1（恒等/取整）豁免。
    """
    _s = str(sql or "").strip()
    if not _s or "*" not in _s:
        return ""
    lits = [a or b for a, b in re.findall(
        r"\*\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*\*", _s)]
    if not lits:
        return ""
    ground = _catalog_block(db_path, 20000) or ""
    bad: list[str] = []
    for lit in lits:
        try:
            if float(lit) in (0.0, 1.0):
                continue
        except ValueError:
            continue
        if not re.search(r"(?<![\d.])" + re.escape(lit) + r"(?![\d.])", ground):
            bad.append(lit)
    if not bad:
        return ""
    return (f"SQL 出现乘以常量 {','.join(bad)} —— 这是假设的费率/系数"
            f"（数据源实查清单里指认不了）。库里没有的费率不许假设；"
            f"该量不可计算就如实不答")


# ---- 金额 / 数量 的单位语义（确定性，只认**数据层的列**，不认题面词表）----
# 2026-10-28 用户截图事故链（总额交 133500、还冒出「43.50 元加班费」）的总
# 根因是**别名会撒谎**：配方写 `SUM(overtime_hours) AS overtime_pay` —— 输出
# 列名看着像钱，聚合的**来源列**其实是小时。所以判据只能落在「聚合的来源列」
# 上，绝不能落在别名上；别名一信，43.5 小时就被当成 43.5 元加进公司总额。
#
# ⚠️ 用户 2026-10-28 再次拍板：**不允许硬编码**。所以这里只做两件纯机制的事
# ——① 把别名还原成真实来源列；② 数量列的值不进合计。判「这题问的是不是钱」
# 「该不该相加」一概不靠题面词表：数据里给了什么就是什么，业务口径交给
# `_fits_judge` 看全貌判。
#
# `_QTY_UNITS` 是**单位**词（hours/天数/次数），不是业务词（部门/津贴/工资）：
# 库里没有单位元信息，列名是唯一可确定性读取的证据源。
_QTY_UNITS = ("hours", "days", "count", "num", "qty", "小时", "天数",
              "次数", "数量")


def _qty_outputs(sql: str) -> dict[str, str]:
    """SQL 投影里「聚合来源列是数量/时长」的输出列 → 真实来源列。

    `SUM(overtime_hours) AS overtime_pay` → `{"overtime_pay": "overtime_hours"}`。
    这张表就是「别名不撒谎」的执行处：合并/合计/叙事都靠它把「看着像钱的
    小时数」打回非金额。解析不到别名时用来源列本身当键。
    """
    _s = str(sql or "")
    out: dict[str, str] = {}
    for m in re.finditer(
            r"\b(?:SUM|AVG)\s*\(\s*([A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff.]*)"
            r"\s*\)\s*(?:AS\s+([A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]*))?",
            _s, re.I):
        col = m.group(1)
        alias = m.group(2) or ""
        if not any(u in col.lower() for u in _QTY_UNITS):
            continue
        if alias.upper() in ("FROM", "WHERE", "GROUP", "ORDER", "HAVING",
                            "LIMIT", "AND", "OR"):
            alias = ""
        out[alias or col] = col
    return out


def _entry_pairs(entry: dict[str, Any]) -> list[tuple[str, Any]]:
    """一条执行结果的首行 (列名, 值) —— dict 行与 list 行两种返回形态都要认。

    ⚠️ 2026-10-28 实测坑：`run_sql` 的 rows 多数是 **list**（列名在 columns
    里），只按 dict 取 key 会让「合计」整段算不出来（合计注进 note 里，但
    data 里没有那个数 → 叙述对账判它指认不了 → 整题退化成原始 JSON）。
    """
    _d = entry.get("data") or {}
    _rows = _d.get("rows") or []
    if not _rows:
        return []
    _r0 = _rows[0]
    if isinstance(_r0, dict):
        return [(str(k), v) for k, v in _r0.items()]
    _cols = [str(c) for c in (_d.get("columns") or [])]
    if _cols and isinstance(_r0, (list, tuple)) and len(_r0) == len(_cols):
        return list(zip(_cols, _r0))
    return []


def _money_parts(executed: list[dict[str, Any]]) -> list[tuple[str, float]]:
    """各条目里「这笔钱」的值：跳过数量/时长列（按来源列判，不按别名）。

    返回 [(来源技能名, 金额)]。同一来源只取第一个金额列（一条配方 = 一笔账）。
    """
    out: list[tuple[str, float]] = []
    for _e in (executed or []):
        _q = _qty_outputs(str(_e.get("sql") or ""))
        for _k, _v in _entry_pairs(_e):
            _kl = str(_k)
            if any(u in _kl.lower() for u in _QTY_UNITS):
                continue
            if _kl in _q:                       # 别名是钱、来源列是小时 → 跳过
                continue
            if isinstance(_v, bool):
                continue
            try:
                _fv = float(_v)
            except (TypeError, ValueError):
                continue
            out.append((str(_e.get("name") or ""), _fv))
            break
    return out


# 「合计」不再靠问句关键词触发：多技能编排（composed_of>1）本身就是「这组
# 条目是同一个总量的组成部分」（既有设计语义，2026-10-25 用户拍板），数据里
# 有 ≥2 个金额来源就该有合计。代码只算数，不猜题意。


def _total_answer(total: float, parts: list[tuple[str, float]]) -> str:
    """合计的确定性答法（代码拼，不经 LLM）：数字可逐项指认，不会被改口径。"""
    _items = "、".join(f"{_n} {_v:,.2f} 元" for _n, _v in parts if _v)
    return f"合计 {total:,.2f} 元（{_items}）"


def _mentions_value(answer: str, value: float) -> bool:
    """答案里说到这个数没有（与叙述对账用同一把剥时间的尺子）。"""
    try:
        import narrator as _n
        return bool(_n.mentions_value(answer, value))
    except Exception:
        # 叙事模块不可用（缺依赖等）不该让整题挂掉：退回朴素包含判断
        _s = re.sub(r"[,，\s]", "", str(answer or ""))
        for cand in (f"{value:.2f}", f"{value:.1f}", str(int(value))):
            if cand.lstrip("0") and cand in _s:
                return True
        return False


# 单位守卫 _unit_guard 已删（2026-10-29 用户拍板「删」）：代码里不再写任何
# 数据口径/语义词表 —— 题面问的是什么口径、结果算不算答对，一律交给看全貌
# 的对题判官（_fits_judge）。防「SUM(数量列) 当钱」的确定性闸退役；兜底仍是
# 字面量守卫（取值真伪）+ 费率守卫（乘常量=假设参数）+ 判官三层。
def _catalog_block(db_path: str, limit: int = 4000) -> str:
    """库的全貌（实查表/列/真实取值）——给 agent 看，而不是给守卫写词表。

    2026-10-28 用户拍板方向：「不能这样写死守卫解决，而是…让 agent 看到
    全貌」。全貌的来源就是原版环境观察面（curriculum_auto.db_observation，
    出题器/攻略层/critic 三方共用的同一份实查渲染）。
    """
    if not str(db_path or "").strip():
        return ""
    try:
        from curriculum_auto import db_observation
        return str(db_observation(str(db_path)) or "")[:limit]
    except Exception:
        return ""


def _scene_skill_catalog(scene_id: str, limit: int = 40) -> tuple[str, list[dict]]:
    """技能库全貌：(给模型看的目录文本, 全部技能 dict)。

    ⚠️ 为什么要给全库而不是只给检索命中的几条（2026-10-28 事故）：
    问「技术部总共要发多少钱」时检索只命中 base_salary 配方，模型据此只算出
    55000 —— 它**看不见**库里还有「津贴」配方，「工资 = 基本工资 + 津贴」这个
    合成关系就没人告诉它。给全库目录 + 允许组合，比写死「总额必须含津贴」的
    词表靠谱（后者是把业务口径硬编码进代码）。
    """
    try:
        import skill_store
        raw = list((skill_store._load_raw().get("skills") or {}).values())
    except Exception:
        return "", []
    mine = [x for x in raw
            if not scene_id or str(x.get("scene_id") or "") in ("", scene_id)]
    if not mine:
        return "", []
    lines = []
    for x in mine[:limit]:
        nm = str(x.get("name") or "")
        tp = str(x.get("topic") or "")
        tx = " ".join(str(x.get("text") or "").split())[:90]
        lines.append(f"- {nm}（topic={tp}）：{tx}")
    return "\n".join(lines), mine


def _numeric_columns(db_path: str, limit: int = 80) -> list[str]:
    """库里所有**数值列**（`表.列`）—— 判官判「完整性」时的唯一合法候选集。

    为什么需要它（2026-10-28 实测）：光给判官一份全貌文字，它会凭行业常识
    补出「还有住房补贴、通讯补贴、奖金没算」，把正确答案一路怼死。把候选
    收窄成 schema 里真实存在的数值列，它就只能指认「employees.base_salary
    没算」这种能被实查证伪的缺口 —— 这是**schema 白名单**，不是业务词表
    （不写死任何业务口径，换库换场景自动跟着变）。

    读不到（库被锁/无 duckdb）→ 返回 []，判官退回纯全貌模式（如实放行）。
    """
    if not str(db_path or "").strip():
        return []
    try:
        import duckdb
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = con.execute(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = 'main' "
                "ORDER BY table_name, ordinal_position").fetchall()
        finally:
            con.close()
    except Exception:
        return []
    out: list[str] = []
    for t, c, ty in rows:
        _ty = str(ty or "").lower()
        if any(k in _ty for k in ("int", "decimal", "double", "real",
                                  "numeric", "float")):
            out.append(f"{t}.{c}")
    return out[:limit]


def _fits_judge(question: str, sql: str, data: Any,
                db_path: str = "",
                items: list[dict[str, Any]] | None = None,
                fails: list[dict[str, str]] | None = None,
                catalog: str = "") -> dict[str, Any]:
    """作答对题判官（**看全貌**判，不靠写死词表）。

    替代 _scope_guard/_subject_guard/_completeness_guard 三张词表：它把
    「题面 + 实际执行的 SQL + 取回的结果 + 库的真实全貌 + 数值列白名单」
    一起给判官，只问三件事：主体对不对（全公司 vs 某部门）、口径对不对
    （工资 vs 津贴）、完整不完整（题面说"总共"就要覆盖**库里实际存在**的
    各组成部分）。

    `items`：逐条执行的账（[{name, sql, data}]）—— 多技能编排时**逐条**渲染
    （每条 SQL 配自己的结果），否则判官看拼在一起的 SQL 会误读成「同一条
    SQL 少算了」（2026-10-28 实测：说「津贴没算上所有员工」，其实子查询里
    已经按 dept 过滤了）。

    ⚠️ 完整性只准拿**库全貌 + 数值列白名单**当依据：判官点名的缺失项必须
    是白名单里真有的 `表.列`，否则代码侧直接**不采信**（当它没缺）—— 这
    一条是硬机制，不靠提示词自觉。全貌里没有的表/列/津贴类别/奖金项目
    一律不许假设存在（与出题器规则 8「库里没有的费率/税率不许假设」同源）。

    ⚠️ 2026-10-29 用户拍板「我们做的是 ai infra，不应该为任何数据口径写死
    守卫，参考 DeepSeek」：原在这里的确定性「缺口闸」（失败 SQL 的表没被
    覆盖 + hours/days/count 词表豁免「数量列」）整体删除 —— 「是不是钱、
    缺不缺组成部分」是数据口径，写死在代码里换个库就失效。主体/口径/完整
    性/失败项要不要紧，全部由判官（MVE_CRITIC_MODEL 档，看全貌）判；代码
    只保留上面那条**存在性事实核对**（查 schema，不是判口径）与真空防线。

    判官不可用（LLM 挂了）→ 如实放行（ok=True + why 注明），绝不因为判官
    故障把正确答案拒掉。返回 {"ok": bool, "why": str, "missing": [列]}。
    """
    rows = None
    if isinstance(data, dict):
        rows = data.get("rows")
    try:
        # default=str：DuckDB 的 DATE/TIMESTAMP/DECIMAL 列返回的是
        # date/datetime/Decimal 对象，json.dumps 原生不认（2026-10-29 实测
        # 学习整轮被误判「执行层失败」）—— 序列化层兜底，不动运行时类型。
        preview = json.dumps(rows[:8], ensure_ascii=False, default=str)[:1200] \
            if rows else str(data)[:800]
    except Exception:
        preview = str(data)[:800]
    obs = _catalog_block(db_path, 3200)
    _nums = _numeric_columns(db_path)
    _num_block = ("、".join(_nums) if _nums else "（读不到 schema —— 完整性问题一律不采信）")
    if items:
        _blocks = []
        for _i, _e in enumerate(items[:6], 1):
            _d = _e.get("data") or {}
            _rr = _d.get("rows") or []
            try:
                _rp = json.dumps(_rr[:6], ensure_ascii=False)[:700]
            except Exception:
                _rp = str(_rr)[:700]
            _blocks.append(
                f"【SQL {_i}｜来源配方 {_e.get('name') or '?'}】\n"
                f"{str(_e.get('sql') or '')[:800]}\n"
                f"→ {len(_rr)} 行：{_rp}")
        sql_block = ("本次作答由以下 %d 条 SELECT 组成（各条结果由上层合并/"
                     "相加，**不是**一条 SQL 要包办全部）：\n\n%s"
                     % (len(items), "\n\n".join(_blocks)))
    else:
        sql_block = f"SQL：\n{str(sql)[:1500]}\n\n结果（前几行）：\n{preview}"
    _fails = [f for f in (fails or []) if isinstance(f, dict)]
    if _fails:
        _guard_names = {str(f.get("name") or "") for f in _fails
                        if f.get("guard")}
        sql_block += ("\n\n⚠️ 本轮这些配方**取数失败**（它们的结果不在上面 —— "
                      "逐个表态是否影响本题）：\n"
                      + "\n".join(
                          (f"- {f.get('name') or '?'}：已被确定性守卫拒"
                           f"（机制事实，不可上诉）—— {str(f.get('why') or '')[:160]}"
                           if f.get("guard") else
                           f"- {f.get('name') or '?'}："
                           f"{str(f.get('why') or '')[:160]}")
                          for f in _fails[:5]))
    sys_p = (
        "你是核对员。给你题面、实际执行的 SQL 与取回的结果、库里真实存在的"
        "表/列/取值全貌、**数值列白名单**，以及本轮取数失败的配方。判断"
        "「这组 SQL + 结果是不是真的回答了题面」，分四项各自表态：\n"
        "① subject_ok 主体：题面问的是谁/什么范围（全公司？某个部门？某个"
        "人？）—— **逐条**检查每条 SQL 的 WHERE：只要有一条把范围缩小到题面"
        "之外的子集（题面问全公司、SQL 却过滤了某个部门），subject_ok 就是 "
        "false；\n"
        "② measure_ok 口径：题面问的量是哪一种，SQL 聚合的列必须就是它；\n"
        "③ missing 完整性：题面说「总共/一共/总额/全部/应发」时，结果要覆盖"
        "库里**实际存在**的各组成部分。缺了哪一项，就把那一项对应的列写进 "
        "missing —— 格式必须是白名单里逐字存在的 `表.列`。完整性以"
        "**「库全貌 + 已学技能目录」**为准：目录里的方法 = 库里已被验证可算"
        "的口径；目录和全貌里都找不到换算参数的量，就是库里没有的口径。\n"
        "④ failed 逐项表态：对每个取数失败的配方单独回答 needed（题面需要它、"
        "缺了它答案就不对 → true；与题面无关、或库里本来没有费率/单价列根本"
        "算不出 → false）。拿不准就写 true（宁可不答，也不交片面数）。\n"
        "⚠️ 被标注「已被确定性守卫拒」的配方：needed **必须 false** —— 守卫"
        "已经机制性地证明这条 SQL 假设了库里不存在的参数（乘常量=造费率、"
        "幻觉取值、JOIN 乘爆），该量按库里可算部分覆盖，缺它不算不完整；"
        "你不再对它表态 true。\n"
        "⚠️ 硬约束：missing 只准从白名单里挑；想不出具体列（只是「感觉应该还"
        "有别的组成部分」）就留空数组 []。别用行业常识补全貌里没有的东西。\n"
        "⚠️ 完整性只数**库里算得出来**的部分：某一项需要的换算参数（费率/"
        "单价/系数/折算标准）**没有任何一列能逐字充当**时，那一项就**不可"
        "计算** —— 注意：它需要的**数量列**（小时/天数/件数）在全貌里存在，"
        "**不等于**该量可计算；把两列相乘/相除**合成**换算参数同样是假设"
        "业务参数。不可计算的项：missing 不写它、对应失败项 needed=false"
        "（缺它不算不完整，答案只覆盖库里算得出的部分）。全貌里没有的量"
        "不许假设。\n"
        "⚠️ 多条 SQL 时，每条只负责题面的一部分（上层负责合并/相加），不要要求"
        "单条 SQL 自己答完整题。\n"
        "只返回 JSON：{\"subject_ok\": true/false, \"measure_ok\": true/false, "
        "\"missing\": [\"表.列\"], "
        "\"failed\": [{\"name\": \"配方名\", \"needed\": true/false}], "
        "\"why\": \"一句话说清哪里不对；对就写口径一致\"}")
    user = (f"题面：{question}\n\n{sql_block}\n\n"
            f"数值列白名单（missing 只准从这里挑）：\n{_num_block}\n\n"
            + (f"已学技能目录（库里已被验证可算的方法 —— 完整性对照它，"
               f"它随每次学习自动更新）：\n{str(catalog)[:2000]}\n\n"
               if str(catalog or "").strip() else "")
            + f"库全貌（实查）：\n{obs}")
    try:
        # 判官模型走 MVE_CRITIC_MODEL（同 task_critic.py:275 的契约：判官不能省
        # 模型钱）。2026-10-29 实测踩到：判官用 flash 时**同一份技术部口径的
        # SQL，第一轮正确退回（「只计算了技术部门」）、重填轮又放行**——
        # 错误答案 57120 当确定句交了出去。判官档的活交给判官档的模型；
        # env 不设则回落全局模型，行为不变。
        import os as _osj
        cj = chat_json([{"role": "system", "content": sys_p},
                        {"role": "user", "content": user}], temperature=0.0,
                       model=_osj.getenv("MVE_CRITIC_MODEL", ""))
    except Exception as e:
        print(f"  （作答对题判官不可用：{type(e).__name__}: {e} → 如实放行）")
        return {"ok": True, "why": f"判官不可用（如实放行）：{type(e).__name__}",
                "missing": []}
    if not isinstance(cj, dict):
        return {"ok": True, "why": "", "missing": []}
    # 兼容老契约（只给 ok/why 的判官输出）：没有三项字段时按 ok 判。
    _has3 = any(k in cj for k in ("subject_ok", "measure_ok", "missing"))
    if not _has3:
        return {"ok": bool(cj.get("ok", True)),
                "why": str(cj.get("why") or "")[:300], "missing": []}
    sub = bool(cj.get("subject_ok", True))
    mea = bool(cj.get("measure_ok", True))
    # ⚠️ 判官对「失败项要不要紧」的意见**不进 ok**（2026-10-28 两次实测它都
    # 不可靠：一会儿说无关的 allowance「与题面无关」→ 交 133500 漏 3600，
    # ---- 完整性 / 失败项要不要紧：**全权交判官**（2026-10-29 用户拍板）----
    # 「我们做的是 ai infra，不应该为任何数据口径写死守卫，参考 DeepSeek。」
    # 上一版这里有一道确定性「缺口闸」：失败 SQL 碰到的表没被成功条目覆盖、
    # 且聚合目标「能当钱用」（用 hours/days/count/小时/天数… 词表豁免数量列）
    # → 判缺口。那套「是不是钱」的判断就是数据口径，写死在代码里 —— 换个库
    # （列名不叫 hours、单位是「件」「人次」）就失效或误伤；而且它跟判官抢
    # 判定权。现在：代码只做**存在性事实核对**（判官点名的缺失列必须在白名单
    # 里真有 —— 这是查 schema，不是判口径）；主体、口径、完整性、失败项要不要
    # 紧，全部由判官（MVE_CRITIC_MODEL 档）看着全貌自己判。
    _asked = [str(m).strip() for m in (cj.get("missing") or []) if str(m).strip()]
    # 只采信白名单里真有的列 —— 判官凭常识编出来的「缺奖金/缺补贴」在这里
    # 被机械地否掉（不是靠提示词求它别编）。读不到 schema 时退回旧行为。
    _real = [m for m in _asked if m in set(_nums)] if _nums else _asked
    # 守卫裁定不被翻案（DeepSeek 原则：规则判过的，LLM 不复判）：
    # 被确定性守卫拒的配方（费率/字面量/fan-out）= 机制证明这条 SQL 假设了
    # 库里不存在的参数 —— 判官对它们说 needed=true 一律无效。2026-10-29
    # 九连拒实证：判官把「数量列存在」当「可算钱」，坚持要库里算不出的
    # 加班费/扣款项，把本可成立的答案整体打成真空。
    _guard_names = {str(f.get("name") or "") for f in _fails if f.get("guard")}
    _needed = any(bool(f.get("needed"))
                  and str(f.get("name") or "") not in _guard_names
                  for f in (cj.get("failed") or []) if isinstance(f, dict))
    why = str(cj.get("why") or "")[:300]
    ok = sub and mea and not _real and not _needed
    if sub and mea and _asked and not _real:
        why = (why + "｜（点名的缺失项 " + "、".join(_asked[:3])
               + " 不在库里真实数值列中 —— 按全貌放行）")[:300]
    elif _real:
        why = (why + "｜实查确认缺失：" + "、".join(_real[:3]))[:300]
    if _needed:
        why = (why + "｜判官认为本轮取数失败的配方里有题面需要的项")[:400]
    elif _guard_names and (cj.get("failed") or []):
        try:
            _g_true = [str(f.get("name") or "") for f in cj["failed"]
                       if isinstance(f, dict) and f.get("needed")
                       and str(f.get("name") or "") in _guard_names]
        except Exception:
            _g_true = []
        if _g_true:
            why = (why + "｜（守卫已拒的配方 " + "、".join(_g_true[:3])
                   + " 被判官点名，按机制事实不计入缺失）")[:400]
    return {"ok": ok, "why": why, "missing": _real}



_PLAN_SYSTEM = (
    "你是信息路规划员（斯坦福 Generative Agents 的「先规划、再可达性检查」"
    "同构）。给你一道题和本场景的**信息路菜单**，规划这道题需要哪几路信息"
    "（可多选）。判断口径：\n"
    "- 题面的量能从场景数据源/已学技能算出来（口径能指认到真实表/列）→ db；\n"
    "- 题面要**场景之外**的信息（外部对比基准、时变行情、公开数据）→ web；"
    "内部与外部混合的对比题 → db 和 web 都选；\n"
    "- 用户上传的文件能提供答案或部分答案 → file；\n"
    "- 纯概念/方法/观点解释，不需要任何取数 → infer。\n"
    "web 只是「规划里有这一步」，不代表可达 —— 可达性执行时另核。\n"
    "只输出 JSON：{\"db\": bool, \"web\": bool, \"file\": bool, \"infer\": bool, "
    "\"why\": \"一句话\"}")


def _web_reachable(scene_id: str) -> tuple[bool, str]:
    """联网步骤的可达性检查（斯坦福 Generative Agents 同构：计划里的动作
    要对真实环境核 —— 世界上没有的东西，计划就得修订）。

    机制判据（三层，全对齐面板「联网能力 · N」的口径）：
    ① 场景动作注册表里有 origin=net 的原语（同一事实源）；
    ② 注册表为空先 restore 快照 —— 面板进程启动时才 restore，CLI /
       pilot 子进程里注册表可能还没填，没填 ≠ 没这个能力；
    ③ 条目在 ≠ 可用：猫爪总开关 + 该组开关都开才可达（开关关掉不是删
       条目，是不暴露 + 拒调用，同 neko 插件语义）。
    不可达 → 计划如实修订，不假装查过、不编数。
    """
    try:
        import actions as _act
        bucket = getattr(_act, "_REGISTRY", {}).get(str(scene_id)) or {}

        def _net_names(b: dict) -> list[str]:
            return [str(a.name) for a in b.values()
                    if str(getattr(a, "origin", "")) == "net"]

        names = _net_names(bucket)
        if not names:
            try:
                _act.restore()
            except Exception:
                pass
            bucket = getattr(_act, "_REGISTRY", {}).get(str(scene_id)) or {}
            names = _net_names(bucket)
        if not names:
            return False, "动作注册表里没有联网原语"
        _gs = sorted({str(_act.group_of(a)) for a in bucket.values()
                      if str(getattr(a, "origin", "")) == "net"})
        if not any(_act.group_enabled(str(scene_id), g) for g in _gs):
            return False, f"联网能力开关没开（{'、'.join(_gs)}）"
        return True, "、".join(names[:4])
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _plan_routes(question: str, *, scene_id: str, has_file: bool,
                 scene_tables: list[str] | None = None) -> dict[str, Any]:
    """信息路规划：先让模型对着**能力菜单**规划这道题要哪几路，再逐路执行。

    斯坦福 Generative Agents 同构（用户 2026-10-29 拍板）：规划先行，执行
    时逐步做可达性检查；DeepSeek V3.2 同构：工具菜单外挂给模型、模型自主
    规划。菜单是事实（当前有什么），不是路由词表（词表是猜）。
    规划 ≠ 可达：web 路执行时还要过 `_web_reachable`（工具开关核不核得上）
    —— 两步分开，不可达就如实修订计划。
    LLM 不可用 → 按旧词表兜底（安全方向：行为退回现状，不会更差）。
    """
    try:
        _cat = _scene_skill_catalog(scene_id)[0][:1200]
    except Exception:
        _cat = ""
    # 规划员不可用时的兜底（2026-10-29 用户拍板「不应该有词表」）：
    # 不猜题型 —— 不碰环境、不联网，只走模型推断并如实标注。这是「没有
    # 规划就不执行计划外动作」的安全方向；词表这条路由已删，不再复活。
    _fallback = {"db": False, "web": False, "file": bool(has_file),
                 "infer": True,
                 "why": "规划员不可用 → 不碰环境、不联网，只做模型推断",
                 "planned": False}
    try:
        cj = chat_json(
            [{"role": "system", "content": _PLAN_SYSTEM},
             {"role": "user", "content": (
                 f"题面：{question}\n\n"
                 f"信息路菜单：\n"
                 f"- db：场景数据源/已学技能（"
                 f"{'有表：' + '、'.join((scene_tables or [])[:6]) if (scene_tables or []) else '当前没有表'}）\n"
                 f"- web：场景**外**的信息（外部对比基准/时变行情/公开数据）"
                 f"—— 规划里允许有这一步，可达性执行时另核\n"
                 f"- file：用户上传的文件"
                 f"{'（本次检测到上传）' if has_file else '（本次没有上传）'}\n"
                 f"- infer：概念/方法解释，不需要任何取数\n\n"
                 + (f"已学技能目录（库内已被验证可算的方法）：\n{_cat}\n\n"
                    if _cat else "")
                 + "这道题需要哪几路？只输出 JSON。")}],
            temperature=0.0)
    except Exception:
        return _fallback
    if not isinstance(cj, dict):
        return _fallback
    out: dict[str, Any] = {}
    for k in ("db", "web", "file", "infer"):
        v = cj.get(k)
        out[k] = (bool(v) if isinstance(v, bool)
                  else str(v).strip().lower() in ("true", "1", "yes"))
    if not any(out.values()):
        return _fallback
    out["why"] = str(cj.get("why") or "")[:160]
    out["planned"] = True
    return out


def _inference_answer(question: str, context: str = "") -> dict[str, Any]:
    """inference 兜底（句句有回复的最后一层）：模型凭自身知识回答。

    同 DeepSeek 客户端的知识题回答：不用环境、不联网，答完**必须**带
    「模型推断」标注 —— 未验证、不入库、不计入学习曲线（inference 线的
    既有口径）。自评 grounded=false（自己都不知道）就如实不答。
    """
    out: dict[str, Any] = {"used": False, "answer": "", "note": ""}
    _ctx = str(context or "").strip()
    # ⚠️ 带文件和不带文件是**两种题**，提示词不能共用一句：
    #   · 不带文件 = 纯模型知识（同 DeepSeek 客户端「联网默认关，知识题直接答」）；
    #   · 带文件   = 阅读理解（同 DeepSeek 客户端带附件提问）。这时候再要求
    #     「凭你自己的知识回答」，模型会把「文件里有答案」也判成不 grounded →
    #     明明读得到却说不知道（2026-10-28 场景 G 实测：「引用谁的歌词最多」
    #     文件 6000 字已在上下文里，仍答 fetched_empty）。
    if _ctx:
        _sys = ("用户这次请求带了文件内容（就在下面）。请**依据这份文件**回答，"
                "不要编造文件里没有的事实。规则：\n"
                "① 需要统计/计数（谁最多、几次、哪些）时，逐条数清楚再给结论，"
                "并写出你数到的条目；\n"
                "② 文件里引用了歌词/诗句/名句但没写来源时，可以用你自己的知识判断"
                "它出自哪首歌、谁的作品，但必须说明「这部分是我按已有知识判断的」；\n"
                "③ 文件里确实找不到依据才 grounded=false。\n"
                '只返回 JSON：{"answer": "回答", "grounded": true/false}')
    else:
        _sys = ("凭你自己的知识回答用户问题——这不是从任何数据环境"
                "取的数。如果问题需要具体/实时的数据而你无法确知，"
                "grounded 必须为 false。只返回 JSON："
                '{"answer": "回答", "grounded": true/false}')
    try:
        cj = chat_json(
            [{"role": "system", "content": _sys},
             # 用户上传的文件就是这次的上下文（DeepSeek 客户端同构）：
             # 带文件的知识题 = 读文件作答；没有文件才是纯模型知识。
             {"role": "user",
              "content": (f"【文件内容】\n{_ctx}\n\n【问题】{question}"
                          if _ctx else str(question))}],
            temperature=0.2)
        ans = str((cj or {}).get("answer") or "").strip()
        if ans and bool((cj or {}).get("grounded", True)):
            out["used"] = True
            out["answer"] = ans
        elif ans:
            out["note"] = "模型自评不足以回答（grounded=false，如实不答）"
        else:
            out["note"] = "inference 没产出回答"
    except Exception as e:
        out["note"] = f"inference 失败：{type(e).__name__}: {str(e)[:140]}"
    return out


def _fanout_guard(sql: str, db_path: str) -> str:
    """消费重放侧的确定性 fan-out 闸门：跨一对多 JOIN 把驱动表行放大、且 SQL
    在做聚合 → 父表数值列（如 base_salary）被按子表行数翻倍，聚合值必失真
    （75120 事故根因：李娜 2 条津贴把 base_salary 算两次）。

    与 `_sql_literal_guard`（只查字面量真伪）互补：字面量守卫拦不住乘爆，因为
    乘爆 SQL 的字面量（'技术'/'2026-09'）全是真的；它只靠"行有没有被 JOIN 乘
    起来"的实查证据判定（同构 `vlml_mcp.check_grain`，2026-10-23 落地）。

    返回拒绝原因；'' = 通过。任何异常 / 测不了都返回 ''（不假装测过、不误杀
    —— 字面量守卫与判官仍在兜底）。纯明细查询（SELECT 行）跨 JOIN 放大是合法的，
    只在「聚合 + 行放大」同时成立时才拒（见 `vlml_mcp._has_aggregation`）。
    """
    if not str(sql or "").strip() or not str(db_path or "").strip():
        return ""
    try:
        from voyager_vlml.env.vlml_mcp import check_sql_grain, _has_aggregation
        g = check_sql_grain(sql, db_path)
    except Exception:
        return ""
    if not g:
        return ""
    n, d = int(g.get("rows") or 0), int(g.get("distinct") or 0)
    if d and n > d and _has_aggregation(sql):
        ratio = round(n / d, 2)
        return (f"JOIN 之后 {n} 行，驱动表 `{g.get('driver')}` 只有 {d} 个不同行"
                f"（放大 {ratio}×）—— 跨一对多 JOIN 做聚合会把父表数值按子表行数"
                f"翻倍，答案必错。正确写法：先把右表按关联键聚合成单行再 JOIN"
                f"（如 LEFT JOIN (SELECT 关联键, SUM(子表数值列) AS v FROM 子表 "
                f"WHERE … GROUP BY 关联键) t ON t.关联键=父表.关联键），或"
                f"改用标量子查询 SUM(父表.数值列 + (SELECT COALESCE(SUM(子表.数值列),0) "
                f"FROM 子表 WHERE 子表.关联键=父表.关联键 …))")
    return ""


def _sql_tables(sql: str) -> set[str]:
    """从 SQL 里抽取 FROM/JOIN 之后的表名（去注释与字符串字面量）。

    用于维度覆盖判定：比较答案 SQL 与已检索技能配方各自涉及的源表。
    """
    if not str(sql or "").strip():
        return set()
    s = re.sub(r"--[^\n]*", " ", str(sql))
    s = re.sub(r"'[^']*'", " ", s)
    tabs: set[str] = set()
    for m in re.finditer(r"\b(FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)",
                         s, re.IGNORECASE):
        tabs.add(m.group(2).lower())
    return tabs


def _answer_is_partial(answer_sql: str,
                       skills: list[dict[str, Any]],
                       n_dims: int) -> tuple[bool, str]:
    """维度覆盖守卫的判定核心（确定性，不赌 LLM）。

    合成题（n_dims>=2）的答案是**一条** SQL：若它只触及了「本题各维度技能所涉
    源表」里的 1 张、而维度本跨多张表（如"九月份发多少工资" = base(员工表) +
    津贴(allowance 表) + 加班(attendance 表)，却只写了 SELECT SUM(base_salary)
    FROM employees）→ 这是把合成题当单表/单维度答了，必漏维度，不能当最终数。

    - 返回 (False, "") = 不是片面（原子题 / 同源单表题 / 答案已组合多表）。
    - 返回 (True, 描述) = 片面，调用方据此标未验证并路由去学习组合。

    与 fan-out 闸互补：fan-out 管「JOIN 放大」，本闸管「合成题没组合全」。都不靠
    LLM 判官（2026-10-25 删消费 critic 后的确定性防线）。
    """
    if n_dims < 2 or not skills or not str(answer_sql or "").strip():
        return (False, "")
    dim_tabs: set[str] = set()
    for s in skills:
        dim_tabs |= _sql_tables(s.get("code") or s.get("recipe") or "")
    # 各维度本就同源单表（如只查员工表的"名+基薪"）→ 谈不上"组合漏维"，不拦。
    if len(dim_tabs) < 2:
        return (False, "")
    ans_tabs = _sql_tables(answer_sql)
    hit = dim_tabs & ans_tabs
    if len(hit) >= 1 and len(hit) < len(dim_tabs):
        return (True,
                f"维度覆盖守卫：答案只触及 {len(hit)}/{len(dim_tabs)} 张维度源表"
                f"（{sorted(hit)} / {sorted(dim_tabs)}）—— 合成题被单表答，"
                f"缺其它维度 → 不交片面数，路由去学习组合")
    return (False, "")


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
                           "cold_start": False, "root_scene": "", "node_status": "",
                           # 这次落到技能库的 key（没有则为空）—— 调用方据此
                           # 指认「是哪一条」，别按 code 字符串匹配（泛化后必败）。
                           "skill_key": ""}
    # 无取数 SQL 但有答案的任务（怎么养育/如何理解…）—— 照样算"答出来了"，
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
        out["skill_action"] = "答案无取数配方（不写技能库；技能存的是可执行配方）"
        return out
    if not _looks_like_recipe(sql):
        # 原版技能 = 一次**成功 rollout 的 program_code**，那是真的调了
        # control primitive 的代码（voyager.py:250-283 成功才 add_new_skill）。
        # 纯 `RESULT = {"answer": "…"}` 是模型凭训练记忆写的答案，不是配方：
        # 存进去下次就会被当"可复用配方"重放，等于把记忆固化成能力。
        # 2026-10-19 实测：Scene D 问「什么是阿斯伯格」，消费层写的正是这种代码，
        # 被存成 consume_e81eea3e 技能 —— 假配方就这么进了库。
        out["skill_action"] = ("答案无取数动作（代码里没有任何数据源调用，"
                               "不是配方，不写技能库）")
        return out
    # 入库前确定性守卫（2026-10-24 工资题事故后加）：SQL 字面量取值必须能在
    # 数据源实查清单里指认 —— 'food'/'transportation' 类幻觉从此进不了库。
    # 学习侧与消费侧入库都走 _absorb，这道闸天然两端同享。
    try:
        from scene_profile import profile as _sp
        _dbp = str((_sp(_scene_id) or {}).get("db_path") or "")
        _guard_why = _sql_literal_guard(str(sql), _dbp)
        # 单位守卫已删（2026-10-29 用户拍板）：口径/单位语义全交判官。
        if _guard_why:
            out["skill_action"] = f"拒绝入库：{_guard_why}"
            print(f"  · {_guard_why}")
            return out
    except Exception:  # pragma: no cover
        pass  # 守卫自身故障不拦入库（LLM 判官仍在）
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
            # 调用方（学习闭环 ⑦ 结晶）要能指认**是哪一条** —— 按 code 字符串
            # 匹配在泛化时代必败（库里存的是模板，不是这次跑通的原文），
            # 2026-10-26 实测 test_learn_orchestrate 两条断言就这样挂的。
            out["skill_key"] = str(have[-1])
        else:
            # 入库的同样是**方法**不是数值（用户 2026-10-23）：泛化成槽位模板
            # 再入库，失败原样入库。消费复用走 _refill_sql 按当前问句实例化。
            # ⚠️ 泛化前的**原文**（这次判对后固化的程序）要留在
            # blueprint.rollout_sql：模板（code）是给「检索 + 回填」通道用的，
            # 带 :slot 的模板原样重放必错；plugin_forge 结晶条目重放的是
            # rollout_sql（见 crystallize_skill 的取值口径）。
            _raw_sql = str(sql)
            try:
                sql = generalize_sql_recipe(sql, question)
            except Exception:
                pass
            _, key = skill_store.add(
                tid, tid,
                f"消费时答出来的取数方法：{question[:60]}",
                code=sql,
                blueprint={"source": "consume", "question": question,
                           "rollout_sql": _raw_sql},
                source="consume",
                source_scene=_scene_id,
            )
            out["skill_key"] = str(key or "")
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
