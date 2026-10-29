"""钉住 critic 的两级判据 = 原版语义（LLM 判「任务要求满足了没有」）+ 真空闸门。

原版取证（`voyager_original/voyager/agents/critic.py`）：
    · `:94-117` `ai_check_task_success`：LLM 回 JSON `{reasoning, success, critique}`，
      解析不了就重试（`max_retries=5`）。
    · `:119-141` `check_task_success`：组装 system(=prompts/critic.txt) + human(观察/Task/Context)。
    · `prompts/critic.txt:3`："Exceeding the task requirements is also considered a
      success while failing to meet them requires you to provide critique"
      —— 判的是**任务要求**，不是"执行没报错"。

这里断言的是：
  · 真空（0 行 / 全 NULL / 只有 0）→ 第一级直接拦下，**不调 LLM**
  · 取到数但口径不对（LLM 判 success=false）→ 整体失败，critique 回灌下一轮
  · 口径对（LLM 判 true）→ 成功，`reasoning` 就是结论句（上层拿它当 answer）
  · JSON 坏了 → 按原版重试，重试用尽 → False（原版就是 False）
  · LLM 接口抖动 → 退回第一级并如实标注，不把整轮判死
  · human message 的形状 = SQL + Execution error + Result(n rows) + Task + Context

跑法：python mve/test_critic_caliber.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import voyager_vlml.llm as llm_mod  # noqa: E402
from voyager_vlml.agents.action import VLMLActionAgent  # noqa: E402
from voyager_vlml.agents.critic import VLMLCriticAgent  # noqa: E402

_ORIG_CHAT_JSON = llm_mod.chat_json


class FakeCriticLLM:
    """按脚本回 JSON（dict）或抛异常（Exception 实例）。"""

    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []

    def __call__(self, messages: list[dict[str, str]], **kw: Any) -> dict:
        self.calls += 1
        self.messages.append([dict(m) for m in messages])
        if not self.replies:
            return {}
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _patch(replies: list[Any]) -> FakeCriticLLM:
    f = FakeCriticLLM(replies)
    llm_mod.chat_json = f  # type: ignore[assignment]
    return f


def _restore() -> None:
    llm_mod.chat_json = _ORIG_CHAT_JSON  # type: ignore[assignment]


def _obs(rows: list[list[Any]], cols: list[str] | None = None) -> dict[str, Any]:
    return {"ok": True, "tool": "query_sql", "rows": len(rows), "error": "",
            "data": {"columns": cols or ["a"], "rows": rows}}


# ---------------------------------------------------------------- 第一级：真空
def test_vacuous_short_circuits_without_llm() -> None:
    f = _patch([{"success": True, "critique": "", "reasoning": "不该被调到"}])
    try:
        c = VLMLCriticAgent()
        ok, crit = c.check_task_success(
            observations=[_obs([[None, 0]], ["avg", "n"])],
            task={"task": "算胜率"}, sql="SELECT AVG(x), COUNT(*) FROM agg")
    finally:
        _restore()
    assert ok is False, "真空结果不能算成功"
    assert f.calls == 0, f"第一级拦下时不该调 LLM，实际调了 {f.calls} 次"
    assert c.last_gate == "vacuous", c.last_gate
    assert "没有任何真实值" in crit, crit
    print("PASS  真空 → 第一级拦下、不调 LLM")


def test_all_failed_short_circuits() -> None:
    f = _patch([{"success": True, "critique": "", "reasoning": "x"}])
    try:
        c = VLMLCriticAgent()
        ok, crit = c.check_task_success(
            observations=[{"ok": False, "tool": "query_sql", "rows": 0,
                           "error": "Binder Error: no such column", "data": None}],
            task={"task": "数回合"})
    finally:
        _restore()
    assert ok is False and f.calls == 0
    assert c.last_gate == "no_call", c.last_gate
    assert "no such column" in crit, crit
    print("PASS  全部调用失败 → 不调 LLM，critique 带 SQL 报错")


# ---------------------------------------------------------------- 第二级：口径
def test_caliber_rejected() -> None:
    """取到数但口径错 → 失败（原版判的就是「任务要求」）。"""
    f = _patch([{"success": False,
                 "critique": "把第一回合当成首次击杀了；首血要看 first_kill 列。",
                 "reasoning": "结果算的是第一回合，不是首血。"}])
    try:
        c = VLMLCriticAgent()
        ok, crit = c.check_task_success(
            observations=[_obs([[7]])],
            task={"task": "X 队首杀后胜率"}, sql="SELECT COUNT(*) FROM t WHERE round_number=1")
    finally:
        _restore()
    assert f.calls == 1, f.calls
    assert ok is False, "口径不满足必须判失败（以前这里恒 True）"
    assert c.last_gate == "llm", c.last_gate
    assert "first_kill" in crit, crit
    print("PASS  取到数但口径错 → 失败，critique 指出该看哪一列")


def test_caliber_accepted_reasoning_is_answer() -> None:
    f = _patch([{"success": True, "critique": "",
                 "reasoning": "13 个回合里赢了 9 个，比例 9/13 ≈ 69.2%。"}])
    try:
        c = VLMLCriticAgent()
        d = c.judge(observations=[_obs([[9, 13]], ["wins", "total"])],
                    task={"task": "赢了多少比例"}, sql="SELECT 9, 13")
    finally:
        _restore()
    assert d["success"] is True, d
    assert d["gate"] == "llm", d
    assert "69.2%" in d["reasoning"], d
    assert c.last_reasoning == d["reasoning"]
    print("PASS  口径对 → 成功；reasoning 就是结论句（可当 answer）")


def test_human_message_shape() -> None:
    """形状照原版 critic.render_human_message：观察 + Task + Context。"""
    c = VLMLCriticAgent(use_llm=False)
    msg = c.render_human_message(
        events=[_obs([[9, 13]], ["wins", "total"])],
        task="赢了多少比例", context="首血看 first_kill 列",
        sql="SELECT 9, 13")
    for must in ("SQL: SELECT 9, 13", "Execution error: No error",
                 "Result (1 rows):", "列：wins、total", "行：wins=9，total=13",
                 "Task: 赢了多少比例", "Context: 首血看 first_kill 列"):
        assert must in msg, f"缺 {must!r}\n{msg}"
    err = c.render_human_message(
        events=[{"ok": False, "tool": "query_sql", "rows": 0,
                 "error": "Binder Error: no such column", "data": None}],
        task="数回合", sql="SELECT bad")
    assert "Execution error: Binder Error" in err, err
    assert "Result: None" in err, err
    print("PASS  human message = SQL + Execution error + Result + Task + Context")


def test_system_message_is_prompt_file() -> None:
    c = VLMLCriticAgent(use_llm=False)
    sysmsg = c.render_system_message()
    assert '"success": boolean' in sysmsg, sysmsg[:200]
    assert "口径" in sysmsg, "把口径写清楚，模型才判得动"
    print("PASS  system message 来自 prompts/critic.txt（带 JSON 契约）")


# ---------------------------------------------------------------- JSON / 抖动
def test_bad_json_retries_like_original() -> None:
    """原版 `:106-117`：解析不了递归重试；用尽返回 (False, "")。"""
    f = _patch([{}, {}, {}, {}, {}, {}])
    try:
        c = VLMLCriticAgent(max_retries=5)
        ok, critique, reasoning = c.ai_check_task_success([{"role": "user", "content": "x"}])
    finally:
        _restore()
    assert f.calls == 5, f"应该正好重试 5 次，实际 {f.calls}"
    assert ok is False and critique == "" and reasoning == ""
    print("PASS  JSON 坏 → 重试到 max_retries（5 次），之后 False（同原版）")


def test_bad_json_then_good() -> None:
    f = _patch([{}, {"success": True, "critique": "", "reasoning": "4 根木头"}])
    try:
        c = VLMLCriticAgent()
        ok, _crit, reasoning = c.ai_check_task_success([{"role": "user", "content": "x"}])
    finally:
        _restore()
    assert f.calls == 2 and ok is True and "4" in reasoning
    print("PASS  第一次回的不是 JSON → 重试成功")


def test_llm_down_falls_back_to_gate() -> None:
    _patch([RuntimeError("HTTP 502")])
    try:
        c = VLMLCriticAgent()
        d = c.judge(observations=[_obs([[9]])], task={"task": "数回合"}, sql="SELECT 9")
    finally:
        _restore()
    assert d["success"] is True, "接口抖动不能把整轮判死（第一级已确认取到数）"
    assert d["gate"] == "llm_failed", d["gate"]
    assert "口径检验没跑成" in d["critique"], d["critique"]
    print("PASS  LLM 挂了 → 退回第一级 + 如实标注")


# ---------------------------------------------------------------- 行放大证据
def test_split_from_parsing() -> None:
    """check_grain 的探针要拿到「驱动表别名 + FROM…WHERE 段」。"""
    from voyager_vlml.env.vlml_mcp import _split_from

    alias, tail = _split_from(
        "SELECT 1 FROM ext_player_round_stats e JOIN rounds r "
        "ON e.game_id = r.game_id WHERE e.map_name = 'Sunset' ORDER BY 1 LIMIT 5")
    assert alias == "e", alias
    assert tail.lower().startswith("from ext_player_round_stats e"), tail
    assert "where" in tail.lower() and "order by" not in tail.lower(), tail

    alias, _t = _split_from("SELECT 1 FROM rounds WHERE x = 1")
    assert alias == "rounds", "没别名时用表名"
    assert _split_from("SELECT 1") is None
    assert _split_from("WITH t AS (SELECT 1) SELECT * FROM t JOIN u USING(k)") is None
    assert _split_from("SELECT * FROM (SELECT 1) s JOIN u ON s.a = u.a") is None
    print("PASS  _split_from：别名/无别名/CTE/子查询 都按预期")


def test_grain_evidence_reaches_critic_and_action() -> None:
    obs = _obs([[169, 286]], ["wins", "total"])
    obs["grain"] = {"driver": "e", "rows": 286, "distinct": 13, "ratio": 22.0}
    c = VLMLCriticAgent(use_llm=False)
    msg = c.render_human_message(
        events=[obs], task="X 队首杀后胜率", sql="SELECT ... JOIN rounds r ON ...")
    assert "Grain check" in msg, msg
    assert "286 行" in msg and "13 行不同" in msg and "22.0×" in msg, msg

    a = VLMLActionAgent(env=FakeEnv([]))  # type: ignore[arg-type]
    human = a.render_human_message(
        events=[obs], code="SELECT ...", task="X 队首杀后胜率")
    assert "Grain check" in human and "放大" in human, human

    # 没放大时不喊"放大"，只报实测
    obs2 = _obs([[13, 13]], ["wins", "total"])
    obs2["grain"] = {"driver": "e", "rows": 13, "distinct": 13, "ratio": 1.0}
    msg2 = c.render_human_message(events=[obs2], task="t", sql="s")
    assert "无放大" in msg2 and "放大 1" not in msg2, msg2
    print("PASS  行放大实测进两边观察（放大时写明倍数，无放大如实报）")


def test_observe_grain_only_on_join() -> None:
    """没有 JOIN 的 SQL 不该多打一次库（省一次往返）。"""
    calls: list[str] = []

    class EnvWithGrain(FakeEnv):
        async def check_grain(self, sql: str) -> dict:
            calls.append(sql)
            return {"driver": "a", "rows": 2, "distinct": 1, "ratio": 2.0}

    a = VLMLActionAgent(env=EnvWithGrain([_obs([[1]])]))  # type: ignore[arg-type]
    a.critic = VLMLCriticAgent(use_llm=False)   # 这条只验探针，不烧接口
    _script_action(a, ["SELECT 1 FROM rounds"])

    async def go() -> None:
        await a.reset(task="t")
        await a.step()

    asyncio.run(go())
    assert calls == [], "无 JOIN 不该测粒度"
    print("PASS  无 JOIN 不测粒度（不多打一次库）")



class FakeEnv:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = list(results)
        self.calls: list[str] = []
        self.tool_names = ["query_sql"]

    async def schema_block(self) -> str:
        return "- rounds（285 行）: winning_team_name::str"

    async def run_sql(self, sql: str) -> dict[str, Any]:
        self.calls.append(sql)
        return self.results.pop(0) if self.results else {
            "ok": False, "tool": "query_sql", "rows": 0,
            "error": "no more canned results", "data": None}

    def data_source(self) -> str:
        return "fake.gg"


def _script_action(agent: VLMLActionAgent, replies: list[str]) -> None:
    it = iter(replies)

    def fake_llm(messages: list[dict[str, str]]) -> str:
        return next(it, "")

    agent.llm = fake_llm  # type: ignore[assignment]


def test_critique_feeds_next_iteration_and_answer() -> None:
    """第 1 轮口径错 → critique 进第 2 轮的 Critque 槽；第 2 轮成功 → answer 是结论句。"""
    env = FakeEnv([_obs([[12]], ["n"]), _obs([[9, 13]], ["wins", "total"])])
    a = VLMLActionAgent(env=env)  # type: ignore[arg-type]
    _script_action(a, ["SELECT COUNT(*) AS n FROM t",
                       "SELECT 9 AS wins, 13 AS total"])
    f = _patch([
        {"success": False, "critique": "数的是回合数，不是胜的回合数；要按胜负过滤。",
         "reasoning": "12 是总回合数。"},
        {"success": True, "critique": "", "reasoning": "13 个回合里赢了 9 个，9/13 ≈ 69.2%。"},
    ])
    try:
        obs, sql = asyncio.run(a.rollout(task="X 队赢了多少比例的回合？",
                                        context="首血看 first_kill 列"))
    finally:
        _restore()
    assert f.calls == 2, f.calls
    assert a.rollout_num_iter == 2, a.rollout_num_iter
    human2 = f.messages[1][1]["content"]
    assert "Task: X 队赢了多少比例的回合？" in human2, human2
    assert "Context: 首血看 first_kill 列" in human2, human2
    assert "SQL: SELECT 9 AS wins" in human2, human2
    assert a.last_success is True, "第 2 轮口径对 → 成功"
    assert "69.2%" in a.answer, a.answer
    print("PASS  口径 critique 进下一轮；成功后 answer = 结论句（不是 SQL）")


def test_material_ledger_reaches_judge() -> None:
    """材料账（数据源实际返回）必须进判官视野。

    2026-10-29 端到端实测：gh_repo_info 只返回了仓库 description，答案却多编
    了一句「README 中提到的关键模块是…」，判官照样放行 —— 因为判官只看模型
    自述的 RESULT（observations.data），看不到工具实际返回什么，无从对账。
    """
    import task_critic
    res = {
        "execution_backend": "local", "status": "written",
        "answer": "这个仓库是 XXX，README 提到的关键模块是 MVE",
        "execution": {"data_sources": ["gh_repo_info"], "calls": 1,
                      "real_calls": 1, "rows": 1, "ok": True},
        "observations": [{"ok": True, "rows": 1, "data": {"answer": "…"}}],
        "evidence": [{"tool": "gh_repo_info", "ok": True,
                      "text": "description: LLM Agent 的在线强化学习系统"}],
    }
    txt = task_critic._evidence_text(res)
    assert "材料账" in txt, txt
    assert "gh_repo_info" in txt and "在线强化学习" in txt, txt
    res2 = dict(res, evidence=[{"tool": "web_fetch", "ok": False,
                                "error": "HTTPError: 403 Forbidden"}])
    txt2 = task_critic._evidence_text(res2)
    assert "不算材料" in txt2, txt2
    print("PASS  材料账进判官观察面（实际返回 + 失败条目标注不算材料）")


def main() -> int:
    test_vacuous_short_circuits_without_llm()
    test_all_failed_short_circuits()
    test_caliber_rejected()
    test_caliber_accepted_reasoning_is_answer()
    test_human_message_shape()
    test_system_message_is_prompt_file()
    test_bad_json_retries_like_original()
    test_bad_json_then_good()
    test_llm_down_falls_back_to_gate()
    test_split_from_parsing()
    test_grain_evidence_reaches_critic_and_action()
    test_observe_grain_only_on_join()
    test_critique_feeds_next_iteration_and_answer()
    test_material_ledger_reaches_judge()
    print("\n全部通过：critic = 真空闸门 + 原版口径检验（LLM）+ 行放大实测证据")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
