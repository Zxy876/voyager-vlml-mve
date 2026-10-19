#!/usr/bin/env python3
"""动作编排：**用已注册的取数原语组合出解法**，不是预置模板。

设计案里那句「学习就是把技能写成插件来和 MCP 连用」在本模块的落地：

    · 通用 MCP 是**传输层**，提供 run_query / list_tables / describe_table
      这类**与业务无关**的取数能力（类比 neko mcp_adapter + 网关管线）；
    · 每个场景**注册自己的可执行动作条目**（类比 @plugin_entry(kind="action")），
      条目 = 取数原语，带 name + description + input_schema；
    · Voyager 学到的技能 = **动态注册进来的条目**
      （类比 neko `register_dynamic_entry`，:602）—— 于是「学」和「用」
      走到同一张表上：写代码时检索到的就是这些条目。

编排怎么来（**不许硬编码**）
----------------------------
不是 `if "销量" in q: return [search_news, extract_to_table, sql]`，而是：

    1. `entities_for(question)`  —— **实查** entity_catalog，看这句话里
       出现了哪些可填实体（队伍/图/赛事/选手）。不写死名单。
    2. `entry_candidates(...)`   —— 从**注册表**里挑条目：优先名称/描述
       与「问题里的实体 + 维度词」重合的（词面打分，照 neko match_topics）。
    3. 编排成三步：取数 → 清洗成临时表 → 对临时表跑聚合 SQL。

这样换一个数据源（Scene B）= 换一份注册条目，编排逻辑一行都不用改。
"""

from __future__ import annotations

import re
from typing import Any

import actions


# ---------------------------------------------------------------------------
# ① 实体识别：**实查**，不手写清单
# ---------------------------------------------------------------------------
def entities_for(question: str) -> dict[str, list[str]]:
    """从**当前库**实查出的实体清单里，挑出这句话里真正出现过的那些。

    ⚠️ 手写清单的实测后果（本仓库踩过）：换库之后 GRID 那批队名图名
    （Cloud9 / Lotus / Corrode）已经从库里消失了，手写表还在 → 探针拿
    旧值去问新库 → `No data found for series 1058`，10 个工具里 6 个扑空。
    """
    q = _norm_keep(question)
    out: dict[str, list[str]] = {}
    try:
        import entity_catalog as ec
        ents = (ec.catalog().get("entities") or {})
    except Exception:
        ents = {}
    for kind, items in (ents or {}).items():
        if not isinstance(items, list):
            continue
        hit = []
        for it in items:
            v = str((it or {}).get("value") or "").strip()
            if v and _norm_keep(v) and _norm_keep(v) in q:
                hit.append(v)
        if hit:
            out[str(kind)] = hit
    return out


def _norm_keep(s: str) -> str:
    """归一化但保留中文与字母数字（用于子串包含判定）。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(s or "").lower()).strip()


# ---------------------------------------------------------------------------
# ② 条目检索：从注册表里挑，词面打分（照 neko match_topics）
# ---------------------------------------------------------------------------
def entry_candidates(question: str, scene_id: str, *, limit: int = 5
                     ) -> list[dict[str, Any]]:
    """在该场景注册的条目里，挑与这句话最相关的几个。

    打分 = 名称命中 + 描述命中 + 实体命中，照 neko 那套"词法评分"的思路。
    这里刻意**不认识任何业务词** —— 唯一的信号来自注册表里的条目自身文本，
    所以给任何一个新场景注册条目后，编排立刻能用，不用改代码。
    """
    ents = entities_for(question)
    ent_flat = {v for vals in ents.values() for v in vals}
    q_tok = set(_tokens(question))
    scored: list[tuple[float, dict[str, Any]]] = []
    for spec in actions._REGISTRY.get(str(scene_id), {}).values():
        text = f"{spec.name} {spec.description}"
        n_tok = set(_tokens(text))
        inter = q_tok & n_tok
        if not inter and not (ent_flat & set(_tokens(text))):
            continue
        score = float(len(inter)) + 0.5 * float(len(ent_flat & n_tok))
        scored.append((score, spec.to_entry()))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:limit]]


def _tokens(s: str) -> list[str]:
    """中英混排分词：英文/数字按词，中文按 2-gram（与 skill_store 同一套）。"""
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(s or "").lower())
    out: list[str] = []
    for w in s.split():
        if re.fullmatch(r"[a-z0-9_]+", w):
            out.append(w)
            continue
        if len(w) == 1:
            out.append(w)
            continue
        for i in range(len(w) - 1):
            out.append(w[i:i + 2])
    return out


# ---------------------------------------------------------------------------
# ③ 编排：把条目组合成三步（取数 → 清洗成临时表 → 聚合）
# ---------------------------------------------------------------------------
# ⚠️ 为什么是"组合"不是"模板"：模板意味着代码里写着业务知识（"销量"该调
#   什么工具），换场景就失效，而且会**悄悄正确**（拿旧模板套新问题，跑出 0 行
#   也不报错）。组合只做一件事：把注册表里挑出来的条目串成可执行序列。
def plan(question: str, scene_id: str | None = None) -> dict[str, Any]:
    """编排一次取证：返回每一步要调的条目与参数。

    返回：
        {
          "steps": [{"seq":1,"entry":"<条目名>","args":{...},"why":"..."}],
          "entities": {...},       # 实查到的实体
          "entry_count": int,      # 该场景注册了多少条目（0 = 硬拒绝）
        }
    """
    sid = str(scene_id or "A")
    ents = entities_for(question)
    cands = entry_candidates(question, sid)
    entry_count = actions.entry_count(sid)

    steps: list[dict[str, Any]] = []
    # Step 1：取数 —— 用挑出来的条目（挑不到就退回通用 run_query）
    if cands:
        first = cands[0]
        steps.append({"seq": 1, "entry": first["name"],
                      "args": {k: k for k in
                               (first.get("input_schema") or {})
                               .get("properties", {})},
                      "why": "与这句话最相关的取数原语"})
    else:
        steps.append({"seq": 1, "entry": "run_query", "args": {"sql": "<待生成>"},
                      "why": "未挑到专用原语，退回通用取数"})

    # Step 2：清洗成临时表（临时表名按问题 hash 派生，不写死）
    import hashlib
    h = hashlib.md5(str(question or "").strip().encode("utf-8")).hexdigest()[:8]
    steps.append({"seq": 2, "entry": "extract_to_table",
                  "args": {"table": f"temp_q_{h}"},
                  "why": "把取到的结果清洗成可聚合的临时表"})

    # Step 3：对临时表跑聚合 SQL
    steps.append({"seq": 3, "entry": "run_query",
                  "args": {"sql": f"SELECT ... FROM temp_q_{h}"},
                  "why": "对临时表做聚合得到最终值"})

    return {"steps": steps, "entities": ents, "entry_count": entry_count}


# ---------------------------------------------------------------------------
# 场景初始化：新场景插进来时，把它的可执行动作条目注册好
# ---------------------------------------------------------------------------
def init_scene(scene_id: str, *, data_source: str = "") -> dict[str, Any]:
    """插一个新场景：注册它的可执行动作条目。

    ⚠️ 硬拒绝项的判据就在返回值里：`entry_count == 0` → **不许插入**
    （动作本身缺席，Voyager 在这个场景里什么数据都取不到 —— 这是设计案
    「可执行动作缺了不许插入」的原文含义）。

    返回除条目视图外，另带一个 `insert_allowed` 布尔 + `blocking_reason`，
    让上层（面板/场景注册表）**直接照它决定放不放行**。
    """
    reg = actions.register_builtin_actions(scene_id)
    added = int(reg.get("added") or 0)
    reason = str(reg.get("reason") or "")

    # 非 VLML 场景（普通用户下载下来放本机跑的那一档）：可执行动作 = **本机原语**
    # （list_files / read_file / read_json / query_sql），注册成 origin=local /
    # group=本机能力的一组条目。不注册的话新场景就是「0 个可执行动作」→ 面板按
    # 硬拒绝项拒插 → 普通用户连场景都建不出来（本机没有 vlml 反射）。
    try:
        import scene_profile as _sp
        prof = _sp.profile(scene_id)
        if prof.get("kind") == _sp.LOCAL:
            loc = actions.register_local(scene_id, str(prof.get("scene_dir") or ""))
            added += int(loc.get("added") or 0)
            # 联网能力（用户 2026-10-19：编排要能用「从猫娘搬来的 mcp、插件、
            # 联网能力」去达成任务）—— 与本机能力并排挂猫爪总开关下面。
            added += int(actions.register_net(scene_id).get("added") or 0)
            if loc.get("added"):
                reason = ""
            elif loc.get("reason"):
                reason = str(loc.get("reason"))
    except Exception as e:
        reason = reason or f"本机原语注册失败：{type(e).__name__}: {e}"

    # 场景自带数据源时，额外注册「只针对它」的原语（条目文本由数据源派生，不手写）
    if data_source:
        try:
            import db_switch
            tables = db_switch.list_tables(str(data_source))
        except Exception:
            tables = []
        for t in (tables or [])[:20]:
            if actions.register(scene_id, _make_table_action(t), origin="plugin"):
                added += 1

    view = actions.scene_action_view(scene_id)
    view["insert_allowed"] = view["entry_count"] > 0
    view["blocking_reason"] = ("" if view["insert_allowed"]
                              else (reason or "该场景注册了 0 个可执行动作"))
    view["builtin_added"] = added
    return view


def _make_table_action(table: str):
    """给一张表生成一个取数原语条目（条目文本由**真实表结构**派生）。"""

    async def _fn(mcp, sql: str = "") -> Any:
        q = sql or f"SELECT * FROM {table} LIMIT 200"
        return await mcp.query_sql(sql_query=q)

    _fn.__name__ = f"query_{table}"
    _fn.__doc__ = f"对表 {table} 执行 SQL 取数"
    return actions.action(name=f"query_{table}",
                          description=f"对表 {table} 执行 SQL 取数")(_fn)
