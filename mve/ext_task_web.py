#!/usr/bin/env python3
"""外部 task 闭环的网页入口（薄壳：只调已有 CLI，不重复任何业务逻辑）。

为什么要有它
------------
验收线（人导入 task → 初始 fail → 学 → retry 复用自己的技能）现在只有 CLI：
    python mve/task_intake.py ask "…" / sub <id> / retry <id>
    python -m voyager_vlml.loop --rounds 1
主面板（`/mve/`）是另一条分支的"场景/掌握度/曲线"面板，**没有**外部 task 入口，
面板代码里 `task_intake` 出现 0 次。所以这里只做一层浏览器壳：
每个按钮 = 起一个子进程跑上面那条命令，把 stdout 原样回显。

刻意不做的
----------
· 不在进程内 import task_intake / loop：两者都自己 asyncio.run + 连 MCP 网关，
  和 web 进程的事件循环抢单例（历史坑：连接绑死在旧循环上 → 第二次必超时）。
  子进程各自一个干净环境，崩了也只崩那一次点击。
· 不重写判据/口径/技能逻辑：判在 critic，学在 loop，账在 task_intake。

跑法：MVE_EXT_HOST=172.18.0.1 MVE_EXT_PORT=8779 python mve/ext_task_web.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent          # /root/mve-rebuild
PY = sys.executable
PREFIX = "/mve-ext"
MAX_OUTPUT = 60_000

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{ROOT}:{ROOT / 'mve'}"
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("MVE_PANEL_PREFIX", PREFIX)
    return env


def _start(cmd: list[str], title: str) -> str:
    jid = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[jid] = {"id": jid, "title": title, "cmd": cmd, "status": "running",
                      "output": "", "started": time.time(), "done_at": None,
                      "code": None}

    def run() -> None:
        job = _jobs[jid]
        try:
            p = subprocess.run(cmd, cwd=str(ROOT), env=_env(),
                               capture_output=True, text=True, timeout=1800)
            out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
            job.update(status="done", code=p.returncode, output=out[-MAX_OUTPUT:])
        except subprocess.TimeoutExpired as e:
            job.update(status="timeout", code=-1,
                       output=(str(e.stdout or "") + "\n（超时：30 分钟）")[-MAX_OUTPUT:])
        except Exception as e:  # noqa: BLE001
            job.update(status="error", code=-1,
                       output=f"{type(e).__name__}: {e}")
        finally:
            job["done_at"] = time.time()

    threading.Thread(target=run, daemon=True).start()
    return jid


# ---------------------------------------------------------------- 命令映射
def _cmd_ask(q: str, iters: int) -> list[str]:
    return [PY, "mve/task_intake.py", "ask", q]


def _cmd_retry(tid: str, iters: int) -> list[str]:
    return [PY, "mve/task_intake.py", "retry", tid]


def _cmd_learn(iters: int, topic: str) -> list[str]:
    cmd = [PY, "-m", "voyager_vlml.loop", "--rounds", "1", "--iters", str(iters)]
    if topic:
        cmd += ["--topic", topic]
    return cmd


def _cmd_sub(tid: str, iters: int) -> list[str]:
    return [PY, "mve/task_intake.py", "sub", tid]


def _cmd_list(_iters: int) -> list[str]:
    return [PY, "mve/task_intake.py", "list"]


ACTIONS = {
    "ask": (_cmd_ask, "导入并试答"),
    "learn": (_cmd_learn, "学一轮"),
    "retry": (_cmd_retry, "重试这条"),
    "sub": (_cmd_sub, "看子目标"),
    "list": (_cmd_list, "列账本"),
}


# ---------------------------------------------------------------- 页面
PAGE = """<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MVE · 外部 task 闭环</title>
<style>
 :root{--bg:#0f1115;--card:#171a21;--line:#2a2f3a;--fg:#e6e8ee;--dim:#9aa3b2;--acc:#7aa2f7;--ok:#5fbf7f;--bad:#e06c75}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif}
 .wrap{max-width:980px;margin:0 auto;padding:28px 20px 60px}
 h1{font-size:20px;margin:0 0 6px}
 .sub{color:var(--dim);margin:0 0 22px;font-size:13px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
 label{display:block;color:var(--dim);font-size:12px;margin-bottom:6px}
 textarea,input{width:100%;background:#0d1016;color:var(--fg);border:1px solid var(--line);
   border-radius:8px;padding:10px;font:13px/1.5 inherit}
 textarea{min-height:64px;resize:vertical}
 .row{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px;align-items:center}
 button{background:var(--acc);color:#0b0e14;border:0;border-radius:8px;padding:9px 16px;
   font-weight:600;cursor:pointer}
 button.ghost{background:#222834;color:var(--fg);border:1px solid var(--line);font-weight:500}
 button:disabled{opacity:.45;cursor:default}
 input.small{width:110px}
 pre{background:#0b0e14;border:1px solid var(--line);border-radius:8px;padding:12px;
   white-space:pre-wrap;word-break:break-word;max-height:52vh;overflow:auto;font-size:12.5px}
 .hint{color:var(--dim);font-size:12px;margin-top:10px}
 .st{font-size:12px;color:var(--dim)}
 .st b{color:var(--fg)}
 table{width:100%;border-collapse:collapse;font-size:12.5px}
 th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
 th{color:var(--dim);font-weight:500}
</style></head><body><div class="wrap">
<h1>MVE · 外部 task 闭环</h1>
<p class="sub">有数据源、没有建模表：导入一条外部问题 → 技能库空所以必失败 → 它自己拆课、学、把技能存下来 → 重试同一条。
每个按钮起一个子进程跑已有 CLI（<code>task_intake.py</code> / <code>voyager_vlml.loop</code>），输出原样回显。</p>

<div class="card">
  <label>① 导入一条外部问题（数据源里真能取数的那种）</label>
  <textarea id="q">Karmine Corp 在 Sunset 上先拿到首杀的回合里赢了多少比例</textarea>
  <div class="row">
    <button onclick="run('ask')">导入并试答</button>
    <span class="hint">首次几乎必失败（技能库空 = 验收线的起点）</span>
  </div>
</div>

<div class="card">
  <label>② 让它学一轮（课程围着失败信号自己选）</label>
  <div class="row">
    <input class="small" id="iters" value="4" title="每题最多改几次 SQL">
    <button onclick="run('learn')">学一轮</button>
    <span class="hint">等价 <code>python -m voyager_vlml.loop --rounds 1</code></span>
  </div>
</div>

<div class="card">
  <label>③ 重试同一条（验收点：这次用的是它自己学会的技能）</label>
  <div class="row">
    <input class="small" id="tid" placeholder="task_id">
    <button onclick="run('retry')">重试这条</button>
    <button class="ghost" onclick="run('sub')">看子目标</button>
    <button class="ghost" onclick="run('list')">列账本</button>
  </div>
  <div class="hint">task_id 从下面账本里抄；`sub` 会显示哪些子目标学不动被跳过。</div>
</div>

<div class="card">
  <div class="st" id="st">空闲</div>
  <pre id="out">（还没有输出）</pre>
</div>

<div class="card">
  <div class="row" style="margin-top:0"><button class="ghost" onclick="run('list')">刷新账本</button></div>
  <div id="tasks" class="hint">点「列账本」看导入过的题。</div>
</div>

<script>
const P = location.pathname.indexOf('/mve-ext') === 0 ? '/mve-ext' : '';
async function run(action){
  const q = document.getElementById('q').value.trim();
  const tid = document.getElementById('tid').value.trim();
  const iters = parseInt(document.getElementById('iters').value)||4;
  if((action==='ask'&&!q)||((action==='retry'||action==='sub')&&!tid)){
    alert('先把问题或 task_id 填上'); return;
  }
  setSt('提交中…');
  const r = await fetch(P+'/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action,q,tid,iters})});
  const j = await r.json();
  if(j.error){ setSt('出错：'+j.error); return; }
  poll(j.id);
}
async function poll(id){
  const t0 = Date.now();
  while(true){
    const r = await fetch(P+'/api/job/'+id);
    const j = await r.json();
    document.getElementById('out').textContent = j.output || '（运行中…）';
    setSt(j.title+' · '+j.status+' · '+Math.round((Date.now()-t0)/1000)+'s');
    if(j.status!=='running'){
      if(j.title.includes('账本')) renderTasks(j.output);
      return;
    }
    await new Promise(s=>setTimeout(s,1500));
  }
}
function setSt(s){ document.getElementById('st').innerHTML = '<b>'+s+'</b>'; }
function renderTasks(text){
  try{
    const arr = JSON.parse(text.slice(text.indexOf('[')));
    if(!Array.isArray(arr)||!arr.length) return;
    const rows = arr.slice(-12).reverse().map(t=>`<tr><td><code>${t.task_id||''}</code></td>
      <td>${(t.status||'')}</td><td>${t.tries||''}</td>
      <td>${(t.question||'').slice(0,60)}</td>
      <td>${(t.answer||'').slice(0,80)}</td></tr>`).join('');
    document.getElementById('tasks').innerHTML =
      '<table><tr><th>task_id</th><th>状态</th><th>次数</th><th>问题</th><th>答案</th></tr>'+rows+'</table>';
  }catch(e){}
}
</script></div></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _path(self) -> str:
        p = urlparse(self.path).path
        if p.startswith(PREFIX):
            p = p[len(PREFIX):] or "/"
        return p.rstrip("/") or "/"

    def do_GET(self) -> None:  # noqa: N802
        p = self._path()
        if p == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if p.startswith("/api/job/"):
            jid = p.rsplit("/", 1)[-1]
            job = _jobs.get(jid)
            if job is None:
                self._json({"error": "没有这个任务"}, 404)
                return
            self._json({k: job[k] for k in
                        ("id", "title", "status", "output", "code", "started", "done_at")})
            return
        if p == "/health":
            self._json({"ok": True, "root": str(ROOT)})
            return
        self._json({"error": f"没有这个路径：{p}"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        p = self._path()
        if p != "/api/run":
            self._json({"error": f"没有这个路径：{p}"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        action = str(body.get("action") or "")
        if action not in ACTIONS:
            self._json({"error": f"不认识的动作：{action}"}, 400)
            return
        build, label = ACTIONS[action]
        iters = int(body.get("iters") or 4)
        q = str(body.get("q") or "").strip()
        tid = str(body.get("tid") or "").strip()
        try:
            if action == "ask":
                cmd = build(q, iters)
            elif action in ("retry", "sub"):
                cmd = build(tid, iters)
            elif action == "learn":
                cmd = build(iters, "")
            else:
                cmd = build(iters)
        except TypeError as e:
            self._json({"error": f"参数不对：{e}"}, 400)
            return
        jid = _start(cmd, f"{label}")
        self._json({"id": jid, "cmd": " ".join(cmd[len(cmd) - 3:])})

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静点，只记关键
        if "/health" in (args[0] if args else ""):
            return
        sys.stderr.write("[web] " + fmt % args + "\n")


def main() -> int:
    host = os.getenv("MVE_EXT_HOST", "172.18.0.1")
    port = int(os.getenv("MVE_EXT_PORT", "8779"))
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"外部 task 闭环网页：http://{host}:{port}{PREFIX}/  (root={ROOT})", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
