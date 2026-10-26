#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""学习主循环的**超时契约** —— 对比原版 Voyager learn() 后定位的致命点。

原版 rollout 里 bot 执行本来就允许几十秒（`bot.chat` 一路打进度，
env 侧没有"30 秒没跑完就判你错"这回事）。MVE 之前是：

    外层 `_run_restricted(timeout=30)`  <  内层单次 MCP 调用上限 60s
    （local_agent.py:680 `cfut.result(timeout=60)`）

于是工具**还没回来**就被判超时；而且超时杀不掉线程（Python 做不到安全中止），
那条线程还在等同一个（被限流的）接口 —— 老代码外层还照 `SUBTASK_MAX_RETRIES=4`
原地重试 4 次，等于往同一个接口上堆 4 个僵尸线程。7 条维度 × 4 次 = 十几分钟
跑完、一条都没闭合 → **颗粒无收，学不到技能**（2026-10-20 学习日志实证：
「维度未闭合 0/7 … 执行超时（>30s）｜调过：mcp_neko_web_search」）。
（2026-10-26：外层重试循环已删，超时分支改为判本条维度未闭合、
跳过下一维度 ——「跳过而不是重试」正是本文件钉死的第 2 件事。）

这里钉死三件事：
  1. 超时必须被**认出来**（`timed_out=True`），不能混在普通报错里；
  2. `solve` 要把超时透传到观察结果，上层才可能"跳过而不是重试"；
  3. 学习侧的墙钟上限必须 > 单次 MCP 调用的上限（60s），否则又回到老样子。
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import learn_orchestrate as L  # noqa: E402
import local_agent  # noqa: E402

# 原版单次 MCP 调用的上限（local_agent._make_mcp_callable 里写死的 60）
_MCP_CALL_TIMEOUT = 60.0


def test_run_restricted_marks_timeout_specifically():
    """超时要有专门的标记 —— 调用方据此"跳过"，而不是"再试一次"。"""
    box = local_agent._run_restricted("while True:\n    pass\n", {},
                                      timeout=1.0)
    assert box.get("ok") is False, box
    assert box.get("timed_out") is True, box
    assert "执行超时" in str(box.get("error") or ""), box
    print("PASS  test_run_restricted_marks_timeout_specifically")


def test_solve_propagates_timeout_to_observation():
    """`solve` 要把 timeout 传下去，并把 `timed_out` 放进观察（上层据此决策）。"""
    tmp = Path(tempfile.mkdtemp())

    def fake(_msgs):
        return "```python\nwhile True:\n    pass\n```"

    try:
        obs, _ = asyncio.run(local_agent.solve(
            "随便问一句", str(tmp), llm=fake, scene_id="", max_iters=1,
            timeout=1.0))
        assert obs and obs[-1].get("timed_out") is True, obs
        assert not obs[-1].get("ok"), obs
        assert "执行超时" in str(obs[-1].get("error") or ""), obs[-1]
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS  test_solve_propagates_timeout_to_observation")


def test_exec_timeout_outlives_a_single_mcp_call():
    """学习侧的墙钟上限必须 **大于** 单次 MCP 调用上限，否则工具没回来就判死。"""
    assert L.EXEC_TIMEOUT > _MCP_CALL_TIMEOUT, (
        f"EXEC_TIMEOUT={L.EXEC_TIMEOUT} 必须 > 单次 MCP 调用的 "
        f"{_MCP_CALL_TIMEOUT}s —— 否则又是「外层比内层短」的老毛病")
    # 也别大得离谱（跑一轮 7 条维度不能变成几十分钟）
    assert L.EXEC_TIMEOUT <= 300.0, L.EXEC_TIMEOUT
    print("PASS  test_exec_timeout_outlives_a_single_mcp_call")


if __name__ == "__main__":
    test_run_restricted_marks_timeout_specifically()
    test_solve_propagates_timeout_to_observation()
    test_exec_timeout_outlives_a_single_mcp_call()
    print("\nALL PASS")
