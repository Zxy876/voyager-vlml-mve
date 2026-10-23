"""钉住 action 主循环 = 原版 Voyager 的 reset/step/rollout。

用假环境 + 脚本化 LLM 跑，不碰数据库、不烧 LLM。逐条断言的是**原版的语义**：

  · reset 之后只有两条消息（system + human），human 里 `Code from the last round`
    写"No code in the first round"（原版 action.py:135-138）
  · 每步执行完**重建两条消息**，human 里出现 `Execution error:` / `Observation from …`
    / `Critique:`（原版 action.py:140-200）
  · 解析失败 → 只打 `Trying again!`，**不重建消息、不加 critique**，
    但 rollout_num_iter 照加（原版 voyager.py:271-276）
  · done = rollout_num_iter >= task_max_retries or success（原版 :277-280）
  · rollout = while True: step(); if done: break（原版 :298-304）

跑法：python mve/test_action_loop.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for p in (str(_ROOT), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from voyager_vlml.agents.action import VLMLActionAgent  # noqa: E402


class FakeEnv:
    """只实现 action 用到的那几个口子。"""

    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = list(results)
        self.calls: list[str] = []
        self.tool_names = ["query_sql", "get_database_info"]

    async def schema_block(self) -> str:
        return "- rounds（285 行）: round_id::str，winning_team_name::str"

    async def run_sql(self, sql: str) -> dict[str, Any]:
        self.calls.append(sql)
        if self.results:
            return self.results.pop(0)
        return {"ok": False, "tool": "query_sql", "rows": 0,
                "error": "no more canned results", "data": None}

    def data_source(self) -> str:
        return "fake.gg"


def _obs_ok(rows: int = 3) -> dict[str, Any]:
    return {"ok": True, "tool": "query_sql", "rows": rows, "error": "",
            "data": {"columns": ["team", "n"], "rows": [["A", 2], ["B", 1]]}}


def _obs_err(msg: str = "Binder Error: no such column") -> dict[str, Any]:
    return {"ok": False, "tool": "query_sql", "rows": 0, "error": msg, "data": None}


def _script(agent: VLMLActionAgent, replies: list[str]) -> None:
    """把 llm 换成按脚本回话，同时记录它收到的 messages。"""
    seen: list[list[dict[str, str]]] = []
    it = iter(replies)

    def fake_llm(messages: list[dict[str, str]]) -> str:
        seen.append([dict(m) for m in messages])
        return next(it, "")

    agent.llm = fake_llm  # type: ignore[assignment]
    agent.seen_messages = seen  # type: ignore[attr-defined]


def test_reset_builds_two_messages() -> None:
    env = FakeEnv([])
    a = VLMLActionAgent(env=env)  # type: ignore[arg-type]
    asyncio.run(a.reset(task="数回合", context="只数 2026 年的"))
    assert len(a.messages) == 2, a.messages
    assert a.messages[0]["role"] == "system"
    assert a.messages[1]["role"] == "user"
    human = a.messages[1]["content"]
    assert "No code in the first round" in human
    assert "Task: 数回合" in human
    assert "Context: 只数 2026 年的" in human
    assert "Critique: None" in human
    assert "rounds（285 行）" in a.messages[0]["content"]
    print("PASS  reset → 两条消息；human 有 Code/Task/Context/Critique")


def test_step_rebuilds_messages_with_critique_and_observation() -> None:
    env = FakeEnv([_obs_err()])
    a = VLMLActionAgent(env=env)  # type: ignore[arg-type]
    _script(a, ["SELECT bad_col FROM rounds"])

    async def go() -> tuple[bool, bool]:
        await a.reset(task="数回合")
        first_system = a.messages[0]["content"]
        events, done = await a.step()
        rebuilt = (a.messages[0]["content"] != first_system
                   or "Execution error" in a.messages[1]["content"])
        return done, rebuilt

    done, rebuilt = asyncio.run(go())
    assert env.calls == ["SELECT bad_col FROM rounds"], env.calls
    assert done is False, "一次失败不该 done（上限 4）"
    assert rebuilt, "执行完必须重建消息"
    human = a.messages[1]["content"]
    assert "Execution error:\nBinder Error: no such column" in human, human
    assert "Critique:" in human and "Critique: None" not in human
    print("PASS  执行→判失败→重建消息（带 Execution error + Critique）")


def test_success_rebuilds_and_stops() -> None:
    env = FakeEnv([_obs_ok()])
    a = VLMLActionAgent(env=env)  # type: ignore[arg-type]
    _script(a, ["SELECT team, COUNT(*) AS n FROM rounds GROUP BY 1"])

    async def go() -> bool:
        await a.reset(task="数回合")
        _e, done = await a.step()
        return done

    done = asyncio.run(go())
    assert done is True, "成功即 done"
    human = a.messages[1]["content"]
    assert "Observation from query_sql (3 rows)" in human, human
    assert "列：team、n" in human, human
    assert "Code from the last round:\nSELECT team" in human, human
    print("PASS  成功→done；human 回灌上次代码 + 观察的列与行")


def test_parse_failure_does_not_rebuild_messages() -> None:
    """原版 voyager.py:271-274：解析失败只打印 Trying again，消息不动。"""
    env = FakeEnv([])
    a = VLMLActionAgent(env=env)  # type: ignore[arg-type]
    _script(a, ["我不写 SQL，我聊两句"])

    async def go() -> tuple[bool, list[dict[str, str]], int]:
        await a.reset(task="数回合")
        before = [dict(m) for m in a.messages]
        _e, done = await a.step()
        return done, before, a.rollout_num_iter

    done, before, it = asyncio.run(go())
    assert a.messages == before, "解析失败不该重建消息"
    assert it == 1, "但迭代数照加（原版 :276 在 if/else 之外）"
    assert done is False
    assert env.calls == [], "没解析出 SQL 就不该执行"
    print("PASS  解析失败 → 消息不动、迭代照加、不执行")


def test_retry_cap_and_rollout_loop() -> None:
    """4 次都失败 → rollout 退出；第 2 次成功 → 提前退出。"""
    env = FakeEnv([_obs_err(), _obs_err(), _obs_err(), _obs_err()])
    a = VLMLActionAgent(env=env)  # type: ignore[arg-type]
    _script(a, ["SELECT a FROM t", "SELECT b FROM t", "SELECT c FROM t",
                "SELECT d FROM t", "SELECT e FROM t"])

    async def go() -> tuple[list, str]:
        return await a.rollout(task="数回合")

    obs, sql = asyncio.run(go())
    assert a.rollout_num_iter == 4, a.rollout_num_iter
    assert len(env.calls) == 4, env.calls
    assert sql == "SELECT d FROM t", sql
    assert all(not o["ok"] for o in obs)

    env2 = FakeEnv([_obs_err(), _obs_ok()])
    a2 = VLMLActionAgent(env=env2)  # type: ignore[arg-type]
    _script(a2, ["SELECT a FROM t", "SELECT b FROM t"])

    async def go2() -> None:
        await a2.rollout(task="数回合")

    asyncio.run(go2())
    assert a2.rollout_num_iter == 2, a2.rollout_num_iter
    assert len(env2.calls) == 2
    print("PASS  上限 4 次；中途成功即停")


def test_solve_is_thin_wrapper() -> None:
    """solve(hints=…) 必须等价于 rollout(context=hints)——不另开 prompt 通道。"""
    env = FakeEnv([_obs_ok()])
    a = VLMLActionAgent(env=env)  # type: ignore[arg-type]
    _script(a, ["SELECT 1"])

    async def go() -> list[dict[str, str]]:
        await a.solve("数回合", hints=["口径：只数 2026 年"], max_iters=4)
        return a.seen_messages[0]

    first = asyncio.run(go())
    human = first[1]["content"]
    assert "Context: 口径：只数 2026 年" in human, human
    assert len(first) == 2, "原版 messages 恒为两条"
    print("PASS  solve = reset+rollout；hints 走 Context 槽")


def main() -> int:
    test_reset_builds_two_messages()
    test_step_rebuilds_messages_with_critique_and_observation()
    test_success_rebuilds_and_stops()
    test_parse_failure_does_not_rebuild_messages()
    test_retry_cap_and_rollout_loop()
    test_solve_is_thin_wrapper()
    print("\n全部通过：主循环 = 原版 reset/step/rollout")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
