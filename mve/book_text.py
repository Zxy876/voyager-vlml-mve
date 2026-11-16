#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""book_text —— 书里的原文（逐页文本），给「照书出题」用。

和书单路线的关系
----------------
graph_books 只给出「这一关是哪本书的哪一节、在哪几页」。模型拿到一个章节
标题就去编题，等于没读书：实测「Similarity」出了勾股定理。本模块把**那几页
的原文**取出来，塞进出题上下文，题才有出处。

数据
----
book_text/<book_id>.json：{"pages": {"13": "...", ...}, "stats": {...}}
  - Book of Proof：PDF 自带文本层，直抽（≈2400 字符/页）
  - Essential Prealgebra：字体缺 ToUnicode，300dpi + tesseract OCR
    （≈900 字符/页，上标会丢：4² 可能成 42 / 4?）

两个已知失真，出题提示里必须声明，不许假装原文精确：
  1. OCR 的上标丢失 —— 见到 `4?`、`3°`、`42 = 4×4` 这类按幂理解
  2. 花括号在 Book of Proof 的字体映射里是 `©…ª`

用法
----
    python3 book_text.py                    # 有哪些书、每本多少字符
    python3 book_text.py l2-03 1            # 打印第 1 单元的给模型的摘录
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEXT_DIR = HERE / "book_text"
# 「这本书这一节出过哪几道原题」的账。按**书 + 单元 + 题号**记，不按场景记：
# 同一道印在书上的题，换个场景还是同一道题，重复出就是重复（用户 2026-11-15：
# 「书里有什么题就出什么题」—— 照书往下走，而不是反复出同一道）。
USED_PATH = HERE.parent / "scene_data" / "used_exercises.json"

LIG = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
       "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"}


def _clean(t: str) -> str:
    for k, v in LIG.items():
        t = t.replace(k, v)
    # Book of Proof 的花括号被字体映射成 © ª（这两个字符在数学正文里不会正常出现）
    t = t.replace("©", "{").replace("ª", "}")
    t = t.replace("\u00a0", " ")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


@lru_cache(maxsize=8)
def _book(book_id: str) -> dict:
    p = TEXT_DIR / f"{book_id}.json"
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    d["pages"] = {int(k): _clean(v) for k, v in (d.get("pages") or {}).items()}
    return d


def available(book_id: str) -> bool:
    return bool(_book(book_id).get("pages"))


def page(book_id: str, pno: int) -> str:
    return (_book(book_id).get("pages") or {}).get(int(pno), "")


def _range(book_id: str, i: int) -> tuple[int, int] | None:
    import book_content

    d = book_content.toc(book_id)
    if not d:
        return None
    for u in d.get("units") or []:
        if int(u["i"]) == int(i):
            a, b = u.get("pdf_from"), u.get("pdf_to")
            if a and b and b >= a:
                return int(a), int(b)
            return None
    return None


def _is_exercise(t: str) -> bool:
    """这一页是本题的习题区（教科书每节末的 Exercises）。"""
    return bool(re.search(r"\bExercises?\b|\bProblems?\b", t or ""))


_CJK = re.compile(r"[一-鿿]")


def _is_cjk_book(t: str) -> bool:
    """这段文字是中文教材吗（按汉字占比判，不看书名）。

    2026-11-16：中文教材（人教A版）进来后，原来只按英文词数判讲解页，
    每页都判成「没有讲解」，正文只能走头尾兜底、习题抽 0 条。
    """
    s = t or ""
    cjk = len(_CJK.findall(s))
    return cjk >= 40 and cjk / max(1, len(s)) >= 0.25


def _flat(t: str) -> str:
    """去掉所有空白：OCR 在汉字间插假空格（"求 集 合"），
    任何多字词匹配前都必须先摊平，否则「计算」「证明」全都匹配不上。"""
    return re.sub(r"\s+", "", t or "")


def _is_prose(t: str) -> bool:
    """这一页有讲解（成段的英文），不是纯公式/纯编号页。

    练习册每章 20+ 页习题，OCR 出来是 `(v2) =`、`13) (V1) =` 这种半残行，
    塞给出题器等于让它照抄一道坏题；讲解页 OCR 干净，才是能读懂的那部分。
    """
    # 几何/彩图教材一页里公式与图注占多数，55 词的门槛会把整章都判成"没有讲解"。
    # 30 词 + 120 字符就能保证这是能读的段落，不是 OCR 碎渣。
    if _is_cjk_book(t):
        # 中文讲解页：汉字量足够 + 不是纯编号/纯公式页（中文版没有「词」的概念，
        # 用汉字数代替词数；一行 80 汉字就是一段能读的讲解）。
        return len(_CJK.findall(t or "")) >= 80
    return (len(re.findall(r"[A-Za-z]{4,}", t or "")) >= 30
            and len((t or "").strip()) >= 120)


def unit_reading(book_id: str, i: int, *, max_chars: int = 3200) -> str:
    """该单元给模型看的原文摘录。

    选页优先级：
      1. **习题页**（教科书每节末的 Exercises）—— 有真题就别让它编
      2. 页序靠前的**讲解页** —— 定义/定理/例题都在这里
      3. 都没有（练习册后半本全是烂 OCR 的习题）就按头尾取

    这样一本《Book of Proof》的一节能拿到「本节定义 + 本节习题」两头，
    模型是在书里挑题，不是照标题编题。
    """
    rng = _range(book_id, i)
    if not rng or not available(book_id):
        return ""
    a, b = rng
    pages = [p for p in range(a, b + 1) if page(book_id, p)]
    if not pages:
        return ""
    total = sum(len(page(book_id, p)) for p in pages)
    if total <= max_chars:
        pick = pages
    else:
        pick: list[int] = []
        ex = [p for p in pages if _is_exercise(page(book_id, p))]
        if ex:
            pick.append(ex[0])
        for p in pages:
            if len(pick) >= 4:
                break
            if p in pick:
                continue
            if _is_prose(page(book_id, p)):
                pick.append(p)
        if not pick:
            head = pages[:2]
            tail = pages[-2:] if len(pages) > 3 else []
            pick = head + [p for p in tail if p not in head]
        pick.sort()
    per = max(700, max_chars // max(1, len(pick)))
    out = []
    for p in pick:
        t = page(book_id, p)
        if len(t) > per:
            cut = t[:per]
            # 尽量切在句子边界，别把最后一句切成半个词
            m = max(cut.rfind(". "), cut.rfind(".\n"))
            if m > per * 0.6:
                cut = cut[:m + 1]
            t = cut + " …"
        out.append(f"〔PDF p{p}〕\n{t}")
    return "\n\n".join(out)


_EX_CUE = re.compile(
    r"\b(find|compute|calculate|evaluate|prove|show|determine|how many|"
    r"in how many|examine|construct|suppose|whether|is it|verify|obtain|"
    r"give an example|list all|count|solve)\b", re.I)

# 中文题干动词（2026-11-16）：中文教材的题面没有英文 cue 词，原判据把
# 人教A版整本书的习题全滤成 0 条。判据在**去空格后的文本**上跑（_flat）。
_EX_CUE_CN = re.compile(
    r"求|计算|证明|求证|判断|化简|解方程|解不等式|解关于|写出|列出|列举|"
    r"比较|讨论|设|已知|若|说明|指出|画出|画|叙述|举例|表示|检验|估计|"
    r"推导|探究|填写|选择|填空|回答|分析|验证|观察|思考|发现|归纳")


# 习题区里混着解答残行（"= 128." / "Hence, h = 2x"），这些不是题面。
_EX_JUNK = re.compile(r"^\s*(=|Hence|Thus|So,|Therefore|Since|Because|"
                      r"Chapter|Section|Example|Solution|Fig\.|Table)\b", re.I)

# 中文答案/解析残行：**必须带冒号**才算 junk —— 「解方程 x²=4」是题面，
# 「解：x=±2」才是答案，不能一视同仁（2026-11-16）。
_EX_JUNK_CN = re.compile(
    r"^(解|答|证明|分析|点评|注意|说明|略|提示|解析)[:：]|^例\s?\d")


# 送分题：一句 Compute P(8,5) / Prove Theorem 1.2.2 就是一步套公式或纯背书，
# 放进候选池只会被挑中。直接不让它们进池 —— 提示词说「跳过」模型并不听。
_EX_TRIVIAL = re.compile(
    r"^\s*(Compute|Calculate|Evaluate|Simplify)\b"
    r"(?!.*\b(number of|ways|how many|probability|coefficient|in how many))"
    r"|^\s*Prove\s+(Theorem|Lemma|Corollary)\s+\d", re.I)


def unit_exercises(book_id: str, i: int, *, limit: int = 24) -> list[str]:
    """从单元页范围里抓带编号的题行，返回「页号 + 题号 + 完整题面」。

    为什么要抓真题：早先只把正文摘录丢给模型，模型就把「SOLVED PROBLEM」的
    结构抄一遍、数字一换，几何题抄成了「请说明全等依据」的送分题。有真题时
    应该原样照出，模型只做翻译，不做创作。

    抓不全的地方也认：编号必须是 1 / 1.7 / 1.7. 三种都收，带题干动词的才留
    （正文里的编号公式、解答残行会被 cue 与 junk 两道闸滤掉）。
    """
    rng = _range(book_id, i)
    if not rng:
        return []
    a, b = rng
    pat = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3})?)[.)．]\s*(.{6,})$")
    # 先把整段页文本摊成 (页号, 行) 序列：题面常被换页/换栏截断，必须能跨行续接。
    lines: list[tuple[int, str]] = []
    for p in range(a, b + 1):
        for ln in page(book_id, p).split("\n"):
            s = ln.strip()
            if s:
                lines.append((p, s))

    out: list[str] = []
    k = 0
    while k < len(lines):
        p, ln = lines[k]
        m = pat.match(ln)
        if not m:
            k += 1
            continue
        label, body = m.group(1), m.group(2).strip()
        _b_flat = _flat(body)
        if (_EX_JUNK.match(body) or _EX_JUNK_CN.match(_b_flat)
                or re.search(r"\.{4,}", body)
                or not (_EX_CUE.search(body) or _EX_CUE_CN.search(_b_flat))):
            k += 1
            continue
        # 续行：直到下一道题的编号、或明显的页眉/公式/标题行为止。
        j = k + 1
        while j < len(lines) and len(body) < 320:
            nxt = lines[j][1]
            if (pat.match(nxt) or _EX_JUNK.match(nxt)
                    or re.fullmatch(r"\d{1,3}", nxt)
                    or re.match(r"^\d{1,2}\.\d{1,2}\s+[A-Z]", nxt)
                    or re.fullmatch(r"[A-Z][A-Z\s\.\-]{7,}", nxt)):
                break
            body = f"{body} {nxt}".strip()
            j += 1
        if _EX_TRIVIAL.match(body):
            k = j
            continue
        out.append(f"p{p} #{label}: {body}")
        k = j
        if len(out) >= limit:
            break
    return out


_EX_LABEL = re.compile(r"^p(\d+)\s+#([\d.]+)\s*:")


def _label_of(line: str) -> str:
    """从 `p52 #1.3: ...` 里取题号 `1.3`。"""
    m = _EX_LABEL.match(str(line or "").strip())
    return m.group(2) if m else ""


def used_labels(book_id: str, i: int) -> set[str]:
    """这一单元已经出过的题号（如 {"1.3","1.8"}）。"""
    try:
        d = json.loads(USED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {str(x.get("label")) for x in (d.get(f"{book_id}:{i}") or [])}


def mark_used(book_id: str, i: int, label: str, question: str = "") -> None:
    """记一笔「这题出过了」。题号形如 1.8 / 3（书里只写 3 的章）。"""
    label = str(label or "").strip()
    if not label:
        return
    try:
        d = json.loads(USED_PATH.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    key = f"{book_id}:{i}"
    arr = d.setdefault(key, [])
    if any(str(x.get("label")) == label for x in arr):
        return
    import time
    arr.append({"label": label, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "q": str(question or "")[:80]})
    USED_PATH.parent.mkdir(parents=True, exist_ok=True)
    USED_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                         encoding="utf-8")


def scene_hint(book_id: str, i: int) -> str:
    """给提示词的一段说明（含失真声明 + 原题候选）。没有原文就返回空串。"""
    body = unit_reading(book_id, i)
    exs_all = unit_exercises(book_id, i)
    done = used_labels(book_id, i)
    exs = [e for e in exs_all if _label_of(e) not in done]
    if not body and not exs_all:
        return ""
    head = (
        "【教材原文摘录】下面是这本书**本单元对应页**的原文（OCR/文本层抽取）。\n"
        "失真提示：数学上标在抽取时可能丢，形如 `4?`、`3°`、`42 = 4×4` 的地方"
        "要按幂理解（4²）；花括号可能显示成 `©…ª`。\n"
        "注意：摘录里形如 `Fig. 3-20`、`in the figure below` 的配图**没有**被"
        "抽出来。凡是靠图才能解的题，一律不要选。\n"
        "---- 原文开始 ----\n" + (body or "") + "\n---- 原文结束 ----"
    )
    if not exs_all:
        return head + (
            "\n（本单元页范围内没抓到题面完整的编号习题 —— 这种情况才允许照摘录"
            "里的定义/定理自己出一道同类题。）\n")
    if not exs:
        return head + (
            "\n（本单元的原题已经全部出过一遍了 —— 这次才允许照摘录里的定理/"
            "例题自己出一道同类题，并在 Context 里写「自编」。）\n")
    return head + (
        f"\n【原题候选】书里本单元**真实印着的**题（页号 + 题号 + 原题面）。\n"
        f"已剔除依赖配图的、以及**已经出过的 {len(done)} 道**，下面是还没出过的。\n"
        + "\n".join(exs) +
        "\n【取材规则】有这张清单时，必须从里面选**一道**，原样翻成中文照出：\n"
        "  · 数字、字母、条件一个都不许改（计数题的答案会随数字变，改了就不是原题）；\n"
        "  · 只做两件事：翻成中文、把原文因换行被截断的部分补回完整一句话；\n"
        "  · 优先选带 (a)(b)(c) 小问的、或需要分情况讨论的那道；\n"
        "  · 题号页号写进 Context（例：`原题 p52 1.8`）。\n")


def stats() -> dict:
    out = {}
    if TEXT_DIR.exists():
        for p in sorted(TEXT_DIR.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                out[p.stem] = d.get("stats") or {}
            except Exception:
                out[p.stem] = {"error": True}
    return out


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3:
        bid, i = sys.argv[1], int(sys.argv[2])
        print(scene_hint(bid, i))
        ex = unit_exercises(bid, i, limit=6)
        print("\n抓到的题行:", *ex, sep="\n  ")
    else:
        print(json.dumps(stats(), ensure_ascii=False, indent=1))
