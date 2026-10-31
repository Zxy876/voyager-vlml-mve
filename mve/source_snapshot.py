"""来源快照：拆维度**之前**，先把「题面点名的那个来源里到底有什么」取到手（2026-10-31）。

抄的是原版 Voyager 的哪一行
--------------------------
`voyager/agents/curriculum.py:render_observation` 把
`nearby_blocks` / `inventory` / `biome` / `equipment` 一起喂给
`propose_next_task()`；`voyager.py:decompose_task(task)` 也把 `last_events`
（同一份环境观察）传下去。**出题器看得见环境，就不会拆出"挖不存在的东西"这种
子目标** —— 原版从来不靠"失败了再跳过"来兜这个底。

MVE 里这个机制的数据场景版本是 `curriculum_auto.db_observation`（实查表/列/真实
取值）。文档/仓库场景**一直没有同构物**，于是 Scene H 拆出了

    「README.md 文件中提供的仓库最后更新时间是什么时候？」

而 README 里根本没有「更新/时间/日期」任何字样（2026-10-31 实查 raw README 核实）。
模型只好退回去用 `gh_repo_info.updated_at` 顶替，被 source_binding 拦下 —— 判据没错，
错在**出题时假设了一个环境里不存在的事实**。

本模块 = 文档/仓库场景的 `db_observation`：按题面点名的来源取一份「观察面」，
喂给骨架生成器，让维度只指向**看得见的东西**。

口径
----
· 只认 `source_binding.targets()` 判出的 file / url / slug 三类（table 归
  `db_observation` 管，url 不预取正文 —— 慢且不可复现，如实说明「没取」）。
· 任何失败都返回空串：观察面取不到时，退回原来那句「不许造材料里没有的东西」，
  **不许**因为取不到就少拆维度。
· 有体积上限，且**如实标注截断** —— 观察面不是全文，别让下游以为它看完了。
"""

from __future__ import annotations

import os
import re
from typing import Any

# 观察面的预算：文件清单 60 条 + 正文 2600 字。骨架只要"有没有这个事实"，
# 不需要全文；给太多会把拆维度的 prompt 挤爆（原版也只用 blockRecords 前若干条）。
TREE_LIMIT = 60
TEXT_LIMIT = 2600

# URL 指向 GitHub 仓库时**按仓库取**：`targets()` 的优先级（file>url>slug）是为
# 「绑定判定」定的，快照这里要的是「哪份材料最有用」。H 的题面就是
# `https://github.com/Zxy876/voyager-vlml-mve 的readme…`，若照 url 分支走，
# 观察面只剩一句「没有预取正文」（93 字）—— 等于没给环境（2026-10-31 实测）。
_GH_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9_.\-]{0,38})/"
    r"([A-Za-z0-9][A-Za-z0-9_.\-]{0,38})", re.IGNORECASE)


def _repo_from_url(text: str) -> str:
    """`https://github.com/owner/repo/...` → `owner/repo`（其它 URL 返回空）。"""
    m = _GH_URL_RE.search(str(text or ""))
    if not m:
        return ""
    repo = m.group(2)
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    return f"{m.group(1)}/{repo}" if repo else ""


def _read_local(path: str, scene_id: str, limit: int = TEXT_LIMIT) -> str:
    """读本机文件：复用执行层同一套边界（`read_file` 的 _resolve_under / 可读根）。"""
    p = str(path or "").strip()
    if not p:
        return ""
    try:
        import local_agent
        import scene_profile
        d = str(scene_profile.scene_dir(scene_id) or "")
        if not d:
            return ""
        r = local_agent.read_file(d, p, max_bytes=limit * 4)
        return str((r or {}).get("text") or "")[:limit]
    except Exception:
        return ""


def _repo_snapshot(repo: str) -> str:
    """仓库：文件清单 + README 正文（两样都是「这里真有什么」的直接证据）。"""
    lines: list[str] = []
    try:
        import gh_tools
        tree = gh_tools.gh_tree(repo)
        paths = [str(x) for x in (tree.get("paths") or [])][:TREE_LIMIT]
        if paths:
            more = "（还有更多，已截断）" if tree.get("truncated") else ""
            lines.append(f"仓库 {repo} 的文件清单（实查 {tree.get('count')} 项，"
                         f"列前 {len(paths)} 项）{more}：\n"
                         + "；".join(paths))
    except Exception as e:
        lines.append(f"仓库 {repo} 的文件清单没取到（{type(e).__name__}: "
                     f"{str(e)[:120]}）—— 下面的内容不代表它没有别的文件")
    # README 是仓库题最常用的材料；取不到就试几个常见入口，如实报
    for name in ("README.md", "readme.md", "README.rst"):
        try:
            import gh_tools
            r = gh_tools.gh_read(repo, name)
            txt = str(r.get("text") or "")
            if txt.strip():
                cut = "（已截断）" if len(txt) > TEXT_LIMIT else ""
                lines.append(f"{name} 正文（实查，前 {min(len(txt), TEXT_LIMIT)} 字）{cut}：\n"
                             + txt[:TEXT_LIMIT])
                break
        except Exception:
            continue
    else:
        lines.append("README 正文没取到（仓库里可能没有 README.md）。")
    return "\n\n".join(lines)


def snapshot(text: str, scene_id: str = "", *, db_path: str = "") -> dict[str, Any]:
    """按题面点名的来源取一份观察面 → {"kind","target","text"}。

    text 为空 = 没点名 / 取不到（调用方照旧走原路径，不多说一句）。
    """
    out: dict[str, Any] = {"kind": "", "target": "", "text": ""}
    repo = _repo_from_url(text)
    if repo:
        # 优先按仓库取：URL 指向仓库时，仓库结构 + README 才是真正有用的观察面
        out["kind"], out["target"] = "slug", repo
        out["text"] = _repo_snapshot(repo)
        return out
    try:
        import source_binding as _sb
        kind, toks = _sb.targets(text, "")
    except Exception:
        return out
    if not toks or kind not in ("file", "url", "slug"):
        return out
    name = toks[0]
    out["kind"], out["target"] = kind, name
    body = ""
    if kind == "slug":
        body = _repo_snapshot(name)
    elif kind == "file":
        body = _read_local(name, scene_id)
        body = (f"文件 {name} 正文（实查，前 {len(body)} 字）：\n" + body) if body else \
            f"文件 {name} 是本机文件，这里没读到内容（不在可读根下或不存在）。"
    elif kind == "url":
        # 不预取网络正文：慢、可能被限流，而且现场取到的才是可复现的证据。
        body = (f"题面点名了网址 {name}，这里**没有预取**它的正文 —— "
                "相关维度要自己去取，不许凭记忆描述它上面有什么。")
    out["text"] = body
    return out


def describe(snap: dict[str, Any]) -> str:
    """日志一句话：`仓库 Zxy876/x（观察面 1234 字）`。"""
    if not snap or not snap.get("text"):
        return ""
    return f"{snap.get('target')}（观察面 {len(str(snap.get('text')))} 字）"
