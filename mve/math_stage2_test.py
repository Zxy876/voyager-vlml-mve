#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同构实验·阶段二（2026-10-22）：关远端工具，测 Voyager 会不会做题。

阶段一（MVP：scene_learn 循环 24 任务，已完成 11/失败 13）已经真实跑完：
  · 技能库 scene=M 攒了 11 条 SQL 技能（含「从点算斜率/截距」的计算式配方）
  · 图谱 root_scene=M 有 15 个节点
  · 场景目录有 math_world.duckdb（概念/公式/例题点数据，无预存答案列）

本脚本（用户定稿流程的测试段）：
  1. 盘点 + 取证当前账本
  2. 关掉远端工具（MCP 远端工具 / 联网能力 → off），只留本机/VLML 工具集
  3. knowledge_sink：图谱节点等学习成果回填进场景库（对 query_sql 可见）
  4. 消费侧测试：Q1（学过的题，应重放/复用配方）+ Q2（换数字同型新题，泛化）
判据：答案数值正确 + 轨迹零 mcp_*/web_search + 有 query_sql 取数。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
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
T0 = time.time()


def log(*a):
    print(f"[stage2 T+{time.time() - T0:.0f}s]", *a, flush=True)


def save(name, obj):
    (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1,
                                       default=str), encoding="utf-8")
    log("证据已落盘：", name)


def _answer_text(r: dict) -> str:
    parts = [str(r.get("answer") or ""), str(r.get("narrative") or "")]
    for f in r.get("facts") or []:
        parts.append(str(f.get("value") or f.get("fact") or "")
                     if isinstance(f, dict) else str(f))
    parts.append(json.dumps(r.get("observations") or "", ensure_ascii=False))
    return "\n".join(p for p in parts if p)


def _trajectory(r: dict) -> list[str]:
    names = [str(x) for x in (r.get("trajectory") or [])]
    for o in r.get("observations") or []:
        if isinstance(o, dict):
            names += [str(x) for x in (o.get("data_sources") or [])]
    return sorted({n for n in names if n})


def main() -> int:
    import actions
    actions.restore_switches()
    # ---- 1 盘点 ----
    import skill_store
    mine = [s for s in skill_store.by_scene(SCENE)
            if str(s.get("code") or "").strip()]
    inv = [{"key": str(s.get("key") or ""), "topic": str(s.get("topic") or ""),
            "code": str(s.get("code") or "")[:300]} for s in mine]
    log(f"1. 技能库 scene={SCENE}：{len(mine)} 条 SQL 技能")
    save("s2_inventory.json", inv)
    # ---- 2 关远端 ----
    actions.set_switch(SCENE, "MCP 远端工具", False)
    actions.set_switch(SCENE, "联网能力", False)
    enabled = [e.get("name") for e in actions.enabled_entries(SCENE)]
    log("2. 远端已关；本机仍可用：", enabled)
    # ---- 3 学习成果回填 ----
    import knowledge_sink
    sk = knowledge_sink.sink(SCENE)
    log(f"3. 回填：db={sk.get('db')} learned={sk.get('learned_rows')} "
        f"graph={sk.get('graph_rows')} err={sk.get('errors') or '无'}")
    save("s2_sink.json", sk)
    # ---- 4 消费做题测试 ----
    os.environ["MVE_SCENE"] = SCENE
    import voyager_vlml.consume as consume
    out: dict = {"cases": []}
    for tag, q, expect in (("Q1 复答", Q1, r"2(\.0)?\s*\*?\s*x(?![-\d])"),
                           ("Q2 同型新题", Q2, r"2(\.0)?\s*\*?\s*x\s*[-−]\s*1")):
        t = time.time()
        r = asyncio.run(consume.consume(q))
        text = _answer_text(r)
        tools = _trajectory(r)
        banned = [n for n in tools if n.startswith("mcp_")
                  or n in ("web_search", "web_fetch", "http_get", "fetch_url")]
        case = {"tag": tag, "question": q,
                "wall_seconds": round(time.time() - t, 1),
                "answer_excerpt": text[:500], "trajectory": tools,
                "banned_tools_used": banned,
                "sql": str(r.get("sql") or "")[:400],
                "reused": bool(r.get("reused")),
                "used_skills": r.get("used_skills"),
                "judgment_result": str(r.get("judgment_result") or ""),
                "skipped": str(r.get("skipped") or "")[:200],
                "reuse_errors": [str(x)[:200]
                                 for x in (r.get("reuse_errors") or [])],
                "answer_correct": bool(re.search(expect, text)),
                "no_remote_tools": not banned}
        out["cases"].append(case)
        log(f"4. {tag} → 答案对={case['answer_correct']} 轨迹={tools} "
            f"复用={case['reused']} 判读={case['judgment_result']} "
            f"用时={case['wall_seconds']}s")
        log("   答案：", case["answer_excerpt"][:200].replace("\n", " | "))
    # ---- verdict ----
    ok = all(c["answer_correct"] and c["no_remote_tools"]
             and "query_sql" in c["trajectory"] for c in out["cases"])
    out["verdict"] = ("PASS：关掉远端工具后，Voyager 只凭本机 query_sql + 已学"
                      "SQL 配方做出题（含换数字泛化）" if ok else
                      "FAIL：见 exp_math/s2_*.json 逐层取证")
    out["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save("s2_summary.json", out)
    log("==== verdict:", out["verdict"], "====")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
