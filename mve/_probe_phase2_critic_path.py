#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针 2：验证「取不到数时，critic 判成功这条路径是否可达」。

背景（A/B 探针 1 的实测结论）：
  旧版 / 新版 在「模型交了程序但取不到数」时，**out["code"] 都非空**
  （因为 `out["code"] = str(code or "")` 在闸门之前执行），
  编排层的 `if str(_fc.get("code") or "").strip():` 两边都成立 ⇒
  intent_diff 两边都被调用。

  真正的差异是：**critic 调用次数 旧 0 / 新 2**。
  旧版在「取不到数」处 continue ⇒ 直接跳过了 ③ critic 那一段 ⇒
  critic **一次都没被问过** ⇒ 模型只收到「程序没跑出结果，去取数」这种
  取数导向的评语 ⇒ 永远不知道「spec 与 code 对不对齐」⇒ 原地打转。

决定性问题：**旧版下，critic 判「做到了」这条路到底可达吗？**
  · 旧版：continue 跳过 critic → verified 永远 False → **不可达**
  · 新版：记录事实后继续 → critic 被问 → 判成功 → verified=True → **可达**

桩设定：solve 返回「代码非空但取不到数」；critic 一律判 success=True。
预期：
  旧版 → verified=False，critic 0 次，why="程序没跑出结果…"
  新版 → verified=True ，critic 1 次，why=""

用法：python mve/_probe_phase2_critic_path.py <模块路径>
"""
import asyncio
import importlib.util
import json
import sys
import types

MOD_PATH = sys.argv[1]
REPO = "/root/mve"
sys.path.insert(0, REPO + "/mve")
sys.path.insert(0, REPO)

CALLS = {"solve": 0, "critic": 0, "run_code": 0}

STUB = types.ModuleType("local_agent")


async def _fake_solve(question, scene_dir, **kw):
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

_SDBG = types.ModuleType("skill_debug")


def _fake_run_code(code, scene_id, **kw):
    CALLS["run_code"] += 1
    return {"ok": False, "RESULT": {}, "error": "NameError: query_sql 未定义",
            "calls": []}


_SDBG.run_code = _fake_run_code
sys.modules["skill_debug"] = _SDBG

spec = importlib.util.spec_from_file_location("lo_probe2", MOD_PATH)
lo = importlib.util.module_from_spec(spec)
sys.modules["lo_probe2"] = lo
spec.loader.exec_module(lo)


async def _fake_critic(**kw):
    """critic 一律判「做到了」—— 如果这条路可达，verified 必须是 True。"""
    CALLS["critic"] += 1
    return {"success": True, "critique": "桩评语：做到了", "reasoning": "",
            "origin": "stub"}


lo._p2_critic = _fake_critic

SPEC = "1. 查 no_such_table 里有多少行\n2. 把行数放进 RESULT['overall']"


async def main():
    res = await lo.phase2_final_code(
        "PROBE", "统计 no_such_table 的行数", SPEC,
        [{"id": "d1", "question": "查 no_such_table 的行数"}],
        db_path="", scene_dir="", llm=None,
        max_iters=1, max_rounds=2, timeout=30.0,
        topic_id="probe_topic", no_draft=True)

    print("── 结果 ──")
    print("verified          :", res.get("verified"),
          "  ← 旧版应为 False（critic 判成功这条路不可达）")
    print("critic 调用次数   :", CALLS["critic"], " ← 旧版应为 0")
    print("solve 调用次数    :", CALLS["solve"])
    print("code 非空         :", bool(str(res.get("code") or "").strip()))
    print("rows              :", res.get("rows"), " ← 取不到数（模拟环境不可达）")
    print("why               :", str(res.get("why") or "")[:140])
    print("critic 判定记录   :", json.dumps(res.get("critic") or {},
                                           ensure_ascii=False)[:160])


asyncio.run(main())
