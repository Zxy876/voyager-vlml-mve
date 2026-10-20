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


def inventory(scene_dir: str, limit: int = 50, *, preview: int = 300,
              preview_files: int = 3) -> list[dict[str, Any]]:
    """场景目录里**现有**的文件清单 —— 直接写进 prompt，别让它猜文件名。

    2026-10-19 服务器实测：模型不知道目录里有什么，凭想象写了
    `book_pattern = "*.txt"`（真实文件是 plan.json），还用了 `os.path`（受限
    命名空间里没有 import）→ ImportError → 取不到数。和 SQL 侧「执行层凭想象
    join」是同一类错：**不要让它猜，先把清单递给它**。

    同一天第二次实测（Scene B projects.json）：清单只有名字和字节数，模型不知道
    结构是 `{"as_of":..., "projects":[...]}`，直接 `for project in read_json(...)`
    遍历了字典的 key → TypeError → 两轮重写都撞同一个坑。所以小文件顺带给
    前 `preview` 个字符的**结构预览**（最多 `preview_files` 个）——读的是本机
    工作区里用户自己的数据，给模型看不越界。
    """
    try:
        rows = list_files(scene_dir, "", "*", limit)
    except Exception:
        return []
    out = []
    shown = 0
    for r in rows:
        if r.get("is_dir"):
            out.append({"name": r["name"], "kind": "dir"})
            continue
        item = {"name": r["name"], "kind": "file",
                "size": int(r.get("size") or 0)}
        if (preview > 0 and shown < preview_files
                and item["size"] <= 200_000
                and str(r["name"]).lower().endswith(
                    (".json", ".csv", ".txt", ".md", ".tsv", ".yaml", ".yml"))):
            try:
                item["preview"] = read_file(scene_dir, r["name"],
                                            max_bytes=preview)["text"]
                shown += 1
            except Exception:
                pass
        out.append(item)
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
2. 把最终答案赋给 `RESULT`（列表或字典），**答案必须来自你调用上面函数取回的真实数据**；
   联网拿到的答案要带来源 URL；
3. 不要 print，不要 import，不要写文件；
4. **先判断这题要不要数据**：
   · 要数据（文件/库/表/统计/排名/多少/最新/热门/推荐…）→ **必须调用至少一个
     数据源函数**（list_files/read_file/read_json/query_sql/web_search/
     web_fetch/mcp_*），不许把训练记忆里的东西写成 `RESULT` 交上来；时效性问题
     一定先 web_search（或 mcp_* 联网工具），再据检索到的真实结果作答；
   · 不要数据（方法/建议/解释类，例如「阿斯伯格孩子怎么养育」）→ 可以**直接
     写答案**（`RESULT = {"answer": "..."}`），但要写清楚依据来自哪里（公认方法/
     权威指南/检索到的来源），不要编具体数字和人名；有网就 web_search 补依据；
5. 本机问题按下面的文件清单直接读，不要猜文件名/扩展名（猜错就取不到数）；
   **read_json 返回的是整个对象**：数据列表通常在某一个键下面（清单里给了
   结构预览，按预览里的真实键名取值，别直接 `for x in read_json(...)` 遍历
   字典的 key——那会拿到字符串然后 TypeError）；
   **清单里的文件和问题无关时，不要拿它凑答案** —— 本机没有就联网查；
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
                max_iters: int = 2, scene_id: str = "", critique: str = "",
                dimensions: list[dict[str, Any]] | None = None,
                avoid: list[str] | None = None,
                require_tools: bool = False
                ) -> tuple[list[dict[str, Any]], str]:
    """写代码 → 跑 → 看结果；失败带反馈重写。返回 (观察列表, 最后一段代码)。

    scene_id 给了就按注册表里的**启用条目**注入原语（猫爪总开关生效）。
    critique 是上一轮 **critic 的评语**（原版 voyager.py:262-268 把 critic 的
    critique 拼进下一次 human message —— 迭代提示的来源）。给了就放在首轮
    user 消息里，别让它重犯同一个坑。

    dimensions / avoid 来自 Maieutic 的追问骨架（maieutic_scaffold）：
      · dimensions —— 必须回答的维度（spec gate）。答案要逐条答到，
        执行层就会按维度组织，而不是糊一段话；
      · avoid —— 预判的偏差（drift/bug），直接告诉它别这么答。
    两者都可能为空（骨架没产出来），为空就不出现那两段，不编。

    `require_tools` —— **学习环节**才开的硬要求（用户 2026-10-20："按道理不是
    消费时凭记忆答、学习时用工具吗"）：这一轮必须**真的调用数据源**取回证据，
    凭训练记忆直接写答案判失败 —— 同构原版：rollout 必须调 control primitive，
    `skill.py` 只在成功 rollout 后存 program_code，没调过 primitive 的代码
    压根不算技能。消费侧不开（消费是"用已学到的能力"：有技能就重放，没技能
    才让模型直接答，那是另一条口径）。

    ⚠️ 兜底：场景里**根本没有可取数的手段**（本机能力 + 联网 + MCP 全关）时不
    强制 —— 那是"没工具可用"，不是"有工具不用"。否则学习循环会在这种场景里
    永远判失败，卡死（如实放行，让 critic 去判）。
    """
    from voyager_vlml.llm import chat
    call = llm or (lambda msgs: chat(msgs, temperature=0.2))

    def _ns(tracer: list[str]) -> dict[str, Any]:
        return _ns_base(scene_dir, scene_id, tracer=tracer)

    # 场景目录里**现有**什么，直接告诉它（否则它会猜 *.txt 而真实文件是 .json）
    inv = inventory(scene_dir)
    if inv:
        lines = []
        for r in inv:
            if r["kind"] == "dir":
                lines.append(f"- {r['name']}（目录）")
                continue
            lines.append(f"- {r['name']}（{r.get('size', 0)} 字节）")
            if r.get("preview"):
                pv = str(r["preview"]).replace("\n", " ")
                lines.append(f"    开头内容（结构预览）：{pv[:300]}")
        inv_txt = "场景目录里现有的文件：\n" + "\n".join(lines)
    else:
        inv_txt = "场景目录里现在是空的（一个文件都没有）。"

    obs: list[dict[str, Any]] = []
    code = ""
    fb = ""
    # 学习环节的"必须真取数"只在**确实有手段可取**时开（见 docstring 兜底）：
    # 命名空间里一个原语都没有就别硬逼，那是没工具可用。
    _probe: dict[str, Any] = {}
    if require_tools:
        try:
            _probe = _ns_base(scene_dir, scene_id, tracer=None)
        except Exception:
            _probe = {}
    _can_fetch = bool(_probe)
    # 经 MCP 接入的远端工具（名字/签名）动态拼进 system —— server 连上了才出现，
    # 没连上不误导模型去调一个不存在的函数。
    system = PROMPT_SYSTEM
    mcp_sec = _mcp_prompt_section(scene_id)
    if mcp_sec:
        system = PROMPT_SYSTEM + mcp_sec
    if require_tools and _can_fetch:
        system += ("\n\n【这是**学习环节**，不是答题】你的答案必须来自你**真正调用"
                   "数据源取回的内容**（list_files / read_file / read_json / "
                   "query_sql / web_search / web_fetch / mcp_*）。直接凭记忆写一段"
                   "「正确」答案 = 没有取证 = 这轮判不通过，也不会被记成能力。"
                   "先调工具取证，再据实组织答案；取不到就如实说取到了什么。")
    for _ in range(max(1, max_iters)):
        dim_txt = ""
        if dimensions:
            rows = [f"{i}. {str((d or {}).get('question') or '').strip()}"
                    for i, d in enumerate(dimensions, start=1)
                    if str((d or {}).get("question") or "").strip()]
            if rows:
                dim_txt = ("\n必须回答的维度（逐条答到；漏一条这轮就算没完成）：\n"
                           + "\n".join(rows) + "\n")
        avoid_txt = ""
        if avoid:
            avoid_txt = ("\n常见答偏方式（明确别这么答）：\n"
                         + "\n".join(f"- {str(a)}" for a in avoid if str(a).strip())
                         + "\n")
        # 取证要求必须**写进 user**，不能只在 system：整轮的 user 里有
        # "逐条答到"这种更强的答题指令，模型会照着直接写答案而不取证
        # （2026-10-20 实测：system 里写了要取证，8 条维度 7 条"调过：无"）。
        # 同时把**真正可用的函数名**列出来 —— 光说"调用数据源"，模型在
        # 11 个 MCP 工具里挑花眼，反而不调。
        fetch_txt = ""
        if require_tools and _can_fetch:
            fetch_txt = ("\n【取证要求】这是**学习环节**：答案必须来自你真正调用"
                         "下面这些函数取回的内容 —— "
                         + "、".join(sorted(_probe)[:14])
                         + "。直接凭记忆写一段「正确」答案 = 没取证 = 判不通过，"
                           "也不会被记成能力。先调函数取证，再据实组织答案。\n")
        user = (f"问题：{question}\n\n{inv_txt}\n" + dim_txt + avoid_txt
                + fetch_txt
                + (f"\n上一轮没通过 critic 检查，它的评语（务必针对它改）：{critique}\n"
                   if critique else "")
                + (f"\n上一次的代码：\n{code}\n执行反馈：{fb}\n请修正后重写。"
                   if fb else ""))
        text = call([{"role": "system", "content": system},
                     {"role": "user", "content": user}]) or ""
        code = _extract_code(text)
        if not code:
            fb = "没给出 ```python 代码块"
            obs.append({"ok": False, "tool": "local_python", "rows": 0,
                        "error": fb})
            continue
        tracer: list[str] = []
        ns = _ns(tracer)
        res = await asyncio.to_thread(_run_restricted, code, ns)
        data, why_empty = _payload(ns.get("RESULT"))
        rows = len(data) if isinstance(data, list) else (1 if data else 0)
        err = str(res.get("error") or "")
        # 真空成功：代码跑通、RESULT 非空，却一次数据源都没调。**只对数据型任务
        # 判失败**（题面要文件/库/统计/时效）——「阿斯伯格孩子怎么养育」这类知识
        # 型问题本来就没有数据可取，一刀切会把它们全部判死（2026-10-19 实测连判
        # 4 次 no_data）。知识型交 critic 用 LLM 判（照原版 critic.py 的口径）。
        try:
            import task_critic as _tc
            _needs_data = _tc.needs_data(question)
        except Exception:
            _needs_data = True
        # 真空成功：代码跑通、RESULT 非空，却一次数据源都没调。
        #   · 数据型题：一直就判失败（题面要数却没取数）；
        #   · 学习环节（require_tools）：**无条件**判失败 —— 原版 rollout 必须调
        #     primitive，凭记忆写完就交的东西进不了技能库（用户 2026-10-20）。
        if (not err and res.get("ok") and data is not None and not tracer
                and (_needs_data or (require_tools and _can_fetch))):
            err = ("这题要具体数据（文件/库/统计/时效），但没调用任何数据源"
                   "（list_files/read_file/read_json/query_sql/web_search/"
                   "web_fetch/mcp_*）——答案疑似凭训练记忆编造，不算完成；"
                   "请调用数据源取真实数据再作答"
                   + ("（**学习环节**：没取证就写答案 = 这轮不算学会，也不会"
                      "被记成技能和图谱上的能力）" if require_tools else ""))
        o = {"ok": bool(res.get("ok")) and data is not None and not err,
             "tool": "local_python", "rows": rows, "data": data,
             "data_sources": list(tracer),
             "evidence": ("data" if tracer else "knowledge"),
             "error": err or ("" if data is not None else why_empty)}
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
# MCP 远端工具 → 模型可调用（打通「死通道」）
# ---------------------------------------------------------------------------
def _first_mcp_text(inner: dict) -> str:
    for c in (inner.get("content") or []):
        if isinstance(c, dict) and c.get("type") == "text":
            return str(c.get("text") or "")
    return ""


def _unwrap_mcp(payload: Any) -> Any:
    """把 mcp_gateway.call_remote 的返回（`{result:{...}}` 或 `{error:...}`）
    翻回模型能用的 Python 对象。错误如实抛 RuntimeError —— 执行层会把它记成
    「取不到真数」，绝不冒充成功（同 critic 真空判据）。"""
    if not isinstance(payload, dict):
        return payload
    if "error" in payload:
        raise RuntimeError(f"MCP 远端调用失败：{payload.get('error')}")
    inner = payload.get("result", payload)
    if not isinstance(inner, dict):
        return inner
    if inner.get("isError"):
        sc = inner.get("structuredContent") or {}
        raise RuntimeError(
            f"MCP 工具报错：{sc.get('error') or _first_mcp_text(inner) or '未知错误'}")
    sc = inner.get("structuredContent")
    if sc is not None:
        return sc
    # 兜底：解析 content[0].text 的 JSON
    for c in (inner.get("content") or []):
        if isinstance(c, dict) and c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except Exception:
                return c["text"]
    return inner


def _make_mcp_callable(gw: Any, tool_id: str, loop: Any):
    """造一个**同步**包装器：模型在受限 exec 里只能调同步函数，而 MCP 调用是
    异步的。用 `run_coroutine_threadsafe` 把调用桥回主事件循环（网关的 MCP 客户端
    子进程与读循环都绑在那个循环上），worker 线程阻塞等结果。

    ⚠️ 跨线程桥接是必须的：网关的 `_request` 在**主循环**里 `await` 读循环解析
    出来的 future，若在执行线程里另起一个事件循环跑它，future 跨循环不唤醒 → 死锁。
    run_coroutine_threadsafe 是标准正确路径。
    """
    def _sync(**kwargs: Any) -> Any:
        cfut = asyncio.run_coroutine_threadsafe(
            gw.call_remote(tool_id, **kwargs), loop)
        return _unwrap_mcp(cfut.result(timeout=60))
    _sync.__name__ = str(tool_id)
    return _sync


def _recorder(tracer: list[str] | None, name: str):
    """给一个可调用套一层：**被调用时**把 name 记进 tracer（先记后调，调用失败也留痕）。

    这是堵「真空成功」新形态的关键：代码跑通、RESULT 非空，但如果一次数据源都没
    真正调用，答案八成是拿训练记忆编的 —— tracer 空就是证据。
    """
    def deco(fn: Any) -> Any:
        def _w(*a: Any, **k: Any) -> Any:
            if tracer is not None:
                tracer.append(name)
            return fn(*a, **k)
        _w.__name__ = str(name)
        return _w
    return deco


def _mcp_prompt_section(scene_id: str) -> str:
    """把经 MCP 接入的远端工具（名字/签名）写进 prompt，让模型知道能调它。

    只在真有 mcp 条目（server 连上、expose_as_actions 注册了）时才返回非空 ——
    server 没连上就不误导模型去调一个不存在的函数。
    """
    if not scene_id:
        return ""
    try:
        import actions
        items: list[str] = []
        for e in actions.enabled_entries(scene_id):
            if str(e.get("origin") or "") != "mcp":
                continue
            name = str(e.get("name"))
            schema = e.get("input_schema") or {}
            props = schema.get("properties") or {}
            required = set(schema.get("required") or [])
            parts = []
            for pname, pinfo in props.items():
                ptype = (pinfo or {}).get("type", "str")
                parts.append(f"{pname}: {ptype}" if pname in required
                             else f"{pname}: {ptype}=...")
            sig = ", ".join(parts) if parts else ""
            desc = str(e.get("description") or "")[:48].replace("\n", " ")
            items.append(f"  {name}({sig})  → {desc}")
        if items:
            return ("\n此外，以下**经 MCP 接入的远端工具（猫娘计划 mcp 同构）**可直接调用"
                    "（名字就是函数名，联网优先用它们）：\n" + "\n".join(items))
    except Exception:
        pass
    return ""


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
def _ns_base(scene_dir: str, scene_id: str = "",
             tracer: list[str] | None = None) -> dict[str, Any]:
    """执行命名空间。

    给了 scene_id 就**按注册表里的启用条目**建（猫爪总开关关掉的组不注入 ——
    模型连名字都拿不到，这和 neko 关掉插件后条目消失是同构的）；没给就用
    全量原语（结晶重放/测试用）。本机原语（origin=local）+ 联网原语
    （origin=net）+ MCP 远端工具（origin=mcp）都从这里出。

    `tracer`：给了就记录**哪些数据源被真正调用**（先记后调，失败也留痕）——
    solve 用它堵「代码跑通、RESULT 非空却一次都没取数」的真空成功新形态。
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
    # 套调用记录：每个原语被调用时往 tracer 记一笔（可读性让位于「真的取了数吗」）。
    # tracer 为空（技能重放 run_saved / 测试直连）时**不套**，保持函数原样 ——
    # 免得 repr 变成 <function _w> 而不是 <function web_search>（test_scene_backend
    # 靠 str(web_search) 认名，实测被这个包装破坏过一次）。
    if tracer is not None:
        full = {k: _recorder(tracer, k)(v) for k, v in full.items()}
    allowed_origins = {"local", "net"}
    names = set(full)
    if scene_id:
        try:
            import actions
            import mcp_gateway
            # 条目必须先注册，`enabled_entries()` 才看得到 —— 注册是幂等的，
            # 但**不能省**：实测（2026-10-19 服务器）在独立进程里直接重放一条
            # 学到的联网配方，报 `NameError: name 'web_search' is not defined`，
            # 只因为那个进程没走过 consume 的注册那一步。技能是跨入口复用的资产，
            # 重放不能依赖「调用方刚好注册过」。
            actions.register_local(scene_id, str(scene_dir))
            actions.register_net(scene_id)
            names = {str(e.get("name")) for e in actions.enabled_entries(scene_id)
                     if str(e.get("origin") or "") in allowed_origins} & set(full)
        except Exception:
            names = set(full)
    ns: dict[str, Any] = {k: v for k, v in full.items() if k in names}
    # ---- MCP 远端工具经 stdio 接入，喂给模型（打通「死通道」）----
    # expose_as_actions 早已把 mcp_* 注册进 registry，但执行层只注入了 local/net，
    # 模型写代码调不到它们 → 整条「猫娘能力经 MCP」是断的。这里把 origin=mcp 的
    # 条目按名字注入执行命名空间（同步包装器桥接主循环），prompt 由 _mcp_prompt_section 提供。
    if scene_id:
        try:
            import actions
            import mcp_gateway
            loop = asyncio.get_running_loop()
            gw = mcp_gateway.get_gateway()
            for e in actions.enabled_entries(scene_id):
                if str(e.get("origin") or "") != "mcp":
                    continue
                tid = str(e.get("name"))
                if tid in ns:        # 不覆盖本机/联网同名（理论上不会重名）
                    continue
                _entry = _make_mcp_callable(gw, tid, loop)
                ns[tid] = (_recorder(tracer, tid)(_entry)
                           if tracer is not None else _entry)
        except RuntimeError:
            # 没有运行中的事件循环（技能重放 run_saved 等）→ 不注入 mcp 远端工具
            pass
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
