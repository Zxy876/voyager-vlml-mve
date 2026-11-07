#!/usr/bin/env python3
"""学习侧取证落库（web_ingest_learning）测试 + 三条设计不变量的守卫。

跑法：python mve/test_web_ingest_learning.py（服务器有 duckdb，本地没有）。

⚠️ 2026-11-07 用户定调的设计不变量（写死在这里防回退）：

  **web_docs / web_facts 一开始就是空的，只有真的取证之后才落库。**
  这是**设计**，不是缺数据：
  · 这两张表的内容是「联网取回的真实资料」—— 预置/造数据会让表里出现
    **不实内容**，幻觉防线（判官看材料账）就失效了；
  · 落库的是 **claim**（confirmed=False），人确认后才升级 fact、不进学习线
    —— 这就是「人在回路」那一刀（web_ingest 模块头的语义）。

  所以：**任何预置 web_docs 数据的路径都是 bug**，本测试逐个守住。
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


def _fresh_db(tmp: str) -> str:
    import duckdb
    dbp = os.path.join(tmp, "scene.duckdb")
    con = duckdb.connect(dbp)
    con.execute("CREATE TABLE web_docs (query VARCHAR, title VARCHAR, "
                "url VARCHAR, snippet VARCHAR, body VARCHAR, "
                "fetched_at TIMESTAMP)")
    con.execute("CREATE TABLE web_facts (item VARCHAR, source_url VARCHAR, "
                "confidence DOUBLE, confirmed BOOLEAN)")
    con.close()
    return dbp


def main() -> int:
    import duckdb
    import web_ingest
    import web_ingest_learning as wil

    tmp = tempfile.mkdtemp()
    dbp = _fresh_db(tmp)
    web_ingest._db = lambda sid: dbp                       # noqa: ARG005
    web_ingest._scene_dir = lambda sid: tmp                # noqa: ARG005
    web_ingest._ledger = lambda sid: os.path.join(tmp, web_ingest.LEDGER)

    # ---- 1. per_site 形状（Sherlock 这类跨站点探测）----
    print("== 1. per_site 形状映射 ==")
    ret = {"username": "torvalds", "per_site": [
        {"site": "GitHub", "verdict": "CLAIMED", "http": 200,
         "url": "https://www.github.com/torvalds"},
        {"site": "Twitter", "verdict": "AVAILABLE", "http": 404,
         "url": "https://twitter.com/torvalds"}]}
    rows = wil._rows_from_return(ret)
    check("拆出 4 行（2 站点 + 2 状态码）", len(rows) == 4, f"实得 {len(rows)}")
    gh = [r for r in rows if r["attribute"] == "GitHub"]
    check("站点名作 attribute", len(gh) == 1 and gh[0]["value"] == "CLAIMED")
    # ⚠️ 状态码属性名必须带站点名：web_facts 按 attribute 摊平，同名属性多行
    # 并存时 PIVOT 用 first(value) 只留一个 → 不带站点名会互相顶掉。
    st = [r for r in rows if "状态码" in r["attribute"]]
    check("状态码属性名带站点名", all(" " in r["attribute"] for r in st),
          "、".join(r["attribute"] for r in st))

    # ---- 2. 真落库 + 维度 SQL 查得到 ----
    print("== 2. 落库后维度 SQL 查得到 ==")
    n = wil.ingest("T", "查询用户名 torvalds", "mcp_sherlock_lookup_username", ret)
    check("落库有行", n > 0, f"n={n}")
    con = duckdb.connect(dbp, read_only=True)
    try:
        d = con.execute("SELECT count(*) FROM web_docs").fetchone()[0]
        f = con.execute("SELECT count(*) FROM web_facts").fetchone()[0]
        check("web_docs 有行", d > 0, f"{d} 行")
        check("web_facts 有行", f > 0, f"{f} 行")
        cols = [r[0] for r in con.execute("DESCRIBE web_facts").fetchall()]
        check("web_facts 有站点列", "GitHub" in cols, "、".join(cols))
        check("web_facts 有状态码列", "GitHub http 状态码" in cols,
              "、".join(cols))
        row = con.execute("SELECT item, source_url, confirmed FROM web_facts "
                           "WHERE item LIKE '%torvalds%'").fetchall()
        # 两个站点各一行（GitHub + Twitter）——不是只有 GitHub 一行
        check("维度 SQL① 查得到 item", len(row) == 2, str(row))
        # 落的是 claim（confirmed=False），不是 fact
        check("落的是 claim（confirmed=False）",
              bool(row) and all(r[2] is False for r in row))
        row2 = con.execute("SELECT url FROM web_docs WHERE query LIKE "
                           "'%torvalds%'").fetchall()
        check("维度 SQL② 查得到 url", len(row2) == 2, str(row2))
    finally:
        con.close()

    # ---- 3. results 形状（搜索类）----
    print("== 3. results 形状映射 ==")
    rows2 = wil._rows_from_return([{"title": "T1", "url": "https://a.com",
                                     "snippet": "S1"}])
    check("搜索结果拆出 1 行", len(rows2) == 1)
    check("搜索结果带 url", rows2 and rows2[0]["url"] == "https://a.com")

    # ---- 4. 单篇正文形状（fetch 类）----
    print("== 4. 单篇正文形状映射 ==")
    rows3 = wil._rows_from_return({"url": "https://b.com",
                                    "text": "正文内容" * 30})
    check("正文拆出 1 行", len(rows3) == 1)
    check("正文进 body", rows3 and len(rows3[0]["body"]) > 100)

    # ---- 5. 认不出的返回不硬塞 ----
    print("== 5. 认不出的返回 ==")
    check("无法识别的返回不落库",
          wil._rows_from_return({"foo": "bar"}) == [] or
          not wil._rows_from_return({"foo": "bar"}))
    check("空返回不落库", wil.ingest("T", "q", "t", None) == 0)

    # ---- 6. 设计不变量：web_docs 无预置数据路径 ----
    print("== 6. 不变量：web_docs 只在取证后写 ==")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    import re
    offenders = []
    for root, _dirs, files in os.walk(repo):
        if "/.git" in root or "__pycache__" in root:
            continue
        for fn in files:
            if not fn.endswith(".py") or fn.startswith("test_"):
                continue
            fp = os.path.join(root, fn)
            try:
                src = open(fp, encoding="utf-8").read()
            except Exception:
                continue
            # 直接往 web_docs 插数据的（绕过 save_docs）就是预置路径
            for mt in re.finditer(
                    r"INSERT\s+INTO\s+web_docs", src, re.IGNORECASE):
                offenders.append(f"{fn}:{src[:mt.start()].count(chr(10)) + 1}")
    check("没有绕过 save_docs 直接 INSERT web_docs 的路径",
          not offenders, "、".join(offenders))

    # ---- 7. 设计不变量：观测汇入顶层（漏账那条）----
    print("== 7. 不变量：维度层观测汇入顶层 ==")
    lo = os.path.join(repo, "mve", "learn_orchestrate.py")
    src = open(lo, encoding="utf-8").read()
    lines = src.split("\n")
    # 只管**维度层**的那次 solve：`obs1, code1 = await local_agent.solve(...)`
    # —— 它的观测在 obs1 里，不汇进 observations 的话，顶层汇总看不到
    # 「维度层真取到了数」→ calls=0 → 真空防线判 no_data（2026-11-07 实测）。
    # 顶层自己的 solve（`observations, sql = await ...`）本来就直写顶层，
    # 不在此列。
    missing = []
    found = 0
    for i, ln in enumerate(lines):
        if "obs1, code1 = await" not in ln:
            continue
        found += 1
        # 往后扫到下一个 obs1 赋值（或 60 行封顶）—— VLML 分支的 extend
        # 在 else 分支末尾，离赋值处有 30+ 行。
        seg = []
        for j in range(i, min(len(lines), i + 60)):
            seg.append(lines[j])
            if j > i and "obs1, code1 = await" in lines[j]:
                break
        if "observations.extend(obs1)" not in "\n".join(seg):
            missing.append(f"行 {i + 1}")
    check("维度层每次 solve 后都汇入 observations",
          found > 0 and not missing,
          f"共 {found} 处维度层 solve" + ("，缺: " + "、".join(missing)
                                        if missing else "，全部已汇入"))

    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
