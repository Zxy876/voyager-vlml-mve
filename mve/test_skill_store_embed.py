#!/usr/bin/env python3
"""技能检索向量化单测（2026-10-26 对齐原版：embedding 相似度取代词面 Jaccard）。

原版同构证据：voyager/agents/skill.py:83-87（description 入向量库）、
:117-130（similarity_search_with_score 盲取 top_k）。本测试用**假向量**
（不联网）验证：
  ① 语义排序：同义不同词的问法（词面 Jaccard 排不上的）按余弦排到第一；
  ② 回退：embedding API 不可用 → 词面 Jaccard 路径仍返回结果；
  ③ 向量隔离：向量只落 skill_emb.json，技能条目与 retrieve() 返回值都不带；
  ④ delete_skill / reset 连带清向量。
运行：python mve/test_skill_store_embed.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import shutil
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


def _setup(monkey_vectors):
    """重定向 STORE/EMB_STORE 到临时目录，并注入假 _embed_api。"""
    import skill_store as S
    tmp = Path(tempfile.mkdtemp())
    S.STORE = tmp / "skill_store.json"
    S.EMB_STORE = tmp / "skill_emb.json"
    S._RAW_CACHE["sig"] = None
    S._RAW_CACHE["data"] = None
    S._EMB_CACHE["sig"] = None
    S._EMB_CACHE["data"] = None
    S._embed_api = monkey_vectors
    return S, tmp


def _fake_embed(texts):
    """假向量化：含 支出/工资/津贴 → [1,0]；请假/考勤 → [0,1]。"""
    out = []
    for t in texts:
        if any(w in t for w in ("支出", "工资", "津贴", "薪酬")):
            out.append([1.0, 0.0])
        else:
            out.append([0.0, 1.0])
    return out


def main() -> int:
    S, tmp = _setup(_fake_embed)
    try:
        # —— 造库：两条技能，词面上「总支出」与 base_salary 几乎无重叠 ——
        # （文本必须 ≥ MIN_LESSON_LEN=12，否则被判无用经验不入库）
        S.add("learn_aaaa", "total_salary",
              "九月份公司总支出的计算方法（基本工资加津贴合计）",
              code="SELECT SUM(base_salary) AS t FROM employees",
              source_scene="B", exact_only=True)
        S.add("learn_aaaa", "leave_days",
              "员工请假天数统计方法（按月汇总考勤）",
              code="SELECT SUM(leave_days) AS d FROM attendance",
              source_scene="B", exact_only=True)

        print("== ① 语义排序（同义不同词）==")
        hits = S.retrieve("learn_aaaa", "九月份公司的总支出是多少",
                          scene_id="B")
        check("检回 2 条", len(hits) == 2, f"n={len(hits)}")
        check("总支出配方排第一（词面 Jaccard 排不上）",
              bool(hits) and hits[0].get("name") == "total_salary",
              f"top1={hits[0].get('name') if hits else '—'}")
        check("返回项不带向量", all("emb" not in h for h in hits))

        print("== ② 向量隔离：落 skill_emb.json，不进技能条目 ==")
        raw = json.loads(S.STORE.read_text(encoding="utf-8"))
        check("技能条目无 emb 字段",
              all("emb" not in v for v in raw["skills"].values()))
        check("EMB_STORE 有向量", S.EMB_STORE.exists()
              and len(json.loads(S.EMB_STORE.read_text())) == 2)

        print("== ③ 回退：API 不可用 → 词面 Jaccard 仍出结果 ==")
        S._embed_api = lambda texts: None
        S._EMB_CACHE["data"] = None  # 清缓存，强制走回退
        hits2 = S.retrieve("learn_aaaa", "员工请假天数统计", scene_id="B")
        check("回退路径仍检回", len(hits2) >= 1, f"n={len(hits2)}")
        check("回退路径 top1=请假配方",
              bool(hits2) and hits2[0].get("name") == "leave_days",
              f"top1={hits2[0].get('name') if hits2 else '—'}")

        print("== ④ delete_skill 连带清向量 ==")
        S._embed_api = _fake_embed
        S.delete_skill("learn_aaaa::leave_days")
        embs = json.loads(S.EMB_STORE.read_text())
        check("向量被清", "learn_aaaa::leave_days" not in embs,
              f"keys={sorted(embs)}")

        print("== ⑤ reset 连带清向量文件 ==")
        S.reset()
        check("EMB_STORE 已删", not S.EMB_STORE.exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if _FAILS:
        print(f"✗ 失败 {len(_FAILS)} 项：{_FAILS}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
