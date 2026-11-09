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

# value 的长度上限（2026-11-07 用户拍板：「把限制放开吧，让他多看到一些文本」）。
# ⚠️ 原来这里是 200，害得一条合理的维度永远取不到值：
#   GitHub API 返回体 1291 字，`created_at` 在第 1216 位 → 被切掉 →
#   `web_facts.正文` 只剩前 200 字（JSON 截断）→ `JSON_EXTRACT` 报
#   Malformed JSON → 维度「账号创建时间」反复失败，判词再准也执行不了。
#   真答案其实**在** `web_docs.body`（完整），但维度问的是 web_facts 的正文列。
# 为什么给 20000 而不是无限：`value` 是摊平宽表的一列，长文本进宽表会拖慢
# PIVOT 刷新（每次写证据都重建整张表）；20000 与 `body` 的上限对齐，够用。
VALUE_MAX = 20000


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


def _cols(con, table: str) -> list[str]:
    try:
        return [str(r[1]) for r in
                con.execute(f"PRAGMA table_info('{table}')").fetchall()]
    except Exception:
        return []


def ensure(db_path: str) -> None:
    """数据源侧建表（幂等）。

    ⚠️ `body`（正文）是 2026-10-29 补的：第一版只存搜索摘要 snippet，
    学习侧拿到的 `web_docs` 是一张「只有标题+两行摘要」的表 —— 调研类问题
    （「这个仓库是干什么的」）在 SQL 上**永远答不出来**，于是它反复给
    WHERE 条件打磨过滤（生产实测 4 次重试全是同一句 SELECT 换 LIKE），
    而摘要在语义上根本不含答案。表里必须放**能查的东西**（本模块头那条
    分层原则），正文才是资料本体。老库用 ADD COLUMN 补列，不重建。
    """
    con = _connect(db_path, write=True)
    try:
        con.execute(f"""
            CREATE TABLE IF NOT EXISTS {DOCS} (
                query      VARCHAR,
                title      VARCHAR,
                url        VARCHAR,
                snippet    VARCHAR,
                body       VARCHAR,
                fetched_at TIMESTAMP
            )""")
        if "body" not in _cols(con, DOCS):
            con.execute(f"ALTER TABLE {DOCS} ADD COLUMN body VARCHAR")
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


# ---- 取证落点表的「当前状态」（2026-11-09）----
# 为什么需要它：骨架（= 原版 maieutic 教师端）必须知道**学生够得着什么**，
# 原版靠 `scaffolding.ts:262 buildScaffoldingUserMessage` 把 targetUnit /
# targetLanguage 告诉骨架生成器。MVE 的同构物是「这个场景现在真的能取到
# 什么」—— 里面就包括取证落点表**此刻装了什么**。
#
# ⚠️ 之前只给了「表名 + 一句『初始是空的』」，那是**过时**的：表早就装满了
# （场景 L 实测 web_docs 3796 行 / web_facts 3736 行）。而且更危险的是：
# 分不清 web_docs 里装的是**搜索引擎垃圾**（「怎么查找github用户名」这类
# 介绍文章）还是**真探测结果** → 骨架会拆出「从 web_docs 查该用户名」的
# 维度，而那些行根本不含该用户名的探测结论。
#
# 所以这里给的是**事实**：行数 / distinct 计数 / 值域 / **少量样本**。
# 样本是关键 —— 「这是介绍文章还是真结果」得让模型自己看样本判断，
# 我们不替它下结论（那是发明语义）。
_STATE_SAMPLE = 5


def ingest_state(scene_id: str) -> list[str]:
    """取证落点表**此刻的真实状态**（实查；查不到就不列）。

    返回给骨架的每一段都是事实：行数、distinct 计数、值域、样本值。
    不替模型判断「这些数据好不好」—— 只让它看得见。
    """
    out: list[str] = []
    db = _db(scene_id)
    led = _ledger(scene_id)
    # ① 资料行 web_docs（原始资料：搜索结果页 / 抓回来的正文）
    try:
        import duckdb
        con = duckdb.connect(db, read_only=True)
        try:
            n = con.execute(f"SELECT count(*) FROM {DOCS}").fetchone()[0]
            dq = con.execute(
                f"SELECT count(DISTINCT query) FROM {DOCS}").fetchone()[0]
            du = con.execute(
                f"SELECT count(DISTINCT url) FROM {DOCS}").fetchone()[0]
            nb = con.execute(
                f"SELECT count(*) FROM {DOCS} "
                f"WHERE body IS NOT NULL AND length(body) > 0").fetchone()[0]
            samp = con.execute(
                f"SELECT query, title FROM {DOCS} LIMIT {_STATE_SAMPLE}"
            ).fetchall()
            seg = (f"- {DOCS}（资料行）：{n} 行 / {du} 个不同 url / "
                   f"{dq} 个不同 query / {nb} 行有正文")
            if samp:
                seg += "；样本（query → 标题）：" + "；".join(
                    f"{str(a or '')[:40]} → {str(b or '')[:60]}" for a, b in samp)
            out.append(seg)
        finally:
            con.close()
    except Exception:
        pass
    # ② 事实宽表 web_facts（按 attribute 摊平；列名 = 属性名）
    try:
        import duckdb
        con = duckdb.connect(db, read_only=True)
        try:
            cols = [str(r[1]) for r in con.execute(
                f"PRAGMA table_info('{FACTS}')").fetchall()]
            n = con.execute(f"SELECT count(*) FROM {FACTS}").fetchone()[0]
            di = con.execute(
                f"SELECT count(DISTINCT item) FROM {FACTS}").fetchone()[0]
            base = {"item", "source_url", "confidence", "confirmed"}
            site_cols = [c for c in cols if c not in base]
            seg = (f"- {FACTS}（事实宽表）：{n} 行 / {di} 个不同 item / "
                   f"{len(cols)} 列（其中 {len(site_cols)} 列是属性列）")
            if site_cols:
                seg += ("；属性列名（= 各站点的判定结果与 http 状态码）前 12 个："
                        + "、".join(site_cols[:12]))
            ci = con.execute(
                f"SELECT confirmed, count(*) FROM {FACTS} "
                f"GROUP BY confirmed").fetchall()
            if ci:
                seg += "；confirmed 分布：" + "、".join(
                    f"{b}={c}" for b, c in ci)
            samp = con.execute(
                f"SELECT item FROM {FACTS} LIMIT {_STATE_SAMPLE}").fetchall()
            if samp:
                seg += "；item 样本：" + "、".join(str(a or '')[:40] for (a,) in samp)
            out.append(seg)
        finally:
            con.close()
    except Exception:
        pass
    # ③ 账本 web_evidence（EAV；属性名与取值域在这里最全 —— 宽表是 PIVOT 出来的）
    try:
        import duckdb
        con = duckdb.connect(led, read_only=True)
        try:
            n = con.execute(f"SELECT count(*) FROM {EVID}").fetchone()[0]
            da = con.execute(
                f"SELECT count(DISTINCT attribute) FROM {EVID}").fetchone()[0]
            vals = con.execute(
                f"SELECT value, count(*) c FROM {EVID} "
                f"WHERE value IS NOT NULL AND length(value) <= 30 "
                f"GROUP BY value ORDER BY c DESC LIMIT 12").fetchall()
            attrs = con.execute(
                f"SELECT attribute, count(*) c FROM {EVID} "
                f"GROUP BY attribute ORDER BY c DESC LIMIT 12").fetchall()
            seg = (f"- {EVID}（审计账本 EAV）：{n} 行 / {da} 个不同属性")
            if vals:
                seg += "；取值域（按出现次数）：" + "、".join(
                    f"{str(v)[:30]}×{c}" for v, c in vals)
            if attrs:
                seg += "；属性（按出现次数）：" + "、".join(
                    f"{str(a)[:30]}×{c}" for a, c in attrs)
            out.append(seg)
        finally:
            con.close()
    except Exception:
        pass
    return out


def save_docs(scene_id: str, query: str, results: list[dict[str, Any]],
              bodies: dict[str, str] | None = None) -> int:
    """原始资料入库（**按 url 去重**：同一个页面只留一行，query 记最后一次）。

    `bodies`：{url: 正文}。搜索只给摘要，摘要答不了「这项目干什么」，正文
    才是证据（2026-10-29：学习侧在只有 snippet 的表上必败）。给了就一起存，
    没抓到的 url 只存摘要 —— 如实存，不留空壳。

    ⚠️ 2026-10-28 实测踩到：原来按 (query, url) 去重，同一个页面被不同关键词
    搜到就存成好几行；学习侧拿 `web_docs JOIN web_facts ON url` 一算，每行事实
    被放大成 N 行 —— 8 条引用数成了 14 条（75120 乘爆的同款形状）。资料是
    「有哪些页面」，页面本来就该唯一。
    """
    db = _db(scene_id)
    ensure(db)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    bodies = {str(k): str(v) for k, v in (bodies or {}).items() if str(v).strip()}
    n = 0
    con = _connect(db, write=True)
    try:
        for r in results or []:
            url = str(r.get("url") or "").strip()
            if not url.startswith("http"):
                continue
            try:
                con.execute(f"DELETE FROM {DOCS} WHERE url = ?", [url])
                con.execute(
                    f"INSERT INTO {DOCS} (query, title, url, snippet, body, "
                    f"fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                    [str(query), str(r.get("title") or "")[:200], url,
                     str(r.get("snippet") or "")[:2000],
                     bodies.get(url, "")[:20000], ts])
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
                 val[:VALUE_MAX], str(r.get("source_title") or "")[:200],
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
