#!/usr/bin/env python3
"""外部 task 闭环的离线自测（不烧 LLM、不连库）。

跑法：`python mve/test_task_intake.py`
全部 PASS 才说明「导入 → 记失败账 → 拆子目标 → 课程围着它 → 子目标全完成 → 重试成功」
这条链在**结构与记账**上成立（真取数与模型行为要上服务器接 rib 才测得到）。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import task_intake as T  # noqa: E402


def _tmp_ledger():
    """换一个临时账本，别污染真的 failed_tasks.json。"""
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                    encoding="utf-8")
    f.write("[]")
    f.close()
    T.TASKS_PATH = Path(f.name)


Q = "Cloud9 在 Corrode 上首血率多少"


def test_ask_fail_records_and_splits() -> None:
    _tmp_ledger()
    T._answer_once = lambda q, **kw: {                  # type: ignore[assignment]
        "ok": False, "trajectory": [], "rows": 0, "skills": [],
        "skipped": "技能库里没有与它相关的配方（还没学过）",
    }
    out = T.ask(Q, topic_id="vlml_first_blood")
    assert out["ok"] is False, out
    assert out["task_id"], out
    assert out["subtopics"], f"没拆出子目标：{out}"
    rows = T.list_tasks()
    assert len(rows) == 1 and rows[0]["status"] == "pending", rows
    assert rows[0]["tries"] == 1
    print(f"PASS  ask 失败 → 记账 + 拆课（{len(out['subtopics'])} 条子目标）")


def test_next_focus_and_curriculum() -> None:
    focus = T.next_focus()
    assert focus and focus["topic_id"], focus
    # 课程规划必须看得见它（否则"学什么由失败信号决定"就是空话）
    from voyager_vlml.agents.curriculum import VLMLCurriculumAgent
    nxt = VLMLCurriculumAgent().propose_next_task()
    assert nxt["reason"] == "external_task", nxt
    assert nxt["topic_id"] == focus["topic_id"], (nxt, focus)
    assert "人导入的任务" in nxt["context"], nxt["context"][:200]
    print(f"PASS  课程围着外部 task 选：{nxt['reason']} · {nxt['topic_id']}")


def test_subtopics_done_then_retry() -> None:
    row = T.list_tasks()[0]
    for tid in row["subtopics"]:
        T.mark_subtopic_done(tid)
    rows = T.list_tasks()
    assert rows[0]["ready_for_retry"] is True, rows[0]
    assert T.next_focus() is None, "子目标全学完就不该再围着它出课"

    T._answer_once = lambda q, **kw: {                  # type: ignore[assignment]
        "ok": True, "trajectory": ["query_sql"], "rows": 12, "sql": "SELECT 1",
        "skills": [{"name": "vlml_first_blood"}], "answer": "SELECT 1",
    }
    out = T.retry(row["task_id"])
    assert out["ok"] is True, out
    assert out["reused_skills"] == ["vlml_first_blood"], out
    rows = T.list_tasks()
    assert rows[0]["status"] == "solved", rows[0]
    assert rows[0]["tries"] == 2
    print(f"PASS  子目标全完成 → 重试成功（第 {rows[0]['tries']} 次），"
          f"复用技能 {out['reused_skills']}")


def test_vacuous_answer_is_not_success() -> None:
    _tmp_ledger()
    # 调了工具但没事实 / 有事实但没调工具，两种都不算成功（同学习循环的尺子）
    T._answer_once = lambda q, **kw: {                  # type: ignore[assignment]
        "trajectory": ["query_sql"], "facts": [],          # 旧 coach 通路结构
    }
    assert T.ask(Q)["ok"] is False
    T._answer_once = lambda q, **kw: {                  # type: ignore[assignment]
        "trajectory": [], "facts": [{"value": 1}],
    }
    assert T.ask(Q)["ok"] is False
    print("PASS  真空成功被挡住（只有轨迹没事实 / 只有事实没轨迹 都算失败）")


def test_no_datasource_learns_nothing() -> None:
    """数据源缺失时学习循环学不到东西 —— 这条是「必须带数据源」的机制证据。"""
    _tmp_ledger()
    T._answer_once = lambda q, **kw: {                  # type: ignore[assignment]
        "ok": False, "trajectory": [], "rows": 0, "skills": [],
        "skipped": "库里没有这张表",
    }
    out = T.ask(Q)
    assert out["ok"] is False and out["subtopics"], out
    # 每轮都取不到数 → 没有任何子目标会被 mark 完成 → ready_for_retry 永远不为真
    assert T.list_tasks()[0].get("ready_for_retry") in (None, False)
    print("PASS  无数据源：task 永远 pending（学不动，如实记账）")


def test_stuck_subtopic_is_skipped_not_blocking_retry() -> None:
    """学不动的子目标要被「跳过」，不能把整条 task 钉死。

    同构原版 `learn()`：任务失败 → `failed_tasks.append` + "Skipping to next task"
    （`curriculum.py:340-344`），学习继续；**原版从不等所有任务成功**。
    所以门槛只能是"没有还能学的子目标了"，不能是"所有子目标都 done"。
    """
    _tmp_ledger()
    T._answer_once = lambda q, **kw: {                  # type: ignore[assignment]
        "ok": False, "trajectory": [], "rows": 0, "skills": [],
        "skipped": "技能库里没有与它相关的配方（还没学过）",
    }
    out = T.ask(Q, topic_id="vlml_first_blood")
    subs = T.list_tasks()[0]["subtopics"]
    assert subs, out

    g = T._graph()
    try:
        for tid in subs:
            # 练 3 次仍没跑通 → stale（= 原版 failed_tasks 的等价物）
            for _ in range(3):
                g.mastery.update(tid, score=0.0, commit=False)
            T.note_subtopic_result(tid, False)          # 课程失败分支每轮都会调它

        row = T.list_tasks()[0]
        assert row["done_subtopics"] == [], row
        assert T.pending_subtopics(row) == [], row
        assert sorted(T.skipped_subtopics(row)) == sorted(subs), row
        assert row["ready_for_retry"] is True, f"学不动了就该交给人重试：{row}"
        assert T.next_focus() is None, "已放弃的不该再围着它出课"
        print(f"PASS  子目标全部 stale → 记进「太难」账（{len(subs)} 条）→ "
              f"不再出课、ready_for_retry=True")
    finally:
        # 清掉 stale，别污染后面的用例（commit=False：不写盘）
        g.mastery.unstale(commit=False)


def main() -> int:
    test_ask_fail_records_and_splits()
    test_next_focus_and_curriculum()
    test_subtopics_done_then_retry()
    test_vacuous_answer_is_not_success()
    test_no_datasource_learns_nothing()
    test_stuck_subtopic_is_skipped_not_blocking_retry()
    print("\n全部通过（离线部分）。真取数与复用要接 rib 跑："
          "ask → loop.py --rounds N → retry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
