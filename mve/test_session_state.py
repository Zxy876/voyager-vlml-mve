#!/usr/bin/env python3
"""第三刀 B：会话 phase 状态机 + append-only 事件流 —— 回归测试。

跑法：服务器 `python mve/test_session_state.py`。

钉死的行为（全部来自原版 maieutic 复核 + 2026-11-09 用户判断）：

 1. 阶段语义照抄原版 `Session.currentPhase`（1 spec / 2 writing / 3 review / 4 closed）
 2. **归档 = completed_at 非空 + phase=4 + 一条 phase_transition 事件**
    （照抄 `advancePhase`，`src/lib/sessions.ts:222-249`；**无归档表/文件/快照**）
 3. 事件流 **append-only**：只追加，从不覆盖旧事件
 4. 阶段**只许前进不许后退**（后退会让归档失效）
 5. 同一阶段重复推进 → 不写事件（原版 `if (from === to) return;`）
 6. phase 3 = 「验收跑完、答案待披露」（用户那句「验收已跑完只是没披露」）
 7. 会话状态住在骨架缓存**同一个文件**里（同一把钥匙）
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

import session_state as SS          # noqa: E402
import req_registry as RR           # noqa: E402

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
    return f"_S{tag}{int(time.time() * 1000) % 100000}"


def _cleanup(sid: str) -> None:
    try:
        p = RR.CKPT / sid
        if p.exists():
            shutil.rmtree(p)
    except Exception:
        pass


# ---------------------------------------------------------------------------
print("\n== 1. 阶段常量照抄原版 currentPhase ==")
# ---------------------------------------------------------------------------
check("phase 1 = 已拆骨架", SS.PHASE_SPEC == 1, str(SS.PHASE_SPEC))
check("phase 2 = 学习中", SS.PHASE_LEARNING == 2, str(SS.PHASE_LEARNING))
check("phase 3 = 已验待披露", SS.PHASE_VERIFIED == 3, str(SS.PHASE_VERIFIED))
check("phase 4 = 已闭合归档", SS.PHASE_CLOSED == 4, str(SS.PHASE_CLOSED))
check("四个阶段名齐",
      set(SS.PHASE_NAMES) == {1, 2, 3, 4}, str(SS.PHASE_NAMES))


# ---------------------------------------------------------------------------
print("\n== 2. 归档 = completed_at 非空 + phase=4 + 一条 phase_transition ==")
# ---------------------------------------------------------------------------
d: dict = {}
r = SS.mark_archived(d, reason="测试")
check("归档成功", r["ok"] is True, str(r))
st = SS.read_session(d)
check("phase = 4", st["phase"] == 4, str(st["phase"]))
check("completed_at 非空", st["completed_at"] > 0, str(st["completed_at"]))
check("archived = True（completed_at 非空即归档）", st["archived"] is True, "")
kinds = [e.get("kind") for e in st["events"]]
check("一条 phase_transition 事件", kinds == ["phase_transition"], str(kinds))
check("事件带 from/to",
      st["events"][0].get("payload", {}).get("from") in (0, 1, 2, 3)
      and st["events"][0].get("payload", {}).get("to") == 4,
      str(st["events"][0]))
# 原版没有归档表/文件/拷贝 → 数据里不该冒出 archive 字段
check("不产生归档表/快照字段（照抄原版：无归档表）",
      not any(k in d for k in ("archive", "archived_snapshot", "archive_path")),
      str(list(d.keys())))


# ---------------------------------------------------------------------------
print("\n== 3. 事件流 append-only ==")
# ---------------------------------------------------------------------------
d2: dict = {}
SS.append_event(d2, "dim_closed", {"dim_id": "d1", "r": 1.0})
SS.append_event(d2, "dim_closed", {"dim_id": "d2", "r": 0.8})
SS.note_dim_closed(d2, "d3", r=0.5, how="tool")
evs = SS.read_session(d2)["events"]
check("三条事件（只追加）", len(evs) == 3, str(len(evs)))
check("顺序保持", [e["payload"].get("dim_id") for e in evs] == ["d1", "d2", "d3"],
      str([e.get("payload") for e in evs]))
check("每条事件带时间戳", all(e.get("at", 0) > 0 for e in evs), "")
check("note_dim_closed 记 how",
      evs[-1]["payload"].get("how") == "tool", str(evs[-1]))
# 再追加一条 → 旧的**不被改**
old_first = dict(evs[0])
SS.append_event(d2, "dim_closed", {"dim_id": "d4"})
evs2 = SS.read_session(d2)["events"]
check("追加后旧事件逐字不变（append-only）",
      evs2[0] == old_first, f"{evs2[0]} vs {old_first}")
check("追加后事件变 4 条", len(evs2) == 4, str(len(evs2)))


# ---------------------------------------------------------------------------
print("\n== 4. 阶段只许前进不许后退 ==")
# ---------------------------------------------------------------------------
d3: dict = {}
SS.set_phase(d3, SS.PHASE_LEARNING)
SS.set_phase(d3, SS.PHASE_VERIFIED)
r_back = SS.set_phase(d3, SS.PHASE_SPEC)
check("后退被拒", r_back["ok"] is False, str(r_back))
check("phase 仍是 3", SS.read_session(d3)["phase"] == 3, "")
# 事件数：ensure_session 建 phase=1 → set_phase(2) 一条 → set_phase(3) 一条
# → 后退被拒不写 → 仍是 2 条（★ 后退**不许**产生事件）
n_ev = len(SS.read_session(d3)["events"])
check("后退不写事件", n_ev == 2, str(n_ev))
check("事件全是 phase_transition（没有倒退记录）",
      all(e.get("kind") == "phase_transition"
          for e in SS.read_session(d3)["events"]),
      str([e.get("kind") for e in SS.read_session(d3)["events"]]))
r_fwd = SS.set_phase(d3, SS.PHASE_CLOSED)
check("前进到 4 允许", r_fwd["ok"] is True, str(r_fwd))


# ---------------------------------------------------------------------------
print("\n== 5. 同一阶段重复推进 → 不写事件（照抄原版）==")
# ---------------------------------------------------------------------------
d4: dict = {}
SS.set_phase(d4, SS.PHASE_LEARNING)
n1 = len(SS.read_session(d4)["events"])
SS.set_phase(d4, SS.PHASE_LEARNING)
n2 = len(SS.read_session(d4)["events"])
check("重复推进不写事件", n1 == n2, f"{n1} → {n2}")


# ---------------------------------------------------------------------------
print("\n== 6. phase 3 = 验收跑完待披露 ==")
# ---------------------------------------------------------------------------
d5: dict = {}
SS.mark_verified(d5, reason="验收跑完")
st5 = SS.read_session(d5)
check("mark_verified → phase 3", st5["phase"] == 3, str(st5["phase"]))
check("phase 3 不算归档（completed_at 还空）", st5["archived"] is False, "")
check("phase 名 = 已验待披露", st5["phase_name"] == "已验待披露",
      st5["phase_name"])


# ---------------------------------------------------------------------------
print("\n== 7. 接线：闭合维度 → 事件 + phase 3；里程碑达成 → 归档 ==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("b")
try:
    import maieutic_scaffold as MS
    q = "会话接线测试题"
    RR.register(sid, q)
    p = MS._cache_path(sid, q)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"question": q,
                             "dimensions": [{"id": "d1", "question": "一"}]},
                            ensure_ascii=False), encoding="utf-8")
    # 闭合一条维度（写 codes → 应触发事件 + phase 3）
    MS.save_progress_codes(sid, q, [{"id": "d1", "question": "一",
                                      "code": "SELECT 1",
                                      "answer": '{"rows": [[1]]}',
                                      "r": 1.0, "how": "tool"}])
    data = json.loads(p.read_text(encoding="utf-8"))
    st7 = SS.read_session(data)
    check("闭合后 phase = 3（已验待披露）", st7["phase"] == 3, str(st7["phase"]))
    kinds7 = [e.get("kind") for e in st7["events"]]
    check("有 dim_closed 事件", "dim_closed" in kinds7, str(kinds7))
    check("有 phase_transition 事件", "phase_transition" in kinds7, str(kinds7))
    # 里程碑达成 → 归档
    MS.mark_milestone_achieved(sid, q, fetched=True)
    data2 = json.loads(p.read_text(encoding="utf-8"))
    st7b = SS.read_session(data2)
    check("里程碑达成 → phase 4", st7b["phase"] == 4, str(st7b["phase"]))
    check("里程碑达成 → completed_at 非空", st7b["completed_at"] > 0, "")
    check("里程碑达成 → archived", st7b["archived"] is True, "")
    # 会话状态与骨架**同住一个文件**（同一把钥匙）
    check("会话状态在骨架文件里（不另开存储）",
          "session" in data2, str(list(data2.keys())))
    # 事件流没被覆盖：dim_closed 还在
    check("归档后 dim_closed 事件仍在（append-only）",
          "dim_closed" in [e.get("kind") for e in st7b["events"]],
          str([e.get("kind") for e in st7b["events"]]))
finally:
    _cleanup(sid)


print(f"\n=== 刀 B 回归：{PASS} 过 / {FAIL} 败 ===")
sys.exit(1 if FAIL else 0)
