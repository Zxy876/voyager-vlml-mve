#!/usr/bin/env python3
"""学习完成 → 自动回访（2026-10-29 用户拍板）。

用户原话的痛点：「缺一个信号 —— MVE 什么时候学好了、可以来消费了；学好了
就在对话里重新回答人排队待解决的问题，不需要人去总是调试帮它学习（浪费时间）」。

三件套：
  ① **排队**：consume 没答出来且已自动拉起学习时，把 (场景, 问题) 记进
     `revisit_queue.json`（原子写；同一题只记一次）。
  ② **信号**：一轮学习真的学出东西（status ∈ learned/partial）→ 学习线程
     收尾处调 `revisit_pending(场景)` —— 这就是「学好了」的信号，不需要人盯。
  ③ **重答**：逐条重跑 consume（`auto_learn=False`，失败不连环拉学习，防
     回访自己把自己又送去学习）。答出来（judgment=written）→ 出队 + 落
     `revisit_log.json`；仍答不出 → attempts+1，满 `MAX_ATTEMPTS` 出队
     （同一道题最多自动试 3 次，不无限烧钱）。

结果去哪看：`revisit_log` 里 `delivered=False` 的条目由对话页
`GET /api/revisit` 拉走展示（「🔁 学习完成后自动回访」气泡）并标记已送达；
轨迹页同时有 `mode=revisit` 的 run_log 行。

⚠️ 回访是**增强**不是**依赖**：任何一步挂（LLM 挂 / consume 异常 / 文件损坏）
都不许影响学习本身 —— 全部 try/except 兜住、损坏文件改名留证后重建。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
QUEUE = HERE / "revisit_queue.json"
LOG = HERE / "revisit_log.json"
MAX_ATTEMPTS = 3          # 同一道题最多自动回访次数（防无限烧钱）
MAX_LOG = 120             # 回访结果最多留多少条

try:                       # 测试要能替换；真实运行用全局单例
    import run_log as _runlog
except Exception:          # pragma: no cover
    _runlog = None

_LOCK = __import__("threading").Lock()
_RUNNING = False


def _norm_q(q: str) -> str:
    return " ".join(str(q or "").split())


def _key(scene: str, q: str) -> str:
    return f"{scene}|{_norm_q(q)}"


def _load(path: Path, default: Any) -> Any:
    """读 JSON；missing → default，broken → 改名留证后按 default 重建。"""
    if not path.exists():
        return default
    try:
        obj = json.loads(path.read_text("utf-8"))
        return obj if isinstance(obj, type(default)) else default
    except Exception:
        try:
            path.rename(path.with_suffix(
                path.suffix + f".broken.{int(time.time())}"))
        except Exception:
            pass
        return default


def _save(path: Path, obj: Any) -> None:
    """原子写（tmp + os.replace）—— 与 scenes.json 同款纪律。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, path)


def mark_pending(scene_id: str, question: str) -> None:
    """consume 没答出来且学习已拉起 → 排进回访队列（同一题只记一次）。"""
    try:
        q = _norm_q(question)
        if not q:
            return
        data = _load(QUEUE, {})
        k = _key(str(scene_id or "A"), q)
        if k in data:
            data[k]["ts"] = round(time.time(), 1)   # 最近一次没答上的时间
        else:
            data[k] = {"scene": str(scene_id or "A"), "question": q,
                       "attempts": 0, "ts": round(time.time(), 1)}
        _save(QUEUE, data)
    except Exception:
        pass                       # 排队失败不影响消费/学习主链路


def pending(scene_id: str = "") -> list[dict[str, Any]]:
    """还没解决、且没试满 MAX_ATTEMPTS 的题（旧→新排序）。"""
    data = _load(QUEUE, {})
    out = [dict(v, key=k) for k, v in data.items()
           if int((v or {}).get("attempts") or 0) < MAX_ATTEMPTS
           and (not scene_id or str((v or {}).get("scene") or "") == scene_id)]
    out.sort(key=lambda e: float((e or {}).get("ts") or 0))
    return out


def _log_result(entry: dict[str, Any]) -> None:
    try:
        log = _load(LOG, [])
        log.append(entry)
        _save(LOG, log[-MAX_LOG:])
    except Exception:
        pass


def deliver(scene_id: str = "") -> list[dict[str, Any]]:
    """给对话页拉走的「已解决但还没看过」的回访结果（拉走即标记已送达）。"""
    log = _load(LOG, [])
    out: list[dict[str, Any]] = []
    changed = False
    for e in log:
        if e.get("delivered") or not e.get("ok"):
            continue
        if scene_id and str(e.get("scene") or "") != scene_id:
            continue
        out.append(e)
        e["delivered"] = True
        changed = True
    if changed:
        try:
            _save(LOG, log)
        except Exception:
            pass
    return out


def revisit_pending(scene_id: str, limit: int = 2) -> dict[str, Any]:
    """信号触发的自动回访：逐条重跑 consume，答出来就出队并留痕。

    在学习线程/学习子进程收尾处调用 —— 「学好了」的信号就是这次学习
    真的写了技能（status ∈ learned/partial）。消费侧用 `auto_learn=False`：
    回访失败**不**再连环拉学习（防回访↔学习互相触发转圈烧钱）。
    ⚠️ 重入保护：多轮学习连着完成时可能同时拉起多个回访线程 —— 同一时刻
    只跑一个，后来的直接跳过（队列里的题下一轮学习结束还会再试）。
    """
    global _RUNNING
    if _RUNNING:
        return {"checked": 0, "resolved": 0, "skipped": "已有回访在跑"}
    items = pending(scene_id)[:max(1, int(limit or 1))]
    if not items:
        return {"checked": 0, "resolved": 0}
    with _LOCK:
        if _RUNNING:                       # 双检：等锁的间隙可能已被拉起
            return {"checked": 0, "resolved": 0, "skipped": "已有回访在跑"}
        _RUNNING = True
    try:
        return _revisit_run(items, scene_id)
    finally:
        _RUNNING = False


def _revisit_run(items: list[dict[str, Any]], scene_id: str) -> dict[str, Any]:
    # 场景还原（同 _mve_ask 的纪律）：回访属于别的场景的题时先切过去。
    _prev = ""
    try:
        import scenes as _sc
        _prev = str(_sc.current_scene_id() or "")
        _want = str(items[0].get("scene") or scene_id or "")
        if _want and _want != _prev:
            _sc.switch_scene(_want)
    except Exception:
        _prev = ""
    resolved = 0
    try:
        import asyncio as _aio
        try:
            # 导入失败（路径/依赖问题）→ 环境问题，**不烧 attempts**：
            # 那会让人以为「学不会」，其实是「没跑起来」。
            import voyager_vlml.consume as _C
        except Exception:
            return {"checked": 0, "resolved": 0, "why": "consume 导入失败"}
        for e in items:
            q = str(e.get("question") or "")
            r: dict[str, Any] = {}
            try:
                r = _aio.run(_C.consume(q, auto_learn=False)) or {}
            except Exception as ex:
                r = {"judgment_result": "", "answer": "",
                     "errors": [f"{type(ex).__name__}: {ex}"]}
            ok = str(r.get("judgment_result") or "") == "written"
            data = _load(QUEUE, {})
            k = str(e.get("key") or _key(str(e.get("scene") or scene_id), q))
            if ok:
                data.pop(k, None)
                resolved += 1
            elif k in data:
                data[k]["attempts"] = int(data[k].get("attempts") or 0) + 1
                data[k]["ts"] = round(time.time(), 1)
                if int(data[k]["attempts"]) >= MAX_ATTEMPTS:
                    data.pop(k, None)   # 试满了：留在失败账上，不再自动试
            try:
                _save(QUEUE, data)
            except Exception:
                pass
            entry = {
                "scene": str(e.get("scene") or scene_id or ""),
                "question": q,
                "ok": ok,
                "verdict": str(r.get("judgment_result") or "failed"),
                "answer": str(r.get("answer") or "")[:400],
                "ts": round(time.time(), 1),
                "delivered": False,
            }
            _log_result(entry)
            try:
                if _runlog is not None:
                    _runlog.append({
                        "mode": "revisit",
                        "scene": entry["scene"],
                        "topic_id": "", "task": q[:60], "question": q,
                        "round": int(e.get("attempts") or 0) + 1,
                        "hint": "学习完成后自动回访",
                        "coverage": None, "score": None,
                        "verdict": "ok" if ok else "still_failed",
                        "success": ok, "done": ok,
                        "evidence_status": "collected" if ok else "none",
                        "judge": "consume_deterministic",
                        "no_tool_calls": False, "feedback": {},
                        "tool_calls": 0,
                        "errors": [] if ok else ["回访仍答不出"],
                        "covered": [], "missing": [] if ok else [q[:60]],
                        "narrative": entry["answer"][:200],
                        "generated_task": False, "sql": "",
                        "skill_key": "", "reused_skills": [],
                        "learn_status": "", "backend": "",
                    })
            except Exception:
                pass
    finally:
        try:
            import scenes as _sc2
            if _prev and str(_sc2.current_scene_id() or "") != _prev:
                _sc2.switch_scene(_prev)
        except Exception:
            pass
    return {"checked": len(items), "resolved": resolved}
