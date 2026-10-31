#!/usr/bin/env python3
"""来源绑定（A 项）的测试：维度点名的表，必须真被查过。

跑法：python mve/test_source_binding.py（服务器无 pytest，同 test_*.py 口径）。

为什么有这一层（生产实证 2026-10-31 Scene H）：
    维度问「web_facts 表中该仓库的 claim 字段内容是什么？」→ 执行层调
    gh_repo_info，把仓库 description 当 claim 交上去 → 闸门看材料非空就闭合
    → 里程碑假亮 4/5、5/5。这条测试把那个缺口钉死：判据不看「有没有材料」，
    而看「材料是不是题目点名的那一份」。
"""

import os
import sys

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
    import source_binding as sb

    print("== 0. 真库直查（有 duckdb + 场景库时才算；本地无 duckdb 则跳过）==")
    _real = sb.table_names("/root/mve/scene_data/H/scene.duckdb")
    if _real:
        check("真库表清单读得出来（show tables）", "web_facts" in _real,
              "、".join(_real[:6]))
        _H = "/root/mve/scene_data/H/scene.duckdb"
        check("真库题面点名识别正确",
              sb.named_sources("web_facts 表中该仓库的 confirmed 字段值", _H)
              == ["web_facts"])
    else:
        print("  （跳过：这台机器没有那个场景库，或没装 duckdb）")

    # 本地没有 duckdb / 没有场景库都不影响这几项：表清单直接注入。
    sb.table_names = lambda _p: ["web_facts", "web_docs", "web"]
    DB = "ignored.duckdb"

    print("== 1. 点名识别：只认本库真实表名，不做同义词猜测 ==")
    q = "web_facts 表中该仓库的 confirmed 字段值是什么？"
    check("点名 web_facts", sb.named_sources(q, DB) == ["web_facts"],
          str(sb.named_sources(q, DB)))
    check("同题也点名 web（独立标识符）→ 两张都算",
          sb.named_sources("web 和 web_facts 都对一下", DB) == ["web_facts", "web"])
    check("标识符边界：website 不算命中 web",
          sb.named_sources("看看 website 上怎么写的", DB) == [],
          str(sb.named_sources("看看 website 上怎么写的", DB)))
    check("没点名任何表 → 不约束", sb.named_sources("这个仓库有多少 star？", DB) == [])

    print("== 2. 绑定判定：入参有没有点到那张表 ==")
    ev_ok = [{"tool": "query_sql", "ok": True, "args": "SELECT confirmed FROM web_facts LIMIT 5",
              "text": "{\"columns\": [\"confirmed\"], \"rows\": [[false]]}"}]
    ok, why = sb.check(q, ev_ok, DB)
    check("查了 web_facts 且成功 → 绑定", ok is True and why == "", why)

    # ⚠️ 就是这一条对应生产事故：真取到了内容，但不是这张表。
    ev_wrong = [{"tool": "gh_repo_info", "ok": True,
                 "args": "Zxy876/voyager-vlml-mve",
                 "text": "{\"repo\": \"...\", \"description\": \"LLM Agent 的在线强化学习系统\"}"}]
    ok, why = sb.check(q, ev_wrong, DB)
    check("只用了 gh_repo_info → 不绑定", ok is False, why)
    check("判词指名道姓：说清问的是哪张表、实际用了什么",
          "web_facts" in why and "gh_repo_info" in why, why)

    ev_fail = [{"tool": "query_sql", "ok": False,
                "args": "SELECT confirmed FROM web_facts WHERE item = '...'",
                "error": "KeyError: 0"}]
    ok, why = sb.check(q, ev_fail, DB)
    check("查了但失败 → 仍不绑定（没取到内容不算答到）", ok is False, why)
    check("判词区分「查了但没取到」与「一步没查」",
          "没取到内容" in why, why)

    ev_mixed = [
        {"tool": "gh_read", "ok": True, "args": "Zxy876/voyager-vlml-mve/README.md",
         "text": "# Voyager VLML"},
        {"tool": "query_sql", "ok": True, "args": "select * from web_facts limit 1",
         "text": "{\"rows\": [[...]]}"},
    ]
    check("混着用：只要有一条真查到了就绑定", sb.check(q, ev_mixed, DB)[0] is True)

    print("== 3. 无法归因时不拦（判据只判它看得见的东西）==")
    ev_old = [{"tool": "query_sql", "ok": True, "text": "{\"rows\": [[1]]}"}]
    check("老材料账没有 args → 放过，不凭空判死", sb.check(q, ev_old, DB)[0] is True)
    check("证据为空且无表名约束 → 放过",
          sb.check("这个仓库有多少 star？", [], DB)[0] is True)

    print("== 4. 执行前提示与判据同口径 ==")
    hint = sb.bind_hint(q, DB)
    check("提示里点名了那张表", "web_facts" in hint, hint[:60])
    check("提示说明了「别的来源顶替不算」", "不算答到" in hint)
    check("没点名表时不给提示（不打扰）", sb.bind_hint("有多少个提交？", DB) == "")

    print("== 5. 材料账要记入参（_recorder）+ 回执要带入参 ==")
    import local_agent as la
    ev: list[dict] = []
    tracer: list[str] = []

    @la._recorder(tracer, "query_sql", ev)
    def _fake(sql: str = "") -> dict:
        return {"columns": ["confirmed"], "rows": [[False]]}

    _fake("SELECT confirmed FROM web_facts")
    check("成功调用记了 args", "web_facts" in str(ev[0].get("args") or ""),
          str(ev[0].get("args")))
    check("成功调用仍是材料（ok=True）", ev[0].get("ok") is True)

    ev2: list[dict] = []

    @la._recorder([], "query_sql", ev2)
    def _boom(sql: str = "") -> dict:
        raise KeyError(0)

    try:
        _boom("SELECT * FROM web_docs")
    except KeyError:
        pass
    check("失败调用也记 args（才判得出「查了但没取到」）",
          "web_docs" in str(ev2[0].get("args") or ""), str(ev2[0].get("args")))
    check("失败调用不进材料（ok=False）", ev2[0].get("ok") is False)

    rc = la._env_receipt(ev + ev2, ["query_sql", "gh_repo_info"])
    check("环境回执里带入参（模型才判得出「我问错了对象」）",
          "query_sql(SELECT confirmed FROM web_facts)" in rc, rc[:120])
    check("环境回执不判对错（只陈述事实）",
          "✅" not in rc and "失败" not in rc.split("·")[0])

    print("== 6. 接进学习主循环：判据与提示都在 ==")
    from pathlib import Path
    lo = (Path(__file__).resolve().parent / "learn_orchestrate.py").read_text(
        encoding="utf-8")
    check("维度闭合前跑来源绑定判定", "source_binding as _sb2" in lo
          and "_sb2.check(_q1, _ev1, _bind_db)" in lo)
    check("执行前注入绑定提示（不是事后才知道）", "bind_hint(_q1, _bind_db)" in lo)
    check("不绑定 → 本次维度判不闭合（ok1/_inner_ok 双清零）",
          "if not _bind_ok:\n                            ok1 = False\n"
          "                            _inner_ok = False" in lo)

    print("== 7. 菜单要给返回形状（模型不该靠试错猜 API）==")
    _desc = {p["name"]: str(p.get("desc") or "") for p in la.PRIMITIVES}
    check("query_sql 说明写清 {columns, rows}", "columns" in _desc["query_sql"]
          and "rows" in _desc["query_sql"], _desc["query_sql"][:60])
    check("query_sql 说明给了取值的写法（r[\"rows\"][0][0]）",
          'r["rows"][0][0]' in _desc["query_sql"])
    check("read_file 说明写清返回 text 字段", "text" in _desc["read_file"])
    check("list_files 说明写清是对象列表", "is_dir" in _desc["list_files"])

    print("== 8. 真空防线不许被「单参调用」悄悄关掉 ==")
    # 2026-10-31 H 实测：needs_data(question) 少传 has_env → 恒 False →
    # 「没取到真数」这条防线对所有场景失效（那一轮 calls=4/rows=0 被判 learned
    # 且「里程碑解锁：是」）。这里扫源码，任何生产调用都必须带第二个实参。
    import re as _re
    _bad = []
    for f in sorted((Path(__file__).resolve().parent).glob("*.py")):
        if f.name.startswith("test_"):
            continue
        src = f.read_text(encoding="utf-8")
        for mm in _re.finditer(r"needs_data\(\s*([^)]*)\)", src):
            args = mm.group(1).strip()
            if args.startswith("question") and "," not in args:
                _bad.append(f"{f.name}: needs_data({args[:30]})")
            if args and not args.startswith(("question", "q", "task", "has_env")):
                continue
    check("生产代码里没有「只传 question」的 needs_data 调用", not _bad,
          "；".join(_bad))
    _lo2 = (Path(__file__).resolve().parent / "learn_orchestrate.py").read_text(
        encoding="utf-8")
    check("学习侧把环境判定传进去（scene_has_env）",
          "needs_data(question, _rw_n.scene_has_env(scene))" in _lo2)

    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
