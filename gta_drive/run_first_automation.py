"""run_first_automation.py — 第一个自动化：MVE 自己写驾驶代码、自己开到路点。

链路（全部复用 MVE 已有能力，无 mock、无手写驾驶）：
  1. 玩家在 GTA V 里上车、按 F9 激活 MVEBridge、在地图设一个路点
  2. 本脚本把 obs 文本 + 动作空间说明交给 MVE 学生（llm_client，glm-4-flash）
  3. 学生产出一段 Python（定义 act(obs) 策略函数）——这是 MVE 的输出
  4. GtaEnv 在受控沙箱执行该代码，逐帧 act(obs) 下发驾驶指令
  5. 环境判据（非 LLM）：到达路点 5m 内 = success；超时/断线/连续报错 = failed
  6. 全程落 trace（学习侧账本），同构 learn_trace

跑法（在装有 GTA V 的 Windows 机上）：
  cd gta_drive
  python run_first_automation.py            # 游戏内按 F9 后再跑
  python run_first_automation.py --model glm-4.5   # 换强模型重写策略
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "mve"))        # 复用 MVE 的 llm_client
sys.path.insert(0, str(Path(__file__).resolve().parent / "adapter"))

import llm_client                            # noqa: E402  (mve/llm_client.py)
from mve_gta_env import GtaEnv, extract_code  # noqa: E402

TRACE = Path(__file__).with_suffix(".trace.jsonl")

SYSTEM = """你是 MVE 学习循环里的学生。任务：写一段 Python 定义策略函数 act(obs)，
让 GTA V 里的一辆车自动开到玩家在地图上设的路点并停稳。

obs 是 dict，字段：
  speed      车速 m/s
  pos        [x, y, z] 车辆坐标
  heading    朝向角度
  waypoint   [x, y, z] 当前路点（可能为 None）
可调动作（由环境注入你的命名空间）：
def drive_to(x, y, z=0.0, speed=20.0, style=1)   # 开到坐标，speed 单位 m/s
def cruise(speed=15.0)                            # 沿路巡航
def stop()                                        # 停车
def handback()                                    # 交还玩家
辅助：dist2d(a, b) 算两点平面距离；abs/min/max/round 可用。
禁止：import、open、网络、exec/eval。

规则：
1. act(obs) 每帧被调用一次，返回一个动作调用 dict（如 {"t":"drive_to", ...}）
   或 None（本帧不动）。
2. 判据是「到达路点 5m 内」，环境自己判定，你不用管终止。
3. 只输出代码，不要解释。代码里必须有 act 函数。"""


def gen_policy(model: str) -> tuple[str, str]:
    """MVE 学生产出驾驶策略代码（MVE 的输出，不是脚本写死的）。"""
    text = llm_client.chat(
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content":
          "把车开到路点。起点任意、路点任意（obs 里给）。"
          "策略要能从静止起步、持续朝路点开。"}],
        temperature=0.2, max_tokens=1200, model=model)
    code = extract_code(text)
    return code, text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9777)
    ap.add_argument("--model", default="")       # 留空 = mve/.env 的默认模型
    a = ap.parse_args()

    print("① 连接游戏桥…")
    env = GtaEnv(a.host, a.port)

    print("② MVE 学生生成驾驶策略（模型: %s）…" % (a.model or llm_client.MODEL))
    t0 = time.time()
    code, raw = gen_policy(a.model)
    print(f"   生成耗时 {time.time() - t0:.0f}s，代码 {len(code)} 字")
    act, err = env.compile_policy(code)
    if act is None:
        print(f"✗ 学生代码不合格：{err}")
        print("---- 学生原始输出 ----")
        print(raw[:2000])
        return 1

    print("③ 启动 episode：请在游戏里上车、地图设路点、按 F9 激活桥…")
    result = env.run_episode(
        act,
        on_tick=lambda t, obs, act_: None,       # trace 已在 env 内记
    )
    print(f"④ 判据：{result['verdict']}（{result['reason']}）"
          f" ticks={result['ticks']} 最近距离={result['min_dist']}m")

    TRACE.write_text(json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": a.model or llm_client.MODEL,
        "policy_code": code,
        "result": result,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"   账本：{TRACE}")
    return 0 if result["verdict"] == "success" else 2


if __name__ == "__main__":
    sys.exit(main())
