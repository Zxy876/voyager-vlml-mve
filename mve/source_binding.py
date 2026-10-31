"""来源绑定：维度点名的数据源，必须**真的被查过**（2026-10-31，A 项）。

为什么需要这一层（生产实证，Scene H 2026-10-31 09:36 那轮）：

    维度问：「web_facts 表中该仓库的 claim 字段内容是什么？」
    执行层做：调了一次 `gh_repo_info("Zxy876/voyager-vlml-mve")`，
              把仓库 description 当 "claim" 交上去。
    闸门看到：材料非空（真取到一段正文）+ 答案措辞对得上 → 闭合。
    结果：里程碑 3/5 → 4/5 → 5/5 全亮，技能库里多出一条「用仓库元数据
          回答表字段问题」的错误配方。

材料是真的，来源是错的。旧判据只问「取到没有」（`is_vacuous` / `real_calls`），
从没问过「取的是不是**这一份**」。原版 Voyager 不会遇到这个：Minecraft 里
问题与末态在同一空间，看得见 `iron_ore ×1` 就算数；SQL/文档场景里「别处的
真数据」和「这里的答案」长得一模一样，必须显式判来源。

判据是**结构化**的，不含任何词表：

    「维度题面里出现了本库 `show tables` 里的哪张表」→ 环境指认（表名来自
    真实库，不是我们写的同义词表）
    → 这些表里至少一张，必须有**一条 ok 的调用**其入参点到了它
    → 没碰到 / 碰了失败 / 只用了别的工具 → 不闭合，判词指名道姓说清楚。

为什么看「入参」而不是「返回文本」：返回里通常只有列名和值，不重复表名；
入参（SQL 语句、仓库 slug、URL）才是"我问了谁"的原始凭据。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_CACHE: dict[str, tuple[float, list[str]]] = {}


def table_names(db_path: str) -> list[str]:
    """本库真实存在的表名（`show tables` 直查；打不开就返回空 = 不拦）。"""
    p = str(db_path or "").strip()
    if not p or not Path(p).exists():
        return []
    try:
        mt = os.path.getmtime(p)
    except OSError:
        mt = 0.0
    hit = _CACHE.get(p)
    if hit and hit[0] == mt:
        return list(hit[1])
    got: list[str] = []
    try:
        import duckdb
        con = duckdb.connect(p, read_only=True)
        try:
            got = [str(r[0]) for r in con.execute("show tables").fetchall()]
        finally:
            con.close()
    except Exception:
        got = []
    _CACHE[p] = (mt, list(got))
    return got


def _mentions(hay: str, name: str) -> bool:
    """`name` 是否作为独立标识符出现（`web` 不该命中 `website`）。

    中文紧邻不算边界破坏（CJK 不在 [A-Za-z0-9_] 里），所以「web_facts 表中」
    这种写法照常命中。
    """
    if not name:
        return False
    pat = r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])"
    return re.search(pat, hay, re.IGNORECASE) is not None


def named_sources(dim_text: str, db_path: str) -> list[str]:
    """维度题面点名了本库的哪几张表（顺序按表清单，稳定）。"""
    txt = str(dim_text or "")
    if not txt:
        return []
    return [t for t in table_names(db_path) if _mentions(txt, t)]


def bind_hint(dim_text: str, db_path: str) -> str:
    """执行前给模型的**硬约束**（和判据同一口径，先讲清再判）。

    不给人看的一句话，是给模型的一句话：本题答案必须来自这几张表。
    """
    names = named_sources(dim_text, db_path)
    if not names:
        return ""
    return ("\n\n【来源绑定】这条问题的答案在库里的 "
            + "、".join(names)
            + " 中 —— 必须真的查这几张表并把取到的值作为答案；"
            "用别的数据源（仓库元数据、网页、记忆）顶替不算答到，判未闭合。"
            "先 `SELECT * FROM " + names[0] + " LIMIT 1` 看清列名与真实取值。")


def check(dim_text: str, evidence: list[dict[str, Any]] | None,
          db_path: str) -> tuple[bool, str]:
    """返回 (是否绑定, 不绑定的判词)。无点名表 / 无法归因 → (True, "")。

    「无法归因」= 证据条目里一条 `args` 都没有（老路径产的材料账没记入参）。
    这时不拦：判据只能判它看得见的东西，凭空判死比放过更糟。
    """
    names = named_sources(dim_text, db_path)
    if not names:
        return True, ""
    items = [e for e in (evidence or []) if isinstance(e, dict)]
    if not any(str(e.get("args") or "").strip() for e in items):
        return True, ""
    ok_hit: set[str] = set()
    tried: set[str] = set()
    for e in items:
        hay = str(e.get("args") or "")
        if not hay:
            continue
        for t in names:
            if _mentions(hay, t):
                (ok_hit if e.get("ok") else tried).add(t)
    if ok_hit:
        return True, ""
    if tried:
        why = (f"本轮确实查了 {'、'.join(sorted(tried))}，但那次调用**没取到内容**"
               "（报错/空结果）—— 表是对的，查询条件或列名要按实查清单改")
    else:
        used = [str(e.get("tool") or "?") for e in items if e.get("ok")]
        uniq = sorted({u for u in used if u})[:4]
        why = (f"这条问题问的是 {'、'.join(names)} 里的内容，但本轮**一步都没查它**"
               f"（实际只用了 {'、'.join(uniq) if uniq else '没有可用调用'}）——"
               "别处的真数据不能当它的答案。用能查库的原语（如 query_sql）"
               f"直接查 {'、'.join(names)}，先 `SELECT * … LIMIT 1` 看清列与取值")
    return False, why
