#!/usr/bin/env python3
"""MVE 面板：看 Voyager 的进步曲线，并能在面板上一键启停它。

跑法：
    python mve/dashboard.py            # 然后打开 http://127.0.0.1:8777
    python mve/dashboard.py --port 9000

形态来源（重要）：
- VLML 仓库里没有任何前端文件（无 html/tsx/jsx/vue），它的"面板"就是 LLM 对话界面，
  没有可抄的 UI。所以面板形态取自猫娘伴学的 static/ 前端：
    · 布局：page-shell → hero（左标题+状态 / 右 summary-pills+控件）
            → workspace-nav（6 格 tab）→ workspace-stage（**一次只显示一个面板**）
    · 视觉：纸质卡 rgba 白 + 柔和阴影 + 8px 圆角；选中态用品牌色底 + 底部 3px 指示条
    · 色阶：直接复用伴学的 mastery 五档色（weak/progress/good/mastered/new）
  数据取自 MVE 自己的 run_log.jsonl / causal_timeline.jsonl / pilot_state.json。

为什么改成 tab 分区：上一版把 12 个 section 一路堆到底，信息密度过高，
"当前该看什么"完全靠人自己找。伴学的做法是**一次只展开一个工作区**。

面板不做任何推断，只显示真实发生过的东西。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import panel_data  # noqa: E402
import pilot  # noqa: E402

ROOT = Path(__file__).resolve().parent


def _graph_json() -> str:
    """知识图谱：读落盘的 json，**不 import vlml_env**（保持面板轻量、秒开）。

    照伴学 `graph_read_model.py` 的规矩：只返回结构，不在这一层做任何推断。
    没建过图谱时返回 empty 标记，前端据此提示"先跑 --build"。
    """
    try:
        import knowledge_graph
        g = knowledge_graph.KnowledgeGraph.load()
    except Exception as e:
        return json.dumps({"empty": True, "error": f"{type(e).__name__}: {e}"},
                          ensure_ascii=False)
    if not g.nodes:
        return json.dumps({"empty": True, "error": "图谱未构建，先跑 python mve/knowledge_graph.py --build"},
                          ensure_ascii=False)
    out = g.to_dict()
    # 待审候选：运行期题带进来的新维度名，**不建节点**（照伴学：运行期新建的
    # 知识点不进权威层）。单独返回，前端据此在图谱页顶上显示一条。
    try:
        import knowledge_graph
        out["pending_dims"] = knowledge_graph.load_pending_dimensions()
    except Exception:
        out["pending_dims"] = []
    return json.dumps(out, ensure_ascii=False)

def _scope_json() -> str:
    """当前练习范围（只读）。照伴学 study_get_practice_scope。"""
    try:
        import practice_scope
        return json.dumps({"ok": True, "scope": practice_scope.get_scope()},
                          ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                          ensure_ascii=False)


# 默认只绑回环：面板没有任何鉴权，绑 0.0.0.0 等于把「启停进程」的接口裸奔到公网。
# 要放到服务器上从外面访问，显式给 --host（配合安全组只放行自己的 IP）。
PORT = int(os.environ.get("MVE_PANEL_PORT") or 8777)
HOST = os.environ.get("MVE_PANEL_HOST") or "127.0.0.1"
# 挂到反向代理子路径下时用（如 https://host:8443/mve/）：
# 服务端剥前缀，前端所有 fetch 也要跟着加前缀，否则会打到代理的根路径上去。
PREFIX = os.environ.get("MVE_PANEL_PREFIX") or ""

for _i, _a in enumerate(sys.argv[1:]):
    if _a == "--port" and _i + 2 <= len(sys.argv):
        PORT = int(sys.argv[_i + 2])
    if _a == "--host" and _i + 2 <= len(sys.argv):
        HOST = sys.argv[_i + 2]
    if _a == "--prefix" and _i + 2 <= len(sys.argv):
        PREFIX = "/" + sys.argv[_i + 2].strip("/")
def _strip_prefix(path: str) -> str:
    """剥掉反向代理挂在前面的子路径（/mve/api/state → /api/state）。

    设了前缀却没带前缀的请求一律打空串（走 404），避免代理后面被直接绕过前缀访问。
    """
    if PREFIX:
        if path == PREFIX:
            return "/"
        return path[len(PREFIX):] if path.startswith(PREFIX + "/") else ""
    return path


def _html() -> str:
    """前缀注入：前端 fetch('/api/...') 要带上前缀，否则请求会打到代理的根路径。"""
    if not PREFIX:
        return HTML
    return (HTML.replace("<head>", '<head>\n'
                         f'<script>window.MVE_PREFIX="{PREFIX}";</script>', 1)
                .replace("fetch('", "fetch(window.MVE_PREFIX+'"))


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MVE 面板 · Voyager 进步曲线</title>
<style>
:root{
  color-scheme: light;
  font-family:"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  --bg:#f3f7f1;
  --paper:rgba(253,255,250,.94);
  --paper-strong:rgba(255,255,255,.98);
  --ink:#1f2924;
  --muted:#607168;
  --line:rgba(31,41,36,.13);
  --brand:#2f7d57;
  --brand-strong:#17563d;
  --accent:#d58b2b;
  --accent-strong:#8a5317;
  --warning:#b7791f;
  --warning-bg:rgba(183,121,31,.10);
  /* 伴学 mastery 五档色，直接搬过来 */
  --mastery-new:#cbd5d0;
  --mastery-weak:#e89a90;
  --mastery-progress:#e2b85a;
  --mastery-good:#9bd9b8;
  --mastery-mastered:#82d99e;
  --shadow:0 10px 24px rgba(31,52,40,.07);
  --radius:8px;
  --radius-sm:6px;
  background:var(--bg);
  color:var(--ink);
}
*{box-sizing:border-box}
body{
  margin:0;min-height:100vh;
  background:
    linear-gradient(90deg,rgba(47,125,87,.045) 1px,transparent 1px),
    linear-gradient(rgba(47,125,87,.04) 1px,transparent 1px),
    linear-gradient(180deg,#f8fbf6 0%,#eef5f2 100%);
  background-size:36px 36px,36px 36px,auto;
}
h1,h2,p{margin:0}
button,textarea,select{font:inherit}

/* ---------- 骨架 ---------- */
.page-shell{width:min(1240px,calc(100vw - 24px));min-width:0;margin:0 auto;padding:28px 0 36px}
@media(max-width:980px){.page-shell{width:calc(100vw - 24px);padding:20px 0 28px}}
.main-view{display:grid;gap:18px}

/* ---------- hero ---------- */
.hero{
  position:relative;display:grid;gap:24px;align-items:center;
  grid-template-columns:minmax(300px,.9fr) minmax(520px,1.3fr);
  min-height:166px;padding:24px 24px 24px 28px;
  border:1px solid rgba(47,125,87,.20);
  border-left:6px solid rgba(47,125,87,.72);
  border-radius:var(--radius);
  background:linear-gradient(135deg,rgba(255,255,255,.96),rgba(244,249,241,.86)),var(--paper);
  box-shadow:var(--shadow);overflow:hidden;
}
@media(max-width:980px){.hero{grid-template-columns:1fr;min-height:0}}
.hero__copy,.hero__controls{position:relative;z-index:1}
.hero__copy{display:grid;gap:10px}
.hero__eyebrow{color:var(--brand-strong);font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}
.hero-title{display:inline-flex;align-items:center;gap:8px;font-size:26px;line-height:1.15}
.hero-title__cat{display:inline-grid;place-items:center;width:32px;height:32px;
  border:1px solid rgba(47,125,87,.20);border-radius:var(--radius);
  background:rgba(47,125,87,.10);font-size:18px;line-height:1}
.hero__status{color:var(--muted);font-size:14px}
.hero__controls{display:grid;gap:14px;justify-items:end}
.summary-pills{display:flex;gap:8px;align-items:center;justify-content:flex-end;
  width:100%;flex-wrap:wrap}
.summary-pill{display:grid;gap:3px;min-width:132px;max-width:230px;padding:9px 12px;
  border:1px solid rgba(47,125,87,.16);border-left:3px solid rgba(47,125,87,.48);
  border-radius:var(--radius-sm);background:rgba(253,255,250,.86);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.92)}
.summary-pill span{color:var(--muted);font-size:12px;font-weight:700;text-transform:uppercase}
.summary-pill strong{min-width:0;color:var(--ink);font-size:14px;line-height:1.35;overflow-wrap:anywhere}

/* ---------- 主控台（启动 / 停止） ---------- */
.runctl{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-top:2px}
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 16px;border-radius:var(--radius-sm);
  border:1px solid transparent;cursor:pointer;font-size:13.5px;font-weight:600;
  transition:transform 150ms ease,background 150ms ease,border-color 150ms ease}
.btn:hover{transform:translateY(-1px)}
.btn:disabled{opacity:.55;cursor:not-allowed;transform:none}
.btn-primary{background:var(--brand);color:#fff;box-shadow:0 1px 2px rgba(31,52,40,.12)}
.btn-primary:hover{background:var(--brand-strong)}
.btn-danger{background:#c0392b;color:#fff}
.btn-danger:hover{background:#96281a}
.btn-secondary{background:#fff;color:#374151;border-color:var(--line)}
.btn-secondary:hover{border-color:rgba(47,125,87,.42);background:rgba(237,248,240,.94)}
.runctl select{padding:8px 10px;border:1px solid var(--line);border-radius:var(--radius-sm);
  background:#fff;color:var(--ink);font-size:13px}
.runctl .lbl{font-size:12px;color:var(--muted);font-weight:700;text-transform:uppercase}
.runstate{display:inline-flex;align-items:center;gap:7px;font-size:13px;color:var(--muted)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mastery-new);display:inline-block}
.dot.on{background:#22c55e;box-shadow:0 0 0 3px rgba(34,197,94,.18);animation:pulse 1.6s ease-in-out infinite}
.dot.off{background:#9ca3af}
/* 练习范围条：范围由知识图谱页设定，这里只显示 + 清除 */
.scopebar{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-top:8px;
  padding:7px 10px;border:1px dashed var(--line);border-radius:var(--radius-sm);background:rgba(255,255,255,.55)}
.scopebar .lbl{font-size:12px;color:var(--muted);font-weight:700;text-transform:uppercase}
.scopebar .hint{font-size:12px;color:var(--muted)}
.scope-act{display:inline-flex;gap:6px;align-items:center}
.tbl tr.now td{background:#eef8f3}
@keyframes pulse{50%{box-shadow:0 0 0 6px rgba(34,197,94,.06)}}

/* ---------- 工作区 tab ---------- */
.workspace-nav{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:10px;min-width:0;
  padding:10px;border:1px solid rgba(31,41,36,.12);border-radius:var(--radius);
  background:rgba(255,255,255,.88);box-shadow:var(--shadow)}
@media(max-width:980px){.workspace-nav{grid-template-columns:repeat(3,minmax(0,1fr))}}
.workspace-tab{position:relative;display:grid;grid-template-columns:auto minmax(0,1fr);
  grid-template-rows:auto auto;gap:5px 9px;align-items:center;min-width:0;min-height:68px;
  padding:11px 12px;color:var(--ink);text-align:left;overflow:hidden;
  border:1px solid rgba(31,41,36,.11);border-radius:var(--radius-sm);
  background:rgba(250,252,249,.94);box-shadow:0 1px 2px rgba(31,52,40,.04);
  cursor:pointer;transition:transform 150ms ease,border-color 150ms ease,background 150ms ease}
.workspace-tab::after{content:"";position:absolute;inset:auto 10px 0;height:3px;border-radius:999px 999px 0 0;
  background:var(--brand);opacity:0;transform:scaleX(.55);transition:opacity 150ms ease,transform 150ms ease}
.workspace-tab:hover{border-color:rgba(47,125,87,.30);background:#fff;transform:translateY(-2px)}
.workspace-tab[aria-selected="true"]{color:var(--brand-strong);
  border-color:rgba(47,125,87,.38);background:rgba(237,248,240,.92)}
.workspace-tab[aria-selected="true"]::after{opacity:1;transform:scaleX(1)}
.workspace-tab__icon{grid-row:1/-1;display:grid;place-items:center;width:34px;height:34px;
  color:var(--brand-strong);border:1px solid rgba(47,125,87,.20);border-radius:10px;
  background:rgba(237,248,240,.92);font-size:17px}
.workspace-tab__label{min-width:0;font-size:13.5px;font-weight:700;line-height:1.25}
.workspace-tab__status{min-width:0;font-size:11.5px;color:var(--muted);line-height:1.35;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ---------- 面板 ---------- */
.workspace-stage{display:grid;gap:16px}
.panel{border:1px solid var(--line);border-radius:var(--radius);background:var(--paper-strong);
  box-shadow:var(--shadow);padding:18px 20px}
.panel__head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:12px;
  padding-bottom:10px;border-bottom:1px solid var(--line)}
.panel__head h2{font-size:16px;line-height:1.3}
.panel__head .hint{font-size:12px;color:var(--muted)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:980px){.grid2{grid-template-columns:1fr}}

h3{margin:16px 0 8px;font-size:13px;color:var(--muted);font-weight:700;
   text-transform:uppercase;letter-spacing:.04em}
h3:first-child{margin-top:0}
.kpis{display:flex;gap:26px;flex-wrap:wrap}
.kpi .label{font-size:12px;color:var(--muted);font-weight:700;text-transform:uppercase}
.kpi .value{font-size:26px;font-weight:600;line-height:1.2}
.chip{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
      background:rgba(47,125,87,.12);color:var(--brand-strong);margin-right:6px}
.chip.warn{background:var(--warning-bg);color:#784910}
.chip.bad{background:rgba(192,57,43,.12);color:#8a2015}
.chip.ok{background:rgba(130,217,158,.28);color:#14532d}
.chip.grey{background:rgba(31,41,36,.07);color:#4b5563}
.chip.m-weak{background:var(--mastery-weak);color:#7a2318}
.chip.m-progress{background:var(--mastery-progress);color:#6b4308}
.chip.m-good{background:var(--mastery-good);color:#14532d}
.chip.m-mastered{background:var(--mastery-mastered);color:#0f4a2a}
.chip.m-new{background:var(--mastery-new);color:#3f4a44}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:700;font-size:12px}
td.num{font-variant-numeric:tabular-nums}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.empty{color:var(--muted);font-size:13px;padding:8px 0}
.note{border:1px solid #fde68a;border-radius:var(--radius-sm);padding:10px 12px;
      font-size:12.5px;color:#78350f;background:#fffbeb;margin-top:10px}
.note.info{border-color:rgba(47,125,87,.28);background:rgba(237,248,240,.92);color:#17563d}
ul{margin:6px 0 0;padding-left:20px}
li{margin:3px 0}
.tool{display:inline-block;background:rgba(31,41,36,.05);border:1px solid var(--line);
      border-radius:5px;padding:1px 7px;margin:2px 3px 2px 0;font-size:12px}
.tool.new{background:rgba(130,217,158,.28);border-color:#86efac;color:#14532d}
.legend{font-size:12px;color:var(--muted);margin-top:6px}
.nocmp{border:1px dashed #9ca3af;border-radius:var(--radius-sm);padding:12px 14px;
       margin-top:10px;background:#fafafa}
.nocmp-tag{display:inline-block;font-size:11px;color:#6b7280;border:1px dashed #9ca3af;
           border-radius:4px;padding:1px 7px;margin-bottom:7px}
.nocmp-body{font-size:13.5px;line-height:1.75}
.nocmp-sub{font-size:12px;color:var(--muted);margin-top:7px}
.nocmp-sub ul{margin:4px 0 0}
.tasks{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:10px}
@media(max-width:980px){.tasks{grid-template-columns:1fr}}
.taskcard{border:1px solid var(--line);border-radius:var(--radius-sm);padding:11px 13px;background:#fcfcfd}
.taskcard.now{border-color:rgba(47,125,87,.42);background:rgba(237,248,240,.72)}
.taskcard .th{font-size:13px;margin-bottom:6px;line-height:1.45}
.taskcard .tm{font-size:12px;color:var(--muted);margin-top:4px}
.recmd{border:1px solid var(--line);border-radius:var(--radius-sm);padding:11px 13px;background:#fff;margin-top:10px}
.recmd.now{border-color:rgba(47,125,87,.42);background:rgba(237,248,240,.72)}
.recmd .rq{font-size:13.5px;line-height:1.5;margin:5px 0}
.recmd .rw{font-size:12.5px;color:#374151;margin-top:5px}
.changed{display:inline-block;font-size:11px;background:var(--warning-bg);color:#784910;
         border-radius:4px;padding:1px 7px;margin-left:6px}
.timeline{font-size:12.5px}
.timeline .tl{display:flex;gap:9px;padding:5px 0;border-bottom:1px solid rgba(31,41,36,.07)}
.timeline .tl:last-child{border-bottom:0}
.timeline .seq{color:#9ca3af;font-variant-numeric:tabular-nums;min-width:28px}
.timeline .kd{min-width:78px}
.timeline .ts{color:var(--muted);margin-left:auto;font-size:11.5px;white-space:nowrap}
.imp{border:1px solid rgba(47,125,87,.28);background:rgba(237,248,240,.62);
     border-radius:var(--radius);padding:14px 16px}
.imp textarea{width:100%;min-height:64px;resize:vertical;padding:9px 11px;
  border:1px solid var(--line);border-radius:var(--radius-sm);font:13.5px/1.6 inherit;
  color:var(--ink);background:#fff;outline:none}
.imp textarea:focus{border-color:var(--brand)}
.imp select{padding:7px 10px;border:1px solid var(--line);border-radius:var(--radius-sm);
  font:13px inherit;background:#fff;color:var(--ink);max-width:360px}
.imp-row{display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap;margin-top:10px}
.hit{font-size:12px;color:var(--muted);margin-top:8px;min-height:18px}
.hit b{color:var(--brand-strong)}
.hit.no b{color:#8a2015}
.fact{border-left:3px solid rgba(47,125,87,.38);padding-left:10px;margin:8px 0;font-size:13px}
.fact .fv{font-weight:600}
.spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(47,125,87,.24);
      border-top-color:var(--brand);border-radius:50%;animation:sp .7s linear infinite;
      vertical-align:-1px;margin-right:6px}
@keyframes sp{to{transform:rotate(360deg)}}
.console{background:#12211a;color:#d7e6dc;border-radius:var(--radius-sm);padding:12px 14px;
  font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;
  max-height:460px;overflow:auto;margin-top:10px}
.console .dim{color:#7f9a8c}
</style>
</head>
<body>
<div class="page-shell">
  <div class="main-view">
    <header class="hero">
      <div class="hero__copy">
        <p class="hero__eyebrow">MVE · Voyager × VLML</p>
        <h1 class="hero-title"><span class="hero-title__cat">🛰</span>Voyager 进步曲线</h1>
        <p class="hero__status" id="statusLine">加载中…</p>
        <div class="runctl">
          <span class="lbl">驾驶舱</span>
          <button class="btn btn-primary" id="runStart">▶ 启动 Voyager</button>
          <button class="btn btn-danger" id="runStop" disabled>■ 停止</button>
          <button class="btn btn-secondary" id="btnExport" title="把当前学习过程导出成一份 md">⬇ 导出 md</button>
          <span class="runstate"><i class="dot off" id="runDot"></i><span id="runState">未运行</span></span>
        </div>
        <!-- 练习范围：伴学 onboarding.md:82 —— 人在知识图谱上点知识点定范围，
             出题器只在范围里出题。有了范围就不需要"选难度 / 选题"这类键钮，
             驾驶舱只剩开始 / 停止。 -->
        <div class="scopebar" id="scopeBar">
          <span class="lbl">练习范围</span>
          <span id="scopeChip"><span class="chip grey">未设置 · 出题器自选</span></span>
          <span class="hint" id="scopeHint">去「知识图谱」页点某个维度的「练习此知识点」</span>
        </div>
      </div>
      <div class="hero__controls">
        <div class="summary-pills" id="pills"></div>
      </div>
    </header>

    <nav class="workspace-nav" id="nav"></nav>
    <div class="workspace-stage" id="stage"><div class="empty">加载中…</div></div>
  </div>
</div>

<script>
const ESC = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

const TABS = [
  {id:'overview', icon:'📈', label:'概览',   status:s=>'掌握度 / 覆盖率轨迹'},
  {id:'practice', icon:'🎯', label:'练习',   status:s=>s.topic_id||'—'},
  {id:'import',   icon:'✍️', label:'人导入', status:s=>(s.imports||[]).length+' 条导入'},
  {id:'trace',    icon:'🧭', label:'轨迹',   status:s=>s.total_rounds+' 轮'},
  {id:'skill',    icon:'🧠', label:'技能 · 因果', status:s=>(s.skill_stats&&s.skill_stats.skills||0)+' 条技能'},
  {id:'graph',    icon:'🕸', label:'知识图谱',
   status:s=>s.practice_scope&&s.practice_scope.active?('范围：'+s.practice_scope.label):'维度 → 工具'},
  {id:'console',  icon:'🖥', label:'运行日志', status:s=>s.pilot&&s.pilot.running?'运行中':'空闲'},
];
let TAB = localStorage.getItem('mve_tab') || 'overview';

/* ---------------- 小工具 ---------------- */
function chips(items, cls){
  if(!items||!items.length) return '<span class="chip grey">无</span>';
  return items.map(i=>`<span class="chip ${cls||''}">${ESC(typeof i==='string'?i:i.label)}</span>`).join('');
}
function lineChart(pts, opts){
  const W=560,H=190,P={l:38,r:14,t:14,b:26};
  if(!pts.length) return '<div class="empty">无数据</div>';
  const iw=W-P.l-P.r, ih=H-P.t-P.b;
  const X=i=>P.l+(pts.length===1?iw/2:iw*i/(pts.length-1));
  const Y=v=>P.t+ih*(1-Math.max(0,Math.min(1,v)));
  const grid=[0,.25,.5,.75,1].map(v=>
    `<line x1="${P.l}" y1="${Y(v)}" x2="${W-P.r}" y2="${Y(v)}" stroke="rgba(31,41,36,.08)"/>
     <text x="${P.l-8}" y="${Y(v)+4}" font-size="10" fill="#9ca3af" text-anchor="end">${v*100}%</text>`).join('');
  const path=pts.map((p,i)=>`${i?'L':'M'}${X(i).toFixed(1)},${Y(p.y).toFixed(1)}`).join(' ');
  const dots=pts.map((p,i)=>
    `<circle cx="${X(i).toFixed(1)}" cy="${Y(p.y).toFixed(1)}" r="3.2" fill="${opts.color}">
     <title>第${p.x}次 · ${p.label}</title></circle>`).join('');
  const xlab=pts.map((p,i)=>
    `<text x="${X(i).toFixed(1)}" y="${H-8}" font-size="10" fill="#9ca3af" text-anchor="middle">${p.x}</text>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px">
    ${grid}
    <path d="${path}" fill="none" stroke="${opts.color}" stroke-width="2.2"
          stroke-dasharray="${opts.dash||''}" stroke-linejoin="round"/>
    ${dots}${xlab}</svg>`;
}

/* ---------------- hero ---------------- */
function renderHero(s){
  const last=s.series&&s.series.length?s.series[s.series.length-1]:null;
  const cls={'weak':'m-weak','progress':'m-progress','good':'m-good','mastered':'m-mastered','unassessed':'m-new'};
  const pills=[];
  if(s.has_data){
    pills.push(['掌握度变化', s.mastery_delta||'—']);
    pills.push(['状态', `<span class="chip ${cls[last.ui_status]||'m-new'}">${ESC(last.status_label)}</span>`]);
    pills.push(['累计轮次', s.total_rounds]);
    pills.push(['技能库', (s.skill_stats&&s.skill_stats.skills||0)+' 条 / 膨胀率 '+(s.skill_stats&&s.skill_stats.bloat||1)]);
    if(s.hallucination_total>0) pills.push(['拦下编造', `<span class="chip bad">${s.hallucination_total} 条</span>`]);
  }
  pills.push(['数据源', `<span class="chip ${s.env.state==='ready'?'ok':'bad'}">${s.env.state==='ready'?'VLML 就绪':'VLML 不可用'}</span>`]);
  document.getElementById('pills').innerHTML =
    pills.map(([l,v])=>`<div class="summary-pill"><span>${ESC(l)}</span><strong>${v}</strong></div>`).join('');

  const p=s.pilot||{};
  const dot=document.getElementById('runDot');
  dot.className='dot '+(p.running?'on':'off');
  document.getElementById('runState').textContent =
    p.running ? `运行中 · ${p.mode==='loop'?'连续自适应':'单题'} · 已跑 ${p.iterations||0} 题` : (p.why||'未运行');
  document.getElementById('runStart').disabled = !!p.running;
  document.getElementById('runStop').disabled  = !p.running;

  document.getElementById('statusLine').textContent = s.has_data
    ? (s.reading_text||'')
    : (s.hint||'还没有任何运行记录');

  renderScopeBar(s);
}

/* 练习范围条：有范围就显示「范围 → 题」，没有就提示去图谱页设。
   范围决定出题，所以驾驶舱不需要难度 / 选题键钮，只剩开始 / 停止。 */
function renderScopeBar(s){
  window.__scope = s.practice_scope || {active:false};
  const sc=window.__scope;
  const chip=document.getElementById('scopeChip');
  const hint=document.getElementById('scopeHint');
  if(!chip) return;
  if(sc.active){
    chip.innerHTML = `<span class="chip ok">${ESC(sc.label)}</span>`
      + `<span class="chip grey">${ESC((sc.topics||[]).join('、'))}</span>`
      + `<span class="chip grey">r${sc.scope_revision||0}</span>`
      + ` <button class="btn btn-secondary scope-act" data-scope-clear="1"
           style="padding:4px 9px;font-size:12px">清除范围</button>`;
    hint.textContent = sc.point ? ('范围口径：'+sc.point) : '';
  }else if(sc.invalidated){
    chip.innerHTML = `<span class="chip warn">范围已失效</span>`
      + `<span class="chip grey">${ESC(sc.label||'')}</span>`;
    hint.textContent = ESC(sc.reason||'维度已不在图谱里');
  }else{
    chip.innerHTML = `<span class="chip grey">未设置 · 出题器自选</span>`;
    hint.textContent = '去「知识图谱」页点某个维度的「练习此知识点」';
  }
}

function renderNav(s){
  document.getElementById('nav').innerHTML = TABS.map(t=>`
    <button class="workspace-tab" role="tab" data-tab="${t.id}" aria-selected="${TAB===t.id}">
      <span class="workspace-tab__icon">${t.icon}</span>
      <span class="workspace-tab__label">${t.label}</span>
      <span class="workspace-tab__status">${ESC(t.status(s))}</span>
    </button>`).join('');
}

/* ---------------- 各分区 ---------------- */
/* 真实水平曲线：撤支架考核（学习曲线该看的那一列） */
function examCard(s){
  const e=s.exam_curve||{};
  const p=s.pilot||{};
  const busy=!!p.running;
  const runningJob=busy?(p.job_label||p.job||'任务'):'';
  const ctl=`<div class="runctl">
      <button class="btn btn-primary" id="btnPlacement" ${busy?'disabled':''}>摸底（全库撤图谱考一遍）</button>
      <button class="btn btn-secondary" id="btnFinal" ${busy?'disabled':''}>结业考（带技能库再考一遍）</button>
      <span class="lbl">单元</span>
      <select id="learnUnits">${[2,4,6,8].map(n=>`<option value="${n}" ${n===4?'selected':''}>${n}</option>`).join('')}</select>
      <span class="lbl">迁移对照每</span>
      <select id="learnTransfer">${[0,2,3,4].map(n=>`<option value="${n}" ${n===3?'selected':''}>${n===0?'不考':n+' 单元'}</option>`).join('')}</select>
      <button class="btn btn-secondary" id="btnLearn" ${busy?'disabled':''}>跑学习单元</button>
      <button class="btn btn-danger" id="btnExamStop" ${busy?'':'disabled'}>■ 停止</button>
    </div>
    ${busy?`<div class="note info">正在跑：<b>${ESC(runningJob)}</b>
      ${p.iterations?`（第 ${p.iterations} 次）`:''} —— 进度看「运行日志」页。
      摸底 8 道题约 7 分钟，期间刷新页面不会丢。</div>`:''}`;
  if(!e.has_data) return `<div class="panel"><div class="panel__head"><h2>真实水平曲线 · 撤支架考核</h2>
      <span class="hint">练习那条线是带知识图谱跑的，量的是支架不是水平</span></div>
    <div class="empty">${ESC(e.hint||'还没有裸考记录')}</div>
    <div class="note">点「摸底」开始：把全库每道题**撤掉知识图谱**考一遍（约 7 分钟），
      得出零基础的真实水平。它要求技能库为空 —— 带着技能库考出来的是
      "练过之后的水平"，不是起点。</div>
    ${ctl}</div>`;
  const rowsHtml=(e.tracks||[]).map(t=>{
    const after=t.after.length
      ? t.after.map(a=>`<span class="chip ${a.cov>=1?'ok':(a.cov>0?'warn':'bad')}">${ESC(a.pct)}</span>`).join(' ')
      : '<span class="hint">还没重考过</span>';
    const d=t.delta_pp===null?'':(t.delta_pp>0
      ? `<span class="chip ok">+${t.delta_pp}pp</span>`
      : (t.delta_pp<0?`<span class="chip bad">${t.delta_pp}pp</span>`:'<span class="chip grey">持平</span>'));
    return `<tr><td class="mono">${ESC(t.topic_id)}${t.exhausted?' <span class="chip grey">已毕业让位</span>':''}</td>
      <td>${ESC(t.baseline_pct)} <span class="hint">${ESC(t.level)}</span></td>
      <td>${after}</td>
      <td>${t.final_pct?`<span class="chip ${t.final>=1?'ok':(t.final>0?'warn':'bad')}">${ESC(t.final_pct)}</span>`:'<span class="hint">—</span>'}</td>
      <td>${d}</td></tr>`;
  }).join('');
  const tr=e.transfer;
  const trHtml=tr?`<div class="note"><b>迁移对照（${tr.count} 次未练题考核）：</b>${ESC((tr.seq||[]).join(' → '))} ——
    ${tr.rose?'涨了，说明是真学会而不只是记住了这道题。':'<b>没涨</b>：上升的是「记住了这道题的解法」，不是可迁移的能力。'}</div>`:'';
  const fin=e.final;
  const finHtml=fin?`<div class="note"><b>结业考（${fin.count} 道${fin.covered_all?'·全库':'·部分'}）：</b>
    摸底均值 ${fin.avg_baseline}% → 结业均值 ${fin.avg}%
    （${fin.delta_pp>0?'+':''}${fin.delta_pp}pp，≥80% 的 ${fin.mastered} 道）
    —— 这是<b>带着技能库</b>撤图谱再考一遍的结果，和摸底同一个分母，可直接比。</div>`:'';
  return `<div class="panel"><div class="panel__head"><h2>真实水平曲线 · 撤支架考核</h2>
      <span class="hint">练习给图谱 / 考核撤图谱 —— 按题分行，不首尾相连</span></div>
    <div class="kpis">
      <div class="kpi"><div class="label">摸底均值</div><div class="value">${e.avg_baseline}%</div></div>
      <div class="kpi"><div class="label">最新均值</div><div class="value">${e.avg_latest}%</div></div>
      <div class="kpi"><div class="label">掌握 ≥80%（摸底→现在）</div><div class="value">${e.mastered} → ${e.mastered_now}</div></div>
      <div class="kpi"><div class="label">重考过 / 涨了</div><div class="value">${e.retested} / ${e.rose}</div></div>
      <div class="kpi"><div class="label">平均涨幅</div><div class="value">${e.avg_delta_pp===null?'—':(e.avg_delta_pp>0?'+':'')+e.avg_delta_pp+'pp'}</div></div>
    </div>
    <table><thead><tr><th>题</th><th>摸底（裸考）</th><th>练后重考</th><th>结业考</th><th>Δ</th></tr></thead>
      <tbody>${rowsHtml}</tbody></table>
    ${finHtml}
    ${trHtml}
    ${ctl}
    <div class="legend">摸底 = 清库后第一次全库裸考（基线，不会被顶掉）；重考 = 练完这一题后撤掉图谱再考；
      结业考 = <b>带着现有技能库</b>把全库再考一遍，和摸底同一个分母，可直接比。
      出题器按「最弱优先」选题，读的是摸底那一列。</div></div>`;
}

function pOverview(s){
  // 空态分两种，不能混：run_log 为空 ≠ 没有任何数据。
  // 摸底/考核走 exam_log.jsonl（有意不入 run_log），跑完了照样要给看 ——
  // 否则就会出现"摸底明明跑完了，总览页却说还没有任何运行记录"。
  if(!s.has_data){
    const ex=s.exam_curve||{};
    if(!ex.has_data) return `<div class="panel"><div class="empty">${ESC(s.hint||'暂无运行记录')}</div>
    <div class="note info">面板只读 run_log.jsonl（练习），摸底/考核走 exam_log.jsonl。
      点顶部「▶ 启动 Voyager」，或先在命令行跑一次 <span class="mono">python mve/run_mve.py --llm</span>。</div></div>
    ${envCard(s)}`;
    return `<div class="note info"><b>练习记录（run_log）为空</b> —— 这是正常的：
      摸底不入 run_log。真实水平曲线在下面；下一步点「跑学习单元」，
      练习曲线和重考曲线就会一起长出来。</div>
    ${examCard(s)}
    ${envCard(s)}`;
  }
  const cov=s.series.filter(p=>p.coverage!==null).map(p=>({x:p.i,y:p.coverage,label:(p.coverage*100).toFixed(0)+'%'}));
  const mas=s.series.filter(p=>p.mastery!==null).map(p=>({x:p.i,y:p.mastery,label:p.mastery_pct}));
  const last=s.series[s.series.length-1];
  const cls={'weak':'m-weak','progress':'m-progress','good':'m-good','mastered':'m-mastered','unassessed':'m-new'};
  return `<div class="panel">
    <div class="panel__head"><h2>当前状态</h2>
      <span class="hint">判读必须看覆盖率，不能看掌握度</span></div>
    <div class="kpis">
      <div class="kpi"><div class="label">掌握度变化</div><div class="value">${ESC(s.mastery_delta)}</div></div>
      <div class="kpi"><div class="label">状态</div>
        <div class="value"><span class="chip ${cls[last.ui_status]||'m-new'}">${ESC(last.status_label)}</span></div></div>
      <div class="kpi"><div class="label">等级</div><div class="value">${ESC(last.level||'—')}</div></div>
      <div class="kpi"><div class="label">累计轮次</div><div class="value">${s.total_rounds}</div></div>
    </div>
    <div style="margin-top:10px">${chips(last.flags.map(f=>f.label),'warn')}</div>
    <div class="note ${s.reading==='learned'?'info':''}"><b>判读：</b>${ESC(s.reading_text)}</div>
  </div>
  ${examCard(s)}
  <div class="grid2">
    <div class="panel"><div class="panel__head"><h2>覆盖率轨迹 · 判据</h2></div>
      ${lineChart(cov,{color:'#2f7d57'})}
      <div class="legend">实线＝事实集命中 rubric 的加权比例，这个数才说明「做全了没有」。</div></div>
    <div class="panel"><div class="panel__head"><h2>掌握度轨迹 · 仅供参考</h2></div>
      ${lineChart(mas,{color:'#9ca3af',dash:'5 4'})}
      <div class="legend">虚线＝不可作为学习证据。</div>
      ${s.mastery_warning?`<div class="note">${ESC(s.mastery_warning)}</div>`:''}</div>
  </div>
  ${envCard(s)}`;
}

function pPractice(s){
  const r=s.recommend||{};
  const changed = window.__changedFrom && r.topic_id && r.topic_id!==window.__changedFrom;
  const recmd = r.topic_id ? `<div class="recmd ${changed?'now':''}">
      <div><span class="chip ok">出题器当前推荐</span>
        <span class="chip grey">${ESC(r.reason_label||r.reason)}</span>
        <span class="chip grey">难度 ${r.difficulty}${r.difficulty_target?` → 目标 ${r.difficulty_target}`:''}</span>
        ${r.hint?`<span class="chip grey">支架 ${ESC(r.hint)}</span>`:''}
        ${changed?`<span class="changed">本次导入后改推此题（原 ${ESC(window.__changedFrom)}）</span>`:''}</div>
      <div class="rq">${ESC(r.question)}</div>
      <div class="rw"><b>为什么是这题：</b>${ESC(r.explanation)}</div>
      ${r.difficulty_why?`<div class="rw" style="margin-top:4px"><b>难度梯度：</b>${ESC(r.difficulty_why)}</div>`:''}
      ${r.hint_label?`<div class="rw" style="margin-top:4px"><b>支架档位：</b>${ESC(r.hint)} —— ${ESC(r.hint_label)}</div>`:''}
      <div class="tm mono" style="margin-top:6px;font-size:12px;color:var(--muted)">${ESC(r.cmd||'')}</div>
    </div>` : '';

  const taskCards=(s.tasks||[]).map(t=>{
    const done=(s.attempted||{})[t.topic_id]||0;
    const wrong=(s.failed||{})[t.topic_id]||0;
    const isNow=t.topic_id===s.topic_id;
    const tag = isNow ? '<span class="chip ok">当前</span>'
      : wrong>0 ? `<span class="chip bad">错题 ${wrong}</span>`
      : done>0 ? `<span class="chip grey">已做 ${done}</span>`
      : '<span class="chip">未做</span>';
    return `<div class="taskcard ${isNow?'now':''}">
      <div class="th">${ESC(t.question)} ${tag}</div>
      <div class="tm"><span class="chip grey">难度 ${t.difficulty}</span>
        <span class="chip grey">${t.key_points.length} 个评分点</span>
        <span class="chip grey">${t.has_answer_spec} 条确定性配方</span></div>
      <div class="tm">必用工具：${(t.requires_tools||[]).map(x=>`<span class="tool">${ESC(x)}</span>`).join('')||'—'}</div>
      <div class="tm">跑法：<span class="mono">python mve/run_mve.py --llm --topic ${ESC(t.topic_id)}</span></div>
    </div>`;
  }).join('');

  const ref=s.referee||{state:'pending',facts:[]};
  const refState={ready:['ok','就绪'],pending:['warn','待跑'],empty:['bad','未产出'],unavailable:['bad','不可用']}[ref.state]||['grey',ref.state];
  const refRows=ref.facts.length?ref.facts.map(f=>
    `<tr><td class="mono">${ESC(f.subject)}</td><td class="mono">${ESC(f.dimension)}</td>
     <td class="num"><b>${ESC(f.value)}</b></td><td class="num">${f.base===null?'—':f.base}</td>
     <td class="mono">${ESC(f.source)}</td></tr>`).join('')
    :`<tr><td colspan="5" class="empty">裁判还没跑过这道题。跑一次主循环会自动生成并缓存。</td></tr>`;

  return `<div class="panel">
      <div class="panel__head"><h2>出题器 · 下一题</h2>
        <span class="hint">优先级链照伴学：错题重试 &gt; 到期复习 &gt; 人导入方向 &gt; 补工具覆盖 &gt; 推进新题</span></div>
      ${recmd||'<div class="empty">出题器不可用</div>'}
    </div>
    <div class="panel">
      <div class="panel__head"><h2>当前题目</h2></div>
      <div class="mono" style="font-size:13px">${ESC(s.question||'—')}</div>
      <div class="legend">topic_id: ${ESC(s.topic_id||'—')}</div>
    </div>
    <div class="panel">
      <div class="panel__head"><h2>裁判 VLML0 · 标准答案</h2>
        <span class="hint">原版 VLML 独立跑出，非手写</span>
        <span class="chip ${refState[0]}">${ESC(refState[1])}</span></div>
      ${ref.trajectory&&ref.trajectory.length?`<div style="margin-bottom:8px"><span class="chip grey">编排：${ref.trajectory.map(t=>ESC(t)).join(' → ')}</span></div>`:''}
      <table><thead><tr><th>subject</th><th>dimension</th><th>标准值</th><th>base</th><th>来源</th></tr></thead>
        <tbody>${refRows}</tbody></table>
      <div class="note info">Voyager 永远看不到这一列的值，只看得到自己的覆盖率。
        答完判对即停，不会硬跑满轮数。</div>
    </div>
    <div class="panel">
      <div class="panel__head"><h2>练习范围</h2><span class="hint">排序照伴学 ordered_scope_topics</span></div>
      <div class="tasks">${taskCards}</div>
    </div>`;
}

function pImport(s){
  return `<div class="panel">
    <div class="panel__head"><h2>人导入意图</h2>
      <span class="hint">人为发意图 → 影响自适应出题</span></div>
    <div class="imp">
      <div style="font-size:12.5px;color:var(--muted);margin-bottom:8px">
        写一句你想问 VLML 的话。它由<b>原版 VLML</b> 取数并解释（不经过 Voyager），
        然后作为 <b>control fact</b> 写进行动因果时间线 —— 出题器读这条线决定下一题。
      </div>
      <div style="font-size:12px;color:var(--muted);margin-bottom:8px">
        <b>收录判据</b>（不是"图谱里有没有同名维度"）：VLML 这次取数读到的表，
        有没有被图谱里<b>表内覆盖的工具集</b>（46 个 SQL 洞察）覆盖到。
        覆盖得到 → 收录，落法是<b>建边、不建节点</b>（伴学同款：材料映射不上
        不为它新建知识点）；覆盖不到 → 如实不收，也不据此出题。
      </div>
      <textarea id="q" placeholder="例：Cloud9 的手枪局到底打得怎么样？这个结论置信度够吗？"></textarea>
      <div class="imp-row">
        <select id="topic"><option value="">自动匹配（按关键词归类）</option></select>
        <button class="btn btn-primary" id="go">交给 VLML 解释</button>
        <button class="btn btn-secondary" id="clr">清空</button>
      </div>
      <div class="hit" id="hit">归类结果会在这里显示。</div>
      <div id="result"></div>
    </div>
    <h3>我的导入历史（${(s.imports||[]).length} 条）</h3>
    <div class="legend">虚线框 = comparable: false，不参与比对，也不进掌握度 —— 只进因果时间线。</div>
    <div id="implist"></div>
  </div>`;
}

function pTrace(s){
  if(!s.has_data) return '<div class="panel"><div class="empty">还没有轨迹数据。</div></div>';
  const detail=s.series.map(p=>`
    <tr><td class="num">${p.i}</td>
      <td>${p.trajectory.length?p.trajectory.map(t=>`<span class="tool ${s.new_tools.includes(t)?'new':''}">${ESC(t)}</span>`).join(''):'<span class="chip grey">未调用</span>'}</td>
      <td>${p.covered.length?chips(p.covered,'ok'):'<span class="chip grey">无</span>'}</td>
      <td>${p.missing.length?chips(p.missing,'bad'):'<span class="chip grey">无</span>'}</td>
      <td>${p.rejected.length?chips(p.rejected,'warn'):'<span class="chip grey">无</span>'}</td></tr>`).join('');
  const rows=s.series.map(p=>`
    <tr><td class="num">${p.i}</td><td class="mono">${ESC(p.mode)}</td><td class="num">${p.round}</td>
      <td><b>${ESC(p.verdict)}</b></td>
      <td class="num">${p.coverage===null?'—':(p.coverage*100).toFixed(0)+'%'}</td>
      <td class="num">${p.score===null?'—':p.score}</td>
      <td class="num">${p.facts??'—'}</td>
      <td>${chips(p.flags.map(f=>f.label),'warn')}</td>
      <td class="mono">${ESC(p.evidence)}</td>
      <td>${p.judge==='referee'?'<span class="chip ok">VLML0</span>':'<span class="chip warn">rubric</span>'}</td>
      <td>${p.no_tool_calls?'<span class="chip bad">无工具调用</span>':'<span class="chip grey">—</span>'}</td>
      <td>${p.hallucination_count>0
            ?`<span class="chip bad" title="${ESC((p.hallucinations||[]).join(' / '))}">拦下 ${p.hallucination_count} 条</span>`
            :'<span class="chip grey">—</span>'}</td></tr>`).join('');
  const withStory=s.series.filter(p=>p.narrative);
  const storyBlocks=withStory.length?withStory.slice(-2).map(p=>`
    <div class="nocmp">
      <div class="nocmp-tag">不参与比对 · comparable: false</div>
      <div class="nocmp-body">${ESC(p.narrative)}</div>
      ${p.insights.length?`<div class="nocmp-sub"><b>洞察：</b><ul>${p.insights.map(i=>`<li>${ESC(i)}</li>`).join('')}</ul></div>`:''}
      ${p.caveats.length?`<div class="nocmp-sub"><b>数据局限：</b>${p.caveats.map(c=>ESC(c)).join('；')}</div>`:''}
      <div class="nocmp-sub">第 ${p.i} 次作答 · 基于 ${p.narrative_based_on} 条<b>已通过裁判核对</b>的事实</div>
    </div>`).join('')
    :'<div class="empty">还没有叙事产出。叙事只在「有事实通过裁判核对」之后才生成——顺序不能反。</div>';

  return `<div class="panel">
      <div class="panel__head"><h2>编排轨迹</h2><span class="hint">「学会编排」的直接证据</span></div>
      <table><thead><tr><th>#</th><th>本轮调用的工具序列</th><th>已覆盖评分点</th><th>缺失</th><th>base 未过闸</th></tr></thead>
        <tbody>${detail}</tbody></table>
      ${s.new_tools.length?`<div class="note info">绿色＝相对上一轮<b>新出现</b>的工具：${s.new_tools.map(t=>ESC(t)).join('、')}。
        工具序列变长/换新，才是「编排方式真的变了」。</div>`:'<div class="legend">工具序列没有变化。</div>'}
    </div>
    <div class="panel">
      <div class="panel__head"><h2>轮次明细</h2></div>
      <table><thead><tr><th>#</th><th>模式</th><th>轮</th><th>verdict</th><th>覆盖率</th><th>得分</th>
        <th>事实数</th><th>flags</th><th>证据状态</th><th>判据</th><th>溯源</th><th>幻觉拦截</th></tr></thead>
        <tbody>${rows}</tbody></table>
    </div>
    <div class="panel">
      <div class="panel__head"><h2>叙事洞察</h2>
        <span class="hint">VLML README:97 —— 指标与证据由工具出，洞察由 LLM 出</span></div>
      ${storyBlocks}
    </div>`;
}

function pSkill(s){
  const st=s.skill_stats||{};
  const cards=(s.skill_detail||[]).map(x=>`
    <div class="taskcard" style="margin-top:8px">
      <div class="th">${ESC(x.text)}</div>
      <div class="tm"><span class="chip grey">${ESC(x.topic||'—')}</span>
        <span class="chip grey">命中 ${x.hits}</span>
        <span class="chip ${x.ok>0?'ok':'grey'}">有效 ${x.ok}</span>
        ${x.versions>1?`<span class="chip warn">已覆盖 ${x.versions} 版</span>`:''}</div>
    </div>`).join('');
  const c=s.causal||{};
  const kc={control:'ok',attempt:'',referee:'grey',narrative:'grey'};
  const tl=(c.rows||[]).length?`<div class="timeline">${(c.rows||[]).slice().reverse().map(r=>`
      <div class="tl"><span class="seq">#${r.seq}</span>
        <span class="kd"><span class="chip ${kc[r.kind]||'grey'}">${ESC(r.kind_label)}</span></span>
        <span>${ESC(r.summary)}</span>
        <span class="ts">${ESC(String(r.ts||'').replace('T',' ').slice(5,19))}</span></div>`).join('')}</div>`
    :'<div class="empty">时间线为空。</div>';
  return `<div class="panel">
      <div class="panel__head"><h2>技能库</h2><span class="hint">Voyager 自己写的教训（照 Voyager 原仓库：同名覆盖、只取 top_k）</span></div>
      <div class="legend">条目 <b>${st.skills||0}</b> · 累计写入 <b>${st.writes||0}</b> 次
        （覆盖 ${st.rewrites||0} · 丢弃 ${st.skipped||0}）·
        膨胀率 <span class="chip ${(st.bloat||1)>1?'ok':'warn'}">${st.bloat||1}</span>
        · 每题最多注入 <b>${st.top_k||5}</b> 条进 prompt</div>
      ${cards||'<div class="empty">空。Voyager 还没积累任何经验。</div>'}
    </div>
    <div class="panel">
      <div class="panel__head"><h2>行动因果时间线</h2>
        <span class="hint">control（人导入）与 attempt（Voyager 行动）在同一条单调序列上，只保序不评分</span></div>
      ${tl}
    </div>`;
}

function pConsole(s){
  const p=s.pilot||{};
  const log=ESC(p.tail||'（暂无日志）');
  return `<div class="panel">
    <div class="panel__head"><h2>驾驶舱</h2>
      <span class="hint">面板只负责起停与读日志，进程状态写在 mve/pilot_state.json</span>
      <span class="chip ${p.running?'ok':'grey'}">${p.running?'运行中':'未运行'}</span>
      ${p.job_label?`<span class="chip ${p.running?'ok':'grey'}">${ESC(p.job_label)}</span>`:''}
      ${p.mode?`<span class="chip grey">${ESC(p.mode==='loop'?'连续自适应':'单题')}</span>`:''}
      ${p.topic?`<span class="chip grey">${ESC(p.topic)}</span>`:''}
      ${p.iterations?`<span class="chip grey">已跑 ${p.iterations} 次</span>`:''}
      ${p.started_at?`<span class="chip grey">起于 ${ESC(String(p.started_at).slice(11,19))}</span>`:''}</div>
    ${p.why&&!p.running?`<div class="legend">${ESC(p.why)}</div>`:''}
    <div class="note info">loop 模式：跑完一题会<b>再问出题器要下一题</b>，一直跑到你点停止。
      选题权在出题器手里 —— 这就是「自己接自适应出题」。单题轮数是上限，判对即停。</div>
    <div class="console">${log}</div>
    <div style="margin-top:10px">
      <button class="btn btn-secondary" id="clearLog">清空日志</button>
      <button class="btn btn-secondary" id="refreshNow">立即刷新</button>
    </div>
  </div>`;
}

function envCard(s){
  const env=s.env||{}, db=s.db||{};
  const map={ready:['ok','就绪'],degraded:['warn','降级'],unavailable:['bad','不可用']};
  const [c,label]=map[env.state]||['grey',env.state];
  const tables=(env.tables!==undefined&&env.tables!==null)?env.tables:'—';
  return `<div class="panel">
    <div class="panel__head"><h2>数据源与数据库</h2>
      <span class="hint">VLML 的库是一个 .duckdb 文件；换库 = 换这个文件</span>
      <span class="chip ${c}">${ESC(label)}</span></div>
    <div style="margin-bottom:8px">
      ${env.ok?`<span class="chip grey">${tables} 张表</span>`:''}
      ${(env.events!==undefined&&env.events!==null)?`<span class="chip grey">${env.events} 条事件</span>`:''}
      ${env.usage_tips?`<span class="chip grey">${env.usage_tips} 条编排提示</span>`:''}
      ${db.is_default?'<span class="chip grey">默认库</span>':'<span class="chip warn">已切换到自定义库</span>'}
    </div>
    <div class="legend mono" style="word-break:break-all">当前库：${ESC(db.path||'—')}</div>
    ${env.reason?`<div class="note">${ESC(env.reason)}</div>`:''}

    <h3>载入新的数据库</h3>
    <div class="imp-row" style="margin-top:4px">
      <input id="dbPath" class="mono" placeholder="/绝对路径/到/另一个.duckdb"
             style="flex:1;min-width:280px;padding:8px 10px;border:1px solid var(--line);
                    border-radius:var(--radius-sm);background:#fff;color:var(--ink)">
      <button class="btn btn-secondary" id="dbProbe">验货</button>
      <button class="btn btn-primary" id="dbSwitch" disabled>切换并重启进程</button>
      <label class="legend" style="display:flex;align-items:center;gap:5px;margin:0">
        <input type="checkbox" id="dbReset"> 切换后清档
      </label>
    </div>
    <div class="hit" id="dbHit">先点「验货」：会报出几张表、几条事件、原题依赖的 series 还在不在。</div>

    <div class="note info">
      换库后**必须重启面板进程**才生效 —— VLML 的工具里写死
      <span class="mono">EventDatabase(read_only=True)</span>（不带 db_path），
      MVE 靠在 <span class="mono">vlml_env.py</span> 里打补丁换掉默认库，
      而补丁只在进程启动时装一次。
      <br><br>
      ⚠️ 已知限制：题目注册表里的
      <span class="mono">SERIES / 队伍名</span> 是写死的，换库后这些常量要重配，
      否则所有题都查不到数据。验货会替你检查这一条。
    </div>
  </div>`;
}

/* ---------------- 知识图谱 ----------------
   照伴学 knowledge_map.tsx：只读地呈现结构，不做任何推断。
   布局用**分层**（表 → 工具 → 维度，从左到右）而不是力导向 ——
   图谱小且层次天然固定，分层比力导向可读，也不会每次刷新都跳。 */
let GRAPH=null;
async function loadGraph(){
  if(GRAPH) return GRAPH;
  try{ const r=await fetch('/api/graph?t='+Date.now()); GRAPH=await r.json(); }
  catch(e){ GRAPH={empty:true,error:String(e)}; }
  return GRAPH;
}
const REL={produced_by:['#2f7d5f','产出','实线'],derived_from:['#8a94a6','由表算出','虚线'],
           confusable:['#c2703a','易混','虚线'],co_occurs:['#b9c2ce','同题共现','虚线'],
           procedure_step:['#6d5bd0','编排顺序','实线']};
function renderGraph(g){
  if(g.empty) return `<div class="empty">${ESC(g.error||'图谱为空')}</div>`;
  const nodes=g.nodes||[], edges=g.edges||[];
  const byId={}; nodes.forEach(n=>byId[n.id]=n);
  // 只画**有边**的节点：孤立工具（VLML 有 9 个但多数没被任何题用到）
  // 画进来只会让图变乱，且有边的才有信息量。
  const linked=new Set();
  edges.forEach(e=>{linked.add(e.from);linked.add(e.to);});
  const cols={table:[],tool:[],dimension:[]};
  nodes.filter(n=>linked.has(n.id)).forEach(n=>{(cols[n.kind]||(cols[n.kind]=[])).push(n);});
  ['table','tool','dimension'].forEach(k=>cols[k].sort((a,b)=>a.label.localeCompare(b.label)));
  if(!cols.dimension.length) return `<div class="empty">图谱里还没有连上边的维度</div>`;

  // 待审候选条：运行期题带来的新维度名**不占节点位**（照伴学：运行期新建的
  // 知识点不进权威层，只登记待审）。维度层只由种子题的 rubric 派生。
  const pendHtml = (g.pending_dims||[]).length ? `<div class="note info" style="margin-bottom:8px">
    <b>待审候选 ${(g.pending_dims||[]).length} 个</b>（运行期题带来的新维度，
    <b>未建节点</b>，只落在边上 —— 维度层只由种子题派生，不随出题增长）：
    ${(g.pending_dims||[]).map(p=>`<span class="chip grey">${ESC(p.dimension)}`
      + (p.nearest?` <span style="opacity:.7">≈ ${ESC(p.nearest)}</span>`:'')
      + `</span>`).join(' ')}
  </div>` : '';

  const COLW=262, ROW=30, PADX=54, PADY=26, NW=168, NH=22;
  const order=['table','tool','dimension'];
  const pos={};
  const maxN=Math.max(...order.map(k=>cols[k].length));
  const H=PADY*2+maxN*ROW, W=PADX*2+COLW*2+NW;
  order.forEach((k,i)=>{
    const items=cols[k], total=items.length*ROW;
    const top=PADY+(H-PADY*2-total)/2;
    items.forEach((n,j)=>{ pos[n.id]={x:PADX+i*COLW, y:top+j*ROW+ROW/2}; });
  });
  const colTitle=[['table','表（VLML 建模）'],['tool','工具（MCP）'],['dimension','维度']]
    .map(([k,t],i)=>`<text x="${PADX+i*COLW+NW/2}" y="${PADY-9}" font-size="11" font-weight="700"
        fill="#6b7686" text-anchor="middle">${t}</text>`).join('');

  const es=edges.filter(e=>pos[e.from]&&pos[e.to]).map(e=>{
    const a=pos[e.from], b=pos[e.to], [c,,dash]=REL[e.relation]||['#b9c2ce','','虚线'];
    const x1=a.x+NW, y1=a.y, x2=b.x, y2=b.y;
    const same=(a.x===b.x);
    const mx=(x1+x2)/2;
    // 同列（维度之间）的边走外侧弧，否则会压在节点上
    const d=same?`M${x1},${y1} C${x1+46},${y1} ${x2+46},${y2} ${x2},${y2}`
                :`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
    return `<path d="${d}" fill="none" stroke="${c}" stroke-width="${e.relation==='produced_by'?1.9:1.2}"
      ${dash==='虚线'?'stroke-dasharray="4 3"':''} opacity=".72">
      <title>${ESC(byId[e.from]&&byId[e.from].label||e.from)} → ${ESC(byId[e.to]&&byId[e.to].label||e.to)}
${ESC(REL[e.relation]&&REL[e.relation][1]||e.relation)}${e.origin==='observed'?'（实测）':''}</title></path>`;
  }).join('');

  // 维度节点可点：点了就把它设成练习范围（同「练习此知识点」按钮）
  const ns=Object.entries(pos).map(([id,p])=>{
    const n=byId[id]||{};
    const fill={dimension:'#e8f2ee',tool:'#eaf0fb',table:'#f2f0e8'}[n.kind]||'#eee';
    const stroke={dimension:'#2f7d5f',tool:'#48679c',table:'#a8976a'}[n.kind]||'#999';
    const deg=edges.filter(e=>e.from===id||e.to===id).length;
    const clickable = n.kind==='dimension'
      ? ` data-scope-set="${ESC(n.label||id)}" style="cursor:pointer"` : '';
    const isScope = n.kind==='dimension' && window.__scope && window.__scope.active
      && window.__scope.label===n.label;
    return `<g${clickable}><rect x="${p.x}" y="${p.y-NH/2}" width="${NW}" height="${NH}" rx="5"
      fill="${isScope?'#d9efe3':fill}" stroke="${stroke}" stroke-width="${isScope?2.4:1.2}"/>
      <text x="${p.x+8}" y="${p.y+4}" font-size="11" fill="#22303a">${ESC(n.label||id)}</text>
      ${isScope?'<text x="'+(p.x+NW-6)+'" y="'+(p.y+4)+'" font-size="10" fill="#2f7d5f" '
        +'text-anchor="end">练习中</text>':''}
      <title>${ESC(n.label||id)}
类型：${ESC(n.kind)}
连边：${deg}${n.detail&&n.detail.topic_id?'\n题：'+ESC(n.detail.topic_id):''}${n.detail&&n.detail.required_by?'\n被题要求：'+ESC((n.detail.required_by).join('、')):''}${n.kind==='dimension'?'\n点击设为练习范围':''}</title></g>`;
  }).join('');

  // 下方位清单：照伴学 relation_groups —— 图看结构，清单看内容
  const dims=cols.dimension.map(n=>n.label);
  const rows=dims.map(d=>{
    const prod=[...new Set(edges.filter(e=>e.to==='dim:'+d&&e.relation==='produced_by')
      .map(e=>(byId[e.from]||{}).label||e.from))];
    const tbl=[...new Set(edges.filter(e=>e.to==='dim:'+d&&e.relation==='derived_from')
      .map(e=>(byId[e.from]||{}).label||e.from))];
    const conf=[...new Set(edges.filter(e=>e.relation==='confusable'&&(e.to==='dim:'+d||e.from==='dim:'+d))
      .map(e=>(e.to==='dim:'+d?(byId[e.from]||{}):(byId[e.to]||{})).label||''))].filter(Boolean);
    const src=prod.length?`<span class="chip ok">${prod.map(ESC).join('、')}</span>`
                         :(tbl.length?`<span class="chip grey">表 ${tbl.map(ESC).join('、')}</span>`
                                     :`<span class="chip warn">无来源</span>`);
    const isNow = window.__scope && window.__scope.active && window.__scope.label===d;
    return `<tr${isNow?' class="now"':''}><td class="mono">${ESC(d)}</td><td>${src}</td>
      <td>${conf.length?conf.map(c=>`<span class="chip warn">${ESC(c)}</span>`).join(''):'<span class="chip grey">—</span>'}</td>
      <td><button class="btn ${isNow?'':'btn-secondary'}" data-scope-set="${ESC(d)}"
        style="padding:4px 10px;font-size:12px">${isNow?'✓ 练习中':'练习此知识点'}</button></td></tr>`;
  }).join('');

  const sm=g.summary||{};
  return scopeBarHtml() + pendHtml + `<div class="panel__head"><h2>维度 → 工具 → 表</h2>
      <span class="hint">${sm.dimensions||0} 维度 · ${sm.tools||0} 工具 · ${sm.tables||0} 表 · ${sm.edges||0} 边</span></div>
    <svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px;background:#fff;border-radius:8px">
      ${colTitle}${es}${ns}
    </svg>
    <div class="note info" style="margin-top:10px">判错时反馈里的「这个维度由哪个工具产出」
      就来自这张图 —— 它是 <span class="mono">answer_spec.tool</span> 声明出来的，不是模型猜的。
      孤立节点（没有任何边）不画：VLML 有 9 个工具，被真正用到的才进图。
      <br><b>点任意维度</b>（或下表按钮）把它设成练习范围，出题器就只在这范围里出题。</div>
    <table class="tbl" style="margin-top:12px">
      <thead><tr><th>维度</th><th>由谁产出</th><th>易混</th><th>练习</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
}

/* 图谱页顶部的练习范围条：照伴学 i18n
   ui.knowledge.practice_topic =「练习此知识点」
   ui.practice.next_step.choose_scope =「选择练习范围」 */
function scopeBarHtml(){
  const sc=window.__scope||{active:false};
  if(sc.active){
    return `<div class="scopebar" style="margin:0 0 14px">
      <span class="lbl">练习范围</span>
      <span class="chip ok">${ESC(sc.label)}</span>
      <span class="chip grey">${ESC((sc.topics||[]).join('、'))}</span>
      <button class="btn btn-primary" data-scope-practice="1"
        style="padding:6px 14px;font-size:13px">▶ 按此范围练习</button>
      <button class="btn btn-secondary" data-scope-clear="1"
        style="padding:6px 12px;font-size:13px">清除范围</button>
      <span class="hint">${ESC(sc.point||'')}</span>
    </div>`;
  }
  if(sc.invalidated){
    return `<div class="scopebar" style="margin:0 0 14px">
      <span class="lbl">练习范围</span>
      <span class="chip warn">已失效</span><span class="chip grey">${ESC(sc.label||'')}</span>
      <span class="hint">${ESC(sc.reason||'')} —— 请重新点一个维度</span>
    </div>`;
  }
  return `<div class="scopebar" style="margin:0 0 14px">
    <span class="lbl">练习范围</span>
    <span class="chip grey">未设置 · 出题器自选</span>
    <span class="hint">点图上任意维度或下表「练习此知识点」，驾驶舱就只在这个范围里出题</span>
  </div>`;
}
function pGraph(s){
  const id='graphBody';
  loadGraph().then(g=>{ const el=document.getElementById(id); if(el) el.innerHTML=renderGraph(g); });
  return `<div class="panel" id="${id}"><div class="empty">加载图谱…</div></div>`;
}

/* ---------------- 练习范围操作 ----------------
   事件委托：图谱是异步渲染的（loadGraph().then 才写 innerHTML），
   在 render() 里给按钮挂 onclick 会挂到空节点上，所以监听 document。 */
async function setScope(dim){
  try{
    const r=await fetch('/api/practice-scope',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'set', dim:dim})});
    const d=await r.json();
    uiEvent('practice_scope_set',
      {维度: dim, 关联题: (d.scope&&d.scope.topics||[]).join('、')||'（无）',
       revision: d.scope&&d.scope.scope_revision},
      d.ok ? ('已设为练习范围 → '+(d.scope&&d.scope.topics||[]).join('、'))
           : ('失败：'+(d.error||d.code||'未知')));
    if(!d.ok) alert('设不了：'+(d.error||'未知'));
  }catch(e){ alert('设范围失败：'+e); }
  finally{ await load(); }
}
async function clearScope(){
  try{
    const r=await fetch('/api/practice-scope',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'clear'})});
    const d=await r.json();
    uiEvent('practice_scope_clear',{原范围: d.cleared||'—'},
      d.ok?'已清除，回到出题器自选':'失败');
  }catch(e){ alert('清除失败：'+e); }
  finally{ await load(); }
}
document.addEventListener('click',e=>{
  const s=e.target.closest('[data-scope-set]');
  if(s){ setScope(s.dataset.scopeSet); return; }
  if(e.target.closest('[data-scope-clear]')){ clearScope(); return; }
  if(e.target.closest('[data-scope-practice]')){ pilotStart(); return; }
});

const PANELS={overview:pOverview,practice:pPractice,import:pImport,trace:pTrace,skill:pSkill,graph:pGraph,console:pConsole};

/* ---------------- 人导入（只在 import 分区里挂事件） ---------------- */
function fillTopics(s){
  // 驾驶舱已经没有「选题」下拉了（选题由练习范围决定），只剩人导入分区的归类下拉
  const sel=document.getElementById('topic');
  if(!sel) return;
  // dataset.loaded 挡重复填充，但**也挡住了新题**：导入生成一道新题后，
  // 下拉里永远没有它，直到手动刷新页面。改成「题数变了就重建」——
  // 平时照旧不闪（4 秒轮询不重填），新题一出现立刻能选。
  const n=(s.tasks||[]).filter(t=>t.topic_id&&t.topic_id!=='?').length;
  if(sel.dataset.loaded && String(n)===sel.dataset.taskn) return;
  const keep=sel.value;
  sel.innerHTML='<option value="">自动匹配（按关键词归类）</option>'+
    (s.tasks||[]).filter(t=>t.topic_id&&t.topic_id!=='?').map(t=>
      `<option value="${ESC(t.topic_id)}">${ESC(t.topic_id)} · 难度${t.difficulty}</option>`).join('');
  sel.value=keep; sel.dataset.loaded='1'; sel.dataset.taskn=String(n);
}

function importsList(s){
  const imps=s.imports||[];
  if(!imps.length) return '<div class="empty">还没有人工导入过。</div>';
  return imps.slice().reverse().map(im=>`
    <div class="nocmp" style="margin-top:8px">
      <div class="nocmp-tag">不参与掌握度评价 · validated_target: false</div>
      <div class="nocmp-sub" style="margin-top:0;font-size:12px">
        ${ESC(String(im.ts||'').replace('T',' '))}
        ${im.topic_id?`<span class="chip">${ESC(im.topic_id)}</span>`:'<span class="chip bad">未归入任何题</span>'}
        ${im.topic_hit?`<span class="chip grey">命中「${ESC(im.topic_hit)}」</span>`:''}
        ${(im.trajectory||[]).length?`<span class="chip grey">编排：${(im.trajectory||[]).map(t=>ESC(t)).join(' → ')}</span>`:'<span class="chip bad">未取到数据</span>'}
        ${graphLinkChips(im.graph_link)}
        <span class="chip grey">${im.facts_count} 条事实</span>
      </div>
      <div class="nocmp-body" style="margin-top:6px;font-size:13px">${ESC(im.question)}</div>
      ${im.narrative?`<div class="nocmp-sub">${ESC(im.narrative)}</div>`:''}
      ${(im.insights||[]).length?`<div class="nocmp-sub"><b>洞察：</b><ul>${im.insights.map(i=>`<li>${ESC(i)}</li>`).join('')}</ul></div>`:''}
      ${(im.caveats||[]).length?`<div class="nocmp-sub"><b>数据局限：</b>${im.caveats.map(c=>ESC(c)).join('；')}</div>`:''}
    </div>`).join('');
}

/* 人导入在图谱侧落了什么 —— 收录判据是「VLML 能不能用表内覆盖的工具集
   完成这个工作」，收录的落法是**建边、不建节点**。这一行让它在面板上可查，
   而不是只在终端日志里出现一次。 */
function graphLinkChips(gl){
  if(!gl || !Object.keys(gl).length) return '';
  if(!gl.covered)
    return '<span class="chip bad">图谱未收录：工具集覆盖不到</span>';
  return '<span class="chip ok">图谱已收录（建边，不建节点）</span>'
    + `<span class="chip grey">覆盖层 ${ESC(gl.cover_level||'')}`
    + `｜洞察 ${(gl.insights||[]).length}/${ESC(gl.insights_total||0)} 个`
    + `｜表 ${(gl.tables||[]).length} 张</span>`;
}

function renderImportResult(res){
  const box=document.getElementById('result'); if(!box) return;
  if(!res){ box.innerHTML=''; return; }
  if(!res.ok){ box.innerHTML=`<div class="note" style="margin-top:10px"><b>导入失败：</b>${ESC(res.error)}</div>`; return; }
  const facts=(res.facts||[]).map(f=>
    `<div class="fact"><b>${ESC(f.subject||'')}</b> · ${ESC(f.dimension||'')} =
       <span class="fv">${ESC(f.value)}</span>${f.base!==null&&f.base!==undefined?` <span style="color:var(--muted)">(base ${ESC(f.base)})</span>`:''}</div>`).join('');
  box.innerHTML=`<div class="note info" style="margin-top:10px">
    <div style="margin-bottom:6px">
      ${res.topic_id?`<span class="chip ok">归入 ${ESC(res.topic_id)}</span>`:'<span class="chip bad">没归入任何题 → 不驱动出题</span>'}
      ${res.topic_hit?`<span class="chip grey">命中「${ESC(res.topic_hit)}」</span>`:''}
      <span class="chip grey">validated_target: ${ESC(res.validated_target)}</span>
      ${(res.trajectory||[]).length?`<span class="chip grey">编排：${(res.trajectory||[]).map(t=>ESC(t)).join(' → ')}</span>`:'<span class="chip bad">未取到数据</span>'}
      ${graphLinkChips(res.graph_link)}
    </div>
    ${res.skipped?`<div><b>未产出解释：</b>${ESC(res.skipped)}</div>`:''}
    ${(res.errors||[]).length?`<div style="margin-top:5px"><b>工具报错：</b>${(res.errors||[]).map(e=>ESC(e)).join('；')}</div>`:''}
    ${facts?`<div style="margin-top:9px"><b>VLML 给出的事实（${(res.facts||[]).length} 条）：</b>${facts}
      <div style="font-size:12px;color:var(--muted);margin-top:5px">
        注意：这条通路的 facts <b>没有经过裁判核对</b>，维度名也是开放式自取的
        —— 它比 Voyager 那条过了裁判的事实软，不能拿来当判据。</div></div>`:''}
    ${res.narrative?`<div class="nocmp" style="margin-top:9px">
        <div class="nocmp-tag">不参与比对 · comparable: false</div>
        <div class="nocmp-body">${ESC(res.narrative)}</div>
        ${(res.insights||[]).length?`<div class="nocmp-sub"><b>洞察：</b><ul>${res.insights.map(i=>`<li>${ESC(i)}</li>`).join('')}</ul></div>`:''}
        ${(res.caveats||[]).length?`<div class="nocmp-sub"><b>数据局限：</b>${res.caveats.map(c=>ESC(c)).join('；')}</div>`:''}
      </div>`:''}
    <div style="margin-top:8px;font-size:12px">这条已写进因果时间线（control fact）。
      <b>不计入掌握度</b> —— 出题器看得见，评分看不见。</div>
  </div>`;
}

let pvTimer=null;
function preview(){
  const q=document.getElementById('q'); const hit=document.getElementById('hit');
  if(!q||!hit) return;
  const v=q.value.trim();
  if(!v){ hit.className='hit'; hit.textContent='归类结果会在这里显示。'; return; }
  fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:v})})
    .then(r=>r.json()).then(d=>{
      if(d.matched){ hit.className='hit';
        hit.innerHTML=`将归入 <b>${ESC(d.topic_id)}</b>（命中「${ESC(d.hit)}」）· ${ESC(String(d.question).slice(0,40))}…`;
      }else{ hit.className='hit no';
        hit.innerHTML=`<b>没匹配到任何题</b> —— 仍会记录并解释，但 topic_id 为空，出题器读不到，<b>不驱动出题</b>。可在下拉里手动指定。`;
      }
    }).catch(()=>{});
}

let busy=false;
async function submitImport(){
  if(busy) return;
  const q=document.getElementById('q'); if(!q) return;
  const v=q.value.trim();
  if(!v){ document.getElementById('hit').innerHTML='<b>先写一句话再提交。</b>'; return; }
  const btn=document.getElementById('go');
  busy=true; btn.disabled=true; btn.innerHTML='<span class="spin"></span>VLML 正在编排…';
  window.__changedFrom=(window.__lastRec||{}).topic_id||'';
  const sel=document.getElementById('topic');
  const picked=sel ? sel.value : '';
  try{
    const r=await fetch('/api/coach',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question:v, topic_id:picked})});
    const res=await r.json();
    renderImportResult(res);
    uiEvent('human_import',
      {问题: v.slice(0,300),
       手动指定的题: picked || '未指定（走关键词自动归类）'},
      res.ok
        ? ('归入 '+(res.topic_id||'未归入任何题')
           +(res.topic_hit?'（命中「'+res.topic_hit+'」）':'')
           +' · '+(res.facts||[]).length+' 条事实 · 编排 '+(res.trajectory||[]).join(' → ')
           +' · validated_target=false 不计掌握度')
        : ('失败：'+(res.error||'未知')));
    q.value=''; q.blur();   // 失焦：让下一轮轮询照常刷新导入历史（焦点在输入框上时渲染是被 hold 住的）
    document.getElementById('hit').textContent='归类结果会在这里显示。';
    await load();
  }catch(e){ renderImportResult({ok:false,error:String(e)}); }
  finally{ busy=false; btn.disabled=false; btn.textContent='交给 VLML 解释'; }
}

/* ---------------- 驾驶舱控制 ---------------- */
/* ---------------- 界面交互埋点 ----------------
   用户要求导出的 md「一定要具体到界面交互（按了什么键，然后选了什么难度）」。
   所以每一次按键、每一个下拉选择都落盘成 ui_events.jsonl，导出时排在最前。 */
const TAB_LABEL = {overview:'概览', practice:'练习', import:'人导入',
                   trace:'轨迹', skill:'技能·因果', graph:'知识图谱', console:'运行日志'};
function uiEvent(kind, detail, result){
  try{
    fetch('/api/ui-event',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({kind:kind, detail:detail||{}, result:result||''})});
  }catch(e){}
}

async function runJob(job, opts){
  opts=opts||{};
  const b=opts.btnId?document.getElementById(opts.btnId):null;
  if(b&&b.disabled) return;
  const body={job:job, mode:opts.mode||'once', rounds:opts.rounds||3,
              units:+(opts.units||1), transfer:+(opts.transfer||0),
              use_scope:opts.use_scope!==false};
  if(b){ b.disabled=true; b.innerHTML='<span class="spin"></span>'+ESC(opts.busyText||'启动中…'); }
  try{
    const r=await fetch('/api/pilot/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)});
    const d=await r.json();
    uiEvent('pilot_start',
      {任务:d.job_label||job, 单元:body.units, 迁移对照:body.transfer||'不考', 模式:body.mode},
      d.ok?('启动成功 pid '+d.pid+' · '+(d.job_label||job)):('失败：'+(d.error||'未知')));
    if(!d.ok){ alert('启动失败：'+(d.error||'未知')); }
    else { TAB='console'; localStorage.setItem('mve_tab',TAB); }
    await load();
  }catch(e){
    uiEvent('pilot_start',{任务:job},'异常：'+e);
    alert('启动失败：'+e);
  }
  finally{ if(b){ b.disabled=false; b.textContent=opts.btnText||'▶ 启动 Voyager'; } }
}
async function pilotStart(){
  // 没有难度 / 选题键钮了：选题由练习范围决定（伴学 onboarding.md:82）。
  //   有范围 → once：把范围里最优的一题跑透（判对就停）
  //   没范围 → loop：交给出题器连续自适应出题
  const sc=window.__scope||{active:false};
  await runJob('practice',{mode: sc.active?'once':'loop', rounds:3, use_scope:true,
    btnId:'runStart', btnText:'▶ 启动 Voyager', busyText:'启动中…'});
}
async function startPlacement(){
  const n=window.__skills||0;
  if(n>0){
    const ok=confirm('技能库里已经有 '+n+' 条程序。\n\n'
      +'摸底测的是「零基础上的真实水平」。带着技能库去考，考出来的是「练过之后的水平」，'
      +'而且会覆盖掉原来那份零基础的摸底值（画像就废了）。\n\n'
      +'点确定：先清库（自动备份到 mve/_backup_*）再摸底；点取消：不摸。');
    if(!ok) return;
    try{
      const r=await fetch('/api/reset',{method:'POST'});
      const d=await r.json();
      if(!d.ok){ alert('清库失败：'+(d.error||'未知')); return; }
      uiEvent('reset_all',{原因:'摸底前清库', 清库前技能数:n},'已清库（已备份）');
    }catch(e){ alert('清库失败：'+e); return; }
  }
  await runJob('placement',{btnId:'btnPlacement',
    btnText:'摸底（全库撤图谱考一遍）', busyText:'摸底中…'});
}
/* 结业考：和摸底**前提相反** —— 摸底要空库（测零基础），结业考要有库
   （测"学到现在，撤掉支架还剩多少"）。它落盘 kind="final"，不覆盖摸底基线，
   所以摸底那一列跑完结业考后还在。 */
async function startFinal(){
  const n=window.__skills||0;
  if(!n){
    alert('技能库是空的 —— 结业考测的是「学到现在还剩多少」，没学就没得考。\n\n'
      +'先跑「学习单元」攒技能，或先点「摸底」测零基础水平。');
    return;
  }
  await runJob('final',{btnId:'btnFinal',
    btnText:'结业考（带技能库再考一遍）', busyText:'结业考中…'});
}
async function pilotStop(){
  const b=document.getElementById('runStop'); if(b.disabled) return;
  b.disabled=true; b.textContent='停止中…';
  const p=window.__pilot||{};
  try{
    await fetch('/api/pilot/stop',{method:'POST'});
    await load();
    uiEvent('pilot_stop',
      {停止时模式: p.mode||'—', 已跑题数: p.iterations||0, 选题: p.topic||'—'},
      '已停止');
  }
  catch(e){ alert('停止失败：'+e); }
  finally{ b.disabled=false; b.textContent='■ 停止'; }
}

/* ---------------- 主渲染 ---------------- */
function render(s){
  window.__lastRec = s.recommend || {};
  window.__pilot   = s.pilot || {};
  window.__skills  = (s.skill_stats||{}).skills || 0;
  // topic_id -> 难度（埋点里要把"选了什么难度"记下来）
  window.__diff = {};
  (s.tasks||[]).forEach(t=>{ if(t.topic_id) window.__diff[t.topic_id]=t.difficulty; });
  window.__difficultyOf = id => window.__diff[id];
  renderHero(s);
  renderNav(s);
  const stage=document.getElementById('stage');
  // ---- 有人在操作表单控件时，不能整页重建 ----
  // 轮询每 4 秒 render 一次，stage.innerHTML 一换，所有控件的值都会被打回
  // HTML 里的默认值。实测中招的控件（同一根因，一次修完）：
  //   learnUnits / learnTransfer —— 永远弹回 4 和 3，选不了别的
  //   q          —— 打到一半的问题消失（上一轮修的）
  //   topic      —— 人导入手动指定的归类被清回「自动匹配」
  //   dbPath     —— 数据库切换的路径输入被清空
  //   刚出的解释结果（#result）也被洗掉 —— 提交进行中（busy）同样跳过。
  // 人把焦点挪开（点了页面别处）后，下一轮轮询照常刷新，数据不会少。
  const af=document.activeElement;
  const FORM=['SELECT','INPUT','TEXTAREA'];
  const holdingForm = af && af.tagName && FORM.includes(af.tagName)
                      && stage.contains(af);
  if(!(TAB==='import' && busy) && !holdingForm){
    // 即便重建，也把用户改过的控件值带过去 —— 焦点在按钮上时轮询照常刷，
    // 不能因为刷新把人刚选的单元数/迁移间隔/草稿重置掉。
    const vals={}, afId=(af&&af.id&&stage.contains(af))?af.id:null;
    stage.querySelectorAll('select[id],input[id],textarea[id]')
      .forEach(el=>{ vals[el.id]=el.value; });
    stage.innerHTML=(PANELS[TAB]||pOverview)(s);
    Object.entries(vals).forEach(([id,v])=>{
      const n=document.getElementById(id);
      if(n && v) n.value=v;
    });
    if(afId){
      const n=document.getElementById(afId);
      if(n && n.tagName && FORM.includes(n.tagName)) n.focus();
    }
    fillTopics(s);          // 重建后 #topic 是新元素，得重新填（在恢复值之后跑，keep 才保得住）
    const il=document.getElementById('implist');
    if(il) il.innerHTML=importsList(s);

    const go=document.getElementById('go'); if(go) go.onclick=submitImport;
    const clr=document.getElementById('clr'); if(clr) clr.onclick=()=>{
      document.getElementById('q').value='';
      document.getElementById('result').innerHTML='';
      document.getElementById('hit').textContent='归类结果会在这里显示。';
    };
    const qq=document.getElementById('q');
    if(qq){
      qq.addEventListener('input',()=>{clearTimeout(pvTimer);pvTimer=setTimeout(preview,350);});
      qq.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter')submitImport();});
    }
  }
  const cl=document.getElementById('clearLog'); if(cl) cl.onclick=async()=>{
    await fetch('/api/pilot/clear-log',{method:'POST'});
    uiEvent('clear_log',{},'运行日志已清空');
    await load();
  };
  const bp=document.getElementById('btnPlacement');
  if(bp) bp.onclick=startPlacement;
  const bf=document.getElementById('btnFinal');
  if(bf) bf.onclick=startFinal;
  const bl=document.getElementById('btnLearn');
  if(bl) bl.onclick=()=>runJob('learn',{btnId:'btnLearn', btnText:'跑学习单元',
    busyText:'学习中…',
    units:+((document.getElementById('learnUnits')||{}).value||4),
    transfer:+((document.getElementById('learnTransfer')||{}).value||0)});
  const bes=document.getElementById('btnExamStop'); if(bes) bes.onclick=pilotStop;
  const rn=document.getElementById('refreshNow'); if(rn) rn.onclick=load;
  const ex=document.getElementById('btnExport'); if(ex) ex.onclick=exportMd;
  const dp=document.getElementById('dbProbe'); if(dp) dp.onclick=dbProbe;
  const ds=document.getElementById('dbSwitch');
  if(ds){ ds.onclick=dbSwitch; ds.disabled=!dbProbedOk; }
}

/* ---------------- 数据库切换：先验货，再切换 ----------------
   验货必须在切换**之前**：切过去才发现是空库/错库，人已经不知道刚才的数据去哪了。 */
let dbProbedOk=false;
async function dbProbe(){
  const hit=document.getElementById('dbHit'); if(!hit) return;
  const p=document.getElementById('dbPath').value.trim();
  hit.className='hit'; hit.innerHTML='验货中…';
  try{
    const r=await fetch('/api/db/probe',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:p})});
    const d=await r.json();
    dbProbedOk=!!d.ok;
    document.getElementById('dbSwitch').disabled=!dbProbedOk;
    if(!d.ok){ hit.className='hit no'; hit.innerHTML=`<b>不能用：</b>${ESC(d.error||'未知')}`; return; }
    hit.innerHTML=`能用 · <b>${d.tables.length}</b> 张表`
      + ((d.events!==undefined&&d.events!==null)?` · <b>${d.events}</b> 条事件`:'')
      + (d.series_ids&&d.series_ids.length?` · series: ${d.series_ids.map(x=>ESC(x)).join(', ')}`:'')
      + (d.warning?`<br><b style="color:#8a2015">⚠ ${ESC(d.warning)}</b>`:'');
  }catch(e){ hit.className='hit no'; hit.innerHTML='验货失败：'+ESC(e); }
}
async function dbSwitch(){
  const b=document.getElementById('dbSwitch'); if(!b||b.disabled) return;
  const p=document.getElementById('dbPath').value.trim();
  const reset=document.getElementById('dbReset')?.checked;
  if(!confirm((p?('切换到：'+p):'回到默认库')
      +(reset?'，并清空全部学习数据':'')
      +'\n\n切换后需要重启面板进程才生效。继续？')) return;
  b.disabled=true;
  try{
    const r=await fetch('/api/db/switch',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:p, reset:reset})});
    const d=await r.json();
    const hit=document.getElementById('dbHit');
    hit.className='hit';
    hit.innerHTML = d.ok
      ? `<b>已写入配置</b>：${ESC(d.db_path)}<br>现在请重启面板进程：
         <span class="mono">lsof -ti:8777 | xargs kill; python mve/dashboard.py</span>`
      : `<b>失败：</b>${ESC(d.error||'未知')}`;
    await load();
  }catch(e){ document.getElementById('dbHit').innerHTML='切换失败：'+ESC(e); }
  finally{ b.disabled=false; }
}

async function exportMd(){
  const b=document.getElementById('btnExport'); if(!b||b.disabled) return;
  b.disabled=true; const old=b.textContent; b.textContent='导出中…';
  try{
    const r=await fetch('/api/export',{method:'POST'});
    const d=await r.json();
    if(!d.ok){ alert('导出失败：'+(d.error||'未知')); return; }
    const blob=new Blob([d.markdown],{type:'text/markdown;charset=utf-8'});
    const a=document.createElement('a');
    a.href=URL.createObjectURL(blob); a.download=d.filename;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(a.href);
  }catch(e){ alert('导出失败：'+e); }
  finally{ b.disabled=false; b.textContent=old; }
}

async function load(){
  try{
    const r=await fetch('/api/state?t='+Date.now());
    render(await r.json());
  }catch(e){
    document.getElementById('stage').innerHTML='<div class="panel"><div class="empty">加载失败：'+ESC(e)+'</div></div>';
  }
}

document.getElementById('nav').addEventListener('click',e=>{
  const b=e.target.closest('[data-tab]'); if(!b) return;
  const from=TAB; TAB=b.dataset.tab; localStorage.setItem('mve_tab',TAB);
  uiEvent('tab_switch',{从: TAB_LABEL[from]||from, 切到: TAB_LABEL[TAB]||TAB}, '');
  load();
});
document.getElementById('runStart').onclick=pilotStart;
document.getElementById('runStop').onclick=pilotStop;

load();
setInterval(load, 4000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _payload(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        path = _strip_prefix(self.path.split("?")[0])
        if path in ("/", "/index.html"):
            body = _html().encode("utf-8")
            ctype = "text/html; charset=utf-8"
        elif path == "/api/state":
            body = panel_data.as_json().encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif path == "/api/graph":
            body = _graph_json().encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif path == "/api/practice-scope":
            body = _scope_json().encode("utf-8")
            ctype = "application/json; charset=utf-8"
        else:
            self.send_error(404)
            return
        self._send(200, body, ctype)

    def do_POST(self) -> None:  # noqa: N802
        """写接口。分两组：

        人发意图：/api/preview（只归类，不拉 VLML）、/api/coach（真跑）
        驾驶舱  ：/api/pilot/start|stop|clear-log
        """
        path = _strip_prefix(self.path.split("?")[0])
        payload = self._payload()

        if path == "/api/preview":
            try:
                import tasks
                topic, hit = tasks.infer_topic(str(payload.get("question") or ""))
                task = tasks.TASKS.get(topic)
                self._json(200, {
                    "topic_id": topic, "hit": hit, "matched": bool(topic),
                    "question": task.question if task else "",
                    "difficulty": task.difficulty if task else 0,
                })
            except Exception as e:
                self._json(500, {"matched": False, "error": f"{type(e).__name__}: {e}"[:200]})
            return

        if path == "/api/coach":
            q = str(payload.get("question") or "").strip()
            if not q:
                self._json(400, {"ok": False, "error": "问题是空的"})
                return
            try:
                import asyncio

                import coach  # 会拉起 VLML 环境（chdir + 打补丁），按需加载
                res = asyncio.run(coach.explain(q, topic_id=str(payload.get("topic_id") or "")))
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
                return
            facts = [
                {
                    "subject": "/".join(f"{k}={v}" for k, v in (f.get("subject") or {}).items()),
                    "dimension": f.get("dimension"),
                    "value": f.get("value"),
                    "base": f.get("base"),
                    "unit": f.get("unit"),
                }
                for f in (res.get("facts") or [])
            ]
            self._json(200, {"ok": True,
                             **{k: v for k, v in res.items() if k != "facts"},
                             "facts": facts})
            return

        if path == "/api/db/probe":
            try:
                import db_switch
                import ui_events
                p = str(payload.get("path") or "")
                pr = db_switch.probe(p)
                # 验货也要留痕：换库是唯一一个「点了之后可能全盘查不到数据」的操作，
                # 出问题时人要能回看当时体检到的是什么。
                ui_events.append(
                    ui_events.DB_PROBE,
                    detail={"目标库": pr.get("path") or "（默认库）",
                            "体检": (f"{len(pr.get('tables') or [])} 张表"
                                     f" · {pr.get('events')} 条事件")
                                    if pr.get("ok") else (pr.get("error") or "失败"),
                            "series 检查": pr.get("warning") or "原题 series 仍在库里"},
                    result="能换" if pr.get("ok") else "不能换",
                )
                self._json(200, pr)
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
            return

        if path == "/api/db/switch":
            try:
                import db_switch
                import ui_events
                p = str(payload.get("path") or "")
                res = db_switch.write(p)
                detail = {"切换到": res["db_path"]}
                if res["is_default"]:
                    detail["说明"] = "已回到默认库"
                if payload.get("reset"):
                    try:
                        import reset_all
                        import subprocess
                        subprocess.run([sys.executable, str(ROOT / "reset_all.py")],
                                       capture_output=True, timeout=60)
                        detail["顺带"] = "已清档"
                    except Exception:
                        detail["顺带"] = "清档失败（数据库已切换）"
                ui_events.append(ui_events.DB_SWITCH, detail=detail, result="ok")
                self._json(200, {**res, "need_restart": True})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/practice-scope":
            # set = 把图谱上的某个维度设成练习范围；clear = 回到出题器自选
            # 形态照伴学 study_set_practice_scope：canonical 校验 + revision 递增
            try:
                import practice_scope
                import ui_events
                if str(payload.get("action") or "") == "clear":
                    res = practice_scope.clear_scope()
                    ui_events.append(ui_events.PRACTICE_SCOPE_CLEAR,
                                     detail={"原范围": res.get("cleared") or "—"},
                                     result="已清除，回到出题器自选")
                    self._json(200, {**res, "scope": practice_scope.get_scope()})
                    return
                dim = str(payload.get("dim") or "")
                scope = practice_scope.set_scope(dim)
                ui_events.append(
                    ui_events.PRACTICE_SCOPE_SET,
                    detail={"维度": dim,
                            "关联题": "、".join(scope.get("topics") or []),
                            "revision": scope.get("scope_revision")},
                    result="范围已生效 → " + "、".join(scope.get("topics") or []),
                )
                self._json(200, {"ok": True, "scope": scope})
            except Exception as e:
                code = str(getattr(e, "code", "") or "PRACTICE_SCOPE_ERROR")
                try:
                    import practice_scope
                    cur = practice_scope.get_scope()
                except Exception:
                    cur = {"active": False}
                self._json(200, {"ok": False, "code": code,
                                 "error": f"{type(e).__name__}: {e}"[:300],
                                 "scope": cur})
            return

        if path == "/api/ui-event":
            try:
                import ui_events
                ev = ui_events.append(
                    str(payload.get("kind") or "unknown"),
                    detail=payload.get("detail") or {},
                    result=str(payload.get("result") or ""),
                )
                self._json(200, {"ok": True, "seq": ev["seq"]})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
            return

        if path == "/api/export":
            try:
                import export_md
                import ui_events
                md = export_md.build_markdown()
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                ui_events.append(ui_events.EXPORT,
                                 detail={"字符数": len(md)},
                                 result=f"mve_export_{stamp}.md")
                self._json(200, {"ok": True, "markdown": md,
                                 "filename": f"mve_export_{stamp}.md"})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/pilot/start":
            # topic 一律留空：选题权在练习范围（或出题器）手里，不在驾驶舱键钮手里。
            # 面板上因此没有难度 / 选题下拉，只剩开始 / 停止。
            self._json(200, pilot.start(
                mode=str(payload.get("mode") or "once"),
                topic="",
                rounds=int(payload.get("rounds") or 3),
                use_scope=bool(payload.get("use_scope", True)),
                # 三种任务：practice（练题）/ placement（摸底）/ learn（学习单元）
                job=str(payload.get("job") or "practice"),
                units=int(payload.get("units") or 1),
                transfer=int(payload.get("transfer") or 0),
            ))
            return

        if path == "/api/pilot/stop":
            self._json(200, pilot.stop())
            return

        if path == "/api/pilot/clear-log":
            pilot.clear_log()
            self._json(200, {"ok": True})
            return

        # 清档：摸底前要先把技能库清空（否则考出来的是"练过之后的水平"）。
        # reset_all 自己会先备份到 mve/_backup_* 再删，所以这个键钮不额外加确认。
        if path == "/api/reset":
            try:
                import reset_all
                _old = sys.argv[:]
                sys.argv = ["reset_all.py"]       # reset_all.main() 自己读 argv
                try:
                    reset_all.main()
                finally:
                    sys.argv = _old
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
            return

        self.send_error(404)

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        pass


def main() -> None:
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"MVE 面板已启动：http://{HOST}:{PORT}")
    print("停止：Ctrl-C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
