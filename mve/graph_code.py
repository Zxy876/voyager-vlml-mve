#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph_code —— 场景 S（MVE 学 MVE）的锚点图谱第二支柱：AST import 依赖边。

和 graph_books 的分工
--------------------
graph_books 种「书单声明」（书→单元，declared）；本模块种**源码事实**：
单元的 files 里 `import` 了哪些模块，而那些模块又落在哪个单元 →
    topic:unit:A --depends_on--> topic:unit:B   declared（AST 取证）

为什么用 AST 不用 LLM：依赖关系是机械事实，ground truth = 语法树，
判据可机检（论文「判据通用性」的证据：同一个判据哲学，材料面用 SQL 取数、
代码面用 AST/grep，都不让 LLM 当裁判）。

学习循环怎么消费这些边（用户 2026-11-17 执行门拍板）：
  学生带**全权限**在真仓库探索（读文件、grep、python 真跑），**必然自己出题**
  —— depends_on 边就是出题的抓手：学 skill_store 前先看它依赖什么/被谁依赖，
  学生自己提议的题顺着边找到可达的取证路径。

用法
----
    python3 graph_code.py --scene S --seed      # 种/刷新 depends_on 边（幂等）
    python3 graph_code.py --scene S --show      # 列出已种边的明细
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE  # 本仓库根（graph_code.py 与被读模块同层）


def _repo_root(scene: str = "S") -> Path:
    """取材仓库根：场景声明优先（S 的 data_source = GitHub 完整仓库克隆），
    兜底本模块所在目录（生产目录）。完整仓库的模块在 mve/ 子目录。"""
    try:
        import scenes as _s
        ds = str((_s.get_scene(str(scene or "S")) or {}).get("data_source") or "").strip()
        if ds:
            p = Path(ds)
            if p.exists():
                sub = p / "mve"
                if (sub / "skill_store.py").exists():
                    return sub
                if (p / "skill_store.py").exists():
                    return p
    except Exception:
        pass
    return HERE


REPO = _repo_root()


def _module_of(fname: str, repo: Path | None = None) -> str:
    """skill_store.py → skill_store（只认取材仓库同层的 .py 模块）。"""
    repo = repo or REPO
    f = str(fname or "").strip()
    if f.endswith(".py"):
        f = f[:-3]
    if (repo / f"{f}.py").exists() or (repo / f / "__init__.py").exists():
        return f
    return ""


def _imports_of(pyfile: Path, repo: Path | None = None) -> list[str]:
    """一个 .py 的顶层 import 模块名集合（ast 解析，解析失败如实返回空）。"""
    repo = repo or REPO
    mods: set[str] = set()
    try:
        tree = ast.parse(pyfile.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add((a.name or "").split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return sorted(m for m in mods if m and _module_of(f"{m}.py", repo))


def _scene_units(scene: str) -> dict[str, dict]:
    """scene 的全部单元节点：tid -> {label, files, module_id}。"""
    from knowledge_graph import KnowledgeGraph

    g = KnowledgeGraph.load()
    out: dict[str, dict] = {}
    for nid, nd in (getattr(g, "nodes", {}) or {}).items():
        if str(getattr(nd, "kind", "")) != "topic":
            continue
        if str(getattr(nd, "root_scene", "") or "") != str(scene):
            continue
        d = getattr(nd, "detail", {}) or {}
        if not str(d.get("module_id") or "").startswith("unit:ms-"):
            continue  # 只处理 MVE 自读书单的单元
        out[nid] = {"label": str(getattr(nd, "label", "") or ""),
                    "files": [str(f) for f in (d.get("files") or [])],
                    "module_id": str(d.get("module_id") or "")}
    return out


def seed(scene: str = "S", *, save: bool = True) -> dict:
    from knowledge_graph import Edge, KnowledgeGraph

    units = _scene_units(scene)
    if not units:
        return {"scene": scene, "error": "no ms-* units in graph; run graph_books --seed first"}

    # 文件 → 覆盖它的单元（一个文件只归属一个单元：books_ms.json 里没重复）
    file2unit: dict[str, str] = {}
    repo = _repo_root(scene)
    for tid, u in units.items():
        for f in u["files"]:
            m = _module_of(f, repo)
            if m:
                file2unit[m] = tid

    g = KnowledgeGraph.load()
    new_edges = 0
    unresolved: list[str] = []   # import 了但没人覆盖的模块（单元清单外的真模块）
    detail_rows: list[dict] = []
    for tid, u in sorted(units.items()):
        for f in u["files"]:
            fp = repo / f
            if not fp.exists():
                unresolved.append(f"{tid}:{f} (文件不存在)")
                continue
            for mod in _imports_of(fp):
                dst = file2unit.get(mod, "")
                if not dst:
                    unresolved.append(f"{f} -> {mod}")
                    continue
                if dst == tid:
                    continue  # 同一单元内部依赖，不成边
                before = len(g.edges)
                g.add_edge(Edge(src=tid, dst=dst, relation="depends_on",
                                origin="declared",
                                reason=f"AST import：{f} imports {mod}"))
                if len(g.edges) > before:
                    new_edges += 1
                    detail_rows.append({"src": tid, "dst": dst, "file": f, "module": mod})

    if save:
        g.save()
    return {"scene": scene, "units": len(units), "files": len(file2unit),
            "new_depends_on_edges": new_edges,
            "unresolved": unresolved[:40], "unresolved_count": len(unresolved),
            "total_nodes": len(getattr(g, "nodes", {}) or {}),
            "total_edges": len(getattr(g, "edges", []) or {})}


def show(scene: str = "S") -> None:
    from knowledge_graph import KnowledgeGraph

    g = KnowledgeGraph.load()
    units = _scene_units(scene)
    label = {tid: u["label"] for tid, u in units.items()}
    rows = []
    for e in getattr(g, "edges", []) or []:
        if getattr(e, "relation", "") != "depends_on":
            continue
        if str(getattr(e, "origin", "")) != "declared":
            continue
        rows.append(f"{label.get(e.src, e.src)}  --depends_on-->  {label.get(e.dst, e.dst)}"
                    f"   [{getattr(e, 'reason', '')}]")
    print(json.dumps({"scene": scene, "edges": len(rows), "detail": sorted(rows)},
                     ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description="MVE 自读：AST import 依赖边")
    ap.add_argument("--scene", default="S")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    if a.seed:
        print(json.dumps(seed(a.scene), ensure_ascii=False, indent=1))
    if a.show:
        show(a.scene)


if __name__ == "__main__":
    main()
