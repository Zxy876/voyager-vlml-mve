#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""泛化验收测试（G-Test）——「1→n 泛化成功与否」用真实运行回答，不靠感觉。

在服务器上跑（真实 LLM + 真实 MCP + 真实技能库）：

    MVE_PANEL_URL=... python mve/generalization_test.py
    # 或直接：python mve/generalization_test.py   （consume 走进程内调用）

五个命题（每个都打印证据，任何一个 FAIL → 退出码 1）：

  G1 同场景跨问法复用   ：Scene I 学过的题，换一种问法再问 → 复用已学配方
                          （used_skills 非空）+ 真取到数（rows>0）+ 掌握 +1。
  G2 跨场景隔离（负命题）：同样的数学问题拿到 Scene G 问 → **不许**复用
                          Scene I 的场景专属技能（source_scene=I 被过滤）——
                          隔离是设计，泛化不许靠"串味"。但同时 G 自己仍要
                          能答（现场写，取到数）。
  G3 跨场景迁移（正命题）：把一条 Scene I 技能显式标成 source_scene=default
                          （技能是跨场景资产的唯一通道）→ 同样的问题在
                          Scene G 问 → 命中它并取到数 → 跑完恢复标记。
  G4 双循环 MCP 连通   ：一个进程里两个 asyncio.run 循环都调通 MCP
                          （fc41f7b 回归：面板每次导入都是新循环）。
  G5 隔离回归           ：test_scene_isolation 仍全绿（场景切换不串数据）。

判定口径（诚实）：
  - G1/G3 只认 coach_log 里的真实记录（ok_tool_calls>0 且 used_skills 命中），
    不看 CLI 打印；
  - G2 是**负命题**：它的 PASS 证明隔离没被破坏；
  - 全过 → 「1→n 泛化（同场景跨问法 + 显式跨场景迁移 + 隔离不破）」成立。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

COACH_LOG = HERE / "coach_log.jsonl"

RESULTS: list[tuple[str, bool, str]] = []


def _record(name: str, ok: bool, evidence: str) -> None:
    RESULTS.append((name, ok, evidence))
    print(f"{'✅ PASS' if ok else '❌ FAIL'}  {name}\n        {evidence}\n")


def _last_coach(scene_id: str) -> dict:
    """coach_log 里该场景最后一条导入记录（账目是唯一可信源）。"""
    rec = {}
    if COACH_LOG.exists():
        for line in COACH_LOG.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            if str(d.get("root_scene") or "A") == scene_id and d.get("mode") == "voyager_consume":
                rec = d
    return rec


def _consume(question: str, scene_id: str) -> dict:
    """走面板同一条消费链路（进程内 asyncio.run，等价于 CLI/面板）。"""
    os.environ["MVE_SCENE"] = scene_id
    from voyager_vlml.consume import consume
    return asyncio.run(consume(question))


def _skill_scene(key: str) -> str:
    import skill_store
    for s in skill_store.all_skills():
        if str(s.get("key") or "") == key:
            return str(s.get("source_scene") or "default")
    return "?"


def g1_same_scene_paraphrase() -> None:
    """G1：Scene I 学过「一次函数交点」，换问法再问 → 必须复用且取到数。"""
    q = "一次函数y=kx+6的图像与y轴的交点坐标应该怎么确定？请说明方法。"
    r = _consume(q, "I")
    rec = _last_coach("I")
    used = rec.get("used_skills") or []
    scenes = {_skill_scene(str(u.get("key") or "")) for u in used}
    got_data = (int(rec.get("ok_tool_calls") or 0) > 0
                or int(rec.get("rows") or 0) > 0)
    mastered = bool(rec.get("mastered"))
    ok = bool(used) and "I" in scenes and got_data and mastered
    _record("G1 同场景跨问法复用", ok,
            f"used_skills={[u.get('name') for u in used]}（场景 {scenes}）"
            f" ok_tool_calls={rec.get('ok_tool_calls')} rows={rec.get('rows')}"
            f" judgment={rec.get('judgment_result')} mastered={mastered}"
            f" 归题={rec.get('topic_id')}")


def g2_cross_scene_isolation() -> None:
    """G2（负命题）：同样的数学题在 Scene G 问 → 不许用 I 的场景技能。"""
    q = "一次函数y=kx+6的图像与y轴的交点坐标怎么确定？"
    _consume(q, "G")
    rec = _last_coach("G")
    used = rec.get("used_skills") or []
    leaked = [u.get("key") for u in used
              if _skill_scene(str(u.get("key") or "")) == "I"]
    got_data = (int(rec.get("ok_tool_calls") or 0) > 0
                or int(rec.get("rows") or 0) > 0)
    ok = (not leaked) and got_data
    _record("G2 跨场景隔离（I 的技能不串到 G）", ok,
            f"泄漏的 I 技能={leaked or '无'}；G 自己取到数={got_data}"
            f"（ok_tool_calls={rec.get('ok_tool_calls')} rows={rec.get('rows')}，"
            f"现场写也算过）")


def g3_cross_scene_migration() -> None:
    """G3（正命题）：一条 I 技能显式标 default → 在 G 命中并取到数 → 恢复。"""
    import skill_store
    target_key = "consume_420617ca::dim2"     # y 轴交点配方
    data = skill_store._load_raw()
    skill = data["skills"].get(target_key)
    if not skill:
        _record("G3 跨场景迁移（default 通道）", False, f"找不到技能 {target_key}")
        return
    old_scene = str(skill.get("source_scene") or "default")
    try:
        skill["source_scene"] = "default"
        skill_store._save(data)               # _save 会同步解析缓存

        q = "一次函数y=kx+6的图像与y轴的交点坐标怎么确定？"
        _consume(q, "G")
        rec = _last_coach("G")
        used = rec.get("used_skills") or []
        used_keys = [str(u.get("key") or "") for u in used]
        got_data = (int(rec.get("ok_tool_calls") or 0) > 0
                    or int(rec.get("rows") or 0) > 0)
        ok = target_key in used_keys and got_data
        _record("G3 跨场景迁移（default 通道）", ok,
                f"标 default 后在 G 命中={target_key in used_keys}"
                f" used={used_keys} 取到数={got_data}"
                f"（ok_tool_calls={rec.get('ok_tool_calls')} rows={rec.get('rows')}）")
    finally:
        # 恢复场景标记，不留测试痕迹
        data = skill_store._load_raw()
        s = data["skills"].get(target_key)
        if s:
            s["source_scene"] = old_scene
            skill_store._save(data)


def g4_two_loops_mcp() -> None:
    """G4：一个进程两个 asyncio.run 循环都调通 MCP（fc41f7b 回归）。"""
    import local_agent as la

    async def _call() -> dict:
        await la.ensure_mcp_tools("I")
        import mcp_gateway
        gw = mcp_gateway.get_gateway()
        return await gw.call_remote("mcp_time_get_current_time",
                                    timezone="Asia/Shanghai")

    r1 = asyncio.run(_call())
    r2 = asyncio.run(_call())          # 第二个循环：旧连接绑死循环 → 必须重连
    ok = "error" not in (r1 or {}) and "error" not in (r2 or {})
    _record("G4 双循环 MCP 连通（fc41f7b 回归）", ok,
            f"第 1 循环 error={bool((r1 or {}).get('error'))}，"
            f"第 2 循环 error={(r2 or {}).get('error') or '无'}")


def g5_isolation_regression() -> None:
    """G5：场景隔离回归测试仍全绿（子进程跑，不污染本进程状态）。"""
    p = subprocess.run(
        [sys.executable, str(HERE / "test_scene_isolation.py")],
        capture_output=True, text=True, timeout=180,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    ok = p.returncode == 0 and "❌" not in p.stdout
    tail = (p.stdout or p.stderr).strip().splitlines()
    _record("G5 场景隔离回归（test_scene_isolation）", ok,
            tail[-1] if tail else f"returncode={p.returncode}")


def main() -> int:
    print("=" * 72)
    print("泛化验收测试（G-Test）· 真实 LLM + 真实 MCP · 账目只认 coach_log")
    print("=" * 72)
    for fn in (g4_two_loops_mcp,          # 先跑不花 LLM 的
               g1_same_scene_paraphrase,
               g2_cross_scene_isolation,
               g3_cross_scene_migration,
               g5_isolation_regression):
        try:
            fn()
        except Exception as e:
            _record(fn.__name__, False, f"{type(e).__name__}: {e}")
    print("=" * 72)
    n_pass = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, _ in RESULTS:
        print(f"  {'✅' if ok else '❌'} {name}")
    print(f"===== G-Test：{n_pass}/{len(RESULTS)} 过 =====")
    return 0 if n_pass == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
