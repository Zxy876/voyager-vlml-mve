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


def test_resolve_units() -> None:
    """配置解析器单测（纯函数，不连任何 server，mac/服务器一致的验收线）：
    npx 这类非 python 命令必须走 which 保留（旧实现会被 sys.executable 顶掉），
    -y/-m 这类 flag 必须原样（旧实现会被解析成 /root/mve/mve/-y 这种鬼路径）。"""
    import shutil
    import mcp_gateway as G

    # command：绝对路径原样
    assert G._resolve_command("/opt/x/bin/npx") == "/opt/x/bin/npx"
    # command：哨兵 → 当前解释器
    assert G._resolve_command("auto") == G.sys.executable
    assert G._resolve_command("") == G.sys.executable
    # command：npx → PATH which（服务器 /usr/local/bin/npx，mac 的 npx 都在 PATH）
    expect_npx = shutil.which("npx")
    assert G._resolve_command("npx") == (expect_npx or "npx"), G._resolve_command("npx")
    # command：venv 同目录 console script 回退（PATH 没有时，解释器旁边有就算）
    sibling = G.Path(G.sys.executable).resolve().parent / "mve-nonexistent-cli"
    assert G._resolve_command("mve-nonexistent-cli") == "mve-nonexistent-cli"  # 都没有→原样如实报

    # arg：flag 原样（这是旧实现炸掉的地方）
    assert G._resolve_arg("-y") == "-y"
    assert G._resolve_arg("-m") == "-m"
    assert G._resolve_arg("--transport") == "--transport"
    # arg：模块名/包名原样（交给 python -m / npx 解释）
    assert G._resolve_arg("mcp_server_fetch") == "mcp_server_fetch"
    assert G._resolve_arg("@modelcontextprotocol/server-sequential-thinking") == \
        "@modelcontextprotocol/server-sequential-thinking"
    # arg：本仓脚本 → 相对配置目录（mve/）的绝对路径
    r = G._resolve_arg("mcp_neko_server.py")
    assert r.endswith("mcp_neko_server.py") and G.Path(r).is_absolute(), r
    # arg：绝对路径原样
    assert G._resolve_arg("/a/b/c.py") == "/a/b/c.py"

    # sibling 回退必须能穿透 venv symlink：/v/bin/python → python3 →（外部），
    # 直接 resolve() 会跳出 venv 导致 console script 找不到（服务器实测踩过）
    import tempfile
    v = Path(tempfile.mkdtemp())
    (v / "bin").mkdir()
    (v / "bin" / "python3").write_text("#!/bin/sh\n")     # 充当系统解释器
    (v / "bin" / "python").symlink_to("python3")          # venv 式 symlink
    (v / "bin" / "mve-fake-cli").write_text("#!/bin/sh\n")
    old_exe = G.sys.executable
    G.sys.executable = str(v / "bin" / "python")
    try:
        assert G._resolve_command("mve-fake-cli") == str(v / "bin" / "mve-fake-cli"), \
            G._resolve_command("mve-fake-cli")
    finally:
        G.sys.executable = old_exe
    print("PASS  test_resolve_units")


async def test_gateway_connect() -> None:
    """连真实 server（只用本仓自写的 mcp_neko_server.py——纯标准库，mac/服务器
    都能连；外部 server 的连通性在服务器上用生产配置单独验，不塞进本地自测）。"""
    import tempfile
    import mcp_gateway

    # 配置隔离：临时配置只含 neko，monkeypatch CONFIG —— 不读生产 mcp_servers.json
    tmp = Path(tempfile.mkdtemp())
    cfg = tmp / "mcp_servers.json"
    cfg.write_text(json.dumps({
        "servers": {
            "neko": {
                "command": "auto",
                "args": [str(HERE / "mcp_neko_server.py")],
                "env": {},
                "enabled": True,
            }
        }
    }), encoding="utf-8")
    old_cfg = mcp_gateway.CONFIG
    mcp_gateway.CONFIG = cfg
    try:
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
    finally:
        mcp_gateway.CONFIG = old_cfg
        try:
            await mcp_gateway.get_gateway().disconnect_all()
        except Exception:
            pass
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

    # 3) 知识型问题（不需要数据）不取数也放行 —— 成败交给 critic 判，
    #    不再一刀切"必须取数"（2026-10-19 用户实测：养育类问题被判死 4 次）
    f3 = _Fake()
    f3.push('```python\nRESULT = {"answer": "结构化养育建议：明确规则、视觉化日程、情绪共情"}\n```')
    obs3, _ = asyncio.run(L.solve("阿斯伯格孩子怎么养育？", str(d),
                                  llm=f3, max_iters=1))
    assert obs3 and obs3[0]["ok"] is True, json.dumps(obs3, ensure_ascii=False)
    assert obs3[0].get("evidence") == "knowledge", obs3[0].get("evidence")
    print("PASS  test_vacuum_enforcement：知识型问题不取数也放行（evidence=knowledge）")


def main() -> None:
    test_unwrap_unit()
    test_resolve_units()
    asyncio.run(test_gateway_connect())
    test_vacuum_enforcement()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
