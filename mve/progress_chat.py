#!/usr/bin/env python3
"""进度问答 chat —— 人在面板上问「MVE 现在学到哪了」（2026-11-18）。

从 maieutic 抽的同构位：原版学生在 IDE chat 里问自己的代码；教师侧看
学习进度。这里给**人**一个只读的进度问答通道 —— 回答只转述服务端已
落账的状态，不推断、不补数、**不做任何判定**（同 chat_page 铁律：
不长第二把尺子，掌握度永远由消费侧算）。

与 phase2_chat 的边界（红线，一条不许破）：
  · phase2 chat 是**学生内问**，opusMode 游程喂 r_site / 档 B 封点 ——
    人的消息**绝不进那本账**（C4 只 append 学生 exchange、C3 mode 由
    chat 判，两处都不许被人侧污染）。
  · 本模块**零写账**：不 append exchange、不动 inventory、不动节点、
    不写技能库。读坏了一段就如实标「拿不到」，不猜。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------- 状态采集
def _pilot_state() -> str:
    # 2026-11-19 补：缺 pilot_state.json 是正常停机态（pilot.py:115-120 的
    # 口径就是 running=False + "从未启动过"），不是"拿不到"—— 清库/新装后
    # AI 读账不该报 FileNotFoundError 吓人。
    if not (ROOT / "pilot_state.json").exists():
        return "循环状态：未运行（从未启动过）"
    try:
        d = json.loads((ROOT / "pilot_state.json").read_text("utf-8"))
        return (f"循环状态：{'运行中' if d.get('running') else '已停止'}"
                f"（{d.get('job_label') or ''}，迭代 {d.get('iterations')} 次"
                + (f"，上次退出码 {d.get('last_exit')}" if d.get("last_exit") is not None else "")
                + (f"，{d.get('why')}" if d.get("why") else "") + "）")
    except Exception as e:
        return f"循环状态：拿不到（{type(e).__name__}）"


def _backpack(scene: str) -> str:
    try:
        inv = json.loads((ROOT / "ckpt_curriculum" / scene / "inventory.json").read_text("utf-8"))
        slots = inv.get("slots") or {}
        sealed = inv.get("sealed") or {}
        lines = [f"背包：{len(slots)}/36 格"
                 + (f"，已封点：{sum(sealed.values())} 条" if sealed else "")]
        for k, v in list(slots.items())[-5:]:
            lines.append(f"  · 格「{str(v.get('q') or k)[:50]}」（{v.get('at')}）")
        return "\n".join(lines)
    except Exception as e:
        return f"背包：拿不到（{type(e).__name__}）"


def _curriculum(scene: str) -> str:
    try:
        base = ROOT / "ckpt_curriculum" / scene
        comp = json.loads((base / "completed_tasks.json").read_text("utf-8")) if (base / "completed_tasks.json").exists() else []
        fail = json.loads((base / "failed_tasks.json").read_text("utf-8")) if (base / "failed_tasks.json").exists() else []
        L = [f"课程账：完成 {len(comp)} / 失败 {len(fail)}（progress={len(comp)}）"]
        for t in comp[-3:]:
            L.append(f"  · 完成：{str(t)[:60]}")
        return "\n".join(L)
    except Exception as e:
        return f"课程账：拿不到（{type(e).__name__}）"


def _milestone(scene: str) -> str:
    try:
        import maieutic_scaffold as ms
        tgt = ms.current_target(scene)
        if not tgt:
            return "里程碑：当前无锚点（自由学模式）"
        lit = ms.milestone_lit(scene, tgt)
        unclosed = ms.milestone_unclosed(scene, tgt)
        # milestone_lit 没有 closed 字段（只有 total_dims / achieved /
        # verified_real）——闭合数要对账 unclosed 得出，别显示成「?/3」。
        total = int(lit.get("total_dims") or 0)
        closed = max(total - len(unclosed), 0) if total else 0
        return (f"里程碑：「{str(tgt)[:60]}」\n"
                f"  维度 {closed}/{total} 闭合"
                f"｜达成：{'是' if lit.get('achieved') else '否'}"
                + ("（点亮时取到真数）" if lit.get("verified_real") else "")
                + (f"｜未闭合 {len(unclosed)} 条：" +
                   "、".join(str((x or {}).get("id") or "")[:24] for x in unclosed[:4])
                   if unclosed else "｜未闭合 0 条"))
    except Exception as e:
        return f"里程碑：拿不到（{type(e).__name__}）"


def _chat_ledger(scene: str) -> str:
    try:
        base = ROOT / "ckpt_curriculum" / scene / "chat"
        if not base.exists():
            return "chat 账本：还没有 exchange（学生内问尚未发生）"
        L = []
        for f in sorted(base.glob("*.json"))[-3:]:
            d = json.loads(f.read_text("utf-8"))
            exs = d.get("opusExchanges") or []
            modes = [e.get("opusMode") for e in exs[-5:]]
            L.append(f"  · {f.stem}：{len(exs)} 笔，最近 mode {modes}")
            # 教师监控位看得见问答原文（此前只给 mode 计数，人问「学生问了什么」
            # 就答不上——2026-11-18 用户截图指出）。
            for e in exs[-2:]:
                L.append(f"      学生问：{str(e.get('studentMessage') or '')[:160]}")
                L.append(f"      chat 答：{str(e.get('opusResponse') or '')[:200]}")
        return "chat 账本（学生内问，只读展示）：\n" + "\n".join(L)
    except Exception as e:
        return f"chat 账本：拿不到（{type(e).__name__}）"


def _skills() -> str:
    try:
        d = json.loads((ROOT / "skill_store.json").read_text("utf-8"))
        items = d.get("items") if isinstance(d, dict) else d
        items = items or []
        L = [f"技能库：{len(items)} 条"]
        for s in items[-3:]:
            k = s.get("key") or (s if isinstance(s, str) else "")
            L.append(f"  · {str(k)[:60]}")
        return "\n".join(L)
    except Exception as e:
        return f"技能库：拿不到（{type(e).__name__}）"


def _run_log_tail() -> str:
    try:
        f = ROOT / "run_log.jsonl"
        if not f.exists():
            return "运行日志：还没有落账"
        lines = f.read_text("utf-8").strip().splitlines()[-5:]
        L = ["最近 5 轮运行账："]
        for ln in lines:
            try:
                d = json.loads(ln)
                L.append(f"  · [{d.get('finished_at') or d.get('ts') or ''}] "
                         f"{str(d.get('task') or d.get('question') or '')[:45]}"
                         f" → {d.get('verdict') or d.get('status') or ''}")
            except Exception:
                continue
        return "\n".join(L)
    except Exception as e:
        return f"运行日志：拿不到（{type(e).__name__}）"


def _case(scene: str) -> str:
    """学生**当前这一题**的实际交付物（教师监控位的核心面，2026-11-18）。

    只读 load_case：题面 / 骨架维度 / 冻结 spec / Phase1 追问轨迹 /
    Phase2 提交的 finalCode / critic 评语 / 机检结果 / 历史提交 / Phase3 分歧。
    零写账。
    """
    try:
        import maieutic_scaffold as ms
        tgt = ms.current_target(scene)
        if not tgt:
            return "学生当前这一题：无锚点（自由学待出题，账上没有在做的题）"
        d = ms.load_case(scene, tgt)
        if not d:
            return f"学生当前这一题：「{str(tgt)[:100]}」（账本读不到，可能是刚建还没落盘）"
        L = [f"学生当前这一题：「{str(d.get('question') or tgt)[:120]}」"]
        ms_ = d.get("milestone") or {}
        if ms_.get("name"):
            L.append(f"  里程碑：{str(ms_['name'])[:100]}")
        dims = d.get("dimensions") or []
        if dims:
            names = []
            for x in dims[:6]:
                if isinstance(x, dict):
                    names.append(str(x.get("name") or x.get("id") or "")[:36])
                else:
                    names.append(str(x)[:36])
            L.append(f"  骨架 {len(dims)} 维：" + "；".join(n for n in names if n))

        p1 = d.get("phase1") or {}
        spec = str(p1.get("final_spec_text") or "")
        if spec:
            L.append(f"  Phase1 冻结 spec（{len(spec)} 字）：{spec[:800]}"
                     + ("…（后略）" if len(spec) > 800 else ""))
        its = p1.get("iterations") or []
        if its:
            # phase1.iterations 的形状（scaffold 写的那本账）：
            #   run / round / spec_text / questions / gaps_identified /
            #   gaps_addressed / emergent / passed / ts
            # ⚠️ 不是 progress.iterations 的 (id, rounds, asked, asked_q) ——
            #    2026-11-18 实测按后者读出来全是 None。
            L.append(f"  Phase1 追问轨迹（{len(its)} 轮）：")
            for it in its[-5:]:
                if not isinstance(it, dict):
                    continue
                qs = [str(q)[:80] for q in (it.get("questions") or [])[:2]]
                addr = [str(g)[:20] for g in (it.get("gaps_addressed") or [])[:3]]
                ident = [str(g)[:20] for g in (it.get("gaps_identified") or [])[:3]]
                seg = (f"    · 第 {it.get('round')} 轮 passed={it.get('passed')}")
                if ident:
                    seg += f"｜新识别：{'、'.join(ident)}"
                if addr:
                    seg += f"｜答到：{'、'.join(addr)}"
                L.append(seg)
                if qs:
                    L.append(f"      追问：{' / '.join(qs)}")

        p2 = d.get("phase2") or {}
        if p2:
            code = str(p2.get("final_code") or "")
            L.append(f"  Phase2 学生提交的 finalCode（{p2.get('code_chars') or len(code)} 字"
                     f"，第 {p2.get('run') or 1} 遍，submitted={p2.get('submitted')}"
                     f" @{p2.get('submitted_at') or '-'}）：")
            if code:
                L.append("```python")
                L.append(code[:2600] + ("\n…（后略）" if len(code) > 2600 else ""))
                L.append("```")
            if p2.get("critique"):
                L.append(f"  critic 评语：{str(p2['critique'])[:900]}")
            L.append(f"  机检：verified={p2.get('verified')}"
                     f"（{p2.get('verified_by') or '-'}）"
                     f"｜divergences={p2.get('divergences')}"
                     f"｜open_items={len(p2.get('open_items') or [])}")
            if p2.get("open_items"):
                L.append("  open_items：" + " / ".join(str(x)[:50] for x in (p2.get("open_items") or [])[:4]))
            if p2.get("smoke"):
                L.append(f"  smoke：{json.dumps(p2.get('smoke'), ensure_ascii=False)[:260]}")
            if p2.get("why"):
                L.append(f"  Phase2 结论/缺口：{str(p2['why'])[:420]}")
            if p2.get("skills"):
                L.append(f"  本轮注入配方：{p2.get('skills')}｜draft_from={p2.get('draft_from') or '-'}")
        else:
            L.append("  Phase2：账上还没有 finalCode（还没交码或没落账）")

        hist = d.get("phase2_history") or []
        if hist:
            L.append(f"  历史提交 {len(hist)} 遍（覆盖前存档）：")
            for h in hist[-3:]:
                L.append(f"    · 第 {h.get('run') or '?'} 遍 {h.get('code_chars')} 字"
                         f" submitted={h.get('submitted')} verified={h.get('verified')}"
                         f"｜{str(h.get('why') or '')[:90]}")

        p3 = d.get("phase3") or {}
        if p3:
            L.append(f"  Phase3 分歧账：{json.dumps(p3, ensure_ascii=False)[:400]}")
        return "\n".join(L)
    except Exception as e:
        return f"学生当前这一题：拿不到（{type(e).__name__}: {e}）"


def _past_cases(scene: str, limit: int = 2) -> str:
    """**历史封格的题**（教师位摆整张桌：学生之前交过的码与评语，2026-11-18）。

    只读 ckpt_maieutic/<scene>/req_*.json，按 mtime 倒序，跳过当前锚点与
    没有 phase2 的题；每题给：题面 / 码长 / 机检 / critic 评语首句。
    零写账。
    """
    try:
        import maieutic_scaffold as ms
        base = ROOT / "ckpt_maieutic" / scene
        if not base.exists():
            return "历史题：还没有账本"
        tgt = ms.current_target(scene)
        files = sorted([f for f in base.glob("req_*.json")],
                       key=lambda x: x.stat().st_mtime, reverse=True)
        rows = []
        for f in files:
            if len(rows) >= limit:
                break
            try:
                d = json.loads(f.read_text("utf-8"))
            except Exception:
                continue
            q = str(d.get("question") or "")
            if not q or q == tgt:
                continue
            p2 = d.get("phase2") or {}
            if not p2:
                continue
            crit = str(p2.get("critique") or "").replace("\n", " ")[:110]
            rows.append(f"  · 「{q[:60]}」第 {p2.get('run') or 1} 遍交码 "
                        f"{p2.get('code_chars') or 0} 字｜submitted={p2.get('submitted')}"
                        f"｜verified={p2.get('verified')}"
                        f"｜divergences={p2.get('divergences')}"
                        + (f"｜评语：{crit}" if crit else ""))
            # 最近一题连码一起摆上桌（教师问「上一题代码到底写了什么」时，
            # 只给评语摘要等于让人从评语反推——监控位应直接看到那份码）。
            if len(rows) == 1:
                code = str(p2.get("final_code") or "")
                if code:
                    rows.append("      finalCode 片段：")
                    rows.append("      ```python")
                    rows.append("      " + code[:1600].replace("\n", "\n      ")
                                + ("\n      …（后略）" if len(code) > 1600 else ""))
                    rows.append("      ```")
        if not rows:
            return "历史题：此前没有已交码的题（账上只有当前这一题）"
        return "历史封格的题（教师位可查）：\n" + "\n".join(rows)
    except Exception as e:
        return f"历史题：拿不到（{type(e).__name__}）"


def context_snapshot(scene: str = "S") -> str:
    """五段状态输入（maieutic 五段输入的同构位：题面/水平/spec/code/exchanges
    → 循环/背包/课程/里程碑/chat+技能+日志）。每段独立可降级。"""
    return "\n".join([
        _pilot_state(),
        _backpack(scene),
        _curriculum(scene),
        _milestone(scene),
        _case(scene),
        _past_cases(scene),
        _chat_ledger(scene),
        _skills(),
        _run_log_tail(),
    ])


# ---------------------------------------------------------------- 问答
SYSTEM = """你是 MVE 学习系统的**教师端监控**讲解员。人（教师）在监控位看学生
（agent）的学习过程与交付物。教师位在原版助产士里本来就是**全可见**的：
学生交的码、critic 的评语、追问轨迹、历史提交遍数、机检结果，都摆在同一张桌上。

【你能看到的账面（下面【当前状态账】里全都有）】
· 循环状态 / 背包格 / 课程账 / 里程碑闭合 / 技能库 / 运行日志
· 学生当前这一题：题面、骨架维度、Phase1 冻结 spec、追问轨迹、
  **Phase2 提交的 finalCode 全文**（超长会标「后略」）、critic 评语、
  机检（verified / divergences / open_items / smoke）、历史提交、Phase3 分歧
· chat 账本：学生问了什么、chat 怎么答的、mode 游程

规则：
1. 教师问学生做过什么、交了什么东西、为什么没过——先在账面里找，找到就直接答，
   可以引用代码片段与评语原文，指出对应位置（比如「critique 第 2 条说的就是这段」）。
2. 只有账面里**确实没有**的字段，才说「账上没有这一项」。
   **禁止**回答「我只能回答学习进度类问题」——学生作业就在账上。
3. 长代码不必全文复述：说清它做了什么、贴关键几行、指出问题在哪。
4. 只转述账面事实，不评分、不判定是否学会（掌握度由消费侧算）。
5. 中文，结论先行，控制在 400 字以内（贴代码时可放宽）。"""


def ask(scene: str, question: str) -> dict[str, Any]:
    q = str(question or "").strip()
    if not q:
        return {"ok": False, "error": "问题是空的"}
    ctx = context_snapshot(scene)
    from llm_client import chat
    try:
        text = chat(
            [{"role": "system", "content": SYSTEM},
             {"role": "user",
              "content": f"【当前状态账】\n{ctx}\n\n【教师的问题】\n{q}"}],
            # 45s + retries=1：最坏 45s，留余量给 nginx 默认 60s 读超时
            # （2026-11-18 实测：90s×重试会拖到网关 504，前端拿到空响应）。
            temperature=0.3, max_tokens=800, timeout=45.0, retries=1,
            model=os.getenv("ZHIPU_MODEL", "glm-4.6"),
            thinking={"type": "disabled"})
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    return {"ok": True, "answer": str(text).strip(),
            "context": ctx}
