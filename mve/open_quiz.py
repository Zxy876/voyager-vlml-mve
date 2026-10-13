#!/usr/bin/env python3
"""MVE 的开放式出题器（移植自猫娘伴学的 tutor_llm_agent_question_generate）。

出题逻辑（移植，不 import 伴学运行时，跨进程只走 HTTP 桥）
----------------------------------------------------------
1. 选题由 `topic_graph.TopicGraph.select_next` 负责（有序课程 + 完成门控 + 放弃机制）。
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
# 当前库真实 schema（生成参考 SQL 的唯一事实来源；跟着库走，换库自动更新）
# ---------------------------------------------------------------------------
def _active_db() -> Path:
    cfg = HERE / "db_config.json"   # db_config.json 与 open_quiz.py 同目录（mve/）
    try:
        obj = json.loads(Path(cfg).read_text(encoding="utf-8"))
        p = str(obj.get("db_path") or "")
        if p:
            # ⚠️ 相对路径按**仓库根**解析，不能原样返回（与 vlml_env /
            #    anchor / entity_catalog 同口径）：vlml_env 会把 cwd 切到
            #    vlml/，原样返回会在那儿被拼两遍（2026-10-13 服务器实测）。
            q = Path(p).expanduser()
            return q if q.is_absolute() else (HERE.parent / q).resolve()
    except Exception:
        pass
    # 兜底走唯一定义处（当前是 rib.gg，用户 2026-10-13 定为默认库）——
    # 不再各自写 vlml_events.duckdb 字面量。
    from db_switch import DEFAULT_DB
    return DEFAULT_DB


_SCHEMA_CACHE: str | None = None


def _schema_block() -> str:
    """当前库所有真实表与列（连接库实查；照抄伴学 entity_catalog 的"实查不写死"原则）。"""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is not None:
        return _SCHEMA_CACHE
    try:
        import duckdb
        con = duckdb.connect(str(_active_db()), read_only=True)
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY 1").fetchall()]
        lines = []
        for t in tables:
            try:
                cols = [r[0] for r in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name=? ORDER BY 1", [t]).fetchall()]
            except Exception:
                cols = []
            lines.append(f"- {t}: {', '.join(cols)}")
        con.close()
        block = "\n".join(lines)
    except Exception as e:
        block = f"（无法读取库 schema：{e}）"
    _SCHEMA_CACHE = block
    return block


_SCHEMA_CAVEATS = (
    "【当前库真实 schema 与常见坑——reference_sql 必须严格遵守】\n"
    "- 没有名为 `teams` 的表。队伍名散落在：games(team1_name, team2_name, "
    "winning_team_name)、rounds(winning_team_name, losing_team_name)、"
    "series(team1_name, team2_name)、各 agg_* 表用 team_name / opponent_team_name / "
    "winning_team_name / losing_team_name。\n"
    "- 地图列一律叫 `map_name`（不是 map）。\n"
    "- `agent_roles` 是静态维表，只有 agent_name / agent_role / is_controller / "
    "is_duelist / is_initiator / is_sentinel，没有 team / map / player 列——"
    "只能查\"某特工属于什么位置\"，不能按队/图/选手过滤。\n"
    "- 要查\"某队在某图用了哪些某位置（Initiator/Sentinel/Duelist/Controller）特工\"，"
    "用 **agg_player_round_stats**（含 team_name, map_name, agent_name, agent_role）。"
    "注意 agg_player_game_stats 只有 agent_name、**没有 agent_role 列**，别用错表。\n"
    "- 选手名：agg_player_* 与 ext_player_* 用 player_name；base_events 用 "
    "actor_player_name / target_player_name（没有 player_name 列）。\n"
    "- base_events 的队伍列是 **actor_team_name / target_team_name**（不是 team_name）。\n"
    "- 多表 JOIN 时，所有**重复列名必须加表前缀**（如 base_events.weapon_name），"
    "否则报 Ambiguous reference。\n"
    "- `agg_team_round_stats` **没有 series_id 列**（只有 game_id / round_id）；"
    "要按系列赛过滤，请先 `JOIN games ON game_id` 拿 series_id，或改用 "
    "agg_team_game_stats（含 series_id）。\n"
    "- `agg_post_plant_stats` 的队伍列是 planting_team / defending_team / "
    "winning_team_name（没有 team_name）。\n"
    "- 类型别混：series_id 是整数（如 1058），tournament_id 是字符串"
    "（如 'valorant-champions-2026'）。\"系列赛\"过滤用 series_id，不要拿 1058 去查 "
    "tournament_id。\n"
    "- reference_sql 仅一条 SELECT，表名与列名必须来自下方【真实 schema】；"
    "跑不通的题会被直接淘汰，请务必先核对列名再写。"
)


def _validate_sql(sql: str) -> tuple[bool, str]:
    """直接对当前库跑一次 SELECT，返回 (可执行且返回非空行?, 错误信息)。
    这是生成题参考 SQL 的硬闸门：跑不通 → 该题没有客观真值（VLML 判不了成功）。"""
    s = str(sql or "").strip()
    if not s.lower().lstrip().startswith("select"):
        return False, "reference_sql 不是以 SELECT 开头"
    try:
        import duckdb
        con = duckdb.connect(str(_active_db()), read_only=True)
        try:
            rows = con.execute(s).fetchall()
        finally:
            con.close()
        if not rows:
            return False, "reference_sql 执行返回 0 行（本题在当前库无解）"
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


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
                    scene_text: str = "", sql_error: str = "") -> list[dict[str, str]]:
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

    # 口径锁定：防止出题器跨粒度串味（实测：grain_game 被出成「多少个回合」的
    # round 题——因为 scene/实体上下文带进了上一题 grain_round 的措辞与实体，
    # 模型沿用了上一题的计数粒度）。粒度标记取自知识点名里的括号（rounds/games/
    # series/first_blood/economy…），并强制 question 锁定本粒度。
    gran = ""
    _gm = re.search(r"[（(]([^（）()]+)[）)]", name)
    if _gm:
        gran = _gm.group(1).strip()
    # 粒度标记本身往往就是表名（rounds / games / series / base_events /
    # agg_tournament_stats…），直接当取数锚点点名，比只说"锁定粒度"更硬：
    # 模型对抽象的"粒度"不敏感，但对"不许 FROM 某张表"这类具体约束敏感
    # （实测：只说锁定粒度时 grain_series 仍被出成 FROM rounds 的回合题）。
    gran_hint = ""
    if gran:
        gran_hint = (
            f"\n  本题粒度标记是 `{gran}`：reference_sql 必须围绕 `{gran}` 取数"
            f"（若该粒度对应的是报告工具，则以 series_id 等入口参数调用它）；"
            f"**严禁**用与 `{gran}` 粒度不符的表做计数——例如粒度不是 rounds 就"
            f"不要写 FROM rounds，不是 games 就不要写 FROM games。"
        )
    gran_lock = (
        f"\n【口径锁定】本题知识点是「{name}」"
        + (f"，粒度标记为 {gran}" if gran else "")
        + "。你出的 question 的**计数/分析对象必须严格锁定本知识点粒度**"
        "（务必参照上方『问法示例』的口径，例如数图/数局/数系列赛/数回合/首血/"
        "经济，不要混用）。严禁把其它粒度的问法（如本粒度是『图/局』却去问『多少个"
        "回合』、本粒度是『回合』却去问『几张图』）套到本题；也不要照搬上一题的措辞。"
        + gran_hint
    )

    repeat_guard = ""
    if last_question:
        repeat_guard = (
            f"\n⚠️ 上次练过的题是：「{last_question}」\n"
            "请务必**换一个角度 / 换一张地图 / 换一个实体 / 切换计数粒度**出题，"
            "不要和上次雷同（尤其不要沿用上一题在数什么）。"
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
        "  reference_sql: 一条 SELECT 语句，返回本题**目标数值**（必须在当前库可执行，"
        "SELECT 开头；只允许查真实存在的表与列）。这是给「VLML0 裁判」独立跑出标准答案用的——"
        "数值由数据库算，绝不是你编的，你只负责写出能取到该数的查询\n"
        "  dimension: 指标维度短标识（英文，如 event_count / win_rate / kill_count）\n"
        "  subject: 实体字典，如 {\"series\":\"2843069\",\"team\":\"NRG\",\"map\":\"Lotus\"}"
        "（从当前库真实实体取，不要编造；键只能用 series/team/map）\n"
        "  value_column: 目标数值在结果第几列（从 0 开始，默认 0）\n"
        "  numeric_tolerance: 数值容差（默认 0.5）\n"
        "不要编造不存在的表或列；口径必须可验证；reference_sql 必须能跑通。"
    )
    system += (
        "\n\n【真实 schema——reference_sql 的表名/列名只能从这里选】\n"
        f"{_schema_block()}\n\n"
        f"{_SCHEMA_CAVEATS}"
    )
    scene_line = ""
    if scene_text:
        scene_line = f"\n{scene_text}\n"
    sql_err_line = ""
    if sql_error:
        sql_err_line = (
            f"\n\n⚠️ 上一轮生成的 reference_sql 在当前库执行报错，**请修正后重新出一道题**"
            f"（可换问法），确保新 SQL 能在当前库跑通：\n{sql_error}\n"
        )
    user = (
        f"【知识点】{name}（章节：{chapter}）\n"
        f"{style_hint}\n"
        f"\n【技能要求】\n{skill_block}\n"
        f"\n【问法与拿表回答示意（参考，不要照抄原题）】\n{ex_block}\n"
        f"\n【实测常见误区（出题时埋下陷阱或明确警示）】\n{mis_block}\n"
        f"\n难度建议：{topic.get('difficulty')}"
        f"{scene_line}"
        f"{gran_lock}"
        f"{_entity_block()}"
        f"{repeat_guard}"
        f"{sql_err_line}"
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
    reference_sql = _as_str(raw.get("reference_sql"))
    dimension = _as_str(raw.get("dimension"))
    subject = raw.get("subject") if isinstance(raw.get("subject"), dict) else {}
    value_column = _clamp_int(raw.get("value_column"), 0, 20, 0)
    try:
        numeric_tolerance = float(_as_str(raw.get("numeric_tolerance")) or 0.5)
    except (TypeError, ValueError):
        numeric_tolerance = 0.5
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
        "reference_sql": reference_sql,
        "dimension": dimension,
        "subject": subject,
        "value_column": value_column,
        "numeric_tolerance": numeric_tolerance,
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
                 memories: bool = True, scene: bool = True,
                 sql_error: str = "") -> dict[str, Any]:
    """为某个知识点生成一道开放式题。失败抛异常，由调用方降级。

    memories=True 时，先按知识点文本从 mem0 召回既往经验注入出题 prompt
    （Task 3：技能消费带记忆系统——人消费路径也接记忆）。
    scene=True 时，注入「初始场景（数据世界）」块（Task 2：渐进披露——
    模型只能看到当前进度档允许看到的表/工具）。
    sql_error：上一轮参考 SQL 执行报错时回灌，让模型带错重试（见 generate_validated）。
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
                               memories=mem, scene_text=scene_text, sql_error=sql_error)
    raw = _quiz_json(messages)
    if not raw:
        raise ValueError("出题 LLM 返回空/非 JSON")
    q = _normalize(raw, topic, attempt=attempt)
    q["memories_recalled"] = len(mem)
    return q


def _real_tables() -> set[str]:
    """从 _schema_block 解析当前库真实表名（复用缓存，不重复连库）。"""
    tabs: set[str] = set()
    for line in _schema_block().splitlines():
        line = line.strip()
        if line.startswith("- ") and ":" in line:
            tabs.add(line[2:line.index(":")].strip())
    return tabs


def _gran_table(topic: dict[str, Any]) -> str:
    """本题粒度对应的**取数表**；粒度标记本身就是真实表名时才返回它，否则空串。

    数据驱动、不硬编码：粒度标记来自知识点名括号（rounds / games / series /
    base_events / agg_tournament_stats…），是否真是一张表由当前库 schema 判定。
    """
    name = _as_str(topic.get("name"), topic.get("id", ""))
    m = re.search(r"[（(]([^（）()]+)[）)]", name)
    if not m:
        return ""
    gran = m.group(1).strip()
    return gran if gran in _real_tables() else ""


def _sql_tables(sql: str) -> set[str]:
    """提取 reference_sql 的 FROM / JOIN 主表名，用于粒度闸门比对。"""
    out: set[str] = set()
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)",
                         str(sql or ""), re.I):
        out.add(m.group(1).strip('"'))
    return out


def generate_validated(topic_id: str, *, max_tries: int = 3, attempt: int = 0,
                       last_question: str = "", memories: bool = True,
                       scene: bool = True) -> tuple[dict[str, Any], str | None]:
    """出题 + 参考 SQL 自检：生成题后立刻拿真实库校验 reference_sql，
    跑不通就把**具体 SQL 错误**回灌给模型重试（最多 max_tries 次）。

    返回 (q, error)：error 为 None 表示 reference_sql 已可执行；
    否则为最后一条错误（调用方应淘汰换题）。这样客观裁判永远拿得到真值，
    不会出现"题出得对但 SQL 跑不通 → 裁判 unjudgeable → 永远 0% 学会"的死循环。
    """
    sql_error = ""
    last_q: dict[str, Any] = {}
    # 粒度闸门：本题粒度标记若是一张真实表，就要求 reference_sql 必须落在它上面
    try:
        topic = TopicGraph().topics.get(topic_id) or {}
    except Exception:
        topic = {}
    gran_tbl = _gran_table(topic)
    for i in range(max_tries):
        try:
            q = generate_for(topic_id, attempt=attempt, last_question=last_question,
                             memories=memories, scene=scene, sql_error=sql_error)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if i == max_tries - 1:
                return {}, err
            sql_error = f"上一轮出题本身失败：{err}"
            continue
        last_q = q
        sql = str(q.get("reference_sql") or "").strip()
        if not sql:
            # 明确没给 SQL：留给调用方按"无参考 SQL"处理，这里不重试
            return q, "无 reference_sql"
        ok, err = _validate_sql(sql)
        if ok:
            # —— 粒度闸门：SQL 能跑通还不够，取数层必须落在本题粒度上 ——
            # 实测：grain_series 的 SQL 可执行却查的是 rounds（回合粒度），
            # 于是题面与知识点错位 → 裁判按 series 口径判 → 必然 coverage=0%。
            # 提示词层约束不住（模型对"锁定粒度"不敏感），必须在运行时拦下重试。
            if gran_tbl:
                used = _sql_tables(sql)
                if gran_tbl not in used:
                    sql_error = (
                        f"上一次的 reference_sql 能跑通，但**取数粒度与本题知识点不符**"
                        f"（本题粒度表应为 `{gran_tbl}`，你实际查的是 "
                        f"{sorted(used) or '未知'}）。请重写：question 与 reference_sql "
                        f"都必须落在 `{gran_tbl}` 这一层计数，不要借用其它粒度的表。\n"
                        f"错误 SQL：{sql}"
                    )
                    print(f"  （出题自检 {topic_id} 第{i+1}次粒度不符："
                          f"应为 {gran_tbl}，实为 {sorted(used)} → 重试）")
                    continue
            return q, None
        sql_error = (
            f"上一次生成的 reference_sql 执行报错，请修正后重出：\n{sql}\n"
            f"报错：{err}"
        )
        print(f"  （出题自检 {topic_id} 第{i+1}次参考 SQL 不通：{err} → 重试）")
    return last_q, sql_error or "参考 SQL 多次尝试仍不可执行"


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
    # 范围内无「可拿下」任务（设计：应由调用方生成新任务）→ 直接返回选题依据，
    # 不调 generate_for（否则 tid="" 会抛错）。
    if sel.get("exhausted"):
        return {"selection": sel, "exhausted": True,
                "question": "", "reference_sql": "", "topic": ""}
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
