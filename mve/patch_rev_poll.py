#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-11-18 面板卡顿治理（第二步）：rev 轮询。

问题：前端 setInterval(load, 4000) 每 4 秒无条件拉全量 /api/state（1MB，
gzip 后 126KB），按钮 handler 结尾也 await load() —— 用户感知"按钮点击
响应慢"，且浏览器每次白解析渲染 1MB JSON。

治法：新增轻量 GET /api/rev —— 返回数据指纹（mve 目录所有文件的
mtime_ns/size 哈希 + coach/learn 在跑标志）。前端每 3 秒只拉 rev，
指纹没变就不拉全量；任务在跑（busy）时保持全量轮询（计时器要刷新）。
按钮行为不变（动作后一次全量 load，现在只有 126KB）。

改动：
  panel_data.py  — 新增 state_rev()（1 秒微缓存的文件指纹）
  dashboard.py   — ① /api/rev 路由；② /api/state 载荷带 rev/busy；
                   ③ 前端 setInterval(load,4000) → rev 轮询 poll()
"""
import hashlib
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def splice(fname, old, new):
    p = HERE / fname
    src = p.read_text(encoding="utf-8")
    n = src.count(old)
    if n != 1:
        print(f"[SKIP] {fname}: 锚文本出现 {n} 次（期望 1）→ {old[:60]!r}")
        return False
    shutil.copy2(p, str(p) + ".bak_rev")
    p.write_text(src.replace(old, new), encoding="utf-8")
    print(f"[OK] {fname}: {old[:50]!r} …")
    return True


ok = True

# ---------- 1. panel_data.py：state_rev() ----------
ok &= splice(
    "panel_data.py",
    '''def build_state_invalidate() -> None:''',
    '''_REV_CACHE: dict[str, Any] = {"at": 0.0, "value": ""}


def state_rev() -> str:
    """全库数据指纹（2026-11-18 面板卡顿治理）。

    对 mve 目录下所有文件（跳 __pycache__/.git/node_modules）的
    mtime_ns+size 做 sha1 —— 学习循环（独立进程）和面板（人操作）
    写任何账目，指纹必变。前端轮询 /api/rev，指纹没变就不拉 1MB
    全量 /api/state。1 秒微缓存：多页签同帧轮询只算一次。
    """
    now = time.monotonic()
    if now - _REV_CACHE["at"] < 1.0 and _REV_CACHE["value"]:
        return _REV_CACHE["value"]
    h = hashlib.sha1()
    skip = {"__pycache__", ".git", "node_modules"}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
                h.update(f"{p}|{st.st_mtime_ns}|{st.st_size}\\n".encode())
            except OSError:
                pass
    _REV_CACHE["at"] = now
    _REV_CACHE["value"] = h.hexdigest()[:16]
    return _REV_CACHE["value"]


def build_state_invalidate() -> None:''',
)

# panel_data 需要 os / time / hashlib（查漏补 import）
src = (HERE / "panel_data.py").read_text(encoding="utf-8")
need = []
if not re.search(r"^import os$", src, re.M):
    need.append("import os")
if not re.search(r"^import time$", src, re.M):
    need.append("import time")
if not re.search(r"^import hashlib$", src, re.M):
    need.append("import hashlib")
if need:
    m = re.search(r"^(ROOT = Path\(.*\))$", src, re.M)
    anchor = m.group(1) if m else "ROOT = Path(__file__).resolve().parent"
    src = src.replace(anchor, "\n".join(need) + "\n" + anchor, 1)
    (HERE / "panel_data.py").write_text(src, encoding="utf-8")
    print(f"[OK] panel_data.py 补 import: {need}")

# ---------- 2. dashboard.py：/api/rev 路由 + state 载荷 + 前端 ----------
ok &= splice(
    "dashboard.py",
    '''        elif path == "/api/state":
            import time as _t''',
    '''        elif path == "/api/rev":
            # 轻量轮询（2026-11-18 卡顿治理）：指纹没变前端就不拉全量 state。
            busy = bool(COACH_JOB["running"] or LEARN_JOB["running"])
            self._json(200, {"rev": panel_data.state_rev(), "busy": busy})
            return
        elif path == "/api/state":
            import time as _t''',
)

ok &= splice(
    "dashboard.py",
    '''            st["coach_error"] = str(COACH_JOB["error"] or "")''',
    '''            st["coach_error"] = str(COACH_JOB["error"] or "")
            st["rev"] = panel_data.state_rev()''',
)

# 前端：load 记住 rev；轮询改 rev 探测
ok &= splice(
    "dashboard.py",
    '''async function load(){
  try{
    const r=await fetch('/api/state?t='+Date.now());
    render(await r.json());''',
    '''let lastRev='';
async function load(){
  try{
    const r=await fetch('/api/state?t='+Date.now());
    const d=await r.json();
    if(d.rev) lastRev=d.rev;
    render(d);''',
)

ok &= splice(
    "dashboard.py",
    '''setInterval(load, 4000);''',
    '''// 2026-11-18 卡顿治理：轮询改轻量 rev 探测——指纹没变不拉全量；
// 任务在跑（busy）或指纹变了才 load()（动作后按钮自带的 await load() 不变）。
async function poll(){
  try{
    const r=await fetch('/api/rev?t='+Date.now());
    const d=await r.json();
    if(d.busy || d.rev!==lastRev){ lastRev=d.rev; load(); }
  }catch(e){}
}
setInterval(poll, 3000);''',
)

if not ok:
    print("\n有锚文本没匹配上，中止（未写坏文件，已成功的有 .bak_rev 备份）")
    sys.exit(1)
print("\nALL PATCHED")
