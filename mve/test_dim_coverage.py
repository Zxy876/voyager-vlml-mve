#!/usr/bin/env python3
"""消费侧「维度覆盖守卫」单测（2026-10-25 用户定「做掉」）。

验证 _sql_tables / _answer_is_partial：
  - _sql_tables 正确抽取 FROM/JOIN 表名（去注释与字面量）。
  - _answer_is_partial：
      A. 55000 类（合成题只写单表 base）→ 片面=True；
      B. 组合多表（base+津贴+加班全 JOIN）→ 片面=False；
      C. 原子题（dims=1）→ 不拦；
      D. 同源单表题（两维都查员工表）→ 不拦（len(dim_tabs)<2）；
      E. 答案只触及维度源表外的表（hit=0）→ 不拦；
      F. 技能非 SQL（本机 python）→ dim_tabs 空 → 不拦。

不联网、不碰真库，纯字符串/结构判定。
运行：python mve/test_dim_coverage.py
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
    print(f"  {'✓' if ok else '✗'} {name}" + (f" —— {detail}" if detail and not ok else ""))
    if not ok:
        _FAILS.append(name)


def _skill(name: str, code: str) -> dict:
    return {"name": name, "key": "k_" + name, "code": code}


def main() -> int:
    from voyager_vlml.consume import _sql_tables, _answer_is_partial

    print("== ① _sql_tables 抽取 ==")
    t1 = _sql_tables("SELECT SUM(base_salary) FROM employees WHERE dept = :dept")
    check("单表 FROM", t1 == {"employees"}, str(t1))
    t2 = _sql_tables(
        "SELECT e.base_salary FROM employees e JOIN allowance a "
        "ON a.emp_id=e.emp_id WHERE e.dept=:dept")
    check("JOIN 双表", t2 == {"employees", "allowance"}, str(t2))
    t3 = _sql_tables(
        "SELECT x FROM employees -- FROM fake\n JOIN attendance ON 1=1")
    check("去注释后双表", t3 == {"employees", "attendance"}, str(t3))
    t4 = _sql_tables("")
    check("空串→空集", t4 == set(), str(t4))

    print("== ② 技能配方（同构场景 B 工资题 6 维）==")
    skills = [
        _skill("tech_dept_employees",
               "SELECT name FROM employees WHERE dept = :dept"),
        _skill("tech_dept_base_salary",
               "SELECT SUM(base_salary) AS total_base_salary "
               "FROM employees WHERE dept = :dept"),
        _skill("tech_dept_overtime_pay",
               "SELECT SUM(overtime_hours) * 50 AS total_overtime_salary "
               "FROM attendance WHERE emp_id IN "
               "(SELECT emp_id FROM employees WHERE dept = :dept) "
               "AND month = :month"),
        _skill("tech_dept_allowance",
               "SELECT SUM(CASE WHEN allowance.kind = '餐补' THEN amount ELSE 0 END)"
               " + SUM(CASE WHEN allowance.kind = '交通' THEN amount ELSE 0 END) "
               "AS total_allowance FROM allowance WHERE emp_id IN "
               "(SELECT emp_id FROM employees WHERE dept = :dept) "
               "AND month = :month"),
        _skill("tech_dept_leaves",
               "SELECT SUM(leave_days) AS total_leave_days FROM attendance "
               "JOIN employees ON attendance.emp_id = employees.emp_id "
               "WHERE employees.dept = :dept AND attendance.month = :month"),
        _skill("tech_dept_attendance",
               "SELECT e.name, a.overtime_hours, a.leave_days FROM employees e "
               "JOIN attendance a ON e.emp_id = a.emp_id WHERE e.dept = :dept"),
    ]

    print("== ③ A：55000 类——合成题只写单表 base ==")
    ans_a = ("SELECT SUM(base_salary) AS total_base_salary "
             "FROM employees WHERE dept = :dept")
    partial, why = _answer_is_partial(ans_a, skills, n_dims=6)
    check("A 片面判定=True", partial is True, why)
    check("A 含覆盖说明", "维度覆盖守卫" in why, why)

    print("== ④ B：组合多表（base+津贴+加班全涉及）==")
    ans_b = ("SELECT SUM(e.base_salary) + COALESCE((SELECT SUM(amount) "
             "FROM allowance a WHERE a.emp_id=e.emp_id AND a.month=:month),0) "
             "+ COALESCE((SELECT SUM(overtime_hours)*50 FROM attendance at "
             "WHERE at.emp_id=e.emp_id AND at.month=:month),0) "
             "FROM employees e WHERE e.dept = :dept")
    partial_b, why_b = _answer_is_partial(ans_b, skills, n_dims=6)
    check("B 片面判定=False（已组合）", partial_b is False, why_b)

    print("== ⑤ C：原子题 dims=1 ==")
    partial_c, why_c = _answer_is_partial(ans_a, skills, n_dims=1)
    check("C 不拦（原子题）", partial_c is False, why_c)

    print("== ⑥ D：同源单表题（两维都查员工表）==")
    skills_d = [
        _skill("emp_name", "SELECT name FROM employees WHERE dept = :dept"),
        _skill("emp_base", "SELECT SUM(base_salary) FROM employees "
               "WHERE dept = :dept"),
    ]
    ans_d = ("SELECT name, SUM(base_salary) AS b FROM employees "
             "WHERE dept = :dept GROUP BY name")
    partial_d, why_d = _answer_is_partial(ans_d, skills_d, n_dims=2)
    check("D 不拦（同源单表，len(dim_tabs)<2）", partial_d is False, why_d)

    print("== ⑦ E：答案只触及维度源表外的表（hit=0）==")
    ans_e = "SELECT COUNT(*) FROM some_other_table"
    partial_e, why_e = _answer_is_partial(ans_e, skills, n_dims=6)
    check("E 不拦（hit=0）", partial_e is False, why_e)

    print("== ⑧ F：技能非 SQL（本机 python 程序）==")
    skills_f = [_skill("py1", "def run():\n    return RESULT(1)")]
    ans_f = "def run():\n    return RESULT(42)"
    partial_f, why_f = _answer_is_partial(ans_f, skills_f, n_dims=3)
    check("F 不拦（dim_tabs 空）", partial_f is False, why_f)

    print()
    print("== ⑨ milestone_dimensions（导入式里程碑权威维度）==")
    try:
        import json as _json
        import tempfile as _tf
        import shutil as _sh
        import maieutic_scaffold as _ms
        # 自建临时骨架缓存（不依赖真实 ckpt —— 测试必须可在干净环境跑）：
        # 走 _cache_path 同一把钥匙写 3 维骨架，再读回。
        tmp = _tf.mkdtemp()
        _old_ckpt = _ms.CKPT
        _ms.CKPT = Path(tmp)
        try:
            _q = "九月份要给技术部员工发多少工资"
            _p = _ms._cache_path("B", _q)
            _p.parent.mkdir(parents=True, exist_ok=True)
            _p.write_text(_json.dumps({
                "question": _q,
                "dimensions": [{"id": "d1", "question": "筛选技术部"},
                               {"id": "d2", "question": "取基本工资"},
                               {"id": "d3", "question": "取津贴"}],
            }, ensure_ascii=False), encoding="utf-8")
            md = _ms.milestone_dimensions("B", _q)
            check("里程碑维度>=2（权威拆分）", len(md) >= 2, f"n={len(md)}")
        finally:
            _ms.CKPT = _old_ckpt
            _sh.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        check("里程碑维度读取", False, f"{type(e).__name__}: {e}")

    if _FAILS:
        print(f"✗ 失败 {len(_FAILS)} 项：{_FAILS}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
