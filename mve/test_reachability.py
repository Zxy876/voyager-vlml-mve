#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「任务可达」必须是**真判**，不是常量 true。

调研取证（2026-10-20，两个原项目对照）：
  · 原版 Voyager **没有**独立的可达判定环节 —— 靠生成期 prompt 约束
    （curriculum.txt:29/33）+ 执行反馈（critic.py:42-45 onError 短路、
    action.py:263-273 把「我还缺什么」当下一轮检索 query）。
  · maieutic 靠"课程单元工具箱"在**生成期**就卡掉不可答维度
    （scaffolding.ts:57-64）+ "assume X" 也算答到（spec-examiner.ts:91-97）。
  · MVE 有明确的能力清单（猫爪总开关下的条目 = 同构 neko 插件），所以可达
    **可以也必须真判**。

旧代码在本机/知识型分支无条件写 `status="reachable"`、why 写死一句 ——
那是"不判"，不是"判过"：MCP 没连上、联网开关关掉时照样宣称可达，模型就
只能凭记忆答（学习闭环退化）。现在改成查注册表，这里钉死两种结果。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import learn_orchestrate as L  # noqa: E402


class _FakeActions:
    """假注册表：可控地返回"有/没有可用条目"。"""

    def __init__(self, names: list[str]):
        self.names = names
        self.registered = []

    def restore_switches(self):
        return {}

    def register_local(self, scene_id, scene_dir):
        self.registered.append(("local", scene_id))
        return {}

    def register_net(self, scene_id):
        self.registered.append(("net", scene_id))
        return {}

    def register_gh(self, scene_id):
        # 2026-10-29 起 available_grabs 会注册 gh 组（学习侧与消费侧同一份
        # 工具菜单），假注册表必须跟着有这个入口 —— 否则 available_grabs
        # 走 except 分支返回空列表，下面两个用例会假阴性。
        self.registered.append(("gh", scene_id))
        return {}

    def enabled_entries(self, scene_id):
        return [{"name": n, "origin": "local"} for n in self.names]


def test_no_grabs_means_not_reachable():
    """一个可用手段都没有 → 空列表（调用方据此判 gap，不宣称可达）。"""
    fake = _FakeActions([])
    old = sys.modules.get("actions")
    sys.modules["actions"] = fake
    try:
        assert L.available_grabs("T", "/tmp/x") == [], "没条目就该报空"
        # 仍然要把本机/联网注册一遍再判（注册前查会得到假阴性）
        assert ("local", "T") in fake.registered
        assert ("net", "T") in fake.registered
    finally:
        if old is None:
            sys.modules.pop("actions", None)
        else:
            sys.modules["actions"] = old
    print("PASS  test_no_grabs_means_not_reachable")


def test_grabs_are_listed_not_assumed():
    """有条目 → 把**名字**列出来当依据（why 要能给人看），不是一句口头保证。"""
    fake = _FakeActions(["list_files", "web_search", "mcp_ddg_search"])
    old = sys.modules.get("actions")
    sys.modules["actions"] = fake
    try:
        grabs = L.available_grabs("T", "/tmp/x")
        assert grabs == ["list_files", "web_search", "mcp_ddg_search"], grabs
    finally:
        if old is None:
            sys.modules.pop("actions", None)
        else:
            sys.modules["actions"] = old
    print("PASS  test_grabs_are_listed_not_assumed")


def test_learned_skills_are_injected_back_into_execution():
    """「可达 = 一整个循环」（用户 2026-10-20 纠正）：

    原版不先判"能不能做到"，而是把**已学会的技能 code** 拼进可用函数清单
    （voyager.py:195 `retrieve_skills` → action.py:94 `+ skills`）—— 技能本身
    就是新的 primitive，所以能力边界随技能库增长而扩张：以前做不到的，学会之后
    下次就够得着。MVE 的 VLML 路径一直这么干（voyager_vlml/agents/action.py:
    159-199），本机/知识型路径的 `local_agent.solve` 之前**没有 skills 参数**
    → 技能只进库、不回流，学再多次也不会更可达。
    """
    import asyncio
    import shutil
    import local_agent

    tmp = Path(tempfile.mkdtemp())
    seen: dict[str, str] = {}

    def fake(msgs):
        seen["system"] = str((msgs[0] or {}).get("content") or "")
        return "```python\nRESULT = {'answer': 'ok'}\n```"

    code = 'r = web_search(query="阿斯伯格 诊断标准")'
    try:
        asyncio.run(local_agent.solve(
            "诊断标准有哪些？", str(tmp), llm=fake, scene_id="", max_iters=1,
            skills=[{"name": "diagnosis", "code": code}]))
        # 合成（不是照抄）：prompt 给的是**可调用的函数名** + 原版那条规则
        assert "skill_diagnosis()" in seen["system"], \
            "已学技能没进 system：" + seen["system"][-300:]
        assert "Reuse the above useful programs" in seen["system"], \
            seen["system"][-300:]

        seen.clear()
        asyncio.run(local_agent.solve(
            "诊断标准有哪些？", str(tmp), llm=fake, scene_id="", max_iters=1))
        assert code not in seen.get("system", ""), "没给 skills 就不该出现"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS  test_learned_skills_are_injected_back_into_execution")


def test_real_registry_never_crashes():
    """真注册表（有本机原语）下不抛异常、返回非空 —— 兜底不许炸掉学习循环。"""
    tmp = tempfile.mkdtemp()
    grabs = L.available_grabs("__test_scene__", tmp)
    assert isinstance(grabs, list), grabs
    assert grabs, "本机原语应该总在（register_local 会注册）"
    assert all(isinstance(x, str) and x for x in grabs), grabs
    print("PASS  test_real_registry_never_crashes")


if __name__ == "__main__":
    test_no_grabs_means_not_reachable()
    test_grabs_are_listed_not_assumed()
    test_learned_skills_are_injected_back_into_execution()
    test_real_registry_never_crashes()
    print("\nALL PASS")
