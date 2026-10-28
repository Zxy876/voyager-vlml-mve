#!/usr/bin/env python3
"""对话页（/mve/chat）与 OpenAI 兼容层的测试。

这一页的定位是「完全照 DeepSeek R1 客户端构建」，所以测试也按 R1 的
这几件事逐条验：思考过程单独一通道、流式、会话历史、深度思考开关、
复制/重新生成/停止，以及**前端不许自带判定**（判定口径只有服务端一套）。

跑法：python mve/test_chat_page.py（服务器无 pytest，同 test_*.py 口径）。
"""

import inspect
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


SAMPLE = {
    "answer": "技术部9月的餐补和交通补贴总额是2120.0元。",
    "judgment_result": "written", "mastered": True, "scope_line": "learn",
    "used_skills": [{"name": "tech_allowance", "r": 1.0}],
    "sql": "SELECT SUM(a.amount) FROM allowance a WHERE a.month='2026-09'",
    "rows": 3, "real_rows": 3, "skills_found": 1, "topic_id": "consume_x",
    "referee_compared": False,
}


def main() -> int:
    import chat_page as CP
    import dashboard as D

    print("== 1. 思考轨迹：只转述 consume() 的结果，不臆造 ==")
    r = CP.reasoning_text(SAMPLE, scene_id="B", scene_label="工资")
    check("点明供给线", "学习线" in r)
    check("点名命中的技能与 r", "tech_allowance（r=1.0）" in r)
    check("贴出回填的 SQL", "SELECT SUM(a.amount)" in r)
    check("报实查行数", "实查返回 3 行" in r)
    check("报判定与掌握", "written" in r and "掌握 ✓" in r)
    check("措辞不再出现「场景：B」这种双冒号", "场景：B" not in r)

    r2 = CP.reasoning_text({"scope_line": "learn"}, scene_id="B")
    check("没有技能时如实说没命中", "没有命中任何配方" in r2)
    check("没有行数时不编造行数", "实查返回" not in r2)
    check("没有判定时不编造判定", "判定：" not in r2)

    r3 = CP.reasoning_text(dict(SAMPLE, critic_note="数据与问题对不上"))
    check("核对没过会写进轨迹", "核对没过" in r3)
    r4 = CP.reasoning_text(dict(SAMPLE, tool_line_used=True,
                                tool_sources=[{"url": "x"}]))
    check("工具线标注 claim", "claim" in r4)
    check("空结果返回空串", CP.reasoning_text(None) == "")

    print("== 2. 与 R1 协议面对位 ==")
    c, rz, x = D._openai_answer(SAMPLE, "B", "A")
    check("正文带判定披露", "—— MVE｜" in c and "written" in c)
    check("思考走独立通道（reasoning_text）", bool(rz) and "技能库" in rz)
    check("x_mve 带 skill 供前端标复用", x.get("skill") == "tech_allowance")
    check("x_mve 带 scene/scope/mastered",
          x.get("scene") == "B" and x.get("scope_line") == "learn"
          and x.get("mastered") is True)
    empty = D._openai_answer({"errors": ["boom"]}, "B", "A")
    check("没答出来也给出可读交代", "没答出来" in empty[0] and "boom" in empty[0])

    src_chat = inspect.getsource(D.Handler._openai_chat)
    check("支持 stream=true", "stream" in src_chat)
    src_stream = inspect.getsource(D.Handler._openai_stream)
    check("发 reasoning_content 增量", "reasoning_content" in src_stream)
    check("发 [DONE] 收尾", "[DONE]" in src_stream)
    check("等待期间发心跳（不干等超时）", "keep-alive" in src_stream)
    check("断连不抛（停止生成）", "BrokenPipeError" in src_stream)
    src_hdr = inspect.getsource(D.Handler._sse_headers)
    check("SSE 头齐全", "text/event-stream" in src_hdr and "chunked" in src_hdr)
    check("HTTP/1.1（分块编码前提）",
          getattr(D.Handler, "protocol_version", "") == "HTTP/1.1")

    print("== 3. 会话历史与示例题（不许硬编码题面） ==")
    src_samples = inspect.getsource(D._samples_by_scene)
    check("示例题来自真实消费账", "_human_imports" in src_samples)
    check("只取判定 written 的", '!= "written"' in src_samples)
    src_models = inspect.getsource(D.Handler._openai_models)
    check("模型列表带 samples", "samples" in src_models)

    print("== 4. 页面本身（R1 客户端的骨架） ==")
    h = CP.CHAT_HTML
    for name, needle in [
        ("侧栏会话历史", 'id="hist"'),
        ("新对话", 'id="newChat"'),
        ("深度思考开关", 'id="thinkSw"'),
        ("已深度思考（用时 N 秒）", "已深度思考"),
        ("停止生成", 'id="stop"'),
        ("复制", "⧉ 复制"),
        ("重新生成", "重新生成"),
        ("模型下拉（场景）", 'id="menu"'),
        ("深浅色切换", 'id="themeBtn"'),
        ("输入框", 'id="q"'),
        ("免责声明", "内容由 MVE 生成"),
    ]:
        check(name, needle in h)
    check("流式解析（读 ReadableStream）", "getReader()" in h)
    check("消费 delta.reasoning_content", "reasoning_content" in h)
    # 铁律：判定只能来自服务端。前端出现赋值式判定就是长出第二把尺子。
    for bad in ["mastered =", "judgment_result =", "mastered:", "judgment_result:"]:
        check(f"前端不自己算 {bad.strip()}", bad not in h)

    print("== 5. 前缀注入（/mve 代理子路径） ==")
    old = D.PREFIX
    try:
        D.PREFIX = ""
        check("无前缀时不注入", "MVE_PREFIX=" not in D._chat_html())
        D.PREFIX = "/mve"
        injected = D._chat_html()
        check("有前缀时注入 window.MVE_PREFIX",
              'window.MVE_PREFIX="/mve"' in injected)
        check("注入点在 <head> 之后",
              injected.index("MVE_PREFIX") < injected.index("</head>"))
    finally:
        D.PREFIX = old

    print("== 6. 路由与消费通路 ==")
    src_get = inspect.getsource(D.Handler.do_GET)
    check("GET /chat 已注册", '"/chat", "/chat.html"' in src_get)
    check("返回 text/html", "self._send(200, _chat_html()" in src_get)
    src_file = open(os.path.join(_HERE, "dashboard.py"), encoding="utf-8").read()
    check("主面板有「对话页」入口", 'id="gotoChat"' in src_file)
    src_ask = inspect.getsource(D.Handler._mve_ask)
    check("端点走 consume()", "consume(" in src_ask)
    _code = "\n".join(ln.split("#")[0] for ln in src_ask.splitlines())
    check("不接 coach.explain 老路", "coach.explain" not in _code)
    check("模型后缀切场景后还原", "switch_scene" in src_ask)

    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
