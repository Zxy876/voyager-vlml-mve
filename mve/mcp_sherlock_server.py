#!/usr/bin/env python3
"""stdio MCP server —— 把 Sherlock 的站点探测能力经 MCP 暴露给 MVE。

为什么是 MCP，而不是让模型自己拼 URL + web_fetch
-------------------------------------------------
Sherlock 真正的资产不是「URL 拼接」，而是 `resources/sherlock_sites.json` 里
481 条**站点规格**：每站声明 url 模板 / urlProbe（探测地址可与主页不同）/
errorType（status_code|message|response_url 的组合）/ errorCode / errorMsg /
regexCheck / headers / request_method+payload。判定是**确定性**的：

    message       正文含 errorMsg → 无账号，不含 → 有账号
    status_code   命中 errorCode 或 ≥300 或 <200 → 无账号
    response_url  关重定向后 200-299 才算有账号

散调 web_fetch 的代价（服务器实测）：481 次工具调用、若每站回传 2KB 正文
≈962KB 进上下文；而结构化四字段只有 ~51KB，只回候选可压到 ~8KB。更要紧的是
95 个站点要正则、27 个要关重定向、7 个要额外 headers —— 那是本该由代码做的
事，推给模型读文本必然出错。

⚠️ 它给的是**候选**，不是身份断言（2026-11-07 实测）：148 条 CLAIMED 里只有
97 条是 http=200，34 条是 403/429 —— 数据中心 IP 被 WAF 挡，属假阳性。
所以本 server 把 `claimed` 与 `blocked` 分开返回；上层要验身份，仍须点开链接
看内容与时间戳（MVE 的判据本来就是「链接可打开 + 帖子有时间戳」）。

协议：手写 JSON-RPC over stdio，形状与 `mcp_neko_server.py` 完全一致（客户端
`mcp_gateway.MCPClient` 期望换行分隔帧 + protocolVersion 2024-11-05）。
依赖：只用 requests（/root/mve/.venv 已有）；不需要 requests_futures ——
`concurrent.futures.ThreadPoolExecutor` 与 Sherlock 的 20 worker 等价。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
SITES_FILE = HERE / "resources" / "sherlock_sites.json"
CACHE_DIR = Path(os.environ.get("MVE_SHERLOCK_CACHE") or "/root/.cache/mve_sherlock")

_PROTOCOL_VERSION = "2024-11-05"
_UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:129.0) Gecko/20100101 Firefox/129.0")

# WAF 指纹（照抄 sherlock.py:375-380）：命中的响应不是「账号不存在」，
# 而是「我们被拦了」。少了这一层，Cloudflare 站点会成片假阳性。
_WAF_HIT = (
    r"body.dark .lds-ring div{border-color:#999 transparent transparent}",  # Cloudflare
    r'<span id="challenge-error-text">',                                    # CF error page
    r"AwsWafIntegration.forceRefreshToken",                                 # AWS WAF
    r'{return l.onPageView}}),Object.defineProperty(r,"perimeterxIdentifiers"',  # PerimeterX
)

TOOLS: list[dict] = [
    {
        "name": "lookup_username",
        "description": (
            "在 481 个公开站点上并发探测某 username 是否被注册（Sherlock 站点规格驱动，"
            "确定性判定，不回传网页正文）。关键词：用户名、账号、账号存在、主页链接、"
            "状态码、跨平台身份。返回五态统计、http=200 的候选主页 URL 列表、以及被 WAF "
            "挡住的可疑项。想找某个 username 在其他平台的身份主页时用它，不要自己拼 URL "
            "逐个抓。claimed 里 http=200 的才是可点开确认的候选；blocked 表示「没问到」"
            "而非「不存在」。"
            "⚠️ 当题目里出现「用 sherlock 查某用户名」时，**直接调用本工具**，不要用"
            "搜索引擎去查「sherlock 这个工具的说明」——那是介绍文章，不是查询结果。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "username": {"type": "string", "description": "要查的用户名"},
                "sites": {
                    "type": "array", "items": {"type": "string"},
                    "description": "只查这些站点名（如 ['GitHub','Reddit']）；省略=全量 481 站，约 35-50 秒",
                },
                "timeout": {"type": "number", "description": "单站点请求超时秒数，默认 8"},
                "include_available": {
                    "type": "boolean",
                    "description": "是否返回「未注册」的站点清单，默认 false（省 token）",
                },
                "max_items": {"type": "integer", "description": "各清单最多返回条数，默认 120"},
                "ttl": {"type": "number", "description": "结果缓存秒数，默认 3600；传 0 强制重查"},
            },
            "required": ["username"],
        },
    },
    {
        "name": "list_sites",
        "description": (
            "列出 Sherlock 支持的站点规格（站点名、主页、判定方式、是否需正则/headers）。"
            "关键词：站点清单、站点列表、哪些站点、支持的站点。"
            "用于挑选要查哪些站点，或用 keyword 过滤（按站点名匹配）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "按站点名过滤，不区分大小写；省略=全部"},
                "limit": {"type": "integer", "description": "最多返回条数，默认 60"},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 站点规格装载
# ---------------------------------------------------------------------------
def _load_sites() -> dict[str, dict]:
    if not SITES_FILE.exists():
        raise FileNotFoundError(f"站点规格不存在：{SITES_FILE}")
    data = json.loads(SITES_FILE.read_text(encoding="utf-8"))
    data.pop("$schema", None)
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _err_types(spec: dict) -> list[str]:
    et = spec.get("errorType")
    if isinstance(et, str):
        return [et]
    return list(et or [])


def _as_list(v) -> list:
    if v is None:
        return []
    return [v] if not isinstance(v, list) else list(v)


# ---------------------------------------------------------------------------
# 单站点探测：严格照 sherlock.py:238-460 的规格解释逻辑
# ---------------------------------------------------------------------------
def _probe(name: str, spec: dict, username: str, timeout: float) -> dict:
    url_user = str(spec.get("url") or "").replace("{}", username)
    err = _err_types(spec)

    # 站点自身规则不允许这个名字 → 不请求（sherlock.py:248）
    rx = spec.get("regexCheck")
    if rx:
        try:
            if re.search(rx, username) is None:
                return {"site": name, "verdict": "ILLEGAL", "http": "", "url": url_user, "sec": 0.0}
        except re.error:
            pass  # 规格写坏不该拖垮整轮

    headers = {"User-Agent": _UA}
    headers.update({str(k): str(v) for k, v in (spec.get("headers") or {}).items()})

    url_probe = str(spec.get("urlProbe") or spec.get("url") or "").replace("{}", username)
    # status_code 型用 HEAD 就够（sherlock.py:285-292）
    method = str(spec.get("request_method") or ("HEAD" if "status_code" in err else "GET")).upper()
    # response_url 型必须关重定向（sherlock.py:296-304）
    allow_redirects = "response_url" not in err

    payload = spec.get("request_payload")
    if isinstance(payload, dict):
        payload = {k: (v.replace("{}", username) if isinstance(v, str) else v)
                   for k, v in payload.items()}

    t0 = time.time()
    try:
        r = requests.request(method, url_probe, headers=headers, timeout=timeout,
                             allow_redirects=allow_redirects, json=payload)
    except Exception as e:
        return {"site": name, "verdict": "UNKNOWN", "http": "?", "url": url_user,
                "sec": round(time.time() - t0, 2), "why": type(e).__name__}

    st, body = r.status_code, (r.text or "")
    verdict = "UNKNOWN"

    if any(hit in body for hit in _WAF_HIT):
        verdict = "WAF"
    elif any(t not in ("message", "status_code", "response_url") for t in err):
        verdict = "UNKNOWN"
    else:
        if "message" in err:
            needles = [str(x) for x in _as_list(spec.get("errorMsg"))]
            verdict = "AVAILABLE" if any(n in body for n in needles) else "CLAIMED"
        if "status_code" in err and verdict != "AVAILABLE":
            codes = [c for c in _as_list(spec.get("errorCode")) if isinstance(c, int)]
            verdict = "AVAILABLE" if (st in codes or st >= 300 or st < 200) else "CLAIMED"
        if "response_url" in err and verdict != "AVAILABLE":
            verdict = "CLAIMED" if 200 <= st < 300 else "AVAILABLE"

    return {"site": name, "verdict": verdict, "http": st, "url": url_user,
            "sec": round(time.time() - t0, 2)}


# ---------------------------------------------------------------------------
# 缓存（学习循环会反复查同一个 username，不该每次都打 481 站）
# ---------------------------------------------------------------------------
def _cache_path(username: str, sites: list[str] | None) -> Path:
    key = hashlib.sha1(
        (username + "|" + ",".join(sorted(sites or []))).encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"{key}.json"


def _cache_get(username: str, sites: list[str] | None, ttl: float):
    if ttl <= 0:
        return None
    p = _cache_path(username, sites)
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        age = time.time() - float(obj.get("ts") or 0)
        if age <= ttl:
            obj["age"] = round(age, 1)
            return obj
    except Exception:
        return None
    return None


def _cache_put(username: str, sites: list[str] | None, obj: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _cache_path(username, sites)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)          # 原子写：半截缓存比没缓存更糟
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
def sherlock_lookup(username: str, sites: list[str] | None = None, timeout: float = 8.0,
                    include_available: bool = False, max_items: int = 120,
                    ttl: float = 3600.0, max_workers: int = 20,
                    deadline: float = 50.0) -> dict:
    username = str(username or "").strip()
    if not username:
        raise ValueError("username 不能为空")
    sites = [str(s) for s in (sites or [])] or None

    hit = _cache_get(username, sites, ttl)
    if hit is not None:
        out = {k: v for k, v in hit.items() if k != "ts"}
        out["cached"] = True
        return out

    all_sites = _load_sites()
    if sites:
        picked = {n: all_sites[n] for n in sites if n in all_sites}
        missing = [n for n in sites if n not in all_sites]
    else:
        picked, missing = all_sites, []
    if not picked:
        return {"username": username, "counts": {"sites": 0},
                "error": f"没有可用站点；未找到：{missing[:10]}"}

    t0 = time.time()
    results: list[dict] = []
    timed_out = 0
    workers = max(1, min(int(max_workers), 32))
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {ex.submit(_probe, n, s, username, float(timeout)): n for n, s in picked.items()}
        done, not_done = wait(futures, timeout=float(deadline), return_when=FIRST_COMPLETED)
        # FIRST_COMPLETED 只是先醒一次，继续收直到 deadline
        end = t0 + float(deadline)
        while not_done:
            left = end - time.time()
            if left <= 0:
                break
            d, not_done = wait(not_done, timeout=left, return_when=FIRST_COMPLETED)
            done |= d
        for f in done:
            try:
                results.append(f.result())
            except Exception as e:
                results.append({"site": futures[f], "verdict": "UNKNOWN", "http": "?",
                                "url": "", "why": type(e).__name__})
        timed_out = len(not_done)
        for f in not_done:
            f.cancel()
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    elapsed = round(time.time() - t0, 1)
    counts = {"sites": len(picked), "claimed": 0, "available": 0,
              "unknown": 0, "illegal": 0, "waf": 0, "timeout": timed_out}
    claimed, available, unknown = [], [], []
    for r in results:
        v = r.get("verdict")
        if v == "CLAIMED":
            counts["claimed"] += 1
            claimed.append({"site": r["site"], "url": r["url"], "http": r.get("http")})
        elif v == "AVAILABLE":
            counts["available"] += 1
            available.append(r["site"])
        elif v == "ILLEGAL":
            counts["illegal"] += 1
        elif v == "WAF":
            counts["waf"] += 1
            unknown.append({"site": r["site"], "url": r["url"], "why": "WAF"})
        else:
            counts["unknown"] += 1
            unknown.append({"site": r["site"], "url": r["url"], "why": r.get("why") or "UNKNOWN"})

    # http=200 的排前面：那才是能点开做二次确认的
    claimed.sort(key=lambda x: (0 if x["http"] == 200 else 1, str(x["site"]).lower()))
    blocked = [c for c in claimed if isinstance(c["http"], int) and c["http"] >= 400]

    out = {
        "username": username,
        "counts": counts,
        "claimed": claimed[:max_items],
        "blocked": blocked[:max_items],
        "unknown": unknown[:20],
        "elapsed": elapsed,
        "note": ("claimed 里 http=200 是可点开确认的候选；blocked/unknown 是「没问到」，"
                 "不能当作「不存在」。要断言身份仍须打开链接看内容与时间戳。"),
    }
    if include_available:
        out["available"] = available[:max_items]
    if missing:
        out["missing_sites"] = missing[:20]
    _cache_put(username, sites, {**out, "ts": time.time()})
    out["cached"] = False
    return out


def sherlock_sites(keyword: str = "", limit: int = 60) -> dict:
    data = _load_sites()
    kw = str(keyword or "").strip().lower()
    rows = []
    for name, spec in data.items():
        if kw and kw not in name.lower():
            continue
        et = _err_types(spec)
        rows.append({
            "site": name,
            "url_main": spec.get("urlMain"),
            "guess_url": str(spec.get("url") or "").replace("{}", "<username>"),
            "detect": "+".join(et) if et else "?",
            "has_regex": bool(spec.get("regexCheck")),
            "custom_headers": bool(spec.get("headers")),
        })
    rows.sort(key=lambda x: str(x["site"]).lower())
    return {"total": len(rows), "shown": min(len(rows), int(limit or 60)),
            "sites": rows[:int(limit or 60)]}


# ---------------------------------------------------------------------------
# JSON-RPC 传输层（形状与 mcp_neko_server.py 一致）
# ---------------------------------------------------------------------------
def _write(frame: dict) -> None:
    import sys
    sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _dispatch(name: str, arguments: dict) -> object:
    try:
        if name == "lookup_username":
            return sherlock_lookup(
                str(arguments.get("username", "")),
                arguments.get("sites") or None,
                float(arguments.get("timeout", 8.0)),
                bool(arguments.get("include_available", False)),
                int(arguments.get("max_items", 120)),
                float(arguments.get("ttl", 3600.0)),
            )
        if name == "list_sites":
            return sherlock_sites(str(arguments.get("keyword", "")),
                                  int(arguments.get("limit", 60)))
    except Exception as e:
        return {"__error__": f"{type(e).__name__}: {e}"}
    return {"__error__": f"未知工具 {name}"}


def _format_result(py_obj: object) -> dict:
    """structuredContent 必须是 dict（MCP 规范 + 桥接层 pydantic 校验）。"""
    if isinstance(py_obj, dict) and "__error__" in py_obj:
        err = str(py_obj["__error__"])
        return {"content": [{"type": "text", "text": err}], "isError": True,
                "structuredContent": {"error": err}}
    text = json.dumps(py_obj, ensure_ascii=False)
    sc = py_obj if isinstance(py_obj, dict) else (
        {"results": py_obj} if isinstance(py_obj, list) else {"value": py_obj})
    return {"content": [{"type": "text", "text": text}], "isError": False,
            "structuredContent": sc}


def _serve() -> None:
    import sys
    for raw in sys.stdin.buffer:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        rid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            _write({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mve-sherlock", "version": "1.0"},
            }})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _write({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            _write({"jsonrpc": "2.0", "id": rid,
                    "result": _format_result(_dispatch(str(params.get("name") or ""),
                                                       params.get("arguments") or {}))})
        elif rid is not None:
            _write({"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"unknown method: {method}"}})


if __name__ == "__main__":
    _serve()
