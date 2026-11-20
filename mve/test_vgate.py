#!/usr/bin/env python3
"""场景执行门（_verify_claim_gate）五种情形单元验证。"""
from task_critic import _verify_claim_gate as g

def blk(s):
    return "```mve_verify " + s + " ```"

cases = [
    ("无证据块", "S", {"answer": "我学会了 skill_store 的防膨胀规矩，add() 同名覆盖，retrieve top_k=5"}),
    ("假行号", "S", {"answer": "依据如下 " + blk('{"refs": [{"file": "skill_store.py", "line": 99999, "expect": "def add("}]}')}),
    ("真证据", "S", {"answer": "依据 " + blk('{"refs": [{"file": "skill_store.py", "line": 310, "expect": "def add("}, {"file": "skill_store.py", "line": 2, "expect": "SkillManager"}]}')}),
    ("P 直通", "P", {"answer": "没有证据块"}),
    ("路径逃逸", "S", {"answer": "证据 " + blk('{"refs": [{"file": "../../etc/passwd", "line": 1}]}')}),
    ("坏 JSON", "S", {"answer": "证据 " + blk('{"refs": [broken')}),
]
for name, scene, result in cases:
    ok, why = g(scene, result)
    print(f"{name}: ok={ok}" + (f" | {why[:70]}" if why else ""))
