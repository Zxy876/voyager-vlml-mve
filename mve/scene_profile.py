#!/usr/bin/env python3
"""场景 → 学习后端（数据源）的唯一判定处。

用户 2026-10-19 的两条硬要求（本模块就是为它们存在的）
----------------------------------------------------
1. 「启动 voyager 学习时场景隔离，只学习当前场景」
2. 「而且在别的场景下，voyager 的学习和 VLML 没有关系」

所以学习**不能**默认落回 VLML / rib 库。判据只有一条：这个场景自己的数据源是什么。

    kind = "vlml"    Scene A（默认场景）：VLML 数据域，用当前 VLML 库
    kind = "scene"   场景显式配了 data_source（或场景目录里有 .duckdb）：只读那个库
    kind = "local"   其它场景：**本机**就是数据源（场景目录里的文件/本机原语），
                     不需要用户配任何库（对照猫娘：插件即本机能力，没有"配库"这一步）
    kind = "missing" 配了路径但文件不存在

为什么非默认场景不「继承默认库」，而是走本机
--------------------------------------------
`scenes.py` 的老注释写的是「data_source 为空 = 继承默认库」。那是 1→n 之前只有
一个场景时的写法。实测（2026-10-19，Scene B）：「现在最前沿的世界模型是什么」
在 Scene B 导入后，取数走的是 rib.gg 的 Valorant 表，答案里列出一串
G2 Esports / LOUD 之类的战队名 —— 场景隔离在图谱和技能上成立了，**在数据源上
根本不成立**。

用户 2026-10-19 进一步指出：「对于普通用户而言，数据源就是本机不是吗」——
所以非 VLML 场景不是"没有数据源"，而是**数据源 = 这台电脑**：场景目录里的
文件（csv/json/duckdb）+ 本机只读原语（list_files / read_file / query_sql）。
不需要用户先配一个库（对照猫娘：app_launcher / lifekit / music 这些插件即本机
能力，用户从来没"配数据源"这一步）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

VLML = "vlml"
SCENE = "scene"
LOCAL = "local"
NONE = "none"          # 保留：判定异常时的兜底，不再用于「没配库」
MISSING = "missing"

# 场景目录（本机数据落这里；不用配置，建场景即有）
SCENE_DATA_DIR = Path(__file__).resolve().parent.parent / "scene_data"


def _scene(sid: str) -> dict[str, Any]:
    try:
        import scenes
        return dict(scenes.get_scene(sid) or {})
    except Exception:
        return {}


def _default_scene_id() -> str:
    try:
        import scenes
        return str(scenes.DEFAULT_SCENE_ID or "A")
    except Exception:
        return "A"


def profile(scene_id: str = "") -> dict[str, Any]:
    """返回 {scene_id, kind, db_path, tables, why}。kind 见模块头。

    `db_path` 只对 kind=vlml/scene 有值；vlml 取**当前** VLML 库
    （db_switch.current_path()，面板可切），scene 取场景自己配的路径。
    """
    sid = str(scene_id or "").strip()
    if not sid:
        try:
            import scenes
            sid = str(scenes.current_scene_id() or _default_scene_id())
        except Exception:
            sid = _default_scene_id()
    sc = _scene(sid)
    name = str(sc.get("name") or "")
    out: dict[str, Any] = {"scene_id": sid, "kind": NONE, "db_path": "",
                           "tables": [], "why": "", "scene_name": name}

    if sid == _default_scene_id():
        try:
            import db_switch
            p = str(db_switch.current_path() or "")
        except Exception as e:
            out["why"] = f"取当前 VLML 库失败：{type(e).__name__}: {e}"
            return out
        out.update(kind=VLML, db_path=p,
                   why="默认场景（VLML 数据域）：用当前 VLML 库")
        out["tables"] = _tables(p)
        return out

    ds = str(sc.get("data_source") or "").strip()
    if not ds:
        # 本机就是数据源：场景目录里有 .duckdb 就用它；否则给本机文件原语。
        d = scene_dir(sid)
        dbs = sorted(Path(d).glob("*.duckdb")) if d else []
        if dbs:
            out.update(kind=SCENE, db_path=str(dbs[0]),
                       why=f"本机场景目录里的库：{Path(dbs[0]).name}")
            out["tables"] = _tables(str(dbs[0]))
        else:
            out.update(kind=LOCAL, db_path="", scene_dir=str(d),
                       why="本机（场景目录里还没有 .duckdb：先用本机文件原语学）")
            out["tables"] = []
        return out

    try:
        import db_switch
        p = str(db_switch.resolve(ds))
    except Exception:
        p = ds
    if not Path(p).exists():
        out.update(kind=MISSING, db_path=p,
                   why=f"场景数据源不存在：{p}")
        return out
    out.update(kind=SCENE, db_path=p, scene_dir=str(scene_dir(sid)),
               why=f"本场景自己的数据源：{Path(p).name}（不经 VLML）")
    out["tables"] = _tables(p)
    return out


def scene_dir(scene_id: str) -> str:
    """场景的本机目录（不存在就建）：普通用户的"数据源"就落在这里。"""
    d = SCENE_DATA_DIR / str(scene_id or "A")
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return str(d)


def _tables(db_path: str) -> list[str]:
    try:
        import db_switch
        return list(db_switch.list_tables(db_path))
    except Exception:
        return []


def is_vlml(scene_id: str = "") -> bool:
    return profile(scene_id)["kind"] == VLML


if __name__ == "__main__":  # pragma: no cover
    import json
    import sys
    sid = sys.argv[1] if len(sys.argv) > 1 else ""
    print(json.dumps(profile(sid), ensure_ascii=False, indent=1))
