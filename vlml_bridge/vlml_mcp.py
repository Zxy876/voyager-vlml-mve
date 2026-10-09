#!/usr/bin/env python3
"""VLML0 的 MCP 客户端（stdio 传输）。

为什么单独写一个而不是 `pip install mcp`：
  VLML 的服务端是 FastMCP，跑 `stdio`（`vlml/src/vlml/server.py:98-107`）。
  MCP 协议本身很简单 —— 就是**换行分隔的 JSON-RPC 2.0**，初始化握手 +
  `tools/list` + `tools/call`。用一个子进程 + 两个管道就能驱动，不必引入
  一整套 SDK；插件里多一个重依赖不划算，也更容易在猫娘宿主里出问题。

协议最小实现（够用即可）：
  1. 发 `initialize`，回读它的响应
  2. 发 `notifications/initialized`（通知，不用等响应）
  3. `tools/list` 拿工具清单
  4. `tools/call` 调工具，结果在 `result.content[0].text`（JSON 文本）

10 个工具（`vlml/src/vlml/server.py`）：
  报告类 7：match_analysis_report / match_summary_report / match_players_report /
            match_rounds_report / match_economy_report / player_profile_report /
            scouting_report / pattern_detection_report   ← 实际 8 个
  数据类 2：query_sql / get_database_info
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# VLML 源码根 —— 可用环境变量覆盖（不同机器路径不同，写死必然踩坑）
def _find_vlml_src() -> str:
    """按优先级探测 vlml/src 的位置：

    1. 环境变量 VLML_SRC（显式覆盖，换机器最省事）
    2. 项目根布局：__file__ = <项目根>/study_companion/vlml_mcp.py
       → parents[1]/vlml/src（本地开发、跑测试都走这条）
    3. 猫娘运行时布局：插件装在 <userData>/plugins/study_companion/ 里，
       parents[1] 是 plugins/，不是项目根 —— 此时靠插件目录下的 vlml 软链
       （指向真实 VLML 仓库，含 data/*.duckdb）。
       注意不能只靠 resolve()：硬链接会让 resolve() 停在 plugins/ 层。
    """
    env = os.environ.get("VLML_SRC")
    if env:
        return env
    here = Path(__file__).resolve()
    for cand in (here.parents[1] / "vlml" / "src", here.parent / "vlml" / "src"):
        if (cand / "vlml" / "server.py").exists():
            return str(cand)
    # 都没有就按旧默认报错（spawn 时会 FileNotFoundError，容易误判，
    # 这里提前给出可读的原因）
    raise FileNotFoundError(
        "找不到 vlml/src：既不是项目根布局（<根>/vlml/src），"
        "也不是猫娘运行时布局（插件目录/vlml 软链）。"
        "设 VLML_SRC 环境变量指向 VLML 仓库根可解。"
    )


VLML_SRC = Path(_find_vlml_src())
def _looks_like_python(exe: str) -> bool:
    """判断这个可执行文件是不是一个「普通 Python 解释器」。

    猫娘宿主里 sys.executable 是宿主自己的 App 二进制
    （N.E.K.O.app/.../projectneko_server），拿它 executes `-c ...` 会真的
    **再启动一个应用实例**，触发单例/重载逻辑 —— 实测后果是宿主从里面把
    插件进程重启，正在跑的请求直接孤儿化，面板永远「正在解释…」。
    所以宿主二进制既不能用来 spawn VLML server，也不能用来做依赖探测。
    """
    name = Path(exe).name.lower()
    return name.startswith("python")


def _find_vlml_python() -> str:
    """找一个真的能跑 VLML server 的 Python（要有 mcp + duckdb）。

    候选顺序刻意把**宿主自己的解释器排在最后且加了形态校验**：先试环境变量、
    再试系统/虚拟环境的 python3，最后才考虑 sys.executable（并且它必须长得
    像 python）。宁可参考hibit慢一点也不去 spawn 宿主 App。
    """
    import shutil

    candidates: list[str] = []
    env = os.environ.get("VLML_PYTHON")
    if env:
        candidates.append(env)
    candidates += [
        str(Path.home() / ".workbuddy/binaries/python/envs/default/bin/python3"),
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        shutil.which("python3") or "",
    ]
    if sys.executable and _looks_like_python(sys.executable):
        candidates.append(sys.executable)
    for cand in candidates:
        if not cand or not _looks_like_python(cand):
            continue
        if not (Path(cand).exists() or shutil.which(cand)):
            continue
        try:
            probe = subprocess.run(
                [cand, "-c", "import mcp, duckdb"],
                capture_output=True, timeout=8,
            )
        except Exception:
            continue
        if probe.returncode == 0:
            return cand
    # 全军覆没：返回空串，调用方 spawn 时会 FileNotFoundError，
    # 由 vlml_evidence 的 try/except 兜住（退化为空讲），不会让插件崩。
    return ""


VLML_PYTHON = _find_vlml_python()

# 工具名 → 说明（给模型看，决定要不要调）
TOOL_DOCS: dict[str, str] = {
    "match_summary_report": "系列赛概览（元数据、队伍对比、关键指标）。拿不准先调它。",
    "match_analysis_report": "单场深度分析（首血、转换、回合结构等）。",
    "match_players_report": "选手视角（表现、KAST、高光回合）。",
    "match_rounds_report": "逐回合时间线（含半场拆分，可分页）。",
    "match_economy_report": "经济与战术节奏。",
    "player_profile_report": "选手跨系列赛画像。",
    "scouting_report": "对手侦察（跨系列赛）。",
    "pattern_detection_report": "跨大数据集的套路识别（有 min_rounds 门槛）。",
    "query_sql": "自定义 SQL（只读 SELECT）。要精确数字就调它。",
    "get_database_info": "库结构与各表行数。拿不准查哪张表时先调它。",
}


class VlmlMcpError(RuntimeError):
    pass


def _mkdir_on_existing_dir_blocked() -> bool:
    """探测：本环境是否禁止"对已存在的目录再 mkdir"。

    实测（本机）：`Path('/tmp/x').mkdir(exist_ok=True)` 第二次调用会抛
    `PermissionError: EEXIST: file already exists, mkdir ...`。
    而 VLML 每次建库连接都会走 `data_dir.mkdir(exist_ok=True)`
    （`vlml/src/vlml/db/manager.py:21`）—— 于是 `query_sql` /
    `get_database_info` 全部失败，报的却是 mkdir 的错，很容易误判成
    "MCP 没接上"。**先探测、再决定要不要绕**，不要无脑打补丁。
    """
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as td:
        d = _P(td) / "probe"
        try:
            d.mkdir()
            d.mkdir(exist_ok=True)
        except PermissionError:
            return True
        except Exception:
            return False
    return False


# 兼容引导：只在上面探测为真时才用。它把"对已存在目录的 mkdir"变成空操作，
# 其余情况原样放行 —— 不改 VLML 源码，只在启动它的那一次进程里生效。
_BOOTSTRAP = '''
from pathlib import Path as _P
_orig = _P.mkdir
def _mkdir(self, mode=0o777, parents=False, exist_ok=False):
    try:
        return _orig(self, mode=mode, parents=parents, exist_ok=exist_ok)
    except PermissionError:
        if exist_ok and self.is_dir():
            return None
        raise
_P.mkdir = _mkdir
import runpy
runpy.run_module("vlml.server", run_name="__main__")
'''


class VlmlMcpClient:
    """一个子进程、一条 stdio 连接。**进程复用**，不要每次调用都起一次。"""

    def __init__(
        self,
        *,
        src: Path | str | None = None,
        python: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._src = str(src or VLML_SRC)
        self._python = python or VLML_PYTHON
        self._timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._id = 0
        self._tools: list[dict[str, Any]] | None = None
        self._shim: bool | None = None

    def _need_mkdir_shim(self) -> bool:
        if self._shim is None:
            self._shim = _mkdir_on_existing_dir_blocked()
        return self._shim

    # ---------------- 进程与协议 ----------------
    def _spawn(self) -> subprocess.Popen:
        if not self._python:
            raise VlmlMcpError(
                "找不到能跑 VLML server 的 Python（需要 mcp + duckdb）。"
                "设 VLML_PYTHON 环境变量指定解释器。"
            )
        env = dict(os.environ)
        env["PYTHONPATH"] = self._src + os.pathsep + env.get("PYTHONPATH", "")
        argv = [self._python, "-m", "vlml.server"]
        if self._need_mkdir_shim():
            # 见 _mkdir_on_existing_dir_blocked 的注释：本机禁止对已存在目录
            # mkdir，VLML 建连接时必然踩到。用一个一次性的引导脚本绕开，
            # 不改 VLML 源码。
            boot = Path(tempfile.gettempdir()) / "_vlml_mcp_bootstrap.py"
            boot.write_text(_BOOTSTRAP, encoding="utf-8")
            argv = [self._python, str(boot)]
        return subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=self._src,
            text=True,
            bufsize=1,
        )

    def _send(self, proc: subprocess.Popen, payload: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise VlmlMcpError("MCP 进程没活着（先调 start()）")
        rid = self._next_id()
        self._send(proc, {"jsonrpc": "2.0", "id": rid, "method": method,
                          "params": params or {}})
        assert proc.stdout is not None
        deadline = time.time() + self._timeout
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                raise VlmlMcpError("MCP 进程关闭了 stdout")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue          # 子进程可能打日志，跳过非 JSON 行
            if isinstance(msg, dict) and msg.get("id") == rid:
                if "error" in msg:
                    raise VlmlMcpError(f"{method} 出错：{msg['error']}")
                return msg.get("result") or {}
        raise VlmlMcpError(f"{method} 超时（{self._timeout}s）")

    def start(self) -> None:
        with self._lock:
            if self._proc is not None and self._poll_alive():
                return
            self._proc = self._spawn()
            # 1) 握手
            self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "study-companion", "version": "0.1.0"},
            })
            # 2) 通知已初始化（不回读）
            assert self._proc.stdin is not None
            self._send(self._proc, {"jsonrpc": "2.0", "method": "notifications/initialized",
                                    "params": {}})

    def _poll_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        with self._lock:
            if self._proc is None:
                return
            try:
                if self._proc.poll() is None:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
            finally:
                self._proc = None
                self._tools = None

    # ---------------- 对外 API ----------------
    def list_tools(self) -> list[dict[str, Any]]:
        self.start()
        if self._tools is None:
            res = self._request("tools/list", {})
            self._tools = list(res.get("tools") or [])
        return self._tools

    def tool_names(self) -> list[str]:
        return [str(t.get("name")) for t in self.list_tools()]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """调一个工具，返回**解析后的**结果（dict / str / 原始 content）。"""
        self.start()
        res = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        content = res.get("content") or []
        if not content:
            return None
        first = content[0]
        text = first.get("text") if isinstance(first, dict) else None
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return content

    def __enter__(self) -> "VlmlMcpClient":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def environment_report() -> dict[str, Any]:
    """自检：能不能真的把 VLML MCP 拉起来、拿到 10 个工具。

    这条必须能跑 —— 不然"解释接了 MCP"只是嘴上说说。
    """
    out: dict[str, Any] = {
        "vlml_src": str(VLML_SRC),
        "vlml_src_exists": Path(VLML_SRC).is_dir(),
        "python": VLML_PYTHON,
    }
    try:
        with VlmlMcpClient(timeout=60.0) as c:
            names = c.tool_names()
            out["ok"] = True
            out["tool_count"] = len(names)
            out["tools"] = names
    except Exception as exc:
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


if __name__ == "__main__":
    print(json.dumps(environment_report(), ensure_ascii=False, indent=1))
