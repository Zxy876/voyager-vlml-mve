#!/usr/bin/env python3
"""数据库切换：把 VLML 指向另一个 .duckdb 文件。

为什么要有这个：题目注册表里的 SERIES / 队伍名是**写死**的（tasks.py 的
SERIES='2843069'、C9='Cloud9'），换一个库这些常量就全失效。
所以本模块不假装"换库即换题"，而是：

  1. 换库后**立即体检**：能打开吗、几张表、几条事件、
     原题目依赖的那场 series 还在不在。
  2. 体检结果如实报给面板，由人决定要不要顺带清档重来。

体检必须做在切换**之前**——切过去才发现是空库/错库，人已经不知道刚才的数据
去哪了。所以流程是：先 probe(path) 验货 → 人确认 → 才写 db_config.json。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "db_config.json"
# 默认库 = rib.gg（用户 2026-10-13 定：「接回 rib.gg，并设置为默认的库」）。
# 理由：rib 库带 ext_ 层（首血 roundStats / 经济 roundEconomy / 攻防 sideStats），
# 可分析轴比 GRID 单场切片多；GRID 切片 `vlml_events.duckdb` 保留为可选项。
#
# ⚠️ 这个常量是**唯一**的默认库定义。以前 `vlml_env` / `anchor` /
# `entity_catalog` / `open_quiz` / `scene_context` 各自写一份
# `vlml/data/vlml_events.duckdb` 字面量 —— 那正是"db_path 一空就悄悄换库"
# 那类 bug 的土壤（2026-10-13 服务器实测：配置被切空，六个模块里五个
# 静默落回 GRID，只有面板显示还停在 rib）。所以那些兜底全部 import 本常量，
# 不要再各自写死。要换默认库，只改这一行。
DEFAULT_DB = ROOT.parent / "vlml" / "data" / "vlml_rib.duckdb"


def _abs(p: str) -> str:
    """相对路径按仓库根解析（与 vlml_env / anchor / entity_catalog 同口径）。

    ⚠️ 不能按 cwd 解析：`vlml_env` 会把 cwd 切到 `vlml/`，相对路径会被拼两遍
       （2026-10-13 服务器实测：配了 rib 却连不上，报 database does not exist）。
    """
    q = Path(p or "").expanduser()
    return str(q if q.is_absolute() else (ROOT.parent / q).resolve())


def current_path() -> str:
    try:
        obj = json.loads(CONFIG.read_text(encoding="utf-8"))
        p = str((obj or {}).get("db_path") or "")
    except Exception:
        p = ""
    return _abs(p) if p else str(DEFAULT_DB)


def is_default() -> bool:
    try:
        return Path(current_path()).resolve() == DEFAULT_DB.resolve()
    except Exception:
        return True


def write(path: str) -> dict[str, Any]:
    """切换数据库。**只写配置**，不校验 —— 校验走 probe()。

    两个必须守住的点：
    1. **存绝对路径**。相对路径在不同 cwd 下会指向不同的文件（vlml_env 会把
       cwd 切到 vlml/），从源头存绝对就不再有歧义。
    2. **保留配置里的其它字段**。以前是 `{"db_path": p}` 整体覆盖，会把同一份
       配置里的 `anchor`（当前 series/team/map 锚点）连带抹掉 —— 换一次库，
       锚点就得重设一次。
    """
    p = str(path or "").strip()
    stored = _abs(p) if p else ""
    try:
        obj = json.loads(CONFIG.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            obj = {}
    except Exception:
        obj = {}
    obj["db_path"] = stored
    CONFIG.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ⚠️ 锚点必须跟着库一起走 —— 而且要用 refresh()（"在库里就沿用"），
    #   不是保留原值。
    #   保留原值那版（2026-10-13 修的）是为了治「整体覆盖会把 anchor 抹掉」，
    #   但它治过头了：anchor 留在旧库上，于是
    #
    #     db_path = ""                        → GRID 库（只有 Cloud9 / NRG）
    #     anchor  = 1058 / Nongshim RedForce  → rib.gg 的实体
    #
    #   服务器实测后果（2026-10-14）：消费记录里模型原话 ——
    #     "Voyager 计算的是 Cloud9 的回合赢得情况，
    #      而 VLML 计算的是 Nongshim RedForce 的回合赢得情况"
    #   连 schema 探针都被它带崩：`_default_args` 取 SERIES/C9（来自 anchor），
    #   拿 rib 的 1058 去问 GRID 库 → "No data found for series 1058"，
    #   6/10 个工具只探回一条 error。
    #
    #   `anchor.refresh()` 的语义正好两头都照顾到：`pick(path, prefer=旧锚点)`
    #   —— 旧锚点在新库里**还在**就沿用（换库不无谓改题面），不在就按
    #   "回合数最多的那场/队/图"重挑。所以这里无条件 refresh。
    anchor_note = ""
    try:
        import anchor
        before = dict(obj.get("anchor") or {})
        new_anchor = anchor.refresh(stored or str(DEFAULT_DB))
        # ⚠️ 必须**分字段**说，不能一句"旧锚点查不到"带过（2026-10-14 实测）：
        #   切到 rib 时 series 1058 在库里被沿用了、只有 team 变了
        #   （Nongshim RedForce → Karmine Corp，因为 pick 按"该 series 里
        #   回合数最多的队"挑）。整句说成"旧锚点查不到"会让人以为全没沿用，
        #   下次排查就照着错的线索走。
        keys = ("series", "team", "map")
        changed = [k for k in keys
                   if str(before.get(k) or "") != str(new_anchor.get(k) or "")]
        kept = [k for k in keys if k not in changed and str(before.get(k) or "")]
        if not changed:
            anchor_note = "锚点在新库里仍然有效，全部沿用"
        else:
            anchor_note = (f"锚点已跟着新库重算：{'/'.join(changed)} 变了"
                           f"（{before.get(changed[0])} → {new_anchor.get(changed[0])}）")
            if kept:
                anchor_note += f"；{'/'.join(kept)} 在新库里仍在，已沿用"
    except Exception as e:
        anchor_note = f"锚点重算失败（{type(e).__name__}），请手动跑 python mve/anchor.py --refresh"

    return {"ok": True, "db_path": stored or str(DEFAULT_DB),
            "is_default": not bool(stored), "anchor_note": anchor_note}


def probe(path: str) -> dict[str, Any]:
    """验货：这个库能不能用、里面有什么。切换前必须先跑这个。"""
    p = str(path or "").strip() or str(DEFAULT_DB)
    out: dict[str, Any] = {"path": p, "ok": False, "is_default": not bool(path)}
    f = Path(p)
    if not f.exists():
        out["error"] = "文件不存在"
        return out
    if f.suffix.lower() not in (".duckdb", ".db", ".ddb"):
        out["error"] = f"扩展名 {f.suffix} 不像 DuckDB 库（.duckdb / .db / .ddb）"
        return out

    try:
        import duckdb
        con = duckdb.connect(str(f), read_only=True)
    except Exception as e:
        out["error"] = f"打不开：{type(e).__name__}: {e}"[:200]
        return out

    try:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY table_name").fetchall()]
        out["tables"] = tables
        # 事件量：不同 schema 下表名不同，两个都试
        events = None
        for t in ("base_events", "events"):
            if t in tables:
                try:
                    events = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    break
                except Exception:
                    continue
        out["events"] = events

        # 关键的兼容性检查：题目依赖的那场 series 还在不在
        series_ids: list[str] = []
        try:
            from tasks import SERIES
            want = SERIES
        except Exception:
            want = ""
        for t in tables:
            try:
                cols = [r[0] for r in con.execute(
                    f"SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name='{t}'").fetchall()]
                if "series_id" not in cols:
                    continue
                rows = con.execute(
                    f"SELECT DISTINCT CAST(series_id AS VARCHAR) FROM {t} LIMIT 20"
                ).fetchall()
                for r in rows:
                    v = str(r[0])
                    if v and v not in series_ids:
                        series_ids.append(v)
            except Exception:
                continue
        out["series_ids"] = series_ids[:10]
        out["ok"] = bool(tables)
        out["warning"] = ""
        if want and series_ids and want not in series_ids:
            out["warning"] = (
                f"原题依赖的 series {want} 不在这个库里（现有 {', '.join(series_ids[:5])}）"
                " —— 换库后题目里的 series/队伍常量要重配，否则所有题都查不到数据"
            )
        elif want and not series_ids:
            out["warning"] = "没找到任何 series_id 列，无法判断题目的 series 是否还在"
    finally:
        try:
            con.close()
        except Exception:
            pass
    return out


def anchor_status() -> dict[str, Any]:
    """锚点（series / team / map）跟**当前库**对不对得上。

    ⚠️ 为什么单开这个函数（2026-10-14 服务器实测）：
      `db_config.json` 里 `anchor` 和 `db_path` 是**两个独立字段**，
      切库时只写 `db_path`，`anchor` 原样留着。于是会稳定复现这种状态：

        db_path = ""                       → 退回 GRID 默认库
                                             （里面只有 Cloud9 / NRG，
                                              Haven / Corrode / Lotus）
        anchor  = {series: "1058",
                   team: "Nongshim RedForce",
                   map: "Sunset"}          → 这三个全是 **rib.gg 库**的实体

      后果不是报错，而是**悄悄答错**：题目照 anchor 问「Nongshim RedForce」，
      SQL 却在 GRID 库里查。消费记录里模型的原话就是证据：

        "Voyager 计算的是 Cloud9 的回合赢得情况，
         而 VLML 计算的是 Nongshim RedForce 的回合赢得情况"

      所以切库 / 清库之后必须把 anchor 的归属算出来给人看，由人决定重不重设。
      这里**不自动删 anchor** —— 那是人的配置，静默清掉是另一种自欺。
    """
    out: dict[str, Any] = {"anchor": {}, "checked": False, "stale": False,
                           "hint": ""}
    try:
        obj = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return out
    anchor = (obj or {}).get("anchor") or {}
    if not isinstance(anchor, dict) or not anchor:
        return out
    out["anchor"] = anchor

    path = current_path()
    try:
        import duckdb
        con = duckdb.connect(path, read_only=True)
    except Exception as e:
        out["hint"] = f"连不上当前库，无法核对锚点：{type(e).__name__}: {e}"
        return out

    try:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main'").fetchall()]
    except Exception as e:
        con.close()
        out["hint"] = f"读表清单失败：{type(e).__name__}"
        return out

    def _has(cols_needed: tuple[str, ...], value: str) -> bool | None:
        """库里存不存在这个值。`None` = 没有可核对的列（查不了，不等于没有）。"""
        val = str(value or "").strip()
        if not val:
            return None
        for t in tables:
            try:
                cols = [r[0] for r in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name='{t}'").fetchall()]
            except Exception:
                continue
            for c in cols_needed:
                if c not in cols:
                    continue
                try:
                    n = con.execute(
                        f"SELECT COUNT(*) FROM {t} "
                        f"WHERE CAST({c} AS VARCHAR) = '{val}'").fetchone()[0]
                except Exception:
                    continue
                if n:
                    return True
        return False

    try:
        series_in = _has(("series_id",), str(anchor.get("series") or ""))
        team_in = _has(("team_name", "winning_team_name"),
                       str(anchor.get("team") or ""))
        map_in = _has(("map_name",), str(anchor.get("map") or ""))
        out["checked"] = True
        out["series_in_db"] = series_in
        out["team_in_db"] = team_in
        out["map_in_db"] = map_in
        # 只对"查得到结论"的字段判陈旧：None（没列可查）不算陈旧
        judged = [v for v in (series_in, team_in, map_in) if v is not None]
        out["stale"] = bool(judged) and not any(judged)
        if out["stale"]:
            out["hint"] = (
                f"锚点 {anchor.get('team')} / {anchor.get('map')} / series "
                f"{anchor.get('series')} 在当前库里都查不到 —— "
                "题目会照锚点问，SQL 却在空库里查，答案是 0 而不是报错。"
                "请重设锚点（面板「数据源与数据库」）或切回对应的库。"
            )
        elif judged and not all(judged):
            miss = [k for k, v in (("series", series_in), ("team", team_in),
                                   ("map", map_in)) if v is False]
            out["hint"] = "锚点部分对不上（查不到：" + "、".join(miss) + "）"
    finally:
        con.close()
    return out


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        print(json.dumps(probe(sys.argv[2] if len(sys.argv) > 2 else ""),
                         ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "set":
        print(json.dumps(write(sys.argv[2] if len(sys.argv) > 2 else ""),
                         ensure_ascii=False))
    else:
        print(json.dumps({"current": current_path(), "is_default": is_default(),
                          "probe": probe("")}, ensure_ascii=False, indent=2))
