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
        # ⚠️ 超时**不会**杀掉这个线程（Python 没法安全地中止线程）：它还在
        # 里面等 MCP 返回。所以调用方必须能**认出**这是超时而不是"代码错了"
        # —— 否则会原地重试，越重试越多僵尸线程在等同一个被限流的接口
        # （2026-10-20 实测：外层 30s 先爆、内层 MCP 上限 60s，4 次重试全部
        # 无效，7 条维度全未闭合 → 整轮颗粒无收）。
        return {"ok": False, "error": f"执行超时（>{int(timeout)}s）",
                "timed_out": True}
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


def _skill_call_name(raw: str) -> str:
    """技能名 → 可调用的 Python 函数名（原版是 `async function name(bot)`）。

    原版技能本身就是**具名函数**（skill.py:96-101 `generate_skill_description`
    生成 `async function {program_name}(bot){//描述}`），所以新代码能直接写
    `await craftFurnace(bot)`。MVE 的技能是**脚本**形态（`RESULT = {...}`），
    没有名字 → 这里按名字造一个，命名规则确定性（同名技能同一个函数名）。
    """
    import re as _re
    s = _re.sub(r"[^0-9A-Za-z_]+", "_", str(raw or "").strip()).strip("_")
    if not s:
        return ""
    if s[0].isdigit():
        s = "d" + s
    return "skill_" + s


def _make_skill_fn(skill: dict[str, Any], scene_dir: str, scene_id: str,
                   tracer: list[str] | None, timeout: float = 45.0):
    """一条已学技能 → 一个**可调用**的函数（同构原版 `programs`）。

    原版怎么合成（取证）：
      · `skill.py` 的 `programs` 属性 = control_primitives + **全部技能代码**；
      · `env/mineflayer/index.js:253-256`
        `await eval("(async () => {" + programs + "\\n" + code + "})()")`
        —— 技能定义与新写的代码在**同一个作用域**里，所以新代码能直接调用
        已学技能。证据：skill_library/trial1 里 `smeltFiveRawIron` 内部调用了
        `craftFurnace`、`mineTenCobblestone` 内部调用了 `craftWoodenPickaxe`
        （51 条技能共 7 处真实合成）；
      · prompt 明说 "Reuse the above useful programs as much as possible"
        （prompts/action_template.txt:39）。

    MVE 之前只把技能 code **贴进 system 让模型照抄** —— 那是"给个例子"，
    不是合成：执行命名空间里压根没有这个函数，模型想调用也调不了，于是
    命中的题照样现场现做（2026-10-20 截图：两次导入都"用到 0 条技能"）。

    这里把每条技能包成 `skill_<name>()`：在带原语/MCP 的干净命名空间里跑
    那段脚本并返回它的 RESULT。于是新代码可以合成：
        RESULT = {"定义": skill_def(), "起源": skill_origin()}
    """
    code = str(skill.get("code") or "").strip()
    label = str(skill.get("name") or skill.get("topic") or "?")
    fname = _skill_call_name(label)

    def _fn() -> Any:
        if tracer is not None:
            tracer.append(f"skill:{label}")     # 先记后跑：真调用过才有据
        ns: dict[str, Any] = _ns_base(scene_dir, scene_id, tracer=tracer)
        box = _run_restricted(code, ns, timeout=timeout)
        if not box.get("ok"):
            raise RuntimeError(
                f"已学技能 {label} 重放失败：{box.get('error') or '未知错误'}")
        val = ns.get("RESULT")
        if val is None:
            raise RuntimeError(f"已学技能 {label} 没产出 RESULT（配方失效）")
        return val

    _fn.__name__ = fname
    _fn.__doc__ = str(skill.get("text") or "")[:220]
    return _fn


def _build_skill_fns(skills: list[dict[str, Any]] | None, scene_dir: str,
                     scene_id: str, tracer: list[str],
                     limit: int = 8) -> dict[str, Any]:
    """把检索到的技能编成一组可调用函数（同原版 `programs` 拼进同一作用域）。"""
    out: dict[str, Any] = {}
    for s in list(skills or [])[:limit]:
        if not str((s or {}).get("code") or "").strip():
            continue
        fn = _make_skill_fn(s, scene_dir, scene_id, tracer)
        if fn.__name__ and fn.__name__ not in out:
            out[fn.__name__] = fn
    return out


async def solve(question: str, scene_dir: str, *, llm=None,
                max_iters: int = 2, scene_id: str = "", critique: str = "",
                dimensions: list[dict[str, Any]] | None = None,
                avoid: list[str] | None = None,
                require_tools: bool = False,
                skills: list[dict[str, Any]] | None = None,
                require_skills: bool = False,
                timeout: float = 30.0,
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
    # ⚠️ 延迟 import：传了 `llm` 的调用方（测试/别的编排）不该被迫拉起整条
    # VLML 依赖链 —— 实测 `from voyager_vlml.llm import chat` 在只插了 mve/
    # 的进程里直接 ModuleNotFoundError，把调用方拖死。
    def call(msgs):
        if llm is not None:
            return llm(msgs)
        from voyager_vlml.llm import chat
        return chat(msgs, temperature=0.2)

    def _ns(tracer: list[str]) -> dict[str, Any]:
        ns = _ns_base(scene_dir, scene_id, tracer=tracer)
        # 已学技能进**执行域**（不是只进 prompt）—— 同构原版
        # index.js:256 的 `programs + code` 同作用域 eval：模型写的
        # `skill_def()` 才真的调得到。
        ns.update(_build_skill_fns(skills, scene_dir, scene_id, tracer))
        return ns

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
    # 已学技能也是**取证手段**（重放配方 = 真调一次数据源），所以只有原语
    # 全关、但有技能时仍然算"有办法取数"—— 别把这种场景误判成没工具可用。
    _skill_names = [_skill_call_name(str((s or {}).get("name") or ""))
                    for s in (skills or [])
                    if str((s or {}).get("code") or "").strip()]
    _skill_names = [n for n in _skill_names if n]
    _can_fetch = bool(_probe) or bool(_skill_names)
    if require_tools:
        # 这个场景到底有没有取证手段、有哪些 —— 必须看得见：
        # 2026-10-20 整轮 8 条维度 7 条"调过：无"，靠这行定位是"没工具可用"
        # 还是"有工具不用"（两者是截然不同的失败，处理也完全不同）。
        print(f"  · 取证手段：{len(_probe)} 个"
              + (f"（{'、'.join(sorted(_probe)[:10])}）" if _probe
                 else "（一个都没有 —— 本机/联网/MCP 全不可用，不强制取证）")
              + (f"｜已学技能 {len(_skill_names)} 条可调用"
                 if _skill_names else ""),
              flush=True)
    # 经 MCP 接入的远端工具（名字/签名）动态拼进 system —— server 连上了才出现，
    # 没连上不误导模型去调一个不存在的函数。
    system = PROMPT_SYSTEM
    mcp_sec = _mcp_prompt_section(scene_id)
    if mcp_sec:
        system = PROMPT_SYSTEM + mcp_sec
    if _skill_names:
        # 已学会的技能**回注执行层** —— 这是「可达 = 一整个循环」的那一环
        # （用户 2026-10-20 指出）：原版不先判"能不能做到"，而是
        # `retrieve_skills`（voyager.py:195）把技能 code 拼在 control_primitives
        # 后面（action.py:94 `+ skills`）一起喂模型 —— **技能本身就是新的
        # primitive**，所以能力边界随技能库增长而扩张：以前做不到的，学会之后
        # 下次就够得着了。
        #
        # ⚠️ 合成（composition）才是原版的重点，不是"照抄"：
        #   · 执行时技能与 primitives 在**同一个作用域**
        #     （env/mineflayer/index.js:256 `eval(programs + code)`），
        #     所以新代码能直接 `skill_def()`；
        #   · 原版真这么干 —— skill_library/trial1 的 51 条技能里有 7 处
        #     技能调用技能（`smeltFiveRawIron` → `craftFurnace` 等）；
        #   · prompt 的原话是 "Reuse the above useful programs as much as
        #     possible"（prompts/action_template.txt:39）。
        # 只把 code 贴进 system 等于"给个例子"，模型看得见却调不到 →
        # 命中的题照样现场现做（2026-10-20 截图：两次导入都"用到 0 条技能"）。
        _lines: list[str] = []
        for _s in list(skills)[:8]:
            _nm = _skill_call_name(str((_s or {}).get("name") or ""))
            if not _nm or not str((_s or {}).get("code") or "").strip():
                continue
            _what = str((_s or {}).get("text") or "")[:80].replace("\n", " ")
            _lines.append(f"- {_nm}()  → {_what}")
        if _lines:
            system += (
                "\n\n【你已经学会的技能（原版 voyager.py:195 先 retrieve，"
                "action.py:94 拼进可用函数清单；执行时它们与原语在**同一个作用域**，"
                "可以直接调用）】\n" + "\n".join(_lines) + "\n"
                "调用规则（原版 prompts/action_template.txt:39 「Reuse the above "
                "useful programs as much as possible」）：\n"
                "  1. 这些技能覆盖得了这道题就**直接调用**它们，不要重写它们做过的事；\n"
                "  2. 可以同时调多条**合成**一个完整答案：\n"
                "     ```python\n"
                "     RESULT = {\"定义\": skill_def(), \"起源\": skill_origin()}\n"
                "     ```\n"
                "  3. 全部用不上时才自己写取数代码（用上面的原语/MCP 工具）。")
    if require_skills and _skill_names:
        # 「命中的题必须用已学技能」（用户 2026-10-20）—— 同构原版：技能就是
        # 新的 primitive（action.py:94），命中意味着能力边界已经覆盖这道题，
        # 再"现场现做"等于技能库白建。不强制的话模型一律走最省事的路线：
        # 凭训练记忆直接写答案（实测：给了 2 条可调用技能，它一个都没调）。
        system += ("\n\n【命中即复用】这道题**已经学过**（图谱命中），而且你上面"
                   "有 " + str(len(_skill_names)) + " 个已学函数可调用。规则：\n"
                   "  1. 你必须**调用**它们（" + "、".join(_skill_names[:6]) +
                   "）来取数/组合答案；\n"
                   "  2. 一条都不调用、直接凭记忆写答案 —— 判**不通过**，"
                   "会带着评语让你重写；\n"
                   "  3. 只有全部调用失败（报错）时，才允许自己写取数代码。")
    if require_tools and _can_fetch:
        system += ("\n\n【这是**学习环节**，不是答题】你的答案必须来自你**真正调用"
                   "数据源取回的内容**（list_files / read_file / read_json / "
                   "query_sql / web_search / web_fetch / mcp_*）。直接凭记忆写一段"
                   "「正确」答案 = 没有取证 = 这轮判不通过，也不会被记成能力。"
                   "先调工具取证，再据实组织答案；取不到就如实说取到了什么。")
    for _i in range(max(1, max_iters)):
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
            _names = [n for n in sorted(_probe) if not n.startswith("__")]
            # 示例里的函数名必须**真的存在**：写 `web_search` 而本场景只有
            # mcp_*_search（本机联网没注册）时，模型照抄就 NameError。
            _ex = ("web_search" if "web_search" in _names
                   else next((n for n in _names if "search" in n),
                             _names[0] if _names else "web_search"))
            fetch_txt = (
                "【取证要求（先做这个，再写答案）】这是**学习环节**：你的答案必须"
                "来自你**真正调用**下列函数取回的内容，凭记忆直接写的答案判不通"
                "过、也不会被记成能力。\n可用函数："
                + "、".join(_names[:16]) + "\n"
                "代码必须长这样（先取证，再据实组织；参数照上面签名的名字传）：\n"
                "```python\n"
                f"r = {_ex}(query=\"<本题关键词>\")\n"
                f"RESULT = {{\"answer\": r, \"source\": \"{_ex}\"}}\n"
                "```\n")
        user = (f"问题：{question}\n\n{inv_txt}\n" + fetch_txt
                + dim_txt + avoid_txt
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
        res = await asyncio.to_thread(_run_restricted, code, ns, timeout)
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
        # 命中即复用：给了技能却不调用 → 带评语重写一次（不无限循环，最后一轮
        # 如实放行并记下"没调用"，不假装复用过）。
        _called = [str(t)[6:] for t in tracer if str(t).startswith("skill:")]
        if (require_skills and _skill_names and not _called
                and not err and data is not None
                and _i < max(1, max_iters) - 1):
            err = (f"你一条已学技能都没调用（可调用的有："
                   + "、".join(_skill_names[:6]) + "）。这道题已经学过，"
                   "必须调用它们取数/组合答案 —— 凭记忆写的答案不算复用，重写。")
        o = {"ok": bool(res.get("ok")) and data is not None and not err,
             "tool": "local_python", "rows": rows, "data": data,
             "data_sources": list(tracer),
             # 超时要能被上层认出来（见 `_run_restricted` 的说明）：重试一个
             # 还在等外部接口的执行是纯浪费，且僵尸线程会越堆越多。
             "timed_out": bool(res.get("timed_out")),
             "skills_called": _called,
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


def _make_mcp_callable(gw: Any, tool_id: str, loop: Any,
                       schema: dict[str, Any] | None = None):
    """造一个**同步**包装器：模型在受限 exec 里只能调同步函数，而 MCP 调用是
    异步的。用 `run_coroutine_threadsafe` 把调用桥回主事件循环（网关的 MCP 客户端
    子进程与读循环都绑在那个循环上），worker 线程阻塞等结果。

    ⚠️ 跨线程桥接是必须的：网关的 `_request` 在**主循环**里 `await` 读循环解析
    出来的 future，若在执行线程里另起一个事件循环跑它，future 跨循环不唤醒 → 死锁。
    run_coroutine_threadsafe 是标准正确路径。

    ⚠️ 必须收 `*args`（2026-10-20 服务器实测根因）：MCP 工具的入参是**具名**的
    （schema.properties），而模型照 prompt 示例写的是位置调用 `mcp_x("关键词")`。
    只收 `**kwargs` 会直接抛
    `TypeError: _sync() takes 0 positional arguments but 1 was given`
    —— 表现为「调过：mcp_x」却取不到数、维度判未闭合。这里按 schema 声明顺序
    把位置参数补成关键字（MCP 侧仍只认具名入参）。
    """
    props: list[str] = list(((schema or {}).get("properties") or {}).keys())

    def _sync(*args: Any, **kwargs: Any) -> Any:
        if args:
            if not props:
                raise TypeError(
                    f"{tool_id}() 不接受位置参数（该工具没声明入参，请用关键字传）")
            if len(args) > len(props):
                raise TypeError(
                    f"{tool_id}() 最多 {len(props)} 个位置参数"
                    f"（{'、'.join(props)}），收到 {len(args)} 个")
            for key, val in zip(props, args):
                kwargs.setdefault(key, val)
        cfut = asyncio.run_coroutine_threadsafe(
            gw.call_remote(tool_id, **kwargs), loop)
        return _unwrap_mcp(cfut.result(timeout=60))
    _sync.__name__ = str(tool_id)
    if props:
        _sync.__doc__ = f"{tool_id}({', '.join(props)}) —— MCP 远端工具"
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


async def ensure_mcp_tools(scene_id: str) -> dict[str, Any]:
    """把**当前场景**的远端 MCP 工具挂进注册表（连 server + expose 成场景条目）。

    为什么必须在使用它的每条路径上都做一次（2026-10-20 服务器实测）：
    `_ns_base` 只注入「注册表里已有」的 mcp 条目，而这些条目是
    `learn_orchestrate` ③ 步 `connect_all() + expose_as_actions()` 才注册的。
    consume 是**新进程**，从没走过那一步 → 复用配方时 `mcp_neko_web_search`
    一个都注入不进去 → 重放直接 `NameError` → 用户看到「第二次导入还是用不上
    第一次学的」。能力来源（网关）必须先接上，后面才有"注入"可言。

    幂等：已连上的 server 不重连（重连会白起子进程）。连不上/没配置都如实返回
    原因，不假装工具有了。
    """
    if not scene_id:
        return {"exposed": [], "why": "未指定场景 → 不接远端工具"}
    try:
        import mcp_gateway
        gw = mcp_gateway.get_gateway()
        if not gw.connected_servers():
            await gw.connect_all()
        exp = gw.expose_as_actions(scene_id)
        return {"exposed": list(exp.get("exposed") or []),
                "servers": gw.connected_servers(),
                "reason": str(exp.get("reason") or "")}
    except Exception as e:      # 网关挂了不该拖垮消费/学习，如实报即可
        return {"exposed": [], "error": f"{type(e).__name__}: {e}"}


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
             tracer: list[str] | None = None,
             loop: Any = None) -> dict[str, Any]:
    """执行命名空间。

    给了 scene_id 就**按注册表里的启用条目**建（猫爪总开关关掉的组不注入 ——
    模型连名字都拿不到，这和 neko 关掉插件后条目消失是同构的）；没给就用
    全量原语（结晶重放/测试用）。本机原语（origin=local）+ 联网原语
    （origin=net）+ MCP 远端工具（origin=mcp）都从这里出。

    `tracer`：给了就记录**哪些数据源被真正调用**（先记后调，失败也留痕）——
    solve 用它堵「代码跑通、RESULT 非空却一次都没取数」的真空成功新形态。

    `loop`：MCP 同步包装器要把调用桥回**哪个**事件循环。不传就取当前线程正在跑的
    循环（solve 在 loop 线程里调用，够用）；`run_saved_async` 把 exec 挪到 worker
    线程后必须**显式传**（worker 线程自己没有运行中的循环，`get_running_loop()`
    必抛 RuntimeError → mcp_* 一个都注入不进去 → 重放配方 NameError）。
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
            # ⚠️ 必须先取 `loop` 再取网关：worker 线程里 `get_running_loop()` 会抛，
            # 位置不能换到后面去（换了就变成"网关建好了、工具却没注入"）。
            _loop = loop if loop is not None else asyncio.get_running_loop()
            gw = mcp_gateway.get_gateway()
            for e in actions.enabled_entries(scene_id):
                if str(e.get("origin") or "") != "mcp":
                    continue
                tid = str(e.get("name"))
                if tid in ns:        # 不覆盖本机/联网同名（理论上不会重名）
                    continue
                _entry = _make_mcp_callable(gw, tid, _loop,
                                            e.get("input_schema") or {})
                ns[tid] = (_recorder(tracer, tid)(_entry)
                           if tracer is not None else _entry)
        except RuntimeError:
            # 既没传 loop、当前线程也没有运行中的事件循环 → 不注入 mcp 远端工具
            # （技能重放请走 run_saved_async，它会把主循环显式带进来）
            pass
    ns["__builtins__"] = {"len": len, "list": list, "dict": dict, "str": str,
                          "int": int, "float": float, "sorted": sorted,
                          "sum": sum, "min": min, "max": max, "any": any,
                          "all": all, "range": range, "enumerate": enumerate,
                          "Exception": Exception}
    return ns


def run_saved(code: str, scene_dir: str, timeout: float = 30.0,
              scene_id: str = "", loop: Any = None) -> Any:
    """跑一段已存下来的本机技能代码，返回它的 RESULT（失败抛异常，由调用方如实报）。

    `loop` 见 `_ns_base`：在事件循环里重放要走 `run_saved_async`（它负责把
    exec 挪到 worker 线程并把主循环带进来），**不要**在协程里直接调这个同步版。
    """
    ns = _ns_base(scene_dir, scene_id, loop=loop)
    box = _run_restricted(code, ns, timeout=timeout)
    if not box.get("ok"):
        raise RuntimeError(box.get("error") or "本机技能执行失败")
    return ns.get("RESULT")


async def run_saved_async(code: str, scene_dir: str, timeout: float = 30.0,
                          scene_id: str = "") -> Any:
    """在**事件循环里**重放配方 —— 必须换到 worker 线程跑 exec。

    为什么（2026-10-20 实测）：配方里的取数动作是 MCP 工具，而 MCP 调用靠
    `run_coroutine_threadsafe(..., loop)` 桥回主事件循环（见 `_make_mcp_callable`
    的说明）。如果 exec 就在主循环线程里同步跑，这个"提交给 loop 再阻塞等结果"
    就变成**自己等自己**：loop 正卡在 `cfut.result(timeout=60)` 上，没机会推进
    MCP 请求 → 60 秒后超时抛错。

    现象极具误导性：命中配方、`reuse_errors` 里只有一句超时，于是被当成"配方
    失效"跳过，退回现场现做 —— 用户看到的就是"学过的东西第二次没用上"。

    换到 worker 线程后，`run_coroutine_threadsafe` 才是它设计时的用法（跨线程
    桥接），主循环可以正常推进。`asyncio.to_thread` 在 3.9+ 可用。

    ⚠️ 光换线程还不够（2026-10-20 补）：worker 线程**没有运行中的事件循环**，
    `_ns_base` 里 `asyncio.get_running_loop()` 会抛 RuntimeError → 被 `except
    RuntimeError: pass` 吞掉 → `mcp_*` 一个都不注入，重放直接
    `NameError: name 'mcp_xxx' is not defined`。所以这里把**主循环显式传进去**。
    """
    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(run_saved, code, scene_dir, timeout,
                                   scene_id, loop)
