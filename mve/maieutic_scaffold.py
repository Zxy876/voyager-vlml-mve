#!/usr/bin/env python3
"""Maieutic 的「提问骨架」移植 —— 把一段材料拆成**必须回答的维度** + **预判的漂移**。

来源（本机 `~/ maieutic_fork/maieutic`，逐条对照，不是凭印象）
---------------------------------------------------------------
  `src/lib/opus/prompts/scaffolding.ts:6-186`  SCAFFOLDING_SYSTEM
    · 产出 spec_gate_dimensions：学生写代码前必须做出**承诺**的具体问题
      （"Handle edge cases" FAILS / "空字符串时返回什么？" PASSES）
    · 产出 expected_divergences：drift / revision / bug 三类偏差模式
    · 硬要求：维度数**与复杂度成比例**（简单 2-3、复杂 5-7，不许固定条数）；
      每条必须针对这道题；材料太模糊时给 prompt_quality_note，不拒绝。
  `src/lib/opus/prompts/intent-diff.ts:10-45`  三类定义（本模块沿用同一份口径）
    drift    = 做的比承诺少 / 承诺过的东西没做
    revision = 换了一种仍然满足承诺的做法（**不是**错误）
    bug      = 按承诺做了但机械地失败
  `src/lib/opus/prompts/phase2-chat.ts:20-34`  追问模式：不给答案，只给反问/诊断手法

MVE 里的落点（同构映射，不新造概念）
----------------------------------
    原版概念                     MVE 对应
    学生写的 spec          →    人导入的那句话（材料）
    spec_gate_dimensions   →    课程要逐个回答的**维度**（= 学习子目标）
    expected_divergences   →    执行层的「别这么答」清单 + critic 的判据
    drift 判定             →    critic：答案少了某个维度 = drift = 这轮没学会

为什么不直接塞进 DECOMPOSE_SYSTEM
---------------------------------
原版 Voyager 的 decompose 出的是**动宾格式的操作步骤**（"Get X" / "Rank by Y"），
那是为 `reachability()` 的词面匹配服务的（拿它去撞表名/条目名）。知识型问题
没有表可撞，用动宾格式只会拆出 "Get definition of X" 这种空壳（实测就 1 条）。
所以：**数据/库场景照旧用原版 decompose，本机/知识型场景用 Maieutic 维度**。
两条路都在 task_decompose.plan 里汇合，执行层/critic 拿到的形状是一样的。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

CKPT = Path(__file__).resolve().parent / "ckpt_maieutic"

# 照 scaffolding.ts 的 SCAFFOLDING_SYSTEM 重写：把「CS1 代码题」换成
# 「领域问题/材料」，其余骨架（三条硬要求、drift 三分类、模糊材料不拒绝）原样保留。
SCAFFOLD_SYSTEM = """你是一位擅长「产婆术」（Maieutic）的课程规划助手。人会给你一句**想弄清楚的问题**
（材料可能很粗糙），你的任务不是回答它，而是产出一份**追问骨架**，让一个只会取数/
检索的助手能一个问题一个问题地把这块知识补齐。

你要产出三样东西：

0. milestone —— 这整份骨架本身就是一个**里程碑**：一个能被命名、能被判"达成了没有"
   的能力单元（同构课程里的一章/一个单元）。
   · name：这句短语，像教材里的单元标题（如"阿斯伯格与孤独症谱系的边界"），
     **不许**是"回答以下问题"这种空话，也不许把原问题原样抄一遍；
   · why：为什么把它算一个里程碑（答清它之后，能力上多了什么）。
   ⚠️ 里程碑**只在这里生成一次**（骨架生成时），运行时不再生成 —— 运行时做的
   只是把下面的 dimensions 一条条闭合。所以 name 要能撑住"以后复用它"这件事。

1. dimensions —— 要真正答清楚这句问题，**必须先做出承诺/必须回答**的具体问题。
   每条都是一个可以被回答、也可以被判对错的具体问题，不是一个笼统的标签。
   · 「处理边界情况」→ **不合格**
   · 「阿斯伯格与孤独症在语言发育上的区别是什么？」→ **合格**
   维度数量**与问题复杂度成比例**：一句话能答清的 2-3 条；需要多角度（定义/机制/
   区别/适用条件/常见误解）的 5-7 条。**不要固定条数**，凑数就是仪式感。

2. expected_drifts —— 这题最可能出现的偏差模式，分三类（定义不许改）：
   · drift    —— 答得比问题要求的少，或漏掉了上面某个维度（最常见）
   · revision —— 换了一种仍然成立的答法（**不是错误**，不要当缺点报）
   · bug      —— 想答对但事实上错了（编数字、张冠李戴、把相似概念混为一谈）
   每条必须**针对这道题**：「可能回答不完整」→ 不合格；
   「把阿斯伯格当成孤独症的一个'轻度版本'来答」→ 合格。

硬约束：
- 维度必须**可回答**：要么能靠检索/取数回答，要么能靠明确的定义与权威说法回答。
  如果一个维度连"答成什么样算对"都说不清，就不要出它。
- 不要造出材料里没有的具体数字/专名。需要外部事实时，维度应该写成
  「X 的官方诊断标准是怎么规定的？」，而不是预设答案。
- 如果材料太模糊（比如只有两个字），照样产出你能产出的骨架，并在
  material_note 里如实说明模糊在哪 —— **不要拒绝**。

只输出一个 JSON 对象，不要前言、不要 markdown 代码围栏。Schema：
{
  "milestone": {"name": "<里程碑名，一句短语>", "why": "<为什么它是一个里程碑>"},
  "dimensions": [
    {"id": "<snake_case 短标识>", "question": "<具体问题，一句话>",
     "why": "<为什么答清这句问题必须先回答它>"}
  ],
  "expected_drifts": [
    {"category": "drift" | "revision" | "bug", "pattern": "<这道题上具体的偏差样子>"}
  ],
  "material_note": "<字符串或 null>"
}"""


def _default_llm(messages: list[dict[str, str]]) -> str:
    from llm_client import chat
    return chat(messages, temperature=0.3, max_tokens=1200)


def _cache_path(scene_id: str, question: str) -> Path:
    h = hashlib.md5(str(question or "").strip().encode("utf-8")).hexdigest()[:12]
    return CKPT / str(scene_id or "A") / f"{h}.json"


def _parse(text: str) -> dict[str, Any]:
    """容错解析（同构原版 fix_and_parse_json 的角色）：允许 ```json 围栏/多余前言。"""
    raw = str(text or "")
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.S)
    if m:
        raw = m.group(1)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _clean_milestone(data: dict[str, Any], question: str = "") -> dict[str, str]:
    """里程碑块（骨架**生成时**产出一次，运行时只读不生成）。

    老缓存 / 模型没给 → 用问题本身当名字（**不重建骨架**，去重名一样能用），
    判据不受影响：里程碑是否达成 = 维度有没有全闭合。
    """
    m = data.get("milestone")
    if isinstance(m, dict):
        name = str(m.get("name") or "").strip()
        if name:
            return {"name": name[:40],
                    "why": str(m.get("why") or "").strip()[:120]}
    if isinstance(m, str) and m.strip():
        return {"name": m.strip()[:40], "why": ""}
    return {"name": str(question or "").strip()[:40], "why": ""}


def _clean_dims(data: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for i, d in enumerate(data.get("dimensions") or []):
        if not isinstance(d, dict):
            continue
        q = str(d.get("question") or "").strip()
        if not q:
            continue
        out.append({"id": str(d.get("id") or f"dim_{i + 1}").strip(),
                    "question": q,
                    "why": str(d.get("why") or "").strip()})
    return out


def render_material(question: str, scene_id: str, *, extras: str = "") -> str:
    """把「材料」渲染给模型：人问的那句话 + 这个场景现在有什么（复用观测，不另造）。"""
    lines = [f"人想弄清楚的问题：{question}"]
    try:
        import task_decompose
        obs = task_decompose.scene_observation(question, scene_id)
        nodes = obs.get("nodes") or []
        ents = obs.get("entities") or {}
        if nodes:
            lines.append("这个场景**已经学过**的节点（别重复拆成维度）："
                         + "；".join(nodes[:20]))
        if ents:
            lines.append("已确认的实体：" +
                         "；".join(f"{k}: {', '.join(v[:6])}" for k, v in list(ents.items())[:5]))
    except Exception:
        pass
    try:
        import scene_profile
        prof = scene_profile.profile(scene_id)
        lines.append(f"数据源：{prof.get('kind')}（{prof.get('why') or '本机/知识型'}）")
    except Exception:
        pass
    if extras:
        lines.append(str(extras))
    return "\n".join(lines)


def scaffold(question: str, scene_id: str = "A", *, llm: Callable | None = None,
             use_cache: bool = True, extras: str = "") -> dict[str, Any]:
    """生成一份追问骨架。

    返回：
        {"dimensions": [{"id","question","why"}],
         "expected_drifts": [{"category","pattern"}],
         "material_note": str|"", "origin": "llm"|"cache"|"failed", "reason": ""}

    ⚠️ 失败时不编维度（origin="failed" + reason），调用方据此退回原版 decompose。
    """
    q = str(question or "").strip()
    out: dict[str, Any] = {"dimensions": [], "expected_drifts": [],
                           "milestone": {}, "material_note": "",
                           "origin": "failed", "reason": ""}
    if not q:
        out["reason"] = "空问题"
        return out
    path = _cache_path(scene_id, q)
    if use_cache and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            dims = _clean_dims(data)
            if dims:
                return {"dimensions": dims,
                        "expected_drifts": list(data.get("expected_drifts") or []),
                        "milestone": _clean_milestone(data, q),
                        "material_note": str(data.get("material_note") or ""),
                        "origin": "cache", "reason": ""}
        except Exception:
            pass
    messages = [{"role": "system", "content": SCAFFOLD_SYSTEM},
                {"role": "user", "content": render_material(q, scene_id, extras=extras)
                 + "\n\n请产出追问骨架，只输出 JSON。"}]
    try:
        text = (llm or _default_llm)(messages)
    except Exception as e:
        out["reason"] = f"LLM 调用失败：{type(e).__name__}: {e}"
        return out
    data = _parse(text)
    dims = _clean_dims(data)
    if not dims:
        out["reason"] = "LLM 输出里没有可用维度（解析失败/空列表）"
        return out
    drifts = [d for d in (data.get("expected_drifts") or []) if isinstance(d, dict)]
    out.update(dimensions=dims, expected_drifts=drifts,
               milestone=_clean_milestone(data, q),
               material_note=str(data.get("material_note") or ""),
               origin="llm", reason="")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**data, "question": q}, ensure_ascii=False,
                                   indent=1), encoding="utf-8")
    except Exception:
        pass
    return out


def milestone_state(scene_id: str, question: str,
                    learned_ids: list[str] | set[str], *,
                    scaffold_result: dict[str, Any] | None = None) -> dict[str, Any]:
    """里程碑达成了没有 —— **判定只读清单，不重新生成骨架**。

    口径（照 Maieutic spec-examiner 的闭合定义，`maieutic_gate` 已在用的同一条）：
      · 达成 = `gaps_still_open` 为空 = 骨架里的维度**全部闭合**；
      · 少一条就不算达成（不作废已闭合的部分 —— 它们各自已经是技能单位）。

    用户 2026-10-20 定的语义：里程碑骨架**每次跑生成一次**，运行时拆出来的
    子任务只是"闭合骨架的追问"。所以这里不调 LLM、不碰缓存，纯粹记账。
    """
    res = scaffold_result
    if res is None or not (res.get("dimensions") or []):
        res = scaffold(str(question or ""), str(scene_id or "A"))
    dims = [d for d in (res.get("dimensions") or [])
            if str(d.get("id") or "").strip()]
    total = len(dims)
    got = {str(x) for x in (learned_ids or [])}
    missing = [str(d.get("id")) for d in dims if str(d.get("id")) not in got]
    ms = res.get("milestone") or {}
    return {
        "name": str(ms.get("name") or "").strip() or str(question or "")[:40],
        "why": str(ms.get("why") or "").strip(),
        "total": total,
        "closed": total - len(missing),
        "missing": missing,
        # 0 个维度的骨架不构成里程碑（没有可闭合的东西就别报"达成"）
        "achieved": bool(total) and not missing,
    }


def drift_notes(scaffold_result: dict[str, Any], limit: int = 4) -> list[str]:
    """把预判偏差渲染成执行层能看懂的「别这么答」清单（只取 drift/bug）。"""
    out: list[str] = []
    for d in (scaffold_result.get("expected_drifts") or []):
        cat = str(d.get("category") or "").lower()
        pat = str(d.get("pattern") or "").strip()
        if not pat or cat == "revision":
            continue
        if cat in ("drift", "bug"):
            out.append(pat)
        if len(out) >= limit:
            break
    return out


def load_progress(scene_id: str, question: str) -> set[str]:
    """读这道题的维度学习进度（已答到的维度 id 集）。

    为什么需要：原版子目标制（voyager.py:403-421）里 `while progress <
    len(sub_goals)` 是**断点续学** —— 重试时已完成的子目标不重跑。
    进度就存在骨架缓存同一个文件里（同一把钥匙），不另开存储。
    """
    path = _cache_path(scene_id, question)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pr = data.get("progress") or {}
        return {str(x) for x in (pr.get("learned") or [])}
    except Exception:
        return set()


def save_progress(scene_id: str, question: str, learned_ids: list[str]) -> None:
    """写维度学习进度（断点续学用；失败静默 —— 进度丢了顶多重学）。"""
    path = _cache_path(scene_id, question)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    data["progress"] = {"learned": [str(x) for x in learned_ids],
                        "updated": __import__("time").time()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    except Exception:
        pass


def load_progress_codes(scene_id: str, question: str) -> dict[str, dict[str, str]]:
    """读断点续学里**上次跑通的代码**（维度 id → {"question","code"}）。

    为什么必须存代码：原版技能 = 一次成功 rollout 的 `program_code`。
    续学时这一轮**不重跑**（断点续学的本意），于是没有任何新代码 ——
    2026-10-20 实测：7/7 全闭合、里程碑达成，技能却入库 0 条。
    把上次跑通的代码留下来，续学时才复用得回那次 rollout 的成果。
    """
    path = _cache_path(scene_id, question)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pr = data.get("progress") or {}
        out: dict[str, dict[str, str]] = {}
        for c in (pr.get("codes") or []):
            if isinstance(c, dict) and str(c.get("id") or ""):
                out[str(c["id"])] = {"question": str(c.get("question") or ""),
                                     "code": str(c.get("code") or ""),
                                     "answer": str(c.get("answer") or "")}
        return out
    except Exception:
        return {}


def save_progress_codes(scene_id: str, question: str,
                        codes: list[dict[str, Any]]) -> None:
    """把这一轮跑通的代码写进进度（与已闭合维度 id 同存一份）。"""
    if not codes:
        return
    path = _cache_path(scene_id, question)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    pr = data.get("progress") or {}
    merged: dict[str, dict[str, str]] = {}
    for c in (pr.get("codes") or []):
        if isinstance(c, dict) and str(c.get("id") or ""):
            merged[str(c["id"])] = {"question": str(c.get("question") or ""),
                                    "code": str(c.get("code") or ""),
                                    "answer": str(c.get("answer") or "")}
    for c in codes:                       # 本轮新跑的覆盖旧的
        cid = str(c.get("id") or "").strip()
        if cid and (str(c.get("code") or "").strip()
                    or str(c.get("answer") or "").strip()):
            merged[cid] = {"question": str(c.get("question") or "")[:200],
                           "code": str(c.get("code") or ""),
                           "answer": str(c.get("answer") or "")[:800]}
    pr["codes"] = [{"id": k, "question": v["question"], "code": v["code"],
                    "answer": v["answer"]} for k, v in merged.items()]
    data["progress"] = pr
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    except Exception:
        pass


def attach_dimensions(scene_id: str, topic_id: str,
                      dimensions: list[dict[str, Any]], *,
                      verified: bool = False, graph: Any = None,
                      bump: bool = True) -> list[str]:
    """把追问骨架的维度**挂进知识图谱**（这是"图谱靠不停学习长大"的那一步）。

    每个维度 = 一枚 kind="dimension" 的节点，边 `问题节点 -has_dimension-> 维度`。
    · 幂等：同一 (topic, dim_id) 重复学不重复建，只把证据数 +1；
      `bump=False` = 只是"补挂确认它在"（断点续学跳过重跑时用），**不重复
      加证据** —— 没有新证据就不许把证据数刷上去（2026-10-19）。
    · 学成功才 verified（verified=True），没学成功就别冒充掌握；
    · parent 找不到时挂到场景根上（边不许悬挂，add_edge 会直接丢弃）。

    返回新建的节点 id 列表（面板/日志据此显示 Δ 节点数）。
    """
    dims = [d for d in (dimensions or []) if isinstance(d, dict)
            and str(d.get("question") or "").strip()]
    if not dims:
        return []
    try:
        from knowledge_graph import KnowledgeGraph, Node, Edge
    except Exception:
        return []
    g = graph if graph is not None else KnowledgeGraph.load()
    try:
        g.ensure_scene_root(scene_id)
    except Exception:
        pass
    tid = str(topic_id or "").strip()
    parent = f"consume:{tid}" if tid.startswith("consume_") else f"topic:{tid}"
    if parent not in g.nodes:
        parent = f"scene:{scene_id}"
    added: list[str] = []
    for d in dims:
        label = str(d.get("question") or "").strip()
        slug = str(d.get("id") or "").strip() or \
            hashlib.md5(label.encode("utf-8")).hexdigest()[:8]
        did = f"dim:{tid or 'scene'}:{slug}"
        old = g.nodes.get(did)
        if old is not None:
            if verified:
                old.status = "verified"
                if bump:
                    old.evidence = max(int(old.evidence or 0), 1) + 1
            continue
        g.add_node(Node(id=did, kind="dimension", label=label[:90],
                        detail={"why": str(d.get("why") or ""),
                                "from": "maieutic_spec_gate",
                                "topic": tid},
                        root_scene=str(scene_id),
                        status=("verified" if verified else "unverified"),
                        evidence=1))
        if parent in g.nodes:
            g.add_edge(Edge(src=parent, dst=did, relation="has_dimension",
                            origin="observed", reason="Maieutic 追问骨架"))
        added.append(did)
    if graph is None and added:
        try:
            g.save()
        except Exception:
            pass
    return added
