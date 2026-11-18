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
from pathlib import Path
from typing import Any


def _load_env_once() -> None:
    """密钥兜底：从 mve/.env 读（照 mve/llm_client.load_env 的做法）。

    pilot 起的子进程不一定带上 ZHIPU_API_KEY，新后端要能自己拿到密钥。
    """
    env = Path(__file__).resolve().parents[1] / "mve" / ".env"
    try:
        if not env.exists():
            return
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass


def _endpoint() -> tuple[str, str, str]:
    _load_env_once()
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
    max_tokens: int = 6000,
    timeout: float = 300.0,
    retries: int = 3,
    model: str = "",
) -> str:
    """发一轮对话，返回模型文本。

    `model`：单次调用覆盖模型（默认走 MVE_QUIZ_MODEL / ZHIPU_MODEL）。
    用途：**critic 换强模型**（2026-10-24 实测 glm-4-flash 当 judge 三连放行
    错误答案 —— 判官这把尺子不能省模型钱；出题/执行层可继续用 flash）。

    max_tokens 1200→6000、timeout 90→300（2026-11-12）：学生换 glm-4.6 后
    思考默认开 —— 思考段与正文共用额度，1200 全被 reasoning 吃光、content
    为空（llm_client.chat 头注释同款坑）；且带思考的生成普遍 >90s，默认
    90s 直接 ReadTimeout×3 重试全超 → 整轮 failed（17:21 实测）。
    """
    import httpx

    url, key, _model = _endpoint()
    if not key:
        raise RuntimeError("缺少 LLM 密钥（MVE_QUIZ_API_KEY 或 ZHIPU_API_KEY）")
    model = str(model or "").strip() or _model

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
                    # 2026-11-18：思考开关由环境变量统一控制（默认关）。
                    # 实测（同一题同一份 messages）：
                    #   glm-4.6 th=enabled  → 单次 2~5 分钟，拆骨架那条直接
                    #                         ReadTimeout（服务端一个字都不返回）
                    #   glm-4.6 th=disabled → 单次约 20s，结果照样是合法 JSON
                    # 全局同一个模型时，思考就是唯一的速度旋钮：不设这个，
                    # 一圈几十次调用必然拖到半小时以上。
                    **({"thinking": {"type": os.getenv("MVE_THINKING", "disabled")}}
                       if os.getenv("MVE_THINKING", "disabled") in
                       ("enabled", "disabled") else {}),
                },
                timeout=timeout,
            )
            if r.status_code >= 500 or r.status_code == 429:
                last = f"HTTP {r.status_code}: {r.text[:120]}"
            else:
                r.raise_for_status()
                data = r.json()
                msg = ((data.get("choices") or [{}])[0]
                       .get("message", {}) or {})
                text = str(msg.get("content") or "").strip()
                if not text:
                    # glm-4.5/4.6 的思考模式：思考段可能把 max_tokens 吃光，
                    # 正文落到 reasoning_content 而 content 为空
                    # （2026-10-29 生产实证：内层 critic 连续 5 次「回复不是
                    # {reasoning, success, critique} 形状」→ 学习必然失败）。
                    # 兜底取 reasoning_content —— 它是模型真说的话，不是编的。
                    text = str(msg.get("reasoning_content") or "").strip()
                return text
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
