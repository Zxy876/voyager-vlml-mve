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

    # 改写重试（2026-10-29）：首答失败 → LLM 改写搜索词 → 再来一轮。
    # 题面带指代（「这家公司」）直接搜命中率低，实测同题换通用措辞就能取回。
    _fetch_calls = {"n": 0}
    def _fetch_stateful(url, max_bytes=200000):
        _fetch_calls["n"] += 1
        if _fetch_calls["n"] == 1:
            return {"url": url, "status": 200, "bytes": 10, "text": "  "}
        return {"url": url, "status": 200, "bytes": 100,
                "text": "2026 年互联网行业平均年薪 30 万元。"}
    stub_net.web_fetch = _fetch_stateful
    stub_net.web_search = lambda q, n=5: [
        {"title": "行业薪资", "url": "https://example.com/sal", "snippet": "s"}]
    def _cj_dispatch(msgs, **kw):
        _sysc = str((msgs or [{}])[0].get("content") or "")
        if "改写成适合搜索引擎" in _sysc:
            return {"query": "互联网行业 平均薪资 水平"}
        return {"answer": "行业平均年薪约 30 万元（资料1）", "grounded": True}
    C.chat_json = _cj_dispatch
    r5 = asyncio.run(C._tool_line_fallback("这家公司的待遇好不好啊"))
    check("首答失败 → 改写重试成功 used=True",
          r5.get("used") is True and "30 万" in str(r5.get("answer")))
    check("改写后的查询真被拿去取过正文", _fetch_calls["n"] >= 2)

    # 多源重试（2026-10-29 用户拍板「一定做」）：单个源抓不到不该拖垮整轮
    _urls = []
    def _fetch_multi(url, max_bytes=200000):
        _urls.append(url)
        if "bad" in url:
            raise RuntimeError("403 反爬")
        return {"url": url, "status": 200, "bytes": 100,
                "text": "2026 年互联网行业平均年薪 30 万元。"}
    stub_net.web_fetch = _fetch_multi
    stub_net.web_search = lambda q, n=5: [
        {"title": "坏源", "url": "https://example.com/bad1", "snippet": "s"},
        {"title": "坏源2", "url": "https://example.com/bad2", "snippet": "s"},
        {"title": "好源", "url": "https://example.com/good", "snippet": "s"}]
    C.chat_json = lambda msgs, **kw: (
        {"query": "互联网行业 平均薪资 水平"}
        if "改写成适合搜索引擎" in str((msgs or [{}])[0].get("content") or "")
        else {"answer": "行业平均年薪约 30 万元（资料1）", "grounded": True})
    r6 = asyncio.run(C._tool_line_fallback("其他公司待遇怎么样"))
    check("单个源反爬 → 换源后仍然取到（used=True）",
          r6.get("used") is True)
    check("坏源被重试过（同一 url 至少两次）",
          _urls.count("https://example.com/bad1") >= 2, f"urls={_urls[:6]}")
    check("好源被取到且在 sources 里",
          any("good" in str(x.get("url")) for x in (r6.get("sources") or [])))
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

    print("== 6. 信息路规划接线（2026-10-29 拍板：先规划、再可达性检查）==")
    src_plan = inspect.getsource(C.consume)
    check("consume 主流程调用 _plan_routes", "_plan_routes(" in src_plan)
    check("对比题 db 半场目标被规划裁剪（「本轮只做 db 半场」）",
          "本轮只做 db 半场" in src_plan and "_solve_task" in src_plan)
    # 「所有问题都应由外部规划去完成」（2026-10-29 用户拍板）：
    # 走不走环境由规划决定，不由词表决定；词表只做规划员挂掉时的兜底。
    check("环境门控 = 规划门控（_use_env）",
          '_use_env = bool(_plan.get("db"))' in src_plan)
    check("词表已彻底删除（源码零调用 + 函数不存在）",
          "_is_data_question" not in src_plan
          and not hasattr(C, "_is_data_question"))
    _all_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "voyager_vlml", "consume.py"),
                    encoding="utf-8").read()
    check("规划员不可用时不碰环境（兜底 planned=False + db=False）",
          "规划员不可用 → 不碰环境" in _all_src
          and '"db": False, "web": False' in _all_src)
    check("混合题两半由规划合成（_compose_halves）",
          "_compose_halves(" in src_plan and "库内半场" in src_plan)
    # 2026-10-29 服务器实测（腾讯校招起薪）：规划只排 web 路，却仍去技能库
    # 检索并实查库内配方 → 判官按「主体范围错误」退回，白烧一轮。检索也得看规划。
    check("技能检索受规划门控（无 db 路不检索库内配方）",
          "_retrieve_skills(question, topic_id or topic_hint, _scene_id)"
          in src_plan and "if _use_env else [])" in src_plan)
    # 同一实测的另一半：没查库的题不许报「真空」判词、不许叫人「往场景目录里放数据」。
    check("没查库的题不套真空判词 / 不叫人放数据",
          "规划里没有 db 路 —— 这一问走工具线（联网），不查场景库：" in src_plan
          and '("" if critic_ok else str(critic_why or "")[:300])\n'
              '                            if _use_env else "")' in src_plan)
    # 2026-10-29 撤销自创的「web 题学方法不学数值」（qa_cache 方法沉淀 + 消费侧复用）。
    # 事实口径：DeepSeek 的联网经验在训练期进权重，运行时不留检索记忆；MVE 侧仿造属发明。
    # 这条是回归守卫：机制不许复活，否则又是一份自创。
    check("自创「学方法」机制已撤且不许复活",
          "record_web_method(" not in _all_src
          and "_web_tips(" not in _all_src
          and '"method_learned"' not in _all_src
          and '"method_reused"' not in _all_src)
    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
