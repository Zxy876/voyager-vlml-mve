#!/usr/bin/env python3
"""驾驶舱：让面板能一键启动 / 停止 Voyager。

为什么单独开一个文件，而不是在 dashboard.py 里 subprocess.Popen：
　面板是 HTTP 服务，进程句柄挂在请求里，面板一重启就丢了 —— 人点过"启动"
　却查不到在不在跑，比不给按钮更糟。所以驾驶舱自己是一个**独立子进程**，
　状态写在文件里，面板只读文件。

   面板 ──Popen──> pilot.py _run ──Popen──> run_mve.py --llm
     │                   ↑                      │
     └── 读 pilot_state.json ──────────────────┘

三种任务（`--job`，面板上三个键钮）：
  practice  —— 跑 run_mve.py：练题，**带**知识图谱
  placement —— 跑 exam.py --all：**摸底**，全库撤掉图谱考一遍（~7 分钟）
  learn     —— 跑 learn.py --units N：学习单元 = 练一题 + 撤图谱重考同一题

两种模式（仅 practice 有）：
  once —— 跑一题就退出（出题器选题，或钉住指定题）
  loop —— 跑完一题**再问出题器要下一题**，一直跑到人点停止。
          这才是"自己接自适应出题"：选题权在出题器手里，不在人手里。
          （run_mve.py 不给 --topic 就会调 planner.select_next()）

停止语义：SIGTERM 给驾驶舱进程，它转给当前在跑的 run_mve。
另外写 stop_requested —— 防止面板重启后旧状态被误读成"还在跑"。
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
PROJECT = ROOT.parent
STATE = ROOT / "pilot_state.json"
LOG = ROOT / "pilot.log"


def _pick_python() -> str:
    """挑一个**能 import duckdb 的**解释器来跑 run_mve。

    坑（实测踩到）：驾驶舱用 sys.executable 起子进程，也就是**谁启动面板就
    用谁的解释器**。用没装 VLML 依赖的 python 起面板 → 点启动 → run_mve 直接
    `ModuleNotFoundError: No module named 'duckdb'`，退出码 1，人只看到
    "已停止（run_mve 退出码 1）"，根本不知道是解释器选错了。

    所以启动时先探一遍：能用 `import duckdb` 的那个才拿来跑题。
    MVE_PY 环境变量可强制指定。
    """
    cands = [os.environ.get("MVE_PY") or "", sys.executable or ""]
    cands += sorted(str(p) for p in (Path.home() / ".workbuddy/binaries/python/envs").glob("*/bin/python"))
    cands.append("python3")
    for c in cands:
        if not c:
            continue
        try:
            r = subprocess.run([c, "-c", "import duckdb"],
                               capture_output=True, timeout=20)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return sys.executable or "python3"


PY = _pick_python()

# 面板上三个键钮对应三种活。摸底 / 学习单元都要跑好几分钟，
# 单题超时（15 分钟）对它们不够 —— 学习单元 8 个要 15 分钟以上。
JOB_LABEL = {"practice": "练题（带图谱）",
             "placement": "摸底（全库撤图谱考一遍）",
             "learn": "学习单元（练一题 + 撤图谱重考）"}
JOB_TIMEOUT = {"practice": 900, "placement": 1800, "learn": 5400}

# 当前在跑的 run_mve 子进程（停止时要一起带走，否则会留孤儿）
_CHILDREN: list[subprocess.Popen] = []


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write(state: dict[str, Any]) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _alive(pid: int) -> bool:
    """进程是否还活着。

    坑：光用 `os.kill(pid, 0)` 会把**僵尸进程**判成活着 —— 进程已经死了、
    但父进程没 wait 它，pid 还在表里，kill 探测照样返回成功。
    实测就栽在这：点了停止，面板还显示"运行中"。
    所以先用 waitpid(WNOHANG) 把它回收掉，回收到就说明确实已退出。
    """
    try:
        wpid, _status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False
    except ChildProcessError:
        pass  # 不是本进程的子进程（面板重启过），退回 kill 探测
    except OSError:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, PermissionError, OSError):
        return False
    return True


def read() -> dict[str, Any]:
    """当前状态。进程不在了就自动判定为已停 —— 不信任文件里的 running 位。"""
    if not STATE.exists():
        return {"running": False, "mode": "", "topic": "", "rounds": 3,
                "iterations": 0, "pid": None, "started_at": "",
                "stopped_at": "", "last_exit": None, "why": "从未启动过"}
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"running": False, "why": "状态文件损坏"}

    pid = st.get("pid")
    # 文件说在跑但进程没了 —— 崩了或被强杀，如实报
    if st.get("running") and pid and not _alive(int(pid)):
        st["running"] = False
        st["why"] = "进程已退出（崩溃或被强杀）"
    if not st.get("running") and not st.get("why"):
        st["why"] = "已停止"
    return st


def tail(lines: int = 60) -> str:
    if not LOG.exists():
        return ""
    content = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def _log(text: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(text)
        f.flush()


def start(*, mode: str = "once", topic: str = "", rounds: int = 3,
          use_scope: bool = True, job: str = "practice",
          units: int = 1, transfer: int = 0) -> dict[str, Any]:
    """启动驾驶舱（自己再起子进程）。

    job 决定**跑什么** —— 面板上三个键钮对应三种活：
      practice  —— 跑 run_mve.py（练题，带知识图谱）
      placement —— 跑 exam.py --all（**摸底**：全库撤掉图谱考一遍）
      learn     —— 跑 learn.py --units N（学习单元：练一题 + 撤图谱重考）

    为什么摸底必须做成后台任务而不是同步接口：全库 8 道题撤图谱考一遍要
    ~7 分钟，HTTP 请求等不起，而且中途刷新页面不该丢进度。驾驶舱本来就
    是这套机制（状态写文件 + 日志可 tail），直接复用，不另起一套。

    use_scope —— 没显式钉题时，按**练习范围**选题（practice_scope.py）。
    范围是在知识图谱页上点「练习此知识点」设的；设了范围，人就不用在驾驶舱里
    挑难度 / 选题，界面只剩开始 / 停止（伴学 onboarding.md:82 的做法）。
    """
    cur = read()
    if cur.get("running"):
        return {"ok": False, "error": f"已经在跑了（pid {cur.get('pid')}，"
                                     f"任务 {cur.get('job') or 'practice'}）"}

    mode = "loop" if mode == "loop" else "once"
    rounds = max(1, min(10, int(rounds or 3)))
    job = str(job or "practice")
    if job not in ("practice", "placement", "learn"):
        job = "practice"
    units = max(1, min(20, int(units or 1)))
    transfer = max(0, min(10, int(transfer or 0)))

    scope_label = ""
    if job == "practice" and use_scope and not topic:
        try:
            import practice_scope
            sc = practice_scope.get_scope()
            if sc.get("active"):
                scope_label = str(sc.get("label") or "")
                topic = practice_scope.resolve_topic() or ""
        except Exception:
            scope_label = ""

    cmd = [PY, str(ROOT / "pilot.py"), "_run", "--mode", mode,
           "--rounds", str(rounds), "--job", job,
           "--units", str(units), "--transfer", str(transfer)]
    if topic:
        cmd += ["--topic", str(topic)]

    # start_new_session：脱离面板进程组，面板退出也不带走它
    proc = subprocess.Popen(
        cmd, cwd=str(PROJECT), start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    st = {
        "running": True, "mode": mode, "job": job, "units": units,
        "transfer": transfer, "job_label": JOB_LABEL[job],
        "topic": topic or ("（出题器自选）" if job == "practice" else "（全库 / 出题器自选）"),
        "rounds": rounds, "pid": proc.pid, "started_at": _now(),
        "stopped_at": "", "iterations": 0, "last_exit": None,
        "stop_requested": False, "why": "运行中",
        "scope": scope_label,
    }
    _write(st)
    _log(f"\n===== 启动 {_now()} · 任务 {JOB_LABEL[job]} · 模式 {mode} · "
         f"轮数上限 {rounds}"
         + (f" · 单元 {units}" if job == "learn" else "")
         + (f" · 迁移对照每 {transfer} 单元" if job == "learn" and transfer else "")
         + f" · 选题 {st['topic']}"
         + (f" · 来自练习范围「{scope_label}」" if scope_label else "")
         + f" · pid {proc.pid} =====\n")
    return {"ok": True, **st}


def stop() -> dict[str, Any]:
    """停止。先礼貌 SIGTERM（驾驶舱自己会带走 run_mve），5 秒后还在就 SIGKILL。

    不能直接 SIGKILL 了事：驾驶舱当时正阻塞在等 run_mve，一杀就留下孤儿，
    run_mve 会继续跑完并往 run_log 里写 —— 人以为停了，数据却还在涨。
    """
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
        # 强杀后驾驶舱没机会写状态，这里补写 —— 否则面板永远显示"运行中"
        try:
            cur = json.loads(STATE.read_text(encoding="utf-8"))
            cur.update({"running": False, "stopped_at": _now(),
                        "why": "已停止（强杀）", "child_pid": None})
            _write(cur)
        except Exception:
            pass
    _log(f"\n===== 停止 {_now()} · 跑了 {st.get('iterations', 0)} 题 =====\n")
    return {"ok": True, "stopped": True, "iterations": st.get("iterations", 0)}


def clear_log() -> None:
    """清空驾驶舱日志（面板上「清屏」用）。"""
    if LOG.exists():
        LOG.unlink()


# ---------------------------------------------------------------------------
# 驾驶舱子进程本体
# ---------------------------------------------------------------------------

def _install_signal_handlers() -> None:
    """收到 SIGTERM 时，先把在跑的 run_mve 带走再退出。

    不加这个 handler，Python 默认行为是直接终止驾驶舱 —— 而它当时正阻塞在
    communicate() 上，run_mve 就成了孤儿继续跑完，日志还会继续长。
    """

    def handler(signum, _frame):
        # 只 terminate 记录在案的 run_mve。
        # 别用 killpg 想"顺手带走整个进程组"：run_mve 是 start_new_session 起的，
        # 早就不在本组里，killpg 唯一的实际效果是把 SIGTERM 又发给自己一次，
        # 打断本 handler，状态还没写完进程就死了（实测：面板显示"崩溃或被强杀"）。
        for p in _CHILDREN:
            try:
                p.terminate()
            except Exception:
                pass
        try:
            cur = json.loads(STATE.read_text(encoding="utf-8"))
            cur.update({"running": False, "stopped_at": _now(),
                        "why": "已停止", "child_pid": None})
            _write(cur)
        except Exception:
            pass
        sys.exit(143)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _run(mode: str, topic: str, rounds: int, job: str = "practice",
         units: int = 1, transfer: int = 0) -> int:
    _install_signal_handlers()

    # 日志文件句柄必须**全程持有**：用 `with` 包住 Popen 会在它返回后立刻关掉 fd，
    # 子进程 exec 时拿到的是个已失效的 fd，实测直接
    #   Fatal Python error: init_sys_streams: ... OSError: [Errno 9] Bad file descriptor
    logf = LOG.open("a", encoding="utf-8")
    try:
        code = _loop(mode, topic, rounds, logf, job, units, transfer)
    finally:
        try:
            logf.close()
        except Exception:
            pass
    return code


def _loop(mode: str, topic: str, rounds: int, logf, job: str = "practice",
          units: int = 1, transfer: int = 0) -> int:
    pid = os.getpid()

    def mark(text: str) -> None:
        """写标记行。走同一个句柄并立刻 flush —— 否则父进程的缓冲会在最后
        才落盘，"第 N 题"这种分隔行会跑到子进程输出后面去，日志顺序全乱。"""
        logf.write(text)
        logf.flush()

    _write({
        "running": True, "mode": mode, "job": job, "units": units,
        "transfer": transfer, "job_label": JOB_LABEL.get(job, job),
        "topic": topic or ("（出题器自选）" if job == "practice" else "（全库 / 出题器自选）"),
        "rounds": rounds, "pid": pid, "started_at": _now(),
        "stopped_at": "", "iterations": 0, "last_exit": None,
        "stop_requested": False, "why": "运行中",
    })

    def asked_to_stop() -> bool:
        try:
            return bool(json.loads(STATE.read_text(encoding="utf-8")).get("stop_requested"))
        except Exception:
            return False

    code = 0
    iterations = 0
    # 摸底与学习单元**只跑一次**：它们自己内部就是一轮全库循环，
    # 驾驶舱再套一层 loop 会变成"摸底完了又摸一遍"。
    max_iterations = 1 if job != "practice" else (200 if mode == "loop" else 1)
    timeout = JOB_TIMEOUT.get(job, 900)

    while iterations < max_iterations and not asked_to_stop():
        iterations += 1
        if job == "placement":
            cmd = [PY, str(ROOT / "exam.py"), "--all"]
            what = f"摸底（全库撤图谱考一遍）· {_now()}"
        elif job == "learn":
            cmd = [PY, str(ROOT / "learn.py"), "--units", str(units),
                   "--rounds", str(rounds)]
            if transfer:
                cmd += ["--transfer", str(transfer)]
            what = (f"学习单元 {units} 个"
                    + (f"（每 {transfer} 单元加考一道没练过的题）" if transfer else "")
                    + f" · {_now()}")
        else:
            cmd = [PY, str(ROOT / "run_mve.py"), "--llm", "--rounds", str(rounds)]
            if topic:
                cmd += ["--topic", topic]
            what = f"第 {iterations} 题 · {_now()} · {' '.join(cmd[-4:])}"

        mark(f"\n----- {what} -----\n")
        try:
            cur = json.loads(STATE.read_text(encoding="utf-8"))
            cur["iterations"] = iterations
            cur["running"] = True
            _write(cur)
        except Exception:
            pass

        try:
            # 三个决定，各自都有踩过的理由：
            # 1) Popen 而非 subprocess.run —— 要拿 pid，否则停止时杀不掉 run_mve
            #    （只杀驾驶舱会留下孤儿，它还会继续往 run_log 里写）
            # 2) stdout 直接重定向到日志文件而非 PIPE —— 用 communicate() 的话
            #    要等整题跑完才一次性吐出来，面板上的运行日志会一直空白，
            #    人点了启动却看不见它在干嘛
            # 3) start_new_session —— 脱离进程组，面板退出不带走它
            # 4) PYTHONUNBUFFERED —— 不加这个，run_mve 的 print 走块缓冲（4KB），
            #    跑一整题都刷不出来，面板上的运行日志还是空的
            # 5) stdin=DEVNULL —— 踩过：从命令行（python -c / 非交互 shell）启动驾驶舱时，
            #    父进程的 stdin 可能是个已经关掉的 fd，run_mve 继承过去就直接
            #    Fatal Python error: init_sys_streams ... Bad file descriptor，
            #    整题一次都没跑就退出码 1。stdout/stderr 都重定向了，stdin 没管就中招。
            env = dict(os.environ, PYTHONUNBUFFERED="1")
            # 用 os.open 拿**裸 fd**，而不是传文本句柄：
            # 共享同一个 TextIOWrapper 时，第二次 Popen 实测又拿到坏 fd
            # （同样的 init_sys_streams / Bad file descriptor）。
            # 每次迭代新开一个 fd，用完即关，最稳。
            fd = os.open(str(LOG), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(PROJECT), stdin=subprocess.DEVNULL,
                    stdout=fd, stderr=fd, env=env,
                    start_new_session=True,
                )
                _CHILDREN.append(proc)
                try:
                    cur = json.loads(STATE.read_text(encoding="utf-8"))
                    cur["child_pid"] = proc.pid
                    _write(cur)
                except Exception:
                    pass
                try:
                    code = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    mark(f"\n[驾驶舱] 单个任务超过 {timeout // 60} 分钟，已杀掉\n")
                    code = 124
                _CHILDREN.remove(proc)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                cur = json.loads(STATE.read_text(encoding="utf-8"))
                cur["child_pid"] = None
                _write(cur)
            except Exception:
                pass
        except Exception as e:
            mark(f"\n[驾驶舱] 起进程失败：{type(e).__name__}: {e}\n")
            code = 1

        if code != 0:
            # 非 0 就停，并把退出码摆到面板上 —— 反复重试一个结构性错误只会污染日志，
            # 而且如果不暴露 code，人会以为是"跑完了"而不是"崩了"
            mark(f"\n[驾驶舱] 「{JOB_LABEL.get(job, job)}」退出码 {code}"
                 f"（非 0 → 停止，不重试）\n")
            break

    label = JOB_LABEL.get(job, job)
    try:
        cur = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    cur.update({
        "running": False, "stopped_at": _now(), "last_exit": code,
        "iterations": iterations, "child_pid": None,
        "why": "已停止" if code == 0 else f"已停止（{label} 退出码 {code}）",
    })
    _write(cur)
    mark(f"\n===== 驾驶舱结束 {_now()} · 任务 {label} · "
         f"共 {iterations} 次 · 退出码 {code} =====\n")
    return code


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "_run":
        m = "loop"
        if "--mode" in argv:
            m = argv[argv.index("--mode") + 1]
        r = 3
        if "--rounds" in argv:
            r = int(argv[argv.index("--rounds") + 1])
        t = ""
        if "--topic" in argv:
            t = argv[argv.index("--topic") + 1]

        def _flag(name: str, default: str) -> str:
            return argv[argv.index(name) + 1] if name in argv else default

        job = _flag("--job", "practice")
        units = int(_flag("--units", "1"))
        transfer = int(_flag("--transfer", "0"))
        sys.exit(_run(m, t, r, job, units, transfer))

    if argv and argv[0] == "status":
        st = read()
        print(json.dumps({**st, "tail": tail(20)}, ensure_ascii=False, indent=2))
        sys.exit(0)

    if argv and argv[0] == "stop":
        print(json.dumps(stop(), ensure_ascii=False))
        sys.exit(0)

    print(__doc__)
    print("用法：python mve/pilot.py status | stop")
