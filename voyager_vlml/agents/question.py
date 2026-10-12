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
    "只返回 JSON：{\"question\": \"题面（自然语言，说清口径：哪张图/哪个队/哪场系列赛）\"}\n"
    "要求：题面必须能用一条 SELECT 回答；不要编造库里不存在的表或列；不要给答案。"
)


def _gran(name: str) -> str:
    m = re.search(r"[（(]([^（）()]+)[）)]", name or "")
    return m.group(1).strip() if m else ""


class VLMLQuestionAgent:
    def __init__(self, env: Any = None) -> None:
        self.env = env  # 用于取真实 schema（不传则不带 schema 块）

    def _schema_block(self) -> str:
        """从库里实查表与列（env.schema_block，照伴学 entity_catalog 的"实查不写死"原则）。"""
        if self.env is None or not hasattr(self.env, "schema_block"):
            return ""
        import asyncio

        try:
            return asyncio.run(self.env.schema_block()) or ""
        except Exception:
            return ""

    def generate(self, topic: dict[str, Any]) -> str:
        """为一个里程碑节点出题，返回题面文本。"""
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
        schema = self._schema_block()
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
        return str(out.get("question") or "").strip()
