#!/usr/bin/env python3
"""启动学习闭环 —— 同构原版 Voyager.learn() 的 MVE 单轮（编排器）。

用户闭环（2026-10-19 指令原文，本模块一层层对应）
------------------------------------------------
「人消费能力（无法命中）→ 图谱建点 → 启动学习（图谱任务拆分，拆到任务有抓手可达）
→ 用猫娘计划的 mcp 去写插件，编排插件使用两个层面来完成任务 → 任务达成（取到值）
→ 写入技能库 → 人再消费同一个问题（命中且有答案，掌握度上升）」

    环节                    本模块的落地
    图谱任务拆分            task_decompose.plan()（同构 decompose_task）
    拆到任务有抓手可达      task_decompose.reachability() 逐个子目标判；
                           判 gap → forge、判 unreachable → 如实停
    用 mcp 写插件           plugin_forge.forge_gaps()（缺口→插件文件+动态注册）
    编排插件使用            plugin_forge.crystallize_skill()（技能→条目）+
                           mcp_gateway.expose_as_actions()（远端工具→条目）+
                           orchestrate.plan()（条目组合成步骤序列）
    任务达成（取到值）      VLMLActionAgent.solve()（写 SQL 真取数，带技能注入）
    写入技能库              consume._absorb()（图谱建点+掌握+配方入库，
                           与消费侧同一套账，不另造一本）
    人再消费同一问题命中    routing.match 打的是图谱节点 —— _absorb 里的
                           cold_start.ensure_cold_start_node 已建点

和原版 learn() 的对应（voyager.py:306-391）
------------------------------------------
learn() 一圈 = propose_next_task → rollout → 成功则 add_new_skill +
update_exploration_progress → 下一轮。本模块的 `start_learning()` 就是
一圈：拆分 → 补缺口 → 执行 → **取到值才固化**。原版铁律「失败不写技能库」
照搬：没取到真数 → status=no_data，不结晶、不入库、不建点。

诚实地界（不装全能）
------------------
· 执行层（写 SQL）复用 VLMLActionAgent —— 编排出来的条目描述/schema 进它的
  system 消息（技能注入），条目本身是**命名的抓手与记账**，不重复实现一个
  并行执行器。
· LLM 不可用（无 key/超时）→ 拆分降级 rule_fallback，origin 如实标；
  执行层失败 → status=failed + why，绝不编造取数结果。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
for _p in (str(HERE), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _resolve_scene(scene_id: str) -> str:
    sid = str(scene_id or "").strip()
    if sid:
        return sid
    try:
        import scenes
        return str(scenes.current_scene_id() or "A")
    except Exception:
        return "A"


def on_miss(question: str, scene_id: str = "", *,
            reason: str = "") -> dict[str, Any]:
    """人导入**未命中** → ① 待审区登记 ② **建图谱节点**（用户闭环第 2 步）。

    为什么要建点（用户 2026-10-19 澄清的验收线）：
        「人导入（未命中）---- 建图谱节点 ---- voyager 规划课程自主学习 …」
    只进待审区（pending_dimensions.json）的话，图谱上什么都没有 ——
    「图谱任务拆分」就没有可拆的对象，人也看不见这条问题落在哪。
    所以未命中当场建一枚 `status="cold_start_guess"` / `evidence=1` 的猜测
    节点（同构 neko candidate_knowledge_items：先建猜测项，解题成功才升级）。

    ⚠️ 只建不升：重复导入同一未命中问题**不涨证据**（没解出来就不算证据）。
    升级只发生在 `_absorb`（真的取到值并入能力库）那一侧。

    返回 {"pending_id", "node_id", "node_created", "node_status", "reason"}。
    """
    question = str(question or "").strip()
    scene = _resolve_scene(scene_id)
    out: dict[str, Any] = {"question": question, "scene_id": scene,
                           "pending_id": "", "node_id": "",
                           "node_created": False, "node_status": "",
                           "reason": ""}
    if not question:
        out["reason"] = "空问题"
        return out
    # ① 待审区（同构 neko candidate_knowledge_items，被拒项留审计不删）
    tid = ""
    try:
        import routing
        item = routing.add_pending(
            question, scene, reason=reason or "检索图谱未命中（相似度低于双阈值）")
        tid = str(item.get("topic_id") or "")
        out["pending_id"] = tid
    except Exception as e:
        out["reason"] = f"待审区登记失败：{type(e).__name__}: {e}"
    if not tid:
        return out
    # ② 图谱建点：只建不升（见 docstring）
    try:
        from knowledge_graph import KnowledgeGraph
        g = KnowledgeGraph.load()
        nid = f"consume:{tid}"
        node = g.nodes.get(nid)
        if node is None:
            g.add_cold_start_node(scene, nid, "dimension",
                                  label=question[:60],
                                  detail={"source": "human_import_miss"})
            g.save()
            out.update(node_id=nid, node_created=True,
                       node_status="cold_start_guess")
        else:
            out.update(node_id=nid, node_status=str(node.status))
    except Exception as e:
        out["reason"] = (out["reason"] + "；" if out["reason"] else "") + \
            f"建图谱节点失败：{type(e).__name__}: {e}"
    # ③ **需求级 id 登记**（2026-11-09 第三刀 A）
    #    人导入这一刻 = 原版 maieutic「教师发布 Exercise」的那一刻
    #    （照抄 `prisma/schema.prisma:14` 的 `Exercise.id` = slug，
    #      发布时定死、运行时不改、**不从题目文本算出来**）。
    #    为什么在这一步：req id 是**这个需求**的身份，跨问法、跨批次恒定 ——
    #    而 `consume:` 节点是「这道题的运行时记录」、`import:` 是「哪一次导入」，
    #    两者都不承载需求身份。骨架/进度三本账/技能/会话归档全部挂 req id。
    try:
        import req_registry as _rr
        _reg = _rr.register(scene, question)
        out["req_id"] = str(_reg.get("req_id") or "")
        out["req_created"] = bool(_reg.get("created"))
        if out["req_id"]:
            _rr.ensure_req_node(scene, question, out["req_id"])
    except Exception as e:
        out["reason"] = (out["reason"] + "；" if out["reason"] else "") + \
            f"需求登记失败：{type(e).__name__}: {e}"
    return out


def _answer_text(obs: list[dict[str, Any]], *, ok_only: bool = False) -> str:
    """取最后一轮观察里的 RESULT 当"答案"（critic 判知识型任务靠它）。

    原版 critic 看的是环境观察（inventory 等）；MVE 的知识型任务没有环境数据，
    模型交的 RESULT 就是全部证据 —— 不把它交给 critic，critic 只能看执行汇总，
    等于让它猜。截断到 1200 字，够判切不切题。

    `ok_only` —— 只认**判定通过**的观察。学习环节（`require_tools`）必须开：
    没取证被判失败的那一轮，data 里仍留着那段凭记忆写的答案，直接取出来
    交 gate = 闸门又判"答到了"，强制取证就白强制了（2026-10-20 修）。
    """
    for o in reversed(obs or []):
        if ok_only and not (o or {}).get("ok"):
            continue
        d = (o or {}).get("data")
        if d in (None, "", [], {}):
            continue
        try:
            if isinstance(d, str):
                return d[:1200]
            # default=str：DuckDB DATE 列是 date 对象（同 1d6ffa0 的坑），
            # 不兜底会静默跌进 str(d) 的 Python repr，判官看到的是垃圾格式。
            return json.dumps(d, ensure_ascii=False, default=str)[:1200]
        except Exception:
            return str(d)[:1200]
    return ""


def _has_real(obs: list[dict[str, Any]]) -> bool:
    """至少一次观察取到了**真数**（批判据复用 critic.has_real_data，不重造）。"""
    if not obs:
        return False
    try:
        from voyager_vlml.agents.critic import has_real_data
        return any(has_real_data(o) for o in obs)
    except Exception:
        # 判据取不到时退回最保守口径：ok 且有行
        return any(o.get("ok") and int(o.get("rows") or 0) > 0 for o in obs)


# 「表格类」工具：它们的真空由 is_vacuous（有没有真值）判。
_SQL_TOOLS = ("query_sql", "run_sql", "sql_query", "duckdb", "exec_sql")
_MIN_BODY = 20


def _material_real(ev: Any) -> list[dict[str, Any]]:
    """材料账里**正文类**工具真取回内容的条目（真空防线的文档侧口径）。

    为什么需要（2026-10-31 生产实测，Scene H dependencies_list）：真空防线
    `has_real_data` 只看观察里的 `data` 有没有真值 —— 这对表格成立，对
    「gh_read / web_fetch / read_file 取回一篇正文」不成立：那类工具**没有行**，
    data 里也没有数字，`is_vacuous` 会把满篇正文判成真空。那一轮材料账里躺着
    12004 字、来源也绑对了，却因为 solve 最终步没写 RESULT 而记成
    calls=4 / 真取到=0 / 行数=0 → status=no_data → 整条维度判死、0 技能入库。

    SQL 工具**不**走这条：表格的空结果（全 NULL / 0）必须继续被判真空，
    不许靠「正文非空」自救（那是 75120 那类乘爆/空表的老坑）。
    """
    out: list[dict[str, Any]] = []
    for e in (ev or []):
        if not isinstance(e, dict) or not e.get("ok"):
            continue
        name = str(e.get("tool") or "").lower()
        if any(k in name for k in _SQL_TOOLS):
            continue
        if len(str(e.get("text") or "").strip()) >= _MIN_BODY:
            out.append(e)
    return out


# 一次 rollout（跑一段取证代码）的墙钟上限。
# 为什么不是默认的 30s（2026-10-20 实测致命点）：单次 MCP 调用自己就有 60s
# 上限（local_agent.py:680），外层比内层短 → 工具还没回来就判超时，而超时又
# 不会杀掉线程（Python 做不到），于是 4 次重试只是往同一个被限流的接口上堆
# 4 个僵尸线程；7 条维度 × 4 次 = 整轮十几分钟、颗粒无收。
# 原版 rollout 里 bot 执行本来就允许几十秒（`bot.chat` 一路打进度），这里按
# 外部工具的量级给，可用环境变量覆盖（跑本地/纯 SQL 场景时可以调小）。
EXEC_TIMEOUT = float(os.environ.get("MVE_EXEC_TIMEOUT") or 90.0)

# 内层步数 = 原版 `action_agent_task_max_retries`（voyager 配置 4，
# `agents/action.py:713 solve(max_iters=4)` 就是它的默认值）：一个任务内
# 「写代码 → 跑 → 看执行反馈 → 改」最多 4 步。
#
# ⚠️ 2026-10-29 实测偏离（Scene H）：本文件 4 个 local_agent.solve 调用点
# 都没传 max_iters → 吃 `local_agent.solve` 的默认 2，等于只允许「改一次」。
# H 的四个维度各跑 12~17s 就死（两次代码都撞 GitHub 403，第二次本来该换
# gh_read/raw 却没步数了），与实际「4 步迭代」的注释对不上。调度层要和原版
# 同构，这里就必须显式传 4，不能靠默认值。
INNER_MAX_ITERS = int(os.environ.get("MVE_INNER_ITERS") or 4)


def available_grabs(scene_id: str, scene_dir: str = "") -> list[str]:
    """这个场景**现在真的能调**哪些取证手段（本机原语 + 联网 + 经 MCP 接入的）。

    为什么单开这个函数（2026-10-20 调研两个原项目后补）：
      原版 Voyager **没有**"先判任务可不可达"这一环 —— 它靠两条软机制：
        · 生成期 prompt 约束（curriculum.txt:29/33「别提需要没学会资源的任务」）
        · 执行反馈（critic.py:42-45 onError 直接短路 + action.py:263-273 把
          「我还缺什么」提炼成下一轮检索 query）
      而 MVE 有明确的**能力清单**（猫爪总开关下面的条目 = 同构 neko 插件），
      所以可达可以也应该**真判**，不该退化成常量 true。

      旧代码在本机分支把每条子任务硬写成 `status="reachable"`、why 写死一句
      "本机只读原语可用" —— 那是不判，不是判过。这里改成查注册表：
      · 有可用条目 → reachable，并把**依据**（手段名）写进 why，人看得见
      · 一个都没有 → gap（不是 unreachable：装数据源/开开关就有，如实给路）
    """
    try:
        import actions
        actions.restore_switches()
        if scene_dir:
            actions.register_local(scene_id, scene_dir)
        actions.register_net(scene_id)
        # GitHub 原语同 net 同理（2026-10-29 实测：人导入的调研任务
        # 「联网去仓库地址」学习侧 5 条全 failed —— 学习子进程没注册 gh 组，
        # 模型调 gh_read 直接 NameError；技能是跨入口复用的资产，
        # 学习侧与消费侧必须同一份工具菜单）
        actions.register_gh(scene_id)
        return [str(e.get("name") or "").strip()
                for e in actions.enabled_entries(scene_id)
                if str(e.get("name") or "").strip()]
    except Exception:
        return []


def learn_milestone_skills(scene_id: str, topic_id: str, question: str,
                           milestone: dict[str, Any],
                           dim_codes: list[dict[str, Any]],
                           *, import_id: str = "",
                           req_id: str = "",
                           ) -> dict[str, Any]:
    """里程碑**闭合**后把这一轮跑通的代码写进技能库 —— 同构原版
    `skill.py:64-99 add_new_skill`（原版只在 rollout **成功**后写：
    `voyager.py:230 if success: add_new_skill(info)`，存的是 program_code）。

    为什么单开这条（2026-10-20 定位的致命问题）：知识型/本机场景没有 SQL，
    走 `_absorb` 那条路会被判"无可执行配方"直接跳过 —— **学了一整轮，技能库
    一条都不涨**。而原版学的从来不是"答案"，是**跑通的那段代码**：
    本机场景里它就是调 `web_search / read_file / query_sql / mcp_*` 的那段脚本。

    铁律不变：**没真取到数的不入**。判据复用消费侧同一个 `_looks_like_recipe`
    （不重造），纯 `RESULT = {"answer": "…"}` 那种凭记忆写的答案照样不入库 ——
    否则就是把记忆固化成能力（2026-10-19 已删过 3 条这种伪配方）。

    粒度：一条闭合的维度 = 一次成功 rollout = 一条技能（原版就是一次 rollout
    一条）。里程碑整体达成只加**记账**，不另外造一条"大技能"。
    """
    out: dict[str, Any] = {"added": [], "skipped": [], "milestone":
                           str(milestone.get("name") or ""),
                           "achieved": bool(milestone.get("achieved"))}
    if not str(topic_id or "").strip() or not dim_codes:
        return out
    try:
        from voyager_vlml.consume import _looks_like_recipe
    except Exception:
        return out
    try:
        import skill_store
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    for item in dim_codes:
        code = str(item.get("code") or "").strip()
        did = str(item.get("id") or "").strip()
        if not code or not did:
            continue
        if not _looks_like_recipe(code):
            out["skipped"].append(did)      # 没调取数动作 = 不是配方，如实跳过
            continue
        # 技能存**算法**不存数值（用户 2026-10-23「不要把数值写死，而是把
        # 算法积累成技能」）：SQL 配方入库前泛化成槽位模板 —— 表/列/聚合/
        # JOIN 结构是方法，上一题的实体值（部门名/月份…）换成 :slot，头部
        # 注释写明每个槽位填什么。原版同构：原版技能是带参数的函数，实体值
        # 在调用时传入。泛化失败（LLM 挂/产出不像 SQL）就原样入库 —— 泛化
        # 是增强不是闸门，不阻塞学习闭环。
        # 单位守卫已删（2026-10-29 用户拍板「删」）：口径/单位语义不在代码里
        # 写词表判，入库判定交内层 critic + 字面量守卫 + 费率守卫 + 判官。
        try:
            from voyager_vlml.consume import generalize_sql_recipe as _gen
            # 泛化前的原文留着：模板（code）给检索+回填通道，原文给
            # plugin_forge 结晶重放（带 :slot 的模板原样重放必错）。
            _raw_dim = code
            code = _gen(code, str(item.get("question") or question))
        except Exception:
            _raw_dim = code
            pass
        # ---- 材料来源标记 + 文本侧确定性守卫（2026-11-03 场景 J 事故后加）----
        # 来源按**实际调用的工具**判（read_file/list_files → 上传文档；
        # web_*/gh_* → 联网；其余 → 场景库）。来源不标记的后果是实测过的：
        # 从 web 缓存结晶的技能被当成"库内已验证"复用，回访一路传下去。
        _prov = _provenance_of(list(item.get("tools") or []), code)
        _text_dim = (f"【{out['milestone'] or question[:30]}】"
                     f"{item.get('question') or question}"
                     f"：闭合这个维度时跑通的取数做法")
        if _prov == "upload":
            _qg = _upload_quote_guard(_text_dim, code, scene_id)
            if _qg:
                out["skipped"].append(did)
                out.setdefault("quote_guard", []).append(_qg)
                continue
        try:
            _act, key = skill_store.add(
                # key = f"{topic}::{name}"（skill_store._key），所以 name 直接用
                # 维度 id —— 写成 f"{topic}::{did}" 会出现 topic::topic::dim 双前缀。
                topic_id, did,
                # ⚠️ 技能描述必须够长：`add()` 对 <MIN_LESSON_LEN(12) 的 text
                # 直接 skipped（照原版 "Deposit useless items" 特判）。
                # 光写维度问句在短问题上会静默丢技能 —— 补一句固定后缀。
                _text_dim,
                code=code,
                source="learn",
                provenance=_prov,
                # 维度 id 是精确主键：不许相似度合并（实测把 differences 并进
                # 了叫 def 的老条目，key 与实际维度对不上，面板报的配方名失真）。
                exact_only=True,
                blueprint={"from": "maieutic_milestone",
                           "milestone": out["milestone"],
                           "dimension": did,
                           "question": str(item.get("question") or ""),
                           "scene": scene_id,
                           # 这次判对后固化的原文（泛化前）—— 结晶条目重放它
                           "rollout_sql": _raw_dim,
                           # 技能归属那次人导入（用户 2026-10-20）：
                           # 「技能的归属也是如此」。面板据此显示
                           # 「这条技能来自 imp_xxx（第 N 次导入）」，
                           # 第二次导入同一问题时掌握度涨的那一分能解释。
                           "import_id": str(import_id or ""),
                           # ★ 需求级 id（2026-11-09 第三刀 A/E）：
                           # 技能**属于哪个需求**的精确连接。
                           # ⚠️ 为什么必须有它：`milestone` 字段是**标题字符串**
                           #   （可重名），`topic_id` 是 `learn_<md5(question)>` —
                          #   两者都还是**问句派生**，换问法就断。
                           #   面板要按会话聚合技能时，靠的就是这个 req_id
                           #   （照抄原版：技能挂在 Session 上，靠 sessionId 关联）。
                           # 字段引入前的老技能没有这一项 → 面板如实标「未归属」。
                           "req_id": str(req_id or ""),
                           "tools": list(item.get("tools") or [])},
                source_scene=str(scene_id or "A"),
                # ⭐ 组内择优（第 2 步 B）：维度闭合时 critic 沉淀的 r 随技能
                # 入库；同维度重写时 r 更高才接受新 code（skill_store.add 内拦）
                r=float(item.get("r", -1.0)))
            if key:
                out["added"].append({"key": key, "action": _act, "dim": did})
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
    return out


# 里程碑级复合技能的组合 prompt（原版 learn() 第二步的同构物：
# 「组合成复合 program 按里程碑索引入库」—— 用户 2026-10-25：
# 「把里程碑在学习侧重试之后计入技能，不计的话消费检索不到」）
_COMPOSITE_SYSTEM = """你是数据取数方法组合员。手里有为同一道题学好的多条**维度配方**
（每条答一个维度），要把它们组合成**一条**能整体回答题目的 SELECT。

规则：
1. 每条维度配方算的量都要进最终 SQL（该加的加、该减的减，口径照配方，不许改口径）。
2. 乘爆防线：跨「一对多」JOIN 必须先把右表按关联键聚合成分组子查询再 JOIN
   （LEFT JOIN (SELECT 关联键, SUM(子表数值列) AS v FROM 子表 GROUP BY 关联键) t），
   绝不对明细行直接 SUM 父表列。
3. 字面量（表/列/筛选值）只能用输入里给出的，一个都不许新造。
4. 库里给不出的量（费率不存在、类别取值不存在）**不许编**：按 0 处理或省略该维度，
   并在 note 里说明哪条维度因为什么被按 0/省略。
5. 输出必须是单条 SELECT 或 WITH 开头的语句，不许 INSERT/UPDATE/DROP。

只返回一个 JSON：{"sql": "组合后的 SQL", "note": "一句话说明组合方式与取舍"}"""


def _session_program(dim_codes: list[dict[str, Any]], sql_text: str) -> str:
    """整题程序 = 各维配方**内联** + 整体取数 + RESULT 汇总（自包含）。

    形态依据（三处都指向同一件事，别自创第四种）：
      · **原版 Voyager**：一条技能 = 一段**可执行程序**
        （`voyager.py:225` `code = parsed_result["program_code"] + "\\n" +
        parsed_result["exec_code"]`；`skill.py:89` 存的是 `program_code`）
        —— 裸 SQL 不是技能形态，装进技能库里一调就
        `SyntaxError: Invalid star expression`（`SELECT COUNT(*)` 的 `*`
        被当 Python 星号表达式）。
      · **原版 maieutic 的 Phase2**：学生交的是**一份** finalCode
        （`schemas.ts:178-184` Phase2Data.finalCode），它自己对整篇 spec 负责
        —— 所以它必须**自包含**，不能依赖「另外几个技能刚好在同一作用域」。
      · **2026-11-11 实测**：旧的 `d_1 = skill_example_count()` 形态在
        `run_saved` 的命名空间里必 NameError（那里只注入原语，不注入兄弟
        技能），所以组合程序从来跑不起来 → 结晶时被 blueprint.rollout_sql
        （一条 SELECT）顶掉，整题能力没能沉淀成可调用技能。
      · **消费侧现成样板** `consume_7c45c9b6`：兄弟调用 + `RESULT = {...}`
        —— 消费侧仍可用该形态；学习侧产出的**整题程序**必须能独立重放。

    每维包成一个函数：函数内跑它自己的配方，末尾 `return RESULT`。
    各维变量互不污染（各自局部作用域），也不依赖兄弟技能是否在册。
    """
    lines = [
        "# 整题程序（自包含）：各维配方内联，互相不依赖",
        "# 每维一个函数：跑完返回它自己的 RESULT（对应原版 finalCode）",
        "",
    ]
    calls: list[tuple[str, str]] = []
    for _c in (dim_codes or [])[:8]:
        _did = str((_c or {}).get("id") or "").strip()
        _code = str((_c or {}).get("code") or "").strip()
        if not _did or not _code:
            continue
        _fn = "_dim_" + re.sub(r"[^0-9A-Za-z_]+", "_", _did).strip("_")
        lines.append("def %s():" % _fn)
        for _ln in _code.splitlines():
            lines.append(("    " + _ln) if _ln.strip() else "")
        lines.append("    return RESULT")
        lines.append("")
        calls.append((_did, _fn))
    lines.append("r = query_sql(%s)" % repr(str(sql_text or "")))
    lines.append("RESULT = {")
    for _did, _fn in calls:
        lines.append('    "%s": %s(),' % (_did, _fn))
    lines.append('    "overall": r,')
    lines.append("}")
    return "\n".join(lines)


def _session_text(question: str, milestone_name: str = "",
                  dim_questions: list[str] | None = None) -> str:
    """会话的**检索文本** = 里程碑题目 + 各维度的问题（maieutic 累积并集）。

    为什么必须带维度问题（2026-11-09 实测）：
      只写里程碑题目的话，会话只能被「里程碑原题」检索到 —— 用组成维度的
      问句（「GitHub=CLAIMED 的记录总共有多少行」）检索，会话进不了 top5。
      而用户要的恰恰是「同一目标的各种问法」都能检索到会话
      （「都是要查某人在xx年xx月的social media… 按会话去检索它就可以整合
      这一段时间的所有技能」）。向量化的对象是 name + text
      （skill_store._entry_embed_text）→ text 覆盖整个目标 = 语义面覆盖。

    单一事实源：学习侧合成与迁移脚本重建都调这里。
    """
    qs = [str(q or "").strip() for q in (dim_questions or [])]
    qs = [q for q in qs if q]
    out = (f"【里程碑{('：' + str(milestone_name)[:40]) if milestone_name else ''}】"
           f"{str(question or '').strip()}：全维度闭合后组合出的整体取数做法"
           f"（维度配方组合，非单维度）")
    if qs:
        out += "；涵盖的维度问题：" + "；".join(qs[:6])
    return out


async def learn_milestone_composite(scene_id: str, topic_id: str, question: str,
                                    dim_codes: list[dict[str, Any]], *,
                                    db_path: str = "",
                                    milestone_name: str = "",
                                    scene_dir: str = "",
                                    in_dims: list[str] | None = None,
                                    req_id: str = "",
                                    ) -> dict[str, Any]:
    """里程碑**全闭合**后把「里程碑本身」组合成**一条复合技能**入库。

    用户 2026-10-25 指令：「把里程碑在学习侧重试之后计入技能，不计的话消费
    检索不到」。维度技能各存各的，消费侧复合复用（_pick_composite_skill +
    _refill_sql）只会**单配方**实例化，押在一条维度技能上必漏维 —— 实测
    （2026-10-25 22:21）押中含「奖金」的维度配方，回填写出库里不存在的
    kind='奖金' 被字面量守卫拒用，落兜底现做。原版 learn() 的第二步就是把
    原子技能**组合成复合 program 按里程碑索引入库**（voyager.py:230 成功才
    add_new_skill）—— 这里补上这一步。

    入库四道关（缺一不入，宁缺毋滥）：
      ① 字面量守卫：组合 SQL 里的每个取值必须能在 db_observation 实查里指认；
      ② fan-out 闸：跨一对多 JOIN 乘爆的 SQL 不入；
      ③ 真执行取到真数：跑不通 / 0 行 / 整行 NULL（空表聚合 (NULL,0) 骗局）
         都不入；
      ④ 会话程序真跑得出真值（2026-11-09 加）：入库的形态是**组合程序**
         （兄弟调用 + RESULT 汇总），不是裸 SQL —— 光有 ③ 还不够，
         程序本身跑不通（兄弟调不到 / 语法错 / RESULT 空）照样不入库。
    任何一步失败都不入库（复合技能缺失只是退回「消费单配方复用」，不会存坏技能）。

    `in_dims`：**真入库**的维度 id 列表（来自 learn_milestone_skills 的
    `added`）。库里没有的维度不能出现在会话程序里 —— 调了必然 NameError，
    整条会话崩掉。没给就用本轮闭合的全部维度。
    """
    out: dict[str, Any] = {"added": "", "skipped": "", "sql": "", "note": ""}
    codes = [str(d.get("code") or "").strip() for d in (dim_codes or [])]
    codes = [c for c in codes if c]
    if not codes or not str(topic_id or "").strip():
        out["skipped"] = "没有维度配方或 topic 为空"
        return out
    try:
        from voyager_vlml.llm import chat_json as _cj
        from voyager_vlml.consume import (_sql_literal_guard as _lg,
                                          _fanout_guard as _fg,
                                          generalize_sql_recipe as _gen)
        from scene_db import SceneDBEnv
        import skill_store
    except Exception as e:
        out["skipped"] = f"依赖不可用：{type(e).__name__}: {e}"
        return out
    # 环境观察（地面真话）：组合时字面量只能从这里取 —— 防止把维度配方里
    # 泛化前的旧值或幻觉值（'奖金'）再抄回来。
    ground = ""
    if str(db_path or "").strip():
        try:
            from curriculum_auto import db_observation as _dbobs
            ground = str(_dbobs(db_path) or "")
        except Exception:
            ground = ""
    recipes = "\n\n".join(
        f"【维度 {i+1}｜{str((dim_codes[i] or {}).get('id') or '')}】"
        f"问：{str((dim_codes[i] or {}).get('question') or '')[:80]}\n"
        f"{c[:700]}"
        for i, c in enumerate(codes[:6]))
    user = (f"题目：{question}\n\n已学好的维度配方：\n{recipes}\n\n"
            # ground 给全量（2026-11-10 实测）：[:4500] 在观察面升级（15.3k）后
            # 恰好把 web_facts 段切掉 → 模型幻觉「表不存在」→ 产 SELECT 0 假兜底
            # （被真数校验拦下，但合成作废）。合成是低频操作，给全量。
            f"当前库真实存在的表/列/真实取值（字面量只能从这里取）：\n"
            f"{ground[:16000]}\n\n按规则产出组合 SQL。")
    try:
        # max_tokens 给足（2026-11-10 实测）：默认 1200 被 GLM-4.6 思维链
        # 吃光 → content 空 → 兜底取 reasoning_content（散文）→ 解析 {} →
        # 「组合产物不是 SELECT/WITH」。与教师端同病同修：预算小不是思考有害。
        resp = _cj([{"role": "system", "content": _COMPOSITE_SYSTEM},
                    {"role": "user", "content": user}],
                   temperature=0.2, max_tokens=8000, timeout=240.0)
    except Exception as e:
        out["skipped"] = f"组合 LLM 失败：{type(e).__name__}: {e}"
        return out
    sql = str(resp.get("sql") or "").strip()
    # 剥代码围栏（2026-11-10 实测）：模型常把 SQL 裹进 ```sql ... ```，
    # 围栏残留会让下面的前缀检查误拒（「组合产物不是 SELECT/WITH」）。
    import re as _re_c
    _m = _re_c.search(r"```(?:sql|SQL)?\s*(.+?)```", sql, _re_c.S)
    if _m:
        sql = _m.group(1).strip()
    note = str(resp.get("note") or "")
    out["note"] = note
    body = sql
    if not body.lower().startswith(("select", "with")):
        out["skipped"] = "组合产物不是 SELECT/WITH"
        return out
    out["sql"] = body
    # ① 字面量守卫（'奖金' 幻觉在这里被拦）
    _gwhy = _lg(body, str(db_path or ""))
    if _gwhy:
        out["skipped"] = f"组合 SQL 被字面量守卫拒用：{_gwhy[:160]}"
        return out
    # ② fan-out 闸
    _fwhy = _fg(body, str(db_path or ""))
    if _fwhy:
        out["skipped"] = f"组合 SQL 被 fan-out 闸拒用：{_fwhy[:160]}"
        return out
    # ②' 单位守卫已删（2026-10-29 用户拍板）：组合 SQL 的口径判定交判官。
    # ③ 真执行取真数（真空成功防线：整行 NULL 不算）
    try:
        env = SceneDBEnv(str(db_path or ""))
        rr = await env.run_sql(body)
    except Exception as e:
        out["skipped"] = f"组合 SQL 执行抛错：{type(e).__name__}: {e}"
        return out
    _d = (rr or {}).get("data") or {}
    rows = _d.get("rows") or []
    # ★ 2026-11-09：改用项目**既有**的真空判据 `critic.is_vacuous`
    # （复用，不重造 —— 同 `_has_real` 的做法）。
    # 旧写法 `any(v is not None ...)` 把 `SELECT 0 AS record_count` 判成
    # 「取到真数」→ 整体取数是**假值**也照样入库（实测：L 场景里程碑
    # 「八月份工资记录数」的会话 rollout_sql = `SELECT 0 AS record_count`，
    # 而表根本不在这个场景 → 该说答不了，却固化了一条假配方）。
    # `is_vacuous` 里 `_is_zeroish(0)` → True → 全零/全空 = 真空 → 拦。
    try:
        from voyager_vlml.agents.critic import is_vacuous as _isvac
        real = bool(rows) and not _isvac(_d)
    except Exception:
        # 判据取不到时退回最保守口径：任何非 None 的值都算真数
        real = bool(rows) and any(
            v is not None for row in rows[:3]
            for v in (row if isinstance(row, (list, tuple)) else [row]))
    if not (rr or {}).get("ok") or not real:
        out["skipped"] = (f"组合 SQL 没取到真数（ok={bool((rr or {}).get('ok'))} "
                          f"rows={len(rows)}）—— 不入库")
        return out
    # 技能存算法不存数值：组合 SQL 也先泛化成槽位模板 —— 但**模板不进 code**。
    # ⚠️ 2026-11-09 实测修正：模板进 code 会让会话程序**原样重放就跑不通**
    # （`ParserException: syntax error at or near ":"` —— 槽位 `:status` 没实例化）。
    # 而 MVE 的维度技能本来就没被泛化（code 里写死 'CLAIMED'/'ZXY876'，
    # 靠消费侧回填层按当前问句换实体）—— 会话必须跟维度技能**同一套形态**：
    # code 里是真值（原样重放能跑），泛化模板另存 blueprint 供回填层参考。
    try:
        sql_tpl = _gen(body, question)
    except Exception:
        sql_tpl = body
    # body 是**真执行取到真数**的那条（上面 ③ 验过）—— 泛化前的原文留
    # blueprint.rollout_sql，结晶条目重放它（模板给检索+回填通道）。
    _raw_comp = body
    # ---- 会话程序（2026-11-09）：形态从「孤立 SQL」改成「可执行的组合程序」----
    # 为什么必须改：技能是**程序**不是 SQL（原版 skill.py:89 存 program_code；
    # 消费侧现成样板 consume_7c45c9b6 也是组合调用 + RESULT）。
    # 之前这里存的是裸 `SELECT COUNT(*) ...` → 一调就
    # `SyntaxError: Invalid star expression`（`*` 被当 Python 星号表达式）
    # → 召回有（hits 高）但一条都跑不起来 → ok 恒 0。
    # 组合关系照 maieutic 的累积并集（会话 = 为这个目标积累的全部维度技能），
    # 兄弟互相可见照原版 programs 同作用域。
    _by_id = {str((d or {}).get("id") or ""): (d or {})
              for d in (dim_codes or [])}
    # 只为**真入库**的维度生成兄弟调用行：库里没有的维度调了必然 NameError，
    # 会话整条崩掉（连带它带的整体取数一起废）。in_dims 空 → 用本轮闭合的全部。
    _use_ids = [str(x) for x in (in_dims or []) if str(x) in _by_id] \
        or list(_by_id.keys())
    # 入库与验收都用**真值版**（原样重放跑得通）；泛化模板只进 blueprint。
    # ★ 2026-11-11（用户：「phase2 对 voyager 来说才是真正沉淀积累技能的」）：
    #   整题程序改成**自包含**——各维配方内联成函数，不再 `d_1 = skill_xxx()`。
    #   旧形态依赖"兄弟技能在同一作用域"，而 run_saved 的 ns 只注入原语 →
    #   组合程序永远跑不起来 → 结晶时被 rollout_sql（一条 SELECT）顶掉，
    #   整题能力从未沉淀成可调用技能（实测 learned_milestone.py 只有一条 COUNT）。
    _prog = _session_program(
        [_by_id[_did] for _did in _use_ids], _raw_comp)
    out["session_code"] = _prog
    # ---- ③' 真执行验收（会话形态）：程序真跑得出 RESULT 才入库 ----
    # 上面 ③ 验的是「SQL 本身能取到数」；这里验的是「**会话程序**能跑」——
    # 兄弟调不通 / 程序语法错 / RESULT 空 → 一律不入库。
    # 兄弟函数用**泛化前**的维度原文现场造（泛化模板带 :slot 跑不通）。
    _ok_prog = False
    try:
        import local_agent as _la
        _ns = _la._ns_base(str(scene_dir or ""), "", db_path=str(db_path or ""))
        for _did in _use_ids[:8]:
            _fn = _la._make_skill_fn(
                {"code": str((_by_id.get(_did) or {}).get("code") or ""),
                 "name": _did,
                 "text": str((_by_id.get(_did) or {}).get("question") or "")},
                str(scene_dir or ""), str(scene_id or ""), None,
                db_path=str(db_path or ""))
            if _fn.__name__:
                _ns[_fn.__name__] = _fn
        _box = _la._run_restricted(_prog, _ns, timeout=45.0)
        _res = _ns.get("RESULT")
        # ★ 2026-11-09：整体取数（`overall`）必须**自己是真值**。
        # 旧判据只看「RESULT 里有没有任何非空值」→ 兄弟维度取到真值就放行，
        # 而 `overall` 是 `SELECT 0 AS record_count` 这种**占位假值**也照样
        # 入库（实测：L 场景里程碑「八月份工资记录数」的会话，
        # 表不在本场景 → overall 恒 0，兄弟却是真的表存在性检查结果）。
        # 会话的「整体取数」是它区别于维度技能的那一半 —— 它假了，整条会话
        # 就是一条假配方。判据复用 critic.is_vacuous（不重造）。
        _overall_real = False
        if isinstance(_res, dict):
            _ov = _res.get("overall")
            try:
                from voyager_vlml.agents.critic import is_vacuous as _isvac2
                _overall_real = (_ov is not None) and not _isvac2(_ov)
            except Exception:
                _overall_real = (_ov is not None and _ov != {}
                                 and _ov != [] and _ov != "")
        _vals = []
        if isinstance(_res, dict):
            for _v in _res.values():
                _vals.extend(list(_v) if isinstance(_v, (list, tuple)) else [_v])
        elif _res is not None:
            _vals.append(_res)
        _ok_prog = bool(_box.get("ok")) and _overall_real and any(
            v is not None and v != "" and v != [] and v != {}
            for v in _vals)
        if not _ok_prog:
            _why = ("整体取数不是真值（overall 真空/缺）"
                    if (bool(_box.get("ok")) and not _overall_real)
                    else f"ok={bool(_box.get('ok'))} "
                         f"err={str(_box.get('error') or '')[:120]}")
            out["skipped"] = f"会话程序没跑出真值（{_why}）—— 不入库"
            return out
        # ★ Phase3 的 actual 面（2026-11-11 A 原版）：finalCode 实跑出来的
        #   RESULT 就是「做成了什么」，拿它跟整篇 spec（「说过什么」）对账 ——
        #   同构原版 intent-diff（spec vs finalCode），不是跨会话比对。
        out["verified_result"] = _res if isinstance(_res, dict) else {}
    except Exception as e:
        out["skipped"] = (f"会话程序验收抛错：{type(e).__name__}: {e}")
        return out
    # 会话的**检索文本**要覆盖整个目标（maieutic 累积并集），不只是里程碑题目。
    # ⚠️ 2026-11-09 实测：只写里程碑题目的话，会话只能被「里程碑原题」检索到 ——
    # 用组成维度的问句（「GitHub=CLAIMED 的记录总共有多少行」）检索，
    # 会话进不了 top5（score 1.045/0.98/0.957… 全是维度技能）。
    # 而用户要的恰恰是「同一目标的各种问法」都能检索到会话
    # （「都是要查某人在xx年xx月的social media… 按会话去检索它就可以整合
    # 这一段时间的所有技能」）。向量化的对象是 name + text（_entry_embed_text），
    # 所以把各维度的问题并进 text = 会话的语义面覆盖整个目标。
    _dim_qs = [str((_by_id.get(_d) or {}).get("question") or "").strip()
               for _d in _use_ids]
    text = _session_text(question, milestone_name, _dim_qs)
    # 材料来源 + 文本侧守卫：组合技能的配方由维度代码合成，来源按合成后的
    # 代码实际调用的工具判（同一套口径，别让两条写入通道不同构）。
    _prov_c = _provenance_of([], _prog)
    if _prov_c == "upload":
        _qg_c = _upload_quote_guard(text, _prog, str(scene_id or ""))
        if _qg_c:
            out["skipped"] = _qg_c
            return out
    try:
        _act, key = skill_store.add(
            topic_id, "milestone", text, code=_prog, source="learn",
            provenance=_prov_c,
            exact_only=True,
            # 组合技能的 r = 组成维度 r 的均值（全部无 r 时 -1，不计入择优）
            r=(lambda _rs: round(sum(_rs) / len(_rs), 3) if _rs else -1.0)(
                [float(d.get("r", -1.0)) for d in (dim_codes or [])
                 if float(d.get("r", -1.0)) >= 0]),
            blueprint={"from": "maieutic_milestone_composite",
                       "milestone": str(milestone_name or ""),
                       "question": str(question or ""),
                       "scene": str(scene_id or ""),
                       # ★ 需求级 id（2026-11-09 第三刀 E）：复合技能同样属于
                       # 某个需求 —— 面板按会话聚合技能时靠它精确连接。
                       "req_id": str(req_id or ""),
                       # 真执行取到真数的组合 SQL 原文（泛化前）
                       "rollout_sql": _raw_comp,
                       # ★ 组成维度存**完整 key**（{topic}::{dim_id}）：
                       # 维度 id 不是全局唯一的 —— 实测 `web_facts_github_status`
                       # 这一条 id 同时存在于 5 个 topic 下（同名维度各存一条），
                       # 只存 id 定位不到具体技能。只存 id 的老条目消费侧按
                       # 「同 topic 下找」兜底（见 consume._expand_session_skills）。
                       "dims": [f"{topic_id}::{_did}" for _did in _use_ids],
                       # 会话程序（泛化后）也留一份，便于面板/人看清组合关系
                       "session_code": _prog,
                       # 泛化后的槽位模板（**不进 code**：模板进 code 会让
                       # 会话程序原样重放报 ParserException —— 槽位没实例化。
                       # 模板只给消费侧回填层参考，与维度技能同一套机制。）
                       "rollout_sql_tpl": sql_tpl},
            source_scene=str(scene_id or "A"))
        out["added"] = str(key or "")
        out["action"] = str(_act or "")
    except Exception as e:
        out["skipped"] = f"入库失败：{type(e).__name__}: {e}"
    return out



# ── Phase2 循环里的评论 = 原版 critic（voyager/agents/critic.py + prompts/critic.txt）──
# 2026-11-12 用户指正：「我说的评论是 voyager 自己学习循环里的环节，按照道理来说
# 应该改得动代码的」。此前这里用的是 intent-diff 的分歧清单当评论 —— 那是**提交
# 之后**的分流判据（`submit_route.ts:52-58`：0 分歧 → phase4，否则 phase3），不是
# 循环内的 critic。它说的是「你的 spec 里写 A、你的程序做的是 B」——审计结论，
# 模型照着改不动，三轮原地打转。
#
# 原版循环（`voyager.py:222-297`）每轮做四件事，缺一件就改不动：
#   ① 写码         llm(messages)：system 里是**可用程序库**（control primitives
#                  的完整源码 + 检索到的技能），human 里是结构化的现场
#   ② 真跑         env.step(program_code)；programs 与 code **同一作用域** eval
#   ③ 判 + 评      critic.check_task_success(events, task, context, chest_obs)
#                  → (success, critique)。输入是**环境末态**（critic.py:36-105
#                  读 inventory / chests / nearby blocks），**不是代码文本**
#   ④ 重检索       retrieve_skills(context + summarize_chatlog(events))，然后
#                  render_system_message(skills=new_skills) —— system 每轮都换
#                  （voyager.py:255-258）
#
# 这里按四条逐条同构。SQL/数据场景的「环境末态」= 这一轮的执行观测（报错原话 /
# 真实取回的内容 / RESULT 的键）+ 环境实查块（真实的表、列、取值）。
P2_CRITIC_SYSTEM = """你是 Voyager 学习循环里的 critic，但这一轮的**验收标准只有一个：Task 里那份冻结的 finalText（spec）**。
你不写代码、不给答案，只做两件事：判断「这一轮交的 code 有没有把 finalText 承诺的每一项做出来」，并说清「下一步怎么补」。

为什么这样判（照助产士原版的 submit 路由）：原版没有"取到数才算过关"这道闸 ——
Phase1 冻结 finalText 之后，code 交上来就跟 finalText 逐条对账
（`submit/route.ts:64` 的 intent-diff：分歧 0 → 通过，有分歧 → 逐条解释）。
取材方式是 finalText 自己说了算：它说要查就查，说算就算。**判官不挑方式，只判对齐。**

我会给你这些信息：
- Task：冻结的 finalText。它就是验收标准，里面承诺的每一项都要在 RESULT 里有对应。
- Execution：这一轮程序真跑出来的东西 —— 报错原话、程序实际取回的外部内容、
  RESULT 里有哪些键。（没有外部材料的场景，取回内容为空是**正常**的，不算失败。）
- Context：环境实查 —— 有外部材料时才给表/列/取值；没有时为空也是正常的。
- 它可以调用的函数。

判定：
1. finalText 承诺的每一项在 RESULT 里都有对应，且值确实是这段程序执行出来的 → success=true。做多了也算成功。
2. RESULT 里的值只是把 finalText 的句子/结论原样抄进代码（没有任何推导或取数的一步）→ false。
3. 程序报错 → false。**RESULT 为空不是判失败的理由**（见下方 ★★ 段）。
4. 少做一项、或者换了个说法其实没做 → false。
5. **不许**因为「没调数据源」「取回 0 行」「RESULT 是算出来的」判失败 —— 那只是取材方式不同。

★★ 2026-11-15（同构助产士原版 submit 路由）—— 第 3 条收窄：
  原版 Phase2 的提交物**只有 finalCode 这一份文本**
  （`~/maieutic src/app/api/session/[sid]/submit/route.ts:24-26`：
  `z.object({ finalCode: z.string().max(100_000) })`）—— **不要求跑通、不要求
  有输出**。intent-diff 的 user message 里也**没有任何"运行结果"块**
  （`src/lib/opus/prompts/intent-diff.ts:307-333`）；「运行」在原版是纯 IDE
  教学反馈（stdout 只留在浏览器 `ExerciseClient.tsx:1047` RunConsole），
  从不参与判定。判官**只判「spec 与 code 对不对齐」**，不判「取没取到数」。

  ⇒ 所以：
  · 「RESULT 为空」**不再是判失败的理由**。空 RESULT 是事实（程序没交出
    交付物），要说的是「spec 承诺的哪几项在代码里没有对应」，而不是
    「没取到数」。第 3 条改为只保留「程序报错 → false」。
  · 「取回 0 行 / 没调数据源 / RESULT 是算出来的」一律**不许**判失败
    （第 5 条不变，且现在有了明确的落点）。
  · **你判的是「对齐」，不是「取没取到数」** —— 别把这两件事混成一件。
    取不到数这件事由执行层如实记录（Execution 段里的报错原话与取回内容），
    你要基于它说清「下一步怎么改」，而不是用它判失败。

critique 的要求（下一轮它只能看到这一段，写不好它就原地打转）：
- 说清**差 finalText 的哪一句**；
- 说清**下一步具体做什么**，用「它可以调用的函数」里的名字（没有外部材料时就说该怎么算）；
- 如果它是**用错了接口/方言**（按另一种引擎的记忆写的），直接把正确的那一次调用写出来；
- 不要复述报错（它已经看到），不要写「请仔细检查」这类空话。

只输出 JSON，能被 json.loads 解析：
{"reasoning": "...", "success": true/false, "critique": "..."}
"""

P2_CRITIC_FEW_SHOTS = """先看三个例子。

例1
Execution: 程序跑通，无报错；实际取回 {"cols":["name","type"],"rows":[["id","INTEGER"],["score","DOUBLE"]]}；RESULT 的键 ["columns"]
Task（spec）：① 列出这张表有哪些列 ② 每列的类型 ③ 有没有主键
JSON: {"reasoning":"①②都取到了；spec 第③项在结果里没有对应内容，RESULT 里也没有这个键。","success":false,"critique":"还差第③项「有没有主键」：用 query_sql 查这个引擎的**约束元数据**（不要从列信息里猜），把结论放进 RESULT['primary_key']；查之前先把这次调用的返回原样放进 RESULT['probe']，看清字段名再取值。"}

例2
Execution: 程序报错 IndexError: tuple index out of range；实际取回 ("id","INTEGER","NO")
Task（spec）：同上
JSON: {"reasoning":"它从这个接口的返回里取第 7 列，但这个引擎只返回 3 列 —— 字段顺序是按另一种引擎的记忆写的。","success":false,"critique":"不要从这个接口取它根本不返回的字段。改法三条：① 先把这次调用的返回**原样**放进 RESULT['probe']，看清有几列、每列是什么；② 只取真实存在的列（按键名或按长度判断，不要写死下标）；③ 主键/外键/索引这类约束不在列信息里，改去查约束元数据，把查询和返回一起放进 RESULT。"}

例3
Execution: 程序跑通；实际取回 [{"cnt":175}]；RESULT 的键 ["chapter","count"]
Task（spec）：按 chapter 等于 Functions 筛选，给出条数
JSON: {"reasoning":"筛的是 chapter='Functions'，真取回 175，与 RESULT 一致。","success":true,"critique":""}

例4（没有外部材料，全靠推导 —— 取回内容为空是正常的，别拿它判失败）
Execution: 程序跑通，无报错；没有外部数据源调用（本题材料在题面里）；RESULT 的键 ["ef","verified"]
Task（spec）：① 写出比例式 ② 代入求 EF ③ 验算；已知 AB/DE=2/3、BC=8
JSON: {"reasoning":"程序里比例式与代入都真算了，EF=12；验算把 BC/EF 与 2/3 对照后写 verified=True。三项都有对应。","success":true,"critique":""}
"""


def _p2_round_facts(obs: list[dict[str, Any]], result: Any,
                    code: str) -> tuple[str, str, str]:
    """把这一轮的观察压成 critic 要看的三个事实（同构 critic.py:36-105 的 render）。

    critic 读的是**环境末态**而不是代码文本 —— 报错原话、真实取回什么东西、
    RESULT 里到底有哪些键。这三样给全，评语才落得到「哪一项没做到」上。
    """
    errs = [str(o.get("error") or "") for o in (obs or [])
            if isinstance(o, dict) and str(o.get("error") or "").strip()]
    _data = ""
    for o in reversed(obs or []):
        if isinstance(o, dict) and o.get("ok") and o.get("data") is not None:
            try:
                _data = json.dumps(o.get("data"), ensure_ascii=False,
                                   default=str)
            except Exception:
                _data = str(o.get("data"))
            break
    _keys = ""
    if isinstance(result, dict) and result:
        try:
            _keys = json.dumps(sorted(str(k) for k in result.keys()),
                               ensure_ascii=False)
        except Exception:
            _keys = str(list(result.keys()))
    return ("\n".join(errs)[-1200:], _data[:1200], _keys)


async def _p2_critic(*, question: str, spec: str,
                     obs: list[dict[str, Any]], result: Any, code: str,
                     funcs: list[str] | None = None, db_path: str = "",
                     llm: Callable | None = None) -> dict[str, Any]:
    """原版 `CriticAgent.check_task_success`（critic.py:117-141）的异步同构物。

    返回 {"success","critique","reasoning","origin"}。判官用 `_intent_diff_call`
    同一条通道（glm-4.6 + 思考）—— 原版 critic 是 gpt-4，不是 gpt-3.5；
    实测 flash 在「做到没做到」这类判定上会把一致判成不一致。
    """
    out: dict[str, Any] = {"success": False, "critique": "",
                           "reasoning": "", "origin": "failed"}
    _err, _data, _keys = _p2_round_facts(obs, result, code)
    _funcs = [str(f) for f in (funcs or []) if str(f).strip()]
    user = (
        P2_CRITIC_FEW_SHOTS + "\n\n现在轮到你了。下面是真实的这一轮。\n\n"
        + "Execution:\n"
        + "  · 报错：" + (_err or "（无）") + "\n"
        + "  · 程序实际取回的内容（末次成功）："
        + (_data or "（什么都没取到）") + "\n"
        + "  · RESULT 里的键：" + (_keys or "（空）") + "\n\n"
        + "Task（冻结的 spec，验收标准）：\n\"\"\"\n" + str(spec or "") + "\n\"\"\"\n\n"
        + "题目：" + str(question or "")[:600] + "\n\n"
        + "它可以调用的函数：" + ("、".join(_funcs[:20]) if _funcs
                                 else "（见它的 system：数据源原语 + 已学技能）")
        + "\n"
        + _env_obs_block(db_path)
        + "\n按 schema 只输出 JSON。")
    call = llm or _intent_diff_call
    last = ""
    for _ in range(3):
        try:
            text = call([{"role": "system", "content": P2_CRITIC_SYSTEM},
                         {"role": "user", "content": user}])
        except Exception as e:
            last = f"LLM 调用失败：{type(e).__name__}: {e}"
            continue
        try:
            import maieutic_gate as _mg
            data = _mg._parse(text)
            if not isinstance(data, dict) or "success" not in data:
                last = "输出解析不出 success"
                continue
        except Exception as e:
            last = f"解析失败：{type(e).__name__}: {e}"
            continue
        out["success"] = bool(data.get("success") is True)
        out["reasoning"] = str(data.get("reasoning") or "")[:600]
        out["critique"] = str(data.get("critique") or "").strip()[:1200]
        out["origin"] = "llm"
        # 原版 critic.py:112-115：success=true 时 critique 允许为空；false 时必须
        # 有话说，否则下一轮等于没评论（这里退化成「reasoning」也别返回空）。
        if not out["success"] and not out["critique"]:
            out["critique"] = out["reasoning"] or last or "这一轮没有把 spec 里的项做完"
        return out
    out["critique"] = last or "critic 判定失败"
    return out


def _p2_retrieve(scene_id: str, question: str,
                 critique: str = "",
                 topic_id: str = "") -> list[dict[str, Any]]:
    """原版 `retrieve_skills` 的同构物（voyager.py:255-258）——**每轮重检索**。

    原版的查询串是 `context + summarize_chatlog(events)`（action.py:274-283
    从 chat log 里抽出「I also need X」）：把**这一轮缺的材料**拼进检索词。
    这里同理，用 critic 的评语（它写的正是「还差哪一件」）。

    只留 Python 形态（code 里有 RESULT）—— SQL 配方 exec 只会 SyntaxError，
    传进执行域等于喂 NameError（与 orchestrate 层 `_py_skills` 同一条口径）。

    `topic_id`（2026-11-13）= 本题的技能归属（`learn_<md5(question)>`）。传进去
    才拿得到**本题的草稿**（上次没过关的码）与本题已验证版本；不传就只有
    verified 的跨题参考。
    """
    try:
        import skill_store
        q = str(question or "")
        if str(critique or "").strip():
            q = q + "\n" + str(critique)[:400]
        _sk = skill_store.retrieve("", q, scene_id=str(scene_id or ""),
                                   own_topic=str(topic_id or ""))
        _py = [s for s in (_sk or [])
               if "RESULT" in str((s or {}).get("code") or "")]
        # 同名只能有一个可调用函数（`_build_skill_fns` 按 name 建、先到先得）：
        # 本题版本在，就丢掉同名的跨题版本 —— 否则 prompt 里列两个 milestone()，
        # 执行域里只有一个，模型可能照着错的那条改。
        _own_names = {str((s or {}).get("name") or "") for s in _py
                      if (s or {}).get("_own")}
        if _own_names:
            _py = [s for s in _py
                   if (s or {}).get("_own")
                   or str((s or {}).get("name") or "") not in _own_names]
        return _py
    except Exception:
        return []


# ── 交付物冒烟（2026-11-13）────────────────────────────────────────────────
# 同构原版 maieutic Phase2：`finalCode` 就是编辑器里被 Run 的那一份
# （`schemas.ts` Phase2Data.finalCode / `lib/run-python.ts` runPython），
# 不存在「代码里的代码」。我们这边模型常把工具塞成 RESULT 里的一段字符串：
# 外层脚本被跑了、内层工具从没跑过 —— 08:35 那发的出题函数里 SQL 少了
# Python 拼接，调用即 IndexError，却照样被 critic 判「做到了」并入库。
_DELIV_KEY_HINTS = ("final", "replay", "logic", "program", "code", "script",
                    "implementation", "tool", "generator",
                    # 中文键：学生常照题面原话给键名（实测 2026-11-13 14:15
                    # 「3_可重放逻辑代码」——纯英文表命中不了，门整场没触发）
                    "重放", "逻辑", "代码", "工具", "程序", "函数", "实现")


def _extract_delivered_program(result: Any) -> tuple[str, str]:
    """从 RESULT 里认出「交付物程序本体」，返回 (键路径, 代码)。

    递归遍历嵌套 dict/list（深度 ≤ 4）：交付物常被学生嵌在 delivery/result
    这类子字典里，旧版只扫顶层 —— 2026-11-13 14:15 那发把「可重放代码」放在
    RESULT["delivery"]["3_可重放逻辑代码"]，门没触发，纯注释当代码入了库。
    """
    best: tuple[str, str] = ("", "")

    def _walk(v: Any, path: str, depth: int) -> None:
        nonlocal best
        if depth > 4:
            return
        if isinstance(v, dict):
            for _k, _v in v.items():
                _walk(_v, (path + "." + str(_k)) if path else str(_k), depth + 1)
            return
        if isinstance(v, (list, tuple)):
            for _i, _v in enumerate(list(v)[:20]):
                _walk(_v, path + "[" + str(_i) + "]", depth + 1)
            return
        if not isinstance(v, str):
            return
        s = v
        if "\n" not in s or "def " not in s:
            return
        kl = path.lower()
        if not any(h in kl for h in _DELIV_KEY_HINTS):
            return
        if len(s) > len(best[1]):
            best = (path, s)

    _walk(result, "", 0)
    return best


def _smoke_delivered_program(code: str, *, scene_dir: str, scene_id: str,
                             db_path: str = "", question: str = "",
                             timeout: float = 90.0) -> dict[str, Any]:
    """把交付物当成**程序**真跑一次（原版：学生按 Run 看控制台）。

    三道硬门，任何一条不过都不算「做到了」：
      ① 有入口函数（def name(...)）
      ② 有示例调用 —— 只有 def 没有调用，等于交了一份从没被执行过的工具
      ③ 真跑不抛异常
    """
    import re as _re
    import local_agent as _la   # 执行域/执行器同一个（与 phase2 循环内用的是同一份）
    defs = _re.findall(r"(?m)^def\s+([A-Za-z_]\w*)\s*\(", code)
    if not defs:
        return {"ok": False, "stage": "entry",
                "why": "交付物里没有任何函数定义（只有说明文字或散装语句）"}
    called: list[str] = []
    in_main = False
    for line in code.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        indented = line[:1] in (" ", "\t")
        if not indented and s.startswith("if __name__"):
            in_main = True
            continue
        if not indented:
            in_main = False
        # 只看「顶层语句」和 `__main__` 块里的语句：写在别的函数体内、
        # 而那个函数从没被调用，等于没跑 —— 这正是我们要拦的形态。
        if not ((not indented) or in_main):
            continue
        if s.startswith(("def ", "class ", "import ", "from ", "return ")):
            continue
        for nm in defs:
            # `(?<![\w.])` 防 `obj.name(` / `xname(` 误判
            if _re.search(r"(?<![\w.])" + nm + r"\s*\(", s):
                called.append(nm)
                break
    if not called:
        return {"ok": False, "stage": "demo", "defs": defs,
                "why": ("交付物只有函数定义、没有一次示例调用 —— 这一段从来没被"
                        "执行过。必须在文件末尾真调一次，或写在 "
                        "if __name__ == '__main__': 下面")}
    try:
        ns = _la._ns_base(str(scene_dir or ""), str(scene_id or ""),
                          db_path=str(db_path or ""), question=str(question or ""))
        r = _la._run_restricted(str(code), ns, timeout=float(timeout))
    except Exception as e:
        return {"ok": False, "stage": "namespace",
                "why": f"冒烟没能建执行域：{type(e).__name__}: {e}"}
    if not r.get("ok"):
        return {"ok": False, "stage": "run", "defs": defs, "called": called,
                "why": "交付物跑不起来：" + str(r.get("error") or "")[:400]}
    return {"ok": True, "stage": "run", "defs": defs, "called": called,
            "ran": str(called[0]) if called else ""}

async def phase2_final_code(scene_id: str, question: str, spec_text: str,
                            dims: list[dict[str, Any]] | None = None, *,
                            db_path: str = "", scene_dir: str = "",
                            llm: Callable | None = None,
                            max_iters: int = 3,
                            max_rounds: int = 0,
                            timeout: float = 120.0,
                            topic_id: str = "",
                            no_draft: bool = False) -> dict[str, Any]:
    """原版 Phase2：照**冻结的 spec** 交一份 finalCode，并真跑通。

    依据 `submit_route.ts:24-58` + `sessions.ts:374-389`：Phase2 的唯一产物是
    `finalCode` 这一份程序（同时写 `currentCode` / `submittedAt`）。

    ★ 2026-11-12 补「不过关 → 追问 → 改」（用户原话：编写代码—评论改正）：
    原版这一步是**人**坐在 IDE 里改 —— 写不通就在 chat 里问，opus 用
    interrogative 模式只指出问题、不写代码（`phase2-chat.ts:29-34`）。MVE 的
    "学生"是 voyager，没有人在旁边改，所以由编排把它跑成**原版那条 rollout
    循环**（`voyager.py:222-297`，`action_agent_task_max_retries=4`）：

        ① 写码（system 里有可用程序库：数据源原语 + 已学技能）
        ② 真跑（solve：执行域里 skills 与 code 同作用域）
        ③ critic 判「任务做到没有」+ 给**下一步怎么补**的评语
        ④ 按「题面 + 缺什么」**重检索技能**，system 换掉，带上一轮程序重写
        …至多 `max_rounds` 轮

    评论来自 critic（`_p2_critic`，同构 `critic.py:117-141` + `prompts/critic.txt`），
    **不是** intent-diff。intent-diff 是原版**提交之后**的分流判据
    （`submit_route.ts:52-58`：0 分歧 → phase4，否则 phase3），属 Phase3 的事；
    拿它当循环内的评论，模型收到的是「你的 spec 说 A、你的程序做 B」这种审计
    结论，改不动代码（2026-11-12 实测：三轮原地重写同一个错）。

    success（critic 判「做到了」）→ `verified=True` 并允许入库技能 —— 与
    `voyager.py:369-370`「success 才 add_new_skill」同一条。
    到上限仍没做到 → `verified=False`，但**把最后那份跑得通的程序交上去**：
    原版学生的提交不受"过关"限制（点了就是交了），交上去才有 Phase3 可以
    逐条解释 —— 这正是原版 `revisionChoice=skipped` 那条路。

    返回：{"code","verified","why","rows","result","obs","rounds","critic"}
    """
    out: dict[str, Any] = {"code": "", "verified": False, "why": "",
                           "rows": 0, "result": {}, "obs": [],
                           "rounds": 0, "divergences": [], "history": []}
    spec = str(spec_text or "").strip()
    if not spec:
        out["why"] = "没有冻结的 spec（Phase1 未闭合），Phase2 无从写起"
        return out
    try:
        import local_agent as _la
    except Exception as e:
        out["why"] = f"执行器不可用：{type(e).__name__}: {e}"
        return out
    # 取材方式由 **spec 自己说了算**，不由场景/学科标签决定（2026-11-14 用户
    # 口径「不要把数学这一分类写死」）：spec 承诺取数就真取，承诺推导就真算。
    # 代码交没交对，判据只有一条 —— 与 finalText 的 intent-diff（助产士原版
    # `submit/route.ts:64`），不在这里预设"必须查到行/必须调用数据源"。
    #
    # `_p2_nosrc` 只说明**环境事实**：这个场景有没有可查的外部材料。它只影响
    # 提示词（没有材料就别假装去查），不是验收判据。
    try:
        import scene_profile as _sp_p2
        _p2_nosrc = not bool(_sp_p2.grounded(scene_id))
    except Exception:
        _p2_nosrc = False
    _dims = [d for d in (dims or []) if isinstance(d, dict)]
    _dtxt = "\n".join(
        "- %s：%s" % (str(d.get("id") or ""), str(d.get("question") or ""))
        for d in _dims[:8] if str(d.get("question") or "").strip())
    _base = (
        "【题面】\n" + str(question or "") + "\n\n"
        "【已经冻结的说明（spec）—— 不许改写，它就是这次交付的验收标准】\n"
        + spec + "\n\n"
        + ("请照这份 spec 写出**一段可执行的 Python 程序**：\n"
           "  · **取材按 spec 写的来**：spec 要查（库/文件/联网）就真去查；"
           "spec 要算（算式、推导、数值）就真用代码算。哪样都不做、直接把结论"
           "写成字面量，等于没实现；\n"
           "  · 每一项的结果放进 RESULT（键名用你取到/算出的那件事），"
           "最终答案放进 RESULT[\"overall\"]；\n"
           "  · spec 里写过的结论，代码要复算一遍再对照：一致就记 verified=True，"
           "不一致把差在哪写进 RESULT[\"mismatch\"]；\n"
           "  · **不许**只复述 spec，**不许**只回一句话，"
           "更不许把 spec 里的句子原样当成结果交上来。\n"
           "\n【交付形态：一段**能独立跑完整个流程**的程序（这就是「技能」）】\n"
           "它不是「几个能被别人调用的函数」，而是**跑一次就把这条流程走完**的\n"
           "程序 —— 就像点一次 start：\n"
           "  ① 取材：把这道题要用的材料拿到手（按 spec 查数据源 / 读题面已知量）；\n"
           "  ② 独立算：用代码从拿到的东西推出结论，再与 spec 的结论对齐；\n"
           "  ③ 产出：每一步的结果与最终答案装进 RESULT，验算过程也留一条。\n"
           "形状：先把每段写成函数（`def load(...)` / `def derive(...)` /\n"
           "`def build(...)`），**末尾按 ①②③ 顺序各调用一次**，最后\n"
           "`RESULT = {...}`。没有入参、不依赖任何外部上下文 —— 别人单跑一次\n"
           "就该得到同样的 RESULT。会被这样验收：**零上下文单独跑一遍**。\n"
           "跑不通、跑完 RESULT 是空的、或 RESULT 的值全是从代码里抄的字面量，\n"
           "都不算交了（那样它只是一段探查脚本，谁也用不了）。\n")
        + (("\n【spec 承诺了这些项】\n" + _dtxt + "\n") if _dtxt else ""))
    _rounds = int(max_rounds or 0)
    if _rounds <= 0:
        try:
            _rounds = int(os.environ.get("MVE_P2_ROUNDS") or 3)
        except Exception:
            _rounds = 3
    _rounds = max(1, min(_rounds, 5))
    _crit = ""
    _prev_code = ""
    # ★ 本题的上一版（2026-11-13 用户验收口径「它写的技能要可以用」）：
    # Phase2 交过码但没过关时，那份码以**草稿**形态留在技能库（同 topic），
    # 下次同一道题（含本会话重派）从它开始改 —— 而不是从零重写。
    # 原版学生在 IDE 里改的就是自己那份 currentCode（sessions.ts:374-389），
    # 这条同构物此前缺失：草稿不进库 → 每轮都从零 → 同一道题永远学不会。
    try:
        import skill_store as _ss
        # ★ 2026-11-15：`no_draft`（重做的第 N 遍）= **不许**从上一遍那份程序起步。
        #   上一版代码是"这一遍的草稿"，只有**同一遍内**的下一轮才该在这份上改
        #   （原版学生在 IDE 里改自己刚写的那份，sessions.ts:374-389）。跨遍沿用
        #   就不是重解：它拿着上次交上来的成品改两行，Δ 量到的是记忆 + 抄写，
        #   不是能力。技能库**不清**（那是跨遍的长期能力，正是"做得更好"的来源），
        #   清掉的只是本题这道"底稿"。
        _mine = [] if no_draft else _ss.retrieve("", str(question or ""),
                                                scene_id=str(scene_id or ""),
                                                own_topic=str(topic_id or ""))
        if no_draft:
            print("  · Phase2 重做：不带本题上一版底稿（从零写）", flush=True)
        _mine = [s for s in (_mine or [])
                 if str((s or {}).get("topic") or "") == str(topic_id or "")
                 and str((s or {}).get("code") or "").strip()
                 # 没 RESULT 的码不是技能（是探查脚本），不能当起点
                 and "RESULT" in str((s or {}).get("code") or "")]
        if _mine:
            _prev_code = str(_mine[0].get("code") or "")
            out["draft_from"] = str(_mine[0].get("key") or "")
            out["draft_verified"] = bool(_mine[0].get("verified", True))
            print("  · Phase2 带上本题上一版（"
                  + ("已验证" if out["draft_verified"] else "草稿，未通过")
                  + "）：" + out["draft_from"], flush=True)
    except Exception:
        _prev_code = ""
    _best: dict[str, Any] | None = None
    for _r in range(1, _rounds + 1):
        out["rounds"] = _r
        # ④ 重检索（原版 voyager.py:255-258）：查询串 = 题面 + 「这一轮缺什么」
        # （原版是 context + summarize_chatlog(events)，从 chat log 抽出
        # "I also need X"）。首轮还没有评语，就是题面本身 —— 与 reset() 里
        # 那次 retrieve_skills 同形。
        _sk = _p2_retrieve(str(scene_id or ""), question, _crit,
                           topic_id=str(topic_id or ""))
        _snames = [str((s or {}).get("name") or "") for s in (_sk or [])[:6]]
        # 记这一遍到底看见了哪几条配方（2026-11-15）：面板要拿它标「第 1 遍：
        # 0 条 / 第 2 遍：1 条」。没有这个数，第 1 遍与第 2 遍的 Δ 只能靠猜
        # —— 分不清是"变强了"还是"这次库里有货"。
        for _nm in _snames:
            if _nm and _nm not in out.setdefault("skills", []):
                out["skills"].append(_nm)
        # 上一轮的**程序本体**也要给它：原版学生在 IDE 里改的是自己那份代码，
        # 不是看完评语从头重写。实测（2026-11-12 场景 N）：只给评语、不给代码，
        # 第 2 轮重写又把同一个 `PRAGMA table_info` 的第 6 列误读成"外键/索引
        # 标志"，两轮犯同一个错。
        task = _base + (
            (("\n【你上一版为这道题写的程序（在这份上改，不要从头重写）】\n"
              if (_r == 1 and out.get("draft_from"))
              else "\n【你上一轮交的程序（在这份上改，不要从头重写）】\n")
             + "```python\n"
             + _prev_code[:4000] + "\n```\n") if _prev_code else "")
        try:
            obs, code = await _la.solve(
                task, str(scene_dir or ""), llm=llm,
                scene_id=str(scene_id or ""),
                max_iters=max(1, int(max_iters)),
                # 要求「必须调工具」只在**有外部材料**时成立。没有材料可查的
                # 场景逼它调工具，只会逼它编一次假调用（2026-11-14 P 实测）。
                #
                # ★ 2026-11-15（同构助产士原版）：Phase2 的取材方式由 **spec
                #   自己说了算** —— spec 承诺查就查、承诺算就算（:1145-1147
                #   的口径）。原版 Phase2 提交物只有 finalCode 文本
                #   （`~/maieutic src/app/api/session/[sid]/submit/route.ts:24-26`），
                #   从不要求「必须调过数据源」。
                #   ⇒ 「必须调工具」这道**待填键**在这里也不设：它把「取数
                #     这个动作」当成了交作业的前提。而真空防线（没调工具就
                #     写答案 = 凭记忆编造）在 `local_agent.solve` 里已有，
                #     由**环境有没有可指认数据源**决定，不该在 Phase2 这层
                #     再按「场景有没有材料」重复设一遍。
                #     保留 require_tools=False：spec 说要取数时，模型自己会去
                #     取；真空防线照样在 solve 里兜底。
                require_tools=False,
                timeout=float(timeout),
                context=(_env_obs_block(db_path) if db_path else ""),
                db_path=str(db_path or ""),
                # 已学技能进**执行域**（原版 index.js:256 `programs + code`
                # 同作用域 eval）—— 模型写的 skill_xxx() 才真的调得到，
                # 不只是"看见了"。
                skills=_sk,
                # 原版 human message 里的 `Critique:` 段（action.py:200-205）
                critique=_crit)
        except Exception as e:
            out["why"] = f"finalCode 跑不起来：{type(e).__name__}: {e}"
            return out
        out["code"] = str(code or "")
        out["obs"] = list(obs or [])
        if out["code"].strip():
            # 只记**非空**的那份：某轮没交码时不能让下一轮丢掉手上的程序
            _prev_code = out["code"]
        rows = sum(int(o.get("rows") or 0) for o in (obs or [])
                   if isinstance(o, dict) and o.get("ok"))
        out["rows"] = rows
        # RESULT 取最后一轮成功观察里的 data（solve 把 RESULT 放在 data）
        _res: Any = None
        for o in reversed(obs or []):
            if isinstance(o, dict) and o.get("ok") and o.get("data") is not None:
                _res = o.get("data")
                break
        out["result"] = _res if isinstance(_res, dict) else {}
        # ── 执行层：没交出程序 / 没取到数 → 这就是这一轮的评论 ──
        # 与 critic.py:44-47 同形：原版 critic 遇到 onError 直接 return None
        # （critique 为空），让 human message 里的 `Execution error:` 段自己说话。
        # 这里同理，不再多花一次判官调用 —— 事实比评语硬。
        if not out["code"].strip():
            out["why"] = "这一轮没有写出程序（模型没交 finalCode）"
            out["history"].append({"round": _r, "stage": "exec",
                                   "why": out["why"][:120]})
            # ★ 这道**保留**：它是「提交按钮」本身 —— 原版提交按钮只要求代码
            #   非空（`~/maieutic src/components/student/ExerciseClient.tsx:1025-1030`
            #   `disabled={finalSubmitting || !code.trim()}`），不要求跑通。
            #   没交出代码 = 没点提交 = 拿不到追问和判词（原版同构：
            #   intent-diff 只在 submit 路由被调用，`submit/route.ts:63-80`）。
            #   但评语口径改一下：原版不要求"必须调数据源取数"——
            #   spec 要查就查、要算就算（:1145-1147 已有这条口径）。
            _crit = ("上一轮没交出程序（" + out["why"] + "）。这一轮必须交一段 "
                     "```python 程序：照 spec 写 —— spec 承诺取数就真去取数，"
                     "承诺推导就算出来；结果放进 RESULT。")
            continue
        # 没有外部材料时，「有没有数据源调用」不是交付判据 —— 它的产出是
        # 算出来的。有材料时也不在这里判：判"做没做到 spec"的是 intent-diff，
        # 不是"调没调 query_sql"（助产士原版没有这道闸）。
        #
        # ★★ 2026-11-15 用户口径（同构助产士原版 submit 路由）：
        #   「result 相当于提交按钮，不提交是拿不到追问和判词的」
        #   「仍然有除助产士闭合标准（缺口序列）以外的待填键（比如取数这个
        #     动作）要做，我们要割掉它」
        #
        #   原版 Phase2 的提交物**只有 finalCode 这一份文本**
        #   （`~/maieutic src/app/api/session/[sid]/submit/route.ts:24-26`：
        #   `z.object({ finalCode: z.string().max(100_000) })`）——
        #   **不要求它跑通、不要求有输出**。判官 intent-diff 的 user message
        #   也没有任何"运行结果"块（`src/lib/opus/prompts/intent-diff.ts:307-333`），
        #   「运行」在原版是纯 IDE 教学反馈（stdout 只留在浏览器
        #   `ExerciseClient.tsx:1047` RunConsole），从不参与判定。
        #   追问的唯一来源就是提交（`submit/route.ts:92` setPhase3Divergences）——
        #   **不提交就没有追问和判词**。
        #
        #   ⇒ 所以「取不到数」**不是不提交的理由**：代码交了，intent-diff 就该
        #     拿到 spec + code 对账，该给分歧就给分歧、该追问就追问。
        #     原来在这里 `continue`（= 这一轮白跑、不提交）等于把
        #     「取不到数」当成了"没交作业"—— 于是判官根本没被问过，
        #     追问和判词一个都没有，模型只能原地重写 → 空转。
        #
        #   现在：**取不到数的事实照实记下来（给模型当下一轮的线索），
        #   但这一轮照样提交**，让 intent-diff 去判「spec 与 code 对不对齐」。
        #   「对不对齐」是原版的唯一判据；「取没取到数」由它自己从 code 读。
        #
        #   ★ 与 Phase2 内的 critic（voyager 的 rollout critic）分工：
        #     critic 只决定「下一轮怎么改」，不决定过不过关（见 :5595
        #     `_p2_verdict = _p2_diff_ok if _p2_diff_known else ...`）。
        #   ★ 与入库门（`save_final_code_skill` 的 skill_debug.verdict）分工：
        #     「这段程序能不能当技能用」由入库门判，与「这一轮算不算提交」
        #     是两件事 —— 别把入库门的判据提前到提交前（原版也没有）。
        _got_something = bool(rows > 0) or bool(out["result"])
        if not _got_something:
            # ⚠️ 评论**必须带执行器给的原话**（2026-11-12 实测）：只写"没取到数、
            # 核对表名"时，模型看不出真正的失败原因（那次是 `PRAGMA table_info`
            # 只有 6 列、它去取 row[6] → IndexError），三轮原地重写同一个错。
            # 原版 chat 里 opus 看到的是 IDE 的报错栈，这里也必须给同一份东西。
            _err = ""
            for _o in reversed(obs or []):
                if isinstance(_o, dict) and str(_o.get("error") or "").strip():
                    _err = str(_o["error"])
                    break
            # 还要给**它自己取回来的东西**：实测（14:06）模型按 sqlite 的习惯写
            # `row[5] == 1` 判主键，而 duckdb 给的是 `False` —— 报错只说
            # "list index out of range"，它猜不到是值域不同，三轮照旧。
            # 原版学生在 IDE 里能直接看到返回结构，这里用同一份事实补上。
            _sample = ""
            for _o in reversed(obs or []):
                if isinstance(_o, dict) and _o.get("ok") and _o.get("data"):
                    _sample = str(_o["data"])[:400]
                    break
            out["last_error"] = _err[:800]
            out["why"] = ("程序没跑出结果：" +
                          (_err[:200] if _err else "一行都没取到（rows=0）"))
            out["history"].append({"round": _r, "stage": "exec",
                                   "why": out["why"][:160]})
            # 错误分两类，评语必须跟着走（2026-11-14 实测）：这一档原来一律
            # 给「把那个查询单独跑一次、看它给几列」—— 那是**数据源**的错法。
            # 纯 Python 的传值错误（KeyError/TypeError/NameError/IndexError，
            # 报错里没有 SQL/引擎字样）被这段评语一带，模型会去"探查查询"，
            # 三轮原地重写同一个 KeyError：`derive()` 返回 {"EF": …}，
            # `build()` 却读 data["ratio"] —— 它永远想不到"上一步漏传了个键"。
            _py_err = re.search(
                r"\b(KeyError|TypeError|NameError|IndexError|AttributeError|"
                r"ZeroDivisionError|UnboundLocalError|ValueError)\b", _err or "")
            _is_src_err = bool(re.search(r"(?i)\b(duckdb|sqlite|sql|query|"
                                         r"table|column|catalog)\b", _err or ""))
            if _py_err and not _is_src_err:
                _ln = re.search(r"第\s*(\d+)\s*行", _err or "")
                _crit = (
                    "上一轮的程序**在它自己内部崩溃了**（不是数据源的错）。"
                    "执行器原话：\n```\n" + (_err[:600] or "（无错误信息）")
                    + "\n```\n"
                    "这是**你自己函数之间传值**出的错：某一步读了上一步没给出的"
                    "名/键/位。\n常错的三种形状，对号入座：\n"
                    "  1. 上一步 `return {\"a\": …}` 没写这一项，这一步却 `data[\"b\"]`"
                    " → 把这一步要的每个键都在上一步的返回值里**写全**；\n"
                    "  2. 函数声明了几个参数、调用时给少了/给多了 → 对齐参数表；\n"
                    "  3. 上一步返回的是标量，这一步按字典读 → 统一成字典返回。\n"
                    + (f"报错指在第 {_ln.group(1)} 行：" if _ln else "")
                    + "**只改那一处**（补上缺的键/对齐签名），别整段重写；"
                      "改完自己先在心里跑一遍每个函数返回什么、下一步读什么。")
                # ★ 不再 continue：这一轮照样提交（助产士原版 submit 只收
                #   finalCode 文本，不要求跑通）。取不到数的事实已记进
                #   out["why"]/history，评语也回灌了 —— 下一轮照样能改。
                #   原来在这里 continue = 「这一轮不算交」→ intent-diff
                #   拿不到 code → 判官没被问过 → 拿不到追问和判词 → 空转。
                out["exec_note"] = ("程序内部崩溃（已记原话，供下一轮修）："
                                    + (_err[:200] if _err else ""))
            else:
                _crit = ("上一轮的程序没跑出结果。执行器给的原话是：\n```\n"
                         + (_err[:800] or "（执行器没有给出错误信息，RESULT 是空的）")
                         + "\n```\n"
                         + (("执行器**这一次真正取回的内容**（原样，对照着看，"
                             "别按记忆猜字段数/取值类型）：\n```\n"
                             + _sample + "\n```\n") if _sample else "")
                         + "改之前先做一件事：**把出错的那个查询单独跑一次、把它的返回原样"
                           "放进 RESULT（`RESULT = {\"probe\": 返回值}`）**，确认它到底给几列、"
                           "每列是什么类型和取值 —— 不要按记忆假设它有哪几列、"
                           "也不要假设布尔/整数的写法（不同引擎不一样）。\n"
                           "然后只改报错指到的那一处（报错指哪一行就改哪一行，"
                           "别整段重写），改完再按 spec 逐项取数；"
                           "每一项的结果都要真的进 RESULT。")
                # ★ 不再 continue：同上一条 —— 这一轮照样提交。
                out["exec_note"] = ("程序没跑出结果（已记执行器原话，供下一轮修）："
                                    + (_err[:200] if _err else "rows=0 且 RESULT 空"))
        # ── ③″ 交付判据：跑完必须有 RESULT（2026-11-13）──
        # 实测：模型交了一段"探查脚本"（查两张表、最后一行是个变量名 `probe_result`），
        # rows>0、程序也"跑通"，于是被当成 finalCode 收下 —— 但它**没有任何交付物**，
        # 既不能被当技能调用（没有 RESULT），也没法跟 spec 对账（Phase3 只能判 drift）。
        # 原版 Phase2 的 finalCode 是要被 Run 出结果的（`run-python.ts` 抛 stdout/
        # 结果），空结果在那边等于没交。这里同一条：没有 RESULT 就是没交，重写。
        #
        # ★★ 2026-11-15 用户口径（同构助产士原版 submit 路由）：
        #   「result 相当于提交按钮，不提交是拿不到追问和判词的」
        #   「仍然有除助产士闭合标准（缺口序列）以外的待填键（比如取数这个
        #     动作）要做，我们要割掉它」
        #
        #   原版 Phase2 的提交物**只有 finalCode 这一份文本**
        #   （`~/maieutic src/app/api/session/[sid]/submit/route.ts:24-26`），
        #   **没有 RESULT 这个键** —— RESULT 是 MVE 自己造的容器（原版
        #   对应物是「程序跑出来的 stdout」，只留在浏览器，不参与判定）。
        #   原版不因「程序没跑出东西」而拒绝提交 —— 代码交了，intent-diff
        #   就拿 spec + code 对账（`intent-diff.ts:307-333` 的 user message
        #   里没有运行结果块），该给分歧就给分歧。
        #
        #   ⇒ 所以「RESULT 空」也不该是**不提交**的理由。它是"这段代码还没
        #     交付产物"的事实 —— 照样提交，让 intent-diff 判「spec 承诺的每
        #     一项在 code 里有没有对应」；它判出来的分歧就是给学生的追问。
        #   ★ 「能不能当技能用」由**入库门**判（`save_final_code_skill` 的
        #     `skill_debug.verdict`：跑不通/无 RESULT/占位符 → 不入库）——
        #     那是入库的判据，不是提交的判据，别提前（原版也没有）。
        if not out["result"]:
            out["why"] = "程序跑了，但 RESULT 是空的 —— 等于什么都没交"
            out["history"].append({"round": _r, "stage": "result",
                                   "why": out["why"][:120]})
            _shape = ("```python\n"
                      "def derive():\n"
                      "    ...  # 从题面已知量一步步算出结果\n"
                      "    return {\"value\": …, \"check\": …}\n"
                      "RESULT = {\"overall\": derive()[\"value\"], "
                      "\"verified\": True}\n"
                      "```") if _p2_nosrc else (
                      "```python\n"
                      "def build(module_id):\n"
                      "    rows = query_sql(\"SELECT … WHERE module_id = '\" + "
                      "module_id + \"'\")\n"
                      "    return {\"problem\": rows[\"rows\"][0][0], \"answer\": …}\n"
                      "one = build('m51252')\n"
                      "RESULT = {\"chosen\": one, \"verified\": True, \"replay_ok\": True}\n"
                      "```")
            _crit = ("上一轮的程序跑完了，但 **RESULT 是空的**。你交的是一段"
                     "「探查脚本」（跑了东西、最后一行只是个变量名），不是交付物。\n"
                     "程序必须以 `RESULT = {…}` 结尾，spec 承诺的每一项各占一个键。\n"
                     "照这个形状写（把里面的 … 换成你真实取到/算出的东西）：\n"
                     + _shape)
            # ★ 不再 continue：这一轮照样提交（助产士原版 submit 只收
            #   finalCode 文本，不要求有输出）。RESULT 空是事实，已记进
            #   out["why"]/history，评语也回灌了 —— 下一轮照样能改。
            #   原来在这里 continue = 「这一轮不算交」→ intent-diff 拿不到
            #   code → 判官没被问过 → 拿不到追问和判词 → 空转。
            out["exec_note"] = "RESULT 为空（已记，供下一轮补交付物）"
        # ── ③⁗ 单技能调试门：零上下文单独跑一遍，必须走完流程并交出产出 ──
        # 用户口径（2026-11-13 19:45）：「它跑这段代码，能够做到我想要的流程
        # （我触发就是点 start）—— 它会跑每一段：比如 ①查书 ②出题」。
        # 判据不是代码长相，而是**同一个 runner 真跑一次**（skill_debug.run_code：
        # 原语 + 已学技能，与将来调用它的执行域完全一致）。跑不通 / 跑完没
        # RESULT / 一次数据源都没调 → 它只是探查脚本，不是自动化，这一轮等于没交。
        _chk: dict[str, Any] = {}
        try:
            import skill_debug as _sdbg
            _chk = _sdbg.run_code(out["code"], str(scene_id or ""),
                                  db_path=str(db_path or ""),
                                  scene_dir=str(scene_dir or ""),
                                  quiet=True, timeout=float(timeout))
            # 这一道只回答「这段程序能不能零上下文单跑」—— 即原版 Phase2
            # 的 Run（`run-python.ts` 把 stdout/stderr 交回来）。"做得对不对"
            # 不在这里判：那是 intent-diff 的事。之前在这里加"必须调用数据源 /
            # 值不能是字面量"是自造闸门，会把不查库的解法全挡在门外
            # （2026-11-14 用户：不要自己发明）。
            _ok_auto = bool(_chk.get("ok")) and bool(_chk.get("RESULT"))
            if not _ok_auto:
                _auto_why = (("单跑跑不通：" + str(_chk.get("error") or "")[:200])
                             if not _chk.get("ok")
                             else "单跑跑完了，但没有 RESULT（等于什么都没交）")
        except Exception:
            _ok_auto, _auto_why = True, ""      # 调试器自身故障不阻断通路
        if not _ok_auto:
            out["why"] = _auto_why
            out["history"].append({"round": _r, "stage": "standalone",
                                   "why": str(_auto_why)[:160]})
            _seq = " → ".join(
                "%s(%s)" % (str(c.get("fn") or ""),
                            str(c.get("arg") or "")[:40])
                for c in (_chk.get("calls") or [])[:6])
            _crit = ("上一轮的程序**零上下文单独跑**没能走完整条流程：" + _auto_why
                     + "\n它自己单跑时按顺序调用了：" + (_seq or "（一次都没有）")
                     + "\n改法：让它**单跑一次**就把整条流程走完 —— ①取材（查数据源 "
                       "或读题面已知量）②独立推出结论并核验 ③组装产出，末尾 "
                       "`RESULT = {…}` 交出产出；不要交一段只查数、最后一行是个"
                       "变量名的探查代码。")
            # ★ 不再 continue（同构助产士原版 submit 路由）：
            #   这道门问的是「这段程序能不能当技能用」= **入库**判据，
            #   原版 Phase2 提交时根本没有这道闸（提交物只有 finalCode 文本，
            #   `~/maieutic src/app/api/session/[sid]/submit/route.ts:24-26`）。
            #   MVE 的入库门（`save_final_code_skill` 的 `skill_debug.verdict`）
            #   会用同一个 runner 再跑一次做同样的检查 —— 同一件事判两遍，
            #   且判在前面的那次会把「这一轮不算交」→ intent-diff 拿不到
            #   code → 判官没被问过 → 拿不到追问和判词 → 空转。
            #   现在：只记事实 + 回灌评语，这一轮照样提交。
            out["exec_note"] = ("零上下文单跑没过（已记，供下一轮修）："
                                + str(_auto_why)[:200])
        # ── ③′ 执行门：交付物必须是**被执行过的那份代码** ──
        # 用户口径（2026-11-13 14:25）：Phase2 的执行环境同构原版 Voyager ——
        # 每段代码都能跑、可修正可跑。执行器只跑 out["code"]（solve 内部的
        # _run_restricted），RESULT 只是它跑完留下的数据。所以「能不能跑」不必
        # 二次冒烟：写在本体里的函数必然跑过，写在 RESULT 字符串里的从没跑过。
        #   ① 本体有函数 + 顶层有调用 → 真跑过，放行
        #   ② 本体有函数 + 无调用     → 死代码，没验证过，拦
        #   ③ 函数只在 RESULT 字符串   → 从没执行过，拦（要求搬进本体）
        #   ④ 都没有                   → 本题不产出工具，不干预（交 critic）
        import re as _re2
        _body_defs = _re2.findall(r"(?m)^def\s+([A-Za-z_]\w*)\s*\(", out["code"])
        _dkey, _dcode = _extract_delivered_program(out["result"])
        _sm: dict[str, Any] = {}
        if _body_defs:
            _called: list[str] = []
            for _line in out["code"].splitlines():
                _s = _line.strip()
                if not _s or _s.startswith("#"):
                    continue
                if _line[:1] in (" ", "\t"):
                    continue          # 函数体内/块内的调用不算（只认顶层）
                for _fn in _body_defs:
                    if _s.startswith("def " + _fn):
                        continue      # 定义行本身
                    if _re2.search(r"\b" + _re2.escape(_fn) + r"\s*\(", _s):
                        _called.append(_fn)
            if _called:
                _sm = {"ok": True, "stage": "body", "ran": _called[0]}
            else:
                _sm = {"ok": False, "stage": "dead_code",
                       "why": ("代码本体里定义了函数（"
                               + "、".join(_body_defs[:3])
                               + "），但没有任何一次调用 —— 它从没被执行过，"
                                 "等于交了一段没人用过的代码。")}
            out["smoke"] = dict(_sm, key="<body>", chars=len(out["code"]))
        elif _dcode:
            _sm = {"ok": False, "stage": "not_in_body",
                   "why": ("交付的代码只写在 RESULT[" + repr(_dkey) + "] 这个"
                           "字符串里，而执行器跑的是代码本体 —— 它**从没被执行"
                           "过**。把这段函数挪进代码本体，在末尾真调一次。")}
            out["smoke"] = dict(_sm, key=_dkey, chars=len(_dcode))
        else:
            out["smoke"] = {"ok": None,
                            "why": "RESULT 里没有程序本体（本题不产出工具）"}
        if _sm and _sm.get("ok") is False:
            print(f"  · Phase2 第 {_r} 轮：执行门不通过｜"
                  + str(_sm.get("why") or "")[:160], flush=True)
            out["history"].append({"round": _r, "stage": "smoke",
                                   "why": str(_sm.get("why") or "")[:200]})
            _crit = ("执行门没过：" + str(_sm.get("why") or "")
                     + "\n把要交付的函数写进**代码本体**（不是 RESULT 里的字符串），"
                       "在文件末尾真调一次 —— 执行器只跑本体，跑过才算数。"
                       "返回值放进 RESULT（执行域里没有 print）。")
            # ★ 不再 continue（同构助产士原版 submit 路由）：
            #   这道门问的是「交付的代码有没有在本体里被真跑过」= **入库**判据
            #   （能不能当技能用），原版 Phase2 提交时没有这道闸
            #   （`submit/route.ts:24-26` 只收 finalCode 文本）。MVE 的入库门
            #   （`save_final_code_skill` 的 `skill_debug.verdict`）会再判一次。
            #   同一件事判两遍、且提前判的那遍会把「这一轮不算交」→
            #   intent-diff 拿不到 code → 判官没被问过 → 空转。
            #   现在：只记事实 + 回灌评语，这一轮照样提交。
            out["exec_note"] = ("执行门没过（已记，供下一轮修）："
                                + str(_sm.get("why") or "")[:200])
        if _sm.get("ok"):
            print(f"  · Phase2 第 {_r} 轮：执行门通过（"
                  + str(_sm.get("ran") or "") + " 在本体里被真跑过）", flush=True)

        # ── ③ critic：判「任务做到没有」+ 说清「下一步怎么补」──
        # 原版 critic.py:117-141，输入是环境末态（这一轮的观测 + RESULT），
        # 不是代码文本。它的评语就是下一轮 human message 里的 `Critique:`。
        _c = await _p2_critic(
            question=question, spec=spec, obs=list(obs or []),
            result=out["result"], code=out["code"],
            funcs=list(_snames), db_path=str(db_path or ""), llm=llm)
        out["critic"] = {"round": _r, "success": bool(_c.get("success")),
                         "critique": str(_c.get("critique") or "")[:800],
                         "reasoning": str(_c.get("reasoning") or "")[:400],
                         "origin": str(_c.get("origin") or "")}
        out["history"].append({"round": _r, "stage": "critic",
                               "success": bool(_c.get("success")),
                               "critique": str(_c.get("critique") or "")[:200],
                               "skills": list(_snames)})
        print(f"  · Phase2 第 {_r} 轮：critic 判 "
              + ("做到了" if _c.get("success") else "还没做到")
              + (f"｜回注技能 {len(_sk)} 条（"
                 + "、".join(x for x in _snames if x) + "）" if _sk
                 else "｜技能库无命中")
              + ("" if _c.get("success")
                 else "｜评语：" + str(_c.get("critique") or "")[:120]),
              flush=True)
        if _c.get("success"):
            out["verified"] = True
            return out
        # 跑得通、但没做到 spec —— 记下这份"可提交"的最新版
        _best = {"code": out["code"], "rows": out["rows"],
                 "result": out["result"], "obs": out["obs"]}
        _crit = str(_c.get("critique") or "")
    # 到上限仍未做到：以最后那份跑得通的程序"提交"（原版提交不受过关限制）
    if _best:
        out.update(_best)
        out["verified"] = False
        out["why"] = (f"改写 {out['rounds']} 轮后 critic 仍判没做到："
                      + (str((out.get("critic") or {}).get("critique") or "")
                         or "（无评语）")[:200]
                      + "｜程序本身跑得通，已按现状提交，交 Phase3 逐条解释")
    else:
        out["verified"] = False
    return out


def save_final_code_skill(scene_id: str, topic_id: str, question: str,
                          code: str, *, milestone_name: str = "",
                          dims: list[dict[str, Any]] | None = None,
                          req_id: str = "", verified: bool = True,
                          from_phase: str = "") -> tuple[str, str]:
    """把 Phase2 交出的 finalCode 写成**一条**技能（原版：一次会话一份 program）。

    与原版 `skill.py:64-99 add_new_skill` 同构：**跑通之后**才写，写的是
    program_code 本身，不是它的结果。这里不再拼 `_session_program` ——
    finalCode 已经是自包含的一份程序（这正是原版 Phase2 的形态）。

    `verified`（2026-11-13）：Phase2 交上来但 critic 判没做到时传 False ——
    写成**草稿**。草稿只回注同题（在那份上改），不喂别的题；Phase3 revision
    真跑通并补上分歧项后，用同 key 覆盖为 verified=True（`from_phase` 记
    `maieutic_phase3_revision`，面板能看出这条是怎么被改好的）。
    """
    import skill_store
    # ── 入库门：单技能调试（2026-11-13 用户验收口径）────────────────
    # 「它写的技能要可以用：我跑学习的 auto 模式，它会看到这个技能并达到我的
    # 自动化」。合格与否不看代码长相 —— 用**将来调用它的那个执行域**零上下文
    # 单跑一次：跑不通 / 跑完没 RESULT / 一次数据源都没调 → 不是自动化程序，
    # 不入库（草稿也不入：草稿是下一轮的起点，起点不能跑，下一轮只会照着跑偏）。
    _gate_why = ""
    _stub = False
    try:
        import skill_debug as _sdbg
        _chk = _sdbg.run_code(str(code or ""), str(scene_id or ""), quiet=True,
                              timeout=float(os.environ.get(
                                  "MVE_SKILL_GATE_TIMEOUT") or 60))
        # 已验证档要严：跑通 + 有 RESULT + 无占位符（②那步真做了）。
        # 草稿档放宽到「跑通 + 有 RESULT」：它是下一轮的起点，占位符会被
        # 标出来（blueprint.stub=True）并且在回注时点明「上一版②还是空的」。
        _ok_gate, _gate_why = _sdbg.verdict(_chk,
                                           allow_stub=not bool(verified))
        _stub = (not verified) and bool(_sdbg.placeholders(_chk.get("RESULT")))
    except Exception as _e:
        _gate_why = ""      # 调试器自身故障不阻断入库（如实放行）
    if _gate_why:
        print("  · 技能不入库：" + str(_gate_why)[:200], flush=True)
        return "rejected", ""
    if _stub:
        print("  · 草稿是半成品（RESULT 里还有占位符，只当下一轮的起点）",
              flush=True)
    _dims = [d for d in (dims or []) if isinstance(d, dict)]
    text = _session_text(question, milestone_name,
                         [str(d.get("question") or "").strip()
                          for d in _dims if str(d.get("question") or "").strip()])
    try:
        prov = _provenance_of([], code)
    except Exception:
        prov = ""
    return skill_store.add(
        str(topic_id), "milestone", text, code=str(code), source="learn",
        provenance=prov, exact_only=True, r=1.0,
        # verified=False → **草稿**（Phase2 交了、跑得通、没过关）。草稿只回注
        # 同题，等 Phase3 revision 真跑通后升级 —— 这是"技能可以用"的那条线。
        verified=bool(verified),
        blueprint={"from": from_phase or "maeutic_phase2_final_code",
                   "milestone": str(milestone_name or ""),
                   "question": str(question or ""),
                   "scene": str(scene_id or ""),
                   "req_id": str(req_id or ""),
                   **({"stub": True} if _stub else {}),
                   "dims": [str(d.get("id") or "") for d in _dims]},
        source_scene=str(scene_id or "A"))


# ══════════════════════════════════════════════════════════════════════════
# Phase 3 —— intent-diff 复盘（同构 maieutic `opus/prompts/intent-diff.ts`）
# ══════════════════════════════════════════════════════════════════════════
# 原版三段（`src_lib_sessions.ts:391-482`）：
#   setPhase3Divergences    分类器把「spec 全文 vs finalCode 全文」的每条**有意义
#                           的分歧**落成 {divergenceId, initialClassification,
#                           initialConfidence, predictedJustification,
#                           studentFacingQuestion, evidenceFromSpec,
#                           evidenceFromCode}（`Divergence` schema :197-212）。
#   recordDivergenceResponse 学生逐条回答后写 studentResponse / alignment
#                           (aligned|partial|diverged) / finalClassification；
#                           alignment=diverged 追加 `alignment_failure` 事件。
#   recordFinalRevision     全部答完后学生选 skipped|revised（revisedCode）；
#                           **不动** phase2.finalCode 与分类 —— 学习信号冻结。
#
# MVE 的映射（差异只有「谁作答」，说清楚不藏）：
#   · spec 面 = Phase1 里**学生自己说的话**（`render_session_text` 的
#     `它说的结论：` / `它交的整篇`，判官的判词已剔除）；
#   · code 面 = Phase2 交出的 finalCode 全文；
#   · 作答 = voyager **回到材料上核一次**再说（用户 2026-11-11 拍板），
#     不是让它编造"意图"；
#   · 判定复用同一个 examiner（`maieutic_gate.check`）判"这段回答有没有正面
#     回应这条问题"，映射成 aligned / partial / diverged（原版 Alignment 三值）。
#     原版 Tech Spec §5.4 的 post-hoc **重判** prompt 不在本仓，故
#     finalClassification 沿用初判并在 reason 里写明 —— 不自己发明第三套判官。
#
# 为什么不进技能库：Phase3 是**复盘**（核对已交的东西），不是新配方；原版也
# 只把 rollout 成功的 program 入库，复盘的回答与修订版程序都不覆盖 finalCode。

INTENT_DIFF_SYSTEM = """你在复盘一个学生刚做完的作业：你手里有他自己写的**说明（spec）**
和他交的**最终程序（finalCode）**。你要找出「他说要做的事」和「程序实际做的事」
之间每一条**有意义的分歧**，并针对每一条：

1. 分类：drift（漂移）/ revision（改主意）/ bug（机械错误）；
2. 预测：以他的水平，被问到这条时他大概会怎么解释；
3. 生成一句**中性**的问题问学生 —— 问题里绝对不能透露你把它分成了哪一类；
4. 给出你对这个分类的置信度：high / medium / low。

定义：
- DRIFT：程序比 spec 承诺的**少做**，或漏掉了承诺过的东西。通常不是故意的。
- REVISION：程序实现了一个**自洽的替代方案**，仍然满足 spec。它往往比原来的说法
  更好 —— 是学生中途改了主意。
- BUG：程序**试图**做 spec 说的事，但机械地失败了（语法、差一位、类型、空值…）。

偏向规则（BIAS RULE）：drift 与 revision 的证据说不清时，判 **REVISION**。
误判 drift（把合理的改动说成漂移）会损伤信任；误判 revision（漏掉一次 drift）
最多是少一次干预。两种代价不对称。

什么叫"有意义"：一个有经验的评阅人**会希望学生反思**的分歧才算。以下都不算：
变量命名、空格缩进、不改变行为的局部写法（列表推导换循环）、spec 没有约束过的
实现细节。只标**跨过行为线或策略线**的分歧。

推理框架（REASONING-FRAME）：分类之前，先从 spec 和代码里推断这个学生在哪个层面
工作 —— 实现细节 / 产品行为 / 系统结构 / 运行时语义 / 方法论。按**他自己所在的
层面**判分歧。例如运行时语义层面的 spec，权限边界、状态转换一致性、中断恢复的
同一性、终止规则，通常比底层函数结构更值得问。

先做一遍一致性检验（MUST DO FIRST）
spec 有两种形态，判法不同：
  A. 承诺型：「我要做 X」「结果里要给出 Y」—— 对照代码，看它有没有真的做；
  B. 结论型（数据/查询类作业里最常见）：「这张表有 9 列，都是 VARCHAR，没有主键」
     —— 这里的每一条都是**断言**，不是要求。判据只有一个：
     代码 + 它真实跑出来的结果，与这条断言**是否矛盾**。
        · 断言成立、代码也做到了 → 不是分歧（不要因为代码"没有再把结论复述一遍"而标）；
        · 断言说"没有主键"，程序查了主键、结果为空 → **两者一致**，不是分歧；
        · 断言说"都是 VARCHAR"，结果里写的是 text → 一致（同一件事的不同叫法不算分歧）；
        · 断言说"有 N 行 / 有某列"，结果为空或没有该列 → 这是分歧（drift 或 bug）。
每条候补分歧，先在心里写出一句「spec 说 A，代码/结果做的是 B」。写不出**具体的冲突点**
就不要标 —— 想不出冲突点 = 没有分歧 = 返回空数组。宁可返回空，也不要凑数。

把 spec 拆成一条条陈述，每条判一个状态（三选一，别只判"有没有冲突"）：
  · **支持**：spec 说的，代码做到了 / 结果与这条断言一致 → 不标；
  · **矛盾**：结果与这条断言冲突 → 标（bug 或 drift）；
  · **没有体现**：spec 承诺了这件事，代码里既没做、**结果里也没有对应内容** → 标 drift。
    这正是原版 DRIFT 的定义（"代码比 spec 要求的少做，或漏掉了承诺过的东西"）。
    "没有体现" ≠ "一致"：前者是**漏做**，后者是**做到了**。
  区分办法：到**结果**里找 spec 提到的那个量 / 那个字段 / 那句结论 ——
    · 结果里有、且对得上 → 支持；
    · 结果里有、但对不上 → 矛盾；
    · 结果里根本没有这一项、代码里也没有算它的逻辑 → 没有体现（drift）。

数据题最常见的五种**误判**（它们都不是分歧，实测反复出现，逐条核对）：
  1. spec 说「没有 X」；程序查了 X、结果为空。→ 一致（结果为空正是"没有"）。
  2. spec 说「都是字符串」；结果里写 `text` / `VARCHAR`。→ 一致（同一件事的不同叫法）。
  3. spec 只给结论；程序用 SQL 把同一件事**复核**了一遍。→ 一致（复核不是分歧）。
  4. spec 说「9 列」；程序列出的列名正好 9 个。→ 一致。
  5. 程序里**存在某段查询语句**，不等于它在声称相反的事实 —— 判据是**结果**，不是
     "代码里有没有写这句 SQL"。看到"代码里有查主键的语句"就去问"为什么要查"，
     就是把"复核"读成了"矛盾"。

关键：**一致不是分歧**（CRITICAL — AGREEMENT IS NOT A DIVERGENCE）
标之前先确认 spec 与代码在**可观察行为**上真的不一致。spec 明确写了、代码也正确
实现了的情形，**不是**分歧 —— 哪怕它是边界情形。
示例：
  · spec「输入为空时打印 Hello, !」；代码 `print(f"Hello, {name}!")`，空输入时
    打出的就是 "Hello, !" → 不是分歧，两者一致。
  · spec「空列表返回 0」；代码累加器初值为 0、循环体不执行 → 空列表返回 0。
    不是分歧（代码从初值继承了正确行为）。
  · spec「假定半径为正」；代码直接算 π×r²、没有符号检查，负半径因为平方也得到
    正面积 → 不是分歧（spec 自己把假设摆明了，代码在该假设的定义域内运行）。
同一输入下 spec 的行为 B 与代码的行为 B 相同，就**不要**去问学生"说说你是怎么
想的" —— 那是在消耗他对这套流程的信任。

只在「spec 说一件事、代码做另一件事、且一个认真的评阅人会希望他注意到并解释这个
缺口」时才标。

占位符规则（PLACEHOLDER-IN-SPEC）：学生常在 spec 里用占位符代表用户输入的值：
  「打印 Hello NAME!，NAME 是用户输入的名字」「输入 N 时返回 'N 的平方是 X'」
  「输出 <问候>, <名字>」「消息 'Your total is PRICE'」
代码用 f-string / format / 字符串拼接 / % 在这些位置**替换成真实变量**时，
**不是分歧** —— spec 用占位符描述了输出的形状，代码把占位符填上了值，行为相同。
同样适用于尖括号 <name>、全大写记号 NAME/PRICE、花括号 {name}、"用户输入的那个
名字"这类回指 —— 只要代码在该位置代入的是真实输入，就算一致。
例外：代入的是**错的**值（spec 的 NAME 指用户名字，代码却不管输入都打印
"Hello Alice!"，或用了另一个变量）—— 那是 bug，占位符没绑对。

输出标签宽容（OUTPUT-LABEL LENIENCY，只在学生水平是 week_1_2 时适用）：
初学者常用带标签的例子表示"算出来的值放这儿"：「打印 'Perimeter: FINAL_RESULT'」
「输出 'Your total is PRICE'」。这表示**输出的形状**，不是对标签文字的硬性约定。
代码打出了正确的计算值、只是没有 "Perimeter: " 前缀 → 不是分歧。仅当以下三条
同时成立时才适用：水平是 week_1_2；题目本身没有明确要求带标签/格式化消息；
不一致的只是外围标签、计算值本身对得上。week_3_6 及以上不适用。

会话内修订（IN-SESSION REVISIONS）：如果用户轮里给了「会话内修订」块，把每条修订
的改动**当作学生意图的一部分**。有效意图 = 原 spec 按顺序被这些修订改过之后的样子。
实现了修订所述内容的代码**不是分歧** —— 他已经披露了改动并给了理由。只在代码做了
修订**没有授权**的事、或改过之后仍然偏离 spec 时才标。

按水平校准预测（PREDICTION CALIBRATION）：
- week_1_2：预测要短、具体，常常是忘了 / 没注意到：
  「我忘了这种情况」「我没考虑空输入」「我以为这两个是一回事」「我不知道怎么做就跳过了」。
  在这个水平，"我不知道""我忘了"是**预期之内**且最有诊断价值的。不要预测策略性
  推理、取舍分析、复杂度论证。
- week_3_6：预测可以带一点小取舍的推理：
  「我觉得这里用列表比字典省事」「我改了是因为第一版有个 bug 我找不着」「我用
  range(len(x)) 是因为我还要下标」。
- week_7_plus：预测可以谈策略、复杂度、权衡、架构理由。

提问措辞（QUESTION PHRASING）：
- **绝不预设分类**。"你的 spec 要求 X，你的代码没做 X" 预设了 drift，**禁止**。
- 用中性框架：「我注意到 [观察到的事实]。[你的说明说 X，代码做的是 Y] 能跟我说说
  这里是怎么回事吗？」
- 词汇按水平校准：week_1_2 不要用"不变量""摊还""权衡""幂等"；week_7_plus 可以用
  技术词。中文提问，短。

置信度（CONFIDENCE）：
- high：分类有把握、证据支持；
- medium：分类合理，但存在说得通的另一种解释；
- low：确实说不清。这时问题要写成纯探索式（只问"这里是怎么回事"）。

输出格式：一个 JSON 对象，不要前言、不要 markdown 围栏：
{
  "divergences": [
    {
      "divergence_id": "<短标识，如 missing_null_check>",
      "initial_classification": "drift" | "revision" | "bug",
      "initial_confidence": "high" | "medium" | "low",
      "predicted_justification": "<他大概会怎么解释>",
      "student_facing_question": "<中性问题>",
      "evidence_from_spec": "<spec 里的原句或转述>",
      "evidence_from_code": "<代码片段>"
    }
  ]
}

没有任何有意义的分歧就返回 { "divergences": [] }。**不要**为了有东西可展示而编造分歧。"""


INTENT_DIFF_FEW_SHOTS = r"""示例 A —— week_1_2，drift：

题目：「写一个函数，数一个字符串里有多少个元音。」
学生水平：week_1_2

学生的 spec：
"函数接收一个字符串、返回一个数。它数 a、e、i、o、u，也数 A、E、I、O、U。
如果字符串是空的，返回 0。"

学生的 finalCode：
def count_vowels(s):
    count = 0
    for c in s:
        if c in 'aeiou':
            count = count + 1
    return count

输出：
{"divergences": [{"divergence_id": "case_insensitive_drift", "initial_classification": "drift", "initial_confidence": "high", "predicted_justification": "我忘了大写字母也算。", "student_facing_question": "你的说明里说大小写元音都要数（A、E、I、O、U 也算）。我注意到你的代码只检查了小写，能跟我说说这里是怎么回事吗？", "evidence_from_spec": "也数 A、E、I、O、U", "evidence_from_code": "if c in 'aeiou': —— 没有处理大写"}]}

示例 B —— week_3_6，bug（说好的做法机械地失败了）：

题目：「返回一串以空格分隔的词里出现次数最多的那个词。」
学生水平：week_3_6

学生的 spec：
"函数接收一串以空格分隔的词，返回出现最多的那个词。空串返回空串。
并列时取先出现的那个。"

学生的 finalCode：
def most_common(s):
    if s == "":
        return ""
    words = s.split()
    counts = {}
    for w in words:
        counts[w] = 1
    return max(counts, key=counts.get)

输出：
{"divergences": [{"divergence_id": "count_assignment_overwrites", "initial_classification": "bug", "initial_confidence": "high", "predicted_justification": "我以为我在累加，其实每个词都被写成 1 了。", "student_facing_question": "你的说明说返回出现最多的词。我注意到循环里是 counts[w] = 1，能带我说说这一行在做什么吗？", "evidence_from_spec": "返回出现次数最多的那个词", "evidence_from_code": "counts[w] = 1 —— 赋值，不是自增"}]}

示例 C —— week_3_6，数据题，drift（MVE 常见形态）：

题目：「把 math_docs 这张表的字段结构和记录情况说清楚。」
学生水平：week_3_6

学生的 spec：
"这张表有 9 个字段。我要逐列给出列名和类型，再给出总记录数；类型要按库里的真实
类型写（都是 VARCHAR），不能只说'文本'。"

学生的 finalCode（节选）：
cols = con.execute("SELECT * FROM math_docs LIMIT 1").df().columns
n = con.execute("SELECT count(*) FROM math_docs").fetchone()[0]
RESULT = {"column_count": len(cols), "row_count": n}

输出：
{"divergences": [{"divergence_id": "types_not_reported", "initial_classification": "drift", "initial_confidence": "high", "predicted_justification": "我数了列数，觉得列名和类型不重要就没取。", "student_facing_question": "你的说明里说要逐列给出列名和类型（都按库里的真实类型写）。我注意到程序只算了列数和行数，能跟我说说这里是怎么回事吗？", "evidence_from_spec": "逐列给出列名和类型……都是 VARCHAR", "evidence_from_code": "RESULT = {\"column_count\": len(cols), \"row_count\": n} —— 没有列名/类型"}]}

示例 D —— week_3_6，数据题，**一致 → 没有任何分歧**（必须返回空数组）：

题目：「orders 表有哪些列，各是什么类型？有没有主键？」
学生水平：week_3_6

学生的 spec：
"orders 有 4 列：order_id、user_id、amount、created_at，前三个是整数，created_at 是
时间。该表没有主键。"

学生的 finalCode（节选）：
r = query_sql("SELECT column_name, data_type FROM information_schema.columns WHERE table_name='orders'")
pk = query_sql("SELECT kcu.column_name FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu ON tc.constraint_name=kcu.constraint_name WHERE tc.table_name='orders' AND tc.constraint_type='PRIMARY KEY'")
RESULT = {"column_names": [x[0] for x in r["rows"]], "data_types": [x[1] for x in r["rows"]], "primary_key": pk["rows"]}

程序跑出来的结果：column_names 4 个、data_types 为 integer/integer/integer/text、primary_key 为 []

输出：
{"divergences": []}

说明：spec 的每条都是断言（4 列、类型、没有主键），而**每一条在结果里都有对应项**
（"整数/时间"与 integer/text 是同一件事的不同叫法；primary_key 为 [] 正是"没有主键"）。
**不要**因为"代码没有再复述一遍这些结论"或"查主键的 SQL 很长"就标分歧。

⚠️ 这条界限要看清：如果 RESULT 里**根本没有** `primary_key` 这一项（程序压根没查过主键），
那 spec 的"没有主键"就是**没有体现** → 标 drift。区别不在 spec 写了什么，而在
**结果里有没有这一项**：有 → 支持/矛盾；没有 → 漏做。
"""


def _intent_diff_call(messages: list[dict[str, str]]) -> str:
    """分类器：**开思考**，模型与 gate 同源但配置独立。

    依据 `llm_client.chat` 的注释：「只给**不需要深思**的调用关思考（出题器这类）。
    判官（reason_pass / task_critic / critic）**需要**思考，别动它们。」
    intent-diff 是"逐条比对 spec 与代码、再按偏向规则分类"的推理活（原版注释
    称它 highest-value prompt），所以这里**不**沿用 gate 的关闭思考；max_tokens
    给足 6000（思考段与正文共用额度，2026-11-11 的坑）。

    模型：MVE_ID_MODEL > **glm-4.6**（不继承 MVE_CRITIC_MODEL，见下）；
    `MVE_ID_THINKING=0` 可关。

    ⚠️ 2026-11-12 实测（Scene N，同一份 spec/code/result）：
      · glm-4-flash（当时的 MVE_CRITIC_MODEL）：把「spec 说没有主键、程序查了
        主键返回空」判成 bug，把「都是字符串 vs text」判成 bug —— 3 条分歧全是
        "一致当分歧"，两次跑逐字相同；
      · glm-4.6：同样输入返回 `{"divergences": []}`（正确）。
    原版称 intent-diff 是 highest-value prompt —— 这类判定不能省模型钱。
    """
    model = os.getenv("MVE_ID_MODEL", "") or "glm-4.6"
    kw: dict[str, Any] = {"temperature": 0, "model": model,
                          "max_tokens": 6000, "timeout": 240.0}
    if os.getenv("MVE_ID_THINKING", "1") not in ("0", "false", "no"):
        kw["thinking"] = {"type": "enabled"}
    try:
        from llm_client import chat as _chat
    except Exception:
        try:
            from mve.llm_client import chat as _chat
        except Exception:
            from voyager_vlml.llm import chat as _chat
            kw.pop("thinking", None)
    return _chat(messages, **kw)


def _alignment_call(messages: list[dict[str, str]]) -> str:
    """alignment 判定用的判官调用：prompt 仍是 `maieutic_gate`（不换判据），
    只把模型从默认的 flash 换掉。

    实测同一天：glm-4-flash 在"一致/矛盾"这类判定上不可用（见 `_intent_diff_call`
    注释）。alignment 判的是"这段回答有没有正面回应"，同属判定 —— 不能降到 flash。
    打勾活按 `llm_client.chat` 的注释关思考。
    """
    model = os.getenv("MVE_ID_MODEL", "") or "glm-4.6"
    kw: dict[str, Any] = {"temperature": 0, "model": model,
                          "max_tokens": 2000, "timeout": 180.0}
    # 打勾活**默认关思考**（与 gate 同一条，MVE_GATE_THINKING=1 可开）。
    # ⚠️ 2026-11-12 实测：这里若跟着 intent-diff 开思考，2000 tokens 会被思考段
    # 吃光 → content 为空 → 解析失败 → alignment=None（分歧 2 就是这么挂的）。
    if os.getenv("MVE_GATE_THINKING", "") not in ("1", "true", "yes"):
        kw["thinking"] = {"type": "disabled"}
    try:
        from llm_client import chat as _chat
    except Exception:
        from mve.llm_client import chat as _chat
    return _chat(messages, **kw)


POST_HOC_SYSTEM = """你在做一次课后复盘的**后置判定**（原版 `post-hoc.ts` 同一件事）。
学生刚被问到一处「说明和程序对不上」的地方，他用自己的话回答了。判两件事：

1. alignment —— 他的回答与**初判背后的预测**之间的关系（只看这一条，三档）：
   · aligned：他讲的跟预测**基本一致**（承认漏了 / 承认写错 / 承认换了做法，
     且说法与预测吻合）；
   · partial：与预测有重叠，但缺关键要素（只说了一半、只承认一部分，或理由
     拐到了别的方向）；
   · diverged：他的推理与预测**实质不同**（预测"他忘了"，他说"我是故意这么做
     的，因为……"；或给出完全另一套可核对的解释）。

2. final_classification —— 按**他实际说了什么**重判。规则是反过来的（原版规定）：
   · 只要他给出了**连贯的理由 / 正当性**（不论初判是哪一类）→ revision
     （能把选择的价值讲清楚，是系统最在意的结果）；
   · 他承认"忘了 / 没注意 / 漏了 / 没想到" → drift；
   · 他指出这是**机械性错误**（写错运算符、差一、类型错、赋值当自增、缩进错）→ bug；
   · 他说"不知道 / 说不清" → **保持初判**不变（系统不凭空发明确定性）。

判定只依据三样：说明的原话、程序片段与结果、他的回答。
不要因为他讲得流利就判 aligned；也不要因为他承认得干脆就把分类加重。
回答为空、只复述问题、只贴工具输出/路径/表格/JSON、或只说"我去查一下"
→ alignment=diverged，并按"不知道"处理（final 保持初判）。

只输出一个 JSON 对象，不要前言、不要 markdown：
{"alignment": "aligned" | "partial" | "diverged",
 "final_classification": "drift" | "revision" | "bug",
 "final_classification_reason": "<一句话，说清为什么这么判>"}"""

def _post_hoc(d: dict[str, Any], response: str, *,
              llm: Callable | None = None) -> dict[str, Any]:
    """后置判定（原版 Tech Spec §5.4 / `PostHocOutput`）。

    ⚠️ 本仓没有 §5.4 的 prompt 原文，这里按 `PostHocOutput` 的三个字段
    （alignment / final_classification / final_classification_reason）与原版
    alignment 的定义写的最小实现 —— 判的是"回答与初判对不对得上"，**不是**
    "回答有没有答到"（后者是 examiner 的活）。若拿到上游原文，替换
    `POST_HOC_SYSTEM` 即可，调用链不变。
    """
    out: dict[str, Any] = {"alignment": None, "final_classification": None,
                           "reason": "", "origin": "failed"}
    _raw = str(response or "").strip()
    if not _raw:
        out.update(alignment="diverged", reason="没有作答（空回答）",
                   origin="llm")
        out["final_classification"] = str(
            d.get("initial_classification") or "drift")
        return out
    user = (
        "【说明里的原话】\n" + str(d.get("evidence_from_spec") or "")[:600] + "\n\n"
        + "【程序片段】\n" + str(d.get("evidence_from_code") or "")[:600] + "\n\n"
        + "【初判】" + str(d.get("initial_classification") or "") + "（置信度 "
        + str(d.get("initial_confidence") or "") + "）\n"
        + "【当初预测他会怎么说】" + str(d.get("predicted_justification") or "")[:400]
        + "\n\n【问他的问题】" + str(d.get("student_facing_question") or "")[:400]
        + "\n\n【他的回答】\n\"\"\"\n" + _raw[:2000] + "\n\"\"\"\n\n"
        + "判 alignment 与 final_classification。只输出 JSON。")
    call = llm or _intent_diff_call
    last = ""
    for _ in range(2):
        try:
            text = call([{"role": "system", "content": POST_HOC_SYSTEM},
                         {"role": "user", "content": user}])
        except Exception as e:
            last = f"LLM 调用失败：{type(e).__name__}: {e}"
            continue
        try:
            import maieutic_gate as _mg
            data = _mg._parse(text)
        except Exception as e:
            last = f"解析失败：{type(e).__name__}: {e}"
            continue
        if not data:
            last = "输出解析不出 JSON"
            continue
        _al = str(data.get("alignment") or "").strip().lower()
        _fc = str(data.get("final_classification") or "").strip().lower()
        if _al not in ("aligned", "partial", "diverged"):
            last = "alignment 不是三值"
            continue
        if _fc not in ("drift", "revision", "bug"):
            _fc = str(d.get("initial_classification") or "drift")
        out.update(alignment=_al, final_classification=_fc,
                   reason=str(data.get("final_classification_reason") or "")[:300],
                   origin="llm")
        return out
    out["reason"] = last or "后置判定不可用"
    return out


def _student_words(spec_text: str, limit: int = 8000) -> str:
    """从 session 整篇里只取**学生说的话**当 spec 面。

    依据 `spec_route.ts:16`（Phase1 入参只有 `specText`）+ `Phase1Data.studentSpecText`：
    原版交给分类器的是学生自己写的那段说明，不是判官的判词。MVE 的整篇是
    session 过程记录（`render_session_text`），其中「判定：…」是 examiner 的话、
    「判官（整篇）：…」是判官的段落 —— 它们不属于承诺面，混进来会让分类器拿
    判词去对代码（把"闭合"当成学生承诺）。这里按行过滤；过滤后为空则退回原文。
    """
    keep: list[str] = []
    for line in str(spec_text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("判定：") or s.startswith("判官（整篇）"):
            continue
        if "判官（整篇）" in s and s.startswith("[轮"):
            continue
        keep.append(line.rstrip())
    text = "\n".join(keep).strip()
    return (text or str(spec_text or "").strip())[:limit]


async def intent_diff(scene_id: str, question: str, spec_text: str,
                      final_code: str, *, level: str = "",
                      expected: list[str] | None = None,
                      revisions: list[dict[str, Any]] | None = None,
                      run_result: dict[str, Any] | None = None,
                      llm: Callable | None = None,
                      max_items: int = 3) -> dict[str, Any]:
    """Phase3 第一段：spec 全文 vs finalCode 全文 → divergences（原版分类器）。

    对应 `intentDiffOutputToDivergences`（`opus/schemas.ts:441-458`）：LLM 交
    snake_case，这里转成原版 `Divergence` 的字段（含 post-hoc 的 null 位）。
    `classification` 不在三值里时按 BIAS RULE 落 revision（原版同样的偏向：
    说不清就不指控 drift）。

    返回 {"divergences": [...], "origin": "llm"|"failed", "reason": ""}。
    """
    out: dict[str, Any] = {"divergences": [], "origin": "failed", "reason": ""}
    spec = str(spec_text or "").strip()
    code = str(final_code or "").strip()
    if not spec or not code:
        out["reason"] = "缺 spec 全文或 finalCode 全文，做不了 intent-diff"
        return out
    _lv = str(level or os.environ.get("MVE_STUDENT_LEVEL") or "week_3_6")
    _exp = "\n".join("  - " + str(x)[:200] for x in (expected or [])) or "  （未提供）"
    _revs = [r for r in (revisions or []) if isinstance(r, dict)]
    _rev = ("\n".join(
        "  修订 %d：改动=%s｜理由=%s" % (
            i + 1, str(r.get("amendment") or "")[:300],
            str(r.get("justification") or "")[:200])
        for i, r in enumerate(_revs))) or "  （没有会话内修订）"
    # Phase2 已经把 finalCode 真跑过一遍（`verified_result` 就是那一轮的 RESULT）。
    # 结论型 spec 的断言要拿"代码 + 真实结果"去对 —— 只给代码文本，分类器会
    # 把"代码没再复述这个结论"误判成分歧（2026-11-12 实测：spec 说"没有主键"、
    # 程序查主键返回空，被判成 bug）。
    _result_block = ""
    if isinstance(run_result, dict) and run_result:
        try:
            _rt = json.dumps(run_result, ensure_ascii=False, default=str)
        except Exception:
            _rt = str(run_result)
        _result_block = (
            "\n【程序真实跑出来的结果（Phase2 执行得到，不是声明）】\n\"\"\"\n"
            + _rt[:6000] + "\n\"\"\"\n\n")
    user = (
        INTENT_DIFF_FEW_SHOTS
        + "\n\n现在轮到你了。下面是真实的提交。\n\n"
        + "题目：" + str(question or "")[:2000] + "\n"
        + "学生水平：" + _lv + "\n\n"
        + "本题的预期分歧（只作参考锚点 —— 不必强行对上，一切以实际代码为准）：\n"
        + _exp + "\n\n"
        + "【学生的 spec 全文】\n\"\"\"\n" + spec + "\n\"\"\"\n\n"
        + "【会话内修订】\n" + _rev + "\n\n"
        + "【学生的 finalCode】\n\"\"\"\n" + code[:12000] + "\n\"\"\"\n\n"
        + _result_block
        + "逐条分类分歧。按 schema 只输出 JSON。")
    call = llm or _intent_diff_call
    last = ""
    for _ in range(3):
        try:
            text = call([{"role": "system", "content": INTENT_DIFF_SYSTEM},
                         {"role": "user", "content": user}])
        except Exception as e:
            last = f"LLM 调用失败：{type(e).__name__}: {e}"
            continue
        try:
            import maieutic_gate as _mg
            data = _mg._parse(text)
            if not data or not isinstance(data.get("divergences"), list):
                last = "输出解析不出 divergences"
                _mg._dump_raw(text)
                continue
        except Exception as e:
            last = f"解析失败：{type(e).__name__}: {e}"
            continue
        divs: list[dict[str, Any]] = []
        for d in data.get("divergences") or []:
            if not isinstance(d, dict):
                continue
            _c = str(d.get("initial_classification") or "").strip().lower()
            if _c not in ("drift", "revision", "bug"):
                _c = "revision"          # BIAS RULE：说不清不指控 drift
            _cf = str(d.get("initial_confidence") or "").strip().lower()
            if _cf not in ("high", "medium", "low"):
                _cf = "low"
            _q = str(d.get("student_facing_question") or "").strip()
            if not _q:
                continue
            divs.append({
                "divergence_id": (str(d.get("divergence_id") or "").strip()
                                  or f"d{len(divs) + 1}")[:60],
                "initial_classification": _c,
                "initial_confidence": _cf,
                "predicted_justification": str(
                    d.get("predicted_justification") or "")[:600],
                "student_facing_question": _q[:600],
                "evidence_from_spec": str(d.get("evidence_from_spec") or "")[:600],
                "evidence_from_code": str(d.get("evidence_from_code") or "")[:600],
                # post-hoc 位（原版 Divergence schema 同款，答完再填）
                "student_response": None,
                "alignment": None,
                "final_classification": None,
                "final_classification_reason": None,
                "responded_at": None,
            })
        out.update(divergences=divs[:max(1, int(max_items))], origin="llm",
                   reason="")
        return out
    out["reason"] = last or "分类器不可用"
    return out


def _final_text_of(dims: list[dict[str, Any]] | None,
                   answers: dict[str, Any] | None,
                   fallback: str = "") -> str:
    """Phase1 的 **finalText**：学生自己写下的那份说明（原版 `studentSpecText`）。

    原版交卷时 `Phase1Data.studentSpecText` = 学生针对每个问题写下的回答拼成的
    一份说明；Phase2 的 intent-diff 吃的就是它 —— 不是 examiner 的判词、也不是
    代码执行日志。MVE 的会话整篇里混着这三样，所以这里**按维度账重拼**：
    维度问题 + 学生那一轮写的答案。拼不出来才退回整篇过滤版。
    """
    parts: list[str] = []
    for d in (dims or []):
        if not isinstance(d, dict):
            continue
        _q = str(d.get("question") or "").strip()
        _a = str((answers or {}).get(str(d.get("id") or ""))
                 or d.get("answer") or "").strip()
        if not _a or _a in ("（未作答）", "未作答"):
            continue
        if _a.startswith("（本轮在材料上没答到"):
            continue
        parts.append(("【" + _q + "】\n" if _q else "") + _a)
    text = "\n\n".join(parts).strip()
    return text or _student_words(str(fallback or ""))

def _prose_of(obs: list[dict[str, Any]]) -> str:
    """从观察里取它**说的话**（RESULT 里的 answer / 纯文本），去掉 JSON 外壳。"""
    txt = _answer_text(list(obs or []), ok_only=True) or _answer_text(list(obs or []))
    if not str(txt or "").strip():
        return ""
    try:
        d = json.loads(txt)
        if isinstance(d, dict):
            for k in ("answer", "response", "回答", "结论"):
                v = d.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()[:2000]
            _vs = [str(v) for v in d.values() if isinstance(v, str) and v.strip()]
            if len(_vs) == 1:
                return _vs[0][:2000]
    except Exception:
        pass
    return str(txt).strip()[:2000]


def _looks_like_tool_output(text: str) -> bool:
    """这段"回答"是不是工具输出冒充的。

    原版 Phase3 的界面是 Divergence Q&A 卡片：没有编辑器、没有控制台、没有
    Pyodide 运行时（`ExerciseClient` 只在 Phase2 挂 `PythonEditor`）。学生能做的
    只有**用话回答**。MVE 的学生是沙箱 agent，会条件反射地再查一次库把结果贴
    回来 —— 那不是回答（实测：`/root/mve/scene_data/N/math.duckdb`、
    `{"toc_rows": {...}}` 被当成 studentResponse，后置判定只能判 diverged）。
    """
    s = str(text or "").strip()
    if not s:
        return False
    if s[:1] in "{[" and ('"rows"' in s or '"columns"' in s or '"补查' in s):
        return True
    if s.startswith("/") and "\n" not in s and len(s) < 300:
        return True
    if '"columns"' in s and '"rows"' in s:
        return True
    if s.startswith("[{") and "path" in s and "name" in s:
        return True
    return False


async def _answer_divergence(scene_id: str, question: str,
                             d: dict[str, Any], *, db_path: str = "",
                             scene_dir: str = "", llm: Callable | None = None,
                             env_obs: str = "") -> dict[str, Any]:
    """学生对**一条**分歧作答（原版 `divergence-response` 的输入面）。

    ⚠️ 不给分类、不给预测（原版 `studentFacingQuestion` 就是学生能看到的全部；
    `initialClassification` / `predictedJustification` 只给老师看）。
    ⚠️ 这一步**不能取数**：Phase3 视图没有编辑器与控制台，取证是 Phase2 的事。
    """
    rec: dict[str, Any] = {"response": "", "code": "", "evidence": [],
                           "error": "", "retried": False}
    try:
        import local_agent as _la
    except Exception as e:
        rec["error"] = f"执行器不可用：{type(e).__name__}: {e}"
        return rec
    _head = (
        "【复盘问答：只回答这一条】\n"
        "（原题已经交过了。这一步**没有代码编辑器、不能运行程序、不能查数据源**——"
        "你面前只有这一张问答卡片，用你自己的话回答。贴查询结果、贴路径、贴表格"
        "都不算回答。）\n\n"
        "题面：" + str(question or "")[:1500] + "\n\n"
        + "你的说明（spec）里说：\n" + str(d.get("evidence_from_spec") or "")[:600]
        + "\n\n你的最终程序里这一段是：\n"
        + str(d.get("evidence_from_code") or "")[:600] + "\n\n"
        + "要你回答的问题：" + str(d.get("student_facing_question") or "")[:600])
    _steps = (
        "\n\n按这三步，一段话说完：\n"
        "  1) 指认：程序里哪一行 / 哪几行负责这件事？（引用关键那行；程序里"
        "**没有**这一行就直说「没有」）；\n"
        "  2) 当时的想法：写这段的时候我是怎么想的？\n"
        "  3) 现在回头看，属于哪一种（选一个，并用一句话说清）：\n"
        "     · 我漏了 / 没注意 —— 说明里要求了，写代码时忘了；\n"
        "     · 我当时故意换了个做法 —— 理由是……（说明的意图满足了，只是写法不同）；\n"
        "     · 我想按说明做，但机械地写错了 —— 写错运算符 / 差一 / 类型 / "
        "赋值当自增。\n"
        "**不要**重答原题、不要复述表结构、不要贴 JSON/路径/查询结果、"
        "不要介绍这套流程。")
    ev: list[dict[str, Any]] = []
    try:
        obs, code = await _la.solve(
            _head + _steps, str(scene_dir or ""), llm=llm,
            scene_id=str(scene_id or ""), max_iters=INNER_MAX_ITERS,
            critique="", evidence=ev, require_tools=False,
            timeout=EXEC_TIMEOUT, db_path=str(db_path or ""),
            context=str(env_obs or ""), deliver="prose")
    except Exception as e:
        rec["error"] = f"作答 rollout 抛错 {type(e).__name__}: {e}"
        return rec
    _resp = _prose_of(list(obs or []))
    # 形态守卫：贴回工具输出 = 没作答（原版学生没有这个通道）。重问一次，
    # 第二次仍贴就如实记下来（后置判定会判 diverged，不会被当成"答到了"）。
    if _looks_like_tool_output(_resp):
        rec["retried"] = True
        _hard = (
            _head + "\n\n你刚才交回来的那一段不是回答，是查询结果/路径。"
            "这一步没有编辑器、没有控制台，再多的表格也不算回答。"
            "现在只用你自己的话：指认哪一行（或直说没有）、当时怎么想、"
            "现在归为哪一种（漏做 / 故意换法并给理由 / 写错）。"
            "不超过 5 句，不要贴任何数据。")
        _ev2: list[dict[str, Any]] = []
        try:
            obs2, code2 = await _la.solve(
                _hard, str(scene_dir or ""), llm=llm,
                scene_id=str(scene_id or ""), max_iters=2, critique="",
                evidence=_ev2, require_tools=False, timeout=EXEC_TIMEOUT,
                db_path=str(db_path or ""), context=str(env_obs or ""),
                deliver="prose")
            _r2 = _prose_of(list(obs2 or []))
            if _r2 and not _looks_like_tool_output(_r2):
                _resp, code = _r2, code2
                ev = _ev2
        except Exception:
            pass
    rec["code"] = str(code or "")
    rec["evidence"] = list(ev or [])
    rec["response"] = _resp
    return rec
def _judge_alignment(d: dict[str, Any], response: str, *,
                     evidence: list[dict[str, Any]] | None = None,
                     env_obs: str = "", code: str = "") -> dict[str, Any]:
    """一条分歧的 alignment —— **后置判定优先**（原版 Tech Spec §5.4）。

    原版 alignment 的语义是「学生的回答有没有印证这条分歧的初判」
    （`sessions.ts:413-455` 收的就是这三个值）：
      · aligned  = 他承认了（"我确实没做/漏了/写错了"）→ 这条分歧**真的存在**；
      · diverged = 他否认并给出可核对依据（"我做了，在 X 行/结果里有"）→ 初判站不住；
      · partial  = 一半承认一半否认。
    ⇒ 需要修订的是 aligned/partial（确有缺口），不是 diverged（其实一致）。

    gate（examiner）只在后置判定不可用时**兜底**：它判的是"有没有正面回应"，
    与原义不同（回应了 = 有内容，不等于初判成立），所以 origin 标 gate_fallback。
    """
    _ph = _post_hoc(d, response)
    if str(_ph.get("origin") or "") == "llm":
        return {"alignment": _ph.get("alignment"),
                "final_classification": _ph.get("final_classification"),
                "reason": str(_ph.get("reason") or "")[:300],
                "origin": "post_hoc", "questions": []}
    out: dict[str, Any] = {"alignment": None, "final_classification": None,
                           "reason": str(_ph.get("reason") or "")[:200],
                           "origin": "gate_fallback", "questions": []}
    _q = (str(d.get("student_facing_question") or "").strip()
          + "\n（判据：这段回答有没有正面回应这条问题、并给出可核对的实际情形；"
            "只复述问题、只说要去做、或只给结论没有指认，都不算。）")
    try:
        import maieutic_gate as _mg
        _mat = [x for x in (evidence or [])
                if isinstance(x, dict) and x.get("ok")]
        g = _mg.check(_q, str(response or ""),
                      [{"id": str(d.get("divergence_id") or "d1"),
                        "question": _q}],
                      evidence=_mat, env_obs=str(env_obs or ""),
                      sql=str(code or ""),
                      material_required=bool(_mat),
                      llm=_alignment_call)
    except Exception as e:
        out["reason"] = f"{type(e).__name__}: {e}"
        return out
    out.update(reason=str(g.get("reason") or out["reason"])[:200],
               questions=list(g.get("questions") or [])[:2])
    if str(g.get("origin") or "") != "llm":
        return out
    # gate 的"回应了"映射成 aligned（兜底语义），finalClassification 沿用初判
    out["alignment"] = ("aligned" if (g.get("passed")
                                      and str(response or "").strip())
                        else ("partial" if (g.get("addressed") or [])
                              else "diverged"))
    out["final_classification"] = str(d.get("initial_classification") or "drift")
    return out


def _nosrc_rev(scene_id: str) -> bool:
    """这个场景有没有**可查的外部材料**（判环境事实，不判学科）。"""
    try:
        import scene_profile as _sp
        return not bool(_sp.grounded(scene_id))
    except Exception:
        return False


def _derived_ok(code: str) -> bool:
    """RESULT 里有没有至少一个不是抄来的值（推导型任务的"真做过"）。"""
    try:
        import skill_debug as _sd
        import re as _re
        m = _re.search(r"RESULT\s*=\s*(\{.*\})", str(code or ""), _re.S)
        if m:
            import json as _json
            try:
                R = _json.loads(m.group(1))
            except Exception:
                R = None
            if isinstance(R, dict) and _sd.derived(R, str(code or "")):
                return True
        _chk = _sd.run_code(str(code or ""), "", quiet=True, timeout=30)
        return bool(_chk.get("ok")) and bool(
            _sd.derived(_chk.get("RESULT"), str(code or "")))
    except Exception:
        return False


async def _final_revision(scene_id: str, question: str, code: str,
                          divs: list[dict[str, Any]], *, db_path: str = "",
                          scene_dir: str = "", llm: Callable | None = None,
                          env_obs: str = "", spec_text: str = "",
                          base_result: dict[str, Any] | None = None,
                          ) -> tuple[str, str, str]:
    """Phase3 收尾（原版 `/finalize` + revision coda）。

    原版顺序（`finalize/route.ts`、`sessions.ts:457-482`）：
      ① 全部分歧答完（`completedAt` 已写）；
      ② 学生**自己选** skip 还是 revise —— 交了 `revisedCode` 记 revised；
      ③ `revisedCode` 单独存；原始 finalCode、分歧问题、回答与分类**冻结不动**。

    MVE 加严两处（原版面对人类学生可以摆烂；这里的"学生"是 Voyager，验收口径
    是"它写的技能要能用"）：
      · 复判为 drift / bug 的分歧 = **确认的缺口** → 不允许 skip；
      · 修订版必须真跑通、有 RESULT、且多出缺的那些产出；不达标记
        `revise_failed`，与"自己决定不改"（skipped）分开记账。

    为什么要把 **spec 全文**给它（2026-11-13 实测）：只给 `evidence_from_spec`
    的 200 字片段时，它只补局部，交回来的还是"探查 + 一段推导"，没有 RESULT。
    原版学生面前有自己刚写完的整篇 spec（Phase1 textarea 就在上面）；MVE 的
    学生是零上下文重启的，不把整篇还给它，就等于让它凭片段猜整体承诺。
    返回 (choice, revised_code, note)。
    """
    _gaps = [d for d in (divs or [])
             if str(d.get("final_classification")
                    or d.get("initial_classification") or "") in ("drift", "bug")]
    if not _gaps:
        return ("skipped", "",
                "复判后没有确认的缺口（都是合理替代或其实一致），按原版允许跳过")
    if not str(code or "").strip():
        return "skipped", "", "没有 finalCode，没有可修订的对象"
    try:
        import local_agent as _la
    except Exception:
        return "skipped", "", "执行器不可用"
    _lines = []
    for i, d in enumerate(_gaps[:3], 1):
        _lines.append(
            str(i) + ") 说明里承诺：" + str(d.get("evidence_from_spec") or "")[:200]
            + "\n   程序里：" + str(d.get("evidence_from_code") or "")[:200]
            + "\n   复判：" + str(d.get("final_classification") or "")
            + "；他当时的说法：" + str(d.get("student_response") or "")[:300])
    task = (
        "【revise：按复盘结论交一版能单跑完的程序】\n"
        + "题面：" + str(question or "")[:1200] + "\n\n"
        + "你自己写的说明（这是你自己的承诺，整篇都在这里，按它交）：\n"
        + str(spec_text or "")[:3000] + "\n\n"
        + "你上一版程序：\n```python\n" + str(code or "")[:5000] + "\n```\n\n"
        + "复盘确认下面这些是**真缺口**（说明承诺了、程序没做到）：\n"
        + "\n".join(_lines) + "\n\n"
        + "要求：\n"
        + "  · 交的是**一份完整程序**，不是补丁、不是解释：从查书开始，按你说明里"
          "的步骤一路走到最后一步，末尾用一个 `RESULT` 字典交出每一步的产出"
          "（键名就说那一步拿到了什么）；\n"
        + "  · 上面每条缺口都要真的补上：说明里要查数据源的就真去查，"
          "要算的就真用代码算出来（不许把答案写成字面量）；原来已交付且没有"
          "分歧的内容照旧保留；\n"
        + "  · **不许写占位符**（PENDING / TODO / 待补 / 待实现 / 具体实现留待）"
          "——做不出来就在 RESULT 里如实给空值和原因，那也算结论；\n"
        + "  · 只交一段可执行的 Python，不要 markdown。")
    try:
        obs, new_code = await _la.solve(
            task, str(scene_dir or ""), llm=llm, scene_id=str(scene_id or ""),
            max_iters=INNER_MAX_ITERS,
            # 取材方式跟着 spec 走：不预设"必须调数据源"。
            # 修订有没有解决问题，由 Phase3 后面的 intent-diff 判。
            require_tools=False,
            timeout=EXEC_TIMEOUT, db_path=str(db_path or ""),
            context=str(env_obs or ""))
    except Exception as e:
        _r = ("修订 rollout 抛错 " + type(e).__name__ + ": " + str(e))[:200]
        print("  · Phase3 修订失败：" + _r, flush=True)
        return "revise_failed", "", _r
    if not str(new_code or "").strip():
        print("  · Phase3 修订失败：没交出代码", flush=True)
        return "revise_failed", "", "修订没交出代码"
    # 只要求它能跑起来（原版 Phase3 修订完也要 Run 一次给用户看）；做没做到
    # 由后面的 intent-diff 判，不在这里预设"必须取数 / 值不能是字面量"。
    if not any(isinstance(o, dict) and o.get("ok") for o in (obs or [])):
        print("  · Phase3 修订失败：修订版没跑通", flush=True)
        return "revise_failed", "", "修订版没跑通"
    if str(new_code or "").strip() == str(code or "").strip():
        print("  · Phase3 修订失败：修订版与原版逐字相同", flush=True)
        return "revise_failed", "", "修订版与原版逐字相同（没改）"
    # 质量门（原版 finalize 只看"交没交"；MVE 把门放在执行侧）：修订版必须能在
    # **将来调用它的那个执行域**里零上下文单跑一遍 —— 跑通 + 有 RESULT +
    # 至少一次真取数 + 无占位符（`skill_debug.verdict`，与入库门同一把尺）。
    # ⚠️ 2026-11-13 实测：曾经用"必须新增 RESULT 键"当判据，把"把某个键改对
    # （derived_answer 从 null 变成真值）"误判成"改而未补" —— 键名不变不等于没改。
    _ok, _why = True, ""
    try:
        import skill_debug as _sd
        import asyncio as _aio
        _chk = await _aio.to_thread(
            _sd.run_code, str(new_code), str(scene_id or "N"), quiet=True,
            timeout=float(os.environ.get("MVE_SKILL_GATE_TIMEOUT") or 60))
        _ok, _why = _sd.verdict(_chk)
    except Exception as e:
        _ok, _why = True, ("（单技能调试不可用：" + type(e).__name__ + "）")
    if not _ok:
        print("  · Phase3 修订失败：" + str(_why)[:160], flush=True)
        return ("revise_failed", "",
                ("修订版不过单技能调试：" + str(_why))[:220])
    _new_keys = _result_keys(list(obs or []))
    return ("revised", str(new_code),
            ("补上 " + str(len(_gaps)) + " 条确认缺口｜RESULT "
             + str(len(_new_keys)) + " 键" + ("｜" + _why if _why else "")))


def _literal_bound_names(code: str) -> set[str]:
    """被**字面量**赋值的变量名（`x = []` / `x = ["a"]` / `x = 0`）。"""
    import ast as _ast
    out: set[str] = set()
    try:
        tree = _ast.parse(code)
    except Exception:
        return out
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Assign) and isinstance(
                node.value, (_ast.List, _ast.Dict, _ast.Tuple,
                             _ast.Set, _ast.Constant)):
            for t in node.targets:
                if isinstance(t, _ast.Name):
                    out.add(t.id)
    return out


def _hardcoded_result_keys(code: str) -> set[str]:
    """RESULT 里**写死**的键（值直接是字面量，或来自只被字面量赋过的变量）。

    为什么需要（2026-11-12 实测）：修订版把缺的项写成
    `primary_key_not_checked = []` 再塞进 RESULT —— 键数够了、一次都没查，
    记成 `revised` 就是假账。这条守卫只看字面量，不判业务对错。
    """
    import ast as _ast
    out: set[str] = set()
    try:
        tree = _ast.parse(code)
    except Exception:
        return out
    _lits = _literal_bound_names(code)
    for node in _ast.walk(tree):
        if not (isinstance(node, _ast.Assign)
                and isinstance(node.value, _ast.Dict)):
            continue
        if not any(isinstance(t, _ast.Name) and t.id == "RESULT"
                   for t in node.targets):
            continue
        for k, v in zip(node.value.keys, node.value.values):
            key = (k.value if isinstance(k, _ast.Constant)
                   and isinstance(k.value, str) else "")
            if not key:
                continue
            if isinstance(v, (_ast.List, _ast.Dict, _ast.Tuple,
                              _ast.Set, _ast.Constant)):
                out.add(key)
            elif isinstance(v, _ast.Name) and v.id in _lits:
                out.add(key)
    return out


def _result_keys(obs: list[dict[str, Any]]) -> set[str]:
    """取最后一轮成功观察里的 RESULT 键（RESULT 落在 obs 的 data 上）。"""
    for o in reversed(list(obs or [])):
        if isinstance(o, dict) and o.get("ok") and isinstance(o.get("data"), dict):
            return {str(k) for k in o["data"].keys()}
    return set()


async def phase3_intent_diff_review(scene_id: str, question: str,
                                    spec_text: str, final_code: str, *,
                                    level: str = "", db_path: str = "",
                                    scene_dir: str = "", llm: Callable | None = None,
                                    env_obs: str = "", max_items: int = 0,
                                    run_result: dict[str, Any] | None = None,
                                    topic_id: str = "",
                                    req_id: str = "",
                                    milestone_name: str = "",
                                    pre_divergences: list[dict[str, Any]]
                                    | None = None,
                                    ) -> dict[str, Any]:
    """Phase3 全流程（原版顺序，一步不少）：

        intent-diff 出分歧 → 逐条问学生 + 判 alignment → 全答完 → revision pass

    只在 Phase1 闭合（有冻结的 spec）且 Phase2 交出 finalCode 时跑 —— 没有
    "做成面"就没有可对账的东西（原版 submit 也只接受 phase==2 的提交）。
    返回结构对齐 `Phase3Data`：divergences / started_at / completed_at /
    revision_choice / revised_code / revised_at，外加 MVE 的过程字段。
    """
    _max = int(max_items or os.environ.get("MVE_P3_MAX") or 3)
    out: dict[str, Any] = {
        "divergences": [], "checked": [], "aligned": [], "partial": [],
        "diverged": [], "skipped": [], "started_at": "", "completed_at": "",
        "revision_choice": None, "revised_code": "", "revised_at": "",
        "counts": {},
    }
    spec = _student_words(str(spec_text or ""))
    code = str(final_code or "")
    if not spec or not code.strip():
        out["skipped"].append("没有 spec 全文或 finalCode 全文，Phase3 无从对账")
        return out
    out["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    # Phase2 提交时已经对过一次账（那就是"过不过关"的判据）。有结果就直接用，
    # 不再烧第二遍分类器 —— 两次 LLM 判定还可能互相矛盾。
    if pre_divergences is not None:
        _idf = {"divergences": [d for d in pre_divergences
                                if isinstance(d, dict)],
                "origin": "llm", "reason": ""}
    else:
        _idf = await intent_diff(scene_id, question, spec, code,
                                 level=level, max_items=_max,
                                 run_result=run_result)
    if str(_idf.get("origin") or "") != "llm":
        out["skipped"].append("分类器不可用：" + str(_idf.get("reason") or "")[:160])
        return out
    divs = [d for d in (_idf.get("divergences") or []) if isinstance(d, dict)]
    out["divergences"] = divs
    if not divs:
        out["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        out["revision_choice"] = "skipped"
        out["skipped"].append("没有有意义的分歧（spec 与 finalCode 一致）")
        _save_phase3(scene_id, question, out)
        return out
    for d in divs:
        # 原版 `/divergence-response` 的幂等：已答过的分歧不再答一遍（409）。
        if str(d.get("student_response") or "").strip() and d.get("alignment"):
            out["checked"].append({
                "divergence_id": d.get("divergence_id"),
                "classification": d.get("initial_classification"),
                "alignment": d.get("alignment"),
                "response": str(d.get("student_response"))[:600],
                "reused": True, "origin": "stored", "reason": "已回答过",
            })
            continue
        _r = await _answer_divergence(
            scene_id, question, d, db_path=db_path, scene_dir=scene_dir,
            llm=llm, env_obs=env_obs)
        _resp = str(_r.get("response") or "")
        _g = _judge_alignment(d, _resp, evidence=_r.get("evidence"),
                              env_obs=env_obs, code=str(_r.get("code") or ""))
        _al = _g.get("alignment")
        d["student_response"] = _resp or None
        d["alignment"] = _al
        # 后置判定（§5.4）给了 final_classification 就用它；兜底时沿用初判
        d["final_classification"] = (
            _g.get("final_classification")
            or d.get("initial_classification"))
        d["final_classification_reason"] = (
            ("后置判定：" if str(_g.get("origin") or "") == "post_hoc"
             else "沿用初判（后置判定不可用）：")
            + str(_g.get("reason") or ""))[:400]
        d["responded_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        out["checked"].append({
            "divergence_id": d.get("divergence_id"),
            "classification": d.get("initial_classification"),
            "alignment": _al,
            "response": _resp[:600],
            "evidence_len": len(_r.get("evidence") or []),
            "origin": str(_g.get("origin") or ""),
            "reason": str(_g.get("reason") or "")[:160],
        })
        _bucket = {"aligned": "aligned", "partial": "partial",
                   "diverged": "diverged"}.get(str(_al or ""))
        if _bucket:
            out[_bucket].append(str(d.get("divergence_id") or ""))
        if _al == "diverged":
            # 原版 sessions.ts:446-452：alignment_failure 事件（prediction + response）
            try:
                import maieutic_scaffold as _ms_ev
                _ms_ev.append_session_event(
                    scene_id, question, "alignment_failure", {
                        "divergence_id": str(d.get("divergence_id") or ""),
                        "prediction": str(d.get("predicted_justification") or ""),
                        "response": _resp[:600],
                    })
            except Exception:
                pass
    out["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _choice, _rev_code, _rev_note = await _final_revision(
        scene_id, question, code, divs, db_path=db_path, scene_dir=scene_dir,
        llm=llm, env_obs=env_obs, spec_text=spec, base_result=run_result)
    out["revision_choice"] = _choice
    out["revision_note"] = str(_rev_note or "")[:300]
    out["revised_code"] = str(_rev_code or "")
    out["revised_at"] = (time.strftime("%Y-%m-%dT%H:%M:%S")
                         if _rev_code else "")
    # ★ 把修订版**写回技能库**（2026-11-13 用户拍板：Phase3 的分歧要能改技能）。
    # 原版 revision 是 coda（`sessions.ts:457-482` 只记 revisedCode、不动 finalCode
    # 与分类）—— 因为原版没有技能库。MVE 把 finalCode 沉淀成技能，若修订版只写
    # 进 ckpt，同一道题下一轮还是拿到那份没过关的码，分歧就白问了。这里用**同
    # key 覆盖**（Voyager `add_new_skill` 的 Rewriting 语义：旧版进 history）把
    # 草稿升级成 verified=True；学习信号（divergences/分类）仍冻结不动。
    if (_choice == "revised" and str(_rev_code or "").strip()
            and str(topic_id or "").strip()):
        try:
            _sa, _sk_key = save_final_code_skill(
                scene_id, str(topic_id), question, str(_rev_code),
                milestone_name=str(milestone_name or ""),
                dims=[], req_id=str(req_id or ""),
                verified=True, from_phase="maieutic_phase3_revision")
            out["skill_written"] = str(_sk_key or "")
            out["skill_action"] = str(_sa or "")
            print("  · Phase3 修订版写入技能库（已验证）："
                  + str(_sk_key or ""), flush=True)
        except Exception as _ske:
            out["skill_error"] = f"{type(_ske).__name__}: {_ske}"
    # 复判后没有确认缺口（每条都是 aligned / 合理替代）→ **原件**就是合格的
    # finalCode，按原版「skip 也算完结」把它升为已验证（否则一份解释清楚了的
    # 程序会永远停在草稿，下次还得从头再来）。
    _gaps_left = [d for d in divs
                  if str(d.get("final_classification")
                         or d.get("initial_classification") or "")
                  in ("drift", "bug")]
    if (str(_choice or "") == "skipped" and str(code or "").strip()
            and str(topic_id or "").strip() and not _gaps_left):
        try:
            _sa2, _sk2 = save_final_code_skill(
                scene_id, str(topic_id), question, str(code),
                milestone_name=str(milestone_name or ""),
                dims=[], req_id=str(req_id or ""),
                verified=True, from_phase="maieutic_phase3_aligned")
            out["skill_written"] = str(_sk2 or "")
            out["skill_action"] = str(_sa2 or "")
            print("  · Phase3 复判无缺口，原件升为已验证："
                  + str(_sk2 or ""), flush=True)
        except Exception as _ske2:
            out["skill_error"] = f"{type(_ske2).__name__}: {_ske2}"
    out["counts"] = {
        "total": len(divs),
        "drift": sum(1 for d in divs
                     if d.get("initial_classification") == "drift"),
        "revision": sum(1 for d in divs
                        if d.get("initial_classification") == "revision"),
        "bug": sum(1 for d in divs
                   if d.get("initial_classification") == "bug"),
        "aligned": len(out["aligned"]), "partial": len(out["partial"]),
        "diverged": len(out["diverged"]),
        # 判官给不出结论的那几条（解析失败/调用失败）——如实单列，
        # 不混进 diverged（没判过 ≠ 判成偏了）。
        "unjudged": sum(1 for d in divs if d.get("alignment") is None),
    }
    _save_phase3(scene_id, question, out)
    return out


def _save_phase3(scene_id: str, question: str, rec: dict[str, Any]) -> None:
    """落盘 Phase3（原版 `phase3Data` 同款，只写这一个键）。"""
    try:
        import maieutic_scaffold as _ms3
        _ms3.save_phase3(scene_id, question, rec)
    except Exception:
        pass


def _provenance_of(tools: list[str], code: str = "") -> str:
    """这条技能的材料**来自哪一面**：db / upload / web（2026-11-03）。

    判据是**实际调用的工具**，不是场景种类：同一个场景里，读上传文档结晶的
    技能和查库结晶的技能可信度不同 —— 前者复现要同一份文件，后者同库同 SQL
    同结果。不标来源，「库内已验证（可复现）」这个口径就会被 web 缓存冒名
    （场景 J 实测：标着库内验证，实际查的是联网抓回来的 web_docs）。
    """
    names = {str(t or "").lower() for t in (tools or [])}
    try:
        names |= {str(t).lower() for t in _called_tools(code)}
    except Exception:
        pass
    if any(("read_file" in n) or ("list_files" in n) for n in names):
        return "upload"
    if any(n.startswith(("web_", "gh_")) for n in names):
        return "web"
    return "db"


_UPLOAD_QUOTE_RE = re.compile(r"「([^」]{4,120})」|“([^”]{4,120})”")


def _upload_quote_guard(text: str, code: str, scene_id: str) -> str:
    """上传材料技能的确定性守卫：技能里的**引文**必须在文档全文中字面可定位。

    与 `consume._sql_literal_guard` 同构（那边查库的取值域，这边查文档全文）：
    LLM 判官有随机性，"文档里到底有没有这句话"不该赌它。返回拒绝原因；
    '' = 通过。没有引文可查时放行（确定性只判可判的部分，不扩大否决面）。
    """
    try:
        import upload as _upl_g
        corpus = str(_upl_g.uploads_corpus(scene_id) or "")
    except Exception:
        return ""
    if not corpus:
        return ""
    _c = re.sub(r"\s+", "", corpus)
    for src in (text, code):
        for m in _UPLOAD_QUOTE_RE.finditer(str(src or "")):
            q = re.sub(r"\s+", "", (m.group(1) or m.group(2) or ""))
            if len(q) >= 4 and q not in _c:
                return (f"引文「{q[:30]}」在文档全文里定位不到 —— "
                        f"大概率是幻觉原文，拒绝入库")
    return ""


def _called_tools(code: str) -> list[str]:
    """从跑通的代码里解析**调用了哪些动作**（与 consume._RECIPE_CALL 同一族，
    但这里是提取名字不是判真假 —— 真假判据仍走 `_looks_like_recipe`）。

    为什么需要：执行层 tracer 只在**真的跑起来**时记录（observations 的
    data_sources）；断点续学补挂时不重跑，只能从**上次跑通的代码**里解析。
    """
    import re as _re
    return sorted({m.group(1) for m in _re.finditer(
        r"\b(list_files|read_file|read_json|read_csv|query_sql"
        r"|web_search|web_fetch|http_get|fetch_url"
        r"|mcp_[A-Za-z0-9_]+)\s*\(", str(code or ""))})


def close_one_dimension(scene_id: str, topic_id: str, question: str,
                        dim: dict[str, Any], *, bump: bool = True,
                        tools: list[str] | None = None,
                        import_id: str = "") -> list[str]:
    """**已停用**（用户 2026-11-12 口径）：维度不再上图。

    原因：维度是过程，条数是动态的 —— 同一句话换个问法，骨架拆出来的条数
    就变，图谱上「长了几个节点」根本不能表示「学到了多少」。Phase1 的闭合
    是整篇的（同构助产士 examiner），过程动态、没有可预见的中间点；图谱
    落点只保留一枚：过关的 Phase2 = 技能沉淀的位置（见 `attach_phase_nodes`）。

    函数保留为空实现：调用点（断点续学补挂 / 逐维闭合）保持原样，
    不再产生图谱节点，也不再动 evidence 计数。
    """
    return []


def attach_phase_nodes(scene_id: str, req_id: str, question: str, *,
                       phase2: dict[str, Any] | None = None,
                       verified: bool | None = None) -> list[str]:
    """一轮学习在图谱上**只落一枚**节点：过关的 Phase2（用户 2026-11-12 口径）。

        req:<req_id>:phase2 —— finalCode 过关（critic 判「做到了」），
                              即**技能沉淀的位置**。

    Phase1 不上图：Phase1 的闭合是**整篇**的（同构助产士 examiner ——
    gaps_still_open 空了才算闭合），过程是动态的，没有「第几维闭合了」这种
    可预见的中间点；总览里也就不存在「某维度不闭合」，只有闭合 / 不闭合。
    不闭合就永远走不到 Phase2 —— 技能不沉淀、图谱不长节点。所以图谱上
    「这个需求学到了」的唯一证据就是那枚过关的 phase2 节点。

    幂等：同 (req, phase2) 只建一次，重复学只把 evidence +1。
    """
    sid = str(scene_id or "A")
    rid = str(req_id or "").strip()
    q = str(question or "").strip()
    if not rid:
        try:
            import req_registry as _rr
            rid = str(_rr.req_id_of(sid, q) or "")
        except Exception:
            rid = ""
    if not rid:
        return []
    try:
        from knowledge_graph import KnowledgeGraph, Node, Edge
    except Exception:
        return []
    want: list[tuple[str, str, dict[str, Any]]] = []
    # 课程场景（数学题）没有 Phase2：判过（learned）就是这一关过了。
    # 不传这一档，progress 永远 0（2026-11-14 P 实测：判了 learned，
    # done 仍是 0，下一轮还出同一单元）。
    _verified = (bool(phase2.get("verified")) if isinstance(phase2, dict)
                 else bool(verified))
    if isinstance(phase2, dict) and phase2.get("verified"):
        want.append(("phase2", "Phase2 · 过关 · 技能沉淀",
                     {"question": q, "req_id": rid,
                      "source": "maieutic_phase2",
                      "code_chars": len(str(phase2.get("code")
                                             or phase2.get("final_code") or "")),
                      "rows": int(phase2.get("rows") or 0)}))
    if not want and not _verified:
        return []
    out_ids: list[str] = []
    try:
        g = KnowledgeGraph.load()
        g.ensure_scene_root(sid)
        parent = f"req:{rid}"
        for suffix, label, detail in want:
            nid = f"{parent}:{suffix}"
            if nid in (getattr(g, "nodes", {}) or {}):
                try:
                    g.nodes[nid].evidence = int(g.nodes[nid].evidence) + 1
                except Exception:
                    pass
            else:
                g.add_node(Node(id=nid, kind="phase", label=label,
                                detail=detail, root_scene=sid,
                                status="verified", evidence=1))
                if parent in (getattr(g, "nodes", {}) or {}):
                    g.add_edge(Edge(src=parent, dst=nid,
                                    relation="has_phase", origin="observed",
                                    reason="Phase2 过关：技能沉淀的位置"))
            out_ids.append(nid)
        # req 节点翻「已做」：只升不降（重做没过关也不把已做翻回将做 ——
        # 账本口径：做过一遍永远算数；将做→已做由 Phase2 过关触发）。
        if _verified and parent in (getattr(g, "nodes", {}) or {}):
            _rn = g.nodes[parent]
            try:
                _rn.status = "verified"
                _rn.evidence = max(1, int(getattr(_rn, "evidence", 0) or 0) + 1)
                _rn.detail = dict(getattr(_rn, "detail", {}) or {})
                _rn.detail.setdefault("solved_first_at", _rn.detail.get(
                    "solved_at") or time.strftime("%Y-%m-%dT%H:%M:%S"))
                _rn.detail["solved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                _rn.detail["solved_run"] = int((phase2 or {}).get("run") or 0)
            except Exception:
                pass
        g.save()
    except Exception:
        return out_ids
    # 技能沉淀的位置同时也是「这一关的这道题做完了」的证据：把 exercise 节点
    # 挂到它所属的 topic（关）下面，规划层据此算完成度（graph_curriculum）。
    # 过关记 verified（计入完成度）；没过关也记，但 status=unverified，
    # 只表示「试过」，不推进进度。
    try:
        import graph_curriculum as _gc
        _eid, _mid = "", ""
        _m = _gc.EID_RE.search(q)
        if _m:
            _eid = _m.group(1)
        if not _eid:
            # 没有 exercise_id 的场景（P：题由模型按单元现场出）。位置不由
            # 题面指定，先用自由学的精确账（提题时记的 题→单元，中文题面
            # 与英文目录词面不重合，locate 的词面打分够不到 0.45），再退
            # 回内容认：题面跟哪个单元最贴，这次学习就落在那儿。
            try:
                import hashlib
                import graph_books as _gb
                _mid = str(_gb.unit_of_proposed(sid, q) or "")
                if not _mid:
                    _c = (_gb.locate(sid, q).get("candidates") or [])
                    if _c and float(_c[0].get("score") or 0) >= 0.45:
                        _mid = str(_c[0]["unit"])
                if _mid:
                    _bid, _i = _gb.parse_unit_key(_mid)
                    _tag = hashlib.md5(str(q).encode("utf-8")).hexdigest()[:8]
                    _eid = f"gen:{sid}:{_bid}:{_i}:{_tag}"
            except Exception:
                _eid = ""
        if _eid:
            _gc.record_solved(sid, _eid, module_id=_mid, req_id=rid,
                              verified=_verified,
                              problem=q)
    except Exception:
        pass
    return out_ids


def render_session_text(events: list[dict], *, limit: int = 6000) -> str:
    """把 session 事件流渲染成「整篇」（用户 2026-11-11 口径）。

    「当前 voyager 在跑这个 session 的记忆、日志就是整篇，这个是增量的」——
    所以整篇**不重拼**：它就是这个 session 一路追加下来的过程记录，
    每轮往后长，旧条目一个字节不改。

    判官因此核的是过程（代码 + 工具 + 实际结果 + 上一次的判词），
    而不是要求模型在一句话里把 175 个 ID 逐字列全（answer 通道 2500 字上限，
    根本放不下 → 只剩「省略号」和「整段搬运」两条被拒的路）。
    """
    blocks: list[str] = []
    for ev in (events or []):
        if not isinstance(ev, dict):
            continue
        kind = str(ev.get("kind") or "")
        p = ev.get("payload") or {}
        if not isinstance(p, dict):
            p = {}
        if kind == "spec_submit":
            # 整篇作答（原版 Phase1 的「学生交 spec」）：一轮一条，旧的逐条追加。
            body = [f"[轮 {p.get('round') or '?'}] 它交的整篇"]
            if str(p.get("spec") or "").strip():
                body.append(str(p["spec"])[:4000])
            if str(p.get("error") or "").strip():
                body.append("    报错：" + " ".join(str(p["error"]).split())[:200])
            blocks.append("\n".join(body))
        elif kind == "dim_attempt":
            head = (f"[轮 {p.get('round') or '?'}] 维度 {p.get('dim_id') or ''}"
                    f"｜{str(p.get('question') or '')[:140]}")
            body = [head]
            # ★ 2026-11-11（用户拍板「A 原版」）：整篇**只放它说的话**。
            #   原版 Phase1 交给 examiner 的只有 studentSpecText 一段文字
            #   （spec_route.ts:16 入参 `specText: z.string()`；
            #   examiner_pre.ts:208-262 模板里没有代码、没有执行结果、没有
            #   环境）—— 结构上就没给「代码里有 WHERE 就算口径给了」留路径。
            #   MVE 之前把代码与结果都拼进来，判官于是在代码里找答案
            #   （23:19 那次 5/5 全是假闭合）。工具调用也不放：
            #   执行过 ≠ 作答过，它不属于承诺面。
            if str(p.get("answer") or "").strip():
                body.append("    它说的结论：" + " ".join(str(p["answer"]).split())[:400])
            if str(p.get("error") or "").strip():
                body.append("    报错：" + " ".join(str(p["error"]).split())[:200])
            body.append("    判定：" + ("闭合" if p.get("closed") else "未闭合")
                        + (("—— " + str(p.get("why"))[:300]) if p.get("why") else ""))
            blocks.append("\n".join(body))
        elif kind == "gate_verdict":
            _add = ", ".join(str(x) for x in (p.get("addressed") or [])) or "（无）"
            _opn = ", ".join(str(x) for x in (p.get("open") or [])) or "（无）"
            _qs = "；".join(str(x)[:160] for x in (p.get("questions") or []))
            blocks.append(
                f"[轮 {p.get('round') or '?'}] 判官（整篇）："
                + ("判过" if p.get("passed") else "未过")
                + f"｜答到：[{_add}]｜仍开：[{_opn}]"
                + (f"｜追问：{_qs}" if _qs else "")
                + (f"｜判不了：{str(p.get('reason') or '')[:120]}"
                   if p.get("reason") else ""))
    text = "\n\n".join(blocks)
    if len(text) > limit:
        # 只丢最老的（增量语义：新的必须在），并如实写丢了多少
        keep = text[-limit:]
        cut = text[:len(text) - limit].count("\n[")
        text = f"（更早的 {cut} 条记录略）\n\n" + keep.split("\n\n", 1)[-1]
    return text


def _env_obs_block(db_path: str) -> str:
    """环境实查块（学习侧判官共用）：库里没有的费率/口径不许假设。

    原版 critic 读环境末态判（`voyager/agents/critic.py:30` 传 events +
    chest_observation），SQL 场景的同构物 = `curriculum_auto.db_observation`
    实查清单。两个数值列相乘/相除合成新口径（base_salary × overtime_hours
    当加班费）= 假设业务参数 —— 2026-10-26 实测该模式穿过字面量守卫和
    fan-out 闸入库（164000 假数、1.0 假数），判官必须点名这条。
    """
    try:
        if not str(db_path or "").strip():
            return ""
        from curriculum_auto import db_observation as _dbobs
        obs = str(_dbobs(db_path) or "")
        if not obs:
            return ""
        return ("\n\n[环境实查（数据源真实的表/列/取值）：任务假设的任何表、列、"
                "业务参数（费率/汇率/税率/政策数值…）都必须能在这里指认，指认不了"
                " = 任务在数据源上不可答，判未完成，并明说缺什么。**特别地：两个"
                "数值列相乘/相除合成新口径（如 base_salary × overtime_hours "
                "当作「加班费」）同样是假设业务参数** —— 库里没有该口径的列或"
                "费率，就是不可计算，判未完成；把月薪当时薪用是语义造假]\n" + obs)
    except Exception:
        return ""


async def start_learning(
    question: str,
    scene_id: str = "",
    *,
    llm: Callable | None = None,
    db_path: str = "",
    execute: bool = True,
    use_mcp: bool = True,
    critique: str = "",
    judge: bool = False,
    context: str = "",
    import_id: str = "",
    preset_dimension: dict | None = None,
    milestone_retry: bool = False,
    req_id: str = "",
    fresh: bool = False,
    new_req: bool = False,
) -> dict[str, Any]:
    """启动一轮学习（同构 learn() 单圈）。返回全量过程记录，失败如实带 why。

    薄壳：跑完把 MCP 网关干净断开。**必须在这里断且趁事件循环还没关**——
    否则 stdio 子进程的 transport 会在解释器退出、loop 关闭后才 __del__，
    实测在 scene_learn 里刷一条 `RuntimeError: Event loop is closed` 噪声
    （子进程能靠 stdin EOF 兜底退出，但不干净）。
    """
    try:
        return await _start_learning_impl(
            question, scene_id, llm=llm, db_path=db_path,
            execute=execute, use_mcp=use_mcp, critique=critique,
            judge=judge, context=context, import_id=import_id,
            preset_dimension=preset_dimension,
            milestone_retry=milestone_retry, req_id=req_id, fresh=fresh,
            new_req=new_req)
    finally:
        if use_mcp:
            try:
                import mcp_gateway
                await mcp_gateway.get_gateway().disconnect_all()
            except Exception:
                pass


async def _start_learning_impl(
    question: str,
    scene_id: str = "",
    *,
    llm: Callable | None = None,
    db_path: str = "",
    execute: bool = True,
    use_mcp: bool = True,
    critique: str = "",
    judge: bool = False,
    context: str = "",
    import_id: str = "",
    preset_dimension: dict | None = None,
    milestone_retry: bool = False,
    req_id: str = "",
    fresh: bool = False,
    new_req: bool = False,
) -> dict[str, Any]:
    """启动一轮学习（同构 learn() 单圈）的实现体（断开逻辑见外层 start_learning）。

    `milestone_retry`（2026-10-26，用户：「参考原 voyager 的重试里程碑方法，
    不要自己发明」）—— 里程碑重试轮走**原版 rollout 同构**（voyager.py:283-289
    rollout(task)）：对 task **本体**执行 —— skills 注入（:199 retrieve →
    render_system_message）、上轮 critique 进 context 槽（:231-234
    render_human_message(critique=...)）、内层 4 步迭代（solve 自带）。
    已闭合维度**不重学**（原版子目标完成即 progress+1，不重做）；本体取到
    真数 → 存量闭合账判达成 → 组合复合技能入库（learn() 第二步同构）。
    之前的问题：重试轮走维度循环，全部「维重复用」跳过 → 整轮唯一取数步骤
    是组合层，它被守卫拒后整轮 calls=0 → no_data 死循环（实测场景 B 10s/轮）。
    """
    question = str(question or "").strip()
    scene = _resolve_scene(scene_id)
    # 执行层无条件要用 local_agent.solve。下面 scaffold 分支里虽然也 import 了
    # 一次，但 `import` 会让 local_agent 成为**整个函数的局部名** —— 任何一条
    # 没走到那次 import 的路径（如本轮走「里程碑本体重试」分支）都会
    # UnboundLocalError（2026-11-15 P 场景「相似三角形」实测：5 秒内整轮失败、
    # 什么都没学，日志只有一行「执行层失败」）。绑定提到函数体最前，管到底。
    import local_agent
    # ---- 需求级 id（2026-11-09 第三刀 A/E）----
    # 学习侧一开始就把身份定下来，后面**所有**产物都挂它：
    # 骨架键、进度账、技能 blueprint 的 req_id。
    # ⚠️ 必须在读进度账（_ms.load_progress，走 _cache_path → req_id_of）
    #    **之前**登记 —— 骨架键挂在 req id 上，身份没定下来就读不到账。
    # `register` 是幂等的：同一问句再学一次拿回同一个 req id，
    # 换问法则由 req_id_of 的归题找回（②级）拿回同一个。
    _req_id = ""
    try:
        import req_registry as _rr
        # ★ 2026-11-11（拆骨架不做归一）：**发布方给的身份优先**。
        #   kick_learning 派发时已 force_new 登记好新 req，这里必须原样用它 ——
        #   若再走 register（按问句解析）会被 find_by_question 拽回旧账，
        #   重拆等于没发生。没给 req_id（断点续学 / 课内逐维续跑）才走幂等登记。
        if str(req_id or "").strip():
            _req_id = str(req_id).strip()
            _rr.ensure_req_node(scene, question, _req_id)
        elif new_req:
            # 自由学（图谱将做 / 书单当前单元）出的题是**新题**，必须拿新身份。
            # 不能走 register 的「归题找回」：routing.match 只认词面，实测把
            # 「计算 2/3 × 9/8，并约分」归到「计算 3 的 6 次方是多少？」——
            # 两道题的追问历史、addressed 全串进同一份 ckpt，新题一上来就
            # 「累计答到 3/3（本轮新增 0）」，还把旧题的作答当「你上一轮提交
            # 的整篇」带进 context（2026-11-15 P 场景实测）。force_new 只跳过
            # 幂等与归题两道闸，骨架怎么拆、账怎么记仍走既有账。
            _req_id = str(_rr.register(scene, question,
                                       force_new=True).get("req_id") or "")
        else:
            _req_id = str(_rr.register(scene, question).get("req_id") or "")
    except Exception:
        _req_id = ""
    # ---- 导入批次（用户 2026-10-20）----
    # 「每次人导入-运行学习一批内容，这批图谱上长出来的内容就是那次人导入的
    #   子节点，技能归属也是如此」。调用方没给批次 id 时，按问句文本找**最近
    #   一次**人导入记录（同场景、严格同句），拿它的批次 id —— 这样无论是
    #   面板点「启动 Voyager」还是命令行 scene_learn.py，都能挂到那次导入下。
    # 找不到（纯手动学、没导过）就留空：不编一个假批次。
    imp_id = str(import_id or "").strip()
    imp_n = 0
    if not imp_id:
        try:
            import coach as _coach_h
            _hist = _coach_h.import_history(question, scene)
            imp_id = str(_hist.get("import_id") or "")
            imp_n = int(_hist.get("count") or 0)
        except Exception:
            imp_id, imp_n = "", 0
    if imp_id:
        print(f"  · 导入批次：{imp_id}"
              + (f"（这个问题第 {imp_n} 次导入）" if imp_n else "")
              + " —— 这一轮长出来的节点都挂在它下面", flush=True)
    out: dict[str, Any] = {
        "question": question, "scene_id": scene, "status": "", "why": "",
        "plan": {}, "forged": {}, "exposed": {}, "stuck": [],
        "execution": {}, "absorbed": {}, "crystallized": {}, "milestone": {},
        "import_id": imp_id, "import_count": imp_n,
        "req_id": _req_id,
        "started_at": round(time.time(), 1),
    }
    if not question:
        out.update(status="failed", why="空问题，无可学")
        _record(out)
        return out

    # 已逐条闭合的维度 id（最小技能单位：闭合一条就落一枚节点，见
    # `close_one_dimension`）。定义在这是给 ⑥ 的兜底去重用的 —— 非维度模式下
    # 它是空集，⑥ 的 `not in` 照样成立，不会 NameError。
    closed_ids: set[str] = set()

    # ---- ⓪ 场景隔离：这一轮学的是**哪个后端**，先定死 ----
    # 用户 2026-10-19 两条要求：
    #   「启动 voyager 学习时场景隔离，只学习当前场景」
    #   「别的场景下，voyager 的学习和 VLML 没有关系」
    #   「对于普通用户而言，数据源就是本机」
    # 所以后端三分：Scene A → VLML（rib 库 / VLML 工具集）；场景配了自己的库
    # → 只读那个库；其余 → **本机**（场景目录里的文件 + 只读原语），
    # 不需要用户配任何库。三条都不许互相借数据。
    import scene_profile
    prof = scene_profile.profile(scene)
    out["backend"] = {"kind": prof["kind"], "db_path": prof["db_path"],
                      "scene_dir": prof.get("scene_dir", ""),
                      "scene_name": prof.get("scene_name", ""),
                      "why": prof.get("why", "")}
    if prof["kind"] in (scene_profile.NONE, scene_profile.MISSING):
        out.update(status="unreachable", why=str(prof.get("why") or ""))
        _record(out)
        return out
    use_vlml = prof["kind"] == scene_profile.VLML
    is_local = prof["kind"] == scene_profile.LOCAL
    # 课程场景（数学题）：答案是推导出来的，不靠取证。强制"必须调数据源"
    # 只会逼学生编一个假来源 —— 2026-11-14 P 实测：答对 ∠D=30° 仍被判
    # "没调用任何数据源"，RESULT 里还被迫写 basis.from=query_sql。
    _req_tools = bool(scene_profile.grounded(scene))
    # ⚠️ 2026-11-03 场景 J 实测：kind=scene（有库）的场景原来 local_dir 恒 ""，
    # 于是 `available_grabs` 里 `if scene_dir:` 为假、register_local 不调用 ——
    # 学习侧拿不到 read_file/list_files，lib/uploads/ 下用户上传的文档
    # （sidecar 全文）谁都读不到，只能把 web_docs 联网缓存当材料结晶。
    # 场景目录对 lib/ 只读原语是安全面（_resolve_under 只认本场景目录内、
    # 拒密钥文件），库仍是唯一可查的库（query_sql 绑 db_path），不破隔离。
    local_dir = ("" if use_vlml else
                 str(prof.get("scene_dir") or scene_profile.scene_dir(scene)))
    scene_db = "" if (use_vlml or is_local) else str(prof["db_path"])
    db_path = db_path or scene_db          # 调用方显式给的优先
    out["backend"]["vlml"] = use_vlml

    # ---- 走哪条执行路径：代码为主，SQL 只是代码里的一行（2026-10-29）----
    # 生产实证：Scene H（kind=scene，库里有 web_docs 快照）一直走 VLML SQL
    # agent —— 那条路径的执行域里**只有 SQL**，gh/联网原语一个都拿不到，于是
    # 「读某个 GitHub 仓库」被硬拗成 3~4 轮
    # `SELECT * FROM web_docs WHERE query LIKE ...` 的过滤条件打磨（第 4 步
    # 甚至开始让判词教它加 AND title LIKE），永远不可能答出来。用户原话
    # 「感觉它还在试错 sql」就是这个形状 —— 不是它笨，是我们只给了它 SQL。
    # 消费侧 4c25a6a 已经同构地改过（「一个执行工具：SQL 只是代码里的一行」），
    # 学习侧不跟上 = 两侧不同构。代码路径里 query_sql / gh_read /
    # web_search / list_files 在**同一个命名空间**（local_agent._ns_base），
    # 用哪个由环境和题目决定，不由我们分类。
    #
    # ⚠️ 判据是**环境形态**（kind），不是场景 id —— 新增任何场景都自动适用：
    #   local（本机文件）、scene（自带库，B/C/H/P/G…）→ 代码路径
    #   vlml（场景 A，数据面是 VLML 库）→ 默认仍走它专用的 SQL 执行器
    # 开关（env，不给就 auto）：
    #   MVE_CODE_EXEC=0    → 全部退回旧路径（vlml→VLML agent，scene→SceneDBEnv）
    #   MVE_CODE_EXEC=all  → 连 VLML 场景也走代码路径（query_sql 只读它自己的库）
    # 场景目录：profile 没带（老场景数据）就用同一套推导兜底 —— 学习侧要读
    # uploads/ 里的 sidecar，目录指错就白给了工具（2026-11-03）。
    exec_dir = str(prof.get("scene_dir") or "") or (
        "" if use_vlml else scene_profile.scene_dir(scene))
    # 代码路径要能查库：scene 用契约库，VLML 用它的数据面库（只读）
    exec_db = str(db_path or prof.get("db_path") or "")
    _mode = str(os.environ.get("MVE_CODE_EXEC", "auto")).strip().lower()
    _off = _mode in ("0", "off", "false", "no")
    _code_exec = (not _off) and (not use_vlml or _mode == "all")
    # 代码路径可重放的技能（下方取到 skills 后填）：只有 Python 形态
    # （code 里有 RESULT）能 exec；SQL 配方 exec 只会 SyntaxError。
    _py_skills: list[dict[str, Any]] = []

    # ---- ① 图谱任务拆分（同构 decompose_task；LLM 失败降级会如实标 origin）----
    # 本机/知识型场景走 Maieutic 追问骨架（maieutic_scaffold，移植自
    # ~/ maieutic_fork 的 scaffolding.ts）：出的不是"Get X"动宾空壳，而是
    # 「要答清这句必须先回答的维度」——知识型问题没有表可撞，动宾格式只能拆出
    # 1 条空壳（实测）。数据/库场景仍走原版 decompose（reachability 靠词面撞表名）。
    import task_decompose
    # 什么时候用 Maieutic 骨架：**本机场景 + 知识型问题**。
    # 数据型问题（文件/库/统计/排名/时效）仍走原版 decompose —— 它出的动宾子目标
    # 才能被 reachability 拿去撞表名/条目名；知识型问题没有表可撞，动宾格式只会
    # 拆出 "Get definition of X" 这种空壳（实测就 1 条）。
    try:
        import task_critic as _tc0
        # 复杂度拆解在执行层对每任务做（原版 voyager.py:403-421 子目标逐个完成）。
        # 两种场景都走 Maieutic 追问骨架，但**数据/SQL 题的骨架必须吃 DB 观察面**
        # （curriculum_auto.db_observation 实查的表/列/真实取值）—— 否则 Maieutic
        # 会对着 payroll.duckdb 凭空编出「时薪/加班费/税率」等库里根本不存在的业务
        # 参数（全 unreachable，永远闭合不了）。maieutic_scaffold 已改为：场景有
        # db_path 时把 db_observation 喂给 LLM 并加「只指向真实表/列、不许编参数」
        # 的硬约束。这样拆出的维度每个都有 DB 操作落点，交给 Voyager 的 SQL 执行层
        # 逐个 solv、闭合、落原子技能，全闭合即里程碑。vlml（use_vlml）仍走自己的
        # 单解分支，不进维度循环。
        # ★ 2026-11-14 回退前一刀的错：书单场景曾为了绕开"空库被当成 SQL 场景"
        # 而**关掉 scaffold**，结果 phase1/phase2 全不走 —— 每道题只做一次单解，
        # 没有维度闭合、没有 finalCode、没有技能沉淀（用户指出："完全没走助产士
        # 拆骨架 phase1phase2，这不行"）。根因是材料喂错了，不是骨架不适用：
        # maieutic_scaffold 现在有 SCAFFOLD_SYSTEM_COURSE（答案靠推不靠查），
        # 并把当前教材单元与考点喂进材料，维度因此落在数学考点上。
        _use_scaffold = bool(is_local) or (bool(db_path) and not use_vlml)
    except Exception:
        _use_scaffold = bool(is_local)
    # 预设维度（驾驶舱逐维度闭合里程碑用）：明确要走维度循环，不管本场景默认的
    # 拆解路线——传进来的就是一条已知可达、已知有 DB 落点的维度。
    if preset_dimension is not None:
        _use_scaffold = True
    # 上传文件清单：**拆骨架前**就要给。规划员看不见 uploads/ 的话，空库里
    # 只能拆出「在 scene.duckdb 中查…」这类维度，真正的材料被挡在门外
    # （2026-11-03 场景 J：文集在库里查不到，维度全指向空库）。
    try:
        import upload as _upl_plan
        _upl_manifest = str(_upl_plan.uploads_manifest(scene) or "")
    except Exception:
        _upl_manifest = ""
    plan = task_decompose.plan(question, scene, llm=llm, db_path=db_path,
                               scaffold=_use_scaffold, extras=_upl_manifest,
                               req_id=_req_id)
    out["plan"] = plan
    out["plan_origin"] = str(plan.get("origin") or "")
    # 预设维度（驾驶舱逐维度闭合里程碑）：这条维度已知可达、已知有 DB 落点，
    # 别让它被「无子目标 / 硬不可达」短路踢出去。兜底把子目标补成一条
    # reachable、counts 对齐，保证后面稳稳进维度循环。
    if preset_dimension is not None:
        if not plan.get("subgoals"):
            plan["subgoals"] = [{"need": str(preset_dimension.get("question") or ""),
                                "status": "reachable"}]
        _c = plan.get("counts") or {}
        _c["unreachable"] = 0
        plan["counts"] = _c
    if not plan.get("subgoals"):
        out.update(status="failed", why="拆分不出子目标（LLM 不可用且规则兜底也空）")
        _record(out)
        return out
    if is_local:
        # 本机场景：抓手 = 本机只读原语 + 联网 + MCP（同构「插件即本机能力」）。
        # 没有表可匹配，所以不走「相关表」那条判据；也不许退回 rib 的表。
        # ⚠️ 2026-10-20 改：不再无条件写 `reachable`（那是不判，不是判过）。
        # 真去查注册表里现在能调什么，把**依据**写进 why —— 一个手段都没有
        # 时报 gap 并说清怎么补（装数据源 / 打开猫爪总开关 / 接 MCP）。
        _grabs = available_grabs(scene, str(local_dir or ""))
        _n = len(plan["subtasks"])
        _why = ("可用取证手段："
                + "、".join(_grabs[:6]) + (f" 等 {len(_grabs)} 个" if _grabs else "")
                if _grabs else
                "本场景没有任何可用取证手段（本机原语/联网/MCP 全不可用 —— "
                "装数据源、打开猫爪总开关，或接一个 MCP server）")
        prim = {"name": (_grabs[0] if _grabs else "local:list_files/read_file"),
                "description": _why}
        for st in plan["subtasks"]:
            st.update(status=("reachable" if _grabs else "gap"), grab=prim,
                      score=1.0, why=_why)
        plan["counts"] = {"reachable": (_n if _grabs else 0),
                          "gap": (0 if _grabs else _n), "unreachable": 0}
        out["plan"] = plan
        out["plan_reachability"] = {"grabs": _grabs, "basis": _why,
                                    "counts": dict(plan["counts"])}
        print(f"  · 可达：{plan['counts']['reachable']}/{_n} 个子目标可达"
              f"（依据：{len(_grabs)} 个可用手段"
              + (f"：{'、'.join(_grabs[:4])}" if _grabs else "，一个都没有")
              + "）", flush=True)
    elif plan["counts"]["unreachable"] == len(plan["subgoals"]):
        # 硬不可达：既无抓手也无相关表。学不出来不是循环的失败，是数据源
        # 缺席 —— 如实报给人（装数据源/换场景），不硬编一个"学过了"。
        out.update(status="unreachable",
                   why="注册表无抓手且库中无相关表 —— 硬不可达（装数据源/换场景）")
        _record(out)
        return out

    # ---- ② 缺口 → 写插件（第一层：写插件；本机场景没有"表缺口"这一档）----
    if plan["counts"]["gap"] and not is_local:
        import plugin_forge
        out["forged"] = plugin_forge.forge_gaps(plan, scene, db_path=db_path)

    # ---- ③ 远端 MCP 工具 → 暴露为抓手段（编排的接入点；失败不拦学习）----
    if use_mcp:
        try:
            import mcp_gateway
            gw = mcp_gateway.get_gateway()
            conn = await gw.connect_all()
            exposed = gw.expose_as_actions(scene)
            out["exposed"] = {"connected": conn.get("connected", []),
                              "failed": conn.get("failed", []),
                              "exposed": exposed.get("exposed", []),
                              "reason": str(exposed.get("reason") or "")}
        except Exception as e:
            out["exposed"] = {"error": f"{type(e).__name__}: {e}"}

    # ---- ④ 重判：forge/expose 之后还有没有抓不到的子目标（照实，不宣称全通）----
    # 本机场景在 ① 判可达时 mcp 条目还没注册（③ 才 expose），可能低估 →
    # 这里用**现在的**可用手段复核一次。旧代码 `[] if is_local` 整段跳过本机，
    # 于是"可达"在本机/知识型路径上永远是常量，判了等于没判。
    if is_local:
        _grabs2 = available_grabs(scene, str(local_dir or ""))
        if _grabs2:
            for st in plan["subtasks"]:
                if st.get("status") != "reachable":
                    st.update(status="reachable", why=str(st.get("why") or "")
                              + "｜MCP 接入后复核：现在有 "
                              + "、".join(_grabs2[:6]))
            plan["counts"] = {"reachable": len(plan["subtasks"]),
                              "gap": 0, "unreachable": 0}
            out["plan"] = plan
            out["plan_reachability"] = {"grabs": _grabs2,
                                        "basis": "MCP 接入后复核",
                                        "counts": dict(plan["counts"])}
    stuck: list[dict[str, Any]] = []
    for st in ([] if is_local else plan["subtasks"]):
        if st.get("status") == "reachable":
            continue
        r = task_decompose.reachability(str(st.get("need") or ""), scene,
                                       db_path=db_path)
        if r.get("status") != "reachable":
            stuck.append({"seq": st.get("seq"), "need": st.get("need"),
                          "status": r.get("status"), "why": r.get("why")})
    out["stuck"] = stuck
    if stuck and len(stuck) == len(plan["subtasks"]):
        out.update(status="unreachable",
                   why="forge/expose 后所有子目标仍无抓手：" +
                       "；".join(str(s["need"])[:30] for s in stuck))
        _record(out)
        return out

    # ---- ⑤ 执行层：写 SQL 真取数（带已学配方注入，同构 retrieve→rollout）----
    sql = ""
    if execute:
        try:
            import skill_store
            # 技能按场景取（口径与面板一致：本场景 + default）。
            # 跨场景复用 A 的配方去答 B 的问题，就是「场景隔离没做到」的另一种样子。
            skills = skill_store.retrieve("", question, scene_id=scene)
            # 可观测：回注了几条、哪几条。技能是新的 primitive（原版
            # action.py:94 `+ skills`），所以这条数就是"能力边界"的可视化 ——
            # 不打印的话，人只能从"学得快没快"猜它有没有生效。
            if skills:
                # ⚠️ 2026-10-29 实测缺口：这里只打印、**从不记账** —— H 场景 17 条
                # 技能 hits 全 0，面板一律显示「⚪ 未验证」，而检索打分里的质量项
                # （0.25*min(hits,6) + 0.6*ok/hits）因此恒为 0：学到的配方永远爬
                # 不上来，人看到的就是「学了半天没用」。注入就是一次 hit
                # （consume.py:232 一直这么记，学习侧漏了）。
                _inj_keys = [str((s or {}).get("key") or "") for s in skills]
                try:
                    skill_store.mark_used(_inj_keys)
                except Exception:
                    pass
                # 有效性记账的分子（2026-11-17 补链）：原版 voyager.py:2517
                # 判对后 mark_result(self._injected_keys, True)，场景路径漏接
                # —— hits 涨 ok 恒 0，面板把在用技能全标「🔴 失效」。先存
                # out，判过 → ⑥ 前统一 mark_result。
                out["_injected_skill_keys"] = _inj_keys
                _snm = [str((s or {}).get("name") or (s or {}).get("topic") or "")
                        for s in skills[:6]]
                print(f"  · 已学配方回注执行层：{len(skills)} 条"
                      f"（{'、'.join(x for x in _snm if x)}）", flush=True)
            agent = None
            # 代码路径只能重放 **Python 形态**的技能（code 里有 RESULT）；scene
            # 场景的旧技能是 SQL 配方，exec 一段 SQL 只会 SyntaxError —— 传进去
            # 等于给模型喂 NameError。判据与 consume「只认本机形态的配方」同一条。
            _py_skills = [s for s in (skills or [])
                          if "RESULT" in str((s or {}).get("code") or "")]
            if _code_exec:
                # 全场景统一代码路径（本机/scene；MVE_CODE_EXEC=all 时含 vlml）：
                # 不构造 VLML 环境，模型写代码调真实原语（原版 Voyager 就是写代码
                # 调 control_primitives，SQL 只是其中一行）。
                out["execution_backend"] = (f"code:{exec_dir or exec_db}"
                                            + ("（vlml 库只读）"
                                               if use_vlml else ""))
            elif use_vlml:
                from voyager_vlml.agents.action import VLMLActionAgent
                agent = VLMLActionAgent()
            else:
                # 场景配了自己的库：写 SQL 的 prompt/critic 照旧复用，但**数据面**
                # 只连这个库（SceneDBEnv），不引导 VLML、不读 rib。
                from voyager_vlml.agents.action import VLMLActionAgent
                from scene_db import SceneDBEnv
                agent = VLMLActionAgent(env=SceneDBEnv(prof["db_path"]))
                out["execution_backend"] = f"scene_db:{prof['db_path']}"
            # 抓手条目 = 注册表里本场景的**全部**条目（builtin + forged），
            # 同构 MCP tools/list 交给模型。forge 出来的条目描述里带着实查到的
            # 真实列名 —— 不交给它，它就凭 schema 瞎猜 join 键（实测踩过）。
            grabs: list[dict[str, Any]] = []
            try:
                import actions as _act
                grabs = [{"name": e.get("name", ""),
                          "description": str(e.get("description") or "")}
                         for e in _act.enabled_entries(scene)]
                # 场景自带数据源不受猫爪门控（同 local_agent._ns_base 的修法）：
                # 有库就必须把 query_sql 列进抓手，否则猫爪一关规划器就判
                # 「没有数据源可达」，学习整段趴窝（2026-10-24 用户实测）。
                if db_path and "query_sql" not in {g["name"] for g in grabs}:
                    grabs.append({"name": "query_sql",
                                  "description": "查场景目录里的 .duckdb（只读）"})
            except Exception:
                grabs = []
            out["grabs_offered"] = len(grabs)
            # 候选表：本题所有子目标实查匹配到的表（并集，保序）。给执行层点名，
            # 免得它挑一张名字最眼熟但对本题无用的表（实测问「下包之后翻盘」
            # 去查 games，还编了个 series_id 字面量）。
            focus: list[str] = []
            try:
                for st in plan["subtasks"]:
                    for t in (task_decompose.related_tables(
                            str(st.get("need") or ""), db_path=db_path) or []):
                        if t not in focus:
                            focus.append(t)
            except Exception:
                focus = []
            # 数据/SQL 的 Maieutic 维度：中文维度词面撞不上英文表名 → related_tables
            # 恒空，focus 全空，执行层没了点名的表（只能靠 schema_block 瞎挑）。
            # 但 scaffold 已先验过维度都落在这张库上，所以直接把**本库全部表**
            # 列为候选焦点表（场景隔离：只列本场景库的表，不借 rib）。3 张小库
            # 全给也不算噪，反能防止它挑错表编字面量。
            if not focus and _use_scaffold and db_path:
                try:
                    import db_switch
                    _all = db_switch.list_tables(db_path) or []
                    focus = [t for t in _all if t not in focus][:10]
                except Exception:
                    focus = []
            out["focus_tables"] = focus[:10]
            _answer_override = ""
            # 题目上下文（口径说明：哪张表、字面量用哪个真实值）随维度进去——
            # 原版 voyager.py:419-424 的 context 槽。场景数据题执行层据此写
            # dept='技术' 而非常识的 '技术部'（2026-10-23 实测坑）。
            # ⚠️ 2026-11-03 追加：**上传文件清单**也进 context 槽。不给清单，
            # 学习器拿到 read_file 也不知道 uploads/ 里有什么（场景 J 实测：
            # 只有 web_docs 表可见 → 把联网缓存当材料结晶）。
            # 清单在上面的拆骨架前已算好（同一个字符串，别算两遍口径分叉）。
            _upl_hint = _upl_manifest
            out["uploads_hint"] = bool(_upl_hint)
            _hint_ctx = [str(x).strip()
                         for x in (str(context or ""), str(critique or ""),
                                   _upl_hint)
                         if str(x or "").strip()]
            # 整篇作答（同构 maieutic：学生每轮交的是**整篇 spec**）：
            # 上一轮提交的全文进 context 槽，这一轮在它上面补缺口，不是从零重答。
            try:
                import maieutic_scaffold as _ms0
                # ★ 重做的第 N 遍**不**带入上一遍冻结的那篇 spec（2026-11-15）：
                #   它跟"上一版代码"是同一类跨遍记忆 —— 带着上一遍的整篇作答开问，
                #   第一轮就报"全答到了"，Δ 恒为 0（实测第 4 遍：183 字 spec 一带，
                #   25 秒内 Phase1 就闭合）。同一遍内的下一轮照旧带（那是补缺口）。
                _prev_spec = ("" if fresh
                              else _ms0.load_spec_text(scene, question))
            except Exception:
                _prev_spec = ""
            if str(_prev_spec or "").strip():
                _hint_ctx.append(
                    "你上一轮提交的**整篇作答**（这轮在它基础上补缺口，"
                    "不要从零重答）：\n" + str(_prev_spec)[:3000])
                print("  · 上一轮整篇作答已带入（" + str(len(_prev_spec))
                      + " 字）", flush=True)
            # 执行层下面无条件要用 local_agent.solve；原来这个 import 挂在
            # scaffold 分支里，课程场景关掉 scaffold 之后就
            # UnboundLocalError（2026-11-14 P 场景实测）。
            import local_agent
            # scaffold 关掉（课程场景）时不会有维度骨架，但执行层仍要读
            # _dims/_all_dims/_avoid —— 不预置就是 UnboundLocalError
            # （2026-11-14 P 场景实测，连续两发）。空 _dims = 走单解路径。
            _dims: list[dict[str, Any]] = []
            _all_dims: list[dict[str, Any]] = []
            _avoid: list[Any] = []
            if _use_scaffold:
                # 本机原语先注册成条目（origin=local / group=本机能力），
                # 才谈得上被 `enabled_entries` 当抓手段交给执行层。
                # db_path 一起传：scene 形态的库不在场景目录里时，query_sql
                # 也要认它（2026-10-29 的「一个执行工具」口径）。
                try:
                    _act.register_local(scene, local_dir, db_path=db_path)
                    _act.register_net(scene)
                    _act.register_gh(scene)
                except Exception:
                    pass
                try:
                    import maieutic_scaffold as _ms
                    _dims = plan.get("dimensions") or []
                    # ★ maieutic 外层视角：骨架的**全集**维度（不被下面
                    #   preset_dimension 的单维覆盖影响）。原版每一轮学生交的是
                    #   整篇 spec、对着 **exercise 的全部维度**判
                    #   （spec-examiner.ts:13-16）。驾驶舱逐维派发时若只拿 preset
                    #   那一条，"整篇"就退化成一句话（2026-11-11 实测 spec_text
                    #   只有 50 字：dim2 +（未作答））。
                    _all_dims = list(_dims)
                    _avoid = _ms.drift_notes(plan)
                    # ★ 2026-11-11（A 原版，用户拍板）：骨架已固定 → 逐条
                    #   在材料上作答，是原版 **Phase1** 的循环（spec 对话），
                    #   不是 Phase2。这里**不推进阶段** —— 推进到 2 会让
                    #   「写代码」这一段没有真实起点（实测事件流 1→2→3 的
                    #   中段其实是 Phase1 的循环）。Phase2 的起点是
                    #   「spec 已冻结、开始写 finalCode」，在下面里程碑分支里。
                except Exception:
                    _dims, _avoid = [], []
                # 预设维度（驾驶舱逐维度闭合）：只跑传进来的这一条维度，但
                # `plan["dimensions"]` 仍是**完整**维度集（里程碑判定要用它算
                # 总进度）。下面 milestone_state 也改用完整集，单条闭合不会误报
                # "达成"，全闭合那一发才翻 achieved。
                if preset_dimension is not None:
                    _dims = [dict(preset_dimension)]
                if _dims:
                    # ---- 原版子目标制：逐维度 rollout + critic ----
                    # 原版 voyager.py:403-421：拆出的 sub_goals 是**按序逐个
                    # 完成**的，每个子目标一次完整的 rollout。2026-10-19 实测
                    # （Scene D，9 个维度塞一次 prompt 让它一口气答）：闸门
                    # 只能 1/9，两轮连挂。维度即子任务才是原版语义，也是
                    # "闸门评估任务完成没有"的正确粒度。
                    observations: list[dict[str, Any]] = []
                    learned_dims: list[dict[str, Any]] = []
                    failed_dims: list[dict[str, Any]] = []
                    answers: list[str] = []
                    closed_nodes: list[str] = []
                    dim_codes: list[dict[str, Any]] = []
                    # {维度id: [实测调用过的动作]} —— 回写图谱 produced_by
                    # （origin=observed）用的，"工具（MCP）"列的数据源
                    tools_map: dict[str, list[str]] = {}
                    # 父节点（这枚"问题节点"）的 id 必须与 ⑥ `_absorb` 用同一算法，
                    # 否则循环里先落的维度节点和 ⑥ 后落的会算出两套 id → 不幂等。
                    # ⚠️ 学习侧**不能用**消费侧的 `consume_<md5>` 运行期 topic：
                    # `_runtime_topic_id`（consume.py:1949）永远返回 `consume_<md5>`，
                    # 那是"答出来但归不上图谱节点"的**消费运行时**标记。若学习侧复用
                    # 它，学过的里程碑技能会被打上 `consume_*` 前缀 → 消费侧
                    # `consume.py:877` 的过滤把它们当"消费会话级脏账"排除 → 检索不到
                    # 已学技能 → 表现正是用户原话「学过还是现做」。学习侧改用独立的
                    # `learn_<md5>` 前缀，与消费侧 `consume_` 运行时脏账彻底隔离；
                    # 消费侧过滤只排 `consume_*`，放行 `learn_*`。
                    import hashlib as _hl
                    _tid = (f"learn_{_hl.md5(str(question or '').strip()
                                        .encode('utf-8')).hexdigest()[:8]}")
                    try:
                        _prog = _ms.load_progress(scene, question)
                    except Exception:
                        _prog = set()
                    try:
                        # 上次跑通的代码：续学跳过 rollout 时靠它补回技能
                        _old_codes = _ms.load_progress_codes(scene, question)
                    except Exception:
                        _old_codes = {}
                    try:
                        # 追问历史（同 maieutic 的 priorIterations）：上次问了
                        # 什么、还差什么 —— 跨次学习不丢，否则同一批维度反复
                        # 撞同一堵墙，且看不出"上次卡在哪"。
                        _iters = _ms.load_progress_iterations(scene, question)
                    except Exception:
                        _iters = {}
                    # _iters 里混着**上一轮加载来的旧账**和本轮要写的新账。落盘时
                    # 只能交「本轮真正尝试过的那几条」：save_progress_iterations
                    # 是老值 + 本次（同 priorIterations 的按轮累积），把没尝试过的
                    # 旧账整包传回去会被**再加一遍**（预设维度路径每轮只跑一条，
                    # 其余 4 条的轮次会翻倍 → 轮次账虚高，面板就会误报「很顽固」）。
                    _iters_round: dict[str, dict] = {}
                    # ---- 会话日志补种：此前学成的维度也要在「整篇」里 ----
                    # 用户 2026-11-11 口径的必然推论：整篇 = 这个 session 的记忆。
                    # 驾驶舱逐维派发时本次只跑 1 条，其余维度是上一轮学成的 ——
                    # 它们不在本次事件流里，整篇就缺它们，判官只能判「未答到」，
                    # 于是每派一次都把已闭合的维度重新开出来（实测：单维派发后
                    # 整篇只含 1 条，open 里冒出 example_count/module_id/chapter_name）。
                    # 所以开场把「账上有代码/答案、事件流里还没有」的维度补种一条
                    # dim_attempt（如实写"此前已闭合，这次没重跑"），只补一次。
                    try:
                        _have_ev = set()
                        for _e0 in _ms.load_session_events(scene, question):
                            if str(_e0.get("kind") or "") != "dim_attempt":
                                continue
                            _p0 = _e0.get("payload") or {}
                            if isinstance(_p0, dict) and _p0.get("dim_id"):
                                _have_ev.add(str(_p0["dim_id"]))
                        for _sid, _oc in (_old_codes or {}).items():
                            if not _sid or _sid in _have_ev:
                                continue
                            if not isinstance(_oc, dict):
                                continue
                            _ms.append_session_event(
                                scene, question, "dim_attempt", {
                                    "round": 0,
                                    "dim_id": str(_sid),
                                    "question": str(_oc.get("question") or ""),
                                    "tools": [str(x) for x in
                                              (_oc.get("tools") or [])],
                                    "code": str(_oc.get("code") or "")[:900],
                                    "result": "",
                                    "answer": str(_oc.get("answer") or "")[:600],
                                    "closed": True,
                                    "why": "此前已闭合（这次没重跑，账在 progress.codes）",
                                })
                    except Exception:
                        pass
                    # 整篇作答的段落账（维度 id → 这一维的结论），轮末拼成
                    # 全文提交（同构 maieutic studentSpecText：交整篇，不交片段）。
                    # ⚠️ **失败维也要写**（2026-11-11）：整篇要在材料上作答，
                    # 不能留「（未作答）」—— 空段落在判官眼里等于"这一维你没说话"，
                    # 追问只能重复同一句。失败维写的是**材料事实 + 判词**
                    # （取到了什么 / 材料上没有这个量），一样是"在材料上作答"。
                    _ledger: dict[str, str] = {}
                    # 冻结的整篇（Phase1 闭合后写一次，Phase3 拿它当"承诺"面）
                    _spec = ""
                    # 本轮全部**取成功**的材料（整篇判定的材料面，失败项不进）
                    _ev_all: list[dict[str, Any]] = []
                    _timed_out_dims = 0
                    if milestone_retry:
                        # ---- 里程碑重试轮：原版 rollout 同构（用户 2026-10-26：
                        # 「参考原 voyager 的重试里程碑方法，不要自己发明」）----
                        # voyager.py:283-289 rollout(task)：执行层对 task **本体**
                        # 写代码执行。同构三件套：skills 注入（:195 retrieve →
                        # :199 render_system_message，上面 739 行已做）、上轮
                        # critique 进 context 槽（:231-234 render_human_message
                        # (critique=...)，scene_learn 跨轮传入 → _hint_ctx）、
                        # 内层 4 步迭代（solve 自带）。已闭合维度**不重学**（原版
                        # 子目标完成即 progress+1，不会重做）—— 本轮只做「里程碑
                        # 本体取到真数」，critic 收口；组合复合技能入库走下方
                        # achieved 分支（learn() 第二步同构，用存量配方）。
                        print("  · 里程碑重试：对题目本体执行（已闭合维度不重学，"
                              "上轮评语进 context 槽）", flush=True)
                        if _code_exec:
                            _ev0: list[dict[str, Any]] = []
                            # ⚠️ context 槽必须带上 _hint_ctx（里程碑名/导入原话/
                            # 追问历史）—— 2026-10-29 踩到：里程碑维度题只传了
                            # 库表观察块，题干里没有仓库地址 → `_focus_repos`
                            # 抓不到「调研哪个仓库」→ 模型拿搜索引擎的二手结果
                            # 充数。里程碑名（「联网去仓库地址：<https://…>」）
                            # 就在 _hint_ctx 里，别丢。
                            observations, sql = await local_agent.solve(
                                question, local_dir or exec_dir, llm=llm,
                                scene_id=scene, max_iters=INNER_MAX_ITERS,
                                critique=str(critique or ""), dimensions=[],
                                avoid=_avoid, skills=_py_skills,
                                require_tools=_req_tools, evidence=_ev0,
                                timeout=EXEC_TIMEOUT, db_path=exec_db,
                                context=((_env_obs_block(exec_db)
                                          if exec_db else "")
                                         + "\n" + "\n".join(_hint_ctx)).strip())
                        else:
                            observations, sql = await agent.solve(
                                question, skills=skills, grabs=grabs,
                                focus_tables=focus, hints=_hint_ctx)
                        # 存量闭合账（原版 progress：完成的子目标数——不重做）
                        learned_dims = [d for d in _dims
                                        if str(d.get("id") or "") in _prog]
                        closed_ids.update(str(d.get("id") or "") for d in learned_dims
                                          if str(d.get("id") or ""))
                        dim_codes = [
                            {"id": _did,
                             "question": str((_old_codes.get(_did) or {}).get(
                                 "question") or ""),
                             "code": (_old_codes.get(_did) or {}).get("code"),
                             "answer": (_old_codes.get(_did) or {}).get("answer")}
                            for _did in [str(d.get("id") or "") for d in learned_dims]
                            if str((_old_codes.get(_did) or {}).get(
                                "code") or "").strip()]
                        answers = [
                            f"【{str((_old_codes.get(str(d.get('id') or '')) or {}).get('question') or '')[:60]}】"
                            f"{str((_old_codes.get(str(d.get('id') or '')) or {}).get('answer') or '')[:500]}"
                            for d in learned_dims
                            if str((_old_codes.get(str(d.get('id') or '')) or {})
                                   .get("answer") or "").strip()]
                    # milestone_retry 时维度循环零次（已闭合维度不重学，原版
                    # progress 语义），上面的本体 rollout 产物直接进判定段。
                    # ══════ Phase 1 轮循环（同构 maieutic spec/route.ts:83-106）══════
                    # 原版一次次提交：整篇 spec → examiner 判每条维度答到没有 →
                    # gaps_still_open 非空就问（≤3 条）→ 改整篇再交 → 直到为空 →
                    # 进 Phase 2。MVE 原来的循环只跑一遍，判官说"还开着 5 维"也
                    # 没人接（2026-11-11 实测 passed=False 却直接收工）。
                    # 这里补的就是那一圈：闭合 = gaps_still_open 为空，闭合即停，
                    # 等下次派任务。
                    # ★ 2026-11-11 用户（定案）：**不设轮次上限**。
                    #   原版 maieutic 对人做 Phase1 时，只要 gaps_still_open 非空就
                    #   一直追问，没有"问够 3 次就收工"这回事；追问和回答都长在
                    #   同一份整篇上（spec/route.ts 的 iterations 是累积的）。
                    #   MVE 里追问交给 voyager、它在同一份整篇（session 事件流）上
                    #   作答 —— 那"追问不停 → 学习不停、作答不停"就是同一条规则，
                    #   掐轮次等于凭空把学生的嘴封上。
                    #   `MVE_PHASE1_ROUNDS` 只在人工排障时当上限用（0/未设 = 不限）。
                    _p1_round = 0
                    # 「第几次重跑」——原版靠 (exerciseId, studentId) 唯一活跃会话
                    # 天然分段（sessions.ts:159 findOrCreateSession），MVE 的缓存按
                    # question 长期复用，必须显式记段号，否则重跑会接在上一段
                    # passed 之后，缺口序列看起来像退步（实为新一轮的第一轮）。
                    try:
                        _p1_run = _ms.next_spec_run(scene, question)
                    except Exception:
                        _p1_run = 1
                    _p1_open: list[str] = []
                    _p1_qs: list[str] = []
                    _p1_passed = False
                    _p1_judged = False
                    _P1_MAX = int(os.environ.get("MVE_PHASE1_ROUNDS") or 0)
                    # 无进展兜底（2026-11-13）：连续几轮缺口数不降就收。原版是
                    # 人对人，人可以一直问；机器遇到"只能执行才知道"的维度会
                    # 永远绕圈，必须有这条出口。
                    try:
                        _P1_STALL = int(os.environ.get("MVE_P1_STALL") or 4)
                    except Exception:
                        _P1_STALL = 4
                    _p1_stall = 0
                    # ★ 2026-11-15：stall 守卫改按 open **集合**判无进展
                    #   （见 :5490 附近注释）—— 这里存上一轮的缺口集合。
                    #   （原来的 `_p1_prev_open`（数量）是死变量，已随判据
                    #   改成集合而删掉。）
                    _p1_prev_open_set = frozenset()
                    # ★ 原地打转守卫（2026-11-11 实测教训）：filtering_criteria
                    #   连问 9 轮，每轮 code 逐字相同、结果同样 175，判官也每轮
                    #   都在问同一句 —— 追问确实送到了，机器就是不动。原版对人
                    #   可以「问到会为止」，因为人每轮会改答案；机器同一动作同一
                    #   结果重复，就是**没有进展**，再问只是烧 token。
                    #   这里只判「有没有变化」，不判「该不该这么答」，也不封维度
                    #   的门：摘掉的维度仍记未闭合（交人看），门开着等下次派任务。
                    _fp_round: dict[str, str] = {}      # 本轮每维（代码+答案）指纹
                    _p1_fp: dict[str, str] = {}         # 上一轮指纹
                    _p1_stuck: dict[str, int] = {}      # 连续无变化轮数
                    # ★ 摘掉要**持久**（2026-11-11 实测）：判官的 open 每轮
                    #   重算，摘掉的维度下一轮又会出现 → 4/6 在 1~2 维之间
                    #   无限磨。进了这个集合就不再问，只记未闭合。
                    _p1_dropped: set[str] = set()
                    # ★ 已答到的维度是**累计并集**（原版 sessions.ts
                    #   appendPhase1Iteration：instructorConfiguredDimensionsAddressed
                    #   = 每轮 gapsAddressedThisRound 的 cumulative union）。判官每轮
                    #   只看本轮整篇，学生改写时删掉某段文字，那一维就会重新变回
                    #   "还开着" —— 2026-11-13 实测 62 轮里三次 4/1→0/5 的回落就是它。
                    #
                    # ★ 这份并集的**生命周期是「一条会话」，不是「一次 learn()」**
                    #   （2026-11-14 修）：同一 question 的缓存就是原版那条
                    #   phase1.iterations（缓存按 question 复用 = 会话不结束），
                    #   原版的 instructorConfiguredDimensionsAddressed 也挂在这条
                    #   会话上，逐轮累加、绝不回退。原来只在本次调用内累加，于是
                    #   每次重跑都从空集开始 —— 上一段已经 passed 的维度全部重新
                    #   变回「还开着」，实测 req_155633 r3 passed(open=0) 之后
                    #   r4 又 open=4，两段在「4→1→0」上各走一遍，Δ 恒为 0。
                    #   这里把并集从 phase1.addressed 接回来（原版同名字段，
                    #   `src_lib_sessions.ts:264-266` 每轮并集写回、从不重置）。
                    try:
                        _p1_addressed: set[str] = {
                            str(_x) for _x in
                            _ms.load_spec_addressed(scene, str(question))
                            if str(_x or "").strip()
                        }
                    except Exception:
                        _p1_addressed = set()
                    _dim_rounds: dict[str, int] = {}    # 本次发布里该维累计作答轮数
                    _stuck_now: list[dict] = []
                    _P1_STUCK_ROUNDS = int(os.environ.get("MVE_P1_STUCK_ROUNDS") or 2)
                    _P1_DIM_ROUNDS = int(os.environ.get("MVE_P1_DIM_ROUNDS") or 4)
                    # 整篇作答（原版 Phase1）：一轮一次 rollout，输入只有问题 +
                    # 上一轮 maieutic 的 qa（≤3 条追问）。骨架只给判官。
                    # MVE_P1_WHOLE=0 可回退旧逐维路径（排障用）。
                    _whole = str(os.environ.get("MVE_P1_WHOLE") or "1") != "0"
                    _last_spec = ""
                    _ev_carry: list[dict] = []
                    _round_code = ""
                    _spec_fp_prev = ""
                    _spec_same = 0
                    _spec_prev_empty = False
                    # 整篇 = 这次发布的**全部**维度（原版 exercise 的全部 spec gate
                    # 维度，spec-examiner.ts:13-16）。驾驶舱逐维派发时 `_dims` 只有
                    # 被派的那一条，判官却对着全集判 —— 所以"整篇"要用全集，否则
                    # 判官说"还开着 filtering_criteria"，而这一轮只认 preset 那一条
                    # → 过滤器里找不到它 → 后面几轮 0 维空转（2026-11-11 实测
                    # 「Phase1 第 2 轮：上轮仍开着 0 维」）。
                    _spec_dims = list(_all_dims or _dims)
                    # ★ 骨架不给 voyager 看（用户 2026-11-12：「让 voyager 看不见
                    #   骨架，只看的见问题和 qa」）。依据原版：spec_route.ts:22-24
                    #   学生这一面只有 `specText` 一个字段，body 里没有任何维度；
                    #   spec_gate_dimensions 只进 examiner 的 user 消息
                    #   （examiner_pre.ts:208-262）—— 它是**判官的尺子**，不是学生
                    #   的题面。之前把清单塞进 context，模型把骨架当任务清单逐条
                    #   执行（问「按什么口径筛」也照旧交 175），这是假闭合与原地
                    #   打转的上游。现在它只拿到：题面 + 本轮这一个问题 + 上一轮
                    #   的 qa（见下方 crit1）。
                    _face_ctx = "\n【助产士的题面】\n" + str(question).strip()
                    def _prune_stuck_dims(open_ids, fp_round, fp_prev, stuck_n,
                                          thr, dim_cap, dim_rounds):
                        """把「已问到、机器不动」的维度从本轮追问清单里摘掉。

                        两条判据都只看**有没有进展**，不看答案内容对不对：
                          ① 连续 thr 轮（默认 2）（代码+答案）指纹逐字相同；
                          ② 本次发布里该维累计作答轮数到顶（默认 4）——
                             防它在两个写法之间来回摆（指纹每轮都变，①抓不住）。
                        返回 (保留的 open 清单, 摘掉的条目)。摘掉 ≠ 判闭合：
                        调用方必须如实记未闭合。
                        """
                        gone = []
                        for _id in list(open_ids):
                            _fp = str(fp_round.get(_id) or "")
                            if not _fp:
                                continue
                            dim_rounds[_id] = int(dim_rounds.get(_id) or 0) + 1
                            if fp_prev.get(_id) == _fp:
                                stuck_n[_id] = int(stuck_n.get(_id) or 1) + 1
                            else:
                                stuck_n[_id] = 1
                            fp_prev[_id] = _fp
                            if (int(stuck_n.get(_id) or 0) >= int(thr or 2)
                                    or (int(dim_cap or 0) > 0
                                        and dim_rounds[_id] >= int(dim_cap))):
                                gone.append({"id": _id,
                                             "stuck_rounds": int(stuck_n.get(_id) or 0),
                                             "attempts": int(dim_rounds[_id] or 0)})
                        _gone = {g["id"] for g in gone}
                        return [x for x in open_ids if x not in _gone], gone

                    def _apply_stuck_guard():
                        """轮末把打转的维度摘掉并如实落账（判过 / 判官不可用都走它）。"""
                        nonlocal _p1_open, _stuck_now
                        _p1_open, _stuck_now = _prune_stuck_dims(
                            _p1_open, _fp_round, _p1_fp, _p1_stuck,
                            _P1_STUCK_ROUNDS, _P1_DIM_ROUNDS, _dim_rounds)
                        if not _stuck_now:
                            return
                        # 摘掉 = 本次发布不再问它（判官的 open 每轮重算，
                        # 不收进这个集合下一轮又回来）。
                        _p1_dropped.update(str(g["id"]) for g in _stuck_now)
                        print("  · 原地打转："
                              + "、".join(
                                  "%s（%d 轮同一作答/共 %d 轮）"
                                  % (g["id"], g["stuck_rounds"], g["attempts"])
                                  for g in _stuck_now)
                              + " —— 助产士已问到，机器没进展，如实记未闭合",
                              flush=True)
                        out.setdefault("stuck", []).extend(_stuck_now)
                        for _g in _stuck_now:
                            try:
                                _ms.append_session_event(
                                    scene, question, "dim_stuck", {
                                        "round": _p1_round,
                                        "dim_id": _g["id"],
                                        "stuck_rounds": _g["stuck_rounds"],
                                        "attempts": _g["attempts"],
                                        "why": "连续轮次同一动作同一结果，"
                                               "追问未改变行为",
                                    })
                            except Exception:
                                pass

                    _submit_state: dict[str, Any] = {
                        "last_hash": "", "verdicts": [], "last_open": []}
                    _round_v0 = 0            # 本轮开始时已有多少次交卷判词
                    _no_submit_streak = 0    # 连续多少轮判官给不出判词

                    def _submit_fn(text, via="submit_spec"):
                        # 「交卷」动作（用户 2026-11-13）：返回值 = 判官判词。
                        # 学生拿到的是对**这一次提交**的裁决，不是抽象要求。
                        _t = str(text or "").strip()
                        if not _t:
                            return {"error": "提交内容为空：submit_spec 里放"
                                             "你的整篇答案文字（陈述句）"}
                        import hashlib as _hl
                        _h = _hl.md5(_t.encode()).hexdigest()
                        if _h == _submit_state["last_hash"]:
                            return {"error": "与上一次提交一字不差 —— 这不是"
                                             "修改。针对仍开着的维度把结论"
                                             "写成完整句子再提交",
                                    "open": _submit_state["last_open"]}
                        _submit_state["last_hash"] = _h
                        try:
                            import maieutic_gate as _mg_s
                            _pq: list[str] = []
                            for _v in _submit_state["verdicts"]:
                                _pq.extend(str(x) for x in
                                           (_v.get("questions") or []))
                            try:
                                _rounds_gate = _ms.load_spec_rounds(
                                    scene, str(question))
                            except Exception:
                                _rounds_gate = []
                            _g = _mg_s.check(
                                str(question), _t, list(_spec_dims),
                                evidence=([] if _whole else list(_ev_all)),
                                material_required=(not _whole),
                                # ★ 2026-11-15：整篇模式**也给**环境实查清单。
                                #   原来传空（`"" if _whole`）→ 闸门的「口径真实性」
                                #   判据（maieutic_gate.GATE_SYSTEM 的
                                #   「口径必须能在清单里指认」）在生产路径上**没有
                                #   清单可指认** → 只能凭空判 → 口径不可达的量
                                #   被判未答到 → 追问 → 永远答不出来 → 空转。
                                #   实查清单不是「人工口径」（`db_observation`
                                #   现场查表/列/取值），补上它不违反「删口径」。
                                env_obs=_env_obs_block(exec_db or db_path),
                                # ⚠️ 原来这里是 `prior_iterations=_pq[-12:]` ——
                                # `_pq` 是**问句字符串**，而 gate 只收 dict
                                # （`isinstance(x, dict)` 过滤），于是整块静默
                                # 丢掉：提交这条主路径上的判官从来没看见过
                                # PRIOR ITERATIONS，每轮都从零重推缺口（原版
                                # spec-examiner 是每轮都拿 priorIterations）。
                                # 问句走 prior_questions，轮级历史走
                                # prior_iterations，两条通道分开。
                                prior_questions=_pq[-12:],
                                prior_iterations=_rounds_gate)
                        except Exception as _e:
                            return {"error": f"判官不可用：{type(_e).__name__}:"
                                             f" {_e}"}
                        _submit_state["verdicts"].append(_g)
                        _submit_state["last_open"] = [
                            str(x) for x in (_g.get("open") or [])]
                        try:
                            _ms.append_session_event(
                                scene, str(question), "spec_submit", {
                                    "round": _p1_round,
                                    "via": via,
                                    "spec": _t[:4000],
                                    "passed": bool(_g.get("passed")),
                                    "addressed": [str(x) for x in
                                                  (_g.get("addressed") or [])],
                                    "open": [str(x) for x in
                                             (_g.get("open") or [])],
                                    "questions": [str(q)[:200] for q in
                                                  (_g.get("questions")
                                                   or [])][:3],
                                })
                        except Exception:
                            pass
                        return {"passed": bool(_g.get("passed")),
                                "addressed": [str(x) for x in
                                              (_g.get("addressed") or [])],
                                "open": [str(x) for x in
                                         (_g.get("open") or [])],
                                "questions": [str(q)[:300] for q in
                                              (_g.get("questions") or [])][:3],
                                "note": ("闸门已闭合：本轮收工"
                                         if _g.get("passed") else
                                         "未闭合：针对 open 把结论写成完整"
                                         "句子再 submit_spec")}

                    # 交卷动作注册成场景条目（用户 2026-11-13：和 MCP 工具
                    # 一样是注册过的工具 —— 菜单里看得见、面板动作清单里有）。
                    try:
                        local_agent.set_submit_handler(scene, _submit_fn)
                    except Exception:
                        pass
                    while True:
                        _p1_round += 1
                        _fp_round = {}
                        _stuck_now = []
                        _round_v0 = len(_submit_state["verdicts"])
                        # 第一轮做全集；之后只做上一轮判官说还开着的那些
                        # （原版"改整篇"的 MVE 等价：已有作答留在 _ledger，
                        #  只重做没答到的，不把已闭合的重跑一遍烧 token）。
                        if _p1_round == 1:
                            _round_dims = list(_dims)
                        else:
                            # 追问轮：按**整篇的全集**取还开着的那几条（不能按
                            # `_dims` 取 —— 逐维派发时它只有一条，判官说开着的
                            # 往往是别的维，过滤后就 0 维，两轮空转）。
                            _round_dims = [d for d in _spec_dims
                                           if str(d.get("id") or "") in set(_p1_open)]
                            # 整篇模式不按维执行：open 里有对不上的 id 也照样把
                            # 整篇交回去改（判官的尺子只它自己用），不收工。
                            if not _round_dims and not _whole:
                                print("  · 追问轮没有可执行的维度（判官给的 open "
                                      "在本次发布的维度里找不到）—— 如实收工，"
                                      "不空转", flush=True)
                                break
                        if _p1_round > 1:
                            print(f"  · Phase1 第 {_p1_round} 轮：上轮仍开着 "
                                  f"{len(_round_dims)} 维，带追问答"
                                  f"（{len(_p1_qs)} 条）", flush=True)
                        if _whole:
                            # ===== 一轮 = 一次整篇作答（原版 Phase1）=====
                            # voyager 只拿到：问题（题面）+ 上一轮 maieutic 的 qa
                            # （≤3 条追问 + 它上次交的整篇）。骨架（维度清单）只
                            # 给判官：spec_route.ts:23 学生面只有 specText 一个
                            # 字段；examiner_pre.ts:208-262 的 dimensions 只进
                            # 判官的 user 消息。
                            _q1 = str(question).strip()
                            # ★ 交付形态 = 原版 Phase1 的 textarea（2026-11-13
                            # 用户拍板）：Phase1 学生交的是「打算怎么做」的
                            # **承诺**，不是执行结果——验证/可重放代码是 Phase2
                            # 解锁编辑器之后的事（README：学生必须在代码编辑器
                            # 解锁前描述程序行为）。之前在这里要求「实际得到的
                            # 结果是什么——查到的写来源」，把 Phase2 的验收
                            # 提前塞进 Phase1，学生被逼着在沙箱里跑同一份 SQL
                            # 凑「实际结果」（req_...b36b 四轮同文的上游）。
                            try:
                                import scene_profile as _sp_p1
                                _p1_course = not bool(_sp_p1.grounded(scene))
                            except Exception:
                                _p1_course = False
                            if _p1_course:
                                # 无外部材料的场景（推导型）：Phase1 交的是
                                # **这一题的解答**（依据 + 代入 + 结果 + 验算），
                                # 不是代码、不是"我将…"。判据是环境事实
                                # （`is_course` = `not grounded`），不是学科。
                                # 原版 Phase1 交"承诺"是因为答案要靠执行；这类
                                # 题结论当场就能写出来，Phase2 的代码是复核
                                # （2026-11-14 实测：学生连交 4 轮"我将使用…"，
                                # 判官无法判闭合）。
                                _q1 = (f"【题面】{_q1}\n\n"
                                       "【这一轮交什么】这一题的**解答**，不是"
                                       "代码、不是「我打算怎么做」。逐条写：\n"
                                       "① 已知：题面给了哪些量/条件（数值与"
                                       "对应的对象）；\n"
                                       "② 依据：用哪个定义/定理/公式（把等式"
                                       "写出来，如 AB/DE = BC/EF）；\n"
                                       "③ 代入与计算：把数代进式子，写出中间量"
                                       "与最终结果（**要给出具体数值或表达式**）；\n"
                                       "④ 验算：怎么确认这个结果对（量纲、范围、"
                                       "反向代入、与另一条性质对照）。\n"
                                       "每条一句完整的话。**不要写「我将使用…／"
                                       "我打算…」这类空承诺** —— 判官核的是结论"
                                       "本身，不是打算。")
                            else:
                                _q1 = (f"【题面】{_q1}\n\n"
                                       "【这一轮交什么】不是一句答案，不是代码，是"
                                       "**一整篇打算**：你打算怎么做（要查什么/怎么"
                                       "算、用哪些条件或公式）、结果是什么形态（有"
                                       "哪些项/每项是什么）、你承诺的验证方法是什么"
                                       "。分条写，每条一句完整的话。写了「我将…/"
                                       "按…筛/用…验证」就是承诺，判官逐条核。\n"
                                       "**另外必须有一节「这段程序跑一次会做什么」**："
                                       "Phase2 会把你这份打算写成一段**单跑一次就走完"
                                       "整个流程**的程序（点一次 start 就出结果，不用"
                                       "人再补一步）。所以这一节要按顺序写清："
                                       "①查书——从哪张表取什么、按什么规则选；"
                                       "②算/出题——怎么从题面独立推出答案、怎么跟真"
                                       "值核；③交出什么——RESULT 里有哪些键、每个键"
                                       "是什么形态。每步一句，别写笼统口号。")
                            _crit = ""
                            if _p1_round > 1 and str(
                                    (out.get("spec_gate") or {}).get("reason")
                                    or "").startswith("编排层代交未获判词"):
                                _crit = ("上一轮判官没能出判词。这一轮把整篇"
                                         "打算写成陈述句（每条维度一句承诺 + "
                                         "你依据的表/列），不要只写查询代码。")
                            elif _p1_round > 1 and not str(_last_spec or "").strip():
                                # 原版 render_human_message 的 Critique 槽：
                                # 追问必须放在**决定产出内容**的位置。上一轮
                                # 没交出整篇陈述：在这里点明。
                                _crit = ("上一轮你没有交出整篇打算（判官收到"
                                         "的是空/只有代码）。判官核的是**整篇"
                                         "陈述**——每个维度一句承诺。这一轮"
                                         "把整篇写出来。")
                            _prev_txt = ""
                            if _p1_round > 1 and _last_spec:
                                # 原版 render_human_message 的 "Code from the last
                                # round"：上一轮产物回填，学生**在它上面改**
                                # （"refine, not restart"）—— 属于资料区，留
                                # 在 context。
                                _prev_txt = ("\n\n【上次你交的整篇】（在这一份上"
                                             "改，已答对的段落原样保留，别推倒重"
                                             "写）：\n" + str(_last_spec)[:2000])
                            _qs_txt = ""
                            if _p1_round > 1 and _p1_qs:
                                # 原版 render_human_message 的 Critique 槽：追问
                                # 在 Voyager 里的同构物就是 critique —— 模型写
                                # 下一轮代码前读到的最后一段反馈。所以不能拼进
                                # context（资料区），要拼进 critique。
                                _qs_txt = "\n".join("- " + str(x)
                                                     for x in _p1_qs[:3])
                            _crit_out = _crit
                            if _qs_txt:
                                _crit_out = (((_crit + "\n\n") if _crit else "")
                                             + "助产士这一轮要你回答的问题（逐条给"
                                             "结论；答不到就如实说还差什么）：\n"
                                             + _qs_txt)
                            _env1 = _env_obs_block(db_path)
                            _bind_db = exec_db or db_path
                            try:
                                import source_binding as _sb2
                                _bind_desc = _sb2.describe(_q1, _bind_db)
                                _bind_names = _sb2.named_sources(_q1, _bind_db)
                                _bind_tip = _sb2.bind_hint(_q1, _bind_db)
                            except Exception:
                                _bind_desc, _bind_names, _bind_tip = "", [], ""
                            _ev1 = list(_ev_carry)
                            obs1, code1 = [], ""
                            if _whole:
                                # ★ Phase1 学生 = 原版 textarea（2026-11-13 用户
                                # 拍板「Phase1 不做验证和可重放代码」）：原版
                                # maieutic 学生只有 textarea，写「打算怎么做」的
                                # 承诺，代码编辑器 Phase2 才解锁。之前把学生塞进
                                # solve 的沙箱执行循环 —— SQL 在手就手痒，它每轮
                                # 跑同一份查询、把口径写进注释当副产品（req_...
                                # b36b 四轮同文的上游）。现在：单次 LLM 生成整篇
                                # 承诺。输入面同构 buildSpecExaminerUserMessage
                                # 的学生侧：题面 + 上一轮整篇（refine, not
                                # restart）+ 追问（critique 槽）+ 数据源真实
                                # 结构（防瞎承诺）。不给技能、不给执行、不给
                                # 取证 —— 那些是 Phase2 的（承诺→实现→对账
                                # 三段防线的第一段只管承诺）。
                                if _p1_course:
                                    _p1_sys = (
                                        "你是学生，正在解一道教材上的数学题。"
                                        "这一轮交的是**这道题的解答**（纸笔能写完"
                                        "的那种），不是代码、不是执行脚本。\n"
                                        "格式：已知 / 依据（写出等式）/ 代入与"
                                        "计算（给中间量与结果，结果是具体数值或"
                                        "表达式）/ 验算。每条一句完整的话。\n"
                                        "⚠️ 不许写「我将使用…／我打算…／下一步"
                                        "会…」这类空承诺 —— 判官逐条核的是"
                                        "**结论**，空承诺不算答到。\n"
                                        "⚠️ 不许编造题面没有的条件；算不下去就"
                                        "如实写「还差什么」，不要糊过去。\n"
                                        "⚠️ 结果要用题目给的量算出来，对应关系"
                                        "的方向不能反（谁比谁、哪两条是对应边）。")
                                else:
                                    _p1_sys = (
                                        "你是学生。这一轮交的不是代码、不是执行"
                                        "结果，是一整篇【打算】（spec）：你打算"
                                        "查什么/怎么算、按什么条件筛（用哪张表哪"
                                        "一列、什么值）、结果是什么形态、你承诺的"
                                        "验证方法是什么。分条写，每条一句完整的话。"
                                        "\n⚠️ 这一轮不执行、不取数——代码编辑器"
                                        "还没解锁（原版 maieutic：Phase2 才解锁）。"
                                        "写「我将…/按…筛/用…验证」就是承诺；不许"
                                        "编造执行结果，也不许贴查询代码代替陈述。"
                                        "表名/列名必须真实存在（参照下面给的数据源"
                                        "结构），不许编。")
                                _p1_user = (
                                    "【题面】" + str(question).strip()
                                    + "\n\n【数据源的真实结构】（承诺口径时"
                                      "参照）：\n"
                                    + (_env1 if exec_db else "")
                                    + str(_prev_txt or "")
                                    + (("\n\n" + str(_crit_out)) if _crit_out
                                       else ""))
                                _p1_msgs = [{"role": "system",
                                             "content": _p1_sys},
                                            {"role": "user",
                                             "content": _p1_user}]
                                _p1_text = ""
                                try:
                                    if llm is not None:
                                        _p1_text = str(llm(_p1_msgs) or "")
                                    else:
                                        from voyager_vlml.llm import chat \
                                            as _p1_chat
                                        _p1_text = str(await asyncio.to_thread(
                                            _p1_chat, _p1_msgs,
                                            temperature=0.2) or "")
                                except Exception as _p1e:
                                    obs1 = [{"ok": False, "tool": "spec_draft",
                                             "rows": 0, "data": None,
                                             "error": f"{type(_p1e).__name__}: "
                                                      f"{_p1e}"}]
                                if _p1_text.strip():
                                    # 复用下游的 RESULT 提取形状：data.answer
                                    obs1 = [{"ok": True, "tool": "spec_draft",
                                             "rows": 1,
                                             "data": {"answer":
                                                      _p1_text.strip()[:6000]}}]
                                _round_code = ""   # Phase1 无代码（textarea）
                            elif _code_exec:
                                obs1, code1 = await local_agent.solve(
                                    _q1, local_dir or exec_dir, llm=llm,
                                    scene_id=scene, max_iters=INNER_MAX_ITERS,
                                    avoid=_avoid, critique=_crit_out, evidence=_ev1,
                                    skills=_py_skills,
                                    # （MVE_P1_WHOLE=0 回退路径）Phase1 不强制
                                    # 取证：推理本身算作答。防编不在这里：Phase2
                                    # 的 finalCode 要真跑出 spec 承诺的结果，
                                    # Phase3 再拿 spec 和 code 对账。
                                    require_tools=False,
                                    allow_reasoning=True,
                                    timeout=max(EXEC_TIMEOUT, 240.0),
                                    db_path=exec_db,
                                    submit_fn=_submit_fn,
                                    deliver="prose",
                                    context=((_env1 if exec_db else "")
                                             + _face_ctx + _bind_tip + _prev_txt))
                            else:
                                obs1, code1 = await agent.solve(
                                    _q1, skills=skills, grabs=grabs,
                                    focus_tables=focus,
                                    hints=_hint_ctx + [_face_ctx, _prev_txt, _crit_out])
                                _ev1 = list(obs1)
                            observations.extend(obs1)
                            # 它交的整篇（RESULT 里的 answer，形态不限）
                            _spec_round = ""
                            try:
                                _d0 = ((obs1 or [{}])[-1] or {}).get("data")
                                if isinstance(_d0, dict):
                                    _spec_round = str(_d0.get("answer")
                                                      or _d0.get("spec") or "")
                                    if not _spec_round and _d0:
                                        # RESULT 没有 answer/spec 键（实测 4.6
                                        # 学生把搜索结果原样塞进 RESULT）——
                                        # **原样上交判官**，不能变空：判官要核
                                        # 的就是「它交了什么」，空整篇会让它
                                        # 0/5 却不知道学生干了什么。
                                        _spec_round = json.dumps(
                                            _d0, ensure_ascii=False)[:6000]
                                elif isinstance(_d0, str):
                                    _spec_round = _d0
                                elif _d0 is not None:
                                    _spec_round = json.dumps(_d0, ensure_ascii=False)
                            except Exception:
                                _spec_round = ""
                            _obs_ok = [o for o in (obs1 or [])
                                       if isinstance(o, dict) and o.get("ok")]
                            try:
                                _ev_all.extend(_obs_ok)
                            except Exception:
                                pass
                            # 材料留给下一轮（追问轮在材料上作答）
                            _ev_carry = [
                                {"tool": str(o.get("tool") or ""), "ok": True,
                                 "text": json.dumps(o.get("data"),
                                                    ensure_ascii=False)[:800]}
                                for o in _obs_ok][:3]
                            _last_spec = _spec_round
                            _round_code = str(code1 or "")
                            try:
                                _ms.append_session_event(
                                    scene, question, "spec_submit", {
                                        "round": _p1_round,
                                        "submitted": bool(
                                            len(_submit_state["verdicts"])
                                            > _round_v0),
                                        "spec": str(_spec_round)[:4000],
                                        "tools": sorted({
                                            s for o in _obs_ok
                                            for s in (o.get("data_sources") or [])
                                            if str(s).strip()}),
                                        "code": str(code1 or "")[:900],
                                        "error": str((((obs1 or [{}])[-1] or {})
                                                      .get("error")) or "")[:300],
                                    })
                            except Exception:
                                pass
                        else:

                            for _d in ([] if milestone_retry else _round_dims):
                                _did = str(_d.get("id") or "")
                                _q1 = str(_d.get("question") or "").strip()
                                if not _q1:
                                    continue
                                # 外部工具已经连续超时两条 → 剩下的维度大概率同样卡住，
                                # 继续跑只是堆僵尸线程（原版也不会在环境不可用时硬撑：
                                # 它靠 critic 的 onError 短路 + 跳到下一个任务）。
                                if _timed_out_dims >= 2:
                                    print("  · 外部工具连续超时 2 条 —— 本轮到此为止"
                                          "（已闭合的已落盘，剩下的下一轮续学），不硬撑",
                                          flush=True)
                                    break
                                if _did and _did in _prog:
                                    learned_dims.append(_d)     # 断点续学：答到过的不重跑
                                    # 但仍要**保证节点在**（闭合过就该有那一枚技能单位）：
                                    # 旧代码里 ⑤' 判 partial 会提前 return，出现过"答到
                                    # 8 条却颗粒无收"。补挂不重跑、不加证据（bump=False）。
                                    closed_nodes.extend(
                                        close_one_dimension(
                                            scene, _tid, question, _d, bump=False,
                                            import_id=imp_id,
                                            tools=list(_old_codes.get(_did, {}).get(
                                                "tools") or [])
                                            or _called_tools(
                                                _old_codes.get(_did, {}).get("code"))))
                                    if _did:
                                        closed_ids.add(_did)
                                    # Obsidian 式反链：这条维度**被哪几次导入学过**。
                                    # 第二次导入同一问题时，这里会数出 2 条 —— 复用不是
                                    # 口头保证，是图上的入边（maieutic_scaffold.imports_of）。
                                    try:
                                        _imps = _ms.imports_of(scene, _tid, _did)
                                    except Exception:
                                        _imps = []
                                    if len(_imps) >= 1:
                                        print(f"  · 维重复用：{_q1[:22]}｜已有 "
                                              f"{len(_imps)} 次导入学过它"
                                              f"（{', '.join(i['import_id'] for i in _imps[-3:])}）",
                                              flush=True)
                                    # 这一轮没重跑，但**上次跑通的代码**还在 —— 那次
                                    # rollout 的成果不该因为续学被丢掉（原版 program_code）
                                    _c0 = _old_codes.get(_did) or {}
                                    if str(_c0.get("code") or "").strip():
                                        dim_codes.append({"id": _did,
                                                          "question": _c0.get("question")
                                                          or _q1,
                                                          "code": _c0.get("code"),
                                                          "answer": _c0.get("answer")})
                                    _a0 = str(_c0.get("answer") or "").strip()
                                    if _a0:
                                        answers.append(f"【{_q1}】{_a0[:500]}")
                                    # 上次跑通的代码里解析调用过的动作 —— 续学不重跑，
                                    # produced_by 的实测证据只能从代码里来
                                    _t0 = (list(_c0.get("tools") or [])
                                           or _called_tools(_c0.get("code")))
                                    if _t0:
                                        tools_map[_did] = _t0
                                    continue
                                # 维度的一次 attempt（原版一个 task 的 rollout）：
                                # 步数上限 4 在 solve 内（见下方的原版同构说明），
                                # 步与步之间的 critique 回灌也在 solve 内 —— 原版
                                # voyager.py:262-268 的迭代提示同款，不另起外层重试。
                                ok1, crit1, obs1, r1 = False, "", [], {}
                                a1 = ""   # 本维本次作答（qa 回放用，见 _iters["answer"]）
                                # 内层判定（真伪）末态：闸门（覆盖）与内层 critic（真伪）
                                # 两个都过才闭合这条维度，见下方的合并判定
                                _inner_ok = False
                                # 追问历史回灌：这条维度上次（上一轮/上一次学这句话）
                                # 被问过什么、还差什么 —— 直接作为第一轮的输入，别让
                                # 它从零开始撞同一堵墙（maieutic priorIterations 同款）。
                                _h = _iters.get(_did) or {}
                                # 上轮判官问句原文 / 上轮实取材料摘要（qa 回放的两段，
                                # 拆开存：拼好的 qa 只用于本轮输入，账里存原句）
                                _asked_raw = ""
                                _mat_prev = ""
                                if _h and not _h.get("passed"):
                                    # ★ 分层（用户 2026-11-11）：「voyager 学习循环里
                                    # 只让它看到自身情况」。所以这里只递**助产士的问句
                                    # 原话**（_p1_qs 落下来的 asked_q），不把外层的判词
                                    # 拼装（闸门理由 / 来源核对 / 材料搬运判定 / 调过
                                    # 哪些工具）塞进 voyager 的 critique 槽 —— 那些是
                                    # 助产士的账，写进 session 日志即可，不该反过来
                                    # 喂进它的学习循环。
                                    _hq = [str(q).strip() for q in (_h.get("asked_q") or [])
                                           if str(q).strip()]
                                    # ★ 2026-11-11：按维度取追问只能用**本维**那句。
                                    #   asked_q 是整轮 questions 的副本（maieutic 的
                                    #   平行列表），塞进每一维就串味 —— 实测
                                    #   module_ids 收到 example_ids 的追问，于是两轮
                                    #   交同一份答案、被守卫判打转。
                                    #   `asked` 才是上一轮针对这一维的判词。
                                    _ha = str(_h.get("asked") or "").strip()
                                    # qa 文案（用户 2026-11-12：「只看得见问题和 qa」）：
                                    # 上一轮它自己交的那句话 + 助产士的追问，成对回放；
                                    # 不给维度清单、也不把判词拼装塞进来。原版同构：
                                    # examiner_pre.ts:231 的 priorIterations 也是
                                    # 「Round i: student wrote: "…"」这样的问答回放。
                                    _hpa = str(_h.get("answer") or "").strip()
                                    _mat_prev = str(_h.get("result") or "").strip()
                                    # qa 三段：它上次说的、它上次取到的、助产士追问。
                                    # 第三段是"问题"，前两段是"它自己的情况"——用户
                                    # 2026-11-12：「只看的见问题和 qa」。材料要一起给：
                                    # 只给追问等于让它在没有材料的情况下作答。
                                    _qa = []
                                    if _hpa:
                                        _qa.append(f"上次你的作答：{_hpa}")
                                    if _mat_prev:
                                        _qa.append(f"上次你取到的材料：{_mat_prev}")
                                    _asked_raw = _ha or "；".join(_hq[:3])
                                    if _asked_raw:
                                        _qa.append(f"助产士的追问：{_asked_raw}")
                                    crit1 = "\n".join(_qa)
                                    if crit1:
                                        print(f"  · 追问历史：{_q1[:24]}｜上次问过："
                                              f"{crit1[:70]}", flush=True)
                                # 证据账（同构助产士的 gathered）：每次工具调用成功取回的
                                # 内容 —— 失败项 ok=False，**不进材料**。闸门判的是「材料
                                # 有没有答到维度」，不是「模型写的文本像不像答案」。
                                # 材料账：追问轮把上一轮**真取到**的材料带进来（「在
                                # 材料上作答」的前提；原版 Phase1 学生手上就有 gathered
                                # 材料）。首轮为空，真空防线不动。
                                _ev1: list[dict[str, Any]] = [
                                    x for x in (_h.get("evidence") or [])
                                    if isinstance(x, dict)
                                ] if isinstance(_h.get("evidence"), list) else []
                                # ===== 原版同构：一个维度 = 一次 rollout，4 步封顶 =====
                                # （voyager.py:277-279 `done = rollout_num_iter >=
                                #   action_agent_task_max_retries(4) or success`；
                                #  curriculum.py:342 失败即 "Skipping to next task"）
                                # 2026-10-26 用户验收日志拍板「和原版做一样」：曾经的
                                # SUBTASK_MAX_RETRIES=4 外层循环，每一轮是一次**完整
                                # solve()**（自带 4 步 + 步内 critique 回灌——那本来就是
                                # 原版 step 的机制，voyager.py:262-268），外层再套一层
                                # 重试 = 单维度 16 步、双重重试，是原版 4 倍（实测交通
                                # 补贴维度 3+ 个 solve 块连跑 4 分钟）。
                                # 现在（2026-11-11 分层）：一次 solve（4 步）封顶，步内
                                # 只由它自己的 critic 判（原版 action.py:197-200 的
                                # critique 回灌）；maieutic 在**轮末**判整篇、出追问，
                                # 追问作为下一轮的 critique 交给它。外层不进来判。
                                # 不闭合进 failed_dims、跳过下一维度（原版 failed_tasks
                                # 的软重试语义，curriculum.py:184 把 failed 给下一轮看）。
                                # ⚠️ 第二个返回值是**这一轮跑通的代码**（原版的
                                # `program_code`）。以前写成 `obs1, _ =`，代码直接丢
                                # 掉 → 知识型场景永远学不到技能（2026-10-20 定位）。
                                # 场景数据题（db_path）走 VLMLActionAgent + SceneDBEnv
                                # 写 SQL；本机题走 local_agent 写代码跑本机原语 ——
                                # **两种后端都逐维度 solv，每维度闭合写一条技能**
                                # （原版对每任务都拆，不区分知识/数据）。
                                # ★ 分层（用户 2026-11-11）：maieutic 只在**轮末**判一次
                                # （上面的整篇提交判定），不介入 voyager 的 4 步循环 ——
                                # 「maieutic 不该干扰 voyager 的学习循环，它是控制它的外层」。
                                # 原来这里挂了一个步级闸门回调（gate_check），每步判一次并
                                # 把追问并进当步 critique：那是让外层判官在 rollout 内部
                                # 一边判一边改它的输入，rollout 的成败就不再由它自己的
                                # critic 决定了。删掉；追问改为轮末一次性交给它（下一轮的
                                # critique 槽），与原版「critic 判完 → critique 进下一步
                                # 人类消息」同一时序。
                                # env_obs 块一次 attempt 算一次（库只读，rollout 内
                                # 不会变；db_observation 无缓存，每步打库是浪费）。
                                _env1 = _env_obs_block(db_path)

                                # 来源绑定（2026-10-31，A 项）：维度题面点名了本库哪张表，
                                # 就在执行前把「答案必须来自这几张表」讲成硬约束 —— 判据
                                # （下方 _bind）与这句提示同一口径，不做事后才知道的坑。
                                _bind_db = exec_db or db_path
                                try:
                                    import source_binding as _sb
                                    _bind_desc = _sb.describe(_q1, _bind_db)
                                    _bind_names = _sb.named_sources(_q1, _bind_db)
                                    _bind_tip = _sb.bind_hint(_q1, _bind_db)
                                except Exception:
                                    _bind_desc, _bind_names, _bind_tip = "", [], ""
                                if _bind_desc:
                                    print(f"  · 来源绑定：本题点名了{_bind_desc} → "
                                          f"必须真的取到手", flush=True)
                                if _code_exec:
                                    obs1, code1 = await local_agent.solve(
                                        _q1, local_dir or exec_dir, llm=llm,
                                        scene_id=scene, max_iters=INNER_MAX_ITERS,
                                        avoid=_avoid, critique=str(crit1 or ""),
                                        evidence=_ev1,
                                        skills=_py_skills,
                                        # Phase1 不强制取证（同构原版，与上方整篇
                                        # 路径同一口径）：原版 Phase1 学生交的是
                                        # 一段文字，推理算作答；学生自选工具。
                                        # 防编后移到 Phase2 真跑 + Phase3 对账。
                                        require_tools=False,
                                        allow_reasoning=True,
                                        timeout=EXEC_TIMEOUT, db_path=exec_db,
                                        # 库的表/列/真实取值 = 环境观察面（三方共用）。
                                        # 不给它，模型只能猜表名（"库里有没有 web_docs、
                                        # 里面是正文还是两行摘要"全靠猜）；给了它才判得出
                                        # 「这张表里没有答案」→ 换 gh_read 去读仓库。
                                        # 这就是"见人说人话"，不是我们替它分类。
                                        # _face = 题面 + 随题出现的维度清单（助产士给的输入）。
                                        context=((_env1 if exec_db else "")
                                                 + _face_ctx + _bind_tip))
                                    # ⚠️ 代码路径的观测**必须汇进顶层**（2026-11-07 修）：
                                    # 只有下面 else（VLML）分支有 `observations.extend(obs1)`，
                                    # 代码路径漏了 → 顶层汇总时 observations 为空 →
                                    # calls=0 / real_calls=0 → 真空防线判「一次数据源都没调」
                                    # → 明明维度层查到了真数（_ev1 有料、维度闭合、里程碑
                                    # achieved），整轮却被标 status=no_data。
                                    # 实测（场景 L）：维度两条全闭合、里程碑达成，
                                    # 顶层仍 no_data —— 就是这条漏账。
                                    # 口径：代码路径的观测自带 data_sources=list(tracer)
                                    # （local_agent.py:898），直接汇入即可，不用补。
                                    observations.extend(obs1)
                                else:
                                    obs1, code1 = await agent.solve(
                                        _q1, skills=skills, grabs=grabs,
                                        focus_tables=focus,
                                        hints=_hint_ctx + [_face_ctx, str(crit1 or "")])
                                    # 数据题路径：agent.solve 不收 evidence，它实取的 SQL
                                    # 结果不会进学习循环的 _ev1 材料账；但闸门
                                    # （check_task_success）靠 evidence 判「有没有取到
                                    # 真实内容」，否则会误报「一次都没取到内容」把维度判死。
                                    # 这里把 agent 实取的观察回填进 _ev1（与 local_agent
                                    # 路径同构：本地题是 local_agent 直接写 _ev1）。
                                    _ev1 = list(obs1)
                                    # 同样，agent 实取的观察**不带** data_sources（那是
                                    # local_agent 的 MCP 工具才填的字段）。不补的话
                                    # r1["execution"]["real_calls"] 恒为 0（行 813-815 只
                                    # 数带 data_sources 的观察），闸门 task_critic.py:169
                                    # 会误判「一次数据源都没调」把维度判死。照全局观察补
                                    # data_sources 的口径（1009-1013）：只给 ok 的观察补，
                                    # 标签用 env 自报（SceneDBEnv.data_source →
                                    # "scene_db:payroll.duckdb"）。
                                    _env_src: list[str] = []
                                    try:
                                        _ds_fn = getattr(
                                            getattr(agent, "env", None),
                                            "data_source", None)
                                        if callable(_ds_fn):
                                            _v = str(_ds_fn() or "").strip()
                                            if _v:
                                                _env_src = [_v]
                                    except Exception:
                                        _env_src = []
                                    if _env_src:
                                        obs1 = [
                                            dict(o, data_sources=list(
                                                o.get("data_sources") or (
                                                    _env_src if o.get("ok") else [])))
                                            for o in obs1]
                                observations.extend(obs1)
                                r1: dict[str, Any] = {
                                    "execution": {
                                        "calls": len(obs1),
                                        "real_calls": sum(
                                            1 for o in obs1 if _has_real([o])
                                            and (o.get("data_sources") or [])),
                                        "rows": sum(int(o.get("rows") or 0)
                                                    for o in obs1 if o.get("ok")),
                                        "data_sources": sorted(
                                            {s for o in obs1
                                             for s in (o.get("data_sources") or [])}),
                                        "sql": "", "ok": _has_real(obs1)},
                                    "answer": _answer_text(obs1, ok_only=True),
                                    # 材料（代码剪裁的，不是模型自述的）交给
                                    # critic/闸门：没有材料 = 没取到内容，直接不闭合
                                    "evidence": _ev1,
                                    "status": "learned"}
                                # ---- 闭合判定：步级双判定的结果读取（候选A）----
                                # 数据路径：内层 critic（真伪）× 闸门（清单覆盖）已在
                                # rollout 内**每步**判完（action.py step() 的 gate_check
                                # 契约）：agent.last_success 已含闸门（双过才 True），
                                # agent.last_gate_passed = None 表示闸门没跑/不可用
                                # （不阻塞，退化成内层单独判）。轮级 check_task_success
                                # 对数据路径**退役**——它的活儿步级已经干了，再跑一遍
                                # 是双倍 LLM 调用 + 轮级/步级两个判官口径漂移的风险。
                                # 代码路径（_code_exec：本机 + 所有 scene，见 ⑤ 执行层
                                # 开头）没有内层 critic，轮级闸门仍是唯一判官 —— 它的
                                # 观察面里现在带**材料账**（task_critic._evidence_text
                                # 渲染 result["evidence"]），判的是"答案里的说法能不能
                                # 在工具实际返回里对上"，不再只看模型自述。
                                _inner_ok = True
                                if _code_exec:
                                    try:
                                        import task_critic as _tc1
                                        # dimensions=[一个维度] → 走 maieutic_gate 闭合
                                        # 判定。env_obs 必须一起给：闸门只核清单核不出
                                        # 口径幻觉，2026-10-26 实测 base_salary ×
                                        # overtime_hours 当加班费穿过只核清单的闸门入库
                                        # （164000 假数）——列×列合成新口径 = 假设费率。
                                        import reward as _rw
                                        ok1, crit1 = _tc1.check_task_success(
                                            _q1, str(crit1 or ""), r1, dimensions=[_d],
                                            env_obs=_env_obs_block(exec_db or db_path),
                                            prior_questions=list(
                                                (_iters.get(_did) or {}).get("asked_q")
                                                or []),
                                            has_env=_rw.scene_has_env(scene),
                                            scene_id=scene)
                                    except Exception as e1:
                                        ok1, crit1 = False, f"critic 异常：{type(e1).__name__}: {e1}"
                                else:
                                    _inner_ok = bool(getattr(agent, "last_success", False))
                                    _gp = getattr(agent, "last_gate_passed", None)
                                    # 闭合信号只能来自 maieutic（用户 2026-11-11）：
                                    # 闸门没跑（None）= 没判过 ≠ 通过。原来写
                                    # `is not False`，闸门缺席时内层 critic 独自就能
                                    # 闭合 —— 那等于闭合信号来自内层。
                                    ok1 = _inner_ok and (_gp is True)
                                    # 判词 = 闸门追问 + 内层判词（可操作的反馈；
                                    # 2026-10-26 实证：内层两次 4/4 拒绝
                                    # base_salary×overtime_hours 费率幻觉，闸门放行——
                                    # 两份判词都要留，人和下一轮都需要）
                                    _icrit = ""
                                    try:
                                        _icrit = str(getattr(
                                            getattr(agent, "critic", None),
                                            "last_critique", "") or "")
                                    except Exception:
                                        _icrit = ""
                                    _gcrit = str(getattr(
                                        agent, "last_gate_critique", "") or "")
                                    crit1 = "；".join(x for x in (_gcrit, _icrit)
                                                     if x)[:400]
                                    if _gp is None and _inner_ok:
                                        crit1 = ("闸门本轮没判（last_gate_passed=None）"
                                                 "—— 闭合信号只能来自 maieutic，"
                                                 "按未闭合记"
                                                 + (("｜" + crit1) if crit1 else ""))[:400]
                                    # 闸门留痕（取代轮级 check_task_success 写的
                                    # r1["critic_gate"]）—— 面板/日志要能回看
                                    # 「清单核到哪几条、追问了什么」
                                    _gr = getattr(agent, "last_gate_result", None) or {}
                                    r1["critic_gate"] = {
                                        "addressed": list(_gr.get("addressed") or []),
                                        "open": list(_gr.get("open") or []),
                                        "questions": list(_gr.get("questions") or []),
                                        "emergent": list(_gr.get("emergent") or []),
                                        "origin": str(_gr.get("origin") or ""),
                                        "gate_passed": _gp,
                                    }
                                # （闭合判定在下方：一次 attempt 后的单次判定）
                                # 超时 = 外部工具没回来（慢/被限流），**不是代码写错
                                # 了** —— 原地重试毫无意义（原线程还在等，新线程又
                                # 起），照原版 curriculum.py:342 "Skipping to next
                                # task" 跳过这条维度，等下一轮（断点续学）再来。
                                if any(bool(o.get("timed_out")) for o in obs1):
                                    crit1 = (f"上一次执行超时（>{int(EXEC_TIMEOUT)}s，"
                                             "外部工具没回来：慢或被限流）—— 本条维度"
                                             "判未闭合，下一轮带追问历史续学")
                                    print(f"  · 执行超时 → 本条维度判未闭合（不重试）："
                                          f"{_q1[:28]}", flush=True)
                                    _timed_out_dims += 1
                                    ok1 = False
                                    _inner_ok = False
                                # ---- 来源绑定判定（A 项，确定性，不花 LLM）----
                                # 旧判据只问「取到没有」：H 实测 dim4 调 gh_repo_info 拿仓库
                                # description 回答「web_facts 表 claim 字段是什么」，材料非空、
                                # 措辞也对得上 → 闭合 → 里程碑假亮。这里补问一句
                                # 「取的是不是这一份」：题面点名的真实表，必须有一条成功
                                # 调用其**入参**点到它（判据见 source_binding.py）。
                                #
                                # ★ 取证落点表的「上游等价」放宽（2026-11-09 服务器实测）：
                                #   维度问「web_facts 表中…平台判定列…」而执行层调的是
                                #   `mcp_sherlock_lookup_username` —— web_facts 恰恰就是
                                #   Sherlock 结果落库后的摊平宽表（同一份数据的源头）。
                                #   不放宽 → 把最直接的取法判死 → 这条维度永远闭合不了
                                #   （实测：维度卡在 web_evidence/web_facts 上，连着几轮
                                #   都「维度闭合」但题答不出来）。
                                #   两个清单都是**实况**：取证落点表 = `local_agent.
                                #   _ingest_tables` 实查；远端抓手 = actions 注册表
                                #   origin=net|mcp 的条目。与消费侧同一份实现、同一口径。
                                try:
                                    import source_binding as _sb2
                                    _up_tables: list[str] = []
                                    try:
                                        import local_agent as _la
                                        _up_tables = list(
                                            _la._ingest_tables(str(_bind_db or "")) or [])
                                    except Exception:
                                        _up_tables = []
                                    _up_tools: list[str] = []
                                    try:
                                        import actions as _act
                                        _bucket = getattr(_act, "_REGISTRY", {}).get(
                                            str(scene_id or "")) or {}
                                        if not _bucket:
                                            try:
                                                _act.restore()
                                            except Exception:
                                                pass
                                            _bucket = getattr(_act, "_REGISTRY", {}).get(
                                                str(scene_id or "")) or {}
                                        _up_tools = sorted({
                                            str(a.name) for a in _bucket.values()
                                            if str(getattr(a, "origin", "")) in ("net", "mcp")})
                                    except Exception:
                                        _up_tools = []
                                    _bind_ok, _bind_why = _sb2.check(
                                        _q1, _ev1, _bind_db,
                                        upstream_tables=_up_tables,
                                        upstream_tools=_up_tools)
                                except Exception:
                                    _bind_ok, _bind_why = True, ""
                                if not _bind_ok:
                                    ok1 = False
                                    _inner_ok = False
                                    _tail = str(crit1 or "").strip()
                                    crit1 = (_bind_why + ("；" + _tail if _tail else ""))[:400]
                                    print(f"  · 来源未绑定 → 判未闭合：{_bind_why[:90]}",
                                          flush=True)
                                # ---- 语义收口（2026-10-31，用户按 DeepSeek 定的职责划分）----
                                # 取证归工具、判断归模型：材料已经取到手却没答到（判官说
                                # 「你只交回了原文」），就**关掉工具**，只让模型在材料上
                                # 作答 —— 这正是 V3.2 "Thinking in Tool-Use" 的分工，
                                # 也是「发现一直回答不上 → 启动模型能力」。
                                # 只在来源已绑定时才做：材料取错了对象时，再推理只会错得更远。
                                _via_reason = False
                                try:
                                    import reason_pass as _rp
                                    _mat = _rp.material_text(_ev1)
                                except Exception:
                                    _rp, _mat = None, ""
                                # 执行账按**材料账**对齐（2026-10-31 实测）：solve 最终步
                                # 没产出 RESULT 时 real_calls/rows 会记成 0，但材料账里
                                # 明明有 ok 条目 + 上万字正文。不校对的话，真空防线会抢在
                                # 闸门前面把维度判死，判词还写成「每行都是空值(NULL/0)」
                                # —— 说了一句跟事实相反的话（材料就在手上）。
                                _ok_ev = [e for e in _ev1
                                          if isinstance(e, dict) and e.get("ok")
                                          and str(e.get("text") or "").strip()]
                                if _ok_ev:
                                    _ex1 = dict(r1.get("execution") or {})
                                    _ex1["real_calls"] = max(
                                        int(_ex1.get("real_calls") or 0), len(_ok_ev))
                                    _ex1["rows"] = max(int(_ex1.get("rows") or 0), len(_ok_ev))
                                    _ex1["calls"] = max(int(_ex1.get("calls") or 0), len(_ok_ev))
                                    _ex1["ok"] = True
                                    _ex1["data_sources"] = sorted(
                                        {str(e.get("tool") or "") for e in _ok_ev})
                                    r1["execution"] = _ex1
                                    # 整轮口径也要对齐：no_data 闸门看的是 out（见 _material_real）
                                    _mat_ok = _material_real(_ok_ev)
                                    if _mat_ok:
                                        out["_mat_real"] = max(
                                            int(out.get("_mat_real") or 0), len(_mat_ok))
                                # ---- 搬运材料 ≠ 回答（确定性判据，先于判官）----
                                # 2026-10-31 实测：同一个行为「gh_read → RESULT = 整篇
                                # README」被判官连拒 5 次、又放过 2 次（vlml_details
                                # r=1.0，答案就是 15145 字节原文）。**判官会抖，判据不能抖**。
                                # 材料已经到手还交原文，说明它在用取数逻辑冒答语义题 ——
                                # 判未闭合 —— 由助产士在轮末问「请给判断，别搬原文」。
                                if _mat and ok1 and _inner_ok:
                                    try:
                                        _is_dump, _nhit = _rp.is_material_dump(
                                            str(r1.get("answer") or ""), _mat)
                                    except Exception:
                                        _is_dump, _nhit = False, 0
                                    if _is_dump:
                                        ok1 = False
                                        _inner_ok = False
                                        # 只判、不改写 voyager 的输入（分层）：这条事实
                                        # 进 session 日志，追问由助产士在轮末统一问
                                        # （gate 的 questions），不由这里代它发话。
                                        print("  · 答案是材料搬运 → 判未闭合"
                                              f"（{_nhit}% 逐字命中，确定性判据）", flush=True)
                                # ★ 分层（用户 2026-11-11）：材料到手却没答到，
                                # **不在这里替它答**。原版 voyager 的循环是「写代码 →
                                # 看观察 → 再写」，材料在观察里，下一轮它自己就会在
                                # 材料上作答；外层再起一个 answer_over_material 等于
                                # 在 voyager 之外另建一个答题器，还带示范库往它脑子里
                                # 塞"该怎么判断"—— 这正是"学习循环里套了太多 maieutic"。
                                # 现在只留一行事实日志，追问走轮末 maieutic 的正规通道。
                                if not (ok1 and _inner_ok) and _mat:
                                    print(f"  · 本轮没答到（实取材料 {len(_mat)} 字）→ "
                                          f"交回助产士，由它追问", flush=True)
                                # ---- (success, critique) 不可分契约（2026-10-26 补）----
                                # 原版 check_task_success **一次调用同时产出** success 与
                                # critique（voyager.py:232-238 返回二元组），结构上不存在
                                # 「判过但没判词」这个状态。MVE 的判词是三处拼的（闸门
                                # 追问 + 内层 critic + 执行报错），没有一处兜底 —— 实测出现
                                # 过 4 步全「取到 1 行」、critique 为空就判未闭合，下一轮
                                # 拿着空判词续学 = 原地打转（日志里形如「｜｜」）。
                                # 补齐契约而不是补丁：**没判词 = 这次判定没发生**，如实
                                # 记一行，不拿它当下一轮的追问输入。
                                _crit_missing = not str(crit1 or "").strip()
                                if _crit_missing:
                                    crit1 = ("（本轮没有产生判词：闸门与内层 critic 都未给出"
                                             "理由 —— 如实记录，判未闭合，不带入下一轮追问）")
                                # ---- r 沉淀（数据路径，2026-10-28 服务器实测补漏）----
                                # check_task_success（内含 compute_r 包装层）只在
                                # is_local 分支被调；数据路径判定权在内层 critic + 闸门，
                                # 判完从不算 r → r1 恒无 "r" → dim_codes 全 -1.0 →
                                # 入库口 r >= 0 不成立 → r 字段整体缺失，组内择优与
                                # 索引排序（0.3*r）全空转（learn_74c71ce2 四条技能
                                # 实证）。这里与 is_local 分支同口径补算：判定已定
                                # （ok1 含超时修正），配方载体从实跑代码补进执行账。
                                try:
                                    import reward as _rw_r
                                    r1["execution"]["sql"] = str(code1 or "")
                                    r1["r"] = _rw_r.compute_r(
                                        r1, bool(ok1 and _inner_ok),
                                        _rw_r.scene_has_env(scene))
                                except Exception:
                                    pass
                                # 它交了什么，与闭没闭合无关：qa 回放要「上次你的
                                # 作答」，整篇也要有「它说的结论」。只记闭合维会让
                                # 未闭合维在整篇里只剩判定行，判官只能去别维找话说
                                # （实测 record_matching 的判词引用了 example_count
                                #  的 175）。判定不变，这里只补事实。
                                try:
                                    a1 = str((r1 or {}).get("answer") or "").strip()
                                except Exception:
                                    a1 = ""
                                if ok1 and _inner_ok:
                                    learned_dims.append(_d)
                                    if a1:
                                        answers.append(f"【{_q1}】{a1[:500]}")
                                    # 这一维的结论进整篇作答（轮末拿全文判全集）
                                    if _did:
                                        _ledger[_did] = (a1 or "")[:1200]
                                    # 这条维度**实测**调用过的动作（执行层 tracer 记的；
                                    # tracer 空就从代码解析兜底）——回写图谱的工具来源
                                    _t1 = sorted({s for o in obs1
                                                  for s in (o.get("data_sources") or [])
                                                  if str(s).strip()}) \
                                        or _called_tools(code1)
                                    if _did and _t1:
                                        tools_map[_did] = _t1
                                    dim_codes.append({"id": _did, "question": _q1,
                                                      "code": str(code1 or ""),
                                                      "answer": str(a1 or ""),
                                                      # 事实记录：这条是靠取证后的语义收口
                                                      # 闭合的，还是取证当轮就闭合的
                                                      # （判据不变，只是让人看得出路径）
                                                      "how": "reason" if _via_reason
                                                      else "tool",
                                                      "tools": list(tools_map.get(_did)
                                                                    or []),
                                                      # critic 出口沉淀的标量 reward
                                                      # （task_critic 包装层写进 r1["r"]）
                                                      # → 断点续学账本 + 入库择优都用它
                                                      "r": float(r1.get("r", -1.0))})
                                    # 最小技能单位 = **一枚节点 = 闭合一个（维度）问题**
                                    # （用户 2026-10-19 定）：闭合一条就落一条，不等整个
                                    # 任务闭合 —— 否则 8/9 那种 partial 会因为 ⑤' 提前
                                    # return 而颗粒无收（2026-10-19 服务器实测）。
                                    closed_nodes.extend(
                                        close_one_dimension(
                                            scene, _tid, question, _d, import_id=imp_id,
                                            tools=tools_map.get(_did)))
                                    if _did:
                                        closed_ids.add(_did)
                                    # 每闭合一条就**落盘 + 打一行**：原版 rollout 是一条
                                    # 一条推进的，日志不逐条可见时，人只能干等（实测
                                    # 15 分钟看不到任何输出，以为卡死）。同时断点续学
                                    # 的粒度也从"整轮"细到"每条"——中途挂掉不丢已闭合的。
                                    try:
                                        _ms.save_progress(
                                            scene, question,
                                            [str(d.get("id") or "")
                                             for d in learned_dims])
                                        _ms.save_progress_codes(scene, question,
                                                                dim_codes)
                                    except Exception:
                                        pass
                                    print(f"  · 维度闭合 {len(learned_dims)}/{len(_dims)}"
                                          f"：{_q1[:30]}"
                                          + (f"（工具：{','.join(_t1)}）" if _t1
                                             else "（没调工具）"), flush=True)
                                else:
                                    failed_dims.append({
                                        "id": _did, "question": _q1,
                                        "attempts": 1,  # 一次 rollout 封顶（原版同口径；步数上限在 solve 内）
                                        "critique": str(crit1)[:200]})
                                    # 失败的也要逐条可见：否则整轮跑完只看到"N 条没闭合"，
                                    # 不知道是模型没调工具、还是工具本身报错（2026-10-20）
                                    _e1 = str((obs1 or [{}])[-1].get("error") or "")[:90]
                                    print(f"  · 维度未闭合 {len(learned_dims)}"
                                          f"/{len(_dims)}：{_q1[:28]}"
                                          f"｜{str(crit1)[:60]}"
                                          + (f"｜执行报错：{_e1}" if _e1 else "")
                                          + f"｜调过：{','.join(sorted({s for o in obs1 for s in (o.get('data_sources') or [])})) or '无'}",
                                          flush=True)
                                    # 可观测性缺口（2026-10-31 用户指出「tools=[] 时看不到
                                    # 模型写了什么」）：失败必须留下它最后那段代码的指纹，
                                    # 否则只能靠判词猜它是"没写"、"写错"还是"根本没取数"。
                                    _code_hint = str(code1 or "").strip().replace("\n", " ⏎ ")
                                    print(f"  · 上次代码（{len(str(code1 or ''))} 字）："
                                          f"{_code_hint[:300] or '（没产出代码）'}",
                                          flush=True)
                                # ---- 会话日志：这一条尝试**当场追加**（增量）----
                                # 用户 2026-11-11：整篇 = 这个 session 的记忆与日志，
                                # 增量的。所以每次尝试都往 session.events 里追加一条
                                # （问了什么、调了什么、代码、实际结果、判成什么），
                                # 轮末判官读的就是这条流 —— 不重拼、不覆盖旧条目。
                                try:
                                    _res_ev = ""
                                    for _o in reversed([x for x in (obs1 or [])
                                                        if isinstance(x, dict)
                                                        and x.get("ok")]):
                                        _dd = _o.get("data")
                                        _res_ev = str(
                                            _o.get("text")
                                            or (json.dumps(_dd, ensure_ascii=False)
                                                if _dd is not None else ""))
                                        break
                                    _fp_round[_did] = str(hash((str(code1 or ""),
                                                                str(a1 or ""))))
                                    _ms.append_session_event(
                                        scene, question, "dim_attempt", {
                                            "round": _p1_round,
                                            "dim_id": _did,
                                            "question": _q1,
                                            "tools": (list(tools_map.get(_did) or [])
                                                      or sorted({s for o in obs1
                                                                 for s in (o.get("data_sources") or [])
                                                                 if str(s).strip()})),
                                            "code": str(code1 or "")[:900],
                                            "result": _res_ev[:900],
                                            "answer": str(a1 or "")[:600],
                                            "error": str((obs1 or [{}])[-1].get("error") or "")[:300]
                                                     if obs1 else "",
                                            "closed": bool(ok1 and _inner_ok),
                                            "why": str(crit1 or "")[:400],
                                        })
                                except Exception:
                                    pass
                                # 记一条追问历史（同 maieutic 的 appendPhase1Iteration）：
                                # **成没闭合都要记** —— 下次才知道这题问过、卡在哪一句。
                                #
                                # ⚠️ rounds 这里是**本轮的尝试次数**（恒为 1：一次 rollout
                                # 封顶），累加发生在 save_progress_iterations（老值 + 本次，
                                # 同 maieutic priorIterations 的按轮累积）。别在这里再加一
                                # 遍 —— 实测会双重计数（2026-10-26 修）。
                                _iters[_did] = {
                                    "rounds": 1,
                                    # 存**判官原句**，不存拼好的 qa（拼好的那份含上次的
                                    # qa，再拼就会滚雪球：实测 00:23 出现「上次问过：
                                    # 执行层没有产出答案…」这种自我指涉）
                                    "asked": (_asked_raw or str(crit1))[:300],
                                    # asked_q：本次闭合问过的**干净追问清单**（步级
                                    # 闸门的 questions）—— 下一轮 gate 回调的
                                    # prior_questions 从这取（同构 priorIterations，
                                    # 防重复问）。asked 是合并判词（人看的），
                                    # asked_q 是问句列表（机器复用的），两份都要。
                                    # ⚠️ 判词缺失时**不写 asked_q**：那是下一轮闸门回调的
                                    # prior_questions（机器复用的追问清单），拿一条占位
                                    # 话进去等于让下一轮对着空气问（见上面契约注释）。
                                    "asked_q": ([] if _crit_missing else
                                                [str(q)[:200] for q in
                                                 (getattr(agent, "_gate_asked", None)
                                                  or [])][:6]) if not _code_exec else [],
                                    "gap": ("" if (ok1 and _inner_ok) else str(_q1)[:300]),
                                    # 本次作答原文（下一轮的 qa 回放读它）：让 voyager
                                    # 看到「我上次说了什么」，而不是只有助产士的问句。
                                    "answer": str(a1 or "")[:300],
                                    # 本轮实取材料的摘要（下一轮 qa 的「上次你取到的
                                    # 材料」）：增量整篇，旧条目不改写。
                                    "result": str(locals().get("_res_ev") or "")[:400],
                                    # 本轮实取的材料（下一轮追问轮带进 _ev1）：
                                    # 只收 ok 且有返回的观察，text 化后截断。
                                    "evidence": [
                                        {"tool": str(o.get("tool") or ""), "ok": True,
                                         "text": json.dumps(o.get("data"),
                                                            ensure_ascii=False)[:800]}
                                        for o in (locals().get("obs1") or [])
                                        if isinstance(o, dict) and o.get("ok")
                                        and o.get("data") is not None
                                    ][:3],
                                    "passed": bool(ok1 and _inner_ok),
                                    "tools": list(tools_map.get(_did) or []),
                                }
                                # 只把**本轮尝试过的**交出去落盘（老值 + 本次）；未尝试的
                                # 旧账留在文件里不动，见 _iters_round 处的注释。
                                # 同一 learn() 内重跑同一维（Phase1 第 N 轮）时轮次要
                                # 累加，否则 save 那层只看到 1，面板会显示"只问过一次"，
                                # 实际已经问了三轮（2026-11-11 实测 rounds 恒为 1）。
                                _iters[_did]["rounds"] = (
                                    int((_iters_round.get(_did) or {}).get("rounds") or 0)
                                    + 1)
                                _iters_round[_did] = _iters[_did]
                                # ---- 失败维的"材料作答"（2026-11-11）----
                                # 整篇要在材料上作答：成功的维写 answer（上面 2050），
                                # 失败的维也要写**它这次在材料上得到了什么**，否则整篇
                                # 里是「（未作答）」—— 判官看不到它试过什么，追问只能
                                # 重复同一句，第二次派任务也不知道上次卡在哪。
                                # 写的是事实（判词 + 有没有取到材料），不是解释。
                                if _did and not (ok1 and _inner_ok):
                                    _ledger[_did] = (
                                        "（本轮在材料上没答到："
                                        + (str(crit1)[:240] if str(crit1 or "").strip()
                                           else "闸门与内层 critic 都无判词")
                                        + f"｜实取材料 {len([o for o in (obs1 or []) if isinstance(o, dict) and o.get('ok')])} 条"
                                        + "）")
                                try:
                                    _ev_all.extend([o for o in (obs1 or [])
                                                    if isinstance(o, dict)
                                                    and o.get("ok")])
                                except Exception:
                                    pass
                        # ---- 轮末提交：整篇作答 × 全集清单（同构 maieutic Phase 1）----
                        # 原版 spec-examiner.ts:14-16：学生每轮交的是**整篇 spec**，
                        # examiner 拿「全部维度 + 当前全文 + priorIterations」判一次，
                        # 输出 gaps_addressed / gaps_still_open / emergent_gaps / questions。
                        # MVE 原先每维一次单点判定（dims=[_d]），判官既看不到全文也看不到
                        # 历史 → 单句像在答就过（2026-11-11 实证 4/4 全过、答案全错）。
                        # 这里补的是「一次提交」的那一次判定；rollout 内不再有步级
                        # 闸门（2026-11-11 分层：maieutic 只在轮末判一次）。
                        # _spec_dims 在轮循环之前已算好（整篇 = 全集维度）。
                        if _spec_dims:
                            # ★ 整篇 = 这个 session 自己的记忆与日志（增量）：
                            #   用户 2026-11-11「把当前 voyager 在跑这个 session
                            #   的记忆、日志当作整篇，这个是增量的」。
                            #   所以这里**不重拼**一份 Q&A：直接渲染 session
                            #   事件流（每条尝试的代码/工具/实际结果/判词 + 前几轮
                            #   判官的结论），它每轮往后长，旧条目不动。
                            #   ⚠️ 2026-11-11 实测教训：原来轮末从 _ledger 现拼，
                            #   失败只有一句「（本轮在材料上没答到）」、成功的只剩
                            #   一段 answer 字符串 → 判官看不到 voyager 实际跑了什么，
                            #   于是把「列出 175 个 ID」判成「你用省略号」／「材料搬运」
                            #   交替死锁，三轮回合全部原地打转。
                            try:
                                _spec = render_session_text(
                                    _ms.load_session_events(scene, question))
                            except Exception:
                                _spec = ""
                            if not str(_spec or "").strip():
                                # 还没有任何事件（首次进来 / 事件没落上）→ 退回
                                # 逐维账，绝不让判官拿到空全文
                                _spec_parts: list[str] = []
                                for _i, _d in enumerate(_spec_dims):
                                    _sid = str(_d.get("id") or "")
                                    _ans = str(_ledger.get(_sid) or "").strip()
                                    if not _ans:
                                        _oc = (_old_codes.get(_sid)
                                               if isinstance(_old_codes, dict) else None)
                                        _ans = str((_oc or {}).get("answer") or "").strip()
                                    _spec_parts.append(
                                        "【" + str(_i + 1) + "】"
                                        + str(_d.get("question") or "").strip() + "\n"
                                        + (_ans or "（未作答）"))
                                _spec = "\n\n".join(_spec_parts)
                            _gate_all: dict[str, Any] = {}
                            if len(_submit_state["verdicts"]) > _round_v0:
                                # 学生本轮已经用 submit_spec 交过卷并拿到了
                                # 判词 —— 直接采用**最后一次**提交的判定，
                                # 不重判（同一份 spec 判两次 = 两次意见，
                                # 且白烧一次 LLM）。
                                _gate_all = dict(
                                    _submit_state["verdicts"][-1])
                                _gate_all.setdefault("origin", "llm")
                            elif _whole:
                                # Voyager 同构（用户 2026-11-13 08:13 拍板）：
                                # 原版 voyager.py step() 学生**零提交动作**——
                                # harness 执行代码后自动采集 events，判官
                                # （critic.py check_task_success）直接看 events；
                                # 判词 + events + 代码回灌下一轮。我们的 events
                                # 等价物 = render_session_text(session events)
                                # （每轮增量、旧条目不动）。轮末由**编排层代交**
                                # 这份事件流给同一个判官（_submit_fn →
                                # maieutic_gate.check，判词口径不变），学生零动作。
                                # submit_spec 按钮保留为**可选**通道（学生主动
                                # 改稿重交；上面 verdicts 分支先采用它的判词）。
                                # 原版 render_human_message 同构：判官看
                                # **本轮**执行记录 + 上轮判词（_submit_fn 里
                                # prior_iterations 已带上判词历史），不是无限
                                # 增长的全量事件流 —— session 渲染 6000 截断
                                # 会把学生的实际工作挤成「更早的 N 条记录略」
                                # （req_0823059a54 实测判官因此说「正文里没有
                                # 给出清单」）。
                                _auto_parts: list[str] = []
                                if str(_last_spec or "").strip():
                                    _auto_parts.append(
                                        "【本轮学生 RESULT（它交的整篇）】\n"
                                        + str(_last_spec)[:6000])
                                if str(_round_code or "").strip():
                                    _auto_parts.append(
                                        "【本轮代码】\n"
                                        + str(_round_code)[:1600])
                                if _auto_parts:
                                    _auto_text = "\n\n".join(_auto_parts)
                                else:
                                    _auto_text = str(_spec or "")
                                try:
                                    _auto = _submit_fn(_auto_text,
                                                       via="harness")
                                except Exception as _au_e:
                                    _auto = {"error": f"{type(_au_e).__name__}: "
                                                      f"{_au_e}"}
                                if isinstance(_auto, dict) \
                                        and "passed" in _auto:
                                    _gate_all = dict(
                                        _submit_state["verdicts"][-1])
                                    _gate_all.setdefault("origin", "llm")
                                else:
                                    _why = str((_auto or {}).get("error")
                                               or "代交未获判词")[:110]
                                    _gate_all = {"origin": "no_submit",
                                                 "reason": "编排层代交未获判词"
                                                           "（" + _why + "）"}
                            else:
                                try:
                                    import maieutic_gate as _mg2
                                    # 逐维回退路径：保留轮末判（各维自己的
                                    # 闭合账 close_one_dimension 依赖它）。
                                    _gate_all = _mg2.check(
                                        question, _spec, _spec_dims,
                                        evidence=([] if _whole else _ev_all),
                                        material_required=(not _whole),
                                        # ★ 2026-11-15：同 :4034 —— 整篇模式
                                        #   也给环境实查清单，让口径真实性判据
                                        #   有实况可指认（见该处注释）。
                                        env_obs=_env_obs_block(exec_db or db_path),
                                        prior_iterations=_ms.load_spec_rounds(
                                            scene, question))
                                except Exception as _ga_e:
                                    _gate_all = {"origin": "failed",
                                                 "reason": str(type(_ga_e).__name__)}
                            if str(_gate_all.get("origin") or "") == "llm":
                                _p1_judged = True
                                _add2 = [str(x) for x in
                                         (_gate_all.get("addressed") or [])]
                                _open2 = [str(x) for x in
                                          (_gate_all.get("open") or [])]
                                _qs2 = [str(q)[:200] for q in
                                        (_gate_all.get("questions") or [])][:3]
                                # ---- Phase1 轮循环的状态（同构原版：
                                # gaps_still_open 决定下一轮做什么）----
                                # ① 累计并集（原版 appendPhase1Iteration）
                                _p1_addressed.update(
                                    x for x in _add2 if x not in _p1_dropped)
                                # ② gaps_still_open 由**配置维度**减已答集推导
                                #    （原版只认 configured dimensions，判官自造
                                #    id 不进 open 账）。
                                _lock_all = [str(d.get("id") or "")
                                             for d in _spec_dims
                                             if str(d.get("id") or "")]
                                _lock_still = [x for x in _lock_all
                                               if x not in _p1_addressed]
                                # ③ passed 由 open 是否为空**本地重算**，覆盖
                                #    模型的 passed（原版 spec/route.ts：passed 是
                                #    gaps_still_open.length === 0 的纯函数）。
                                _p1_passed = not _lock_still
                                # 摘掉的维度仍算"还开着"，只是不再问 —— 不能
                                # 因为 stuck 摘掉就宣布闭合。
                                _p1_open = [x for x in _lock_still
                                            if x not in _p1_dropped]
                                _p1_qs = list(_qs2)
                                # 追问已送到、行为却没变 → 摘掉，如实记未闭合
                                _apply_stuck_guard()
                                _have = {str(d.get("id") or "")
                                         for d in learned_dims}
                                # ⚠️ 不把 gate 的 addressed 直接并进 learned_dims：
                                # MVE 的"闭合"= 落一枚技能节点（有代码、有工具）；判据
                                # 说答到、但本轮 rollout 没成功 = 只有文本、没有配方。
                                # 两本账分开（判据看 out["spec_gate"]，账看
                                # dimension_results），免得账面闭合、技能库却是空的。
                                _only_gate = [x for x in _add2 if x not in _have]
                                # 追问归到未闭合维度（原版 questions 与 gaps_still_open
                                # 是两个平行列表，不强行一一配对）。
                                for _oid in _open2:
                                    # 外层只把**追问**递下去（下一轮 voyager 的
                                    # 第一句输入），不改写它的 passed/gap ——
                                    # 判定权在 maieutic（out["spec_gate"]），
                                    # 账权在 voyager 自己的 rollout。
                                    if not _qs2:
                                        continue
                                    # ⚠️ 这里**不覆盖** asked / asked_q：
                                    #   _iters[_oid]["asked"] 是这一维本轮自己的
                                    #   判词（2414 处写的），覆盖成整轮 questions
                                    #   会让下一轮拿别维的问题问它（串味）。
                                    #   本轮整轮的问句另存 out["spec_gate"]。
                                    _rec2 = dict(_iters.get(_oid) or {})
                                    _rec2.setdefault("asked_q", [])
                                    _iters[_oid] = _rec2
                                    _iters_round[_oid] = _rec2
                                out["spec_gate"] = {
                                    # 累计（= 原版 instructorConfiguredDimensions
                                    # Addressed），另存本轮新增供对账。
                                    "addressed": sorted(_p1_addressed),
                                    "addressed_this_round": list(_add2),
                                    "open": list(_p1_open),
                                    "questions": _qs2,
                                    "emergent": list(_gate_all.get("emergent") or []),
                                    "passed": bool(_p1_passed),
                                    "judge_passed": bool(_gate_all.get("passed")),
                                    "judge_open": list(_open2),
                                    "only_gate": _only_gate,
                                }
                                try:
                                    _ms.append_spec_round(scene, question, {
                                        # 段号 + 段内轮号：原版 Phase1Iteration 天然
                                        # 挂在一条会话上，MVE 靠这两个字段补回边界。
                                        "run": _p1_run,
                                        "round": _p1_round,
                                        "spec_text": _spec,
                                        "questions": _qs2,
                                        # 原版 priorIterations 存「那轮结束时
                                        # 还开着什么」—— 用累计账，别用本轮
                                        # 判官的原始 open（会回落）。
                                        "gaps_identified": list(_p1_open),
                                        "gaps_addressed": _add2,
                                        "emergent": list(
                                            _gate_all.get("emergent") or []),
                                        "passed": bool(_p1_passed),
                                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                    })
                                except Exception:
                                    pass
                                print("  · 整篇提交判定：累计答到 "
                                      + str(len(_p1_addressed)) + "/"
                                      + str(len(_spec_dims))
                                      + "（本轮新增 " + str(len(_add2)) + "）"
                                      + "｜仍开 " + str(len(_p1_open))
                                      + (("｜追问 " + str(len(_qs2)) + " 条")
                                         if _qs2 else "")
                                      + (("｜清单外发现 "
                                          + str(len(_gate_all.get("emergent") or []))
                                          + " 条（不阻断）")
                                         if (_gate_all.get("emergent") or []) else ""),
                                      flush=True)
                            elif _gate_all:
                                # ★ 判官不可用 ≠ 已闭合。maieutic 是"何时结束"
                                #   的唯一裁判（用户 2026-11-11：maieutic 控外层、
                                #   voyager 只管回答追问）；它判不了就不能宣布结束
                                #   —— 退回 voyager 自己的逐维账当 open。
                                if str(_gate_all.get("origin")) == "no_submit":
                                    _no_submit_streak += 1
                                else:
                                    _no_submit_streak = 0
                                if _no_submit_streak >= 3:
                                    # 连续 3 轮判官给不出判词（事件流空或一字
                                    # 未改）—— 判卷回路未建立，如实收工，不再
                                    # 空转烧 token。
                                    print("  · 连续 3 轮判官给不出判词"
                                          "（代交未获判词）—— 如实收工",
                                          flush=True)
                                    try:
                                        _ms.append_session_event(
                                            scene, question, "dim_stuck", {
                                                "round": _p1_round,
                                                "dim_id": "spec_submit",
                                                "stuck_rounds":
                                                    _no_submit_streak,
                                                "attempts": _p1_round,
                                                "why": "连续 3 轮判官给不出"
                                                       "判词（代交未获判词），"
                                                       "判卷回路未建立",
                                            })
                                    except Exception:
                                        pass
                                    break
                                _p1_judged = False
                                _p1_passed = False
                                _p1_qs = []
                                # 判官挂了不撤销已答到的维度（原版 addressed 的
                                # 语义是"已证明过的承诺"，不因一次判定失败作废）。
                                _lock_all2 = [str(d.get("id") or "")
                                              for d in _spec_dims
                                              if str(d.get("id") or "")]
                                _lock_still2 = [x for x in _lock_all2
                                                if x not in _p1_addressed]
                                _p1_open = [x for x in _lock_still2
                                            if x not in _p1_dropped]
                                # 判官不可用也要判「有没有进展」：同一动作同一结果
                                # 重复，等判官回来也不会有不同结局。摘掉、如实记。
                                _apply_stuck_guard()
                                out["spec_gate"] = {
                                    "addressed": sorted(_p1_addressed),
                                    "open": list(_p1_open),
                                    "questions": [], "passed": False,
                                    "judged": False,
                                    "reason": str(_gate_all.get("reason") or "")[:120],
                                }
                                print("  · 整篇提交判定不可用（"
                                      + str(_gate_all.get("reason") or "")[:60]
                                      + "）—— 不判闭合，退回逐维账：仍开 "
                                      + str(len(_p1_open)) + " 维", flush=True)
                        # ---- 判官这一轮的结论也进会话日志（增量）----
                        # 原版 priorIterations 落的是「上一轮你问了什么、当时还开着
                        # 什么」（spec-examiner.ts:227-234）。这里同构：判官每轮追加
                        # 一条，下一轮它读整篇时就看得到自己上轮判过什么、问过什么，
                        # 不必靠 prior_iterations 再传一遍（那条通路在判官自身解析
                        # 失败时会整段丢失 —— 2026-11-11 实测第 2、3 轮带着 0 条
                        # 追问原地重跑，就是它丢了）。
                        try:
                            _sg_ev = out.get("spec_gate") or {}
                            _ms.append_session_event(
                                scene, question, "gate_verdict", {
                                    "round": _p1_round,
                                    "judged": bool(_sg_ev.get("judged", True)),
                                    "passed": bool(_p1_passed),
                                    "addressed": [str(x) for x in
                                                  (_sg_ev.get("addressed") or [])],
                                    "open": [str(x) for x in
                                             (_sg_ev.get("open") or [])],
                                    "questions": [str(x)[:200] for x in
                                                  (_sg_ev.get("questions") or [])][:3],
                                    "reason": str(_sg_ev.get("reason") or "")[:200],
                                })
                        except Exception:
                            pass
                        # ---- phase1 轮末：闭合就收工，没闭合就带追问再答 ----
                        # 追问已在 _iters 里写了 asked/asked_q（上面的落账段），
                        # 下一轮该维度第一次进 solve 时 crit1 会自动带上它
                        # （maieutic priorIterations 同款，spec-examiner.ts:224-235）。
                        out["phase1"] = {"rounds": _p1_round,
                                         "run": _p1_run,
                                         "open": list(_p1_open),
                                         "judged": bool(_p1_judged),
                                         "passed": bool(_p1_passed)}
                        # 缺口序列的观测（同构助产士的进步账本）：只摊平已有
                        # 轨迹 —— 本次重跑用了几轮、第几轮首次闭合、相对上次
                        # 快了多少、还卡在哪几维。不产生分数，不判对错。
                        try:
                            out["phase1"]["progress"] = _ms.save_spec_progress(
                                scene, question)
                        except Exception:
                            pass
                        if _whole:
                            # 整篇模式：维度账由判官给（骨架只在它那一侧）。
                            _sg_w = out.get("spec_gate") or {}
                            _add_w = [str(x) for x in (_sg_w.get("addressed") or [])]
                            _opn_w = [str(x) for x in (_sg_w.get("open") or [])]
                            _it_w = set(_add_w)
                            learned_dims = [d for d in _spec_dims
                                            if str(d.get("id") or "") in _it_w]
                            failed_dims = [d for d in _spec_dims
                                           if str(d.get("id") or "") in set(_opn_w)]
                            closed_ids.update(_it_w)
                            if str(_last_spec or "").strip() and _round_code:
                                # Phase2 的 finalCode 来源：整题程序一份（原版
                                # Phase2 交一份 finalCode，不是逐维拼装）。
                                dim_codes = [{
                                    "id": str(_tid or "milestone"),
                                    "question": str(question),
                                    "code": str(_round_code),
                                    "answer": str(_last_spec)[:1200],
                                    "tools": [], "how": "spec",
                                    "r": 1.0 if _p1_passed else 0.6,
                                }]
                            for _d in _spec_dims:
                                _dk = str(_d.get("id") or "")
                                if not _dk:
                                    continue
                                _r0 = dict(_iters.get(_dk) or {})
                                _r0["asked_q"] = [str(x)[:200]
                                                  for x in (_p1_qs or [])][:3]
                                _r0["asked"] = "；".join(
                                    str(x)[:80] for x in (_p1_qs or []))[:300]
                                _r0["passed"] = _dk in _it_w
                                _r0["gap"] = "" if _r0["passed"] else _dk
                                _r0["rounds"] = int(_r0.get("rounds") or 0) + 1
                                _iters[_dk] = _r0
                                _iters_round[_dk] = _r0
                            # 打转守卫（2026-11-13 修正口径）：三种状态分开 ——
                            #   ① 两轮都空 = 学生根本没交出整篇（RESULT 里没有
                            #     作答文本），这不是「打转」，修法是交付收集，
                            #     不是改追问；日志原把空==空误报成「逐字相同」，
                            #     把排查引向追问有效性（实测 req_...b36b 两轮
                            #     RESULT 全空被误判打转）。
                            #   ② 非空且逐字相同 = 拿到追问也没改一个字（真打转）。
                            #   ③ 其余 = 有进展，继续轮。
                            _fp_w = str(hash(str(_last_spec)))
                            _spec_empty = not str(_last_spec or "").strip()
                            if _spec_empty and _spec_prev_empty:
                                print("  · 两轮都没交出整篇（RESULT 里没有作答"
                                      "文本）—— 不是打转，是没交；如实记未闭合",
                                      flush=True)
                                out.setdefault("stuck", []).append(
                                    {"id": "no_delivery"})
                                break
                            if (_spec_fp_prev and _fp_w == _spec_fp_prev
                                    and not _spec_empty):
                                print("  · 原地打转：整篇与上一轮逐字相同 —— "
                                      "助产士已问到，机器没进展，如实记未闭合",
                                      flush=True)
                                out.setdefault("stuck", []).append({"id": "whole"})
                                break
                            _spec_prev_empty = _spec_empty
                            _spec_fp_prev = _fp_w
                        if _p1_passed:
                            print("  ✓ Phase1 闭合：gaps_still_open 为空"
                                  + (f"（第 {_p1_round} 轮）"
                                     if _p1_round > 1 else "")
                                  + " —— 本轮结束，等下次派任务", flush=True)
                            break
                        if _stuck_now and not _p1_open:
                            print("  · Phase1 停在原地：追问已到位、机器连续交同一条"
                                  " 作答 —— 不宣布闭合，如实记未闭合（交人看）",
                                  flush=True)
                            break
                        if not _p1_open:
                            # 判官没给 open 却也没说 passed（异常态）→ 不硬转，
                            # 也绝不宣布闭合（闭合只有一个依据：open 为空且判过）。
                            print("  · Phase1 本轮没有结论（判官未给出 open 清单）"
                                  " —— 不宣布闭合，等下次派任务", flush=True)
                            break
                        # 无进展兜底（2026-11-13）：判官问题一旦指向"只能执行才
                        # 知道的事实"，学生每轮只能答"我还差一次执行"（文字在变，
                        # 所以逐字打转检测抓不到）→ 无限循环。判据：连续
                        # `_P1_STALL` 轮 open 数量一点没降，就如实收，交人看
                        # 维度本身要不要改（不宣布闭合）。
                        #
                        # ★ 2026-11-15 修绕过漏洞：原来判据是「open **数量**
                        #   不降」（`>=` 加一 / `<` 归零）—— open 数在 N 与 N-1
                        #   之间摆动时（补一维掉一维）计数会被清零 → 守卫可被
                        #   无限绕过，Phase1 在 `MVE_PHASE1_ROUNDS=0`（默认不限）
                        #   下可能真的无限循环。
                        #   现在改成：只要**还是那些同样的缺口开着**就算没进展
                        #   （按 open **集合**比，不按数量）—— 集合变了才算有
                        #   进展（哪怕数量没降）。
                        _p1_open_set = frozenset(str(x) for x in _p1_open)
                        if _p1_open_set == _p1_prev_open_set:
                            _p1_stall = _p1_stall + 1
                        else:
                            _p1_stall = 0
                        _p1_prev_open_set = _p1_open_set
                        if _p1_stall >= _P1_STALL:
                            print(f"  · Phase1 连续 {_p1_stall} 轮缺口没减少"
                                  f"（仍开 {len(_p1_open)} 维："
                                  f"{', '.join(_p1_open[:6])}）—— 追问推不动，"
                                  "如实记未闭合（问题多半在维度口径本身，"
                                  "不是学生没答）", flush=True)
                            break
                        # 只有人工显式设了 MVE_PHASE1_ROUNDS 才按上限收；
                        # 默认（0）= 跟着 gaps_still_open 走：非空就继续追问、
                        # 继续在同一份整篇上作答（原版 maieutic 的 Phase1 就是这个
                        # 语义，没有轮数概念）。
                        if _P1_MAX > 0 and _p1_round >= _P1_MAX:
                            print(f"  · Phase1 达人工上限 {_P1_MAX}，仍开着 "
                                  f"{len(_p1_open)} 维："
                                  f"{', '.join(_p1_open[:6])}"
                                  " —— 如实记未闭合（MVE_PHASE1_ROUNDS 设的）",
                                  flush=True)
                            break
                    try:
                        local_agent.clear_submit_handler(scene)
                    except Exception:
                        pass
                    # ---- 跨轮去重（第二起重跑同一维会重复 append）----
                    if _p1_round > 1:
                        _seen_ids: set[str] = set()
                        _ld = []
                        for _d in learned_dims:
                            _k = str(_d.get("id") or "")
                            if _k in _seen_ids:
                                continue
                            _seen_ids.add(_k)
                            _ld.append(_d)
                        learned_dims = _ld
                        _seen_ids = set()
                        _dc: list[dict] = []
                        for _c in dim_codes:
                            _k = str(_c.get("id") or "")
                            if _k in _seen_ids:
                                continue
                            _seen_ids.add(_k)
                            _dc.append(_c)
                        dim_codes = _dc
                    try:
                        _ms.save_progress(scene, question,
                                          [str(d.get("id") or "")
                                           for d in learned_dims])
                        _ms.save_progress_codes(scene, question, dim_codes)
                        _ms.save_progress_iterations(scene, question,
                                                     _iters_round)
                    except Exception:
                        pass
                    # 用户 2026-11-12 口径：**不再记「闭合几维」**。
                    # 维度条数是动态的（同一句话换个问法，骨架拆出来的条数就变），
                    # maieutic 自己也不按维度记账 —— 一次会话只有两个可预见的
                    # 闭合点：Phase1 冻结 spec、Phase2 提交 finalCode。所以这里
                    # 只留这两个布尔，供下游/面板使用；learned_dims 仍留在内存里
                    # 供本轮追问与技能生成，但**不进账、不上图、不进面板计数**。
                    out["spec_outcome"] = {
                        "phase1_passed": bool((out.get("spec_gate") or {})
                                              .get("passed")),
                        # ⚠️ 这里**不写** phase2_* 字段（2026-11-12 同构修正）：
                        # 原版没有"快照"——每个阶段的数据由**完成该阶段的动作**
                        # 自己写入（submit_route.ts 先 finalizePhase2Code 再
                        # advancePhase；sessions.ts:225-238 currentPhase 在转移
                        # 当下才推进）。Phase1 闭合时 Phase2 还没跑，在这里预写
                        # phase2_submitted/verified=False，Phase2 后来过关了
                        # 账本里仍是 False（实测 learn_recent 两处对不上）。
                        # phase2 字段改由 Phase2 真跑完后合并（见 ~4490）。
                    }
                    # ---- 里程碑判定（骨架一次生成，这里只判闭合）----
                    try:
                        # ⚠️ 第三个参数要传**累计账**（_prog 存量 ∪ 本轮新闭合），
                        # 不能只传 `learned_dims`：预设维度路径下 `_dims` 只有
                        # 「本轮那一条」（:931 `_dims = [dict(preset_dimension)]`），
                        # 于是 `learned_dims = _dims ∩ _prog` 最多一条 → milestone_state
                        # 每次都只看得到本轮那一条，上一轮闭合的从 closed 里掉出去。
                        # 生产实测（Scene H，2026-10-29）：project_overview 那轮报
                        # 「1/6 闭合」，紧接着 technical_architecture 失败那轮就回落
                        # 成「0/6 闭合，还差 project_overview」—— 数字来回跳、achieved
                        # 永远翻不了，人看到的就是「学了也不涨」。
                        _learned_now = (set(str(x) for x in _prog)
                                        | set(str(x) for x in closed_ids)
                                        | {str(d.get("id") or "") for d in learned_dims})
                        _mst = _ms.milestone_state(
                            scene, question,
                            sorted(x for x in _learned_now if x),
                            # 用**完整**维度集算总进度：预设维度单发闭合时，
                            # 其它维度仍是未闭合，achieved 才不会误翻。
                            # 里程碑重试轮传 None → milestone_state 读**骨架
                            # 缓存**（plan 是重拆的，维度 id 可能与骨架漂移；
                            # 存量闭合账 _prog 记的是骨架维度 id，两者必须
                            # 同一把尺子，否则 achieved 永远判不出）。
                            scaffold_result=(None if milestone_retry else
                                             {"dimensions": (plan.get("dimensions")
                                                             or []),
                                              "milestone": plan.get("milestone") or {}}))
                        out["milestone_scaffold"] = _mst
                        # 达成了 → 这一轮跑通的代码进技能库（同原版 add_new_skill）
                        # 没全闭合也照样把**已闭合**那几条入库 —— 它们各自是一次
                        # 成功 rollout，不该因为别的维度没闭合就被连坐丢掉。
                        # 整篇模式下 Phase1 交付的是**文字**（原版 spec），
                        # 不是逐维 rollout —— 拿它当维度技能存，就是把 spec
                        # 固化成了"能力"（正是 2026-10-19 删过的那种伪配方）。
                        # 技能只由 Phase2 的 finalCode 沉淀。
                        out["skills"] = (
                            {"added": [], "skipped": [],
                             "milestone": str((_mst or {}).get("name") or ""),
                             "achieved": bool((_mst or {}).get("achieved"))}
                            if _whole else
                            learn_milestone_skills(
                                scene, _tid, question, _mst, dim_codes,
                                import_id=imp_id, req_id=_req_id))
                        # 里程碑**全闭合** → 把里程碑本身组合成一条复合技能
                        # 入库（用户 2026-10-25：「把里程碑在学习侧重试之后
                        # 计入技能，不计的话消费检索不到」）。原版 learn()
                        # 第二步：原子技能组合成复合 program 按里程碑索引入库。
                        # 没全闭合不组合（组合了也是漏维的片面 SQL）。
                        #
                        # ★ Phase2 的门是**spec 冻结**，不是 achieved
                        #   （2026-11-14 用户口径：「phase2 不写代码、不积累技能，
                        #   之后遇到问题怎么复用」）。原版 submit_route.ts:44-58
                        #   的判据就是「Phase1 冻结了 spec 吗」——finalCode 是
                        #   交给冻结 spec 的实现，不是"全维闭合的奖状"。旧写法
                        #   以 achieved 为门，实测 P 场景跑了整单 phase2 仍是
                        #   None：一维没过 → 连代码都不写 → 技能库零条 → 下次
                        #   同类题从零开始（1→n 泛化彻底断掉）。
                        #   现在：spec 冻结就交码；过没过关只决定进**主库**
                        #   还是进**草稿**（草稿回注同题，下次在这份上改）。
                        _sg_frozen = bool(
                            (out.get("spec_gate") or {}).get("passed")
                            and str(_spec or "").strip())
                        # Phase2 的对账结果给 Phase3 复用（同一次 diff，不重复烧
                        # 一次分类器）；没进 Phase2 时这就是空表。
                        _p2_divs: list[dict[str, Any]] = []
                        _p2_diff_known = False
                        _p2_open = bool(_sg_frozen
                                        or (_mst or {}).get("achieved"))
                        if _whole and _p2_open:
                            # ══ Phase2（A 原版）：照冻结的 spec 交**一份 finalCode** ══
                            # submit_route.ts:44-58 —— Phase1 冻结后才允许交码，
                            # 交上来的唯一字段是 finalCode；sessions.ts:374-389
                            # 把它写进 finalCode/currentCode/submittedAt。
                            # 因此这里**不能**复用 Phase1 的文字作答（它是
                            # `RESULT={"answer":"…"}`，那是 spec 不是实现）。
                            try:
                                _ms.note_phase(
                                    scene, question, 2,
                                    reason="spec 已冻结，照它写 finalCode")
                            except Exception:
                                pass
                            try:
                                # 交码时手上只有 Phase1 的 **finalText**（学生自己
                                # 针对本题各维度写下的说明），不是整篇会话流水
                                # —— 那份里混着上一题的账和本轮执行日志，判官会
                                # 拿别的题的要求来要求这一轮的代码（2026-11-14
                                # 实测：critic 说「RESULT 里残留上一题的
                                # cube_root_definition」）。
                                _ft = _final_text_of(_spec_dims, _ledger,
                                                     str(_spec or ""))
                                _fc = await phase2_final_code(
                                    scene, question, _ft,
                                    _spec_dims,
                                    # ⚠️ 必须与 Phase1 同一个库：旧写法只传
                                    # `prof["db_path"]`（场景配置），场景 N 的配置
                                    # 里没有这一项 → 空串 → Phase2 的观察面为空，
                                    # 学生与 critic 都在"没有表清单"的情况下判：
                                    # 实测 critic 断言「数据源里不存在 math_toc」，
                                    # 而那个库明明有（scene_data/N/math.duckdb）。
                                    db_path=str(exec_db or db_path or ""),
                                    scene_dir=str(local_dir or exec_dir or ""),
                                    llm=llm, topic_id=str(_tid or ""),
                                    # 重做这一遍：不带上一遍的底稿（同遍内仍然
                                    # 逐轮改自己那份，那是"改代码"，不是"抄成品"）
                                    no_draft=bool(fresh))
                                # ══ 过关判据（照助产士原版 submit 路由）════
                                # 原版：Phase1 冻结 finalText → 学生交 finalCode →
                                # intent-diff 把 finalText 与 finalCode **逐条对账**，
                                # 分歧 0 就是通过，有分歧才进 Phase3 逐条解释
                                # （`submit/route.ts:64`；整体完成与否也是确定性
                                # 判 `gaps_still_open` 为空，`spec/route.ts:85`）。
                                # 没有"取到数才算过关"这道闸 —— 那是 voyager 的
                                # critic，不是助产士的判据。用错判据的后果实测：
                                # 推导型题的 code 永远 rows=0 → 永远不过关 →
                                # 技能库零条 → 下次同类题从零开始，1→n 断掉
                                # （2026-11-14 用户：「code 过不过关应该根据这个
                                # finalText 来」）。
                                _p2_diff_ok = False
                                _p2_diff_known = False
                                _p2_crit = ""
                                if str(_fc.get("code") or "").strip():
                                    try:
                                        # 原版 `submit/route.ts:64` 的那一次
                                        # 分类：输入只有学生的 finalText 与
                                        # finalCode（外加这次真跑出来的结果），
                                        # 输出 divergences；**0 条 = 通过**。
                                        # 没有"取到数 / 有 RESULT 值"这类自造
                                        # 闸门 —— 那会把推导型任务永远挡在门外。
                                        _idf = await asyncio.wait_for(
                                            intent_diff(
                                                scene, question,
                                                _final_text_of(
                                                    _spec_dims, _ledger,
                                                    str(_spec or "")),
                                                str(_fc.get("code") or ""),
                                                max_items=5,
                                                run_result=(_fc.get("result")
                                                            or {})),
                                            timeout=float(os.environ.get(
                                                "MVE_P2_DIFF_TIMEOUT") or 240.0))
                                        if str(_idf.get("origin") or "") == "llm":
                                            _p2_diff_known = True
                                            _p2_divs = [
                                                d for d in
                                                (_idf.get("divergences") or [])
                                                if isinstance(d, dict)]
                                            _p2_diff_ok = not _p2_divs
                                            # 学生面只给中性追问 ——
                                            # classification 不进（原版
                                            # `intent-diff.ts:16-18`）。
                                            _p2_crit = "\n".join(
                                                "· " + str(d.get(
                                                    "student_facing_question")
                                                    or "")[:300]
                                                for d in _p2_divs)
                                        print("  · Phase2 intent-diff："
                                              + (f"{len(_p2_divs)} 条分歧"
                                                 if _p2_diff_known
                                                 else "分类器不可用")
                                              + f" → {'过关' if _p2_diff_ok else '未过'}",
                                              flush=True)
                                    except Exception as _ie:
                                        _p2_crit = ""
                                        print("  · Phase2 intent-diff 失败"
                                              "（不据此判死）："
                                              f"{type(_ie).__name__}: {_ie}",
                                              flush=True)
                                # 分类器不可用时退回 critic 的判定 —— 不能因为判官
                                # 抖动把一份能用的代码判成草稿（也不能凭空放过）。
                                _p2_verdict = (_p2_diff_ok if _p2_diff_known
                                               else bool(_fc.get("verified")))
                                out["phase2"] = {
                                    "final_code": len(str(_fc.get("code") or "")),
                                    "added": "", "skipped": "",
                                    # verified = **code 与 finalText 无分歧**
                                    # （原版 submit 路由），不是"取到数了"。
                                    "verified": bool(_p2_verdict),
                                    # submitted（原版 `submittedAt` 非空）= 交上
                                    # 一份真跑得通的程序，**与"过关"无关**：原版
                                    # 学生随时能点提交，没过关的提交才走 Phase3
                                    # 逐条解释。
                                    "submitted": bool(
                                        _p2_verdict
                                        or str(_fc.get("code") or "").strip()),
                                    "rounds": int(_fc.get("rounds") or 0),
                                    # 与 finalText 对不上的条数（0 = 全兑现）
                                    "divergences": len(_p2_divs),
                                    "diff_known": bool(_p2_diff_known),
                                    # 对不上的具体条目（逐条），Phase3 直接复用
                                    "critique": str(_p2_crit or "")[:1200],
                                    "open_items": [str(d.get("divergence_id") or "")
                                                   for d in (_p2_divs or [])],
                                    # 循环内的评论来自 critic（voyager 的
                                    # rollout critic）；它只决定"下一轮怎么改"，
                                    # 不再决定过不过关。
                                    "critic": dict(_fc.get("critic") or {}),
                                    "smoke": dict(_fc.get("smoke") or {}),
                                    # 这一遍的"起点"（面板据此标基线/非基线）：
                                    # skills = 本次检索到并注入的配方名；
                                    # draft_from = 从哪份上一版代码起步（空 = 从零写）
                                    "skills": list(_fc.get("skills") or []),
                                    "draft_from": str(_fc.get("draft_from") or ""),
                                    "why": str(_fc.get("why") or "")[:300],
                                }
                                # 主库 vs 草稿的分界：**与 finalText 对齐**就进主库
                                # （可被别的题复用），有分歧先进草稿（同题回注，
                                # 等 Phase3 逐条解释完修订后升级）。
                                # 里程碑是否全闭合只影响进度账，不再当入库门 ——
                                # 那正是"学了整单、技能库零条"的原因。
                                _p2_ok = bool(_p2_verdict)
                                if _p2_ok:
                                    _act2, _key2 = save_final_code_skill(
                                        scene, _tid, question,
                                        str(_fc.get("code") or ""),
                                        milestone_name=str(
                                            ((_mst or {}).get("milestone")
                                             or {}).get("name") or ""),
                                        dims=_spec_dims, req_id=_req_id)
                                    out["phase2"].update(
                                        added=str(_key2 or ""),
                                        action=str(_act2 or ""))
                                    # Phase3 的 actual 面：finalCode 真跑出来的
                                    # RESULT（intent-diff 的"做成面"）
                                    out["milestone_skill"] = {
                                        "added": str(_key2 or ""),
                                        "session_code": str(_fc.get("code") or ""),
                                        "verified_result": (_fc.get("result")
                                                            or {}),
                                    }
                                    out["_p2_rows"] = int(_fc.get("rows") or 0)
                                    print(f"  · Phase2 交出 finalCode："
                                          f"{len(str(_fc.get('code') or ''))} 字、"
                                          f"取回 {int(_fc.get('rows') or 0)} 行 → "
                                          f"{_key2}", flush=True)
                                else:
                                    out["phase2"]["skipped"] = (
                                        str(_p2_crit or "")[:600]
                                        or str(_fc.get("why") or "")
                                        or ("finalCode 没有兑现 finalText 的每一条"
                                            " → 先存草稿（只回注同题，"
                                            "下次在这份上改）"))
                                    # 不过关也把最后那份跑得通的程序带下去：
                                    # Phase3 的 code 面来自这里，没有它就只能
                                    # 打"没有可比的分歧"（原版提交不受过关限制）。
                                    out["milestone_skill"] = {
                                        "skipped": str(_fc.get("why") or ""),
                                        "session_code": str(
                                            _fc.get("code") or ""),
                                        "verified_result": (_fc.get("result")
                                                            or {}),
                                    }
                                    if str(_fc.get("code") or "").strip():
                                        out["_p2_rows"] = int(
                                            _fc.get("rows") or 0)
                                    # ★ 草稿入库（2026-11-13 用户验收口径）：
                                    # 「已经有一个技能是这道题的，而且那个技能
                                    # 不过关，只能通过 Phase3 的分歧去改」——
                                    # 旧实现不过关就什么都不写，Phase3 改完也只
                                    # 落在 ckpt 里，技能库永远没有这道题的版本，
                                    # 下一轮又从零写。现在把这份**跑得通的码**
                                    # 存成草稿（verified=False）：只回注同题，
                                    # Phase3 revision 真跑通后原地升级。
                                    _dk = ""
                                    # 只有"跑通且真交了东西（RESULT 非空）"的码才配当
                                    # 草稿：探查脚本存进去也调不动（没 RESULT），
                                    # 下一轮拿它当起点只会继续跑偏（2026-11-13 实测：
                                    # 存进去的草稿就是一段 probe_result 脚本）。
                                    if (str(_fc.get("code") or "").strip()
                                            and _fc.get("result")):
                                        try:
                                            _a3, _dk = save_final_code_skill(
                                                scene, _tid, question,
                                                str(_fc.get("code") or ""),
                                                milestone_name=str(
                                                    ((_mst or {}).get("milestone")
                                                     or {}).get("name") or ""),
                                                dims=_spec_dims,
                                                req_id=_req_id,
                                                verified=False,
                                                from_phase=(
                                                    "maieutic_phase2_draft"))
                                            out["phase2"].update(
                                                draft=str(_dk or ""),
                                                draft_action=str(_a3 or ""))
                                            print("  · Phase2 草稿入库（未通过"
                                                  "验证，只回注同题）："
                                                  + str(_dk or ""), flush=True)
                                        except Exception as _de:
                                            out["phase2"]["draft_error"] = (
                                                f"{type(_de).__name__}: {_de}")
                                    print("  · Phase2 没过关（已按现状提交）："
                                          + str(_fc.get("why") or "")[:140],
                                          flush=True)
                            except Exception as _fe:
                                out["phase2"] = {
                                    "final_code": 0, "added": "",
                                    "skipped": f"{type(_fe).__name__}: {_fe}",
                                    "verified": False}
                                print("  · Phase2 失败："
                                      f"{type(_fe).__name__}: {_fe}", flush=True)
                        elif bool((_mst or {}).get("achieved")) and dim_codes:
                            # ★ Phase2 = 交**一份** finalCode（A 原版）：
                            #   原版 Phase2 的产物就是一份 finalCode
                            #   （schemas.ts Phase2Data.finalCode），它对
                            #   Phase1 冻结的整篇 spec 负责。MVE 里这一步 =
                            #   各维取证配方内联成一条自包含程序并**真跑通**
                            #   —— 跑通 = 交付；跑不通 = 没交（如实记 skipped，
                            #   不拿别的东西顶替）。
                            try:
                                _ms.note_phase(
                                    scene, question, 2,
                                    reason="spec 已冻结，开始写 finalCode")
                            except Exception:
                                pass
                            try:
                                # 会话程序只调**真入库**的维度（库里没有的
                                # 维度调了必然 NameError → 整条会话崩）。
                                _in_dims = [str(x.get("dim") or "")
                                            for x in ((out.get("skills") or {})
                                                      .get("added") or [])]
                                out["milestone_skill"] = (
                                    await learn_milestone_composite(
                                        scene, _tid, question, dim_codes,
                                        db_path=str(prof.get("db_path") or ""),
                                        milestone_name=str(
                                            ((_mst or {}).get("milestone")
                                             or {}).get("name") or ""),
                                        scene_dir=str(local_dir or exec_dir or ""),
                                        in_dims=_in_dims,
                                        req_id=_req_id))
                                _mcomp = out.get("milestone_skill") or {}
                                out["phase2"] = {
                                    "final_code": len(str(
                                        _mcomp.get("session_code") or "")),
                                    "added": str(_mcomp.get("added") or ""),
                                    "skipped": str(_mcomp.get("skipped") or ""),
                                    "verified": bool(_mcomp.get("verified_result")),
                                }
                                if _mcomp.get("added"):
                                    print(f"  · 里程碑复合技能已入库："
                                          f"{_mcomp['added']}", flush=True)
                                elif _mcomp.get("skipped"):
                                    print(f"  · 里程碑复合技能未入库："
                                          f"{str(_mcomp['skipped'])[:110]}",
                                          flush=True)
                            except Exception as _ce:
                                out["milestone_skill"] = {
                                    "skipped": f"{type(_ce).__name__}: {_ce}"}
                    except Exception as e:
                        out["milestone_scaffold"] = {
                            "error": f"{type(e).__name__}: {e}"}
                    # ══════ Phase 3：intent-diff 复盘（同构 maieutic Phase 3）══════
                    # 原版顺序（`sessions.ts:391-482`）：Phase1 冻结 spec →
                    # Phase2 交 finalCode → 分类器做 **spec 全文 vs finalCode 全文**
                    # 的 intent-diff（drift/revision/bug）→ 学生对每条中性问题作答
                    # → 判 alignment（aligned/partial/diverged）→ 全答完后
                    # revision pass（skipped|revised，不动 finalCode 与分类）。
                    # MVE 的 spec 面 = Phase1 里**学生自己说的话**（整篇里判官的判词
                    # 已剔除），code 面 = Phase2 的 finalCode 全文；作答由 voyager
                    # 回到材料上核一次（用户 2026-11-11 拍板）。
                    # 没有 finalCode 就没有"做成面"，Phase3 无从对起（原版 submit
                    # 也只接受 phase==2 的提交）。注意判据是 **submitted** 不是
                    # verified：没过关的提交正是 Phase3 存在的理由（逐条解释），
                    # 拿 verified 当门槛会把"没做到"整段跳过，一句解释都没有。
                    # ════ Phase2 字段补写（同构原版"完成即写"）════
                    # Phase1 闭合的 spec_outcome 不含 phase2_*；Phase2 真跑完
                    # （或没跑）的**此刻**才是写这些字段的位置。Phase2 没发生
                    # （里程碑没达成）→ submitted=False 也是此刻的真话。
                    if out.get("spec_outcome") is not None:
                        _p2o = out.get("phase2") or {}
                        out["spec_outcome"].update(
                            phase2_submitted=bool(_p2o.get("submitted")),
                            phase2_verified=bool(_p2o.get("verified")),
                            phase2_rounds=int(_p2o.get("rounds") or 0))
                    _p2_done = bool((out.get("phase2") or {}).get("submitted"))
                    _p2_code = str((out.get("milestone_skill") or {})
                                   .get("session_code") or "")
                    # ════ Phase2 的账落盘 ════
                    # ★ 2026-11-14（用户：「开始修 phase1 到之后的 phase 的帐」）：
                    #   原版一条会话记录里 phase1 / phase2 / phase3 是同一份账
                    #   （`src_lib_opus_schemas.ts` Phase2Data.finalCode/submittedAt）；
                    #   MVE 的 Phase2 之前只活在内存 `out["phase2"]` 里 ——
                    #   run_log / coach_log 都不收，进程一重启就永久缺失，面板上
                    #   一道题只剩「问了什么」和「分歧几条」，中间「交没交码、
                    #   跑通没跑通」查无此项。这里不分成功/跳过都记一笔：
                    #   submitted=False 也是这件事此刻的真话（原版同口径）。
                    #   `final_code` 有的分支写的是**长度**（里程碑复合技能那条），
                    #   真码在 milestone_skill.session_code 里 —— 一并带上。
                    if isinstance(out.get("phase2"), dict):
                        try:
                            _p2rec = dict(out["phase2"])
                            _fc_txt = str(_p2rec.get("final_code") or "")
                            if _fc_txt.isdigit():
                                _fc_txt = _p2_code
                            _p2rec.update(
                                final_code=_fc_txt,
                                submitted_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                                run=int(_p1_run or 0),
                                # 这一遍的起点：走 `phase2_final_code` 的分支自带这份账；
                                # 走里程碑复合技能那条没有 → 退回实际注入执行域的那批。
                                skills=(list(_p2rec.get("skills") or [])
                                        or [str((s or {}).get("name") or "")
                                            for s in (_py_skills or [])][:8]),
                                draft_from=str(_p2rec.get("draft_from") or ""))
                            _ms.save_phase2(scene, question, _p2rec)
                        except Exception:
                            pass
                    if (_whole and bool(_p1_passed) and str(_spec or "").strip()
                            and _p2_done and _p2_code.strip()):
                        try:
                            _ms.save_spec_text(
                                scene, question,
                                _final_text_of(_spec_dims, _ledger,
                                               str(_spec or "")))
                        except Exception:
                            pass
                        try:
                            _ms.note_phase(scene, question, 3,
                                           reason="finalCode 已交，对账 spec vs code")
                        except Exception:
                            pass
                        try:
                            # Phase3 加**总时限**：它是"复盘"，而学习本身
                            # （Phase1 闭合 + Phase2 交码）在它之前就完成了。
                            # 不加时限时，数学题的分歧逐条作答 + 修订可能跑
                            # 20 分钟以上，整轮学习请求卡在这里不返回，
                            # 面板上看起来就是"学了但什么也没发生"
                            # （2026-11-14 P 实测：25 分钟被杀，进度全丢）。
                            out["phase3"] = await asyncio.wait_for(
                                phase3_intent_diff_review(
                                scene, question,
                                _final_text_of(_spec_dims, _ledger,
                                               str(_spec or "")), _p2_code,
                                db_path=str(exec_db or db_path),
                                scene_dir=str(local_dir or exec_dir or ""),
                                llm=llm,
                                run_result=((out.get("milestone_skill") or {})
                                            .get("verified_result") or {}),
                                env_obs=_env_obs_block(exec_db or db_path),
                                topic_id=str(_tid or ""),
                                req_id=str(_req_id or ""),
                                # 复用 Phase2 那次对账（"过不过关"的同一份判据）。
                                # 那次分类器没跑成（None）→ 这里自己再对一次。
                                pre_divergences=(list(_p2_divs)
                                                 if _p2_diff_known else None),
                                milestone_name=str(
                                    ((_mst or {}).get("milestone")
                                     or {}).get("name") or "")),
                                timeout=float(os.environ.get("MVE_P3_TIMEOUT")
                                              or 240.0))
                            _p3 = out["phase3"] or {}
                            _c3 = _p3.get("counts") or {}
                            if _p3.get("divergences"):
                                print("  · Phase3 分歧 "
                                      + str(len(_p3["divergences"])) + " 条｜"
                                      + "drift " + str(_c3.get("drift") or 0)
                                      + "｜revision " + str(_c3.get("revision") or 0)
                                      + "｜bug " + str(_c3.get("bug") or 0)
                                      + "｜对齐 " + str(_c3.get("aligned") or 0)
                                      + "｜部分 " + str(_c3.get("partial") or 0)
                                      + "｜仍偏 " + str(_c3.get("diverged") or 0)
                                      + "｜未判 " + str(_c3.get("unjudged") or 0)
                                      + "｜修订 "
                                      + str(_p3.get("revision_choice") or "?"),
                                      flush=True)
                            elif _p3.get("skipped"):
                                print("  · Phase3 跳过："
                                      + str(_p3["skipped"])[:140], flush=True)
                        except asyncio.TimeoutError:
                            out["phase3"] = {
                                "skipped": ["Phase3 对账超时（Phase1/Phase2 已完成，"
                                            "对账留到下次同一题）"],
                                "timed_out": True}
                            print("  · Phase3 超时 —— 学习已记账，对账留到下次",
                                  flush=True)
                        except Exception as _p3e:
                            out["phase3"] = {
                                "error": f"{type(_p3e).__name__}: {_p3e}"}
                        # Phase3 的结论回写「过关」：助产士原版的会话是在
                        # 提交 → 对账 → **逐条解释/修订** 之后完结的，并不要求
                        # "提交那一刻就 0 分歧"。所以：
                        #   · 交出了修订版（缺口已补）→ 这份码兑现了 finalText；
                        #   · 或每条分歧复判都是 aligned（其实一致/解释成立）→ 原件即可。
                        _p3v = out.get("phase3") or {}
                        _c3v = _p3v.get("counts") or {}
                        _p3_total = int(_c3v.get("total") or 0)
                        _p3_ok = (
                            str(_p3v.get("revision_choice") or "") == "revised"
                            or (_p3_total > 0
                                and int(_c3v.get("aligned") or 0) == _p3_total))
                        if _p3_ok and (out.get("phase2") or {}):
                            out["phase2"]["verified"] = True
                            out["phase2"]["verified_by"] = "phase3"
                            # Phase3 的结论回写「过关」后要**重新落一次账** ——
                            # 否则 phase2 上留的是提交那一刻的 False，面板按
                            # P1→P2→P3 读会显示「交了但没过、分歧也判成对齐了」，
                            # 自相矛盾（口径见 _p3_ok 的注释）。
                            try:
                                _ms.save_phase2(scene, question, {
                                    **out["phase2"],
                                    "final_code": str(_p2_code or ""),
                                    "submitted_at": time.strftime(
                                        "%Y-%m-%dT%H:%M:%S"),
                                    "run": int(_p1_run or 0)})
                            except Exception:
                                pass
                    observations = observations[-8:]
                    sql = ""
                    _answer_override = "\n\n".join(answers)[:2500]
                else:
                    # 材料账（同构 gathered）：整题一次执行也可能直接交 answer，
                    # 同样把「实际取回的内容」按账收着，供 critic/闸门判定。
                    _ev3: list[dict[str, Any]] = []
                    if _code_exec:
                        observations, sql = await local_agent.solve(
                            question, local_dir or exec_dir, llm=llm,
                            scene_id=scene, max_iters=INNER_MAX_ITERS,
                            critique=str(critique or ""),
                            dimensions=_dims, avoid=_avoid,
                            skills=_py_skills, require_tools=_req_tools,
                            evidence=_ev3, timeout=EXEC_TIMEOUT,
                            db_path=exec_db,
                            # 同 1007 行：_hint_ctx（含里程碑名里的仓库地址）
                            # 必须进 context，否则题干没 URL 时模型不知道调研谁。
                            context=((_env_obs_block(exec_db)
                                      if exec_db else "")
                                     + "\n" + "\n".join(_hint_ctx)).strip())
                    else:
                        observations, sql = await agent.solve(
                            question, skills=skills, grabs=grabs,
                            focus_tables=focus, hints=_hint_ctx)
                    _answer_override = ""
            else:
                # ⚠️ 出题器的 Context（口径说明：哪张表、字面量用哪个真实值）
                # 必须跟题一起进执行层的 Context 槽 —— 原版 voyager.py:419-424
                # `rollout(task=next_task, context=get_task_context(next_task))`
                # 就是一路传的。2026-10-23 实测（Scene P）：课程出的题面是
                # 「技术部有哪些在职员工？」、Context 明说 dept='技术'，但这儿
                # 只把子目标 need 当 hints，Context 被丢掉 → 执行层按常识写
                # dept='技术部' → 0 行 → 连败。`solve(hints=...)` 就是原版
                # context 槽（见 action.py:507），所以把 context 拼在最前。
                _hint_ctx = [str(x).strip() for x in (str(context or ""), str(critique or ""))
                             if str(x or "").strip()]
                if _code_exec:
                    # 出题器自由出题也走代码路径（同一套原语：库/本机/联网/GitHub）。
                    # 不这么写，MVE_CODE_EXEC=all 时 agent 是 None → 这里 AttributeError。
                    observations, sql = await local_agent.solve(
                        question, local_dir or exec_dir, llm=llm,
                        scene_id=scene, max_iters=INNER_MAX_ITERS,
                        critique=str(critique or ""),
                        dimensions=[], avoid=_avoid, skills=_py_skills,
                        require_tools=_req_tools, timeout=EXEC_TIMEOUT,
                        db_path=exec_db,
                        context=((_env_obs_block(exec_db) if exec_db else "")
                                 + "\n" + "\n".join(_hint_ctx)).strip())
                else:
                    observations, sql = await agent.solve(
                        question, skills=skills, grabs=grabs, focus_tables=focus,
                        hints=_hint_ctx
                        + [str(s.get("need") or "") for s in plan["subtasks"]])
                # 唯一 critic（2026-10-24 同构化）：solve 内层每步的判定（口径=
                # 原版 critic.txt + 环境实查，见 VLMLActionAgent.critic_check）
                # 就是原版 rollout 的 critic —— 这里把最后一步的判定/判词带出，
                # ⑤' 直接采用，外层不再让 task_critic 重判一遍（双 critic 偏离）。
                if agent is not None and getattr(agent, "last_success", None) is not None:
                    _ic = getattr(agent, "critic", None)
                    out["inner_critic"] = {
                        "success": bool(agent.last_success),
                        "critique": str(getattr(_ic, "last_critique", "") or ""),
                        "reasoning": str(getattr(agent, "critic_reasoning", "") or ""),
                    }
                    if agent.last_success and agent.critic_reasoning \
                            and not str(out.get("answer") or "").strip():
                        # 原版：critic 的 reasoning 就是结论句（answer）
                        out["answer"] = agent.critic_reasoning
            # 执行环境自报数据来源：SceneDBEnv / VLMLMCPEnv 都有 data_source()，
            # 但它们的观察里**不带** data_sources（那是 local_agent 的 MCP 工具才
            # 填的字段）→ 于是真取到数也会算成 real_calls=0 / evidence=knowledge，
            # 判据跟着走「知识型」分支，critic 拿不到"确实取到数"这件事。
            # 这里按 env 补一次，判据口径不变（仍然是"真的取到数才算"）。
            _env_src: list[str] = []
            try:
                _ds_fn = getattr(getattr(agent, "env", None), "data_source", None)
                if callable(_ds_fn):
                    _v = str(_ds_fn() or "").strip()
                    if _v:
                        _env_src = [_v]
            except Exception:
                _env_src = []
            if _env_src and observations:
                observations = [
                    dict(o, data_sources=list(o.get("data_sources") or (
                        _env_src if o.get("ok") else [])))
                    for o in observations]

            real = _has_real(list(observations or []))
            # 无外部材料的场景没有数据源可指认，"真取到"= 交出了结论值。
            # 不补这一档，real_calls 恒为 0 → task_critic 判"没取到数"，
            # 学生答对也会被记成失败（2026-11-14 P 实测）。
            try:
                import scene_profile as _spc
                _course_real = 1 if ((not _spc.grounded(scene)) and real) else 0
            except Exception:
                _course_real = 0
            # ★ Phase2 交了 finalCode 并真跑通 → 这一轮的"取到真数"由它成立。
            #   原版 Phase1 是纯文字 spec（**不取数**），取数发生在 Phase2 的
            #   finalCode 里；旧口径只看 Phase1 的观察，会把整篇模式判成
            #   no_data（2026-11-12 实测：里程碑已入库、整轮却 status=no_data）。
            _p2r = int(out.pop("_p2_rows", 0) or 0)
            if _p2r > 0:
                real = True
            # 正文类材料也算「真取到」（真空防线的文档侧口径，见 _material_real）。
            # 放在这里而不是只改 r1：`real` 是 no_data 闸门的判据，r1 那份只给日志看
            # —— 2026-10-31 实测「只对齐 r1」等于没对齐，闸门照样判死这一轮。
            _mat_n = max(int(out.get("_mat_real") or 0),
                         len(_material_real(out.get("evidence"))))
            out.pop("_mat_real", None)      # 只是轮内口径，不进 run_log
            if _mat_n:
                real = True
            out["execution"] = {
                "ok": real, "sql": sql,
                "calls": max(len(observations or []), _mat_n),
                # 「真取到」只数**真的有数据源支撑**的观察：知识型答案（RESULT 是一段
                # 回答、没有任何数据源调用）不该记成"取到数"——否则日志会自欺
                # （2026-10-19 实测：养育问题打出了"真取到=1"）。
                "real_calls": max(
                    sum(1 for o in (observations or [])
                        if _has_real([o]) and (o.get("data_sources") or [])),
                    _mat_n, _course_real),
                "rows": max(
                    sum(int(o.get("rows") or 0)
                        for o in (observations or []) if o.get("ok")),
                    _mat_n, _p2r),
                "data_sources": sorted({s for o in (observations or [])
                                        for s in (o.get("data_sources") or [])}),
                "evidence": ("data" if any(o.get("data_sources")
                                           for o in (observations or []))
                             else "knowledge"),
            }
            # 材料账挂在 result 顶层（execution.evidence 是另一个含义：data/knowledge）
            # ⚠️ 维度分支没有 _ev3（每条维度自己记），这时**不设这个键** ——
            # 设成空列表会被 critic 当成「一次都没取到内容」误判失败。
            try:
                out["evidence"] = list(_ev3)          # type: ignore[name-defined]
            except NameError:
                pass
            # 答案与逐轮观察留给 critic / 面板（知识型任务没有数据源，critic 靠答案判）
            out["answer"] = (str(_answer_override or "").strip()
                             or _answer_text(observations))
            out["observations"] = [
                {"ok": bool(o.get("ok")), "rows": int(o.get("rows") or 0),
                 "data_sources": list(o.get("data_sources") or []),
                 "evidence": str(o.get("evidence") or ""),
                 "error": str(o.get("error") or "")[:200],
                 "data": (json.dumps(o.get("data"), ensure_ascii=False,
                                     default=str)[:600]
                          if not isinstance(o.get("data"), str)
                          else str(o.get("data"))[:600])}
                for o in (observations or [])][-4:]
            if not real:
                # 数据型任务：没取到真数 = 没完成（真空防线，保持）。
                # 知识型任务（怎么养育/如何理解…）：本来就没有数据可取 —— 交给下面
                # 的 critic 用 LLM 判，别一刀切判死（2026-10-19 用户实测踩过）。
                try:
                    import task_critic as _tc
                    # ⚠️ has_env 必须传（2026-10-31 H 实测踩到）：needs_data 的
                    # 签名是 (question, has_env=False)，只传一个参数 = 永远判
                    # 「不需要取数」→ 下面这条真空防线对**所有场景**都失效。
                    # 那一轮里程碑重试 calls=4 / rows=0 / ok=False，本该判
                    # no_data 不写掌握，却被放行成 learned 并「里程碑解锁：是」。
                    # 分型权威 = 环境（task_critic 头注释 2026-10-28 已拍板），
                    # 题面措辞不再参与判断。
                    import reward as _rw_n
                    _needs = _tc.needs_data(question, _rw_n.scene_has_env(scene))
                    # 无外部材料的场景，真值是**推出来的**，不是查出来的：
                    # 学生交的 RESULT 里已经有值（哪怕算错），就不该被
                    # "没取到行"这条闸门吞掉 —— 对错交给判官判。
                    # 2026-11-14 P 实测：相似比方向反了答 12.96（正确 100），
                    # 整轮却被判 no_data，错都记不上。
                    try:
                        import scene_profile as _spn
                        if not _spn.grounded(scene):
                            _needs = False
                    except Exception:
                        pass
                except Exception:
                    _needs = True
                if _needs:
                    _c = int(out["execution"].get("calls") or 0)
                    _r = int(out["execution"].get("rows") or 0)
                    # 说清楚是「没调」还是「调了但全空」—— 后者模型能改，前者要换路子。
                    _what = (f"调了 {_c} 次、取回 {_r} 行，但每行都是空值（NULL/0）"
                             if _c else "一次数据源都没调")
                    out.update(status="no_data",
                               why=f"执行层没拿到真数：{_what} —— "
                                   "原版铁律：失败不写技能库，不结晶不建点")
                    _record(out)
                    return out
        except Exception as e:
            out.update(status="failed",
                       why=f"执行层失败：{type(e).__name__}: {e}")
            _record(out)
            return out
    else:
        out.update(status="prepared",
                   why="只完成拆分+补缺口（execute=False），未取数")
        _record(out)
        return out

    # ---- ⑤' 完成判定（原版 critic.check_task_success 的同构）----
    # 放在入库**之前**：判定结果决定这一轮算不算学到（原版 voyager.py:230 在
    # step 里判，判过才 add_new_skill、update_exploration_progress）。
    # ⚠️ 维度汇总**不受 judge 形参管**（2026-10-26 服务器实测修复）：每条
    # 维度的闭合判定在各自 rollout 内已经做完了（步级双判定：内层 critic
    # × 闸门），这里的汇总只是数 failed 条数，**不花一次 LLM**。曾经整段
    # 挂在 `if judge:` 下面 → 面板「▶ 学会这句」按钮（judge=False）整段
    # 跳过，0/6 全败的一轮被 ⑥ `_absorb` 的「有答案文本」判成 learned
    # （run_log verdict=ok、面板/轨迹页显示成功）—— 正是「有行≠答出来」
    # 铁律要防的事：判定必须过同一把尺子，入口不同不改口径。
    _dres = out.get("dimension_results")
    _crit = ""
    if _dres is not None:
        # 维度模式（`MVE_P1_WHOLE=0` 回退路径）：判定已经在**每个维度各自的
        # rollout 后**做过了（maieutic_gate 的闭合口径）。这里只汇总，
        # 不再让 critic 判一遍。整篇模式不再产这本账（见 spec_outcome）。
        _failed = _dres.get("failed") or []
        _ok = not _failed
        _crit = " ".join(f"[{f.get('id')}] {str(f.get('critique') or '')}"
                         for f in _failed).strip()[:300]
        out["critic"] = {"success": bool(_ok), "critique": _crit,
                         "gate": {"addressed": _dres.get("learned") or [],
                                  "open": [f.get("id") for f in _failed],
                                  "questions": [str(f.get("critique") or "")
                                                for f in _failed]}}
    elif out.get("spec_outcome") is not None:
        # 整篇模式：判定权在 maieutic 判官（spec_gate），这里**不重判**。
        # 用户 2026-11-12 口径：只有两个可预见的闭合点，没有「几维闭合」。
        _so = out["spec_outcome"]
        _sg = out.get("spec_gate") or {}
        _ok = bool(_so.get("phase1_passed"))
        _crit = ("" if _ok else "spec 未冻结："
                 + str(_sg.get("reason") or "判官仍判有缺口")[:300])
        out["critic"] = {
            "success": bool(_so.get("phase1_passed")),
            "critique": ("" if _so.get("phase1_passed")
                         else "spec 未冻结：" + str(_sg.get("reason") or ""
                                                    or "判官仍判有缺口")[:300]),
            "gate": {"addressed": [str(x) for x in (_sg.get("addressed") or [])],
                     "open": [str(x) for x in (_sg.get("open") or [])],
                     "questions": [str(x)[:200] for x in
                                   (_sg.get("questions") or [])][:3]},
            "origin": "maieutic_spec_gate"}
    else:
        # 唯一 critic（2026-10-24 同构化）：执行层走了 solve（scene/vlml
        # 后端）时，内层每步判定就是原版 rollout 的 critic —— 外层不再让
        # task_critic 重判一遍（双 critic 偏离已修）。没有内层判定的后端
        # （local_agent 写代码路径）才落到 task_critic。
        inner = out.get("inner_critic") or {}
        if "success" in inner:
            _ok = bool(inner.get("success"))
            _crit = str(inner.get("critique") or "")
            out["critic"] = {"success": _ok, "critique": _crit,
                             "gate": out.get("critic_gate") or {},
                             "origin": "inner"}
        elif judge:
            try:
                import task_critic
                # 原版同构（2026-10-24 用户定「用原 voyager 看环境的方法来抄」）：
                # 原版 critic 看到的是**环境末态**（背包/附近方块），所以能验证
                # "挖到铁矿" —— 观察里看得见。SQL 场景的同构物 = 库的实查清单
                # （表/列/真实取值）随 context 交给 critic：任务假设的表/列/业务
                # 参数（费率/汇率…）必须能在这份清单里指认，指认不了 = 不可答，
                # 不许把编造的参数当已验证（实测 150 元费率幻觉就是这样放行的）。
                _ctx = str(context or "") + _env_obs_block(db_path)
                import reward as _rw
                _ok, _crit = task_critic.check_task_success(
                    question, _ctx, out, has_env=_rw.scene_has_env(scene),
                    scene_id=scene)
            except Exception as e:
                _ok, _crit = bool(real), f"critic 异常：{type(e).__name__}: {e}"
            out["critic"] = {"success": bool(_ok), "critique": str(_crit or ""),
                             "gate": out.get("critic_gate") or {}}
        else:
            # 无内层判定、又不让调 critic（judge=False 的老入口）：维持原
            # 行为 —— 不拦，交给 ⑥ 入库侧决定（absorb 的 mastered 口径）。
            _ok = True
    # 证据机检统一闸口（2026-11-17）：out["critic"] 的判定入口有多个
    # （维度汇总 / 整篇 spec_gate / 内层 critic / task_critic 兜底）——
    # 「入口不同不改口径」：声明了 verify_claim 的场景，最终 success=True
    # 一律再过一道 _verify_claim_gate；机检不过 → 整体判未完成，评语回灌。
    if _ok and bool((out.get("critic") or {}).get("success")):
        import task_critic as _tcg
        _ok2, _why2 = _tcg._verify_claim_gate(scene, out)
        if not _ok2:
            out["critic"]["success"] = False
            out["critic"]["critique"] = (
                "证据机检未过：" + str(_why2 or "")[:200] +
                "｜请在答案末尾附一段 ```mve_verify``` 代码块（JSON）："
                '{"refs": [{"file": "模块名.py", "line": 行号, '
                '"expect": "该行包含的片段"}]} —— file 相对仓库 mve/ '
                '目录写，行号与片段必须真实存在（可先 read_file 核对）')[:400]
            _ok = False
            _crit = out["critic"]["critique"]
            print("  · 证据机检：未过 —— " + str(_why2 or "")[:160], flush=True)
    if not _ok:
        # 已闭合的维度各自是一次成功 rollout：它们的技能在维度循环里已经
        # 落库（learn_milestone_skills 不连坐，见 1368 行注释）—— **同轮
        # 就要结晶**。以前这里提前 return 把 ⑦ 挡在后面：部分闭合的轮次
        # （2026-10-26 生产实测 2/4 闭合）闭合维度的技能永远停在技能库
        # 文本层，消费侧 invoke 不到（crystallize_all 零调用方，面板重启
        # 自检也不重结晶）。0 闭合时 added 为空，helper 自然空转。
        _crystallize_round_skills(out, scene, local_dir or exec_dir, _code_exec, sql)
        out.update(status=(("partial" if (_dres or {}).get("learned")
                            else "failed") if _dres is not None
                           else ("no_data" if not real else "failed")),
                   why=(str(_crit or "") or "闸门/critic 判未完成")[:300])
        _record(out)
        return out

    # 技能有效记账（2026-11-17 补链）：本轮判过（含证据机检闸口放行），
    # 注入过的技能按原版语义 mark_result(..., True) —— ok≤hits 不变量由
    # mark_result 内部兜底。未判过的路径不记（让低效技能自然沉底）。
    try:
        import skill_store as _ssr
        _ik = [k for k in (out.get("_injected_skill_keys") or []) if k]
        if _ik:
            _ssr.mark_result(_ik, True)
    except Exception:
        pass

    # ---- ⑥ 判过 → 图谱建点 + 掌握 + 配方入库（复用消费侧同一套账）----
    try:
        import voyager_vlml.consume as _consume
        out["absorbed"] = _consume._absorb(question, sql, "",
                                          scene_id=scene,
                                          answer=out.get("answer", ""))
    except Exception as e:
        out.update(status="failed",
                   why=f"入库失败（_absorb）：{type(e).__name__}: {e}")
        _record(out)
        return out

    # 里程碑解锁（用户闭环第 4 步的「不再重复出题」）：
    # **复用原 MVE 已有披露**（scene_context.unlocked_milestones = MasteryStore
    # 里 completed 的知识点 = 已解锁的图谱节点，进运行时上下文；规划/出题器按
    # is_completed 门控不再重复推，见 topic_graph.py:582）。这里不另造读法，
    # 只回答一个问题：**这一轮学到的能力项，现在是不是已解锁里程碑**。
    tid = str(out["absorbed"].get("topic_id") or "")
    # 图谱落点：**只落一枚节点 —— 过关的 Phase2**（用户 2026-11-12 口径）。
    #   req:<id>:phase2 = 技能沉淀的位置。Phase1 不上图：它的闭合是整篇的、
    #   动态的（同构助产士 examiner），没有可预见的中间点；不闭合就永远到
    #   不了 Phase2，技能不沉淀、图谱不长节点。
    try:
        out["phase_nodes"] = attach_phase_nodes(
            scene, str(_req_id or ""), question,
            phase2=(out.get("phase2") or None),
            verified=(out.get("status") == "learned"))
    except Exception as e:
        out["phase_nodes_error"] = f"{type(e).__name__}: {e}"
    out["milestone"] = {"topic_id": tid, "unlocked": False}
    try:
        import scene_context
        out["milestone"]["unlocked"] = tid in scene_context.unlocked_milestones()
    except Exception as e:
        out["milestone"]["why"] = f"{type(e).__name__}: {e}"

    # ---- ⑦ 写入技能库 → 结晶成条目（闭环最后一环：下次抓得到）----
    _crystallize_round_skills(out, scene, local_dir or exec_dir, _code_exec, sql)

    out["status"] = "learned" if out["absorbed"].get("mastered") else "partial"
    if out["status"] == "partial":
        out["why"] = ("取到值并执行了，但图谱标记掌握这步没成"
                      f"（{out['absorbed'].get('skill_action') or '无说明'}）")
    # ★ 2026-11-12（实测抓到）：Phase1 判过 ≠ 这一轮学成了。原版 maieutic 的
    #   一次会话是「Phase1 冻结 spec → Phase2 交 finalCode → Phase3 对账」，
    #   **finalCode 没交出来 = 会话没走完**（sessions.ts 的 submit 只接受
    #   phase==2 的提交，没提交就没有 Phase3/Phase4）。MVE 原来只看
    #   absorbed.mastered（那是 Phase1 闭合时标的），于是出现：
    #     场景 N 重学 → Phase1 3 轮闭合 → Phase2 程序跑出来 rows=0 → 没交 finalCode
    #     → 技能库 0 条、phase2 空 → 面板照样报 status=learned、skill=""。
    #   这是**整轮级的假闭合**（维度级早就不许了，轮级也得堵上）。
    #   口径：Phase1 已闭合但 Phase2 没交出可运行的 finalCode → partial，
    #   并把没交的原因原样带出来（不拿 Phase1 的文字作答冒充交付）。
    _p2 = out.get("phase2") or {}
    if out["status"] == "learned" and _p2 and not _p2.get("verified"):
        out["status"] = "partial"
        out["why"] = ("Phase1 已闭合、finalCode 已提交（"
                      + str(_p2.get("rounds") or 0) + " 轮），但与 spec 仍有对不上的："
                      + (str(_p2.get("skipped") or "") or "未跑通")[:200])
    _record(out)
    return out


# ---------------------------------------------------------------------------
# 后台触发（consume 未命中时接线用）+ 结果留痕（面板 API 读）
# ---------------------------------------------------------------------------
_BG: dict[str, threading.Thread] = {}   # 后台学习线程（问题 → 线程）
_LAST: list[dict[str, Any]] = []
_LAST_MAX = 20


def kick_learning(question: str, scene_id: str = "", *,
                  reason: str = "") -> dict[str, Any]:
    """后台启动一轮学习（fire-and-forget）—— 人导入未命中就自动走这里。

    ⚠️ **必须用独立线程 + 自己的事件循环**（2026-10-19 服务器实测踩的坑）：
    原来写成 `loop.create_task(...)`，而面板的 consume 是
    `asyncio.run(_consume.consume(...))` —— run 收尾会 `_cancel_all_tasks`，
    刚 create 的任务在第一次 await 就被 cancel。表现是「导入完成、说已启动
    学习，但技能库始终是空的」（学习从未真正跑过）。线程的方案与调用方
    生命周期解耦：consume 线程结束、面板重启都不影响它跑完这一轮。

    同一问题已在跑 → 不重复起（如实回 kicked=False + why）。

    `reason` 只进返回值与日志（"为什么学这一轮"）：未命中 → 陌生问题；
    命中但没取到数 → 认识这句话但还不会回答它。两条都要学。
    """
    question = str(question or "").strip()
    if not question:
        return {"kicked": False, "why": "空问题"}
    old = _BG.get(question)
    if old is not None and getattr(old, "is_alive", lambda: False)():
        return {"kicked": False, "why": "同一问题的学习已在后台跑"}

    # ★ 2026-11-11（拆骨架不做归一，用户拍板）：
    #   **派发即发布** —— 真派一次任务 = 原版 maieutic 的「教师发布一次
    #   Exercise」。同一句再派一次，必须拿到新 req id，学习侧才会重拆骨架；
    #   旧的那份骨架与那本账原样保留（面板按 req_id 各自成卡片）。
    #   同构依据：`Exercise.id` 由发布方给、不从题面文本算，全仓无相似度检索
    #   （req_registry 模块头已复核 prisma/schema.prisma:14）。
    #   这里**只**跳过归并闸门，学习侧的拆维判据一律不动。
    _new_req = ""
    try:
        import req_registry as _rr_pub
        _reg_pub = _rr_pub.register(scene_id, question, force_new=True)
        _new_req = str(_reg_pub.get("req_id") or "")
        if _new_req:
            _rr_pub.ensure_req_node(scene_id, question, _new_req)
    except Exception:
        _new_req = ""

    def _run() -> None:
        try:
            asyncio.run(start_learning(question, scene_id, req_id=_new_req))
        except Exception as e:  # 后台任务自己兜住，绝不无声死掉
            _record({"question": question, "scene_id": scene_id,
                     "status": "failed", "why": f"{type(e).__name__}: {e}"})
        # （学习完成后**不再**触发自动回访 —— 2026-11-09 用户指令拆掉整套
        #   排队+回访。_record 现在只做留痕：run_log + 因果时间线。）

    t = threading.Thread(target=_run, name=f"learn-{question[:16]}",
                         daemon=True)
    _BG[question] = t
    t.start()
    return {"kicked": True, "question": question[:60], "mode": "thread",
            "req_id": _new_req, "new_dispatch": True,
            "reason": str(reason or "")}


def _crystallize_round_skills(out: dict[str, Any], scene: str,
                              code_dir: str, code_exec: bool,
                              sql: str) -> None:
    """把这一轮**真实落库**的技能结晶成可 invoke 条目（闭环最后一环）。

    三条通道（2026-10-26 补齐维度路径）：
      ① 整题路径：_absorb 落的配方（absorbed.skill_key）；
      ② 维度路径：闭合维度写的原子技能（skills.added）—— 维度路径
         sql="" 按原文永远匹配不上，且 crystallize_all 全库零调用方、
         面板重启自检只 restore 注册表快照不重结晶 → 不主动结晶，
         「学会」就停在技能库文本层，消费侧 invoke 不到；
      ③ 里程碑全闭合的复合技能（milestone_skill.added）。

    ⚠️ 指认用 key、不许按 code 字符串匹配：入库前 generalize_sql_recipe
    会把 SQL 泛化成 :slot 模板，库里存的是模板、这里是这次跑通的原文
    → 相等永不成立 → 结晶静默跳过（2026-10-26 实测挂过两条断言）。

    **部分闭合的轮次也要调用**（⑤' 判 partial 会提前 return）：已闭合
    维度各自是一次成功 rollout，不该被没闭合的维度连坐（2026-10-26
    生产实测：2/4 闭合，技能在库、零 invoke 条目）。0 闭合时 added
    为空，本函数自然空转，不写 out["crystallized"]。
    """
    try:
        import skill_store
        import plugin_forge
        _keys: list[str] = []
        _sk = str((out.get("absorbed") or {}).get("skill_key") or "")
        if _sk:
            _keys.append(_sk)
        for _a in ((out.get("skills") or {}).get("added") or []):
            _k = str((_a or {}).get("key") or "")
            if _k and _k not in _keys:
                _keys.append(_k)
        _mk = str((out.get("milestone_skill") or {}).get("added") or "")
        if _mk and _mk not in _keys:
            _keys.append(_mk)
        fresh: list[dict[str, Any]] = []
        if _keys:
            _want = set(_keys)
            fresh = [s for s in skill_store.all_skills()
                     if str(s.get("key") or "") in _want]
        if not fresh and str(sql).strip():
            # 兜底（老结果没有 key）：按本次 SQL 原文 / rollout_sql 匹配
            fresh = [s for s in skill_store.all_skills()
                     if str(s.get("code") or "") == str(sql)
                     or str((s.get("blueprint") or {}).get("rollout_sql")
                            or "") == str(sql)]
        _cries: list[dict[str, Any]] = []
        for _s in fresh:
            _cries.append(plugin_forge.crystallize_skill(
                _s, into_scene=scene,
                local_scene_dir=(code_dir if code_exec else "")))
        if _cries:
            # 主结果保持 dict 形状（面板 / scene_learn 按 .ok/.entry 读）；
            # 多条时全量进 crystallized_all —— 别把后几条 silently 丢掉。
            out["crystallized"] = next(
                (c for c in _cries if c.get("ok")), _cries[0])
            if len(_cries) > 1:
                out["crystallized_all"] = _cries
            _n_ok = sum(1 for c in _cries if c.get("ok"))
            if _n_ok:
                print(f"  · 技能结晶：{_n_ok}/{len(_cries)} 条变成可 invoke 条目"
                      f"（{', '.join(str(c.get('entry') or '') for c in _cries if c.get('ok'))}）",
                      flush=True)
    except Exception as e:
        out["crystallized"] = {"ok": False, "why": f"{type(e).__name__}: {e}"}


def _record(result: dict[str, Any]) -> None:
    """留痕：内存里给面板 API，因果时间线给复盘，**run_log 给轨迹页**。

    为什么要落 run_log（2026-10-19 用户改设计）：学习改由人点「▶ 启动 Voyager」
    触发后，每一轮都得在「运行日志/轨迹」页看得见 —— 原来只写内存 _LAST +
    因果时间线，轨迹页永远「0 轮」。字段对齐 loop.py:525 那行的面板读取契约；
    不编造 coverage/score（学习环不判 VLML 分，如实 None，judge=db_evidence）。
    """
    result = dict(result)
    result["finished_at"] = round(time.time(), 1)
    _LAST.append(result)
    del _LAST[:-_LAST_MAX]
    # ---- run_log 一行（轨迹页可见；写失败不拦学习本身）----
    try:
        import run_log
        status = str(result.get("status") or "")
        execution = result.get("execution") or {}
        success = status in ("learned", "partial")
        _VERDICT = {"learned": "ok", "partial": "partial",
                    "no_data": "no_evidence", "unreachable": "unreachable",
                    "prepared": "prepared", "failed": "failed"}
        why = str(result.get("why") or "")
        run_log.append({
            "mode": "scene_learn",
            # 显式带场景：scene_learn 在子进程里跑，人可能已切了场景，
            # 按"当前场景"补会记错账（与 pilot.start 钉场景同一个道理）。
            "scene": str(result.get("scene_id") or ""),
            "topic_id": str((result.get("absorbed") or {}).get("topic_id") or ""),
            "task": str(result.get("question") or "")[:60],
            "question": str(result.get("question") or ""),
            "round": 1,
            "hint": "",
            "coverage": None,
            "score": None,
            "verdict": _VERDICT.get(status, status or "failed"),
            "success": success,
            "done": status == "learned",
            "evidence_status": "collected" if success else "none",
            "judge": "db_evidence",
            "no_tool_calls": not execution.get("calls"),
            "feedback": {},
            "tool_calls": int(execution.get("calls") or 0),
            "errors": [why[:200]] if why else [],
            "covered": [],
            "missing": [] if success else [str(result.get("question") or "")[:60]],
            "narrative": "",
            "generated_task": False,
            "sql": str(execution.get("sql") or ""),
            "skill_key": "",
            "reused_skills": [],
            # 学习环原始状态与后端（轨迹页点开一条能看见学的是哪个数据源）
            "learn_status": status,
            "backend": str((result.get("backend") or {}).get("kind") or ""),
        })
    except Exception:
        pass
    try:
        import causal_timeline
        causal_timeline.append(
            "learn",            topic_id=str(result.get("absorbed", {}).get("topic_id") or ""),
            summary=(f"学习[{result.get('status')}] "
                     f"{str(result.get('question') or '')[:40]}"
                     + (f" —— {result['why'][:60]}" if result.get("why") else "")),
            detail={"status": result.get("status"),
                    "origin": (result.get("plan") or {}).get("origin", ""),
                    "forged": (result.get("forged") or {}).get("counts", {}),
                    "exposed": len((result.get("exposed") or {}).get("exposed")
                                   or []),
                    "stuck": len(result.get("stuck") or []),
                    "execution_ok": (result.get("execution") or {}).get("ok"),
                    "crystallized": bool((result.get("crystallized") or {}).get("ok")),
                    "why": str(result.get("why") or "")[:200]},
            scene=str(result.get("scene_id") or ""),
        )
    except Exception:
        pass                                # 留痕失败不拦学习本身
    # ⚠️ **学习完成不再触发自动回访**（2026-11-09 用户指令：「不要回访了，
    #   也不要排队，会搞乱掉」）。
    #   原来这里在 status ∈ learned/partial 时起线程跑 `revisit.revisit_pending`
    #   —— 把排队里没答上的题逐条重跑 consume、重答、落 run_log(mode=revisit)。
    #   那套（排队 + 回访 + 提示条）已整套删除，`revisit.py` 也不再存在。
    #   现在学习侧只管**学**：写技能、落账、留痕；**不回头重答任何题**。
    #   人再问同一句（或换个问法）时，答案是由**会话召回 + 已验答案披露**
    #   给出的（第三刀）—— 披露学习侧验过的原答案，不必重跑一遍判定。


def last_learn_results(limit: int = 10) -> list[dict[str, Any]]:
    """面板 API 用：最近几轮学习的结果（新的在前）。"""
    return list(reversed(_LAST))[:max(1, int(limit))]


def learn_running() -> list[str]:
    """后台还在跑的问题（面板显示「学着呢」）。"""
    return [q for q, t in _BG.items()
            if getattr(t, "is_alive", lambda: False)()]
