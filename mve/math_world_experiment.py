#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同构实验 v7（用户 2026-10-22 定稿流程）：

  阶段一（远端 MCP 全开）：导入数学题 → 启动 Voyager 循环学习（不停）。
      **我不建表、不规定它学什么** —— 它学习时自己用远端工具搜集、自己长
      知识图谱与技能。我只盯两本账：技能库（source_scene=M）+ 知识图谱
      （root_scene=M 节点数）。
  时机判断：技能 ≥2 条或到达学习窗上限（MVE_LEARN_PHASE_MAX，默认 720s）。
  阶段二：关掉远端工具（MCP 远端工具 + 联网能力 → off），只留本机/VLML 工具集。
  测试：a) 学习循环继续跑，看新任务的取数轨迹（还搜不搜、能不能做题）；
        b) 消费侧测 Q1（学过的题）与 Q2（换数字的同型新题）—— 会不会做题。

判据（做题 ≠ 搜题）：答案数值正确 + 轨迹零 mcp_*/web_search + 有本机取数动作。
运行（服务器 /root/mve）：.venv/bin/python mve/math_world_experiment.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(HERE), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)
os.chdir(REPO)

OUT = REPO / "exp_math"
OUT.mkdir(exist_ok=True)

SCENE = "M"
Q1 = "已知一次函数的图像经过点(1,2)和(3,6)，求这个一次函数的解析式"
Q2 = "已知一次函数的图像经过点(2,3)和(4,7)，求这个一次函数的解析式"

LEARN_PHASE_MAX = float(os.environ.get("MVE_LEARN_PHASE_MAX") or 720)

T0 = time.time()


def log(*a):
    print(f"[exp T+{time.time() - T0:.0f}s]", *a, flush=True)


def save(name: str, obj) -> None:
    (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1,
                                       default=str), encoding="utf-8")
    log("证据已落盘：", name)


def _scene_skills() -> list[dict]:
    import skill_store
    return [s for s in skill_store.by_scene(SCENE)
            if str(s.get("code") or "").strip()]


def _graph_nodes() -> int:
    """root_scene==M 的图谱节点数（只读观察，不干预）。"""
    try:
        import knowledge_graph as _kg
        g = _kg.KnowledgeGraph.load()
        return sum(1 for n in g.nodes.values()
                   if getattr(n, "root_scene", "") == SCENE)
    except Exception as e:
        log("（图谱节点计数失败：", f"{type(e).__name__}: {e}）")
        return -1


# ---------------------------------------------------------------------------
# P0 建场景（干净；不建任何库 —— 学习时 Voyager 自己长）
# ---------------------------------------------------------------------------
def p0_create_scene() -> dict:
    import scenes
    import shutil
    scenes.switch_scene("A")
    if scenes.get_scene(SCENE):
        scenes.delete_scene(SCENE)
        log("P0：已存在的场景 M 已删除（重建保证干净）")
    sid = scenes.create_scene("数学做题", scene_id=SCENE,
                              description="同构实验：先远端 MCP 自主学习，"
                                          "后只留本机工具测做题")
    try:
        import skill_store as _ss
        _old = _ss.by_scene(SCENE)
        for s in _old:
            _ss.delete_skill(str(s.get("key") or ""))
        if _old:
            log(f"P0：清掉场景 M 遗留旧技能 {len(_old)} 条")
    except Exception as e:
        log("P0：清旧技能失败（如实记）：", e)
    for _d in ("ckpt_curriculum", "ckpt_maieutic"):
        _p = HERE / _d / SCENE
        if _p.exists():
            shutil.rmtree(_p)
            log(f"P0：清掉 {_d}/{SCENE}（课程账/断点账随场景重建）")
    # 场景目录里的**库文件**也必须清（v8 实测：v6 遗留 math_world.duckdb
    # 把 profile 顶成 kind=scene，学习被锁死在写 SQL 形态，MCP 搜集形态
    # 全程没用上）。空目录 = local 形态 = MCP 搜集学习的前提。
    for _db in (REPO / "scene_data" / SCENE).glob("*.duckdb"):
        _db.unlink()
        log(f"P0：清掉场景目录遗留库 {_db.name}")
    # 本地工具集（本机只读原语）注册 —— 「VLML 工具集」在本场景的同构物，
    # 只读，不含任何预置数据：模型拿到的是空工作区 + 一双只读的手。
    import actions
    actions.restore_switches()
    actions.register_local(SCENE, str(REPO / "scene_data" / SCENE))
    actions.register_net(SCENE)
    # 保证远端组是开的（阶段一前提；上一轮实验可能关过）
    actions.set_switch(SCENE, "MCP 远端工具", True)
    actions.set_switch(SCENE, "联网能力", True)
    try:
        import local_agent
        conn = asyncio.run(local_agent.ensure_mcp_tools(SCENE))
        log("P1：MCP 已连", conn.get("servers"))
    except Exception as e:
        log("P1：MCP 连接失败（如实记）：", e)
    out = {"scene_id": sid, "scene_dir": str(REPO / "scene_data" / SCENE),
           "graph_nodes": _graph_nodes()}
    log("P0：场景建好（空工作区，无预置数据）", out)
    save("p0_scene.json", out)
    return out


# ---------------------------------------------------------------------------
# P1 导入 + 循环学习（只观察，不干预）
# ---------------------------------------------------------------------------
def p1_learn_and_watch() -> dict:
    os.environ["MVE_SCENE"] = SCENE
    out: dict = {"import": {}, "watch": [], "skills_at_switch": []}
    import voyager_vlml.consume as consume
    r = asyncio.run(consume.consume(Q1))
    out["import"] = {k: r.get(k) for k in (
        "route_decision", "route_score", "route_node", "learn_kicked",
        "miss_node_created", "topic_id", "judgment_result")}
    log("P1：导入 route=%s score=%s kick=%s" % (
        out["import"].get("route_decision"), out["import"].get("route_score"),
        out["import"].get("learn_kicked")))

    learn_log = open(OUT / "learn_loop.log", "w")
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "scene_learn.py"), "--scene", SCENE,
         "--curriculum", "auto", "--max-iterations", "24", "--max-retries", "4"],
        stdout=learn_log, stderr=subprocess.STDOUT, cwd=str(REPO))
    out["learn_pid"] = proc.pid
    log(f"P1：循环学习已启动 pid={proc.pid}（MCP 全开；学习窗 {LEARN_PHASE_MAX:.0f}s，"
        "只盯技能库与图谱）")

    deadline = time.time() + LEARN_PHASE_MAX
    last = (-1, -1)
    while time.time() < deadline:
        skills = _scene_skills()
        nodes = _graph_nodes()
        if (len(skills), nodes) != last:
            last = (len(skills), nodes)
            out["watch"].append({"t": round(time.time() - T0),
                                 "skills": len(skills), "graph_nodes": nodes})
            log(f"P1：观察 技能={len(skills)} 图谱节点={nodes}")
        if len(skills) >= 2:
            log("P1：技能 ≥2 → 时机到")
            break
        if proc.poll() is not None:
            log("P1：学习循环退出（观察窗内）")
            break
        time.sleep(25)
    out["watch_end"] = {"t": round(time.time() - T0),
                        "skills": len(_scene_skills()),
                        "graph_nodes": _graph_nodes()}
    save("p1_watch.json", out)
    return out, proc


# ---------------------------------------------------------------------------
# P2 关远端工具（学习循环不重启 —— 开关即时生效，下一个任务就是无远端形态）
# ---------------------------------------------------------------------------
def p2_switch_off(proc) -> dict:
    import actions
    r1 = actions.set_switch(SCENE, "MCP 远端工具", False)
    r2 = actions.set_switch(SCENE, "联网能力", False)
    enabled = [e.get("name") for e in actions.enabled_entries(SCENE)]
    # 学习成果回填（用户 2026-10-22 设想的机制化）：学习期账本（ckpt_maieutic
    # 维度问答 + 图谱节点）→ scene_knowledge.duckdb。库一出现，下一轮
    # scene_profile 自动判 kind=scene → 学习/消费全换 query_sql 形态。
    import knowledge_sink
    sk = knowledge_sink.sink(SCENE)
    out = {"switches": {"MCP 远端工具": r1.get("enabled"),
                        "联网能力": r2.get("enabled")},
           "still_enabled": enabled,
           "learn_loop_alive": proc.poll() is None if proc else None,
           "sink": {k: sk.get(k) for k in ("db", "learned_rows",
                                           "graph_rows", "files", "errors")}}
    log("P2：远端已关；本机还开着：", enabled,
        "｜回填库：", out["sink"].get("db"),
        f"（learned={out['sink'].get('learned_rows')} "
        f"graph={out['sink'].get('graph_rows')}）")
    save("p2_switches.json", out)
    return out


# ---------------------------------------------------------------------------
# P3 测试：a) 关远端后的学习轨迹；b) 消费侧做题（Q1 复答 + Q2 泛化）
# ---------------------------------------------------------------------------
def _answer_text(r: dict) -> str:
    parts = [str(r.get("answer") or ""), str(r.get("narrative") or "")]
    for f in r.get("facts") or []:
        parts.append(str(f.get("value") or f.get("fact") or "")
                     if isinstance(f, dict) else str(f))
    parts.append(json.dumps(r.get("observations") or "", ensure_ascii=False))
    return "\n".join(p for p in parts if p)


def _consume_trajectory(r: dict) -> list[str]:
    names = [str(x) for x in (r.get("trajectory") or [])]
    for o in r.get("observations") or []:
        if isinstance(o, dict):
            names += [str(x) for x in (o.get("data_sources") or [])]
    return sorted({n for n in names if n})


def p3_test(proc) -> dict:
    out: dict = {"consume": [], "learn_after_off": {}}
    # a) 关远端后的学习：给循环 4 分钟跑新任务，抓日志里实测调用过的工具
    t_end = time.time() + 240
    marker = (OUT / "learn_loop.log").read_text("utf-8").count("\n") \
        if (OUT / "learn_loop.log").exists() else 0
    while time.time() < t_end:
        if proc is not None and proc.poll() is not None:
            log("P3：学习循环已退出")
            break
        time.sleep(20)
    logtxt = (OUT / "learn_loop.log").read_text("utf-8")
    out["learn_after_off"] = {
        "tail": logtxt[-2500:],
        "remote_tools_seen": sorted(set(re.findall(
            r"mcp_[A-Za-z0-9_]+|web_search|web_fetch", logtxt))),
    }
    log("P3：关远端后的学习轨迹远端工具出现：",
        out["learn_after_off"]["remote_tools_seen"] or "无")
    # b) 消费侧做题
    os.environ["MVE_SCENE"] = SCENE
    import voyager_vlml.consume as consume
    for tag, q, expect in (("Q1 复答", Q1, r"2(\.0)?\s*\*?\s*x(?![-\d])"),
                           ("Q2 同型新题", Q2, r"2(\.0)?\s*\*?\s*x\s*[-−]\s*1")):
        t = time.time()
        r = asyncio.run(consume.consume(q))
        text = _answer_text(r)
        tools = _consume_trajectory(r)
        banned = [n for n in tools if n.startswith("mcp_")
                  or n in ("web_search", "web_fetch", "http_get", "fetch_url")]
        case = {"tag": tag, "question": q, "wall_seconds": round(time.time() - t, 1),
                "answer_excerpt": text[:400], "trajectory": tools,
                "banned_tools_used": banned,
                "sql": str(r.get("sql") or "")[:300],
                "reused": bool(r.get("reused")),
                "judgment_result": str(r.get("judgment_result") or ""),
                "skipped": str(r.get("skipped") or "")[:200],
                "reuse_errors": [str(x)[:160] for x in (r.get("reuse_errors") or [])],
                "answer_correct": bool(re.search(expect, text)),
                "no_remote_tools": not banned}
        out["consume"].append(case)
        log(f"P3：{tag} → 答案对={case['answer_correct']} 轨迹={tools} "
            f"判读={case['judgment_result']} 用时 {case['wall_seconds']}s")
    save("p3_test.json", out)
    return out


def main() -> int:
    log("==== 同构实验 v7 开始：场景 M（数学做题）====")
    p0_create_scene()
    _, proc = p1_learn_and_watch()
    p2_switch_off(proc)
    p3 = p3_test(proc)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
    skills = _scene_skills()
    verdict = {
        "scene": SCENE, "q1": Q1, "q2": Q2,
        "skills_found": [{"key": str(s.get("key") or ""),
                          "code_head": str(s.get("code") or "")[:160]}
                         for s in skills],
        "graph_nodes": _graph_nodes(),
        "consume_cases": [{k: c.get(k) for k in (
            "tag", "answer_correct", "no_remote_tools", "trajectory",
            "judgment_result")} for c in p3.get("consume") or []],
        "verdict": "",
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    ok = all(c.get("answer_correct") and c.get("no_remote_tools")
             for c in p3.get("consume") or [])
    verdict["verdict"] = ("PASS：关掉远端后只凭本机工具/已学能力做出题"
                          if ok else
                          "FAIL：关掉远端后做不出题（见 exp_math/*.json 逐层取证）")
    save("summary.json", verdict)
    log("==== verdict:", verdict["verdict"], "====")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
