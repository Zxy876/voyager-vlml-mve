# -*- coding: utf-8 -*-
"""2026-11-18 轨迹/技能账本改造：轨迹页与技能命中率接 MVE 现账。

四件事：
 1. 新账 learn_trace.py + learn_trace.jsonl：一次 learn() 一行，字段 =
    MVE 现在真正看的指标（Phase1 spec 冻结 / Phase2 finalCode 交付 /
    取数真行数 / 里程碑闭合与点亮 / 证据门）。
 2. learn_orchestrate._record：落 learn_trace + 技能 ok 换尺
    （不再等整轮过证据门，而是「注入后真取到数」，与 _light_up_milestone
    的 fetched 口径同一条）；删掉旧的过闸 mark_result 块。
 3. skill_store.retrieve：删掉 0.25*min(hits,6) 命中加成（被注入≠有用，
    奖励它=失败配方越失败越被回注）。
 4. scene_learn：里程碑点亮成功 → learn_trace.mark_lit 回写轨迹行。

面板（dashboard.py / panel_data.py）由 patch_dashboard.py 单独改。
"""
import shutil, sys, os

BASE = "/root/mve/mve"
BAK = ".bak_trace"


def patch(path, pairs, optional=()):
    p = os.path.join(BASE, path)
    src = open(p, encoding="utf-8").read()
    shutil.copy2(p, p + BAK)
    for old, new in pairs:
        if src.count(old) != 1:
            if old in optional:
                print(f"  [skip] {path}: 锚点缺失（optional）")
                continue
            print(f"  [FAIL] {path}: 锚点出现 {src.count(old)} 次（应为 1）")
            print("        锚点前 80 字：", repr(old[:80]))
            sys.exit(1)
        src = src.replace(old, new)
    open(p, "w", encoding="utf-8").write(src)
    print(f"  [ok] {path}")


# ---------- 1. 新模块 learn_trace.py ----------
LEARN_TRACE = r'''# -*- coding: utf-8 -*-
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
        "gate_open": len(gate.get("open") or []),
        "gate_addressed": len(gate.get("addressed") or []),
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
'''

with open(os.path.join(BASE, "learn_trace.py"), "w", encoding="utf-8") as f:
    f.write(LEARN_TRACE)
print("  [ok] learn_trace.py（新建）")

# ---------- 2. learn_orchestrate.py ----------
patch("learn_orchestrate.py", [
    # 2a. _record 里落轨迹账 + 技能 ok 换尺
    (
        '    del _LAST[:-_LAST_MAX]\n'
        '    # ---- run_log 一行（轨迹页可见；写失败不拦学习本身）----',
        '    del _LAST[:-_LAST_MAX]\n'
        '    # ---- 学习轨迹账 + 技能命中率接现账（2026-11-18 用户拍板）----\n'
        '    # 轨迹页不再读 run_log（原版练习线的账，coverage/score 恒空，\n'
        '    # 里程碑点亮了它还记 failed），改读 learn_trace.jsonl：一次 learn()\n'
        '    # 一行，字段 = MVE 现在真正看的指标（Phase1 spec 冻结 / Phase2\n'
        '    # finalCode 交付 / 取数真行数 / 里程碑闭合与点亮 / 证据门开关）。\n'
        '    # 技能「有效」（ok）同步换尺：不再等整轮过证据门，而是「注入后这轮\n'
        '    # 真取到数」（real_calls/rows>0；知识型场景降级为 critic 判过）——\n'
        '    # 与 _light_up_milestone 的 fetched 口径同一条。hits 仍是注入次数\n'
        '    # （⑤ 里 mark_used），ok≤hits 不变量不变。\n'
        '    try:\n'
        '        import learn_trace as _lt\n'
        '        _trow, _fetched = _lt.row_from_result(result)\n'
        '        _lt.append(_trow)\n'
        '        _ik = [k for k in (result.get("_injected_skill_keys") or []) if k]\n'
        '        if _ik and _fetched:\n'
        '            import skill_store as _ssr\n'
        '            _ssr.mark_result(_ik, True)\n'
        '    except Exception:\n'
        '        pass\n'
        '    # ---- run_log 一行（planner/exam 等旧消费方还要读；写失败不拦学习）----',
    ),
    # 2b. 删掉旧的过闸 mark_result 块（记账已统一进 _record）
    (
        '    # 技能有效记账（2026-11-17 补链）：本轮判过（含证据机检闸口放行），\n'
        '    # 注入过的技能按原版语义 mark_result(..., True) —— ok≤hits 不变量由\n'
        '    # mark_result 内部兜底。未判过的路径不记（让低效技能自然沉底）。\n'
        '    try:\n'
        '        import skill_store as _ssr\n'
        '        _ik = [k for k in (out.get("_injected_skill_keys") or []) if k]\n'
        '        if _ik:\n'
        '            _ssr.mark_result(_ik, True)\n'
        '    except Exception:\n'
        '        pass\n',
        '    # 技能有效记账已统一进 _record（2026-11-18）：ok 不再记「整轮过证据\n'
        '    # 门」而是记「注入后真取到数」—— critic 的尺子归 critic，技能的尺子\n'
        '    # 是取数。各 return 路径都过 _record，不会漏记。\n',
    ),
])

# ---------- 3. skill_store.py ----------
patch("skill_store.py", [
    (
        '''      score = 3.0(同题) + 2.5*sim(query, name+text) + 0.25*min(hits,6) + 0.6*(ok/hits)''',
        '''      score = 3.0(同题) + 2.5*sim(query, name+text) + 0.6*(ok/hits)
      （2026-11-18：删掉 0.25*min(hits,6) 命中加成 —— 被注入≠有用，奖励它
       会让失败配方越失败越容易被回注；ok 记「注入后真取到数」。）''',
    ),
    (
        '            score += 2.5 * sim\n'
        '            hits = int(s.get("hits") or 0)\n'
        '            score += 0.25 * min(hits, 6)\n'
        '            score += 0.6 * (int(s.get("ok") or 0) / max(hits, 1))',
        '            score += 2.5 * sim\n'
        '            # 2026-11-18：删掉 0.25*min(hits,6) 奖励项 —— hits 是「被\n'
        '            # 注入次数」，被注入≠有用；奖励它=失败配方越失败越容易被\n'
        '            # 回注（实测：同一条里程碑配方注入 11 次全失败，却靠命中\n'
        '            # 加成霸榜）。质量项只剩 ok/hits，ok 记「注入后真取到数」。\n'
        '            hits = int(s.get("hits") or 0)\n'
        '            score += 0.6 * (int(s.get("ok") or 0) / max(hits, 1))',
    ),
    (
        '    """注入后这一轮判对了 → ok +1。有效性低的技能会在检索里自然沉底。',
        '    """注入后这一轮真取到数（2026-11-18 换尺，原为「判对了」）→ ok +1。\n\n'
        '    尺子换了：critic 的判（证据门）归轨迹账，技能的 ok 记「注入后\n'
        '    这轮真的取到数/真算过」—— 与 _light_up_milestone 的 fetched 口径\n'
        '    同一条。有效性低的技能会在检索里自然沉底。',
    ),
])

# ---------- 4. scene_learn.py ----------
patch("scene_learn.py", [
    (
        '                                cur.update_exploration_progress(\n'
        '                                    {"task": _tgt, "success": True,\n'
        '                                     "critique": ""})\n'
        '                            except Exception as _e:\n'
        '                                _log("[场景学习] 里程碑入 completed 账失败："\n'
        '                                     f"{_e.__class__.__name__}: {_e}")',
        '                                cur.update_exploration_progress(\n'
        '                                    {"task": _tgt, "success": True,\n'
        '                                     "critique": ""})\n'
        '                            except Exception as _e:\n'
        '                                _log("[场景学习] 里程碑入 completed 账失败："\n'
        '                                     f"{_e.__class__.__name__}: {_e}")\n'
        '                            # 轨迹账回写点亮（2026-11-18）：里程碑点亮是\n'
        '                            # 事后事件，learn_trace 那行落盘时还不知道 ——\n'
        '                            # 这里补 lit=True，轨迹页才能显示「已点亮」。\n'
        '                            try:\n'
        '                                import learn_trace as _lt2\n'
        '                                _lt2.mark_lit(scene, _tgt)\n'
        '                            except Exception:\n'
        '                                pass',
    ),
])

print("patch_accounts done")
