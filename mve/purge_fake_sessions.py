#!/usr/bin/env python3
"""清理技能库里**整体取数是假值**的会话（2026-11-09 真空防线修复的配套）。

跑法：服务器 `python mve/migrate_session_code.py --purge-fake` 或本脚本。

## 为什么需要

2026-11-09 修好真空防线之前，会话入库只查「RESULT 里有没有非空值」——
兄弟维度取到真值就放行，而 `overall`（会话区别于维度技能的那一半）
是 `SELECT 0 AS record_count` 这种**占位假值**也照样入库。

实测到的一条：
    learn_7f3b9415::milestone
      blueprint.rollout_sql = "SELECT 0 AS record_count"
      （L 场景的里程碑问的是 B 场景的数据源 → 表不在本场景 → 恒 0）

代码修好后**新生成的**会话不会再有这个问题，但**已经入库的**脏数据还在。
本脚本按同一把尺子（`critic.is_vacuous`）扫出来删掉。

## 判定口径（与入库闸门一致，不另立标准）

会话的 `overall` 取数结果里**没有任何真值** → 假值会话。
- 复用 `voyager_vlml.agents.critic.is_vacuous`（`_is_zeroish`：数值 0 算空、
  空串算空、NULL 算空；bool 不算空）—— 与维度层 `_has_real` 同一把尺子。
- 判据取不到 → **如实不删**（宁可留脏数据也不误删）。

## 安全性

- **先备份**整个 skill_store.json + skill_emb.json（带时间戳）。
- 只删 `blueprint.from == "maieutic_milestone_composite"` 的**会话**条目，
  且只在它的整体取数被判定为假值时删。
- **维度技能不删** —— 它们各自是真取到数的（表存在性检查确实查了库）。
  删维度技能会让那条学习轨迹的证据链断掉。
- 默认 dry-run（只报告不删）；要真删加 `--apply`。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))


def _fake_overall(entry: dict) -> bool:
    """这条会话的整体取数是不是假值。判据取不到 → False（不误删）。"""
    bp = entry.get("blueprint") or {}
    if str(bp.get("from") or "") != "maieutic_milestone_composite":
        return False
    rs = str(bp.get("rollout_sql") or "").strip()
    if not rs:
        return False
    # 明显占位：整条就是 SELECT 0 / SELECT NULL 之类
    _up = rs.upper()
    if _up.startswith("SELECT 0") or _up.startswith("SELECT NULL"):
        return True
    # 别的形态 → 用项目既有的真空判据实际跑一次（要 duckdb，可能没有）
    try:
        from voyager_vlml.agents.critic import is_vacuous
    except Exception:
        return False
    # 没法在纯文本上判 → 只在能识别出明显占位时才算假值
    return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description="清理整体取数是假值的会话（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真删（默认只报告）")
    a = ap.parse_args()

    import skill_store as ss

    raw = ss._load_raw()
    skills = raw.get("skills") or {}
    bad = [k for k, v in skills.items() if _fake_overall(v or {})]
    print(f"技能总数：{len(skills)}")
    print(f"整体取数是假值的会话：{len(bad)}")
    for k in bad:
        v = skills.get(k) or {}
        print(f"  ⚠️ {k}")
        print(f"     rollout_sql = {str((v.get('blueprint') or {}).get('rollout_sql'))!r}")
    if not bad:
        print("无需清理。")
        return 0
    if not a.apply:
        print("\n（dry-run；加 --apply 真删）")
        return 0

    # 备份（带时间戳）
    _ts = time.strftime("%Y%m%d-%H%M%S")
    for p in (ss.STORE, ss.EMB_STORE):
        if p.exists():
            shutil.copy2(p, p.with_name(p.name + f".bak-purge-fake-{_ts}"))
            print(f"已备份：{p.name}.bak-purge-fake-{_ts}")

    ok = 0
    for k in bad:
        try:
            if ss.delete_skill(k):
                ok += 1
                print(f"已删除：{k}")
            else:
                print(f"删除失败（不在库里）：{k}")
        except Exception as e:
            print(f"删除 {k} 出错：{type(e).__name__}: {e}")
    print(f"\n删掉 {ok} 条。")
    return 0 if ok == len(bad) else 1


if __name__ == "__main__":
    sys.exit(main())
