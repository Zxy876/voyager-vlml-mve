#!/usr/bin/env python3
"""验证 MCP 传输层网关（mcp_gateway 同构猫娘 mcp_adapter 的可复现剧本）。

真子进程、真 stdio、真 JSON-RPC 往返（不 mock 传输层）：
  1. 玩具 MCP server（add / echo 两个工具）按配置文件起子进程
  2. connect_all：initialize → initialized → tools/list 全走真帧
  3. tool_id 命名 mcp_{server}_{tool}；重名/查无 → DROP 如实报
  4. call_remote：tools/call 真调用，add(2,3)=5 / echo 透传
  5. expose_as_actions：远端工具变成场景动作条目（origin=mcp，
     inputSchema 原样透传）→ decide 变 SELF → gateway.call 不递归
  6. withdraw + disconnect_all：条目撤下、连接关闭、失败如实报

运行：python mve/test_mcp_gateway.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import actions          # noqa: E402
import mcp_gateway      # noqa: E402

# 玩具 MCP server：换行分隔 JSON-RPC over stdio（与猫娘 _send_request 同帧）
TOY_SERVER = r'''
import json, sys

def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

TOOLS = [
    {"name": "add", "description": "两数相加",
     "inputSchema": {"type": "object",
                     "properties": {"a": {"type": "integer"},
                                    "b": {"type": "integer"}},
                     "required": ["a", "b"]}},
    {"name": "echo", "description": "回显输入文本",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}}}},
]

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        _send({"jsonrpc": "2.0", "id": mid,
               "result": {"protocolVersion": "2024-11-05",
                          "capabilities": {},
                          "serverInfo": {"name": "toy", "version": "0"}}})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name, args = params.get("name"), (params.get("arguments") or {})
        if name == "add":
            val = int(args.get("a", 0)) + int(args.get("b", 0))
            _send({"jsonrpc": "2.0", "id": mid,
                   "result": {"content": [{"type": "text", "text": str(val)}],
                              "isError": False}})
        elif name == "echo":
            _send({"jsonrpc": "2.0", "id": mid,
                   "result": {"content": [
                       {"type": "text", "text": str(args.get("text", ""))}],
                       "isError": False}})
        else:
            _send({"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32601,
                             "message": f"unknown tool {name}"}})
    elif mid is not None:
        _send({"jsonrpc": "2.0", "id": mid,
               "error": {"code": -32601,
                         "message": f"method not found: {method}"}})
'''

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" —— {detail}" if detail else ""))


def _text(res: dict) -> str:
    """从 MCP tools/call 的 result 里取文本（content[0].text）。"""
    try:
        return str(res["result"]["content"][0]["text"])
    except Exception:
        return ""


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mve_gw_"))
    actions.SNAPSHOT = tmp / "action_registry.json"
    toy = tmp / "toy_server.py"
    toy.write_text(TOY_SERVER, encoding="utf-8")
    mcp_gateway.CONFIG = tmp / "mcp_servers.json"
    mcp_gateway.CONFIG.write_text(json.dumps({"servers": {
        "toy": {"command": sys.executable, "args": [str(toy)], "env": {}},
        "broken": {"command": "definitely_not_a_command_xyz", "args": []},
    }}), encoding="utf-8")

    print("== 1. connect_all：真 stdio 往返 ==")
    gw = mcp_gateway.MCPGateway()
    conn = await gw.connect_all(timeout=15.0)
    check("toy 连接成功且列出 2 个工具",
          any(c["server"] == "toy" and c["tools"] == 2 for c in conn["connected"]),
          json.dumps(conn["connected"], ensure_ascii=False))
    check("broken server 如实进 failed（不静默跳过）",
          any(f["server"] == "broken" for f in conn["failed"]),
          json.dumps(conn["failed"], ensure_ascii=False)[:80])

    print("\n== 2. 工具索引与路由三态 ==")
    idx = gw.tool_index()
    check("tool_id = mcp_{server}_{tool}",
          set(idx) == {"mcp_toy_add", "mcp_toy_echo"}, str(sorted(idx)))
    d1 = gw.decide("mcp_toy_add")
    check("未暴露前 decide → REMOTE", d1["mode"] == "remote" and d1["server"] == "toy")
    d2 = gw.decide("mcp_toy_nope")
    check("查无此工具 → DROP 且说清 reason", d2["mode"] == "drop"
          and "查无此工具" in d2["reason"])

    print("\n== 3. call_remote：真 tools/call ==")
    r1 = await gw.call_remote("mcp_toy_add", a=2, b=3)
    check("add(2,3)=5", _text(r1) == "5", _text(r1))
    r2 = await gw.call_remote("mcp_toy_echo", text="你好 MCP")
    check("echo 透传", _text(r2) == "你好 MCP")
    r3 = await gw.call_remote("mcp_toy_missing")
    check("远端没有的工具 → error 字典（不上抛）", "error" in r3, str(r3)[:60])

    print("\n== 4. expose_as_actions：远端工具变抓手段 ==")
    exp = gw.expose_as_actions("T")
    check("两个工具都注册进场景 T",
          set(exp["exposed"]) == {"mcp_toy_add", "mcp_toy_echo"},
          json.dumps(exp["exposed"], ensure_ascii=False))
    entries = {e["name"]: e for e in actions.entries("T")}
    check("条目 origin=mcp",
          entries["mcp_toy_add"]["origin"] == "mcp")
    check("远端 inputSchema 原样透传（不反射本地签名）",
          entries["mcp_toy_add"]["input_schema"].get("properties", {}).keys()
          == {"a", "b"})
    d3 = gw.decide("mcp_toy_add", "T")
    check("暴露后 decide → SELF（本地优先）", d3["mode"] == "self")
    r4 = await gw.call("mcp_toy_add", "T", a=10, b=5)
    check("gateway.call 走本地条目且不递归", _text(r4) == "15")
    r5 = await actions.invoke("T", "mcp_toy_add", a=1, b=1)
    check("actions.invoke 直接调也通", _text(r5) == "2")

    print("\n== 5. withdraw + disconnect ==")
    removed = gw.withdraw_from_scene("T")
    check("撤回后条目从注册表消失",
          set(removed) == {"mcp_toy_add", "mcp_toy_echo"}
          and actions.names("T") == [], str(actions.names("T")))
    await gw.disconnect_all()
    st = gw.status()
    toy_state = next((s for s in st["servers"] if s["name"] == "toy"), {})
    check("disconnect_all 后 connected=False", toy_state.get("connected") is False)
    r6 = await gw.call_remote("mcp_toy_add", a=1, b=1)
    check("断开后调用如实报未连接", "error" in r6, str(r6)[:60])

    print("\n== 6. 配置缺失 ==")
    mcp_gateway.CONFIG = tmp / "nope.json"
    gw2 = mcp_gateway.MCPGateway()
    conn2 = await gw2.connect_all()
    check("无配置 → 0 server + 如实 reason",
          conn2["connected"] == [] and "未配置" in conn2["reason"])

    print(f"\n===== 结果：{len(PASS)} 过 / {len(FAIL)} 挂 =====")
    if FAIL:
        for f in FAIL:
            print(f"  ❌ {f}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
