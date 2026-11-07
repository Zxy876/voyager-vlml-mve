#!/usr/bin/env python3
"""学习侧取证 → 落库（补上回路的另一半）。

为什么必须有这个模块（2026-11-07 实测查实）：

  `web_ingest` 的模块头写着「联网取回的资料落进本场景数据源 → 学习侧把这张
  表当数据源学出技能 → 下次消费直接答」。这条回路的**消费侧**那一半早就在
  （consume.py 调 `save_docs` / `save_evidence`），但**学习侧**那一半一直是
  断的：学习侧调完联网原语 / MCP 工具，结果只进材料账（evidence），**不进
  数据源**。

  后果在场景 L 实跑里原形毕露：
    · 维度派生把题目派成「查 web_facts / web_docs 表」（这个落点是对的 ——
      上网搜到的东西本来就该在 web_docs 里）；
    · 但学习侧没落库 → 两张表 0 行 → 模型查了个空表 →
      写成「否，不存在满足条件的记录」（**真空否定**：不是查证结果，是没查）；
    · 同时 tracer 空 → 判「一次数据源都没调」→ 整题 no_data。

  即：**取证没发生，落点却已指定**，两头都错。

这里做的事只有一件：把一次联网/MCP 取证的返回，**按其自身形状**映射成
`web_docs` 的资料行 + `web_evidence` 的事实行，落进本场景数据源。

三条铁律：

1. **不硬编码任何工具名或站点名。** 认的是**返回形状**（有没有 url /
   per_site / rows 这类结构），不是「是不是 sherlock」。这样新原语族接进来
   不用改这里（接缝原则：新能力经 MCP 注入，判词/判据零改动）。
2. **落库是副产物，不是答题前提。** 落库失败（表不存在、锁冲突）**不许**让
   取证调用失败 —— 取证成功就是成功，材料账照常记。
3. **落库的是 claim，不是 fact。** `confirmed=False`（web_ingest 的默认），
   人确认后才升级 fact、不进学习线。这条是 web_ingest 模块头定的语义，
   这里不越权。

为什么用「形状识别」而不是「工具名白名单」：MCP 工具由 server 自报，返回
形状五花八门（Sherlock 是 per_site、搜索类是 results 列表、fetch 类是
text+url）。白名单要每接一个新 server 就改一次（正犯「注册 ≠ 注入」那条
老毛病）；形状识别则是通用的。
"""

from __future__ import annotations

from typing import Any


def _clip(s: Any, n: int) -> str:
    return str(s or "")[:n]


def _rows_from_return(ret: Any) -> list[dict[str, Any]]:
    """把一次取证的返回拆成「可落库的资料行」。

    每行 = {url, title, snippet, body, item, attribute, value, method}。

    认三种形状（按可靠性排序，先匹配先生效）：

    ① **per_site 形状**（Sherlock 这类「跨站点探测」型工具）：
       `{"username": "X", "per_site": [{"site": "GitHub", "verdict": "CLAIMED",
        "http": 200, "url": "https://github.com/X"}]}`
       → 一行一个站点。attribute 取站点名，value 取判定结论，
       另存 `http` 状态码（用 `http 状态码` 作属性名，落到 web_facts 成一列）。

    ② **results 形状**（搜索类）：`[{"title":..., "url":..., "snippet":...}]`
       → 一行一条搜索结果。item 用标题、attribute 用「来源」、value 用 url。

    ③ **单篇正文形状**（fetch 类）：`{"url":..., "text"/"body"/"content":...}`
       → 一行，body 存正文（正文才是资料本体，见 web_ingest.ensure 的注释）。

    认不出来 → 返回空列表（不落库，如实不落）。
    """
    body = ret
    if isinstance(body, dict) and set(body) == {"result"}:
        body = body.get("result")
    # MCP 网关可能再包一层 content:[{type:text,text:...}] → 那是纯文本，
    # 落库价值低（没有 url 就不是「可查的资料」），交给 _rows_from_text 判。
    if isinstance(body, dict) and isinstance(body.get("content"), list):
        _t = ""
        for c in body["content"]:
            if isinstance(c, dict) and c.get("text"):
                _t += str(c.get("text") or "")
        body = {"text": _t}

    out: list[dict[str, Any]] = []

    # ① per_site：跨站点探测
    if isinstance(body, dict) and isinstance(body.get("per_site"), list):
        uname = _clip(body.get("username"), 200)
        for r in body["per_site"]:
            if not isinstance(r, dict):
                continue
            site = _clip(r.get("site"), 120)
            if not site:
                continue
            url = _clip(r.get("url"), 400)
            verdict = _clip(r.get("verdict"), 60)
            http = r.get("http")
            http_s = str(int(http)) if str(http or "").isdigit() else ""
            item = uname or site
            out.append({
                "url": url, "title": f"{site} — {item}",
                "snippet": _clip(f"verdict={verdict} http={http_s}", 400),
                "body": _clip(f"site={site} item={item} verdict={verdict} "
                              f"http={http_s} url={url}", 2000),
                "item": item, "attribute": site, "value": verdict,
                "method": "probe",
            })
            if http_s:
                # 状态码单独一列（维度常问「状态码是多少」）。
                # ⚠️ 属性名必须**带站点名**（`GitHub http 状态码`），不能叫
                # `http 状态码` —— web_facts 是按 attribute 摊平的宽表，同名
                # 属性多行并存时 PIVOT 用 `first(value)` 只留一个 → GitHub 的
                # 200 会把 Twitter 的 404 顶掉。带站点名 = 每个站点一列，
                # 维度问「GitHub 的状态码」直接命中那一列。
                out.append({
                    "url": url, "title": f"{site} — {item}",
                    "snippet": _clip(f"http={http_s}", 200),
                    "body": _clip(f"site={site} item={item} http={http_s} "
                                  f"url={url}", 2000),
                    "item": item, "attribute": f"{site} http 状态码",
                    "value": http_s, "method": "probe",
                })
        return out

    # ② results：搜索结果列表
    lst = None
    if isinstance(body, list):
        lst = body
    elif isinstance(body, dict):
        for k in ("results", "rows", "items", "hits"):
            if isinstance(body.get(k), list):
                lst = body[k]
                break
    if lst is not None:
        for r in lst:
            if not isinstance(r, dict):
                continue
            url = _clip(r.get("url") or r.get("link"), 400)
            title = _clip(r.get("title") or r.get("name"), 200)
            snip = _clip(r.get("snippet") or r.get("description")
                         or r.get("text") or r.get("body"), 2000)
            if not (url or title or snip):
                continue
            out.append({
                "url": url, "title": title, "snippet": snip, "body": "",
                "item": title or url, "attribute": "来源", "value": url or title,
                "method": "web",
            })
        return out

    # ③ 单篇正文
    if isinstance(body, dict):
        url = _clip(body.get("url"), 400)
        text = body.get("text")
        if not text:
            for k in ("body", "content", "markdown", "html"):
                if body.get(k):
                    text = body.get(k)
                    break
        text = _clip(text, 20000)
        if url or text:
            out.append({
                "url": url, "title": _clip(body.get("title"), 200),
                "snippet": _clip(text, 2000), "body": text,
                "item": _clip(body.get("title") or url or "网页", 200),
                # ⚠️ 正文进 web_facts 的「正文」列时**不能截到 200**
                # （2026-11-07 用户拍板放开限制）：那会把 JSON 正文截成
                # 非法片段，维度想 `JSON_EXTRACT` 直接报 Malformed JSON。
                # 这里给足上限，落库那层还有 web_ingest.VALUE_MAX 兜底。
                "attribute": "正文", "value": _clip(text, 20000),
                "method": "web",
            })
    return out


def ingest(scene_id: str, question: str, tool_name: str, ret: Any) -> int:
    """把一次取证的返回落进本场景数据源。返回落库行数（失败一律 0）。

    **副产物语义**：任何异常都吞掉并返回 0 —— 取证成功不该因为落库失败
    而变成失败（材料账照常记，判官照常看）。
    """
    if not scene_id or ret is None:
        return 0
    try:
        rows = _rows_from_return(ret)
    except Exception:
        return 0
    if not rows:
        return 0
    try:
        import web_ingest
    except Exception:
        return 0
    n = 0
    # ① 资料行（web_docs）—— 有 url 的才进（save_docs 只收 http 开头的 url）
    docs = [{"url": r["url"], "title": r["title"], "snippet": r["snippet"]}
            for r in rows if str(r.get("url") or "").startswith("http")]
    if docs:
        try:
            bodies = {r["url"]: r["body"] for r in rows
                      if str(r.get("url") or "").startswith("http")
                      and str(r.get("body") or "")}
            n += int(web_ingest.save_docs(scene_id, question, docs,
                                          bodies=bodies) or 0)
        except Exception:
            pass
    # ② 事实行（web_evidence → 摊平进 web_facts）—— 这是维度查得到的那张表
    ev = [{"item": r["item"], "attribute": r["attribute"], "value": r["value"],
           "source_title": r["title"], "source_url": r["url"],
           "confidence": 0.5, "confirmed": False}
          for r in rows if r.get("item") and r.get("attribute")
          and r.get("value")]
    if ev:
        try:
            n += int(web_ingest.save_evidence(
                scene_id, ev, question=question,
                method=str(tool_name or "web")) or 0)
        except Exception:
            pass
    return n
