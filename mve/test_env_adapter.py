#!/usr/bin/env python3
"""env_adapter（天授适配器）测试：管道正确性 + 容错口径两侧 + 安全约束。

跑法（本地，无 pytest）：
    /Users/zxydediannao/.workbuddy/binaries/python/envs/default/bin/python \\
        mve/test_env_adapter.py
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from env_adapter import CodeActionEnv, CodeActionError, mechanism_ok

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" —— {detail}" if detail else ""))


HEURISTIC = "action = 1 if obs[2] + obs[3] > 0 else 0"


def main() -> int:
    print("== 1. reset / step 基本管道 ==")
    env = CodeActionEnv("CartPole-v1")
    obs_text, _ = env.reset(seed=0)
    check("reset 返回 obs 文本", "obs = [" in obs_text and "角速度" in obs_text, obs_text[:60])
    out = env.step(HEURISTIC)
    check("合法代码 → 无 error 且有 reward", "error" not in out and out.get("reward") == 1.0,
          str({k: out.get(k) for k in ("reward", "action", "steps")}))
    check("透传 terminated/truncated 布尔",
          isinstance(out.get("terminated"), bool) and isinstance(out.get("truncated"), bool))

    print("== 2. 安全约束 ==")
    r1 = env.step("import os")
    check("import 词面被拦", "error" in r1 and "禁用词面" in r1["error"], r1.get("error", ""))
    r2 = env.step("open('/etc/passwd')")
    check("open( 被拦", "error" in r2 and "禁用词面" in r2["error"], r2.get("error", ""))
    r3 = env.step("action = 1\n" * 200)
    check("超长代码被拦", "error" in r3 and "超长" in r3["error"], r3.get("error", ""))
    r4 = env.step("action = 'x'")
    check("action 非数值被拦", "error" in r4 and "不是数值" in r4["error"], r4.get("error", ""))
    r5 = env.step("raise SystemExit(0)")
    check("代码内异常 → 返回 error 不炸", "error" in r5, r5.get("error", ""))
    r6 = env.step("action = 99")
    check("越界 action 夹进合法空间", "error" not in r6 and r6.get("action") == 1,
          f"action={r6.get('action')}")
    r7 = env.step("action = True")   # bool 是 int 子类，必须显式拦
    check("bool action 被拦", "error" in r7 and "不是数值" in r7["error"], r7.get("error", ""))

    print("== 3. mock 启发式在真 env 上的 return（管道有效性）==")
    returns = []
    for seed in (1, 2, 3):
        ep = env.run_episode(HEURISTIC, seed=seed)
        returns.append(ep["return"])
        check(f"seed={seed} 启发式 return>100（管道无缺陷）", ep["return"] > 100,
              f"return={ep['return']:.0f} steps={ep['steps']} "
              f"terminated={ep['terminated']} truncated={ep['truncated']}")
    mean3 = statistics.mean(returns)
    check("启发式均值远超随机基线 20", mean3 > 100, f"均值={mean3:.1f}")

    print("== 4. 坏代码经 run_episode 如实报错（反馈可回喂）==")
    ep = env.run_episode("import os", seed=1)
    check("run_episode 返回 error 字段", bool(ep.get("error")), ep.get("error", ""))

    print("== 5. 容错口径 mechanism_ok（两侧都测）==")
    ok, why = mechanism_ok([20.0] * 5 + [26.0] * 5)
    check("口径：后半/前半 ≥1.2 → 正常", ok, why)
    ok2, why2 = mechanism_ok([200.0] * 10)
    check("口径：起点即高水位 → 正常（但注明无趋势）",
          ok2 and "无趋势" in why2, why2)
    ok2b, why2b = mechanism_ok([80.0] * 10)
    check("口径：原地低水位（<100 且无趋势）→ 不正常", not ok2b, why2b)
    ok4, why4 = mechanism_ok([50.0] * 5 + [30.0] * 5)
    check("口径：越学越差 → 不正常", not ok4, why4)
    ok5, why5 = mechanism_ok([20.0, 21.0, 22.0])
    check("口径：轮次不足 → 不判定", not ok5, why5)

    print("== 6. 逐步轨迹（collect=True，喂给下一轮反馈）==")
    ep2 = env.run_episode(HEURISTIC, seed=7, collect=True)
    tr = ep2.get("trace") or []
    check("trace 含逐步 action/reward/obs", bool(tr) and "action" in tr[0] and "obs_text" in tr[0],
          f"trace={len(tr)} 步")

    env.close()
    print(f"\nPASS {len(PASS)} / FAIL {len(FAIL)}")
    for f in FAIL:
        print(f"  FAIL: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
