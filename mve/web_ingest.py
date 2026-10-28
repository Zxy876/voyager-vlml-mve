#!/usr/bin/env python3
"""联网资料入库 —— 把「工具线取回的 claim」落进本场景数据源。

用户 2026-10-28 定方向：
  「mve 同构 deepseek 只有消费侧用联网。我想到的就是 mve 把联网之后的资料
   都存进数据源。自己拼一条【学习 × 消费 = 回答】的时间回路，人在回路。」

为什么必须落库（不是"顺手存一下"）：

· **限流是真的**。实测一次 16 条串行搜索，智谱 web-search-pro 直接 429、
  DuckDuckGo 也限流，只回来 2 条。不落库 = 每次都要重新撞墙，题目越复杂
  越取不全。
· **claim 只有落库才能升级 fact**。联网取回的东西"有真值但未知"（信息三级
  里的 claim）；它进不了技能库、不计学习曲线。一旦落进数据源并被**人确认**，
  它就成了可复现的事实 —— 下次同类问题不用联网，直接查库（学习线）。
· **这才是回路**：消费时联网取 claim → 落库 → 人确认 → 学习侧把这张表当
  数据源学出技能 → 下次消费直接答（fact，计入曲线）。工具功劳只算一次，
  学习功劳从第二次开始。

## 存哪、存成什么样（2026-10-28 实测后定的分层）

数据源（`<场景目录>/scene.duckdb`，与上传/学习/消费同一个库）只放**能查的东西**：

  web_docs   联网取回的**原始资料**（query / title / url / snippet / 时间）
  web_facts  **按属性摊平的宽表**：item / source_url / confidence /
             confirmed / <属性名…>（列名从数据里来，不写死业务词）

账本（`<场景目录>/web_ledger.duckdb`，独立文件）放**审计用的 EAV**：

  web_evidence  item / attribute / value / source_url / confidence /
                confirmed / confirmed_by / question / method / fetched_at

⚠️ 为什么账本要搬出去：第一版把 EAV 直接放数据源里，学习侧学出来的 SQL 是
`SELECT attribute AS singer ... GROUP BY singer` —— 它把 attribute（字面量
「歌手」）当成了歌手名，GROUP BY 一个常量列，取到 1 行（歌手, 16）还"有值"，
被判完成；重学一次还是同一条错配方。根因不是模型笨，是**这张表不像人整理
出来的表**。账本不该是学习目标，所以搬到数据源之外，只留摊平后的宽表。

`confirmed` 是人在回路那一刀：False = claim（只能标注着答），True = fact
（可以进学习线、按可复现答）。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

DOCS = "web_docs"
EVID = "web_evidence"
FACTS = "web_facts"
LEDGER = "web_ledger.duckdb"


def _scene_dir(scene_id: str) -> str:
    import scene_profile
    prof = scene_profile.profile(scene_id) or {}
    d = str(prof.get("scene_dir") or "")
    if d:
        return d
    try:
        return str(scene_profile.scene_dir(scene_id))
    except Exception:
        return str(Path(scene_profile.ensure_source(scene_id)).parent)


def _db(scene_id: str) -> str:
    """本场景数据源（与上传/学习/消费同一个库 —— 检索逻辑一致）。"""
    import scene_profile
    return scene_profile.ensure_source(scene_id)


def _ledger(scene_id: str) -> str:
    """审计账本（EAV）。**不放进数据源** —— 它不是学习目标，见模块头。"""
    return str(Path(_scene_dir(scene_id)) / LEDGER)


def _connect(db_path: str, write: bool = False):
    import duckdb
    return duckdb.connect(db_path, read_only=not write)


def ensure(db_path: str) -> None:
    """数据源侧建表（幂等）。"""
    con = _connect(db_path, write=True)
    try:
        con.execute(f"""
            CREATE TABLE IF NOT EXISTS {DOCS} (
                query      VARCHAR,
                title      VARCHAR,
                url        VARCHAR,
                snippet    VARCHAR,
                fetched_at TIMESTAMP
            )""")
    finally:
        con.close()


def _ensure_ledger(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = _connect(path, write=True)
    try:
        con.execute(f"""
            CREATE TABLE IF NOT EXISTS {EVID} (
                id          VARCHAR,
                question    VARCHAR,
                item        VARCHAR,
                attribute   VARCHAR,
                value       VARCHAR,
                source_title VARCHAR,
                source_url  VARCHAR,
                method      VARCHAR,
                confidence  DOUBLE,
                confirmed   BOOLEAN,
                confirmed_by VARCHAR,
                fetched_at  TIMESTAMP
            )""")
    finally:
        con.close()


def _refresh_facts(db_path: str, ledger: str) -> None:
    """把账本摊成**一张说人话的宽表**（列名就是属性名本身）。

    DuckDB 的 PIVOT 按 attribute 的**实际取值**摊列，不写死任何业务词：
      item / source_url / confidence / confirmed / 歌手 / …
    视图不行（PIVOT 的值要从数据里取，DuckDB 明确禁止视图这么用），所以物化
    成表，每次写证据/确认后刷新。
    """
    _ensure_ledger(ledger)
    con = _connect(db_path, write=True)
    try:
        try:
            con.execute("DETACH wl")
        except Exception:
            pass
        con.execute(f"ATTACH '{ledger}' AS wl (READ_ONLY)")
        con.execute(
            f"CREATE OR REPLACE TABLE {FACTS} AS SELECT * FROM (PIVOT "
            f"wl.{EVID} ON attribute USING first(value) "
            f"GROUP BY item, source_url, confidence, confirmed)")
        try:
            con.execute("DETACH wl")
        except Exception:
            pass
    finally:
        con.close()


def _eid(item: str, attribute: str, value: str) -> str:
    """稳定 id：同一条事实重复落库是覆盖，不是又加一行（幂等）。"""
    h = hashlib.sha1(f"{item}|{attribute}|{value}".encode("utf-8")).hexdigest()
    return h[:16]


def save_docs(scene_id: str, query: str, results: list[dict[str, Any]]) -> int:
    """原始资料入库（按 url 去重：同一页只留一次，但 query 仍各记一行）。"""
    db = _db(scene_id)
    ensure(db)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    con = _connect(db, write=True)
    try:
        for r in results or []:
            url = str(r.get("url") or "").strip()
            if not url.startswith("http"):
                continue
            try:
                con.execute(
                    f"DELETE FROM {DOCS} WHERE query = ? AND url = ?",
                    [str(query), url])
                con.execute(
                    f"INSERT INTO {DOCS} VALUES (?, ?, ?, ?, ?)",
                    [str(query), str(r.get("title") or "")[:200], url,
                     str(r.get("snippet") or "")[:2000], ts])
                n += 1
            except Exception:
                continue
    finally:
        con.close()
    return n


def save_evidence(scene_id: str, rows: list[dict[str, Any]],
                  question: str = "", method: str = "web_search") -> int:
    """结构化事实入库（进账本，并刷新数据源里的宽表）。

    rows 每条 = {item, attribute, value, source_title, source_url,
                confidence, confirmed}
    ⚠️ 默认 confirmed=False：**没经人确认的一律是 claim**，不许当 fact 用。
    """
    db = _db(scene_id)
    led = _ledger(scene_id)
    ensure(db)
    _ensure_ledger(led)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    con = _connect(led, write=True)
    try:
        for r in rows or []:
            item = str(r.get("item") or "").strip()
            val = str(r.get("value") or "").strip()
            if not item or not val:
                continue
            attr = str(r.get("attribute") or "归属").strip()
            con.execute(f"DELETE FROM {EVID} WHERE id = ?",
                        [_eid(item, attr, val)])
            con.execute(
                f"INSERT INTO {EVID} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [_eid(item, attr, val), str(question)[:400], item[:400], attr,
                 val[:200], str(r.get("source_title") or "")[:200],
                 str(r.get("source_url") or "")[:400], str(method),
                 float(r.get("confidence") or 0.0),
                 bool(r.get("confirmed") or False),
                 str(r.get("confirmed_by") or ""), ts])
            n += 1
    finally:
        con.close()
    if n:
        _refresh_facts(db, led)
    return n


def confirm(scene_id: str, ids: list[str] | None = None,
            by: str = "human", question: str = "") -> int:
    """人在回路：确认一条/一批 claim 为 fact。

    ids=None 且给了 question → 只确认**这条问题**下的全部（人看完答案点一下
    就是这种情况）；ids=None 又没 question → 拒绝（不给人一键确认全库的
    口子，那是自欺）。
    """
    if not ids and not question:
        raise ValueError("要么给 ids，要么给 question —— 不许整库一键确认")
    db = _db(scene_id)
    led = _ledger(scene_id)
    ensure(db)
    _ensure_ledger(led)
    con = _connect(led, write=True)
    got = 0
    try:
        if ids:
            con.executemany(
                f"UPDATE {EVID} SET confirmed = TRUE, confirmed_by = ? "
                f"WHERE id = ?", [[by, str(i)] for i in ids])
            got = int(con.execute(
                f"SELECT COUNT(*) FROM {EVID} WHERE confirmed").fetchone()[0])
        else:
            con.execute(
                f"UPDATE {EVID} SET confirmed = TRUE, confirmed_by = ? "
                f"WHERE question = ?", [by, str(question)[:400]])
            got = int(con.execute(
                f"SELECT COUNT(*) FROM {EVID} WHERE question = ? AND confirmed",
                [str(question)[:400]]).fetchone()[0])
    finally:
        con.close()
    try:
        _refresh_facts(db, led)     # confirmed 变了，宽表要跟着刷
    except Exception:
        pass
    return got


def pending(scene_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """待确认清单（面板「人看一眼」用的就是它）。"""
    led = _ledger(scene_id)
    if not Path(led).exists():
        return []
    con = _connect(led)
    try:
        rows = con.execute(
            f"SELECT item, attribute, value, source_url, confidence, fetched_at "
            f"FROM {EVID} WHERE COALESCE(confirmed, FALSE) = FALSE "
            f"ORDER BY fetched_at DESC LIMIT ?", [int(limit)]).fetchall()
        return [{"item": r[0], "attribute": r[1], "value": r[2],
                 "source_url": r[3], "confidence": r[4],
                 "fetched_at": str(r[5])} for r in rows]
    except Exception:
        return []
    finally:
        con.close()


def confirmed_rows(scene_id: str) -> list[dict[str, Any]]:
    """已确认的事实（消费侧可当查询源）。"""
    led = _ledger(scene_id)
    if not Path(led).exists():
        return []
    con = _connect(led)
    try:
        rows = con.execute(
            f"SELECT item, attribute, value, source_url FROM {EVID} "
            f"WHERE confirmed ORDER BY fetched_at").fetchall()
        return [{"item": r[0], "attribute": r[1], "value": r[2],
                 "source_url": r[3]} for r in rows]
    except Exception:
        return []
    finally:
        con.close()


def rank(scene_id: str, attribute: str = "归属",
         include_unconfirmed: bool = True) -> list[dict[str, Any]]:
    """「谁最多」类问题的确定性答案：按 value 计数排名（代码数，不靠模型数）。"""
    led = _ledger(scene_id)
    if not Path(led).exists():
        return []
    con = _connect(led)
    try:
        _w = "" if include_unconfirmed else "AND confirmed"
        rows = con.execute(
            f"SELECT value, COUNT(*) AS n, "
            f"SUM(CASE WHEN confirmed THEN 1 ELSE 0 END) AS ok "
            f"FROM {EVID} WHERE attribute = ? {_w} "
            f"GROUP BY value ORDER BY n DESC, ok DESC", [str(attribute)]).fetchall()
        return [{"value": r[0], "count": int(r[1]), "confirmed": int(r[2])}
                for r in rows]
    except Exception:
        return []
    finally:
        con.close()


if __name__ == "__main__":            # 手工看一眼账
    import json
    import sys
    sid = sys.argv[1] if len(sys.argv) > 1 else "G"
    print("db:", _db(sid), "| ledger:", _ledger(sid))
    print(json.dumps({"pending": pending(sid, 5), "rank": rank(sid)},
                     ensure_ascii=False, indent=1))
