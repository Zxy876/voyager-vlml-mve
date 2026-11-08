#!/usr/bin/env python3
"""「本次复用」记账必须指向**真正用上的那条**技能 —— 回归测试。

跑法：python mve/test_reuse_attribution.py（本地可跑，不依赖 duckdb / LLM）。

## 背景（2026-11-09 服务器实测抓到的 bug）

消费侧组合层产出一条 SQL 后，要记账「本次复用了哪条已学技能」。
记账链路是：

    consume._settle()  → reused_skill
      → consume._absorb(used_keys=[reused_skill.key])
        → skill_store.mark_result([key], True)   ← ok +1

`ok` 计数是**衡量「学会没有」的量化指标**（此前总 hits=279 / ok=0；
会话落地后开始转正）。它一旦记到错的技能上，指标就既虚高又指错方向。

## 实测到的错配（服务器，场景 L）

问题：「在 web_docs 表中，query 包含 torvalds 的记录总共有多少行？」
实际 SQL：`SELECT COUNT(*) FROM web_docs WHERE query LIKE '%torvalds%'`
        —— **方法完全正确**，实体也换对了（真值 497，答 497 ✅）
记账到的：`learn_31f7ae77::tiktok_claimed_count`
        —— 一条**完全无关**的技能（它算的是 TikTok 列 = CLAIMED 的行数）

同时另外 3 条复用记录（web_facts 的两条 + 一条 web_docs 的）都记对了
→ 不是「全错」，是**兜底路径**在匹配失败时抓错了人。

## 根因（consume.py 的 `_settle`）

    _first = (dict(_e.get("skill") or {})
              or (dict(skills[0]) if skills else {}))   ← 兜底取检索第一名

`_e["skill"]` 是「模型在 used 里报的名字」对回材料条目，匹配用的是
**模糊子串**（name == _u or name in _u or _u in name）。模型这次报的名字
对不上任何材料 → `_e["skill"]` 为空 → **兜底取 `skills[0]`**。
而 `skills[0]` 只是**检索相关性第一**，跟本次实际复用了谁毫无关系
（问句里「记录总共有多少行」与「count」语义最近 → 排第一的是
tiktok_claimed_count）。

## 铁律

**「本次复用了谁」必须由「本次实际用上的方法」指认，不能由「检索排名」
代答。** 对不上就**不记账**（ok 不动），而不是随便抓一条。

不发明新的打分或门槛机制 —— 只把「记账对象」的来源从「检索排名」
改成「模型点名的技能，逐字对上」；对不上时宁可**如实不记**。
"""

import os
import sys

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


def main() -> int:
    from voyager_vlml.consume import _attribute_reused_skill

    # ---- 1. 模型点名且逐字对得上 → 精确取那条 ----
    print("== 1. 点名对得上 → 精确取那一条（不按检索排名） ==")
    skills = [
        {"key": "learn_31f7ae77::tiktok_claimed_count",
         "topic": "learn_31f7ae77", "name": "tiktok_claimed_count"},
        {"key": "learn_4621a8a9::github_claimed_count",
         "topic": "learn_4621a8a9", "name": "github_claimed_count"},
        {"key": "learn_4621a8a9::web_docs_2026_activity",
         "topic": "learn_4621a8a9", "name": "web_docs_2026_activity"},
    ]
    got = _attribute_reused_skill(
        skills=skills,
        used_names=["github_claimed_count"],
        pool=skills)
    check("点名 github_claimed_count → 取到它",
          str((got or {}).get("key")) == "learn_4621a8a9::github_claimed_count",
          str((got or {}).get("key")))
    check("没被检索第一名（tiktok_claimed_count）抢走",
          str((got or {}).get("name")) != "tiktok_claimed_count")

    # ---- 2. 模型点名的是**材料之外**的名字 → 不记账 ----
    print("== 2. 点名对不上任何材料 → 不记账（不抓检索第一名） ==")
    got2 = _attribute_reused_skill(
        skills=skills,
        used_names=["some_unrelated_recipe"],
        pool=skills)
    check("对不上 → 返回空（不代答）", not got2,
          f"得到 {str((got2 or {}).get('name'))!r}")
    check("明确不是 tiktok_claimed_count",
          str((got2 or {}).get("name")) != "tiktok_claimed_count")

    # ---- 3. 模型没点名（used 为空）→ 不记账 ----
    print("== 3. 模型没点名 → 不记账 ==")
    got3 = _attribute_reused_skill(
        skills=skills, used_names=[], pool=skills)
    check("空 used → 返回空", not got3)

    # ---- 4. 全都没有材料 → 不记账 ----
    print("== 4. 没有材料 → 不记账 ==")
    got4 = _attribute_reused_skill(skills=[], used_names=["x"], pool=[])
    check("无材料 → 返回空", not got4)

    # ---- 5. 模型报的是 key 全串（有的模型会报完整 key）→ 仍能对上 ----
    print("== 5. 模型报完整 key → 也能对上 ==")
    got5 = _attribute_reused_skill(
        skills=skills,
        used_names=["learn_4621a8a9::web_docs_2026_activity"],
        pool=skills)
    check("完整 key → 取到那条",
          str((got5 or {}).get("key")) == "learn_4621a8a9::web_docs_2026_activity",
          str((got5 or {}).get("key")))

    # ---- 6. 真实事故复现：torvalds 那次不能记到 tiktok 上 ----
    print("== 6. 事故复现：web_docs 的问题不许记到 tiktok_claimed_count ==")
    got6 = _attribute_reused_skill(
        skills=skills,
        used_names=["count_total"],       # 模型报了个对不上的泛称
        pool=skills)
    check("泛称对不上 → 不记账",
          str((got6 or {}).get("name")) != "tiktok_claimed_count",
          f"得到 {str((got6 or {}).get('name'))!r}")

    # ---- 7. 模型把**整段材料题头**抄回来 → 要能抽出材料名对上 ----
    # 模型偶尔会把「【材料 3｜topic=…｜some_name】」整段写进 used。
    # 这不是「模糊子串猜」的借口 —— 要**抽出材料名再逐字对**。
    print("== 7. 模型抄整段材料题头 → 抽出材料名再逐字对 ==")
    got7 = _attribute_reused_skill(
        skills=skills,
        used_names=["【材料 3｜topic=learn_4621a8a9｜web_docs_2026_activity】"],
        pool=skills)
    check("整段题头 → 取到那条",
          str((got7 or {}).get("key")) == "learn_4621a8a9::web_docs_2026_activity",
          str((got7 or {}).get("key")))
    got7b = _attribute_reused_skill(
        skills=skills, used_names=["【材料 3】"], pool=skills)
    check("只有序号没有名字 → 对不上 → 不记账", not got7b)

    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
