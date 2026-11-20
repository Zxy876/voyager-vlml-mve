#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-11-18 技能「真取到率」分母修正（用户问「为什么注入多有效少」）。

问题：率 = ok / hits，而
  · hits = 被检索注入 prompt 的**轮数**（开题时就 +1，不管这轮后来死在哪）；
  · ok   = 注入后**这轮真取到数**的轮数。
Phase1 卡死/骨架没冻结就失败的轮，结构上永远不可能取到数 —— 它们全被
算进分母，任何配方都会显得「失效」。旧账里 ok 更是一次都没写过（写入链
11:25 才补，跑的是 10:03 启动的旧进程，11:44 循环就停了）。

治法：新增第三个计数 runs =「注入后真的进入执行（Phase2 交付/取数调用过）
的轮数」，率 = ok / runs；hits 保留为「注入次数」的原始读数。
  · runs<2            → 样本不足，不下判（注入过但大多没进执行）
  · runs>=2 且 率<0.5 → 失效（进了执行却没取到数）
  · hits==0           → 未验证

改动：skill_store.py（mark_reached + 打分用 ok/runs）
      learn_orchestrate.py（_record 里进执行就 mark_reached）
      panel_data.py（skill_detail 带 runs，rate=ok/runs）
      dashboard.py（徽标与健康度换尺）
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
    bak = str(p) + ".bak_reach"
    shutil.copy2(p, bak)
    p.write_text(src.replace(old, new), encoding="utf-8")
    print(f"[OK] {fname}: {old[:60]!r} …")
    return True


ok = True

# ---------- 1. skill_store.py：mark_reached ----------
ok &= splice(
    "skill_store.py",
    '''def mark_result(keys: list[str], ok: bool) -> None:''',
    '''def mark_reached(keys: list[str]) -> None:
    """注入后这一轮**真的进入执行** → runs +1（2026-11-18）。

    分母修正：真取到率原来算 ok/hits，而 hits 在开题注入时就 +1 ——
    Phase1 卡死、骨架没冻结的轮全被算进分母，结构上不可能取到数，
    任何配方都会显示「失效」。runs 只在「这轮进了 Phase2 / 真调过取数
    工具」时才 +1，率 = ok/runs 才是「配方被真正用上时，取到数的比例」。

    hits 保留为注入次数的原始读数（面板上单列「注入 N 次」）。
    """
    if not keys:
        return
    data = _load_raw()
    touched = False
    for k in keys:
        if k in data["skills"]:
            s = data["skills"][k]
            s["runs"] = int(s.get("runs") or 0) + 1
            s["hits"] = max(int(s.get("hits") or 0), s["runs"])
            touched = True
    if touched:
        _save(data)


def mark_result(keys: list[str], ok: bool) -> None:''',
)

# ---------- 2. skill_store.py：mark_result 兜底 runs（ok ≤ runs ≤ hits） ----------
ok &= splice(
    "skill_store.py",
    '''            s["ok"] = int(s.get("ok") or 0) + 1
            s["hits"] = max(int(s.get("hits") or 0), s["ok"])
            touched = True''',
    '''            s["ok"] = int(s.get("ok") or 0) + 1
            s["hits"] = max(int(s.get("hits") or 0), s["ok"])
            s["runs"] = max(int(s.get("runs") or 0), s["ok"])
            touched = True''',
)

# ---------- 3. skill_store.py：检索打分质量项改 ok/runs ----------
ok &= splice(
    "skill_store.py",
    '''            hits = int(s.get("hits") or 0)
            score += 0.6 * (int(s.get("ok") or 0) / max(hits, 1))''',
    '''            # 2026-11-18：质量项分母从 hits 换成 runs —— hits 含
            # 「注入后压根没进执行」的轮，那些轮取不到数不是配方的错。
            runs = int(s.get("runs") or 0)
            score += 0.6 * (int(s.get("ok") or 0) / max(runs, 1))''',
)

# ---------- 4. learn_orchestrate.py：_record 里进执行就 mark_reached ----------
ok &= splice(
    "learn_orchestrate.py",
    '''        _ik = [k for k in (result.get("_injected_skill_keys") or []) if k]
        if _ik and _fetched:
            import skill_store as _ssr
            _ssr.mark_result(_ik, True)''',
    '''        _ik = [k for k in (result.get("_injected_skill_keys") or []) if k]
        if _ik:
            import skill_store as _ssr
            # 2026-11-18 分母修正：先把「这轮真进了执行」记到 runs，
            # 再记「取到数」到 ok —— 率 = ok/runs。进了执行就记 runs，
            # 即使最后没取到数（那才是配方该背的账）；Phase1 就死的轮
            # 不进 runs，否则分母被不可满足的轮稀释成「全员失效」。
            _entered = bool(_fetched or _trow.get("phase2_submitted")
                            or int(_trow.get("fetch_calls") or 0) > 0)
            if _entered:
                try:
                    _ssr.mark_reached(_ik)
                except Exception:
                    pass
            if _fetched:
                _ssr.mark_result(_ik, True)''',
)

# ---------- 5. panel_data.py：skill 反链行带 runs ----------
ok &= splice(
    "panel_data.py",
    '''                "hits": int(s.get("hits") or 0),
                "ok": int(s.get("ok") or 0),
                # 技能本体（跑通的那段程序/SQL）—— 详情页可展开看，默认折叠''',
    '''                "hits": int(s.get("hits") or 0),
                "ok": int(s.get("ok") or 0),
                "runs": int(s.get("runs") or 0),
                # 技能本体（跑通的那段程序/SQL）—— 详情页可展开看，默认折叠''',
)

# ---------- 6. panel_data.py：skill_detail 带 runs，rate=ok/runs ----------
ok &= splice(
    "panel_data.py",
    '''            "hits": int(s.get("hits") or 0),
            "ok": int(s.get("ok") or 0),
            # 1→n 场景归属''',
    '''            "hits": int(s.get("hits") or 0),
            "ok": int(s.get("ok") or 0),
            # runs = 注入后真进了执行的轮数（2026-11-18 分母修正：
            # 真取到率 = ok/runs；hits 只作注入次数读数）
            "runs": int(s.get("runs") or 0),
            # 1→n 场景归属''',
)

ok &= splice(
    "panel_data.py",
    '''            "rate": (
                round(int(s.get("ok") or 0) / int(s.get("hits") or 1), 3)
                if int(s.get("hits") or 0) else None
            ),''',
    '''            "rate": (
                round(int(s.get("ok") or 0) / int(s.get("runs") or 1), 3)
                if int(s.get("runs") or 0) else None
            ),''',
)

# ---------- 7. dashboard.py：健康度换尺 ----------
ok &= splice(
    "dashboard.py",
    '''  const health=x=>{
    const hits=+x.hits||0, ok=+x.ok||0;
    if(hits>=2&&ok/hits<0.5) return '<span class="chip bad">🔴 失效（注入多、真取到少）</span>';
    if(hits===0) return '<span class="chip grey">⚪ 未验证</span>';
    return '<span class="chip ok">🟢 正常复用</span>';};''',
    '''  // 2026-11-18 换尺：分母用 runs（注入后真进了执行的轮数），不用 hits
  // （开题注入就 +1，Phase1 就死的轮永远取不到数，会把所有配方拖成"失效"）。
  const health=x=>{
    const hits=+x.hits||0, ok=+x.ok||0, runs=+x.runs||0;
    if(hits===0) return '<span class="chip grey">⚪ 未验证</span>';
    if(runs<2) return `<span class="chip grey" title="注入 ${hits} 次里只有 ${runs} 次真进了执行，样本不足不下判">⚪ 样本不足</span>`;
    if(ok/runs<0.5) return '<span class="chip bad">🔴 失效（进了执行没取到数）</span>';
    return '<span class="chip ok">🟢 正常复用</span>';};''',
)

ok &= splice(
    "dashboard.py",
    '''        <span class="chip grey" title="被检索注入 prompt 的次数">注入 ${x.hits}</span>
        <span class="chip ${x.ok>0?'ok':'grey'}" title="注入后这轮真取到数的次数">真取到 ${x.ok}</span>
        ${x.hits>0
          ? `<span class="chip ${x.ok>0?'ok':'bad'}">真取到率 ${Math.min(100,Math.round(100*x.ok/x.hits))}%</span>`
          : '<span class="chip dim">还没被用过</span>'}''',
    '''        <span class="chip grey" title="被检索注入 prompt 的轮数（开题即计，不管后来死在哪）">注入 ${x.hits}</span>
        <span class="chip grey" title="注入后真的进入执行（Phase2 交付 / 真调过取数工具）的轮数 —— 真取到率的分母">进执行 ${x.runs||0}</span>
        <span class="chip ${x.ok>0?'ok':'grey'}" title="进入执行且这轮真取到数的轮数">真取到 ${x.ok}</span>
        ${x.runs>0
          ? `<span class="chip ${x.ok>0?'ok':'bad'}">真取到率 ${Math.min(100,Math.round(100*x.ok/x.runs))}%</span>`
          : '<span class="chip dim">还没进过执行</span>'}''',
)

if not ok:
    print("\n有锚文本没匹配上，中止（已成功的文件都有 .bak_reach 备份）")
    sys.exit(1)
print("\nALL PATCHED")
