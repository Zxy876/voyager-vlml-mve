#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「这段返回是正文，还是工具层的失败说明」—— 判据唯一的回归测试。

背景（2026-10-22 场景 B 实测）：MCP 的 fetch 工具把失败包成 `isError: false`
的**普通文本**返回：

    "Error: Could not access the webpage (Client error '403 Forbidden' ...)"
    "Error: Refusing to fetch http://... (could not resolve host ...)"

技能代码只判 `if not page:` → 报错字符串非空 → 被当成正文写进 `RESULT`。
后果是学到的 5 条配方里 4 条，答案就是 403/521/DNS 错误页 —— **学的时候没人拦，
就固化成技能了**，消费时"命中"反而答错。

助产士那边为什么不会出这种事（`src/lib/research/agent.ts`）：
    if (result.ok && result.text) gathered.push(正文)
    else                          report.push({status:"failed"})   ← 失败不进材料
失败项在结构上进不了材料，所以不需要任何 403 字符串特判。MVE 补的是同一层：
`_evidence_item` 判 ok=False → 材料账拒收 → 闸门判不闭合 → 不写技能。

这组测试钉死三件事：
  1. 失败文本认得出（三种真实措辞）；
  2. **不误伤正文**（正文里正常讲 HTTPS 403 必须判成功）—— 上一版裸关键词
     黑名单就是在这儿翻车的；
  3. 判据只有一份：执行域拿到的 `ok_text()` 与证据层 `_evidence_item` 同口径，
     不会出现「闸门认失败、模型认成功」。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_agent as L  # noqa: E402

FAILS: list[str] = []

# 三个都是服务器上真实抓回来的返回值（2026-10-22 重放 consume_ca73e6f8::* 时录的）
REAL_FAILURES = [
    ("403 zhihu",
     {"result": "Error: Could not access the webpage (Client error '403 Forbidden' "
                "for url 'https://www.zhihu.com/question/588482437'\nFor more "
                "information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403)"}),
    ("521 csdn",
     {"result": "Error: Could not access the webpage (Server error '521 ' for url "
                "'https://blog.csdn.net/qq_37175369/article/details/84868765'\nFor "
                "more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/521)"}),
    ("dns kaotop",
     {"result": "Error: Refusing to fetch http://www.kaotop.com/xueshu/4201.html "
                "(could not resolve host 'www.kaotop.com': [Errno -2] Name or "
                "service not known). This server blocks requests to private/"
                "internal addresses."}),
]

REAL_BODY = ("三角函数是数学中常见且重要的概念，它们在几何学、物理学、工程学等领域都有着"
             "广泛的应用。其中 sin(2x) 是三角函数中的一个特殊形式，它具有一系列独特的"
             "性质和应用。") * 8


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS  {name}")
    else:
        FAILS.append(name)
        print(f"FAIL  {name}  {detail}")


def test_real_failures_are_failures() -> None:
    for name, ret in REAL_FAILURES:
        check(f"真实失败文本判失败：{name}",
              L._looks_like_tool_error(L._flat_text(ret)) is True)


def test_normal_body_is_not_failure() -> None:
    check("长正文判成功", L._looks_like_tool_error(L._flat_text({"result": REAL_BODY})) is False)
    # 关键反例：上一版裸关键词黑名单把这段判成了失败
    short = "HTTP 403 表示服务器理解请求但拒绝执行，常见于没有访问权限。"
    check("正文里正常讲 403 不算失败", L._looks_like_tool_error(short) is False)
    check("搜索结果列表不算失败",
          L._looks_like_tool_error(L._flat_text([{"title": "怎样用 matlab", "url": "http://x"}])) is False)
    check("空串不算失败（由调用方判空）", L._looks_like_tool_error("") is False)


def test_evidence_account_rejects_failures() -> None:
    for name, ret in REAL_FAILURES:
        item = L._evidence_item("mcp_ddg_fetch_content", ret)
        check(f"证据账拒收：{name}", item["ok"] is False, str(item)[:120])
        check(f"拒收时给出原因：{name}", bool(str(item.get("error") or "").strip()))
    ok_item = L._evidence_item("mcp_neko_web_fetch", {"result": REAL_BODY})
    check("正文进材料账", ok_item["ok"] is True)
    check("正文材料带正文", REAL_BODY[:20] in ok_item["text"])
    # 结构化失败照旧（dict 里的 error / status>=400）
    check("dict error 仍判失败",
          L._evidence_item("x", {"error": "boom"})["ok"] is False)
    check("status 500 仍判失败",
          L._evidence_item("x", {"status": 500, "text": "err"})["ok"] is False)


def test_one_verdict_shared_by_execution_and_gate() -> None:
    """执行域里的 `ok_text` 必须与证据层同一个判据（否则模型与闸门各说各话）。"""
    ns = L._ns_base("/tmp", "", tracer=None)
    ok_text = ns.get("ok_text")
    check("执行域有 ok_text", callable(ok_text))
    if not callable(ok_text):
        return
    for name, ret in REAL_FAILURES:
        check(f"ok_text 与证据层一致（失败）：{name}", ok_text(ret) is False)
    check("ok_text 与证据层一致（正文）", ok_text({"result": REAL_BODY}) is True)
    # 模型写的代码要能真的用它
    code = ("hits = [" + ",".join('"u%d"' % i for i in range(3)) + "]\n"
            "RESULT = {'ok': 0}\n"
            "for h in hits:\n"
            "    page = {'result': 'Error: Could not access the webpage "
            "(Client error \\'403 Forbidden\\')'}\n"
            "    if ok_text(page):\n"
            "        RESULT = {'ok': 1}\n"
            "        break\n")
    box = L._run_restricted(code, ns, timeout=10)
    check("技能代码能用 ok_text 跳过失败来源",
          bool(box.get("ok")) and ns.get("RESULT") == {"ok": 0}, str(box)[:160])


def test_expose_requires_real_scene_id() -> None:
    """`expose_as_actions` 收到垃圾值要报错，不能静默注册到 `str(obj)` 键下。"""
    import mcp_gateway as G
    gw = G.get_gateway()
    for bad in ("", "   ", {}, {"name": "x"}):
        try:
            gw.expose_as_actions(bad)
            check(f"空场景值应报错：{bad!r}", False)
        except ValueError:
            check(f"空场景值报错：{bad!r}", True)
        except Exception as e:  # noqa: BLE001
            check(f"空场景值报错：{bad!r}", "需要场景 id" in str(e), f"{type(e).__name__}: {e}")


def main() -> int:
    test_real_failures_are_failures()
    test_normal_body_is_not_failure()
    test_evidence_account_rejects_failures()
    test_one_verdict_shared_by_execution_and_gate()
    test_expose_requires_real_scene_id()
    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：", "; ".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
