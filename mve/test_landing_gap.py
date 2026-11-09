#!/usr/bin/env python3
"""「骨架落点够不着」这条链路（第二刀补洞，2026-11-09）的测试。

跑法：python mve/test_landing_gap.py（服务器无 pytest，同 test_*.py 口径）。

这一层守什么
------------
第二刀把「维度落点」固化成了骨架里的 `landing` 字段，消费侧只读它 →
路由不再猜。但**现场唯一存在的坏骨架是第二刀之前生成的**，它**没有**
`landing` 字段，于是：

    · `resplit_needed` 对它恒返回 False → 人看不到「该重拆」的信号
      → 坏骨架永远留在缓存里；
    · 消费侧旧骨架路径两条判据都不命中 → 整题静默落进 `infer`
      = 什么都不查，**且没有任何交代**（人只看到「答不出来」，
      不知道是骨架指向了一条走不通的路）。

本测试钉死：
    ① `unreachable_in_text` 只认「**真存在但不可达**」的表（账本表，
       实查得来）—— 不能把「场景里根本没有的表名」误判成不可达落点；
    ② 旧骨架（无 `landing`）点名了不可达表 → `resplit_needed` = True；
    ③ 旧骨架点名了**可达**表 → `resplit_needed` = False（不误伤）；
    ④ 消费侧旧骨架路径 → 落进 `unreachable_landings`（**如实交代**），
       而不是静默 infer；
    ⑤ 判据**无关键词表**：只靠实查到的表名做字面包含。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


def _pick_scene_with_ledger() -> tuple[str, list[str]] | None:
    """找一个**真的有账本库**的场景（账本表 = 不可达表的样本）。"""
    import reachable as R
    for sid in ("L", "A", "B", "E", "H"):
        try:
            led = R.ledger_tables(sid) or []
        except Exception:
            continue
        if led:
            return sid, led
    return None


def main() -> int:
    import reachable as R
    import voyager_vlml.consume as C

    found = _pick_scene_with_ledger()
    if not found:
        print("  ⚠️ 本机没有带账本库的场景 → 跳过需要真库的断言")
        print(f"\n== 结果：{PASS} 过 / {FAIL} 挂 ==")
        return 1 if FAIL else 0

    sid, led = found
    t_led = led[0]
    reach = R.reachable_tables(sid) or []
    t_reach = reach[0] if reach else ""
    print(f"== 场景 {sid}：账本表 {led} / 可达表 {reach} ==")

    print("== 1. unreachable_in_text：只认「真存在但不可达」的表 ==")
    check(f"点名账本表 {t_led} → 命中",
          R.unreachable_in_text(sid, f"在 {t_led} 表中，item 为 'ce' 的记录")
          == [t_led])
    if t_reach:
        check(f"点名可达表 {t_reach} → 不命中（那张查得到）",
              R.unreachable_in_text(sid, f"在 {t_reach} 表中查") == [])
    check("空文本 → 不命中", R.unreachable_in_text(sid, "") == [])
    check("无关文本 → 不命中",
          R.unreachable_in_text(sid, "帮我查一下这个人注册了哪些平台") == [])
    check("**不存在的表名** → 不命中（不误判成不可达落点）",
          R.unreachable_in_text(sid, "在 definitely_not_a_table_zzz 表中") == [])

    print("== 2. 旧骨架（无 landing）：点名不可达表 → 该重拆 ==")
    import maieutic_scaffold as ms
    tmp = tempfile.mkdtemp(prefix="mve_landing_")
    ck = Path(ms.CKPT) / "_test_landing_scene"
    ck.mkdir(parents=True, exist_ok=True)
    q = "测试用的问句（落点够不着）"
    try:
        # 造一份「旧骨架」：**故意不带 landing 字段**
        dims_bad = [{"id": "d1",
                     "question": f"在 {t_led} 表中，item 为 'ce' 的记录有哪些？",
                     "why": "查这个"}]
        dims_ok = [{"id": "d1",
                    "question": f"在 {t_reach} 表中，item 为 'ce' 的记录有哪些？",
                    "why": "查这个"}] if t_reach else []
        # 用真实的 _cache_path 算键（保证 resplit_needed 能找到同一份文件）
        p = ms._cache_path(ck.name, q)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"question": q, "dimensions": dims_bad},
                                ensure_ascii=False), encoding="utf-8")
        need = ms.resplit_needed(ck.name, q)
        check("旧骨架点名不可达表 → resplit_needed = True", need is True)

        # 换一份：点名可达表 → 不该判要重拆
        p.write_text(json.dumps({"question": q, "dimensions": dims_ok},
                                ensure_ascii=False), encoding="utf-8")
        need2 = ms.resplit_needed(ck.name, q)
        check("旧骨架点名可达表 → resplit_needed = False（不误伤）",
              need2 is False)

        # 空维度 → 不判
        p.write_text(json.dumps({"question": q, "dimensions": []},
                                ensure_ascii=False), encoding="utf-8")
        check("空维度 → resplit_needed = False",
              ms.resplit_needed(ck.name, q) is False)
    finally:
        try:
            p.unlink()
            ck.rmdir()
        except Exception:
            pass

    print("== 3. 消费侧旧骨架路径：落进 unreachable_landings（如实交代）==")
    # 造一份只有一条「点名不可达表」维度的骨架，让消费侧路由读它
    import maieutic_scaffold as ms2
    tmpdir = tempfile.mkdtemp(prefix="mve_landing2_")
    q2 = "测试用的问句二（消费侧路由）"
    p2 = ms2._cache_path(ck.name, q2)
    p2.parent.mkdir(parents=True, exist_ok=True)
    try:
        p2.write_text(json.dumps(
            {"question": q2,
             "dimensions": [{"id": "d1",
                             "question": f"在 {t_led} 表中查 item='ce' 的记录",
                             "why": ""}]},
            ensure_ascii=False), encoding="utf-8")
        plan = C._plan_from_scaffold(q2, scene_id=ck.name, has_file=False)
        unreach = list(plan.get("unreachable_landings") or [])
        check("旧骨架点名不可达表 → 记进 unreachable_landings",
              len(unreach) >= 1, str(unreach)[:80])
        check("这条落点确实被点名了", t_led in str(unreach), str(unreach)[:80])
        check("不误判成 db 路（那张表 SQL 连不到）",
              plan.get("db") is False, f"db={plan.get('db')}")
    finally:
        try:
            p2.unlink()
            ck.rmdir()
        except Exception:
            pass

    print("== 4. 判据无关键词表（只靠实查表名做字面包含）==")
    src = (Path(__file__).resolve().parent / "reachable.py").read_text(
        encoding="utf-8")
    check("reachable.py 用 ledger_tables 实查 + 字面包含",
          "ledger_tables(scene_id)" in src and "t in blob" in src)
    check("没写死任何具体表名（web_evidence/web_facts 不该出现）",
          "web_evidence" not in src and "web_facts" not in src)
    csrc = (Path(__file__).resolve().parent.parent / "voyager_vlml" /
            "consume.py").read_text(encoding="utf-8")
    check("消费侧用的是同一份实现（import reachable）",
          "import reachable as _reach" in csrc
          and "_reach.unreachable_in_text(" in csrc)

    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
