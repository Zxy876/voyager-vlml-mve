"""voyager_vlml 的 LLM 调用（与 mve 同一套配置：默认智谱，env 可切猫娘桥）。

照 mve/open_quiz._quiz_http 的约定：
  · MVE_QUIZ_BASE_URL / MVE_QUIZ_API_KEY / MVE_QUIZ_MODEL  → 猫娘桥（OpenAI 兼容）
  · 否则 ZHIPU_API_KEY / ZHIPU_BASE_URL / ZHIPU_MODEL      → MVE 自有智谱
走 HTTP 不走 SDK（宿主内禁止 fork，HTTP 无此问题）。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any


def _endpoint() -> tuple[str, str, str]:
    base = os.getenv("MVE_QUIZ_BASE_URL", "").strip()
    if base:
        return (
            base.rstrip("/") + "/chat/completions",
            os.getenv("MVE_QUIZ_API_KEY", ""),
            os.getenv("MVE_QUIZ_MODEL", "glm-4-flash"),
        )
    base_u = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    return (
        base_u.rstrip("/") + "/chat/completions",
        os.getenv("ZHIPU_API_KEY", ""),
        os.getenv("ZHIPU_MODEL", "glm-4-flash"),
    )


def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.4,
    max_tokens: int = 1200,
    timeout: float = 90.0,
    retries: int = 3,
) -> str:
    """发一轮对话，返回模型文本。"""
    import httpx

    url, key, model = _endpoint()
    if not key:
        raise RuntimeError("缺少 LLM 密钥（MVE_QUIZ_API_KEY 或 ZHIPU_API_KEY）")

    last = ""
    for _ in range(retries):
        try:
            r = httpx.post(
                url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                timeout=timeout,
            )
            if r.status_code >= 500 or r.status_code == 429:
                last = f"HTTP {r.status_code}: {r.text[:120]}"
            else:
                r.raise_for_status()
                data = r.json()
                return (
                    (data.get("choices") or [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    or ""
                )
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(1.5)
    raise RuntimeError(f"LLM 调用失败：{last}")


def chat_json(messages: list[dict[str, str]], **kw: Any) -> dict:
    """要模型回 JSON；宽容提取（```json 围栏 / 裸对象）。"""
    text = chat(messages, **kw)
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text
    s, e = raw.find("{"), raw.rfind("}")
    if s < 0 or e <= s:
        return {}
    try:
        return json.loads(raw[s : e + 1])
    except json.JSONDecodeError:
        return {}
