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


async def start_learning(
    question: str,
    scene_id: str = "",
    *,
    llm: Callable | None = None,
    db_path: str = "",
    execute: bool = True,
    use_mcp: bool = True,
    critique: str = "",
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
            execute=execute, use_mcp=use_mcp, critique=critique)
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
) -> dict[str, Any]:
    """启动一轮学习（同构 learn() 单圈）的实现体（断开逻辑见外层 start_learning）。"""
    question = str(question or "").strip()
    scene = _resolve_scene(scene_id)
    out: dict[str, Any] = {
        "question": question, "scene_id": scene, "status": "", "why": "",
        "plan": {}, "forged": {}, "exposed": {}, "stuck": [],
        "execution": {}, "absorbed": {}, "crystallized": {}, "milestone": {},
        "started_at": round(time.time(), 1),
    }
    if not question:
        out.update(status="failed", why="空问题，无可学")
        _record(out)
        return out

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
    import task_decompose
    plan = task_decompose.plan(question, scene, llm=llm, db_path=db_path)
    out["plan"] = plan
    if not plan.get("subgoals"):
        out.update(status="failed", why="拆分不出子目标（LLM 不可用且规则兜底也空）")
        _record(out)
        return out
    if is_local:
        # 本机场景：抓手 = 本机只读原语（同构「插件即本机能力」，猫娘同款）。
        # 没有表可匹配，所以不走「相关表」那条判据；也不许退回 rib 的表。
        prim = {"name": "local:list_files/read_file",
                "description": "本机只读原语：list_files / read_file / query_sql"}
        for st in plan["subtasks"]:
            st.update(status="reachable", grab=prim, score=1.0,
                      why="本机只读原语可用（数据源 = 本机）")
        plan["counts"] = {"reachable": len(plan["subtasks"]),
                          "gap": 0, "unreachable": 0}
        out["plan"] = plan
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
            if is_local:
                import local_agent
                # 本机原语先注册成条目（origin=local / group=本机能力），
                # 才谈得上被 `enabled_entries` 当抓手段交给执行层。
                try:
                    _act.register_local(scene, local_dir)
                    _act.register_net(scene)
                except Exception:
                    pass
                observations, sql = await local_agent.solve(
                    question, local_dir, llm=llm, scene_id=scene,
                    critique=str(critique or ""))
            else:
                observations, sql = await agent.solve(
                    question, skills=skills, grabs=grabs, focus_tables=focus,
                    hints=[str(s.get("need") or "") for s in plan["subtasks"]])
            real = _has_real(list(observations or []))
            out["execution"] = {
                "ok": real, "sql": sql,
                "calls": len(observations or []),
                "real_calls": sum(1 for o in (observations or [])
                                  if _has_real([o])),
                "rows": sum(int(o.get("rows") or 0)
                            for o in (observations or []) if o.get("ok")),
            }
            if not real:
                out.update(status="no_data",
                           why="执行层跑通了但没取到真数（空表/真空值）——"
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

    # ---- ⑥ 取到值 → 图谱建点 + 掌握 + 配方入库（复用消费侧同一套账）----
    try:
        import voyager_vlml.consume as _consume
        out["absorbed"] = _consume._absorb(question, sql, "",
                                          scene_id=scene)
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
        fresh = [s for s in skill_store.all_skills()
                 if str(s.get("code") or "") == str(sql)]
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
