#!/usr/bin/env python3
"""外置上传模块：把人给的文件变成**场景数据库里的表**，让 agent 看得见。

为什么要这个东西（2026-10-28 用户拍板）
------------------------------------
用户原话：「不能这样写死守卫解决，而是做 r1 客户端的多模态上传文件那些
外置模块来让 agent 看到全貌」。

R1 / DeepSeek 客户端的形状是：用户把文件**交给**模型（拖进去 / 点回形针），
模型看的是文件本身，而不是靠提示词里写死的业务词表猜。MVE 对应物就是这里：

    文件（csv/tsv/json/jsonl/xlsx） → 场景 duckdb 里的一张表 → 进 schema_block
    与 curriculum_auto.db_observation 的**全貌** → 出题器/回填层/判官全部
    自动看得见（不用改一行业务代码）

与既有结构的关系
----------------
· 落库位置 = `scene_profile.profile(sid)["db_path"]`（场景自己的库）。
  消费/学习两侧都从这个 profile 取库，所以上传完**不需要重启、不需要改配置**，
  下一次提问就看得见新表（这是「看到全貌」的实际兑现方式）。
· 场景还没有库（kind=local/missing）→ 落到 `<场景目录>/uploads.duckdb`，
  它就是该场景唯一的 duckdb，profile 的 glob 会自动选中它。
· 场景是 VLML 域（场景 A）→ 不往 VLML 库里写（那是另一个数据域的东西），
  落到 `uploads.duckdb` 并如实告知「要让它成为数据源，请把场景 data_source
  指到这个文件」。

边界（诚实）
-----------
· xlsx/xls 需要 openpyxl；没装就如实报错让人另存 CSV，不假装解析成功。
· 表名统一 `up_<文件名>`，重复上传同名文件 = 覆盖（CREATE OR REPLACE）。
· 写库要独占锁：duckdb 同一时刻「一个写进程 或 多个只读进程」。面板的连接
  是每查询一开一关（scene_db.py），所以这里带重试；真锁不上就如实报错。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

MAX_BYTES = 32 * 1024 * 1024        # 32MB（面板/客户端都按这个拦）
SUPPORTED = {".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".xlsx", ".xls"}


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


def _scene_db_path(scene_id: str) -> tuple[str, str]:
    """(落库路径, 场景目录)。取场景自己的库；没有就 uploads.duckdb。"""
    import scene_profile
    prof = scene_profile.profile(scene_id) or {}
    d = str(prof.get("scene_dir") or "") or scene_profile.scene_dir(scene_id)
    dbp = str(prof.get("db_path") or "")
    kind = str(prof.get("kind") or "")
    if dbp and kind in ("scene", "vlml"):
        # VLML 域（场景 A）不往里写：那是另一个数据域，混进去等于破了场景隔离。
        if kind == "vlml":
            return str(Path(d) / "uploads.duckdb"), d
        return dbp, d
    return str(Path(d) / "uploads.duckdb"), d


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

    db_path, sdir = _scene_db_path(scene_id)
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


def describe(scene_id: str) -> dict[str, Any]:
    """列本场景已有的上传表（面板展示/自检用）。"""
    db_path, _ = _scene_db_path(scene_id)
    out: dict[str, Any] = {"db_path": db_path, "tables": []}
    if not Path(db_path).exists():
        return out
    try:
        import duckdb
        con = duckdb.connect(db_path, read_only=True)
        try:
            rows = con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='main' AND table_name LIKE 'up\\_%' "
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
