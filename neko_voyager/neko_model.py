#!/usr/bin/env python3
"""把**原版 Voyager** 的 LLM 调用接到**猫娘（N.E.K.O）的模型**上。

## 为什么能"直接换"

原版 Voyager 四个 agent 都是这样造 LLM 的（`voyager/agents/*.py`）：

    from langchain.chat_models import ChatOpenAI
    self.llm = ChatOpenAI(model_name=..., temperature=..., request_timeout=...)

它只认两件事：LangChain 的 chat 接口、以及**环境里的 OPENAI_API_KEY**。
而猫娘宿主暴露的工厂

    from utils.llm_client import create_chat_llm
    llm = create_chat_llm(model=..., base_url=..., api_key=..., temperature=..., timeout=...)

**返回的也是 LangChain 的 ChatOpenAI**（`utils/llm_client/factory.py:119-134`），
只是 model / base_url / api_key 来自猫娘自己的配置，而不是环境变量。

所以"接入猫娘的模型"就是**换掉造 client 的那一行**，接口签名不用动。

## 配置怎么读（照学习插件的读法，不是猜的）

学习插件 `study_companion/study_model_gateway.py:374-405` 的读法：

    manager = utils.config_manager.get_config_manager()
    config  = await manager.aget_model_api_config("agent")   # 或 "vision"
    model / base_url / api_key / provider_type = config[...]

这里照抄。`model_group` 默认 `"agent"`（文本模型）；带图时猫娘自己会切
`"vision"`，这里不替它决定 —— 那是宿主会话的职责。

## 脱离猫娘时怎么办

不在猫娘进程里（比如命令行单跑、跑测试）时 `utils.*` import 不到，
这时**回落**到原版行为：`ChatOpenAI(model_name=..., ...)` + 环境里的
OPENAI_API_KEY。回落必须显式可见 —— 否则你会以为在用猫娘的模型，
其实在用自己的 key，出了偏差查不到。见 `NekoModelSource`。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

# 构造来源 —— 必须可观测，不然"到底用的谁的模型"说不清
SOURCE_NEKO = "neko"        # 走猫娘配置（宿主内）
SOURCE_FALLBACK = "fallback"  # 回落：LangChain + 环境 OPENAI_API_KEY

_last_source: str = SOURCE_FALLBACK
_last_detail: str = ""


def last_source() -> tuple[str, str]:
    """上一次构造走的是哪条路 + 说明。给日志/面板用。"""
    return _last_source, _last_detail


def _record(source: str, detail: str) -> None:
    global _last_source, _last_detail
    _last_source, _last_detail = source, detail


def _neko_runtime_sync(model_group: str = "agent") -> dict[str, Any] | None:
    """同步地取猫娘的模型配置。返回 None 表示不在一个可用的猫娘宿主里。"""
    try:
        import utils.config_manager as config_manager  # type: ignore[import-not-found]
    except Exception as exc:
        _record(SOURCE_FALLBACK, f"import utils.config_manager 失败：{type(exc).__name__}")
        return None

    get_config_manager = getattr(config_manager, "get_config_manager", None)
    if not callable(get_config_manager):
        _record(SOURCE_FALLBACK, "config_manager 没有 get_config_manager")
        return None

    try:
        manager = get_config_manager()
        aget = getattr(manager, "aget_model_api_config", None)
        if callable(aget):
            # 已经在事件循环里就直接 await；没有就自己开一个
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                config = asyncio.run(aget(model_group))
            else:
                # 同步调用方不该在跑着的 loop 里阻塞等；交给调用方用
                # create_neko_chat_async。这里退回同步接口。
                config = manager.get_model_api_config(model_group)
        else:
            config = manager.get_model_api_config(model_group)
    except Exception as exc:
        _record(SOURCE_FALLBACK, f"读模型配置失败：{type(exc).__name__}: {exc}")
        return None

    if not isinstance(config, dict):
        _record(SOURCE_FALLBACK, "模型配置不是 dict")
        return None

    model = str(config.get("model") or "").strip()
    base_url = str(config.get("base_url") or "").strip()
    api_key = str(config.get("api_key") or "").strip()
    if not (model and base_url and api_key):
        _record(SOURCE_FALLBACK,
                f"猫娘配置不完整（model={'有' if model else '缺'} "
                f"base_url={'有' if base_url else '缺'} "
                f"api_key={'有' if api_key else '缺'}）—— 网页端 /api_key 页面填了吗？")
        return None

    return {
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "provider_type": str(config.get("provider_type") or "openai_compatible").strip(),
    }


def _fallback_chat(model_name: str, temperature: float, request_timeout: int, **kw: Any):
    """原版 Voyager 的行为：LangChain ChatOpenAI + 环境里的 key。"""
    from langchain.chat_models import ChatOpenAI  # 原版依赖，保持不动

    _record(SOURCE_FALLBACK,
            f"ChatOpenAI(model_name={model_name}) —— 走环境 OPENAI_API_KEY"
            f"（{'已设置' if os.environ.get('OPENAI_API_KEY') else '未设置！'}）")
    return ChatOpenAI(
        model_name=model_name,
        temperature=temperature,
        request_timeout=request_timeout,
        **kw,
    )


def create_neko_chat(
    model_name: str = "gpt-3.5-turbo",
    temperature: float = 0.0,
    request_timeout: int = 120,
    *,
    model_group: str = "agent",
    prefer_neko: bool = True,
    **kw: Any,
):
    """造一个 chat client：**优先用猫娘的模型**，不行才回落。

    参数名沿用原版 Voyager 的（`model_name` / `request_timeout`），
    这样改造 agent 时只换函数名，不改调用点。

    `model_name` 在猫娘里**不生效**（模型由猫娘配置决定），除非回落。
    这是有意的：接进来就该用宿主配的模型，Voyager 不该自带一套。
    """
    if prefer_neko:
        runtime = _neko_runtime_sync(model_group)
        if runtime is not None:
            try:
                from utils.llm_client import create_chat_llm  # type: ignore[import-not-found]

                llm = create_chat_llm(
                    model=runtime["model"],
                    base_url=runtime["base_url"],
                    api_key=runtime["api_key"],
                    provider_type=runtime["provider_type"],
                    temperature=temperature,
                    timeout=request_timeout,
                    **kw,
                )
                _record(SOURCE_NEKO,
                        f"猫娘模型 {runtime['model']} @ {runtime['base_url']}"
                        f"（group={model_group}, provider={runtime['provider_type']}）")
                return llm
            except Exception as exc:
                _record(SOURCE_FALLBACK,
                        f"create_chat_llm 失败：{type(exc).__name__}: {exc}")
    return _fallback_chat(model_name, temperature, request_timeout, **kw)


async def create_neko_chat_async(
    model_name: str = "gpt-3.5-turbo",
    temperature: float = 0.0,
    request_timeout: int = 120,
    *,
    model_group: str = "agent",
    **kw: Any,
):
    """异步版：在猫娘的事件循环里用 `create_chat_llm_async`，不阻塞 loop。

    `utils/llm_client/factory.py:143` 的 `create_chat_llm_async` 就是为这个
    存在的 —— 它把同步构造丢到线程里，避免卡住宿主的 loop。
    """
    runtime = _neko_runtime_sync(model_group)
    if runtime is not None:
        try:
            from utils.llm_client import create_chat_llm_async  # type: ignore[import-not-found]

            llm = await create_chat_llm_async(
                model=runtime["model"],
                base_url=runtime["base_url"],
                api_key=runtime["api_key"],
                provider_type=runtime["provider_type"],
                temperature=temperature,
                timeout=request_timeout,
                **kw,
            )
            _record(SOURCE_NEKO,
                    f"猫娘模型（async）{runtime['model']} @ {runtime['base_url']}")
            return llm
        except Exception as exc:
            _record(SOURCE_FALLBACK, f"create_chat_llm_async 失败：{type(exc).__name__}: {exc}")
    return _fallback_chat(model_name, temperature, request_timeout, **kw)


# Voyager 里 `ChatOpenAI` 这个名字被四个 agent 直接引用，给一个同名替身，
# 改造时只改 import 一行即可。
def ChatOpenAI(*args: Any, **kwargs: Any):  # noqa: N802 —— 故意与原版同名
    """`langchain.chat_models.ChatOpenAI` 的落点替换：优先猫娘模型。"""
    return create_neko_chat(*args, **kwargs)


__all__ = [
    "ChatOpenAI",
    "SOURCE_FALLBACK",
    "SOURCE_NEKO",
    "create_neko_chat",
    "create_neko_chat_async",
    "last_source",
]
