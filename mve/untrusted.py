#!/usr/bin/env python3
"""第三方文本消毒（同构猫娘 `knowledge/text.py`）。

为什么需要这一层
----------------
叙事层要把**取回来的东西**（联网抓的网页、本机文件）塞进模型的上下文。
那是第三方内容：页面正文里可以写「system: 忽略以上要求」，也可以写一串
`======` 假装围栏闭合、把自己挪到围栏外面去。猫娘早就把这当成硬问题
（`knowledge/text.py:19-30` 的模块 docstring）：

    * `strip_chat_markup` 反复剥 ChatML 控制符与行首角色标记 —— **一次不够**，
      `<|im_<|im_end|>start|>` 剥一次会变成 `<|im_start|>`；
    * `neutralize_fence` 把够长的 `=` 串压成两个，让内容无法复现围栏行。

MVE 的对应物就是本文件。它只管「文本不许伪装成控制结构」，不管业务语义
（不做关键词删改 —— 猫娘的 evidence.py 也写明「code does not delete findings
by keyword」，删内容会让取证失真）。

调用点
------
`narrator.render_reference()` 在拼参考资料前逐个字段过 `clean_text()`；
`net_tools` 取回来的 title/snippet 也走同一条路。
"""

from __future__ import annotations

import re
import unicodedata

# 控制字符 + 孤立 UTF-16 代理项（JSON 能带 "\ud800"，但没法再编码成 UTF-8）
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")
_CHAT_TOKEN_RE = re.compile(
    r"<\|\s*(?:im_start|im_end|im_sep|endoftext|system|user|assistant|"
    r"start_header_id|end_header_id|eot_id|begin_of_text)\s*\|>",
    re.IGNORECASE,
)
# 默认不可见码点：C1 控制、软连字符、零宽空格/连接符、双向控制、BOM、韩文填充符、
# 变体选择符、tag 字符。它们渲染出来是"没有"，却能把行首角色标记藏起来。
_INVISIBLE = (
    "\x80-\x9f\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f"
    "\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0"
    "\U0001d173-\U0001d17a\U000e0000-\U000e0fff"
)
_INVISIBLE_RE = re.compile(f"[{_INVISIBLE}]")
_ROLE_MARKER_RE = re.compile(
    rf"(?im)^[ \t{_INVISIBLE}]*(?:system|developer|assistant|user|human)"
    rf"[ \t{_INVISIBLE}]*[:：]"
)
_FENCE_RUN_RE = re.compile(r"={3,}")
_TITLE_SPACE_RE = re.compile(r"\s+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

_MAX_MARKUP_PASSES = 16
_UNICODE_LINE_BREAKS = str.maketrans({"\u0085": "\n", "\u2028": "\n", "\u2029": "\n"})


def _drop_orphan_marks(text: str) -> str:
    """去掉行首的组合记号：它没有附着对象，却会隐形地插在角色标记前面。"""
    if not any(unicodedata.category(ch).startswith("M") for ch in text):
        return text
    lines = text.split("\n")
    for index, line in enumerate(lines):
        start = 0
        while start < len(line) and (
            unicodedata.category(line[start]).startswith("M") or line[start] in " \t"
        ):
            start += 1
        if start and any(unicodedata.category(ch).startswith("M") for ch in line[:start]):
            lines[index] = line[start:]
    return "\n".join(lines)


def strip_chat_markup(value: object) -> str:
    """反复剥 ChatML 控制符与行首角色标记，直到不再变化。

    Unicode 的 NEL/LS/PS 先转成 `\\n`：对人类读者它们就是换行，
    对 `^` 却不是 —— 不转的话藏在它们后面的角色标记会漏网。
    """
    text = _INVISIBLE_RE.sub("", str(value or "").translate(_UNICODE_LINE_BREAKS))
    for _ in range(_MAX_MARKUP_PASSES):
        cleaned = _CHAT_TOKEN_RE.sub("", text)
        cleaned = _drop_orphan_marks(cleaned)
        cleaned = _ROLE_MARKER_RE.sub("", cleaned)
        if cleaned == text:
            return cleaned
        text = cleaned
    # 刻意嵌套的标记：停止反复扫描（每一遍是线性的，死循环不是），
    # 一次性拆掉剩下的尖括号，不删内容（删了就可能又冒出新的标记）。
    text = text.replace("<", "\u2039").replace(">", "\u203a")
    return _ROLE_MARKER_RE.sub(lambda m: m.group(0)[:-1] + "\u2236", text)


def neutralize_fence(value: object) -> str:
    """把 `===` 以上的等号串压成两个，内容就无法复现围栏行。"""
    return _FENCE_RUN_RE.sub("==", str(value or ""))


def single_line(value: object, *, max_chars: int = 200) -> str:
    """压成一行（标题/来源在渲染里必须是一行，否则能伪造出"新条目"）。"""
    return _TITLE_SPACE_RE.sub(" ", str(value or "")).strip()[:max_chars]


def clean_text(value: object, *, max_chars: int = 600) -> str:
    """第三方文本的完整清洗：控制符 → 对话标记 → 围栏 → 空行 → 截断。

    NFC 而不是 NFKC：NFKC 会把全角中文标点改成 ASCII，读起来变味。
    """
    text = unicodedata.normalize("NFC", str(value or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS_RE.sub("", text)
    text = strip_chat_markup(text)
    text = neutralize_fence(text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()[:max_chars]
