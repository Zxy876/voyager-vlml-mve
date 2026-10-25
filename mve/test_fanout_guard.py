#!/usr/bin/env python3
"""消费重放 JOIN 乘爆根因修复 —— 确定性 fan-out 闸门单测。

覆盖三件事（2026-10-25 用户定「根因修掉」）：
  1. vlml_mcp.check_sql_grain：跨一对多 JOIN 把驱动表行放大 → 实测 ratio>1；
     正确写法（预聚合 / 标量子查询）ratio==1 不误杀。
  2. vlml_mcp._has_aggregation：只认会塌缩的聚合（SUM/AVG/MIN/MAX/COUNT(col)），
     COUNT(*) 与纯明细查询不算。
  3. consume._fanout_guard：聚合 + 行放大 → 返回拒用原因；纯明细放大、正确聚合
     → 返回 ''（放行）。

不联网、不碰真库：用一份临时 duckdb（employees 1:N allowance，同构场景 B）。
运行：python mve/test_fanout_guard.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(HERE), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

os_mod = __import__("os")
os_mod.environ.setdefault("MVE_DB", ":memory:")

_FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f" —— {detail}" if detail and not ok else ""))
    if not ok:
        _FAILS.append(name)


def _make_db(path: str) -> None:
    import duckdb
    con = duckdb.connect(path, read_only=False)
    con.execute(
        "CREATE TABLE employees (emp_id VARCHAR, dept VARCHAR, base_salary DOUBLE)")
    con.execute(
        "CREATE TABLE allowance (emp_id VARCHAR, amount DOUBLE, month VARCHAR)")
    con.execute(
        "INSERT INTO employees VALUES ('E01','技术',1000),('E02','技术',2000),"
        "('E03','技术',1500),('E04','财务',3000)")
    # E02 在 2026-09 有 2 条津贴 → 跨 JOIN 会把 base_salary 算两次（乘爆）
    con.execute(
        "INSERT INTO allowance VALUES "
        "('E02',100,'2026-09'),('E02',200,'2026-09'),('E04',50,'2026-09')")
    con.close()


def main() -> int:
    print("== ① check_sql_grain：fan-out 实查 ==")
    import voyager_vlml.env.vlml_mcp as m

    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "payroll.duckdb")
        _make_db(db)

        # 乘爆 SQL（75120 事故同构）：SUM(e.base_salary + a.amount) 跨一对多 JOIN
        fan_sql = ("SELECT SUM(e.base_salary + a.amount) FROM employees e "
                   "JOIN allowance a ON a.emp_id = e.emp_id "
                   "WHERE e.dept='技术' AND a.month='2026-09'")
        g = m.check_sql_grain(fan_sql, db)
        check("乘爆 SQL 被实测出 fan-out（ratio>1）", bool(g) and g["rows"] > g["distinct"],
              str(g))

        # 正确写法 A：右表先按关联键聚合成单行再 JOIN
        pre_sql = ("SELECT SUM(e.base_salary + COALESCE(t.a,0)) FROM employees e "
                   "LEFT JOIN (SELECT emp_id, SUM(amount) AS a FROM allowance "
                   "WHERE month='2026-09' GROUP BY emp_id) t ON t.emp_id=e.emp_id "
                   "WHERE e.dept='技术'")
        g2 = m.check_sql_grain(pre_sql, db)
        check("预聚合写法不误杀（ratio==1 或 None）",
              not g2 or g2["rows"] == g2["distinct"], str(g2))

        # 正确写法 B：标量子查询（外层无 JOIN）
        sub_sql = ("SELECT SUM(e.base_salary + (SELECT COALESCE(SUM(amount),0) "
                   "FROM allowance a WHERE a.emp_id=e.emp_id AND a.month='2026-09')) "
                   "FROM employees e WHERE e.dept='技术'")
        g3 = m.check_sql_grain(sub_sql, db)
        check("标量子查询写法不误杀（ratio==1 或 None）",
              not g3 or g3["rows"] == g3["distinct"], str(g3))

        # 纯明细查询跨 JOIN 放大（合法）→ 不应误判为乘爆
        detail_sql = ("SELECT e.emp_id, a.amount FROM employees e "
                      "JOIN allowance a ON a.emp_id=e.emp_id "
                      "WHERE e.dept='技术' AND a.month='2026-09'")
        g4 = m.check_sql_grain(detail_sql, db)
        check("纯明细查询放大也如实报出 n>d（闸门靠聚合位判定，不在此拦）",
              bool(g4) and g4["rows"] > g4["distinct"], str(g4))

    print("== ② _has_aggregation：只认会塌缩的聚合 ==")
    check("SUM(...) 算聚合", m._has_aggregation("SELECT SUM(x) FROM t"))
    check("AVG(...) 算聚合", m._has_aggregation("SELECT AVG(x) FROM t"))
    check("COUNT(col) 算聚合", m._has_aggregation("SELECT COUNT(name) FROM t"))
    check("纯明细 SELECT 不算聚合",
          not m._has_aggregation("SELECT e.id, a.amt FROM e JOIN a ON e.id=a.id"))
    check("COUNT(*) 不算聚合（数行，跨 JOIN 有时本就数明细）",
          not m._has_aggregation("SELECT COUNT(*) FROM t JOIN u ON t.id=u.id"))

    print("== ③ _fanout_guard：消费侧确定性拒乘爆 ==")
    import voyager_vlml.consume as consume
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "payroll.duckdb")
        _make_db(db)

        w = consume._fanout_guard(fan_sql, db)
        check("乘爆 SQL（聚合+放大）被 fan-out 闸门拒用", bool(w), w[:80])

        w2 = consume._fanout_guard(pre_sql, db)
        check("预聚合正确写法放行", w2 == "", w2[:80])

        w3 = consume._fanout_guard(sub_sql, db)
        check("标量子查询正确写法放行", w3 == "", w3[:80])

        w4 = consume._fanout_guard(detail_sql, db)
        check("纯明细放大放行（非聚合，不误杀）", w4 == "", w4[:80])

        # 无 db_path → 如实放行（不假装测过）
        w5 = consume._fanout_guard(fan_sql, "")
        check("无 db_path 时如实放行（不误杀、不抛错）", w5 == "")

    print()
    if _FAILS:
        print(f"失败 {len(_FAILS)} 项：{_FAILS}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
