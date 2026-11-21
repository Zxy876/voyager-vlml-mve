#!/usr/bin/env python3
"""seed_watcher.py — 游戏侧种子监视器：人在游戏里设路点 = 投种子。

口径（用户 2026-11-21 二次拍板：F9 多余，地图上选完地点直接学习）
----------
种子不是面板里的一句文本——**种子的本体是游戏里的真实事件**：玩家上车
（不在步行）、在地图设了路点（或剧情任务里有开车目标）。这两件事同时
成立时，本进程调面板**已有的** /api/learn/start（MVE 一行不改）自动投
种子：

    question = "让车自动开到地图路点 (x, y) 并停稳"

坐标来自当帧 obs，所以每个目的地的种子都是真实、不同、账本可读的任务。
学习闭环里 Phase2 学生代码经 MCP 工具（mcp_gta_gta_drive_to /
gta_wait_arrive）真开车，判据环境给出；插件对 drive 类动作自动接管
（无需 F9，cutscene 除外），F9 只是人手动拿回方向盘的开关。
自由学由主循环自己出题连转。

「一直开不停」的种子来源：人连续设新路点（与上次已投种子相差 >50m 视为
新种子）；到账 + 新路点 = 下一轮，技能与会话持续积累。

与 MCP server 共存：中继已改为 1×GAME ↔ N×MVE 广播组（obs 广播、动作
汇聚），监视器只读 obs，不与 Phase2 的 MCP 通道互踢。

部署：systemd mve-gta-watcher（VPS，与面板同机）。--dry-run 只打印不投。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ADAPTER = Path(__file__).resolve().parent / "adapter"
sys.path.insert(0, str(ADAPTER))
from gta_client import GtaClient, dist2d  # noqa: E402

NEW_SEED_M = 50.0        # 与上次已投种子相距多远才算「新路点」
RETRY_OK = 30.0          # 投递失败后的重试间隔
BUSY_POLL = 20.0         # 面板上一轮还在跑时的轮询间隔


def panel_start(panel: str, question: str) -> dict:
    req = urllib.request.Request(
        panel.rstrip("/") + "/api/learn/start",
        data=json.dumps({"question": question}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("GTA_BRIDGE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("GTA_BRIDGE_PORT", "9778")))
    ap.add_argument("--handshake", default=os.environ.get("GTA_BRIDGE_HANDSHAKE", "MVE"))
    ap.add_argument("--panel", default=os.environ.get(
        "GTA_PANEL_URL", "http://127.0.0.1:8779/mve-gta"))
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印种子事件，不真调面板（联调用）")
    a = ap.parse_args()

    print(f"[seed] 连游戏桥 {a.host}:{a.port}（handshake={a.handshake!r}）", flush=True)
    client = GtaClient(a.host, a.port, timeout=15.0, handshake=a.handshake)
    print("[seed] 桥已连。等种子：上车 + 地图设路点（选完即学，无需 F9）", flush=True)

    last_seeded_wp = None
    while True:
        try:
            obs = client.recv_obs()
        except TimeoutError:
            # obs 暂时停流不算断线：GTA 开地图/暂停菜单时 OnTick 停转，
            # obs 会静默十几秒——安静等下一帧，不崩不重连
            time.sleep(0.5)
            continue
        if obs is None:
            print("[seed] 桥断开，30s 后重连", flush=True)
            time.sleep(30.0)
            try:
                client = GtaClient(a.host, a.port, timeout=15.0,
                                   handshake=a.handshake)
                print("[seed] 重连成功", flush=True)
            except Exception as e:
                print(f"[seed] 重连失败：{e}", flush=True)
            continue
        if obs.get("on_foot"):
            continue                              # 没上车，不算驾驶种子
        wp = obs.get("waypoint")
        source = "waypoint"
        if not wp and obs.get("mission_active") and obs.get("mission_target"):
            # 没手动路点但在剧情任务里：任务的开车目标本身就是种子
            #（cutscene 演出时插件已自动交还控制，不打架）
            wp = obs.get("mission_target")
            source = "mission"
        if not wp:
            continue                              # 既无路点也无任务目标
        pos = obs.get("pos") or [0, 0]
        if last_seeded_wp is not None and \
                dist2d(pos, last_seeded_wp) <= NEW_SEED_M and \
                dist2d(wp, last_seeded_wp) <= NEW_SEED_M:
            continue                              # 还是上一个种子附近
        if source == "mission":
            question = (f"完成当前任务的开车部分：把车自动开到任务目标 "
                        f"({wp[0]:.0f}, {wp[1]:.0f}) 并停稳，"
                        f"当前在 ({pos[0]:.0f}, {pos[1]:.0f})，"
                        f"到达后用 gta_wait_arrive target=mission 验收")
        else:
            # 人手点的路点可能不在路上（死胡同/院子/山坡）：GTA 原生驾驶 AI
            # 只能开到最近街道节点，题面若仍写原坐标，车会在附近绕圈永远到
            # 不了（实测 14:44 车卡在目标 44m 的死胡同里乱开）。插件已把路点
            # 吸附到路网，这里用「可开到的那个点」出题，原坐标一并报出来。
            drive = obs.get("waypoint_drive") or wp
            offroad = float(obs.get("waypoint_offroad_m") or 0.0)
            if offroad > 5:
                question = (f"让车自动开到地图路点 ({wp[0]:.0f}, {wp[1]:.0f}) 最近的"
                            f"可行驶位置 ({drive[0]:.0f}, {drive[1]:.0f}) 并停稳"
                            f"（该路点离道路 {offroad:.0f}m，车开不进原坐标），"
                            f"当前在 ({pos[0]:.0f}, {pos[1]:.0f})")
            else:
                question = (f"让车自动开到地图路点 ({wp[0]:.0f}, {wp[1]:.0f}) 并停稳，"
                            f"当前在 ({pos[0]:.0f}, {pos[1]:.0f})")
        print(f"[seed] ★ 种子：{question}", flush=True)
        last_seeded_wp = list(wp)
        if a.dry_run:
            print("[seed] dry-run：不投面板", flush=True)
            continue
        while True:                               # 投到面板收下为止
            try:
                r = panel_start(a.panel, question)
                if r.get("busy"):
                    print(f"[seed] 面板上一轮还在跑，{BUSY_POLL:.0f}s 后重投",
                          flush=True)
                    time.sleep(BUSY_POLL)
                    continue
                print(f"[seed] 已投面板：{json.dumps(r, ensure_ascii=False)}",
                      flush=True)
                break
            except Exception as e:
                print(f"[seed] 投递失败（{type(e).__name__}: {e}），"
                      f"{RETRY_OK:.0f}s 后重试", flush=True)
                time.sleep(RETRY_OK)
        # 到账后回到 obs 循环；新路点（>50m）= 下一个种子


if __name__ == "__main__":
    sys.exit(main())
