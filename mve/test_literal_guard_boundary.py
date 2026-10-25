# -*- coding: utf-8 -*-
"""字面量守卫「比较边界日期」放行的回归测试（2026-10-26）。

背景：total_base_salary 配方 `hire_date <= :cutoff_date` 回填成
'2026-09-30' 后被 _sql_literal_guard 当幻觉拦截 —— 范围谓词的右值是
**查询边界**，语义上允许不在数据取值域内（库里存的是员工入职日，
月末 cutoff 不在任何一行里）。误拦会把核心配方整条废掉，总支出答案
缺了 base 大头。

规则：日期格式字面量若**只**出现在比较谓词（< <= > >= BETWEEN AND）
右侧 → 放行；等值过滤（= / IN）的日期仍必须指认（那才是「假设了一
个库里没有的取值」）。
"""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mve"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_OBS = ("employees hire_date 真实取值示例: 2023-05-12, 2022-01-10; "
        "month: 2026-09; kind: 餐补, 交通")


def _guard(sql: str) -> str:
    mod = types.ModuleType("curriculum_auto")
    mod.db_observation = lambda p: _OBS
    sys.modules["curriculum_auto"] = mod
    from voyager_vlml.consume import _sql_literal_guard
    return _sql_literal_guard(sql, "/tmp/fake.duckdb")


class TestLiteralGuardBoundaryDate(unittest.TestCase):

    def test_boundary_date_le_pass(self):
        # hire_date <= '2026-09-30'：月末 cutoff 不在入职日取值里 → 放行
        self.assertEqual(_guard(
            "SELECT SUM(e.base_salary) FROM employees e "
            "WHERE e.hire_date <= '2026-09-30' AND e.active = True"), "")

    def test_between_both_bounds_pass(self):
        # BETWEEN 双边界都不在取值域 → 放行（右边界前面是 AND）
        self.assertEqual(_guard(
            "SELECT * FROM t WHERE hire_date BETWEEN '2022-01-01' "
            "AND '2026-09-30'"), "")

    def test_real_month_eq_pass(self):
        # 等值但库里真有 → 放行（本来就该过）
        self.assertEqual(_guard(
            "SELECT SUM(amount) FROM allowance WHERE month = '2026-09'"), "")

    def test_fake_month_eq_reject(self):
        # 等值过滤幻觉月 → 仍拒
        self.assertIn("2026-10", _guard(
            "SELECT SUM(amount) FROM allowance WHERE month = '2026-10'"))

    def test_fake_enum_with_boundary_mixed_reject(self):
        # 混写：边界日期合法，但幻觉枚举 'food' 照样拦
        self.assertIn("food", _guard(
            "SELECT SUM(amount) FROM allowance "
            "WHERE kind = 'food' AND d < '2026-09-30'"))

    def test_fake_date_eq_reject(self):
        # 幻觉日期用于等值 → 拒（等值不享边界豁免）
        self.assertIn("2026-09-30", _guard(
            "SELECT * FROM t WHERE hire_date = '2026-09-30'"))

    def test_and_prefix_without_between_reject(self):
        # SQL 里没有 BETWEEN 时，AND 前缀不享豁免（防普通等值组合绕过）
        self.assertIn("2026-09-30", _guard(
            "SELECT * FROM t WHERE x = '1' AND y = '2026-09-30'"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
