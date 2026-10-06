#!/usr/bin/env python3
"""自动出题（照伴学 `question_generate` + `question_validate` 的四段闭环）。

伴学的四段（逐段取证）
----------------------
1. `question_type_mapping.resolve_target_question_type`（:131）
   —— 题型由**服务端**定：种子里的 teaching style → `MACHINE_QUESTION_TYPES`。
   注释写得很直白："a teaching style is pedagogical data, not an LLM-selected
   output format"。LLM 不许自己选题型。
2. `tutor_llm_agent_question_generate._normalize_question`（:46）
   —— LLM 出题面，**同时产出** `answer` / `accepted_answers` / `key_points` /
   `rubric` / `solution_steps` / `difficulty`。
3. `tutor_llm_agent_question_validate._normalize_question_validation`（:18）
   —— **第二个 LLM 调用**独立校验三个布尔：`relevant` / `answer_supported` /
   `difficulty_appropriate`，外加 `reason`；`retry` 必须与三者一致，
   不一致直接 `SdkError`。不通过就重新生成。
4. `question_type_mapping.enforce_mapped_question_type`（:259）
   —— 最后强制把 `question_type` 改回服务端选定的，防 LLM 改题型。

MVE 的关键改造：生成的是**配方**，不是答案
------------------------------------------
伴学把 LLM 给的 `answer` 当标准答案（`accepted_answers`）。
MVE **绝不能**这么做 —— `loop_core.py:165` 写死了：expected 与 tolerance
只从服务端私有的 `answer_spec` 读，绝不用模型生成的参考答案替换。

我上一轮据此推出「MVE 不能自动生成题目，硬边界」——**这个推论是错的**。
错在把「不能信模型给的答案数值」等同于「不能让模型出题」。
正确做法：让 LLM 出的是**可执行的取数配方**（`answer_spec`：
SQL，或 工具 + 参数 + 取值路径），而不是答案数值。

配方是可执行的 → 裁判跑一遍就从 VLML 拿到真值。于是伴学第 3 步里
「由第二个 LLM 判断的 `answer_supported`」，在 MVE 里变成**确定性验证**：

    跑得出来吗？（执行不报错）
    值非空吗？（不是 NULL / 空串）
    样本量够吗？（base ≥ min_base，照 base 闸）
    跟已有题重复吗？（同一个 (subject, dimension) 已经有了就作废）

**这比伴学更强**：伴学的"答案是否被支持"是另一个模型的意见（还会自相矛盾，
所以它才要加 `_answer_reference_answer_consistent` 那种一致性标志）；
MVE 的"这道题成不成立"是数据事实，跑一遍就知道。

用法
----
    python mve/question_gen.py --about "Cloud9 的手枪局" --difficulty 3
    python mve/question_gen.py --about "首血转换" --adopt     # 验过就并入题库
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import vlml_env  # noqa: E402  引导环境

from llm_client import chat_json  # noqa: E402
from loop_core import AnswerSpec, RubricPoint, Task, dig  # noqa: E402

# 生成的题落这里（运行时产物，不进仓库；tasks.py 在启动时合并）
STORE = HERE / "generated_tasks.json"

# 与 action_code.TOOL_NAMES 保持一致（对外暴露的 MCP 工具）
TOOL_NAMES = [
    "match_summary_report", "match_analysis_report", "match_players_report",
    "match_rounds_report", "match_economy_report", "pattern_detection_report",
    "player_profile_report", "scouting_report", "query_sql", "get_database_info",
]

SERIES = "2843069"
C9 = "Cloud9"


# --------------------------------------------------------------------------
# 第 0 段：口径声明（服务端事实，不由模型决定）
# --------------------------------------------------------------------------
_PCT_NAME = ("_pct", "_rate", "_conv", "_percent", "pct_")


def _dim_specs() -> dict[str, dict[str, Any]]:
    """每个维度是**什么量**、**要不要分母** —— 服务端声明，不从模型来。

    伴学不让学生/模型选题型（"a teaching style is pedagogical data, not an
    LLM-selected output format"）；MVE 同理：维度是百分比还是计数、分母从哪来，
    是服务端事实。

    来源：现有题的 `answer_spec`（硬事实），维度名只作兜底。

    ⚠️ 两条路径的 percent 语义**不一样**（这是裁判既有行为，不是 bug）：
      · 工具路径 + `percent=True` → value 是**分子**、base 是**分母**，
        裁判自己算 `num/denom*100`（`vlml0_referee.py:224`）。
      · SQL 路径 → value 就是**最终值**（SQL 里已经 `ROUND(AVG(...)*100,1)`），
        base 只是样本量，裁判**不换算**（`vlml0_referee.py:265`）。

    不区分这两条，就会出「题面问胜率、配方取个数」的废题 —— 实测第 2 次生成
    就是这么过的：`pistol.num` = 3 被当成胜率，真值是 3/6 = 50%。
    """
    out: dict[str, dict[str, Any]] = {}
    try:
        from tasks import TASKS
    except Exception:                                        # pragma: no cover
        return out
    try:
        import knowledge_graph
        g = knowledge_graph.KnowledgeGraph.load()
    except Exception:                                        # pragma: no cover
        g = None
    for t in TASKS.values():
        for p in t.rubric:
            d = str(p.dimension or "")
            if not d:
                continue
            s = getattr(p, "answer_spec", None)
            rec = out.setdefault(d, {
                "kind": "count", "needs_base": False, "example": "",
                "by_tool": False, "by_sql": False,
            })
            if s is None:
                continue
            if s.tool:
                rec["by_tool"] = True
            elif s.sql:
                rec["by_sql"] = True
            if bool(getattr(s, "percent", False)):
                rec["kind"] = "percent"
                rec["needs_base"] = True
                if s.tool and s.base_path:
                    rec["example"] = f"base_path={s.base_path}"
                elif s.sql and s.base_column is not None:
                    rec["example"] = f"SQL 第 {s.base_column} 列作分母/样本量"
    for d, rec in out.items():
        pt = ""
        if g is not None:
            n = g.nodes.get(f"dim:{d}")
            pt = str((n.detail.get("point") if n else "") or "")
        # 只取括号前的主名：「手枪局胜率（0-100 的百分比数值，如 50.0）」→ 手枪局胜率
        rec["desc"] = (pt.split("（")[0].split("(")[0].strip() or d)
        # 维度在图谱里的取值路径 —— 用来**按配方反查维度**（见 enforce）
        vp = ""
        if g is not None:
            n = g.nodes.get(f"dim:{d}")
            vp = str((n.detail.get("value_path") if n else "") or "")
        rec["value_path"] = vp
        rec["producers"] = (list(g.who_produces(d)) if g is not None else [])
        if rec["kind"] != "count":
            continue
        if any(k in d for k in _PCT_NAME):
            # 裁判判 unit 时本来就按名字判（referee.py:278），这里照它
            rec["kind"] = "percent"
            rec["needs_base"] = True
        elif "ratio" in d:
            rec["kind"] = "ratio"
        elif "confidence" in d:
            rec["kind"] = "label"
    return out


_STOP = {"这个", "那个", "什么", "多少", "分别", "计算", "一共", "总共", "请问",
         "数据", "指标", "数值", "结果", "情况", "表现", "the", "and", "for",
         "with", "that", "this", "from", "team", "series"}


def _relevant(point: str, desc: str) -> bool:
    """题面与维度定义是否相关 —— 伴学的 `relevant`，但**确定性**地判。

    伴学这一步是**第二个 LLM 的意见**（所以它还得再加一致性标志防自相矛盾）。
    MVE 用关键词重合：
      · 维度定义「手枪局胜率」+ 题面「Cloud9 的手枪局胜率」   → 相关 ✅
      · 维度定义「置信度标签」+ 题面「Cloud9 的强起局胜率」   → 不相关 ❌
    实测第三轮就是第二种：模型把强起局胜率的 dimension 填成了
    `pattern_confidence`，跑得出值（"moderate"）、维度也合法 ——
    只有这道闸能挡住它。
    """
    def toks(t: str) -> set[str]:
        en = {w for w in re.findall(r"[a-z_]{4,}", t.lower())}
        zh = "".join(re.findall(r"[\u4e00-\u9fff]+", t))
        grams = {zh[i:i + 2] for i in range(max(0, len(zh) - 1))}
        return (en | grams) - _STOP
    return bool(toks(point) & toks(desc))


def _suggest_dims(point: str, specs: dict[str, dict[str, Any]],
                  limit: int = 3) -> list[str]:
    """按题面文案推荐最像的几个维度 —— 只说「换一个维度」模型换不对。"""
    hits = [d for d, r in specs.items()
            if _relevant(point, str(r.get("desc") or d))]
    return sorted(hits)[:limit]


def _dim_tag(d: str, specs: dict[str, dict[str, Any]]) -> str:
    """维度后面那句口径声明（直接进 prompt，让模型一次写对，别靠重试）。"""
    rec = specs.get(d) or {}
    kind = str(rec.get("kind") or "count")
    if kind == "percent":
        ex = str(rec.get("example") or "")
        if rec.get("by_tool") and not rec.get("by_sql"):
            return ("【百分比：走工具时必须 percent=true，value_path 指 .num、"
                    f"base_path 指 .denom{f'（现有题 {ex}）' if ex else ''}】")
        if rec.get("by_sql") and not rec.get("by_tool"):
            return ("【百分比：走 SQL 时 value 列必须已经是算好的百分比"
                    "（如 ROUND(AVG(...)*100,1)），分母列作样本量"
                    f"{f'（现有题 {ex}）' if ex else ''}】")
        return "【百分比：必须同时给分母】"
    if kind == "ratio":
        return "【比值：不要给分母】"
    if kind == "label":
        return "【枚举标签：值照抄工具返回的字段，不要算】"
    return "【计数：整数】"


# --------------------------------------------------------------------------
# 第 1 段：口径骨架由**服务端**给（照 resolve_target_question_type）
# --------------------------------------------------------------------------
# SQL 路径只暴露这几张表（其余表列太多，全给会把 prompt 撑爆）
_SQL_TABLES = ("rounds", "games", "series", "agg_first_blood_stats",
               "agg_team_game_stats", "agg_team_series_stats",
               "agg_team_map_stats", "agg_player_series_stats")
_COLS_CACHE: dict[str, list[str]] = {}
# 列多于此数就只留「跟口径有关」的列
_KEEP = ("win", "rate", "round", "kill", "death", "kast", "adr", "map", "team",
         "series", "pistol", "eco", "fb_", "clutch", "plant", "defuse",
         "streak", "score", "side", "_id", "name")


async def _sql_catalog() -> str:
    """SQL 路径的真实表/列（从 information_schema 拉，绝不手写）。

    跟工具签名同理：**不把真实列名给它，它只能猜**。
    实测不给表结构时，模型写出的 SQL 有三分之一返回 0 行 —— 不是逻辑错，
    是它用了不存在的列名。

    列名顺便缓存进 `_COLS_CACHE`：验题报「0 行」时要用它**点名**哪个标识符
    不是列，光说「照抄表结构」模型改不动（实测连改三轮都加同一个不存在的列）。
    """
    global _COLS_CACHE
    inlist = ", ".join(f"'{t}'" for t in _SQL_TABLES)
    try:
        res = await vlml_env.execute_custom_sql(
            "SELECT table_name, column_name FROM information_schema.columns "
            f"WHERE table_name IN ({inlist}) ORDER BY table_name, ordinal_position")
    except Exception as e:                                   # pragma: no cover
        return f"（表结构探测失败：{type(e).__name__}）"
    from collections import defaultdict
    cols: dict[str, list[str]] = defaultdict(list)
    for t, c in (res.get("rows") or []):
        cols[str(t)].append(str(c))
    _COLS_CACHE = {str(t): list(cs) for t, cs in cols.items()}
    blocks = []
    for t in _SQL_TABLES:
        cs = cols.get(t) or []
        if len(cs) > 25:
            cs = [c for c in cs if any(k in c.lower() for k in _KEEP)]
        blocks.append(f"  {t}({', '.join(cs)})")
    return "\n".join(blocks)


_SQL_KEYS = frozenset("""
select from where and or as count sum avg round case when then else end
group order by limit in not null is on join left inner distinct min max
having asc desc between like cast integer double varchar true false
""".split())


def _bad_columns(sql: str, table: str = "") -> list[str]:
    """SQL 里那些**不是目标表列名**的标识符 —— 多半是模型自己造的列。

    ⚠️ 两个坑（都踩过）：
      · 表名本身不能算坏列 —— 不然 `FROM rounds` 会把 `rounds` 报成坏列。
      · 必须**只对着 FROM 那张表判**，不能用所有表的并集：`team_name` 在
        agg_team_game_stats 里存在，但在 rounds 里没有，用并集就漏掉了它。
    """
    if not _COLS_CACHE:
        return []
    pool = set(_COLS_CACHE.get(table) or []) if table else set()
    allcols: set[str] = set()
    for cs in _COLS_CACHE.values():
        allcols |= set(cs)
    tables = set(_COLS_CACHE) | set(_SQL_TABLES)
    # 先去掉字符串字面量，否则 'Cloud9' / 'Lotus' 会被当成标识符
    stripped = re.sub(r"'[^']*'", " ", sql or "")
    toks = set(re.findall(r"[a-z_][a-z0-9_]*", stripped.lower()))
    cand = [t for t in toks
            if t not in tables and t not in _SQL_KEYS and len(t) > 3]
    if pool:
        bad = [t for t in cand if t not in pool]
    else:
        bad = [t for t in cand if t not in allcols]
    return sorted(bad)


async def _server_skeleton() -> str:
    """图谱 + **真实工具签名与返回结构**给出的「能问什么」。

    伴学不让 LLM 选题型（那是 pedagogical data）；MVE 同理：
    **哪些维度存在、从哪出、工具怎么传参**，全部由服务端声明，不由模型想象。

    为什么必须连签名和结构一起给：实测第一次跑，模型出的
    `pattern_detection_report → key_metrics.economy.pistol.num` 是**真路径**
    （现有题就是这么取的），却因为没传 `team_name` 拿到 `{"error": ...}`，
    被验题判成"无解"。跟 `action_code` 那条路的教训完全一样：
    **不把真实签名和返回结构给它，它只能猜参数名**。
    """
    import action_code
    specs = _dim_specs()
    lines: list[str] = []
    try:
        import knowledge_graph
        g = knowledge_graph.KnowledgeGraph.load()
        for d in sorted(g.dimensions()):
            prod = g.who_produces(d)
            node = g.nodes.get(f"dim:{d}")
            vp = str((node.detail.get("value_path") if node else "") or "")
            cols = (node.detail.get("value_columns") if node else None) or []
            tag = _dim_tag(d, specs)
            if prod and vp:
                lines.append(f"  {d} → 工具 {'/'.join(prod)}，路径 {vp} {tag}")
            elif prod:
                lines.append(f"  {d} → 工具 {'/'.join(prod)} {tag}")
            elif cols:
                lines.append(f"  {d} → 需自己写 SQL，取第 {cols[0]} 列 {tag}")
            else:
                lines.append(f"  {d} {tag}")
    except Exception as e:                                   # pragma: no cover
        lines.append(f"（图谱不可用：{type(e).__name__}）")

    # 真实签名（从函数反射，绝不手写）
    sigs = "\n".join(f"  await mcp.{s}"
                     for s in action_code.TOOL_SIGNATURES.values())
    # 真实返回结构（probe 缓存）
    try:
        schemas = await action_code.probe_schemas(list(action_code.TOOL_SIGNATURES))
        blocks = []
        for t, paths in schemas.items():
            shown = "\n".join(f"    {p}" for p in paths[:40])
            blocks.append(f"  {t} 返回结构（前 {min(40, len(paths))} 条）：\n{shown}")
        struct = "\n".join(blocks)
    except Exception as e:                                   # pragma: no cover
        struct = f"（结构探测失败：{type(e).__name__}）"

    dims = sorted(specs)
    return ("""维度（服务端声明）：
""" + "\n".join(lines) + f"""

★ dimension 字段**只能**从下面这个枚举里挑一个，一字不改地填。
  不要填工具名、不要填中文、不要自己造（实测模型会把工具名当成维度填进去）：
""" + ", ".join(dims) + """

工具真实签名（照抄，参数名写错就拿不到数据）：
""" + sigs + """

工具返回结构（叶子路径，**路径必须照抄**）：
""" + struct + """

SQL 路径可用的表与**真实列名**（列名写错就是 0 行）：
""" + await _sql_catalog())


_PROPOSE_SYSTEM = """你是出题 agent，为「数据分析 agent」出一道**可以用数据回答**的题。

硬规则（违反就是坏题）：
1. 你出的**不是答案**，是**取数配方**。每个评分点必须给出下列之一：
   a) `sql`：一条以 SELECT 开头的 SQL（duckdb，不支持 CTE），并说明取第几列；
   b) `tool` + `tool_args` + `value_path`：调哪个工具、传什么参数、按哪条点分路径取值。
2. **绝对不要**在返回里写答案的数值。数值由服务端跑你的配方去拿 ——
   你写的数字一律视为幻觉，写了就判废。
3. 只能使用下面「能问什么」里列出的维度/工具/表。不要发明不存在的字段。
4. 只出能在本场数据（series_id={SERIES}）里取到值的题。

能问什么（服务端声明）：
{skeleton}

可用工具：{tools}

输出 JSON：
{{
  "topic_id": "英文小写下划线短 id",
  "question": "题面（中文，含口径说明：队伍整体还是某队、哪张图、什么分母）",
  "difficulty": 1-4,
  "requires_tools": ["用到的工具"],
  "rubric": [
    {{
      "point": "评分点文案",
      "subject": {{"series": "{SERIES}", "team": "{C9}"}},
      "dimension": "从上面 ★ 枚举里挑一个，如 pistol_win_rate",
      "weight": 50,
      "min_base": 1,
      "answer_spec": {{
        "sql": "SELECT ...", "value_column": 0, "base_column": null,
        "numeric_tolerance": 0.05
      }}
    }}
  ]
}}
subject 的键只能是 series / team / map / player 这类真实维度，不要编。
★ subject 必须把题面里的**限定条件全写进去**：题面提到某张图就写
  "map": "Lotus"，提到某个队员就写 "player": "名字"。
  只写 series + team 两个键（漏掉 map / player）会撞上已有的题被判废，
  这是最常见的废题原因。

口径规则（服务端定，错了就是废题）—— 两条路径的百分比**不是一回事**：
· 走**工具**且维度标【百分比】：必须 "percent": true，
  value_path 指到 .num、base_path 指到 .denom。裁判会自己算 num/denom*100。
  ★ 只给 .num 不给 .denom = 问胜率却取了个数 = 废题。
· 走 **SQL** 且维度标【百分比】：value_column 那一列必须**已经是算好的百分比**
  （写成 ROUND(AVG(CASE WHEN ... THEN 1.0 ELSE 0.0 END)*100, 1)），
  base_column 指到样本量那一列。裁判**不会再换算**。
  ★ 只写 COUNT(*) 当分子 = 废题。
· 标【计数】【比值】的维度不要给分母；标【枚举标签】的值照抄工具返回。
"""


async def propose(*, about: str = "", difficulty: int = 2,
                  critique: str = "", avoid: str = "") -> dict[str, Any]:
    """第 2 段：LLM 出题面 + 取数配方（**不含答案数值**）。

    `critique` 是**上一轮验题的失败原因**，原样喂回去 —— 这是原版 Voyager 的
    iterative prompting / critic，也是 `action_code.collect()` 里已经验证有效
    的那一招：不给理由的重试，模型只会换一种方式犯同一个错。

    `avoid` 是**已经出过的 (subject, dimension) 清单**。事前给比事后挡便宜：
    实测不给的话，模型每轮都撞已有题，白烧两次调用。
    """
    user = (f"围绕「{about or '本场比赛'}」出一道题，难度 {difficulty}。"
            "记住：给取数配方，不要给答案数值。")
    if avoid:
        user += ("\n\n下面这些 (subject, dimension) 组合**已经有题了，不要再出**：\n"
                 + avoid)
    if critique:
        user += ("\n\n上一轮出的题被**确定性验题**挡下了，逐条原因如下。"
                 "必须逐条改掉再出题（不要换汤不换药）：\n" + critique)
    out = chat_json(
        [
            {"role": "system", "content": _PROPOSE_SYSTEM.format(
                skeleton=await _server_skeleton(), tools=", ".join(TOOL_NAMES),
                SERIES=SERIES, C9=C9)},
            {"role": "user", "content": user},
        ],
        temperature=0.4,
    )
    return out if isinstance(out, dict) else {}


# --------------------------------------------------------------------------
# 第 3 段：确定性验题（伴学的 answer_supported 由第二个 LLM 判 —— 这里用数据）
# --------------------------------------------------------------------------
async def _run_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """跑一遍取数配方，返回 {value, base, error}。**这是验题的全部依据**。"""
    sql = str(spec.get("sql") or "").strip()
    if sql:
        if not sql.lower().startswith("select"):
            return {"error": "sql 必须以 SELECT 开头"}
        try:
            res = await vlml_env.execute_custom_sql(sql_query=sql)
        except Exception as e:
            return {"error": f"SQL 执行失败：{type(e).__name__}: {str(e)[:120]}"}
        rows = (res or {}).get("rows") or []
        if not rows:
            return {"error": "SQL 返回 0 行 —— 这道题在本场数据里无解"}
        row = rows[0]
        vc = int(spec.get("value_column") or 0)
        if vc >= len(row):
            return {"error": f"value_column={vc} 超出返回列数 {len(row)}"}
        value = row[vc]
        base = None
        bc = spec.get("base_column")
        if bc is None and len(row) >= 2:
            # 模型常算出「值 + COUNT(*) 样本量」两列，却忘了填 base_column
            # （实测第 3 轮就差这一个字段）。服务端能看到列数，就替它补上：
            # 取不是 value_column 的最后那一列。
            others = [i for i in range(len(row)) if i != vc]
            if others:
                bc = others[-1]
        if bc is not None and int(bc) < len(row):
            base = row[int(bc)]
        return {"value": value, "base": base, "base_column": bc}

    tool = str(spec.get("tool") or "").strip()
    if tool:
        fn = getattr(vlml_env, tool, None)
        if fn is None:
            return {"error": f"没有工具 {tool}"}
        args = dict(spec.get("tool_args") or {})
        try:
            res = await fn(**args)
        except Exception as e:
            return {"error": f"工具调用失败：{type(e).__name__}: {str(e)[:120]}"
                             f"（传的参数是 {args}）"}
        if isinstance(res, dict) and "error" in res and len(res) <= 2:
            # 实测：pattern_detection_report 不传 team_name 就返回 {"error": ...}，
            # 那时任何路径都取不到 —— 必须说清是**参数**的问题，不是路径的问题。
            return {"error": f"工具返回错误 {str(res.get('error'))[:80]}"
                             f" —— 多半是必填参数没传（当前传的是 {args}）"}
        vp = str(spec.get("value_path") or "")
        if not vp:
            return {"error": "走工具路径却没有给 value_path"}
        value = dig(res, vp)
        bp = str(spec.get("base_path") or "")
        base = dig(res, bp) if bp else None
        got = {"value": value, "base": base}
        if _empty(value):
            got["hint"] = ("返回里有这些顶层段："
                           + ", ".join(list((res or {}).keys())[:8]))
        return got

    return {"error": "answer_spec 既没 sql 也没 tool"}


_ROUNDS_TOTAL: int | None = None


async def _max_rounds() -> int:
    """全场总回合数 —— 计数型维度的量纲上限。

    为什么需要它：实测模型写过 `SELECT SUM(round_number) FROM rounds ...`，
    把**回合号求和**当成"打了多少回合"，跑出 145 —— 非空、能跑、维度也合法，
    前面六道闸全过。只有"值不能超过全场总回合数"这道闸能挡住它。
    """
    global _ROUNDS_TOTAL
    if _ROUNDS_TOTAL is None:
        try:
            r = await vlml_env.execute_custom_sql(
                f"SELECT COUNT(*) FROM rounds WHERE series_id='{SERIES}'")
            _ROUNDS_TOTAL = int((r.get("rows") or [[59]])[0][0])
        except Exception:                                    # pragma: no cover
            _ROUNDS_TOTAL = 59
    return _ROUNDS_TOTAL


def _fix_for_error(err: str, sql: str = "") -> str:
    """把执行错误翻译成**可执行的修法**（只报症状模型改不动，实测三轮都改不动）。

    跟 `action_code.collect()` 里那条 critic 同构：光说「你写的这条不在返回里」
    没用，必须把真实候选路径一起给出去。
    """
    if "0 行" in err or "为空" in err:
        # 先判表名：实测模型最爱把**工具名**当表名（match_rounds_report 之类），
        # 光说"照抄表结构"它只会换一个工具名继续错。
        m = re.search(r"\bfrom\s+([a-z_][a-z0-9_]*)", (sql or "").lower())
        tbl = m.group(1) if m else ""
        if tbl and _COLS_CACHE and tbl not in _COLS_CACHE:
            return (f"FROM 里的 `{tbl}` 不是表名（那是**工具名**，工具只能走 tool 路径，"
                    f"不能放进 FROM）。SQL 只能查这些表：{', '.join(_SQL_TABLES)}。"
                    f"\n  rounds 表的列：{', '.join(_COLS_CACHE.get('rounds', [])[:16])}")
        bad = _bad_columns(sql, tbl)
        if bad:
            # 点名造出来的列，并给出它 FROM 的那张表的真实列
            near = _COLS_CACHE.get(tbl) or _COLS_CACHE.get("rounds") or []
            tip = ""
            if any("team" in b for b in bad):
                tip = (" rounds 表没有 team_name 列，筛队伍用 winning_team_name；"
                       "要按队伍统计去 agg_team_game_stats。")
            return (f"SQL 里这些不是 `{tbl or '目标表'}` 的列：{', '.join(bad)}。"
                    f"{tip}\n  `{tbl or 'rounds'}` 的真实列：{', '.join(near[:18])}")
        return ("SQL 返回 0 行：表名与列名必须照抄 ★ 表结构清单；"
                f"WHERE 里 series_id 用 '{SERIES}'，条件别加太死。")
    if "必填参数" in err or "工具返回错误" in err:
        return "把必填参数补进 tool_args（照「工具真实签名」里的参数名）。"
    if "取不到值" in err or "没有给 value_path" in err:
        return "value_path 必须照抄「工具返回结构」里的叶子路径（不要带前导点）。"
    if "没有工具" in err:
        return "tool 必须从「可用工具」清单里选。"
    if "clamp" in err or "value_column" in err:
        return "value_column 指的是 SELECT 的第几列（从 0 开始），别超出列数。"
    return ("照「工具真实签名」「表结构」「返回结构」逐字核对，"
            "不要自己造表名、列名、字段名。")


def _empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


async def validate(candidate: dict[str, Any], *,
                   existing: set[tuple[str, str]] | None = None) -> dict[str, Any]:
    """验题。**不问模型**，只问数据。

    返回 {ok, checks, reasons}。`checks` 逐条列出每个评分点的验证结果 ——
    验题结论必须可复盘，不能只给一个布尔。
    """
    existing = existing or set()
    specs = _dim_specs()
    reasons: list[str] = []
    checks: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()      # 题内去重（现有题靠 subject 区分图）

    if not str(candidate.get("question") or "").strip():
        return {"ok": False, "checks": [], "reasons": ["没有题面"]}
    rubric = candidate.get("rubric")
    if not isinstance(rubric, list) or not rubric:
        return {"ok": False, "checks": [], "reasons": ["没有评分点"]}

    for rp in rubric:
        point = str(rp.get("point") or "").strip() or "（无文案）"
        dim = str(rp.get("dimension") or "").strip()
        subj = rp.get("subject") if isinstance(rp.get("subject"), dict) else {}
        spec = rp.get("answer_spec")
        row: dict[str, Any] = {"point": point, "dimension": dim}

        if not dim:
            row["ok"] = False
            row["why"] = "没有 dimension"
            checks.append(row)
            reasons.append(f"「{point}」没有 dimension")
            continue
        key = (json.dumps(subj, sort_keys=True, ensure_ascii=False), dim)
        if key in seen:
            # 题内撞车：现有题靠 subject 区分图（Corrode/Haven/Lotus 各一个
            # map_rounds），所以同题里出现两个一样的 (subject, dimension)
            # 就说明模型把两个评分点写成同一条查询了。
            row["ok"] = False
            row["fix"] = ("这道题里已经有同样的 (subject, dimension) 了。"
                          "两个评分点必须用不同的维度，或用 subject 区分"
                          "（如加 map / player 键）。")
            row["why"] = "与本题另一个评分点重复"
            checks.append(row)
            reasons.append(f"「{point}」与本题另一评分点重复（{dim}）")
            continue
        seen.add(key)
        if key in existing:
            row["ok"] = False
            row["fix"] = (f"这个 (subject, dimension) 已经有题了："
                          f"subject={json.dumps(subj, ensure_ascii=False)}、"
                          f"dimension={dim}。把 subject 写细一点（加上 map 或 "
                          "player 键），或者换一个维度。")
            row["why"] = "与已有题重复的 (subject, dimension)"
            checks.append(row)
            reasons.append(f"「{point}」与已有题重复（{dim}）")
            continue
        if not isinstance(spec, dict):
            row["ok"] = False
            row["why"] = "没有 answer_spec（不能信模型给的答案数值）"
            checks.append(row)
            reasons.append(f"「{point}」没有取数配方")
            continue
        # 模型偷写答案数值 → 直接判废（伴学接受它，MVE 不接受）
        if any(spec.get(k) is not None for k in ("expected", "expected_value",
                                                 "answer", "value")):
            row["ok"] = False
            row["why"] = "配方里夹带了答案数值 —— 判废"
            checks.append(row)
            reasons.append(f"「{point}」配方里夹带答案数值")
            continue

        # 闸 0：维度必须是服务端声明过的（模型不许发明口径）
        if dim not in specs:
            row["ok"] = False
            row["fix"] = (f"dimension 必须从 ★ 枚举里选一个（现在填的 {dim} 不在里面）。"
                          f"可用：{', '.join(sorted(specs))}")
            row["why"] = f"维度 {dim} 不在服务端声明的口径里"
            checks.append(row)
            reasons.append(f"「{point}」维度 {dim} 不在服务端声明里")
            continue
        # 闸 0b：题面与维度定义必须相关（伴学的 relevant，这里用关键词重合判）
        dspec = specs[dim]
        desc = str(dspec.get("desc") or dim)
        if not _relevant(point, desc):
            guess = _suggest_dims(point, specs)
            row["ok"] = False
            row["fix"] = (f"dimension={dim} 的服务端定义是「{desc}」，跟题面对不上。"
                          + (f"按题面看，更可能是：{', '.join(guess)}。"
                             if guess else "point 文案必须围绕该维度的定义写。"))
            row["why"] = f"题面与维度不相关（该维度的服务端定义是「{desc}」）"
            checks.append(row)
            reasons.append(f"「{point}」与维度 {dim}（定义：{desc}）不相关")
            continue
        # 闸 1：百分比口径必须给分母（挡「问胜率却取个数」）
        wants_pct = str(dspec.get("kind")) == "percent" and bool(dspec.get("needs_base"))
        if wants_pct:
            # 实际走哪条路径，以 `_run_spec` 的判断为准（有 sql 就走 SQL）
            by_tool = not str(spec.get("sql") or "").strip() and \
                bool(str(spec.get("tool") or "").strip())
            has_base = bool(str(spec.get("base_path") or "").strip()) or \
                spec.get("base_column") is not None
            # SQL 路径允许**运行时补**（模型算出两列却忘填 base_column 是家常便饭，
            # 服务端看得到列数，能替它补），所以这里不预检，跑完再看补没补上。
            # 工具路径没有"列"的概念、推断不了，必须在 spec 里声明 base_path。
            if by_tool and not has_base:
                row["ok"] = False
                row["fix"] = "走工具：补 base_path 指到同层的 .denom。"
                row["why"] = "百分比口径却没给分母（问胜率却取了个数）"
                checks.append(row)
                reasons.append(f"「{point}」百分比口径缺分母")
                continue
            if by_tool and not bool(spec.get("percent")):
                row["ok"] = False
                row["fix"] = "工具路径的百分比口径必须写 \"percent\": true。"
                row["why"] = "工具路径的百分比口径必须写 percent=true"
                checks.append(row)
                reasons.append(f"「{point}」工具路径缺 percent=true")
                continue

        got = await _run_spec(spec)
        if got.get("error"):
            row["ok"] = False
            row["fix"] = _fix_for_error(str(got["error"]),
                                        str(spec.get("sql") or ""))
            row["why"] = got["error"]
            checks.append(row)
            reasons.append(f"「{point}」{got['error']}")
            continue
        # 百分比口径跑完必须拿得到分母/样本量（SQL 路径的自动补在这里兑现）
        if wants_pct and _empty(got.get("base")):
            row["ok"] = False
            row["fix"] = (
                "走工具：base_path 要指到同层的 .denom。"
                if str(spec.get("tool") or "").strip() else
                "走 SQL：SELECT 要返回两列 —— 百分比值 + COUNT(*) 作样本量，"
                "并把 base_column 指到样本量那一列。")
            row["why"] = "百分比口径取不到分母/样本量"
            checks.append(row)
            reasons.append(f"「{point}」百分比口径取不到分母")
            continue
        if _empty(got.get("value")):
            why = "配方跑出来是空 —— 这道题在本场数据里无解"
            if got.get("hint"):
                why += f"（{got['hint']}）"
            row["ok"] = False
            row["why"] = why
            checks.append(row)
            reasons.append(f"「{point}」取值为空")
            continue

        min_base = int(rp.get("min_base") or 1)
        base = got.get("base")
        if base is not None and not _empty(base):
            try:
                if float(base) < min_base:
                    row["ok"] = False
                    row["why"] = f"样本量 {base} < min_base {min_base}"
                    checks.append(row)
                    reasons.append(f"「{point}」样本量不足（{base} < {min_base}）")
                    continue
            except (TypeError, ValueError):
                pass
        # 闸 2：算出来的必须是合法百分比（工具路径 value/base*100，SQL 路径直接用）
        if wants_pct:
            v, b = got.get("value"), got.get("base")
            try:
                if str(spec.get("tool") or "").strip():
                    pct = float(v) / float(b) * 100
                else:
                    pct = float(v)
            except (TypeError, ValueError, ZeroDivisionError) as e:
                row["ok"] = False
                row["why"] = f"百分比口径算不出数值（{type(e).__name__}）"
                checks.append(row)
                reasons.append(f"「{point}」百分比口径算不出数值")
                continue
            if not (0.0 <= pct <= 100.0):
                row["ok"] = False
                row["why"] = f"百分比算出来 {round(pct, 1)} 不在 0-100 —— 口径取错了"
                checks.append(row)
                reasons.append(f"「{point}」百分比越界（{round(pct, 1)}）")
                continue
            row["truth"] = round(pct, 1)

        # 闸 3：量纲闸 —— 计数型维度的值不能超过全场总回合数
        if str(dspec.get("kind")) == "count":
            try:
                v = float(got.get("value"))
            except (TypeError, ValueError):
                pass
            else:
                cap = await _max_rounds()
                if v > cap:
                    row["ok"] = False
                    row["fix"] = (
                        f"值 {got.get('value')} 超过全场总回合数 {cap}，量纲不对。"
                        "多半是把 SUM(round_number) 当 COUNT(*) 用了 —— "
                        "数回合一律用 COUNT(*)。")
                    row["why"] = f"计数值 {got.get('value')} 超过上限 {cap}"
                    checks.append(row)
                    reasons.append(f"「{point}」量纲越界（{got.get('value')} > {cap}）")
                    continue

        row["ok"] = True
        row["why"] = "配方跑得出非空值"
        row["value"] = got.get("value")
        row["base"] = base
        checks.append(row)

    ok = bool(checks) and all(c.get("ok") for c in checks)
    return {"ok": ok, "checks": checks, "reasons": reasons}


# --------------------------------------------------------------------------
# 第 4 段：强制口径来源（照 enforce_mapped_question_type）
# --------------------------------------------------------------------------
def _norm_args(raw: Any) -> dict[str, Any]:
    """把模型可能写歪的 tool_args 规范化成 dict。

    实测模型会把 `{"team_name": "Cloud9"}` 写成 `"team_name=Cloud9"`、
    甚至写成 JSON 字符串 —— `dict(...)` 会直接抛 ValueError 把出题器打挂。
    照伴学 `enforce_mapped_question_type` 的精神：模型写歪的格式，服务端改回来。
    """
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        s = raw.strip()
        if s.startswith("{"):
            try:
                got = json.loads(s)
                return dict(got) if isinstance(got, dict) else {}
            except Exception:
                return {}
        if "=" in s:
            out: dict[str, Any] = {}
            for part in s.split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    out[k.strip()] = v.strip()
            return out
    return {}


def enforce(candidate: dict[str, Any]) -> dict[str, Any]:
    """把 LLM 可能改坏的地方改回服务端口径。

    伴学强制改回 `question_type`；MVE 强制的是**口径的归属**：
    配方里声明的工具必须在白名单里、SQL 必须 SELECT 开头、
    tool_args 必须是 dict（否则工具调用直接崩）。
    """
    out = dict(candidate)
    specs = _dim_specs()
    rubric = []
    for rp in (out.get("rubric") or []):
        if not isinstance(rp, dict):
            continue
        rp = dict(rp)
        dim = str(rp.get("dimension") or "").strip()
        rec = specs.get(dim) or {}
        spec = rp.get("answer_spec")
        if isinstance(spec, dict):
            spec = dict(spec)
            tool = str(spec.get("tool") or "").strip()
            if tool and tool not in TOOL_NAMES:
                spec.pop("tool", None)       # 不在白名单 → 去掉，验题会判废
            sql = str(spec.get("sql") or "").strip()
            if sql and not sql.lower().startswith("select"):
                spec.pop("sql", None)
                sql = ""
            if sql:
                # SQL 与工具路径**二选一**：留着两套互相矛盾的配方，裁判和验题
                # 都会按自己那套解读（实测修法提示因此走错了分支）。
                for k in ("tool", "tool_args", "value_path", "base_path"):
                    spec.pop(k, None)
            spec["tool_args"] = _norm_args(spec.get("tool_args"))
            # 路径别带前导点（模型爱写 ".key_metrics..."）
            for k in ("value_path", "base_path"):
                if str(spec.get(k) or "").startswith("."):
                    spec[k] = str(spec[k]).lstrip(".")
            # 口径归属由服务端定，不靠模型记不记得写：维度声明是百分比、
            # 配方又给了分母，那 percent 就必须是 true（照伴学
            # enforce_mapped_question_type 强制改回服务端选定值的做法）。
            if str(rec.get("kind")) == "percent" and bool(rec.get("needs_base")):
                has_base = bool(str(spec.get("base_path") or "").strip()) or \
                    spec.get("base_column") is not None
                if has_base:
                    spec["percent"] = True
            rp["answer_spec"] = spec
        # 维度填错但**配方是对的** → 按取值路径反查，服务端改回来。
        # 实测模型三轮都把 pistol 的手枪局题填成 kast_pct，而它的 value_path
        # 写的却是 key_metrics.economy.pistol.num —— 路径是硬事实，
        # 比让模型猜维度可靠得多（照伴学 enforce_mapped_question_type：
        # 模型写歪的归属，服务端强制改回）。
        desc0 = str(rec.get("desc") or dim)
        if dim and str(rp.get("point") or "") and not _relevant(
                str(rp.get("point") or ""), desc0):
            vpath = str((spec or {}).get("value_path") or "")
            if vpath:
                for d2, r2 in specs.items():
                    if d2 != dim and str(r2.get("value_path") or "") == vpath:
                        rp["dimension"] = d2
                        dim = d2
                        rec = r2
                        break
        rp["answer_spec"] = spec if isinstance(spec, dict) else rp.get("answer_spec")

        # 只在**工具路径**下纠正工具与参数（走 SQL 时别塞一套矛盾的配方进来）
        if isinstance(spec, dict) and not str(spec.get("sql") or "").strip():
            # 工具名错：图谱说这个维度由谁生产，就用谁（实测模型把
            # pattern_detection_report 写成 match_summary_report，然后报
            # "missing 1 required positional argument: series_id"）。
            prods = [p for p in (rec.get("producers") or []) if p in TOOL_NAMES]
            if prods and str(spec.get("tool") or "") not in prods:
                spec["tool"] = prods[0]
            # 参数忘传：subject 里写着 series / team / map，服务端照签名补。
            # 「没传 team_name 就拿不到数据」是实测最高频的废题原因。
            subj = rp.get("subject") if isinstance(rp.get("subject"), dict) else {}
            args = dict(spec.get("tool_args") or {})
            try:
                import action_code
                sig = str(action_code.TOOL_SIGNATURES.get(
                    str(spec.get("tool") or ""), ""))
            except Exception:                                # pragma: no cover
                sig = ""
            for k_dst, k_src in (("series_id", "series"),
                                 ("team_name", "team"),
                                 ("map_name", "map"),
                                 ("player_name", "player")):
                if k_dst in sig and not args.get(k_dst) and subj.get(k_src):
                    args[k_dst] = subj[k_src]
            if "series_id: 'str'" in sig and not args.get("series_id"):
                args["series_id"] = SERIES
            spec["tool_args"] = args
            if str(rec.get("kind")) == "percent" and bool(rec.get("needs_base")):
                has_base = bool(str(spec.get("base_path") or "").strip()) or \
                    spec.get("base_column") is not None
                if has_base:
                    spec["percent"] = True
            rp["answer_spec"] = spec

        rubric.append(rp)
    out["rubric"] = rubric
    out["requires_tools"] = [
        t for t in (out.get("requires_tools") or []) if t in TOOL_NAMES]
    return out


# --------------------------------------------------------------------------
# 落盘 / 载入
# --------------------------------------------------------------------------
def load_generated() -> list[dict[str, Any]]:
    try:
        raw = json.loads(STORE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def adopt(candidate: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    """验过的题落盘（带上验题证据，事后能复盘"当时为什么算通过"）。"""
    items = load_generated()
    rec = dict(candidate)
    rec["_validation"] = {
        "checks": checks,
        "adopted_at": __import__("datetime").datetime.now().isoformat(
            timespec="seconds"),
    }
    items = [it for it in items
             if str(it.get("topic_id")) != str(rec.get("topic_id"))]
    items.append(rec)
    STORE.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    return rec


def to_task(rec: dict[str, Any]) -> Task:
    """落盘的题 → Task 对象（供 tasks.py 合并进 TASKS）。"""
    rubric = []
    for rp in (rec.get("rubric") or []):
        spec = rp.get("answer_spec") if isinstance(rp.get("answer_spec"), dict) \
            else None
        aspec = None
        if spec is not None:
            aspec = AnswerSpec(
                sql=str(spec.get("sql") or ""),
                value_column=int(spec.get("value_column") or 0),
                base_column=spec.get("base_column"),
                numeric_tolerance=float(spec.get("numeric_tolerance") or 0.0),
                tool=str(spec.get("tool") or ""),
                tool_args=dict(spec.get("tool_args") or {}),
                value_path=str(spec.get("value_path") or ""),
                base_path=str(spec.get("base_path") or ""),
                percent=bool(spec.get("percent")),
            )
        rubric.append(RubricPoint(
            point=str(rp.get("point") or ""),
            subject=dict(rp.get("subject") or {}),
            dimension=str(rp.get("dimension") or ""),
            weight=float(rp.get("weight") or 50),
            min_base=int(rp.get("min_base") or 1),
            answer_spec=aspec,
        ))
    return Task(
        topic_id=str(rec.get("topic_id") or ""),
        question=str(rec.get("question") or ""),
        rubric=rubric,
        difficulty=int(rec.get("difficulty") or 2),
        # 自动生成的题**默认不进掌握度**（照伴学：只有 validated 的题才计分）。
        # 它先要被人或裁判确认口径是对的 —— 生成 ≠ 生效。
        validated_target=bool(rec.get("validated_target") or False),
        min_base=1,
        requires_tools=list(rec.get("requires_tools") or []),
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="自动出题：生成取数配方 + 确定性验题")
    ap.add_argument("--about", default="", help="围绕什么出题")
    ap.add_argument("--difficulty", type=int, default=2)
    ap.add_argument("--tries", type=int, default=3, help="最多生成几次")
    ap.add_argument("--adopt", action="store_true", help="验过就并入库")
    args = ap.parse_args()

def existing_pairs() -> tuple[set[tuple[str, str]], str]:
    """已有题的 (subject, dimension) 集合 + 给模型看的清单。"""
    try:
        from tasks import TASKS
    except Exception:                                        # pragma: no cover
        return set(), ""
    pairs = {
        (json.dumps(p.subject, sort_keys=True, ensure_ascii=False),
         str(p.dimension))
        for t in TASKS.values() for p in t.rubric
    }
    text = "\n".join(
        f"  - {json.dumps(json.loads(s), ensure_ascii=False)} × {d}"
        for s, d in sorted(pairs))
    return pairs, text


def _critique_of(vr: dict[str, Any]) -> str:
    """把验题结果翻成「错在哪 + 怎么改」（伴学的 `generation_feedback`）。

    伴学：`entry_tutor_question_entries.py:1885` 把上一轮的 `validation_failure`
    原样塞进 `generation_feedback`。MVE 同构，但多给一句「怎么改」——
    只报症状实测三轮都改不动（模型会换一种方式犯同一个错）。
    """
    return "\n".join(
        f"  - 评分点「{c.get('point')}」错在：{c.get('why')}"
        + (f"\n      怎么改：{c['fix']}" if c.get("fix") else "")
        for c in vr.get("checks") or [] if not c.get("ok"))


async def generate_adopt(*, about: str = "", difficulty: int = 2,
                         tries: int = 3, do_adopt: bool = False,
                         verbose: bool = True) -> tuple[dict[str, Any] | None,
                                                        list[dict[str, Any]]]:
    """出题编排的**唯一入口**：生成 → 强制 → 验题 →（失败就回灌重试）→ 落盘。

    CLI 和 `run_mve` 都走这里 —— 编排只有一份，不会两边跑偏。
    返回 (落盘记录 or None, 每一轮的验题明细)。
    """
    existing, avoid = existing_pairs()
    critique = ""
    trace: list[dict[str, Any]] = []
    for i in range(1, tries + 1):
        cand = enforce(await propose(about=about, difficulty=difficulty,
                                     critique=critique, avoid=avoid))
        if not cand:
            if verbose:
                print(f"第 {i} 次：模型没返回可用 JSON")
            continue
        if verbose:
            print(f"\n=== 第 {i} 次生成 ===")
            print(f"  topic_id  : {cand.get('topic_id')}")
            print(f"  题面      : {str(cand.get('question'))[:100]}")
            print(f"  难度      : {cand.get('difficulty')}")
            for rp in (cand.get("rubric") or []):
                sp = rp.get("answer_spec") or {}
                how = (f"SQL 取第 {sp.get('value_column')} 列" if sp.get("sql")
                       else f"{sp.get('tool')} → {sp.get('value_path')}")
                print(f"    · {rp.get('point')}｜{rp.get('dimension')}｜{how}")
                if sp.get("sql"):
                    print(f"        SQL: {str(sp.get('sql'))[:220]}")
                if sp.get("tool_args"):
                    print(f"        args: {sp.get('tool_args')}")
                if sp.get("base_path") or sp.get("base_column") is not None:
                    print(f"        分母: {sp.get('base_path') or sp.get('base_column')}"
                          f"｜percent={bool(sp.get('percent'))}")

        vr = await validate(cand, existing=existing)
        trace.append({"round": i, "candidate": cand, "validation": vr})
        if verbose:
            for c in vr["checks"]:
                mark = "✅" if c.get("ok") else "❌"
                extra = ""
                if c.get("ok"):
                    extra = f"（跑出 {c.get('value')!r}"
                    if c.get("base") is not None:
                        extra += f"，base={c.get('base')}"
                    if c.get("truth") is not None:
                        extra += f" → 真值 {c.get('truth')}"
                    extra += "）"
                print(f"    验题 {mark} {c.get('point')} — {c.get('why')}{extra}")
                if not c.get("ok") and c.get("fix"):
                    print(f"         修法：{c['fix']}")
        if vr["ok"]:
            if verbose:
                print("  ✅ 验题通过：每个评分点的配方都能从 VLML 跑出非空值")
            rec = adopt(cand, vr["checks"]) if do_adopt else cand
            if do_adopt and verbose:
                print(f"  已并入题库 → {STORE}")
            return rec, trace
        if verbose:
            print("  ❌ 验题未过：" + "；".join(vr["reasons"])[:300])
        critique = _critique_of(vr)
    return None, trace


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="自动出题：生成取数配方 + 确定性验题")
    ap.add_argument("--about", default="", help="围绕什么出题")
    ap.add_argument("--difficulty", type=int, default=2)
    ap.add_argument("--tries", type=int, default=3, help="最多生成几次")
    ap.add_argument("--adopt", action="store_true", help="验过就并入库")
    args = ap.parse_args()

    rec, _ = asyncio.run(generate_adopt(
        about=args.about, difficulty=args.difficulty,
        tries=args.tries, do_adopt=args.adopt))
    if rec is None:
        print("\n这几次都没生成出成立的题（这是诚实结果，不是 bug："
              "模型编的字段在数据里不存在，就会被确定性验题挡下来）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
