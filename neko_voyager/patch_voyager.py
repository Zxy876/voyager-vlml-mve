#!/usr/bin/env python3
"""把**原版 Voyager** 搬进项目，并把四个 agent 的 LLM 换到猫娘的模型。

改法（只动 import 一行，调用点不动）：
    -from langchain.chat_models import ChatOpenAI
    +try:                       # 猫娘宿主内 → 用猫娘配置的模型
    +    from voyager.neko_model import ChatOpenAI
    +except Exception:          # 脱离宿主 → 原版行为（环境 OPENAI_API_KEY）
    +    from langchain.chat_models import ChatOpenAI

为什么只改 import：原版四个 agent 的调用点全是
`ChatOpenAI(model_name=..., temperature=..., request_timeout=...)`，
`neko_model.ChatOpenAI` 用了**同样**的签名（model_name / request_timeout），
所以调用点一行都不用动 —— 这是"接入"而不是"重写"的判据。

`model_name` 在猫娘里不生效（模型由宿主配置决定），这是有意的。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

SRC = Path("/Users/zxydediannao/Documents/想法一页纸/repos/Voyager")
HERE = Path(__file__).resolve().parent
DST = HERE.parent / "voyager_original"
BRIDGE = HERE / "neko_model.py"

OLD = "from langchain.chat_models import ChatOpenAI"
NEW = (
    "try:  # 猫娘宿主内：用猫娘配置的模型（utils.config_manager）\n"
    "    from voyager.neko_model import ChatOpenAI\n"
    "except Exception:  # 脱离宿主：原版行为（环境 OPENAI_API_KEY）\n"
    "    from langchain.chat_models import ChatOpenAI"
)

AGENTS = [
    "voyager/agents/action.py",
    "voyager/agents/critic.py",
    "voyager/agents/curriculum.py",
    "voyager/agents/skill.py",
]


def main() -> int:
    if not SRC.is_dir():
        print(f"❌ 找不到原版 Voyager：{SRC}")
        return 1
    if DST.exists():
        shutil.rmtree(DST)
    shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    print(f"已复制 {SRC.name} → {DST}")

    shutil.copy2(BRIDGE, DST / "voyager" / "neko_model.py")
    print(f"已放入桥接模块 → {DST / 'voyager' / 'neko_model.py'}")

    changed = 0
    for rel in AGENTS:
        path = DST / rel
        text = path.read_text(encoding="utf-8")
        if OLD not in text:
            print(f"⚠️  {rel}：没找到 {OLD!r}，跳过")
            continue
        n = text.count(OLD)
        text = text.replace(OLD, NEW)
        path.write_text(text, encoding="utf-8")
        changed += n
        print(f"✅ {rel}：改了 {n} 处 import")
    print(f"\n合计 {changed} 处 import 已接到猫娘模型。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
