#!/usr/bin/env python3
"""MCP 传输层网关 —— 同构猫娘计划 mcp_adapter（transport + route + 工具暴露）。

猫娘取证（动本模块前先读这几处源码）
----------------------------------
`plugin/plugins/mcp_adapter/__init__.py`：
  :342-410 `_connect_stdio`：`create_subprocess_exec(stdin/stdout/stderr=PIPE)`
    → `initialize` → `notifications/initialized` → `tools/list` 解析工具表；
  :910-950 `_send_request`：帧 = `json.dumps({jsonrpc:"2.0", id, method,
    params}) + "\\n"` —— **换行分隔 JSON**（不是 LSP 的 Content-Length 头），
    pending future 按自增 int id 配对；
  :1014+ `_read_loop`：按 id 认领响应，无 id 的通知只记日志；
  :747-800 `call_tool`：`tools/call {name, arguments}`，失败走 `{"error": ...}`
    字典**不上抛**；
`plugin/plugins/mcp_adapter/router.py`：
  :65   `tool_id = f"mcp_{server_name}_{tool.name}"`，重名跳过并记 error；
  :204-250 `decide()`：SELF（tool_index 命中）/ DROP（查无此工具）。

MVE 同构映射
------------
    猫娘概念                    MVE 对应
    mcp_adapter 插件          → 本模块 MCPGateway
    MCPClient                 → MCPClient（只实现 stdio，见下方范围声明）
    MCPRouteEngine.decide()   → MCPGateway.decide()：SELF=本地注册表已有条目
                                / REMOTE=远端 server 暴露 / DROP=查无此工具
    远端工具注入聊天上下文    → expose_as_actions()：远端工具变成**场景动作条目**
                                （origin="mcp"）→ task_decompose.reachability
                                从此能在注册表里判到它们

和进程内传输层的关系
------------------
`action_code.MCPHandle`（10 个 VLML 工具）是**进程内**传输层 —— VLML 自己
的 MCP server 已编译进本进程，再起个子进程通信是负资产。本网关管**外部**
MCP server（本地 stdio 子进程）：猫娘插件生态里的 MCP 工具、任何第三方
stdio MCP server。两条路在注册表上汇合：expose_as_actions 注册的远端条目
与 builtin / forged / learned 条目并列，编排器（orchestrate）一视同仁。

⚠️ 范围声明（不装全能）
----------------------
只实现 **stdio** 一种传输。猫娘还支持 sse / streamable-http（要 aiohttp）；
MVE 当前没有任何 HTTP MCP server 要接，为"将来可能"引一个 HTTP 客户端库
是负资产。需要时照猫娘 `_connect_http` 补一个分支，decide/call 的上层接口
不用动。配置缺失/连接失败/工具查无 —— 全部如实报，不静默跳过。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "mcp_servers.json"

# 照猫娘 initialize 的协议版本与 clientInfo 形状
PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "mve-mcp-gateway", "version": "1.0"}


# ---------------------------------------------------------------------------
# 配置路径/命令的可移植解析（本地与服务器共用同一份 mcp_servers.json）
# ---------------------------------------------------------------------------
def _is_abs(path: str) -> bool:
    return bool(path) and (path.startswith("/") or (len(path) > 1 and path[1] == ":"))


# 哨兵值：写 auto/python/python3/空都表示「用面板进程自己的解释器」
_PY_SENTINELS = {"", "auto", "python", "python3"}


def _resolve_command(cmd: str) -> str:
    """命令解析（本地与服务器共用同一份 mcp_servers.json）：
      · 绝对路径 → 原样（如 /root/mve/.venv/bin/duckduckgo-mcp-server）；
      · 哨兵 auto / python / python3 / 空 → 当前解释器（面板进程的解释器：
        服务器上 /root/mve/.venv/bin/python，本地是管理虚拟环境 python）；
      · 其它名字（npx / node / duckduckgo-mcp-server…）按序尝试：
        PATH 里 shutil.which → 面板解释器同目录（venv 的 bin/，console script
        装在哪，server 上 /root/mve/.venv/bin 不在 systemd PATH 里也能找到）；
      都找不到 → 原样返回，create_subprocess_exec 如实报连接失败。
    """
    cmd = str(cmd or "").strip()
    if _is_abs(cmd):
        return cmd
    if cmd.lower() in _PY_SENTINELS:
        return sys.executable
    found = shutil.which(cmd)
    if found:
        return found
    # 面板解释器同目录（venv 的 bin/）：console script 装在哪，server 上
    # /root/mve/.venv/bin 不在 systemd PATH 里也能找到。注意不能对
    # sys.executable 直接 resolve()——venv 的 python 是指向系统解释器的
    # symlink（/root/mve/.venv/bin/python → python3 → /usr/bin/python3.12），
    # resolve() 会跳出 venv（实测踩过：ddg 因此 FileNotFoundError）。
    # 未解析目录优先，再兜底解析后的目录（解释器本身是 symlink 的部署形态）。
    exe = Path(sys.executable)
    for base in (exe.parent, exe.resolve().parent):
        sibling = base / cmd
        if sibling.exists():
            return str(sibling)
    return cmd


def _resolve_arg(arg: str) -> str:
    """参数解析：
      · 绝对路径 / 以 - 开头的 flag（-y/-m/--transport…）→ 原样；
      · 以 .py 结尾或 CONFIG.parent 下确实存在的相对文件（如 mcp_neko_server.py）
        → 解析成相对配置文件目录（mve/）的绝对路径，保证面板进程无论从哪个
        工作目录拉起都能找到；
      · 其余（模块名如 mcp_server_fetch、包名）→ 原样，交给 python -m 解释。
    """
    arg = str(arg or "")
    if _is_abs(arg) or arg.startswith("-"):
        return arg
    if arg.endswith(".py") or (CONFIG.parent / arg).exists():
        return str((CONFIG.parent / arg).resolve())
    return arg

# 路由三态（同构 neko RouteMode；PLUGIN 在 MVE 的语义是「本地注册表优先」）
SELF = "self"        # 本地动作注册表已有该条目（学过的/锻造的优先于远端通用工具）
REMOTE = "remote"    # 本地没有，某个已连接 MCP server 暴露它 → 透传 tools/call
DROP = "drop"        # 查无此工具 → 如实报


# ---------------------------------------------------------------------------
# 配置与工具描述
# ---------------------------------------------------------------------------
@dataclass
class MCPServerConfig:
    """一个外部 MCP server 的配置。

    两种传输：
      · stdio（command + args + env）—— 每个进程各自 spawn 一套，代价是
        transport 绑事件循环、注册表在各自内存里（今天踩的两个坑都在这儿）；
      · HTTP（url）—— 服务由 systemd 常驻（mcp-proxy 把 stdio 转成
        streamable-http），每次 tools/call 就是一次 POST，**没有长连接**。
        这也是用户 2026-10-22 定的方向：「像猫娘一样挂在网关上，不能挂」。
    `fallback_command/args`：HTTP 不通时退回 stdio（服务没起来的部署仍可用）。
    """

    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    url: str = ""
    fallback_command: str = ""
    fallback_args: list[str] = field(default_factory=list)


@dataclass
class MCPTool:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    server_name: str = ""

    @property
    def tool_id(self) -> str:
        return f"mcp_{self.server_name}_{self.name}"


def _parse_tools(result: Any, server_name: str) -> list[MCPTool]:
    """解析 tools/list 的 result（形状不对就返回空，由上层如实报）。"""
    tools: list[MCPTool] = []
    if not isinstance(result, dict):
        return tools
    raw = result.get("tools")
    if not isinstance(raw, list):
        return tools
    for t in raw:
        if not isinstance(t, dict):
            continue
        schema = t.get("inputSchema")
        tools.append(MCPTool(
            name=str(t.get("name") or ""),
            description=str(t.get("description") or ""),
            input_schema=dict(schema) if isinstance(schema, dict) else {},
            server_name=server_name,
        ))
    return [t for t in tools if t.name]


# ---------------------------------------------------------------------------
# MCPClient：一个 stdio MCP server 的连接（同构猫娘 MCPClient）
# ---------------------------------------------------------------------------
class MCPClient:
    """换行分隔 JSON-RPC over stdio 的 MCP 客户端（只实现 stdio）。"""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self.writer: Any = None
        self.connected = False
        self.tools: list[MCPTool] = []
        self.error = ""                     # 连接失败原因（如实报给上层）
        self.loop: Any = None               # **连接时所在的事件循环**（见 connect）
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._read_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._shutdown = False

    async def connect(self, timeout: float = 30.0) -> bool:
        """起子进程 → initialize → initialized → tools/list（照猫娘顺序）。"""
        if not str(self.config.command or "").strip():
            self.error = "stdio 传输必须配 command"
            return False
        try:
            env = os.environ.copy()
            env.update({str(k): str(v) for k, v in (self.config.env or {}).items()})
            self.process = await asyncio.create_subprocess_exec(
                self.config.command, *[str(a) for a in (self.config.args or [])],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self.writer = self.process.stdin
            # stderr 必须持续排空，否则上游 server 写满缓冲区会把自己堵死
            self._read_task = asyncio.create_task(self._read_loop())
            self._stderr_task = asyncio.create_task(self._drain_stderr())

            init = await asyncio.wait_for(self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": dict(CLIENT_INFO),
            }), timeout=timeout)
            if init.get("error"):
                raise RuntimeError(f"initialize 失败：{init['error']}")
            await self._notify("notifications/initialized", {})
            listed = await asyncio.wait_for(
                self._request("tools/list", {}), timeout=timeout)
            if listed.get("error"):
                raise RuntimeError(f"tools/list 失败：{listed['error']}")
            self.tools = _parse_tools(listed.get("result"), self.config.name)
            self.connected = True
            # stdio 的 transport / _read_loop 都绑在**当前**循环上。面板每次
            # 导入都是 asyncio.run() 起的新循环，网关却是进程级单例 —— 不记
            # 这个循环，第二次导入就会把调用桥到已关闭的旧循环上，永远等不到
            # 响应（2026-10-21 Scene H 实测：5 条技能重放全部 >55s 超时，而
            # 第一次导入明明是通的）。ensure_mcp_tools 靠它判断要不要重连。
            self.loop = asyncio.get_running_loop()
            return True
        except asyncio.TimeoutError:
            self.error = f"连接超时（{timeout}s）"
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
        await self.disconnect()
        return False

    async def call_tool(self, tool_name: str, arguments: dict[str, Any], *,
                        timeout: float = 60.0) -> dict[str, Any]:
        """tools/call；任何失败都走 {"error": ...} 返回，不上抛（照猫娘）。"""
        if not self.connected:
            return {"error": f"server '{self.config.name}' 未连接"}
        try:
            res = await asyncio.wait_for(
                self._request("tools/call", {
                    "name": str(tool_name),
                    "arguments": dict(arguments or {}),
                }), timeout=timeout)
        except asyncio.TimeoutError:
            return {"error": f"tools/call 超时（{timeout}s）"}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        if res.get("error"):
            return {"error": res["error"]}
        return {"result": res.get("result")}

    async def disconnect(self) -> None:
        """收尾：停任务 → 关管道 → terminate（5s 不退则 kill）→ 挂起的请求全部报错。"""
        self._shutdown = True
        self.connected = False
        for task in (self._read_task, self._stderr_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        self._read_task = self._stderr_task = None
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass
            self.writer = None
        proc = self.process
        if proc is not None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            except Exception:
                pass
        self.process = None
        self.tools = []
        self.loop = None
        self._fail_pending("连接已关闭")

    # ---- 帧收发（换行分隔 JSON-RPC）----
    async def _request(self, method: str, params: dict[str, Any], *,
                       timeout: float = 60.0) -> dict[str, Any]:
        if self.writer is None:
            raise RuntimeError("Not connected")
        self._request_id += 1
        rid = self._request_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        frame = {"jsonrpc": "2.0", "id": rid, "method": method,
                 "params": params}
        try:
            self.writer.write((json.dumps(frame) + "\n").encode())
            await self.writer.drain()
            return await fut
        finally:
            self._pending.pop(rid, None)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        if self.writer is None:
            return
        frame = {"jsonrpc": "2.0", "method": method, "params": params}
        self.writer.write((json.dumps(frame) + "\n").encode())
        await self.writer.drain()

    async def _read_loop(self) -> None:
        proc = self.process
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue          # server stdout 上的非 JSON 噪声 → 跳过
                if not isinstance(msg, dict):
                    continue
                rid = msg.get("id")
                if rid is None:
                    continue          # 通知：MVE 客户端无需处理
                try:
                    key = int(rid)
                except (TypeError, ValueError):
                    continue
                fut = self._pending.get(key)
                if fut is None or fut.done():
                    continue          # 迟到的响应（等超时的请求）→ 丢弃
                if "error" in msg:
                    fut.set_result({"error": msg["error"]})
                else:
                    fut.set_result({"result": msg.get("result")})
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            self._fail_pending("连接已关闭")

    async def _drain_stderr(self) -> None:
        proc = self.process
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                # 上游 stderr 可能含敏感数据，不写日志不清屏，只排空缓冲区
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def _fail_pending(self, why: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result({"error": why})
        self._pending.clear()


# ---------------------------------------------------------------------------
# MCPHTTPClient：streamable-HTTP（服务常驻，由 mcp-proxy 把 stdio 转出来）
# ---------------------------------------------------------------------------
class MCPHTTPClient:
    """一次调用 = 一次 POST；**不持有长连接**，因此不绑事件循环。

    背景（2026-10-22 用户定方向：「像猫娘一样挂在网关上，不能挂」）：
      · stdio 的 transport/`_read_loop` 绑在**连接时**的循环上，面板每次
        `asyncio.run()` 起新循环就得整批重连（`stale_servers` 那套补丁）；
      · 每个进程各 spawn 一套 server，注册表/开关快照在各自内存里 ——
        长驻面板读到陈旧态，重放技能就 NameError（今天 21:23 实测）。
    HTTP 把这两件事都消掉了：服务是 systemd 常驻，请求彼此独立。

    帧格式照 streamable-http：POST JSON-RPC；响应可能是 application/json
    （直接一个对象）或 text/event-stream（SSE，取 id 匹配的那条 data 帧）。
    session 走 `mcp-session-id` 响应头（initialize 时下发，后续请求带回）。
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.url = str(config.url or "")
        self.session_id = ""
        self.connected = False
        self.tools: list[MCPTool] = []
        self.error = ""
        self.loop: Any = None            # 兼容旧接口（HTTP 用不到）
        self._request_id = 0

    # ---- 底层：一次 POST ----
    def _post(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        import urllib.error
        import urllib.request
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                sid = resp.headers.get("mcp-session-id")
                if sid:
                    self.session_id = sid
                ctype = str(resp.headers.get("content-type") or "")
                raw = resp.read()
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code}：{body or e.reason}") from e
        text = raw.decode("utf-8", errors="replace")
        if "text/event-stream" in ctype:
            out: dict[str, Any] = {}
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                try:
                    obj = json.loads(line[5:].strip())
                except Exception:
                    continue
                if isinstance(obj, dict) and obj.get("id") == payload.get("id"):
                    out = obj
            return out
        try:
            data = json.loads(text)
        except Exception:
            return {}
        # 批量返回（部分实现会包成数组）
        if isinstance(data, list):
            for obj in data:
                if isinstance(obj, dict) and obj.get("id") == payload.get("id"):
                    return obj
            return {}
        return data if isinstance(data, dict) else {}

    def _request(self, method: str, params: dict[str, Any], *,
                 timeout: float = 30.0) -> dict[str, Any]:
        self._request_id += 1
        return self._post({"jsonrpc": "2.0", "id": self._request_id,
                           "method": method, "params": params}, timeout)

    def _notify(self, method: str, params: dict[str, Any],
                timeout: float = 15.0) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": params},
                   timeout)

    # ---- 对外接口（与 MCPClient 一致）----
    async def connect(self, timeout: float = 30.0) -> bool:
        if not self.url:
            self.error = "HTTP 传输必须配 url"
            return False
        try:
            init = await asyncio.to_thread(self._request, "initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": dict(CLIENT_INFO),
            }, timeout=timeout)
            if init.get("error"):
                raise RuntimeError(f"initialize 失败：{init['error']}")
            # 握手完成通知：服务端可能回 202/空体，失败不算致命
            try:
                await asyncio.to_thread(
                    self._notify, "notifications/initialized", {})
            except Exception:
                pass
            listed = await asyncio.to_thread(
                self._request, "tools/list", {}, timeout=timeout)
            if listed.get("error"):
                raise RuntimeError(f"tools/list 失败：{listed['error']}")
            self.tools = _parse_tools(listed.get("result"), self.config.name)
            self.connected = True
            return True
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            self.connected = False
            self.tools = []
            return False

    async def call_tool(self, tool_name: str, arguments: dict[str, Any], *,
                        timeout: float = 60.0) -> dict[str, Any]:
        if not self.connected:
            return {"error": f"server '{self.config.name}' 未连接"}
        try:
            res = await asyncio.to_thread(
                self._request, "tools/call",
                {"name": str(tool_name), "arguments": dict(arguments or {})},
                timeout=timeout)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        if res.get("error"):
            return {"error": res["error"]}
        return {"result": res.get("result")}

    async def disconnect(self) -> None:
        """HTTP 没有长连接要关：清状态即可（服务是常驻的，不该被客户端关掉）。"""
        self.connected = False
        self.tools = []
        self.session_id = ""
        self.loop = None


# ---------------------------------------------------------------------------
# MCPGateway：多 server 管理 + 路由 + 远端工具暴露为场景动作条目
# ---------------------------------------------------------------------------
def _make_remote_entry(gateway: "MCPGateway", tool: MCPTool):
    """给一个远端工具造一个场景动作条目（闭包捕获 gateway + tool_id）。

    注意走 `call_remote` 而不是 `call`：`call` 的 SELF 分支会 invoke 本地
    条目，而这个条目本身就是 tool_id 的本地实现 —— 走 call 会无限递归。
    """
    import actions
    desc = (f"[MCP {tool.server_name}] {tool.name}："
            f"{(tool.description or '').strip()[:120]}")
    schema = tool.input_schema if tool.input_schema else None

    @actions.action(name=tool.tool_id, description=desc, schema=schema)
    async def _entry(mcp=None, **kwargs: Any) -> Any:
        return await gateway.call_remote(tool.tool_id, **kwargs)

    _entry.__doc__ = f"MCP 远端工具 {tool.tool_id}（{tool.server_name}）"
    return _entry


class MCPGateway:
    """多 server 管理 + 工具索引 + 路由 + 暴露/撤回。"""

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._configs: dict[str, MCPServerConfig] = {}
        self._exposed: dict[str, list[str]] = {}   # scene_id -> [tool_id]

    # ---- 配置 ----
    def load_config(self) -> dict[str, Any]:
        """读 mcp_servers.json；缺失/损坏都如实返回原因，不自动创建配置文件。"""
        if not CONFIG.exists():
            return {"servers": {},
                    "reason": f"未配置 {CONFIG.name}（参照本模块 docstring 末尾示例创建）"}
        try:
            data = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception as e:
            return {"servers": {},
                    "reason": f"配置读不出：{type(e).__name__}: {e}"}
        raw = (data or {}).get("servers") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return {"servers": {}, "reason": "配置里没有 servers 对象"}
        servers: dict[str, MCPServerConfig] = {}
        for name, cfg in raw.items():
            if not isinstance(cfg, dict):
                continue
            env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
            raw_args = cfg.get("args") if isinstance(cfg.get("args"), list) else []
            fb_cmd = cfg.get("fallback_command")
            fb_args = cfg.get("fallback_args") if isinstance(
                cfg.get("fallback_args"), list) else []
            servers[str(name)] = MCPServerConfig(
                name=str(name),
                command=_resolve_command(cfg.get("command")),
                args=[_resolve_arg(str(a)) for a in raw_args],
                env={str(k): str(v) for k, v in env.items()},
                enabled=bool(cfg.get("enabled", True)),
                url=str(cfg.get("url") or "").strip(),
                fallback_command=(_resolve_command(fb_cmd) if fb_cmd else ""),
                fallback_args=[_resolve_arg(str(a)) for a in fb_args],
            )
        return {"servers": servers, "reason": ""}

    async def connect_all(self, *, timeout: float = 30.0) -> dict[str, Any]:
        """按配置连接所有启用的 server。失败的不进 _clients，原因进 failed。"""
        cfg = self.load_config()
        self._configs = cfg["servers"]
        out: dict[str, Any] = {"connected": [], "failed": [],
                               "reason": cfg["reason"], "servers": {}}
        for name, c in self._configs.items():
            if not c.enabled:
                continue
            # 配了 url 就走常驻 HTTP 服务（用户 2026-10-22：像猫娘一样挂网关）；
            # 连不上再退回 stdio 自己 spawn —— 服务没起的部署仍可用，且失败原因
            # 会带上「HTTP 那次为什么失败」，不是一句笼统的连不上。
            if c.url:
                client: Any = MCPHTTPClient(c)
                ok = await client.connect(timeout=timeout)
                why_http = "" if ok else client.error
                if not ok and c.fallback_command:
                    client = MCPClient(MCPServerConfig(
                        name=c.name, command=c.fallback_command,
                        args=list(c.fallback_args), env=dict(c.env)))
                    ok = await client.connect(timeout=timeout)
                    if not ok:
                        client.error = (f"HTTP 失败（{why_http}）；退回 stdio 也失败"
                                        f"（{client.error}）")
                elif not ok:
                    client.error = f"HTTP 失败：{why_http}"
            else:
                client = MCPClient(c)
                ok = await client.connect(timeout=timeout)
            if ok:
                self._clients[name] = client
                out["connected"].append({"server": name,
                                         "tools": len(client.tools),
                                         "transport": ("http" if c.url and isinstance(client, MCPHTTPClient) else "stdio")})
            else:
                out["failed"].append({"server": name, "why": client.error})
        out["servers"] = self.status()
        return out

    async def disconnect_all(self) -> None:
        for c in self._clients.values():
            await c.disconnect()
        self._clients.clear()

    # ---- 工具索引（同构 router.rebuild_tool_index）----
    def tool_index(self) -> dict[str, str]:
        idx: dict[str, str] = {}
        for name, c in self._clients.items():
            for t in c.tools:
                tid = t.tool_id
                if tid in idx:
                    continue          # 重名跳过并让上层看见缺口，不静默覆盖
                idx[tid] = name
        return idx

    def list_tools(self) -> list[dict[str, Any]]:
        return [{"tool_id": t.tool_id, "server": t.server_name,
                 "name": t.name, "description": t.description,
                 "input_schema": t.input_schema}
                for c in self._clients.values() for t in c.tools]

    def connected_servers(self, loop: Any = None) -> list[str]:
        """已连上的 server 名（重复 `connect_all` 前先问一句，别白起子进程）。

        `loop` 给了就只算**绑在这个循环上**的连接：stdio 的 transport 和
        _read_loop 都属于连接时的那个循环，换循环后连接虽然在、却已经是
        死的（跨循环等不到响应）。面板每次导入都是新循环 → 必须按循环判。
        """
        out = []
        for n, c in self._clients.items():
            if not getattr(c, "connected", False):
                continue
            if loop is not None and getattr(c, "loop", None) is not loop:
                continue
            out.append(n)
        return out

    def stale_servers(self, loop: Any) -> list[str]:
        """连接还在、但绑在**别的循环**上的 server（要断开重连的一批）。"""
        return [n for n, c in self._clients.items()
                if getattr(c, "connected", False)
                and getattr(c, "loop", None) is not loop]

    def status(self) -> dict[str, Any]:
        servers = []
        for name, c in self._configs.items():
            client = self._clients.get(name)
            servers.append({
                "name": name,
                "connected": bool(client and client.connected),
                "tools": len(client.tools) if client else 0,
                "error": (client.error if client else "未连接"),
                "command": f"{c.command} {' '.join(c.args)}".strip(),
            })
        return {"servers": servers, "tool_count": len(self.tool_index()),
                "exposed_scenes": {k: len(v) for k, v in self._exposed.items()}}

    # ---- 路由（同构 decide：SELF / REMOTE / DROP）----
    def decide(self, tool_id: str, scene_id: str = "") -> dict[str, Any]:
        tid = str(tool_id or "")
        if scene_id:
            import actions
            if tid in actions.names(str(scene_id)):
                return {"mode": SELF, "tool_id": tid, "scene_id": str(scene_id),
                        "reason": f"场景 {scene_id} 注册表已有条目 {tid}（本地优先）"}
        idx = self.tool_index()
        if tid in idx:
            return {"mode": REMOTE, "tool_id": tid, "server": idx[tid],
                    "reason": f"远端 server '{idx[tid]}' 暴露 {tid}"}
        return {"mode": DROP, "tool_id": tid,
                "reason": f"查无此工具 {tid}（本地注册表与已连接 MCP server 都没有）"}

    async def call(self, tool_id: str, scene_id: str = "",
                   **arguments: Any) -> Any:
        """按 decide 路由调用。SELF → 本地条目；REMOTE → 远端 tools/call；
        DROP → 返回 {"error": reason}（不上抛）。"""
        d = self.decide(tool_id, scene_id)
        if d["mode"] == SELF:
            import actions
            return await actions.invoke(str(scene_id), tool_id, **arguments)
        if d["mode"] == REMOTE:
            return await self.call_remote(tool_id, **arguments)
        return {"error": d["reason"]}

    async def call_remote(self, tool_id: str, **arguments: Any) -> Any:
        """直连远端（不走 SELF 分支 —— 暴露条目自己就靠它，防递归）。"""
        idx = self.tool_index()
        server = idx.get(str(tool_id))
        if server is None:
            return {"error": f"已连接的 MCP server 里没有工具 {tool_id}"}
        client = self._clients.get(server)
        if client is None:
            return {"error": f"server '{server}' 连接已断开"}
        prefix = f"mcp_{server}_"
        tool_name = str(tool_id)[len(prefix):]
        return await client.call_tool(tool_name, arguments)

    # ---- 暴露 / 撤回（同构「远端工具注入聊天」）----
    def expose_as_actions(self, scene_id: str) -> dict[str, Any]:
        """把已连接 server 的工具注册成场景动作条目（origin="mcp"）。

        注册后 task_decompose.reachability 就能在注册表里判到这些远端工具
        （词面匹配条目名/描述）—— 这就是「用猫娘计划的 mcp 编排插件使用」的
        接入点：远端工具与 forged / learned 条目并列，编排器一视同仁。
        """
        import actions
        scene_id = str(scene_id)
        exposed: list[str] = []
        skipped: list[str] = []
        for t in _dedup_tools(self.list_tools()):
            if actions.register(scene_id, _make_remote_entry(self, t),
                                origin="mcp"):
                exposed.append(t.tool_id)
            else:
                skipped.append(t.tool_id)
        self._exposed[scene_id] = exposed
        return {"scene_id": scene_id, "exposed": exposed, "skipped": skipped,
                "reason": "" if exposed else "没有可暴露的远端工具（未连接/无工具）"}

    def withdraw_from_scene(self, scene_id: str) -> list[str]:
        """把本网关暴露过的条目从场景撤下（远端断了，抓手段不该还在）。"""
        import actions
        removed = []
        for tid in self._exposed.pop(str(scene_id), []):
            if actions.unregister(str(scene_id), tid):
                removed.append(tid)
        return removed


def _dedup_tools(tools: list[dict[str, Any]]) -> list[MCPTool]:
    """tool_id 去重（同 router：重名跳过，不静默覆盖）。"""
    seen: set[str] = set()
    out: list[MCPTool] = []
    for t in tools:
        if t["tool_id"] in seen:
            continue
        seen.add(t["tool_id"])
        out.append(MCPTool(name=t["name"], description=t["description"],
                           input_schema=t["input_schema"],
                           server_name=t["server"]))
    return out


# ---------------------------------------------------------------------------
# 模块级单例（面板/学习循环共用同一个网关实例）
# ---------------------------------------------------------------------------
_GATEWAY: MCPGateway | None = None


def get_gateway() -> MCPGateway:
    global _GATEWAY
    if _GATEWAY is None:
        _GATEWAY = MCPGateway()
    return _GATEWAY


# ---------------------------------------------------------------------------
# 配置示例（复制成 mve/mcp_servers.json 后按需改；该文件已 gitignore）
# ---------------------------------------------------------------------------
# {
#   "servers": {
#     "vlml": {                          # 例：把 VLML 自己的 MCP server 当外部进程起
#       "command": "/root/mve/.venv/bin/python",
#       "args": ["vlml/server.py"],
#       "env": {},
#       "enabled": true
#     },
#     "filesystem": {                    # 例：任何第三方 stdio MCP server
#       "command": "npx",
#       "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
#       "env": {}
#     }
#   }
# }
