"""来源绑定：题面点名的来源，必须**真的被取证过**（2026-10-31，A 项）。

为什么需要这一层（生产实证，Scene H）：

    ① 2026-10-31 09:36 维度问「web_facts 表中该仓库的 claim 字段内容是什么？」
       执行层调 `gh_repo_info`，把仓库 description 当 claim 交上去 → 闸门看到
       材料非空、措辞对得上就闭合 → 里程碑 3/5→4/5→5/5 全亮。
    ② 同一天 10:14 维度问「README.md 文件中提供的仓库**最后更新时间**是什么
       时候？」→ 执行层调 `gh_repo_info` 答 `updated_at` = 仓库元数据时间，
       而问题要的是 **README 里写的**时间 → 又放行（r=1.0）。

两次都是：材料是真的、来源是错的。旧判据只问「取到没有」（is_vacuous /
real_calls），从没问「取的是不是**这一份**」。

判据是**结构化**的，不含任何领域词表：

    题面里出现了哪些「可指认来源」→ 按**最具体的目标**取一类：
        带扩展名的文件名（README.md / payroll.duckdb…）
        > URL（https://…）
        > 仓库 slug（owner/repo）
        > 本库 show tables 的真实表名
    → 这一类里至少一个 token，必须有**一条 ok 的调用**其入参点到了它
    → 没碰到 / 碰了失败 / 只用了别的工具 → 不闭合，判词指名道姓。

为什么看「入参」而不是「返回文本」：返回里通常只有列名和值，不重复来源名；
入参（SQL、仓库 slug、文件路径、URL）才是"我问了谁"的原始凭据。

⚠️ 与「语义」的分界（2026-10-31 用户指出）：这一层只管**取证有没有打到对象上**，
不管「答得对不对」。判断"是否准确/是否完整"是模型能力（见 reason_pass.py）——
拿工具去代答语义问题，和拿语义去冒充取数，是同一类错。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_CACHE: dict[str, tuple[float, list[str]]] = {}

# 带扩展名的文件名：**扩展名表是文件系统事实，不是领域词表**。
# 只认这些扩展名，避免把 "1.5"/"a.b" 之类误当文件。
_FILE_EXT = ("md", "markdown", "json", "jsonl", "csv", "tsv", "txt", "log",
             "py", "js", "ts", "yaml", "yml", "toml", "ini", "cfg", "sql",
             "duckdb", "db", "sqlite", "html", "htm", "pdf", "xlsx", "ipynb")
_FILE_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-])([A-Za-z0-9_\-][A-Za-z0-9_.\-]{0,80}\."
    r"(?:" + "|".join(_FILE_EXT) + r"))(?![A-Za-z0-9_])", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>\"'）)，。；;]+", re.IGNORECASE)
# owner/repo：两段 ASCII，各至少 1 字，允许 . - _；不含 CJK 段（"练习线/消费线"不匹配）
_SLUG_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-/])([A-Za-z0-9][A-Za-z0-9_.\-]{0,38}"
    r"/[A-Za-z0-9][A-Za-z0-9_.\-]{0,38})(?![A-Za-z0-9_.\-/])")


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


def _dedup(xs: list[str]) -> list[str]:
    out: list[str] = []
    for x in xs:
        if x and x.lower() not in {y.lower() for y in out}:
            out.append(x)
    return out


def targets(dim_text: str, db_path: str) -> tuple[str, list[str]]:
    """题面点名的来源，按「最具体的目标」取一类 → (kind, tokens)。

    kind ∈ file / url / slug / table / ""（空 = 没点名，不约束）。
    为什么要"取一类"而不是"全都要"：一个 URL 里同时含 slug 和文件名，
    要求三者都命中会把「读 README」这种正确做法也判死；而只要求"任意一个"
    又会让 `gh_repo_info` 冒充「README 里的时间」。取最具体那一类是唯一
    既不漏也不冤的口径。
    """
    txt = str(dim_text or "")
    if not txt:
        return "", []
    files = _dedup([m.group(1) for m in _FILE_RE.finditer(txt)])
    if files:
        return "file", files
    urls = _dedup([m.group(0).rstrip(".,;") for m in _URL_RE.finditer(txt)])
    if urls:
        return "url", urls
    slugs = _dedup([m.group(1) for m in _SLUG_RE.finditer(txt)])
    if slugs:
        return "slug", slugs
    tables = [t for t in table_names(db_path) if _mentions(txt, t)]
    if tables:
        return "table", tables
    return "", []


def named_sources(dim_text: str, db_path: str) -> list[str]:
    """兼容旧调用：返回被点名的那一类 token（顺序稳定）。"""
    return targets(dim_text, db_path)[1]


_KIND_CN = {"file": "文件", "url": "网址", "slug": "仓库", "table": "表"}


def bind_hint(dim_text: str, db_path: str) -> str:
    """执行前给模型的**硬约束**（和判据同一口径，先讲清再判）。"""
    kind, toks = targets(dim_text, db_path)
    if not toks:
        return ""
    what = _KIND_CN.get(kind, "来源")
    more = ("（先取它，再在材料上作答）" if kind in ("file", "url") else "")
    return ("\n\n【来源绑定】这条问题点名了" + what + " "
            + "、".join(toks)
            + " —— 必须真的把" + what + "取到手并把取到的内容作为答案依据"
            + more + "；用别的来源（仓库元数据、别的表、记忆）顶替不算答到，"
            "判未闭合。")


def check(dim_text: str, evidence: list[dict[str, Any]] | None,
          db_path: str) -> tuple[bool, str]:
    """返回 (是否绑定, 不绑定的判词)。无点名来源 / 无法归因 → (True, "")。

    「无法归因」= 证据条目里一条 `args` 都没有（老路径产的材料账没记入参）。
    这时不拦：判据只能判它看得见的东西，凭空判死比放过更糟。
    """
    kind, toks = targets(dim_text, db_path)
    if not toks:
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
        for t in toks:
            if _mentions(hay, t):
                (ok_hit if e.get("ok") else tried).add(t)
    if ok_hit:
        return True, ""
    what = _KIND_CN.get(kind, "来源")
    if tried:
        return False, (f"本轮确实取了 {'、'.join(sorted(tried))}，但那次调用**没取到内容**"
                       f"（报错/空结果）—— {what}是对的，换一种取法或换个路径再试")
    used = sorted({str(e.get("tool") or "?") for e in items if e.get("ok")})
    used = [u for u in used if u][:4]
    return False, (
        f"这条问题点名了{what} {'、'.join(toks)}，但本轮**一步都没取它**"
        f"（实际只用了 {'、'.join(used) if used else '没有可用调用'}）——"
        f"别处的真数据不能当它的答案。先把 {toks[0]} 取到手，再在取到的内容上作答"
    )
