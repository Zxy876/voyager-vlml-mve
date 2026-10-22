#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 同步包装器 `_make_mcp_callable` 的入参契约测试。

背景（2026-10-20 服务器实跑取证）：
    模型照 prompt 示例写位置调用 `mcp_neko_web_search("阿斯伯格")`，而旧包装器
    只收 `**kwargs` → `TypeError: _sync() takes 0 positional arguments but 1 was
    given`。表面现象是「调过：mcp_neko_web_search」却取不到数、维度判未闭合，
    看起来像"模型不肯用工具"，实际是包装器不收位置参数。

契约（别再改回去）：
    · 位置参数按 schema.properties 的**声明顺序**补成关键字（MCP 侧只认具名入参）
    · 关键字参数原样透传，不被位置参数覆盖
    · 参数给多了要报错并把参数名列出来（让模型能自我修正），不许静默吞掉
    · 工具没声明入参时位置调用要报错，不许假装成功
"""
import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_agent  # noqa: E402


class _FakeGW:
    """假网关：只记录 call_remote 收到的入参，返回一段合法 MCP 结果。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call_remote(self, tool_id: str, **arguments):
        self.calls.append((tool_id, dict(arguments)))
        return {"result": {"content": [{"type": "text", "text": "ok"}]}}


class _LoopThread:
    """run_coroutine_threadsafe 要求目标 loop 跑在**别的线程**上。"""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._t = threading.Thread(target=self.loop.run_forever, daemon=True)

    def __enter__(self):
        self._t.start()
        return self.loop

    def __exit__(self, *a):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._t.join(timeout=3)


SCHEMA = {"type": "object",
          "properties": {"query": {"type": "string"},
                         "max_results": {"type": "integer"}},
          "required": ["query"]}


def test_positional_args_are_mapped_by_schema_order():
    gw = _FakeGW()
    with _LoopThread() as loop:
        fn = local_agent._make_mcp_callable(gw, "mcp_neko_web_search", loop, SCHEMA)
        fn("阿斯伯格")
    assert gw.calls and gw.calls[0][0] == "mcp_neko_web_search", gw.calls
    assert gw.calls[0][1] == {"query": "阿斯伯格"}, gw.calls
    print("PASS  test_positional_args_are_mapped_by_schema_order")


def test_keyword_args_pass_through():
    gw = _FakeGW()
    with _LoopThread() as loop:
        fn = local_agent._make_mcp_callable(gw, "mcp_ddg_search", loop, SCHEMA)
        fn(query="阿斯伯格", max_results=3)
    assert gw.calls[0][1] == {"query": "阿斯伯格", "max_results": 3}, gw.calls
    print("PASS  test_keyword_args_pass_through")


def test_too_many_positional_args_raises_with_names():
    gw = _FakeGW()
    with _LoopThread() as loop:
        fn = local_agent._make_mcp_callable(gw, "mcp_ddg_search", loop, SCHEMA)
        try:
            fn("a", "b", "c")
        except TypeError as e:
            assert "query" in str(e) and "max_results" in str(e), str(e)
        else:
            raise AssertionError("参数给多了必须报 TypeError")
    assert not gw.calls, "报错前不许发出调用"
    print("PASS  test_too_many_positional_args_raises_with_names")


def test_no_schema_positional_call_raises():
    gw = _FakeGW()
    with _LoopThread() as loop:
        fn = local_agent._make_mcp_callable(gw, "mcp_weird", loop, None)
        try:
            fn("a")
        except TypeError:
            pass
        else:
            raise AssertionError("没声明入参却位置调用必须报错")
    print("PASS  test_no_schema_positional_call_raises")


def test_docstring_exposes_signature_for_prompt():
    with _LoopThread() as loop:
        fn = local_agent._make_mcp_callable(_FakeGW(), "mcp_ddg_search", loop, SCHEMA)
    assert "query" in (fn.__doc__ or ""), fn.__doc__
    assert fn.__name__ == "mcp_ddg_search"
    print("PASS  test_docstring_exposes_signature_for_prompt")


def test_run_saved_async_offloads_to_worker_thread():
    """在事件循环里重放配方必须换线程（`asyncio.to_thread`）。

    否则 exec 在主循环线程里同步等 `run_coroutine_threadsafe` 提交给**同一个
    loop** 的 MCP 调用 —— 自己等自己，60s 超时，配方被误判失效。这里钉死
    async 版能正常返回、失败如实抛，且**把主循环显式带进 worker 线程**
    （worker 线程没有运行中的循环，不带 loop 就注入不了 mcp_*）。
    """
    import asyncio
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp()
    try:
        out = asyncio.run(local_agent.run_saved_async(
            'RESULT = {"answer": 42, "source": "pure"}', tmp))
        assert out == {"answer": 42, "source": "pure"}, out

        # 失败要如实抛，不许把空结果当成功
        # ⚠️ 受限命名空间只放白了 Exception，ValueError 之类不在里面
        try:
            asyncio.run(local_agent.run_saved_async("raise Exception('boom')", tmp))
        except Exception as e:
            assert "boom" in str(e), e
        else:
            raise AssertionError("执行失败必须抛异常")

        # 主循环必须被显式传下去（否则 worker 线程里 get_running_loop() 抛错，
        # mcp_* 静默不注入 → 重放配方 NameError）
        seen: dict = {}
        real = local_agent.run_saved

        def _spy(code, scene_dir, timeout=30.0, scene_id="", loop=None,
                 evidence=None):
            seen["loop"] = loop
            seen["evidence"] = evidence
            seen["thread"] = threading.get_ident()
            return {"ok": True}

        async def _go():
            seen["loop_obj"] = asyncio.get_running_loop()
            local_agent.run_saved = _spy
            try:
                return await local_agent.run_saved_async("RESULT = 1", tmp)
            finally:
                local_agent.run_saved = real

        asyncio.run(_go())
        assert seen.get("loop") is seen.get("loop_obj"), "主循环没传进 run_saved"
        assert seen.get("thread") != threading.get_ident(), "exec 没换线程"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS  test_run_saved_async_offloads_to_worker_thread")


def test_ensure_mcp_tools_mounts_scene_entries():
    """`ensure_mcp_tools` 必须把远端工具挂进场景，且已连上就不重连。

    这是「第二次导入能用上第一次学的」链条上最容易漏的一环：`_ns_base` 只注入
    **注册表里已有**的 mcp 条目，而条目是 connect + expose 才注册的。consume 是
    新进程，不接这一步 → 重放 `mcp_xxx` 直接 NameError（2026-10-20 实测）。
    """
    import asyncio
    import mcp_gateway

    class _FakeGW:
        def __init__(self, connected, stale=None):
            self._connected = connected
            self._stale = list(stale or [])
            self.calls: list[str] = []

        def connected_servers(self, loop=None):
            return [] if self._stale else list(self._connected)

        def stale_servers(self, loop):
            return list(self._stale)

        async def disconnect_all(self):
            self.calls.append("disconnect")
            self._stale = []
            self._connected = []

        async def connect_all(self, timeout=30.0):
            self.calls.append("connect")
            self._stale = []
            self._connected = ["neko"]
            return {"connected": [{"server": "neko", "tools": 3}], "failed": []}

        def expose_as_actions(self, scene_id):
            self.calls.append(f"expose:{scene_id}")
            return {"exposed": ["mcp_neko_web_search"]}

    # ① 没连上 → 先 connect 再 expose，工具清单透传
    gw = _FakeGW([])
    real = mcp_gateway.get_gateway
    mcp_gateway.get_gateway = lambda: gw
    try:
        out = asyncio.run(local_agent.ensure_mcp_tools("D"))
        assert gw.calls == ["connect", "expose:D"], gw.calls
        assert out["exposed"] == ["mcp_neko_web_search"], out

        # ② 已连上（循环匹配）→ 不重连（重连会白起子进程）
        gw2 = _FakeGW(["neko"])
        mcp_gateway.get_gateway = lambda: gw2
        asyncio.run(local_agent.ensure_mcp_tools("D"))
        assert gw2.calls == ["expose:D"], gw2.calls

        # ③ 连接还在但绑在**别的循环**上（面板每次导入都是新 asyncio.run 循环）
        #    → 必须整批断开重连，否则调用桥到已关闭的旧循环、永远等不到响应
        #    （2026-10-21 Scene H 实测：5 条技能重放全部 >55s 超时）。
        gw3 = _FakeGW(["neko"], stale=["neko"])
        mcp_gateway.get_gateway = lambda: gw3
        out3 = asyncio.run(local_agent.ensure_mcp_tools("D"))
        assert gw3.calls == ["disconnect", "connect", "expose:D"], gw3.calls
        assert out3.get("reconnected") == ["neko"], out3

        # ④ 没指定场景 → 什么都不做，如实说原因
        out4 = asyncio.run(local_agent.ensure_mcp_tools(""))
        assert out4["exposed"] == [] and out4.get("why"), out4

        # ⑤ 网关抛错不拖垮上层，错误进返回值
        class _BoomGW:
            def stale_servers(self, loop):
                raise RuntimeError("网关坏了")

        mcp_gateway.get_gateway = lambda: _BoomGW()
        out5 = asyncio.run(local_agent.ensure_mcp_tools("D"))
        assert out5["exposed"] == [] and "网关坏了" in out5.get("error", ""), out5
    finally:
        mcp_gateway.get_gateway = real
    print("PASS  test_ensure_mcp_tools_mounts_scene_entries")


def test_tools_in_code_reports_real_grabs():
    """披露栏的抓手必须从**代码**里认出来，不能写死 local_python。

    2026-10-20 实测：复用一条联网配方（`mcp_neko_web_search` 真取到数），CLI
    却打印「调用 0 次」—— 人会以为答案是模型编的。纯计算（`json.dumps`）
    不算工具，不许虚报。
    """
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from voyager_vlml.consume import _tools_in_code

    code = ('r = mcp_neko_web_search(query="阿斯伯格")\n'
            'RESULT = {"answer": r}')
    assert _tools_in_code(code, "D") == ["mcp_neko_web_search"], _tools_in_code(code, "D")
    assert _tools_in_code('x = json.dumps({})\nRESULT = 1', "D") == []
    assert _tools_in_code('rows = query_sql("select 1")\nRESULT = rows', "") == ["query_sql"]
    assert _tools_in_code("", "D") == []
    print("PASS  test_tools_in_code_reports_real_grabs")


if __name__ == "__main__":
    test_positional_args_are_mapped_by_schema_order()
    test_keyword_args_pass_through()
    test_too_many_positional_args_raises_with_names()
    test_no_schema_positional_call_raises()
    test_docstring_exposes_signature_for_prompt()
    test_run_saved_async_offloads_to_worker_thread()
    test_ensure_mcp_tools_mounts_scene_entries()
    test_tools_in_code_reports_real_grabs()
    print("\nALL PASS")
