#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A/B 探针：验证「割掉取数待填键」前后，Phase2 在**取不到数**时的行为差异。

设计要点（避免污染真环境）：
  · 不调任何 LLM —— solve / critic / intent_diff 全部打桩；
  · 打桩的 solve 模拟「模型交了程序，但库里没那个量」：
      obs = [{"ok": True, "rows": 0, "data": {}, "error": ""}]
      code = 一段带 RESULT 的程序（代码非空 → 排除「没交出程序」那道闸）
  · 桩的 skill_debug.run_code 也返回 ok=False（模拟零上下文单跑门不过）
  · 桩的 _p2_critic 返回 success=False（模拟 critic 没判过）

对照量：
  旧版 → 四道 continue 全触发 → _best 永远 None → out["code"] 为空
        → 编排层 `if str(_fc.get("code") or "").strip():` 不成立
        → intent_diff **根本没被问过** → 无追问、无判词 → 空转
  新版 → 记录事实后继续 → critic 照常跑 → _best 被填 → code 非空
        → intent_diff 被调用 → 有分歧就有追问

用法：python mve/_probe_phase2_submit.py <模块路径>
"""
import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

MOD_PATH = sys.argv[1]
REPO = "/root/mve"
sys.path.insert(0, REPO + "/mve")
sys.path.insert(0, REPO)

# ── 打桩：local_agent.solve ─────────────────────────────────────────
STUB = types.ModuleType("local_agent")
CALLS = {"solve": 0, "critic": 0, "run_code": 0}


async def _fake_solve(question, scene_dir, **kw):
    """模拟：模型交了程序，但环境里**没有那个量**（rows=0、RESULT 空）。"""
    CALLS["solve"] += 1
    obs = [{"ok": True, "rows": 0, "data": {}, "error": "",
            "tool": "query_sql", "sql": "SELECT * FROM no_such_table"}]
    code = ("def derive():\n"
            "    rows = query_sql(\"SELECT * FROM no_such_table\")\n"
            "    return {\"rows\": len(rows.get(\"rows\") or [])}\n"
            "RESULT = {\"overall\": derive(), \"verified\": False}\n")
    return obs, code


STUB.solve = _fake_solve
sys.modules["local_agent"] = STUB

# ── 打桩：skill_debug.run_code（零上下文单跑门 → 不过）────────────────
_SDBG = types.ModuleType("skill_debug")


def _fake_run_code(code, scene_id, **kw):
    CALLS["run_code"] += 1
    return {"ok": False, "RESULT": {}, "error": "NameError: query_sql 未定义",
            "calls": []}


_SDBG.run_code = _fake_run_code
sys.modules["skill_debug"] = _SDBG

# ── 载入目标模块 ────────────────────────────────────────────────────
spec = importlib.util.spec_from_file_location("lo_probe", MOD_PATH)
lo = importlib.util.module_from_spec(spec)
sys.modules["lo_probe"] = lo
spec.loader.exec_module(lo)

# ── 打桩：_p2_critic（critic 判「没做到」，让循环继续）──────────────
_orig_critic = lo._p2_critic


async def _fake_critic(**kw):
    CALLS["critic"] += 1
    return {"success": False, "critique": "桩评语：没做到", "reasoning": "",
            "origin": "stub"}


lo._p2_critic = _fake_critic

# ── 打桩：intent_diff（编排层调用它；这里单独模拟一次真调用）────────
IDF = {"called": 0, "divergences": [], "code_seen": ""}


async def _fake_intent_diff(scene_id, question, spec_text, final_code, **kw):
    IDF["called"] += 1
    IDF["code_seen"] = str(final_code or "")
    IDF["divergences"] = [{
        "student_facing_question": "spec 承诺的每一项在 code 里都有对应吗？",
        "classification": "missing"}]
    return {"divergences": IDF["divergences"], "origin": "llm", "reason": ""}


lo.intent_diff = _fake_intent_diff

# ── 跑 Phase2 ──────────────────────────────────────────────────────
SPEC = ("1. 查 no_such_table 里有多少行\n"
        "2. 把行数放进 RESULT['overall']")


async def main():
    res = await lo.phase2_final_code(
        "PROBE", "统计 no_such_table 的行数", SPEC,
        [{"id": "d1", "question": "查 no_such_table 的行数"}],
        db_path="", scene_dir="", llm=None,
        max_iters=1, max_rounds=2, timeout=30.0,
        topic_id="probe_topic", no_draft=True)

    code = str(res.get("code") or "")
    print("── Phase2 返回 ──")
    print("code 非空        :", bool(code.strip()), "(len=%d)" % len(code))
    print("rows             :", res.get("rows"))
    print("result           :", json.dumps(res.get("result"), ensure_ascii=False))
    print("verified         :", res.get("verified"))
    print("why              :", str(res.get("why") or "")[:120])
    print("exec_note        :", str(res.get("exec_note") or "")[:120])
    print("rounds(LLM solve):", CALLS["solve"])
    print("critic 调用次数  :", CALLS["critic"])
    print("run_code 调用次数:", CALLS["run_code"])

    # ── 模拟编排层（:5693 附近）的判定 ────────────────────────────
    print("")
    print("── 编排层判定（照 learn_orchestrate.py:5693 的写法）──")
    if code.strip():
        idf = await lo.intent_diff("PROBE", "统计 no_such_table 的行数",
                                   SPEC, code, max_items=5, run_result={})
        divs = [d for d in (idf.get("divergences") or []) if isinstance(d, dict)]
        print("intent_diff 被调用:", IDF["called"] > 0)
        print("intent_diff 拿到 code:", bool(IDF["code_seen"].strip()))
        print("分歧条数         :", len(divs))
        print("学生面追问       :",
              (divs[0].get("student_facing_question") if divs else "(无)"))
        print("⇒ 结论           : 有追问有判词（不空转）")
    else:
        print("intent_diff 被调用: False")
        print("⇒ 结论           : **空转**（判官根本没被问过）")


asyncio.run(main())
