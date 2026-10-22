#!/usr/bin/env python3
"""联网原语：web_search / web_fetch —— 「联网能力」组的条目。

用户 2026-10-19 的设计原话：「然后用编排使用从猫娘搬来的那些 mcp，和插件，
**联网能力**，去达到任务（成功依据是取没取到内容）」。

对照猫娘：`web_search` 本来就是它的本机插件之一；OpenClaw/MCP 工具挂在外面。
MVE 这里把联网做成**一组条目**（origin="net" / group="联网能力"），与
「本机能力」并排挂在猫爪总开关下面 —— 关掉这组，模型就再也碰不到网。

安全边界（必须守）：
  · 只读：只有 GET，没有 POST/写文件；
  · `web_fetch` 禁 SSRF：解析 host → 拒绝私网 / 回环 / 链路本地地址；
  · 限长（200KB）+ 超时（15s），不落盘；
  · 取不到就如实抛错/返回 error —— **绝不编造**。这与 critic 的「取到数」
    判据同一条铁律：联网失败不是"答一个像样的"。
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import urllib.request
from typing import Any

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")
_TIMEOUT = 15
_MAX_BYTES = 200_000
NET_GROUP = "联网能力"


# ---------------------------------------------------------------------------
# SSRF 防护：只许公网 http(s)
# ---------------------------------------------------------------------------
def _guard_url(url: str) -> str:
    u = str(url or "").strip()
    if not re.match(r"^https?://", u):
        raise ValueError(f"只许 http(s) URL：{u[:80]}")
    host = re.match(r"^https?://([^/:?#]+)", u)
    if not host:
        raise ValueError(f"URL 里解析不出主机名：{u[:80]}")
    h = host.group(1).strip("[]")
    try:
        infos = socket.getaddrinfo(h, None)
    except Exception as e:
        raise ValueError(f"域名解析失败：{h}（{type(e).__name__}）") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            raise PermissionError(
                f"拒绝访问私网/回环地址：{h} → {ip}（web_fetch 只到公网）")
    return u


def _http_get(url: str, timeout: int = _TIMEOUT) -> tuple[int, bytes]:
    u = _guard_url(url)
    req = urllib.request.Request(u, headers={"User-Agent": _UA,
                                             "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(resp.status or 0), resp.read(_MAX_BYTES)


# ---------------------------------------------------------------------------
# 原语
# ---------------------------------------------------------------------------
def web_search(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """联网搜索（DuckDuckGo 公共端点，无需 API key）。

    返回 [{"title","url","snippet"}]；搜索不到/被限流就抛错（调用方如实报），
    **不返回编造的结果**。
    """
    q = str(query or "").strip()
    if not q:
        raise ValueError("搜索词是空的")
    n = max(1, min(int(max_results or 5), 10))
    last_err = ""
    for endpoint in (f"https://html.duckduckgo.com/html/?q={urllib.request.quote(q)}",
                     f"https://lite.duckduckgo.com/lite/?q={urllib.request.quote(q)}"):
        try:
            _status, raw = _http_get(endpoint)
            html = raw.decode("utf-8", errors="replace")
            results = _parse_ddg(html)
            if results:
                return results[:n]
            last_err = "页面里解析不出结果（可能被限流）"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            # 2026-10-22 场景 B：VPS 出口 IP 被 DDG 限流（403），24 次撞墙没一次
            # 换关键词 —— 报错里必须给出路，别让模型对着同一堵墙原地重试。
            if "403" in last_err or "429" in last_err:
                last_err += ("（可能被搜索服务限流：换个更具体的关键词重试，"
                             "或稍后再试；不要用同一个关键词反复撞）")
    raise RuntimeError(f"联网搜索失败：{last_err} —— 取不到就如实说，不编造")


def _parse_ddg(html: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    # html.duckduckgo.com: <a class="result__a" href="...">title</a>
    for m in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                         html, re.S):
        url = m.group(1)
        # ddg 的跳转链接 /l/?uddg=<urlencoded>
        if "uddg=" in url:
            mm = re.search(r"uddg=([^&]+)", url)
            if mm:
                url = urllib.request.unquote(mm.group(1))
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if title and url.startswith("http"):
            out.append({"title": title, "url": url, "snippet": ""})
    # lite 版：链接表格，抓 href + 相邻文本
    if not out:
        for m in re.finditer(r'<a[^>]*href="(http[^"]+)"[^>]*>([^<]{4,120})</a>', html):
            url, title = m.group(1), m.group(2).strip()
            if ("duckduckgo" in url) or (not title):
                continue
            out.append({"title": title, "url": url, "snippet": ""})
    # snippet：跟在结果后面的 result__snippet（html 版）
    for i, m in enumerate(re.finditer(
            r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)):
        if i < len(out):
            out[i]["snippet"] = re.sub(r"<[^>]+>", "", m.group(1)).strip()[:300]
    # lite 版的摘要在 <td class="result-snippet"> 里（与结果同序；2026-10-22 补
    # —— 之前 lite 端点的结果 snippet 恒空，模型拿到索引页没摘要更不会去 fetch）
    for i, m in enumerate(re.finditer(
            r'class="result-snippet"[^>]*>(.*?)</td>', html, re.S)):
        if i < len(out) and not out[i]["snippet"]:
            out[i]["snippet"] = re.sub(r"<[^>]+>", "", m.group(1)).strip()[:300]
    return out


def web_fetch(url: str, max_bytes: int = _MAX_BYTES) -> dict[str, Any]:
    """抓一个公网页面的**文本**（去标签，限长）。返回 {url,status,text}。"""
    n = max(1000, min(int(max_bytes or _MAX_BYTES), _MAX_BYTES))
    status, raw = _http_get(url, timeout=_TIMEOUT)
    ctype = ""
    text = raw[:n].decode("utf-8", errors="replace")
    # 粗去标签：脚本/样式整段去掉，剩下的标签剥掉 —— 给模型的是可读文本
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return {"url": url, "status": status, "bytes": len(raw), "text": text[:20000]}


# ---------------------------------------------------------------------------
# 条目化（给 actions.register_net 用；schema 手写 —— 这四个函数是本仓库自己的，
# 契约小而稳定，比反射 partial 的退化类型更诚实）
# ---------------------------------------------------------------------------
PRIMITIVES: list[dict[str, Any]] = [
    {"name": "web_search",
     "fn": web_search,
     "desc": "联网搜索（DuckDuckGo，无需 key），返回 [{title,url,snippet}]",
     "schema": {"type": "object", "properties": {
         "query": {"type": "string"}, "max_results": {"type": "integer"}},
         "required": ["query"]}},
    {"name": "web_fetch",
     "fn": web_fetch,
     "desc": "抓一个公网页面的正文文本（只读、限长、禁私网地址）",
     "schema": {"type": "object", "properties": {
         "url": {"type": "string"}, "max_bytes": {"type": "integer"}},
         "required": ["url"]}},
]


def bind() -> list[dict[str, Any]]:
    """条目描述（impl 是模块级函数 → 快照可恢复，不用像本机原语那样重绑）。"""
    return [{"name": p["name"], "fn": p["fn"], "description": p["desc"],
             "schema": dict(p["schema"]),
             "impl": f"net_tools:{p['name']}"} for p in PRIMITIVES]


if __name__ == "__main__":  # pragma: no cover
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "Voyager JSL paper"
    print(json.dumps(web_search(q, 3), ensure_ascii=False, indent=1))
