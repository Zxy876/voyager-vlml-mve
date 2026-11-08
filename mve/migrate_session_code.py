#!/usr/bin/env python3
"""把库里「裸 SQL 形态」的会话技能，重建成可执行的组合程序（2026-11-09）。

背景：会话（= 组合了多条技能的「过程」）的形态从「孤立 SQL」改成
「兄弟调用 + RESULT 汇总」的组合程序之后，库里**已有的**会话还是老形态
—— 不重建，它们一调就炸（`SyntaxError: Invalid star expression`：
`SELECT COUNT(*)` 的 `*` 被当 Python 星号表达式），消费侧照样消费不到。

老会话不是没有价值 —— 它们记着「这个目标是哪些方法攒出来的」
（`blueprint.dims`）+「整体怎么算」（`blueprint.rollout_sql`），
这两样都还在，缺的只是**形态**。本脚本用它们把形态补上。

跑法（服务器有 duckdb 与 LLM；本地无 duckdb，数据校验跑不了）：
    python mve/migrate_session_code.py --dry-run     # 只看会改哪些
    python mve/migrate_session_code.py               # 真改（先自动备份）

安全：
    · 改之前自动备份 skill_store.json（带时间戳）；
    · 每条会话**真执行**验过才写回（兄弟调不通 / RESULT 空 → 跳过不动）；
    · 全部处理完**一次性**原子写回（tmp + os.replace）。
"""

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _is_old_form(code: str) -> bool:
    """老形态 = 裸 SQL（不是可执行程序）。"""
    c = str(code or "").strip()
    return c.lower().startswith(("select", "with"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--scene", default="", help="只处理这个场景（空=全部）")
    args = ap.parse_args()

    import skill_store
    from learn_orchestrate import _session_program
    import local_agent as _la
    import scene_profile

    data = skill_store._load_raw()
    skills = data.get("skills") or {}
    sessions = [(k, v) for k, v in skills.items()
                if str(((v or {}).get("blueprint") or {}).get("from") or "")
                == "maieutic_milestone_composite"]
    print(f"会话技能共 {len(sessions)} 条")
    todo = [(k, v) for k, v in sessions if _is_old_form((v or {}).get("code"))]
    print(f"其中老形态（裸 SQL）{len(todo)} 条")
    if args.scene:
        todo = [(k, v) for k, v in todo
                if str(((v or {}).get("blueprint") or {}).get("scene") or "")
                == args.scene]
        print(f"限定场景 {args.scene} → 待处理 {len(todo)} 条")
    if not todo:
        print("没有要改的")
        return 0

    # 组成维度：同 topic 下按名字找（维度 id 不是全局唯一的 ——
    # 同名维度散在多个 topic 下，跨 topic 拿 = 组合出错的数）
    by_topic: dict[str, dict[str, dict]] = {}
    for k, v in skills.items():
        by_topic.setdefault(str((v or {}).get("topic") or ""), {})[
            str((v or {}).get("name") or "")] = v

    changed: list[tuple[str, str]] = []
    for key, entry in todo:
        bp = dict((entry or {}).get("blueprint") or {})
        topic = str((entry or {}).get("topic") or "")
        scene_id = str(bp.get("scene") or "")
        sql_raw = str(bp.get("rollout_sql") or "").strip() \
            or str((entry or {}).get("code") or "").strip()
        prof = {}
        try:
            prof = scene_profile.profile(scene_id) if scene_id else {}
        except Exception:
            prof = {}
        db_path = str(prof.get("db_path") or "")
        scene_dir = str(prof.get("scene_dir") or "")
        # 组成维度（只取同 topic 下真存在的）
        pool = by_topic.get(topic) or {}
        calls: list[tuple[str, str]] = []
        dim_codes: list[tuple[str, str]] = []
        for i, d in enumerate([str(x) for x in (bp.get("dims") or [])][:8], 1):
            dn = d.split("::")[-1] if "::" in d else d
            e = pool.get(dn)
            if not e or not str(e.get("code") or "").strip():
                continue
            fn = _la._skill_call_name(dn)
            if not fn:
                continue
            calls.append(("d_%d" % i, fn))
            dim_codes.append((dn, str(e.get("code") or "")))
        if not sql_raw:
            print(f"  ⏭ {key}：没有可用 SQL，跳过")
            continue
        prog_raw = _session_program(calls, sql_raw)
        # 真执行验（兄弟调得到 + RESULT 有真值）
        ok = False
        err = ""
        try:
            ns = _la._ns_base(scene_dir, "", db_path=db_path)
            for dn, dcode in dim_codes:
                fn = _la._make_skill_fn(
                    {"code": dcode, "name": dn, "text": ""},
                    scene_dir, scene_id, None, db_path=db_path)
                if fn.__name__:
                    ns[fn.__name__] = fn
            box = _la._run_restricted(prog_raw, ns, timeout=45.0)
            res = ns.get("RESULT")
            vals: list = []
            if isinstance(res, dict):
                for v in res.values():
                    vals.extend(list(v) if isinstance(v, (list, tuple)) else [v])
            elif res is not None:
                vals.append(res)
            ok = bool(box.get("ok")) and any(
                v is not None and v != "" and v != [] and v != {}
                for v in vals)
            if not ok:
                err = str(box.get("error") or "RESULT 空")[:140]
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:140]
        if not ok:
            print(f"  ⏭ {key}：重建后没跑出真值（{err}）—— 不动")
            continue
        # 入库形态用泛化后的 SQL（技能存算法不存数值）
        try:
            from voyager_vlml.consume import generalize_sql_recipe as _gen
            sql_tpl = _gen(sql_raw, str(bp.get("question") or ""))
        except Exception:
            sql_tpl = sql_raw
        prog_tpl = _session_program(calls, sql_tpl)
        entry["code"] = prog_tpl
        bp["dims"] = [f"{topic}::{dn}" for dn, _ in dim_codes]
        bp["session_code"] = prog_tpl
        entry["blueprint"] = bp
        changed.append((key, f"{len(calls)} 条组成"))
        print(f"  ✅ {key}：重建为组合程序（{len(calls)} 条组成）")

    if args.dry_run:
        print(f"\n[dry-run] 会改 {len(changed)} 条，未落盘")
        return 0
    if not changed:
        print("\n没有可改的会话")
        return 0
    # 备份 + 原子写回
    sp = skill_store.STORE
    bak = sp.with_suffix(".json.bak-migrate-%s" % time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(sp, bak)
    print(f"\n已备份：{bak}")
    tmp = sp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, sp)
    # 让解析缓存失效（否则进程内还拿着旧副本）
    try:
        skill_store._RAW_CACHE.clear()
    except Exception:
        pass
    print(f"已写回 {sp}（改了 {len(changed)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
