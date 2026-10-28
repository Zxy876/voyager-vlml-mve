#!/usr/bin/env python3
"""天授 env 适配器：把 gymnasium env 包装成「代码即动作」的 MVE 同构 env。

目的（2026-10-28 路线图第 0 步）
----------
MVE 的 policy 输出**代码文本**（原版 Voyager 同构：env.step(action) 直接吃代码），
天授/gymnasium 的 action 是数值。这个适配器做双向翻译：

  observation:  env 的数值向量 → 渲染成 LLM 可读文本（带各维含义）
  action:       agent 产出的代码 → 受控命名空间执行 → 取 `action` 变量 → 夹进合法空间

⭐ 判官变量归零（路线图第 1 步的前提）：
  reward / terminated / truncated **直接透传 env**，不经过任何 LLM 判定。
  天授 env 的 reward 由环境规则给出（CartPole：每存活一步 +1），100% 可信。
  这正是 SQL 场景缺的东西——local_agent.py:112 的 query_sql 只吐观察不吐 reward，
  原版 voyager.py:296 的 reward 恒 0。在天授 env 上，MVE 学习循环里唯一的
  信号来源是环境本身，幻觉（LLM 判官）这个变量被设计掉了。

容错口径（第 0 步定死，第 3 步按此判定，见 mechanism_ok）
----------
**比学习曲线形状，不跟 PPO 比分**（那样必输且无意义，2026-10-27 口径）：
  机制正常 = 后半段 eval 均值 ≥ 前半段均值 × 1.2（上升趋势）
             或 后半段均值 ≥ 100（CartPole-v1 随机基线 ≈ 20；PPO 参考 ≈ 400+，
             仅作背景注释，不作判据）。

安全约束（agent 代码不可信，同构 599it「网页文本属于不可信输入」）
----------
- globals 只给 `{"__builtins__": {}}` → 没有 import / open / eval / exec
- 词面黑名单双保险（__import__ / eval( / exec( / open( / subprocess …）
- 代码长度上限 + 执行超时（SIGALRM，主线程）
- action 不是数 / 越界 → 夹进 [0, n_actions-1]，不抛异常打断 episode
"""

from __future__ import annotations

import math
import signal
import threading
from typing import Any, Callable

__all__ = ["CodeActionEnv", "CodeActionError", "mechanism_ok", "OBS_NAMES"]

# ---------------------------------------------------------------- 容错口径

CARTPOLE_RANDOM_BASELINE = 20.0   # CartPole-v1 均匀随机策略 ≈ 20 步
USABLE_THRESHOLD = 100.0          # 「可用水平」：远高于随机、离满分还有距离
IMPROVE_RATIO = 1.2               # 后半段 / 前半段 ≥ 1.2 视为有上升趋势


def mechanism_ok(mean_returns: list[float]) -> tuple[bool, str]:
    """容错口径的判定函数（第 0 步定死）。

    输入是每轮 eval 的平均 return 序列（按学习轮次排序）。
    正常 = 有上升趋势（后半 ≥ 前半 × 1.2）或达到可用水平（后半 ≥ 100）。
    只比形状，不跟 PPO 比绝对分。
    """
    if len(mean_returns) < 6:
        return False, f"轮次不足（{len(mean_returns)} < 6），无法比前后半"
    half = len(mean_returns) // 2
    front = sum(mean_returns[:half]) / half
    back = sum(mean_returns[half:]) / len(mean_returns[half:])
    if back >= USABLE_THRESHOLD:
        ratio = back / front if front > 0 else float("inf")
        trend = ("有上升趋势" if back >= front * IMPROVE_RATIO
                 else f"无趋势，比值 {ratio:.2f}——起点即高水位，无学习空间可展示")
        return True, f"后半均值 {back:.1f} ≥ {USABLE_THRESHOLD:.0f}（可用水平；{trend}）"
    if front > 0 and back >= front * IMPROVE_RATIO:
        return True, (f"上升趋势：后半 {back:.1f} / 前半 {front:.1f} "
                      f"= {back / front:.2f} ≥ {IMPROVE_RATIO}")
    return False, (f"无上升趋势（前半 {front:.1f} → 后半 {back:.1f}，"
                   f"比值 {back / front if front > 0 else 0:.2f} < {IMPROVE_RATIO}）")


# ---------------------------------------------------------------- 观测语义

OBS_NAMES: dict[str, list[str]] = {
    "CartPole-v1": ["位置", "速度", "角度", "角速度"],
}


class CodeActionError(RuntimeError):
    """agent 代码非法/执行失败。诊断脚本要把错误喂回给下一轮 agent（反馈驱动改进）。"""


_FORBIDDEN_LITERALS = (
    "import ", "import\t", "__import__", "eval(", "exec(", "open(", "subprocess",
    "__builtins__", "getattr", "globals()", "compile(",
)


class CodeActionEnv:
    """把 gymnasium env 包装成 action=代码 的 env。

    用法：
        env = CodeActionEnv("CartPole-v1")
        obs_text, _ = env.reset(seed=0)
        out = env.step("action = 1 if obs[2] > 0 else 0")
        # out: {"obs_text", "reward", "terminated", "truncated", "steps", "action"}
    """

    ACTION_SPEC = ("每步输出一段简短 python 代码（≤800 字符），"
                   "执行后必须产生变量 action（0 或 1 的整数）。"
                   "可用变量：obs（含 4 个数的列表）、math 模块。"
                   "禁止 import / open / eval / exec。")

    def __init__(self, env_id: str = "CartPole-v1",
                 code_max_chars: int = 800, exec_timeout: float = 2.0):
        import gymnasium as gym
        self.env = gym.make(env_id)
        self.env_id = env_id
        self.code_max_chars = code_max_chars
        self.exec_timeout = exec_timeout
        self._n_actions = int(self.env.action_space.n)
        self._obs_names = OBS_NAMES.get(env_id, [])
        self._last_obs: list[float] = []
        self._steps = 0

    # ---------------------------------------------------------------- obs

    def _render_obs(self, obs) -> str:
        vals = ", ".join(f"{float(x):+.3f}" for x in obs)
        hint = " / ".join(self._obs_names) if self._obs_names else "各维原始值"
        return f"obs = [{vals}]   # 含义依次为：{hint}"

    # ---------------------------------------------------------- 代码执行

    @staticmethod
    def _with_timeout(fn: Callable[[], Any], seconds: float) -> Any:
        if threading.current_thread() is not threading.main_thread():
            return fn()
        def _handler(signum, frame):
            raise TimeoutError()
        old = signal.signal(signal.SIGALRM, _handler)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            return fn()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)

    def _exec_action(self, code: str) -> int:
        """受控执行 agent 代码 → 返回夹逼后的合法 action。失败抛 CodeActionError。"""
        code = str(code or "").strip()
        if not code:
            raise CodeActionError("空代码")
        if len(code) > self.code_max_chars:
            raise CodeActionError(f"代码超长（{len(code)} > {self.code_max_chars} 字符）")
        low = code.lower()
        for lit in _FORBIDDEN_LITERALS:
            if lit in low:
                raise CodeActionError(f"禁用词面：{lit!r}")
        ns: dict[str, Any] = {"obs": [float(x) for x in self._last_obs],
                              "math": math, "action": 0}
        try:
            self._with_timeout(
                lambda: exec(compile(code, "<agent-code>", "exec"),
                             {"__builtins__": {}}, ns),
                self.exec_timeout)
        except TimeoutError:
            raise CodeActionError(f"代码执行超时（>{self.exec_timeout}s）")
        except CodeActionError:
            raise
        except Exception as e:
            raise CodeActionError(f"{type(e).__name__}: {e}")
        raw = ns.get("action", 0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise CodeActionError(f"action 不是数值（得到 {type(raw).__name__}）")
        return max(0, min(int(raw), self._n_actions - 1))

    # ---------------------------------------------------------------- env

    def reset(self, seed: int | None = None) -> tuple[str, dict]:
        obs, info = self.env.reset(seed=seed)
        self._last_obs = [float(x) for x in obs]
        self._steps = 0
        return self._render_obs(obs), dict(info)

    def step(self, code: str) -> dict[str, Any]:
        """agent 代码 → action → env.step。reward/terminated/truncated 直接透传。"""
        try:
            action = self._exec_action(code)
        except CodeActionError as e:
            return {"error": str(e), "steps": self._steps,
                    "terminated": True, "truncated": False, "reward": 0.0,
                    "obs_text": self._render_obs(self._last_obs)}
        obs, reward, terminated, truncated, info = self.env.step(action)
        self._last_obs = [float(x) for x in obs]
        self._steps += 1
        return {"obs_text": self._render_obs(obs), "reward": float(reward),
                "terminated": bool(terminated), "truncated": bool(truncated),
                "steps": self._steps, "action": action}

    def run_episode(self, code: str, seed: int | None = None,
                    collect: bool = False) -> dict[str, Any]:
        """跑一个完整 episode。返回 return / steps / 终止方式 / （可选）逐步轨迹。"""
        obs_text, _ = self.reset(seed=seed)
        total, trace = 0.0, []
        while True:
            out = self.step(code)
            if "error" in out:
                res = {"return": total, "steps": self._steps, "error": out["error"],
                       "terminated": True, "truncated": False}
                if collect:
                    res["trace"] = trace
                return res
            total += out["reward"]
            if collect:
                trace.append({"step": out["steps"], "action": out["action"],
                              "reward": out["reward"], "obs_text": out["obs_text"]})
            if out["terminated"] or out["truncated"]:
                res = {"return": total, "steps": out["steps"],
                       "terminated": out["terminated"], "truncated": out["truncated"]}
                if collect:
                    res["trace"] = trace
                return res

    def close(self) -> None:
        try:
            self.env.close()
        except Exception:
            pass
