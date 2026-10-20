#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""技能「有效率」不变量测试：ok ≤ hits。

背景（2026-10-20 面板截图取证）：
    技能库出现「命中 1 / 有效 3 / 有效率 300%」「命中 2 / 有效 3 / 150%」。
    `ok` 与 `hits` 是**两条独立记账**：
      · hits +1 只在 mark_used（检索把技能注入 prompt）时
      · ok   +1 在 mark_result（这一轮判成功）时，**不看它有没有被注入**
    面板（dashboard.py:1376）与检索打分（skill_store.py:296 的 0.6 权重）
    都把 `ok/hits` 当"有效率"，所以必须保证 ok ≤ hits，否则有效率能破 100%。

    另一条更隐蔽的来源在消费侧：`_absorb` 曾按 `f"{topic}::"` 前缀把同题下
    **所有**技能捞出来集体 mark_result(True) —— 一次成功消费，20 多条技能一起
    +1，而 hits 只涨被注入那条。修复是只记 used_keys（本次真用的那条）。

本测试钉死两件事：
    1. mark_result / boost_skill 之后，ok 永远不超过 hits（有效率 ≤ 100%）
    2. 没调用 mark_used（没注入）也能把 ok 记上，但 hits 同步补上 ——
       "用了但没被检索注入"（复用重放/面板手动标记）是真实存在的路径
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_store  # noqa: E402


class _TmpStore:
    """把技能库指到临时文件，绝不碰真库。"""

    def __enter__(self):
        self._old = skill_store.STORE
        self._dir = tempfile.mkdtemp(prefix="skill_eff_")
        skill_store.STORE = Path(self._dir) / "skill_store.json"
        skill_store.STORE.write_text(
            json.dumps({"version": 2, "skills": {}, "writes": 0}),
            encoding="utf-8")
        return skill_store.STORE

    def __exit__(self, *a):
        skill_store.STORE = self._old


def _add_one(topic="consume_t", name="dim1"):
    act, key = skill_store.add(
        topic, name, "闭合这个维度时跑通的取数做法：先取证再组织答案",
        code='RESULT = web_search(query="关键词")', source="learn",
        source_scene="D")
    assert act in ("added", "rewritten"), (act, key)
    return key


def _stats(key):
    s = skill_store._load_raw()["skills"][key]
    return int(s.get("hits") or 0), int(s.get("ok") or 0)


def test_mark_result_without_mark_used_keeps_ratio_le_100():
    with _TmpStore():
        key = _add_one()
        assert _stats(key) == (0, 0)
        skill_store.mark_result([key], True)      # 没注入却判成功
        hits, ok = _stats(key)
        assert (hits, ok) == (1, 1), (hits, ok)
        assert ok <= hits
    print("PASS  test_mark_result_without_mark_used_keeps_ratio_le_100")


def test_repeated_success_never_exceeds_100_percent():
    with _TmpStore():
        key = _add_one()
        for _ in range(3):
            skill_store.mark_result([key], True)
        hits, ok = _stats(key)
        assert ok <= hits, (hits, ok)
        assert round(100 * ok / hits) == 100, (hits, ok)
    print("PASS  test_repeated_success_never_exceeds_100_percent")


def test_boost_skill_repeatedly_never_exceeds_100_percent():
    """面板「标记为有效」按钮连点 —— 旧实现 hits==0 才补 1，第二次就 200%。"""
    with _TmpStore():
        key = _add_one()
        for _ in range(3):
            assert skill_store.boost_skill(key)
        hits, ok = _stats(key)
        assert ok <= hits, (hits, ok)
    print("PASS  test_boost_skill_repeatedly_never_exceeds_100_percent")


def test_normal_flow_gives_real_ratio_below_100():
    """正常路径（注入 3 次、成功 2 次）有效率是 67%，不被不变量掩盖。"""
    with _TmpStore():
        key = _add_one()
        skill_store.mark_used([key] * 3)
        skill_store.mark_result([key], True)
        skill_store.mark_result([key], True)
        hits, ok = _stats(key)
        assert (hits, ok) == (3, 2), (hits, ok)
        assert round(100 * ok / hits) == 67
    print("PASS  test_normal_flow_gives_real_ratio_below_100")


if __name__ == "__main__":
    test_mark_result_without_mark_used_keeps_ratio_le_100()
    test_repeated_success_never_exceeds_100_percent()
    test_boost_skill_repeatedly_never_exceeds_100_percent()
    test_normal_flow_gives_real_ratio_below_100()
    print("\nALL PASS")
