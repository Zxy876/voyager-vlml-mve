#!/usr/bin/env python3
"""GitHub 公开仓库原语：gh_read / gh_tree / gh_repo_info / gh_search。

用户 2026-10-29 的需求：「给 MVE 接 GitHub 让它帮我学习解释项目」——
读公开仓库的 README/源码/目录结构、搜仓库，同「联网能力」组一样是
一组条目（origin=net / group=GitHub），挂在猫爪总开关下面。

与「连接器市场」的边界（用户同日愿景「规划后自己下载拉取连接器」）：
本组只是**读** GitHub 的原语（同 web_search/web_fetch 的只读纪律），
不下载、不执行任何仓库里的代码 —— 动态拉连接器涉及供应链安全
（manifest 校验/白名单/沙箱），另行设计，不混进这组。

安全/限速：
  · 只读 GET，无写操作；复用 net_tools._http_get（SSRF 守卫 + UA + 限长）；
  · 无 GITHUB_TOKEN 时 api.github.com 限 60 次/小时（raw 抓文件不受此限），
    设了 GITHUB_TOKEN 就带上（5000 次/小时）；
  · 取不到就如实抛错 —— 绝不编造（与 critic「取到数」同一条铁律）。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

GH_GROUP = "GitHub"
_API = "https://api.github.com"
_RAW = "https://raw.githubusercontent.com"
_TEXT_LIMIT = 20000          # 与 web_fetch 的正文上限一致
_TREE_LIMIT = 300            # 目录树最多返回多少条 path

_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def _token() -> str:
    """GITHUB_TOKEN（可选）：环境变量优先。没有就匿名（60 次/小时）。"""
    return os.getenv("GITHUB_TOKEN", "").strip()


def _norm_repo(repo: str) -> str:
    """归一仓库名：接受 'owner/repo'、完整 URL、带 .git 尾巴。"""
    r = str(repo or "").strip()
    r = re.sub(r"^https?://(www\.)?github\.com/", "", r)
    r = re.sub(r"\.git$", "", r)
    r = r.strip("/")
    if not _REPO_RE.match(r):
        raise ValueError(f"仓库名要是 owner/repo 形态：{r[:80]}")
    return r


def _get_json(url: str, max_bytes: int = 0) -> dict[str, Any]:
    import net_tools
    _mb = int(max_bytes) if max_bytes else net_tools._MAX_BYTES
    headers_token = _token()
    # _http_get 只带 UA；带 token 要自己补头 —— 复用 _guard_url 逻辑换实现
    if headers_token:
        import urllib.request
        u = net_tools._guard_url(url)
        req = urllib.request.Request(
            u, headers={"User-Agent": net_tools._UA,
                        "Authorization": f"Bearer {headers_token}",
                        "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=net_tools._TIMEOUT) as resp:
            return json.loads(resp.read(_mb).decode("utf-8",
                                                   errors="replace"))
    status, raw = net_tools._http_get(url, max_bytes=_mb)
    if status != 200:
        _raise_gh(status, url)
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"GitHub 返回的 JSON 不完整（超出限长 {_mb} 字节被截断）"
            f"—— 如实不编造：{url[:120]}") from e


def _raise_gh(status: int, url: str) -> None:
    if status == 404:
        raise RuntimeError(f"GitHub 上没有这个资源（404，检查 owner/repo/path"
                           f" 与分支）：{url[:120]}")
    if status == 403:
        raise RuntimeError("GitHub API 限速（匿名 60 次/小时；设 GITHUB_TOKEN "
                           "可提到 5000 次/小时）—— 稍后再试")
    raise RuntimeError(f"GitHub 请求失败（HTTP {status}）：{url[:120]}")


def gh_repo_info(repo: str) -> dict[str, Any]:
    """仓库元数据：描述/star/语言/默认分支。"""
    r = _norm_repo(repo)
    j = _get_json(f"{_API}/repos/{r}")
    if isinstance(j, dict) and j.get("message"):
        _raise_gh(403 if "rate limit" in str(j.get("message")) else 404, r)
    return {"repo": r,
            "description": str(j.get("description") or "")[:300],
            "stars": int(j.get("stargazers_count") or 0),
            "language": str(j.get("language") or ""),
            "default_branch": str(j.get("default_branch") or "main"),
            "url": str(j.get("html_url") or f"https://github.com/{r}")}


def gh_read(repo: str, path: str, ref: str = "") -> dict[str, Any]:
    """读仓库里的一个文本文件（README/源码/配置）。ref 缺省 = 默认分支（HEAD）。"""
    import net_tools
    r = _norm_repo(repo)
    p = str(path or "").strip().lstrip("/")
    if not p:
        raise ValueError("path 是空的（要读仓库里的哪个文件？）")
    _rf = str(ref or "").strip() or "HEAD"
    # 禁路径穿越：.. 段直接拒
    if any(seg == ".." for seg in p.split("/")):
        raise PermissionError(f"path 不许包含 .. 段：{p[:80]}")
    url = f"{_RAW}/{r}/{_rf}/{p}"
    status, raw = net_tools._http_get(url)
    if status != 200:
        _raise_gh(status, url)
    text = raw.decode("utf-8", errors="replace")
    return {"repo": r, "path": p, "ref": _rf, "bytes": len(raw),
            "text": text[:_TEXT_LIMIT],
            "truncated": len(text) > _TEXT_LIMIT}


def gh_tree(repo: str, ref: str = "") -> dict[str, Any]:
    """仓库目录树（文件路径列表，截前 300 条）—— 「这个项目有什么」先看这个。"""
    r = _norm_repo(repo)
    _rf = str(ref or "").strip() or "HEAD"
    # 大仓库的树 JSON 轻松过 200KB（本仓库实测 ~230KB），放宽到 2MB
    j = _get_json(f"{_API}/repos/{r}/git/trees/{_rf}?recursive=1",
                  max_bytes=2_000_000)
    if isinstance(j, dict) and j.get("message"):
        _raise_gh(403 if "rate limit" in str(j.get("message")) else 404, r)
    paths = [str(t.get("path") or "") for t in (j.get("tree") or [])
             if t.get("path")]
    return {"repo": r, "ref": _rf, "count": len(paths),
            "truncated": len(paths) > _TREE_LIMIT,
            "paths": paths[:_TREE_LIMIT]}


def gh_search(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """搜 GitHub 仓库（按 star 排序的 top 结果）。"""
    import urllib.request
    import net_tools
    q = str(query or "").strip()
    if not q:
        raise ValueError("搜索词是空的")
    n = max(1, min(int(max_results or 5), 10))
    j = _get_json(f"{_API}/search/repositories?"
                  f"{urllib.request.quote(q)}&per_page={n}")
    items = j.get("items") or []
    return [{"full_name": str(it.get("full_name") or ""),
             "description": str(it.get("description") or "")[:200],
             "stars": int(it.get("stargazers_count") or 0),
             "url": str(it.get("html_url") or "")} for it in items[:n]]


# ---------------------------------------------------------------------------
# 条目化（给 actions.register_gh 用；schema 手写，同 net_tools 口径）
# ---------------------------------------------------------------------------
PRIMITIVES: list[dict[str, Any]] = [
    {"name": "gh_repo_info",
     "fn": gh_repo_info,
     "desc": "GitHub 仓库元数据（描述/star/语言/默认分支），repo=owner/repo",
     "schema": {"type": "object", "properties": {"repo": {"type": "string"}},
                "required": ["repo"]}},
    {"name": "gh_tree",
     "fn": gh_tree,
     "desc": "GitHub 仓库目录树（文件路径列表）—— 看一个项目有什么先调这个",
     "schema": {"type": "object", "properties": {
         "repo": {"type": "string"}, "ref": {"type": "string"}},
         "required": ["repo"]}},
    {"name": "gh_read",
     "fn": gh_read,
     "desc": "读 GitHub 仓库里的一个文本文件（README/源码），repo=owner/repo",
     "schema": {"type": "object", "properties": {
         "repo": {"type": "string"}, "path": {"type": "string"},
         "ref": {"type": "string"}},
         "required": ["repo", "path"]}},
    {"name": "gh_search",
     "fn": gh_search,
     "desc": "搜 GitHub 仓库（top 结果：名字/描述/star）",
     "schema": {"type": "object", "properties": {
         "query": {"type": "string"}, "max_results": {"type": "integer"}},
         "required": ["query"]}},
]


def bind() -> list[dict[str, Any]]:
    """条目描述（impl 是模块级函数 → 快照可恢复，同 net_tools）。"""
    return [{"name": p["name"], "fn": p["fn"], "description": p["desc"],
             "schema": dict(p["schema"]),
             "impl": f"gh_tools:{p['name']}"} for p in PRIMITIVES]


if __name__ == "__main__":  # pragma: no cover
    import sys
    print(json.dumps(gh_repo_info(sys.argv[1] if len(sys.argv) > 1
                                  else "Zxy876/voyager-vlml-mve"),
                     ensure_ascii=False, indent=1))
