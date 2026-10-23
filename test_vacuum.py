"""真空检测器边界测试（critic.is_vacuous / has_real_data / check_task_success）。

复现 2026-10-14 实测坑：空表纯聚合返回 1 行 (NULL, 0)，
行数判据 rows=1 会被骗过 → success=True → 坏配方固化。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from voyager_vlml.agents.critic import (
    VLMLCriticAgent, has_real_data, is_vacuous,
)

FAIL = []


def check(name: str, got, want):
    ok = got == want
    print(f"  {'✓' if ok else '✗'} {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


print("== is_vacuous（数据级） ==")
# 空表纯聚合的经典形态：AVG→NULL, COUNT→0
check("(NULL, 0) 真空", is_vacuous([[None, 0]]), True)
check("(100.0, 7) 真数", is_vacuous([[100.0, 7]]), False)
check("(False,) 布尔非空", is_vacuous([[False]]), False)
check("(0,) 只有 0", is_vacuous([[0]]), True)
check("(0, 5) 有非零", is_vacuous([[0, 5]]), False)
check("空列表", is_vacuous([]), True)
check("None", is_vacuous(None), True)
check("('',) 空串", is_vacuous([[""]]), True)
check("dict 行 {summary}", is_vacuous([{"summary": "G2 胜率 100%"}]),
      False)
check("dict 行 {summary 空}", is_vacuous([{"summary": ""}]), True)
check("report 形态 {rows:[...]}", is_vacuous({"rows": [[None, 0]]}), True)
check("report 形态 {rows:[真数]}", is_vacuous({"rows": [[3, 2]]}), False)
check("report 无行状字段", is_vacuous({"summary": "ok"}), False)

print("== has_real_data（观察级） ==")
check("ok + (NULL,0) → False",
      has_real_data({"ok": True, "rows": 1, "data": [[None, 0]]}), False)
check("ok + (100,7) → True",
      has_real_data({"ok": True, "rows": 1, "data": [[100.0, 7]]}), True)
check("ok + 0 行 → False",
      has_real_data({"ok": True, "rows": 0, "data": []}), False)
check("not ok + 有数 → False",
      has_real_data({"ok": False, "rows": 3, "data": [[1]]}), False)
check("ok 但 data 键缺失 → False",
      has_real_data({"ok": True, "rows": 1}), False)
check("ok + report dict → True",
      has_real_data({"ok": True, "rows": 1,
                     "data": {"summary": "有内容"}}), True)
check("ok + rows 键在 data 外层（query_sql 形态）",
      has_real_data({"ok": True, "rows": 1, "data": {"rows": [[7]]}}), True)

print("== check_task_success（判据级） ==")
# 只测第一级（真空闸门）：关掉第二级 LLM 口径检验，否则这几条会真去打接口。
c = VLMLCriticAgent(use_llm=False)
# 坏技能现场：agg 表 0 行，纯聚合骗回 1 行 (NULL, 0)
ok, critique = c.check_task_success(
    observations=[{"ok": True, "rows": 1, "data": [[None, 0]],
                   "tool": "query_sql", "error": ""}],
    task={"task": "算 G2 Split 首血后胜率"})
check("真空 1 行 → success=False", ok, False)
check("critique 提示真空", "没有任何真实值" in critique, True)

# 真取到数
ok, _ = c.check_task_success(
    observations=[{"ok": True, "rows": 1, "data": [[100.0, 7]],
                   "tool": "query_sql", "error": ""}],
    task={})
check("真数 1 行 → success=True", ok, True)

# 多路调用：一路真空一路真实 → 仍算成功（至少一次真实）
ok, _ = c.check_task_success(
    observations=[
        {"ok": True, "rows": 1, "data": [[None, 0]],
         "tool": "query_sql", "error": ""},
        {"ok": True, "rows": 1, "data": [[100.0, 7]],
         "tool": "query_sql", "error": ""},
    ], task={})
check("真空+真实混合 → success=True", ok, True)

# 全失败 → False
ok, _ = c.check_task_success(
    observations=[{"ok": False, "rows": 0, "error": "no such table"}],
    task={})
check("全失败 → success=False", ok, False)

print()
if FAIL:
    print(f"失败 {len(FAIL)} 项：{FAIL}")
    sys.exit(1)
print("全部通过")
