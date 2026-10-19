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


def test_dedupe_exact_only() -> None:
    """去重只做归一化精确相等（原版口径）：换问法的新题不能被误杀。"""
    import curriculum_auto as C
    done = ["projects.json 中哪些项目的 2026 年 stars 数量比 2025 年增加了至少 20,000？"]
    # 减少 vs 增加：长句共享字多，但这是两道不同的题 —— 必须判"不重复"
    assert not C._too_similar(
        "projects.json 中哪个项目的 2026 年 stars 数量比 2025 年减少了？", done)
    assert C._too_similar(done[0], done)                     # 完全一样
    assert C._too_similar(done[0].rstrip("？"), done)         # 只差标点
    print("PASS  test_dedupe_exact_only")


def test_retry_raise_with_reason() -> None:
    """连续重复 → 走原版 raise，但消息里要带最后原因（否则线上无法定位）。"""
    import curriculum_auto as C
    a = C.CurriculumAgent("T", scene_dir="", llm=lambda m: "Task: q1\nContext: c")
    a.completed_tasks = ["q1"]
    try:
        a.propose_next_ai_task(max_retries=2)
        raise AssertionError("应 raise RuntimeError")
    except RuntimeError as e:
        assert "Max retries reached" in str(e)
        assert "重复" in str(e), str(e)
    print("PASS  test_retry_raise_with_reason")


def test_empty_env_no_invented_files() -> None:
    """空工作区 → 不出题，也不把场景路径写进环境描述（防模型脑补文件名）。

    2026-10-19 实测：Scene D 工作区是空的，可我上一版 biome 里报了绝对路径
    `/root/mve/scene_data/D` → 模型连出 3 道「计算 room_data.txt 房间面积」的题，
    全部 no_data。原版 biome 只说地形（plains），不报路径。
    """
    import curriculum_auto as C
    d = Path(tempfile.mkdtemp())
    # ckpt 放**另一个**目录：放在场景目录里会让"工作区为空"这个前提失效
    a = C.CurriculumAgent("T", scene_dir=str(d),
                          state_dir=Path(tempfile.mkdtemp()) / "ckpt",
                          llm=lambda m: "Task: 编一个\nContext: 瞎编")
    task, why = a.propose_next_task()
    assert task == "", task
    assert "没有任何文件" in why, why
    obs = a.render_observation()
    assert str(d) not in obs["biome"], obs["biome"]
    assert "没有任何文件" in obs["nearby_blocks"], obs["nearby_blocks"]
    print("PASS  test_empty_env_no_invented_files")


def test_local_off_hides_files() -> None:
    """本机能力关掉 → 环境里不列文件、并明说关了，也不再自动出题。

    用户 2026-10-19：「我把本机能力关了也没用」—— 开关以前只管执行层，
    课程层照样围着本机文件出题。原版语义：调不到的 primitive 不算环境。
    """
    import curriculum_auto as C
    d = Path(tempfile.mkdtemp())
    (d / "room_data.txt").write_text("1,2,3", encoding="utf-8")
    a = C.CurriculumAgent("T", scene_dir=str(d),
                          state_dir=Path(tempfile.mkdtemp()) / "ckpt",
                          llm=lambda m: "Task: x\nContext: y")
    a.local_enabled = lambda: False            # 模拟猫爪总开关/本机组关掉
    obs = a.render_observation()
    assert "room_data" not in obs["nearby_blocks"], obs["nearby_blocks"]
    assert "关掉" in obs["biome"], obs["biome"]
    assert a.propose_next_task()[0] == "", "本机关了不该再有可学环境"
    print("PASS  test_local_off_hides_files")


def test_import_question_not_done_by_written() -> None:
    """导入题即使消费层判了 written，也仍在**课程**待学队列里。

    课程账（原版 curriculum 两本账）与消费判读是两本账。实测：Scene D 导入
    「什么是阿斯伯格（孤独症）」→ 消费层用模型记忆写了段 RESULT 就判 written
    → 被踢出队列 → 队列空 → 自动课程围着空目录编题。这就是用户看到的
    「课程完全没按我导入的问题来」。
    """
    import json
    import scene_learn as S
    d = Path(tempfile.mkdtemp())
    p = d / "coach.jsonl"
    p.write_text(json.dumps({"root_scene": "T", "mode": "voyager_consume",
                             "question": "什么是阿斯伯格（孤独症）",
                             "judgment_result": "written"},
                            ensure_ascii=False) + "\n", encoding="utf-8")
    old = S.COACH_LOG
    S.COACH_LOG = p
    try:
        qs = [x["question"] for x in S.scene_questions("T")]
    finally:
        S.COACH_LOG = old
    assert "什么是阿斯伯格（孤独症）" in qs, qs
    print("PASS  test_import_question_not_done_by_written")


def test_recipe_requires_real_fetch() -> None:
    """纯答案不是配方：`RESULT = {"answer": …}` 不许进技能库（原版技能=调 primitive 的代码）。"""
    from voyager_vlml.consume import _looks_like_recipe as ok
    assert not ok('RESULT = {"answer": "阿斯伯格是一种广泛性发育障碍……"}')
    assert not ok('answer = "阿斯伯格是……"')
    assert ok('projects = read_json("projects.json")')
    assert ok('RESULT = web_search("阿斯伯格", max_results=3)')
    assert ok('SELECT count(*) FROM games')
    assert ok('r = mcp_ddg_search("x")')
    print("PASS  test_recipe_requires_real_fetch")


def test_needs_data() -> None:
    """题面分型：数据型 vs 知识型（决定要不要把"没取数"判死）。"""
    import task_critic as T
    assert T.needs_data("projects.json 中哪些项目的 2026 年 stars 比 2025 年多？")
    assert T.needs_data("库里有多少行记录？")
    assert T.needs_data("2026 年最热门的 AI 开源项目有哪些？")
    assert not T.needs_data("阿斯伯格孩子怎么养育？")
    assert not T.needs_data("如何理解孩子的情绪崩溃？")
    print("PASS  test_needs_data")


def test_knowledge_vs_data_judge() -> None:
    """知识型：没取数也交 LLM 判（原版就是 LLM 判）；数据型：没取数直接失败。"""
    import task_critic as T
    calls: list[int] = []

    def llm_ok(msgs):
        calls.append(1)
        return '{"reasoning":"答案切题且有依据","success":true,"critique":""}'

    # 知识型 + 有答案 + 没数据源 → 叫 LLM，判成功
    ok, crit = T.check_task_success(
        "阿斯伯格孩子怎么养育？", "知识型问题",
        {"execution": {"ok": False, "real_calls": 0, "calls": 1, "rows": 0},
         "answer": "结构化养育建议：明确规则、视觉化日程、情绪共情……"},
        llm=llm_ok)
    assert ok is True and len(calls) == 1, (ok, calls)

    # 数据型 + 有答案 + 没数据源 → 直接失败，不叫 LLM（真空防线保持）
    calls.clear()
    ok2, crit2 = T.check_task_success(
        "projects.json 里 star 增长最多的是哪个？", "数据型",
        {"execution": {"ok": False, "real_calls": 0, "calls": 1, "rows": 0},
         "answer": "Ollama（凭记忆）"},
        llm=llm_ok)
    assert ok2 is False and calls == [], (ok2, calls)
    assert "数据" in crit2
    print("PASS  test_knowledge_vs_data_judge")


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
    test_dedupe_exact_only()
    test_retry_raise_with_reason()
    test_empty_env_no_invented_files()
    test_local_off_hides_files()
    test_import_question_not_done_by_written()
    test_recipe_requires_real_fetch()
    test_needs_data()
    test_knowledge_vs_data_judge()
    test_critic_shortcut()
    test_critic_parse_retry()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
