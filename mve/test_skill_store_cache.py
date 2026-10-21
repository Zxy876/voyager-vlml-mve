#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skill_store 的**解析缓存**契约（2026-10-21 黑客松面板挂死取证后补）。

事故数据：黑客松实例 skill_store.json = 110MB（878 条 × 每条约 93KB，大头是
每条 4096 维 embedding 原样存成 JSON）。面板一个 build_state 里 all_skills()
被调 5 次 → 5 次全量 json.loads → build_state 13.7s、RSS 3.0G（VPS 共 3.6G）
→ 整机 swap → 接口永不返回。

契约：
  1. 文件没变 → 只解析一遍（后续调用命中缓存，同一对象）；
  2. `_save()` 之后缓存立刻指向写盘那一份（不回头再解析一遍大文件）；
  3. 文件被**外部**改动（学习循环/另一个进程写的）→ 指纹变了必须重读，
     不能抱着旧缓存不放（否则面板永远显示旧技能）。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_store  # noqa: E402


def _fresh_store(tmp: Path) -> Path:
    p = tmp / "skill_store.json"
    p.write_text(json.dumps({"version": 2, "skills": {}, "writes": 0,
                             "added": 0, "rewrites": 0, "skipped": 0}),
                 encoding="utf-8")
    skill_store.STORE = p
    skill_store._RAW_CACHE["sig"] = None
    skill_store._RAW_CACHE["data"] = None
    return p


def test_parse_once_per_file_change():
    with tempfile.TemporaryDirectory() as td:
        _fresh_store(Path(td))
        calls = {"n": 0}
        real = skill_store._load_raw_uncached

        def counted():
            calls["n"] += 1
            return real()

        skill_store._load_raw_uncached = counted
        try:
            a = skill_store._load_raw()
            b = skill_store._load_raw()
            c = skill_store._load_raw()
            assert calls["n"] == 1, f"文件没变却解析了 {calls['n']} 遍"
            assert a is b is c, "应返回同一个对象（避免重复构造 878 条技能）"

            # add() 走 _save：缓存直接换成写盘那一份，不该再解析一遍
            skill_store.add("t1", "n1", "第一条足够长的经验内容，用来测试缓存。",
                            source="test")
            assert calls["n"] == 1, f"_save 之后不该重新解析，实际 {calls['n']} 次"
            assert any(s.get("name") == "n1" for s in skill_store.all_skills()), \
                "add 之后 all_skills 必须立刻看到新条目（缓存没同步）"
            assert calls["n"] == 1, f"all_skills 又解析了一遍：{calls['n']}"

            # 外部改了文件（另一个进程/学习循环直接写）→ 指纹变了必须重读
            p = skill_store.STORE
            data = json.loads(p.read_text(encoding="utf-8"))
            data["skills"]["ext::x"] = {"name": "x", "topic": "ext", "text": "外部写入的一条经验",
                                        "created": 0, "updated": 0, "hits": 0, "ok": 0,
                                        "versions": 1, "history": []}
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.utime(p, (1, 1))          # 保证 mtime/size 变化可被识别
            got = skill_store.all_skills()
            assert calls["n"] == 2, f"外部改动后应重读一次，实际 {calls['n']}"
            assert any(s.get("name") == "x" for s in got), "外部写入的技能没被读到"
        finally:
            skill_store._load_raw_uncached = real
    print("PASS  test_parse_once_per_file_change")


def test_missing_store_does_not_cache_forever():
    """库文件不存在时不能把空库缓存成"永远空"（第一次学习写入前就是这状态）。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "nope.json"
        skill_store.STORE = p
        skill_store._RAW_CACHE.update({"sig": None, "data": None})
        assert skill_store.all_skills() == []
        skill_store.add("t", "first", "库文件不存在时写第一条经验也要能落盘。")
        assert p.exists(), "第一条经验没落盘"
        assert len(skill_store.all_skills()) == 1
    print("PASS  test_missing_store_does_not_cache_forever")


if __name__ == "__main__":
    test_parse_once_per_file_change()
    test_missing_store_does_not_cache_forever()
    print("\nALL PASS")
