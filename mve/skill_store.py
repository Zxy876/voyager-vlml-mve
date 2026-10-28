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
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

STORE = Path(__file__).resolve().parent / "skill_store.json"
# 技能向量（原版同构：Chroma vectordb，skill.py:43-47）。单独落盘、不进
# skill_store.json —— 面板 build_state / coach_log / 回填 prompt 都会携带技能
# dict，2048 维向量混进去就是第二个 110MB（2026-10-21 面板挂死同款）。
EMB_STORE = Path(__file__).resolve().parent / "skill_emb.json"

# 解析结果的进程内缓存（键=文件指纹）。作用见 _load_raw 的 docstring：
# 大库（110MB）下"一次 build_state 解析 5 遍"足以把整机拖进 swap。
_RAW_CACHE: dict[str, Any] = {"sig": None, "data": None}
_RAW_LOCK = threading.Lock()

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


def _store_sig() -> tuple[int, int] | None:
    """文件指纹（mtime_ns, size）。拿不到就返回 None（按"没缓存"处理）。"""
    try:
        st = STORE.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _load_raw() -> dict[str, Any]:
    """读技能库（带**进程内解析缓存**，2026-10-21 面板挂死取证后补）。

    为什么必须缓存：黑客松实例的 skill_store.json 有 **110MB**（878 条 × 每条
    约 93KB，大头是每条 4096 维 embedding 原样存成 JSON）。面板一个 build_state
    里 all_skills() 被调 5 次 —— 没缓存就是 5 次全量 json.loads，实测
    build_state 13.7s、进程 RSS 涨到 **3.0G**，而这台 VPS 只有 3.6G，
    整机进 swap 之后接口永不返回（浏览器 "Unexpected end of JSON input"）。

    缓存键是文件指纹：文件没变就不重复解析；`_save()` 之后立刻把新对象写回
    缓存（写盘那一路的 data 就是权威副本，不用再解析一遍 110MB）。
    """
    sig = _store_sig()
    if sig is None:
        return _empty()
    with _RAW_LOCK:
        if _RAW_CACHE["sig"] == sig and _RAW_CACHE["data"] is not None:
            return _RAW_CACHE["data"]
    data = _load_raw_uncached()
    with _RAW_LOCK:
        _RAW_CACHE["sig"] = sig
        _RAW_CACHE["data"] = data
    return data


def _load_raw_uncached() -> dict[str, Any]:
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
    # 写盘这一路的 data 就是权威副本：直接塞进解析缓存（顺带把指纹换成
    # 写完之后的新指纹），省掉下一次对 110MB 文件的重新解析。
    with _RAW_LOCK:
        _RAW_CACHE["sig"] = _store_sig()
        _RAW_CACHE["data"] = data


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


# ---------------- 检索向量化（原版同构：embedding 相似度，skill.py:117-130） ----------------
# 原版技能检索 = 向量库对「技能描述」做 embedding 相似度（add 时 add_texts 入
# 向量库 skill.py:83-87，检索 similarity_search_with_score :122）。本库此前只有
# 词面 Jaccard（2-gram 重叠）——同义不同词的问法排不上：实测「九月份公司的
# 总支出」检不回 base_salary 配方、单维度技能把合成题答掉（3600/264000）。
# 这里对齐原版：embedding-3 向量化「名字+描述」，余弦相似盲取 top_k；
# API 不可用 / 无向量时回退词面 Jaccard（离线可用性不丢）。

def _embed_api(texts: list[str]) -> list[list[float]] | None:
    """智谱 OpenAI 兼容 /embeddings。任何失败返回 None（调用方回退词面）。"""
    try:
        import llm_client as _lc
        _lc.load_env()
        key = os.getenv("ZHIPU_API_KEY", "")
        if not key or not texts:
            return None
        base = os.getenv("ZHIPU_BASE_URL",
                         "https://open.bigmodel.cn/api/paas/v4")
        import httpx
        r = httpx.post(
            base.rstrip("/") + "/embeddings",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": os.getenv("ZHIPU_EMBED_MODEL", "embedding-3"),
                  "input": list(texts)},
            timeout=20.0,
        )
        r.raise_for_status()
        arr = r.json().get("data") or []
        if len(arr) != len(texts):
            return None
        return [it.get("embedding") for it in arr]
    except Exception:
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    try:
        num = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return num / (na * nb) if na and nb else 0.0
    except Exception:
        return 0.0


def _entry_embed_text(s: dict[str, Any]) -> str:
    """向量化对象 = 名字 + 描述（原版入向量库的是 skill_description，:83-87）。"""
    return f"{s.get('name', '')} {s.get('text', '')}"


_EMB_LOCK = threading.Lock()
_EMB_CACHE: dict[str, Any] = {"sig": None, "data": None}


def _load_embs() -> dict[str, list[float]]:
    try:
        st = EMB_STORE.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    with _EMB_LOCK:
        if sig is not None and _EMB_CACHE["sig"] == sig \
                and _EMB_CACHE["data"] is not None:
            return _EMB_CACHE["data"]
    data: dict[str, list[float]] = {}
    if EMB_STORE.exists():
        try:
            raw = json.loads(EMB_STORE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = {k: v for k, v in raw.items() if isinstance(v, list)}
        except Exception:
            data = {}
    with _EMB_LOCK:
        _EMB_CACHE["sig"] = sig
        _EMB_CACHE["data"] = data
    return data


def _save_embs(embs: dict[str, list[float]]) -> None:
    EMB_STORE.write_text(json.dumps(embs), encoding="utf-8")
    try:
        st = EMB_STORE.stat()
        sig: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    with _EMB_LOCK:
        _EMB_CACHE["sig"] = sig
        _EMB_CACHE["data"] = embs


def _ensure_embs(skills_by_key: dict[str, dict[str, Any]],
                 embs: dict[str, list[float]], cap: int = 64) -> bool:
    """给还没有向量的技能补 embedding（批量一次 API）。返回是否有新增。"""
    missing = [k for k, s in skills_by_key.items()
               if k not in embs and str(s.get("text") or "").strip()][:cap]
    if not missing:
        return False
    vecs = _embed_api([_entry_embed_text(skills_by_key[k]) for k in missing])
    if not vecs:
        return False
    added = False
    for k, v in zip(missing, vecs):
        if v:
            embs[k] = v
            added = True
    if added:
        _save_embs(embs)
    return added


# ---------------- 写入（去重覆盖） ----------------

def add(
    topic: str,
    name: str,
    text: str,
    *,
    source: str = "practice",
    blueprint: dict[str, Any] | None = None,
    code: str = "",
    source_scene: str = "default",
    exact_only: bool = False,
    r: float = -1.0,
) -> tuple[str, str]:
    """写一条技能。返回 (动作, key)，动作 ∈ added / rewritten / skipped。

    `source_scene` 是 1→n 泛化加的**场景归属**标记：技能全局共享，但每条记着自己
    来自哪个场景命名空间（见 mve/scenes.py）。跨场景复用某条技能时，它的
    `source_scene` 仍是原场景 —— 这样面板能显示「这条技能来自 Scene A」，也能
    统计「场景专属技能条数」（与知识图谱的 Δ 场景专属节点数口径一致）。

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
      - `consume`  —— 消费通道（答出来时把取数方法固化成配方）
      - `learn`    —— 学习闭环通道（里程碑闭合后固化这一轮**跑通的代码**，
                      见 `learn_orchestrate.learn_milestone_skills`；
                      本机/知识型场景没有 SQL，只能靠这条涨技能）
    区分的意义：**判错时我们不写自己的做法**（那已被证明是错的），
    但旁路学到的是裁判的做法、已被验证正确 —— 两条通道的可信度不同，
    复盘时要能分清一条技能到底是"我试出来的"还是"我学来的"。

    `exact_only`：**只认精确同名**，关掉「相似度合并」（2026-10-20 实测踩到）。
    里程碑维度的 id 本身就是精确主键，而相似度合并会把**不同维度**并进同一条：
    实测 `consume_010245c5::def` 这条的 key 还叫 `def`，blueprint 已经是
    `differences` —— 面板与 CLI 报的配方名和它真正干的事对不上，人没法核。
    这与课程侧那条教训同一个口径（去重只许精确，相似度阈值撞过两次）。
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
    if not hit and not exact_only:
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
        # ⭐ 组内择优（2026-10-28 第 2 步 B）：重写通道只在**新 r 更高**时才
        # 接受新 code——防止同维度后续尝试把已验证的更优版本改坏（天授诊断
        # 实测 214→68 漂移：没有"保留最优"机制时，改坏了也不回退）。
        # r 缺省 -1（老条目/未带 r 的调用方）→ 不拦，行为与旧版一致。
        _new_r = float(r)
        _old_r = float(s.get("r", -1.0))
        r_regression = (_new_r >= 0 and _old_r >= 0 and _new_r < _old_r)
        if similarity(text, s.get("text", "")) >= SKIP_THRESHOLD and not stale_code:
            data["skipped"] += 1
            _save(data)
            return "skipped", best_key
        if stale_code and r_regression:
            # 新代码更差：保留旧 code 与旧 r，其余文本字段照常更新
            data["skipped"] += 1
            s["text"] = text
            s["updated"] = now
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
        # 场景归属：已有则保持原场景（技能属于它学到的那个场景），否则按本次标注
        s.setdefault("source_scene", str(source_scene or "default"))
        if blueprint:
            s["blueprint"] = blueprint
        if code and not r_regression:
            s["code"] = code
        if _new_r >= 0 and (_old_r < 0 or _new_r >= _old_r):
            s["r"] = _new_r          # r 只升不降（择优语义）
        data["rewrites"] += 1
        _save(data)
        return "rewritten", best_key

    data["skills"][key] = {
        "topic": topic, "name": str(name or ""), "text": text,
        "hits": 0, "ok": 0, "created": now, "updated": now,
        "versions": 1, "history": [],
        "source": str(source or "practice"),
        "source_scene": str(source_scene or "default"),
        "blueprint": dict(blueprint or {}),
        "code": str(code or ""),
        **({"r": float(r)} if r >= 0 else {}),
    }
    data["added"] += 1
    _save(data)
    return "added", key


# ---------------- 检索（top_k） ----------------

def retrieve(
    topic: str, query: str, top_k: int = RETRIEVAL_TOP_K, *,
    scene_id: str = "",
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
    # 场景隔离（2026-10-19 用户要求「学习时场景隔离，只学习当前场景」）：
    # 给了 scene_id 就只取「本场景 + default」的配方 —— 与面板技能卡的既有口径
    # 同一条（技能是跨场景可复用资产，但**复用范围**是本场景 + 通用）。
    # 不传 scene_id 仍按全局取（老调用方/测试行为不变）。
    _sid = str(scene_id or "").strip()
    allowed = {_sid, "default", ""} if _sid else set()
    # 原版同构：向量化检索（skill.py:117-130）。旧条目缺向量先懒补一批；
    # 查询向量化失败或库里完全没有向量 → 整体回退词面 Jaccard。
    embs = _load_embs()
    try:
        _ensure_embs(data["skills"], embs)
    except Exception:
        pass
    qvec: list[float] | None = None
    if embs:
        _qv = _embed_api([str(query or "")])
        qvec = (_qv or [None])[0] if _qv else None
    _vec_mode = bool(qvec) and bool(embs)
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for key, s in data["skills"].items():
        if _sid and str(s.get("source_scene") or "default") not in allowed:
            continue
        if _vec_mode and key in embs:
            # 原版 chroma：相似度盲取 top_k（没有题目/词面门槛，:122-130）；
            # 「不是同一个量」由回填层规则 0 与守卫拦，不在检索层硬卡。
            # ⭐ r 进索引（2026-10-28 第 2 步 A）：向量模式补质量项——旧版这里
            # score = sim 纯语义，质量项 0.6*(ok/hits) 只在词面回退路径生效，
            # 假技能（JOIN 乘爆 75120）与真技能在向量空间几乎无法区分。
            # r 是入库时验证沉淀的标量（reward.compute_r），语义上与词面路径
            # 的 ok/hits 同类：都是「这条经验被验证过多可信」。
            sim = _cosine(qvec, embs[key])
            score = sim + 0.3 * max(float(s.get("r", -1.0)), 0.0)
        else:
            sim = similarity(query, f"{s.get('name','')} {s.get('text','')}")
            same_topic = s.get("topic") == topic
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
        if not (_vec_mode and key in embs) and s.get("topic") != topic:
            if cross >= CROSS_TOPIC_MAX:  # 门槛 2（仅词面回退路径）
                continue
            cross += 1
        if len(out) >= max(1, top_k):
            break
        item = dict(s)
        item.pop("emb", None)             # 向量不外流（prompt/日志/面板）
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
    """注入后这一轮判对了 → ok +1。有效性低的技能会在检索里自然沉底。

    ⚠️ 不变量：**ok ≤ hits**（面板/检索都把 `ok/hits` 当"有效率"，
    dashboard.py:1376、`retrieve()` 的 0.6 权重）。旧实现只加 ok 不管 hits，
    而 hits 只在 mark_used（检索注入）时加 —— 一旦出现"没走检索注入却判成功"
    的路径（复用配方重放、面板 boost 按钮），分子就跑到分母前面，面板出现
    「有效率 150% / 300%」（2026-10-20 截图实测）。这里兜底：ok 涨时若
    hits 落后，把这次算作**一次实际使用**把 hits 补上（它确实被用到了）。
    """
    if not keys or not ok:
        return
    data = _load_raw()
    touched = False
    for k in keys:
        if k in data["skills"]:
            s = data["skills"][k]
            s["ok"] = int(s.get("ok") or 0) + 1
            s["hits"] = max(int(s.get("hits") or 0), s["ok"])
            touched = True
    if touched:
        _save(data)


# ---------------- 1→n 泛化 · 溯源卡片三操作（任务173） ----------------

def boost_skill(key: str) -> bool:
    """溯源卡片操作①：标记为有效（复用提升）。

    把该技能的命中成功数 +1，使其在检索里上浮（「这条技能确实好用」）。
    返回是否命中到技能。
    """
    data = _load_raw()
    if key not in data["skills"]:
        return False
    s = data["skills"][key]
    s["ok"] = int(s.get("ok") or 0) + 1
    # 同 mark_result 的不变量：ok ≤ hits。旧写法是「hits==0 就补 1」，
    # 连点两次「标记有效」就变成 ok=2/hits=1 → 有效率 200%（同一处病根）。
    s["hits"] = max(int(s.get("hits") or 0), s["ok"])
    _save(data)
    return True


def demote_skill(key: str) -> bool:
    """溯源卡片操作②：标记为失效（降权）。

    把命中成功数清零（保留技能本体，但下次检索自然沉底，视为「这条不再可信」）。
    返回是否命中到技能。
    """
    data = _load_raw()
    if key not in data["skills"]:
        return False
    data["skills"][key]["ok"] = 0
    _save(data)
    return True


def delete_skill(key: str) -> bool:
    """溯源卡片操作③：从技能库删除该条技能（本体移除，不可恢复）。

    返回是否真的删到了。删除前不连带删图谱/能力项（那些是共享资产，按场景归属）。
    """
    data = _load_raw()
    if key not in data["skills"]:
        return False
    del data["skills"][key]
    data["writes"] = int(data.get("writes") or 0) + 1
    _save(data)
    # 向量一起清（EMB_STORE 以 key 为主键，孤儿向量无害但会白占检索比较）
    embs = _load_embs()
    if key in embs:
        embs.pop(key, None)
        _save_embs(embs)
    return True


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


def by_scene(scene_id: str) -> list[dict[str, Any]]:
    """返回归属某场景的技能（缺 source_scene 字段的旧条目按 'default' 计）。

    用于面板统计「场景专属技能条数」，与知识图谱的 Δ 场景专属节点数口径一致。
    注意：跨场景被 B 复用的 A 技能仍归属 A，不会计入 B。
    """
    sid = str(scene_id or "default")
    out = []
    for k, s in _load_raw()["skills"].items():
        if str(s.get("source_scene", "default")) == sid:
            out.append(dict(s, key=k))
    return out


def scene_skill_count(scene_id: str) -> int:
    """某场景专属技能条数（不含跨场景复用的外来技能）。"""
    return len(by_scene(scene_id))


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
    if EMB_STORE.exists():
        EMB_STORE.unlink()
    print(f"已清空技能库：{STORE}（含向量 {EMB_STORE.name}）")
