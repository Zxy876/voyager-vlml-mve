#!/usr/bin/env python3
"""代码化取证：Voyager 产出**调用 MCP 工具的代码**，而不是自己誊抄数字。

为什么要有这个模块（用户原话）
------------------------------
「取证的取我认为要回到 Voyager × MCP：Voyager 的实践调用工具层面和 Voyager ×
面板；Voyager 的旁路学习部分……这些你要看原版 Voyager 怎么产出相关 js 操作代码。」

原版 MineDojo/Voyager 的做法（`voyager/agents/action.py`）是一条完整链路：

  1. `render_system_message()` 把**控制原语的源码**拼进 system prompt
     （`load_control_primitives_context(base_skills) + skills`）；
  2. 模型按 `action_response_format.txt` 输出：
         Explain: ...
         Plan:
         1) ...
         Code:
         ```javascript
         // helper functions (only if needed, try to avoid them)
         async function yourMainFunctionName(bot) { ... }
         ```
  3. `process_ai_message()` 用正则抽 ```js 块 → **babel 解析 AST** →
     遍历 `program.body` 找 `FunctionDeclaration` → 取**最后一个 async 函数**
     作主函数 → 断言它**唯一参数名为 `bot`** → 拼出
     `program_code` 与 `exec_code = f"await {name}(bot);"`（解析失败重试 3 次）；
  4. 执行结果由**解释器**产出，观察**原样**读 events 字段回灌
     （`render_human_message`）。

对 MVE 的同构映射
------------------
| 原版 | MVE |
|---|---|
| `bot`（Mineflayer bot，对世界的操作句柄） | `mcp`（VLML MCP 工具句柄） |
| ```javascript 代码块 | ```python 代码块（宿主语言是 Python） |
| `babel.parse` 的 AST | `ast.parse` |
| 最后一个 `async function` | 最后一个 `async def` |
| 断言唯一参数名 `bot` | 断言唯一参数名 `mcp` |
| `exec_code = await name(bot);` | `await name(mcp)` |

为什么这一步能治好"取证不全"
----------------------------
之前的链路是「Voyager 调工具 → **LLM 读工具返回并誊写成 facts** → 判定读 facts」。
誊抄环节会丢东西：实测 `eco_win_rate` 在观测里根本不存在，模型照样交了一个
`None`，判定还把它当成答案去比对 —— 记成"值不可比"。

改成代码化之后：**值由代码从原始返回里按路径取**（`dig()`），不经过 LLM 的嘴。

    async def collect(mcp):
        s = await mcp.match_summary_report(series_id="2843069", team_name="Cloud9")
        p = await mcp.pattern_detection_report(series_id="2843069")
        return {"eco_win_rate": dig(p, "key_metrics.economy.eco.num"), ...}

返回值里的 `None` 就是**没取到**（工具真的没这个字段），而不是"模型抄漏了"。
这正是《取证通路-原版Voyager对照.md》里那句：**原版"原件进判定"，MVE"誊抄本进判定"**。
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import vlml_env  # noqa: E402  引导环境

# --------------------------------------------------------------------------
# MCP 句柄：对外的 10 个工具（MCP 工具名 == Python 函数名，
# 唯一例外：对外 query_sql / 实现 execute_custom_sql，见 vlml_env.py:65-67）
# --------------------------------------------------------------------------
_IMPL_NAME = {"query_sql": "execute_custom_sql"}

# MCP 对外暴露的 10 个工具（vlml/server.py 的 @mcp.tool 列表）
TOOL_NAMES = [
    "match_summary_report", "match_analysis_report", "match_players_report",
    "match_rounds_report", "match_economy_report", "pattern_detection_report",
    "player_profile_report", "scouting_report", "query_sql", "get_database_info",
]

SERIES = "2843069"
C9 = "Cloud9"
# 图名锚点。以前只有 SERIES / C9，于是模型把图名**写死**在 SQL 里 ——
# 换库后那张图根本不存在，技能复用全部跑 0 行（见 `stale_literals` 的实测）。
try:                                                     # pragma: no cover
    import anchor as _anchor
    MAP = str(_anchor.map_name() or "")
    SERIES = str(_anchor.series() or SERIES)
    C9 = str(_anchor.team() or C9)
except Exception:
    MAP = ""


def _tool_signatures() -> dict[str, str]:
    """从真实函数反射签名 —— **绝不手写**。

    手写签名的实测后果：`pattern_detection_report` 我写成
    `(series_id, team_name=None)`，而真实签名是
    `(team_name=None, player_name=None, tournament_name=None,
      series_ids=None, min_rounds=200)` —— 参数名根本对不上，
    探针调用直接 TypeError，结构文档拿不到，模型也就只能继续猜。
    """
    import inspect
    out: dict[str, str] = {}
    for name in TOOL_NAMES:
        fn = getattr(vlml_env, _IMPL_NAME.get(name, name), None)
        if fn is None:
            continue
        out[name] = f"{name}{inspect.signature(fn)}"
    return out


# 模块导入时反射一次（vlml_env 已在上面导入）
TOOL_SIGNATURES: dict[str, str] = _tool_signatures()


class MCPHandle:
    """给模型代码用的工具句柄（原版 `bot` 的同构物）。

    每个方法都是 async，返回工具的**原始返回**（不加工、不摘要）。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def _fn(self, name: str) -> Any:
        return getattr(vlml_env, _IMPL_NAME.get(name, name), None)

    def __getattr__(self, name: str) -> Any:
        if name not in TOOL_SIGNATURES:
            raise AttributeError(f"MCP 没有工具 {name}")
        fn = self._fn(name)
        if fn is None:
            raise AttributeError(f"工具 {name} 未加载")

        async def _call(**kwargs: Any) -> Any:
            try:
                res = await fn(**kwargs)
            except TypeError as e:
                # 参数名写错必须报出来 —— 原版靠这条让模型下一轮改正
                self.calls.append({"tool": name, "args": kwargs,
                                   "error": f"参数错误 {e}"})
                raise
            self.calls.append({"tool": name, "args": kwargs, "ok": True})
            return res

        return _call


# --------------------------------------------------------------------------
# prompt（照原版 action_response_format.txt 的结构）
# --------------------------------------------------------------------------
# 主函数名**不要**写成 `your_main_function_name` 这类占位符：实测模型会原样照抄
# 占位名（存进技能库后叫 `your_main_function_name`，复盘时看不出这是哪题的解法）。
# 给一个具体的示例名，模型就会照着起一个真名字。
RESPONSE_FORMAT = """Explain: 一两句话说明你打算怎么取这些数
Plan:
1) ...
2) ...
...
Code:
```python
# helper functions (only if needed, try to avoid them)
...
# main function after the helper functions
async def collect_stats(mcp):
    # 正确用法示例：先拿工具返回，再用 dig 按**点分路径**取值
    p = await mcp.<工具名>(<参数>)
    return {
        "<维度名>": dig(p, "<照抄结构文档里的路径>"),
    }
```
"""


# --------------------------------------------------------------------------
# 工具返回结构文档（原版 control_primitives 的同构物）
# --------------------------------------------------------------------------
# 原版把控制原语的**源码**拼进 system prompt，模型因此知道 `bot` 上有哪些方法、
# 返回什么。MVE 不这样做的话，模型只能**猜**工具返回里的字段名 ——
# 实测后果：四个维度全部 dig 到 None，因为猜的路径（`economy.pistol_win_rate`）
# 跟真实路径（`key_metrics.economy.pistol.num`）根本不一样。
#
# 这不是"泄题"：给的是**结构**（哪个字段在哪），不是**值**。
# 真实场景里 agent 拿得到 API 文档，原版 Voyager 也给。
SCHEMA_CACHE = HERE / "tool_schemas.json"

# 抽取上限（写进缓存的全量骨架）。
# 实测踩过：这里原来是 60，`match_analysis_report` 正好 61 条，于是
# `key_metrics.team.consistency.kast.num` **在抽取阶段就被砍掉了** ——
# 模型在 prompt 里永远看不到正确路径，只能在看得见的那 60 条里挑一条
# 长得像的（`team_comparison.Cloud9.kast.num`，还抄漏了 consistency），
# 三次 critic 重试也只是在同一条错路上打转。抽取阶段不能砍。
_MAX_PATHS = 400
_MAX_DEPTH = 6

# 展示预算（进 prompt 的条数）。抽取全留 ≠ 全展示：
# 61 条一起塞进 prompt，正确那条会被淹没在 `team_comparison.NRG.*` 这类
# 无关分支里（实测模型就是被它们带偏的）。所以按「与本题维度相关」聚焦。
_RENDER_BUDGET = 60   # 一个维度都没命中时的兜底展示条数
_FOCUS_BUDGET = 40    # 命中维度时的聚焦展示条数
_SIBLINGS_PER_PARENT = 8   # 同父兄弟的配额


def _leaf_paths(obj: Any, prefix: str = "", depth: int = 0,
                out: list[str] | None = None) -> list[str]:
    out = out if out is not None else []
    if len(out) >= _MAX_PATHS or depth > _MAX_DEPTH:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)) and v:
                _leaf_paths(v, p, depth + 1, out)
            else:
                if p not in out:
                    out.append(p)
    elif isinstance(obj, list) and obj:
        _leaf_paths(obj[0], f"{prefix}.0", depth + 1, out)
    return out


def load_schemas() -> dict[str, list[str]]:
    try:
        raw = json.loads(SCHEMA_CACHE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------
# 结构文档的「聚焦裁剪」：把正确路径挑出来，而不是把整个返回骨架截断
# --------------------------------------------------------------------------
# 原版 Voyager 不需要这一步 —— control_primitives 是几十个小函数，全给就行。
# MVE 不一样：`match_analysis_report` 一个工具就有 61+ 条叶子路径，全给会让
# 正确那条被埋掉，按固定条数截断又会把正确那条砍掉（两个方向都实测翻过车）。
# 所以按**维度名**检索：命中优先 → 补同父兄弟（看得见"同层两种混放"）→
# 补顶层骨架（看得懂整体形状）→ 剩余预算才给其它分支。
_STOP_TOKENS = {"pct", "rate", "ratio", "avg", "mean", "total", "num", "denom",
                "count", "value", "index", "score", "check", "analysis"}


def _tokens(dim: str) -> list[str]:
    """维度名 → 匹配用的词。去掉口径后缀，只留指标本体。

    `kast_pct` → ["kast"]、`kd_ratio` → ["kd"] —— `pct`/`ratio` 是口径不是
    指标名，留着会命中一大片无关路径。
    """
    parts = re.split(r"[^a-z0-9]+", str(dim or "").lower())
    return [p for p in parts if len(p) >= 2 and p not in _STOP_TOKENS]


def _stem_of(path: str) -> str:
    """去掉 num/denom 口径后缀，留下指标本体：`....kast.num` → `....kast`。"""
    for suf in (".num", ".denom"):
        if path.endswith(suf):
            return path[: -len(suf)]
    return path


def _uses_declared(used: list[str], declared: str) -> bool:
    """模型有没有按声明的口径取。

    百分比指标声明的是 `...kast.num`，模型写 `pct(p, "...kast.num")` 算命中，
    写 `pct(p, "...kast")` 由 pct 自己推 denom 也算命中（都比到 stem）。
    """
    stem = _stem_of(declared)
    return any(u == declared or _stem_of(u) == stem for u in used)


def _same_metric(a: str, b: str) -> bool:
    """两条路径是不是指向同一个指标（`kd` 与 `kd_ratio` 算同一个）。"""
    ta = set(_tokens(_stem_of(a).rsplit(".", 1)[-1]))
    tb = set(_tokens(_stem_of(b).rsplit(".", 1)[-1]))
    return bool(ta & tb)


def _candidates(schemas: dict[str, list[str]], dims: list[str],
                limit: int = 12) -> list[str]:
    """critic 回灌用：返回里真实存在、名字里含该维度词的路径。"""
    out: list[str] = []
    for d in dims:
        toks = _tokens(d)
        if not toks:
            continue
        for paths in schemas.values():
            for p in paths:
                if p in out:
                    continue
                if any(t in p.lower() for t in toks):
                    out.append(p)
    return out[:limit]


def _parent(path: str) -> str:
    return path.rsplit(".", 1)[0] if "." in path else ""


def focus_paths(paths: list[str], dims: list[str] | None,
                prefer: list[str] | None = None,
                budget: int = _FOCUS_BUDGET) -> tuple[list[str], list[str]]:
    """把 paths 分成（与维度相关的、其余的）。

    返回的两段都由调用方决定怎么展示 —— 相关那段要**完整**给，
    其余那段只在预算富余时给（它们是干扰项的来源）。
    `prefer` 是题干声明的口径路径，永远排最前并打星。
    """
    if not paths:
        return [], []
    toks: list[str] = []
    for d in (dims or []):
        for t in _tokens(d):
            if t not in toks:
                toks.append(t)
    if not toks:
        return [], list(paths)

    hit = [p for p in paths if any(t in p.lower() for t in toks)]
    if not hit:
        return [], list(paths)

    # 同父兄弟：让模型看得见「同一个父节点下 kast 是 num/denom、kd 是标量」
    # 这种混放。只给命中那一条，模型照样会把 kast 的套路套到 kd 上。
    # 每个父节点限 8 条：不设限的话 `player_performance.0` 一个父节点就能
    # 拉进 20 条选手字段，把真正要用的那几条淹掉。
    by_parent: dict[str, list[str]] = {}
    for p in paths:
        by_parent.setdefault(_parent(p), []).append(p)
    sibs: list[str] = []
    for par in {_parent(p) for p in hit}:
        for p in by_parent.get(par, [])[:_SIBLINGS_PER_PARENT]:
            if p not in sibs:
                sibs.append(p)
    # 顶层骨架（depth<=1）：让模型知道返回大概长什么样，不至于把路径编出根
    head = [p for p in paths if p and "." not in p][:8]

    focus: list[str] = []
    for p in hit + sibs + head:
        if p not in focus:
            focus.append(p)
    # 题干声明的口径排到最前：它在文档里排第几，模型就照第几条写 ——
    # 实测声明路径排在 `team_comparison.Cloud9...` 后面时，模型挑了后者。
    if prefer:
        focus = [p for p in prefer if p in focus] + \
                [p for p in focus if p not in set(prefer)]
    rest = [p for p in paths if p not in set(focus)]
    return focus[:budget], rest


async def probe_schemas(tools: list[str], *, force: bool = False) -> dict[str, list[str]]:
    """调一次工具，把它返回的**结构骨架**（叶子路径，不含值）抽出来缓存。"""
    cache = load_schemas()
    todo = [t for t in tools if force or t not in cache]
    if not todo:
        return {t: cache[t] for t in tools if t in cache}

    handle = MCPHandle()
    for t in todo:
        # 按**真实签名**填默认参数：series_id / series_ids / team_name 各自不同，
        # 靠"字符串里有没有 series_id"这种判断会错（series_ids 也含该子串）。
        args = _default_args(t)
        try:
            fn = getattr(handle, t)
            res = await fn(**args)
            cache[t] = _leaf_paths(res if isinstance(res, dict) else {"result": res})
        except Exception as e:
            cache[t] = [f"<探针失败：{type(e).__name__}: {str(e)[:80]}>"]
    SCHEMA_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    return {t: cache[t] for t in tools if t in cache}


def _default_args(tool: str) -> dict[str, Any]:
    """按真实参数名给探针填默认值（照 inspect.signature，不靠字符串猜）。"""
    import inspect
    fn = getattr(vlml_env, _IMPL_NAME.get(tool, tool), None)
    if fn is None:
        return {}
    params = inspect.signature(fn).parameters
    args: dict[str, Any] = {}
    for name, p in params.items():
        required = p.default is inspect._empty
        # 探针的目的是**拿到返回结构**，所以可选参数也要给几个有意义的：
        # 实测 pattern_detection_report 全参数都有默认值，一个都不传它会返回
        # error（拿不到结构）；裁判那边传的是 team_name=C9，照它来。
        if name == "team_name":
            args[name] = C9
        elif name == "series_id":
            args[name] = SERIES
        elif name == "player_name" and required:
            args[name] = ""
        elif name == "sql_query":
            args[name] = "SELECT 1"
        elif required:
            args[name] = SERIES if "series" in name else ""
    return args


def render_tool_docs(schemas: dict[str, list[str]],
                     dims: list[str] | None = None,
                     prefer: list[str] | None = None) -> str:
    """把工具返回结构拼进 prompt —— **按维度聚焦，不再按固定条数截断**。"""
    if not schemas:
        return ""
    stars = set(prefer or [])
    blocks = []
    for t, paths in schemas.items():
        focus, rest = focus_paths(paths, dims, prefer)
        shown = "\n".join(f"    {'★ ' if p in stars else '  '}{p}"
                          for p in focus[:_FOCUS_BUDGET])
        if focus:
            head = (f"await mcp.{TOOL_SIGNATURES.get(t, t)} 返回结构"
                    f" —— **与本题维度相关的路径**（★ = 题干声明的口径，"
                    f"必须用它）：")
            blocks.append(f"{head}\n{shown}")
            if rest:
                # 其余分支只给条数提示，不铺开：铺开就是干扰项
                # （实测 `team_comparison.NRG.*` 把模型带偏过）。
                blocks.append(
                    f"    （另有 {len(rest)} 条与本题维度无关的路径，"
                    f"多为对手/其它分支 —— 不要用）")
        else:
            shown = "\n".join(f"    {p}" for p in paths[:_RENDER_BUDGET])
            blocks.append(f"await mcp.{TOOL_SIGNATURES.get(t, t)} 返回结构"
                          f"（叶子路径，共 {len(paths)} 条，展示前 "
                          f"{min(len(paths), _RENDER_BUDGET)} 条）：\n{shown}")
    # 光给叶子路径不够 —— 原版 control_primitives 里是有**用法示例**的，
    # 模型照着示例才会写对。实测踩过：query_sql 的返回是
    # {"columns": [...], "rows": [[...]]}，模型却写了 res["rounds"]，
    # 三次重试全栽在同一个 KeyError 上，整题 0%。
    if "query_sql" in schemas:
        blocks.append(
            "⚠️ query_sql 的返回**不是**按表名组织的字典，固定是：\n"
            "    {\"columns\": [\"col1\", \"col2\"], \"rows\": [[v1, v2], [v3, v4]]}\n"
            "    取值必须走 rows：\n"
            "        res = await mcp.query_sql(sql_query=\"SELECT ...\")\n"
            "        first_row = res[\"rows\"][0]        # 第一行\n"
            "        value    = first_row[0]            # 第 0 列\n"
            "    不要写 res[\"rounds\"] / res[\"rows\"][0][\"cnt\"] —— rows 里是**数组**不是字典。")
    return "\n\n".join(blocks)


def graph_hints(dims: list[str] | None, task: Any = None) -> str:
    """图谱给的「维度 → 由哪个工具产出 + 按哪条路径取值」。

    没有它模型会选错工具（实测：economy 类维度去调 match_economy_report，
    而图谱声明它们由 pattern_detection_report 产出）。
    """
    if not dims:
        return ""
    try:
        import knowledge_graph
        g = knowledge_graph.KnowledgeGraph.load()
    except Exception:
        g = None
    lines = []
    for d in dims:
        prod = list(g.who_produces(d)) if g else []
        # 必须给到 **value_path**，不能只给工具名。
        # 实测踩过：只说"kast_pct 由 match_analysis_report 产出"，模型就在
        # 返回结构里挑了一条长得像的路径 `team_comparison.Cloud9...`，
        # 而真值口径在 `key_metrics.team...` —— 挑错分支就 dig 到 None。
        # 给路径属于"给结构"不是"给值"（图谱里早就这么给了），不算泄题。
        node = g.nodes.get(f"dim:{d}") if g else None
        vp = str((node.detail.get("value_path") if node else "") or "")
        if not prod or not vp:
            # 图谱缺这个维度时的兜底：**题目自己的 rubric 才是权威口径来源**。
            # 图谱是 tasks.py 的缓存（`build()` 读的就是 answer_spec），
            # 缓存过期不该让模型退回猜路径 —— 实测 kast_adr_check 就是因为
            # 图谱没重建，提示整段为空，模型连着三轮都在错路上重试。
            spec = _spec_of(task, d)
            if spec:
                prod = prod or [spec[0]]
                vp = vp or spec[1]
        line = f"    {d} → 调 {' 或 '.join(prod)}" if prod else ""
        if vp:
            line += (f"，按路径取值 {vp}"
                     + ("（百分比：另有同层 .denom 作分母）" if spec_percent(task, d)
                        else "（标量：dig 到就是最终值，不要再除）"))
        if line:
            lines.append(line)
    if not lines:
        return ""
    return "\n\n知识图谱声明（维度由哪个工具产出）：\n" + "\n".join(lines)


def declared_paths(task: Any, dims: list[str]) -> dict[str, str]:
    """题干声明的口径：维度 → 取值路径。

    权威来源是图谱（它由 tasks.py 的 answer_spec 构建），图谱缺这个维度时
    直接读题目自己的 rubric —— 实测 `kast_adr_check` 加进 tasks.py 后图谱
    没重建，提示整段为空，模型连着三轮在错分支上重试。

    为什么必须是**硬约束**而不是提示：`match_analysis_report` 里
    `team_comparison.*.consistency` 是一支**占位值**分支（Cloud9 与 NRG 的
    kd_ratio 都是 1.0、adr 分母都是 59），而权威段 `key_metrics.team` 才是
    1.24 / 109。两条路都"取得到值"，光看"空不空"根本区分不出来 ——
    所以声明的口径必须拿来筛分支，不能只拿来建议。
    """
    out: dict[str, str] = {}
    try:
        import knowledge_graph
        g = knowledge_graph.KnowledgeGraph.load()
    except Exception:
        g = None
    for d in dims:
        vp = ""
        if g is not None:
            node = g.nodes.get(f"dim:{d}")
            vp = str((node.detail.get("value_path") if node else "") or "")
        if not vp:
            spec = _spec_of(task, d)
            vp = spec[1] if spec else ""
        if vp:
            out[d] = vp
    return out


def _spec_of(task: Any, dim: str) -> tuple[str, str] | None:
    """从**题目自己的 rubric** 读 (tool, value_path) —— 不依赖图谱缓存。"""
    for p in (getattr(task, "rubric", None) or []):
        if str(getattr(p, "dimension", "") or "") != dim:
            continue
        s = getattr(p, "answer_spec", None)
        if s is None:
            continue
        tool = str(getattr(s, "tool", "") or "")
        vp = str(getattr(s, "value_path", "") or "")
        if tool and vp:
            return tool, vp
    return None


def spec_percent(task: Any, dim: str) -> bool:
    for p in (getattr(task, "rubric", None) or []):
        if str(getattr(p, "dimension", "") or "") != dim:
            continue
        s = getattr(p, "answer_spec", None)
        if s is not None and bool(getattr(s, "percent", False)):
            return True
    return False


def render_system_message(tool_docs: str = "", dim_hints: str = "",
                          skills_text: str = "") -> str:
    """照原版 `render_system_message`：把可用的"控制原语"源码拼进 system。

    原版拼的是 `load_control_primitives_context(base_skills) + skills`
    （`voyager/agents/action.py`）—— **两个都有**：先给稳定 API（原语），
    再给**已学会技能的源码**。之前这里只有原语那一半（tool_docs + graph_hints），
    技能库里学到的解法源码没进来，于是"学"和"用"断成两截：
    旁路学到的源码躺在技能库里，写代码时一句都看不到。

    `dim_hints` 是图谱给的「哪个维度由哪个工具产出」—— 原版没有这一项
    （它靠技能库里的 JS 函数名体现），MVE 有图谱就直接给，省得模型选错工具。
    """
    sigs = "\n".join(f"await mcp.{s}" for s in TOOL_SIGNATURES.values())
    return f"""你是一名数据分析 agent，通过写代码调用 MCP 工具来取证。

可用的 MCP 工具（全部 async，返回 dict）：
{sigs}
{tool_docs}

取值的辅助函数已经备好（**按指标类型分两个入口，不要混用**）：
    scalar(obj, "a.b.c")              —— **标量**（如 kd = 1.24）：dig 到就是最终值
    pct(obj, "a.b.num", "a.b.denom")  —— **百分比**（如 kast）：返回 {{"num":..,"denom":..}}
    dig(obj, "a.b.0.c")               —— 通用按点分路径取值，取不到返回 None

⚠️ 先判断指标是标量还是百分比，再决定用哪个：
   同一个父节点下常常两种混着放（实测：consistency 下 kast 是 num/denom、kd 是标量）。
   把标量路径传给 pct() 会拿到 denom=None —— 那时请改用 scalar()。
   **不要用别的指标的 num/denom 去除出当前指标**，那是取错，不是算对。

**路径必须从上面的返回结构里照抄**，不要自己编字段名。

⚠️ 取值只能用 dig —— **不要写 obj.get("a.b.c")**：点分路径不是一个 key，
字典的 key 里没有点号。实测踩过：模型写了
`r.get("key_metrics.economy.eco.num", 0)`，get 取不到就退回默认值 0，
于是交上来的是 **0 而不是"没取到"** —— 那比留 None 更糟，
因为它把"没取到"伪装成了"取到了，值是 0**。

已预置的常量（直接用，不要再自己定义）：
    SERIES = "{SERIES}"      # 本场 series_id
    C9     = "{C9}"          # 队伍名
    MAP    = "{MAP}"         # 图名
{dim_hints}
{skills_text}
硬性要求：
1. 主函数必须是 async，且**唯一参数名为 mcp**；
2. 用 return 返回一个 dict，key 是维度名，value 是从工具返回里**取出来的原值**；
3. **不要把数字手写在代码里** —— 所有值必须来自工具返回；
4. 取不到就让它返回 None —— 那表示"这个维度不在观测里"，比编一个数有用得多；
5. **实体值一律用上面的常量，不要写死字面量** —— 这是"技能能不能复用"的
   分水岭。原版 Voyager 的技能函数是可带参数的（`mineBlock(bot, name,
   count)`，值在调用点传），所以同一个技能换个材料照样能用；写死了值，
   技能就退化成"那一次的答案"。实测：技能里写死
   `series_id=2843069 / winning_team_name='Cloud9' / map_name='Lotus'`，
   换库之后这些值一个都不存在 → 复用时全部 0 行 → 考核覆盖率回 0
   （面板：摸底 16% → 结业 0%）。
   正确写法（用 f-string 拼进 SQL）：
       f"... WHERE series_id='{{SERIES}}' AND map_name='{{MAP}}'"
   错误写法：`... WHERE series_id='2843069'`

输出格式：
{RESPONSE_FORMAT}"""


def render_human_message(*, task: Any = None, code: str = "", error: str = "",
                         critique: str = "", missing: list[str] | None = None,
                         dims: list[str] | None = None) -> str:
    """照原版 `render_human_message`：缺哪项就显式写 None，不留想象空间。"""
    lines = [
        f"任务：{getattr(task, 'question', '') or '（无）'}",
        "",
        f"需要覆盖的维度：{', '.join(dims or []) or '（无）'}",
        f"上一轮没取到的维度：{', '.join(missing or []) or 'None'}",
        "",
        f"上一轮的代码：\n{code}" if code else "上一轮的代码：No code in the first round",
        "",
        f"执行错误：\n{error}" if error else "执行错误：No error",
        "",
        f"Critique：{critique}" if critique else "Critique：None",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 解析（原版 process_ai_message 的同构物）
# --------------------------------------------------------------------------
_CODE_BLOCK = re.compile(r"```(?:python|py)(.*?)```", re.DOTALL)


def is_empty(value: Any) -> bool:
    """维度算不算"没取到"。

    pct() 的返回是 {"num","denom"} 两个数，**两个都空才算没取到**；
    只有一个为空是另一回事（口径上分母本来就可能不存在），不能当成取错。
    """
    if value is None:
        return True
    if isinstance(value, dict):
        keys = {"num", "denom"}
        if keys & set(value):
            return all(value.get(k) is None for k in keys & set(value))
        return len(value) == 0
    if isinstance(value, str):
        return not value.strip()
    return False


def paths_in_code(code: str) -> list[str]:
    """把模型代码里写过的取值路径抽出来（AST，不靠正则）。

    用途：critic 回灌时要能指着说"你用的这条路径不在返回里"。
    只能说"路径不对"而不给候选，模型就只能原地重试 —— 实测三轮回灌，
    它三次交出一模一样的 `team_comparison.Cloud9.kast.num`：
    它不知道还有别的路可走。
    """
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in ("dig", "pct", "scalar"):
            for a in node.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    if a.value not in out:
                        out.append(a.value)
    return out


class CodeParseError(ValueError):
    """解析失败。必须抛出来让模型下一轮改正，不能静默降级。"""


def parse_code(text: str) -> dict[str, Any]:
    """抽代码块 → ast 解析 → 找主函数 → 断言参数名 → 生成 exec_code。

    与原版 `process_ai_message` 一一对应，只是 AST 库换成 `ast`。
    """
    blocks = _CODE_BLOCK.findall(text or "")
    code = "\n\n".join(b.strip() for b in blocks if b.strip())
    if not code:
        # 宽容一次：没有围栏时把整段当代码（原版只认围栏，这里略放宽）
        code = (text or "").strip()
    if not code:
        raise CodeParseError("没有找到 ```python 代码块")

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise CodeParseError(f"语法错误 {e}") from e

    funcs = [n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not funcs:
        raise CodeParseError("没有找到任何函数定义")

    # 找最后一个 async 函数作主函数（照原版 "find the last async function"）
    main = None
    for f in reversed(funcs):
        if isinstance(f, ast.AsyncFunctionDef):
            main = f
            break
    if main is None:
        raise CodeParseError("主函数必须是 async def（原版：Your main function must be async）")

    params = [a.arg for a in main.args.args]
    if params != ["mcp"]:
        raise CodeParseError(
            f"主函数 {main.name} 必须只接受一个名为 mcp 的参数，实际是 {params}"
            "（原版：must take a single argument named 'bot'）")

    return {
        "program_code": code,
        "program_name": main.name,
        "exec_code": f"await {main.name}(mcp)",
    }


_SAFE_BUILTINS = {
    "abs": abs, "min": min, "max": max, "sum": sum, "len": len, "round": round,
    "int": int, "float": float, "str": str, "bool": bool, "list": list,
    "dict": dict, "set": set, "tuple": tuple, "sorted": sorted, "range": range,
    "enumerate": enumerate, "zip": zip, "isinstance": isinstance, "print": print,
}


def dig(obj: Any, path: str) -> Any:
    """按点分路径取值（与 loop_core.dig 同一份语义）。"""
    cur = obj
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        elif isinstance(cur, (list, tuple)) and part.isdigit():
            i = int(part)
            if i >= len(cur):
                return None
            cur = cur[i]
        else:
            return None
    return cur


def scalar(obj: Any, path: str) -> Any:
    """标量指标：dig 到的就是最终值，不用再算。

    与 `pct` 分开成两个函数，是刻意的——实测踩过一次，代价是连着 48 轮停在 50%：
    `key_metrics.team.consistency` 下 kast 是 {num,denom}、kd 是标量 1.24，
    两者长得太像，模型把 kast 的 num/denom 套路套到标量 kd 上，交出 109/109=1.0。
    只靠注释说"这个不是 num/denom"挡不住；**类型层面分开两个入口才挡得住**——
    标量路径传给 pct() 拿不到 denom，模型就只能改调 scalar()。
    """
    return dig(obj, path)


def pct(obj: Any, num_path: str, den_path: str = "") -> dict[str, Any]:
    """百分比指标：返回 {"num":..,"denom":..}，**绝不自己算好再返回一个数**。

    为什么必须返回两个数：insights_reference.md:130 是硬规则——
    67% (16/24) 与 67% (2/3) 数值相同但不是同一个事实，分母参与评分。
    只交分子的实测后果：模型 dig 到 num=3 就交上来（3 ≠ 50.0）。
    """
    num = dig(obj, num_path)
    den = dig(obj, den_path) if den_path else None
    if den is None and str(num_path).endswith(".num"):
        den = dig(obj, str(num_path)[: -len(".num")] + ".denom")
    return {"num": num, "denom": den}


# 全大写的 SQL 关键字/函数名：它们长得像"大写开头的实体名"，但根本不是值。
# 少了这张表，`ORDER BY cnt DESC` 里的 'DESC' 会被当成图名去查库、查不到
# 就整条技能判过期 —— 那是误杀。
_SQL_STOPWORDS = frozenset("""
SELECT FROM WHERE GROUP ORDER BY HAVING LIMIT OFFSET JOIN LEFT RIGHT INNER
OUTER ON AS AND OR NOT IN IS NULL LIKE BETWEEN CASE WHEN THEN ELSE END
ASC DESC COUNT SUM AVG MIN MAX DISTINCT OVER PARTITION ROW_NUMBER RANK LAG
LEAD COALESCE CAST ROUND TRUE FALSE UNION ALL WITH INTERVAL DATE EXTRACT
""".split())

_IN_DB_CACHE: dict[str, bool] = {}


def _value_in_db(value: str) -> bool:
    """这个实体值在**当前库**里到底有没有（带缓存）。"""
    v = str(value or "").strip()
    if not v or v in _IN_DB_CACHE:
        return _IN_DB_CACHE.get(v, False)
    ok = False
    try:
        import duckdb
        import anchor as _anc
        con = duckdb.connect(str(_anc._db_path()), read_only=True)
        try:
            n = int(con.execute(
                "SELECT COUNT(*) FROM rounds WHERE "
                "CAST(series_id AS VARCHAR)=? OR winning_team_name=? "
                "OR losing_team_name=? OR map_name=?",
                [v, v, v, v]).fetchone()[0])
            ok = n > 0
        finally:
            con.close()
    except Exception:
        ok = True          # 查不动就别误杀，交给执行结果去判
    _IN_DB_CACHE[v] = ok
    return ok


def stale_literals(code: str) -> list[str]:
    """技能代码里**写死但当前库已经没有**的实体值 —— 空列表表示没过期。

    为什么必须有这条：技能库里存的是"当时跑通的那段代码"，值写死在里面。
    换数据源（events → vlr → rib）之后，旧值（2843069 / Cloud9 / Lotus /
    Corrode）在当前库里**一个都不存在**，但检索照样会把这条技能捞出来、
    照样 exec、照样跑完不报错 —— 只是**每一行都是 0 行**。
    实测：考核 26 次里 16 次复用了技能，覆盖率却还是
    `0.0, 0.25, 0.0, ...` 一路回零 —— 复用成功 ≠ 做得对。

    判据（"这个字面量像不像实体值"）：
      · 4 位以上纯数字 → 赛事号
      · 大写开头的英文串 → 队名 / 图名（排除 SQL 关键字）
      · entity_catalog 里登记过的值
    **不能只认 catalog**：换库之后 GRID 那批队名图名（Cloud9 / Lotus /
    Corrode）已经从 catalog 里消失了，只查 catalog 会一条都检不出来
    （第一版就是这个毛病，实测 8 条技能判出 0 条过期）。
    """
    s = str(code or "")
    if not s:
        return []
    known: set[str] = set()
    try:
        cat = json.loads((Path(__file__).resolve().parent
                          / "entity_catalog.json").read_text(encoding="utf-8"))
        for vals in (cat.get("entities") or {}).values():
            if isinstance(vals, list):
                known |= {str(v) for v in vals if str(v)}
    except Exception:
        pass
    out: list[str] = []
    # 裸数字也要查：实测 `series_id=2843069` 是**不带引号**写的，
    # 只扫引号会整条漏掉（lotus_win_rate 就只判出 'Cloud9' 一个）。
    lits = set(re.findall(r"'([^']{3,40})'", s)) \
        | set(re.findall(r'"([^"]{3,40})"', s)) \
        | set(re.findall(r"\b(\d{4,})\b", s))
    for lit in lits:
        if lit in _SQL_STOPWORDS:
            continue
        looks_entity = (re.fullmatch(r"\d{4,}", lit) is not None
                        or re.fullmatch(r"[A-Z][A-Za-z0-9 .'\-]{2,}", lit) is not None
                        or lit in known)
        if not looks_entity:
            continue                       # 'num' / 'denom' 这类路径，不查
        if not _value_in_db(lit):
            out.append(lit)
    return sorted(out)


def _subject_of(task: Any) -> dict[str, Any]:
    """从题的 rubric 里收集当前实体（series / team / map）。"""
    out: dict[str, Any] = {}
    for p in (getattr(task, "rubric", None) or []):
        for k, v in ((getattr(p, "subject", None) or {}) or {}).items():
            if isinstance(v, (list, tuple)):
                v = v[0] if v else None
            if v not in (None, ""):
                out.setdefault(str(k), v)
    return out


def _subject_env(subject: dict[str, Any] | None) -> dict[str, str]:
    """把当前这道题的 subject 翻成注入常量。

    原版 Voyager 是两层：主函数 `main(bot)` 每次生成，**技能函数可带参数**
    （`mineBlock(bot, name, count)`，值在调用点传）—— 所以技能是通用方法。
    MVE 的主函数被硬约束成单参 `mcp`（`_parse` 里 `params != ["mcp"]` 就报
    错），改签名要动整条链路。同构的等价做法是：**把值放进exec 的命名空间**，
    技能代码引用 `SERIES` / `C9` / `MAP`，值由**调用方**注入 —— 同样是
    "值在调用点给"，只是载体从参数换成环境。
    """
    env = {"SERIES": SERIES, "C9": C9, "MAP": MAP}
    for k, name in (("series", "SERIES"), ("team", "C9"), ("map", "MAP")):
        v = (subject or {}).get(k)
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        if v not in (None, ""):
            env[name] = str(v)
    return env


async def execute(program_code: str, program_name: str,
                  subject: dict[str, Any] | None = None) -> dict[str, Any]:
    """执行模型产出的代码，返回它 return 的原始 dict。

    照原版：执行结果由**解释器**产出，不经过任何模型加工。

    `subject` = 当前题的实体（series / team / map）。传了就按它覆盖注入
    常量 —— 同一个技能换个队伍/换张图也能跑，这才是"学会了"而不是"背下了"。
    """
    mcp = MCPHandle()
    env = _subject_env(subject)
    ns: dict[str, Any] = {"__builtins__": dict(_SAFE_BUILTINS),
                          "mcp": mcp, "dig": dig,
                          "scalar": scalar, "pct": pct,
                          # 预置常量：不预置的话模型会自己写 `series_id=...`
                          # 然后 NameError（实测踩过）。
                          **env}
    exec(program_code, ns)  # noqa: S102  —— 原版就是这样执行模型代码的
    fn = ns.get(program_name)
    if fn is None:
        raise CodeParseError(f"代码里没有 {program_name}")
    try:
        result = await fn(mcp)
    except CodeParseError:
        raise
    except Exception as e:
        # **所有**执行期异常都要转成可回灌的错误，不能崩出去。
        # 实测踩过：模型写了未定义的变量（NameError），只捕 TypeError 的话
        # 异常会一路抛穿 collect()，重试机制根本没机会让模型改。
        raise CodeParseError(
            f"执行失败：{type(e).__name__}: {e}"
            + ("（参数名写错？工具签名见上）" if isinstance(e, TypeError) else
               "（用了未定义的名字？series_id 等常量已预置，见 system 提示）")
        ) from e
    return {
        "values": result if isinstance(result, dict) else {"_result": result},
        "calls": mcp.calls,
        "tools": [c["tool"] for c in mcp.calls if c.get("ok")],
    }


# --------------------------------------------------------------------------
# 一次完整的代码化取证（带重试，照原版 retry=3）
# --------------------------------------------------------------------------
async def collect(task: Any, *, dims: list[str] | None = None,
                  code: str = "", error: str = "", critique: str = "",
                  missing: list[str] | None = None,
                  retries: int = 3, tools: list[str] | None = None,
                  skills_text: str = "", hint: str = "") -> dict[str, Any]:
    """让模型写一段调 MCP 的代码，执行它，拿回原始值。

    `hint` 是出题器按掌握度算出来的**支架档位**（见 `difficulty.py`）：
    full 给足（★ 声明口径 + critic 给候选），none 全收（只说取空了）。
    MVE 的题目难度是写死的，梯度只能落在支架上 —— 从扶到放。
    """
    import difficulty
    from llm_client import chat

    # 先确定这份题该看哪些工具的结构文档：题目声明的 + 图谱里点名的。
    # 不 probe 的话模型只能猜字段名（实测四个维度全 dig 到 None）。
    if tools is None:
        tools = list(getattr(task, "requires_tools", []) or [])
        try:
            import knowledge_graph
            g = knowledge_graph.KnowledgeGraph.load()
            for d in (dims or []):
                tools += list(g.who_produces(d))
        except Exception:
            pass
        tools = sorted(set(t for t in tools if t in TOOL_SIGNATURES)) or list(TOOL_SIGNATURES)

    schemas = await probe_schemas(tools)
    # 支架档位决定「给多少」：会了就收，不会才扶。
    # 空串（未指定）按 full 处理 —— 保持旧行为，不回退。
    hint = str(hint or "") or difficulty.HINT_FULL
    declared = declared_paths(task, dims or []) \
        if difficulty.hint_allows(hint, "declared") else {}
    messages = [
        {"role": "system", "content": render_system_message(
            render_tool_docs(
                schemas,
                dims if difficulty.hint_allows(hint, "focus") else None,
                list(declared.values())),
            graph_hints(dims, task) if difficulty.hint_allows(hint, "declared") else "",
            skills_text)},
        {"role": "user", "content": render_human_message(
            task=task, code=code, error=error, critique=critique,
            missing=missing, dims=dims)},
    ]
    last_err = ""
    for attempt in range(retries):
        text = chat(messages, temperature=0.2)
        try:
            parsed = parse_code(text)
        except CodeParseError as e:
            last_err = str(e)
            # 把解析错误回灌，让模型下一轮改正（照原版解析失败也重试）
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user",
                             "content": f"解析失败：{last_err}\n请重新输出，注意格式要求。"})
            continue
        try:
            out = await execute(parsed["program_code"], parsed["program_name"],
                                subject=_subject_of(task))
        except CodeParseError as e:
            last_err = str(e)
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user",
                             "content": f"执行失败：{last_err}\n请修正后重出一段完整代码。"})
            continue

        # ---- 原版 critic 环节的同构物：跑通了 ≠ 取对了 ----
        #
        # 此前只在「代码跑不起来」时重试；代码跑通就直接返回，于是
        # "路径取错、值取成 None"这类错误**根本没有修正机会** —— 它既不是
        # 解析错误也不是执行错误，静悄悄地就交上去了。
        # 原版是：执行 → critic 判断有没有完成 → 没完成就把观察回灌 → 再写。
        # 这里同构：执行 → 看维度是不是空/是不是取错分支 → 回灌 → 再写。
        used = paths_in_code(parsed["program_code"])
        missing_now = [d for d in (dims or [])
                       if is_empty(out.get("values", {}).get(d))]
        # 取到值 ≠ 取对分支：`team_comparison.*` 那支是占位值（kd_ratio 恒 1.0），
        # 照样能 dig 出数字。所以口径不符要**单独**判，不能只看空不空。
        off_spec = [d for d in (dims or [])
                    if d in declared and not _uses_declared(used, declared[d])]
        problem = sorted(set(missing_now) | set(off_spec))
        if problem and attempt < retries - 1:
            # 反馈必须带**候选路径**：只说"路径不对"，模型没有可改的方向，
            # 实测三轮交出一模一样的错路径。这里把返回里真实存在的、
            # 名字里含该维度词的路径列出来 —— 这是"执行层观察"的回灌
            # （原版 critic 回灌的也是世界状态，不是"你错了"三个字）。
            # 候选路径也是支架的一部分：会了就只说"取空了"，不给答案方向
            cands = (_candidates(schemas, problem, limit=12)
                     if difficulty.hint_allows(hint, "candidates") else [])
            unknown = [p for p in used
                       if not any(p in paths for paths in schemas.values())]
            msg = []
            if missing_now:
                msg.append(f"代码跑通了，但这些维度取到的是空：{', '.join(missing_now)}")
            if off_spec:
                # 点名"你用的是哪条、声明的是哪条" —— 只说"分支不对"没有可改方向。
                msg.append("这几个维度**没有用题干声明的口径**：")
                for d in off_spec:
                    mine = [p for p in used if _same_metric(p, declared[d])] or used
                    msg.append(f"    {d}：声明按 `{declared[d]}` 取值，"
                               f"你用了 `{'、'.join(mine[:2]) or '（未取）'}`")
                msg.append("    同一份返回里有多支同名字段，只有声明的那一支是"
                           "权威口径；别的分支即使取到数字也不可信。")
            if unknown:
                msg.append("你写的这些路径**不在工具返回里**："
                           + "、".join(unknown[:6]))
            if cands:
                msg.append("返回里真实存在的候选路径（从中挑，不要自己编段名）：")
                msg += [f"    {c}" for c in cands]
            msg += [
                "自查三条：",
                "  1. 这个维度是**标量**还是**百分比**？标量用 scalar(obj, path)，"
                "百分比用 pct(obj, num_path, den_path)；",
                "  2. 是 team 还是 opponent？是队伍整体还是某个选手？"
                "（题目口径优先）；",
                "  3. 有没有拿**别的指标**的 num/denom 去除出它（那是取错）。",
                "取不到就让它空着也不要编，改的是路径不是数。",
            ]
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": "\n".join(msg)})
            missing = missing_now
            continue

        out["program_code"] = parsed["program_code"]
        out["program_name"] = parsed["program_name"]
        out["attempts"] = attempt + 1
        return out

    return {"error": f"代码化取证失败（重试 {retries} 次）：{last_err}",
            "values": {}, "calls": [], "tools": []}


def _main() -> int:
    import argparse
    from tasks import TASKS

    ap = argparse.ArgumentParser(description="代码化取证：模型写调 MCP 的代码，执行拿原值")
    ap.add_argument("--topic", required=True, help=f"题目 id，可选：{', '.join(TASKS)}")
    args = ap.parse_args()

    task = TASKS.get(args.topic)
    if task is None:
        print(f"未知题目 {args.topic}")
        return 1
    dims = sorted({str(p.dimension) for p in task.rubric})
    out = asyncio.run(collect(task, dims=dims))
    print(f"题目    : {args.topic}")
    print(f"需要维度: {', '.join(dims)}")
    if out.get("error"):
        print(f"失败    : {out['error']}")
        return 1
    print(f"调用工具: {' → '.join(out['tools']) or '（无）'}   "
          f"（尝试 {out.get('attempts')} 次）")
    print(f"主函数  : {out.get('program_name')}")
    print("取到的原始值：")
    for k, v in (out.get("values") or {}).items():
        flag = "" if v is not None else "   ← 没取到（该维度不在观测里）"
        print(f"  {k:<24} = {v!r}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
