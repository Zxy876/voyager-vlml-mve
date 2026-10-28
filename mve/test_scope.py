#!/usr/bin/env python3
"""第 2.5 步（判定作用域）的测试：scope 判定器 + 口径词表不复活回归。

跑法：python mve/test_scope.py（服务器无 pytest，同 test_*.py 口径）。
单位守卫 _unit_guard 已删（2026-10-29 用户拍板「删」）—— 这里改成回归断言：
代码里不许再出现口径/语义词表守卫。
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
    print("== 1. 单位守卫已删（2026-10-29 用户拍板）：回归断言 ==")
    import voyager_vlml.consume as _C
    check("consume 不再有 _unit_guard", not hasattr(_C, "_unit_guard"))
    _src_c = open(_C.__file__, encoding="utf-8").read()
    check("consume 源码不再调用 _unit_guard", "_unit_guard(" not in _src_c)

    print("== 2. scope.classify：作用域判定器 ==")
    import scope

    r = scope.classify("B")
    check("场景 B（有 duckdb 数据源）→ learn",
          r.get("line") == "learn" and r.get("has_env") is True, str(r)[:80])
    r2 = scope.classify("__no_such_scene__")
    check("未知场景回落 local（本机即环境，2026-10-19 口径）→ learn",
          r2.get("line") == "learn", str(r2)[:80])
    check("scope_line 便捷取值", scope.scope_line("B") == "learn")
    r3 = scope.classify("Z" * 3)
    check("判定结果带 why（可解释）", bool(str(r3.get("why") or "").strip()))

    print("== 3. 入库口接线：口径词表守卫不复活 ==")
    # _absorb / learn_milestone_skills 的守卫段 —— 回归断言：不再调用 _unit_guard
    from voyager_vlml.consume import _absorb  # noqa: F401
    import inspect
    src = inspect.getsource(_absorb)
    check("_absorb 源码不再调用 _unit_guard", "_unit_guard" not in src)
    src2 = inspect.getsource(
        __import__("learn_orchestrate").learn_milestone_skills)
    check("learn_milestone_skills 源码不再调用 _unit_guard",
          "_unit_guard" not in src2)

    print("== 4. 工具线兜底（第 4 步）：claim 口径 ==")
    import asyncio
    import types
    import voyager_vlml.consume as C

    # stub 联网原语（不真联网）
    stub_net = types.ModuleType("net_tools")
    stub_net.web_search = lambda q, n=5: [
        {"title": "美元汇率", "url": "https://example.com/fx",
         "snippet": "1 美元 = 7.12 人民币"}]
    stub_net.web_fetch = lambda url, max_bytes=200000: {
        "url": url, "status": 200, "bytes": 100,
        "text": "今日美元兑人民币中间价 7.12。"}
    sys.modules["net_tools"] = stub_net
    # stub LLM：验证「只依据资料」的综合出口被调用
    _orig_cj = C.chat_json
    C.chat_json = lambda msgs, **kw: {"answer": "1 美元 ≈ 7.12 人民币（资料1）",
                                      "grounded": True}
    r = asyncio.get_event_loop().run_until_complete(
        C._tool_line_fallback("美元兑人民币汇率是多少？")) if False else \
        asyncio.run(C._tool_line_fallback("美元兑人民币汇率是多少？"))
    check("有资料有答案 → used=True", r.get("used") is True and bool(r.get("answer")))
    check("来源披露 url", "example.com" in str(r.get("sources")))

    # 搜索失败 → 如实不答（联网失败不是答一个像样的）
    def _boom(q, n=5):
        raise RuntimeError("403 限流")
    stub_net.web_search = _boom
    r2 = asyncio.run(C._tool_line_fallback("随便问点啥"))
    check("搜索失败 → used=False + note 如实",
          r2.get("used") is False and "搜索失败" in str(r2.get("note"))
          and not r2.get("answer"))

    # 有搜索没正文 → 如实不答
    stub_net.web_search = lambda q, n=5: [
        {"title": "t", "url": "https://example.com/x", "snippet": "s"}]
    stub_net.web_fetch = lambda url, max_bytes=200000: {
        "url": url, "status": 200, "bytes": 10, "text": "   "}
    r3 = asyncio.run(C._tool_line_fallback("另一个问题"))
    check("正文没取到 → used=False + 如实 note",
          r3.get("used") is False and bool(str(r3.get("note") or "").strip()))

    # 资料不足以回答（grounded=false）→ 不产出 answer
    stub_net.web_fetch = lambda url, max_bytes=200000: {
        "url": url, "status": 200, "bytes": 50, "text": "无关内容"}
    C.chat_json = lambda msgs, **kw: {"answer": "猜一个", "grounded": False}
    r4 = asyncio.run(C._tool_line_fallback("第三个问题"))
    check("grounded=false → used=False（不编造）",
          r4.get("used") is False and not r4.get("answer"))
    C.chat_json = _orig_cj
    sys.modules.pop("net_tools", None)

    print("== 5. 主流程接线与披露 ==")
    src_c = inspect.getsource(C.consume)
    check("consume 主流程调用 _tool_line_fallback", "_tool_line_fallback" in src_c)
    check("claim 标注在答案里", "工具线 claim" in src_c)
    check("base 披露 tool_line_used/tool_sources", 
          "tool_line_used" in src_c and "tool_sources" in src_c)
    # coach.py 导入会拉 vlml_env（chdir 到 VLML 目录，测试机没有）——
    # 改读源码文件验证白名单（避开环境副作用）。
    _coach_src = open(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "coach.py"),
        encoding="utf-8").read()
    check("coach 落盘白名单带 tool_line_used",
          "tool_line_used" in _coach_src and "referee_compared" in _coach_src)

    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
