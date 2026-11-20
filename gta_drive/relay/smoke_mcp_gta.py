#!/usr/bin/env python3
"""smoke_mcp_gta.py — GTA MCP 通道端到端冒烟（VPS 假插件 → 中继 → 隧道 → 本 server）。

验证目标：MCP 工具调用真的穿过 隧道→VPS 中继→GAME 侧，obs 真的回来，
verdict 由「环境」（假插件模拟的到达）给出 —— 这就是 Phase2 代码执行时的通道。
假插件是**传输验证**用的，不是自动驾驶 mock（第一条自动化必须真游戏）。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "mcp_gta_server.py"
SCENE_DIR = HERE.parents[1] / "scene_data" / "G"

env = dict(os.environ,
           GTA_BRIDGE_HOST="127.0.0.1", GTA_BRIDGE_PORT="9778",
           GTA_BRIDGE_HANDSHAKE="MVE", GTA_SCENE_DIR=str(SCENE_DIR))
proc = subprocess.Popen([sys.executable, str(SERVER)], env=env,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        text=True, bufsize=1)

_rid = 0


def rpc(method: str, params: dict | None = None, timeout: float = 90.0) -> dict:
    global _rid
    _rid += 1
    rid = _rid
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                 "method": method,
                                 "params": params or {}}) + "\n")
    proc.stdin.flush()
    t0 = time.time()
    while time.time() - t0 < timeout:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("server 退出")
        msg = json.loads(line)
        if msg.get("id") == rid:
            return msg
    raise TimeoutError(f"{method} {timeout}s 无响应")


def call(name: str, arguments: dict | None = None, timeout: float = 90.0) -> dict:
    out = rpc("tools/call", {"name": name, "arguments": arguments or {}},
              timeout=timeout)
    res = out.get("result") or {}
    sc = res.get("structuredContent") or {}
    if res.get("isError"):
        raise RuntimeError(f"{name} 出错: {sc.get('error') or res.get('content')}")
    return sc


# ① initialize + tools/list
init = rpc("initialize", {"protocolVersion": "2024-11-05",
                          "capabilities": {}, "clientInfo": {"name": "smoke"}})
assert init.get("result", {}).get("serverInfo", {}).get("name") == "mve-gta"
tools = rpc("tools/list")["result"]["tools"]
names = [t["name"] for t in tools]
assert set(names) >= {"gta_status", "gta_get_obs", "gta_drive_to",
                      "gta_cruise", "gta_stop", "gta_handback",
                      "gta_wait_arrive"}, names
print(f"① tools/list OK：{len(tools)} 个工具 {names}")

# ② 状态（桥已连 = 中继配对成功）
st = call("gta_status", {}, timeout=30)
assert st.get("connected"), st
print(f"② gta_status OK：桥已连（配对成功），obs_age={st.get('obs_age_sec')}s")

# ③ obs（数据面真回来了）
time.sleep(1.5)
obs = call("gta_get_obs")
assert obs.get("waypoint") and obs.get("pos"), obs
print(f"③ gta_get_obs OK：pos={obs['pos']} 距路点 {obs['waypoint_dist_m']}m")

# ④ 动作下发（穿过整条链到 GAME 侧）
d = call("gta_drive_to", {"x": 100, "y": 0, "speed": 8})
assert d.get("ok"), d
print("④ gta_drive_to OK：动作已下发")

# ⑤ 环境判据（verdict 由 GAME 侧到达事实给出，不经 LLM）
v = call("gta_wait_arrive", {"radius": 5, "timeout": 60}, timeout=70)
assert v.get("verdict") == "success", v
print(f"⑤ gta_wait_arrive OK：verdict={v['verdict']}（{v['reason']}）"
      f" min_dist={v['min_dist_m']}m ticks={v['ticks']}")

# ⑥ 数据面落盘
trace = SCENE_DIR / "gta_trace.jsonl"
obsf = SCENE_DIR / "gta_obs.jsonl"
assert trace.exists() and obsf.exists(), "数据面文件没落盘"
print(f"⑥ 数据面 OK：{trace.name} {trace.stat().st_size}B / "
      f"{obsf.name} {obsf.stat().st_size}B（list_files/read_file 可读）")

print("MCP_GTA_SMOKE_OK")
