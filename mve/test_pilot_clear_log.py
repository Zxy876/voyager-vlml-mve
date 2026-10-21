#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""驾驶舱「清空日志」在**运行中**必须拒绝（2026-10-21 用户实测「日志怎么没了」）。

事故：循环跑着的时候点了「清空日志」→ `LOG.unlink()`，但 pilot 与
scene_learn 子进程全程握着 pilot.log 的 fd 在写 —— unlink 之后它们继续往
**已删除的 inode** 里写，磁盘上文件再也不会出现，面板「暂无日志」一直挂到
本轮跑完（/proc/<pid>/fd 全是 `(deleted)`），整轮日志等于全丢。

契约：
  1. running / child_pid 在 → 拒绝清屏并说明原因（ok=False + why）；
  2. 已停止 → 正常清空；
  3. 状态文件读不出来（损坏/缺失）→ 按保守处理？——不：读不出来多半是
     没跑过，此时清屏无害，放行。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pilot  # noqa: E402


def test_clear_log_refuses_while_running():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pilot.STATE = root / "pilot_state.json"
        pilot.LOG = root / "pilot.log"
        pilot.LOG.write_text("===== 第 1 题 =====\n", encoding="utf-8")

        # ① 运行中 → 拒绝，文件原样保留
        pilot.STATE.write_text(json.dumps({"running": True, "child_pid": 123}),
                               encoding="utf-8")
        r = pilot.clear_log()
        assert r.get("ok") is False and "停止" in r.get("why", ""), r
        assert pilot.LOG.exists() and "第 1 题" in pilot.LOG.read_text(encoding="utf-8")

        # ② 只有 child_pid 也算在跑（状态写一半的窗口）
        pilot.STATE.write_text(json.dumps({"running": False, "child_pid": 456}),
                               encoding="utf-8")
        r = pilot.clear_log()
        assert r.get("ok") is False, r
        assert pilot.LOG.exists()

        # ③ 已停止 → 正常清空
        pilot.STATE.write_text(json.dumps({"running": False, "child_pid": None}),
                               encoding="utf-8")
        r = pilot.clear_log()
        assert r.get("ok") is True, r
        assert not pilot.LOG.exists()

        # ④ 状态文件缺失（从没跑过）→ 清屏无害，放行
        r = pilot.clear_log()
        assert r.get("ok") is True, r
    print("PASS  test_clear_log_refuses_while_running")


if __name__ == "__main__":
    test_clear_log_refuses_while_running()
    print("\nALL PASS")
