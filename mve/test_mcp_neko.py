#!/usr/bin/env python3
"""MCP 接入本地自测：网关连真实 server + 协议握手 + expose + 调用桥接。

不依赖网络（用「未知工具」逼出 isError 路径验证整条链路不崩；web_search 走网络
只是 best-effort，沙箱不通就验证 error 路径）。直接 `python mve/test_mcp_neko.py` 跑。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(REPO))


def test_unwrap_unit() -> None:
    from local_agent import _unwrap_mcp
    # 成功：structuredContent 直接取
    assert _unwrap_mcp({"result": {"isError": False,
                                  "structuredContent": [1, 2, 3]}}) == [1, 2, 3]
    # 成功：text 兜底解析 JSON
    assert _unwrap_mcp({"result": {"isError": False,
                                  "content": [{"type": "text",
                                               "text": json.dumps({"a": 1})}]}}) == {"a": 1}
    # 错误：result.isError → 抛 RuntimeError
    try:
        _unwrap_mcp({"result": {"isError": True,
                               "structuredContent": {"error": "boom"}}})
        raise AssertionError("应抛 RuntimeError")
    except RuntimeError as e:
        assert "boom" in str(e)
    # 错误：顶层 error → 抛 RuntimeError
    try:
        _unwrap_mcp({"error": "nope"})
        raise AssertionError("应抛 RuntimeError")
    except RuntimeError:
        pass
    print("PASS  test_unwrap_unit")


async def test_gateway_connect() -> None:
    import mcp_gateway
    gw = mcp_gateway.get_gateway()
    res = await gw.connect_all()
    print("connect_all:", json.dumps(res, ensure_ascii=False))
    assert res["connected"], f"没连上 MCP server：{res.get('failed')}"

    tools = gw.list_tools()
    ids = [t["tool_id"] for t in tools]
    assert "mcp_neko_web_search" in ids and "mcp_neko_web_fetch" in ids, ids
    print("工具清单:", ids)

    # 未知工具 → server 返回 isError，整条链路不应崩（不抛、不挂死）
    r = await gw.call_remote("mcp_neko_bogus", query="x")
    assert isinstance(r, dict), r
    print("bogus 调用返回（预期 isError 形状）:",
          json.dumps(r, ensure_ascii=False)[:200])

    # expose_as_actions 把 mcp_* 注册进场景条目（origin=mcp）
    exp = gw.expose_as_actions("TEST")
    print("exposed:", exp)
    assert "mcp_neko_web_search" in exp["exposed"], exp
    assert "mcp_neko_web_fetch" in exp["exposed"], exp

    # ---- 死通道打通验证：local_agent 把 mcp_* 注入执行命名空间 + prompt ----
    import local_agent
    ns = local_agent._ns_base("", "TEST")     # 当前在 asyncio.run 里，有运行中的循环
    assert "mcp_neko_web_search" in ns and "mcp_neko_web_fetch" in ns, list(ns.keys())
    assert callable(ns["mcp_neko_web_search"]), "注入的应是可调用的同步包装器"
    print("ns 注入 mcp 工具:", sorted(k for k in ns if k.startswith("mcp_")))
    sec = local_agent._mcp_prompt_section("TEST")
    assert "mcp_neko_web_search" in sec, "prompt 段应列出 mcp 工具"
    print("prompt 段非空（含 mcp 工具）: OK")

    # 真实 web_search 走网络：通了验证结构化返回，不通了验证 error 路径（都算通过）
    try:
        rr = await gw.call_remote("mcp_neko_web_search",
                                  query="Voyager JSL paper", max_results=3)
        inner = (rr or {}).get("result") if isinstance(rr, dict) else None
        if inner and not inner.get("isError"):
            sc = inner.get("structuredContent")
            print(f"web_search 成功返回 {len(sc) if isinstance(sc, list) else sc} 条（联网通）")
        else:
            print("web_search 在网络受限环境返回 error（预期内）:",
                  json.dumps(rr, ensure_ascii=False)[:200])
    except Exception as e:
        print("web_search 异常（预期内，网络受限）:",
              type(e).__name__, str(e)[:120])

    await gw.disconnect_all()
    print("PASS  test_gateway_connect")


def test_vacuum_enforcement() -> None:
    """闭环真空-success 新形态：代码跑通、RESULT 非空却一次数据源都没调
    （模型把训练记忆写死成 RESULT）→ 判失败；真调数据源才放行。"""
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "metrics.json").write_text(
        '{"latest":"真正来自文件的世界模型"}', encoding="utf-8")
    import local_agent as L

    class _Fake:
        def __init__(self):
            self.queue: list[str] = []

        def push(self, s: str):
            self.queue.append(s)

        def __call__(self, messages, **_kw):
            return self.queue.pop(0) if self.queue else "无"

    # 1) 凭记忆编造（服务器实测踩的那种）：RESULT 是写死的列表，没有取数 → 不算取到数
    f1 = _Fake()
    f1.push('```python\nRESULT = [{"title": "PyTorch", "url": "x"}]\n```')
    obs1, _ = asyncio.run(L.solve("热门AI开源项目", str(d), llm=f1, max_iters=1))
    assert obs1 and obs1[0]["ok"] is False, "凭记忆编造应判失败"
    assert "数据源" in str(obs1[0]["error"]), obs1[0]["error"]
    assert obs1[0].get("data_sources") == [], obs1[0].get("data_sources")
    print("PASS  test_vacuum_enforcement：凭记忆编造被拦下（ok=False + 反馈带回）")

    # 2) 真调数据源：read_file 取到 → ok=True，data_sources 记到 read_file
    f2 = _Fake()
    f2.push('```python\nf = read_file("metrics.json")\nRESULT = {"v": f["text"]}\n```')
    obs2, _ = asyncio.run(L.solve("读 metrics", str(d), llm=f2, max_iters=1))
    assert obs2 and obs2[0]["ok"] is True, json.dumps(obs2, ensure_ascii=False)
    assert "read_file" in obs2[0].get("data_sources", []), obs2[0].get("data_sources")
    print("PASS  test_vacuum_enforcement：真调数据源放行（ok=True, data_sources=",
          obs2[0]["data_sources"], "）")


def main() -> None:
    test_unwrap_unit()
    asyncio.run(test_gateway_connect())
    test_vacuum_enforcement()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
