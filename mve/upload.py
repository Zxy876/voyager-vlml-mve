#!/usr/bin/env python3
"""外置上传模块：把用户给的文件变成**模型输入里的上下文**（DeepSeek 客户端同构）。

为什么是「上下文」而不是「落表」（2026-10-28 用户四次纠正的收敛点）
--------------------------------------------------------------------
用户原话依次是：「上传文件的时候要像 R1 客户端一样当作上下文输入」→
「上下文是上传文件（多模态带进输入文本里的）和 DeepSeek 客户端同构」。

DeepSeek 客户端的真相是：**文件先被转成文本，拼进这次请求的输入**——
表格转成「列名 + 若干真行」，文档转成正文片段，图片交给多模态模型转写。
模型看到的是**这一次对话里的内容**，跟它能不能查库、库在哪里无关。

所以这里分两条通道，都通向同一个 `context_block()`：

  表格类（csv/tsv/json/xlsx）→ 落成本场景 duckdb 的一张 `up_<名>` 表
      ⇒ 既进 context_block（列名 + 真前几行），也进 schema/全貌（能真查）
  文本/文档/图片（txt/md/pdf/docx/png/jpg…）→ 抽出文本片段进 context_block
      ⇒ 只当上下文（查不了库），但模型**看得见内容**，不是只知道文件名

`context_block(scene_id)` 就是「把这次要带进模型输入的上下文」渲染成一段文本：
消费侧的 SQL 回填、本机/场景 solve、知识类 inference、联网工具线都会拼它。
没有上传文件就返回空串（无上下文 = 纯模型知识，行为不变）。

边界（诚实）
-----------
· PDF 需要 pypdf，docx 直接解 zip（无额外依赖）；缺库就如实报错，不假装解析。
· 图片：配了 `MVE_VISION_MODEL`（如 glm-4v-flash）才转写；没配就如实标注
  「当前模型看不了图」——不编造图里有什么。
· 表名统一 `up_<文件名>`，重复上传同名文件 = 覆盖（CREATE OR REPLACE）。
· 写库要独占锁：duckdb 同一时刻「一个写进程 或 多个只读进程」。面板的连接
  是每查询一开一关（scene_db.py），所以这里带重试；真锁不上就如实报错。
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any

MAX_BYTES = 32 * 1024 * 1024        # 32MB（面板/客户端都按这个拦）
SUPPORTED_TABULAR = {".csv", ".tsv", ".json", ".jsonl", ".ndjson",
                     ".xlsx", ".xls"}
SUPPORTED_TEXT = {".txt", ".md", ".markdown", ".log", ".csvx", ".yaml",
                  ".yml", ".ini", ".conf", ".sql", ".py"}
SUPPORTED_DOC = {".pdf", ".docx"}
SUPPORTED_IMAGE = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
SUPPORTED = (SUPPORTED_TABULAR | SUPPORTED_TEXT | SUPPORTED_DOC
             | SUPPORTED_IMAGE)
TEXT_LIMIT = 6000          # 单个文本文件注入上下文的上限（字符）


def _meta_path(scene_id: str) -> Path:
    _db, sdir, _kind = _scene_db_path(scene_id)
    return Path(sdir) / "uploads_meta.json"


def _load_meta(scene_id: str) -> list[dict[str, Any]]:
    p = _meta_path(scene_id)
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        return obj if isinstance(obj, list) else []
    except Exception:
        return []


def _save_meta(scene_id: str, items: list[dict[str, Any]]) -> None:
    """原子写（tmp + os.replace）—— 同 scenes.json 的铁律，别留半个 JSON。"""
    p = _meta_path(scene_id)
    tmp = p.with_suffix(".json.tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(items[-20:], ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass


def _fresh(items: list[dict[str, Any]], minutes: int) -> list[dict[str, Any]]:
    """只留**最近这一次对话**的上传（DeepSeek 语义：文件属于本次会话）。

    ⚠️ 2026-10-28 实测事故：早期把上传当「永久上下文」，场景 B 里一份测试
    残留的 `工资明细.csv` 把之后每道题的 SQL 都带歪（回填层看到「优先按它
    作答」就改查 `up_` 表），公司总额被答成 22600。文件一旦过期就**只留着
    表**（还能查），但不再自动进上下文 —— 要用就重新传一次。
    """
    if minutes <= 0:
        return list(items)
    cut = time.time() - minutes * 60
    out = []
    for it in items:
        try:
            ts = time.mktime(time.strptime(str(it.get("ts") or ""),
                                           "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            continue                      # 没有时间戳的老记录 = 过期
        if ts >= cut:
            out.append(it)
    return out


def context_block(scene_id: str, max_rows: int = 12,
                  fresh_minutes: int = 30) -> str:
    """把「用户上传过的文件」渲染成**上下文块**（DeepSeek 客户端的文件注入）。

    用户 2026-10-28：「上传文件的时候要像 R1 客户端一样当作上下文输入」/
    「上下文是上传文件（多模态带进输入文本里的）」。

    三种来源都渲染成**文本**（模型能直接读的东西），不是只有表名：
      · 表格 → 表名 + 列名 + 真实前几行（还能真查，标注「已接入本场景库」）；
      · 文本/文档 → 正文开头几段；
      · 图片 → 视觉模型转写的文字（没配就如实说看不了图）。
    """
    items = _fresh(_load_meta(scene_id), fresh_minutes)
    if not items:
        return ""
    lines = ["【用户这次上传的文件（真实内容，优先按它作答）】"]
    for it in items[-4:]:
        _kind = str(it.get("kind") or "table")
        _name = it.get("name")
        if _kind == "table":
            tb = str(it.get("table") or "")
            vis = "已接入本场景库，可直接查询" if it.get("visible") \
                else "仅作上下文（未接入本场景库，查不到）"
            lines.append(f"- {_name} → 表 {tb}（{it.get('rows')} 行；{vis}）")
            cols = it.get("columns") or []
            if cols:
                lines.append(f"  列：{', '.join(str(c) for c in cols)}")
            rows = (it.get("sample") or [])[:max_rows]
            if rows:
                try:
                    lines.append("  真实前几行："
                                 + json.dumps(rows, ensure_ascii=False)[:1200])
                except Exception:
                    pass
            continue
        # 文本 / 图片：内容本身就是上下文（多模态已转成文本）
        _txt = str(it.get("text") or "")
        _note = str(it.get("note") or "")
        _hint = "文本" if _kind == "text" else "图片"
        lines.append(f"- {_name}（{_hint}，{len(_txt)} 字"
                     + (f"；{_note}" if _note else "") + "）")
        if _txt:
            lines.append("  内容：")
            lines.append(_txt[:1800])
        elif _note:
            lines.append(f"  （没有可读内容：{_note}）")
    return "\n".join(lines)[:6000]


def _safe_stem(name: str) -> str:
    """文件名 → 合法表名后缀（中文保留，只把标点/空格折成下划线）。

    ⚠️ 保留中文是有意的：用户的文件名常常是「技术部津贴.csv」，全转 ASCII
    会得到 up_2026_9 这种认不出来的名字（2026-10-28 实测），人也对不上账。
    """
    stem = Path(str(name or "upload")).stem
    s = re.sub(r"[^\w]+", "_", stem, flags=re.UNICODE).strip("_").lower()
    return (s or "upload")[:40]


def _q(ident: str) -> str:
    """标识符加双引号（表名可能含中文，写进 SQL 必须引起来）。"""
    return '"' + str(ident).replace('"', '""') + '"'


def _scene_db_path(scene_id: str) -> tuple[str, str, str]:
    """(落库路径, 场景目录, kind) —— **场景分配到的那个数据源**。

    用户 2026-10-28：「给每个场景分配一个数据源，会存放我们对话传上来的
    东西，检索逻辑一致」。所以这里不再自己发明 uploads.duckdb：
      · 普通场景 → `scene_profile.ensure_source()`（每个场景一个固定库，
        学习/消费/出题/判官读的都是它）；
      · VLML 域（默认场景）→ 仍是 `uploads.duckdb`，因为 VLML 有自己的数据域，
        混进去等于破了场景隔离。
    """
    import scene_profile
    prof = scene_profile.profile(scene_id) or {}
    d = str(prof.get("scene_dir") or "") or scene_profile.scene_dir(scene_id)
    kind = str(prof.get("kind") or "")
    if kind == "vlml":
        return str(Path(d) / "uploads.duckdb"), d, kind
    try:
        return str(scene_profile.ensure_source(scene_id)), d, kind
    except Exception:
        dbp = str(prof.get("db_path") or "")
        return (dbp or str(Path(d) / "uploads.duckdb")), d, kind


def _read_text(path: str, limit: int = TEXT_LIMIT) -> str:
    """纯文本 → 文本（UTF-8 优先，退 GB18030 —— 中文文件常是 GBK 存的）。"""
    raw = Path(path).read_bytes()
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)[:limit]
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")[:limit]


def _docx_text(path: str, limit: int = TEXT_LIMIT) -> str:
    """docx → 文本（docx 就是 zip，取 word/document.xml 的 <w:t>，零依赖）。"""
    import zipfile
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
    xml = re.sub(r"</w:p>", "\n", xml)          # 段落换行
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    return re.sub(r"<[^>]+>", "", xml)[:limit]


def _pdf_text(path: str, limit: int = TEXT_LIMIT) -> str:
    """PDF → 文本（pypdf）。没装就如实报错，不假装解析出内容。"""
    try:
        from pypdf import PdfReader
    except Exception as e:
        raise RuntimeError(f"读 PDF 需要 pypdf（{type(e).__name__}）——"
                           f"可把文件另存为 txt/md 再上传")
    reader = PdfReader(path)
    out: list[str] = []
    for page in reader.pages[:12]:
        try:
            out.append(page.extract_text() or "")
        except Exception:
            continue
        if sum(len(x) for x in out) >= limit:
            break
    return "\n".join(out)[:limit]


def _image_text(path: str, limit: int = 2000) -> tuple[str, str]:
    """图片 → 文字（多模态通路）。返回 (文本, 说明)。

    用户 2026-10-28：「上下文是上传文件（**多模态**带进输入文本里的）」——
    所以图片要变成文字进上下文，而不是只记个文件名。没配视觉模型
    （`MVE_VISION_MODEL`，如 glm-4v-flash）就如实说看不了，不编图里有什么。
    """
    model = os.getenv("MVE_VISION_MODEL", "").strip()
    if not model:
        return "", "当前模型看不了图（未配置 MVE_VISION_MODEL）"
    try:
        import httpx
        from llm_client import API_KEY, BASE_URL
        b64 = base64.b64encode(Path(path).read_bytes()).decode()
        suf = (Path(path).suffix.lower().lstrip(".") or "png").replace("jpg", "jpeg")
        r = httpx.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": model, "temperature": 0.0, "max_tokens": 1500,
                  "messages": [{"role": "user", "content": [
                      {"type": "image_url",
                       "image_url": {"url": f"data:image/{suf};base64,{b64}"}},
                      {"type": "text",
                       "text": "把这张图里的内容原样转成文字（表格用 markdown 表格），"
                               "不要解释、不要补充。"}]}]},
            timeout=90.0)
        r.raise_for_status()
        t = str(((r.json().get("choices") or [{}])[0]
                 .get("message") or {}).get("content") or "").strip()
        return (t[:limit], f"已用 {model} 转写") if t else \
            ("", f"{model} 没返回内容")
    except Exception as e:
        return "", f"图片转写失败（{type(e).__name__}: {str(e)[:80]}）"


def _ingest_as_context(scene_id: str, name: str, path: str, ext: str,
                       out: dict[str, Any]) -> dict[str, Any]:
    """非表格文件：抽出文本 → 存进上下文元数据（不落表）。"""
    kind = "image" if ext in SUPPORTED_IMAGE else "text"
    note = ""
    try:
        if ext in SUPPORTED_IMAGE:
            txt, note = _image_text(path)
        elif ext == ".pdf":
            txt = _pdf_text(path)
        elif ext == ".docx":
            txt = _docx_text(path)
        else:
            txt = _read_text(path)
    except Exception as e:
        out["why"] = str(e)
        return out
    items = [x for x in _load_meta(scene_id) if str(x.get("name")) != name]
    items.append({"name": name, "kind": kind, "chars": len(txt), "text": txt,
                  "note": note, "visible": False,
                  "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _save_meta(scene_id, items)
    out.update(ok=True, kind=kind, chars=len(txt), note=note, visible=False,
               table="", text=txt[:300])
    return out


def _xlsx_to_csv(src: str, dst: str) -> str:
    """xlsx → csv（只取第一个工作表）。没有 openpyxl 就抛，让人另存 CSV。"""
    try:
        import csv as _csv

        import openpyxl
    except Exception as e:      # 没装就如实说，不假装能解析
        raise RuntimeError(
            f"读 xlsx 需要 openpyxl（{type(e).__name__}）—— 请把文件另存为 CSV 再上传")
    wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        with open(dst, "w", encoding="utf-8", newline="") as f:
            w = _csv.writer(f)
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                w.writerow(["" if v is None else v for v in row])
                if i > 200000:      # 防呆：超过 20 万行就停（内存/时间）
                    break
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return dst


def _connect_write(db_path: str, tries: int = 6) -> Any:
    """拿写连接（重试）：面板的只读连接是每查询一开一关，短暂占用会撞锁。"""
    import duckdb
    last: Exception | None = None
    for i in range(tries):
        try:
            return duckdb.connect(db_path)
        except Exception as e:      # Conflicting lock / IOException
            last = e
            time.sleep(0.35 * (i + 1))
    raise RuntimeError(f"库被占用，写不进去（{type(last).__name__}: {last}）")


def ingest(scene_id: str, filename: str, blob: bytes) -> dict[str, Any]:
    """把一个上传文件落成场景库里的表。返回 {ok, table, rows, columns, ...}。

    调用方（面板路由）负责体积/后缀的第一道拦截；这里再做一次（谁调都安全）。
    """
    name = str(filename or "").strip()
    ext = Path(name).suffix.lower()
    out: dict[str, Any] = {"ok": False, "name": name, "table": "",
                           "rows": 0, "columns": [], "why": ""}
    if not name:
        out["why"] = "没有文件名"
        return out
    if ext not in SUPPORTED:
        out["why"] = (f"不支持的后缀 {ext or '（无）'} —— 支持 "
                      + "、".join(sorted(SUPPORTED)))
        return out
    if not blob:
        out["why"] = "文件是空的"
        return out
    if len(blob) > MAX_BYTES:
        out["why"] = f"文件太大（{len(blob)//1024//1024}MB > 32MB）"
        return out

    try:
        _drop_expired(scene_id)          # 过期的旧上传不留在库里污染 schema
    except Exception:
        pass
    db_path, sdir, kind = _scene_db_path(scene_id)
    # agent 能不能「看见」这张表：落库路径 == 场景真正的数据源（kind=scene）
    # 才看得见；VLML 域落的是 uploads.duckdb，而 agent 查的是 VLML 库 ——
    # 必须如实告诉人「看不见」，不然就会出现「导入了却答不对」的迷惑
    # （2026-10-28 用户实测踩到：在场景 A 上传，表进了 A 的 uploads.duckdb）。
    out["visible"] = (kind == "scene")
    updir = Path(sdir) / "uploads"
    try:
        updir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        out["why"] = f"建上传目录失败：{e}"
        return out
    # 落原文件（带时间戳前缀：同名不互相覆盖，保留证据）
    stamp = time.strftime("%Y%m%d-%H%M%S")
    src = updir / f"{stamp}_{_safe_stem(name)}{ext}"
    try:
        src.write_bytes(blob)
    except Exception as e:
        out["why"] = f"写文件失败：{e}"
        return out

    # 文本/文档/图片：转成文本进上下文（DeepSeek 同构），不落表。
    if ext in (SUPPORTED_TEXT | SUPPORTED_DOC | SUPPORTED_IMAGE):
        return _ingest_as_context(scene_id, name, str(src), ext, out)

    read_path = str(src)
    tmp_csv = ""
    if ext in (".xlsx", ".xls"):
        tmp_csv = str(src.with_suffix(".csv"))
        try:
            _xlsx_to_csv(str(src), tmp_csv)
        except Exception as e:
            out["why"] = str(e)
            return out
        read_path = tmp_csv

    table = f"up_{_safe_stem(name)}"
    if ext in (".csv", ".tsv"):
        _reader = (f"read_csv_auto('{read_path}', delim='\\t', header=true)"
                   if ext == ".tsv" else
                   f"read_csv_auto('{read_path}', header=true)")
    elif ext in (".json", ".jsonl", ".ndjson"):
        _reader = f"read_json_auto('{read_path}')"
    else:
        _reader = f"read_csv_auto('{read_path}', header=true)"

    con = None
    try:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        con = _connect_write(db_path)
        con.execute(f"CREATE OR REPLACE TABLE {_q(table)} AS "
                    f"SELECT * FROM {_reader}")
        n = con.execute(f"SELECT COUNT(*) FROM {_q(table)}").fetchone()[0]
        desc = con.execute(f"DESCRIBE {_q(table)}").fetchall()
        cols = [str(r[0]) for r in desc]
        sample = con.execute(f"SELECT * FROM {_q(table)} LIMIT 3").fetchall()
        _cols = cols
        out.update(ok=True, table=table, rows=int(n), columns=_cols,
                   sample=[dict(zip(_cols, map(str, r))) for r in sample],
                   db_path=db_path, file=str(src))
        # 记一份**上下文元数据**（文件名/列/前 20 行真值）：R1 客户端式地把
        # 文件当上下文注入用（2026-10-28 用户拍板），不靠写死 prompt。
        try:
            _more = con.execute(f"SELECT * FROM {_q(table)} LIMIT 20").fetchall()
            _items = [x for x in _load_meta(scene_id)
                      if str(x.get("table") or "") != table]
            _items.append({
                "name": name, "kind": "table", "table": table,
                "rows": int(n), "columns": _cols,
                "sample": [dict(zip(_cols, map(str, r))) for r in _more],
                "visible": bool(out.get("visible")),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            _save_meta(scene_id, _items)
        except Exception:
            pass
    except Exception as e:
        out["why"] = f"落表失败：{type(e).__name__}: {e}"
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        if tmp_csv:
            try:
                os.unlink(tmp_csv)
            except Exception:
                pass
    return out


def _drop_tables(db_path: str, names: list[str]) -> list[str]:
    """把上传表从库里删掉（原文件还在 <场景>/uploads/ 里，可重新上传）。"""
    names = [n for n in names if str(n).startswith("up_")]
    if not names or not Path(db_path).exists():
        return []
    import duckdb
    con = _connect_write(db_path)
    done = []
    try:
        for n in names:
            try:
                con.execute(f"DROP TABLE IF EXISTS {_q(n)}")
                done.append(n)
            except Exception:
                continue
    finally:
        try:
            con.close()
        except Exception:
            pass
    return done


def clear(scene_id: str, drop_tables: bool = True) -> dict[str, Any]:
    """清掉本场景的上传上下文（默认连表一起删）。

    设计口径（2026-10-28 用户：「上下文是上传文件…和 DeepSeek 客户端同构」）：
    文件属于**那一次对话**。过期后如果只从上下文里摘掉、表还留在库里，它就会
    一直出现在 schema/全貌里 —— 实测回填层会改查 `up_工资明细` 而不是
    `employees`，公司总额题直接答废。所以过期即清表（原文件仍在 uploads/，
    想用重新传一次）。
    """
    items = _load_meta(scene_id)
    db_path, _sdir, _kind = _scene_db_path(scene_id)
    dropped: list[str] = []
    if drop_tables and items:
        try:
            dropped = _drop_tables(db_path,
                                   [str(x.get("table") or "") for x in items])
        except Exception as e:
            return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    p = _meta_path(scene_id)
    try:
        if p.exists():
            p.unlink()
        return {"ok": True, "cleared": True, "dropped": dropped}
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}", "dropped": dropped}


def _drop_expired(scene_id: str, minutes: int = 30) -> list[str]:
    """上传时顺手清理过期的旧上传（清表 + 从 meta 摘掉）。

    只在**写路径**（ingest）里做：读路径每次提问都去写库会撞 duckdb 的锁。
    """
    items = _load_meta(scene_id)
    fresh = _fresh(items, minutes)
    if len(fresh) == len(items):
        return []
    gone = [x for x in items if x not in fresh]
    db_path, _sdir, _kind = _scene_db_path(scene_id)
    dropped = _drop_tables(db_path, [str(x.get("table") or "") for x in gone])
    _save_meta(scene_id, fresh)
    return dropped


def describe(scene_id: str) -> dict[str, Any]:
    """列本场景已有的上传表（面板展示/自检用）。"""
    db_path, _, _ = _scene_db_path(scene_id)
    out: dict[str, Any] = {"db_path": db_path, "tables": []}
    if not Path(db_path).exists():
        return out
    try:
        import duckdb
        con = duckdb.connect(db_path, read_only=True)
        try:
            rows = con.execute(
                "SELECT table_name FROM information_schema.tables "
                # ⚠️ 别用 LIKE 'up\_%'：duckdb 的 LIKE 不认反斜杠转义（实测
                # 会一个都匹配不到，2026-10-28 服务器踩到）。用 starts_with。
                "WHERE table_schema='main' AND starts_with(table_name, 'up_') "
                "ORDER BY table_name").fetchall()
        finally:
            con.close()
        out["tables"] = [str(r[0]) for r in rows]
    except Exception as e:
        out["why"] = f"{type(e).__name__}: {e}"
    return out


def _main() -> int:
    import sys
    if len(sys.argv) < 3:
        print("用法：upload.py <scene_id> <file>")
        return 2
    sid, f = sys.argv[1], sys.argv[2]
    r = ingest(sid, os.path.basename(f), Path(f).read_bytes())
    print(r)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
