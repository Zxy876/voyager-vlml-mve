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

    # 课程场景（书单）：学习内容是教材的单元，不是 SQL 数据。
    # 2026-11-14 实测事故：P 场景目录里被 ensure_source 自动建了个空
    # scene.duckdb，于是走到下面的 glob 分支判成 kind=scene，出题器拿着
    # 「SQL 库/表名/真实取值」那套约束，连出「数据库有多少张表」。
    # 课程场景必须在这里截断，既不分配库，也不读目录里任何 .duckdb。
    if bool(sc.get("course")):
        out.update(kind=LOCAL, db_path="", scene_dir=str(scene_dir(sid)),
                   why="课程场景（书单）：不查库、不读数据文件，题从当前单元出")
        out["tables"] = []
        return out

    ds = str(sc.get("data_source") or "").strip()
    if not ds:
        # 本机就是数据源：场景目录里有 .duckdb 就用它；否则给本机文件原语。
        d = scene_dir(sid)
        dbs = sorted(Path(d).glob("*.duckdb")) if d else []
        if dbs:
            # ⚠️ scene_dir **必须带上**（2026-11-03 场景 J 实测）：这个分支
            # 只给了 db_path，profile["scene_dir"] 于是恒为 None —— 而学习侧
            # `learn_orchestrate.py:695` 和消费侧 `consume.py:1311` 都读
            # `prof.get("scene_dir")`，拿到 None → 本机原语（read_file/
            # list_files）不注册 → 上传的文档（uploads/ 下的 sidecar）谁都
            # 读不到 → 学习器只能把联网缓存的 web_docs 当材料结晶。
            # 场景目录本来就是这个场景的「数据面」（upload.py 也把文件落这里），
            # 与 kind=scene 不冲突：库仍是唯一可查的库，文件只是只读原语。
            out.update(kind=SCENE, db_path=str(dbs[0]), scene_dir=str(d),
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
        # 2026-10-28 改：配了路径但不存在**不再判 missing 空转**（日志实证：
        # 场景 G 配的 /root/mve/心悦文集 不存在 → 每轮 unreachable，160 轮
        # 跑满、0 个里程碑）。改成降级到「给这个场景分配的数据源」，并把这件
        # 事写进 why 让人看得见（不假装配置有效）。
        alt = ensure_source(sid)
        out.update(kind=SCENE, db_path=alt, scene_dir=str(scene_dir(sid)),
                   source_missing=True,
                   why=(f"你配的数据源 {p} 不存在 —— 已先给本场景分配标准库 "
                        f"{Path(alt).name}；对话上传的文件会存这里，"
                        f"要改用别的库请把 data_source 指到存在的文件"))
        out["tables"] = _tables(alt)
        return out
    out.update(kind=SCENE, db_path=p, scene_dir=str(scene_dir(sid)),
               why=f"本场景自己的数据源：{Path(p).name}（不经 VLML）")
    out["tables"] = _tables(p)
    return out


SOURCE_DB_NAME = "scene.duckdb"


def ensure_source(scene_id: str) -> str:
    """给场景**分配**数据源：<场景目录>/scene.duckdb（没有就建空库）。

    用户 2026-10-28：「给每个场景分配一个数据源，会存放我们对话传上来的
    东西，检索逻辑一致」。

    于是「场景有没有数据源」不再是用户要回答的问题 —— 每个场景**都有**一个，
    位置固定、名字固定。对话里上传的表格落进它，学习/消费/出题/判官读的也是
    它，四条路径看同一个库（检索逻辑一致）。

    优先级：
      ① 场景显式配了 data_source 且文件真存在 → 用户说了算，不另建；
      ② 场景目录里已经有库（早期 uploads.duckdb / 手工放的 .duckdb）→ 复用，
         不制造第二个库；
      ③ 都没有 → 建 scene.duckdb（空库也算「有数据源」，只是还没表）。

    ⚠️ 空库 ≠ 能学习：库里没有表就像空房间，出不了题。调用方要按
    `profile()["tables"]` 判，并如实提示「上传文件或放数据进来」。
    """
    sid = str(scene_id or "").strip()
    d = Path(scene_dir(sid))
    sc = _scene(sid)
    # 课程场景本来就不该有数据源：不建库、不复用目录里的库，返回空串。
    # 否则每次面板启动都会把这个空 scene.duckdb 造回来，profile 又判成
    # kind=scene（2026-11-14：删了三次，每次重启都重生）。
    if bool(sc.get("course")):
        return ""
    ds = str(sc.get("data_source") or "").strip()
    if ds:
        try:
            import db_switch
            p = str(db_switch.resolve(ds))
        except Exception:
            p = ds
        if Path(p).exists():
            return p
    # 已有库就复用（别制造两个：老场景可能叫 uploads.duckdb / payroll.duckdb）
    # 选法固定可解释：① 标准名 scene.duckdb 优先；② 否则取**最近改动**的那个
    # （刚上传/刚放进去的才是当前在用的）；不按字母序瞎挑。
    dbs = list(d.glob("*.duckdb")) if d else []
    if dbs:
        std = d / SOURCE_DB_NAME
        if std.exists():
            return str(std)
        try:
            dbs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        except Exception:
            dbs.sort()
        return str(dbs[0])
    target = d / SOURCE_DB_NAME
    try:
        import duckdb
        con = duckdb.connect(str(target))       # 建空库（只建文件，不建表）
        con.close()
    except Exception:
        pass                                     # 建不出来就让上层如实报
    return str(target)


def source_tables(scene_id: str) -> list[str]:
    """这个场景数据源里现在有哪些表（空 = 还没有数据）。

    课程场景没有数据源（ensure_source 返回空串）——此时必须直接返回空表，
    不能把空串丢给 db_switch：那会解析成**当前 VLML 库**，让 P 凭空看到
    rib 的 players/web_facts，等于拿别的场景的数据冒充自己的。
    """
    p = ensure_source(scene_id)
    if not str(p or "").strip():
        return []
    return _tables(p)


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


_GROUND_CACHE: dict[str, bool] = {}


def grounded(scene_id: str = "") -> bool:
    """这个场景有没有**可查的外部材料**（库 / 数据文件 / 联网源）。

    判的是环境事实，不是学科：问的是「这道题的真值能不能靠查拿到」，而不是
    「这是不是数学题」。没有外部材料时，真值只能靠推导 —— 任何「去数据源
    取数」的要求都不成立，Phase2 交的代码也就没有 query_sql 可调。

    判据顺序（先看显式声明，再猜目录，不靠学科标签）：
      ① scenes.json 的 `grounded`（true/false）—— 场景自己说了算；
      ② 有没有配 data_source；
      ③ 场景目录里有没有数据文件（.duckdb/.db/.sqlite/.csv/.xlsx/.json/.parquet）；
      ④ 旧标记 `course: true` 仍然等于「无外部材料」（向后兼容，语义已泛化）。
    """
    sid = str(scene_id or "").strip()
    if not sid:
        return True
    if sid in _GROUND_CACHE:
        return _GROUND_CACHE[sid]
    # VLML 场景（A）：材料面就是 VLML 库，一直是"有材料"，不看目录
    try:
        if is_vlml(sid):
            _GROUND_CACHE[sid] = True
            return True
    except Exception:
        pass
    sc = _scene(sid)
    val = sc.get("grounded")
    if isinstance(val, bool):
        _GROUND_CACHE[sid] = val
        return val
    if str(sc.get("data_source") or "").strip():
        _GROUND_CACHE[sid] = True
        return True
    ok = False
    d = Path(scene_dir(sid))
    try:
        if d.is_dir():
            ok = any(f.is_file() and f.suffix.lower() in (
                ".duckdb", ".db", ".sqlite", ".sqlite3", ".csv", ".tsv",
                ".xlsx", ".parquet", ".json") for f in d.iterdir())
    except Exception:
        ok = False
    if not ok and bool(sc.get("course")):
        ok = False
    _GROUND_CACHE[sid] = ok
    return ok


def is_course(scene_id: str = "") -> bool:
    """「本题不依赖外部材料」—— 保留旧名，语义已泛化（旧注释叫"课程场景"）。

    = `not grounded()`。为什么留这个名字：11 处调用点问的都是同一件事
    「这个场景要不要去数据源取数」。判据已改成环境事实（见 `grounded`），
    不再是"这个场景是不是数学/是不是书单"。
    """
    return not grounded(scene_id)


def is_vlml(scene_id: str = "") -> bool:
    return profile(scene_id)["kind"] == VLML


if __name__ == "__main__":  # pragma: no cover
    import json
    import sys
    sid = sys.argv[1] if len(sys.argv) > 1 else ""
    print(json.dumps(profile(sid), ensure_ascii=False, indent=1))
