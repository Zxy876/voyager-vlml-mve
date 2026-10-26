"""钉住**步级闸门**（候选A，2026-10-26 用户拍板）：闸门从「rollout 跑完判一次」
移进 `step()` 的每一步 —— 同构原版 Maieutic 的 examiner（spec/route.ts:82-100
一步一判、判词当轮进下一轮输入）与原版 Voyager 的 critique 回灌
（action.py:197-200 的 Critique: 槽）。

契约（action.py 的 gate_check）：
  1. 只有内层 critic 判过（有答案可核）才跑闸门；内层没过 → 闸门不跑；
  2. 闸门不过 → 本步不 done，questions（≤3 条）并进下一步 human 的
     Critique 槽（前缀【清单追问】），内层判词不丢；
  3. 双过（内层 + 闸门）→ 本步成功即 done（4 步封顶不变）；
  4. 闸门不可用（origin=failed / 抛异常）→ 不阻塞，退化成内层单独判；
  5. 本 rollout 已问清单（_gate_asked）跨步累积，闸门第二次调用看得见
     （同构 priorIterations，防重复问）；
  6. 不传 gate_check → 行为与原来完全一样（消费侧重放等旧调用方零改动）。

跑法：python mve/test_gate_stepwise.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

# 只钉闸门接线，不烧第二级 LLM 判官（口径检验在 test_critic_caliber.py）。
os.environ.setdefault("MVE_CRITIC_LLM", "0")

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for p in (str(_ROOT), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from voyager_vlml.agents.action import VLMLActionAgent  # noqa: E402


class FakeEnv:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = list(results)
        self.calls: list[str] = []
        self.tool_names = ["query_sql", "get_database_info"]

    async def schema_block(self) -> str:
        return "- payroll（3 行）: emp_id::str, base_salary::int"

    async def run_sql(self, sql: str) -> dict[str, Any]:
        self.calls.append(sql)
        if self.results:
            return self.results.pop(0)
        return {"ok": False, "tool": "query_sql", "rows": 0,
                "error": "no more canned results", "data": None}

    def data_source(self) -> str:
        return "fake.duckdb"


def _obs_ok(rows: int = 3) -> dict[str, Any]:
    return {"ok": True, "tool": "query_sql", "rows": rows, "error": "",
            "data": {"columns": ["emp_id", "base_salary"],
                     "rows": [["E01", 9000], ["E02", 8000]]}}


def _obs_err(msg: str = "Binder Error: no such column") -> dict[str, Any]:
    return {"ok": False, "tool": "query_sql", "rows": 0, "error": msg,
            "data": None}


def _script(agent: VLMLActionAgent, replies: list[str]) -> None:
    """脚本化 LLM + 记录它每步收到的 messages（追问注没注入，看这里）。"""
    seen: list[list[dict[str, str]]] = []
    it = iter(replies)

    def fake_llm(messages: list[dict[str, str]]) -> str:
        seen.append([dict(m) for m in messages])
        return next(it, "")

    agent.llm = fake_llm  # type: ignore[assignment]
    agent.seen_messages = seen  # type: ignore[attr-defined]


def _mk_gate(script: list[dict[str, Any]]):
    """脚本化闸门 + 调用记录（记录调用时 agent 的已问清单与观察数）。"""
    calls: list[dict[str, Any]] = []
    it = iter(script)

    def gate(agent: VLMLActionAgent) -> dict[str, Any]:
        calls.append({
            "asked": list(getattr(agent, "_gate_asked", None) or []),
            "obs": len(getattr(agent, "observations", None) or []),
        })
        return next(it, {"passed": True, "questions": [], "origin": "llm"})

    return gate, calls


def test_gate_fail_injects_questions_and_keeps_rolling() -> None:
    """闸门不过 → 本步不 done；questions 进下一步 Critique 槽；判词合并。"""
    env = FakeEnv([_obs_ok(), _obs_ok(), _obs_ok(), _obs_ok()])
    a = VLMLActionAgent(env=env)  # type: ignore[arg-type]
    _script(a, ["SELECT emp_id FROM payroll", "SELECT 1", "SELECT 2",
                "SELECT 3", "SELECT 4"])
    gate, calls = _mk_gate([
        {"passed": False, "origin": "llm",
         "questions": ["口径说清：基本工资含不含津贴？", "月份过滤用的是哪一列？"]},
        {"passed": True, "origin": "llm", "questions": []},
    ])

    async def go() -> None:
        await a.solve("八月份工资总额", gate_check=gate, max_iters=4)

    asyncio.run(go())
    assert len(calls) == 2, calls                     # 两步各判一次
    assert a.rollout_num_iter == 2, a.rollout_num_iter
    assert a.last_success is True, "第 2 步双过才收工"
    assert a.last_gate_passed is True
    # 第 1 步闸门不过 → 第 2 步 LLM 收到的 human 里必须带追问（当步注入）。
    # 注意看的是 seen_messages[1]（第 2 步的**输入**），不是 rollout 末条
    # 消息 —— 末条是第 2 步双过后重建的（那时闸门已过、无追问）。
    seen = getattr(a, "seen_messages", [])
    assert len(seen) == 2, len(seen)
    human = seen[1][1]["content"]
    assert "【清单追问】" in human, human
    assert "基本工资含不含津贴" in human, human
    assert "月份过滤用的是哪一列" in human, human
    # 内层判词不被闸门判词顶掉（两份都是可操作反馈）
    assert "取数过程" in human, human
    # 已问清单跨步累积
    assert a._gate_asked == ["口径说清：基本工资含不含津贴？",
                             "月份过滤用的是哪一列？"], a._gate_asked
    print("PASS  闸门不过 → 不 done；追问当步进 Critique 槽；判词合并")


def test_gate_pass_ends_early() -> None:
    """双过 → 本步即 done（不等 4 步）。"""
    env = FakeEnv([_obs_ok(), _obs_ok(), _obs_ok(), _obs_ok()])
    a = VLMLActionAgent(env=env)  # type: ignore[arg-type]
    _script(a, ["SELECT emp_id FROM payroll", "SELECT 2", "SELECT 3",
                "SELECT 4"])
    gate, calls = _mk_gate([{"passed": True, "origin": "llm", "questions": []}])

    async def go() -> None:
        await a.solve("八月份工资总额", gate_check=gate, max_iters=4)

    asyncio.run(go())
    assert len(calls) == 1 and a.rollout_num_iter == 1, (calls, a.rollout_num_iter)
    assert len(env.calls) == 1, env.calls
    assert a.last_success is True and a.last_gate_passed is True
    print("PASS  双过 → 本步 done（4 步封顶不变，提前收工）")


def test_gate_unavailable_does_not_block() -> None:
    """闸门 origin=failed（模型/解析不可用）→ 不阻塞，内层单独判。"""
    env = FakeEnv([_obs_ok()])
    a = VLMLActionAgent(env=env)  # type: ignore[arg-type]
    _script(a, ["SELECT emp_id FROM payroll", "SELECT 2"])
    gate, calls = _mk_gate([{"passed": False, "origin": "failed",
                             "questions": [], "reason": "LLM 调用失败"}])

    async def go() -> None:
        await a.solve("八月份工资总额", gate_check=gate, max_iters=4)

    asyncio.run(go())
    assert len(calls) == 1
    assert a.rollout_num_iter == 1, "闸门不可用不该拖步数"
    assert a.last_success is True, "退化成内层判定"
    assert a.last_gate_passed is None, "None = 没跑成，不是 False"
    assert "闸门不可用" in a.last_gate_critique, a.last_gate_critique
    print("PASS  闸门不可用 → 不阻塞（内层单独判，留痕）")


def test_gate_raises_falls_back() -> None:
    """闸门回调抛异常 → 同上，不把整轮判死。"""

    def boom(agent: VLMLActionAgent) -> dict[str, Any]:
        raise RuntimeError("闸门炸了")

    env = FakeEnv([_obs_ok()])
    a = VLMLActionAgent(env=env)  # type: ignore[arg-type]
    _script(a, ["SELECT emp_id FROM payroll", "SELECT 2"])

    async def go() -> None:
        await a.solve("八月份工资总额", gate_check=boom, max_iters=4)

    asyncio.run(go())
    assert a.rollout_num_iter == 1
    assert a.last_success is True and a.last_gate_passed is None
    assert "闸门不可用" in a.last_gate_critique
    print("PASS  闸门抛异常 → 退回内层判定，不判死")


def test_no_gate_callback_unchanged() -> None:
    """不传 gate_check → 与改动前完全一样（旧调用方零改动）。"""
    env = FakeEnv([_obs_ok()])
    a = VLMLActionAgent(env=env)  # type: ignore[arg-type]
    _script(a, ["SELECT emp_id FROM payroll", "SELECT 2"])

    async def go() -> None:
        await a.solve("八月份工资总额", max_iters=4)

    asyncio.run(go())
    assert a.rollout_num_iter == 1 and a.last_success is True
    assert a.last_gate_passed is None and a.last_gate_result == {}
    assert a.gate_check is None
    print("PASS  无回调 → 原版行为（只有内层 critic 一个判官）")


def test_gate_skipped_when_inner_fails() -> None:
    """内层没过（0 行/报错/真空）→ 闸门不跑（没有可核的提交物）。"""
    env = FakeEnv([_obs_err()])
    a = VLMLActionAgent(env=env)  # type: ignore[arg-type]
    _script(a, ["SELECT bad FROM payroll", "SELECT 2"])
    gate, calls = _mk_gate([{"passed": True, "questions": []}])

    async def go() -> bool:
        await a.reset(task="八月份工资总额", gate_check=gate)
        _e, done = await a.step()
        return done

    done = asyncio.run(go())
    assert done is False
    assert calls == [], "内层没过不该跑闸门"
    assert a.last_gate_result == {} and a.last_gate_passed is None
    assert a.last_success is False
    print("PASS  内层没过 → 闸门不跑（同构「学生交了 spec 才判」）")


def test_prior_questions_accumulate_across_steps() -> None:
    """闸门第二次调用看得见第一次问过什么（同构 priorIterations）。"""
    env = FakeEnv([_obs_ok(), _obs_ok(), _obs_ok(), _obs_ok()])
    a = VLMLActionAgent(env=env)  # type: ignore[arg-type]
    _script(a, ["SELECT 1", "SELECT 2", "SELECT 3", "SELECT 4", "SELECT 5"])
    gate, calls = _mk_gate([
        {"passed": False, "origin": "llm", "questions": ["加班费率出自哪一列？"]},
        {"passed": False, "origin": "llm", "questions": ["SUM 前过滤月份了吗？"]},
        {"passed": True, "origin": "llm", "questions": []},
    ])

    async def go() -> None:
        await a.solve("八月份加班费", gate_check=gate, max_iters=4)

    asyncio.run(go())
    assert len(calls) == 3, calls
    assert calls[0]["asked"] == [], "第一次没有历史"
    assert calls[1]["asked"] == ["加班费率出自哪一列？"], calls[1]
    assert calls[2]["asked"] == ["加班费率出自哪一列？", "SUM 前过滤月份了吗？"], \
        calls[2]
    assert a.rollout_num_iter == 3 and a.last_success is True
    print("PASS  已问清单跨步累积（第二次调用看得见第一次的问句）")


def test_dual_fail_uses_full_cap() -> None:
    """4 步都双不过 → 封顶退出，末态双 False（供上层判未闭合）。"""
    env = FakeEnv([_obs_ok(), _obs_ok(), _obs_ok(), _obs_ok()])
    a = VLMLActionAgent(env=env)  # type: ignore[arg-type]
    _script(a, ["SELECT 1", "SELECT 2", "SELECT 3", "SELECT 4", "SELECT 5"])
    gate, calls = _mk_gate([
        {"passed": False, "origin": "llm", "questions": [f"追问 {i}"]}
        for i in range(4)
    ])

    async def go() -> None:
        await a.solve("八月份加班费", gate_check=gate, max_iters=4)

    asyncio.run(go())
    assert len(calls) == 4 and a.rollout_num_iter == 4
    assert a.last_success is False and a.last_gate_passed is False
    assert len(a._gate_asked) == 4, a._gate_asked
    print("PASS  4 步双不过 → 封顶（attempts=1 的单次 rollout 内完成）")


def main() -> int:
    test_gate_fail_injects_questions_and_keeps_rolling()
    test_gate_pass_ends_early()
    test_gate_unavailable_does_not_block()
    test_gate_raises_falls_back()
    test_no_gate_callback_unchanged()
    test_gate_skipped_when_inner_fails()
    test_prior_questions_accumulate_across_steps()
    test_dual_fail_uses_full_cap()
    print("\n全部通过：步级闸门 = 内层真伪 × 闸门清单，每步双判定、判词当步注入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
