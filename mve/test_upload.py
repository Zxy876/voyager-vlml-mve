#!/usr/bin/env python3
"""外置上传模块的测试（2026-10-28 用户拍板「做 R1 客户端的上传外置模块」）。

三件事必须钉住：
① 只收支持的后缀、空文件、超限 —— 都如实拒，不假装成功；
② 落表后**全貌**里出现（schema/observation 读的是同一个库），这是
   「让 agent 看到全貌」的实际兑现方式，不是把文件丢在磁盘上就算；
③ 同名文件重复上传 = 覆盖同名表（不产生 up_x / up_x_2 这种垃圾）。

跑法：python mve/test_upload.py（服务器无 pytest，同 test_*.py 口径）。
"""

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "voyager_vlml"))

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    os.chdir(_HERE)
    import upload as U

    print("== 1. 表名规整 ==")
    _s = U._safe_stem("2026 年 9 月 工资!.csv")
    check("中文保留、标点空格折下划线", _s == "2026_年_9_月_工资", _s)
    check("纯符号 → 回退 upload", U._safe_stem("!!!.csv") == "upload")
    check("表名有长度上限", len(U._safe_stem("a" * 200 + ".csv")) <= 40)
    check("标识符会被引起来（中文表名必须）", U._q("up_技术") == '"up_技术"')

    print("== 2. 拒收口径（不假装成功） ==")
    r = U.ingest("B", "x.txt", b"hello")
    check("不支持的后缀 → ok=False", not r["ok"] and "不支持的后缀" in r["why"])
    r = U.ingest("B", "x.csv", b"")
    check("空文件 → ok=False", not r["ok"] and "空的" in r["why"])
    r = U.ingest("B", "", b"a")
    check("没文件名 → ok=False", not r["ok"] and "文件名" in r["why"])
    r = U.ingest("B", "x.csv", b"a" * (U.MAX_BYTES + 1))
    check("超 32MB → ok=False", not r["ok"] and "太大" in r["why"])

    print("== 3. 落表进**全貌**（有 duckdb 才跑） ==")
    try:
        import duckdb                        # noqa: F401
        _has = True
    except Exception:
        _has = False
    if not _has:
        print("  （本机没有 duckdb —— 落表用例跳过，服务器上跑）")
    else:
        tmp = tempfile.mkdtemp(prefix="mve_up_")
        db = os.path.join(tmp, "t.duckdb")
        orig = U._scene_db_path
        try:
            U._scene_db_path = lambda sid: (db, tmp)
            csv = ("dept,name,amount\n技术,张伟,600\n技术,李娜,920\n"
                   "财务,王强,300\n").encode("utf-8")
            r = U.ingest("B", "tech 津贴.csv", csv)
            check("CSV → 真表", r["ok"], r.get("why", "")[:60])
            check("行数对", r["rows"] == 3, str(r.get("rows")))
            check("列名对", r["columns"] == ["dept", "name", "amount"],
                  str(r.get("columns")))
            check("表名带 up_ 前缀", str(r.get("table", "")).startswith("up_"),
                  str(r.get("table")))
            # 全貌：任何走 scene 库的观察面都必须看得见这张表（agent 靠它）
            import scene_profile
            con = duckdb.connect(db, read_only=True)
            try:
                tabs = [x[0] for x in con.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='main'").fetchall()]
            finally:
                con.close()
            check("表进了库（schema/全貌读的是同一个库）",
                  r["table"] in tabs, str(tabs))
            r2 = U.ingest("B", "tech 津贴.csv", csv)
            check("同名重复上传 → 覆盖同名表（不产垃圾表）",
                  r2["ok"] and r2["table"] == r["table"], str(r2.get("table")))
            d = U.describe("B")
            check("describe 列出上传表", r["table"] in (d.get("tables") or []),
                  str(d.get("tables")))
            # TSV / JSON 两条通路
            r3 = U.ingest("B", "x.tsv", "a\tb\n1\t2\n".encode("utf-8"))
            check("TSV → 真表（按 tab 切）", r3["ok"] and r3["columns"] == ["a", "b"],
                  str(r3.get("columns")))
            r4 = U.ingest("B", "y.json",
                          b'[{"k": 1, "v": "x"}, {"k": 2, "v": "y"}]')
            check("JSON → 真表", r4["ok"] and r4["rows"] == 2,
                  str(r4.get("why", ""))[:60])
            assert scene_profile  # 只是引用一下，避免 lint 报未使用
        finally:
            U._scene_db_path = orig

    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
