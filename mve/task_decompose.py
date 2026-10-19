#!/usr/bin/env python3
"""任务拆分与可达性判定 —— 同构原版 Voyager 的 CurriculumAgent.decompose_task。

原版取证（改这行逻辑前先读这三处）
----------------------------------
1. `voyager_original/voyager/agents/curriculum.py:372-385` `decompose_task(task,
   events)`：SystemMessage(load_prompt("curriculum_task_decomposition")) +
   render_human_message(观测) + HumanMessage("Final task: {task}") →
   LLM → `fix_and_parse_json(response)`。
2. `voyager_original/voyager/prompts/curriculum_task_decomposition.txt`：输出
   必须是**可 json.loads 的子目标列表**，动宾简洁格式，并且
   **"Include each level of necessary tools as a subgoal"** ——
   前置依赖（工具等级链）也要拆成子目标。
3. `voyager_original/voyager/voyager.py:403-421` `inference()`：没有 sub_goals
   就先拆分，然后 `while progress < len(sub_goals):
   next_task = sub_goals[progress]` —— 子目标**按序**一个一个完成。

MVE 同构映射（1→n 场景化）
--------------------------
    原版概念                MVE 对应
    背包 inventory     →    当前场景的图谱节点 label（场景已有什么）
    附近方块/实体      →    entity_catalog **实查**出的可填实体
    工具等级木→石→铁→钻 →  已注册的可执行动作条目（抓手，actions registry）
    "每级必要工具拆子目标" → "取到这个值所需的**前置取数原语**也拆出来，
                              每个子目标都过一遍抓手检查"

两条自律（本模块每一行都守）
--------------------------
1. **拆分只由 LLM 做**（同构原版：decompose_task 就是一次 LLM 调用）。
   规则兜底只在 LLM 不可用（无 key / 调用失败）时启用，且返回里如实标
   `origin="rule_fallback"` —— 降级可以，假装拆过不行。
2. **可达性判定是确定性的注册表查找**，不用模型、不猜：一个子目标有没有
   抓手，只看该场景的 action registry 里有没有条目能服务它（词面+实体
   打分，照 orchestrate.entry_candidates 同一把尺子）。

子目标状态（可达性）
------------------
    reachable   —— 注册表里有抓手条目，可以直接执行
    gap         —— 没有条目，但当前库里有相关的**表**（实查 information_schema）
                   → 可以写插件补上（plugin_forge.forge_plugin）
    unreachable —— 条目和表都没有 → 硬不可达，如实报给人（装插件/换数据源）
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

# 原版 curriculum_task_decomposition.txt 的 MVE 同构版。
# 逐条对应原版三条 criteria（JSON 列表 / 动宾简洁 / 前置依赖也拆），
# 把"工具等级"换成"取数原语与数据表"——这是 MVE 的"工具"。
DECOMPOSE_SYSTEM = """You are a helpful assistant that decomposes a data question into an ordered list of subgoals.

I'll give you a final question and the current state of the scene (knowledge graph nodes, known entities, registered action entries), you need to decompose the question into subgoals based on that state.

You must follow the following criteria:
1) Return a Python list of subgoals that can be completed in order to answer the specified question.
2) Each subgoal should follow a concise format, such as "Get [entity] [metric]", "Filter [condition]", "Aggregate [metric] by [group]", "Rank by [metric]".
3) Include each prerequisite as a subgoal: if answering needs an entity value or a base metric first, make it its own subgoal.

You should only respond in JSON format as described below:
["subgoal1", "subgoal2", "subgoal3", ...]
Ensure the response can be parsed by Python `json.loads`, e.g.: no trailing commas, no single quotes, etc."""


# ---------------------------------------------------------------------------
# 观测：场景当前状态（原版 render_observation 的 MVE 对应物）
# ---------------------------------------------------------------------------
def scene_observation(question: str, scene_id: str) -> dict[str, Any]:
    """当前场景的「观测」：图谱节点 + 实查实体 + 已注册动作条目。

    三项全部来自真实状态，不手写：
      · 节点 —— KnowledgeGraph.load() 里 root_scene==scene 的 label
      · 实体 —— entity_catalog 实查当前库（ orchestrate.entities_for 同源）
      · 条目 —— actions 注册表（含 builtin / learned / forged）
    """
    nodes: list[str] = []
    try:
        from knowledge_graph import KnowledgeGraph
        g = KnowledgeGraph.load()
        for n in g.nodes.values():
            if str(getattr(n, "root_scene", "A") or "A") != str(scene_id):
                continue
            if str(getattr(n, "status", "") or "") == "deleted":
                continue
            label = str(getattr(n, "label", "") or "").strip()
            if label:
                nodes.append(label)
    except Exception:
        nodes = []
    entities: dict[str, list[str]] = {}
    try:
        import orchestrate
        entities = orchestrate.entities_for(question)
    except Exception:
        entities = {}
    entries: list[dict[str, Any]] = []
    try:
        import actions
        entries = [{"name": e.get("name", ""),
                    "description": str(e.get("description") or "")[:80]}
                   for e in actions.entries(scene_id)]
    except Exception:
        entries = []
    return {"scene_id": str(scene_id), "nodes": nodes[:30],
            "entities": entities, "entries": entries[:30]}


def render_human_message(task: str, obs: dict[str, Any]) -> str:
    """把观测渲染成 prompt 的 human 消息（照原版 render_human_message 的角色）。"""
    nodes = ", ".join(obs.get("nodes") or []) or "None"
    ents = obs.get("entities") or {}
    ent_str = "; ".join(f"{k}: {', '.join(v)}" for k, v in ents.items()) or "None"
    entries = obs.get("entries") or []
    ent_list = "\n".join(f"  - {e['name']}: {e['description']}"
                         for e in entries) or "  (none)"
    return (f"Scene: {obs.get('scene_id')}\n"
            f"Knowledge graph nodes: {nodes}\n"
            f"Known entities: {ent_str}\n"
            f"Registered action entries:\n{ent_list}\n\n"
            f"Final task: {task}")


# ---------------------------------------------------------------------------
# 拆分：LLM（同构原版），规则兜底如实标记
# ---------------------------------------------------------------------------
def _parse_subgoals(text: str) -> list[str]:
    """尽量容忍地解析子目标列表（原版 fix_and_parse_json 的同构物）。"""
    raw = str(text or "")
    m = re.search(r"```(?:json)?\s*(\[.*\])\s*```", raw, re.S)
    if m:
        raw = m.group(1)
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(s).strip() for s in data if str(s).strip()]


def _default_llm(messages: list[dict[str, str]]) -> str:
    """真实 LLM（智谱）。延迟导入，测试可注入假 llm。"""
    from llm_client import chat
    return chat(messages, temperature=0.2, max_tokens=1024)


def decompose(task: str, scene_id: str, *, llm: Callable | None = None,
              max_retries: int = 2) -> dict[str, Any]:
    """把问题拆成有序子目标列表（同构原版 decompose_task）。

    返回 `{"task", "scene_id", "subgoals": [...], "origin", "reason"}`。
    LLM 不可用/解析失败时走规则兜底（origin=rule_fallback），绝不返回空列表
    假装成功 —— 调用方要靠 origin 知道这拆分是怎么来的。
    """
    task = str(task or "").strip()
    obs = scene_observation(task, scene_id)
    human = render_human_message(task, obs)
    messages = [
        {"role": "system", "content": DECOMPOSE_SYSTEM},
        {"role": "user", "content": human},
    ]
    call = llm or _default_llm
    last_err = ""
    for attempt in range(max_retries + 1):
        try:
            text = call(messages)
            goals = _parse_subgoals(text)
            if goals:
                return {"task": task, "scene_id": str(scene_id),
                        "subgoals": goals, "origin": "llm", "reason": ""}
            last_err = "LLM 输出解析不出子目标列表"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < max_retries:
            continue
    # LLM 这条路整个断了 → 规则兜底，如实标 origin
    goals = _rule_decompose(task, obs)
    return {"task": task, "scene_id": str(scene_id), "subgoals": goals,
            "origin": "rule_fallback",
            "reason": f"LLM 拆分不可用（{last_err}），按实体×维度机械拆"}


def _rule_decompose(task: str, obs: dict[str, Any]) -> list[str]:
    """规则兜底拆分：实查实体 + 问题里的维度词，机械拆成「取实体→按维度取数」。

    ⚠️ 这是降级路径，只保证**可执行、可判定**，不保证拆得比 LLM 好。
    为什么还需要它：无 key / LLM 超时是常态（服务器实测过），那时候
    学习循环整个卡住比拆得粗糙更糟。
    """
    goals: list[str] = []
    ents = obs.get("entities") or {}
    flat = [v for vals in ents.values() for v in vals]
    if flat:
        goals.append(f"Get {flat[0]}")
    goals.append(f"Get metrics for: {task[:60]}")
    if len(flat) > 1:
        goals.append(f"Filter by {flat[1]}")
    return goals


# ---------------------------------------------------------------------------
# 可达性：每个子目标的抓手状态（确定性注册表查找，不用模型）
# ---------------------------------------------------------------------------
def reachability(need: str, scene_id: str) -> dict[str, Any]:
    """一个子目标在当前场景有没有「抓手」。

    判据（与 orchestrate.entry_candidates 同一把尺子，不另造）：
      · 条目打分 > 0            → reachable（grab=最相关条目）
      · 无条目但库里有相关表      → gap（可写插件补，gap_hint=相关表）
      · 两者都没有               → unreachable（硬不可达，如实报）

    ⚠️ 查条目时先把**通用动词滤掉**（`_content_query`）：拆分 prompt 要求
    子目标是 "Get X / Filter by Y" 这种动宾格式，而锻造条目的描述里嵌着
    **它自己那个**子目标的原文 —— 不滤动词的话，"Get Tesla stock price"
    会和任何一条 "Get ..." 锻造条目撞上 "get" 一个词 → 假 reachable
    （2026-10-19 实测：特斯拉问题判成 reachable，抓手却是 players 的条目）。
    滤掉 get/filter/by 这类空词后，撞的只能是实体与指标词。

    「库里有相关表」的判据 = 表名与子目标词面有重合（实查 information_schema，
    见 db_switch.list_tables）。不猜表内容，只看名字。
    """
    need = str(need or "").strip()
    out: dict[str, Any] = {"need": need, "status": "unreachable",
                           "grab": None, "score": 0.0, "why": ""}
    if not need:
        out["why"] = "空子目标"
        return out
    try:
        import orchestrate
        cands = orchestrate.entry_candidates(_content_query(need), scene_id,
                                             limit=1)
    except Exception:
        cands = []
    if cands:
        out["status"] = "reachable"
        out["grab"] = cands[0]
        out["score"] = 1.0
        out["why"] = f"抓手条目：{cands[0].get('name')}"
        return out
    # 没有条目 → 看库里有没有相关表（可 forge 的依据）
    tables = _related_tables(need)
    if tables:
        out["status"] = "gap"
        out["gap_hint"] = tables[:5]
        out["why"] = (f"注册表没有抓手，但库里有相关表 {tables[:3]} —— "
                      f"可以为它写插件（plugin_forge.forge_plugin）")
        return out
    out["why"] = "注册表没有抓手，库里也没有相关表 —— 硬不可达"
    return out


def _related_tables(need: str) -> list[str]:
    """表名与需求词面有重合的表（实查，不手写清单）。"""
    try:
        import db_switch
        tables = db_switch.list_tables()
    except Exception:
        return []
    toks = _lex_set(need)
    if not toks:
        return []
    hit = []
    for t in tables:
        if toks & _lex_set(t):
            hit.append(t)
    return hit


def related_tables(need: str) -> list[str]:
    """公开口径：reachability 判 gap 用的就是这张表（plugin_forge 同一把尺子）。"""
    return _related_tables(need)


def _stem(tok: str) -> str:
    """极轻量词干：只脱尾 s（players→player / kills→kill）。

    不是语言学，只是让「查一下 player 的 kills」能匹配上 `players` 表 ——
    `_tokens` 把整词当一个 token，"player" 与 "players" 原样比对永远不重合。
    错判的代价只是多 forge 一个插件（仍从真实结构派生、可执行），
    漏判的代价是把 gap 误判成 unreachable（硬停），所以偏向前者。
    """
    return tok[:-1] if len(tok) > 3 and tok.endswith("s") else tok


def _lex_set(s: str) -> set[str]:
    """词面集合：分词 + 下划线拆词（match_players → match/players）+ 脱尾 s。"""
    out: set[str] = set()
    for w in re.split(r"[\s_]+", str(s or "")):
        for t in _tokens(w):
            out.add(t)
            out.add(_stem(t))
    return out


def _tokens(s: str) -> list[str]:
    """与 orchestrate._tokens 同一套分词（英文按词、中文 2-gram）。"""
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


# 子目标里的通用动词/虚词（拆分 prompt 的动宾格式带来的）。查抓手段时滤掉，
# 防止 "Get X" 与另一条 "Get Y" 锻造条目撞一个 "get" 就假判 reachable。
_STOPWORDS = {
    "get", "filter", "by", "aggregate", "rank", "show", "list", "compute",
    "count", "find", "select", "the", "a", "an", "of", "for", "and", "or",
    "with", "from", "in", "on", "per", "to", "at", "is", "are", "all",
}


def _content_query(need: str) -> str:
    """子目标 → 去掉通用动词后的查询串（全滤光就退回原串，不更糙）。"""
    words = [w for w in re.split(r"\s+", str(need or ""))
             if w.strip(".,?？!;:，。") and
             w.strip(".,?？!;:，。").lower() not in _STOPWORDS]
    return " ".join(words) or str(need or "")


# ---------------------------------------------------------------------------
# 汇总：一次拆分 + 逐个子目标过可达性（学习循环的入口视图）
# ---------------------------------------------------------------------------
def plan(task: str, scene_id: str, *, llm: Callable | None = None) -> dict[str, Any]:
    """拆分一次任务并给每个子目标标注抓手状态。

    返回：
        {
          "task", "scene_id",
          "subgoals": [...],              # 原版口径的有序子目标
          "origin", "reason",             # 拆分来源（llm / rule_fallback）
          "subtasks": [                   # 带可达性的执行视图
              {"seq", "need", "status": reachable|gap|unreachable,
               "grab": entry|None, "gap_hint": [tables]|None, "why"}],
          "counts": {"reachable": n, "gap": n, "unreachable": n},
        }
    """
    dec = decompose(task, scene_id, llm=llm)
    subtasks: list[dict[str, Any]] = []
    counts = {"reachable": 0, "gap": 0, "unreachable": 0}
    for i, need in enumerate(dec["subgoals"], start=1):
        r = reachability(need, scene_id)
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        subtasks.append({"seq": i, "need": need, **r})
    return {**dec, "subtasks": subtasks, "counts": counts}
