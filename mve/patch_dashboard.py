# -*- coding: utf-8 -*-
"""2026-11-18 面板改造：轨迹页/概览判据区/技能页接 learn_trace 现账。"""
import shutil, sys, os

BASE = "/root/mve/mve"
BAK = ".bak_trace"


def patch(path, pairs, optional=()):
    p = os.path.join(BASE, path)
    src = open(p, encoding="utf-8").read()
    shutil.copy2(p, p + BAK)
    for old, new in pairs:
        if src.count(old) != 1:
            if old in optional:
                print(f"  [skip] {path}: 锚点缺失（optional）")
                continue
            print(f"  [FAIL] {path}: 锚点出现 {src.count(old)} 次（应为 1）")
            print("        锚点前 100 字：", repr(old[:100]))
            sys.exit(1)
        src = src.replace(old, new)
    open(p, "w", encoding="utf-8").write(src)
    print(f"  [ok] {path}")


NEW_PTRACE = r'''function pTrace(s){
  const tr=s.trace||[];
  if(!tr.length) return `<div class="panel"><div class="empty">
    学习轨迹账（learn_trace）还没有行 —— 学习循环跑完一轮 learn()，这里就有：
    Phase1 spec 冻结 / Phase2 finalCode 交付 / 取数真行数 / 里程碑闭合与点亮 / 证据门。
    旧账 run_log 已退役（它按 critic 门记账，里程碑点亮了它还记 failed，全是假 ✗）。</div></div>`;
  const rows=tr.slice().reverse().map((p,i)=>{
    const ms=p.ms_total?`<span class="chip ${p.ms_achieved?'ok':'grey'}">${p.ms_closed}/${p.ms_total}${p.ms_achieved?' ✅':''}</span>`:'<span class="chip grey">—</span>';
    return `<tr>
      <td class="num">${tr.length-i}</td>
      <td class="mono" style="font-size:12px">${ESC((p.question||'').slice(0,52))}</td>
      <td>${p.phase1_passed?'<span class="chip ok">冻结</span>':'<span class="chip bad">未冻结</span>'}</td>
      <td>${p.phase2_submitted?'<span class="chip ok">已交</span>':'<span class="chip bad">未交</span>'}</td>
      <td class="num">${p.fetch_calls||0}</td>
      <td>${p.fetch_ok?`<span class="chip ok">真取到 ${p.fetch_rows||0} 行</span>`:'<span class="chip bad">0</span>'}</td>
      <td>${ms}</td>
      <td>${(p.ms_achieved||p.lit)?'<span class="chip ok">已点亮</span>':'<span class="chip grey">—</span>'}</td>
      <td>${(p.gate_addressed>0&&p.gate_open===0)?'<span class="chip ok">门过</span>':`<span class="chip bad">开 ${p.gate_open||0}</span>`}</td>
      <td><b>${ESC(p.status||'')}</b></td>
      <td style="font-size:12px;opacity:.7">${ESC((p.why||'').slice(0,60))}</td></tr>`;
  }).join('');
  return `<div class="panel">
    <div class="panel__head"><h2>学习轨迹</h2>
      <span class="hint">MVE 现账（learn_trace）：一次 learn() 一行 —— Phase1 / Phase2 / 取数 / 里程碑 / 证据门，不再是原版 Voyager 的 coverage/score</span></div>
    <table><thead><tr><th>#</th><th>题面</th><th>Phase1</th><th>Phase2</th><th>取数</th><th>真取到</th><th>里程碑</th><th>点亮</th><th>证据门</th><th>status</th><th>为什么</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

'''

# ---------- panel_data.py ----------
patch("panel_data.py", [
    (
        '    rows = run_log.for_scene(sid)\n',
        '    rows = run_log.for_scene(sid)\n'
        '    # 学习轨迹账（2026-11-18）：轨迹页/概览判据区改读 learn_trace.jsonl。\n'
        '    # run_log 是原版练习线的账（coverage/score 恒空、里程碑点亮了还记\n'
        '    # failed），新后端的真实指标（Phase1/Phase2/取数/里程碑/证据门）在\n'
        '    # learn_trace 里。\n'
        '    try:\n'
        '        import learn_trace as _lt\n'
        '        _trace = _lt.for_scene(sid)\n'
        '    except Exception:\n'
        '        _trace = []\n',
    ),
    (
        '        "series": series,\n',
        '        "series": series,\n'
        '        # 学习轨迹账（MVE 现账，2026-11-18）：轨迹页/判据区读它\n'
        '        "trace": _trace,\n'
        '        "trace_total": len(_trace),\n',
    ),
])

# ---------- dashboard.py ----------
patch("dashboard.py", [
    # 页签徽标：轮数改读 trace_total
    (
        "  {id:'trace',    icon:'🧭', label:'轨迹',   status:s=>s.total_rounds+' 轮'},",
        "  {id:'trace',    icon:'🧭', label:'轨迹',   status:s=>(s.trace_total||0)+' 轮'},",
    ),
    # 概览判据区：两条步进图改读 trace（绿=真取到，蓝=过证据门）
    (
        '  const suc = (s.series||[]).filter(p=>p.done!==null && p.done!==undefined)\n'
        '                            .map(p=>({x:p.i,v:!!p.done}));\n'
        '  const vlm = (s.series||[]).filter(p=>p.success!==null && p.success!==undefined)\n'
        '                            .map(p=>({x:p.i,v:!!p.success}));',
        '  // 2026-11-18：判据区改读学习轨迹账（learn_trace）—— 绿 = 真取到数，\n'
        '  // 蓝 = 过证据门（critic 判完成）。不再用原版 Voyager 的 done/success\n'
        '  // （新后端恒 ✗，画出来像全失败，其实节点都点亮了）。\n'
        '  const suc = (s.trace||[]).map((p,i)=>({x:i+1,v:!!p.fetch_ok}));\n'
        '  const vlm = (s.trace||[]).map((p,i)=>({x:i+1,v:p.status===\'learned\'||p.status===\'partial\'}));',
    ),
    (
        '        <span class="hint">绿 = 跑通 + 拿到数 + 没翻车；这条线决定存不存技能，不是进步曲线</span></div>',
        '        <span class="hint">绿 = 注入后真取到数（跑通 + 拿到数）；蓝 = 过证据门（critic 判完成）—— 数据来自 learn_trace 现账</span></div>',
    ),
    (
        '      <div class="legend">下面这条蓝灰是 VLML 的 success（全评分点达成）—— 考核尺子，不驱动学习。</div>',
        '      <div class="legend">蓝 = 过证据门 —— critic 的尺子（决定轨迹 verdict 与技能入库）；技能的「真取到」记账不走它。</div>',
    ),
    # 技能页：命中→注入、有效→真取到、有效率→真取到率
    (
        '<span class="chip grey">命中 ${x.hits}</span>',
        '<span class="chip grey" title="被检索注入 prompt 的次数">注入 ${x.hits}</span>',
    ),
    (
        "<span class=\"chip ${x.ok>0?'ok':'grey'}\">有效 ${x.ok}</span>",
        "<span class=\"chip ${x.ok>0?'ok':'grey'}\" title=\"注入后这轮真取到数的次数\">真取到 ${x.ok}</span>",
    ),
    (
        '`<span class="chip ${x.ok>0?\'ok\':\'bad\'}">有效率 ${Math.min(100,Math.round(100*x.ok/x.hits))}%</span>`',
        '`<span class="chip ${x.ok>0?\'ok\':\'bad\'}">真取到率 ${Math.min(100,Math.round(100*x.ok/x.hits))}%</span>`',
    ),
    (
        "if(hits>=2&&ok/hits<0.5) return '<span class=\"chip bad\">🔴 失效（命中多、有效少）</span>';",
        "if(hits>=2&&ok/hits<0.5) return '<span class=\"chip bad\">🔴 失效（注入多、真取到少）</span>';",
    ),
    (
        '      <div class="legend"><b>命中</b> = 被检索注入过几次 prompt · <b>有效</b> = 注入后这轮判成功几次 ·\n'
        '        <b>命中涨、有效不涨</b> = 注入了但没帮上，它会在检索排序里自然沉底。\n'
        '        有效率高 = 这条经验真在被复用。\n'
        '      </div>',
        '      <div class="legend"><b>注入</b> = 被检索注入过几次 prompt · <b>真取到</b> = 注入后这轮真的取到数/真算过几次\n'
        '        （2026-11-18 换尺：不再拿 critic 的证据门当技能的尺，与里程碑点亮的 fetched 口径同一条）·\n'
        '        <b>注入涨、真取到不涨</b> = 注入了但没帮上，检索排序里自然沉底（命中加成已删，失败配方不再霸榜）。\n'
        '      </div>',
    ),
    (
        '        <span class="hint">照原版 voyager.py:195/199：每轮<b>先检索</b>再写 SQL；按有效性排序</span></div>',
        '        <span class="hint">每轮<b>先检索</b>再执行（retrieve → rollout）；按真取到率排序</span></div>',
    ),
    (
        '<li><b>${ESC(x.name)}</b> · 命中 ${x.hits} · 有效 ${x.ok}',
        '<li><b>${ESC(x.name)}</b> · 注入 ${x.hits} · 真取到 ${x.ok}',
    ),
])

# pTrace 整函数替换（按区间切）
p = os.path.join(BASE, "dashboard.py")
src = open(p, encoding="utf-8").read()
i = src.find("function pTrace(s){")
j = src.find("function pSkill(s){")
if i < 0 or j < 0 or j <= i:
    print("  [FAIL] pTrace 区间定位失败", i, j)
    sys.exit(1)
src = src[:i] + NEW_PTRACE + src[j:]
open(p, "w", encoding="utf-8").write(src)
print("  [ok] dashboard.py pTrace 整函数替换")

print("patch_dashboard done")
