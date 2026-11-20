#!/usr/bin/env python3
"""本场景「**消费侧真查得到哪些表**」——单一事实源（2026-11-09，第二刀）。

为什么需要这一层（服务器实测，场景 L）
------------------------------------
场景 L 的真实骨架（`mve/ckpt_maieutic/L/*.json`，实读）里有一条维度：

    在 web_evidence 表中，item 为 'ce' 且属性为 '正文' 的记录中，
    值包含 '2007' 或 '03-26' 的记录有哪些？

而实查两个库（`/root/mve/scene_data/L/`）：

    scene.duckdb       -> ['web_docs', 'web_facts']    ← 数据源（消费侧查这个）
    web_ledger.duckdb  -> ['web_evidence']             ← 账本（审计用，不是学习目标）

`web_evidence` 在**账本库**里。消费侧的 `scene_profile.source_tables()` 只列
数据源表 → 这条维度点名的表**不在可达表清单里** → 判不出 db 路 → 又判不出
web 路（`_mentions_external` 的关键词表也不命中「表/记录/属性」）→ 整道题
落进 `infer` = **什么都不查**。

根因不在消费侧的判据，在**教师端**：`maieutic_scaffold.scene_sources()` 把
账本表 `web_evidence` 一起展示给出题器（`web_ingest.ingest_state` 的第 ③ 段
「审计账本 EAV：属性名与取值域在这里最全」），出题器于是拆出「去查账本表」
的维度 —— 但账本**不是学习目标**（`web_ingest.py` 文件头原话：「账本不该是
学习目标，所以搬到数据源之外，只留摊平后的宽表」）。

这一层就是补上那条约束的**唯一事实源**：

    · 教师端（拆骨架）用它决定「哪些表可以当落点」——账本表不算；
    · 消费端（路由）用它判断「维度点名的表查得到吗」——只认真实可达的表。

同构依据（原版 maieutic）
------------------------
原版教师端 `scaffolding.ts:262 buildScaffoldingUserMessage` 把
**targetUnit / targetLanguage** 告诉骨架生成器 —— 也就是「这个学生学过哪些
工具」这条**环境约束**，维度因此必然落在够得着的工具上（原版原文：
「Tools available: … NOT yet: …」，并明写「Do NOT include dimensions whose
only defensible answer requires tools the student hasn't learned」）。

MVE 同构 = 把「本场景哪些表查得到」告诉骨架生成器。MVE 的「工具」= 表。

⚠️ **为什么不能直接把 `scene_profile.source_tables()` 喂给出题器**：
那已经是数据源表清单（web_docs / web_facts），问题在于**账本表也出现在
`ingest_state` 里**。所以这一层要做的不是「多列一张表」，而是**把可达与
不可达分开**：账本表照旧展示（它有「属性名与取值域最全」的价值，出题器据此
知道**有哪些属性**），但**明确标注它不可查**。

本模块只回答两个问题，不做别的判断：

    reachable_tables(scene_id) -> list[str]   # 消费侧真查得到的表
    ledger_tables(scene_id)   -> list[str]   # 账本表（不可直接查）
"""

from __future__ import annotations

from typing import Any

# 账本表在**另一个 duckdb 文件**里，不是数据源表。
# 名字与 web_ingest.EVID 一致（这里是常量，不 import 以免循环依赖）。
LEDGER_FILE = "web_ledger.duckdb"


def _tables_of(db_path: str) -> list[str]:
    """一个 duckdb 文件里真实存在的表（打不开就空 = 不可达）。"""
    p = str(db_path or "").strip()
    if not p:
        return []
    try:
        import duckdb
        con = duckdb.connect(p, read_only=True)
        try:
            return [str(r[0]) for r in con.execute("show tables").fetchall()]
        finally:
            con.close()
    except Exception:
        return []


def _scene_dirs(scene_id: str) -> list[str]:
    """本场景下可能存在的 duckdb 目录（数据源库 + 账本库，同一个目录）。"""
    out: list[str] = []
    try:
        import scene_profile as _sp
        prof = _sp.profile(str(scene_id or "A")) or {}
        dp = str(prof.get("db_path") or "")
    except Exception:
        dp = ""
    if dp:
        out.append(dp)
    try:
        import scene_profile as _sp
        d = _sp.scene_dir(str(scene_id or "A"))
        if d and d not in out:
            out.append(d)
    except Exception:
        pass
    return out


def reachable_tables(scene_id: str) -> list[str]:
    """消费侧**真查得到**的表（数据源库里的表）。

    这是路由判据用的那一份 —— 只认真实存在的表名，不猜、不写词表。
    """
    out: list[str] = []
    try:
        import scene_profile as _sp
        out = list(_sp.source_tables(str(scene_id or "A")) or [])
    except Exception:
        out = []
    return out


def ledger_tables(scene_id: str) -> list[str]:
    """账本表（在**另一个** duckdb 文件里；消费侧查不到，不是学习目标）。"""
    out: list[str] = []
    for d in _scene_dirs(scene_id):
        p = f"{d.rstrip('/')}/{LEDGER_FILE}"
        for t in _tables_of(p):
            if t not in out:
                out.append(t)
    return out


def all_visible_tables(scene_id: str) -> list[str]:
    """数据源表 + 账本表（**给骨架生成器看**的完整清单）。

    ⚠️ 账本表在这里只是「让出题器知道有哪些属性/取值域」——
    它**不可直接查**（不在另一个库），所以教师端拆维度时不能把它当落点。
    """
    out: list[str] = []
    for t in reachable_tables(scene_id) + ledger_tables(scene_id):
        if t not in out:
            out.append(t)
    return out


def is_reachable(scene_id: str, table: str) -> bool:
    """这张表消费侧查得到吗（表名逐字比对，认不出就 False）。"""
    t = str(table or "").strip()
    if not t:
        return False
    return t in reachable_tables(scene_id)


def unreachable_in_text(scene_id: str, text: str) -> list[str]:
    """文本里点名了「**本场景看得见、但消费侧查不到**」的表（账本表）。

    为什么需要这一条（2026-11-09 服务器实测，场景 L）：
        `resplit_needed` 原本只看骨架维度里的 `landing` 字段（第二刀新加的）。
        但**第二刀之前生成的旧骨架没有这个字段** —— 而现场唯一存在的
        那份坏骨架正是旧的（维度写「在 web_evidence 表中查…」）。
        于是 `resplit_needed` 对它返回 False → 人看不到「该重拆」的信号
        → 坏骨架永远留在缓存里 → 那道题在消费侧永远落进 infer。

    判据（**实况，无关键词表**）：
        账本表名是**实查**出来的（`ledger_tables` = 另一个 duckdb 文件里
        真实存在的表）；文本里**字面包含**其中一个表名 ⇒ 这条维度指向的
        是那张查不到的表。与消费侧 `_mentions_scene_table` 同构（字面包含），
        只是方向相反：那边问「有没有点到**可达**表」，这边问「有没有点到
        **不可达**表」。

    ⚠️ 为什么不能反过来用「不在可达表里就算不可达」：
        那会把「这个场景根本没有的表名」（比如别的场景的表）也算成
        不可达落点 —— 判据必须是「**这张表真的存在，只是不在数据源里**」，
        账本表正好是这种情况（它确实存在，只是搬到了另一个库）。
    """
    blob = str(text or "")
    if not blob:
        return []
    return [t for t in ledger_tables(scene_id) if t and t in blob]


def describe(scene_id: str) -> dict[str, Any]:
    """给日志/人看的一句话摘要。"""
    reach = reachable_tables(scene_id)
    led = ledger_tables(scene_id)
    return {"reachable": reach, "ledger": led,
            "note": ("账本表在另一个 duckdb 文件里 —— 消费侧查不到，"
                     "不是学习目标（它的价值是「属性名与取值域最全」）"
                     if led else "")}
