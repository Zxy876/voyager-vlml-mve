#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph_curriculum —— 课程结构进图谱，规划从图谱读回来。

分工（用户 2026-11-14 口径）
---------------------------
  可写  人预写：书的目录 = 「关」（topic 节点），一次写全
        技能沉淀：学完一题 = 「题」（exercise 节点），随学随长
  可读  规划：current_topic() 回答「下一关打哪个、还差什么」
  落点  任务处理：选出的题交给 maieutic 的 Phase1-3

为什么不是另造一份「背包」文件
------------------------------
背包（Inventory）本质是「环境状态」的一个视图。Voyager 里它是真的物品字典，
MVE 里它没有独立实体 —— **它就是图谱上 topic 节点的完成度**。做成独立 json
就多一本账，两本账必然打架（上一版 math_backpack 就是这么下线的）。

为什么不在图谱里铺 1130 个 exercise 节点
----------------------------------------
题是消耗品，学一题长一个；关是书的固有结构，与有没有学过无关（同构
graph_topics.py:440「与题目的关系是零」）。铺满会让图谱文件膨胀十倍，
而且大部分节点永远 status=unverified，污染检索。

节点 / 边
---------
  topic:<module_id>   kind=topic     label=模块标题
                      detail={module_id, chapter, book, ord, usable_total, per_topic}
  exercise:<eid>      kind=exercise  label=题面前 60 字
                      detail={exercise_id, module_id, req_id, verdict}
  scene:<sid> --has_topic--> topic:<mid>          declared（书的目录）
  topic:<m1> --next--> topic:<m2>                 declared（ord 相邻，不是真前置）
  topic:<mid> --has_exercise--> exercise:<eid>    observed（Phase2 过关）

relation 用 `next` 而不是 `prerequisite`：我们只有书的顺序，没有「不学 A 就
学不懂 B」的证据。写 prerequisite 是编造，等有人预写真依赖时再叠加。

用法
----
    python3 graph_curriculum.py --scene N --seed      # 写（幂等）
    python3 graph_curriculum.py --scene N --progress  # 读（给人看）
    python3 graph_curriculum.py --scene N --json      # 读（给程序用）
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path

# gen:<scene>:<book_id>:<unit_ord>:<seq> 是「没有题库的场景」里模型出的题
# （graph_books.spec 生成），和 fs-id… 一样要能被从题面里抓出来。
EID_RE = re.compile(
    r"(fs-id\d+|ti_\d+_\d+_\d+|Example_\d+_\d+_\d+"
    r"|gen:[A-Za-z0-9_\-]+:[A-Za-z0-9_\-]+:\d+:\d+)")

TOPIC_KIND = "topic"
EXERCISE_KIND = "exercise"

# 刷穿一关要几道 verified。2 = 一道学会、一道确认泛化；再往上收益递减。
PER_TOPIC = int(os.environ.get("MVE_PER_TOPIC") or 2)

# 概念问答/证明题没有可对答案的数值，拆不出计算步骤 —— 视为「不可算」
_STOP_START = ("why", "what", "explain", "describe", "discuss",
               "determine whether", "state whether", "is the", "does the")
_STOP_ANY = ("difference between", "in your own words", "explain why",
             "give an example")


# ---------------------------------------------------------------- 场景路径

def db_path(scene: str) -> str:
    try:
        import scene_profile
        cand = str(scene_profile.profile(scene).get("db_path") or "")
        if cand:
            return cand
    except Exception:
        pass
    return f"/root/mve/scene_data/{scene}/math.duckdb"


def scene_dir(scene: str) -> str:
    try:
        import scene_profile
        d = str(scene_profile.profile(scene).get("scene_dir") or "")
        if d:
            return d
    except Exception:
        pass
    return f"/root/mve/scene_data/{scene}"


# ---------------------------------------------------------------- 可算题口径

def is_computable(problem: str, answer: str) -> bool:
    """这道题能不能拆成可计算步骤、并用 answer_norm 对答案。"""
    prob, ans = str(problem or ""), str(answer or "")
    low = prob.lower().strip()
    if not prob or len(prob.strip()) < 40:
        return False
    if not ans.strip() or len(ans.strip()) < 2:
        return False
    if len(ans) > 80:                        # 答案是一段话 → 不是计算题
        return False
    if any(low.startswith(w) for w in _STOP_START):
        return False
    if any(k in low for k in _STOP_ANY):
        return False
    if not any(ch.isdigit() for ch in prob):  # 题面没数字 → 算不了
        return False
    if "http" in low or "accessed" in low:    # 网址/引用类脏数据
        return False
    return True


def _usable(db: str) -> tuple[Counter, dict[str, str]]:
    """(每关可算题数, exercise_id → module_id)。"""
    import duckdb
    con = duckdb.connect(db, read_only=True)
    try:
        rows = con.execute("""
            SELECT e.module_id, e.exercise_id, min(e.problem), min(a.answer_norm)
            FROM math_exercises e
            JOIN math_answers a ON a.item_id = e.exercise_id
            WHERE a.answer_norm IS NOT NULL
              AND length(trim(a.answer_norm)) >= 2
              AND e.problem IS NOT NULL AND length(trim(e.problem)) >= 40
            GROUP BY 1, 2
        """).fetchall()
    finally:
        con.close()
    per: Counter = Counter()
    eid2mod: dict[str, str] = {}
    for mid, eid, prob, ans in rows:
        if not is_computable(prob, ans):
            continue
        per[str(mid)] += 1
        eid2mod[str(eid)] = str(mid)
    return per, eid2mod


def curriculum(db: str = "", scene: str = "") -> list[dict]:
    """书的目录：每一关一条，按 (ord, module_id) 排序。

    ord 取 math_toc 的 min(ord) —— 同一模块在多本书里重复出现，取最小序。
    没有可算题的模块不算一关（学不了，进了图谱只会变成死格子）。
    """
    db = db or db_path(scene)
    import duckdb
    con = duckdb.connect(db, read_only=True)
    try:
        toc = con.execute("""
            SELECT module_id, min(book_id), min(ord), min(chapter)
            FROM math_toc GROUP BY 1
        """).fetchall()
        titles = {str(r[0]): str(r[1] or "")
                  for r in con.execute(
                      "SELECT module_id, min(title) FROM math_modules GROUP BY 1"
                  ).fetchall()}
    finally:
        con.close()
    per, _ = _usable(db)
    out: list[dict] = []
    for mid, book, ordv, chapter in toc:
        mid = str(mid)
        n = int(per.get(mid, 0))
        if n <= 0:
            continue
        out.append({
            "module_id": mid,
            "title": (titles.get(mid) or mid)[:80],
            "chapter": str(chapter or "")[:60],
            "book": str(book or ""),
            "ord": int(ordv or 9999),
            "usable": n,
        })
    out.sort(key=lambda s: (s["ord"], s["module_id"]))
    for i, s in enumerate(out, 1):
        s["index"] = i
    return out


# ---------------------------------------------------------------- 写：seed

def seed(scene: str, db: str = "", *, save: bool = True) -> dict:
    """把书的目录写进图谱（幂等）。

    重复跑只补缺，不改已有人工字段（Node.add_node 是合并式写入，旧 detail
    优先）—— 人预写的 prerequisites / skills 不会被 seed 冲掉。
    """
    from knowledge_graph import Edge, KnowledgeGraph, Node

    db = db or db_path(scene)
    rows = curriculum(db, scene)
    g = KnowledgeGraph.load()
    g.ensure_scene_root(scene)
    root = f"scene:{scene}"
    new_nodes = new_edges = 0
    for r in rows:
        nid = f"{TOPIC_KIND}:{r['module_id']}"
        existed = nid in (getattr(g, "nodes", {}) or {})
        detail = {
            "module_id": r["module_id"],
            "chapter": r["chapter"],
            "book": r["book"],
            "ord": r["ord"],
            "index": r["index"],
            "usable_total": r["usable"],
            "per_topic": PER_TOPIC,
            "source": "seed:math_modules",
        }
        if existed:
            g.nodes[nid].detail = {**detail, **g.nodes[nid].detail}
            if not g.nodes[nid].label:
                g.nodes[nid].label = r["title"]
        else:
            g.add_node(Node(id=nid, kind=TOPIC_KIND, label=r["title"],
                            detail=detail, root_scene=scene,
                            status="verified", evidence=1))
            new_nodes += 1
        before = len(g.edges)
        g.add_edge(Edge(src=root, dst=nid, relation="has_topic",
                        origin="declared", reason="math_modules 课程结构"))
        new_edges += len(g.edges) - before
    for a, b in zip(rows, rows[1:]):
        before = len(g.edges)
        g.add_edge(Edge(src=f"{TOPIC_KIND}:{a['module_id']}",
                        dst=f"{TOPIC_KIND}:{b['module_id']}",
                        relation="next", origin="declared",
                        reason="math_toc.ord 相邻（书的顺序，不是真前置）"))
        new_edges += len(g.edges) - before
    if save:
        g.save()
    return {"scene": scene, "topics": len(rows), "new_nodes": new_nodes,
            "new_edges": new_edges,
            "total_nodes": len(getattr(g, "nodes", {}) or {}),
            "total_edges": len(getattr(g, "edges", []) or [])}


# ---------------------------------------------------------------- 写：学完一题

def record_solved(scene: str, exercise_id: str, *, module_id: str = "",
                  req_id: str = "", verified: bool = True,
                  problem: str = "", db: str = "", save: bool = True) -> str:
    """学完一题：建 exercise 节点 + 连到它那一关。返回节点 id。

    幂等：同一道题重复学只把 evidence +1（同构 attach_phase_nodes 的口径）。
    verified=False（Phase3 没过）也记，但 status=unverified —— 它不算「已学」，
    只说明「试过」，规划层统计完成度时会跳过它。
    """
    from knowledge_graph import Edge, KnowledgeGraph, Node

    eid = str(exercise_id or "").strip()
    if not eid:
        return ""
    mid = str(module_id or "").strip()
    if not mid and eid.startswith("gen:"):
        # gen:<scene>:<book_id>:<unit_ord>:<seq> → 关就是那个单元
        parts = eid.split(":")
        if len(parts) >= 5:
            mid = f"unit:{parts[2]}:{parts[3]}"
    if not mid:
        try:
            _, eid2mod = _usable(db or db_path(scene))
            mid = eid2mod.get(eid, "")
        except Exception:
            mid = ""
    g = KnowledgeGraph.load()
    g.ensure_scene_root(scene)
    nid = f"{EXERCISE_KIND}:{eid}"
    nodes = getattr(g, "nodes", {}) or {}
    if nid in nodes:
        try:
            nodes[nid].evidence = int(nodes[nid].evidence) + 1
        except Exception:
            pass
        if verified:
            nodes[nid].status = "verified"
        if req_id:
            nodes[nid].detail = {**(nodes[nid].detail or {}), "req_id": req_id}
    else:
        g.add_node(Node(
            id=nid, kind=EXERCISE_KIND,
            label=(str(problem or "").strip()[:60] or eid),
            detail={"exercise_id": eid, "module_id": mid, "req_id": req_id,
                    "verdict": "verified" if verified else "unverified"},
            root_scene=scene,
            status="verified" if verified else "unverified",
            evidence=1))
    # 关节点不存在（没 seed 过）就先补一个最小节点，否则边会被丢弃 ——
    # 悬挂边是 MVE 图谱断边的老毛病（learn_orchestrate.py:2679 那段）。
    if mid:
        tid = f"{TOPIC_KIND}:{mid}"
        if tid not in (getattr(g, "nodes", {}) or {}):
            g.add_node(Node(id=tid, kind=TOPIC_KIND, label=mid,
                            detail={"module_id": mid, "source": "runtime"},
                            root_scene=scene, status="verified", evidence=1))
            g.add_edge(Edge(src=f"scene:{scene}", dst=tid, relation="has_topic",
                            origin="observed", reason="学完一题时补建"))
        g.add_edge(Edge(src=tid, dst=nid, relation="has_exercise",
                        origin="observed",
                        reason=f"Phase2 {'过关' if verified else '未过'}"
                               f"{('（' + req_id + '）') if req_id else ''}"))
    if save:
        g.save()
    return nid


# ---------------------------------------------------------------- 读：规划

def _graph_or_load(graph=None):
    if graph is not None:
        return graph
    from knowledge_graph import KnowledgeGraph
    return KnowledgeGraph.load()


def _learned_map(g, scene: str) -> Counter:
    """每关「已学题数」——只数 verified 的 exercise 节点。"""
    out: Counter = Counter()
    for n in (getattr(g, "nodes", {}) or {}).values():
        if str(getattr(n, "kind", "")) != EXERCISE_KIND:
            continue
        if str(getattr(n, "root_scene", "") or "") != str(scene):
            continue
        if str(getattr(n, "status", "") or "") != "verified":
            continue
        mid = str((getattr(n, "detail", {}) or {}).get("module_id") or "")
        if mid:
            out[mid] += 1
    return out


def progress(scene: str, *, graph=None, per: int = PER_TOPIC) -> list[dict]:
    """每一关的完成度。没有 topic 节点（未 seed）时返回空列表。"""
    g = _graph_or_load(graph)
    learned = _learned_map(g, scene)
    slots: list[dict] = []
    for n in (getattr(g, "nodes", {}) or {}).values():
        if str(getattr(n, "kind", "")) != TOPIC_KIND:
            continue
        if str(getattr(n, "root_scene", "") or "") != str(scene):
            continue
        d = getattr(n, "detail", {}) or {}
        mid = str(d.get("module_id") or n.id.split(":", 1)[-1])
        usable = int(d.get("usable_total") or 0)
        got = int(learned.get(mid, 0))
        # per_topic 优先读节点自己的口径：有题库的场景一关要 2 道，书单场景
        # （graph_books）一单元 1 道就够 —— 它没有「这一关有 13 道题」这回事。
        own = int(d.get("per_topic") or per)
        need = max(1, min(own, usable)) if usable else own
        slots.append({
            "module_id": mid,
            "title": str(getattr(n, "label", "") or mid),
            "chapter": str(d.get("chapter") or ""),
            "book": str(d.get("book") or d.get("book_title") or ""),
            "level": int(d.get("level") or 0),
            "subject": str(d.get("subject") or ""),
            "skipped": bool(d.get("skipped")),
            "ord": int(d.get("ord") or 9999),
            "index": int(d.get("index") or 0),
            "usable": usable,
            "learned": got,
            "need": need,
            "done": got >= need,
        })
    slots.sort(key=lambda s: (s["ord"], s["module_id"]))
    for i, s in enumerate(slots, 1):
        s["index"] = i
    return slots


def current_topic(scene: str, *, graph=None, per: int = PER_TOPIC) -> dict | None:
    """下一关：按书的顺序，第一个没刷穿的。全刷穿时返回 None。"""
    for s in progress(scene, graph=graph, per=per):
        if not s["done"]:
            return s
    return None


def summary(scene: str, *, graph=None, per: int = PER_TOPIC) -> dict:
    slots = progress(scene, graph=graph, per=per)
    cur = next((s for s in slots if not s["done"]), None)
    return {
        "scene": scene,
        "slots": len(slots),
        "covered": sum(1 for s in slots if s["done"]),
        "learned_exercises": sum(s["learned"] for s in slots),
        "usable_total": sum(s["usable"] for s in slots),
        "current": cur,
        "next_index": cur["index"] if cur else None,
    }


def render_text(scene: str, *, graph=None, limit: int = 6,
                per: int = PER_TOPIC) -> str:
    """给规划层（curriculum 的 chests）读的一段文本。"""
    s = summary(scene, graph=graph, per=per)
    if not s["slots"]:
        return ""
    head = (f"课程进度：{s['slots']} 关，已刷穿 {s['covered']} 关，"
            f"已学 {s['learned_exercises']} 题，本场景共 "
            f"{s['usable_total']} 道可算题（一关刷穿 = {per} 道验证过的题）\n")
    slots = progress(scene, graph=graph, per=per)
    cur = s["current"]
    start = max(0, (cur["index"] - 1) if cur else 0)
    window = slots[start:start + max(1, int(limit))]
    lines = []
    for t in window:
        mark = "  ← 当前关" if cur and t["module_id"] == cur["module_id"] else ""
        lines.append(f"[{t['index']:>3}/{len(slots)}] {t['module_id']}  "
                     f"{t['title'][:44]:<44} {t['learned']}/{t['need']}"
                     f"  可算 {t['usable']}{mark}")
    tail = ""
    if cur:
        tail = (f"\n下一关：{cur['module_id']}「{cur['title']}」，"
                f"还差 {max(0, cur['need'] - cur['learned'])} 题"
                f"（本关可算 {cur['usable']} 道）。"
                f"出题就从这一关的可算题里挑。")
    else:
        tail = "\n全部刷穿。"
    return head + "\n".join(lines) + tail


# ---------------------------------------------------------------- CLI

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", default="N")
    ap.add_argument("--db", default="")
    ap.add_argument("--seed", action="store_true", help="把课程结构写进图谱")
    ap.add_argument("--progress", action="store_true", help="打印进度（文本）")
    ap.add_argument("--json", action="store_true", help="打印进度（json）")
    ap.add_argument("--solve", default="", help="记一题已学：exercise_id")
    ap.add_argument("--module", default="", help="配合 --solve 指定 module_id")
    ap.add_argument("--req", default="", help="配合 --solve 指定 req_id")
    a = ap.parse_args()
    if a.seed:
        print(json.dumps(seed(a.scene, a.db), ensure_ascii=False, indent=1))
    if a.solve:
        print(record_solved(a.scene, a.solve, module_id=a.module,
                            req_id=a.req, db=a.db))
    if a.json:
        print(json.dumps({"summary": summary(a.scene),
                          "slots": progress(a.scene)},
                         ensure_ascii=False, indent=1))
    elif a.progress or not (a.seed or a.solve):
        print(render_text(a.scene) or "（图谱里还没有这一场景的课程结构，先 --seed）")


if __name__ == "__main__":
    main()
