#!/usr/bin/env python3
"""失败账进拆解观察面的测试（原版 `Failed tasks that are too hard:` 同构）。

跑法：python mve/test_failed_obs.py（服务器无 pytest，同 test_*.py 口径）。

原版依据（2026-10-31 逐行核对）：
  · `voyager.py:inference()` 里 `decompose_task(task)` 排在
    `completed_tasks = []` / `failed_tasks = []` **之前** → 拆子目标时看得到
    上一阶段的两本账；
  · `decompose_task` 的 messages = [system, `render_human_message(events)` , task]，
    而 `render_observation` 里带 `Completed tasks so far:` / `Failed tasks that
    are too hard:`，两项 `warm_up` 都是 0 → `should_include = True`，**必带不随机**；
  · `clean_up_tasks`：后来做成的任务从 failed 里删掉。
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


Q = "README 和仓库内容是否属实？不属实要怎么改"
DIMS = [
    {"id": "d1", "question": "README 里的许可证是否与仓库实际使用的一致？"},
    {"id": "d2", "question": "README 里提到的文档是否存在于仓库中？"},
    {"id": "d3", "question": "面板链接是否有效？"},
]


def main() -> int:
    import maieutic_scaffold as ms

    tmp = Path(tempfile.mkdtemp())
    old = ms.CKPT
    ms.CKPT = tmp
    try:
        p = ms._cache_path("_T", Q)
        ms._atomic_write(p, {"question": Q, "milestone": {"name": "真实性"},
                             "dimensions": DIMS, "progress": {}})

        print("== 1. 没有失败账 → 材料里不出现失败段 ==")
        _m = ms.render_material(Q, "_T")
        check("首次拆解不带 Failed tasks 段", "Failed tasks that are too hard" not in _m)
        check("failed_dims 返回空", ms.failed_dims("_T", Q) == [])

        print("== 2. 试过没做成 → 问句 + 判词都进观察面 ==")
        ms.save_progress_iterations("_T", Q, {
            "d1": {"rounds": 2, "asked": "许可证那一项没取到 LICENSE 文件", "passed": False},
            "d2": {"rounds": 1, "asked": "", "gap": "仓库里没有 docs/ 目录", "passed": False},
        })
        _fd = ms.failed_dims("_T", Q)
        check("两条失败的都收进来", len(_fd) == 2, str([x[0][:12] for x in _fd]))
        check("判词从 asked 取", any("LICENSE" in w for _, w in _fd))
        check("asked 空时退回 gap", any("docs/" in w for _, w in _fd))
        _m2 = ms.render_material(Q, "_T")
        check("材料里有原版那行标题",
              "Failed tasks that are too hard" in _m2)
        check("材料里有「为什么失败」（原版只给任务名，MVE 多给判词）",
              "为什么失败" in _m2)
        check("写明「别原样重出」（原版 curriculum 规则 5 同口径）",
              "不要**原样或换个说法重出" in _m2 or "别原样重出" in _m2)

        print("== 3. clean_up_tasks：做成的从 failed 里删掉 ==")
        ms.save_progress("_T", Q, ["d1"])          # d1 后来闭合了
        _fd2 = ms.failed_dims("_T", Q)
        check("已闭合的不再算「太硬」", len(_fd2) == 1, str([x[0][:12] for x in _fd2]))
        check("留下的正是没做成的那条", _fd2[0][0].startswith("README 里提到的文档"))

        print("== 4. 封顶（中文问句长，按条数封，不按字符砍）==")
        ms._atomic_write(p, {"question": Q, "dimensions": DIMS + [
            {"id": f"e{i}", "question": f"第 {i} 条没做成的维度？"} for i in range(20)],
            "progress": {}})
        ms.save_progress_iterations("_T", Q, {
            f"e{i}": {"rounds": 1, "asked": f"原因 {i}", "passed": False}
            for i in range(20)})
        check(f"最多 {ms.FAILED_SHOWN} 条", len(ms.failed_dims("_T", Q)) == ms.FAILED_SHOWN,
              str(len(ms.failed_dims("_T", Q))))
        ms.save_progress("_T", Q, ["d3"])          # 留一条 completed 给下一节用

        print("== 5. 真的接进了拆骨架的 prompt（不只是 render_material 自己好看）==")
        _seen: dict = {}

        def _fake(messages):
            _seen["user"] = messages[1]["content"]
            return ('{"milestone": {"name": "n", "why": "w"}, "dimensions": '
                    '[{"id": "x", "question": "q", "why": "w"}], "expected_drifts": []}')

        ms.scaffold(Q, "_T", llm=_fake, use_cache=False)
        check("拆解 prompt 的 user 侧带着失败账",
              "Failed tasks that are too hard" in _seen.get("user", ""))
        check("拆解 prompt 的 user 侧也有 completed 侧（原版两本账一起）",
              "Completed tasks so far" in _seen.get("user", ""))

        print("== 6. 场景级：上一道题失败的，下一道题拆解时要看得见 ==")
        # 原版两本账挂在 CurriculumAgent 上（一份 completed_tasks.json +
        # failed_tasks.json），跨任务共享；MVE 的账存在 per-(scene,question)
        # 文件里，只读本文件 = 换题就断片（2026-10-31 发现并修）。
        Q2 = "面板是否已上线并可供访问？"
        p2 = ms._cache_path("_T", Q2)
        ms._atomic_write(p2, {
            "question": Q2,
            "dimensions": [{"id": "old1", "question": "上一道题里没做成的那条？"},
                           {"id": "old2", "question": "上一道题里已经做成的那条？"}],
            "progress": {"learned": ["old2"],
                         "iterations": [{"id": "old1", "rounds": 2,
                                         "asked": "上题：仓库没有 releases",
                                         "passed": False}]}})
        _fd3 = ms.failed_dims("_T", Q2)          # 注意：问的是新题 Q2
        check("新题拆解时看得见旧题的失败账",
              any("上一道题里没做成" in q for q, _ in _fd3),
              str([q[:14] for q, _ in _fd3]))
        check("旧题的判词一起带过来",
              any("没有 releases" in w for _, w in _fd3))
        _cd = ms.completed_dims("_T", Q2)
        check("completed 侧也同样跨题", any("已经做成" in q for q in _cd),
              str([q[:14] for q in _cd]))
        _m3 = ms.render_material(Q2, "_T")
        check("两本账都出现在新题的材料里",
              "Failed tasks that are too hard" in _m3
              and "Completed tasks so far" in _m3)
        check("旧题里已做成的那条**不**出现在 failed 段（clean_up_tasks）",
              "上一道题里已经做成" not in _m3.split("Failed tasks that are too hard")[-1])
    finally:
        ms.CKPT = old
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
