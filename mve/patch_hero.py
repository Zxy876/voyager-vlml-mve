#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-11-18 概览判据区换账本（用户：这个链接的账本应该换一下）。

改前（全是原版 Voyager 的账）：
  · 当前题判据  = run_log 最后一行的 success（练习线判分，里程碑点亮了
                  它照样记 failed）
  · 迁移成功率  = exam_curve.transfer（VLML 迁移考，本场景没考过 → 永远 "—"）
  · 累计轮次    = len(series) = run_log 行数（17 是练习线的旧行）
  · 技能库      = skill_store（这本是真的，不动）

改后（接 MVE 现账 learn_trace）：
  · 当前题判据  = 最后一行 trace：lit（已点亮）> ms_achieved（维度闭合但
                  门未过）> 未达成（带 why）
  · 迁移成功率  = 有迁移考数据时保留原样；没有时该位显示「真取到率」
                  = 真取到轮数 / 进执行轮数（trace 上算，与技能页同尺）
  · 累计轮次    = trace 行数（真实 learn() 轮数）

新增 state.trace_kpi（rounds/entered/fetched/lit/last），面板只读它。
"""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def splice(fname, old, new):
    p = HERE / fname
    src = p.read_text(encoding="utf-8")
    n = src.count(old)
    if n != 1:
        print(f"[SKIP] {fname}: 锚文本出现 {n} 次（期望 1）→ {old[:70]!r}")
        return False
    shutil.copy2(p, str(p) + ".bak_hero")
    p.write_text(src.replace(old, new), encoding="utf-8")
    print(f"[OK] {fname}: {old[:60]!r} …")
    return True


ok = True

# ---------- 1. panel_data.py：trace_kpi ----------
ok &= splice(
    "panel_data.py",
    '''    try:
        import learn_trace as _lt
        _trace = _lt.for_scene(sid)
    except Exception:
        _trace = []''',
    '''    try:
        import learn_trace as _lt
        _trace = _lt.for_scene(sid)
    except Exception:
        _trace = []
    # 概览判据区 KPI（2026-11-18 换账本）：全部从 learn_trace 现算，
    # 不再读 run_log 的 success/行数（那是原版练习线的账）。
    #   entered = 这轮真进了执行（Phase2 交付 / 调过取数 / 真取到）
    #   fetched = 真取到数
    #   lit     = 里程碑点亮（真做出来了的最硬信号）
    try:
        _entered_n = sum(1 for _r in _trace
                         if _r.get("phase2_submitted")
                         or int(_r.get("fetch_calls") or 0) > 0
                         or _r.get("fetch_ok"))
        _trace_kpi = {
            "rounds": len(_trace),
            "entered": _entered_n,
            "fetched": sum(1 for _r in _trace if _r.get("fetch_ok")),
            "lit": sum(1 for _r in _trace if _r.get("lit")),
            "last": dict(_trace[-1]) if _trace else None,
        }
    except Exception:
        _trace_kpi = {"rounds": 0, "entered": 0, "fetched": 0, "lit": 0, "last": None}''',
)

# 空态返回值也带上（面板没数据时不显示 undefined）
ok &= splice(
    "panel_data.py",
    '''            # 导航上的「轨迹 N 轮」读它；不给就是 "undefined 轮"
            "total_rounds": 0,''',
    '''            # 导航上的「轨迹 N 轮」读它；不给就是 "undefined 轮"
            "total_rounds": 0,
            "trace_kpi": _trace_kpi,''',
)

# 主返回值
ok &= splice(
    "panel_data.py",
    '''        "trace": _trace,
        "trace_total": len(_trace),''',
    '''        "trace": _trace,
        "trace_total": len(_trace),
        # 概览判据区（2026-11-18 换账本）：当前题判据/真取到率/累计轮次读它
        "trace_kpi": _trace_kpi,''',
)

# ---------- 2. dashboard.py：hero 三条 KPI 换源 ----------
ok &= splice(
    "dashboard.py",
    '''    const tr=(s.exam_curve||{}).transfer;
    const lastOk = s.last_success ? '<span class="chip ok">success</span>'
                                  : '<span class="chip bad">未达成</span>';
    // 判据区 KPI：当前题 self-verification 结果（不是覆盖率、不是掌握度）
    pills.push(['当前题判据', lastOk]);
    pills.push(['迁移成功率', tr ? (tr.ok_count+' / '+tr.count) : '—']);
    pills.push(['累计轮次', s.total_rounds]);''',
    '''    const tr=(s.exam_curve||{}).transfer;
    // 2026-11-18 换账本：判据区读 learn_trace（MVE 现账），不再读 run_log
    // （原版练习线的 success / 行数 —— 里程碑点亮了它还记 failed）。
    const tk=s.trace_kpi||{}, last=tk.last||null;
    const lastOk = !last
      ? '<span class="chip grey" title="learn_trace 还没有行 —— 起点学习循环后落账">还没有轮次</span>'
      : (last.lit
         ? '<span class="chip ok" title="里程碑点亮（真取到数 + 维度闭合）">达成 · 已点亮</span>'
         : (last.ms_achieved
            ? '<span class="chip warn" title="维度全闭合，证据门/点亮没过">维度已闭合</span>'
            : `<span class="chip bad" title="${ESC((last.why||last.status||'').slice(0,160))}">未达成</span>`));
    pills.push(['当前题判据', lastOk]);
    // 有迁移考数据就显示原指标；没有（本场景没考过）时该位显示现账的真取到率。
    if(tr){
      pills.push(['迁移成功率', tr.ok_count+' / '+tr.count]);
    }else{
      const ent=+tk.entered||0, fet=+tk.fetched||0;
      pills.push(['真取到率', ent
        ? `<span title="learn_trace：真取到 ${fet} 轮 / 进执行 ${ent} 轮">${Math.round(100*fet/ent)}% <span style="opacity:.6;font-size:11px">(${fet}/${ent})</span></span>`
        : '<span class="chip dim" title="还没有进入执行的轮次">—</span>']);
    }
    pills.push(['累计轮次', `<span title="learn_trace 行数（MVE 现账，一次 learn() 一行）">${+tk.rounds||0}</span>`]);''',
)

if not ok:
    print("\n有锚文本没匹配上，中止（已成功的文件都有 .bak_hero 备份）")
    sys.exit(1)
print("\nALL PATCHED")
