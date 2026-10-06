#!/usr/bin/env python3
"""Voyager 技能库持久化 —— 照原仓库 SkillManager 做「防膨胀」。

原仓库 voyager/agents/skill.py 里三条让技能库不炸掉的硬规矩：

  1. 技能不是 list.append，而是 `dict[program_name]` —— 同名技能 **覆盖**，不是新增：
       if program_name in self.skills:
           print(f"Skill {program_name} already exists. Rewriting!")
           self.vectordb._collection.delete(ids=[program_name])
     （SkillManager.add_new_skill）

  2. 检索只取 top_k：
       k = min(self.vectordb._collection.count(), retrieval_top_k)   # retrieval_top_k=5
       docs_and_scores = self.vectordb.similarity_search_with_score(query, k=k)
     技能库再大，进 prompt 的永远只有 5 条 —— 这是"库会涨但 prompt 不涨"的关键。

  3. 无用技能直接不入：
       if info["task"].startswith("Deposit useless items into the chest at"):
           return

本模块把这三条原样搬过来：
  1. add() 按 (topic, name) 去重，命中就 Rewriting（覆盖 + 归档旧版 + versions+1）
  2. retrieve() 只返回 top_k 条（同题优先 + 词面相关性 + 有效性加权）
  3. add() 会丢掉低价值经验（太短 / 与已有技能几乎重复 / 只是复述题目）

膨胀有没有真被拦住必须可观测 —— stats() 给出 writes（累计写入尝试）
与 skills（实际条目）的比值：v1 是 15 次写入 → 15 条（1.0，完全没拦住）。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

STORE = Path(__file__).resolve().parent / "skill_store.json"

# 照原仓库 retrieval_top_k=5：进 prompt 的经验条数上限
RETRIEVAL_TOP_K = 5

# 名字/内容相似度阈值（无向量库，用中英混排的词面 Jaccard 近似）
DUP_THRESHOLD = 0.62    # 超过 → 视为同一条技能 → 覆盖（Rewriting）
SKIP_THRESHOLD = 0.90   # 超过 → 内容几乎没变 → 连覆盖都不用，直接丢弃
MIN_LESSON_LEN = 12     # 照 "Deposit useless items" 特判：太短的经验没有复用价值
MAX_HISTORY = 3         # 旧版本最多留几条（归档，不进 prompt）

# 跨题经验注入 prompt 的两条门槛（不加就会被上一题的经验带偏）
CROSS_TOPIC_MIN_SIM = 0.12   # 与本题题干的词面相关度下限
CROSS_TOPIC_MAX = 2          # 跨题最多几条


# ---------------- 文件读写 ----------------

def _empty() -> dict[str, Any]:
    return {
        "version": 2,
        "skills": {},
        "writes": 0,      # 累计尝试写入次数
        "added": 0,       # 真正新建
        "rewrites": 0,    # 被覆盖（Rewriting）
        "skipped": 0,     # 被判为无用/重复而丢弃
    }


def _load_raw() -> dict[str, Any]:
    if not STORE.exists():
        return _empty()
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    # v1 是 {"memory": [str, ...]} —— 迁移成 v2，否则老的技能库会一直当新条目
    if isinstance(data, dict) and isinstance(data.get("memory"), list):
        return _migrate_v1(data["memory"])
    if not isinstance(data, dict) or not isinstance(data.get("skills"), dict):
        return _empty()
    for k, v in _empty().items():
        data.setdefault(k, v)
    return data


def _migrate_v1(memory: list[str]) -> dict[str, Any]:
    """v1 的 list[str] → v2 的 dict。老条目没有名字，取前 12 字当名字。"""
    out = _empty()
    now = time.time()
    for i, text in enumerate(memory):
        if not isinstance(text, str) or not text.strip():
            continue
        topic = ""
        body = text.strip()
        m = re.match(r"^\[([^\]]+)\]\s*(.*)$", body, re.S)
        if m:
            topic, body = m.group(1), m.group(2)
        name = re.sub(r"\s+", "", body)[:12] or f"skill{i}"
        out["skills"][_key(topic, name)] = {
            "topic": topic, "name": name, "text": body.strip(),
            "hits": 0, "ok": 0, "created": now, "updated": now,
            "versions": 1, "history": [],
        }
    out["writes"] = len(out["skills"])
    out["added"] = len(out["skills"])
    return out


def _save(data: dict[str, Any]) -> None:
    STORE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------- 名字归一化与相似度 ----------------

def norm_name(name: str) -> str:
    """技能名归一化：去空白与标点，照 program_name 当主键用。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(name or "")).lower()


def _key(topic: str, name: str) -> str:
    return f"{topic}::{norm_name(name)}"


def _tokens(s: str) -> set[str]:
    """中英混排分词：英文/数字按词，中文按 2-gram。"""
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(s or "").lower())
    toks: set[str] = set()
    for w in s.split():
        if re.fullmatch(r"[a-z0-9_]+", w):
            toks.add(w)
            continue
        if len(w) == 1:
            toks.add(w)
        for i in range(len(w) - 1):
            toks.add(w[i:i + 2])
    return toks


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------------- 写入（去重覆盖） ----------------

def add(
    topic: str,
    name: str,
    text: str,
    *,
    source: str = "practice",
    blueprint: dict[str, Any] | None = None,
    code: str = "",
) -> tuple[str, str]:
    """写一条技能。返回 (动作, key)，动作 ∈ added / rewritten / skipped。

    照 SkillManager.add_new_skill：同名 → Rewriting，不是 append。

    `code` 是**解法源码** —— 原版存的是 `program_code`（完整 JS 源码），
    检索时 `retrieve_skills` 取回来的也是 `skills[name]["code"]`，
    拼进 prompt 的同样是源码（`programs` property）。
    之前这里只有 `text`（一句自然语言经验），模型读了但不改行为：
    实测注入 2 条经验、轨迹仍是单工具、覆盖率掉到 30%。
    真正的差距就在这儿 —— **原版学的是代码，MVE 学的是话术**。

    `source` 区分技能是从哪条通道来的：
      - `practice` —— 做题通道（判对时固化自己的做法）
      - `bypass`   —— 旁路通道（观摩裁判的标准解法，见 `bypass_learn.py`）
    区分的意义：**判错时我们不写自己的做法**（那已被证明是错的），
    但旁路学到的是裁判的做法、已被验证正确 —— 两条通道的可信度不同，
    复盘时要能分清一条技能到底是"我试出来的"还是"我学来的"。
    """
    text = str(text or "").strip()
    # 照 "Deposit useless items" 特判：没内容的经验不入库
    if len(text) < MIN_LESSON_LEN or text in {"无", "None", "（无）"}:
        data = _load_raw()
        data["writes"] += 1
        data["skipped"] += 1
        _save(data)
        return "skipped", ""

    data = _load_raw()
    data["writes"] += 1
    key = _key(topic, name)
    now = time.time()

    # 同名主键命中 → 直接覆盖
    hit = data["skills"].get(key)
    best_key, best_sim = key, 1.0 if hit else 0.0
    if not hit:
        # LLM 每轮可能给不同名字 —— 照 curriculum 的 qa_cache：
        # similarity_search_with_score < 阈值 就算命中缓存，不能当新条目
        probe = f"{name} {text}"
        for k, s in data["skills"].items():
            if s.get("topic") != topic:
                continue
            sim = similarity(probe, f"{s.get('name','')} {s.get('text','')}")
            if sim > best_sim:
                best_key, best_sim = k, sim

    if best_sim >= DUP_THRESHOLD and best_key in data["skills"]:
        s = data["skills"][best_key]
        # 内容几乎没变 → 连覆盖都省了（这正是 v1 膨胀十几条的那类写入）
        #
        # **但源码变了就必须更新，哪怕文字没变**。实测踩过：percent 口径的
        # 源码从"单条 .num 路径"改成"num/denom 两条路径"之后，lesson 那句话
        # 一字未变 → 走 skipped 分支 → 技能库里一直是旧源码 → 模型照旧 dig
        # 到分子 3 就交上来（3 ≠ 50.0）。文字相同不代表内容相同。
        stale_code = bool(code) and code != str(s.get("code") or "")
        if similarity(text, s.get("text", "")) >= SKIP_THRESHOLD and not stale_code:
            data["skipped"] += 1
            _save(data)
            return "skipped", best_key
        hist = list(s.get("history") or [])
        hist.append(s.get("text", ""))
        s["history"] = hist[-MAX_HISTORY:]
        s["text"] = text
        s["name"] = str(name or s.get("name", ""))
        s["updated"] = now
        s["versions"] = int(s.get("versions") or 1) + 1
        s["source"] = str(source or s.get("source") or "practice")
        if blueprint:
            s["blueprint"] = blueprint
        if code:
            s["code"] = code
        data["rewrites"] += 1
        _save(data)
        return "rewritten", best_key

    data["skills"][key] = {
        "topic": topic, "name": str(name or ""), "text": text,
        "hits": 0, "ok": 0, "created": now, "updated": now,
        "versions": 1, "history": [],
        "source": str(source or "practice"),
        "blueprint": dict(blueprint or {}),
        "code": str(code or ""),
    }
    data["added"] += 1
    _save(data)
    return "added", key


# ---------------- 检索（top_k） ----------------

def retrieve(
    topic: str, query: str, top_k: int = RETRIEVAL_TOP_K
) -> list[dict[str, Any]]:
    """照 SkillManager.retrieve_skills：只取 top_k 条进 prompt。

    没有向量库，用「同题优先 + 词面相关 + 有效性」打分近似：
      score = 3.0(同题) + 2.5*sim(query, name+text) + 0.25*min(hits,6) + 0.6*(ok/hits)

    两条硬门槛（实测不加就会被跨题经验带偏）：
      1. **跨题经验必须真的相关**才注入。库小的时候 top_k 装不满，
         于是上一题的经验被硬塞进这一题的 prompt（"按地图维度下钻 query_sql"
         被套到 pattern 题上，Voyager 就跑偏了）。原仓库靠向量相似度天然
         挡住不相关技能，这里必须显式补一个相关度下限。
      2. 跨题最多 2 条。prompt 里该占主导的是本题经验。
    """
    data = _load_raw()
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for key, s in data["skills"].items():
        same_topic = s.get("topic") == topic
        sim = similarity(query, f"{s.get('name','')} {s.get('text','')}")
        if not same_topic and sim < CROSS_TOPIC_MIN_SIM:
            continue                      # 门槛 1：不相关的跨题经验不进 prompt
        score = 3.0 if same_topic else 0.0
        score += 2.5 * sim
        hits = int(s.get("hits") or 0)
        score += 0.25 * min(hits, 6)
        score += 0.6 * (int(s.get("ok") or 0) / max(hits, 1))
        scored.append((score, key, s))
    scored.sort(key=lambda x: -x[0])

    out: list[dict[str, Any]] = []
    cross = 0
    for score, key, s in scored:
        if s.get("topic") != topic:
            if cross >= CROSS_TOPIC_MAX:  # 门槛 2
                continue
            cross += 1
        if len(out) >= max(1, top_k):
            break
        item = dict(s)
        item["key"] = key
        item["score"] = round(score, 3)
        out.append(item)
    return out


def mark_used(keys: list[str]) -> None:
    """被检索注入了 prompt → hits +1。"""
    if not keys:
        return
    data = _load_raw()
    touched = False
    for k in keys:
        if k in data["skills"]:
            data["skills"][k]["hits"] = int(data["skills"][k].get("hits") or 0) + 1
            touched = True
    if touched:
        _save(data)


def mark_result(keys: list[str], ok: bool) -> None:
    """注入后这一轮判对了 → ok +1。有效性低的技能会在检索里自然沉底。"""
    if not keys or not ok:
        return
    data = _load_raw()
    touched = False
    for k in keys:
        if k in data["skills"]:
            data["skills"][k]["ok"] = int(data["skills"][k].get("ok") or 0) + 1
            touched = True
    if touched:
        _save(data)


# ---------------- 观测与兼容 ----------------

def stats() -> dict[str, Any]:
    """膨胀是否真被拦住，看 bloat = writes / skills。"""
    data = _load_raw()
    n = len(data["skills"])
    writes = int(data.get("writes") or 0)
    return {
        "skills": n,
        "writes": writes,
        "added": int(data.get("added") or 0),
        "rewrites": int(data.get("rewrites") or 0),
        "skipped": int(data.get("skipped") or 0),
        "bloat": round(writes / n, 2) if n else 0.0,
        "top_k": RETRIEVAL_TOP_K,
    }


def all_skills() -> list[dict[str, Any]]:
    data = _load_raw()
    return [dict(s, key=k) for k, s in data["skills"].items()]


def load() -> list[str]:
    """兼容旧调用方：返回全部技能文本（带 [topic] 前缀）。仅用于展示。"""
    return [f"[{s.get('topic','')}] {s.get('text','')}" for s in all_skills()]


def save(memory: list[str]) -> None:
    """兼容旧调用方。现在是 append 语义的直接写入，会绕过去重 —— 仅在迁移时用。"""
    for text in memory:
        topic, body = "", str(text)
        m = re.match(r"^\[([^\]]+)\]\s*(.*)$", body, re.S)
        if m:
            topic, body = m.group(1), m.group(2)
        add(topic, re.sub(r"\s+", "", body)[:12], body)


def reset() -> None:
    if STORE.exists():
        STORE.unlink()
    print(f"已清空技能库：{STORE}")
