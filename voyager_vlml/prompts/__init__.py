"""prompt 文件加载（同构 `voyager_original/voyager/prompts/__init__.py`）。

原版把 prompt 放在 `voyager/prompts/*.txt` 用 `load_prompt("critic")` 读，
不塞进 .py 字符串里 —— 这样 prompt 能单独改、单独对照。这里照做。
"""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    p = _DIR / f"{name}.txt"
    if not p.exists():
        raise FileNotFoundError(f"没有这个 prompt：{p}")
    return p.read_text(encoding="utf-8")
