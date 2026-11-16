#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""book_content —— 已下载 PDF 的真实目录（章/节 + 可回查的页码）。

为什么要有这一层
----------------
graph_books 早先用 23 个学科的**通用单元名**给 72 本书套壳（source=
curriculum:subject）。那是占位：72 本书共用一套模板，「Whole Numbers and
Integers」既不代表 Essential Pre-algebra 的第 1 章，也不代表任何一本。
书里其实没有题，题是模型照单元名编的。

本模块把**真有 PDF 的书**换成从书里抽出来的真目录：单元 = 书里的一章/一节，
带 pdf_from/pdf_to 页范围。抽不出来的书返回 None，调用方退回模板并在 detail
里保留 source=curriculum:subject —— 不冒充原文。

数据文件
--------
book_toc_index.json（离线生成，见 ref/books_pan/build_toc_index.py）：
    {"l2-03": {"title": "Book of Proof", "pages": 313, "page_offset": 10,
               "unit_kind": "section",
               "units": [{"i": 1, "title": "1.1 Introduction to Sets",
                          "chapter": 1, "pdf_from": 13, "pdf_to": 17}, ...]}}

页码一律是 **PDF 页**（1 起）。印刷页码在 page_offset 里，别混用。
answer_from/answer_to 是书后答案区 —— 出题时禁读，不然等于给答案。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX = HERE / "book_toc_index.json"


@lru_cache(maxsize=1)
def _index() -> dict:
    try:
        return json.loads(INDEX.read_text(encoding="utf-8"))
    except Exception:
        # 没有索引不是错误：说明这本书还没下载/没解析，退回模板即可。
        return {}


def has_real_toc(book_id: str) -> bool:
    return str(book_id) in _index()


def toc(book_id: str) -> dict | None:
    return _index().get(str(book_id))


def units(book_id: str) -> list[dict] | None:
    """真实单元列表（i 从 1 起，与 unit:<book>:<i> 的 i 一致）。没有就 None。"""
    d = toc(book_id)
    if not d:
        return None
    out = []
    for u in d.get("units") or []:
        out.append({
            "i": int(u["i"]),
            "title": str(u.get("title") or ""),
            "chapter": u.get("chapter"),
            "chapter_title": u.get("chapter_title") or "",
            "pdf_from": u.get("pdf_from"),
            "pdf_to": u.get("pdf_to"),
            "answer_from": u.get("answer_from"),
            "answer_to": u.get("answer_to"),
        })
    return out


def pdf_of(book_id: str) -> str:
    d = toc(book_id) or {}
    return str(d.get("pdf") or "")


def page_hint(book_id: str, i: int) -> str:
    """给提示词用的一行页范围；没有真目录就返回空串。"""
    d = toc(book_id)
    if not d:
        return ""
    for u in d.get("units") or []:
        if int(u["i"]) != int(i):
            continue
        a, b = u.get("pdf_from"), u.get("pdf_to")
        if a is None or b is None:
            return ""
        s = f"PDF 第 {a}–{b} 页"
        if a <= 0 or b < a:
            return ""
        if u.get("answer_from"):
            s += f"（答案在 {u['answer_from']}–{u['answer_to']} 页，解题时不许看）"
        return s
    return ""


def stats() -> dict:
    idx = _index()
    return {bid: {"title": d.get("title"), "units": len(d.get("units") or []),
                  "pages": d.get("pages"), "kind": d.get("unit_kind")}
            for bid, d in idx.items()}


if __name__ == "__main__":
    print(json.dumps(stats(), ensure_ascii=False, indent=1))
    for bid in _index():
        print(f"\n=== {bid} ===")
        for u in (units(bid) or [])[:5]:
            print(f"  {u['i']:>3} {u['title'][:44]:<44} {page_hint(bid, u['i'])}")
