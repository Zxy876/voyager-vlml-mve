"""VLML 同构的出题 Agent：按**里程碑节点**出题（让闭环真正自己出题）。

设计要点：
  · 题面只描述"要取什么数"，**不附标准答案**（答案由模型自己写 SQL 去库里取）。
    旧后端在这里会同时产出 reference_sql 给 VLML 裁判算标准答案，那是判分用的、
    只该进面板；本文件不产参考答案。
  · 沿用**口径锁定**：知识点名括号里的粒度标记（rounds / games / series /
    base_events…）就是取数主表，题面必须落在该粒度上，避免跨粒度串味
    （实测 grain_game 曾被出成"多少个回合"）。
"""

from __future__ import annotations

import re
from typing import Any

from voyager_vlml.llm import chat_json

_SYSTEM = (
    "你是 Valorant 电竞数据分析的陪练教练。给定【知识点】和当前库真实 schema，"
    "出**一道**需要用 SQL 从库里取数才能回答的练习题。\n"
    "只返回 JSON：{\"question\": \"题面（自然语言，说清口径：哪张图/哪个队/哪场系列赛）\","
    " \"context\": \"这道题怎么算做出来（见下面第 2 条）\"}\n"
    "硬约束（照原版 curriculum 的「可验 / 唯一答案 / 写明口径」三条，见 "
    "voyager_original/voyager/prompts/curriculum.txt:28,33）：\n"
    "1) **可验**：只出「跑完 SQL 在结果里能直接看出来的数或行」的题。"
    "凡是需要看结果之外的东西（图长什么样、谁更强、哪个更好）才能判对错的，不要出。\n"
    "2) **唯一答案**：题面必须让答案收敛到 1 个数或 1 行。不要出「哪一支队伍 / 哪一场 / "
    "哪些选手」这种库里有多行答案的问法 —— 除非题面自带能把答案唯一钉死的条件。\n"
    "3) **写明口径**：问比例/平均，必须写明**分子和分母各自的取数口径**（哪张表、哪一列、"
    "什么过滤）；问多少，必须写明计数单位（回合/场比赛/选手/系列赛）。"
    "context 就是把这些口径讲清楚的一句话（不是答案，不要写数值）。\n"
    "题面必须能用一条 SELECT 回答；不要编造库里不存在的表或列；不要给答案。"
)


def _gran(name: str) -> str:
    m = re.search(r"[（(]([^（）()]+)[）)]", name or "")
    return m.group(1).strip() if m else ""


class VLMLQuestionAgent:
    def __init__(self, env: Any = None) -> None:
        self.env = env  # 用于取真实 schema（不传则不带 schema 块）

    async def _schema_block(self) -> str:
        """从库里实查表与列（env.schema_block，照伴学 entity_catalog 的"实查不写死"原则）。

        ⚠️ 必须是 async：闭环本身跑在事件循环里，这里再用 asyncio.run 会
        "coroutine was never awaited"（实测踩过）。
        """
        if self.env is None or not hasattr(self.env, "schema_block"):
            return ""
        try:
            return await self.env.schema_block() or ""
        except Exception:
            return ""

    async def generate(self, topic: dict[str, Any]) -> dict[str, str]:
        """为一个里程碑节点出题。

        返回 `{"question": 题面, "context": 口径说明}` —— 两样东西一起给，对应原版
        `propose_next_task()` 返回的 `(task, context)`（`curriculum.py:251-318`）：
        原版的 context 由 `get_task_context()` 另一次 LLM 调用生成（"这道题怎么做、
        什么算成功"），这里并进同一次 JSON 要，少烧一次调用。
        """
        name = str(topic.get("name") or topic.get("id") or "")
        skills = "；".join(str(s) for s in (topic.get("skills") or [])[:3])
        ex = ""
        examples = topic.get("examples") or []
        if examples and isinstance(examples[0], dict):
            ex = str(examples[0].get("prompt") or "")

        gran = _gran(name)
        gran_line = (
            f"\n【口径锁定】本题粒度是 `{gran}`，题面必须围绕该粒度（取数主表 `{gran}`）提问，"
            f"不要借用其它粒度（例如粒度不是 rounds 就不要问「多少个回合」）。"
            if gran
            else ""
        )
        schema = await self._schema_block()
        user = (
            f"【知识点】{name}\n"
            f"技能要求：{skills or '（无）'}\n"
            f"问法示例：{ex or '（无）'}"
            f"{gran_line}\n"
            + (f"\n【当前库真实 schema】\n{schema}\n" if schema else "")
            + "\n请只返回 JSON。"
        )
        out = chat_json(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            temperature=0.6,
        )
        q = str(out.get("question") or "").strip()
        ctx = str(out.get("context") or "").strip()
        # 出题器偶尔把口径写进题面、context 留空；两边都空或只有题面时如实返回，
        # 不替它编口径（口径错了比没有更糟，见 docs/外部task闭环.md §9.5）。
        return {"question": q, "context": ctx}
