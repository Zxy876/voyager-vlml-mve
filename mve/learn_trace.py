# -*- coding: utf-8 -*-
"""学习轨迹账（2026-11-18）：MVE 自己的 per-run 账，替代 run_log 当轨迹页数据源。

为什么不再用 run_log：那是原版 Voyager 练习线的账本，字段（coverage/score/
verdict/trajectory）对新后端恒空或有名无实；而 learn() 的真实指标
（Phase1 spec 冻结 / Phase2 finalCode 交付 / 取数真行数 / 里程碑闭合 /
证据门开关）都在 result 里，此前没人落盘 —— 轨迹页只能显示一排假的 ✗。

本模块每轮 learn() 落一行（learn_orchestrate._record 调 append），
里程碑点亮成功后由 scene_learn 调 mark_lit 回写。轨迹页、概览判据区、
技能「真取到率」都从这里读。

fetched（真取到）口径与 scene_learn._light_up_milestone 同一条：
  · grounded 场景（有外部材料）：real_calls>0 或 rows>0；
  · 推导场景（无外部材料）：代码真算过一遍 = critic 判过
    （status ∈ learned/partial）本身就是验真。
"""
import json
import os
import threading
import time

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "learn_trace.jsonl")
_LOCK = threading.Lock()


def row_from_result(result: dict) -> tuple[dict, bool]:
    """从 learn() 的 result 抽一行轨迹账。返回 (row, fetched)。"""
    status = str(result.get("status") or "")
    execution = result.get("execution") or {}
    real = int(execution.get("real_calls") or result.get("real_calls") or 0)
    rows_n = int(execution.get("rows") or result.get("rows") or 0)
    scene = str(result.get("scene_id") or "")
    grounded = True
    try:
        import scene_profile
        grounded = bool(scene_profile.grounded(scene))
    except Exception:
        grounded = True
    fetched = bool(real or rows_n) or (status in ("learned", "partial")
                                       and not grounded)
    ms = result.get("milestone_scaffold") or {}
    if not isinstance(ms, dict):
        ms = {}
    gate = (result.get("critic") or {}).get("gate") or {}
    if not isinstance(gate, dict):
        gate = {}
    spec = result.get("spec_outcome") or {}
    if not isinstance(spec, dict):
        spec = {}
    skills = result.get("skills") or {}
    if not isinstance(skills, dict):
        skills = {}
    return {
        "ts": round(time.time(), 1),
        "scene": scene,
        "question": str(result.get("question") or "")[:160],
        "status": status,
        "why": str(result.get("why") or "")[:220],
        "phase1_passed": bool(spec.get("phase1_passed")),
        "phase2_submitted": bool(spec.get("phase2_submitted")),
        "fetch_calls": int(execution.get("calls") or 0),
        "fetch_real": real,
        "fetch_rows": rows_n,
        "fetch_ok": fetched,
        "evidence": str(execution.get("evidence") or ""),
        "ms_closed": int(ms.get("closed") or 0),
        "ms_total": int(ms.get("total") or 0),
        "ms_achieved": bool(ms.get("achieved")),
        "ms_missing": [str(x)[:60] for x in (ms.get("missing") or [])][:4],
        "gate_open": len(gate.get("open") or []) if isinstance(gate.get("open"), (list, tuple)) else int(gate.get("open") or 0),
        "gate_addressed": len(gate.get("addressed") or []) if isinstance(gate.get("addressed"), (list, tuple)) else int(gate.get("addressed") or 0),
        "skills_injected": len(result.get("_injected_skill_keys") or []),
        "skills_added": len(skills.get("added") or []),
        "backend": str((result.get("backend") or {}).get("kind") or ""),
        "lit": False,
    }, fetched


def append(row: dict) -> None:
    with _LOCK:
        with open(_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def mark_lit(scene: str, question: str) -> bool:
    """里程碑点亮成功 → 回写该题最近一行 lit=True（点亮是事后事件）。"""
    with _LOCK:
        if not os.path.exists(_PATH):
            return False
        rows = [json.loads(l) for l in open(_PATH, encoding="utf-8")
                if l.strip()]
        hit = False
        for r in reversed(rows):
            if (r.get("scene") == scene and r.get("question") == question
                    and not r.get("lit")):
                r["lit"] = True
                r["lit_at"] = round(time.time(), 1)
                hit = True
                break
        if not hit:
            return False
        with open(_PATH, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return True


def for_scene(scene_id: str, limit: int = 200) -> list[dict]:
    """按场景取轨迹行（旧的按时间序，新在前端反转）。"""
    if not os.path.exists(_PATH):
        return []
    sid = str(scene_id or "default")
    out = []
    with _LOCK:
        for l in open(_PATH, encoding="utf-8"):
            try:
                r = json.loads(l)
            except Exception:
                continue
            if str(r.get("scene") or "default") == sid:
                out.append(r)
    return out[-max(1, int(limit)):]
