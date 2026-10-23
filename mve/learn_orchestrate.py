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
            return json.dumps(d, ensure_ascii=False)[:1200]
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


# 单个子任务（维度）的 rollout 上限 —— **抄原版** `action_agent_task_max_retries=4`
# （voyager_original/voyager/voyager.py:30；:277-279 `done = 迭代数 >= 上限 or
# success`）。到上限仍不闭合就跳过这条维度（curriculum.py:342 "Skipping to
# next task"）—— 不是"再试一次看看"，原版就没有无限重试。
SUBTASK_MAX_RETRIES = 4

# 一次 rollout（跑一段取证代码）的墙钟上限。
# 为什么不是默认的 30s（2026-10-20 实测致命点）：单次 MCP 调用自己就有 60s
# 上限（local_agent.py:680），外层比内层短 → 工具还没回来就判超时，而超时又
# 不会杀掉线程（Python 做不到），于是 4 次重试只是往同一个被限流的接口上堆
# 4 个僵尸线程；7 条维度 × 4 次 = 整轮十几分钟、颗粒无收。
# 原版 rollout 里 bot 执行本来就允许几十秒（`bot.chat` 一路打进度），这里按
# 外部工具的量级给，可用环境变量覆盖（跑本地/纯 SQL 场景时可以调小）。
EXEC_TIMEOUT = float(os.environ.get("MVE_EXEC_TIMEOUT") or 90.0)


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
        return [str(e.get("name") or "").strip()
                for e in actions.enabled_entries(scene_id)
                if str(e.get("name") or "").strip()]
    except Exception:
        return []


def learn_milestone_skills(scene_id: str, topic_id: str, question: str,
                           milestone: dict[str, Any],
                           dim_codes: list[dict[str, Any]],
                           *, import_id: str = "",
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
        try:
            _act, key = skill_store.add(
                # key = f"{topic}::{name}"（skill_store._key），所以 name 直接用
                # 维度 id —— 写成 f"{topic}::{did}" 会出现 topic::topic::dim 双前缀。
                topic_id, did,
                # ⚠️ 技能描述必须够长：`add()` 对 <MIN_LESSON_LEN(12) 的 text
                # 直接 skipped（照原版 "Deposit useless items" 特判）。
                # 光写维度问句在短问题上会静默丢技能 —— 补一句固定后缀。
                f"【{out['milestone'] or question[:30]}】"
                f"{item.get('question') or question}"
                f"：闭合这个维度时跑通的取数做法",
                code=code,
                source="learn",
                # 维度 id 是精确主键：不许相似度合并（实测把 differences 并进
                # 了叫 def 的老条目，key 与实际维度对不上，面板报的配方名失真）。
                exact_only=True,
                blueprint={"from": "maieutic_milestone",
                           "milestone": out["milestone"],
                           "dimension": did,
                           "question": str(item.get("question") or ""),
                           "scene": scene_id,
                           # 技能归属那次人导入（用户 2026-10-20）：
                           # 「技能的归属也是如此」。面板据此显示
                           # 「这条技能来自 imp_xxx（第 N 次导入）」，
                           # 第二次导入同一问题时掌握度涨的那一分能解释。
                           "import_id": str(import_id or ""),
                           "tools": list(item.get("tools") or [])},
                source_scene=str(scene_id or "A"))
            if key:
                out["added"].append({"key": key, "action": _act, "dim": did})
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
    return out


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
    """闭合**一个**（维度）问题 = 落**一枚**节点 —— 最小技能单位。

    用户 2026-10-19 定：最小技能单位是一个节点（也就是闭合了一个问题）。
    所以粒度是"每闭合一条维度就记账一次"，不是"整个大问题全闭合才记账"。
    幂等（`attach_dimensions` 同 (topic, dim_id) 只建一次，重复只加证据数）。
    """
    tid = str(topic_id or "").strip()
    if not tid:
        return []
    # 父节点先存在（与 ⑥ `_absorb` 同一个 ensure，幂等），否则维度边会挂到
    # 场景根上，和 ⑥ 落的那批父节点不一致。
    try:
        import cold_start
        cold_start.ensure_cold_start_node(tid, question, scene_id)
    except Exception:
        pass
    try:
        import maieutic_scaffold as _ms
        did = str(dim.get("id") or "").strip()
        return list(_ms.attach_dimensions(
            scene_id, tid, [dim], verified=True, bump=bump,
            tools=({did: list(tools or [])} if did and tools else None),
            # 挂到**这次导入**下面：谁长出来的要能追（用户 2026-10-20）
            import_id=str(import_id or ""), import_label=str(question or "")))
    except Exception:
        return []


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
            judge=judge, context=context, import_id=import_id)
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
) -> dict[str, Any]:
    """启动一轮学习（同构 learn() 单圈）的实现体（断开逻辑见外层 start_learning）。"""
    question = str(question or "").strip()
    scene = _resolve_scene(scene_id)
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
    local_dir = str(prof.get("scene_dir") or "") if is_local else ""
    scene_db = "" if (use_vlml or is_local) else str(prof["db_path"])
    db_path = db_path or scene_db          # 调用方显式给的优先
    out["backend"]["vlml"] = use_vlml

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
        _use_scaffold = bool(is_local) and not _tc0.needs_data(question)
    except Exception:
        _use_scaffold = bool(is_local)
    plan = task_decompose.plan(question, scene, llm=llm, db_path=db_path,
                               scaffold=_use_scaffold)
    out["plan"] = plan
    out["plan_origin"] = str(plan.get("origin") or "")
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
                _snm = [str((s or {}).get("name") or (s or {}).get("topic") or "")
                        for s in skills[:6]]
                print(f"  · 已学配方回注执行层：{len(skills)} 条"
                      f"（{'、'.join(x for x in _snm if x)}）", flush=True)
            agent = None
            if use_vlml:
                from voyager_vlml.agents.action import VLMLActionAgent
                agent = VLMLActionAgent()
            elif not is_local:
                # 场景配了自己的库：写 SQL 的 prompt/critic 照旧复用，但**数据面**
                # 只连这个库（SceneDBEnv），不引导 VLML、不读 rib。
                from voyager_vlml.agents.action import VLMLActionAgent
                from scene_db import SceneDBEnv
                agent = VLMLActionAgent(env=SceneDBEnv(prof["db_path"]))
                out["execution_backend"] = f"scene_db:{prof['db_path']}"
            else:
                # 本机场景：**写代码**调本机只读原语（原版 Voyager 就是写代码调
                # control_primitives）。全程不构造 VLML 环境。
                out["execution_backend"] = f"local:{local_dir}"
            # 抓手条目 = 注册表里本场景的**全部**条目（builtin + forged），
            # 同构 MCP tools/list 交给模型。forge 出来的条目描述里带着实查到的
            # 真实列名 —— 不交给它，它就凭 schema 瞎猜 join 键（实测踩过）。
            grabs: list[dict[str, Any]] = []
            try:
                import actions as _act
                grabs = [{"name": e.get("name", ""),
                          "description": str(e.get("description") or "")}
                         for e in _act.enabled_entries(scene)]
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
            out["focus_tables"] = focus[:10]
            _answer_override = ""
            if is_local:
                import local_agent
                # 本机原语先注册成条目（origin=local / group=本机能力），
                # 才谈得上被 `enabled_entries` 当抓手段交给执行层。
                try:
                    _act.register_local(scene, local_dir)
                    _act.register_net(scene)
                except Exception:
                    pass
                try:
                    import maieutic_scaffold as _ms
                    _dims = plan.get("dimensions") or []
                    _avoid = _ms.drift_notes(plan)
                except Exception:
                    _dims, _avoid = [], []
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
                    try:
                        from voyager_vlml.consume import (
                            _runtime_topic_id as _rtid)
                        _tid = str(_rtid(question, scene) or "")
                    except Exception:
                        _tid = ""
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
                    _timed_out_dims = 0
                    for _d in _dims:
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
                        # 原版重试机制（用户 2026-10-19 定，同 voyager.py:30
                        # `action_agent_task_max_retries=4` + :277-279
                        # `done = 迭代数 >= 上限 or success`）：每次把闸门带回的
                        # **追问**当 critique 灌回 prompt（voyager.py:262-268
                        # 迭代提示同款），**到上限仍不闭合就跳过这条维度**
                        # （curriculum.py:342 "Skipping to next task"），
                        # 不原地无限重试。
                        ok1, crit1, obs1, r1 = False, "", [], {}
                        # 追问历史回灌：这条维度上次（上一轮/上一次学这句话）
                        # 被问过什么、还差什么 —— 直接作为第一轮的输入，别让
                        # 它从零开始撞同一堵墙（maieutic priorIterations 同款）。
                        _h = _iters.get(_did) or {}
                        if _h and not _h.get("passed"):
                            crit1 = str(_h.get("asked") or _h.get("gap") or "")
                            if crit1:
                                print(f"  · 追问历史：{_q1[:24]}｜上次问过："
                                      f"{crit1[:70]}", flush=True)
                        # 证据账（同构助产士的 gathered）：每次工具调用成功取回的
                        # 内容 —— 失败项 ok=False，**不进材料**。闸门判的是「材料
                        # 有没有答到维度」，不是「模型写的文本像不像答案」。
                        _ev1: list[dict[str, Any]] = []
                        for _t in range(SUBTASK_MAX_RETRIES):
                            # ⚠️ 第二个返回值是**这一轮跑通的代码**（原版的
                            # `program_code`）。以前写成 `obs1, _ =`，代码直接丢
                            # 掉 → 知识型场景永远学不到技能（2026-10-20 定位）。
                            obs1, code1 = await local_agent.solve(
                                _q1, local_dir, llm=llm, scene_id=scene,
                                avoid=_avoid, critique=str(crit1 or ""),
                                evidence=_ev1,
                                # 已学会的配方回注（原版 retrieve_skills →
                                # system）：技能是新的 primitive，学会之后
                                # 下一轮就更够得着 —— 可达是循环不是判定。
                                skills=skills,
                                # 学习环节：必须真的调数据源取证（原版 rollout
                                # 必须调 primitive；凭记忆答不入库、不算学会）
                                require_tools=True,
                                # ⚠️ 致命点（2026-10-20 实测）：默认 30s 比**单次
                                # MCP 调用的上限 60s**（local_agent.py:680
                                # `cfut.result(timeout=60)`）还短 —— 工具还没
                                # 回来就被判超时，4 次重试因此全部无效。
                                # 原版 rollout 里 bot 执行本来就允许几十秒。
                                timeout=EXEC_TIMEOUT)
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
                            try:
                                import task_critic as _tc1
                                # dimensions=[一个维度] → 走 maieutic_gate 闭合判定
                                ok1, crit1 = _tc1.check_task_success(
                                    _q1, str(crit1 or ""), r1, dimensions=[_d])
                            except Exception as e1:
                                ok1, crit1 = False, f"critic 异常：{type(e1).__name__}: {e1}"
                            if ok1:
                                break
                            # 超时 = 外部工具没回来（慢/被限流），**不是代码写错
                            # 了** —— 原地重试毫无意义（原线程还在等，新线程又
                            # 起），照原版 curriculum.py:342 "Skipping to next
                            # task" 跳过这条维度，等下一轮（断点续学）再来。
                            if any(bool(o.get("timed_out")) for o in obs1):
                                crit1 = (f"上一次执行超时（>{int(EXEC_TIMEOUT)}s，"
                                         "外部工具没回来：慢或被限流）—— 本轮跳过"
                                         "这条维度，不原地重试")
                                print(f"  · 执行超时 → 跳过本条维度（不重试）："
                                      f"{_q1[:28]}", flush=True)
                                _timed_out_dims += 1
                                break
                        if ok1:
                            learned_dims.append(_d)
                            a1 = str(r1.get("answer") or "").strip()
                            if a1:
                                answers.append(f"【{_q1}】{a1[:500]}")
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
                                              "tools": list(tools_map.get(_did)
                                                            or [])})
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
                                "attempts": SUBTASK_MAX_RETRIES,
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
                        # 记一条追问历史（同 maieutic 的 appendPhase1Iteration）：
                        # **成没闭合都要记** —— 下次才知道这题问过、卡在哪一句。
                        _iters[_did] = {
                            "rounds": 1,
                            "asked": str(crit1)[:300],
                            "gap": ("" if ok1 else str(_q1)[:300]),
                            "passed": bool(ok1),
                            "tools": list(tools_map.get(_did) or []),
                        }
                    try:
                        _ms.save_progress(scene, question,
                                          [str(d.get("id") or "")
                                           for d in learned_dims])
                        _ms.save_progress_codes(scene, question, dim_codes)
                        _ms.save_progress_iterations(scene, question, _iters)
                    except Exception:
                        pass
                    out["dimension_results"] = {
                        "total": len(_dims),
                        "learned": [str(d.get("id") or "") for d in learned_dims],
                        "failed": failed_dims,
                    }
                    out["dimension_nodes"] = closed_nodes
                    # ---- 里程碑判定（骨架一次生成，这里只判闭合）----
                    try:
                        _mst = _ms.milestone_state(
                            scene, question,
                            [str(d.get("id") or "") for d in learned_dims],
                            scaffold_result={"dimensions": _dims,
                                             "milestone": plan.get("milestone") or {}})
                        out["milestone_scaffold"] = _mst
                        # 达成了 → 这一轮跑通的代码进技能库（同原版 add_new_skill）
                        # 没全闭合也照样把**已闭合**那几条入库 —— 它们各自是一次
                        # 成功 rollout，不该因为别的维度没闭合就被连坐丢掉。
                        out["skills"] = learn_milestone_skills(
                            scene, _tid, question, _mst, dim_codes,
                            import_id=imp_id)
                    except Exception as e:
                        out["milestone_scaffold"] = {
                            "error": f"{type(e).__name__}: {e}"}
                    observations = observations[-8:]
                    sql = ""
                    _answer_override = "\n\n".join(answers)[:2500]
                else:
                    # 材料账（同构 gathered）：整题一次执行也可能直接交 answer，
                    # 同样把「实际取回的内容」按账收着，供 critic/闸门判定。
                    _ev3: list[dict[str, Any]] = []
                    observations, sql = await local_agent.solve(
                        question, local_dir, llm=llm, scene_id=scene,
                        critique=str(critique or ""),
                        dimensions=_dims, avoid=_avoid,
                        skills=skills,           # 同上：已学配方回注执行层
                        require_tools=True,      # 同维度分支：学习环节必须取证
                        evidence=_ev3)
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
                observations, sql = await agent.solve(
                    question, skills=skills, grabs=grabs, focus_tables=focus,
                    hints=_hint_ctx
                    + [str(s.get("need") or "") for s in plan["subtasks"]])
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
            out["execution"] = {
                "ok": real, "sql": sql,
                "calls": len(observations or []),
                # 「真取到」只数**真的有数据源支撑**的观察：知识型答案（RESULT 是一段
                # 回答、没有任何数据源调用）不该记成"取到数"——否则日志会自欺
                # （2026-10-19 实测：养育问题打出了"真取到=1"）。
                "real_calls": sum(1 for o in (observations or [])
                                  if _has_real([o])
                                  and (o.get("data_sources") or [])),
                "rows": sum(int(o.get("rows") or 0)
                            for o in (observations or []) if o.get("ok")),
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
                 "data": (json.dumps(o.get("data"), ensure_ascii=False)[:600]
                          if not isinstance(o.get("data"), str)
                          else str(o.get("data"))[:600])}
                for o in (observations or [])][-4:]
            if not real:
                # 数据型任务：没取到真数 = 没完成（真空防线，保持）。
                # 知识型任务（怎么养育/如何理解…）：本来就没有数据可取 —— 交给下面
                # 的 critic 用 LLM 判，别一刀切判死（2026-10-19 用户实测踩过）。
                try:
                    import task_critic as _tc
                    _needs = _tc.needs_data(question)
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
    if judge:
        _dres = out.get("dimension_results")
        if _dres is not None:
            # 维度模式：判定已经在**每个维度各自的 rollout 后**做过了
            # （maieutic_gate 的闭合口径）。这里只汇总，不再让 critic 判一遍。
            _failed = _dres.get("failed") or []
            _ok = not _failed
            _crit = " ".join(f"[{f.get('id')}] {str(f.get('critique') or '')}"
                             for f in _failed).strip()[:300]
            out["critic"] = {"success": bool(_ok), "critique": _crit,
                             "gate": {"addressed": _dres.get("learned") or [],
                                      "open": [f.get("id") for f in _failed],
                                      "questions": [str(f.get("critique") or "")
                                                    for f in _failed]}}
        else:
            try:
                import task_critic
                _ok, _crit = task_critic.check_task_success(
                    question, str(context or ""), out)
            except Exception as e:
                _ok, _crit = bool(real), f"critic 异常：{type(e).__name__}: {e}"
            out["critic"] = {"success": bool(_ok), "critique": str(_crit or ""),
                             "gate": out.get("critic_gate") or {}}
        if not _ok:
            out.update(status=(("partial" if (_dres or {}).get("learned")
                                else "failed") if _dres is not None
                               else ("no_data" if not real else "failed")),
                       why=(str(_crit or "") or "闸门/critic 判未完成")[:300])
            _record(out)
            return out

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
    # 维度节点：Maieutic 骨架出的每条维度都挂到图谱上（问题 -has_dimension-> 维度）。
    # 这是「不停学习 → 图谱长大」的机制层：学一个问题 = 长 N 个维度节点，
    # 而不是只多一枚 consume_* 大字节点。幂等，重复学只加证据数。
    dims = plan.get("dimensions") or []
    if dims:
        try:
            import maieutic_scaffold as _ms
            _dres = out.get("dimension_results") or {}
            _ids = set(_dres.get("learned") or [])
            # 只把**真的答到**的维度上图（没答到的等补齐），避免图谱上出现
            # "学会了"但其实是空的节点。
            good = ([d for d in dims if str(d.get("id") or "") in _ids]
                    if _dres else dims)
            # 逐维度闭合时已经落过的（closed_ids）不再挂一次 —— 否则同一轮
            # 同一节点会被记两次证据。
            good = [d for d in good if str(d.get("id") or "") not in closed_ids]
            out["dimension_nodes"] = list(out.get("dimension_nodes") or []) + list(
                _ms.attach_dimensions(
                    scene, tid, good, verified=True,
                    import_id=imp_id, import_label=question,
                    tools={str(d.get("id") or ""): tools_map.get(
                        str(d.get("id") or ""), [])
                        for d in good if tools_map.get(str(d.get("id") or ""))}))
        except Exception as e:
            out["dimension_nodes_error"] = f"{type(e).__name__}: {e}"
    out["milestone"] = {"topic_id": tid, "unlocked": False}
    try:
        import scene_context
        out["milestone"]["unlocked"] = tid in scene_context.unlocked_milestones()
    except Exception as e:
        out["milestone"]["why"] = f"{type(e).__name__}: {e}"

    # ---- ⑦ 写入技能库 → 结晶成条目（闭环最后一环：下次抓得到）----
    try:
        import skill_store
        import plugin_forge
        fresh = ([s for s in skill_store.all_skills()
                  if str(s.get("code") or "") == str(sql)] if str(sql).strip() else [])
        if fresh:
            out["crystallized"] = plugin_forge.crystallize_skill(
                fresh[-1], into_scene=scene,
                local_scene_dir=(local_dir if is_local else ""))
    except Exception as e:
        out["crystallized"] = {"ok": False, "why": f"{type(e).__name__}: {e}"}

    out["status"] = "learned" if out["absorbed"].get("mastered") else "partial"
    if out["status"] == "partial":
        out["why"] = ("取到值并执行了，但图谱标记掌握这步没成"
                      f"（{out['absorbed'].get('skill_action') or '无说明'}）")
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

    def _run() -> None:
        try:
            asyncio.run(start_learning(question, scene_id))
        except Exception as e:  # 后台任务自己兜住，绝不无声死掉
            _record({"question": question, "scene_id": scene_id,
                     "status": "failed", "why": f"{type(e).__name__}: {e}"})

    t = threading.Thread(target=_run, name=f"learn-{question[:16]}",
                         daemon=True)
    _BG[question] = t
    t.start()
    return {"kicked": True, "question": question[:60], "mode": "thread",
            "reason": str(reason or "")}


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
            "learn",
            topic_id=str(result.get("absorbed", {}).get("topic_id") or ""),
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


def last_learn_results(limit: int = 10) -> list[dict[str, Any]]:
    """面板 API 用：最近几轮学习的结果（新的在前）。"""
    return list(reversed(_LAST))[:max(1, int(limit))]


def learn_running() -> list[str]:
    """后台还在跑的问题（面板显示「学着呢」）。"""
    return [q for q, t in _BG.items()
            if getattr(t, "is_alive", lambda: False)()]
