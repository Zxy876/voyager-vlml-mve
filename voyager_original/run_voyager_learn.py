#!/usr/bin/env python3
"""真实 Voyager 在 MC 里学习：调 ``Voyager.learn()`` 自动课程循环，原版 JS 技能入库。

设计要点（与原版一致，只换 LLM / embeddings 后端）：
- LLM：四个 agent 经 ``voyager.neko_model.ChatOpenAI`` 走 DeepSeek
  （env 里设 ``DEEPSEEK_API_KEY`` 即生效；``OPENAI_API_KEY`` 留空）。
- embeddings：``skill`` / ``curriculum`` 检索走 ``local_embeddings``（本地或哈希兜底）。
- 环境：传 ``mc_port`` 让 Voyager 接**外部已起的 MC 服务器**（不自起服）。

可停止（镜像 MVE 面板「停止」键语义）：
- 收到 SIGTERM/SIGINT 时调 ``voyager.request_stop()``，learn() 在**当前任务结束后**
  优雅退出（技能已按任务落盘，不会被硬杀丢半个技能）。
- 每完成一个任务写 ``voyager_progress.json``（completed/failed/iteration），
  面板据此显示「已学 N 个技能」。

用法（在 /root/voyager 下，且已设 DEEPSEEK_API_KEY）：
    python run_voyager_learn.py --mc-port 25565 --ckpt-dir ckpt
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

PROGRESS = Path(__file__).resolve().parent / "voyager_progress.json"


def _write_progress(completed: list[str], failed: list[str], iteration: int) -> None:
    """把当前进度落盘，让面板（voyager_pilot）能读出结构化的学习状态。

    不依赖解析 Voyager 的 stdout：learn() 每任务调一次钩子，这里直接写 JSON。
    """
    try:
        PROGRESS.write_text(json.dumps({
            "completed": completed,
            "failed": failed,
            "completed_count": len(completed),
            "failed_count": len(failed),
            "iteration": iteration,
            "updated_at": time.time(),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mc-port", type=int, default=25565)
    ap.add_argument("--server-port", type=int, default=3000)
    ap.add_argument("--ckpt-dir", default="ckpt")
    ap.add_argument("--max-iterations", type=int, default=160)
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--skill-library-dir", default=None)
    args = ap.parse_args()

    # 原版会把 OPENAI_API_KEY 设成传入值（我们传空），这里显式留空，
    # 让 neko_model 回落到 DeepSeek；不要让它误以为是可用 key。
    os.environ["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY", "")

    from voyager.voyager import Voyager

    voyager = Voyager(
        mc_port=args.mc_port,
        server_port=args.server_port,
        openai_api_key="",  # 不用 OpenAI
        max_iterations=args.max_iterations,
        action_agent_model_name=args.model,
        curriculum_agent_model_name=args.model,
        curriculum_agent_qa_model_name=args.model,
        critic_agent_model_name=args.model,
        skill_manager_model_name=args.model,
        ckpt_dir=args.ckpt_dir,
        skill_library_dir=args.skill_library_dir,
        resume=False,
    )
    # 进度钩子：每任务写 voyager_progress.json
    voyager._on_progress = _write_progress
    print(f"[run_voyager_learn] Voyager 初始化完成，mc_port={args.mc_port}")

    # 停止信号：面板「停止」键 → voyager_pilot 转发 SIGTERM 到这里 → 优雅退出。
    # 不用默认行为（直接终止会留下连着 MC 的半死 bot），而是在任务边界退出。
    def _stop_handler(signum, _frame):
        print(f"[run_voyager_learn] 收到停止信号 {signum}，将在当前任务结束后退出")
        voyager.request_stop()

    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)

    try:
        voyager.learn()
    except KeyboardInterrupt:
        print("[run_voyager_learn] 用户中断（Ctrl-C）")
    finally:
        voyager.close()
        print("[run_voyager_learn] 已关闭环境")


if __name__ == "__main__":
    main()
