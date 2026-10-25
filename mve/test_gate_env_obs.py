#!/usr/bin/env python3
"""环境实查进闸门：维度闭合判官的口径真实性判据（2026-10-26）。

背景（实测事故）：场景 B 重学后 `SUM(e.base_salary * a.total_overtime_hours)`
入库成 overtime_pay 配方（把月薪当时薪算「加班费」= 164000 假数）、
`leave_days * 100` 入库成 leave_compensation（库里没有补偿费率）。
根因：维度闭合判定走 maieutic_gate（只核清单），环境实查清单从来没给过它；
内层 critic 有实查但判据没点名「列×列合成新口径」这个模式。

本测试锁住修复后的管线：
  ① `_env_obs_block` 拼出的块带点名判据（列×列 = 假设业务参数）；
  ② `maieutic_gate.render` 把 env_obs 放进 user 消息；
  ③ `maieutic_gate.check` 把 env_obs 透传给 LLM（stub 抓包验证）；
  ④ `task_critic.check_task_success(dimensions=…, env_obs=…)` 三层打通；
  ⑤ GATE_SYSTEM 里有口径真实性判据（没有就等于没修）。
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import maieutic_gate as G  # noqa: E402
import task_critic as TC  # noqa: E402

FAKE_OBS = ("employees 列: emp_id, name, dept, base_salary, hire_date, active; "
            "attendance 列: emp_id, month, overtime_hours, leave_days")

# 假 curriculum_auto：db_observation 返回实查清单（不碰真库）
_mod = types.ModuleType("curriculum_auto")
_mod.db_observation = lambda p: FAKE_OBS
sys.modules["curriculum_auto"] = _mod

import learn_orchestrate as LO  # noqa: E402

ok_all = True


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok_all
    ok_all = ok_all and bool(cond)
    print(("✓" if cond else "✗"), name, ("-> " + extra if extra else ""))


# ① _env_obs_block：无库返回空；有库返回带点名判据的块
check("① 无 db_path 返回空", LO._env_obs_block("") == "")
blk = LO._env_obs_block("/tmp/x.duckdb")
check("① 有库返回实查块", bool(blk) and FAKE_OBS in blk)
check("① 块里点名列×列判据",
      "两个数值列相乘/相除合成新口径" in blk and "语义造假" in blk)

# ② render：env_obs 进 user 消息
r = G.render("八月份加班费总额？", "164000 元",
             [{"id": "overtime_pay", "question": "八月份加班费总额？"}],
             env_obs=FAKE_OBS)
check("② render 含环境实查块", "环境实查清单" in r and FAKE_OBS in r)
r2 = G.render("题", "答", [{"id": "d", "question": "q"}], env_obs="")
check("② env_obs 为空时不插块", "环境实查清单" not in r2)

# ③ check：env_obs 透传给 LLM（stub 抓包）
seen = {}


def _stub_llm(msgs):
    seen["user"] = msgs[-1]["content"]
    return '{"gaps_addressed": [], "gaps_still_open": ["overtime_pay"], ' \
           '"questions": ["库里没有加班费率"], "passed": false}'


out = G.check("八月份加班费总额？", "164000 元",
              [{"id": "overtime_pay", "question": "八月份加班费总额？"}],
              llm=_stub_llm, env_obs=FAKE_OBS)
check("③ check 把 env_obs 喂给 LLM",
      "环境实查清单" in seen.get("user", "") and FAKE_OBS in seen.get("user", ""))
check("③ 判官判未闭合", out.get("passed") is False
      and "overtime_pay" in (out.get("open") or []))

# ④ task_critic 三层打通（dimensions 分支 → gate → env_obs）
res = {"answer": "164000 元", "execution": {"real_calls": 1, "rows": 1},
       "evidence": [{"ok": True, "source": "scene_db", "text": "164000"}]}
ok, crit = TC.check_task_success(
    "八月份加班费总额？", "",
    res, dimensions=[{"id": "overtime_pay", "question": "八月份加班费总额？"}],
    env_obs=FAKE_OBS, llm=_stub_llm)
check("④ check_task_success 带 env_obs 判未完成",
      ok is False and "overtime_pay" in str(res.get("critic_gate", {}).get("open", [])))

# ⑤ GATE_SYSTEM 判据在
check("⑤ GATE_SYSTEM 含口径真实性判据",
      "口径真实性" in G.GATE_SYSTEM
      and "base_salary × overtime_hours" in G.GATE_SYSTEM)

print("ALL PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
