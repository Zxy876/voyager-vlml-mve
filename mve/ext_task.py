#!/usr/bin/env python3
"""外部 task 闭环的后台执行器（给主面板 /mve/ 的「外部任务」页签用）。

**这不是一个页面**（2026-10-23 用户拍板：不要再起第二个页面，入口统一在 /mve/）。
这里只有三件事：起子进程、记状态、给面板读。页签的 HTML/JS 在 dashboard.py 里。

为什么走子进程，而不是在本进程 import
--------------------------------------
`task_intake` / `voyager_vlml.loop` 都自己 `asyncio.run()` 并连 MCP 网关（进程级单例）。
在 web 进程里直接调，会把连接绑到 web 的事件循环上 —— 历史坑实测：第一次点击能通，
之后全部 60s 超时（见记忆里「MCP 网关连接绑死在旧循环上」）。子进程各自一个干净环境，
崩了也只崩那一次点击。

刻意不做的
----------
· 不重写判据/口径/技能逻辑：判在 critic，学在 loop，账在 task_intake。
· 不落盘 job 状态：面板重启即清（与 COACH_JOB / LEARN_JOB 一个思路）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent          # 仓库根（/root/mve）
TASKS_PATH = ROOT / "mve" / "failed_tasks.json"
PY = sys.executable
MAX_OUTPUT = 60_000
JOB_TIMEOUT = 1800                                     # 单次点击最多 30 分钟

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{ROOT}:{ROOT / 'mve'}"
    env["PYTHONUNBUFFERED"] = "1"
    return env


# ---------------------------------------------------------------- 命令映射
def _scene_flag(scene: str) -> list[str]:
    """按场景取数（空 = 当前场景）。判定在 mve/scene_env.py，这里只透传。"""
    return ["--scene", str(scene)] if str(scene or "").strip() else []


def _cmd_ask(q: str, _iters: int, scene: str = "") -> list[str]:
    return [PY, "mve/task_intake.py", "ask", q] + _scene_flag(scene)


def _cmd_retry(tid: str, _iters: int, scene: str = "") -> list[str]:
    return [PY, "mve/task_intake.py", "retry", tid] + _scene_flag(scene)


def _cmd_learn(iters: int, scene: str = "", question: str = "") -> list[str]:
    """学一轮。**按场景分两条路**：

    · Scene A（VLML 数据域）→ `voyager_vlml.loop`（原版课程/技能库/图谱那套）
    · 其它场景（自带 duckdb / 本机）→ `mve/scene_learn.py --question …`，
      即 `learn_orchestrate.start_learning`：围着**这条失败的题**拆课、补缺口、
      取数、入库。不能走 loop —— 它的课程来自 VLML 的 topic_graph，
      在工资场景会出电竞题（学了半天与工资无关）。
    """
    sc = str(scene or "").strip()
    if sc:
        try:
            import scene_env
            kind = scene_env.kind_of(sc)
        except Exception:
            kind = ""
        if kind and kind != "vlml":
            cmd = [PY, "mve/scene_learn.py", "--scene", sc,
                   "--max-iterations", "1", "--max-retries", str(max(1, int(iters or 4))),
                   "--curriculum", "queue"]
            if str(question or "").strip():
                cmd += ["--question", str(question).strip()]
            return cmd
    cmd = [PY, "-m", "voyager_vlml.loop", "--rounds", "1", "--iters", str(iters)]
    return cmd + _scene_flag(sc)


def _cmd_sub(tid: str, _iters: int, scene: str = "") -> list[str]:
    return [PY, "mve/task_intake.py", "sub", tid]


def _cmd_list(_iters: int, scene: str = "") -> list[str]:
    return [PY, "mve/task_intake.py", "list"]


# action -> (构造命令, 中文名, 需要哪些参数)
ACTIONS: dict[str, tuple] = {
    "ask":   (_cmd_ask,   "导入并试答", "q"),
    "learn": (_cmd_learn, "学一轮",     ""),
    "retry": (_cmd_retry, "重试这条",   "tid"),
    "sub":   (_cmd_sub,   "看子目标",   "tid"),
    "list":  (_cmd_list,  "列账本",     ""),
}


def start(action: str, *, q: str = "", tid: str = "", iters: int = 4,
          scene: str = "") -> dict[str, Any]:
    """起一个后台子进程。返回 {id,title,cmd} 或 {error}。"""
    if action not in ACTIONS:
        return {"error": f"不认识的动作：{action}"}
    build, label, need = ACTIONS[action]
    if need == "q" and not str(q).strip():
        return {"error": "先把问题填上"}
    if need == "tid" and not str(tid).strip():
        return {"error": "先把 task_id 填上"}
    sc = str(scene or "").strip()
    if action == "ask":
        cmd = build(str(q).strip(), iters, sc)
    elif action in ("retry", "sub"):
        cmd = build(str(tid).strip(), iters, sc)
    elif action == "learn":
        # 非 VLML 场景要围着**当前待学的那条题**学，所以把问题带下去。
        cmd = build(int(iters or 4), sc, str(q or "").strip() or _focus_question())
    else:                       # list
        cmd = build(int(iters or 4), sc)

    jid = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[jid] = {"id": jid, "action": action, "title": label, "scene": sc,
                      "status": "running", "output": "", "code": None,
                      "started": time.time(), "done_at": None}

    def run() -> None:
        job = _jobs[jid]
        try:
            p = subprocess.run(cmd, cwd=str(ROOT), env=_env(),
                               capture_output=True, text=True, timeout=JOB_TIMEOUT)
            out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
            job.update(status="done", code=p.returncode, output=out[-MAX_OUTPUT:])
        except subprocess.TimeoutExpired as e:
            job.update(status="timeout", code=-1,
                       output=(str(e.stdout or "") + f"\n（超时：{JOB_TIMEOUT // 60} 分钟）")[-MAX_OUTPUT:])
        except Exception as e:  # noqa: BLE001
            job.update(status="error", code=-1, output=f"{type(e).__name__}: {e}")
        finally:
            job["done_at"] = time.time()

    threading.Thread(target=run, daemon=True).start()
    return {"id": jid, "title": label,
            "cmd": " ".join(cmd[-3:])}


def _focus_question() -> str:
    """账本里第一条还没答出的题（学一轮时围着它学）。没有就返回空。"""
    for t in tasks(limit=50):
        if str(t.get("status")) != "solved":
            return str(t.get("question") or "")
    return ""


def job(jid: str) -> dict[str, Any] | None:
    j = _jobs.get(str(jid))
    if j is None:
        return None
    return {k: j.get(k) for k in
            ("id", "action", "title", "status", "output", "code", "started", "done_at")}


def tasks(limit: int = 20) -> list[dict[str, Any]]:
    """账本（直接读 failed_tasks.json，不走子进程 —— 面板 4 秒刷一次，省得起进程）。"""
    try:
        rows = json.loads(TASKS_PATH.read_text(encoding="utf-8") or "[]")
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for r in rows[-int(limit or 20):][::-1]:
        if not isinstance(r, dict):
            continue
        out.append({
            "task_id": str(r.get("task_id") or ""),
            "status": str(r.get("status") or ""),
            "tries": r.get("tries") or 0,
            "question": str(r.get("question") or "")[:120],
            "answer": str(r.get("answer") or "")[:160],
            "subtopics": len(r.get("subtopics") or []),
            "done": len(r.get("done_subtopics") or []),
            "ready_for_retry": bool(r.get("ready_for_retry")),
            "last_reason": str(r.get("last_reason") or "")[:160],
            "updated_at": str(r.get("updated_at") or ""),
        })
    return out
