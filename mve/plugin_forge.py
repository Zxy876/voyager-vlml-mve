#!/usr/bin/env python3
"""插件锻造 + 技能结晶 —— 学习闭环的「写插件」层（猫娘计划同构）。

对应用户指令（2026-10-19）
------------------------
「用猫娘计划的 mcp 去写插件，编排插件使用两个层面来完成任务」：

    第一层（本模块）**写插件**：
      · forge_plugin —— 把 task_decompose.plan() 判出的 **gap** 子目标锻造成
        一个插件文件（`mve/plugins/forged_<场景>_<需求slug>.py`），并动态注册进
        该场景的动作命名空间（origin="forged"，同构 neko `register_dynamic_entry`）；
      · crystallize_skill —— 把技能库里**判对后固化的可执行程序**（skill.code）
        结晶成插件条目（origin="learned"）—— orchestrate.py 设计意图「把技能
        写成插件来和 MCP 连用」的字面落地。
    第二层（learn_orchestrate）**编排插件使用**：按子目标顺序 invoke 条目、
    汇总取值。

两条硬自律（与全项目同一套，本模块每一行守它）
--------------------------------------------
1. **派生只从真实结构来**：表清单、列名全部实查 information_schema
   （`db_switch.list_tables` / `db_switch.table_columns`），一个字段名都不手写。
   手写清单的实测后果见 `actions.py` 模块 docstring 的
   pattern_detection_report 签名事故（参数名对不上 → TypeError → 四个维度全 None）。
2. **不可用就如实报**：传输层引不起来（如本地无 vlml/ 数据层）→ 条目照样注册，
   但 invoke 时报 RuntimeError 说清原因；技能源码不是可执行程序（旁路观摩配方
   是伪代码形态）→ **不结晶**，返回 ok=False + why。降级可以，假装成功不行。

原版对照（voyager.py:306-391 / :403-421）
----------------------------------------
原版学到的技能直接进 `skills` 供下一轮 retrieve（`add_new_skill(info)`）。
MVE 多一步「结晶」：技能的 program_code 映射物（skill.code，见
`run_mve._store_skill_for` / `bypass_learn._solution_code`）被包成一个可
invoke 的条目 —— 于是**学**和**用**走到同一张注册表上，下次
`task_decompose.reachability` 就把它判为 reachable 并抓到它，闭环：

    人消费（命中不了）→ 图谱建点 → 拆到抓手可达（gap → forge）
    → invoke 取到值 → 写技能库 → 结晶成条目 → 人再消费同一问题（命中）
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent          # mve/
# 锻造/结晶插件落盘处。生成物（与 skill_store.json / action_registry.json
# 同一性质：由数据派生、可重新生成、不属于源码），已 gitignore。
PLUGIN_DIR = HERE / "plugins"

# 生成插件文件的公共头：docstring + sys.path 自举 + MCP 传输层门面。
# 自举那三行是同构 neko 插件的「自包含」约定：插件文件被
# importlib.util.spec_from_file_location 单独加载时，`import actions`
# 照样能找到 mve/ 目录。
_BOOTSTRAP = '''from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_MVE = Path(__file__).resolve().parent.parent
if str(_MVE) not in sys.path:
    sys.path.insert(0, str(_MVE))


def _new_handle() -> Any:
    """MCP 传输层句柄（猫娘同构的 mcp_adapter 门面；即 action_code.MCPHandle）。"""
    try:
        from action_code import MCPHandle
        return MCPHandle()
    except Exception as e:
        # 传输层没引起来（如本地无 vlml/ 数据层）→ 如实报，不假装能取数
        raise RuntimeError(f"MCP 传输层不可用：{type(e).__name__}: {e}") from e

'''


# ---------------------------------------------------------------------------
# 小工具：标识符 / slug / 进代码的文本净化
# ---------------------------------------------------------------------------
def _ident(s: str) -> str:
    """任意字符串 → 合法标识符（表名/场景名/需求 slug 都用它）。

    中文按 isalnum() 保留（Python 3 标识符合法）；空串/纯符号兜底 "x"。
    """
    out = "".join(ch if (ch.isalnum() or ch == "_") else "_"
                  for ch in str(s or "").strip().lower()).strip("_")
    if not out:
        return "x"
    return out if not out[0].isdigit() else f"t_{out}"


def _slug(s: str, limit: int = 48) -> str:
    """需求文本 → 文件名用 slug（空格转下划线，截断防超长文件名）。"""
    return _ident(str(s or "").strip().replace(" ", "_"))[:limit]


def _oneline(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _safe(s: str) -> str:
    """进生成代码字符串字面量的文本：去引号/反斜杠/换行（防语法破裂）。

    不清内容语义 —— 只保证生成的 .py 永远语法合法。需求原文照进 description
    （entry_candidates 靠它做词面匹配，见 forge_plugin 的注释）。
    """
    return (str(s or "").replace("\\", "/").replace('"', "'")
            .replace("\n", " ").replace("\r", " ").strip())


def _unique(base: str, seen: set[str]) -> str:
    """同一插件内条目名去重（两张表 sanitize 成同名时加序号）。"""
    name, i = base, 2
    while name in seen:
        name = f"{base}_{i}"
        i += 1
    seen.add(name)
    return name


# ---------------------------------------------------------------------------
# 加载器：按模块名从 PLUGIN_DIR 加载（actions.restore 的兜底走这条）
# ---------------------------------------------------------------------------
def load_forged_module(name: str) -> Any:
    """按模块名加载 `mve/plugins/<name>.py`（每次重新 exec，不取缓存）。

    为什么每次重新 exec：forge 会覆盖同名文件（同一需求再 forge = 覆盖更新），
    sys.modules 缓存会返回旧函数对象。也正因此本函数是 `actions._resolve_impl`
    的兜底加载器 —— impl 引用 `forged_A_xxx:query_players` 在面板重启后照样
    能解析（import_module 找不到不在 sys.path 的插件目录，这里按文件找）。

    模块名只认纯标识符：防目录穿越（`../../etc/passwd` 这类拼不进文件名）。
    """
    name = str(name or "")
    if not name or not name.replace("_", "").isalnum():
        return None
    path = PLUGIN_DIR / f"{name}.py"
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod        # 先入表：支持插件内自引用/dataclasses
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        sys.modules.pop(name, None)
        return None


def forged_files() -> list[dict[str, Any]]:
    """PLUGIN_DIR 下的生成插件清单（面板/复盘用：哪个场景 forge 了什么）。"""
    if not PLUGIN_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(PLUGIN_DIR.glob("*.py")):
        stem = p.stem
        kind = ("forged" if stem.startswith("forged_")
                else "learned" if stem.startswith("learned_") else "other")
        out.append({"file": p.name, "module": stem, "kind": kind,
                    "size": p.stat().st_size,
                    "mtime": round(p.stat().st_mtime, 1)})
    return out


# ---------------------------------------------------------------------------
# 第一层①：forge_plugin —— gap 子目标 → 插件文件 + 动态注册
# ---------------------------------------------------------------------------
def forge_plugin(task: str, subtask: dict[str, Any], scene_id: str, *,
                 db_path: str = "") -> dict[str, Any]:
    """给一个 gap 子目标锻造插件：真实表结构 → `@action` 条目 → 文件 + 注册。

    参数：
        task     —— 原问题（进文件头 provenance，也进条目 description）
        subtask  —— task_decompose.plan() 输出的一个子目标（status=="gap"），
                    取它的 need 与 gap_hint（相关表，实查 information_schema 得来）
        scene_id —— 注册进哪个场景的动作命名空间
        db_path  —— 数据源路径（空 = 当前默认库，照 db_switch 口径）

    条目描述里**刻意带上需求原文**：`orchestrate.entry_candidates` 的词面打分
    只看条目自身文本，不带需求就匹配不上 —— 那 forge 完还是 reachable=False，
    闭环断在这。带上需求 + 真实列名，重判 reachability 时才能抓到本条目。

    返回 {"ok", "scene_id", "task", "need", "entries":[...], "tables":{表:列数},
          "skipped":[{table, why}], "file", "module", "why"}。
    """
    out: dict[str, Any] = {"ok": False, "scene_id": str(scene_id),
                           "task": str(task or ""),
                           "need": str((subtask or {}).get("need") or "").strip(),
                           "entries": [], "tables": {}, "skipped": [],
                           "file": "", "module": "", "why": ""}
    need = out["need"]
    tables = [str(t).strip() for t in ((subtask or {}).get("gap_hint") or [])
              if str(t).strip()]
    if not tables and need:
        # gap_hint 丢了（老 plan 输出没有这个字段）→ 按 need 重查，同一把尺子
        try:
            import task_decompose
            tables = task_decompose.related_tables(need)
        except Exception:
            tables = []
    if not need:
        out["why"] = "空子目标，无可锻造"
        return out
    if not tables:
        out["why"] = ("注册表没有抓手、库里也没有相关表 —— 硬不可达。"
                      "该做的是装数据源/换场景，不是 forge 能解决的")
        return out

    import db_switch
    cols_by_table: dict[str, list[str]] = {}
    for t in tables[:10]:                    # 上限 10 张，防 gap_hint 过长
        cols = db_switch.table_columns(str(db_path or ""), t)
        if cols:
            cols_by_table[t] = cols
        else:
            out["skipped"].append({
                "table": t,
                "why": "实查不到列结构（表不存在/不可读）—— 不凭名字编结构"})
    if not cols_by_table:
        out["why"] = f"相关表 {tables} 都实查不到列结构，无法派生条目"
        return out

    module = f"forged_{_ident(scene_id)}_{_slug(need)}"
    src = _render_forge_source(str(scene_id), str(task or ""), need, cols_by_table)
    path = PLUGIN_DIR / f"{module}.py"
    try:
        PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(src, encoding="utf-8")
    except OSError as e:
        out["why"] = f"插件文件写入失败：{type(e).__name__}: {e}"
        return out

    mod = load_forged_module(module)
    if mod is None:
        out["why"] = "插件文件写好了但加载失败（语法/导入错误，见文件本身）"
        return out

    import actions
    n = 0
    for fname in sorted(vars(mod)):
        fn = getattr(mod, fname, None)
        spec = getattr(fn, "__mve_action_spec__", None)
        if spec is None or not callable(fn):
            continue                         # 导入的模块/辅助函数不是条目
        if actions.register(str(scene_id), fn, origin="forged"):
            out["entries"].append(str(spec.name))
            n += 1
    if not n:
        out["why"] = "插件文件里没有可注册的动作条目（生成缺陷）"
        return out
    out.update(ok=True, file=str(path), module=module,
               tables={t: len(c) for t, c in cols_by_table.items()})
    return out


def _render_forge_source(scene_id: str, task: str, need: str,
                         cols_by_table: dict[str, list[str]]) -> str:
    """渲染锻造插件源码（docstring 里写清 provenance：场景/任务/需求/实查结构）。"""
    head = (
        '#!/usr/bin/env python3\n'
        '"""锻造插件（plugin_forge 自动生成，勿手改）。\n\n'
        f'来源：场景 {_safe(scene_id)} · 任务「{_safe(task)}」· '
        f'缺口子目标「{_safe(need)}」\n'
        f'生成时间：{time.strftime("%Y-%m-%d %H:%M:%S")}\n'
        '派生依据（information_schema 实查，不手写）：\n'
        + "".join(f'  - {_safe(t)}: {len(c)} 列\n'
                  for t, c in cols_by_table.items())
        + 'schema 变更后重跑 plugin_forge.forge_gaps 会覆盖本文件。\n'
          '"""\n'
        + _BOOTSTRAP
        + "import actions\n"
    )
    body, seen = "", set()
    for t in sorted(cols_by_table):
        fn_name = _unique(f"query_{_ident(t)}", seen)
        body += _entry_block(t, cols_by_table[t], fn_name, need, scene_id)
    return head + body


def _entry_block(table: str, cols: list[str], fn_name: str, need: str,
                 scene_id: str) -> str:
    """一张表 → 一个取数原语条目。列名截断到前 30 个（防 description 爆炸）。"""
    col_str = ", ".join(str(c) for c in cols[:30])
    if len(cols) > 30:
        col_str += f" …共 {len(cols)} 列"
    desc = (f"（场景 {_safe(scene_id)}）为子目标「{_safe(need)}」锻造的取数原语："
            f"对表 {_safe(table)} 执行 SQL（真实列：{_safe(col_str)}）")
    return f'''

@actions.action(name="{fn_name}", description="{desc}")
async def {fn_name}(mcp=None, sql: str = "") -> Any:
    """对表 {_safe(table)} 执行 SQL 取数（真实列：{_safe(col_str)}）。"""
    handle = mcp if mcp is not None else _new_handle()
    return await handle.query_sql(
        sql_query=sql or f"SELECT * FROM {table} LIMIT 200")
'''


def forge_gaps(plan: dict[str, Any], scene_id: str, *,
               db_path: str = "") -> dict[str, Any]:
    """对一个 plan 里**所有 gap 子目标**逐个 forge，然后照实重判可达性。

    重判是必须的：forge 完就宣称「现在可达了」是声明式结论；本函数对每个
    forge 过的子目标重新走一遍 `task_decompose.reachability`（确定性注册表
    查找，不花钱），把 before/after 都写进返回值 —— 闭环是否真闭合看这个。

    返回 {"task", "scene_id", "forged":[...], "failed":[{need, why}],
          "rechecked":[{seq, need, before, after}], "counts":{...}}。
    """
    import task_decompose
    scene_id = str(scene_id or "A")
    out: dict[str, Any] = {"task": str(plan.get("task") or ""),
                           "scene_id": scene_id, "forged": [], "failed": [],
                           "rechecked": [], "counts": {}}
    gaps = [st for st in (plan.get("subtasks") or [])
            if str((st or {}).get("status")) == "gap"]
    out["counts"]["gap"] = len(gaps)
    forged_needs: list[str] = []
    for st in gaps:
        r = forge_plugin(out["task"], st, scene_id, db_path=db_path)
        if r.get("ok"):
            out["forged"].append(r)
            forged_needs.append(str(st.get("need") or ""))
        else:
            out["failed"].append({"need": str(st.get("need") or ""),
                                  "why": str(r.get("why") or "")})
    for st in gaps:
        need = str(st.get("need") or "")
        after = task_decompose.reachability(need, scene_id)
        out["rechecked"].append({"seq": st.get("seq"), "need": need,
                                 "before": str(st.get("status") or ""),
                                 "after": str(after.get("status") or ""),
                                 "grab": (after.get("grab") or {}).get("name")
                                 if after.get("grab") else ""})
    out["counts"]["forged"] = len(out["forged"])
    out["counts"]["failed"] = len(out["failed"])
    out["counts"]["reachable_after"] = sum(
        1 for r in out["rechecked"] if r["after"] == "reachable")
    return out


# ---------------------------------------------------------------------------
# 第一层②：crystallize_skill —— 技能库的可执行程序 → 插件条目
# ---------------------------------------------------------------------------
def crystallize_skill(skill: dict[str, Any] | str, *,
                      into_scene: str | None = None,
                      local_scene_dir: str = "") -> dict[str, Any]:
    """把一条技能（含可执行 code）结晶成插件条目并注册（origin="learned"）。

    为什么需要 exec：技能的 `code` 是判对后固化的程序（`_plan_program` 渲染的
    重放程序，或模型自己写的 program_code，契约同 action_code：最后一个
    async def、唯一参数 mcp）。原版 add_new_skill 之后下一轮 retrieve 到就能
    用；MVE 的对应物就是把它变成注册表里一个真能 invoke 的条目。

    **只结晶可执行的东西**：技能的 `code` 是程序（async def + 单参数 mcp）
    时原样落盘；是**裸 SQL**（消费侧 _absorb 的配方形态）时机械包成标准
    程序契约（SQL 一字不改，见 `_wrap_sql`）；两者都不是（旁路观摩配方是
    伪代码）→ 如实返回 ok=False。学到话术的技能让它继续以文字形式进 prompt，
    不假装它是程序。

    ⚠️ `code` 存的是**泛化模板**（:slot，给检索+回填通道用）时，结晶 embed
    的换成 `blueprint.rollout_sql`（泛化前判对后固化的原文）—— 带槽位的
    模板原样重放必错，2026-10-26 定位（test_learn_orchestrate 结晶断言挂）。

    into_scene：当前消费场景。技能通常注册进它的 source_scene；若此刻人在另
    一个场景消费同一问题，同时注册进 into_scene（技能是跨场景复用资产，
    但抓手段必须落在人消费的那个场景命名空间里才找得到）。
    """
    out: dict[str, Any] = {"ok": False, "entry": "", "file": "",
                           "module": "", "scenes": [], "why": ""}
    s = _skill_dict(skill)
    if s is None:
        out["why"] = "技能不存在（key 查不到）"
        return out
    code = str(s.get("code") or "")
    # 泛化模板（code 带 :slot）是给「检索 + _refill_sql 回填」通道用的，
    # 原样重放必错（槽位没填）。结晶条目要重放的是**判对后固化的那段程序**
    # —— blueprint.rollout_sql（泛化前的原文，2026-10-26 补记）。没有该
    # 字段的老技能照旧用 code（本机 Python 配方/未泛化的 SQL 都不受影响）。
    _raw = str((s.get("blueprint") or {}).get("rollout_sql") or "").strip()
    if _raw:
        code = _raw
    elif re.search(r":[a-z_][a-z0-9_]*", code, re.I):
        # 泛化模板（带 :slot）且没有 rollout_sql（2026-10-23 泛化上线前
        # 入库的老技能）—— 槽位没填的原样重放必错，如实拒绝，不注册坏条目。
        out["why"] = ("该技能是泛化模板（带 :slot）且无 blueprint.rollout_sql"
                      " —— 模板只能走「检索 + _refill_sql 回填」通道，"
                      "不能原样重放")
        return out
    if not code.strip():
        out["why"] = "该技能没有可执行源码（只有文字经验）—— 学到的是话术不是程序"
        return out
    solve = _extract_solve_name(code)
    wrapped_sql = ""
    local_dir = ""
    if not solve:
        # 第二形态：技能 code 是**裸 SQL**（consume._absorb 存的取数配方就是
        # SQL 文本，不是程序）→ 机械包成标准程序契约（async def + 单参数
        # mcp，调 mcp.query_sql）。SQL 一字不改（json.dumps 生成合法字面量），
        # 包的只是调用外壳 —— 这样消费侧配方也能结晶成可 invoke 的条目，
        # 闭环「写入技能库 → 条目表」对两条通道都成立。
        # 第三形态（2026-10-19，非 VLML 场景 = 本机）：技能 code 是**一段
        # Python**（模型调 list_files/read_file/query_sql 后把答案塞进 RESULT）。
        # 这种技能不能当 SQL 包，也不能当 async def 程序包 —— 用本机执行器
        # 重放：run_saved(code, scene_dir)。用户原话「数据源就是本机」。
        if local_scene_dir and "RESULT" in code:
            local_dir = str(local_scene_dir)
        else:
            wrapped_sql = _wrap_sql(code)
        if local_dir:
            pass                      # 走下面的本机渲染分支
        elif not wrapped_sql:
            out["why"] = ("该技能的源码既不是可执行程序（AST 里找不到「单参数 "
                          "mcp 的 async def」）也不是 SQL（SELECT/WITH 开头）"
                          "—— 旁路观摩配方是伪代码形态，照实不结晶）")
            return out
        if not local_dir:
            code = wrapped_sql
        solve = _extract_solve_name(code) or ""

    name = str(s.get("name") or s.get("key") or "skill")
    entry = f"learned_{_ident(name)}"
    # solve 与条目同名会自覆盖（def 两遍，第二个吃掉第一个）→ 包裹层改名
    wrapper = entry if entry != solve else f"{entry}_run"
    module = f"learned_{_ident(name)}"
    path = PLUGIN_DIR / f"{module}.py"
    try:
        PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        src = (_render_local_crystal_source(s, code, wrapper, local_dir)
               if local_dir else _render_crystal_source(s, code, solve, wrapper))
        path.write_text(src, encoding="utf-8")
    except OSError as e:
        out["why"] = f"插件文件写入失败：{type(e).__name__}: {e}"
        return out

    mod = load_forged_module(module)
    if mod is None:
        out["why"] = "结晶插件加载失败（技能源码原文 exec 出错）"
        return out
    fn = getattr(mod, wrapper, None)
    if fn is None or getattr(fn, "__mve_action_spec__", None) is None:
        out["why"] = "结晶插件里没有条目（生成缺陷）"
        return out

    src_scene = str(s.get("source_scene") or "default")
    scenes = [src_scene]
    if into_scene and str(into_scene) != src_scene:
        scenes.append(str(into_scene))
    import actions
    ok_scenes = [sc for sc in scenes
                 if actions.register(sc, fn, origin="learned")]
    out.update(ok=bool(ok_scenes), entry=wrapper, file=str(path),
               module=module, scenes=ok_scenes,
               why="" if ok_scenes else "注册失败（actions.register 返回 None）")
    return out


def _render_local_crystal_source(s: dict[str, Any], code: str, wrapper: str,
                                 scene_dir: str) -> str:
    """本机技能结晶：技能 code 原文照录，条目用 run_saved 重放（不经 VLML）。"""
    lesson = _safe(_oneline(str(s.get("text") or "")))[:120]
    code_lit = json.dumps(str(code))
    dir_lit = json.dumps(str(scene_dir))
    doc = ("本机技能（plugin_forge 自动生成，勿手改）。\n\n"
           f"技能：{_safe(str(s.get('name') or ''))}（场景 {_safe(scene_dir)}）\n"
           f"文字经验：{lesson}\n"
           "数据源 = 本机：重放时用 local_agent 的本机原语（list_files / "
           "read_file / query_sql），不碰 VLML。")
    return (
        "#!/usr/bin/env python3\n"
        f'"""{doc}"""\n'
        + _BOOTSTRAP
        + "import actions\n"
        + "from local_agent import run_saved\n\n"
        + f"CODE = {code_lit}\n"
        + f"SCENE_DIR = {dir_lit}\n\n\n"
        + f'@actions.action(name="{wrapper}", description="本机技能（可重放）：{lesson}")\n'
        + f"async def {wrapper}(mcp=None) -> Any:\n"
        + '    """重放上面学到的本机代码（只读原语），返回它的结果。"""\n'
        + "    return run_saved(CODE, SCENE_DIR)\n"
    )


def _render_crystal_source(s: dict[str, Any], code: str, solve: str,
                           wrapper: str) -> str:
    """渲染结晶插件：技能 code **原文**照录 + 一个可 invoke 的包裹条目。"""
    lesson = _safe(_oneline(str(s.get("text") or "")))[:120]
    head = (
        '#!/usr/bin/env python3\n'
        '"""结晶插件（plugin_forge 从技能库自动生成，勿手改）。\n\n'
        f'技能：{_safe(str(s.get("name") or ""))}'
        f'（topic={s.get("topic")} · source={s.get("source")} · key={s.get("key")}）\n'
        f'文字经验：{lesson}\n'
        '下方是技能库 code 字段**原文**（判对后固化、口径已由裁判验证的可重放程序）。\n'
        '"""\n'
        + _BOOTSTRAP
        + "import actions\n\n\n"
        + str(code).rstrip() + "\n\n\n"
    )
    tail = (
        f'@actions.action(name="{wrapper}", description="技能条目（可重放）：{lesson}")\n'
        f'async def {wrapper}(mcp=None) -> Any:\n'
        f'    """重放上面学到的程序（{solve}），原样返回它的观测。"""\n'
        f'    handle = mcp if mcp is not None else _new_handle()\n'
        f'    return await {solve}(handle)\n'
    )
    return head + tail


def _extract_solve_name(code: str) -> str | None:
    """找「最后一个 单参数 mcp 的 async def」—— 与 action_code 同一套契约。

    照 action_code.py 的同构映射（原版 process_ai_message 断言唯一参数名
    `bot`）：参数表必须正好 [mcp]，不带 *args/**kwargs/keyword-only。
    """
    try:
        tree = ast.parse(str(code or ""))
    except SyntaxError:
        return None
    solve: str | None = None
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        a = node.args
        pos = list(getattr(a, "posonlyargs", []) or []) + list(a.args)
        if (len(pos) == 1 and pos[0].arg == "mcp"
                and not a.kwonlyargs and a.vararg is None and a.kwarg is None):
            solve = node.name          # 照原版：取最后一个
    return solve


def _wrap_sql(code: str) -> str:
    """裸 SQL → 标准程序契约（async def + 单参数 mcp，调 mcp.query_sql）。

    只发生在技能 code 是 SQL 文本时（消费侧 _absorb 的配方形态）。SQL 原文
    一字不改，用 json.dumps 生成合法 Python 字符串字面量（引号/换行/反斜杠
    都不会把生成文件搞成语法错误）。返回空串 = 不像 SQL（调用方如实报）。
    """
    sql = str(code or "").strip().rstrip(";").strip()
    if not sql or not re.match(r"(?is)^\s*(select|with)\b", sql):
        return ""
    lit = json.dumps(sql)
    return (f"# 由 plugin_forge 从技能库的 SQL 配方机械包裹而成（SQL 原文未改一字）\n"
            f"async def skill_recipe_solve(mcp):\n"
            f"    return await mcp.query_sql(sql_query={lit})\n")


def _skill_dict(skill: dict[str, Any] | str) -> dict[str, Any] | None:
    """接受技能 dict 或 key 字符串（后者去技能库实查）。"""
    if isinstance(skill, dict):
        return skill
    try:
        import skill_store
        for s in skill_store.all_skills():
            if str(s.get("key")) == str(skill):
                return s
    except Exception:
        return None
    return None


def crystallize_all(scene_id: str, *,
                    topic: str | None = None) -> dict[str, Any]:
    """把技能库里**全部可执行**技能结晶进场景（幂等：已注册的跳过）。

    什么时候调：学习跑完一轮后（learn_orchestrate 的学习步末），或面板重启
    自检时（技能库有 code 但注册表被清）。只结晶有 code 且 code 是程序的
    —— 伪代码技能照实进 failed 并带 why。
    """
    import actions
    import skill_store
    scene_id = str(scene_id or "A")
    out: dict[str, Any] = {"scene_id": scene_id, "crystallized": [],
                           "skipped": [], "failed": []}
    for s in skill_store.all_skills():
        if topic and str(s.get("topic")) != str(topic):
            continue
        name = str(s.get("name") or s.get("key") or "skill")
        entry = f"learned_{_ident(name)}"
        src_scene = str(s.get("source_scene") or "default")
        targets = {src_scene, scene_id}
        if all(entry in actions.names(sc) for sc in targets):
            out["skipped"].append({"entry": entry, "why": "已注册（幂等跳过）"})
            continue
        r = crystallize_skill(s, into_scene=scene_id)
        item = {"entry": r.get("entry") or entry, "key": s.get("key"),
                "why": str(r.get("why") or "")}
        (out["crystallized"] if r.get("ok") else out["failed"]).append(item)
    return out
