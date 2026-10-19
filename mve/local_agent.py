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

    list_files(path="", pattern="*")     列本机文件（限定在场景目录 / 家目录）
    read_file(path, max_bytes=200_000)   读文本（限定只读、限长）
    query_sql(sql)                        仅当本机场景目录里有 .duckdb 时可用

安全边界（必须守）：
  · 只读：不提供 write/delete/exec；
  · 路径限制在**场景目录**与**家目录**下，禁止跳出（.. 与绝对路径都做校验）；
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

_HOME = Path.home()
_MAX_BYTES = 200_000


# ---------------------------------------------------------------------------
# 原语（只读、路径受限）
# ---------------------------------------------------------------------------
def _roots(scene_dir: str) -> list[Path]:
    out = []
    for p in (scene_dir, str(_HOME)):
        if p:
            try:
                out.append(Path(p).expanduser().resolve())
            except Exception:
                pass
    return out


def _resolve_under(path: str, scene_dir: str) -> Path:
    p = Path(str(path or "")).expanduser()
    if not p.is_absolute():
        p = Path(scene_dir or _HOME) / p
    p = p.resolve()
    for r in _roots(scene_dir):
        try:
            p.relative_to(r)
            return p
        except ValueError:
            continue
    raise PermissionError(f"路径不在本机场景目录/家目录内：{p}")


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


# ---------------------------------------------------------------------------
# 执行：模型写一段 Python（只许调上面三个原语），在受限命名空间里跑
# ---------------------------------------------------------------------------
PROMPT_SYSTEM = """你是本机数据助手。用户问的是**这台电脑上**的数据（文件、目录、
本地 duckdb），不是任何在线数据库。

你只能使用下面三个已注入的函数（不要导入任何模块，不要读写除它们以外的路径）：

  list_files(path="", pattern="*")  → 文件列表 [{"path","name","is_dir","size","mtime"}]
  read_file(path, max_bytes=200000) → {"path","bytes","text"}（只读）
  query_sql(sql)                    → 本机场景目录里 .duckdb 的只读查询（有库时）

要求：
1. 只输出一个 ```python 代码块；
2. 把最终答案赋给 `RESULT`（列表或字典）；
3. 不要 print，不要 import，不要写文件；
4. 取不到就 `RESULT = {"error": "..."}` 如实说，不要编造。"""


def _run_restricted(code: str, ns: dict[str, Any], timeout: float = 30.0
                    ) -> dict[str, Any]:
    import threading
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            exec(compile(code, "<local_skill>", "exec"), ns)  # noqa: S102
            box["ok"] = True
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
                max_iters: int = 2) -> tuple[list[dict[str, Any]], str]:
    """写代码 → 跑 → 看结果；失败带反馈重写。返回 (观察列表, 最后一段代码)。"""
    from voyager_vlml.llm import chat
    call = llm or (lambda msgs: chat(msgs, temperature=0.2))

    def _ns() -> dict[str, Any]:
        return {
            "list_files": lambda path="", pattern="*": list_files(
                scene_dir, path, pattern),
            "read_file": lambda path, max_bytes=_MAX_BYTES: read_file(
                scene_dir, path, max_bytes),
            "query_sql": lambda sql: query_sql(scene_dir, sql),
            "__builtins__": {"len": len, "list": list, "dict": dict,
                             "str": str, "int": int, "sorted": sorted,
                             "sum": sum, "min": min, "max": max,
                             "any": any, "all": all, "range": range,
                             "enumerate": enumerate, "Exception": Exception},
        }

    obs: list[dict[str, Any]] = []
    code = ""
    fb = ""
    for _ in range(max(1, max_iters)):
        user = (f"问题：{question}\n"
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
# 技能结晶：把一段跑通的 Python 存成可重放的条目
# ---------------------------------------------------------------------------
def _ns_base(scene_dir: str) -> dict[str, Any]:
    return {
        "list_files": lambda path="", pattern="*": list_files(
            scene_dir, path, pattern),
        "read_file": lambda path, max_bytes=_MAX_BYTES: read_file(
            scene_dir, path, max_bytes),
        "query_sql": lambda sql: query_sql(scene_dir, sql),
        "__builtins__": {"len": len, "list": list, "dict": dict, "str": str,
                         "int": int, "sorted": sorted, "sum": sum,
                         "min": min, "max": max, "any": any, "all": all,
                         "range": range, "enumerate": enumerate,
                         "Exception": Exception},
    }


def run_saved(code: str, scene_dir: str, timeout: float = 30.0) -> Any:
    """跑一段已存下来的本机技能代码，返回它的 RESULT（失败抛异常，由调用方如实报）。"""
    ns = _ns_base(scene_dir)
    box = _run_restricted(code, ns, timeout=timeout)
    if not box.get("ok"):
        raise RuntimeError(box.get("error") or "本机技能执行失败")
    return ns.get("RESULT")
