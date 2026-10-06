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
async def your_main_function_name(mcp):
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
_MAX_PATHS = 60
_MAX_DEPTH = 6


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


def render_tool_docs(schemas: dict[str, list[str]]) -> str:
    if not schemas:
        return ""
    blocks = []
    for t, paths in schemas.items():
        shown = "\n".join(f"    {p}" for p in paths[:_MAX_PATHS])
        blocks.append(f"await mcp.{TOOL_SIGNATURES.get(t, t)} 返回结构（叶子路径）：\n{shown}")
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


def graph_hints(dims: list[str] | None) -> str:
    """图谱给的「维度 → 由哪个工具产出」。

    没有它模型会选错工具（实测：economy 类维度去调 match_economy_report，
    而图谱声明它们由 pattern_detection_report 产出）。
    """
    if not dims:
        return ""
    try:
        import knowledge_graph
        g = knowledge_graph.KnowledgeGraph.load()
    except Exception:
        return ""
    lines = []
    for d in dims:
        prod = g.who_produces(d)
        if not prod:
            continue
        # 必须给到 **value_path**，不能只给工具名。
        # 实测踩过：只说"kast_pct 由 match_analysis_report 产出"，模型就在
        # 返回结构里挑了一条长得像的路径 `team_comparison.Cloud9...`，
        # 而真值口径在 `key_metrics.team...` —— 挑错分支就 dig 到 None。
        # 给路径属于"给结构"不是"给值"（图谱里早就这么给了），不算泄题。
        node = g.nodes.get(f"dim:{d}")
        vp = str((node.detail.get("value_path") if node else "") or "")
        line = f"    {d} → 调 {' 或 '.join(prod)}"
        if vp:
            line += f"，按路径取值 {vp}"
        lines.append(line)
    if not lines:
        return ""
    return "\n\n知识图谱声明（维度由哪个工具产出）：\n" + "\n".join(lines)


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

工具返回里取值的辅助函数已经备好：
    dig(obj, "a.b.0.c")   —— 按点分路径取值，取不到返回 None（绝不猜、绝不兜底）

**dig 的路径必须从上面的返回结构里照抄**，不要自己编字段名。

⚠️ 取值只能用 dig —— **不要写 obj.get("a.b.c")**：点分路径不是一个 key，
字典的 key 里没有点号。实测踩过：模型写了
`r.get("key_metrics.economy.eco.num", 0)`，get 取不到就退回默认值 0，
于是交上来的是 **0 而不是"没取到"** —— 那比留 None 更糟，
因为它把"没取到"伪装成了"取到了，值是 0**。

已预置的常量（直接用，不要再自己定义）：
    SERIES = "{SERIES}"      # 本场 series_id
    C9     = "{C9}"          # 队伍名
{dim_hints}
{skills_text}
硬性要求：
1. 主函数必须是 async，且**唯一参数名为 mcp**；
2. 用 return 返回一个 dict，key 是维度名，value 是从工具返回里**取出来的原值**；
3. **不要把数字手写在代码里** —— 所有值必须来自工具返回；
4. 取不到就让它返回 None —— 那表示"这个维度不在观测里"，比编一个数有用得多。

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


async def execute(program_code: str, program_name: str) -> dict[str, Any]:
    """执行模型产出的代码，返回它 return 的原始 dict。

    照原版：执行结果由**解释器**产出，不经过任何模型加工。
    """
    mcp = MCPHandle()
    ns: dict[str, Any] = {"__builtins__": dict(_SAFE_BUILTINS),
                          "mcp": mcp, "dig": dig,
                          # 预置常量：不预置的话模型会自己写 `series_id=...`
                          # 然后 NameError（实测踩过）。
                          "SERIES": SERIES, "C9": C9}
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
                  skills_text: str = "") -> dict[str, Any]:
    """让模型写一段调 MCP 的代码，执行它，拿回原始值。"""
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
    messages = [
        {"role": "system", "content": render_system_message(
            render_tool_docs(schemas), graph_hints(dims), skills_text)},
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
            out = await execute(parsed["program_code"], parsed["program_name"])
        except CodeParseError as e:
            last_err = str(e)
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user",
                             "content": f"执行失败：{last_err}\n请修正后重出一段完整代码。"})
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
