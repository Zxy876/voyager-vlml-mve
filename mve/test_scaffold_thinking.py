#!/usr/bin/env python3
"""出题器的**模型适配**回归测试（2026-11-09 服务器实测后加）。

跑法：python mve/test_scaffold_thinking.py（服务器无 pytest，同 test_*.py 口径）。

这一层守什么
------------
**出题器必须关掉思维链**，否则整条「教师端同构」链路在生产上是断的。

生产实证（2026-11-09，服务器场景 L 重拆）：
    作废坏骨架 → 触发重拆 → **5 次重试全失败**，
    报「LLM 输出里没有可用维度（解析失败/空列表）」。
逐层取证后定位到根因（**不在 prompt，不在判据**）：

    · GLM-4.6 **默认开着思维链**，思考段与正文**共用 max_tokens**；
    · 出题器的活儿是「把材料翻译成结构化维度」——**不需要深思**，
      但模型的思考把 2400 的预算吃光：
        实测 finish_reason=length、completion_tokens=2400 全是
        reasoning_tokens、**content=0** → 正文一字没有 → 解析必然失败；
    · 传 `thinking={"type":"disabled"}` 后：
        reasoning_tokens=0、content=1781 → 4 条维度、解析成功，
        且维度**全部落在可达表上**（web_facts / web_docs）。

本测试钉死：
    ① 出题器**真的**传了 thinking=disabled（接线在）；
    ② 判官那几处**不许**被顺手关掉思考（它们需要深思）；
    ③ `llm_client.chat` 支持透传 thinking / response_format；
    ④ 「思考吃光预算」要能在失败原因里**看得见**（不笼统报「解析失败」）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    here = Path(__file__).resolve().parent
    ms_src = (here / "maieutic_scaffold.py").read_text(encoding="utf-8")
    lc_src = (here / "llm_client.py").read_text(encoding="utf-8")

    print("== 1. 出题器接线：真的关掉了思考 ==")
    check("maieutic_scaffold._default_llm 里传了 thinking",
          'thinking={"type": "disabled"}' in ms_src
          or "thinking={'type': 'disabled'}" in ms_src)
    # 确认它确实落在 _default_llm 里（不是别处）
    _i = ms_src.find("def _default_llm(")
    _j = ms_src.find("def _call_llm(")
    _seg = ms_src[_i:_j] if (_i >= 0 and _j > _i) else ""
    check("就在 _default_llm 里", "thinking" in _seg, _seg[:120])
    check("max_tokens 仍是 2400（不是靠堆预算糊过去）",
          "max_tokens=2400" in _seg)

    print("== 2. 模型适配层：支持透传 thinking / response_format ==")
    check("llm_client.chat 有 thinking 参数",
          "thinking: dict | None = None" in lc_src)
    check("llm_client.chat 有 response_format 参数",
          "response_format: dict | None = None" in lc_src)
    check("thinking 真的进了请求 body",
          'body["thinking"] = thinking' in lc_src)
    check("response_format 真的进了请求 body",
          'body["response_format"] = response_format' in lc_src)
    check("没把 thinking 硬编码进 chat（各调用点自己决定）",
          'body["thinking"] = {"type": "disabled"}' not in lc_src)

    print("== 3. 判官那几处不许被顺手关思考 ==")
    # 判官需要深思 —— 它们的调用不该带 thinking=disabled
    _judges = {
        "mve/reason_pass.py": "reason_pass",
        "mve/task_critic.py": "task_critic",
        "voyager_vlml/agents/critic.py": "voyager critic",
    }
    for rel, label in _judges.items():
        p = here.parent / rel
        if not p.exists():
            check(f"{label} 文件在（{rel}）", False)
            continue
        s = p.read_text(encoding="utf-8")
        bad = ('thinking={"type": "disabled"}' in s
               or "thinking={'type': 'disabled'}" in s)
        check(f"{label} 没被关思考（它需要深思）", not bad, rel)

    print("== 4. 「思考吃光预算」要看得见（不笼统报解析失败）==")
    check("llm_client 空内容时区分 finish_reason=length",
          'finish_reason' in lc_src and '"length"' in lc_src)
    check("报出思考占了多少 tokens",
          "reasoning_tokens" in lc_src)
    check("maieutic_scaffold 的失败原因里带方向提示",
          "thinking" in ms_src and "max_tokens" in ms_src
          and "解析失败/空列表" in ms_src)

    print("== 5. 不改既有判据语义 ==")
    check("出题器仍然按原版重试 5 次",
          "SCAFFOLD_LLM_RETRIES" in ms_src)
    check("_parse 的容错逻辑没被削弱（围栏/前言仍能解析）",
          "```(?:json)?" in ms_src or "json)?\\s" in ms_src
          or 'r"```' in ms_src)

    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
