#!/usr/bin/env python3
"""自动课程 + 任务完成判定的单测（照原版机制逐条验：两本账、Task: 解析、
5 次重试、critic 真空短路）。不联网、不连库，直接 `python mve/test_curriculum_critic.py`。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(REPO))


def test_parse_ai_message() -> None:
    import curriculum_auto as C
    task, ctx = C.CurriculumAgent.parse_ai_message(
        "好的，我想好了\nTask: projects.json 里 star 最多的项目是哪个？\n"
        "Context: 读 projects.json 的 projects 列表，按 stars_2026 排序取第一")
    assert task == "projects.json 里 star 最多的项目是哪个？", task
    assert "stars_2026" in ctx, ctx
    # 原版只认 Task: 行 —— 没有就返回空（上层据此重试/退出）
    t2, c2 = C.CurriculumAgent.parse_ai_message("我觉得应该学点别的")
    assert t2 == "" and c2 == ""
    print("PASS  test_parse_ai_message")


def test_two_books() -> None:
    """两本账 + clean_up_tasks（原版 curriculum.py:330-365）。"""
    import curriculum_auto as C
    d = Path(tempfile.mkdtemp())
    a = C.CurriculumAgent("T", scene_dir=str(d), state_dir=d / "ckpt")
    a.update_exploration_progress({"task": "q1", "success": True})
    a.update_exploration_progress({"task": "q2", "success": False, "critique": "没取到数"})
    assert a.completed_tasks == ["q1"], a.completed_tasks
    assert a.failed_tasks == ["q2"], a.failed_tasks
    assert a._failed_why.get("q2") == "没取到数"
    # 同一个任务后来成功了 → 从 failed 里删掉（原版 clean_up_tasks 语义）
    a.update_exploration_progress({"task": "q2", "success": True})
    assert a.failed_tasks == [], a.failed_tasks
    assert a.completed_tasks == ["q1", "q2"], a.completed_tasks
    # 去重（completed 不重复）
    a.update_exploration_progress({"task": "q1", "success": True})
    assert a.completed_tasks == ["q1", "q2"], a.completed_tasks
    # 落盘 + 重载（原版 resume 读回两本账）
    b = C.CurriculumAgent("T", scene_dir=str(d), state_dir=d / "ckpt")
    assert b.completed_tasks == ["q1", "q2"] and b.failed_tasks == []
    print("PASS  test_two_books")


def test_queue_priority_and_retry() -> None:
    """人导入优先；失败过的题不再出；LLM 出题解析失败会重试（原版 max_retries=5）。"""
    import curriculum_auto as C
    d = Path(tempfile.mkdtemp())
    (d / "projects.json").write_text('{"projects":[{"name":"a","stars_2026":3}]}',
                                     encoding="utf-8")
    calls: list[int] = []

    def fake_llm(msgs):
        calls.append(1)
        if len(calls) == 1:
            return "这题我不知道怎么出"          # 第一次：没有 Task: 行 → 触发重试
        return "Task: projects.json 里有几个项目？\nContext: 读 projects 列表数长度"

    a = C.CurriculumAgent("T", scene_dir=str(d), state_dir=d / "ckpt", llm=fake_llm)
    # 队列优先（人导入 = 原版 manual 模式）
    t, c = a.propose_next_task(queue=["人导入的问题"], max_retries=5)
    assert t == "人导入的问题", t
    # 队列里的题已失败 → 跳过它，走 LLM 出题（且第一次解析失败要重试）
    a.update_exploration_progress({"task": "人导入的问题", "success": False,
                                   "critique": "没数据"})
    t2, _ = a.propose_next_task(queue=["人导入的问题"], max_retries=5)
    assert t2 == "projects.json 里有几个项目？", t2
    assert len(calls) == 2, f"应重试一次（第一次没有 Task: 行），实际调用 {len(calls)} 次"
    print("PASS  test_queue_priority_and_retry")


def test_critic_shortcut() -> None:
    """critic 真空短路：没取到真数直接 False，不叫 LLM（原版 messages[1] is None）。"""
    import task_critic
    called: list[int] = []

    def fake_llm(msgs):
        called.append(1)
        return '{"reasoning":"x","success":true,"critique":""}'

    # 没取到数（real_calls=0）→ False，且 LLM 一次都不该被调
    ok, why = task_critic.check_task_success(
        "q", "ctx", {"execution": {"ok": False, "real_calls": 0,
                                   "calls": 1, "rows": 0}}, llm=fake_llm)
    assert ok is False and called == [], (ok, called)
    assert why, "失败必须带原因"

    # 真取到数 → 叫 LLM 判，按其 success 返回
    ok2, c2 = task_critic.check_task_success(
        "q", "ctx", {"execution": {"ok": True, "real_calls": 1,
                                   "calls": 1, "rows": 5, "sql": "读取 projects.json"}},
        llm=fake_llm)
    assert ok2 is True and called == [1], (ok2, called)
    print("PASS  test_critic_shortcut")


def test_critic_parse_retry() -> None:
    """critic 输出不是 JSON → 重试 5 次，5 次都坏 → (False, "")（原版 critic.py:112-135）。"""
    import task_critic
    n: list[int] = []

    def bad_llm(msgs):
        n.append(1)
        return "我觉得挺好的（不是 JSON）"

    ok, c = task_critic.check_task_success(
        "q", "ctx", {"execution": {"ok": True, "real_calls": 1, "calls": 1, "rows": 2}},
        llm=bad_llm, max_retries=5)
    assert ok is False and c == "", (ok, c)
    assert len(n) == 5, f"应重试到 5 次，实际 {len(n)}"
    print("PASS  test_critic_parse_retry")


def main() -> None:
    test_parse_ai_message()
    test_two_books()
    test_queue_priority_and_retry()
    test_critic_shortcut()
    test_critic_parse_retry()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
