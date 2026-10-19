#!/usr/bin/env python3
"""stdio MCP server —— 把「猫娘计划的联网能力」经 MCP 暴露（手写 JSON-RPC 传输层）。

为什么手写、不引 mcp SDK
-----------------------
服务器 venv（`/root/mve/.venv`）**没有**装 mcp SDK，而「猫娘式联网能力」只依赖
标准库（见 net_tools.py：ipaddress / json / re / socket / urllib），没必要为它
引一个第三方依赖。所以严格照 `mve/mcp_gateway.py` 里 `MCPClient` 期望的协议手搓
一份 stdio server：

    · 帧 = 换行分隔 JSON（`json.dumps(frame) + "\n"`，与客户端 `_request` 的写帧
      一模一样）；
    · protocolVersion = "2024-11-05"（与客户端 `PROTOCOL_VERSION` 完全一致）；
    · 握手顺序：initialize → notifications/initialized（无响应）→ tools/list
      → tools/call；
    · tools/call 的结果用 `structuredContent` 带**原始 Python 对象**，客户端
      `call_remote` 直接取它，同时 `content[0].text` 给一份 JSON 文本兜底。

暴露的工具（取自 `net_tools`，即从猫娘搬来的联网能力）
----------------------------------------------------
    · web_search(query, max_results=5)  → [{"title","url","snippet"}]
    · web_fetch(url, max_bytes=200000)  → {"url","status","text"}

诚实边界（与 net_tools 一致，照搬不打折）
----------------------------------------
只读（只有 GET）、限长（200KB）、禁私网/回环（SSRF 防护）；取不到就返回
`isError=True` + 错误信息，**绝不编造**一个像样的答案。

⚠️ 接 N.E.K.O 全量工具的前提（与用户对齐过，诚实交代）
-------------------------------------------------------
N.E.K.O 自身**不暴露** MCP server —— 它的 `plugin/plugins/mcp_adapter` 是 **MCP
客户端**（用来连外部 MCP server），且服务器上 `neko-main` 容器没有跑起来。所以
这里用「把猫娘式联网能力经手搓 MCP server 暴露」证明**整条 MCP 链路（网关连
接 → expose_as_actions 注册 → 模型在写代码时直接调用 → 取到真数）可用**；真要
N.E.K.O 全量工具，得先让 N.E.K.O 以 MCP server 形态运行并可达，再改本配置文件的
`command`/`args` 指向它即可（协议完全一致，无需改客户端）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# 工具清单：description/schema 直接取自 net_tools（不手抄，避免与实现漂移）。
TOOLS: list[dict] = [
    {
        "name": "web_search",
        "description": ("联网搜索（DuckDuckGo，无需 key），返回 "
                        "[{'title','url','snippet'}]。从猫娘搬来的联网能力，经 MCP 暴露。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": ("抓一个公网页面的正文文本（只读、限长、禁私网地址）。"
                        "从猫娘搬来的联网能力，经 MCP 暴露。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_bytes": {"type": "integer"},
            },
            "required": ["url"],
        },
    },
]

_PROTOCOL_VERSION = "2024-11-05"


def _write(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _dispatch(name: str, arguments: dict) -> object:
    """调用真实实现；任何失败都包成 {'__error__': ...}（不走异常，便于映射 isError）。"""
    try:
        import net_tools
    except Exception as e:  # 极端：net_tools 不可用 → 如实报
        return {"__error__": f"net_tools 不可用：{type(e).__name__}: {e}"}
    try:
        if name == "web_search":
            return net_tools.web_search(
                str(arguments.get("query", "")),
                int(arguments.get("max_results", 5)))
        if name == "web_fetch":
            return net_tools.web_fetch(
                str(arguments.get("url", "")),
                int(arguments.get("max_bytes", 200_000)))
    except Exception as e:
        # 诚实：取不到就报，客户端转 isError，模型侧判「没取到真数」。
        return {"__error__": f"{type(e).__name__}: {e}"}
    return {"__error__": f"未知工具 {name}"}


def _format_result(py_obj: object) -> dict:
    """tools/call 的 result：structuredContent 带原始对象，content[0].text 兜底。

    客户端 `call_remote` 把整段 result 透传给调用方；`_unwrap_mcp`（local_agent）
    优先取 structuredContent，没有再解析 text。
    """
    if isinstance(py_obj, dict) and "__error__" in py_obj:
        err = str(py_obj["__error__"])
        return {
            "content": [{"type": "text", "text": err}],
            "isError": True,
            "structuredContent": {"error": err},
        }
    text = json.dumps(py_obj, ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": False,
        "structuredContent": py_obj,
    }


def _serve() -> None:
    # 标准输入按行读（客户端写帧用的是 ... + "\n"）；stdin 关闭即退出。
    for raw in sys.stdin.buffer:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        rid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            _write({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "neko-mcp-standin", "version": "1.0"},
            }})
        elif method == "notifications/initialized":
            # 通知：无 id、无需响应（客户端 _read_loop 对无 id 帧直接跳过）。
            continue
        elif method == "tools/list":
            _write({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            _write({"jsonrpc": "2.0", "id": rid,
                    "result": _format_result(_dispatch(name, arguments))})
        else:
            # 礼貌回个错误，避免客户端读帧卡死（未知方法）。
            if rid is not None:
                _write({"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32601,
                                  "message": f"unknown method: {method}"}})


if __name__ == "__main__":
    _serve()
