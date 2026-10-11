#!/usr/bin/env python3
"""Voyager 控制相关文件的共享配置（被 voyager_pilot / voyager_learn_reset 共用）。

VPS 上 Voyager 装在 /root/voyager，venv 在 /root/voyager-venv，密钥在 /root/.voyager_secret。
本地或换机器用环境变量覆盖即可，不必改代码。
"""
from __future__ import annotations

import os
from pathlib import Path

# Voyager 源码 / 运行目录（run_voyager_learn.py 在这里）
VOYAGER_DIR = Path(os.environ.get("VOYAGER_DIR") or "/root/voyager")
# 跑 Voyager 用的 Python（带全部依赖的 venv）
VOYAGER_PY = os.environ.get("VOYAGER_PY") or "/root/voyager-venv/bin/python"
# DEEPSEEK_API_KEY 存放文件（run_voyager.sh / voyager_pilot 都会读它）
SECRET_FILE = Path(os.environ.get("VOYAGER_SECRET") or "/root/.voyager_secret")
# 检查点目录（相对 VOYAGER_DIR）：技能库 / 课程进度 / 事件历史都在这里
CKPT_DIR = os.environ.get("VOYAGER_CKPT_DIR") or "ckpt"
DEFAULT_MAX_ITER = int(os.environ.get("VOYAGER_MAX_ITER") or "160")
# run_voyager_learn.py 每任务写的进度文件
PROGRESS = VOYAGER_DIR / "voyager_progress.json"

# 三个清档目标的真实路径
SKILL_DIR = VOYAGER_DIR / CKPT_DIR / "skill"          # 清库：已学 JS 技能库
CURRICULUM_DIR = VOYAGER_DIR / CKPT_DIR / "curriculum"  # 清记忆：课程进度 / QA 缓存
EVENTS_DIR = VOYAGER_DIR / CKPT_DIR / "events"          # 清记忆：事件历史
