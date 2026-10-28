#!/usr/bin/env python3
"""天授 env 二分诊断（2026-10-28 路线图第 1 步）。

⭐ 这是**排除法不是定位法**：天授 env 的 reward 由环境规则给出、无 LLM 判官，
   幻觉这个变量被设计掉了。所以：
     机制正常  → 根因锁定在判官（幻觉），下一步修判官/补 reward
     机制不正常 → 先修机制（动作空间/反馈回路），别急着归因幻觉

学习语义（同构 MVE 学习循环的最小闭环）：
  每轮 agent 看历史反馈（上轮代码 / eval 均值 / 死因 / 末段轨迹）改进代码 →
  eval 5 个固定 seed 的 episode 取均值 → 记入 return 序列。
  技能库/里程碑不进本实验 —— 目的只检验「reward 可信 + 反馈驱动改进」能否让
  曲线上升。

容错口径（env_adapter.mechanism_ok，第 0 步定死）：
  比形状不比分；后半 ≥100 或 后半 ≥ 前半×1.2 判机制正常。

跑法：
  MOCK=1  python mve/tianshou_diagnose.py   # 管道自检（启发式，应满分无趋势）
  python mve/tianshou_diagnose.py           # 真 LLM（glm-4-flash）诊断
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from env_adapter import CodeActionEnv, mechanism_ok

N_ROUNDS = 10
EVAL_EPISODES = 5
SEED_BASE = 12345          # eval seeds 固定 → 各轮可比（同题重测，不是换题）
TRAIN_SEED_BASE = 99000    # 训练反馈 episode 的 seed（与 eval 分开）

ENV_DESC = """环境：CartPole-v1（经典倒立摆控制）。
每步你可以让小车向左（0）或向右（1）推。杆保持直立、小车不出界则每步 +1 分。
obs = [位置, 速度, 角度, 角速度]：位置是横坐标，角度是杆与竖直方向的夹角（弧度）。
episode 在杆倒下/小车出界（terminated）或满 500 步（truncated）时结束。满分 500。
随机乱选平均只能活约 20 步。"""


# ---------------------------------------------------------------- agents

class MockAgent:
    """管道自检：经典启发式，应第一轮即满分（口径判正常但注明无趋势）。"""

    def improve(self, history: list[dict]) -> tuple[str, str]:
        return "action = 1 if obs[2] + obs[3] > 0 else 0", "经典启发式"


class LLMAgent:
    """真诊断 agent：同构 MVE「反馈驱动改进」——看历史、改代码，无判官。"""

    def __init__(self) -> None:
        from llm_client import chat_json
        self._chat_json = chat_json

    def improve(self, history: list[dict]) -> tuple[str, str]:
        hist_lines = []
        for h in history[-4:]:
            line = (f"第 {h['round']} 轮：eval 均值 {h['mean']:.0f} 步。\n"
                    f"代码：\n{h['code']}\n")
            fb = h.get("feedback") or ""
            if fb:
                line += f"教练反馈：{fb}\n"
            hist_lines.append(line)
        prompt = (f"{ENV_DESC}\n\n要求：{CodeActionEnv.ACTION_SPEC}\n\n"
                  + ("历轮成绩与反馈：\n" + "\n".join(hist_lines) if hist_lines
                     else "这是第一轮，还没有历史成绩。\n")
                  + "\n基于历史反馈改进策略代码，输出 JSON："
                    '{"code": "...", "idea": "一句话思路"}')
        data = self._chat_json(
            [{"role": "system", "content": "你是控制策略程序员，只输出 JSON。"},
             {"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=900)
        code = str(data.get("code") or "").strip()
        idea = str(data.get("idea") or "").strip()
        if not code:
            raise RuntimeError(f"LLM 未返回 code：{json.dumps(data, ensure_ascii=False)[:200]}")
        # 容忍 ```python 包裹
        if code.startswith("```"):
            code = code.strip("`")
            if code.lower().startswith("python"):
                code = code[6:]
            code = code.strip()
        return code, idea


# ---------------------------------------------------------------- 反馈渲染

def feedback_from_train(env: CodeActionEnv, code: str, seed: int) -> str:
    """跑一个训练 episode（collect=True），渲染成教练反馈文本。"""
    ep = env.run_episode(code, seed=seed, collect=True)
    if ep.get("error"):
        return (f"代码第一步就报错：{ep['error']}。"
                f"修复代码，保证每步都能执行出 action。")
    tail = ep.get("trace") or []
    why = ("杆倒下/出界（terminated）" if ep.get("terminated") else "满 500 步（truncated）")
    tail_txt = "\n".join(f"  {t['step']}: action={t['action']} obs={t['obs_text']}"
                         for t in tail[-5:])
    return (f"存活 {ep['steps']} 步（return {ep['return']:.0f}），最终死因：{why}。"
            f"死亡前最后 5 步：\n{tail_txt}\n"
            f"据此调整策略：让杆倒向哪边就往哪边推。")


# ---------------------------------------------------------------- 主流程

def main() -> int:
    mock = len(sys.argv) > 1 and sys.argv[1] == "MOCK"
    agent: MockAgent | LLMAgent = MockAgent() if mock else LLMAgent()
    label = "MOCK（启发式）" if mock else f"LLM（{Path('llm_client.py') and 'glm-4-flash'}）"

    env = CodeActionEnv("CartPole-v1")
    history: list[dict] = []
    means: list[float] = []

    print(f"== 天授 env 二分诊断：{label} ==")
    print(f"轮数={N_ROUNDS} eval_episodes={EVAL_EPISODES} "
          f"eval seeds={SEED_BASE}..{SEED_BASE + EVAL_EPISODES - 1}\n")

    for rnd in range(1, N_ROUNDS + 1):
        t0 = time.time()
        try:
            code, idea = agent.improve(history)
        except Exception as e:
            # LLM 失败 ≠ 机制失败，但也不许静默：如实记录并终止（结论标记不完整）
            print(f"第 {rnd} 轮 agent 失败：{e}")
            print("\n结论：诊断不完整（agent 侧故障），不能作为二分依据。")
            return 2
        evals = [env.run_episode(code, seed=SEED_BASE + i)["return"]
                 for i in range(EVAL_EPISODES)]
        mean = statistics.mean(evals)
        fb = feedback_from_train(env, code, TRAIN_SEED_BASE + rnd)
        history.append({"round": rnd, "code": code, "mean": mean,
                        "idea": idea, "feedback": fb})
        means.append(mean)
        print(f"第 {rnd:2d} 轮  eval 均值 {mean:6.1f}  "
              f"({min(evals):.0f}~{max(evals):.0f})  思路：{idea or '—'}  "
              f"[{time.time() - t0:.1f}s]")

    env.close()

    print("\n-- return 序列 --")
    print(" ".join(f"{m:.0f}" for m in means))
    ok, why = mechanism_ok(means)

    print(f"\n== 二分结论（容错口径：{why}）==")
    if ok:
        print("机制正常 —— reward 可信且反馈驱动改进有效。")
        print("→ 根因锁定在判官（幻觉）：回 SQL 场景修判官 / 补 rule-based reward")
        print("  （落点：原版 voyager.py:296 恒 0 槽位语义，r 进索引排序）。")
    else:
        print("机制不正常 —— 先修机制（动作空间映射 / 反馈回路 / 代码执行），")
        print("  幻觉先别急着归因：判官变量在此实验中根本不存在。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
