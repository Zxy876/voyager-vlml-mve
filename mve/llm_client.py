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
    retries: int = 3,
) -> str:
    """带退避重试的 chat。

    为什么必须重试：裁判（VLML0）和 Voyager 是并发打的，实测会撞上限流/超时，
    表现为「模型规划返回空 → 一次工具都没调 → 判无证据」。
    不重试的话，这种间歇故障会被误读成「Voyager 没学会」——那是假阴性。
    """
    if not API_KEY:
        raise LLMError("缺少 ZHIPU_API_KEY（写到 mve/.env）")

    last = ""
    for attempt in range(retries):
        try:
            r = httpx.post(
                f"{BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": MODEL, "messages": messages,
                      "temperature": temperature, "max_tokens": max_tokens},
                timeout=timeout,
            )
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}: {r.text[:150]}"
            else:
                r.raise_for_status()
                data = r.json()
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
                if text.strip():
                    return text
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
