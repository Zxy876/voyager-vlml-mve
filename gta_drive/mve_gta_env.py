"""mve_gta_env.py — GTA 场景环境：MVE「代码即动作」同构的驾驶环境。

与 mve/env_adapter.py（CodeActionEnv）同构，四口径对齐：
  observation : 插件发来的车辆/路点状态，渲染成 LLM 可读文本
  action      : MVE 学生产出的**代码**（定义 act(obs) 策略函数），
                在受控命名空间执行，每帧由环境调 act(obs) 拿驾驶指令
  reward      : 环境规则给（距路点距离递减 + 到达终止），不经 LLM 判定
  判据        : 成功 = episode 时限内到达路点 ARRIVE_M 米内并停稳（硬拒绝项）

安全（同 env_adapter 的口径）：
  - 学生代码在受限命名空间 exec：没有 import/open/eval/网络
  - 词面黑名单双保险
  - act(obs) 单帧超时 → 判 failed action，不打断 episode
"""

from __future__ import annotations

import re
import threading
import time

from gta_client import GtaClient, dist2d

ARRIVE_M = 5.0
EPISODE_SEC = 180
ACT_TIMEOUT = 1.0          # act(obs) 单帧时限（秒）

# 学生代码的词面黑名单（双保险之一；namespace 已无 builtins）
BLOCKED = ("__import__", "eval(", "exec(", "open(", "subprocess",
           "socket", "os.", "sys.", "shutil", "urllib", "requests")


class GtaEnv:
    """驾驶环境。obs 每帧从桥接插件拉，动作经桥下发。"""

    def __init__(self, host="127.0.0.1", port=9777, timeout=10.0):
        self.client = GtaClient(host, port, timeout=timeout)
        self.last_obs = {}

    # ── observation：文本化（LLM 可读，各维带含义）──
    def obs_text(self, obs: dict) -> str:
        pos = obs.get("pos") or [0, 0, 0]
        wp = obs.get("waypoint")
        sp = (obs.get("speed") or 0) * 3.6
        lines = [
            f"车速: {sp:.0f} km/h（obs['speed'] 为 m/s）",
            f"位置: x={pos[0]:.0f} y={pos[1]:.0f}（obs['pos'] [x,y,z]）",
            f"朝向: {obs.get('heading', 0):.0f}°（obs['heading']）",
            f"发动机健康: {obs.get('health_engine', 0):.0f}",
        ]
        if wp:
            d = dist2d(pos, wp)
            lines.append(f"当前路点: x={wp[0]:.0f} y={wp[1]:.0f}，"
                         f"直线距离 {d:.0f} m（obs['waypoint']）")
        else:
            lines.append("当前路点: 无（地图上没有设路点）")
        return "\n".join(lines)

    # ── 动作空间（学生代码可调的唯四指令）──
    DOC_ACTIONS = """
def drive_to(x, y, z=0.0, speed=20.0, style=1):
    # 开到坐标 (x,y)；speed 单位 m/s（20≈72km/h）；
    # style: 1=Normal 2=无视红绿灯 3=Rushed 4=避障
def cruise(speed=15.0):
    # 沿当前道路巡航
def stop():
    # 立即停车
def handback():
    # 交还玩家控制权
"""

    # ── 学生代码 → 受控策略函数 ──
    def compile_policy(self, code: str) -> tuple[object, str]:
        """执行学生代码，取回 act 策略函数。失败返回 (None, 原因)。"""
        if len(code) > 8000:
            return None, "代码超长（>8000 字符）"
        for w in BLOCKED:
            if w in code:
                return None, f"代码含禁用词面 {w!r}"
        ns: dict = {
            "__builtins__": {},
            "abs": abs, "min": min, "max": max, "round": round,
            "dist2d": dist2d,
        }
        exec(code, ns)                      # noqa: S102 —— 受控命名空间
        act = ns.get("act")
        if not callable(act):
            return None, "代码必须定义策略函数 act(obs)"
        return act, ""

    def _call_act(self, act, obs):
        """act(obs) 带单帧超时（线程跑，超时判 None）。"""
        box: dict = {}

        def _t():
            try:
                box["a"] = act(dict(obs))
            except Exception as e:
                box["err"] = f"{type(e).__name__}: {e}"

        th = threading.Thread(target=_t, daemon=True)
        th.start()
        th.join(ACT_TIMEOUT)
        if "a" not in box:
            return None, box.get("err") or f"act 超时（>{ACT_TIMEOUT}s）"
        return box["a"], box.get("err", "")

    # ── 一个 episode：环境主循环（reward 来自环境规则，非 LLM）──
    def run_episode(self, act, *, on_tick=None) -> dict:
        """跑一个 episode，返回 {verdict, reason, ticks, min_dist, trace}。"""
        t0 = time.time()
        min_dist = float("inf")
        trace: list[dict] = []
        verdict = reason = None
        ticks = 0
        last_obs_t = 0.0
        while verdict is None:
            if time.time() - t0 > EPISODE_SEC:
                verdict, reason = "failed", f"超时（{EPISODE_SEC}s 未到达路点）"
                break
            obs = self.client.recv_obs()
            if obs is None:
                verdict, reason = "failed", "与游戏桥的连接断开"
                break
            if obs.get("ts") == last_obs_t:
                time.sleep(0.02)
                continue
            last_obs_t = obs.get("ts")
            self.last_obs = obs
            ticks += 1

            wp = obs.get("waypoint")
            pos = obs.get("pos") or [0, 0]
            if not wp:
                verdict, reason = "failed", "没有路点（玩家需在地图设一个路点）"
                break
            d = dist2d(pos, wp)
            min_dist = min(min_dist, d)

            a, err = self._call_act(act, obs)
            if a is None:
                trace.append({"tick": ticks, "err": err})
                if len(trace) > 30 and all(t.get("err") for t in trace[-10:]):
                    verdict = "failed"
                    reason = f"act 连续报错：{trace[-1]['err']}"
                    break
                self.client.stop()
            elif isinstance(a, dict) and a.get("t"):
                self.client.send_action(a)
                trace.append({"tick": ticks, "dist": round(d, 1),
                              "action": a.get("t")})
                if d < ARRIVE_M:
                    self.client.stop()
                    verdict, reason = "success", f"到达路点（{d:.1f}m < {ARRIVE_M}m）"
                    break
            if on_tick:
                on_tick(ticks, obs, a)
        return {"verdict": verdict, "reason": reason, "ticks": ticks,
                "min_dist": round(min_dist, 1), "trace": trace[-50:]}


# 学生代码里的 ```python 围栏剥离
FENCE = re.compile(r"```(?:python)?\s*(.*?)```", re.S)


def extract_code(text: str) -> str:
    m = FENCE.search(text)
    return (m.group(1) if m else text).strip()
