#!/usr/bin/env python3
"""对话记忆 → 指代消解（2026-10-29）。

用户实测指出的怪：上一轮答完「九月份一共支付 137,100」，下一轮问
「相比于网上其他公司，这家公司的待遇好不好」——「这家公司」没人认识，
工具线联网搜回来一家不相干的公司。根因：对话页把完整 messages 发上来了，
但 `_openai_chat` 只取最后一条 user 就把历史扔了 —— consume 每次都失忆。

修法：入口做一次指代消解（`_resolve_history`），下游照旧只吃一句独立问句。
本测试钉住：
  ① 没有历史 → 零开销、零 LLM 调用、原句照答（行为不变）；
  ② 有历史 → 消解结果被采用；LLM 挂/返回空 → 如实回落原句；
  ③ 历史只取最近 8 轮、每条截断（防窗口爆）；
  ④ 面板把「最后一条 user 之前」的历史传进 consume（流式/非流式两条路）。
跑法：python mve/test_context_memory.py
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(HERE), str(REPO), str(REPO / "voyager_vlml")):
    if p not in sys.path:
        sys.path.insert(0, p)

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    import voyager_vlml.consume as C

    print("== ① 无历史：零开销，原句照答 ==")
    calls: list[list] = []
    _orig = C.chat_json
    C.chat_json = lambda *a, **k: (calls.append(a), {})[1]
    r = C._resolve_history("技术部九月份发多少工资", None)
    check("不调 LLM（历史为空直接返回）", not calls)
    check("原句返回", r == "技术部九月份发多少工资")
    r = C._resolve_history("", [{"role": "user", "content": "hi"}])
    check("空问题也原样返回", r == "")

    print("== ② 有历史：消解结果被采用；挂了回落原句 ==")
    seen: list[str] = []

    def _stub(msgs, **kw):
        seen.append(str(msgs[-1].get("content") or ""))
        return {"question": "相比于网上其他公司，本公司（九月一共支付137,100元）"
                            "的待遇好不好"}

    C.chat_json = _stub
    hist = [{"role": "user", "content": "九月份公司一共要支付多少钱"},
            {"role": "assistant", "content": "九月份公司一共支付137,100.00元。"},
            {"role": "user", "content": "相比于网上其他公司，这家公司的待遇好不好"}]
    r = C._resolve_history("相比于网上其他公司，这家公司的待遇好不好", hist)
    check("改写结果被采用", "本公司" in r and "137,100" in r, r[:60])
    check("历史进了提示词（上一问与上一答都在）",
          "九月份公司一共要支付多少钱" in seen[-1]
          and "137,100.00" in seen[-1])
    check("最新一句进了提示词", "这家公司的待遇好不好" in seen[-1])

    def _boom(*a, **k):
        raise RuntimeError("llm down")

    C.chat_json = _boom
    r = C._resolve_history("这家公司待遇好不好", hist)
    check("LLM 挂 → 原句照答（消解不许挡路）",
          r == "这家公司待遇好不好")

    C.chat_json = lambda *a, **k: {"question": ""}
    r = C._resolve_history("这家公司待遇好不好", hist)
    check("返回空 → 原句", r == "这家公司待遇好不好")

    print("== ③ 历史只看最近 8 轮 ==")
    C.chat_json = _stub
    _many = [{"role": "user", "content": f"第{i}轮的问题"} for i in range(1, 11)]
    C._resolve_history("现在这句", _many)
    _u = seen[-1]
    check("第 10 轮在", "第10轮的问题" in _u)
    check("第 1 轮被截掉（防窗口爆）", "第1轮的问题" not in _u)
    check("第 3 轮在（10 - 8 = 前 2 轮截掉）", "第3轮的问题" in _u)
    C.chat_json = _orig

    print("== ④ 面板把「最后一条 user 之前」的历史传进 consume ==")
    import dashboard as D
    src_chat = inspect.getsource(D.Handler._openai_chat)
    check("从 messages 里切出历史（最后一条 user 之前）",
          "_hist" in src_chat and "_last_u" in src_chat)
    check("流式/非流式两路都传 history",
          "history=_hist" in src_chat)
    src_stream = inspect.getsource(D.Handler._openai_stream)
    check("stream 透传 history", "history=history" in src_stream)
    src_ask = inspect.getsource(D.Handler._mve_ask)
    check("_mve_ask 收 history 并传给 consume",
          "history" in str(inspect.signature(D.Handler._mve_ask))
          and "history=history" in src_ask)
    src_consume = inspect.getsource(C.consume)
    check("consume 签名带 history",
          "history" in str(inspect.signature(C.consume)))
    check("消解发生在文件上下文/规划之前（它们吃到的就是消解后的问句）",
          src_consume.index("_resolve_history")
          < src_consume.index("context_block"))
    check("消解挂掉不挡路（try/except 包着）",
          "消解挂了就当没有历史" in src_consume)

    if FAILS:
        print(f"✗ 失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
