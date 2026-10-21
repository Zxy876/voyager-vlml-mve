#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""消费侧重放已学技能的**墙钟预算**（2026-10-21 Scene G 实测后补）。

事故：许嵩第二次导入（学习后、HIGH_CONFIDENCE 命中），模型正确合成了
5 条技能 `skill_lyrics() → … → skill_reception()`，trace 里 5 条全进了 ——
但每条技能都是一次真实联网取数（一次 web_search 5~20s），串行 5 条
远超 30s 默认墙钟 → 「执行超时（>30s）」整次判死 → 0 行 →
掌握度（只认消费侧成功取数）一次都不涨 → 用户看到「学习前后没变化」。

契约：
  1. 预算随技能条数走：30s 基础 + 25s/条，封顶 180s；
  2. env MVE_CONSUME_TIMEOUT 整体覆盖（下限 30s）；
  3. 非法 env 值不炸，回落到公式。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voyager_vlml.consume import _consume_timeout  # noqa: E402


def test_timeout_scales_with_skills():
    assert _consume_timeout(0) == 30.0, _consume_timeout(0)
    assert _consume_timeout(1) == 55.0, _consume_timeout(1)
    assert _consume_timeout(5) == 155.0, _consume_timeout(5)   # Scene G 那次
    assert _consume_timeout(99) == 180.0, _consume_timeout(99)  # 封顶
    print("PASS  test_timeout_scales_with_skills")


def test_env_override():
    os.environ["MVE_CONSUME_TIMEOUT"] = "45"
    try:
        assert _consume_timeout(0) == 45.0
    finally:
        del os.environ["MVE_CONSUME_TIMEOUT"]
    os.environ["MVE_CONSUME_TIMEOUT"] = "abc"      # 非法值不能炸
    try:
        assert _consume_timeout(5) == 155.0, "非法 env 应回落公式"
    finally:
        del os.environ["MVE_CONSUME_TIMEOUT"]
    print("PASS  test_env_override")


if __name__ == "__main__":
    test_timeout_scales_with_skills()
    test_env_override()
    print("\nALL PASS")
