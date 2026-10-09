#!/usr/bin/env python3
"""Voyager 网络天才 —— 猫娘插件（骨架）。

照内置插件 game_agent_minecraft 的形态做：lifecycle + @llm_tool + plugin_entry。
只做「身体和感官」的连通：接收任务 → 观察/取数（走桥）→ 每步 SSE 推送 →
请求人确认 → 人/LLM 调 voyager_resume 继续。学习逻辑由原版 Voyager 本体承担。

铁律：本插件进程内**禁止 fork**（宿主 projectneko_server fork 不安全）。
取数一律走 HTTP 桥（vlml_bridge_client），不 spawn 任何子进程。
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

from plugin.sdk.plugin import (  # 仅宿主内可用
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    plugin_entry,
)

from .loop import VoyagerLoop

# 网关地址：面板/推送走它。插件在宿主内跑，回环直连。
_GATEWAY = os.environ.get(
    "NEKO_GATEWAY",
    "http://127.0.0.1:" + (os.environ.get("NEKO_USER_PLUGIN_SERVER_PORT") or "48916"),
)
_PLUGIN_ID = "voyager_genius"

VOYAGER_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "要 Voyager 去搞清楚的问题（例如：Cloud9 这场总共打了多少回合）",
        },
        "overwrite": {
            "type": "boolean",
            "description": "打断当前正在跑的任务并重新开始",
        },
    },
    "required": ["task"],
}

VOYAGER_TASK_DESCRIPTION = (
    "让 Voyager 去自主查证一个电竞数据问题：它会观察库、取真实数字、"
    "把每一步推到面板，做完后请求人确认是否继续。适合需要查数据的提问。"
)


class VoyagerGeniusPlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
        level = (os.environ.get("NEKO_LOG_LEVEL") or "INFO").strip().upper()
        try:
            self.file_logger = self.enable_file_logging(log_level=level)
        except ValueError:
            self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger
        self._loop = VoyagerLoop(emit=self._push_ui)
        self._last_result: dict[str, Any] = {}

    # ---------------- 推送（SSE，思考透明） ----------------
    def _push_ui(self, text: str) -> None:
        """把一步思考/取数推到面板（后端 → 前端 SSE）。失败不阻断循环。"""
        try:
            req = urllib.request.Request(
                f"{_GATEWAY}/plugin/{_PLUGIN_ID}/ui-api/push",
                data=json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except Exception as exc:
            self.logger.debug("voyager push failed: {}", exc)

    # ---------------- 生命周期 ----------------
    @lifecycle(id="startup")
    async def startup(self, **_):
        from . import vlml_bridge_client as bridge

        health = bridge.health()
        self.logger.info("Voyager 插件启动，VLML 桥状态: {}", health)
        return Ok({"status": "ready", "bridge": health})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        return Ok({"status": "stopped"})

    # ---------------- LLM 可调用（用技能回答人） ----------------
    @llm_tool(
        name="voyager_task",
        description=VOYAGER_TASK_DESCRIPTION,
        parameters=VOYAGER_TASK_SCHEMA,
        timeout=120.0,
    )
    async def voyager_task(self, *, task: Any = None, overwrite: Any = False, **_):
        if not isinstance(task, str) or not task.strip():
            return {"summary": "缺少 task：请告诉 Voyager 要去查什么。"}
        if self._loop.awaiting_human and overwrite is not True:
            return {
                "summary": "上一步还在等确认：要换任务请带 overwrite=true，"
                           "或先调 voyager_resume 让它继续。"
            }
        result = self._loop.run(task.strip())
        self._last_result = result
        summary = "；".join(
            f"{s['kind']}: {s['text']}" for s in result["steps"][-3:]
        )
        return {"summary": summary, "status": result["status"]}

    # ---------------- 人的通道 ----------------
    # 同时也注册成 LLM 工具：这样人在对话里说一句「继续，去查首血转换率」
    # 就能驱动它，不必只靠面板按钮。
    @llm_tool(
        name="voyager_resume",
        description="Voyager 做完请求确认后，用它继续：给它下一条指令或让它换一个知识点。",
        parameters={
            "type": "object",
            "properties": {
                "instruction": {"type": "string", "description": "下一条指令（可空=继续）"},
            },
        },
        timeout=60.0,
    )
    async def voyager_resume_tool(self, *, instruction: Any = "", **_):
        result = self._loop.resume(str(instruction or ""))
        return {"summary": result.get("note", ""), "status": result.get("status")}

    @plugin_entry(
        id="voyager_resume",
        name="让 Voyager 继续",
        description="Voyager 做完请求确认后，由人（或猫娘）下达下一条指令。",
    )
    async def voyager_resume(self, instruction: str = "", **_):
        try:
            result = self._loop.resume(str(instruction or ""))
            return Ok(result)
        except Exception as exc:
            return Err(SdkError(f"resume failed: {exc}"))

    @plugin_entry(
        id="voyager_status",
        name="Voyager 状态",
        description="返回当前任务的步骤轨迹（思考 infra 全可见）。",
    )
    async def voyager_status(self, **_):
        return Ok({"last_result": self._last_result, **self._loop.snapshot()})
