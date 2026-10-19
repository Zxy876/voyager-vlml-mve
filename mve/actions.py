#!/usr/bin/env python3
"""通用 MCP：传输层 + **场景级可执行动作条目**（同构猫娘计划）。

为什么要这个模块
----------------
设计案里的硬拒绝项「可执行动作」一直是个含糊词。照猫娘 `N.E.K.O` 的做法把它
拆成**两层**（`PLUGIN_DEVELOPMENT_GUIDE.md:1438` 的 MCP Adapter 是传输层、`:95`
的代码目录与状态目录分离、`@plugin_entry(kind="action")` 注册条目）：

    ┌─ 通用 MCP / Voyager 核心（传输层）───────────────────────┐
    │ 不持有任何业务动作，只提供**通用取数机制**：          │
    │   · run_query(sql)      执行一段 SQL，返回 rows       │
    │   · list_tables()       列出数据源里的表              │
    │   · describe_table(t)   取某张表的列                  │
    │ 类比 neko 的 mcp_adapter + 网关管线。                │
    └──────────────────────────────────────────────────┘
                     ▲ 调用
    ┌─ 场景（Scene A / B）──────────────────────────────────┐
    │ 每个场景 = 一个插件（类比 @neko_plugin）             │
    │ 各自注册自己的**可执行动作条目**（类比 @plugin_entry） │
    │ 条目 = 取数原语：name + description + input_schema    │
    └──────────────────────────────────────────────────────┘

「插入 Scene B 时能不能看到新增了哪几个可执行动作条目」→ 能：
`entries(scene_id)` 返回该场景注册的全部条目（name/description/input_schema），
面板直接列出来（仿 neko `PluginMeta.entries` / `entry_count`，`api.ts:137,149`）。

⚠️ **不许硬编码**（用户明确要求）
--------------------------------
两条硬规矩，本模块的每一行都在守它：

1. **条目清单不写死**。Scene A 的 10 个工具来自 `vlml_env` 的**真实函数反射**
   （`inspect.signature` + `typing.get_type_hints`），不是手抄的名单。
   手写名单的实测后果（本仓库踩过）：`pattern_detection_report` 我写成
   `(series_id, team_name=None)`，真实签名是 `(team_name=None, player_name=None,
   tournament_name=None, series_ids=None, min_rounds=200)` —— 参数名对不上，探针调用
   直接 TypeError，结构文档拿不到，模型只能继续猜字段名（四个维度全 dig 到 None）。

2. **实体可填值不写死**。取数配方里的实体（队伍/图/赛事）一律走
   `entity_catalog` **实查** + 注入常量（见 `action_code._subject_env`）。
   写死字面量的实测后果：技能里写死 `series_id=2843069 / map_name='Lotus'`，
   换库之后这些值一个都不存在 → 复用时全部 0 行 → 考核覆盖率回 0
   （面板：摸底 16% → 结业 0%）。

   所以下面 `_reflect_schema()` 是**反射**，`register_scene_actions()` 从
   **运行时注册表**取，不预先写死任何工具名。
"""

from __future__ import annotations

import inspect
import json
import os
import typing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

# 条目类型（照 neko 的 EntryKind；MVE 用到的两种）
ACTION = "action"        # 可执行动作（取数原语）
VIEW = "view"            # 只读视图（面板卡片）

# 场景 → {条目名: ActionSpec}
_REGISTRY: dict[str, dict[str, "ActionSpec"]] = {}

# 注册表快照落盘处（与 scenes.json / skill_store.json 同目录，gitignored）。
# ⚠️ 为什么必须落盘：注册表是**进程内存态**，面板一重启就没了 —— 实测
# `action_count {"A":0}` 而独立进程里反射出 10 条。场景卡会显示「0 个可执行
# 动作 → 硬拒绝」，把明明能跑的场景误判成跑不起来。所以每注册一次就落盘，
# 面板启动时恢复。
SNAPSHOT = Path(__file__).resolve().parent / "action_registry.json"

# 装饰器打在函数上的标记
_SPEC_ATTR = "__mve_action_spec__"


@dataclass
class ActionSpec:
    """一个场景级可执行动作条目（同构 neko 的 PluginEntry）。"""

    name: str
    description: str
    fn: Callable[..., Any]
    scene_id: str = "A"
    kind: str = ACTION
    input_schema: dict[str, Any] = field(default_factory=dict)
    # 来源可追溯：builtin（通用 MCP 自带）/ learned（学到的技能）/ plugin（场景插件）
    origin: str = "builtin"
    # impl 描述（可序列化）："module:qualified_name" —— 快照里只存这个，
    # 靠它恢复可调用实现，而不是把函数对象写进 json（那不可能）。
    impl: str = ""

    def to_entry(self) -> dict[str, Any]:
        """给 UI 的条目视图（仿 `PluginMeta.entries`）。"""
        return {
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "input_schema": self.input_schema,
            "scene_id": self.scene_id,
            "origin": self.origin,
            "impl": self.impl,
        }


# ---------------------------------------------------------------------------
# 反射：签名 → JSON Schema（**绝不手写**）
# ---------------------------------------------------------------------------
_JSON_TYPE: dict[Any, str] = {
    str: "string", int: "integer", float: "number", bool: "boolean",
    list: "array", dict: "object",
}


def _type_name(ann: Any) -> str:
    """把 typing 注解翻成 JSON Schema 的 type 名。"""
    origin = typing.get_origin(ann)
    # Optional[X] / X | None → X
    if origin in (typing.Union, getattr(__import__("types"), "UnionType", object)):
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        return _type_name(args[0]) if args else "string"
    if origin is not None:
        return _JSON_TYPE.get(origin, "string")
    return _JSON_TYPE.get(ann, "string")


def _reflect_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    """从**真实函数签名**生成 JSON Schema —— 不手写任何参数名。

    这是"通用 MCP 传输层"的核心自律：条目说明来自代码本身，代码改了条目
    自动跟着改，不存在"文档与实现漂移"。
    """
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}
    sig = inspect.signature(fn)
    props: dict[str, Any] = {}
    required: list[str] = []
    for pname, p in sig.parameters.items():
        if pname in ("self", "mcp"):
            continue
        ann = hints.get(pname, p.annotation)
        props[pname] = {"type": _type_name(ann)}
        if p.default is inspect._empty:
            required.append(pname)
    return {"type": "object", "properties": props,
            "required": required}


# ---------------------------------------------------------------------------
# 装饰器（同构 @plugin_entry(kind="action")）
# ---------------------------------------------------------------------------
def action(*, kind: str = ACTION, name: str | None = None,
           description: str = "", schema: dict[str, Any] | None = None):
    """把一个函数注册成**某个场景**的可执行动作条目。

    用法（与 neko 插件同构）：

        @action(name="run_query_on_B", description="在 B 的数据源上执行 SQL")
        async def run_query(mcp, sql: str):
            return await mcp.query_sql(sql_query=sql)

    注意主函数约定与 `action_code` 一致：
    第一个参数是 `mcp`（传输层句柄），其余是业务参数。
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        spec = ActionSpec(
            name=name or fn.__name__,
            description=description or ((fn.__doc__ or "").strip().splitlines()
                                        or [""])[0],
            fn=fn,
            kind=kind,
            input_schema=schema or _reflect_schema(fn),
            origin="plugin",
        )
        setattr(fn, _SPEC_ATTR, spec)
        return fn

    return deco


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
def _impl_ref(fn: Callable[..., Any]) -> str:
    """函数的可序列化引用：`module:qualified_name`。"""
    mod = getattr(fn, "__module__", "") or ""
    qual = getattr(fn, "__qualname__", "") or getattr(fn, "__name__", "") or ""
    return f"{mod}:{qual}" if mod else qual


def _resolve_impl(ref: str) -> Callable[..., Any] | None:
    """按 `module:qualified_name` 找回可调用实现。找不到返回 None（如实报缺）。"""
    if not ref or ":" not in ref:
        return None
    mod_name, _, qual = ref.partition(":")
    mod = None
    try:
        import importlib
        mod = importlib.import_module(mod_name)
    except Exception:
        mod = None
    if mod is None:
        # 锻造/结晶插件兜底：插件文件在 mve/plugins/ 下，不在 sys.path 上，
        # import_module 找不到 —— 走 plugin_forge 的文件加载器（同一条 impl
        # 引用，重启后照样能恢复条目）。
        try:
            import plugin_forge
            mod = plugin_forge.load_forged_module(mod_name)
        except Exception:
            mod = None
    if mod is None:
        return None
    obj: Any = mod
    for part in qual.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj if callable(obj) else None


def _save_snapshot() -> None:
    """把注册表快照落盘（只存可序列化字段 + impl 引用）。"""
    try:
        data = {"version": 1, "scenes": {
            sid: [s.to_entry() for s in specs.values()]
            for sid, specs in _REGISTRY.items()
        }}
        SNAPSHOT.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    except Exception:
        pass


def restore() -> dict[str, int]:
    """从快照恢复注册表（面板启动时调，避免重启后「0 个可执行动作」误报）。

    返回每场景恢复到的条目数。恢复不了的 impl（模块不在）就跳过并如实少算 ——
    不编一个假的补上。
    """
    if not SNAPSHOT.exists():
        return {}
    try:
        data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, int] = {}
    for sid, entries in (data.get("scenes") or {}).items():
        n = 0
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            fn = _resolve_impl(str(e.get("impl") or ""))
            if fn is None:
                continue
            _REGISTRY.setdefault(sid, {})[e["name"]] = ActionSpec(
                name=e["name"], description=str(e.get("description") or ""),
                fn=fn, scene_id=sid, kind=str(e.get("kind") or ACTION),
                input_schema=dict(e.get("input_schema") or {}),
                origin=str(e.get("origin") or "builtin"),
                impl=str(e.get("impl") or ""),
            )
            n += 1
        out[sid] = n
    return out


def register(scene_id: str, fn: Callable[..., Any], *,
             origin: str = "plugin") -> ActionSpec | None:
    """把一个带 `@action` 标记的函数注册进某场景的命名空间。

    ⚠️ 用 `replace()` 复制一份 spec 而不是原地改：原实现直接 `spec.scene_id =
    str(scene_id)`，同一个 fn 注册进两个场景时两个命名空间会**共享同一个
    spec 对象**，后注册的场景把先注册的 scene_id 覆盖掉（快照落盘时两个
    场景的条目都显示成后一个场景）。插件锻造的「同一技能注册进来源场景+
    当前场景」正好踩这条，所以这里改成每场景一份独立 spec。
    """
    spec = getattr(fn, _SPEC_ATTR, None)
    if spec is None:
        return None
    # impl 在这里重算：装饰器当时不知道该 fn 会注册进哪个模块上下文，
    # 而 fn.__module__ 对插件文件是加载时定的，注册时取才准。
    reg = replace(spec, scene_id=str(scene_id), origin=origin,
                  impl=_impl_ref(spec.fn))
    _REGISTRY.setdefault(str(scene_id), {})[reg.name] = reg
    _save_snapshot()
    return reg


def register_builtin_actions(scene_id: str) -> dict[str, Any]:
    """把**通用 MCP 传输层自带的取数原语**注册进场景。

    ⚠️ 关键：这里是**反射** `vlml_env` 里真实存在的工具函数，不手写名单。
    拿不到（环境未引导，如浅克隆没有 `vlml/`）就返回 0 **并说明原因**，
    让上层如实报「0 个可执行动作 → 硬拒绝」，
    而不是编一份看起来很全的清单（那会让人以为能取数，实际一调就崩）。

    返回 `{"added": int, "tools": [...], "reason": "为什么拿不到"}`。
    """
    try:
        import action_code
    except Exception as e:
        return {"added": 0, "tools": [],
                "reason": f"无法引导 VLML 环境：{type(e).__name__}: {e}"[:300]}
    tools = list(getattr(action_code, "TOOL_NAMES", []) or [])
    if not tools:
        return {"added": 0, "tools": [], "reason": "TOOL_NAMES 为空"}
    impl_names = getattr(action_code, "_IMPL_NAME", {}) or {}
    n = 0
    for t in tools:
        impl_name = impl_names.get(t, t)
        fn = getattr(action_code.vlml_env, impl_name, None)
        if fn is None:
            continue
        spec = ActionSpec(
            name=t,
            description=((fn.__doc__ or "") or f"MCP 工具 {t}").strip().splitlines()[0][:120],
            fn=fn,
            scene_id=str(scene_id),
            input_schema=_reflect_schema(fn),
            origin="builtin",
            impl=_impl_ref(fn),
        )
        _REGISTRY.setdefault(str(scene_id), {})[t] = spec
        n += 1
    if n:
        _save_snapshot()
    return {"added": n, "tools": tools, "reason": ""}


def unregister(scene_id: str, name: str) -> bool:
    bucket = _REGISTRY.get(str(scene_id))
    if not bucket or name not in bucket:
        return False
    del bucket[name]
    _save_snapshot()
    return True


def entries(scene_id: str) -> list[dict[str, Any]]:
    """某场景注册的全部可执行动作条目（UI 用）。"""
    return [s.to_entry() for s in
            _REGISTRY.get(str(scene_id), {}).values()]


def entry_count(scene_id: str) -> int:
    """某场景注册了多少个可执行动作条目（== 硬拒绝项判据：0 就不许插入）。"""
    return len(_REGISTRY.get(str(scene_id), {}))


def names(scene_id: str) -> list[str]:
    return sorted(_REGISTRY.get(str(scene_id), {}).keys())


async def invoke(scene_id: str, name: str, **kwargs: Any) -> Any:
    """执行某场景的一个动作条目。"""
    spec = _REGISTRY.get(str(scene_id), {}).get(name)
    if spec is None:
        raise KeyError(f"场景 {scene_id} 没有可执行动作 {name}")
    res = spec.fn(**kwargs)
    if inspect.isawaitable(res):
        res = await res
    return res


def scene_action_view(scene_id: str) -> dict[str, Any]:
    """场景卡要显示的「可执行动作」视图（仿 neko 插件条目清单）。"""
    return {
        "scene_id": str(scene_id),
        "entry_count": entry_count(scene_id),
        "entries": entries(scene_id),
    }
