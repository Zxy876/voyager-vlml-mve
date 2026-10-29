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

import base64
import ipaddress
import json
import os
import pathlib
import re
import socket
import threading
import time
import urllib.request
from typing import Any

# 节流 + 退避（2026-10-28 实测踩到）：一次 16 条串行搜索，智谱 web-search-pro
# 直接 429、DuckDuckGo 也限流，只回来 2 条。工具线越"勤快"越拿不到东西。
#   · 最小间隔 1.2s：别把搜索当本地函数连打；
#   · 撞 429/限流退避重试（2s → 5s → 10s），仍失败才如实抛错。
_MIN_GAP = 1.2
_RETRY_WAITS = (2.0, 5.0, 10.0)
_throttle_lock = threading.Lock()
_last_search_at = 0.0


def _throttle() -> None:
    global _last_search_at
    with _throttle_lock:
        gap = _MIN_GAP - (time.time() - _last_search_at)
        if gap > 0:
            time.sleep(gap)
        _last_search_at = time.time()

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")
_TIMEOUT = 15
_SEARCH_TIMEOUT = 10
_MAX_BYTES = 200_000
NET_GROUP = "联网能力"
# 智谱 web-search-pro：搜索在**厂商侧**抓取+摘要，不受本机出口 IP 的信誉连累
# （2026-10-22 实测：这台 VPS 上 DDG 连接超时、Bing 发降级页、维基 403、
# Brave 429、搜狗/360 也开始拦 —— 免 key 的抓取式搜索全灭，只剩这条路）。
ZHIPU_TOOLS_URL = "https://open.bigmodel.cn/api/paas/v4/tools"


def _zhipu_key() -> str:
    """智谱 key：环境变量优先，其次本地密钥文件（与 voyager_pilot 同源）。
    没有就返回空 —— 调用方降级到 DDG，不报错（别的部署可能没有这个 key）。"""
    k = os.getenv("ZHIPU_API_KEY", "").strip()
    if k:
        return k
    for p in (pathlib.Path(__file__).with_name(".env"),
              pathlib.Path("/root/.voyager_secret"),
              pathlib.Path.home() / ".voyager_secret"):
        try:
            if not p.is_file():
                continue
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("ZHIPU_API_KEY="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v
        except Exception:
            continue
    return ""


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


def _http_get(url: str, timeout: int = _TIMEOUT,
              max_bytes: int = _MAX_BYTES) -> tuple[int, bytes]:
    u = _guard_url(url)
    req = urllib.request.Request(u, headers={"User-Agent": _UA,
                                             "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # 上限钳到 5MB：默认 200KB 够网页；GitHub git/trees?recursive 这类
        # JSON 响应大仓库轻松过 200KB（2026-10-29 gh_tree 实测踩到）
        return int(resp.status or 0), resp.read(max(1000, min(int(max_bytes), 5_000_000)))


# ---------------------------------------------------------------------------
# 原语
# ---------------------------------------------------------------------------
def web_search(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """联网搜索（带节流与退避）。返回 [{"title","url","snippet"}]。

    后端顺序：智谱 web-search-pro（厂商侧抓取，摘要即正文片段）→ Bing →
    DuckDuckGo html/lite（免 key 兜底）。全失败就抛错并**列全链错误**
    （调用方如实报），**不返回编造的结果**。

    ⚠️ 撞限流要退避，不要原地重打（2026-10-28 实测：连打 16 次只回来 2 条）。
    调用方（工具线/多跳）应把**取回的结果落库**（web_ingest），别隔一会儿
    又问同一句 —— 那正是「联网资料存进数据源」要解决的问题。
    """
    last: Exception | None = None
    for i in range(len(_RETRY_WAITS) + 1):
        _throttle()
        try:
            return _search_once(query, max_results)
        except _NoBackend:
            raise
        except Exception as e:      # 限流/超时/解析不出 —— 退避再来
            last = e
            if i < len(_RETRY_WAITS):
                time.sleep(_RETRY_WAITS[i])
    raise last if last else RuntimeError("联网搜索失败")


def _search_once(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """单次搜索（不节流、不重试；web_search 负责这两件事）。

    后端链：智谱 web-search-pro → DuckDuckGo → Bing。每个后端的失败
    都记进 errors，全灭时报错**列全链**——2026-10-29 实测踩到：智谱对
    天气/实时类 query 返回空 search_result（大多数 query 正常），错误
    被 DDG 的报错覆盖，最后只看到「页面里解析不出结果」，智谱为什么
    空完全没交代，排查半天。

    ⚠️ **每个后端的返回都要过相关性闸**（2026-10-29 生产实测，这是
    「调研类问题 0/6 闭合」的真根因）：后端没搜到东西时**不报错，而是
    返回一堆无关页面**，于是链上第一个「能解析出条目」的后端就赢 ——
    VPS 上实测同一个 query 三个后端给出：

        query = "voyager vlml mve github"
        智谱 → search_result 空 list
        Bing(rss, setmkt=zh-CN) → 「扫雷游戏网页版 - Minesweeper」×3
        DDG  → GitHub Zxy876/voyager-vlml-mve、掘金文章（正确）

    Bing 那一组被原样交给模型，模型拿着「扫雷」去答「这个项目的社区活跃度」，
    判官只能说「材料里没有该项目信息」。所以这里按**零交集**判垃圾（只是
    「query 里的词一条都没出现」，不做精确相关度，避免误杀）：不合格就把
    这一组丢掉、记原因、继续试下一个后端。全链都不合格 = 如实报「没搜到
    相关内容」，别拿无关页面充数。

    顺序也换了（DDG 提到 Bing 前）：本 VPS 实测 DDG 对技术类 query 准，
    Bing 的中文市场对 "mve" 这类短词会解析成完全无关的热门站。
    """
    q = str(query or "").strip()
    if not q:
        raise ValueError("搜索词是空的")
    n = max(1, min(int(max_results or 5), 10))
    errors: list[str] = []
    # 1) 智谱：有 key 就走（快、稳、摘要带正文）
    try:
        rs = _zhipu_search(q, n)
        kept, dropped = _split_relevant(q, rs)
        if kept:
            return kept[:n]
        errors.append(f"智谱: {dropped} 条全与问题无关（query 里的词一条都没出现）")
    except _NoBackend:
        errors.append("智谱: 未配置 ZHIPU_API_KEY")
    except Exception as e:
        errors.append(f"智谱: {type(e).__name__}: {str(e)[:120]}")
    # 2) DuckDuckGo（免 key，实测本 VPS 上准确率最高）
    for endpoint in (f"https://html.duckduckgo.com/html/?q={urllib.request.quote(q)}",
                     f"https://lite.duckduckgo.com/lite/?q={urllib.request.quote(q)}"):
        try:
            _status, raw = _http_get(endpoint, timeout=_SEARCH_TIMEOUT)
            html = raw.decode("utf-8", errors="replace")
            results = _parse_ddg(html)
            if results:
                kept, dropped = _split_relevant(q, results)
                if kept:
                    return kept[:n]
                errors.append(f"DDG: {dropped} 条全与问题无关")
            else:
                errors.append("DDG: 页面里解析不出结果（可能被限流）")
        except Exception as e:
            err = f"DDG: {type(e).__name__}: {e}"
            # 2026-10-22 场景 B：VPS 出口 IP 被 DDG 限流（403），24 次撞墙没一次
            # 换关键词 —— 报错里必须给出路，别让模型对着同一堵墙原地重试。
            if "403" in err or "429" in err:
                err += ("（可能被搜索服务限流：换个更具体的关键词重试，"
                        "或稍后再试；不要用同一个关键词反复撞）")
            errors.append(err)
    # 3) Bing（2026-10-29 新增兜底：VPS 实测 200 稳定；先 RSS 后 HTML ——
    #    RSS（format=rss）是官方输出：明文链接、带摘要、无自家导航噪声，
    #    天气类 query 的 HTML 版只会给 images/dict 这类自家导航页）
    #    市场按 query 语言选：中文 query 用 zh-CN，纯英文用 en-US ——
    #    原来一律 zh-CN，对英文技术词是负优化（实测给出「扫雷游戏」）。
    _mkt = "zh-CN" if re.search(r"[\u4e00-\u9fff]", q) else "en-US"
    for url in (f"https://www.bing.com/search?q={urllib.request.quote(q)}"
                f"&setmkt={_mkt}&format=rss",
                f"https://www.bing.com/search?q={urllib.request.quote(q)}"
                f"&setmkt={_mkt}"):
        try:
            _status, raw = _http_get(url, timeout=_SEARCH_TIMEOUT)
            text = raw.decode("utf-8", errors="replace")
            results = (_parse_bing_rss(text) if "format=rss" in url
                       else _parse_bing(text))
            if not results:
                errors.append("Bing: 没有条目" if "format=rss" in url
                              else "Bing: 页面里解析不出结果")
                continue
            kept, dropped = _split_relevant(q, results)
            if kept:
                return kept[:n]
            errors.append(f"Bing: {dropped} 条全与问题无关")
        except Exception as e:
            errors.append(f"Bing: {type(e).__name__}: {str(e)[:120]}")
    raise RuntimeError(
        "联网搜索失败（后端全灭：" + "；".join(errors) + "）—— 取不到就如实说，不编造")


# URL/域名里的通用碎片，不能当作「query 命中」的证据
# （否则任何 .com 站点都会通过 "com" 这一条，垃圾页永远挡不住）。
_TLD_NOISE = {"com", "www", "http", "https", "html", "org", "net", "cn", "io",
              "the", "and", "for", "with", "from", "that", "this", "are", "was",
              "you", "can", "not", "but", "如何", "什么", "哪些", "怎么"}


def _query_terms(q: str) -> list[str]:
    """query 里的判别词：优先 ASCII 词（≥3 字符），纯中文时退回汉字 2-gram。"""
    ascii_toks = [t.lower() for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.\-]{2,}", q)
                  if t.lower() not in _TLD_NOISE]
    if ascii_toks:
        return ascii_toks
    zh = re.sub(r"[^\u4e00-\u9fff]", "", q)
    if len(zh) < 2:
        return []
    return [zh[i:i + 2] for i in range(len(zh) - 1)]


def _split_relevant(q: str, results: list[dict[str, Any]]
                    ) -> tuple[list[dict[str, Any]], int]:
    """把「query 里的判别词一条都没出现」的结果挑出去，返回 (留下的, 丢掉几条)。

    只做**零交集**判定，不做精确相关度：宁可放进一条弱相关的，也不误杀一条
    真结果（后面还有判官和材料账把关；这里只负责挡住「扫雷游戏」这种整组
    跑偏的）。
    """
    terms = _query_terms(q)
    if not terms or not results:
        return results, 0
    kept: list[dict[str, Any]] = []
    for r in results:
        blob = " ".join(str(r.get(k) or "") for k in
                        ("title", "url", "snippet")).lower()
        if any(t in blob for t in terms):
            kept.append(r)
    return kept, len(results) - len(kept)


class _NoBackend(Exception):
    """没有配置任何搜索后端（例如没填 ZHIPU_API_KEY）。"""


def _zhipu_search(q: str, n: int) -> list[dict[str, Any]]:
    """智谱 web-search-pro：返回 [{title,url,snippet}]，snippet=智谱已抓的正文片段。

    ⚠️ 它返回的是 `tool_calls` 里 type=search_result 的条目，**不是**普通 text；
    取不到条目要如实抛错（否则会被上层当成"搜到了 0 条"静默跳过）。
    """
    key = _zhipu_key()
    if not key:
        raise _NoBackend()
    body = json.dumps({"tool": "web-search-pro",
                       "messages": [{"role": "user", "content": q}]}).encode()
    req = urllib.request.Request(
        ZHIPU_TOOLS_URL, data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        j = json.loads(resp.read().decode("utf-8", errors="replace"))
    out: list[dict[str, Any]] = []
    for c in ((j.get("choices") or [{}])[0].get("message") or {}
              ).get("tool_calls") or []:
        if c.get("type") != "search_result":
            continue
        for it in c.get("search_result") or []:
            url = str(it.get("link") or it.get("url") or "").strip()
            if not url.startswith("http"):
                continue
            out.append({"title": str(it.get("title") or "").strip()[:120],
                        "url": url,
                        "snippet": re.sub(r"\s+", " ",
                                          str(it.get("content") or "")).strip()[:800]})
    if not out:
        raise RuntimeError("智谱 web-search-pro 没返回 search_result 条目")
    return out[:n]


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


def _parse_bing_rss(xml_text: str) -> list[dict[str, Any]]:
    """Bing RSS 输出解析（format=rss；比 HTML 版稳：明文链接+摘要，无导航噪声）。"""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_text)
    out: list[dict[str, Any]] = []
    for item in root.iter("item"):
        t = item.findtext("title") or ""
        l = (item.findtext("link") or "").strip()
        d = item.findtext("description") or ""
        if t.strip() and l.startswith("http"):
            out.append({"title": re.sub(r"\s+", " ", t).strip()[:120],
                        "url": l,
                        "snippet": re.sub(
                            r"\s+", " ",
                            re.sub(r"<[^>]+>", " ", d)).strip()[:300]})
    return out


def _parse_bing(html: str) -> list[dict[str, Any]]:
    """Bing 结果页解析（2026-10-29 新增兜底后端）。

    结构：`<li class="b_algo">…<h2><a href="…">标题</a></h2>…`，
    摘要在 `b_caption` 里的 `<p>`（与结果同序）。
    链接两种形态都处理：
      · setmkt=zh-CN 时 href 直接是真实 URL；
      · 不带时是 `bing.com/ck/a?...&u=a1<base64url>` 加密跳转 —— 解 base64
        还原真实地址，解不开就丢弃（跳转链接对 web_fetch 没意义还绕远）。
    """
    out: list[dict[str, Any]] = []
    for m in re.finditer(
            r'<li class="?b_algo[^>]*>.*?<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            html, re.S):
        url = m.group(1).replace("&amp;", "&")
        if "bing.com/ck/a" in url:
            mm = re.search(r"[?&]u=a1([^&]+)", url)
            if not mm:
                continue
            b = mm.group(1)
            b += "=" * (-len(b) % 4)
            try:
                url = base64.urlsafe_b64decode(b).decode("utf-8", "replace")
            except Exception:
                continue
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if title and url.startswith("http"):
            out.append({"title": title[:120], "url": url, "snippet": ""})
    # 摘要：b_caption 里的 <p>，与结果同序
    for i, m in enumerate(re.finditer(
            r'class="b_caption[^"]*"[^>]*>.*?<p[^>]*>(.*?)</p>', html, re.S)):
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
     "desc": "联网搜索（智谱 web-search-pro，摘要即正文片段；Bing/DDG 兜底），"
             "返回 [{title,url,snippet}]",
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
