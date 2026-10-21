#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板 /api/state 的**单飞 + 短缓存**契约（2026-10-21 黑客松面板挂死取证后补）。

事故形态（py-spy 实锤）：前端每 4s 轮询 /api/state，第一个请求一慢，后续轮询
并发堆线程 —— ThreadingHTTPServer 来者不拒，几十个线程各自跑 build_state、
各自开 duckdb 连接（77 个 fd 同时开着），内存冲到 3.2G 开始换页 → 接口永远
不返回 → 浏览器报 "Unexpected end of JSON input"。

契约：
  1. 同一时刻 build_state 的**重活只算一遍**（单飞），并发调用共享同一份结果；
  2. 等待方拿到的是**完整结果**（等 build 完），不是半截的 {}；
  3. TTL 内直接给缓存；`build_state_invalidate()` 后强制重算。
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import panel_data  # noqa: E402


def test_single_flight_and_cache():
    real = panel_data._build_state_uncached
    calls = {"n": 0}

    def slow():
        calls["n"] += 1
        time.sleep(0.5)
        return real()

    panel_data._build_state_uncached = slow
    try:
        panel_data.build_state_invalidate()
        results: list = []

        def worker():
            results.append(panel_data.build_state())

        ts = [threading.Thread(target=worker) for _ in range(12)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert calls["n"] == 1, f"并发 12 次调用实际算了 {calls['n']} 遍"
        assert all(r is results[0] for r in results), "应共享同一份结果"
        assert all(r for r in results), "等待方应拿到完整结果，不是 {}"

        time.sleep(panel_data._STATE_TTL + 0.2)
        panel_data.build_state()
        assert calls["n"] == 2, calls          # TTL 过期 → 重算
        panel_data.build_state()
        assert calls["n"] == 2, calls          # TTL 内 → 缓存

        panel_data.build_state_invalidate()
        panel_data.build_state()
        assert calls["n"] == 3, calls          # invalidate → 强制重算
    finally:
        panel_data._build_state_uncached = real
    print("PASS  test_single_flight_and_cache")


def test_switch_scene_bypasses_cache():
    """切场景必须立刻换一份 —— 缓存按场景分桶，不能把 A 的 state 给 B。

    这条是**回归守门员**：2026-10-21 首版缓存只按时间，切到 B 后前端还拿到
    A 的导入/曲线/技能卡，test_scene_isolation 直接 9 项失败。
    """
    real_build = panel_data._build_state_uncached
    real_key = panel_data._scene_cache_key
    cur = {"scene": "A"}
    calls = {"n": 0}

    def fake_build():
        calls["n"] += 1
        return {"scene": {"id": cur["scene"]}}

    panel_data._build_state_uncached = fake_build
    panel_data._scene_cache_key = lambda: cur["scene"]
    try:
        panel_data.build_state_invalidate()
        a1 = panel_data.build_state()
        a2 = panel_data.build_state()
        assert a1 is a2 and calls["n"] == 1, f"同场景应命中缓存 calls={calls}"

        cur["scene"] = "B"                      # 人切了场景
        b = panel_data.build_state()
        assert b["scene"]["id"] == "B", f"切到 B 却拿到 {b['scene']['id']} 的数据"
        assert calls["n"] == 2, f"切场景必须重算 calls={calls}"

        cur["scene"] = "A"                      # 切回去（缓存只有一份，也要重算）
        a3 = panel_data.build_state()
        assert a3["scene"]["id"] == "A", a3
        assert calls["n"] == 3, calls
    finally:
        panel_data._build_state_uncached = real_build
        panel_data._scene_cache_key = real_key
    print("PASS  test_switch_scene_bypasses_cache")


if __name__ == "__main__":
    test_single_flight_and_cache()
    test_switch_scene_bypasses_cache()
    print("\nALL PASS")
