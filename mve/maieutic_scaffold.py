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

你要产出两样东西：

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
                           "material_note": "", "origin": "failed", "reason": ""}
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
               material_note=str(data.get("material_note") or ""),
               origin="llm", reason="")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**data, "question": q}, ensure_ascii=False,
                                   indent=1), encoding="utf-8")
    except Exception:
        pass
    return out


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
