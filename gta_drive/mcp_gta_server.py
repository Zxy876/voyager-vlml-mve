#!/usr/bin/env python3
"""mcp_gta_server.py — GTA 环境的 MCP 通道（同构 mcp_sherlock_server.py）。

定位（用户 2026-11-21 口径）
----------
MVE 一行不改。GTA 的「环境和数据带到 MVE」= 一条 mcp_servers.json 配置 +
本 server。工具经 mcp_gateway expose 成 origin=mcp 条目后：
  - 注入场景执行域：local_agent.py:1929-1951（Phase2/学生代码直接调 gta_*）
  - 写进 prompt 抓手清单：local_agent.py:1501-1543（模型知道自己能开车）
原版 Voyager 解包同构：mineflayer bot 的动作原语（bot.chat / 挖矿 / 移动）
= 这里的 gta_drive_to / cruise / stop / handback；env.step 的 obs 事件
= gta_get_obs 的车辆状态。Phase2 代码「在 GTA 环境沙箱里执行过」的验收
= gta_wait_arrive —— 环境规则判定（到达路点 radius 内 = success），
  判据不经 LLM，与 mve_gta_env.py 同一口径。

数据面（GTA 的数据带到 MVE）
----------
obs（采样）/ 每次动作 / verdict 全部 append 到 <GTA_SCENE_DIR>/gta_obs.jsonl
与 gta_trace.jsonl —— 场景目录就是本场景的数据源（scene_profile kind=local），
MVE 的 list_files / read_file 直接可读，出题/判据面从此有 GTA 的真实数据。

传输：本 server 不直连游戏，走 gta_client（隧道 → VPS 中继 → 游戏插件）：
  Mac: ssh -N -L 127.0.0.1:9778 → VPS 9778（mve-bridge-relay）
  握手：GTA_BRIDGE_HANDSHAKE=MVE（中继按角色配对；直连游戏插件留空）
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

# ── 复用 gta_drive/adapter/gta_client.py（与 run_first_automation 同一套）──
ADAPTER = Path(__file__).resolve().parent / "adapter"
sys.path.insert(0, str(ADAPTER))
from gta_client import GtaClient, dist2d  # noqa: E402

_PROTOCOL_VERSION = "2024-11-05"

BRIDGE_HOST = os.environ.get("GTA_BRIDGE_HOST", "127.0.0.1")
BRIDGE_PORT = int(os.environ.get("GTA_BRIDGE_PORT", "9778"))
BRIDGE_HANDSHAKE = os.environ.get("GTA_BRIDGE_HANDSHAKE", "MVE")
SCENE_DIR = Path(os.environ.get(
    "GTA_SCENE_DIR",
    str(Path(__file__).resolve().parents[1] / "scene_data" / "G")))

OBS_SAMPLE_SEC = 2.0      # obs 落盘采样间隔（游戏侧 10Hz，全落太大）
ARRIVE_M_DEFAULT = 5.0    # 与 mve_gta_env.ARRIVE_M 同口径
WAIT_MAX_SEC = 600.0      # wait_arrive 上限（防学生代码把会话挂死）
OBS_STALE_SEC = 10.0      # wait_arrive 等待期新 obs 断流判定（10Hz 常流下 10s 没帧=停流）

# ---------------------------------------------------------------------------
# 桥接层：懒连接 + obs 泵线程 + 发送锁（工具调用在工作线程，必须串行化）
# ---------------------------------------------------------------------------


class Bridge:
    def __init__(self) -> None:
        self._lock = threading.Lock()          # 保护 client 与 latest_obs
        self._client: GtaClient | None = None
        self.latest_obs: dict = {}
        self.last_obs_ts = 0.0
        self.connected = False
        self.last_error = ""
        self._pump: threading.Thread | None = None
        self._last_dump = 0.0

    # -- 连接管理 --
    def ensure_connected(self) -> None:
        with self._lock:
            if self.connected and self._client is not None:
                return
            try:
                self._client = GtaClient(BRIDGE_HOST, BRIDGE_PORT,
                                         timeout=8.0,
                                         handshake=BRIDGE_HANDSHAKE)
                self.connected = True
                self.last_error = ""
                self._start_pump()
            except Exception as e:
                self.connected = False
                self.last_error = f"{type(e).__name__}: {e}"
                raise RuntimeError(
                    f"游戏桥连不上 {BRIDGE_HOST}:{BRIDGE_PORT}（{self.last_error}）"
                    "—— 检查：Windows 机隧道挂着？VPS mve-bridge-relay 活着？"
                    "游戏里按 F9 了吗？") from e

    def _start_pump(self) -> None:
        client = self._client

        def _pump() -> None:
            while True:
                obs = client.recv_obs()
                if obs is None:
                    with self._lock:
                        if self._client is client:
                            self.connected = False
                            self.last_error = "游戏桥连接断开"
                    return
                if not isinstance(obs, dict):
                    continue
                with self._lock:
                    self.latest_obs = obs
                    self.last_obs_ts = time.time()
                self._maybe_dump_obs(obs)

        self._pump = threading.Thread(target=_pump, daemon=True)
        self._pump.start()

    # -- 数据面落盘 --
    def _maybe_dump_obs(self, obs: dict) -> None:
        now = time.time()
        if now - self._last_dump < OBS_SAMPLE_SEC:
            return
        self._last_dump = now
        row = {"ts": now, **{k: obs.get(k)
                             for k in ("speed", "pos", "heading", "waypoint")}}
        if obs.get("events"):
            row["events"] = obs["events"]     # 真实游戏事件进数据面
        self._dump("gta_obs.jsonl", row)

    def _dump(self, fname: str, row: dict) -> None:
        try:
            SCENE_DIR.mkdir(parents=True, exist_ok=True)
            with open(SCENE_DIR / fname, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass                                  # 账写不进去不挡驾驶，如实可查

    # -- 对外操作 --
    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.latest_obs)

    def send(self, action: dict) -> dict:
        """发动作（串行化），返回 {ok, obs 快照} 或 {error}。"""
        self.ensure_connected()
        with self._lock:
            client = self._client
        try:
            client.send_action(action)
        except Exception as e:
            self.connected = False
            self.last_error = f"{type(e).__name__}: {e}"
            return {"error": f"动作下发失败：{self.last_error}"}
        row = {"ts": time.time(), "action": action.get("t")}
        row.update({k: v for k, v in action.items() if k not in ("t",)})
        self._dump("gta_trace.jsonl", row)
        return {"ok": True, "sent": action.get("t"), "obs": self.snapshot()}


BRIDGE = Bridge()

# ---------------------------------------------------------------------------
# 工具实现（判据面：环境规则，不经 LLM —— 同 mve_gta_env.run_episode 口径）
# ---------------------------------------------------------------------------

_CANCEL = threading.Event()


def gta_status() -> dict:
    try:                                      # 排查工具必须真连一次才说得清
        BRIDGE.ensure_connected()
    except RuntimeError as e:
        BRIDGE.last_error = str(e)
    obs = BRIDGE.snapshot()
    return {
        "connected": BRIDGE.connected,
        "bridge": f"{BRIDGE_HOST}:{BRIDGE_PORT}（handshake={BRIDGE_HANDSHAKE!r}）",
        "last_error": BRIDGE.last_error,
        "obs_age_sec": (round(time.time() - BRIDGE.last_obs_ts, 1)
                        if BRIDGE.last_obs_ts else None),
        "obs": obs or None,
        "hint": ("connected=false 时的排查顺序：Windows 机隧道挂着（ssh -N -L）"
                 "→ VPS mve-bridge-relay active → 游戏内按 F9 激活 MVEBridge"),
    }


def gta_get_obs() -> dict:
    """取最新车辆状态（Voyager 同构的 observation 面）。"""
    BRIDGE.ensure_connected()
    obs = BRIDGE.snapshot()
    if not obs:
        return {"error": "还没有收到 obs（桥刚连上？等 1-2 秒再取）",
                "connected": BRIDGE.connected}
    pos = obs.get("pos") or [0, 0]
    wp = obs.get("waypoint")
    out = {
        "speed_ms": obs.get("speed"),
        "speed": obs.get("speed"),        # 兼容别名（老草稿按 raw obs 键名取值）
        "speed_kmh": round((obs.get("speed") or 0) * 3.6, 1),
        "pos": pos,
        "heading": obs.get("heading"),
        "waypoint": wp,
        "waypoint_dist_m": round(dist2d(pos, wp), 1) if wp else None,
        "obs_age_sec": (round(time.time() - BRIDGE.last_obs_ts, 1)
                        if BRIDGE.last_obs_ts else None),
    }
    if obs.get("events"):
        out["events"] = obs["events"]     # 自上一条 obs 以来真实发生的游戏事件
    for k in ("health_engine", "nearby_vehicles", "running", "on_foot",
              "in_vehicle_model", "mission_active", "mission_target",
              "mission_dist_m"):
        if k in obs:
            out[k] = obs[k]
    return out


def gta_drive_to(x: float, y: float, z: float = 0.0, speed: float = 20.0,
                 style: int = 1) -> dict:
    """开到坐标（MVE 主动作，GTA 原生驾驶 AI 执行）。"""
    return BRIDGE.send({"t": "drive_to", "x": float(x), "y": float(y),
                        "z": float(z), "speed": float(speed), "style": int(style)})


def gta_cruise(speed: float = 15.0) -> dict:
    return BRIDGE.send({"t": "cruise", "speed": float(speed)})


def gta_stop() -> dict:
    return BRIDGE.send({"t": "stop"})


def gta_handback() -> dict:
    return BRIDGE.send({"t": "handback"})


def gta_wait_arrive(radius: float = ARRIVE_M_DEFAULT,
                    timeout: float = 180.0, target: str = "waypoint") -> dict:
    """阻塞等环境判定：到达路点/任务目标 radius 米内 = success（判据不经 LLM）。

    Phase2 代码「真的在 GTA 环境里跑过」的验收原语：返回 verdict / min_dist /
    ticks / elapsed，与 mve_gta_env.run_episode 的返回同口径。
    target="waypoint"（默认）= 玩家手动路点；target="mission" = 任务目标 blip
    （剧情任务的开车部分用这个，无路点也可判）。
    """
    BRIDGE.ensure_connected()
    timeout = min(max(float(timeout), 5.0), WAIT_MAX_SEC)
    radius = max(float(radius), 1.0)
    t0, ticks, min_d = time.time(), 0, float("inf")
    last_ts = 0.0
    last_fresh_wall: float | None = None  # 最近一次收到新 obs 的墙钟时刻
    verdict, reason = None, None
    seen_events: list = []                # 等待期间真实发生的游戏事件（撞/追/翻…）
    while verdict is None:
        if _CANCEL.is_set():
            verdict, reason = "cancelled", "上游取消了本次等待"
            break
        if time.time() - t0 > timeout:
            verdict = "failed"
            reason = f"超时（{timeout:.0f}s 未到达目标）"
            break
        obs = BRIDGE.snapshot()
        if obs.get("ts") == last_ts:
            # 观测停流检测：新 obs 迟迟不来 = 游戏端没在推观测（插件 OnTick
            # 停转 / 转发器或隧道半开卡死）。如实判停流，不烧满 timeout、
            # 不拿冻结的首帧距离冒充「未到达」。
            if last_fresh_wall is not None and \
                    time.time() - last_fresh_wall > OBS_STALE_SEC:
                frozen = (f"{min_d:.1f}m" if min_d < float("inf") else "从未采样")
                verdict = "failed"
                reason = (f"观测停流（{time.time() - last_fresh_wall:.0f}s 没有新 obs，"
                          f"距离采样冻结在 {frozen}）——游戏端没在推观测，"
                          "到达与否未知；查 Windows 转发器/隧道，恢复后重试")
                break
            time.sleep(0.05)
            continue
        last_ts = obs.get("ts")
        last_fresh_wall = time.time()
        for ev in (obs.get("events") or []):
            if len(seen_events) < 50:
                seen_events.append(ev)
        wp, pos = obs.get("waypoint"), obs.get("pos")
        if target == "mission":
            wp = obs.get("mission_target")
            if not wp:
                verdict = "failed"
                reason = "没有任务目标（当前没有进行中的任务或目标不在雷达上）"
                break
        elif not wp:
            verdict = "failed"
            reason = "没有路点（玩家需在地图设一个路点，或 target=mission 用任务目标）"
            break
        d = dist2d(pos or [0, 0], wp)
        min_d = min(min_d, d)
        ticks += 1
        if d < radius:
            verdict = "success"
            reason = f"到达路点（{d:.1f}m < {radius:.0f}m）"
            break
        if not BRIDGE.connected:
            verdict = "failed"
            reason = "与游戏桥的连接断开"
            break
        time.sleep(0.05)
    elapsed = round(time.time() - t0, 1)
    out = {"verdict": verdict, "reason": reason, "ticks": ticks,
           "min_dist_m": round(min_d, 1) if min_d < float("inf") else None,
           "elapsed_sec": elapsed}
    if seen_events:
        out["events"] = seen_events       # 任务窗口内的真实事件随 verdict 入账
    BRIDGE._dump("gta_trace.jsonl", {"ts": time.time(), "event": "verdict",
                                     **out})
    return out


# ---------------------------------------------------------------------------
# 工具清单（形状与 mcp_sherlock_server.py 一致）
# ---------------------------------------------------------------------------
TOOLS: list[dict] = [
    {"name": "gta_status",
     "description": ("GTA 游戏桥连接状态 + 最新车辆状态快照。排查「连不上/没反应」"
                     "先用它：connected、last_error、obs 年龄、排查顺序提示都在返回里。"),
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "gta_get_obs",
     "description": ("取 GTA 里玩家车辆的最新状态：车速（m/s 与 km/h）、坐标 pos、"
                     "朝向 heading、玩家在地图上设的路点 waypoint 及直线距离、"
                     "任务面（mission_active / mission_target / mission_dist_m："
                     "当前剧情任务的开车目标），"
                     "以及 events——游戏里真实发生的事件（ped_hit 撞人 / "
                     "vehicle_damage 碰撞 / wanted 被警察通缉 / rolled_over 翻车 / "
                     "cutscene_start 任务演出 / auto_handback 自动交还等）。"
                     "写驾驶策略前先取它看清楚环境；events 是环境反馈，"
                     "撞了东西/被追了要据此调整策略。"),
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "gta_drive_to",
     "description": ("让车自动开到指定坐标（GTA 原生驾驶 AI 沿路网执行，会避障跟车）。"
                     "speed 单位 m/s（20≈72km/h）；style 1=Normal 2=无视红绿灯 "
                     "3=Rushed 4=避障。返回下发确认 + 当时车辆快照。"),
     "inputSchema": {"type": "object", "properties": {
         "x": {"type": "number"}, "y": {"type": "number"},
         "z": {"type": "number", "description": "默认 0"},
         "speed": {"type": "number", "description": "m/s，默认 20"},
         "style": {"type": "integer", "description": "默认 1"},
     }, "required": ["x", "y"]}},
    {"name": "gta_cruise",
     "description": "沿当前道路巡航（无目的地）。speed 单位 m/s，默认 15。",
     "inputSchema": {"type": "object", "properties": {
         "speed": {"type": "number", "description": "m/s，默认 15"},
     }, "required": []}},
    {"name": "gta_stop", "description": "立即停车（拉手刹）。",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "gta_handback",
     "description": "把车辆控制权交还玩家（结束自动驾驶）。",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "gta_wait_arrive",
     "description": ("阻塞等待环境判定「车是否到达目标」：radius 米内 = success，"
                     "超时/无目标/断连 = failed。target 默认 \"waypoint\"（玩家手动"
                     "路点）；\"mission\"= 任务目标 blip（剧情任务的开车部分用这个）。"
                     "这是 GTA 场景的**验收判据**，由环境规则判定、不经 LLM —— "
                     "驾驶任务完成与否以它的 verdict 为准，不要自己拿距离估断。"),
     "inputSchema": {"type": "object", "properties": {
         "radius": {"type": "number", "description": "到达半径米数，默认 5"},
         "timeout": {"type": "number", "description": "等待上限秒数，默认 180，最大 600"},
         "target": {"type": "string", "enum": ["waypoint", "mission"],
                    "description": "判定目标：waypoint=手动路点，mission=任务目标 blip"},
     }, "required": []}},
]

_DISPATCH = {
    "gta_status": lambda a: gta_status(),
    "gta_get_obs": lambda a: gta_get_obs(),
    "gta_drive_to": lambda a: gta_drive_to(
        a.get("x", 0), a.get("y", 0), a.get("z", 0.0),
        a.get("speed", 20.0), a.get("style", 1)),
    "gta_cruise": lambda a: gta_cruise(a.get("speed", 15.0)),
    "gta_stop": lambda a: gta_stop(),
    "gta_handback": lambda a: gta_handback(),
    "gta_wait_arrive": lambda a: gta_wait_arrive(
        a.get("radius", ARRIVE_M_DEFAULT), a.get("timeout", 180.0),
        a.get("target", "waypoint")),
}

# ---------------------------------------------------------------------------
# JSON-RPC stdio 传输层（与 mcp_sherlock_server.py 同构：tools/call 放工作线程，
# 主循环持续读 stdin —— 取消通知才进得来）
# ---------------------------------------------------------------------------


def _write(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _dispatch(name: str, arguments: dict) -> object:
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"__error__": f"未知工具 {name}"}
    try:
        return fn(arguments or {})
    except Exception as e:
        return {"__error__": f"{type(e).__name__}: {e}"}


def _format_result(py_obj: object) -> dict:
    if isinstance(py_obj, dict) and "__error__" in py_obj:
        err = str(py_obj["__error__"])
        return {"content": [{"type": "text", "text": err}], "isError": True,
                "structuredContent": {"error": err}}
    text = json.dumps(py_obj, ensure_ascii=False)
    sc = py_obj if isinstance(py_obj, dict) else (
        {"results": py_obj} if isinstance(py_obj, list) else {"value": py_obj})
    return {"content": [{"type": "text", "text": text}], "isError": False,
            "structuredContent": sc}


def _serve() -> None:
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
        rid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            _write({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mve-gta", "version": "1.0"},
            }})
        elif method == "notifications/initialized":
            continue
        elif method == "notifications/cancelled":
            _CANCEL.set()
            continue
        elif method == "tools/list":
            _write({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            _CANCEL.clear()
            _name = str(params.get("name") or "")
            _args = params.get("arguments") or {}

            def _work(rid=rid, name=_name, args=_args) -> None:
                try:
                    payload = _format_result(_dispatch(name, args))
                except Exception as e:
                    payload = _format_result({"__error__": f"{type(e).__name__}: {e}"})
                _write({"jsonrpc": "2.0", "id": rid, "result": payload})
            threading.Thread(target=_work, daemon=True).start()
        elif rid is not None:
            _write({"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"unknown method: {method}"}})


if __name__ == "__main__":
    _serve()
