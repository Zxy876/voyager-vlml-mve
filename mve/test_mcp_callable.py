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

        def _spy(code, scene_dir, timeout=30.0, scene_id="", loop=None):
            seen["loop"] = loop
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


if __name__ == "__main__":
    test_positional_args_are_mapped_by_schema_order()
    test_keyword_args_pass_through()
    test_too_many_positional_args_raises_with_names()
    test_no_schema_positional_call_raises()
    test_docstring_exposes_signature_for_prompt()
    test_run_saved_async_offloads_to_worker_thread()
    print("\nALL PASS")
