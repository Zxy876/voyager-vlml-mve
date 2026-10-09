#!/usr/bin/env python3
"""Voyager 学习循环的**骨架**（只做连通，不填学习逻辑）。

骨架刻意只跑「前置交互条件」这条线 —— 原版 Voyager 在 Minecraft 里靠
observation/*.js 获得世界状态、靠 onChat/onError 拿到反馈、靠
curriculum_qa_step1/2 向人提问。这里对位地做四步：

    1. 观察（observe）  → 桥的 get_database_info，看世界有多大
    2. 获取（acquire）  → 桥的 query_sql，拿真实数字
    3. 汇报（report）   → 每一步都推 SSE，思考全程可见
    4. 请求人（ask）    → 推一条确认请求，等人/LLM 调 voyager_resume

**学习逻辑本身不在这里**：curriculum / action / critic / skill library 由
原版 Voyager 承担，本插件只是它在猫娘里的「身体和感官」。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from . import vlml_bridge_client as bridge

# 骨架阶段要跑的两条观测 SQL（对应"看看世界里有什么"）
DEFAULT_QUERIES: list[tuple[str, str]] = [
    ("回合总数", "SELECT count(*) AS total_rounds FROM rounds"),
    ("Cloud9 赢下的回合",
     "SELECT count(*) AS c9_won FROM rounds WHERE winning_team_name = 'Cloud9'"),
]


class VoyagerLoop:
    """一次任务的骨架循环。纯同步、无子进程、无阻塞长任务。"""

    def __init__(self, *, emit: Callable[[str], None]) -> None:
        self._emit = emit            # 每一步的推送回调（SSE / 面板）
        self.steps: list[dict[str, Any]] = []
        self.started_at = 0.0
        self.finished_at = 0.0
        self.awaiting_human = False

    # ---------------- 内部 ----------------
    def _step(self, kind: str, text: str) -> None:
        entry = {"at": round(time.time(), 3), "kind": kind, "text": text}
        self.steps.append(entry)
        self._emit(f"[{kind}] {text}")

    # ---------------- 循环 ----------------
    def run(self, task: str) -> dict[str, Any]:
        self.started_at = time.time()
        self.steps = []
        self.awaiting_human = False

        self._step("task", f"收到任务：{task}")

        # 1) 观察：世界还在吗、多大
        self._step("observe", "观察世界（VLML 库结构）…")
        world = bridge.observe_world()
        if not world.get("ok"):
            self._step("observe", f"观察失败：{world.get('error')}")
            return self._finish("failed", "桥不可达，取不到数（VLML 桥没起？）")
        stats = world.get("statistics") or {}
        self._step("observe", f"世界规模：{stats}")

        # 2) 获取：真实数字
        facts: dict[str, Any] = {}
        for label, sql in DEFAULT_QUERIES:
            self._step("acquire", f"取数：{label}")
            got = bridge.query(sql)
            if not got.get("ok"):
                self._step("acquire", f"{label} 失败：{got.get('error')}")
                continue
            rows = (got.get("result") or {}).get("rows") or []
            value = rows[0][0] if rows and rows[0] else None
            facts[label] = value
            self._step("acquire", f"{label} = {value}")

        # 3) 汇报（骨架阶段不做推理，原样给出实测值）
        self._step("report", f"实测结果：{facts}")

        # 4) 请求人：这是原版 curriculum_qa 的落点
        self.awaiting_human = True
        self._step(
            "ask",
            "做完了。要我继续（换一个知识点/更深一层），还是你来指定下一个任务？"
            " 调 voyager_resume 继续。",
        )
        return self._finish("awaiting_human", "等待人确认")

    def resume(self, instruction: str = "") -> dict[str, Any]:
        """人/LLM 让 Voyager 继续 —— 骨架里只记一步，学习逻辑由 Voyager 本体接。"""
        if not self.awaiting_human:
            return {"status": "idle", "note": "当前没有等待确认的任务"}
        self.awaiting_human = False
        self._step("resume", f"收到人的指令：{instruction or '（继续）'}")
        return self._finish("resumed", f"已继续：{instruction or '（继续）'}")

    # ---------------- 收尾 ----------------
    def _finish(self, status: str, note: str) -> dict[str, Any]:
        self.finished_at = time.time()
        return {
            "status": status,
            "note": note,
            "elapsed_s": round(self.finished_at - self.started_at, 2),
            "steps": list(self.steps),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "awaiting_human": self.awaiting_human,
            "steps": list(self.steps),
            "step_count": len(self.steps),
        }
