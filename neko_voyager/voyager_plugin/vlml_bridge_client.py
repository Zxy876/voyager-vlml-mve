#!/usr/bin/env python3
"""VLML 桥的客户端。**只用 urllib（socket），绝不 fork。**

猫娘宿主 projectneko_server fork 不安全（实测 7 份崩溃报告），插件进程里
任何 subprocess 都会随机把进程杀掉。所以取数必须走外部常驻服务
（vlml_bridge/vlml_http_bridge.py，127.0.0.1:39481）。
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

BRIDGE = os.environ.get("VLML_BRIDGE", "http://127.0.0.1:39481").rstrip("/")
_TIMEOUT = float(os.environ.get("VLML_BRIDGE_TIMEOUT", "20"))


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        BRIDGE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def health() -> dict[str, Any]:
    """桥活着吗？返回 {"ok": true, "tools": 10} 或 {"ok": false, "error": ...}"""
    try:
        with urllib.request.urlopen(BRIDGE + "/health", timeout=min(_TIMEOUT, 8)) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def query(sql: str) -> dict[str, Any]:
    """跑一条只读 SQL。失败返回 {"ok": False, "error": ...}，不抛异常。"""
    try:
        return _post("/query", {"sql": sql})
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def call_tool(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """调桥上的任意 VLML0 MCP 工具。"""
    try:
        return _post("/tool", {"name": name, "args": args or {}})
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def observe_world() -> dict[str, Any]:
    """骨架版的「观察世界」：拿库的结构与规模 —— 对应原版 Voyager 的 voxels。"""
    info = call_tool("get_database_info", {})
    if not info.get("ok"):
        return info
    result = info.get("result")
    stats = result.get("statistics") if isinstance(result, dict) else None
    return {
        "ok": True,
        "database": (result or {}).get("database_path") if isinstance(result, dict) else None,
        "statistics": stats or {},
    }
