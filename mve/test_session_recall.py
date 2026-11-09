#!/usr/bin/env python3
"""第三刀 C/D：召回会话而非技能 + 披露已验答案 —— 回归测试。

跑法：服务器 `python mve/test_session_recall.py`。

钉死的行为：

 C. 召回（照抄原版 `findOrCreateSession` / `resolveSession`）
 1. 按 req id **精确**召回（不是按问句向量检索）
 2. 换问法 → 归题找回同一个 req → **召回同一个会话**
 3. 召回结果带：骨架维度 / 已闭合集合 / 已验答案（code+answer+r+how）
 4. 找不到 → 如实 found=False，**不拿别的需求的会话顶替**
 5. 全部闭合 → is_learned=True；否则给出未闭合维度清单

 D. 披露
 6. 只披露**验过真值**的（有 answer/code 且 r >= 0）
 7. **原样披露**：不改写、不摘要、不补全
 8. 会话没闭合 → 不整题披露（缺的那部分还没验过）
 9. 派任务与验收分开：学过（有已验答案）→ **不派任务**
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import req_registry as RR           # noqa: E402
import session_recall as SR         # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {detail}")


def _tmp_scene(tag: str) -> str:
    return f"_R{tag}{int(time.time() * 1000) % 100000}"


def _cleanup(sid: str) -> None:
    try:
        p = RR.CKPT / sid
        if p.exists():
            shutil.rmtree(p)
    except Exception:
        pass


def _write_session(sid: str, q: str, dims: list[dict], learned: list[str],
                   codes: list[dict]) -> tuple[str, Path]:
    reg = RR.register(sid, q)
    rid = reg["req_id"]
    import maieutic_scaffold as MS
    p = MS._cache_path(sid, q)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "question": q, "req_id": rid, "dimensions": dims,
        "progress": {"learned": learned, "codes": codes},
    }, ensure_ascii=False), encoding="utf-8")
    return rid, p


DIMS2 = [{"id": "d1", "question": "一", "landing": "t1"},
         {"id": "d2", "question": "二", "landing": "t1"}]
CODES2 = [
    {"id": "d1", "question": "一", "code": "SELECT 1",
     "answer": '{"rows": [[1]]}', "tools": ["query_sql"], "r": 1.0,
     "how": "tool"},
    {"id": "d2", "question": "二", "code": "SELECT 2",
     "answer": '{"rows": [[2]]}', "tools": ["query_sql"], "r": 0.9,
     "how": "tool"},
]


# ---------------------------------------------------------------------------
print("\n== 1. 按 req id 精确召回 ==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("c")
try:
    q = "召回测试题"
    rid, p = _write_session(sid, q, DIMS2, ["d1", "d2"], CODES2)
    s = SR.recall(rid, sid)
    check("召回成功", s["found"] is True, str(s.get("why")))
    check("req_id 对上", s["req_id"] == rid, f"{s['req_id']} vs {rid}")
    check("带回骨架维度（2 条）", len(s["dims"]) == 2, str(len(s["dims"])))
    check("维度 id 对",
          [d["id"] for d in s["dims"]] == ["d1", "d2"],
          str([d.get("id") for d in s["dims"]]))
    check("带回闭合账", s["learned"] == ["d1", "d2"], str(s["learned"]))
    check("带回已验答案（2 条）", len(s["codes"]) == 2, str(list(s["codes"])))
    check("答案带 r 与 how",
          s["codes"]["d1"]["r"] == 1.0 and s["codes"]["d1"]["how"] == "tool",
          str(s["codes"].get("d1")))
    # 找不到 → 如实 false
    s2 = SR.recall("req_不存在", sid)
    check("找不到 → found=False", s2["found"] is False, str(s2))
    check("找不到 → 如实说为什么", bool(s2.get("why")), str(s2.get("why")))
    check("找不到 → 不返回别人的维度", s2["dims"] == [], str(s2["dims"]))
finally:
    _cleanup(sid)


# ---------------------------------------------------------------------------
print("\n== 2. 换问法 → 召回同一个会话 ==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("d")
try:
    orig = "帮我查查用户名 ce 的这位女生都活跃在哪些社交平台上"
    alt = "查一下ce这个用户都活跃在哪些社交平台上"
    rid, p = _write_session(sid, orig, DIMS2, ["d1", "d2"], CODES2)
    # 归位：换个问法 → 同一个需求
    RR.note_question(sid, alt, rid)
    s = SR.recall_by_question(alt, sid)
    check("换问法召回成功", s["found"] is True, str(s.get("why")))
    check("换问法 → 同一个 req", s["req_id"] == rid, f"{s['req_id']} vs {rid}")
    check("换问法 → 维度一致",
          [d["id"] for d in s["dims"]] == ["d1", "d2"],
          str([d.get("id") for d in s["dims"]]))
    check("换问法 → 已验答案还在", len(s["codes"]) == 2, str(list(s["codes"])))
finally:
    _cleanup(sid)


# ---------------------------------------------------------------------------
print("\n== 3. is_learned / unclosed_dims（派任务与验收的分界）==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("e")
try:
    q = "闭合判定测试题"
    rid, p = _write_session(sid, q, DIMS2, ["d1"], CODES2)
    s = SR.recall(rid, sid)
    check("部分闭合 → is_learned=False", SR.is_learned(s) is False, "")
    check("未闭合维度 = d2", SR.unclosed_dims(s) == ["d2"],
          str(SR.unclosed_dims(s)))
    # 全闭合
    import maieutic_scaffold as MS
    MS.save_progress(sid, q, ["d2"])
    s2 = SR.recall(rid, sid)
    check("全闭合 → is_learned=True", SR.is_learned(s2) is True, "")
    check("全闭合 → 无未闭合维度", SR.unclosed_dims(s2) == [], "")
    # 无骨架 → 不算达成（照抄 milestone_state：0 维度不构成里程碑）
    rid3, p3 = _write_session(sid, "无骨架题", [], [], [])
    s3 = SR.recall(rid3, sid)
    check("无维度 → is_learned=False", SR.is_learned(s3) is False, "")
finally:
    _cleanup(sid)


# ---------------------------------------------------------------------------
print("\n== 4. 披露判据：只披露验过真值的 ==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("f")
try:
    q = "披露判据测试题"
    dims = [{"id": "d1", "question": "一"},
            {"id": "d2", "question": "二"},
            {"id": "d3", "question": "三"}]
    codes = [
        {"id": "d1", "code": "SELECT 1", "answer": '{"rows": [[1]]}',
         "r": 1.0, "how": "tool"},
        {"id": "d2", "code": "SELECT 2", "answer": '{"rows": [[2]]}',
         "r": -1.0, "how": ""},              # 无评分 → 不认
        {"id": "d3", "code": "", "answer": "", "r": 0.5, "how": ""},
        # d3 上面那行：既没 code 也没 answer → 不认
    ]
    rid, p = _write_session(sid, q, dims, ["d1", "d2", "d3"], codes)
    s = SR.recall(rid, sid)
    v = SR.verified_answers(s)
    check("只披露 d1（有 answer + r>=0）", list(v.keys()) == ["d1"],
          str(list(v.keys())))
    check("d2（r=-1 无评分）不认", "d2" not in v, str(list(v.keys())))
    check("d3（既没 code 也没 answer）不认", "d3" not in v, str(list(v.keys())))
    check("原样披露（answer 逐字不变）",
          v["d1"]["answer"] == '{"rows": [[1]]}', str(v["d1"]["answer"]))
    check("披露带 r 与 how",
          v["d1"]["r"] == 1.0 and v["d1"]["how"] == "tool", str(v["d1"]))
finally:
    _cleanup(sid)


# ---------------------------------------------------------------------------
print("\n== 5. 消费侧接线：披露短路 + 不重复派任务 ==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("g")
try:
    src = (_ROOT / "voyager_vlml" / "consume.py").read_text(encoding="utf-8")
    check("消费侧按 req id 召回会话",
          "session_recall" in src and "recall_by_question" in src, "")
    check("消费侧披露已验答案",
          "disclosed_from_session" in src, "")
    check("披露前检查全闭合（is_learned）",
          "_sr.is_learned(_sess)" in src, "")
    check("学过 → 不派任务（派任务与验收分开）",
          "不重复派任务" in src, "")
    check("未闭合维度如实交代",
          "未闭合维度" in src, "")
finally:
    _cleanup(sid)


print(f"\n=== 刀 C/D 回归：{PASS} 过 / {FAIL} 败 ===")
sys.exit(1 if FAIL else 0)
