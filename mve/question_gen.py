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

# ⚠️ 不再写死：换库（GRID → vlr.gg → rib.gg）后 2843069 / Cloud9 在新库里
# **根本不存在** —— 实测 rib 库上种子题 17 个评分点 **0 个跑得出数**。
# 锚点跟着库走，从库实查（见 anchor.py）。旧库挑出来的仍是这两个值。
try:
    import anchor as _anchor
    SERIES = _anchor.series()
    C9 = _anchor.team()
except Exception:                                        # pragma: no cover
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
    # ---- 人导入采纳的新维度 ----
    # 不加这一段，`_graph_blueprint()` 的 `if not specs.get(d,{}).get("by_sql")`
    # 会把新人导入的维度整条跳过 —— 实测：图谱里明明有了
    # `plant_success_rate` 节点，出题点却仍挑回 `map_fb_conv`，
    # 出的题跟人问的毫无关系（人问爆弹成功率，出了首血转换率）。
    # 这些维度是 VLML **用 SQL 答出来的**，所以 by_sql 必为真 —— 不是猜的。
    try:
        import knowledge_graph
        for irec in knowledge_graph.load_imported_links():
            if not irec.get("covered"):
                continue                 # 工具集覆盖不到的，不配当出题方向
            d = str(irec.get("dimension") or "").strip()
            if not d or d in out:
                continue
            low = d.lower()
            is_pct = any(k in low for k in ("rate", "ratio", "pct", "conv",
                                            "success", "win"))
            out[d] = {
                "kind": "percent" if is_pct else "count",
                "needs_base": bool(is_pct),
                "example": "SQL 里自己算好百分比，另取一列作样本量",
                "by_tool": bool(irec.get("tool")) and not bool(irec.get("sql")),
                "by_sql": bool(irec.get("sql")),
                # 人导入的维度没有 dim 节点，desc 不会从节点来 —— 这里直接用
                # 人的原话，`_relevant` 才有得比对（见 `_human_desc` 的注释）
                "desc": _human_desc(str(irec.get("question") or "")),
                "origin": "human_import",
            }
    except Exception:                                        # pragma: no cover
        pass
    for d, rec in out.items():
        pt = ""
        if g is not None:
            n = g.nodes.get(f"dim:{d}")
            pt = str((n.detail.get("point") if n else "") or "")
        # 只取括号前的主名：「手枪局胜率（0-100 的百分比数值，如 50.0）」→ 手枪局胜率
        # `or rec.get("desc")` —— 人导入的维度没有节点，desc 已经在上面用人的
        # 原话填好了，这里不能把它退回英文原名（会把验题闸 0b 打回不相关）。
        # ⚠️ `point` 是**那道题的评分点文案**，绑死了具体值（"Corrode 上 Cloud9
        # 的胜率"）—— 不能直接当维度定义，否则换值出的新题全被闸 0b 判不相关。
        # 抽象化：剥掉具体实体值，只留口径。人导入的 desc 已是人的原话，不动。
        rec["desc"] = (rec.get("desc")
                       or _abstract_desc(pt.split("（")[0].split("(")[0].strip(), d))
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


def _subject_values(key: str) -> list[str]:
    """这个学科下**可填入的具体值** —— 连数据源实查，不手写。

    为什么要实查：手写一张清单换一次库就全错。`entity_catalog` 会连
    `db_config.json` 指向的库（默认 vlml/data/vlml_events.duckdb）把
    series / team / map / player 的实际取值查出来并缓存。
    查不到就返回空（该学科出不了题），**绝不编一个值出来**。

    再叠一层**人登记的学科**（`subjects.py`）：换数据源（接新的公开源）后，
    人先填一个学科，出题器就得能往这个学科上出题 —— 否则换了源，出题器
    还在出老库里那一批值。⚠️ 如实说明：人填的值若数据源里没有对应数据，
    这道题跑不出数值，会被验题闸拦下，不会混进题库。
    """
    vals: list[str] = []
    try:
        import entity_catalog
        vals = [v for v in entity_catalog.options(key) if v]
    except Exception:
        vals = []
    try:
        import subjects
        for v in subjects.values(key):
            if v and v not in vals:
                vals.append(v)
    except Exception:
        pass
    return vals


def _human_desc(question: str) -> str:
    """人导入的维度没有服务端定义 —— 用**人的原话**当它的定义。

    不这么做，验题闸 0b（`_relevant`：题面与维度定义的关键词重合）必然判
    不相关。实测（服务器 e2e）：维度 `plant_success_rate` 的 desc 退回英文
    原名，题面是中文「Cloud9 在 Lotus 图上每回合的爆弹成功率」，中英文
    token 零重合 → 两次生成 **2/2 全被这道闸拦下**，出题一次都没成功。

    括号要**去掉而不是截断**：v1 的老教训 —— 截到括号前会把「成功率」
    这个词弄丢，剩下的「…爆弹」照样跟题面对不上。
    """
    t = str(question or "").strip()
    t = re.sub(r"[（(][^）)]*[）)]", "", t)          # 去括号及其中内容
    t = re.sub(r"^(请问|帮我|我想知道|麻烦|请)[，,：:\s]*", "", t)
    t = re.sub(r"[？?。！!]+$", "", t)
    t = re.sub(r"(是多少[^，,。？?]{0,6}|是什么[^，,。？?]{0,6}"
               r"|有多少[^，,。？?]{0,6}|怎么算|如何计算)$", "", t)
    t = re.sub(r"\s+", " ", t).strip(" ，,、的")
    return t


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


def _known_entity_values() -> set[str]:
    """库里出现过的**具体实体值**（图名/队名/选手/赛事号），用来抽象化维度定义。"""
    out: set[str] = set()
    try:
        import json as _json
        from pathlib import Path as _P
        cat = _json.loads((_P(__file__).resolve().parent / "entity_catalog.json")
                          .read_text(encoding="utf-8"))
        for vals in (cat.get("entities") or {}).values():
            if isinstance(vals, list):
                out |= {str(v) for v in vals if str(v)}
    except Exception:
        pass
    return out


def _abstract_desc(pt: str, d: str) -> str:
    """把评分点文案里的**具体实体值**剥掉，只留抽象口径。

    ⚠️ 踩到的坑：维度定义直接拿种子题 rubric 的 `point`（如
    「Corrode 上 Cloud9 的胜率」「Corrode 回合数」），那是**绑定了 GRID 那场
    数据**的文案。换库出题后，题面是「Karmine Corp 在 Abyss 的回合胜率」，
    跟定义**一个词都不重合** → 验题闸 0b 判"不相关"，连出 2 次全废。

    维度定义必须是**抽象口径**（"某队在某图的胜率"），不能是某道题的评分点。
    剥值后仍能挡真错配（题面说"强起局胜率"、维度填"置信度标签"照样不相关）。
    """
    out = str(pt or "")
    for v in _known_entity_values():
        if len(v) >= 3 and v in out:
            out = out.replace(v, "")
    # 兜底规则：**大写开头 / 含数字的英文词**基本都是实体名（Corrode、Cloud9、
    # NRG、2843069），而口径词是中文。用它剥 catalog 里没有的旧实体
    # （换库后 GRID 那批图名/队名已不在 catalog 里，上面那步剥不掉）。
    out = re.sub(r"\b[A-Z][A-Za-z0-9_]{2,}\b", "", out)
    out = re.sub(r"\b\d{4,}\b", "", out)
    # 顺带剥掉当前锚点（可能不在 catalog 里，比如刚换的库）
    try:
        import anchor as _anc
        for v in (_anc.series(), _anc.team(), _anc.map_name()):
            if v and len(str(v)) >= 3:
                out = out.replace(str(v), "")
    except Exception:
        pass
    out = re.sub(r"[（(]\s*[）)]", "", out)          # 剥空的括号
    out = re.sub(r"\s+", " ", out).strip()
    # ⚠️ 第二层坑：剥完实体会**留下孤立虚词**，定义退化成「上 的胜率」——
    # 看上去还在，实际关键词只剩"上/的"，闸 0b 拿它跟题面比 bigram 照样
    # 判不相关（实测连废 2 次）。虚词必须一并清掉，只留口径实词。
    for _ in range(3):
        out2 = re.sub(r"(^| )[在的上中里下与和及为对把被而、，,](?= |$)", " ", out)
        out2 = re.sub(r"\s+", " ", out2).strip()
        if out2 == out:
            break
        out = out2
    out = re.sub(r"\s+", " ", out).strip(" 的·、，,。-—")
    return out or d


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
over partition row_number lag lead rank dense_rank union intersect except
with exists all any some cross outer full natural using offset fetch
first last nulls rows range preceding following current unbounded
subquery subquery1 subquery2 subquery3 derived alias
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
            det = (getattr(node, "detail", None) or {})
            vp = str(det.get("value_path") or "")
            cols = det.get("value_columns") or []
            tag = _dim_tag(d, specs)
            if prod and vp:
                lines.append(f"  {d} → 工具 {'/'.join(prod)}，路径 {vp} {tag}")
            elif prod:
                lines.append(f"  {d} → 工具 {'/'.join(prod)} {tag}")
            else:
                # SQL 维度：必须把**表 / 列 / 口径形态 / 典型错法**全给出来。
                #
                # 此前这里只写一句「需自己写 SQL，取第 N 列」，把 detail 里
                # 已经存在的 tables / columns_by_table / semantics /
                # typical_errors 全吞了。于是模型只能靠猜 —— 实测三次生成
                # 全部写成 `FROM match_summary_report`（那是**工具名**）
                # 和 `MAX(conversion.fb_conv)`（把**维度名**当列名）。
                # 声明一直都在图谱里，只是没给它看。
                #
                # 同构：伴学的知识点节点自带 question_types 与
                # typical_misconceptions，出题时**照节点声明走**
                # （question_type_mapping.py:131 "map it without LLM input"）。
                tbl = (det.get("tables") or ["?"])[0]
                cbt = det.get("columns_by_table") or {}
                cols_txt = "、".join((cbt.get(tbl) or det.get("columns") or []))
                sem = str(det.get("semantics") or "").strip()
                lines.append(
                    f"  {d} → 自己写 SQL：FROM **{tbl}**"
                    + (f"，可用列：{cols_txt}" if cols_txt else "")
                    + (f"；口径形态：{sem[:80]}" if sem else "")
                    + f" {tag}")
                for e in (det.get("typical_errors") or [])[:2]:
                    lines.append(f"      典型错法：{str(e)[:90]}")
                # 建模声明（表节点，来自 VLML 的 DATA_MODEL.md / DERIVED_TABLES.md
                # / column_definitions.yaml）：一行是什么、每列是什么意思。
                # 只给列名的后果已经实测过：模型把 fb_player 写成 player_name，
                # critique 每轮都把真名列出来，它照样按常识编三次。
                tnode = g.nodes.get(f"table:{tbl}")
                tdet = (getattr(tnode, "detail", None) or {})
                if tdet.get("pk"):
                    lines.append(
                        f"      {tbl} 主键：{'、'.join(tdet['pk'])}"
                        f"（一行 = {tdet.get('grain') or tdet.get('grain_doc') or ''}）")
                desc = tdet.get("column_desc") or {}
                named = [c for c in (cbt.get(tbl) or det.get("columns") or [])
                         if c in desc]
                if named:
                    lines.append("      列的含义："
                                 + "；".join(
                                     f"{c}={str((desc[c] or {}).get('desc') or '')[:50]}"
                                     for c in named[:5]))
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


def _subject_key_of(col: str) -> str:
    """列名 → 能当 subject 键的语义（照图谱里已有的 subject 命名）。"""
    c = str(col or "").lower()
    if "player" in c:
        return "player"
    if c.startswith("map") or c.endswith("_map"):
        return "map"
    if "team" in c:
        return "team"
    if "series" in c:
        return "series"
    if "game" in c:
        return "game"
    return ""


_ROWS_CACHE: dict[str, int] = {}


_DIM_MASTERY_CACHE: dict[str, dict[str, Any]] = {}


def _dim_mastery() -> dict[str, dict[str, Any]]:
    """维度级掌握度（带缓存）。数据源 = **exam_log**（考核，撤了支架的真水平）。

    ⚠️ 不能用 run_log：练习是带图谱跑的，覆盖率恒 100% 的贴顶假平线，
    量的是支架不是能力（项目核心设计）。拿它当掌握度等于自欺。
    """
    if _DIM_MASTERY_CACHE:
        return _DIM_MASTERY_CACHE
    try:
        import mastery_model as _mm
        _DIM_MASTERY_CACHE.update(_mm.dimension_mastery() or {})
    except Exception:                                    # pragma: no cover
        pass
    return _DIM_MASTERY_CACHE


def _table_rows(table: str) -> int:
    """当前库里这张表有几行（带缓存，一个进程只查一次）。

    ⚠️ 换数据源后**图谱里指向的表可能是空的**：rib/vlr 没有 kill 事件，
    于是 `agg_first_blood_stats` 0 行、`base_events.is_kill` 0 行 —— 而图谱
    是 GRID 时代建的，维度节点照旧指向它。挑空表出题，模型写得再对也跑不出数
    （实测连出 2 次全被验题以"配方跑出来是空"判废）。
    所以挑题点前必须先确认这张表在当前库里**真的有数据**。
    """
    t = str(table or "").strip()
    if not t:
        return 0
    if t in _ROWS_CACHE:
        return _ROWS_CACHE[t]
    n = 0
    try:
        import duckdb
        import anchor as _anc
        con = duckdb.connect(str(_anc._db_path()), read_only=True)
        try:
            n = int(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
        finally:
            con.close()
    except Exception:
        n = 0
    _ROWS_CACHE[t] = n
    return n


_SEED_SQL_CACHE: dict[str, str] = {}


def _seed_skeleton(dim: str) -> str:
    """图谱没 recipe 时的**骨架兜底**：取种子题里那条人审过的正确 SQL。

    ⚠️ 这是伴学 `examples` 的同构位 —— **给正确例题让模型照抄**，而不是事后
    拦它。此前走岔了：图谱的 `recipe` 只有部分维度有（map_win_rate 是空），
    骨架一空模型就只能自己发明 WHERE，于是把 `winning_team_name` 写进
    WHERE、胜率恒 100%，题是废的；我为此加了一道"口径自噬"闸去事后拦，
    结果模型连着两轮写出**一模一样**的 SQL —— 事后拦不动，只能事前给对。

    而**正确写法一直在种子题里**（tasks.py:229）：
        SELECT ROUND(AVG(CASE WHEN winning_team_name='<队伍>' ...)), COUNT(*)
        FROM rounds WHERE series_id='<赛事>' AND map_name='<图>'
    WHERE 里干干净净，胜负交给 CASE 判。它是人审过、跑得出数的。
    """
    d = str(dim or "").strip()
    if not d:
        return ""
    if d in _SEED_SQL_CACHE:
        return _SEED_SQL_CACHE[d]
    out = ""
    try:
        from tasks import TASKS
        best = ""
        for _t in TASKS.values():
            for _p in (getattr(_t, "rubric", None) or []):
                if str(getattr(_p, "dimension", "") or "") != d:
                    continue
                _s = str(getattr(getattr(_p, "answer_spec", None), "sql", "") or "")
                if len(_s) > len(best):
                    best = _s
        if best:
            out = best
            # 脱敏：具体实体值换成 '?'（骨架只给形状，值要模型自己去库里查）
            try:
                import anchor as _anc
                for v in (_anc.series(), _anc.team(), _anc.map_name(), SERIES, C9):
                    if v and len(str(v)) >= 3:
                        out = out.replace(str(v), "?")
            except Exception:
                pass
            for v in _known_entity_values():
                if len(v) >= 3 and v in out:
                    out = out.replace(v, "?")
            out = re.sub(r"'[A-Z][^']{2,}'", "'?'", out)
    except Exception:                                        # pragma: no cover
        out = ""
    _SEED_SQL_CACHE[d] = out
    return out


_SCOPED_CACHE: dict[tuple[str, str], list[str] | None] = {}


def _scoped_values(kind: str, series_id: str) -> list[str] | None:
    """**这场比赛里真有的**实体值。查不到/不适用就返回 None（表示不限定）。

    ⚠️ 第三道可解性校验（前两道：表非空、列存在）。`_subject_values()` 给的
    是**全库**的值域，不等于这场比赛打过 —— 实测：1058 只打了 Sunset /
    Haven / Summit，却从全库 7 张图里挑出 **Abyss**，于是
    `WHERE series_id='1058' AND map_name='Abyss'` 0 回合 → 题无解，连出 3 次
    全废。所以值必须再按 series 收一次口。
    """
    k = str(kind or "").strip()
    sid = str(series_id or "").strip()
    if not k or not sid:
        return None
    if (k, sid) in _SCOPED_CACHE:
        return _SCOPED_CACHE[(k, sid)]
    out: list[str] | None = None
    if k in ("map", "team"):
        try:
            import duckdb
            import anchor as _anc
            con = duckdb.connect(str(_anc._db_path()), read_only=True)
            try:
                if k == "map":
                    rows = con.execute(
                        "SELECT DISTINCT map_name FROM rounds "
                        "WHERE CAST(series_id AS VARCHAR)=? "
                        "AND map_name IS NOT NULL", [sid]).fetchall()
                else:
                    rows = con.execute(
                        "SELECT DISTINCT t FROM (SELECT winning_team_name AS t "
                        "FROM rounds WHERE CAST(series_id AS VARCHAR)=? "
                        "UNION ALL SELECT losing_team_name FROM rounds "
                        "WHERE CAST(series_id AS VARCHAR)=?) "
                        "WHERE t IS NOT NULL AND t<>''", [sid, sid]).fetchall()
                out = [str(r[0]) for r in rows if r[0]]
            finally:
                con.close()
        except Exception:
            out = None
    _SCOPED_CACHE[(k, sid)] = out
    return out


def _graph_blueprint(difficulty: int = 2,
                     avoid: set[tuple[str, str]] | None = None,
                     prefer: str = "") -> dict[str, Any]:
    """服务端**从图谱挑出题点** —— 照伴学，题点不由 LLM 选。

    伴学的做法（`question_type_mapping.py:131` 的 docstring 说得最直白）：
        "Choose the first declared teaching style and **map it without
         LLM input**. Seed ordering is preserved as the author-provided
         priority."
    即：**知识点节点自带 question_types / difficulty / typical_misconceptions，
    出题器选中那个节点，题型与口径由节点声明决定**，模型只负责把题面写出来。

    MVE 同构：图谱的维度节点 detail 里已经有 `tables` / `columns_by_table` /
    `semantics` / `recipe` / `typical_errors`，服务端据此挑定
    「哪个维度 × 哪个 subject 键 × 哪张表 × 哪些列 × 什么口径形态」，
    把这些**写死进 prompt**，模型只填 WHERE 的值和题面文案。

    为什么必须做到这一步（实测教训）：只把"真实列清单"放进 critique 是不够的
    —— 三次生成里 critique 每次都把 `fb_player` 列在真实列里，模型照样坚持写
    不存在的 `player_name`。**让它挑，它就会按常识编**；唯一可靠的办法是不让它挑。
    """
    avoid = avoid or set()
    try:
        import knowledge_graph
        g = knowledge_graph.KnowledgeGraph.load()
    except Exception:                                        # pragma: no cover
        return {}
    specs = _dim_specs()
    cands: list[tuple[int, str, dict[str, Any]]] = []
    for d in sorted(g.dimensions()):
        node = g.nodes.get(f"dim:{d}")
        det = getattr(node, "detail", None) or {}
        tables = det.get("tables") or []
        if not tables:
            continue                     # 工具维度：口径受工具签名限制，先不拿来出新题
        if not specs.get(d, {}).get("by_sql"):
            continue
        # 挑**当前库里真有数据**的那张表，不是照图谱顺序取第一张
        tbl, cols = "", []
        for t in tables:
            if _table_rows(t) == 0:
                continue
            tbl = t
            cols = ((det.get("columns_by_table") or {}).get(t)
                    or det.get("columns") or [])
            break
        if not tbl or not cols:
            continue                 # 这个维度在当前库里没有可解的表
        # 这个维度能撑起哪些还没用过的 subject 键
        keys = [k for k in (_subject_key_of(c) for c in cols) if k]
        for k in dict.fromkeys(keys):
            # subject 的**值**从数据源实查（entity_catalog），不手写 ——
            # 以前这里写 `subj[k] = "?"` 把取值权交给模型，于是永远出
            # Cloud9 / 2843069 那一道；现在每个可填值都是一个独立候选。
            vals = _subject_values(k)
            if k in ("series", "team"):
                # 已有题固定用 series=2843069 / team=Cloud9。
                # 换**别的实体**才叫新题（也是迁移：同一知识点换个队伍算不算得出）
                vals = [v for v in vals if v not in (SERIES, C9)]
            if not vals:
                continue
            for v in vals:
                subj = {"series": SERIES, "team": C9}
                subj[k] = v
                # ---- subject 必须是**这个维度自己的粒度**，不能一律带 team ----
                # 实测：map_rounds 只用 map_name + series_id，但 subject 里
                # 躺着 team=Cloud9，模型照着写 `AND team_name='...'` ——
                # rounds 表根本没有 team_name，0 行，题无解。
                # 伴学的同构位是 `project_target_topic_evidence()`：只投影
                # **这个知识点声明过的**字段，不多给。
                grain = set(keys) | {"series"}
                subj = {kk: vv for kk, vv in subj.items() if kk in grain}
                if not subj.get("series"):
                    subj["series"] = SERIES
                # ---- 第三道可解性校验：值必须**这场比赛里真有** ----
                _sid = str(subj.get("series") or SERIES)
                if k == "series":
                    # 换的是**另一场比赛**，队伍也得跟着换 —— 否则
                    # `WHERE series_id=<新场> AND team_name=Cloud9` 直接 0 行
                    _teams = _scoped_values("team", str(v)) or []
                    if _teams:
                        subj["team"] = C9 if C9 in _teams else _teams[0]
                else:
                    _sc = _scoped_values(k, _sid)
                    if _sc and str(v) not in _sc:
                        continue        # 这场没打过这张图 / 没这个队
                key = (json.dumps(subj, sort_keys=True), d)
                if key in avoid:
                    continue
                score = 0
                if prefer and d == prefer:
                    score -= 10          # planner 指定的方向优先
                # 难度匹配：读**图谱自带**的 difficulty（伴学的难度长在知识点
                # 节点上，不在出题器里）。以前这里自己数 recipe 的关键字，而
                # recipe 只是脱敏骨架、常常没有 GROUP BY，于是明明要分组的题
                # 被判成 1 档。
                own = int(det.get("difficulty") or 0)
                if not own:                  # 老图没这个字段才退回数关键字
                    low = str(det.get("recipe") or det.get("semantics") or "").lower()
                    own = 1
                    if "over (" in low or "over(" in low or "from (select" in low:
                        own = 4
                    elif " join " in low or "case when" in low:
                        own = 3
                    elif "group by" in low:
                        own = 2
                # 难度匹配要吃**掌握度**，不能只吃静态种子难度。
                # 伴学 `difficulty_policy.py:94-139`：
                #     combined = (种子难度归一 + 掌握度) / 2 → _level() → 2/3/4
                # 即"这个知识点现在该出多难"是**种子难度与掌握度的联合函数**，
                # 不是节点上写死的常量。MVE 此前只读 `det["difficulty"]`
                # （种子难度），读不到"Voyager 现在会到哪" → 梯度少一维，
                # 于是曲线只能 0→100 阶跃（实测 42 条日志）。
                own_m = float((_dim_mastery().get(d) or {}).get("mastery") or 0.0)
                try:
                    from difficulty import seed_unit as _su, _level as _lv
                    fit = _lv((_su(own) + own_m) / 2.0)
                except Exception:                        # pragma: no cover
                    fit = own
                score += abs(fit - int(difficulty or 2)) * 3
                # 伴学 readiness：没掌握的先来（先修没过的不给进下一层）。
                # 掌握度低的维度在**低档**优先出，高档则不优先 —— 否则
                # 一上来就拿不会的维度出难题，又是阶跃。
                if own_m < 0.5:
                    score += (-4 if int(difficulty or 2) <= 2 else +4)
                cands.append((score, d, {
                    "dimension": d,
                    "subject": subj,
                    "subject_key": k,
                    "table": tbl,
                    "columns": list(cols),
                    "semantics": str(det.get("semantics") or ""),
                    # ↓↓↓ 伴学 `project_target_topic_evidence()` 的 KNOWLEDGE 组。
                    # 这四个字段**早就随脚手架照搬进图谱了**（question_types 15/15、
                    # examples 15/15、typical_misconceptions 13/15、skills 9/15 有值），
                    # 但出题器一直没读 —— 等于素材搬来了没人用。
                    "question_types": list(det.get("question_types") or []),
                    "skills": list(det.get("skills") or []),
                    "examples": list(det.get("examples") or [])[:2],
                    # ⚠️ 误区必须用 `typical_misconceptions`（**完整版**），不是
                    # `typical_errors`（裁过的）。实测差一条，而漏的正是最容易
                    # 踩的那条：fb_conv 的 "agg_first_blood_stats 已是 round_id
                    # 一行，别再 JOIN rounds 去重，会放大行数"。
                    "misconceptions": list(
                        det.get("typical_misconceptions")
                        or det.get("typical_errors") or [])[:4],
                    # 结构骨架：**难度 4 能不能出出来，全靠给不给它**。
                    # 实测不给骨架连试 4 次，模型每次都写成
                    # `COUNT(*) OVER (ORDER BY ...)`（累计计数，不是连续段数）
                    # 或把 `team_name` 写进没有这列的 rounds 表 —— 4/4 全废。
                    # 而 gaps-and-islands 的正确形状**就在图谱里**（脱敏过的
                    # recipe，还标了 WHERE 该放哪一层）。让模型照骨架改值，
                    # 而不是让它从零发明 —— 这正是"题点归服务端"该覆盖的最后一层。
                    # 图谱没 recipe 的维度，退回种子题里那条人审过的 SQL。
                    "skeleton": str(det.get("recipe") or _seed_skeleton(d))[:700],
                    "own_difficulty": own,
                    # 百分比维度要连样本量一起取（见 propose 里的 require_base 提示）
                    "require_base": bool(specs.get(d, {}).get("needs_base")),
                }))

    # ---- 候选来源 2：人导入落的**边**（不建节点，见 knowledge_graph.link_import）----
    #
    # 为什么必须有这一段：图谱的维度节点全部从 `TASKS[].rubric` 派生（出过题
    # 才进图），而人导入的题**故意不建节点** —— 于是上面那个循环永远挑不到
    # 人的方向，自适应出题又绕回旧题。人定的题要影响出题，入口只能在边上：
    # 边记录了它真实用到的表/列/骨架（来自那次真跑过的 SQL）。
    try:
        import knowledge_graph as _kg
        for lk in _kg.load_imported_links():
            if not lk.get("covered"):
                continue
            d = str(lk.get("dimension") or "").strip()
            if not d or not specs.get(d, {}).get("by_sql"):
                continue                 # 没真跑过 SQL 的方向不拿来出题
            sql = str(lk.get("sql") or "")
            shape = _kg.sql_shape(sql, list(lk.get("tables") or []))
            if not shape["tables"]:
                continue
            tbl = shape["tables"][0]
            cols = (shape["columns_by_table"].get(tbl)
                    or shape["columns"] or [])
            # 人导入的边本身不带教学素材；维度名若对得上已有节点就借用它的
            lk_det: dict[str, Any] = {}
            _node = g.nodes.get(f"dim:{d}")
            if _node is not None:
                lk_det = getattr(_node, "detail", None) or {}
            # 人的题面自带 subject（那次取数的粒度），它才是"新"的地方
            subj = dict(lk.get("subject") or {}) or {"series": SERIES}
            key = (json.dumps(subj, sort_keys=True), d)
            if key in avoid:
                continue
            score = -6                   # 人导入的方向优先（弱于 prefer 的 -10）
            if prefer and d == prefer:
                score -= 10
            own = int(shape["difficulty"] or 2)
            score += abs(own - int(difficulty or 2)) * 3
            cands.append((score, d, {
                "dimension": d,
                "subject": subj,
                "subject_key": next((k for k in subj
                                     if k not in ("series", "team")), "series"),
                "table": tbl,
                "columns": list(cols),
                "semantics": shape["semantics"],
                # 边上没有教学素材字段（它记的是那次真跑过的 SQL）。但人的题
                # 常常落在**已有维度**上 —— 那就把那个节点的素材补过来，
                # 否则人导入的方向永远是"裸骨架出题"，比种子方向少一层。
                "question_types": list(lk_det.get("question_types") or []),
                "skills": list(lk_det.get("skills") or []),
                "examples": list(lk_det.get("examples") or [])[:2],
                "misconceptions": list(
                    lk_det.get("typical_misconceptions")
                    or lk_det.get("typical_errors") or [])[:4],
                "skeleton": str(shape["recipe"] or "")[:700],
                "own_difficulty": own,
                "require_base": bool(specs.get(d, {}).get("needs_base")),
                "source": "human_link",           # 溯源：这个方向来自人导入的边
                "seed_question": str(lk.get("question") or "")[:300],
                "cover_level": str(lk.get("cover_level") or ""),
                "insights": list(lk.get("insights") or []),
            }))
    except Exception:                                        # pragma: no cover
        pass

    if not cands:
        return {}
    cands.sort(key=lambda x: (x[0], x[1]))
    return cands[0][2]


async def propose(*, about: str = "", difficulty: int = 2,
                  critique: str = "", avoid: str = "",
                  blueprint: dict[str, Any] | None = None) -> dict[str, Any]:
    """第 2 段：LLM 出题面 + 取数配方（**不含答案数值**）。

    `critique` 是**上一轮验题的失败原因**，原样喂回去 —— 这是原版 Voyager 的
    iterative prompting / critic，也是 `action_code.collect()` 里已经验证有效
    的那一招：不给理由的重试，模型只会换一种方式犯同一个错。

    `avoid` 是**已经出过的 (subject, dimension) 清单**。事前给比事后挡便宜：
    实测不给的话，模型每轮都撞已有题，白烧两次调用。
    """
    band = DIFFICULTY_RUBRIC.get(int(difficulty) or 2) or {}
    band_txt = ""
    if band:
        band_txt = (
            f"\n\n难度 {difficulty} 的含义（服务端会**照这条逐字数**你的 SQL，"
            f"不达标就判废）：{band.get('needs', '')}。"
            f"配方里必须出现 {' / '.join(band.get('any_of') or {})} 之一。")
    user = (f"围绕「{about or '本场比赛'}」出一道题，难度 {difficulty}。"
            "记住：给取数配方，不要给答案数值。" + band_txt)
    if blueprint:
        # 服务端已挑定出题点 —— 模型**不许改**，只许把值填进去。
        # 这是伴学 `resolve_target_question_type` + `enforce_mapped_question_type`
        # 那一对的同构物：题点归服务端，题面归模型。
        bp = (
            f"\n\n【服务端已挑定的出题点 —— 不许改动，照它出题】"
            f"\n  维度 dimension 必须写：{blueprint.get('dimension')}"
            f"\n  subject 必须写：{json.dumps(blueprint.get('subject') or {}, ensure_ascii=False)}"
            f"  （其中 {blueprint.get('subject_key')} 的值你去库里查一个真实存在的填进去）"
            f"\n  FROM 必须是这张表：{blueprint.get('table')}"
            f"\n  只能用这些列：{'、'.join(blueprint.get('columns') or [])}"
            f"\n  口径形态照这个：{str(blueprint.get('semantics') or '')[:160]}"
            f"\n  **不要**写上面没出现的列名，也不要换表。表里的列就是这些，"
            f"没有 player_name / team_name 这种常识列 —— 队员列是 fb_player，队伍列是 fb_team。"
        )
        # ── 题型由**知识点节点声明**决定，不经模型 ──
        # 伴学 `question_type_mapping.py:131`：取节点自带的 question_types
        # 第一项映射到机器题型，"map it without LLM input"。
        qt = [str(x) for x in (blueprint.get("question_types") or []) if x]
        if qt:
            kind = ("写 SQL" if "sql" in qt[0]
                    else "调工具取值" if "tool" in qt[0] else qt[0])
            bp += (f"\n  答案配方类型（**由知识点节点声明，不许改**）：{qt[0]}"
                   f" → answer_spec 里必须给 {kind}")
        sk = [str(x) for x in (blueprint.get("skills") or []) if x]
        if sk:
            bp += f"\n  这个点考的技能：{'、'.join(sk[:4])}"

        # 误区用**完整版**（typical_misconceptions），不是裁过的 typical_errors。
        # 出题时该"引出"这些错法，而不是在题面里把答案说破。
        mis = [str(x) for x in (blueprint.get("misconceptions") or []) if x]
        if mis:
            bp += "\n  ★ 这个知识点的**典型误区**（你的题要能区分「真会」和「蒙对」，"
            bp += "别在题面里直接把答案说出来）："
            for m in mis:
                bp += f"\n    - {m}"
        # 百分比口径必须**额外取一列**当样本量。不写这条，模型每次都只取
        # 分子（实测：连续 4 次被验题以"取不到分母/样本量"判废，白烧 4 次调用）。
        # 事前说清楚比事后回灌 critique 便宜。
        if blueprint.get("require_base"):
            bp += (f"\n  ★ 这个维度是**百分比**：SQL 里除了比值本身，必须再取一列"
                   f"当样本量/分母，并在 answer_spec 里用 `base_column` 标出它是第几列"
                   f"（0 开始数）。只给比值、不给样本量的题一律判废。")
        skel = str(blueprint.get("skeleton") or "").strip()
        if skel:
            # 骨架是**服务端给出的正确形状**（脱敏过的，'?' 是占位符）。
            # 不给骨架的实测：难度 4 连出 4 次全废（写成累计计数 / 编造列）。
            bp += (
                "\n  ★ 照这个**结构骨架**写你的 SQL（'?' 换成你查到的真实值，"
                "形状不许改）：\n" + skel +
                "\n  骨架里 WHERE 的**位置就是口径**：内层先编号、外层再筛。"
                "不要把过滤条件全塞进最内层 —— 塞进去段就断了。")
        # ⚠️ 实测踩到：口径用 CASE WHEN 判胜负时，模型把那一列**又写进 WHERE**
        # （`WHERE winning_team_name='X'`）→ 只剩赢的回合，胜率恒为 100 或直接
        # 空。WHERE 只筛范围（series/map），胜负必须交给 CASE 判。
        if "case when" in (str(blueprint.get("semantics") or "")
                           + str(blueprint.get("skeleton") or "")).lower():
            bp += ("\n  ★★ 胜/负必须交给 **CASE WHEN** 判，"
                   "**不要**把判定胜负的那列再写进 WHERE —— 写进去就只剩赢的"
                   "回合，胜率恒为 100 或直接跑空。WHERE 只用来限定范围"
                   "（series_id / map_name），胜负在 SELECT 里判。")
        # 例题：节点自带的**正确口径长什么样**。骨架是脱敏形状，例题带真实值，
        # 两个合起来模型才既有形状又有落法（值仍要它自己去库里查）。
        ex = [str(x) for x in (blueprint.get("examples") or []) if x]
        if ex:
            bp += ("\n  参考例题（**口径形状**照它，值换成你查到的真实值，"
                   "不要照抄字面量）：\n" + str(ex[0])[:320])
        user += bp
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


def _difficulty_gap(candidate: dict[str, Any], want: int) -> str:
    """这道题的配方配不配得上它自称的难度？返回缺口说明，配得上返回空串。

    判据必须**可数**（数 SQL 里的关键字），不能靠模型自评 ——
    自评等于让它自己给自己打分。
    """
    need = DIFFICULTY_RUBRIC.get(int(want) or 2)
    if not need:
        return ""
    rubric = [rp for rp in (candidate.get("rubric") or []) if isinstance(rp, dict)]
    sqls = []
    for rp in rubric:
        spec = rp.get("answer_spec") if isinstance(rp, dict) else None
        if isinstance(spec, dict):
            s = str(spec.get("sql") or "")
            if s:
                sqls.append(s.lower())
    if not sqls:
        # 工具路径：没有 SQL 可数，只能看评分点数量（弱判据，不拦）
        return ""
    joined = " \n ".join(sqls)
    has_group = "group by" in joined
    for feat, kws in (need.get("any_of") or {}).items():
        if any(k in joined for k in kws):
            return ""
    # 弱判据：同一档也可以靠"拆得更细"达到 —— 分组 + 足够的评分点数。
    # 少了这一条，难度 2 会误杀「两个评分点但没 GROUP BY」的题（实测
    # `SELECT COUNT(*)...` 被判不达标，其实那就是一道正经的 2 档题）。
    alt = int(need.get("or_points") or 0)
    if alt and len(rubric) >= alt and (has_group or not need.get("or_points_need_group")):
        return ""
    return f"需要出现 {' / '.join(need.get('any_of') or {})} 之一" \
           + (f"，或分组且不少于 {alt} 个评分点" if alt else "")


# 难度分档：每档写清"什么样才算这个难度"。
#
# 为什么必须写死：此前 prompt 里只有一句 `难度 {n}`，schema 里只有
# `"difficulty": 1-4` —— 模型自由解读，服务端从不校验。于是出题器把
# 梯度算出来的难度传过来，生成出的题却可能还是一条 `SELECT COUNT(*)`，
# "按难度生成不同的题"名存实亡。
#
# 照伴学的同构：伴学的难度是**题库铺开**的（82 个知识点 × 3 档，每档的题
# 面与评分点数量都不同），不是靠生成时临时发挥。MVE 没有那份题库，
# 就把"每档长什么样"压缩成可数的 SQL 特征，生成后逐条数。
DIFFICULTY_RUBRIC: dict[int, dict[str, Any]] = {
    1: {"needs": "单表单指标聚合",
        "any_of": {"聚合函数": ["count(", "avg(", "sum(", "max(", "min("]}},
    2: {"needs": "分组聚合（GROUP BY）或多个评分点",
        "any_of": {"分组": ["group by"]},
        "or_points": 2},
    3: {"needs": "分组 + 条件过滤，或多表 JOIN",
        "any_of": {"多表 JOIN": [" join "],
                   "条件分支": ["case when", " having ", "coalesce("]},
        "or_points": 3, "or_points_need_group": True},
    4: {"needs": "窗口函数 / 自连接 / 嵌套子查询（gaps-and-islands、最长连续、排名）",
        "any_of": {"窗口函数": ["over (", "over("],
                   "嵌套子查询": ["from (select", "from(select"]}},
}


# ---------------------------------------------------------------------------
# 一道题**合不合格**的判据（2026-10-08 定的，别再加闸）
# ---------------------------------------------------------------------------
#   合格线只有一条：**让 VLML 跑一遍，跑得出非空值**。
#
# 为什么是这一条：题是给 Voyager 做的，Voyager 的答案要跟"VLML 跑出来的真值"
# 比。跑不出值的题根本没有真值，等于给 Voyager 一道无法判分的题 —— 这才是
# 唯一必须挡的。
#
# 伴学的同构位是 `answer_supported`（答案能被材料支撑），它靠**第二个 LLM
# 的意见**判；MVE 有真实数据源，可以**确定性**地判 —— 跑一遍就行，不必问
# 模型。这是 MVE 比伴学强的那一处，不是要向它看齐的地方。
#
# 下面 `STRICT_MODE` 里的几条（百分比越界 / 量纲 / 非数字值 / "连续"形状）
# 是**合理性兜底**，不是合格线。它们是踩坑时一条条加上去的（每条的注释里
# 都记着当时废了哪道题），但**正确用法是让它们逐步退场**：骨架给对之后
# 模型不再乱写，这几条就该拦不到东西。要只用合格线，跑 `--loose`。
# ---------------------------------------------------------------------------
STRICT_MODE = True


async def validate(candidate: dict[str, Any], *,
                   existing: set[tuple[str, str]] | None = None) -> dict[str, Any]:
    """验题。**不问模型**，只问数据 —— 让 VLML 跑一遍，跑得出非空值就算过。

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
            if STRICT_MODE and not (0.0 <= pct <= 100.0):
                row["ok"] = False
                row["why"] = f"百分比算出来 {round(pct, 1)} 不在 0-100 —— 口径取错了"
                checks.append(row)
                reasons.append(f"「{point}」百分比越界（{round(pct, 1)}）")
                continue
            row["truth"] = round(pct, 1)

        # 闸 3（严格档）：量纲 —— 计数型维度的值不能超过全场总回合数
        if STRICT_MODE and str(dspec.get("kind")) == "count":
            try:
                v = float(got.get("value"))
            except (TypeError, ValueError):
                # 取出来的不是数（实测取到过 'Haven' —— value_column 指到了
                # map_name 那列）。此时不能 pass 放行：一个地图名当"最长连败
                # 回合数"混进题库，裁判会拿它当真值，整道题的评分就废了。
                row["ok"] = False
                row["fix"] = (f"配方取出来的值是 {got.get('value')!r}，不是数字 —— "
                              "value_column 指错列了（多半指到了 map_name / "
                              "player_name 这类标签列）。把它改成数值那一列。")
                row["why"] = f"取出的值 {got.get('value')!r} 不是数字"
                checks.append(row)
                reasons.append(f"「{point}」取出的值不是数字（{got.get('value')!r}）")
                continue
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

        # 闸 3b（通用）：只要这道评分点是**按数值比**（有 numeric_tolerance，
        # 或维度声明不是枚举/标签），跑出来的值就必须能当数字用。
        #
        # 为什么不能只靠上面那条 count 专属的闸：实测 `max_losing_streak_map`
        # 就是这么混进来的 —— 它的 SQL 是
        #   `SELECT map_name, MAX(cnt) ... GROUP BY map_name` + `value_column: 0`
        # 取到的是**地图名** 'Lotus'，而题干问"最长连败回合数"、tolerance=0.05。
        # 裁判拿 'Lotus' 当真值，模型交任何数字都判错 → 连跑 5 轮全 0%，
        # critique 一字不变。那不是"学不会"，是**题是坏的**。
        # 坏题比没有题更糟：它会伪装成"学习曲线没起来"，把人引向错的方向。
        if STRICT_MODE and str(dspec.get("kind")) != "count" and spec.get("sql"):
            try:
                float(got.get("value"))
            except (TypeError, ValueError):
                row["ok"] = False
                row["fix"] = (
                    f"配方取出来的值是 {got.get('value')!r}，不是数字 —— "
                    "value_column 指错列了（多半指到了 map_name / player_name "
                    "这类标签列）。把它改成数值那一列；"
                    "如果这一列本来就是分组键，就把它从 SELECT 里去掉或挪到后面。")
                row["why"] = f"取出的值 {got.get('value')!r} 不是数字（题干按数值评分）"
                checks.append(row)
                reasons.append(f"「{point}」取出的值不是数字（{got.get('value')!r}）")
                continue

        row["ok"] = True
        row["why"] = "配方跑得出非空值"
        row["value"] = got.get("value")
        row["base"] = base
        checks.append(row)

    ok = bool(checks) and all(c.get("ok") for c in checks)

    # ---- 闸 8（题级）：难度达标 ----
    # 光把「难度 4」这句话塞进 prompt 是没用的：模型不知道 4 意味着什么，
    # 生成完也没有人检查 —— 于是"按难度生成不同的题"名存实亡，题库涨了
    # 难度不涨。这里两件事一起做：
    #   (a) 把每档的**可验证特征**写死在服务端（见 DIFFICULTY_RUBRIC）；
    #   (b) 生成后**真的去数**：SQL 里有没有窗口函数 / 子查询 / JOIN。
    #
    # **独立于 ok 计算**：此前把它放在 `if ok:` 里，结果模型在前面几道闸
    # （表名写错、评分点重复）就先挂了，永远收不到"难度不达标"这句反馈 ——
    # 实测三次生成全是 `GROUP BY player_name`，没有任何难度 4 该有的结构，
    # 而 critique 里一个字没提。难度缺口必须**每次都算、每次都回灌**，
    # 让模型同时改"表名"和"难度形态"，而不是改完一个才知道还有下一个。
    gap = _difficulty_gap(candidate, int(candidate.get("difficulty") or 2))
    if gap:
        reasons.append(f"难度不达标（要求 {candidate.get('difficulty')} 档）：{gap}")

    # ---- 闸 9（题级·严格档）："连续"语义必须有 gaps-and-islands 的可数形状 ----
    # `COUNT(*) OVER (ORDER BY ...)` 也是窗口函数，能过闸 8，但它算的是
    # **累计计数**（到当前行为止一共输了多少），MAX 出来 = 总共输了几个回合，
    # 根本不是"最长**连续**连败"。实测 `max_losing_streak_map` 就是这么生成的：
    # 难度闸过了、结构闸过了，口径却是错的 —— 于是这道题永远做不对。
    #
    # gaps-and-islands 的形状是可数的，认这两种：
    #   (a) 差值法：ROW_NUMBER 出现 ≥2 次（两个编号相减得到段号）
    #   (b) 重置法：LAG / SUM(...) OVER 做断点累计
    # 两条都没有 → 判废，并把这个形状要求原样写进 fix 回灌给模型。
    text = " ".join(
        [str(candidate.get("question") or "")]
        + [str(rp.get("point") or "")
           for rp in (candidate.get("rubric") or []) if isinstance(rp, dict)]
    ).lower()
    if STRICT_MODE and any(k in text for k in ("连续", "连败", "连胜", "streak")):
        sqls = " \n ".join(
            str((rp.get("answer_spec") or {}).get("sql") or "").lower()
            for rp in (candidate.get("rubric") or []) if isinstance(rp, dict))
        if sqls.strip():
            n_rn = sqls.count("row_number")
            has_reset = ("lag(" in sqls or "lead(" in sqls
                         or ("sum(" in sqls and " over " in sqls))
            if n_rn < 2 and not has_reset:
                gap_msg = ("「连续/连败」必须用 gaps-and-islands："
                           "两个 ROW_NUMBER() 相减得段号"
                           "（ROW_NUMBER() OVER (ORDER BY round_number) "
                           "- ROW_NUMBER() OVER (PARTITION BY 队伍 ORDER BY round_number)），"
                           "再用 LAG/SUM() OVER 做断点重置。"
                           "COUNT(*) OVER (ORDER BY ...) 是**累计计数**，不是连续段数。")
                reasons.append(gap_msg)
                for c in checks:
                    c["ok"] = False
                    c.setdefault("fix", gap_msg)
                ok = False

    return {"ok": ok, "checks": checks, "reasons": reasons,
            "difficulty": candidate.get("difficulty"),
            "difficulty_gap": gap}


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
    # topic_id 必须是英文小写下划线 —— 模型会给中文（实测给了
    # `max_losing_streak_map粒度`），这个 id 会进题库、进日志、进面板 URL，
    # 不能留中文。服务端能补的就不废题（照伴学 enforce 精神）。
    tid = str(out.get("topic_id") or "").strip()
    if tid:
        slug = re.sub(r"[^a-z0-9_]+", "_", tid.lower()).strip("_")
        slug = re.sub(r"_+", "_", slug)
        if not slug:
            slug = "gen_" + re.sub(r"[^a-z0-9]+", "", str(
                out.get("question") or "").lower())[:24] or "gen_topic"
        out["topic_id"] = slug
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


async def revalidate_store() -> list[dict[str, Any]]:
    """回头复核**已入库**的生成题 —— 新闸管不到存量，必须补一刀。

    为什么必须有它：验题规则是逐步补的，题是先入库的。实测
    `max_losing_streak_map` 在新闸（"取出的值必须是数字"）加上之前就落盘了，
    之后无论重跑多少轮，它都静静地躺在库里当"永远做不对的题"：
    连跑 5 轮 0%、critique 一字不变 —— 看起来像"学习曲线起不来"，
    其实是**一道坏题把整条曲线钉死在地板上**。
    """
    items = load_generated()
    bad: list[dict[str, Any]] = []
    changed = False
    for rec in items:
        topic = str(rec.get("topic_id") or "")
        if not topic:
            continue
        try:
            vr = await validate(rec)
        except Exception as e:                               # pragma: no cover
            rec["invalid"] = True
            rec["invalid_reason"] = f"复核时报错：{type(e).__name__}: {e}"
            bad.append(rec); changed = True
            continue
        if not vr.get("ok") or vr.get("difficulty_gap"):
            rec["invalid"] = True
            rec["invalid_reason"] = "；".join(
                str(r) for r in (vr.get("reasons") or []))[:400]
            rec["invalid_note"] = ("复核判废（不是删除）：配方在**现在的**验题规则下"
                                   "过不了。留着是为了复盘，但不再进题库。")
            bad.append(rec); changed = True
        elif rec.get("invalid"):
            # 修好了就恢复 —— 复核是双向的
            rec.pop("invalid", None); rec.pop("invalid_reason", None)
            rec.pop("invalid_note", None)
            changed = True
    if changed:
        STORE.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    return bad


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
    lines = [
        f"  - 评分点「{c.get('point')}」错在：{c.get('why')}"
        + (f"\n      怎么改：{c['fix']}" if c.get("fix") else "")
        for c in vr.get("checks") or [] if not c.get("ok")
    ]
    # 难度缺口独立于评分点闸 —— 见 `validate` 闸 8 的注释：
    # 放在 `if ok:` 里的话，模型永远收不到这句反馈。
    gap = str(vr.get("difficulty_gap") or "").strip()
    if gap:
        lines.append(
            f"  - 【难度不达标】要求 {vr.get('difficulty')} 档：{gap}。"
            "要么把 SQL 改写成该档的形态，要么如实把 difficulty 调低 —— "
            "不要靠改题面文案蒙过去。")
    return "\n".join(lines)


async def generate_adopt(*, about: str = "", difficulty: int = 2,
                         tries: int = 3, do_adopt: bool = False,
                         verbose: bool = True,
                         focus_dimension: str = "") -> tuple[dict[str, Any] | None,
                                                             list[dict[str, Any]]]:
    """出题编排的**唯一入口**：生成 → 强制 → 验题 →（失败就回灌重试）→ 落盘。

    CLI 和 `run_mve` 都走这里 —— 编排只有一份，不会两边跑偏。
    返回 (落盘记录 or None, 每一轮的验题明细)。
    """
    existing, avoid_txt = existing_pairs()
    # 出题点由**服务端从图谱挑定**，不交给模型（照伴学：题型由知识点节点声明）。
    bp = _graph_blueprint(difficulty, avoid=existing,
                          prefer=str(focus_dimension or ""))
    if bp:
        # 服务端既已挑定，`about` 里 planner 给的"范围"要求就必须让位 ——
        # 实测两者打架：`about` 说"用队员级范围"，而图谱挑中的
        # max_losing_streak 是**回合级**口径（rounds 表根本没有队员列），
        # 模型夹在中间，把 fb_player 写进了 rounds 表，三次全挂。
        # 题点归服务端，题面归模型 —— 不能两头都听。
        about = (f"围绕「{bp.get('dimension')}」这个口径，按 "
                 f"{bp.get('subject_key')} 的粒度出一道新题")
        # 方向来自人导入的**边**时，把人的原题给模型当母本 —— 否则它只看到
        # 一个干巴巴的维度名，出的题跟人问的不是一回事（这是"影响自适应
        # 出题"真正要落的地方）。
        if bp.get("source") == "human_link" and bp.get("seed_question"):
            about += f"。参考人导入的原题：{bp['seed_question']}"
        if verbose:
            print(f"  图谱出题点 : {bp.get('dimension')} × {bp.get('subject_key')}"
                  f"｜表 {bp.get('table')}"
                  f"｜列 {'、'.join(bp.get('columns') or [])[:80]}")
    critique = ""
    trace: list[dict[str, Any]] = []
    for i in range(1, tries + 1):
        cand = enforce(await propose(about=about, difficulty=difficulty,
                                     critique=critique, avoid=avoid_txt,
                                     blueprint=bp))
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
        # 同一个方向连着撞墙两次 → 换个方向，别在一条死路上耗完次数。
        # 实测：出题器给的 about 是「围绕 kast_pct，用队员级范围出题」，
        # 而 kast_pct 的服务端口径是**队伍级**的 —— 模型只能硬去查
        # match_players_report（那是工具名不是表），三次全挂在同一个错上。
        # 只说"再改一次"它就会换个措辞重犯；必须显式让它换方向。
        if i >= 2:
            critique += ("\n  - 【换方向】这个角度已经连着失败了 "
                         f"{i} 次，很可能它在当前数据里根本不成立"
                         "（比如某个维度只支持队伍/比赛级，没有队员级口径）。"
                         "请换一个**服务端确实声明过**的维度或 subject 键重新出题，"
                         "不要在原方向上改措辞。")
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
    ap.add_argument("--revalidate", action="store_true",
                    help="回头复核已入库的生成题，在新的验题规则下过不了的标记判废")
    ap.add_argument("--loose", action="store_true",
                    help="只用合格线（VLML 跑一遍跑得出非空值），关掉合理性兜底闸")
    args = ap.parse_args()

    if args.revalidate:
        if args.loose:
            global STRICT_MODE
            STRICT_MODE = False
        bad = asyncio.run(revalidate_store())
        if not bad:
            print("存量复核：全部通过（没有被判废的题）")
            return 0
        print(f"存量复核：{len(bad)} 道题在新验题规则下过不了，已标记 invalid（不删，留证据）")
        for r in bad:
            print(f"  ✗ {r.get('topic_id')}：{str(r.get('invalid_reason'))[:200]}")
        return 1

    if args.loose:
        STRICT_MODE = False
        print("（--loose：只用合格线 —— VLML 跑一遍跑得出非空值即合格）")
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
