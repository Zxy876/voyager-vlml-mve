#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""技能库体检：逐条重放，按**材料账**判这条技能现在还取不取得到真内容。

为什么需要（2026-10-22 场景 B 实测）：学到的 5 条配方里 4 条，答案其实是
403 / 521 / DNS 失败页 —— 固化时没人拦（那时还没有材料账），于是"命中即复用"
反而答错。材料账补上之后新技能不会再这么进库，但**历史坏配方要清**，否则
检索照样命中它们。

判定口径与运行时**完全一致**：`local_agent._evidence_item`（不是另写一套关键词）。
  OK    —— 至少一条证据 ok=True（真取到了内容）
  真空  —— 所有证据 ok=False（403 / 超时 / 返回空 / 工具报错）
  报错  —— 技能代码本身抛异常（缺名字、语法、工具崩）

用法：
  python mve/audit_skills.py                     # 体检当前场景，只报告
  python mve/audit_skills.py --scene B --limit 20
  python mve/audit_skills.py --scene B --fix     # 删掉真空/报错的条目（先备份）
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import actions          # noqa: E402
import local_agent      # noqa: E402
import mcp_gateway      # noqa: E402
import scenes           # noqa: E402
import skill_store      # noqa: E402

STORE = Path(__file__).resolve().parent / "skill_store.json"


def _scene_dir(scene_id: str) -> str:
    return str(Path(__file__).resolve().parent / "scene_data" / scene_id)


def verdict_of(evidence: list[dict], exc: str, result: object) -> tuple[str, str]:
    """→ (OK/真空/报错, 原因)。口径与闸门一致，不另立判据。"""
    if exc:
        return "报错", exc[:220]
    ok_items = [e for e in evidence if isinstance(e, dict) and e.get("ok")]
    if ok_items:
        return "OK", f"{len(ok_items)} 条证据（{ok_items[0].get('tool')}）"
    if evidence:
        why = "；".join(str(e.get("error") or "")[:80] for e in evidence[:3])
        return "真空", why or "证据全失败"
    # 一次取数动作都没调，RESULT 却是非空 —— 典型的凭记忆作答
    text = local_agent._flat_text(result)
    return "真空", ("一次都没调取数工具，答案是凭记忆写的"
                    if text.strip() else "没有任何返回")


async def audit(scene_id: str, limit: int) -> list[dict]:
    skills = [s for s in skill_store.by_scene(scene_id)
              if (s.get("code") or "").strip()]
    skills.sort(key=lambda s: s.get("created_at") or s.get("ts") or 0, reverse=True)
    if limit:
        skills = skills[:limit]
    if not skills:
        print(f"场景 {scene_id} 没有带代码的技能")
        return []

    gw = mcp_gateway.get_gateway()
    conn = await gw.connect_all()
    print("MCP 已连接 %d 个：" % len(conn["connected"])
          + "、".join(f"{c['server']}({c['transport']})" for c in conn["connected"]))
    if conn["failed"]:
        print("  连不上：" + "；".join(f"{f['server']}:{str(f['why'])[:60]}" for f in conn["failed"]))
    gw.expose_as_actions(scene_id)
    actions.register_net(scene_id)

    rows = []
    for s in skills:
        ev: list[dict] = []
        exc = ""
        out = None
        t0 = time.time()
        try:
            out = await local_agent.run_saved_async(
                s.get("code") or "", _scene_dir(scene_id),
                timeout=120, scene_id=scene_id, evidence=ev)
        except Exception as e:  # noqa: BLE001
            exc = f"{type(e).__name__}: {e}"
        dt = time.time() - t0
        verdict, why = verdict_of(ev, exc, out)
        rows.append({"key": s.get("key"), "name": s.get("name"), "verdict": verdict,
                     "why": why, "secs": dt, "tools": [e.get("tool") for e in ev]})
        print(f"  [{verdict}] {s.get('key')}  {dt:.1f}s  {why}")
    await gw.disconnect_all()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="技能库体检（按材料账判真空）")
    ap.add_argument("--scene", default="", help="场景 id，缺省取当前场景")
    ap.add_argument("--limit", type=int, default=0, help="最多体检几条（0=全部）")
    ap.add_argument("--fix", action="store_true", help="删除真空/报错的条目（先备份）")
    a = ap.parse_args()

    scene_id = a.scene or str(scenes.current_scene_id() or "A")
    print(f"== 体检场景 {scene_id} ==")
    rows = asyncio.run(audit(scene_id, a.limit))
    if not rows:
        return 0

    bad = [r for r in rows if r["verdict"] != "OK"]
    print(f"\n合计 {len(rows)} 条：OK {len(rows) - len(bad)} / 坏 {len(bad)}")
    if not bad:
        print("全部能取到真内容 ✅")
        return 0
    for r in bad:
        print(f"  ✗ {r['key']}  [{r['verdict']}]  {r['why']}")

    if not a.fix:
        print("\n（只报告。要删除这些坏配方：加 --fix）")
        return 1

    backup = STORE.with_name(f"skill_store.json.bak_{int(time.time())}")
    shutil.copy2(STORE, backup)
    print(f"\n已备份 → {backup}")
    removed = 0
    for r in bad:
        if r["key"] and skill_store.delete_skill(r["key"]):
            removed += 1
            print(f"  删除 {r['key']}")
    print(f"删除 {removed}/{len(bad)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
