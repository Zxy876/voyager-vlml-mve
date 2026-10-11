#!/usr/bin/env python3
"""Voyager 驾驶舱：让 MVE 面板能一键启动 / 停止**真实 MC 里的学习**。

为什么单独开一个文件（同 mve/pilot.py 的论证）：面板是 HTTP 服务，进程句柄挂在请求里
面板一重启就丢了 —— 人点过「启动」却查不到在不在跑，比不给按钮更糟。所以驾驶舱自己
是一个**独立子进程**，状态写在文件里，面板只读文件。

   面板 ──Popen──> voyager_pilot.py _run ──Popen──> run_voyager_learn.py（venv）
    │                   ↑                              │
    └── 读 voyager_pilot_state.json ───────────────────┘

停止语义（镜像 MVE 面板「停止」键）：
  SIGTERM 给驾驶舱进程 → 转发给 run_voyager_learn.py（它有 SIGTERM 处理器，会在
  当前任务结束后调 voyager.request_stop() 优雅退出 learn 循环）→ 驾驶舱等子进程退出后
  写最终状态。技能已按任务落盘，不会被硬杀丢半个技能。

两类清除（对应 MVE 的清库 / 清记忆，且都由 voyager_learn_reset 实现，先备份再删）：
  清库   —— 删 ckpt/skill/（已学的 JS 技能库）
  清记忆 —— 删 ckpt/curriculum/ + ckpt/events/（课程进度 / QA 缓存 / 事件历史）

用法（被面板调用，不直接跑）：
    python mve/voyager_pilot.py status | stop
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "voyager_pilot_state.json"
LOG = ROOT / "voyager_pilot.log"

# ---- Voyager 安装位置（VPS 上 /root/voyager，本地可调环境变量覆盖）----
from voyager_paths import (  # noqa: E402
    VOYAGER_DIR, VOYAGER_PY, SECRET_FILE, CKPT_DIR, DEFAULT_MAX_ITER, PROGRESS,
)

_JOB_LABEL = "真实 MC 思路学习（自动课程）"
# 子进程（run_voyager_learn.py 的 learn 循环）单个任务可能跑很久；
# 停止时给 120s 宽限让当前任务收尾，超时才硬杀。
STOP_GRACE = 120

_CHILDREN: list[subprocess.Popen] = []


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write(state: dict[str, Any]) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _alive(pid: int) -> bool:
    """进程是否还活着（先用 waitpid 回收僵尸，再 kill 探测）。"""
    try:
        wpid, _status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, PermissionError, OSError):
        return False
    return True


def _secret_key() -> str:
    """从密钥文件读出 DEEPSEEK_API_KEY（支持 `KEY=val` 或纯 key 两种写法）。"""
    if not SECRET_FILE.exists():
        return ""
    txt = SECRET_FILE.read_text(encoding="utf-8", errors="replace").strip()
    if not txt:
        return ""
    if "=" in txt.splitlines()[0]:
        for line in txt.splitlines():
            line = line.strip()
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return txt


def _progress() -> dict[str, Any]:
    """读 run_voyager_learn.py 写的进度（completed/failed/iteration）。"""
    if not PROGRESS.exists():
        return {}
    try:
        return json.loads(PROGRESS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def read() -> dict[str, Any]:
    """当前状态。进程不在了就自动判定为已停 —— 不信任文件里的 running 位。"""
    base: dict[str, Any] = {
        "running": False, "pid": None, "started_at": "", "stopped_at": "",
        "iterations": 0, "max_iterations": DEFAULT_MAX_ITER,
        "last_exit": None, "why": "从未启动过", "completed_count": 0,
        "failed_count": 0, "completed": [], "failed": [], "mc_port": 25565,
    }
    if not STATE.exists():
        return base
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {**base, "why": "状态文件损坏"}
    pid = st.get("pid")
    if st.get("running") and pid and not _alive(int(pid)):
        st["running"] = False
        st["why"] = "进程已退出（崩溃或被强杀）"
    if not st.get("running") and not st.get("why"):
        st["why"] = "已停止"
    # 合并实时进度（即使进程已退，也把最后进度带出来）
    prog = _progress()
    if prog:
        st["completed_count"] = prog.get("completed_count", 0)
        st["failed_count"] = prog.get("failed_count", 0)
        st["completed"] = prog.get("completed", [])
        st["failed"] = prog.get("failed", [])
        if prog.get("iteration") is not None:
            st["iterations"] = prog["iteration"]
    return {**base, **st}


def tail(lines: int = 80) -> str:
    if not LOG.exists():
        return ""
    content = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def _log(text: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(text)
        f.flush()


def start(*, mc_port: int = 25565, max_iterations: int = DEFAULT_MAX_ITER) -> dict[str, Any]:
    cur = read()
    if cur.get("running"):
        return {"ok": False, "error": f"已经在跑了（pid {cur.get('pid')}）"}

    cmd = [
        str(VOYAGER_PY), str(VOYAGER_DIR / "run_voyager_learn.py"),
        "--mc-port", str(mc_port), "--ckpt-dir", CKPT_DIR,
        "--max-iterations", str(max_iterations),
    ]
    # 启动时把密钥文件里的 DEEPSEEK_API_KEY 注入子进程环境
    env = dict(os.environ)
    key = _secret_key()
    if key:
        env["DEEPSEEK_API_KEY"] = key
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        cmd, cwd=str(VOYAGER_DIR), env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    st = {
        "running": True, "pid": proc.pid, "started_at": _now(),
        "stopped_at": "", "iterations": 0, "max_iterations": max_iterations,
        "mc_port": mc_port, "last_exit": None, "why": "学习中",
        "completed_count": 0, "failed_count": 0, "completed": [], "failed": [],
        "stop_requested": False,
    }
    _write(st)
    _log(f"\n===== 启动 Voyager 学习 {_now()} · mc_port {mc_port} · "
         f"上限 {max_iterations} 轮 · pid {proc.pid} · key {'有' if key else '无'} =====\n")
    if not key:
        _log("[驾驶舱] 警告：没找到 DEEPSEEK_API_KEY（/root/.voyager_secret），"
             "LLM 调用会失败。\n")
    return {"ok": True, **st}


def stop() -> dict[str, Any]:
    st = read()
    pid = st.get("pid")
    if not st.get("running") or not pid:
        return {"ok": True, "stopped": True, "note": "本来就没在跑"}
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (ProcessLookupError, ValueError, PermissionError, OSError) as e:
        return {"ok": True, "stopped": True, "note": f"进程已不在（{e}）"}

    deadline = time.time() + 5
    while time.time() < deadline:
        time.sleep(0.3)
        if not read().get("running"):
            break
    else:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (ProcessLookupError, ValueError, PermissionError, OSError):
            pass
        try:
            cur = json.loads(STATE.read_text(encoding="utf-8"))
            cur.update({"running": False, "stopped_at": _now(),
                        "why": "已停止（强杀）", "child_pid": None})
            _write(cur)
        except Exception:
            pass
    _log(f"\n===== 停止 {_now()} · 已学 {st.get('completed_count', 0)} 个技能 =====\n")
    return {"ok": True, "stopped": True, "completed_count": st.get("completed_count", 0)}


def clear_log() -> None:
    if LOG.exists():
        LOG.unlink()


# ---------------------------------------------------------------------------
# 驾驶舱子进程本体
# ---------------------------------------------------------------------------

def _install_signal_handlers() -> None:
    """收到 SIGTERM 时，转发给在跑的 run_voyager_learn（它自己会优雅退出 learn 循环），
    并标记停止。不要直接 sys.exit：要让主循环等子进程退出后写最终状态，否则若在这里
    退出，子进程（start_new_session 起的，不在本组）会成为孤儿继续跑。"""

    def handler(signum, _frame):
        for p in _CHILDREN:
            try:
                p.terminate()  # SIGTERM → run_voyager_learn 的 handler 调 request_stop()
            except Exception:
                pass
        try:
            cur = json.loads(STATE.read_text(encoding="utf-8"))
            cur.update({"running": False, "stop_requested": True,
                        "stopped_at": _now(), "why": "已停止（请求中）",
                        "child_pid": None})
            _write(cur)
        except Exception:
            pass

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _run(mc_port: int, max_iterations: int) -> int:
    _install_signal_handlers()
    logf = LOG.open("a", encoding="utf-8")
    try:
        code = _loop(mc_port, max_iterations, logf)
    finally:
        try:
            logf.close()
        except Exception:
            pass
    return code


def _loop(mc_port: int, max_iterations: int, logf) -> int:
    pid = os.getpid()

    def mark(text: str) -> None:
        logf.write(text)
        logf.flush()

    _write({
        "running": True, "pid": pid, "started_at": _now(), "stopped_at": "",
        "iterations": 0, "max_iterations": max_iterations, "mc_port": mc_port,
        "last_exit": None, "why": "学习中", "completed_count": 0,
        "failed_count": 0, "completed": [], "failed": [], "stop_requested": False,
    })

    env = dict(os.environ)
    key = _secret_key()
    if key:
        env["DEEPSEEK_API_KEY"] = key
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [
        str(VOYAGER_PY), str(VOYAGER_DIR / "run_voyager_learn.py"),
        "--mc-port", str(mc_port), "--ckpt-dir", CKPT_DIR,
        "--max-iterations", str(max_iterations),
    ]
    what = f"真实 MC 思路学习（自动课程）· mc_port {mc_port} · 上限 {max_iterations} 轮 · {_now()}"
    mark(f"\n----- {what} -----\n")

    fd = os.open(str(LOG), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(VOYAGER_DIR), env=env, stdin=subprocess.DEVNULL,
            stdout=fd, stderr=fd, start_new_session=True,
        )
        _CHILDREN.append(proc)
        try:
            cur = json.loads(STATE.read_text(encoding="utf-8"))
            cur["child_pid"] = proc.pid
            _write(cur)
        except Exception:
            pass
        # 正常等子进程退出（learn 循环跑完或超过 max_iterations）。
        # 若收到停止信号，handler 已转发 SIGTERM 给子进程，子进程会在任务边界退出；
        # 这里给 STOP_GRACE 宽限，超时才硬杀，避免卡死。
        try:
            code = proc.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            mark(f"\n[驾驶舱] 子进程 {STOP_GRACE}s 内未退出，已强杀\n")
            code = 124
        _CHILDREN.remove(proc)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    prog = _progress()
    try:
        cur = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    cur.update({
        "running": False, "stopped_at": _now(), "last_exit": code,
        "child_pid": None, "stop_requested": False,
        "completed_count": (prog.get("completed_count", 0)
                           if prog else cur.get("completed_count", 0)),
        "failed_count": (prog.get("failed_count", 0)
                         if prog else cur.get("failed_count", 0)),
        "why": "已停止" if code == 0 else f"已停止（退出码 {code}）",
    })
    _write(cur)
    mark(f"\n===== 驾驶舱结束 {_now()} · 退出码 {code} · "
         f"已学 {cur.get('completed_count', 0)} 个技能 =====\n")
    return code


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "_run":
        mp = 25565
        mi = DEFAULT_MAX_ITER
        if "--mc-port" in argv:
            mp = int(argv[argv.index("--mc-port") + 1])
        if "--max-iterations" in argv:
            mi = int(argv[argv.index("--max-iterations") + 1])
        sys.exit(_run(mp, mi))

    if argv and argv[0] == "status":
        st = read()
        print(json.dumps({**st, "tail": tail(20)}, ensure_ascii=False, indent=2))
        sys.exit(0)

    if argv and argv[0] == "stop":
        print(json.dumps(stop(), ensure_ascii=False))
        sys.exit(0)

    print(__doc__)
    print("用法：python mve/voyager_pilot.py status | stop")
