#!/usr/bin/env python3
"""MVE 的开放式出题器（移植自猫娘伴学的 tutor_llm_agent_question_generate）。

出题逻辑（移植，不 import 伴学运行时，跨进程只走 HTTP 桥）
----------------------------------------------------------
1. 选题由 `topic_graph.TopicGraph.select_next` 负责（前置门控 + 薄弱优先 + 难度梯度）。
2. 本题：拿选中的知识点（伴学 build_vlml_seed 的 29 个知识点之一）的
   `examples`（prompt=问法 / answer_outline=拿表回答步骤）、`typical_misconceptions`
   （实测踩出来的坑）、`skills`，组装成出题提示。
3. **题型轮换**（移植 question_type_mapping.select_question_style）：同一知识点多次
   练习时轮换 declared 的 question_types，且**明确告诉模型"不要和上次的题一样"**
   —— 这正是解决"重复出一道题"的关键。
4. 走 HTTP LLM：默认 MVE 自有智谱（llm_client）；env `MVE_QUIZ_BASE_URL` 一拨就
   切到猫娘 OpenAI 兼容桥（前序画死的"跨进程只走 HTTP 桥，不 fork"）。

输出归一化（移植 _normalize_question 的形状）
-------------------------------------------
{question, answer, accepted_answers, key_points, solution_steps,
 question_type, math_equivalence_engine, difficulty, topic, screen_type}
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from topic_graph import MasteryStore, TopicGraph

HERE = Path(__file__).resolve().parent

# 可填题型 → 机器题型（移植 question_type_mapping，最小可用版）
_STYLE_TO_MACHINE = {
    "计算": "math_exact", "判读": "short_answer", "概念": "short_answer",
    "口径": "short_answer", "跨表": "math_reasoning", "对比": "short_answer",
}
_MACHINE_TYPES = ("short_answer", "math_exact", "math_reasoning")
_DEFAULT_MACHINE = "short_answer"


def _clamp_int(v: Any, lo: int = 1, hi: int = 5, default: int = 3) -> int:
    try:
        x = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, x))


def _as_str(v: Any, fallback: str = "") -> str:
    return "" if v is None else str(v).strip()


def _as_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _machine_type_of(topic: dict[str, Any], attempt: int) -> tuple[str, str]:
    """题型轮换：首试取第一个声明题型，之后在声明题型间轮换；重试避开上一题。"""
    styles = [_as_str(s) for s in _as_list(topic.get("question_types")) if _as_str(s)]
    if not styles:
        return _DEFAULT_MACHINE, ""
    machine = _STYLE_TO_MACHINE.get(styles[attempt % len(styles)].lower(), _DEFAULT_MACHINE)
    return machine, styles[attempt % len(styles)]


# ---------------------------------------------------------------------------
# LLM 调用（默认智谱；env 切猫娘桥）
# ---------------------------------------------------------------------------
def _quiz_http(messages: list[dict[str, str]], *, temperature: float = 0.6,
               max_tokens: int = 1600, timeout: float = 90.0) -> str:
    import httpx

    base = os.getenv("MVE_QUIZ_BASE_URL", "").strip()
    if base:
        # 猫娘桥：OpenAI 兼容 /v1/chat/completions（前序边界：HTTP 不 fork）
        key = os.getenv("MVE_QUIZ_API_KEY", "")
        model = os.getenv("MVE_QUIZ_MODEL", "glm-4-flash")
        url = base.rstrip("/") + "/chat/completions"
    else:
        # MVE 自有智谱
        key = os.getenv("ZHIPU_API_KEY", "")
        base_u = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
        model = os.getenv("ZHIPU_MODEL", "glm-4-flash")
        url = base_u.rstrip("/") + "/chat/completions"

    if not key:
        raise RuntimeError("缺少 LLM 密钥（MVE_QUIZ_API_KEY 或 ZHIPU_API_KEY）")

    last = ""
    for _ in range(3):
        try:
            r = httpx.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages,
                      "temperature": temperature, "max_tokens": max_tokens},
                timeout=timeout,
            )
            if r.status_code >= 500 or r.status_code == 429:
                last = f"HTTP {r.status_code}: {r.text[:120]}"
            else:
                r.raise_for_status()
                data = r.json()
                return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        except httpx.HTTPError as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(1.5)
    raise RuntimeError(f"出题 LLM 调用失败：{last}")


def _quiz_json(messages: list[dict[str, str]], **kw) -> dict:
    text = _quiz_http(messages, **kw)
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text
    s, e = raw.find("{"), raw.rfind("}")
    if s < 0 or e <= s:
        return {}
    try:
        return json.loads(raw[s:e + 1])
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# 当前库真实实体（锚点，跟着库走；开源题必须围绕真实存在的实体，否则不可答）
# ---------------------------------------------------------------------------
def _current_entities() -> dict[str, str]:
    try:
        import anchor
        return anchor.get()
    except Exception:
        return {"series": "", "team": "", "map": ""}


def _entity_block() -> str:
    e = _current_entities()
    s, t, m = e.get("series", ""), e.get("team", ""), e.get("map", "")
    if not (s and t):
        return ""
    return (
        f"\n【当前库真实实体——出题与参考答案必须围绕这些，不要编造其他队/图】\n"
        f"系列赛={s}｜队伍={t}" + (f"｜代表图={m}" if m else "")
    )


# ---------------------------------------------------------------------------
# 提示组装
# ---------------------------------------------------------------------------
def recall_context(task_text: str, limit: int = 5) -> list[str]:
    """Task 3：从 mem0 长期记忆召回与本题相关的经验（带记忆系统消费）。

    优雅降级：mem0 未装 / 未 seed / 连不上 → 返回空列表，不阻断出题。
    记忆内容 = 4 条跨域规划原则 + 失败纠正经验（见 mem0_backend）。
    """
    try:
        import mem0_backend
        return mem0_backend.recall(task_text, limit=limit)
    except Exception:
        return []


def _build_messages(topic: dict[str, Any], *, attempt: int, last_question: str = "",
                    memories: list[str] | None = None,
                    scene_text: str = "") -> list[dict[str, str]]:
    name = _as_str(topic.get("name"), topic.get("id", ""))
    chapter = _as_str(topic.get("chapter"))
    skills = _as_list(topic.get("skills"))
    examples = _as_list(topic.get("examples"))
    mis = _as_list(topic.get("typical_misconceptions"))

    ex_block = ""
    if examples:
        lines = []
        for ex in examples[:2]:
            if isinstance(ex, dict):
                p = _as_str(ex.get("prompt"))
                ao = _as_list(ex.get("answer_outline"))
                if p:
                    lines.append(f"- 问法示例：「{p}」")
                if ao:
                    lines.append(f"  拿表回答示意：{' → '.join(_as_str(x) for x in ao[:3])}")
        ex_block = "\n".join(lines)

    mis_block = "\n".join(f"- {_as_str(x)}" for x in mis[:4]) if mis else "（无特别记录）"
    skill_block = "\n".join(f"- {_as_str(s)}" for s in skills) if skills else "（无）"

    machine_type, style = _machine_type_of(topic, attempt)
    style_hint = f"本次题型风格：{style}（机器题型 {machine_type}）" if style else f"机器题型：{machine_type}"

    repeat_guard = ""
    if last_question:
        repeat_guard = (
            f"\n⚠️ 上次练过的题是：「{last_question}」\n"
            "请务必**换一个角度 / 换一张地图 / 换一个实体**出题，不要和上次雷同。"
        )

    system = (
        "你是 Valorant 电竞数据分析的陪练教练。给定【知识点】，出【一道】练习题，"
        "让学生用 VLML 的数据（表 agg_* / rounds / base_events 等）去回答。"
        "你必须返回一个 JSON 对象，字段：\n"
        "  question: 题目（自然语言，说清口径，例如'哪张图''哪个队'）\n"
        "  answer: 参考答案（简洁）\n"
        "  accepted_answers: 可接受答案列表（含等价表述/关键数值）\n"
        "  key_points: 评分要点列表（判分用）\n"
        "  solution_steps: 解题步骤列表（先确认粒度→选表→写 SQL/调工具→报数）\n"
        "  question_type: 机器题型（short_answer / math_exact / math_reasoning）\n"
        "  difficulty: 1-5 整数\n"
        "不要编造不存在的表或列；口径必须可验证。"
    )
    scene_line = ""
    if scene_text:
        scene_line = f"\n{scene_text}\n"
    user = (
        f"【知识点】{name}（章节：{chapter}）\n"
        f"{style_hint}\n"
        f"\n【技能要求】\n{skill_block}\n"
        f"\n【问法与拿表回答示意（参考，不要照抄原题）】\n{ex_block}\n"
        f"\n【实测常见误区（出题时埋下陷阱或明确警示）】\n{mis_block}\n"
        f"\n难度建议：{topic.get('difficulty')}"
        f"{scene_line}"
        f"{_entity_block()}"
        f"{repeat_guard}"
        + (_memory_block(memories) if memories else "")
        + "\n请返回 JSON。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# 归一化（移植 _normalize_question 的形状）
# ---------------------------------------------------------------------------
def _normalize(raw: dict[str, Any], topic: dict[str, Any], *, attempt: int) -> dict[str, Any]:
    question = _as_str(raw.get("question")) or _as_str(raw.get("prompt"))
    if not question:
        raise ValueError("模型未返回 question")
    machine_type, style = _machine_type_of(topic, attempt)
    answer = _as_str(raw.get("answer")) or _as_str(raw.get("reference_answer"))
    accepted = [_as_str(x) for x in _as_list(raw.get("accepted_answers")) if _as_str(x)]
    if answer and not accepted:
        accepted = [answer]
    key_points = [_as_str(x) for x in _as_list(raw.get("key_points")) if _as_str(x)]
    steps = [_as_str(x) for x in _as_list(raw.get("solution_steps")) if _as_str(x)]
    mis = _as_list(topic.get("typical_misconceptions"))
    hint = "；".join(_as_str(x) for x in mis[:3]) if mis else ""
    return {
        "question": question,
        "answer": answer,
        "accepted_answers": accepted,
        "key_points": key_points,
        "solution_steps": steps,
        "hint": hint,
        "question_type": _as_str(raw.get("question_type")) or machine_type,
        "math_equivalence_engine": {"enabled": False},
        "difficulty": _clamp_int(raw.get("difficulty"), default=_clamp_int(topic.get("difficulty", 3) * 5, default=3)),
        "topic": topic.get("id"),
        "topic_name": _as_str(topic.get("name"), topic.get("id", "")),
        "screen_type": "open_quiz",
    }


def _memory_block(memories: list[str] | None) -> str:
    if not memories:
        return ""
    bullets = "\n".join(f"  - {m}" for m in memories[:6])
    return (
        f"\n\n【既往经验记忆（mem0 召回，出题时可参考其取数思路/避坑）】\n"
        f"{bullets}"
    )


def generate_for(topic_id: str, *, attempt: int = 0, last_question: str = "",
                 memories: bool = True, scene: bool = True) -> dict[str, Any]:
    """为某个知识点生成一道开放式题。失败抛异常，由调用方降级。

    memories=True 时，先按知识点文本从 mem0 召回既往经验注入出题 prompt
    （Task 3：技能消费带记忆系统——人消费路径也接记忆）。
    scene=True 时，注入「初始场景（数据世界）」块（Task 2：渐进披露——
    模型只能看到当前进度档允许看到的表/工具）。
    """
    graph = TopicGraph()
    topic = graph.topics.get(topic_id)
    if topic is None:
        raise ValueError(f"未知知识点 {topic_id}")
    mem = recall_context(_memory_query(topic)) if memories else []
    scene_text = ""
    if scene:
        try:
            import scene_context
            scene_text = scene_context.scene_injection(topic_id)
        except Exception:
            scene_text = ""
    messages = _build_messages(topic, attempt=attempt, last_question=last_question,
                               memories=mem, scene_text=scene_text)
    raw = _quiz_json(messages)
    if not raw:
        raise ValueError("出题 LLM 返回空/非 JSON")
    q = _normalize(raw, topic, attempt=attempt)
    q["memories_recalled"] = len(mem)
    return q


def _memory_query(topic: dict[str, Any]) -> str:
    """拼一条用于语义召回的任务文本（知识点名 + 技能要求）。"""
    name = _as_str(topic.get("name"), topic.get("id", ""))
    skills = "；".join(_as_str(s) for s in _as_list(topic.get("skills"))[:4])
    return f"{name}｜{skills}" if skills else name


def generate_next(*, scope: dict[str, Any] | None = None, explicit: str | None = None,
                  last_question: str = "", memories: bool = True,
                  scene: bool = True) -> dict[str, Any]:
    """选下一知识点并出题，一并返回选题依据。"""
    graph = TopicGraph()
    sel = graph.select_next(scope=scope, explicit=explicit)
    tid = sel["topic_id"]
    # attempt 数 = 该知识点已尝试次数（用于题型轮换）
    mrow = graph.mastery.get(tid)
    attempt = int((mrow or {}).get("attempts", 0))
    q = generate_for(tid, attempt=attempt, last_question=last_question,
                     memories=memories, scene=scene)
    q["selection"] = sel
    return q


def record_correction_on_failure(topic_id: str, *, user_answer: str = "",
                                 grade: dict[str, Any] | None = None) -> int:
    """Task 3：人消费路径的失败→纠正闭环。答错（score<0.6）时把本题的
    「常见误区 + 漏掉要点」写成 mem0 纠正记忆，下次出题/作答可召回。

    优雅降级：mem0 未装/未 seed → 返回 0，不阻断主流程。
    与 failure_correction_loop 的区别：那里记的是「模型自己写错的 SQL 异常」
    （取证闭环），这里记的是「人答错暴露的知识点误区」（人消费闭环）。
    """
    score = float((grade or {}).get("score", 1.0))
    if score >= 0.6:
        return 0
    try:
        import mem0_backend
    except Exception:
        return 0
    graph = TopicGraph()
    topic = graph.topics.get(topic_id) or {}
    mis = _as_list(topic.get("typical_misconceptions"))[:3]
    misses = _as_list((grade or {}).get("misses"))[:4]
    if not (mis or misses):
        return 0
    entries = [{
        "error": f"人答错知识点 {topic_id}（得分 {score:.2f}）："
                 + (_as_str(user_answer)[:60] or "未作答"),
        "fix": "本题关键：" + "；".join([*mis, *misses]),
        "condition": f"涉及 {topic_id} 的出题/作答",
        "verified": True,
    }]
    try:
        added = mem0_backend.add_correction(entries, force=False)
        return len(added)
    except Exception:
        return 0


def record_result(topic_id: str, *, score: float) -> dict[str, Any]:
    """记录一次作答得分（0-1），更新掌握度。"""
    graph = TopicGraph()
    return graph.mastery.update(topic_id, score=score)


# ---------------------------------------------------------------------------
# 判分（accepted_answers + key_points 语义判分，移植伴学的 accepted_answers 思路）
# ---------------------------------------------------------------------------
def grade_answer(question_payload: dict[str, Any], user_answer: str) -> dict[str, Any]:
    """用 LLM 判分：返回 {score(0-1), feedback, hits, misses}。

    对 math_exact 且 accepted_answers 含数值的题，先做数值容差快判（强信号，
    不污染失败→纠正闭环）；其余走语义判分。
    """
    q_text = _as_str(question_payload.get("question"))
    accepted = _as_list(question_payload.get("accepted_answers"))
    key_points = _as_list(question_payload.get("key_points"))
    ua = _as_str(user_answer)
    if not ua:
        return {"score": 0.0, "feedback": "未作答", "hits": [], "misses": key_points}

    # 数值快判（math_exact）
    if str(question_payload.get("question_type")) == "math_exact":
        nums = [n for a in accepted for n in _extract_numbers(a)]
        unum = _extract_numbers(ua)
        if nums and unum:
            if any(abs(n - u) <= max(0.5, abs(n) * 0.05) for n in nums for u in unum):
                return {"score": 1.0, "feedback": "数值正确", "hits": ["数值"], "misses": []}

    messages = [
        {"role": "system", "content": (
            "你是严格的阅卷人。根据【题目】【参考答案/可接受答案】【评分要点】评判"
            "【学生作答】。返回 JSON：{score: 0到1 的小数, feedback: 简短点评, "
            "hits: 命中的要点, misses: 漏掉/错的要点}。"
        )},
        {"role": "user", "content": (
            f"【题目】{q_text}\n"
            f"【可接受答案】{json.dumps(accepted, ensure_ascii=False)}\n"
            f"【评分要点】{json.dumps(key_points, ensure_ascii=False)}\n"
            f"【学生作答】{ua}\n"
            "请返回 JSON。"
        )},
    ]
    raw = _quiz_json(messages, temperature=0.2)
    try:
        score = float(raw.get("score", 0))
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))
    return {
        "score": round(score, 3),
        "feedback": _as_str(raw.get("feedback")) or ("通过" if score >= 0.6 else "未通过"),
        "hits": _as_list(raw.get("hits")),
        "misses": _as_list(raw.get("misses")),
    }


def _extract_numbers(text: str) -> list[float]:
    out = []
    for m in re.findall(r"-?\d+(?:\.\d+)?", text):
        try:
            out.append(float(m))
        except ValueError:
            pass
    return out


def attempt(topic_id: str, user_answer: str, *, attempt: int = 0,
            last_question: str = "") -> dict[str, Any]:
    """选/出一道题 → 判分 → 记录掌握度，一步到位。返回完整结果。"""
    q = generate_for(topic_id, attempt=attempt, last_question=last_question)
    g = grade_answer(q, user_answer)
    q["grade"] = g
    if topic_id:
        record_result(topic_id, score=g["score"])
    return q


if __name__ == "__main__":
    import sys
    tid = sys.argv[1] if len(sys.argv) > 1 else TopicGraph().select_next()["topic_id"]
    try:
        q = generate_for(tid, attempt=0)
        print(json.dumps(q, ensure_ascii=False, indent=2))
    except Exception as e:
        print("出题失败：", e)
