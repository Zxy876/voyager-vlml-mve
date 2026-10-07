#!/usr/bin/env python3
"""VLML 的**结构发现**：21 张表怎么分层、46 个洞察读哪些表、9 个工具由哪些洞察组成。

为什么要单独建这个文件
----------------------
用户问了一句关键的：「22 张表之间的关系，一般是 MCP 工具再分出洞察的建模」。
之前图谱里只有 2 张表（rounds / agg_first_blood_stats），因为只从**我们自己的
rubric SQL** 里反推 —— 那等于"出过题的部分才进图"，VLML 真正的结构一概没进。

实际结构（取证自 VLML 源码）是四层：

    MCP 工具（9 个报告工具）
      └─ section / 洞察（如 key_metrics.economy.pistol、scope.confidence）
           └─ SQL 文件（46 个，tools/sql/*.sql，由 load_sql() 加载）
                └─ 表（21 张）

而 section 的名字**就是** rubric 里 answer_spec.value_path 的前缀 ——
「洞察」这一层缺失，正是"知道调哪个工具、不知道值从哪一段来"的原因。

分层的依据（这是伴学 `stage` 的同构物）
--------------------------------------
伴学的知识点带 stage（primary / junior_high / senior_high），并按
`stage_to_ids` 索引 —— 学什么之前必须先会什么，是**学习顺序**。
MVE 的同构物是**数据流顺序**：算什么之前必须先有什么。

    raw      base_events（原始事件流）
    meta     series / games / rounds + 4 张参考表（元数据与字典）
    agg      13 张 agg_* 派生聚合表（由 raw/meta 算出，见 transformations/）
    insight  46 个洞察 SQL 单元
    tool     9 个报告工具
    dimension MVE 的评分维度（最上层，rubric 声明）

照伴学的规矩：**一切可查的都实查，不手写**。
列名、表清单来自 information_schema（实查）；派生关系来自 transformations/*.sql；
洞察来自 tools/sql/*.sql；归属来自源码里的 load_sql() 调用点。

跑法
----
    python mve/vlml_schema.py            # 打印发现到的结构
    python mve/vlml_schema.py --tables   # 只看表与分层
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

VLML_ROOT = HERE.parent / "vlml"
SQL_DIR = VLML_ROOT / "src" / "vlml" / "tools" / "sql"
REPORTS_DIR = VLML_ROOT / "src" / "vlml" / "tools" / "reports"
TRANSFORM_DIR = VLML_ROOT / "database" / "transformations"
SCHEMA_DIR = VLML_ROOT / "database" / "schema"

# ---------------------------------------------------------------------------
# 阶段：照伴学的 stage（primary / junior_high / senior_high）
# ---------------------------------------------------------------------------
STAGE_RAW = "raw"
STAGE_META = "meta"
STAGE_AGG = "agg"
STAGE_INSIGHT = "insight"
STAGE_TOOL = "tool"
STAGE_DIMENSION = "dimension"

STAGE_LABEL = {
    STAGE_RAW: "原始事件",
    STAGE_META: "元数据/字典",
    STAGE_AGG: "派生聚合",
    STAGE_INSIGHT: "洞察",
    STAGE_TOOL: "工具",
    STAGE_DIMENSION: "评分维度",
}

# 学习的顺序 = 数据的顺序：下标小的先有
STAGE_ORDER = [STAGE_RAW, STAGE_META, STAGE_AGG, STAGE_INSIGHT,
               STAGE_TOOL, STAGE_DIMENSION]

_FAKE_TABLES = {
    "select", "where", "group", "order", "limit", "having", "union",
    "values", "set", "table", "lateral",
}

_RE_FROM = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 表：实查 information_schema
# ---------------------------------------------------------------------------
def _stage_of_table(name: str) -> str:
    """按 VLML 自己的命名约定分层（可追溯，不是猜的）。"""
    if name == "base_events":
        return STAGE_RAW
    if name.startswith("agg_"):
        return STAGE_AGG
    return STAGE_META


def _grain_of_table(name: str) -> str:
    """表的粒度（伴学 `unit` 的同构物）。

    从**表名的命名约定**推：agg_{主体}_{粒度}_stats。
    这是 VLML 自己的约定（见 database/transformations/ 的文件名），不是我编的。
    """
    subject = {"player": "选手", "team": "队伍", "tournament": "赛事"}
    grain = {"round": "每回合", "game": "每图", "series": "每 series",
             "map": "每图", "daily": "每天", "series_stats": "每 series"}
    if name == "base_events":
        return "每个事件一行"
    if name == "rounds":
        return "每个回合一行"
    if name == "games":
        return "每张图一行"
    if name == "series":
        return "每个 series 一行"
    m = re.match(r"agg_(\w+?)_(round|game|series|map|daily)_stats$", name)
    if m:
        return f"{subject.get(m.group(1), m.group(1))} · {grain.get(m.group(2), m.group(2))}"
    if name.startswith("agg_"):
        return "聚合表"
    return "参考/字典表"


async def _q(sql: str) -> list[tuple]:
    import vlml_env  # noqa: F401
    from vlml_env import execute_custom_sql
    try:
        res = await execute_custom_sql(sql)
    except Exception:
        return []
    return list((res or {}).get("rows") or [])


async def discover_tables() -> dict[str, dict[str, Any]]:
    """实查表清单 + 列名 + 主键 + 行数。"""
    rows = await _q("SELECT table_name, column_name FROM information_schema.columns "
                    "ORDER BY table_name, ordinal_position")
    cols: dict[str, list[str]] = {}
    for t, c in rows:
        cols.setdefault(str(t), []).append(str(c))

    out: dict[str, dict[str, Any]] = {}
    for name in sorted(cols):
        try:
            cnt = await _q(f"SELECT COUNT(*) FROM {name}")
            n = int(cnt[0][0]) if cnt else 0
        except Exception:
            n = 0
        out[name] = {
            "stage": _stage_of_table(name),
            "grain": _grain_of_table(name),
            "columns": cols[name],
            "rows": n,
            "source": "information_schema（实查）",
        }
    return out


def discover_lineage(known: set[str]) -> dict[str, list[str]]:
    """派生表来自哪些表 —— 解析 transformations/*.sql。

    这些文件是 INSERT INTO agg_x SELECT ... FROM base_events ... 的形状，
    FROM/JOIN 里出现的**非自身**表名就是上游。自环（DELETE FROM 自身）要剔除，
    否则每张表都变成自己的上游。
    """
    out: dict[str, list[str]] = {}
    if not TRANSFORM_DIR.exists():
        return out
    for path in sorted(TRANSFORM_DIR.glob("*.sql")):
        target = re.sub(r"^\d+_", "", path.stem)
        text = path.read_text(encoding="utf-8")
        ups = {m.lower() for m in _RE_FROM.findall(text)}
        ups = {u for u in ups if u in known and u != target
               and u not in _FAKE_TABLES}
        if ups:
            out[target] = sorted(ups)
    return out


def discover_insights(known: set[str]) -> dict[str, dict[str, Any]]:
    """46 个洞察 SQL：每个读哪些表、属于哪个报告、干什么用。"""
    out: dict[str, dict[str, Any]] = {}
    if not SQL_DIR.exists():
        return out
    purpose = _readme_purpose()
    owner = _sql_owner()
    for path in sorted(SQL_DIR.glob("*.sql")):
        name = path.stem
        text = path.read_text(encoding="utf-8")
        tbls = {m.lower() for m in _RE_FROM.findall(text)}
        tbls = {t for t in tbls if t in known and t not in _FAKE_TABLES}
        reports = sorted(owner.get(name) or [])
        out[name] = {
            "stage": STAGE_INSIGHT,
            "tables": sorted(tbls),
            "reports": reports,
            "purpose": purpose.get(name, ""),
            "file": f"tools/sql/{path.name}",
            "source": "tools/sql（解析 FROM/JOIN）",
        }
    return out


def _readme_purpose() -> dict[str, str]:
    """README 里那张 `| SQL File | Used By | Purpose |` 表 —— VLML 自己写的说明。"""
    out: dict[str, str] = {}
    readme = SQL_DIR / "README.md"
    if not readme.exists():
        return out
    for line in readme.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*`([a-z0-9_]+)\.sql`\s*\|\s*`([^`]+)`\s*\|\s*([^|]+)\|", line)
        if m:
            out[m.group(1)] = m.group(3).strip()
    return out


def _sql_owner() -> dict[str, set[str]]:
    """哪个报告模块 load 了这个 SQL（解析源码里的 load_sql("xxx.sql")）。"""
    out: dict[str, set[str]] = {}
    module_to_report = {
        "scouting.py": "scouting_report",
        "player_profile.py": "player_profile_report",
        "match_analysis.py": "match_analysis_report",
        "data_fetch.py": "",      # 共享工具，不属于单个报告
    }
    for path in list(REPORTS_DIR.glob("*.py")):
        report = module_to_report.get(path.name, "")
        for m in re.finditer(r'load_sql\(\s*"([a-z0-9_]+\.sql)"',
                             path.read_text(encoding="utf-8")):
            out.setdefault(Path(m.group(1)).stem, set())
            if report:
                out[Path(m.group(1)).stem].add(report)
    return out


# ---------------------------------------------------------------------------
# 工具 → section（洞察）：解析报告函数的 return 结构
# ---------------------------------------------------------------------------
def discover_tools() -> dict[str, dict[str, Any]]:
    """每个报告工具由哪些 section 组成。

    怎么来的：报告函数 `return {...}` 里的键名就是 section；
    section 的值大多来自 `_xxx()`，而这些函数内部 load 了哪些 SQL，
    就得到 section → SQL 的归属（靠**同一个函数名**对上）。
    """
    # 扫描 reports/ 下**所有** `async def *_report(` —— 不写死文件名。
    # 写死过一次就漏了 pattern_detection_report（它在 reports/pattern_detection.py），
    # 于是它的 section 一直是空，"pistol_win_rate 取 key_metrics 段"这条
    # 关键信息就出不来。
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(REPORTS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"async def ([a-z0-9_]+)\(", text):
            func = m.group(1)
            if not func.endswith("_report"):
                continue
            tool = func
            # 函数体 = 到下一个顶层 def 为止（写死长度会漏掉长函数）
            nxt = re.search(r"\n(?:async def |def )", text[m.end():])
            body = text[m.end(): m.end() + (nxt.start() if nxt else 20000)]
            # 必须是**多行**的 return {：函数开头那些 `return {"error": ...}`
            # 是提前返回的错误分支，一行写完，不是我们要的报告结构。
            rm = re.search(r"return\s*\{\s*\n", body)
            if not rm:
                continue
            depth, i = 1, rm.end()
            while i < len(body) and depth > 0:
                if body[i] == "{":
                    depth += 1
                elif body[i] == "}":
                    depth -= 1
                i += 1
            block = body[rm.end(): i - 1]
            # 只要缩进 ≥8 的顶层键（section），过滤掉 report_type/version 这类元字段
            skip = {"report_type", "version", "series_id", "team_name",
                    "player_name", "map_name", "error"}
            sections = [s for s in re.findall(r'^\s{8,}"([a-z_]+)":', block, re.M)
                        if s not in skip]
            doc = re.search(r'"""(.*?)"""', text[:m.end()], re.S)
            returns = ""
            if doc:
                rdoc = re.search(r"Returns:\s*\n(.*?)(?:\n\s*\n|\"\"\")",
                                 doc.group(1), re.S)
                if rdoc:
                    returns = rdoc.group(1)
            out[tool] = {
                "stage": STAGE_TOOL,
                "sections": sections,
                "section_desc": dict(list(
                    dict(re.findall(r"-\s*([a-z_]+):\s*(.+)", returns)).items())[:12]),
                "source": f"tools/reports/{path.name}（解析 return 结构）",
            }
    return out


_SKIP_CALLS = {
    "if", "for", "while", "return", "not", "and", "or", "in", "is", "len",
    "int", "str", "float", "bool", "round", "sorted", "list", "dict", "set",
    "min", "max", "sum", "abs", "print", "super", "lambda", "next", "zip",
    "enumerate", "isinstance", "getattr", "format", "range", "tuple",
}


def _func_graph(text: str) -> dict[str, dict[str, list[str]]]:
    """函数 → {加载的 SQL, 调用的函数}。

    必须连**调用关系**一起存：key_metrics 走的是两层
    （focus_metrics → assemble_team_metrics → _team_economy_metrics → load_sql），
    只记"谁 load 了 SQL"会得到空集 —— 实测第一次就漏了 key_metrics，
    而它恰恰是 pistol_win_rate / eco_win_rate 所在的段。
    """
    out: dict[str, dict[str, list[str]]] = {}
    for m in re.finditer(r"(?:async\s+)?def ([a-z_0-9]+)\(", text):
        name = m.group(1)
        nxt = re.search(r"\n(?:async def |def |class )", text[m.end():])
        body = text[m.end(): m.end() + (nxt.start() if nxt else 20000)]
        sqls = sorted({Path(s).stem
                       for s in re.findall(r'load_sql\(\s*"([a-z0-9_]+\.sql)"', body)})
        calls = sorted({c for c in re.findall(r"([a-z_][a-z_0-9]*)\s*\(", body)
                        if c not in _SKIP_CALLS and c != name})
        out[name] = {"sqls": sqls, "calls": calls}
    return out


def _collect_sqls(fn: str, graph: dict[str, dict[str, list[str]]],
                  depth: int = 3, seen: set[str] | None = None) -> list[str]:
    """递归展开：这个函数（含它调用的）一共用到哪些 SQL。"""
    seen = seen or set()
    if fn in seen or depth < 0:
        return []
    seen.add(fn)
    node = graph.get(fn) or {}
    out = list(node.get("sqls") or [])
    for c in node.get("calls") or []:
        out += _collect_sqls(c, graph, depth - 1, seen)
    return out


def section_insights() -> dict[str, dict[str, list[str]]]:
    """工具 → section → 由哪些洞察 SQL 组成。

    静态链条（VLML 的真实写法）：
        return {"key_metrics": {"team": focus_metrics, ...}}
        focus_metrics = assemble_team_metrics(...)
        assemble_team_metrics() → _team_round_metrics / _team_economy_metrics ...
        _team_economy_metrics() → load_sql("team_economy_pistol.sql")

    所以要走三步：section 的表达式 → 变量 → 函数 → 它 load 的 SQL。
    缺了这层，"洞察"就只是图里的一批节点，永远进不了 prompt。
    """
    result: dict[str, dict[str, list[str]]] = {}
    # 函数图必须**跨文件**建：pattern_detection.py 里
    # `key_metrics = assemble_team_metrics(...)`，而 assemble_team_metrics
    # 定义在 match_analysis.py —— 按文件建图会查不到，于是
    # pattern_detection_report 的所有 section 都是空。
    graph: dict[str, dict[str, list[str]]] = {}
    texts: dict[str, str] = {}
    for path in sorted(REPORTS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        texts[path.name] = text
        for fn, node in _func_graph(text).items():
            slot = graph.setdefault(fn, {"sqls": [], "calls": []})
            slot["sqls"] = sorted(set(slot["sqls"]) | set(node["sqls"]))
            slot["calls"] = sorted(set(slot["calls"]) | set(node["calls"]))

    for fname, text in texts.items():
        for m in re.finditer(r"async def ([a-z0-9_]+)\(", text):
            tool = m.group(1)
            if not tool.endswith("_report"):
                continue
            nxt = re.search(r"\n(?:async def |def )", text[m.end():])
            body = text[m.end(): m.end() + (nxt.start() if nxt else 20000)]
            rm = re.search(r"return\s*\{\s*\n", body)
            if not rm:
                continue
            depth, i = 1, rm.end()
            while i < len(body) and depth > 0:
                if body[i] == "{":
                    depth += 1
                elif body[i] == "}":
                    depth -= 1
                i += 1
            block = body[rm.end(): i - 1]
            # 变量 → 函数（focus_metrics = assemble_team_metrics(...)）
            var2func = dict(re.findall(r"^\s*([a-z_0-9]+)\s*=\s*([a-z_0-9]+)\s*\(",
                                       body, re.M))
            sections: dict[str, list[str]] = {}
            for sm in re.finditer(r'^\s{8,}"([a-z_]+)":\s*(.*)$', block, re.M):
                sec, expr = sm.group(1), sm.group(2)
                if sec in ("report_type", "version", "series_id", "team_name",
                           "player_name", "map_name", "error"):
                    continue
                # 嵌套块（"scope": { 换行 …）→ 取到配对的 } 为止，
                # 否则表达式只有 "{"，什么函数调用都看不到。
                if expr.strip().endswith("{"):
                    depth, k = 1, sm.end(2)
                    while k < len(block) and depth > 0:
                        if block[k] == "{":
                            depth += 1
                        elif block[k] == "}":
                            depth -= 1
                        k += 1
                    expr = block[sm.end(2): k]
                # 两种都要：`_team_comparison(db, ...)` 是函数调用，
                # `key_metrics,` 是**裸变量**（值来自前面的赋值），只取前者会漏。
                names = set(re.findall(r"([a-z_][a-z_0-9]*)\s*\(", expr))
                names |= set(re.findall(r"([a-z_][a-z_0-9]{3,})", expr))
                sqls: list[str] = []
                for n in sorted(names):
                    fn = var2func.get(n, n)
                    sqls += _collect_sqls(fn, graph)
                if sqls:
                    sections[sec] = sorted(set(sqls))
            if sections:
                result[tool] = sections
    return result


# --------------------------------------------------------------------------
# 建模声明：VLML **自己写**的建模文档
# --------------------------------------------------------------------------
# 和 `discover()` 的分工
# ----------------------
# `discover()` 是**实查数据库 / 解析源码** → 得到"库里现在有什么"（列名、行数、
# 派生关系）。连不上库它就没了 —— 实测断库时图谱里 21 张表全空。
#
# 但伴学的知识点并不是"实查"出来的：457 个节点**自带** difficulty /
# question_types / typical_misconceptions / prerequisites，是作者声明的。
# VLML 里同层的"作者声明"就是 `database/` 下这四份建模文档：
#
#   DATA_MODEL.md              每张表的 Grain + Use cases + 核心关系链
#   DERIVED_TABLES.md          7 张派生表的粒度/上游/用途/关键列/示例查询
#   metadata/column_definitions.yaml   列级口径（141 行）
#   DATA_DICTIONARY.json       13 张表的 pk / 列类型 / 指标公式
#
# 这些是**文件**，永远在。把它们解析成结构化声明写进图谱，图谱才真正承担起
# 伴学 knowledge_seeds 那个角色：出题器读的是图谱，不是读数据库、也不是读题。
DATA_DIR = VLML_ROOT / "database"
MODEL_FILES = (
    DATA_DIR / "DATA_MODEL.md",
    DATA_DIR / "DERIVED_TABLES.md",
    DATA_DIR / "metadata" / "column_definitions.yaml",
    DATA_DIR / "DATA_DICTIONARY.json",
)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _parse_core_lineage(text: str) -> dict[str, dict[str, Any]]:
    """DATA_MODEL.md 的 Core Relationships：series → games → rounds → base_events。

    这是 VLML 的**主干血缘**（谁是谁的子表），伴学 `prerequisites` 的同构物：
    要算回合级指标，必须先有 rounds；要算事件级，必须先有 base_events。
    """
    m = re.search(r"##\s+Core Relationships\s*```(.*?)```", text, re.S)
    if not m:
        return {}
    out: dict[str, dict[str, Any]] = {}
    stack: list[tuple[int, str]] = []
    for raw in m.group(1).splitlines():
        if not raw.strip():
            continue
        nm = re.search(r"([a-z_][a-z_0-9]*)\s*\(", raw)
        if not nm:
            continue
        name = nm.group(1)
        keys: list[str] = []
        km = re.search(r"\(([^)]*)\)", raw)
        if km:
            keys = [k.strip() for k in km.group(1).split(",") if k.strip()]
        indent = len(raw) - len(raw.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        out[name] = {"parent": stack[-1][1] if stack else "", "keys": keys}
        stack.append((indent, name))
    return out


def _parse_data_model(text: str) -> dict[str, dict[str, Any]]:
    """`### Title (`tbl`)` + `**Grain:**` + `**Use cases:**`。"""
    out: dict[str, dict[str, Any]] = {}
    for m in re.finditer(
            r"^###\s+(.+?)\s*\(`([a-z_0-9]+)`\)\s*$(.*?)(?=^###\s|\Z)",
            text, re.S | re.M):
        tbl, body = m.group(2), m.group(3)
        rec: dict[str, Any] = {"title": m.group(1).strip()}
        gm = re.search(r"\*\*Grain:\*\*\s*(.+)", body)
        um = re.search(r"\*\*Use cases:\*\*\s*(.+)", body)
        if gm:
            rec["grain"] = gm.group(1).strip()
        if um:
            rec["use_cases"] = um.group(1).strip()
        if len(rec) > 1:
            out[tbl] = rec
    # 参考表是 bullet 形式：「- `agent_roles`: one row per agent, role mapping」
    rm = re.search(r"##\s+Reference Tables(.*?)(?=^##\s|\Z)", text, re.S | re.M)
    if rm:
        for lm in re.finditer(r"^-\s+`([a-z_0-9]+)`:\s*(.+)$", rm.group(1), re.M):
            rec = out.setdefault(lm.group(1), {"title": lm.group(1)})
            rec.setdefault("use_cases", lm.group(2).strip())
    return out


def _parse_derived_tables(text: str) -> dict[str, dict[str, Any]]:
    """DERIVED_TABLES.md：概览表（粒度/上游/用途）+ 每个 `###` 块的详情。"""
    out: dict[str, dict[str, Any]] = {}
    for lm in re.finditer(
            r"^\|\s*`([a-z_0-9]+)`\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|",
            text, re.M):
        tbl, grain, src, purpose = lm.groups()
        if tbl in ("Table", "table") or set(tbl) <= set("-: "):
            continue                       # 表头行
        out[tbl] = {"grain": grain.strip(), "upstream_raw": src.strip(),
                    "purpose": purpose.strip()}
    for m in re.finditer(r"^###\s+([a-z_0-9]+)\s*$(.*?)(?=^###\s|\Z)",
                         text, re.S | re.M):
        tbl, body = m.group(1), m.group(2)
        rec = out.setdefault(tbl, {})
        pm = re.search(r"\*\*Primary Key:\*\*\s*`?([^`\n]+)`?", body)
        if pm:
            rec["pk_text"] = pm.group(1).strip().strip("`")
        # 标题下第一段散文 = 用途
        for line in body.splitlines():
            s = line.strip()
            if s and not s.startswith(("**", "-", "|", "```", "#")):
                rec.setdefault("use_cases", s)
                break
        kc = re.search(r"\*\*Key Columns:\*\*(.*?)(?=\*\*|\Z)", body, re.S)
        if kc:
            cols: dict[str, str] = {}
            # `\s+` 会吃掉换行，于是 "- a, b - desc" 会跨行粘到下一条 bullet
            # 上（实测 games_played 的说明变成了下一行的列名）——只吃空格。
            for cm in re.finditer(r"^-\s+(.+?)[ \t]+-[ \t]+(.+)$",
                                  kc.group(1), re.M):
                for n in re.findall(r"`([a-z_0-9]+)`", cm.group(1)):
                    cols[n] = cm.group(2).strip()
            if cols:
                rec["key_columns"] = cols
        rc = re.search(r"\*\*Row count:\*\*\s*(.+)", body)
        if rc:
            rec["row_count_text"] = rc.group(1).strip()
        ex = re.search(r"```sql(.*?)```", body, re.S)
        if ex:
            rec["example_sql"] = ex.group(1).strip()
    return out


def _parse_column_yaml(path: Path) -> dict[str, dict[str, Any]]:
    """metadata/column_definitions.yaml：列级口径（VLML 手写，比字典更细）。"""
    try:
        import yaml
        raw = yaml.safe_load(_read(path)) or {}
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for tbl, body in (raw.get("tables") or {}).items():
        if not isinstance(body, dict):
            continue
        out[str(tbl)] = {
            "description": str(body.get("description") or ""),
            "columns": {str(k): str(v)
                        for k, v in (body.get("columns") or {}).items()},
        }
    return out


def _parse_data_dictionary(path: Path) -> dict[str, dict[str, Any]]:
    """DATA_DICTIONARY.json：pk / 列类型 / flag / unit / **指标公式**。"""
    try:
        raw = json.loads(_read(path))
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for e in raw or []:
        tbl = str(e.get("table") or "")
        if not tbl:
            continue
        cols: dict[str, dict[str, Any]] = {}
        for c, v in (e.get("columns") or {}).items():
            if isinstance(v, dict):
                item = {k: v[k] for k in ("desc", "type", "unit")
                        if v.get(k)}
                if v.get("flag"):
                    item["flag"] = True
                cols[str(c)] = item
            elif v:
                cols[str(c)] = {"desc": str(v)}
        out[tbl] = {
            "grain": str(e.get("grain") or ""),
            "pk": [str(x) for x in (e.get("pk") or [])],
            "columns": cols,
            "metrics": {str(k): str(v)
                        for k, v in (e.get("metrics") or {}).items()},
        }
    return out


def _upstream_of(rec: dict[str, Any], parent: str, known: set[str]) -> list[str]:
    """上游表：DERIVED_TABLES 的 Source 列 + 主干父表。

    `seed` 不是上游（它表示"手工种子数据"），自身也不算。
    """
    got: list[str] = []
    for tok in re.split(r"[,\s]+", str(rec.get("upstream_raw") or "")):
        t = tok.strip().strip("`")
        if t and t != "seed" and t in known and t not in got:
            got.append(t)
    if parent and parent not in got:
        got.append(parent)
    return got


def _layer_of(name: str, core: dict[str, dict[str, Any]],
              derived: dict[str, Any]) -> str:
    """这张表在建模里的角色（伴学 `chapter` 的同构物）。"""
    if name in core:
        return "core"                 # 主干：series / games / rounds / base_events
    if derived:
        return "derived"              # DERIVED_TABLES.md 里单列声明的 7 张
    if name.startswith(("agent_", "weapon_", "map_", "ability_", "ref_")):
        return "ref"                  # 参考/字典表
    return "agg"


def model_specs() -> dict[str, Any]:
    """把四份建模文档解析成每张表的结构化声明。

    返回 `{"tables": {name: {...}}, "fingerprint": str, "files": [...]}`。
    fingerprint 用来判断缓存是否过期 —— 建模文档一改，图谱就得重建，
    否则新声明永远进不了已经落盘的 `knowledge_graph.json`。
    """
    dm_p, dt_p = MODEL_FILES[0], MODEL_FILES[1]
    cd_p, dd_p = MODEL_FILES[2], MODEL_FILES[3]
    dm_text, dt_text = _read(dm_p), _read(dt_p)
    core = _parse_core_lineage(dm_text)
    model = _parse_data_model(dm_text)
    derived = _parse_derived_tables(dt_text)
    ycols = _parse_column_yaml(cd_p)
    ddic = _parse_data_dictionary(dd_p)

    names = set(core) | set(model) | set(derived) | set(ycols) | set(ddic)
    tables: dict[str, dict[str, Any]] = {}
    for n in sorted(names):
        a, b = model.get(n) or {}, derived.get(n) or {}
        c, d = ycols.get(n) or {}, ddic.get(n) or {}

        # 列口径：yaml 是手写的（更细），字典补 type/flag/unit
        coldesc: dict[str, dict[str, Any]] = {}
        for col, txt in (c.get("columns") or {}).items():
            coldesc[str(col)] = {"desc": str(txt)}
        for col, meta in (d.get("columns") or {}).items():
            cur = coldesc.setdefault(str(col), {})
            if meta.get("desc") and not cur.get("desc"):
                cur["desc"] = str(meta["desc"])
            for k in ("type", "flag", "unit"):
                if meta.get(k) is not None:
                    cur[k] = meta[k]
        # 派生表不在 DATA_DICTIONARY 里（13 张表没覆盖 7 张派生表），列口径
        # 只能从 DERIVED_TABLES.md 的 Key Columns 取 —— 而 agg_first_blood_stats
        # 恰恰是出题用得最多的表，`fb_team_won` 的口径就在那儿写着。
        for col, txt in (b.get("key_columns") or {}).items():
            cur = coldesc.setdefault(str(col), {})
            if not cur.get("desc"):
                cur["desc"] = str(txt)
                cur["origin"] = "DERIVED_TABLES"
        coldesc = {k: v for k, v in coldesc.items() if v.get("desc")}

        pk: list[str] = []
        pkm = re.findall(r"[a-z_][a-z_0-9]*", str(b.get("pk_text") or ""))
        if pkm:
            pk = pkm
        elif d.get("pk"):
            pk = list(d["pk"])

        # 反引号是 markdown 的行内代码标记，不是粒度的一部分
        grain = re.sub(r"`", "", str(
            a.get("grain") or b.get("grain") or d.get("grain") or "")).strip()
        tables[n] = {
            "title": str(a.get("title") or n),
            # 用途优先取**完整散文句**（"Pre-joined first blood events with
            # round outcomes."），概览表里的 "FB events + outcomes" 是缩写
            "purpose": (str(a.get("use_cases") or "")
                        or str(b.get("use_cases") or "")
                        or str(c.get("description") or "")
                        or str(b.get("purpose") or "")),
            "purpose_short": str(b.get("purpose") or ""),
            "description": str(c.get("description") or ""),
            "use_cases": (str(a.get("use_cases") or "")
                          or str(b.get("use_cases") or "")),
            "grain": grain,
            "pk": pk,
            "parent": str((core.get(n) or {}).get("parent") or ""),
            "keys": list((core.get(n) or {}).get("keys") or []),
            "upstream": _upstream_of(b, (core.get(n) or {}).get("parent", ""),
                                     names),
            "column_desc": coldesc,
            "metrics": dict(d.get("metrics") or {}),
            "key_columns": dict(b.get("key_columns") or {}),
            "example_sql": str(b.get("example_sql") or "")[:900],
            "row_count_text": str(b.get("row_count_text") or ""),
            # 层：主干（series→games→rounds→base_events）/ 派生（DERIVED_TABLES
            # 里声明的 7 张）/ 参考字典 / 其余走命名约定的聚合表
            "layer": _layer_of(n, core, b),
        }

    h = hashlib.sha1()
    for p in MODEL_FILES:
        try:
            h.update(p.read_bytes())
        except Exception:
            h.update(str(p).encode("utf-8"))
    return {
        "tables": tables,
        "fingerprint": h.hexdigest()[:16],
        "files": [str(p.relative_to(VLML_ROOT)) for p in MODEL_FILES],
    }


async def discover() -> dict[str, Any]:
    tables = await discover_tables()
    known = set(tables)
    return {
        "tables": tables,
        "lineage": discover_lineage(known),
        "insights": discover_insights(known),
        "tools": discover_tools(),
        "section_insights": section_insights(),
    }


def _main() -> int:
    argv = sys.argv[1:]
    data = asyncio.run(discover())
    if "--json" in argv:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    tables = data["tables"]
    print(f"表 {len(tables)} 张（实查 information_schema）")
    by_stage: dict[str, list[str]] = {}
    for name, info in tables.items():
        by_stage.setdefault(info["stage"], []).append(name)
    for st in STAGE_ORDER:
        if st not in by_stage:
            continue
        print(f"\n[{st}] {STAGE_LABEL.get(st, st)}")
        for name in sorted(by_stage[st]):
            t = tables[name]
            print(f"  {name:28} {t['grain']:14} {t['rows']:>7} 行 "
                  f"{len(t['columns'])} 列")
    print(f"\n派生关系（transformations 解析）")
    for target, ups in sorted(data["lineage"].items()):
        print(f"  {target:28} ← {', '.join(ups)}")
    ins = data["insights"]
    print(f"\n洞察 {len(ins)} 个（tools/sql 解析）")
    if "--tables" not in argv:
        for name, info in sorted(ins.items())[:12]:
            print(f"  {name:30} 读 {', '.join(info['tables']) or '（无）'}"
                  + (f"  [{','.join(info['reports'])}]" if info["reports"] else ""))
    print(f"\n工具 {len(data['tools'])} 个（reports 解析 return 结构）")
    for name, info in sorted(data["tools"].items()):
        print(f"  {name:26} sections: {', '.join(info['sections'][:8])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
