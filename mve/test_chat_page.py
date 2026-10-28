#!/usr/bin/env python3
"""对话页（/mve/chat）的测试：它是 OpenAI 兼容端点的一层壳，不该自带判定。

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


def main() -> int:
    import dashboard as D

    print("== 1. 对话页只调端点，不自带判定 ==")
    h = D.CHAT_HTML
    check("调 /v1/chat/completions", "/v1/chat/completions" in h)
    check("调 /v1/models 列场景", "/v1/models" in h)
    check("读服务端 x_mve 展示判定", "x_mve" in h)
    # 关键：判定/掌握/复用技能只能来自服务端，前端不许自己算。
    check("前端不自己算 mastered", "mastered:" not in h and "mastered =" not in h)
    check("工具线 claim 有可见标注", "工具线 claim" in h)
    check("未掌握有可见标注", "未掌握" in h)

    print("== 2. 前缀注入（/mve 代理子路径） ==")
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

    print("== 3. 路由与主面板入口 ==")
    src_get = inspect.getsource(D.Handler.do_GET)
    check("GET /chat 已注册", '"/chat", "/chat.html"' in src_get)
    check("返回 text/html", "self._send(200, _chat_html()" in src_get)
    src_file = open(os.path.join(_HERE, "dashboard.py"), encoding="utf-8").read()
    check("主面板有「对话页」入口", 'id="gotoChat"' in src_file)
    check("入口跳转带前缀", "(window.MVE_PREFIX||'')+'/chat'" in src_file)

    print("== 4. 与面板同一条消费通路 ==")
    src_chat = inspect.getsource(getattr(D.Handler, "_openai_chat"))
    check("端点走 consume()", "consume(" in src_chat)
    # 注释里点名「别接 coach.explain」是留证，不算调用——剥掉注释行再判。
    _code = "\n".join(ln.split("#")[0] for ln in src_chat.splitlines())
    check("端点不接 coach.explain 老路", "coach.explain" not in _code)
    check("支持 model 后缀切场景", "switch_scene" in src_chat)

    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
