#!/usr/bin/env python3
"""MVE 对话页（/mve/chat）——完全照 DeepSeek R1 客户端的形态构建。

为什么单独一个模块：
  dashboard.py 已经 3900 行，再塞 800 行前端会把路由和页面搅在一起。
  这里只放「页面 + 把 MVE 的执行过程翻译成 R1 那段思考」两件事，
  路由仍归 dashboard（GET /chat → chat_page.render(PREFIX)）。

与 R1 客户端的同构关系（不是"看起来像"，是逐个功能对位）：
  R1 的 <think> 折叠块     ↔ MVE 的执行轨迹（供给线/命中技能/回填的 SQL/
                              实查行数/核对结论/判定）——同一件事：把"怎么想的"
                              收进折叠区，用户只看可验证的结论。
  R1 的 delta.reasoning_content ↔ 本页 SSE 里的 reasoning_content 通道
                              （DeepSeek 官方 API 的字段名，照抄不改名）。
  R1 的「深度思考」开关     ↔ 同一个开关：关掉就不显示轨迹，只留答案。
  会话历史 / 新对话 / 复制 / 重新生成 / 停止生成 —— 逐个都有。

铁律：这个页面**不算任何判定**。判定、掌握、复用技能全部读服务端
x_mve / reasoning_content，前端只负责显示（否则就长出第二把尺子）。
"""

from __future__ import annotations

import html
import json
from typing import Any


# ---------------------------------------------------------------- 思考轨迹

def _kv(name: str, value: Any) -> str:
    """一行「名字：值」，空值不占行。"""
    if value is None or value == "" or value == [] or value == {}:
        return ""
    return f"{name}：{value}"


def reasoning_text(res: dict[str, Any] | None, *, scene_id: str = "",
                   scene_label: str = "") -> str:
    """把 consume() 的结果翻译成 R1 式的「思考过程」（纯文本，前端等宽显示）。

    ⚠️ 这里只做**转述**：每条都来自 res 里已有的字段，不推断、不补数。
    拿不到的就写"没走到"，让这段轨迹本身也是可核对的。
    """
    if not isinstance(res, dict):
        return ""
    L: list[str] = []
    where = scene_id or str(res.get("scene") or res.get("root_scene") or "")
    if where and scene_label:
        where = f"{where}（{scene_label}）"

    line = str(res.get("scope_line") or "")
    at = f"场景 {where}" if where else "当前场景"
    if line == "learn":
        L.append(f"先判这句话归哪条供给线：{at}有环境、答案可复现，"
                 "走学习线（学一次反复兑现）。")
    elif line == "tool":
        L.append(f"这句话的环境里没有可复现的答案（{at}），"
                 "走工具线：联网取事实，只能算 claim，不入库。")
    elif line:
        L.append(f"没有可指认的环境（{at}），"
                 "这条只能算 inference：模型推的，不写图。")

    # 规划披露（2026-10-29）：先规划、再逐路执行。规划≠可达，web 实际
    # 执行结果看下面工具线那段 —— 这里只转述 consume 返回的字段。
    _pr = res.get("plan_routes")
    if isinstance(_pr, dict) and any(bool(v) for v in _pr.values()):
        _names = " + ".join(k for k in ("db", "web", "file", "infer")
                            if _pr.get(k))
        _pwhy = str(res.get("plan_why") or "").strip()
        L.append(f"这一问的规划：走 {_names} 路"
                 + (f"（{_pwhy[:120]}）" if _pwhy else "") + "。")

    used = res.get("used_skills") or []
    if used:
        names = []
        for s in used[:6]:
            nm = str(s.get("name") or s.get("topic") or "")
            r = s.get("r")
            names.append(f"{nm}（r={r}）" if r not in (None, "") else nm)
        L.append(f"技能库里命中 {len(used)} 条已学配方：{'、'.join(names)}。"
                 f"{str(res.get('reuse_note') or '')}".strip())
    elif res.get("skills_found"):
        L.append("技能库里有相关条目，但这次没有一条能直接回填使用。")
    else:
        L.append("技能库里没有命中任何配方，这一问是现做的（做完若通过会被泛化入库）。")

    sql = str(res.get("sql") or "").strip()
    if sql:
        L.append("回填出的查询：\n" + sql)

    rows = res.get("rows")
    if rows is not None:
        real = res.get("real_rows")
        tail = "" if real in (None, rows) else f"，其中真数 {real} 行"
        L.append(f"实查返回 {rows} 行{tail}" +
                 ("（全是真空值，不算取到数）。" if res.get("vacuous") else "。"))

    errs = [str(e) for e in (res.get("errors") or []) if str(e).strip()]
    if errs:
        L.append("执行过程中报过错：" + "；".join(errs[:3])[:300])

    critic = str(res.get("critic_note") or "").strip()
    if critic:
        L.append("核对没过：" + critic[:300])

    if res.get("referee_compared"):
        verdict = "口径一致，做法没有硬伤" if res.get("voyager_sound") \
            else "裁判对照判出硬伤"
        if not res.get("same_metric"):
            verdict = "两边算的不是同一个量，这一问没真正判到"
        L.append(f"独立对照（裁判那一路）：{verdict}。"
                 + (str(res.get("method_reason") or "")[:200]))

    jr = str(res.get("judgment_result") or "")
    if jr:
        if jr == "claim":
            # 工具线答出来的：有答案但**不是**学到的能力（联网资料，未验证）。
            # 不能沿用「未掌握」那行 —— 那读起来像没答出来（2026-10-29 实测）。
            L.append("判定：claim（联网取回、未经库内验证）；"
                     "不进能力账、不计学习曲线。")
        else:
            m = "掌握 ✓" if res.get("mastered") else "未掌握"
            L.append(f"判定：{jr}；{m}。")
    if res.get("learn_kicked"):
        L.append("这道题我还没学会 —— 已自动拉起后台学习（同构 R1「答不了"
                 "就进入思考」：MVE 答不了数据题就进入学习），学完再问同一句"
                 "即可复用。")
    elif res.get("learn_kick_note"):
        L.append(f"学习状态：{str(res['learn_kick_note'])[:160]}")
    if res.get("inference_used"):
        L.append("这一答来自模型自身知识（inference 线）：未经环境验证，"
                 "不入库、不计入学习曲线。")
    if res.get("skipped"):
        L.append("跳过原因：" + str(res.get("skipped"))[:300])
    if res.get("tool_line_used"):
        srcs = res.get("tool_sources") or []
        L.append(f"工具线取回 {len(srcs)} 个来源（claim，不入库、不计曲线）。")
    else:
        # 联网这半场走了但没答成（如 grounded=false）也要如实转述 ——
        # 不披露就会被读成「没触发联网」（2026-10-29 用户实测误解）。
        _tln = str(res.get("tool_line_note") or "").strip()
        if _tln:
            L.append(f"联网这半场的执行结果：{_tln[:160]}")
    note = str(res.get("answer_note") or "").strip()
    if note:
        L.append("叙述说明：" + note[:200])
    return "\n".join(x for x in L if x.strip())


def chat_title(question: str, limit: int = 26) -> str:
    """会话标题（R1 客户端就是拿第一条提问当标题）。"""
    q = " ".join(str(question or "").split())
    return (q[:limit] + "…") if len(q) > limit else (q or "新对话")


# ---------------------------------------------------------------- 页面

def render(prefix: str = "") -> str:
    """返回对话页 HTML。prefix 是反向代理子路径（如 /mve），用于拼接口地址。"""
    page = CHAT_HTML
    if prefix:
        page = page.replace(
            "<head>", '<head>\n<script>window.MVE_PREFIX="%s";</script>' % prefix, 1)
    return page


CHAT_HTML = r"""<!doctype html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MVE 对话</title>
<style>
:root{
  --accent:#4d6bfe; --accent-soft:rgba(77,107,254,.16);
  --ok:#3fb950; --warn:#d29922; --bad:#f85149;
  --radius:14px;
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",
              "Segoe UI",Roboto,system-ui,sans-serif;
}
html[data-theme="dark"]{
  color-scheme:dark;
  --bg:#191a1e; --side:#141519; --panel:#1f2025; --panel-2:#25262c;
  --line:#2b2c33; --ink:#e8eaed; --dim:#9aa0a6; --dim2:#6b7280;
  --bubble:#2b2d34; --code:#15161a;
}
html[data-theme="light"]{
  color-scheme:light;
  --bg:#ffffff; --side:#f7f8fa; --panel:#f3f4f6; --panel-2:#e9ebef;
  --line:#e3e5ea; --ink:#1b1f24; --dim:#5f6672; --dim2:#8b919b;
  --bubble:#eef2ff; --code:#f5f6f8;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);display:flex;overflow:hidden;
     -webkit-font-smoothing:antialiased}
button{font-family:inherit}

/* ---------- 侧栏（DeepSeek 客户端左侧那一列） ---------- */
.side{width:260px;flex:0 0 260px;background:var(--side);border-right:1px solid var(--line);
      display:flex;flex-direction:column;min-height:0}
.brand{display:flex;align-items:center;gap:9px;padding:16px 16px 10px}
.brand .logo{width:26px;height:26px;border-radius:8px;background:var(--accent);color:#fff;
      display:grid;place-items:center;font-size:13px;font-weight:700}
.brand b{font-size:15px;letter-spacing:.2px}
.brand span{font-size:11px;color:var(--dim2);margin-left:auto}
.newchat{margin:6px 12px 10px;padding:10px 12px;border:1px solid var(--line);
      border-radius:12px;background:var(--panel);color:var(--ink);cursor:pointer;
      display:flex;align-items:center;gap:8px;font-size:13.5px;width:calc(100% - 24px)}
.newchat:hover{background:var(--panel-2)}
.newchat .plus{font-size:15px;line-height:1}
.hist{flex:1;overflow-y:auto;padding:4px 8px 8px}
.hist .grp{font-size:11px;color:var(--dim2);padding:10px 8px 4px}
.item{display:flex;align-items:center;gap:6px;padding:8px 10px;border-radius:10px;
      font-size:13px;color:var(--ink);cursor:pointer;overflow:hidden}
.item:hover{background:var(--panel)}
.item.on{background:var(--panel-2)}
.item .t{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item .del{opacity:0;border:0;background:transparent;color:var(--dim);cursor:pointer;
      font-size:13px;padding:0 2px}
.item:hover .del{opacity:1}
.sidefoot{border-top:1px solid var(--line);padding:10px 12px;display:flex;align-items:center;
      gap:8px;font-size:12px;color:var(--dim)}
.sidefoot .dot{width:7px;height:7px;border-radius:50%;background:var(--ok)}
.sidefoot .theme{margin-left:auto;border:0;background:transparent;color:var(--dim);
      cursor:pointer;font-size:15px}

/* ---------- 主区 ---------- */
.main{flex:1;min-width:0;display:flex;flex-direction:column;min-height:0}
.top{display:flex;align-items:center;gap:10px;padding:10px 18px;border-bottom:1px solid var(--line);
     background:var(--bg)}
.model{position:relative}
.model > button{display:flex;align-items:center;gap:6px;border:0;background:transparent;
      color:var(--ink);font-size:15px;font-weight:600;cursor:pointer;padding:6px 8px;border-radius:10px}
.model > button:hover{background:var(--panel)}
.model .chev{color:var(--dim);font-size:11px}
.menu{position:absolute;top:38px;left:0;min-width:260px;background:var(--panel);
      border:1px solid var(--line);border-radius:12px;padding:6px;z-index:20;
      box-shadow:0 12px 32px rgba(0,0,0,.28);display:none}
.menu.open{display:block}
.menu .opt{padding:8px 10px;border-radius:8px;font-size:13px;cursor:pointer;display:flex;gap:8px}
.menu .opt:hover{background:var(--panel-2)}
.menu .opt .id{font-family:ui-monospace,Menlo,Consolas,monospace;color:var(--dim);font-size:12px}
.thinktoggle{margin-left:auto;display:flex;align-items:center;gap:7px;font-size:13px;color:var(--dim)}
.sw{width:38px;height:21px;border-radius:11px;background:var(--panel-2);border:1px solid var(--line);
    position:relative;cursor:pointer;transition:background .18s}
.sw i{position:absolute;top:2px;left:2px;width:15px;height:15px;border-radius:50%;
     background:var(--dim);transition:all .18s}
.sw.on{background:var(--accent-soft);border-color:var(--accent)}
.sw.on i{left:20px;background:var(--accent)}

.scroll{flex:1;overflow-y:auto;min-height:0;scroll-behavior:smooth}
.thread{max-width:790px;margin:0 auto;padding:26px 20px 120px;display:flex;
        flex-direction:column;gap:22px}
.empty{max-width:790px;margin:8vh auto 0;padding:0 20px;text-align:center;color:var(--dim)}
.empty h2{font-size:22px;color:var(--ink);margin:0 0 8px;font-weight:600}
.empty p{font-size:13px;line-height:1.9;margin:0 auto;max-width:560px}
.empty .chips{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin-top:20px}
.empty .chip{border:1px solid var(--line);background:var(--panel);color:var(--ink);
      padding:8px 12px;border-radius:999px;font-size:12.5px;cursor:pointer}
.empty .chip:hover{border-color:var(--accent);color:var(--accent)}

.msg{display:flex;flex-direction:column;gap:8px}
.msg.me{align-items:flex-end}
.msg.me .txt{max-width:78%;background:var(--bubble);padding:10px 14px;border-radius:16px;
      font-size:14.5px;line-height:1.7;white-space:pre-wrap;word-break:break-word}
.msg.ai .txt{font-size:14.8px;line-height:1.85}
.msg.ai .txt p{margin:0 0 10px}
.msg.ai .txt p:last-child{margin-bottom:0}
.msg.ai .txt h3,.msg.ai .txt h4{margin:14px 0 8px;font-size:15px}
.msg.ai .txt code{background:var(--code);border:1px solid var(--line);border-radius:5px;
      padding:1px 5px;font-size:13px;font-family:ui-monospace,Menlo,Consolas,monospace}
.msg.ai .txt pre{background:var(--code);border:1px solid var(--line);border-radius:10px;
      padding:10px 12px;overflow-x:auto;margin:10px 0}
.msg.ai .txt pre code{border:0;background:transparent;padding:0;font-size:12.5px;line-height:1.6}
.cur{display:inline-block;width:7px;height:15px;background:var(--accent);vertical-align:-2px;
     margin-left:2px;animation:blink 1s steps(1) infinite}
@keyframes blink{50%{opacity:0}}

.think{border:1px solid var(--line);background:var(--panel);border-radius:12px;
       overflow:hidden;max-width:100%}
.think .hd{display:flex;align-items:center;gap:8px;padding:9px 12px;cursor:pointer;
       font-size:12.8px;color:var(--dim);user-select:none}
.think .hd:hover{background:var(--panel-2)}
.think .hd .arw{transition:transform .16s;font-size:10px}
.think.open .hd .arw{transform:rotate(90deg)}
.think .bd{display:none;padding:0 14px 12px;font-size:12.6px;line-height:1.75;
       color:var(--dim);white-space:pre-wrap;word-break:break-word;
       font-family:ui-monospace,Menlo,Consolas,"PingFang SC",monospace}
.think.open .bd{display:block}
.think .bd .lbl{color:var(--ink)}

.meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:2px}
.tag{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);
     color:var(--dim);background:var(--panel)}
.tag.ok{color:var(--ok);border-color:rgba(63,185,80,.35);background:rgba(63,185,80,.1)}
.tag.warn{color:var(--warn);border-color:rgba(210,153,34,.35);background:rgba(210,153,34,.1)}
.tag.bad{color:var(--bad);border-color:rgba(248,81,73,.35);background:rgba(248,81,73,.1)}
.acts{display:flex;gap:4px;margin-top:2px}
.acts button{border:0;background:transparent;color:var(--dim2);cursor:pointer;
     padding:4px 6px;border-radius:7px;font-size:12px}
.acts button:hover{background:var(--panel);color:var(--ink)}

.dock{padding:0 20px 14px;background:linear-gradient(180deg,transparent,var(--bg) 24%)}
.composer{max-width:790px;margin:0 auto;border:1px solid var(--line);background:var(--panel);
      border-radius:20px;padding:10px 10px 8px 14px;display:flex;flex-direction:column;gap:6px;
      transition:border-color .15s}
.composer:focus-within{border-color:var(--accent)}
.composer textarea{width:100%;border:0;outline:0;background:transparent;color:var(--ink);
      resize:none;font-family:inherit;font-size:14.5px;line-height:1.7;max-height:180px;
      min-height:26px;padding:2px 0}
.composer .row{display:flex;align-items:center;gap:8px}
.composer .attach{width:30px;height:30px;border-radius:50%;border:1px solid var(--line);
  background:var(--panel-2);color:var(--ink);cursor:pointer;font-size:14px;line-height:1}
.composer .attach:hover{border-color:var(--accent)}
.composer .attach.up{opacity:.5;cursor:default}
.composer .hint{font-size:11.5px;color:var(--dim2)}
.composer .send{margin-left:auto;width:34px;height:34px;border-radius:50%;border:0;
      background:var(--accent);color:#fff;font-size:16px;cursor:pointer;display:grid;
      place-items:center}
.composer .send:disabled{background:var(--panel-2);color:var(--dim2);cursor:default}
.composer .stop{display:none;margin-left:auto;padding:0 14px;height:34px;border-radius:17px;
      border:1px solid var(--line);background:var(--panel-2);color:var(--ink);cursor:pointer;
      font-size:12.5px}
.busy .composer .send{display:none}
.busy .composer .stop{display:block}
.disclaim{max-width:790px;margin:8px auto 0;text-align:center;font-size:11px;color:var(--dim2)}

@media (max-width:820px){
  .side{display:none}
  .thread,.composer,.disclaim,.empty{max-width:100%}
  .msg.me .txt{max-width:86%}
}
</style>
</head>
<body>
<aside class="side">
  <div class="brand"><div class="logo">MV</div><b>MVE</b><span>学习能力</span></div>
  <button class="newchat" id="newChat"><span class="plus">＋</span>新对话</button>
  <div class="hist" id="hist"></div>
  <div class="sidefoot">
    <span class="dot" id="liveDot"></span>
    <span id="liveTxt">正在连接…</span>
    <button class="theme" id="themeBtn" title="切换深浅色">🌙</button>
  </div>
</aside>

<div class="main" id="main">
  <div class="top">
    <div class="model">
      <button id="modelBtn"><span id="modelName">MVE</span><span class="chev">▾</span></button>
      <div class="menu" id="menu"></div>
    </div>
    <div class="thinktoggle">
      <span>深度思考</span>
      <div class="sw on" id="thinkSw"><i></i></div>
    </div>
    <div class="thinktoggle" title="规划里排了 web 这一步时，是否真的去联网取回资料">
      <span>联网</span>
      <div class="sw on" id="webSw"><i></i></div>
    </div>
  </div>

  <div class="scroll" id="scroll">
    <div class="thread" id="thread"></div>
    <div class="empty" id="empty">
      <h2>我是 MVE，不是聊天模型</h2>
      <p>我答问题不是「生成一段话」，而是去技能库里找学过的配方、在你的数据环境里真查一遍，
         再拿数字逐个对账。所以：答得出来 = 能复现；答不出来我会说没学，不编。</p>
      <div class="chips" id="chips"></div>
    </div>
  </div>

  <div class="dock">
    <div class="composer">
      <textarea id="q" rows="1" placeholder="给 MVE 发送消息，例如：技术部 9 月的餐补和交通补贴总额是多少？"></textarea>
      <div class="row">
        <button class="attach" id="attach" title="上传文件（CSV / JSON / Excel）——导入后这个场景就能查到它">📎</button>
        <input type="file" id="fileInput" hidden accept=".csv,.tsv,.json,.jsonl,.ndjson,.xlsx,.xls,.txt,.md,.markdown,.log,.pdf,.docx,.png,.jpg,.jpeg,.webp,.gif">
        <span class="hint">Enter 发送 · Shift+Enter 换行</span>
        <button class="send" id="send" title="发送">↑</button>
        <button class="stop" id="stop">■ 停止生成</button>
      </div>
    </div>
    <div class="disclaim">内容由 MVE 生成 · 判定 / 复用技能 / 供给线随答案标注，请以数据为准</div>
  </div>
</div>

<script>
var P = window.MVE_PREFIX || '';
var $ = function(id){ return document.getElementById(id); };
var scroll = $('scroll'), thread = $('thread'), ta = $('q');
var sendBtn = $('send'), stopBtn = $('stop'), main = $('main');

/* ---------------- 本地会话（R1 客户端左侧那一列） ---------------- */
var KEY = 'mve_chat_sessions_v1';
var store = {sessions: [], current: null};
try { store = JSON.parse(localStorage.getItem(KEY)) || store; } catch(e) {}
if (!store.sessions) store.sessions = [];
function save(){ try { localStorage.setItem(KEY, JSON.stringify(store)); } catch(e) {} }
function cur(){ return store.sessions.filter(function(s){ return s.id === store.current; })[0]; }
function newSession(){
  var s = {id: 's' + Date.now(), title: '', ts: Date.now(), model: model || '',
           messages: []};
  store.sessions.unshift(s); store.current = s.id; save(); renderHist(); paint();
  return s;
}
function renderHist(){
  var h = $('hist'); h.innerHTML = '';
  var today = new Date().toDateString();
  var g = null;
  store.sessions.forEach(function(s){
    var label = new Date(s.ts).toDateString() === today ? '今天' : '更早';
    if (label !== g){
      g = label;
      var d = document.createElement('div');
      d.className = 'grp'; d.textContent = label; h.appendChild(d);
    }
    var it = document.createElement('div');
    it.className = 'item' + (s.id === store.current ? ' on' : '');
    var t = document.createElement('div');
    t.className = 't'; t.textContent = s.title || '新对话';
    var x = document.createElement('button');
    x.className = 'del'; x.textContent = '✕'; x.title = '删除会话';
    x.onclick = function(ev){
      ev.stopPropagation();
      store.sessions = store.sessions.filter(function(o){ return o.id !== s.id; });
      if (store.current === s.id) store.current = (store.sessions[0] || {}).id || null;
      save(); renderHist(); paint();
    };
    it.onclick = function(){ store.current = s.id; save(); renderHist(); paint(); };
    it.appendChild(t); it.appendChild(x); h.appendChild(it);
  });
}

/* ---------------- 场景 = 模型（R1 的模型下拉） ---------------- */
var model = 'mve', models = [];
function loadModels(){
  fetch(P + '/v1/models').then(function(r){ return r.json(); }).then(function(d){
    models = d.data || [];
    var m = $('menu'); m.innerHTML = '';
    models.forEach(function(x){
      var o = document.createElement('div');
      o.className = 'opt';
      o.innerHTML = '<span>' + (x.description || x.id) + '</span>';
      var id = document.createElement('span');
      id.className = 'id'; id.textContent = x.id;
      o.appendChild(id);
      o.onclick = function(){ setModel(x); };
      m.appendChild(o);
    });
    var hint = document.createElement('div');
    hint.className = 'opt'; hint.style.color = 'var(--dim2)';
    hint.style.cursor = 'default';
    hint.textContent = '每个场景是一套独立的数据环境与技能库';
    m.appendChild(hint);
    if (models.length){ setModel(models[0], true); }
    $('liveTxt').textContent = '已连接 · ' + models.length + ' 个场景';
  }).catch(function(e){
    $('liveDot').style.background = 'var(--bad)';
    $('liveTxt').textContent = '连接失败';
  });
}
function setModel(x, quiet){
  model = x.id;
  $('modelName').textContent = (x.description || x.id) + ' · ' + x.id;
  $('menu').classList.remove('open');
  if (!quiet && cur()) { cur().model = model; save(); }
  var c = $('chips'); c.innerHTML = '';
  (x.samples || []).forEach(function(q){
    var b = document.createElement('button');
    b.className = 'chip'; b.textContent = q;
    b.onclick = function(){ ta.value = q; ta.focus(); autoGrow(); };
    c.appendChild(b);
  });
}
$('modelBtn').onclick = function(e){
  e.stopPropagation();
  $('menu').classList.toggle('open');
};
document.addEventListener('click', function(){ $('menu').classList.remove('open'); });

/* ---------------- 主题 / 深度思考 ---------------- */
var theme = localStorage.getItem('mve_chat_theme') || 'dark';
function applyTheme(){
  document.documentElement.setAttribute('data-theme', theme);
  $('themeBtn').textContent = theme === 'dark' ? '🌙' : '☀️';
  localStorage.setItem('mve_chat_theme', theme);
}
applyTheme();
$('themeBtn').onclick = function(){ theme = theme === 'dark' ? 'light' : 'dark'; applyTheme(); };

var webOn = localStorage.getItem('mve_chat_web') !== '0';
if (!webOn) $('webSw').classList.remove('on');
$('webSw').onclick = function(){
  webOn = !webOn;
  $('webSw').classList.toggle('on', webOn);
  localStorage.setItem('mve_chat_web', webOn ? '1' : '0');
};

var deepThink = localStorage.getItem('mve_chat_think') !== '0';
if (!deepThink) $('thinkSw').classList.remove('on');
$('thinkSw').onclick = function(){
  deepThink = !deepThink;
  $('thinkSw').classList.toggle('on', deepThink);
  localStorage.setItem('mve_chat_think', deepThink ? '1' : '0');
  Array.prototype.forEach.call(document.querySelectorAll('.think'), function(t){
    t.style.display = deepThink ? '' : 'none';
  });
};

/* ---------------- 渲染 ---------------- */
function esc(s){
  return String(s).replace(/[&<>]/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];
  });
}
function md(src){
  var parts = String(src || '').split('```');
  var out = '';
  for (var i = 0; i < parts.length; i++){
    if (i % 2 === 1){
      var body = parts[i].replace(/^[\w+-]*\n/, '');
      out += '<pre><code>' + esc(body) + '</code></pre>';
    } else {
      var t = esc(parts[i]);
      t = t.replace(/`([^`\n]+)`/g, '<code>$1</code>');
      t = t.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
      t = t.replace(/^#### (.*)$/gm, '<h4>$1</h4>');
      t = t.replace(/^### (.*)$/gm, '<h4>$1</h4>');
      t = t.replace(/^## (.*)$/gm, '<h3>$1</h3>');
      out += t.split(/\n{2,}/).map(function(p){
        if (!p.trim()) return '';
        if (/^\s*<(h3|h4|pre|ul|ol)/.test(p)) return p;
        return '<p>' + p.replace(/\n/g, '<br>') + '</p>';
      }).join('');
    }
  }
  return out;
}
function el(tag, cls, txt){
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
}
function msgNode(m){
  var wrap = el('div', 'msg ' + (m.role === 'user' ? 'me' : 'ai'));
  if (m.role === 'user'){
    wrap.appendChild(el('div', 'txt', m.content));
    return wrap;
  }
  if (m.reasoning && deepThink){
    var box = el('div', 'think');
    var hd = el('div', 'hd');
    hd.appendChild(el('span', 'arw', '▶'));
    var lbl = el('span', '', '已深度思考（用时 ' + (m.secs || '?') + ' 秒）');
    hd.appendChild(lbl);
    hd.onclick = function(){ box.classList.toggle('open'); };
    var bd = el('div', 'bd', m.reasoning);
    box.appendChild(hd); box.appendChild(bd);
    wrap.appendChild(box);
  }
  var txt = el('div', 'txt');
  txt.innerHTML = md(m.content || '');
  wrap.appendChild(txt);
  if (m.x) wrap.appendChild(tagRow(m.x));
  if (!m.note) wrap.appendChild(actRow(m));
  return wrap;
}
function tagRow(x){
  var row = el('div', 'meta');
  function t(txt, cls){
    if (!txt) return;
    var s = el('span', 'tag' + (cls ? ' ' + cls : ''), txt);
    row.appendChild(s);
  }
  var jr = String(x.judgment_result || '');
  if (jr === 'claim') {
    // 工具线答出来的：有答案但不是学到的能力 —— 不显示「未掌握」（那读起来
    // 像没答出来，2026-10-29 实测），如实标 claim + 不入账。
    t('判定 claim（联网取回、未验证）', 'warn');
    t('不进能力账', '');
  } else {
    t('判定 ' + (jr || '未知'), jr === 'written' ? 'ok' : 'warn');
    t(x.mastered ? '掌握 ✓' : '未掌握', x.mastered ? 'ok' : '');
  }
  if (x.tool_line_used) t('工具线 claim', 'warn');
  if (x.inference_used) t('模型推断', 'warn');
  if (x.learn_kicked) t('已派学习', 'warn');
  t('供给线 ' + (x.scope_line || '?'), x.scope_line === 'learn' ? 'ok' : 'warn');
  if (x.skill) t('复用 ' + x.skill, 'ok');
  if (x.topic_id) t(x.topic_id);
  return row;
}
function actRow(m){
  var row = el('div', 'acts');
  var c = el('button', '', '⧉ 复制');
  c.onclick = function(){
    var s = String(m.content || '');
    if (navigator.clipboard) navigator.clipboard.writeText(s);
    c.textContent = '✓ 已复制';
    setTimeout(function(){ c.textContent = '⧉ 复制'; }, 1400);
  };
  var r = el('button', '', '↻ 重新生成');
  r.onclick = function(){ regenerate(m); };
  row.appendChild(c); row.appendChild(r);
  return row;
}
function paint(){
  thread.innerHTML = '';
  var s = cur();
  var msgs = (s && s.messages) || [];
  $('empty').style.display = msgs.length ? 'none' : '';
  msgs.forEach(function(m){ thread.appendChild(msgNode(m)); });
  scrollDown();
}
function scrollDown(){ scroll.scrollTop = scroll.scrollHeight; }

/* ---------------- 发送 / 流式 ---------------- */
var busy = false, abort = null;
function autoGrow(){
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 180) + 'px';
}
ta.addEventListener('input', autoGrow);
ta.addEventListener('keydown', function(e){
  if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); send(); }
});
$('newChat').onclick = function(){ newSession(); ta.focus(); };
sendBtn.onclick = send;
stopBtn.onclick = function(){ if (abort) abort.abort(); };

/* ---------------- 外置上传模块（R1 客户端的「把文件交给它」） ----------------
   为什么在前端做入口、在后端落表：用户拍板「...做 r1 客户端的多模态上传文件
   那些外置模块来让 agent 看到全貌」。文件进了场景库 = 进了 schema_block 与
   db_observation 的全貌，出题/回填/判官下一次提问就都看得见，不用改业务代码。 */
function sceneIdOf(m){
  var s = String(m || '');
  var i = s.indexOf(':');
  return i >= 0 ? s.slice(i + 1) : '';
}
function b64(buf){
  var bytes = new Uint8Array(buf), out = '', step = 0x8000;
  for (var i = 0; i < bytes.length; i += step){
    out += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
  }
  return btoa(out);
}
function noteMsg(text){
  var s = cur() || newSession();
  s.messages.push({role: 'ai', content: text, note: true, ts: Date.now()});
  save(); paint();
}
$('attach').onclick = function(){ $('fileInput').click(); };
$('fileInput').onchange = function(ev){
  var f = (ev.target.files || [])[0];
  if (!f) return;
  var btn = $('attach');
  btn.classList.add('up'); btn.textContent = '⏳';
  var fr = new FileReader();
  fr.onload = function(){
    var sid = sceneIdOf((cur() && cur().model) || model) || 'A';
    noteMsg('📎 正在导入 `' + f.name + '`（' + Math.round(f.size / 1024) +
            ' KB）到场景 ' + sid + ' …');
    fetch(P + '/api/upload', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({scene_id: sid, name: f.name, data_b64: b64(fr.result)})
    }).then(function(r){ return r.json(); }).then(function(d){
      if (d.ok && (d.kind === 'text' || d.kind === 'image')){
        // 文档/图片 = 上下文注入（DeepSeek 同构）：内容已转成文本进输入，
        // 与「能不能查库」无关 —— 不该再吓唬人「agent 看不见它」。
        noteMsg('✅ **' + f.name + '** 已作为上下文注入（' +
                (d.kind === 'image' ? '图片' : '文本') +
                (d.chars ? '，' + d.chars + ' 字' : '') +
                '，场景 ' + sid + '）。\n\n' +
                (d.note ? '⚠️ ' + d.note + '\n\n' : '') +
                '直接问文件里的内容就行 —— 答案会标注「按你上传的文件作答」。');
      } else if (d.ok && d.visible === false){
        noteMsg('⚠️ 文件已落盘，但**这个场景的 agent 看不见它**（场景 ' + sid +
                ' 是 VLML 域，查的是另一个库）。\n\n' +
                '请把顶部模型切到你要问的场景（如 mve:B），再重新上传 —— ' +
                '上传跟着当前选中的场景走。');
      } else if (d.ok){
        noteMsg('✅ 已导入 **' + d.table + '**（' + d.rows + ' 行 / ' +
                (d.columns || []).length + ' 列）到场景 ' + sid + ' 的库。\n\n' +
                '列：`' + (d.columns || []).join('`, `') + '`\n\n' +
                '现在直接问它就查得到 —— 这张表和库里其它表一样进全貌，' +
                '不写死任何业务词。');
      } else {
        noteMsg('⚠️ 导入失败：' + (d.why || '未知原因'));
      }
    }).catch(function(e){
      noteMsg('⚠️ 导入失败：' + e.message);
    }).then(function(){
      btn.classList.remove('up'); btn.textContent = '📎';
      ev.target.value = '';
    });
  };
  fr.onerror = function(){
    noteMsg('⚠️ 读不了这个文件');
    btn.classList.remove('up'); btn.textContent = '📎';
  };
  fr.readAsArrayBuffer(f);
};

function send(text){
  if (busy) return;
  var q = (text != null ? text : ta.value).trim();
  if (!q) return;
  var s = cur() || newSession();
  if (!s.messages.length) s.title = q.slice(0, 26) + (q.length > 26 ? '…' : '');
  s.messages.push({role: 'user', content: q});
  s.ts = Date.now(); save(); renderHist(); paint();
  ta.value = ''; autoGrow();
  run(s, q);
}

async function run(s, q){
  busy = true; main.classList.add('busy'); ta.disabled = true;
  var t0 = Date.now();
  var wrap = el('div', 'msg ai');
  var thinkBox = null, thinkBd = null, thinkLbl = null;
  if (deepThink){
    thinkBox = el('div', 'think open');
    var hd = el('div', 'hd');
    hd.appendChild(el('span', 'arw', '▶'));
    thinkLbl = el('span', '', '正在深度思考…');
    hd.appendChild(thinkLbl);
    hd.onclick = function(){ thinkBox.classList.toggle('open'); };
    thinkBd = el('div', 'bd', '');
    thinkBox.appendChild(hd); thinkBox.appendChild(thinkBd);
    wrap.appendChild(thinkBox);
  }
  var txt = el('div', 'txt', '');
  var cur = el('span', 'cur');
  txt.appendChild(cur);
  wrap.appendChild(txt);
  thread.appendChild(wrap); scrollDown();

  var reasoning = '', content = '', xmve = null;
  abort = new AbortController();
  var payload = {
    model: s.model || model,
    stream: true,
    web: webOn,
    messages: s.messages.filter(function(m){ return m.role === 'user' || m.role === 'assistant'; })
                       .map(function(m){ return {role: m.role, content: m.content}; })
  };
  try {
    var r = await fetch(P + '/v1/chat/completions', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
      signal: abort.signal
    });
    if (!r.ok || !r.body){
      var errTxt = await r.text();
      throw new Error('HTTP ' + r.status + ' ' + errTxt.slice(0, 200));
    }
    var reader = r.body.getReader(), dec = new TextDecoder(), buf = '';
    while (true){
      var step = await reader.read();
      if (step.done) break;
      buf += dec.decode(step.value, {stream: true});
      var lines = buf.split('\n');
      buf = lines.pop();
      for (var i = 0; i < lines.length; i++){
        var ln = lines[i].trim();
        if (!ln || ln.indexOf('data:') !== 0) continue;
        var data = ln.slice(5).trim();
        if (data === '[DONE]') continue;
        var j;
        try { j = JSON.parse(data); } catch(e){ continue; }
        if (j.x_mve) xmve = j.x_mve;
        var d = (j.choices && j.choices[0] && j.choices[0].delta) || {};
        if (d.reasoning_content){
          reasoning += d.reasoning_content;
          if (thinkBd) thinkBd.textContent = reasoning;
        }
        if (d.content){
          content += d.content;
          txt.innerHTML = md(content);
          txt.appendChild(cur);
        }
        scrollDown();
      }
    }
  } catch(e){
    if (e.name === 'AbortError'){
      content += (content ? '\n\n' : '') + '（已停止生成）';
    } else {
      content = '失败：' + e.message;
    }
  } finally {
    abort = null; busy = false; main.classList.remove('busy'); ta.disabled = false;
    var secs = Math.max(1, Math.round((Date.now() - t0) / 1000));
    if (cur.parentNode) cur.parentNode.removeChild(cur);
    txt.innerHTML = md(content);
    if (thinkBox){
      thinkLbl.textContent = '已深度思考（用时 ' + secs + ' 秒）';
      if (!reasoning) thinkBox.style.display = 'none';
    }
    var rec = {role: 'assistant', content: content, reasoning: reasoning,
               secs: secs, x: xmve};
    s.messages.push(rec); s.ts = Date.now(); save();
    thread.removeChild(wrap);
    thread.appendChild(msgNode(rec));
    scrollDown();
  }
}

function regenerate(m){
  var s = cur(); if (!s) return;
  var i = s.messages.indexOf(m);
  if (i < 0) return;
  var q = '';
  for (var k = i; k >= 0; k--){
    if (s.messages[k].role === 'user'){ q = s.messages[k].content; break; }
  }
  s.messages = s.messages.slice(0, i);
  save(); paint();
  if (q) run(s, q);
}

/* ---------------- 起手 ---------------- */
if (!store.current){
  if (store.sessions.length) store.current = store.sessions[0].id;
  else newSession();
} else {
  renderHist(); paint();
}
renderHist(); paint();
loadModels();
ta.focus();
/* ---------------- 学习完成自动回访（2026-10-29） ----------------
   学习侧学出东西后会把排队没答上的题自动重答（revisit.py）；答出来的
   结果在这里拉取展示 —— 人不用自己回来重问。拉走即标记已送达，不重复弹。 */
function pullRevisit(){
  fetch(P + '/api/revisit').then(function(r){ return r.json(); }).then(function(d){
    (d && d.items || []).forEach(function(it){
      var a = String(it.answer || '');
      noteMsg('🔁 学习完成后自动回访：之前没答上的「' + String(it.question || '') +
              '」现在能答了 —— ' + a + (a ? '' : '（详情见轨迹页）'));
    });
    if (d && d.pending > 0){
      // 提示条不重复堆（每次刷新都会拉到 pending 数，记过就不再记）
      var t = '⏳ 还有 ' + d.pending + ' 个排队问题没答上，学会后我会自动回来重答，不用你盯着。';
      var s = cur();
      var has = s && (s.messages || []).some(function(m){ return m.content === t; });
      if (!has) noteMsg(t);
    }
  }).catch(function(){});
}
pullRevisit();
// 分享链：/mve/chat?q=问题 → 打开就自动问（也方便回归截图）。
var urlQ = '';
try { urlQ = new URLSearchParams(location.search).get('q') || ''; } catch(e) {}
if (urlQ){ setTimeout(function(){ send(urlQ); }, 600); }
</script>
</body>
</html>
"""
