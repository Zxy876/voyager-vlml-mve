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

    print("== ⑤ 口径二义披露（2026-10-29 用户拍板「答案披露双口径」）==")
    C.chat_json = lambda *a, **k: {
        "sql": "SELECT SUM(month_pay) FROM up_工资明细 WHERE month='2026-09'",
        "used": ["skill_0"], "note": "按当期明细合计",
        "alt_sql": "SELECT SUM(base_salary) FROM employees WHERE active",
        "alt_note": "只在职，不含已离职的"}
    r5 = _compose_sql("九月份公司一共要支付多少钱", skills, "t(x)")
    check("alt_sql 透传", r5.get("alt_sql", "").startswith("SELECT SUM(base_salary)"))
    check("alt_note 透传", "在职" in r5.get("alt_note", ""))
    check("主 SQL 不受影响（口径披露不改主口径）",
          "month_pay" in r5.get("sql", ""))
    _sys5 = str(seen[-1][0].get("content") or "")
    check("system 要求「口径二义要披露，不许悄悄挑一个」",
          "二义" in _sys5 and "alt_sql" in _sys5)
    check("system 同时钉住「无二义别乱给」", "别为保险乱给" in _sys5)

    C.chat_json = lambda *a, **k: {"sql": "SELECT 1", "used": [], "note": "",
                                   "alt_sql": "", "alt_note": ""}
    r6 = _compose_sql("q", skills, "t(x)")
    check("无二义 → alt 为空（不产生废话披露）",
          r6.get("alt_sql") == "" and r6.get("alt_note") == "")

    print("== ⑥ 备选值取数：bool 不许当金额 ==")
    from voyager_vlml.consume import _first_number
    check("状态列（bool）被跳过，取到真金额",
          _first_number({"columns": ["active", "total"],
                         "rows": [[True, 137100.0]]}) == 137100.0)
    check("字符串数字（带千分位）也认",
          _first_number({"rows": [["1,234.5"]]}) == 1234.5)
    check("没有数值 → None（不编）",
          _first_number({"rows": [["技术"]]}) is None)

    C.chat_json = _orig
    if _FAILS:
        print(f"✗ 失败 {len(_FAILS)} 项：{_FAILS}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
