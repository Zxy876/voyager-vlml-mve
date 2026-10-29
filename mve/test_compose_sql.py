#!/usr/bin/env python3
"""消费侧组合层（_compose_sql）单测 —— 原版 Voyager 同构契约（2026-10-29）。

原版证据：`voyager.py:226` `env.step(code, programs=self.skill_manager.programs)`
—— agent 写的那段**程序**在技能定义同一作用域里执行，组合写在程序里
（`skill_a() + skill_b()`）；原版全文**没有**「把多条技能结果相加」的合并器。

MVE 的等价物 = 一条组合 SQL。本测试钉住四条契约：
  ① 只产出**一条** SQL（不是 items 列表）；
  ② 材料不够时输出空 SQL + note（如实不答，不拼凑）；
  ③ 提示词里带「同一笔钱不许在一条 SQL 里被算两次」（双计是本契约要杜绝的错）；
  ④ 上一轮拒因（feedback）会被拼进本轮 user 消息（原版 step 迭代同构）。
运行：python mve/test_compose_sql.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(HERE), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

_FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        _FAILS.append(name)


def main() -> int:
    import voyager_vlml.consume as C
    from voyager_vlml.consume import _compose_sql

    skills = [{"topic": f"learn_x::{i}", "name": f"skill_{i}",
               "text": f"材料{i}", "code": f"SELECT SUM(c{i}) FROM t{i}"}
              for i in range(3)]
    seen: list[list[dict]] = []

    def _stub(_msgs, **kw):
        seen.append(_msgs)
        return {"sql": "SELECT SUM(base_salary) + SUM(amount) FROM employees",
                "used": ["skill_0", "skill_1"],
                "note": "基薪 + 津贴，一条算完"}

    _orig = C.chat_json
    C.chat_json = _stub

    print("== ① 只产出一条 SQL ==")
    r = _compose_sql("九月份一共要支付多少钱", skills, "employees(base_salary) allowance(amount)")
    check("返回 dict 带 sql", isinstance(r, dict) and bool(r.get("sql")), f"keys={list(r)}")
    check("**没有** items 列表（不再是多条）", "items" not in r)
    check("used 透传", r.get("used") == ["skill_0", "skill_1"], f"{r.get('used')}")
    check("note 透传", r.get("note") == "基薪 + 津贴，一条算完")

    print("== ② 材料不够 → 空 SQL + 说明（不拼凑）==")
    C.chat_json = lambda *a, **k: {"sql": "", "used": [],
                                   "note": "库里没有加班费率，加班费算不出来"}
    r2 = _compose_sql("技术部九月份加班费", skills, "employees(base_salary)")
    check("空 SQL 不报错、原样透传", r2.get("sql") == "" and "费率" in r2.get("note", ""))

    print("== ③ 提示词钉住「不许算两次」==")
    C.chat_json = _stub
    _compose_sql("q", skills, "t(x)")
    _sys = str(seen[-1][0].get("content") or "")
    check("system 带双计禁令", "两次" in _sys and "包含" in _sys)
    check("system 声明只输出一条 SELECT", "一条" in _sys and "sql" in _sys)
    _user = str(seen[-1][1].get("content") or "")
    check("材料以「方法材料」给，不是「待相加的项」", "材料" in _user)

    print("== ④ feedback（原版 critique）拼进本轮 ==")
    _compose_sql("q", skills, "t(x)", feedback="- 上一轮：binder error: no such column")
    _u2 = str(seen[-1][1].get("content") or "")
    check("拒因进入 user 消息", "binder error" in _u2)
    check("并说明是「上一轮没成」", "上一轮" in _u2)

    C.chat_json = _orig
    if _FAILS:
        print(f"✗ 失败 {len(_FAILS)} 项：{_FAILS}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
