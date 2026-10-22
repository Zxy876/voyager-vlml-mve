#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""学习成果回填（knowledge sink）：把本场景已经学到的知识落成**可查询的表**。

为什么需要（2026-10-22 同构实验定位的机制缺口）
------------------------------------------------
学习期（远端 MCP 开）走 local 形态：模型用 mcp_*/web_search 搜集知识，
成果落在两本账里 —— 技能库（RESULT 代码）与 ckpt_maieutic（维度 question/
answer/code）+ 知识图谱节点。但**本机原语全是只读**，这些知识落不成数据表
→ 关掉远端后 query_sql 无表可查，「会搜题」变不成「会做题」。

本模块 = 学习期账本 → 场景库 的单向回填（幂等）：

    ckpt_maieutic/<scene>/*.json (progress.codes: 维度问答/工具)
    knowledge_graph.json (root_scene==scene 的节点)
        ↓ sink(scene_id)
    scene_data/<scene>/scene_knowledge.duckdb
        ├── learned_facts(dim_id, question, answer, tools, source_question, learned_at)
        └── graph_facts(node_id, name, description, kind, root_scene)

库一旦出现在场景目录，`scene_profile` 下一轮自动判 kind=scene，执行层换成
query_sql —— 「先远端搜集学习、后本机做题」的时序由此闭合。

**注意时序**：学习期目录里不能有库（否则 kind=scene 会把执行层锁死在写 SQL
的形态，MCP 搜集形态被堵死）。所以回填的触发点 = 关掉远端工具的那一刻
（面板按钮 / 实验脚本 / 未来的自动机制都调这里）。

用法：python mve/knowledge_sink.py --scene M
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CKPT = HERE / "ckpt_maieutic"
GRAPH = HERE / "knowledge_graph.json"


def sink(scene_id: str) -> dict[str, Any]:
    """把场景 <scene_id> 的学习成果回填成 scene_knowledge.duckdb（幂等）。"""
    sid = str(scene_id or "").strip()
    out: dict[str, Any] = {"scene": sid, "learned_rows": 0, "graph_rows": 0,
                           "db": "", "files": 0, "errors": []}
    if not sid:
        out["errors"].append("空场景 id")
        return out
    import duckdb
    scene_dir = HERE.parent / "scene_data" / sid
    scene_dir.mkdir(parents=True, exist_ok=True)
    # 场景目录里**已有库**就写进它（dbs[0] 与 scene_profile 的判定同规则，
    # 保证回填的表对 query_sql 立即可见）；没有才新建 scene_knowledge.duckdb。
    existing = sorted(scene_dir.glob("*.duckdb"))
    db = existing[0] if existing else (scene_dir / "scene_knowledge.duckdb")
    out["db"] = str(db)
    con = duckdb.connect(str(db))
    con.execute("""CREATE TABLE IF NOT EXISTS learned_facts(
        dim_id VARCHAR, question VARCHAR, answer VARCHAR, tools VARCHAR,
        source_question VARCHAR, learned_at TIMESTAMP)""")
    con.execute("""CREATE TABLE IF NOT EXISTS graph_facts(
        node_id VARCHAR, name VARCHAR, description VARCHAR, kind VARCHAR,
        root_scene VARCHAR)""")

    # ---- ① 维度问答账（ckpt_maieutic/<scene>/*.json 的 progress.codes）----
    scene_ckpt = CKPT / sid
    if scene_ckpt.exists():
        for f in sorted(scene_ckpt.glob("*.json")):
            out["files"] += 1
            try:
                data = json.loads(f.read_text("utf-8"))
            except Exception as e:
                out["errors"].append(f"{f.name}: {type(e).__name__}: {e}")
                continue
            pr = data.get("progress") or {}
            src_q = str(data.get("question") or data.get("milestone", {})
                        .get("name") if isinstance(
                            data.get("milestone"), dict) else "")[:200]
            src_q = (str(data.get("question") or "") or src_q)[:200]
            for c in (pr.get("codes") or []):
                if not isinstance(c, dict):
                    continue
                did = str(c.get("id") or "").strip()
                ans = str(c.get("answer") or "").strip()
                q = str(c.get("question") or "").strip()
                if not did or not (ans or q):
                    continue
                tools = ",".join(str(x) for x in (c.get("tools") or []))
                # 幂等：同 (source_question, dim_id) 先删后插
                con.execute(
                    "DELETE FROM learned_facts WHERE source_question = ? "
                    "AND dim_id = ?", [src_q, did])
                con.execute(
                    "INSERT INTO learned_facts VALUES (?,?,?,?,?,?)",
                    [did, q[:500], ans[:2000], tools[:200], src_q,
                     time.strftime("%Y-%m-%d %H:%M:%S")])
                out["learned_rows"] += 1

    # ---- ② 图谱节点（root_scene==scene）----
    try:
        import knowledge_graph as _kg
        g = _kg.KnowledgeGraph.load()
        for nid, n in g.nodes.items():
            if getattr(n, "root_scene", "") != sid:
                continue
            con.execute(
                "DELETE FROM graph_facts WHERE node_id = ?", [str(nid)])
            con.execute(
                "INSERT INTO graph_facts VALUES (?,?,?,?,?)",
                [str(nid)[:200], str(getattr(n, "name", "") or "")[:300],
                 str(getattr(n, "description", "") or "")[:2000],
                 str(getattr(n, "kind", "") or "")[:60], sid])
            out["graph_rows"] += 1
    except Exception as e:
        out["errors"].append(f"graph: {type(e).__name__}: {e}")

    con.close()
    return out


def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", required=True)
    a = ap.parse_args()
    r = sink(a.scene)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r.get("db") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
