#!/usr/bin/env python3
"""智谱 GLM 的最小客户端（OpenAI 兼容端点）。

密钥从 mve/.env 读取，不写进代码。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx


def load_env() -> None:
    env = Path(__file__).resolve().parent / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_env()

API_KEY = os.getenv("ZHIPU_API_KEY", "")
BASE_URL = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
MODEL = os.getenv("ZHIPU_MODEL", "glm-4-flash")


class LLMError(RuntimeError):
    """LLM 调用最终失败。调用方必须能看见它，不能静默降级成"无证据"。"""


def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout: float = 90.0,
    model: str = "",
    retries: int = 3,
    thinking: dict | None = None,
    response_format: dict | None = None,
) -> str:
    """带退避重试的 chat。

    为什么必须重试：裁判（VLML0）和 Voyager 是并发打的，实测会撞上限流/超时，
    表现为「模型规划返回空 → 一次工具都没调 → 判无证据」。
    不重试的话，这种间歇故障会被误读成「Voyager 没学会」——那是假阴性。

    ★ `thinking` 参数（2026-11-09 服务器实测加）：智谱 GLM-4.6 **默认开着
      思维链**，而思考段与正文**共用 max_tokens**。任务若是「把材料翻译成
      结构化 JSON」这种**不需要深思**的活儿，思考会把 max_tokens 吃光 →
      `finish_reason="length"`、正文一个字都没有 → 解析必然失败。

      实测（场景 L 出题器，prompt 3486 tokens）：
        · 不传 thinking：completion_tokens=2400 全是 reasoning_tokens、
          content=0 → 拆骨架失败（5 次重试全失败）；
        · `thinking={"type":"disabled"}`：reasoning_tokens=0、
          content=1781 → 4 条维度，解析成功。
      智谱官方参数：`{"type": "enabled"|"disabled"}`（GLM-4.5+ 支持）。

      ⚠️ **只给「不需要深思」的调用关思考**（出题器这类）。
        判官（reason_pass / task_critic / critic）**需要**思考，别动它们。
    """
    if not API_KEY:
        raise LLMError("缺少 ZHIPU_API_KEY（写到 mve/.env）")

    body: dict = {"model": model or MODEL, "messages": messages,
                  "temperature": temperature, "max_tokens": max_tokens}
    if isinstance(thinking, dict) and thinking:
        body["thinking"] = thinking
    if isinstance(response_format, dict) and response_format:
        body["response_format"] = response_format

    last = ""
    for attempt in range(retries):
        try:
            r = httpx.post(
                f"{BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}",
                         "Content-Type": "application/json"},
                json=body,
                timeout=timeout,
            )
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}: {r.text[:150]}"
            else:
                r.raise_for_status()
                data = r.json()
                choice = (data.get("choices") or [{}])[0]
                msg = (choice.get("message", {}) or {})
                text = str(msg.get("content") or "").strip()
                if not text:
                    # glm-4.5/4.6 的思考模式：思考段可能把 max_tokens 吃光，
                    # 正文落到 reasoning_content 而 content 为空
                    # （2026-11-06 Scene R 生产实证：语义层 34,720 字材料 +
                    # max_tokens=1800 → 连续「返回空内容」。同构
                    # voyager_vlml/llm.py:103 的兜底 —— 它是模型真说的话）。
                    text = str(msg.get("reasoning_content") or "").strip()
                if text.strip():
                    return text
                # ⚠️ 空内容要**说清是什么空**（2026-11-09 实测）：思考吃光了
                # max_tokens 与「模型真的什么都没说」是两回事，前者可以靠
                # thinking=disabled / 加大 max_tokens 修，后者不能。
                _fr = str(choice.get("finish_reason") or "")
                _usage = data.get("usage") or {}
                _rt = int((_usage.get("completion_tokens_details") or {})
                          .get("reasoning_tokens") or 0)
                if _fr == "length":
                    last = (f"输出被 max_tokens={max_tokens} 截断"
                            f"（思考段占 { _rt } tokens）"
                            + ("—— 这是**思考吃光了预算**，"
                               "调大 max_tokens 或 thinking=disabled 可解"
                               if _rt else ""))
                else:
                    last = "返回空内容"
        except httpx.HTTPError as e:
            last = f"{type(e).__name__}: {e}"

        if attempt < retries - 1:
            time.sleep(1.5 * (2 ** attempt))   # 1.5s → 3s → 6s

    raise LLMError(f"LLM 调用失败（重试 {retries} 次）：{last}")


def chat_json(messages: list[dict[str, str]], **kw) -> dict:
    """要求模型输出 JSON 对象；解析失败返回 {}（调用方负责降级）。"""
    text = chat(messages, **kw)
    # 容忍 ```json ... ``` 包裹
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {}
