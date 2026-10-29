#!/usr/bin/env python3
"""web_ingest 的「正文进库」测试（2026-10-29）。

跑法：python mve/test_web_ingest.py（服务器有 duckdb，本地没有）。

为什么单独立一条账：`web_docs` 原先只存搜索摘要 snippet，学习侧拿到的是一张
「有表没料」的表 —— 调研类问题（「这个仓库是干什么的」）在 SQL 上永远答不出，
于是它反复打磨 WHERE 过滤条件（生产实测 4 次重试全是同一句 SELECT 换 LIKE）。
正文必须真的落进 `body` 列，这条链路才可能通。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    import duckdb
    import web_ingest

    tmp = tempfile.mkdtemp()
    dbp = os.path.join(tmp, "scene.duckdb")
    # 绕开场景画像：这一层只验「存什么、存成什么样」，不验目录解析。
    web_ingest._db = lambda sid: dbp                       # noqa: ARG005
    web_ingest._scene_dir = lambda sid: tmp                # noqa: ARG005
    web_ingest._ledger = lambda sid: os.path.join(tmp, web_ingest.LEDGER)

    print("== 1. 老库补列（ADD COLUMN，不重建）==")
    con = duckdb.connect(dbp)
    con.execute("CREATE TABLE web_docs (query VARCHAR, title VARCHAR, "
                "url VARCHAR, snippet VARCHAR, fetched_at TIMESTAMP)")
    con.execute("INSERT INTO web_docs VALUES ('旧问题','旧标题','https://old',"
                "'旧摘要', NULL)")
    con.close()
    web_ingest.ensure(dbp)
    con = duckdb.connect(dbp, read_only=True)
    cols = [str(r[1]) for r in
            con.execute("PRAGMA table_info('web_docs')").fetchall()]
    n_old = int(con.execute("SELECT COUNT(*) FROM web_docs").fetchone()[0])
    con.close()
    check("body 列补上了", "body" in cols, str(cols))
    check("老行没被清掉（补列不是重建）", n_old == 1, f"{n_old} 行")

    print("== 2. save_docs 存正文 ==")
    n = web_ingest.save_docs(
        "T", "这个仓库是干什么的",
        [{"title": "voyager-vlml-mve", "url": "https://github.com/x/y",
          "snippet": "摘要只有两行"},
         {"title": "没抓到正文的页面", "url": "https://example.com/z",
          "snippet": "只有摘要"}],
        bodies={"https://github.com/x/y": "README 正文：这个项目做学习闭环。"})
    check("两行都进库", n == 2, f"n={n}")
    con = duckdb.connect(dbp, read_only=True)
    rows = con.execute(
        "SELECT url, snippet, body FROM web_docs WHERE query = ? "
        "ORDER BY url", ["这个仓库是干什么的"]).fetchall()
    con.close()
    got = {str(r[0]): (str(r[1] or ""), str(r[2] or "")) for r in rows}
    check("抓到正文的：body 是正文",
          got.get("https://github.com/x/y", ("", ""))[1].startswith("README 正文"),
          str(got.get("https://github.com/x/y"))[:60])
    check("没抓到的：body 空、snippet 留着（不留空壳也不编）",
          got.get("https://example.com/z", ("", "x"))[1] == ""
          and got.get("https://example.com/z", ("", ""))[0] == "只有摘要",
          str(got.get("https://example.com/z"))[:60])

    print("== 3. 按 url 去重（同页不同 query 只有一行）==")
    web_ingest.save_docs("T", "换个问法", [{"title": "voyager-vlml-mve",
                                            "url": "https://github.com/x/y"}],
                         bodies={"https://github.com/x/y": "正文第二版"})
    con = duckdb.connect(dbp, read_only=True)
    cnt, body = con.execute(
        "SELECT COUNT(*), MAX(body) FROM web_docs WHERE url = ?",
        ["https://github.com/x/y"]).fetchone()
    con.close()
    check("同一 url 只有一行", int(cnt) == 1, f"{cnt} 行")
    check("覆写成新内容", str(body) == "正文第二版", str(body)[:40])

    print("== 4. 学习侧真能查出来（SQL 不是摆设）==")
    con = duckdb.connect(dbp, read_only=True)
    hit = con.execute(
        "SELECT title, body FROM web_docs WHERE body LIKE '%学习闭环%' "
        "OR query LIKE '%仓库%' LIMIT 5").fetchall()
    con.close()
    check("按 body 内容查得到东西", bool(hit), str(hit)[:80])

    print(f"\n通过 {PASS} / 挂 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
