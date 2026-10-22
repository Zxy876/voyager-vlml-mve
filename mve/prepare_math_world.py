#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给场景建数学世界库（VLML 颗粒度）：MCP 三轮搜集 → LLM 结构化 → duckdb。

表结构 = v8 实验里学习循环真正攒出 SQL 做题配方的形态（中文表/列名，纯原始
事实，不含任何预存答案列）：
    函数_concepts(概念, 定义, 说明)
    一次函数_formula(公式名, 表达式, 参数说明, 适用条件)
    一次函数_examples(题目, 点1x, 点1y, 点2x, 点2y, 解题步骤)
    —— 另插本题原始点数据行（坐标是事实；k/b 必须由做题的 SQL 自己算）。

用法：.venv/bin/python mve/prepare_math_world.py --scene N
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(HERE), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)
os.chdir(REPO)

Q1 = "已知一次函数的图像经过点(1,2)和(3,6)，求这个一次函数的解析式"
Q2 = "已知一次函数的图像经过点(2,3)和(4,7)，求这个一次函数的解析式"

STRUCT_PROMPT = """你是数学知识工程师。下面是 web_search 搜到的关于一次函数的原始摘要。
把它们结构化成 JSON，三张表（信息颗粒度对齐一个"数据世界"：概念表/公式表/原始
数据点表——**只有原始事实，没有算好的答案**）：

{
 "concepts":  [{"概念": "...", "定义": "...", "说明": "..."}],
 "formulas":  [{"公式名": "...", "表达式": "...", "参数说明": "...", "适用条件": "..."}],
 "examples":  [{"题目": "...", "点1x": 数字, "点1y": 数字, "点2x": 数字, "点2y": 数字,
                "解题步骤": "可复用方法论：先求斜率再求截距，步骤写清楚"}]
}

硬性要求：
1. examples 给 5 道不同数字的例题，但**绝不允许**出现经过 (1,2)(3,6) 或 (2,3)(4,7) 的题。
2. 只存**原始事实**（点坐标、公式、概念），**绝不**预先算出斜率/截距/解析式 ——
   答案必须由做题的人（写 SQL 计算）自己得出。
3. 搜索摘要里没有的数学事实可以补全，但概念/公式必须以搜索内容为纲。
4. 只输出 JSON 对象，不要其他文字。

搜索摘要：
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", required=True)
    a = ap.parse_args()
    sid = a.scene
    t0 = time.time()

    def log(*x):
        print(f"[prepare T+{time.time() - t0:.0f}s]", *x, flush=True)

    # ---- MCP 搜集（三轮换搜索轴）----
    out: dict = {"web_results": [], "errors": []}

    async def _collect():
        import local_agent
        conn = await local_agent.ensure_mcp_tools(sid)
        log("MCP 已连", conn.get("servers"))
        import actions
        actions.restore_switches()
        actions.register_local(sid, str(REPO / "scene_data" / sid))
        actions.register_net(sid)
        actions.set_master(sid, True)
        actions.set_switch(sid, "MCP 远端工具", True)
        actions.set_switch(sid, "联网能力", True)
        for q in ("一次函数 定义 斜率 截距 性质 图像",
                  "一次函数 已知两点 求解析式 方法 步骤",
                  "一次函数 典型例题 求解析式 练习题"):
            try:
                r = await actions.invoke(sid, "mcp_neko_web_search",
                                         query=q, max_results=6)
                out["web_results"].append({"query": q, "results": r})
                log(f"web_search[{q[:14]}…] 完成")
            except Exception as e:
                out["errors"].append(f"web_search({q}): {type(e).__name__}: {e}")
                log("web_search 失败（如实记）", e)

    asyncio.run(_collect())

    import llm_client
    digest = json.dumps(out["web_results"], ensure_ascii=False)[:9000]
    _kw = {"timeout": 240.0, "retries": 4, "max_tokens": 3000}
    s = llm_client.chat_json(
        [{"role": "system", "content": "你是严谨的数学知识工程师，只输出 JSON。"},
         {"role": "user", "content": STRUCT_PROMPT + digest}], **_kw)
    if not s:
        log("LLM 结构化空 → llm_only 兜底")
        s = llm_client.chat_json(
            [{"role": "user",
              "content": STRUCT_PROMPT + "（搜索不可用，凭教科书标准知识）"}],
            **_kw)
    out["evidence"] = "web+llm" if out["web_results"] else "llm_only"

    # ---- 建库 ----
    import duckdb
    scene_dir = REPO / "scene_data" / sid
    scene_dir.mkdir(parents=True, exist_ok=True)
    db = scene_dir / "math_world.duckdb"
    if db.exists():
        db.unlink()
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE 函数_concepts(概念 VARCHAR, 定义 VARCHAR, 说明 VARCHAR)")
    con.execute("CREATE TABLE 一次函数_formula(公式名 VARCHAR, 表达式 VARCHAR, "
                "参数说明 VARCHAR, 适用条件 VARCHAR)")
    con.execute("CREATE TABLE 一次函数_examples(题目 VARCHAR, 点1x DOUBLE, 点1y DOUBLE, "
                "点2x DOUBLE, 点2y DOUBLE, 解题步骤 VARCHAR)")
    for row in s.get("concepts") or []:
        con.execute("INSERT INTO 函数_concepts VALUES (?,?,?)", (
            str(row.get("概念") or ""), str(row.get("定义") or ""),
            str(row.get("说明") or "")))
    for row in s.get("formulas") or []:
        con.execute("INSERT INTO 一次函数_formula VALUES (?,?,?,?)", (
            str(row.get("公式名") or ""), str(row.get("表达式") or ""),
            str(row.get("参数说明") or ""), str(row.get("适用条件") or "")))
    for row in s.get("examples") or []:
        try:
            con.execute("INSERT INTO 一次函数_examples VALUES (?,?,?,?,?,?)", (
                str(row.get("题目") or ""), float(row.get("点1x")),
                float(row.get("点1y")), float(row.get("点2x")),
                float(row.get("点2y")), str(row.get("解题步骤") or "")))
        except (TypeError, ValueError):
            out["errors"].append(f"例题行跳过：{str(row.get('题目'))[:40]}")
    for q, p in ((Q1, (1, 2, 3, 6)), (Q2, (2, 3, 4, 7))):
        con.execute("INSERT INTO 一次函数_examples VALUES (?,?,?,?,?,?)", (
            q, float(p[0]), float(p[1]), float(p[2]), float(p[3]),
            "先用 k=(y2-y1)/(x2-x1) 求斜率，再代入 y=kx 求截距 b=y-kx"))
    tables = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
              for t in ("函数_concepts", "一次函数_formula", "一次函数_examples")}
    con.close()
    out["tables"] = tables
    out["db"] = str(db)
    log("库建好", tables)
    (REPO / "exp_math" / f"prepare_{sid}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
