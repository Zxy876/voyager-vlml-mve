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


if __name__ == "__main__":
    test_skill_name_is_stable_and_safe()
    test_skill_function_is_callable_in_execution_scope()
    test_model_code_can_compose_two_skills()
    test_retrieve_uses_this_questions_topic()
    print("\nALL PASS")
