#!/usr/bin/env python3
"""本机执行层：普通用户的「数据源就是本机」。

用户 2026-10-19：「对于普通用户而言，数据源就是本机不是吗，这个你要好好学习猫娘计划了」。

对照猫娘（N.E.K.O）：它的工具层里没有"数据库"这一档 —— app_launcher（启动本机
应用）、lifekit、music、web_search、wechat 全是**本机/外部能力**，用户不需要先
配一个库；插件即能力，能力即本机。

所以 MVE 的场景后端只有两类：
  · Scene A（VLML 数据域）→ VLML 工具集 + rib 库（SQL）
  · 其它场景 → **本机**：场景目录里的文件（csv/json/duckdb）+ 本机只读原语

本模块给非 VLML 场景一个「本机原语 + 写代码执行」的执行层（原版 Voyager 就是
写代码调 control_primitives，不是写 SQL）：

    list_files(path="", pattern="*")     列文件（限定在应用工作区 / 场景目录）
    read_file(path, max_bytes=200_000)   读文本（限定只读、限长）
    read_json(path)                      读 JSON 并解析（别逼模型自己解析）
    query_sql(sql)                        仅当本机场景目录里有 .duckdb 时可用

安全边界（必须守）：
  · 只读：不提供 write/delete/exec；
  · 路径限制在**应用工作区**与**场景目录**下，禁止跳出（.. 与绝对路径都做
    校验）；**不碰操作者的家目录** —— 用户 2026-10-19：「一般用户下载下来放
    本机用啊，不需要我的本机数据的」；
  · 模型写的代码在受限命名空间里跑：禁 `import`、禁 `__builtins__` 逃逸；
  · 单次执行有超时（线程 + join）。

诚实：这里不做"什么都能干"的通用 agent —— 只给这三个原语。取不到就如实报
（`ok=False` + why），绝不编造。
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
from pathlib import Path
from typing import Any

_APP_ROOT = Path(__file__).resolve().parent.parent   # 应用自己的工作区（= 用户下载后放的那份）
_MAX_BYTES = 200_000


# ---------------------------------------------------------------------------
# 原语（只读、路径受限）
# ---------------------------------------------------------------------------
def _roots(scene_dir: str) -> list[Path]:
    """允许的根 = **应用自己的工作区** + 场景目录。

    ⚠️ 刻意不含 `Path.home()`（用户 2026-10-19 纠正：一般用户是把应用下载下来
    放本机用，**不需要**让程序去翻操作者的私人文件）。「数据源就是本机」指的是
    「应用就跑在这台机器上，数据在它自己的工作区里」，不是"去读我的家目录"。
    """
    out = []
    for p in (scene_dir, str(_APP_ROOT)):
        if p:
            try:
                out.append(Path(p).expanduser().resolve())
            except Exception:
                pass
    return out


def _resolve_under(path: str, scene_dir: str) -> Path:
    p = Path(str(path or "")).expanduser()
    if not p.is_absolute():
        p = Path(scene_dir or _APP_ROOT) / p
    p = p.resolve()
    for r in _roots(scene_dir):
        try:
            p.relative_to(r)
            return p
        except ValueError:
            continue
    raise PermissionError(f"路径不在应用工作区/场景目录内：{p}")


def list_files(scene_dir: str, path: str = "", pattern: str = "*",
               limit: int = 200) -> list[dict[str, Any]]:
    base = _resolve_under(path or ".", scene_dir)
    if not base.exists():
        raise FileNotFoundError(str(base))
    rows = []
    targets = [base] if base.is_file() else sorted(base.iterdir())
    for f in targets:
        if not fnmatch.fnmatch(f.name, str(pattern or "*")):
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        rows.append({"path": str(f), "name": f.name,
                     "is_dir": f.is_dir(), "size": int(st.st_size),
                     "mtime": int(st.st_mtime)})
        if len(rows) >= max(1, int(limit)):
            break
    return rows


def read_file(scene_dir: str, path: str, max_bytes: int = _MAX_BYTES) -> dict[str, Any]:
    p = _resolve_under(path, scene_dir)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    n = min(int(max_bytes or _MAX_BYTES), 5_000_000)
    raw = p.read_bytes()[:n]
    text = raw.decode("utf-8", errors="replace")
    return {"path": str(p), "bytes": len(raw), "text": text}


def query_sql(scene_dir: str, sql: str) -> dict[str, Any]:
    """本机场景目录里的 .duckdb（有就用，没有就如实说没有）。"""
    dbs = sorted(Path(scene_dir).glob("*.duckdb")) if scene_dir else []
    if not dbs:
        raise FileNotFoundError(
            f"本机场景目录里没有 .duckdb：{scene_dir}（放一个进去就能查）")
    import duckdb
    con = duckdb.connect(str(dbs[0]), read_only=True)
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in (cur.description or [])]
        return {"columns": cols, "rows": cur.fetchall(),
                "db": str(dbs[0])}
    finally:
        try:
            con.close()
        except Exception:
            pass


def read_json(scene_dir: str, path: str) -> Any:
    """读 JSON 文件并解析（普通用户的数据大多是 .json，不该逼模型自己解析）。"""
    import json as _json
    raw = read_file(scene_dir, path)
    try:
        return _json.loads(raw["text"])
    except Exception as e:
        raise ValueError(f"不是合法 JSON：{Path(path).name}（{type(e).__name__}）") from e


def inventory(scene_dir: str, limit: int = 50) -> list[dict[str, Any]]:
    """场景目录里**现有**的文件清单 —— 直接写进 prompt，别让它猜文件名。

    2026-10-19 服务器实测：模型不知道目录里有什么，凭想象写了
    `book_pattern = "*.txt"`（真实文件是 plan.json），还用了 `os.path`（受限
    命名空间里没有 import）→ ImportError → 取不到数。和 SQL 侧「执行层凭想象
    join」是同一类错：**不要让它猜，先把清单递给它**。
    """
    try:
        rows = list_files(scene_dir, "", "*", limit)
    except Exception:
        return []
    out = []
    for r in rows:
        if r.get("is_dir"):
            out.append({"name": r["name"], "kind": "dir"})
        else:
            out.append({"name": r["name"], "kind": "file",
                        "size": int(r.get("size") or 0)})
    return out


# ---------------------------------------------------------------------------
# 执行：模型写一段 Python（只许调上面三个原语），在受限命名空间里跑
# ---------------------------------------------------------------------------
PROMPT_SYSTEM = """你是本机数据助手。你能用的是**这个应用工作区里**的数据（它就跑在
用户自己的电脑上：工作区里的文件、目录、本地 duckdb），加上**联网**（搜索/抓网页）。
你连的不是任何在线数据库。

你只能使用下面这些已注入的函数（不要导入任何模块）：

  list_files(path="", pattern="*")  → 文件列表 [{"path","name","is_dir","size","mtime"}]
  read_file(path, max_bytes=200000) → {"path","bytes","text"}（只读）
  read_json(path)                   → 直接读出解析好的 JSON 对象（.json 用它，别自己解析）
  query_sql(sql)                    → 场景目录里 .duckdb 的只读查询（有库时）
  web_search(query, max_results=5)  → 联网搜索，返回 [{"title","url","snippet"}]
  web_fetch(url)                    → 抓一个公网页面的正文文本（只读、限长）

⚠️ 这个环境里**没有 import**：`os` / `json` / `pandas` 都用不了，写 `os.path`
就是 ImportError。要文件名用 `list_files` 结果里的 name，要 JSON 用 `read_json`。

要求：
1. 只输出一个 ```python 代码块；
2. 把最终答案赋给 `RESULT`（列表或字典），**联网拿到的答案要带来源 URL**；
3. 不要 print，不要 import，不要写文件；
4. 本机问题按下面的文件清单直接读，不要猜文件名/扩展名（猜错就取不到数）；
5. **清单里的文件和问题无关时，不要拿它凑答案** —— 本机没有就联网查；
   联网也取不到（没网/被限流）才 `RESULT = {"error": "..."}` 如实说。
   拿无关文件的内容当答案是比"没有答案"更糟的错（2026-10-19 服务器实测：
   问世界模型，它把 subjects.json 整份当成答案交了上来）；
6. 取不到就如实说，不要编造。"""


def _run_restricted(code: str, ns: dict[str, Any], timeout: float = 30.0
                    ) -> dict[str, Any]:
    import threading
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            exec(compile(code, "<local_skill>", "exec"), ns)  # noqa: S102
            box["ok"] = True
        except ImportError as e:
            box["ok"] = False
            box["error"] = (f"{type(e).__name__}: {e}"
                            " —— 这个环境没有 import：文件名用 list_files 的 name，"
                            "JSON 用 read_json，别用 os/json 模块")
        except Exception as e:
            box["ok"] = False
            box["error"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return {"ok": False, "error": f"执行超时（>{int(timeout)}s）"}
    return box


def _payload(value: Any) -> tuple[Any, str]:
    """RESULT → (data, error)。

    ⚠️ 真空成功的老毛病必须在这里也挡掉（同 critic.is_vacuous 的口径：
    「取到数」= 至少一个真实值）：模型自报 `RESULT = {"error": "..."}`、
    或把空列表/空字典/空串塞进 RESULT，都**不算取到数**，否则一次失败会被
    记成成功，坏配方固化进技能库，下次复用即答错。
    """
    if value is None:
        return None, "RESULT 为空（模型没把答案放进 RESULT）"
    if isinstance(value, dict) and list(value.keys()) == ["error"]:
        return None, str(value.get("error") or "模型自报取不到（RESULT.error）")
    if isinstance(value, (list, dict, str)) and len(value) == 0:
        return None, "RESULT 是空的（没取到任何内容）"
    if isinstance(value, list):
        return (value, "")
    if isinstance(value, dict):
        return (value, "")
    return ({"value": value}, "")


async def solve(question: str, scene_dir: str, *, llm=None,
                max_iters: int = 2, scene_id: str = ""
                ) -> tuple[list[dict[str, Any]], str]:
    """写代码 → 跑 → 看结果；失败带反馈重写。返回 (观察列表, 最后一段代码)。

    scene_id 给了就按注册表里的**启用条目**注入原语（猫爪总开关生效）。
    """
    from voyager_vlml.llm import chat
    call = llm or (lambda msgs: chat(msgs, temperature=0.2))

    def _ns() -> dict[str, Any]:
        return _ns_base(scene_dir, scene_id)

    # 场景目录里**现有**什么，直接告诉它（否则它会猜 *.txt 而真实文件是 .json）
    inv = inventory(scene_dir)
    if inv:
        inv_txt = ("场景目录里现有的文件：\n" + "\n".join(
            f"- {r['name']}（{'目录' if r['kind'] == 'dir' else str(r.get('size', 0)) + ' 字节'}）"
            for r in inv))
    else:
        inv_txt = "场景目录里现在是空的（一个文件都没有）。"

    obs: list[dict[str, Any]] = []
    code = ""
    fb = ""
    for _ in range(max(1, max_iters)):
        user = (f"问题：{question}\n\n{inv_txt}\n"
                + (f"\n上一次的代码：\n{code}\n执行反馈：{fb}\n请修正后重写。"
                   if fb else ""))
        text = call([{"role": "system", "content": PROMPT_SYSTEM},
                     {"role": "user", "content": user}]) or ""
        code = _extract_code(text)
        if not code:
            fb = "没给出 ```python 代码块"
            obs.append({"ok": False, "tool": "local_python", "rows": 0,
                        "error": fb})
            continue
        ns = _ns()
        res = await asyncio.to_thread(_run_restricted, code, ns)
        data, why_empty = _payload(ns.get("RESULT"))
        rows = len(data) if isinstance(data, list) else (1 if data else 0)
        o = {"ok": bool(res.get("ok")) and data is not None,
             "tool": "local_python", "rows": rows, "data": data,
             "error": str(res.get("error") or ("" if data is not None
                                               else why_empty))}
        obs.append(o)
        if o["ok"]:
            break
        fb = o["error"] or "RESULT 为空"
    return obs, code


def _extract_code(text: str) -> str:
    import re
    m = re.search(r"```(?:python)?\s*(.+?)```", str(text or ""), re.S)
    return (m.group(1) if m else "").strip()


# ---------------------------------------------------------------------------
# 条目化：本机原语是「猫爪总开关」下的一组条目（同构 neko 的插件条目）
# ---------------------------------------------------------------------------
# 用户 2026-10-19：「voyager 的基础抓手、能力和猫娘 neko 的 MCP、插件、猫爪
# 总开关、以及猫爪总开关下面的内容，这块是同构的」。所以本机原语不能是藏在
# 执行层里的几个 lambda —— 它必须**注册成条目**：
#     origin="local" / group="本机能力"
# 这样执行层给模型的抓手清单（actions.enabled_entries）里就有它们，面板上也
# 能看到「本机能力」这一组的总开关；关掉 = 对模型不可见、也调不动。
PRIMITIVES: list[dict[str, Any]] = [
    {"name": "list_files",
     "fn": list_files,
     "desc": "列出场景目录/应用工作区里的文件（path 相对场景目录）",
     "schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "pattern": {"type": "string"},
         "limit": {"type": "integer"}}, "required": []}},
    {"name": "read_file",
     "fn": read_file,
     "desc": "读一个文本文件（只读、限长）",
     "schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "max_bytes": {"type": "integer"}},
         "required": ["path"]}},
    {"name": "read_json",
     "fn": read_json,
     "desc": "读一个 JSON 文件并解析（.json 用它，别自己解析）",
     "schema": {"type": "object", "properties": {"path": {"type": "string"}},
                "required": ["path"]}},
    {"name": "query_sql",
     "fn": query_sql,
     "desc": "查场景目录里的 .duckdb（只读；没有库时如实报没有）",
     "schema": {"type": "object", "properties": {"sql": {"type": "string"}},
                "required": ["sql"]}},
]
LOCAL_GROUP = "本机能力"


def bind(scene_dir: str) -> list[dict[str, Any]]:
    """把原语绑到某个场景目录上（给 actions.register_local 用）。"""
    from functools import partial
    return [{"name": p["name"], "fn": partial(p["fn"], scene_dir),
             "description": p["desc"], "schema": dict(p["schema"]),
             "impl": f"local_agent:{p['name']}"} for p in PRIMITIVES]


def primitive_names() -> list[str]:
    return [p["name"] for p in PRIMITIVES]


# ---------------------------------------------------------------------------
# 技能结晶：把一段跑通的 Python 存成可重放的条目
# ---------------------------------------------------------------------------
def _ns_base(scene_dir: str, scene_id: str = "") -> dict[str, Any]:
    """执行命名空间。

    给了 scene_id 就**按注册表里的启用条目**建（猫爪总开关关掉的组不注入 ——
    模型连名字都拿不到，这和 neko 关掉插件后条目消失是同构的）；没给就用
    全量原语（结晶重放/测试用）。本机原语（origin=local）+ 联网原语
    （origin=net）都从这里出。
    """
    full: dict[str, Any] = {
        "list_files": lambda path="", pattern="*": list_files(
            scene_dir, path, pattern),
        "read_file": lambda path, max_bytes=_MAX_BYTES: read_file(
            scene_dir, path, max_bytes),
        "read_json": lambda path: read_json(scene_dir, path),
        "query_sql": lambda sql: query_sql(scene_dir, sql),
    }
    try:
        import net_tools
        full["web_search"] = net_tools.web_search
        full["web_fetch"] = net_tools.web_fetch
    except Exception:
        pass
    allowed_origins = {"local", "net"}
    names = set(full)
    if scene_id:
        try:
            import actions
            names = {str(e.get("name")) for e in actions.enabled_entries(scene_id)
                     if str(e.get("origin") or "") in allowed_origins} & set(full)
        except Exception:
            names = set(full)
    ns: dict[str, Any] = {k: v for k, v in full.items() if k in names}
    ns["__builtins__"] = {"len": len, "list": list, "dict": dict, "str": str,
                          "int": int, "float": float, "sorted": sorted,
                          "sum": sum, "min": min, "max": max, "any": any,
                          "all": all, "range": range, "enumerate": enumerate,
                          "Exception": Exception}
    return ns


def run_saved(code: str, scene_dir: str, timeout: float = 30.0,
              scene_id: str = "") -> Any:
    """跑一段已存下来的本机技能代码，返回它的 RESULT（失败抛异常，由调用方如实报）。"""
    ns = _ns_base(scene_dir, scene_id)
    box = _run_restricted(code, ns, timeout=timeout)
    if not box.get("ok"):
        raise RuntimeError(box.get("error") or "本机技能执行失败")
    return ns.get("RESULT")
