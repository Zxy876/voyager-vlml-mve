#!/usr/bin/env python3
"""reward（标量 r）测试：compute_r 的构成、inference 上限、scene_has_env。

跑法（本地，无 pytest）：
    /Users/zxydediannao/.workbuddy/binaries/python/envs/default/bin/python \\
        mve/test_reward.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reward import compute_r, scene_has_env

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" —— {detail}" if detail else ""))


def _res(real=1, rows=3, sql="SELECT 1", ev_ok=True, obs=None) -> dict:
    d: dict = {"execution": {"real_calls": real, "rows": rows, "sql": sql}}
    if obs is not None:
        d["observations"] = obs
    else:
        d["evidence"] = [{"ok": ev_ok}]
    return d


def main() -> int:
    print("== 1. compute_r：有环境（rule 主导）==")
    check("真取数+材料+sql+判官 = 1.0",
          compute_r(_res(), True, has_env=True) == 1.0,
          str(compute_r(_res(), True, has_env=True)))
    check("真取数+材料+sql、判官否 = 0.9",
          compute_r(_res(), False, has_env=True) == 0.9,
          str(compute_r(_res(), False, has_env=True)))
    check("没取到数（真空）= 0.1（真空否决全部 rule 信号，只剩判官兜底）",
          compute_r(_res(real=0, rows=0), True, has_env=True) == 0.1,
          str(compute_r(_res(real=0, rows=0), True, has_env=True)))
    check("取到数但证据全挂 = 0.8（0.6 取数 + 0.1 sql + 0.1 判官）",
          compute_r(_res(ev_ok=False), True, has_env=True) == 0.8,
          str(compute_r(_res(ev_ok=True, obs=[{"ok": False}]), True, has_env=True)))
    check("空表聚合（real=1 rows=0）不算取到数 → 0.1",
          compute_r(_res(real=1, rows=0), True, has_env=True) == 0.1,
          str(compute_r(_res(real=1, rows=0), True, has_env=True)))
    check("无 sql 可复现载体 = 0.9（0.6 取数 + 0.2 材料 + 0.1 判官）",
          compute_r(_res(sql=""), True, has_env=True) == 0.9)

    print("== 2. compute_r：无环境（inference 上限）==")
    check("无环境 + 判官放行 = 0.1（上限）",
          compute_r(_res(), True, has_env=False) == 0.1)
    check("无环境 + 判官否 = 0.0",
          compute_r(_res(), False, has_env=False) == 0.0)
    check("无环境时 rule 信号再多也不加分（真取数也只 0.1）",
          compute_r(_res(real=5, rows=9), True, has_env=False) == 0.1)

    print("== 3. scene_has_env（分型权威 = 环境 kind）==")
    # scene_profile 真实行为（2026-10-28 实测）：未知场景回落 kind=local
    # （本机即数据源，用户 2026-10-19 口径）；空场景回落默认场景 A（vlml）。
    # 两者都算「有环境」——不窄化成 duckdb。
    check("未知场景 → local（本机即环境）→ True",
          scene_has_env("__nope__") is True, str(scene_has_env("__nope__")))
    check("空场景 → 默认场景（vlml）→ True",
          scene_has_env("") is True, str(scene_has_env("")))

    print("== 4. skill_store：r 入库 + 择优覆盖 + r 进向量索引 ==")
    import tempfile
    import skill_store as SS
    with tempfile.TemporaryDirectory() as td:
        from pathlib import Path as _P
        SS.STORE = _P(td) / "skill_store.json"
        SS.EMB = _P(td) / "embs.json" if hasattr(SS, "EMB") else None
        # 若模块用别的向量缓存路径变量，测试不依赖向量模式（无 embedding API）
        a1, k1 = SS.add("T1", "d1", "这是一条足够长的技能描述文本用于入库",
                        code="SELECT 1", exact_only=True, r=0.9)
        check("带 r 入库 added", a1 == "added" and k1,
              f"{a1} {k1}")
        raw = SS._load_raw()
        check("r 存进技能条目", float(raw["skills"][k1].get("r", -1)) == 0.9)
        # 同维度重写：r 更低 → 拒绝新 code（择优）
        a2, k2 = SS.add("T1", "d1", "这是一条足够长的技能描述文本用于入库改",
                        code="SELECT 2 -- 改坏的", exact_only=True, r=0.3)
        raw = SS._load_raw()
        check("r 回退被拦：code 保留旧版",
              raw["skills"][k2].get("code") == "SELECT 1", a2)
        # r 更高 → 接受新 code 且 r 升
        a3, k3 = SS.add("T1", "d1", "这是一条足够长的技能描述文本用于入库改好",
                        code="SELECT 3 -- 更优", exact_only=True, r=0.95)
        raw = SS._load_raw()
        check("r 更高：code 更新且 r 只升不降",
              raw["skills"][k3].get("code") == "SELECT 3 -- 更优"
              and float(raw["skills"][k3].get("r")) == 0.95, a3)

    print(f"\nPASS {len(PASS)} / FAIL {len(FAIL)}")
    for f in FAIL:
        print(f"  FAIL: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
