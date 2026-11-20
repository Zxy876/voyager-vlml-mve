#!/usr/bin/env python3
import re, json, sys
sys.path.insert(0, "/root/mve/mve")
import inspect, task_critic
src = inspect.getsource(task_critic._verify_claim_gate)
for ln in src.splitlines():
    if "re.search" in ln:
        print("gate 正则:", ln.strip())
s = '{"refs": [{"file": "skill_store.py", "line": 99999, "expect": "def add("]}'
answer = "```mve_verify " + s + " ```"
m = re.search(r"```mve_verify\s*(.*?)\s*```", answer, re.S)
print("captured:", repr(m.group(1)) if m else None)
try:
    json.loads(m.group(1))
    print("json ok")
except Exception as e:
    print("json err:", e)
