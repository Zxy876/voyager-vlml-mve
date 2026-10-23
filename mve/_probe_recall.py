"""诊断：这条 task 的问题，为什么召回不到已学配方。"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for p in (str(_ROOT), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import skill_store  # noqa: E402

Q = "Karmine Corp 在 Sunset 上先拿到首杀的回合里赢了多少比例"
raw = skill_store._load_raw().get("skills") or {}

print("== 库里的技能 ==")
for k, s in raw.items():
    print(f"  {k}  topic={s.get('topic')}")
    print(f"    text={str(s.get('text'))[:70]}")
    print(f"    code={str(s.get('code'))[:140]}")

print("\n== 不同 topic 键的召回 ==")
for t in ["", "vlml_fb_conversion", "vlml_first_blood"]:
    got = skill_store.retrieve(t, Q, top_k=4)
    print(f"  topic={t!r:24s} -> {[(s['key'], s['score']) for s in got]}")

print("\n== 词面相似度 vs 跨题门槛 ==")
thr = getattr(skill_store, "CROSS_TOPIC_MIN_SIM", None)
print(f"  CROSS_TOPIC_MIN_SIM = {thr}")
for k, s in raw.items():
    hay = f"{s.get('name') or ''} {s.get('text') or ''}"
    print(f"  {k}: sim={skill_store.similarity(Q, hay):.3f}")
