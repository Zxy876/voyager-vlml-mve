#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""技能**合成**（composition）—— 原版 Voyager 真正的复用方式。

取证（原版，对照后才有底气说"同构"）：
  · `voyager/agents/skill.py`：`programs` 属性 = control_primitives + **全部技能
    代码**；`generate_skill_description` 把技能做成**具名函数**
    `async function {name}(bot) { // 语义 }`。
  · `voyager/env/mineflayer/index.js:253-256`
    `await eval("(async () => {" + programs + "\\n" + code + "})()")`
    —— 技能定义和本次新写的代码在**同一个作用域**里，所以新代码能直接调用
    已学技能。
  · 真合成的证据：skill_library/trial1 的 51 条技能里有 7 处技能调用技能
    （`smeltFiveRawIron`→`craftFurnace`、`mineTenCobblestone`→`craftWoodenPickaxe`
    、`cookPorkchops`→`craftFurnace` …）。
  · prompt 原话（prompts/action_template.txt:39）：
    "Reuse the above useful programs as much as possible"。

MVE 之前只把技能 code **贴进 system 让模型照抄** —— 那是"给个例子"，不是
合成：执行命名空间里没有这个函数，模型想调也调不到。表现（2026-10-20 截图）：
图谱高置信命中、技能库有 6 条，消费却两次都报"现场现做 · 用到 0 条技能"。

这组测试钉死四件事：
  1. 技能名 → 稳定、合法的函数名；
  2. 技能在**执行域**里可调用（不是只在 prompt 里出现）；
  3. 新代码能一次调用多条技能**合成**一个答案；
  4. 消费侧检索用**本题归题结果**当键（原来传的是空的 topic_hint，
     于是按词面挑到了别人那批技能）。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_agent  # noqa: E402


def test_skill_name_is_stable_and_safe():
    """技能名 → 合法标识符；同名同函数；空名不给名字（不许造 skill_）。"""
    f = local_agent._skill_call_name
    assert f("def") == "skill_def", f("def")
    assert f("core elements") == "skill_core_elements"
    assert f("2nd") == "skill_d2nd", f("2nd")       # 数字开头要补前缀
    assert f("") == "" and f(None) == "" and f("###") == ""
    assert f("def") == f("def"), "同名必须稳定（否则跨次调用对不上）"
    print("PASS  test_skill_name_is_stable_and_safe")


def test_skill_function_is_callable_in_execution_scope():
    """技能必须在**执行域**里，模型写 `skill_def()` 才真的调得到（不是照抄）。"""
    import asyncio

    tmp = Path(tempfile.mkdtemp())
    seen: dict[str, str] = {}

    def fake(msgs):
        seen["system"] = str((msgs[0] or {}).get("content") or "")
        return "```python\nRESULT = skill_def()\n```"

    try:
        obs, code = asyncio.run(local_agent.solve(
            "正念的定义是什么？", str(tmp), llm=fake, scene_id="", max_iters=1,
            skills=[{"name": "def", "text": "正念的定义",
                     "code": "RESULT = {'answer': '正念是…', 'source': 'learned'}"}]))
        assert code.strip() == "RESULT = skill_def()", code
        assert obs and obs[-1].get("ok"), obs
        assert obs[-1]["data"] == {"answer": "正念是…", "source": "learned"}, obs[-1]
        # 调用了已学技能 → 取证痕迹里要有它（真空判据此不再判"没取数"）
        assert "skill:def" in (obs[-1].get("data_sources") or []), obs[-1]
        # prompt 里要给的是**可调用的函数名**，不是"照抄这段"
        assert "skill_def()" in seen["system"], seen["system"][-400:]
        assert "Reuse the above useful programs" in seen["system"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS  test_skill_function_is_callable_in_execution_scope")


def test_model_code_can_compose_two_skills():
    """合成 = 一次调用多条已有技能，把它们的结果拼成一个新答案（原版 7 处实证）。"""
    import asyncio

    tmp = Path(tempfile.mkdtemp())

    def fake(_msgs):
        return ("```python\n"
                "RESULT = {'定义': skill_def(), '起源': skill_origin()}\n"
                "```")

    try:
        obs, _ = asyncio.run(local_agent.solve(
            "正念是什么", str(tmp), llm=fake, scene_id="", max_iters=1,
            skills=[{"name": "def", "code": "RESULT = 'A'"},
                    {"name": "origin", "code": "RESULT = 'B'"}]))
        assert obs and obs[-1].get("ok"), obs
        assert obs[-1]["data"] == {"定义": "A", "起源": "B"}, obs[-1]
        src = obs[-1].get("data_sources") or []
        assert "skill:def" in src and "skill:origin" in src, src
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS  test_model_code_can_compose_two_skills")


def test_retrieve_uses_this_questions_topic():
    """消费侧检索键 = **本题归题结果**（2026-10-20 实测根因）。

    问「正念是什么」（topic=consume_ca2125be）时，空的 topic_hint 会让
    `_pick_ability_topic` 按词面挑到「正念练习的常见误区」那批技能 →
    本题自己那批一条都进不了 prompt，于是"命中却用到 0 条技能"。
    """
    import skill_store

    root = Path(tempfile.mkdtemp())
    old = skill_store.STORE
    try:
        skill_store.STORE = root / "skill_store.json"
        # 两批技能：ca2125be（本题）与 2461737d（别的题，词面上更"像"）
        texts = {"def": "正念的定义是什么（闭合这个维度时跑通的取数做法）",
                 "origin": "正念的起源是什么（闭合这个维度时跑通的取数做法）",
                 "mis1": "正念练习的常见误区是什么（闭合这个维度时跑通的取数做法）"}
        for topic, dims in (("consume_ca2125be", ["def", "origin"]),
                            ("consume_2461737d", ["mis1"])):
            for d in dims:
                skill_store.add(
                    topic, d, texts[d],
                    code=f"RESULT = {{'t': '{topic}', 'd': '{d}'}}",
                    source="learn", exact_only=True,
                    blueprint={"dimension": d})
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from voyager_vlml.consume import _retrieve_skills, _pick_ability_topic

        # 词面兜底会挑到别人那批（这就是原来的 bug）
        picked = _pick_ability_topic("正念是什么",
                                     list(skill_store.all_skills()))
        got = _retrieve_skills("正念是什么", "consume_ca2125be", "")
        assert got, "归题结果有技能就必须检索到"
        assert {s.get("topic") for s in got} == {"consume_ca2125be"}, \
            [s.get("topic") for s in got]
        # 顺便验证兜底分支：本题那批不存在时才按词面挑
        got2 = _retrieve_skills("正念是什么", "consume_nothing", "")
        assert {s.get("topic") for s in got2} == {picked}, \
            ([s.get("topic") for s in got2], picked)
    finally:
        skill_store.STORE = old
        shutil.rmtree(root, ignore_errors=True)
    print("PASS  test_retrieve_uses_this_questions_topic")


def test_hit_question_must_call_learned_skills():
    """命中的题**必须**调用已学技能（用户 2026-10-20：「所有命中的题目都要用到
    已经学到的技能」）。

    给 2 条可调用技能，模型第一版照旧凭记忆写答案（实测就是这样）→ 判不通过
    并带评语重写 → 第二版调用 `skill_def()` 才通过。没有这条强制，技能库就是
    摆设：图谱命中、技能在库，消费却永远"现场现做 · 用到 0 条技能"。
    """
    import asyncio

    tmp = Path(tempfile.mkdtemp())
    n = {"i": 0}

    def fake(_msgs):
        n["i"] += 1
        if n["i"] == 1:                       # 第一版：凭记忆（没调用技能）
            return "```python\nRESULT = {'answer': '凭记忆写的'}\n```"
        return "```python\nRESULT = skill_def()\n```"

    try:
        obs, _ = asyncio.run(local_agent.solve(
            "正念是什么", str(tmp), llm=fake, scene_id="", max_iters=2,
            require_skills=True,
            skills=[{"name": "def", "code": "RESULT = {'answer': '从技能来'}"}]))
        assert len(obs) == 2, obs                # 第一版被判不通过 → 重写
        assert not obs[0]["ok"], obs[0]
        assert "一条已学技能都没调用" in (obs[0].get("error") or ""), obs[0]
        assert obs[-1]["ok"], obs[-1]
        assert obs[-1]["data"] == {"answer": "从技能来"}, obs[-1]
        assert obs[-1].get("skills_called") == ["def"], obs[-1]

        # 对照：不强制时凭记忆的答案照收（学习/本机场景不能一律判死）
        n["i"] = 0
        obs2, _ = asyncio.run(local_agent.solve(
            "正念是什么", str(tmp), llm=fake, scene_id="", max_iters=1,
            skills=[{"name": "def", "code": "RESULT = {'answer': '从技能来'}"}]))
        assert obs2[-1]["ok"] and obs2[-1]["data"] == {"answer": "凭记忆写的"}, obs2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS  test_hit_question_must_call_learned_skills")


def test_skill_replay_inherits_event_loop():
    """技能重放必须**显式继承主循环**（2026-10-21 服务器实测根因）。

    许嵩 4 次导入全报 `已学技能 style 重放失败：NameError: name
    'mcp_neko_web_search' is not defined`：技能函数在外层 `_run_restricted`
    的 worker 线程里重新构建命名空间，那个线程没有运行中的循环，
    `_ns_base` 吞掉 RuntimeError → mcp_* 一个都注入不进 → 技能内 NameError。
    与 run_saved_async 忘传主循环是同一类坑，只是长在嵌套的技能函数路径上。
    """
    import asyncio
    import threading

    tmp = Path(tempfile.mkdtemp())
    real = local_agent._ns_base
    seen: list[tuple[str, Any]] = []

    def spy(scene_dir, scene_id="", tracer=None, loop=None, evidence=None,
        db_path=""):
        seen.append((threading.current_thread().name, loop))
        return real(scene_dir, scene_id, tracer=tracer, loop=loop,
                    evidence=evidence)

    local_agent._ns_base = spy

    def fake(_msgs):
        return "```python\nRESULT = skill_def()\n```"

    try:
        obs, _ = asyncio.run(local_agent.solve(
            "许嵩的音乐风格", str(tmp), llm=fake, scene_id="", max_iters=1,
            skills=[{"name": "def", "code": "RESULT = {'a': 1}"}]))
        assert obs and obs[-1]["ok"], obs[-1]
        assert seen, "spy 没被调到"
        main_loop = seen[0][1]
        assert main_loop is not None, "solve 必须在循环线程捕获主循环"
        worker_calls = [s for s in seen[1:]
                        if s[0] != threading.main_thread().name]
        assert worker_calls, f"技能重放应发生在 worker 线程：{seen}"
        for tname, loop in worker_calls:
            assert loop is main_loop, (
                f"worker 线程（{tname}）里的技能重放收到 loop={loop}，"
                "必须显式传主循环，否则 mcp_* 注入全失败 → 技能内 NameError")
    finally:
        local_agent._ns_base = real
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS  test_skill_replay_inherits_event_loop")


if __name__ == "__main__":
    test_skill_name_is_stable_and_safe()
    test_skill_function_is_callable_in_execution_scope()
    test_model_code_can_compose_two_skills()
    test_retrieve_uses_this_questions_topic()
    test_hit_question_must_call_learned_skills()
    test_skill_replay_inherits_event_loop()
    print("\nALL PASS")
