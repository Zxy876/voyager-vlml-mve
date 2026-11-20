import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
import skill_store
import run_log
import learn_trace
import learn_orchestrate

tmp = Path(tempfile.mkdtemp())
skill_store.STORE = tmp / "ss.json"
shutil.copy("skill_store.json", str(skill_store.STORE))
skill_store._RAW_CACHE["sig"] = None
skill_store._RAW_CACHE["data"] = None
run_log.LOG = tmp / "rl.jsonl"
learn_trace._PATH = str(tmp / "lt.jsonl")

key = sorted(skill_store._load_raw()["skills"])[1]


def get(k):
    d = skill_store._load_raw()
    s = d["skills"][k]
    return (int(s.get("hits") or 0), int(s.get("runs") or 0), int(s.get("ok") or 0))


before = get(key)

base = {"scene_id": "S", "question": "冒烟题", "status": "learned",
        "_injected_skill_keys": [key], "backend": {"kind": "local"},
        "spec_outcome": {"phase1_passed": True, "phase2_submitted": True},
        "execution": {"calls": 3, "real_calls": 1, "rows": 2}}

# ① 进了执行且真取到数 → runs+1 ok+1
learn_orchestrate._record(dict(base))
mid = get(key)
print("① 进执行+取到数:", before, "->", mid,
      "OK" if mid[1] == before[1] + 1 and mid[2] == before[2] + 1 else "FAIL")

# ② Phase1 就死（没进执行）→ runs / ok 都不动
dead = dict(base, status="failed", _injected_skill_keys=[key],
            spec_outcome={"phase1_passed": False, "phase2_submitted": False},
            execution={"calls": 0, "real_calls": 0, "rows": 0})
learn_orchestrate._record(dead)
after = get(key)
print("② Phase1 就死:", mid, "->", after,
      "OK" if after[1] == mid[1] and after[2] == mid[2] else "FAIL")

# ③ 进了执行但没取到数 → runs+1、ok 不动
nof = dict(base, status="failed", _injected_skill_keys=[key],
           execution={"calls": 2, "real_calls": 0, "rows": 0})
learn_orchestrate._record(nof)
last = get(key)
print("③ 进执行没取到:", after, "->", last,
      "OK" if last[1] == after[1] + 1 and last[2] == after[2] else "FAIL")

rows = [json.loads(l) for l in open(learn_trace._PATH) if l.strip()]
print("trace rows:", len(rows), "| 字段 runs 口径依赖:", rows[0]["phase2_submitted"], rows[0]["fetch_calls"])
print("临时目录:", tmp)
