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
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------- 状态采集
def _pilot_state() -> str:
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
        L = [f"课程账：完成 {len(comp)} / 失败 {len(fail)}（progress={len(comp)}）"
             "｜口径：completed=里程碑点亮后封格（recall_session），"
             "与单轮运行账的 failed/success verdict 可以不同——不是“补交通过”"]
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
        # ⚠️ 账本 v2 的键是 skills（dict: key -> 条目），不是 items
        # （2026-11-20 实测：只读 items 恒报 0 条 → 讲解员答「技能库是空的」，
        #   而盘上实际有 24 条 learn_*::milestone、22 条 verified）。
        if isinstance(d, dict) and isinstance(d.get("skills"), dict):
            keys = list(d["skills"].keys())
            items = [d["skills"][k] for k in keys]
        elif isinstance(d, dict):
            items = d.get("items") or []
            keys = [(s.get("key") if isinstance(s, dict) else str(s)) for s in items]
        else:
            items, keys = list(d or []), [str(x) for x in (d or [])]
        nver = sum(1 for s in items if isinstance(s, dict) and s.get("verified"))
        names: dict[str, int] = {}
        for k in keys:
            n = str(k).split("::")[-1]
            names[n] = names.get(n, 0) + 1
        L = [f"技能库：{len(items)} 条（verified {nver} 条）"
             + (f"｜按技能名：{', '.join(f'{n}×{c}' for n, c in sorted(names.items(), key=lambda x: -x[1])[:5])}"
                if names else "")]
        for k in keys[-3:]:
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


def _case(scene: str, d: dict[str, Any] | None = None, *,
          label: str = "学生当前这一题", code_cap: int = 2600) -> str:
    """一张 req 卡的实际交付物（教师监控位的核心面，2026-11-18）。

    只读：题面 / 骨架维度 / 冻结 spec / Phase1 追问轨迹 / Phase2 提交的
    finalCode / critic 评语 / 机检结果 / 历史提交 / Phase3 分歧。零写账。
    d=None 时读当前锚点；传入已加载的卡 dict 则直接渲染——「教师问到的卡」
    摆桌用（2026-11-20：整张桌摆全，不再只摆最近 2 张）。
    """
    try:
        if d is None:
            import maieutic_scaffold as ms
            tgt = ms.current_target(scene)
            if not tgt:
                return f"{label}：无锚点（自由学待出题，账上没有在做的题）"
            d = ms.load_case(scene, tgt)
            if not d:
                return f"{label}：「{str(tgt)[:100]}」（账本读不到，可能是刚建还没落盘）"
        L = [f"{label}：「{str(d.get('question') or '')[:120]}」"]
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
                L.append(code[:code_cap] + ("\n…（后略）" if len(code) > code_cap else ""))
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


def _card_rows(scene: str) -> list[tuple[str, dict[str, Any]]]:
    """整张桌：全部 req 卡（按 mtime 倒序）。返回 [(rid, 卡dict)]，只读。

    rid 从文件名 stem 里抠出（req_req_<rid>_<dh> 双前缀也要能对上）。"""
    base = ROOT / "ckpt_maieutic" / scene
    if not base.exists():
        return []
    rows = []
    for f in sorted(base.glob("req_*.json"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            d = json.loads(f.read_text("utf-8"))
        except Exception:
            continue
        if not d.get("question"):
            continue
        m = re.search(r"req_\d{8}T\d{6}[0-9a-f]{4}", f.stem)
        rows.append((m.group(0) if m else f.stem, d))
    return rows


def _card_index(scene: str) -> str:
    """卡片索引（账 6 的全量版）：每张卡一行 req_id + 题面 + 阶段状态。

    此前只有「当前锚点 + 最近 2 张有 phase2 的卡」上桌，同题多卡、还没
    交码的卡都不在桌上 → 讲解员只能按题面模糊匹配拿错卡、或答「账上没有」
    （2026-11-19 实错：把 recall_block 的轨迹讲成「两条声明」题）。现在
    全部卡入索引，讲解员先对准 req_id 再作答。"""
    rows = _card_rows(scene)
    if not rows:
        return "卡片索引：账上还没有 req 卡"
    try:
        import maieutic_scaffold as ms
        cur = str(ms.current_target(scene) or "")
    except Exception:
        cur = ""
    L = [f"卡片索引（全部 {len(rows)} 张，按时间倒序；回答某题前先在这里对准 req_id）："]
    for rid, d in rows:
        p1 = d.get("phase1") or {}
        its = p1.get("iterations") or []
        p2 = d.get("phase2") or {}
        p3 = d.get("phase3") or {}
        st = f"P1 {len(its)} 段"
        if p2:
            st += f"｜P2 第 {p2.get('run') or 1} 遍已交 verified={p2.get('verified')}"
        else:
            st += "｜P2 未交"
        if p3:
            st += f"｜P3 {len(p3.get('divergences') or [])} 条分歧"
        mark = " ←当前锚点" if str(d.get("question") or "") == cur else ""
        L.append(f"  · {rid}{mark}｜「{str(d.get('question'))[:48]}」｜{st}")
    return "\n".join(L)


def _match_card(scene: str, question: str) -> tuple[str, dict[str, Any]] | None:
    """确定性摆桌（不烧 LLM、不写账）：教师问句点名 req_id → 直接命中；
    否则按题面字符 2-gram 重叠选最相近的一张（阈值 0.20，宁缺勿错——
    匹配错的卡只是「最相近」提示，SYSTEM 规则要求先对准 req_id 再作答）。
    命中当前锚点则不重复摆（桌上已有全文）。"""
    q = re.sub(r"\s+", "", str(question or ""))
    if len(q) < 8:
        return None
    rows = _card_rows(scene)
    if not rows:
        return None
    m = re.search(r"req_\d{8}T\d{6}[0-9a-f]{4}", q)
    if m:
        for rid, d in rows:
            if m.group(0) in rid:
                return rid, d
        return None
    try:
        import maieutic_scaffold as ms
        cur = str(ms.current_target(scene) or "")
    except Exception:
        cur = ""

    def _bg(s: str) -> set[str]:
        s = re.sub(r"\s+", "", s)
        return {s[i:i + 2] for i in range(len(s) - 1)}

    qb = _bg(q)
    best, best_o = None, 0.0
    for rid, d in rows:
        if str(d.get("question") or "") == cur:
            continue
        o = len(qb & _bg(str(d.get("question") or ""))) / max(len(qb), 1)
        if o > best_o:
            best, best_o = (rid, d), o
    return best if (best and best_o >= 0.20) else None


def context_snapshot(scene: str = "S", question: str = "") -> str:
    """九段状态输入（maieutic 五段输入的同构位：题面/水平/spec/code/exchanges
    → 循环/背包/课程/里程碑/chat+技能+日志）。每段独立可降级。

    2026-11-20 摆全桌：新增卡片索引（全部 req 卡）+「教师问到的卡」——
    按问句确定性匹配一张非当前卡，全文摆上（req_id 点名直接命中，否则
    2-gram 相似选卡）。仍是零写账。"""
    parts = [
        _pilot_state(),
        _backpack(scene),
        _curriculum(scene),
        _milestone(scene),
        _case(scene),
        _card_index(scene),
        _past_cases(scene),
    ]
    hit = _match_card(scene, question)
    if hit:
        rid, d = hit
        parts.append(_case(scene, d, label=f"【教师问到的卡】req_id={rid}",
                           code_cap=1600)
                     + "\n  （这是按问句匹配的最相近卡，全文已摆上；"
                       "同题多卡请以卡片索引对准 req_id）")
    parts += [
        _chat_ledger(scene),
        _skills(),
        _run_log_tail(),
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------- 问答
SYSTEM = """你是 MVE 学习系统的**教师端监控**讲解员。人（教师）在监控位看学生
（agent）的学习过程与交付物。教师位在原版助产士里本来就是**全可见**的：
学生交的码、critic 的评语、追问轨迹、历史提交遍数、机检结果，都摆在同一张桌上。

【你能看到的账面（下面【当前状态账】里全都有）】
· 循环状态 / 背包格 / 课程账 / 里程碑闭合 / 技能库 / 运行日志
· 卡片索引：账上**全部** req 卡（req_id + 题面 + 阶段状态，含还没交码的）
· 学生当前这一题：题面、骨架维度、Phase1 冻结 spec、追问轨迹、
  **Phase2 提交的 finalCode 全文**（超长会标「后略」）、critic 评语、
  机检（verified / divergences / open_items / smoke）、历史提交、Phase3 分歧
· 【教师问到的卡】：按你的问句确定性匹配后全文摆上的一张卡
· chat 账本：学生问了什么、chat 怎么答的、mode 游程

规则：
1. 教师问学生做过什么、交了什么东西、为什么没过——先在账面里找，找到就直接答，
   可以引用代码片段与评语原文，指出对应位置（比如「critique 第 2 条说的就是这段」）。
2. 只有账面里**确实没有**的字段，才说「账上没有这一项」。
   **禁止**回答「我只能回答学习进度类问题」——学生作业就在账上。
3. 长代码不必全文复述：说清它做了什么、贴关键几行、指出问题在哪。
4. 只转述账面事实，不评分、不判定是否学会（掌握度由消费侧算）。
5. 回答「某题学生怎么做的/答了什么」之前，先在卡片索引里对准是哪一张：
   同题多卡必须用 req_id 区分，不许按题面模糊对号。【教师问到的卡】一节
   直接转述它，不得说「账上没有」；若索引里还有别的同题卡，先说明存在
   多张、请教师确认 req_id，再作答。
6. 课程账口径：completed = 里程碑点亮后封格，与单轮运行账的 failed/success
   verdict 可以不同——这不是「补交通过」，不要编造补交叙事。
7. 中文，结论先行，控制在 400 字以内（贴代码时可放宽）。"""


def ask(scene: str, question: str) -> dict[str, Any]:
    q = str(question or "").strip()
    if not q:
        return {"ok": False, "error": "问题是空的"}
    ctx = context_snapshot(scene, q)
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
