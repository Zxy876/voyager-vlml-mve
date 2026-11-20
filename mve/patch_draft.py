#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-11-18 执行账排除 Phase1 spec_draft 草稿。

对照原助产士确认：Phase1 = 纯承诺（原版 spec/route.ts 只收 specText，
无执行）。但 MVE 的执行账汇总（learn_orchestrate.py 6364-6382）把
Phase1 每轮的 spec_draft 观察行（rows=1）也数进去：
  · calls/rows/real_calls 被草稿灌水 —— 写一轮 spec = "取到 1 行"；
  · 6334-6338 若环境实查清单存在，草稿行还被绑 data_sources → 算成真取到；
  · 下游 learn_trace.fetch_ok / 概览真取到率 / 技能 ok 全被污染
    （open_quiz 两轮 trace 显示"真取到"，实为一行数没取）。

修法：执行账四个汇总（calls/real_calls/rows/data_sources/evidence）
排除 tool=="spec_draft"；草稿只留在 observations / answer（过程记录、
知识型任务的答案来源）。**闸门 real 的口径不动** —— whole-mode
「推理算作答」场景（数学题等）的 no_data 闸门依赖草稿成立。
"""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

OLD = '''            out["execution"] = {
                "ok": real, "sql": sql,
                "calls": max(len(observations or []), _mat_n),
                # 「真取到」只数**真的有数据源支撑**的观察：知识型答案（RESULT 是一段
                # 回答、没有任何数据源调用）不该记成"取到数"——否则日志会自欺
                # （2026-10-19 实测：养育问题打出了"真取到=1"）。
                "real_calls": max(
                    sum(1 for o in (observations or [])
                        if _has_real([o]) and (o.get("data_sources") or [])),
                    _mat_n, _course_real),
                "rows": max(
                    sum(int(o.get("rows") or 0)
                        for o in (observations or []) if o.get("ok")),
                    _mat_n, _p2r),
                "data_sources": sorted({s for o in (observations or [])
                                        for s in (o.get("data_sources") or [])}),
                "evidence": ("data" if any(o.get("data_sources")
                                           for o in (observations or []))
                             else "knowledge"),
            }'''

NEW = '''            # 2026-11-18：Phase1 的 spec_draft 草稿**不是执行**（原版
            # maieutic：Phase1 无执行、编辑器 Phase2 才解锁）。此前草稿被
            # 算进 calls/rows/real_calls —— 写一轮 spec 就"取到 1 行"，
            # 还可能被上面 _env_src 绑上 data_sources 直接算成真取到
            # （open_quiz 两轮 trace"真取到✅"实为一行数没取）。执行账
            # 一律排除草稿；草稿只留在 observations/answer（过程记录与
            # 知识型任务的答案来源）。闸门 real 的口径不动（whole-mode
            # 「推理算作答」场景的 no_data 闸门依赖草稿成立）。
            _exec_obs = [o for o in (observations or [])
                         if str(o.get("tool") or "") != "spec_draft"]
            out["execution"] = {
                "ok": real, "sql": sql,
                "calls": max(len(_exec_obs), _mat_n),
                # 「真取到」只数**真的有数据源支撑**的观察：知识型答案（RESULT 是一段
                # 回答、没有任何数据源调用）不该记成"取到数"——否则日志会自欺
                # （2026-10-19 实测：养育问题打出了"真取到=1"）。
                "real_calls": max(
                    sum(1 for o in _exec_obs
                        if _has_real([o]) and (o.get("data_sources") or [])),
                    _mat_n, _course_real),
                "rows": max(
                    sum(int(o.get("rows") or 0)
                        for o in _exec_obs if o.get("ok")),
                    _mat_n, _p2r),
                "data_sources": sorted({s for o in _exec_obs
                                        for s in (o.get("data_sources") or [])}),
                "evidence": ("data" if any(o.get("data_sources")
                                           for o in _exec_obs)
                             else "knowledge"),
            }'''

p = HERE / "learn_orchestrate.py"
src = p.read_text(encoding="utf-8")
n = src.count(OLD)
if n != 1:
    print(f"[SKIP] 锚文本出现 {n} 次（期望 1），中止")
    sys.exit(1)
shutil.copy2(p, str(p) + ".bak_draft")
p.write_text(src.replace(OLD, NEW), encoding="utf-8")
print("[OK] learn_orchestrate.py 执行账排除 spec_draft（备份 .bak_draft）")
