#!/usr/bin/env python3
"""把 MVE 的学习过程导出成一份 md 产物。

2026-11-18 重写（备份 .bak_export）：面板概览/轨迹/技能页已经全部换到
MVE 现账（learn_trace.jsonl + trace_curve + skill_store 的 hits/runs/ok），
导出器原先还在读旧练习线（run_log 的 series、exam_log、mastery），导出来
的主体章节要么是空的要么是误导的（12 条全 failed、掌握度 None）。

新结构（账本与面板一致）：
    # 标题 + 元信息
    ## 概览                                  —— trace_kpi（真取到率同一把尺）
    ## 课程曲线（里程碑重做）                —— trace_curve：重做老题 first_pass_round 变短
    ## 学习轨迹明细                          —— learn_trace 每行：P1/P2/取数/里程碑/点亮/证据门
    ## 技能库                                —— 注入 hits / 进执行 runs / 真取到 ok / 率=ok/runs
    ## 真实水平 · 撤支架考核                 —— exam_log（有数据才展开）
    ## 人导入历史 / 行动因果时间线
    ## 怎么读这份导出                        —— 按新账重写

跑法：
    python mve/export_md.py                # 写到 mve/exports/
    python mve/export_md.py --stdout       # 直接打印
    python mve/export_md.py -o /tmp/a.md   # 指定路径
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

import panel_data  # noqa: E402
import ui_events  # noqa: E402

_MARKDOWN_ESCAPE_RE = re.compile(r"([\\`*_{}\[\]()#+!|])")
_CODE_SPAN_RE = re.compile(r"`[^`]*`")
_MAX_TEXT_CHARS = 2000
_MAX_MARKDOWN_CHARS = 120_000
# 考核类型后缀：重考与迁移对照在表里长一样，不标就读不出迁移
KIND_SUFFIX = {"placement": "·摸底", "practice_exam": "·重考",
               "transfer": "·迁移", "final": "·结业"}


def _escape_segment(text: str) -> str:
    return _MARKDOWN_ESCAPE_RE.sub(r"\\\1", text)


def escape_markdown(value: Any, limit: int = _MAX_TEXT_CHARS) -> str:
    """转义会把 md 结构弄坏的字符，并限长。反引号内原样保留。"""
    text = "" if value is None else str(value)
    text = text.replace("\n", " ").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    out: list[str] = []
    idx = 0
    for m in _CODE_SPAN_RE.finditer(text):
        out.append(_escape_segment(text[idx:m.start()]))
        out.append(m.group(0))
        idx = m.end()
    out.append(_escape_segment(text[idx:]))
    return "".join(out)


def code(value: Any, limit: int = _MAX_TEXT_CHARS) -> str:
    r"""行内代码：反引号包裹，**不转义**。"""
    text = "" if value is None else str(value)
    text = text.replace("`", "'").replace("\n", " ").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return f"`{text}`" if text else "`—`"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def _short_q(q: Any, limit: int = 46) -> str:
    r"""题目太长，表格里只留函数与入参那段（「证明 B ≠ A：xxx.py 的 f() —— …」）。
    用 code() 包反引号：题名里的 _() 是标识符的一部分，escape_markdown 会把它
    写成 \_\(\)\)，渲染出来满屏反斜杠没法读。"""
    t = "" if q is None else str(q)
    return code(t, limit)


def _curriculum_counts() -> tuple[int, int]:
    """课程账（ckpt_curriculum/<scene>/completed|failed_tasks.json）。读不到就 (0,0)。"""
    done = fail = 0
    try:
        import glob
        for p in glob.glob(str(ROOT / "ckpt_curriculum" / "*" / "completed_tasks.json")):
            d = json.load(open(p))
            done += len(d) if isinstance(d, list) else len(d or {})
        for p in glob.glob(str(ROOT / "ckpt_curriculum" / "*" / "failed_tasks.json")):
            d = json.load(open(p))
            fail += len(d) if isinstance(d, list) else len(d or {})
    except Exception:
        pass
    return done, fail


def build_markdown(state: dict[str, Any] | None = None) -> str:
    s = state if state is not None else panel_data.build_state()
    now = datetime.now().isoformat(timespec="seconds")

    env = s.get("env") or {}
    pilot = s.get("pilot") or {}
    rec = s.get("recommend") or {}
    stats = s.get("skill_stats") or {}
    kpi = s.get("trace_kpi") or {}
    trace: list[dict] = s.get("trace") or []
    curve: list[dict] = s.get("trace_curve") or []
    detail = s.get("skill_detail") or []

    if env.get("ok"):
        events_n = env.get("events")
        env_note = f" · {env.get('tables')} 张表"
        env_note += f" / {events_n} 条事件" if events_n else ""
    elif env.get("reason"):
        env_note = f" · {env.get('reason')}"
    else:
        env_note = ""
    lines: list[str] = [
        "# MVE 学习记录导出",
        "",
        f"- Exported at: `{now}`",
        f"- 数据源: `{env.get('state', '—')}`{env_note}",
        f"- 出题器当前推荐: `{rec.get('topic_id', '—')}`"
        + (f"（难度 {rec.get('difficulty')}，{rec.get('reason_label', '')}）"
           if rec.get("difficulty") else ""),
        f"- 驾驶舱: {'运行中' if pilot.get('running') else '未运行'}"
        + (f"（启动于 {pilot.get('started_at', '')[:19]}）" if pilot.get("running") else ""),
        "",
    ]

    # ---------------- 概览（MVE 现账：learn_trace + 课程账）----------------
    rounds = int(kpi.get("rounds") or 0)
    entered = int(kpi.get("entered") or 0)
    fetched = int(kpi.get("fetched") or 0)
    lit = int(kpi.get("lit") or 0)
    done_n, fail_n = _curriculum_counts()
    lines.extend(["", "## 概览", "",
                  f"- 学习轮次（一次 learn() 一行）: **{rounds}**"
                  f" · 进执行 {entered} · 真取到数 {fetched}"
                  f" · 里程碑点亮 {lit}"],
                 )
    rate = f"{round(100 * fetched / entered)}%" if entered else "—"
    lines.append(f"- **真取到率: {fetched}/{entered} = {rate}**"
                 "（分母是「真进了 Phase2/调过取数」的轮，Phase1 卡死的轮不计）")
    lines.extend([
        f"- 课程账: 已完成 **{done_n}** / 失败 {fail_n}"
        f"（ckpt_curriculum，节点点亮以 fetched 判据为准）",
        f"- 技能库: {stats.get('skills', 0)} 条"
        f"（累计写入 {stats.get('writes', 0)} 次 · 覆盖 {stats.get('rewrites', 0)}"
        f" · 丢弃 {stats.get('skipped', 0)}"
        f" · 膨胀率 {stats.get('bloat', 1)}）",
        f"- 人导入: {len(s.get('imports') or [])} 条",
    ])

    # ---------------- 当前题目 ----------------
    last = kpi.get("last") or {}
    if last:
        lines.extend(["", "## 当前题目", ""])
        lines.append(f"- 题面: {code(last.get('question'), 400)}")
        verdict = ("✅ **已点亮**" if last.get("lit")
                   else ("🟡 **维度已闭合**（里程碑全闭，卡在证据机检/完成判定）"
                         if (last.get("ms_achieved") or
                             (last.get("ms_total") and last.get("ms_closed") == last.get("ms_total")))
                         else "❌ 未达成"))
        lines.append(f"- 判据: {verdict}"
                     + (f" —— {escape_markdown(last.get('why'), 300)}" if last.get("why") and not last.get("lit") else ""))
        lines.append(f"- 里程碑: {last.get('ms_closed')}/{last.get('ms_total')} 闭合"
                     f" · Phase1 {'✅' if last.get('phase1_passed') else '—'}"
                     f" · Phase2 {'✅' if last.get('phase2_submitted') else '—'}")

    # ---------------- 课程曲线（里程碑重做：first_pass_round 变短）----------------
    lines.extend(["", "## 课程曲线（里程碑 · 重做老题）", "",
                  "闭环的核心读数在这里：同一道题**重做的轮数应该一次比一次少**"
                  "（first_pass_round 变短 = 技能/记忆真的在起效）。", ""])
    if curve:
        lines.append("| 题 | 重做次数 | 首过轮次序列 | Δ | 当前未闭 | 最近活动 |")
        lines.append("|---|---|---|---|---|---|")
        for c in sorted(curve, key=lambda x: str(x.get("last_ts") or ""), reverse=True):
            fps = [r.get("first_pass_round") for r in (c.get("runs") or [])]
            fps_s = " → ".join(str(x) if x else "✗未过" for x in fps) or "—"
            d = c.get("delta") or {}
            dcell = (f"轮 {d.get('rounds', 0):+d}" if d else ("—" if len(fps) < 2 else "0"))
            lines.append(
                f"| {_short_q(c.get('question'))} "
                f"| {c.get('n_runs')} | {fps_s} | {dcell} "
                f"| {c.get('opens_now')} | {str(c.get('last_ts') or '')[:16].replace('T', ' ')} |"
            )
        improved = [c for c in curve if (c.get("delta") or {}).get("rounds")]
        better = sum(1 for c in improved if (c["delta"]["rounds"] or 0) < 0)
        lines.append("")
        lines.append(f"- 重做过且有效对比的题 {len(improved)} 道，其中 **{better} 道**首过轮次缩短。")
    else:
        lines.append("_课程曲线还没有数据（学习循环没跑过或图谱为空）。_")

    # ---------------- 学习轨迹明细（learn_trace，与面板轨迹页同源）----------------
    lines.extend(["", "## 学习轨迹明细（learn_trace）", "",
                  "一次 learn() 一行。**status=failed 不等于没学到**："
                  "可能是题做出来了（里程碑全闭）但证据机检没过。", ""])
    if trace:
        lines.append("| 时间 | 题 | P1 | P2 | 取数(真) | 真取到 | 里程碑 | 点亮 | 证据门 | 注入/入库 | 判 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for r in trace:
            t = str(r.get("ts") or "")
            ts = datetime.fromtimestamp(float(t)).strftime("%H:%M") if t else "—"
            fc, fr, frows = r.get("fetch_calls") or 0, r.get("fetch_real") or 0, r.get("fetch_rows") or 0
            fetch = f"{fc}({fr})/{frows}行"
            ms = f"{r.get('ms_closed')}/{r.get('ms_total')}"
            gate = f"{r.get('gate_open')}开/{r.get('gate_addressed')}答"
            status = "✅learned" if r.get("status") == "learned" else ("✨失败但点亮" if r.get("lit") else "❌failed")
            lines.append(
                f"| {ts} | {_short_q(r.get('question'))} "
                f"| {'✓' if r.get('phase1_passed') else '✗'} "
                f"| {'✓' if r.get('phase2_submitted') else '✗'} "
                f"| {fetch} | {'✓' if r.get('fetch_ok') else '✗'} "
                f"| {ms} | {'✨' if r.get('lit') else '—'} "
                f"| {gate} | {r.get('skills_injected')}/{r.get('skills_added')} "
                f"| {status} |"
            )
        failed_rows = [r for r in trace if r.get("status") != "learned"]
        if failed_rows:
            lines.append("")
            lines.append("**失败原因摘要**：")
            for r in failed_rows[-6:]:
                tag = "（题已做出来，卡证据面）" if r.get("lit") or r.get("ms_achieved") else ""
                # why 里「｜」后面全是机检模板话术，导出里只留第一句
                why1 = str(r.get("why") or "").split("｜")[0].split(" —— ")[0].strip()
                lines.append(f"- {_short_q(r.get('question'))}: {escape_markdown(why1, 160)} {tag}")
    else:
        lines.append("_learn_trace 还没有行（新账从 2026-11-18 起，旧练习线不追溯）。_")

    # ---------------- 技能库（注入/进执行/真取到，与技能页同一把尺）----------------
    lines.extend(["", "## 技能库（Voyager 自己写的教训）", "",
                  "三个数：**注入**=被检索进 prompt 的轮次；**进执行**=注入后真进了 Phase2；"
                  "**真取到**=进执行且真取到数。率 = 真取到 / 进执行"
                  "（进执行 0 次的条目显示「样本不足」，不判失效）。", ""])
    if detail:
        for x in detail:
            _h = int(x.get("hits") or 0)
            _r = int(x.get("runs") or 0)
            _o = int(x.get("ok") or 0)
            if _r:
                rate_s = f"率 {round(100 * _o / _r)}%"
            else:
                rate_s = "样本不足"
            lines.append(
                f"- {escape_markdown(x.get('text'), 160)} "
                f"（题: {code(x.get('topic'), 40)} · "
                f"注入 {_h} · 进执行 {_r} · 真取到 {_o} · {rate_s}"
                + (f" · 已覆盖 {x.get('versions')} 版" if (x.get("versions") or 1) > 1 else "")
                + "）"
            )
    else:
        lines.append("_技能库为空。_")

    # ---------------- 真实水平：撤支架考核（有数据才展开）----------------
    exam_view = s.get("exam_curve") or {}
    exam_tracks = exam_view.get("tracks") or []
    if exam_view.get("has_data"):
        lines.extend(["", "## 真实水平 · 撤支架考核（exam_log）", ""])
        tr = exam_view.get("transfer")
        lines.extend([
            f"- 摸底均值 {exam_view.get('avg_baseline')}% → "
            f"最新均值 {exam_view.get('avg_latest')}%"
            f"（Δ {exam_view.get('avg_delta_pp_all') or 0:+d} 个百分点）",
            f"- 重考过 {exam_view.get('retested')} 道，其中 {exam_view.get('rose')} 道涨了"
            + (f"，平均 {exam_view['avg_delta_pp']:+d}pp"
               if exam_view.get("avg_delta_pp") is not None else ""),
        ])
        if tr:
            verdict = ("涨了 → 有迁移" if tr.get("rose")
                       else "没涨 → 上升的是「记住了这道题」，不是可迁移的能力")
            lines.append(f"- 迁移对照（{tr.get('count')} 次**未练过**的题）："
                         f"{' → '.join(tr.get('seq') or [])} —— {verdict}")
        lines.append("")
        lines.append("| 题 | 摸底（裸考） | 摸底等级 | 练后重考 | 结业考 | 最新 | Δ | 考核次数 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for t in exam_tracks:
            after = " → ".join(
                f"{a.get('pct') or '—'}{KIND_SUFFIX.get(str(a.get('kind') or ''), '')}"
                for a in (t.get("after") or [])
            ) or "（还没重考过）"
            d = t.get("delta_pp")
            dcell = "—" if d is None else (f"+{d}pp" if d > 0 else (f"{d}pp" if d < 0 else "持平"))
            lines.append(
                f"| {code(t.get('topic_id'), 60)}"
                f"{' · 已毕业让位' if t.get('exhausted') else ''} "
                f"| {t.get('baseline_pct')} "
                f"| {escape_markdown(t.get('level'))} | {after} "
                f"| {t.get('final_pct') or '—'} "
                f"| {t.get('latest_pct')} | {dcell} | {t.get('exam_count')} |"
            )
    # 没数据就不留空节：本场景没跑过撤支架考核时，硬写一节「还没有记录」
    # 加一段说教，只会稀释正文（以前无条件输出，读起来像坏账）。

    # ---------------- 人导入 ----------------
    lines.extend(["", "## 人导入历史", ""])
    imports = s.get("imports") or []
    if imports:
        for im in imports:
            lines.append(
                f"- {escape_markdown((im.get('ts') or '').replace('T', ' '))} "
                f"· {escape_markdown(im.get('question'), 300)}"
            )
            lines.append(
                f"  - 归入: `{im.get('topic_id') or '未归入任何题'}`"
                + (f"（命中 {code(im.get('topic_hit'), 60)}）" if im.get("topic_hit") else "")
            )
    else:
        lines.append("_还没有人导入。_")

    # ---------------- 行动因果时间线 ----------------
    lines.extend(["", "## 行动因果时间线", ""])
    crows = (s.get("causal") or {}).get("rows") or []
    if crows:
        lines.append("| seq | 时间 | 类型 | 题目 | 摘要 |")
        lines.append("|---|---|---|---|---|")
        for r in crows:
            lines.append(
                f"| {r.get('seq')} | {escape_markdown((r.get('ts') or '').replace('T', ' ')[5:19])} "
                f"| {r.get('kind_label')} | {code(r.get('topic_id'), 40)} "
                f"| {escape_markdown(r.get('summary'), 200)} |"
            )
    else:
        lines.append("_时间线为空。_")

    # ---------------- 判读说明（按新账重写）----------------
    lines.extend([
        "", "## 怎么读这份导出", "",
        "- **账本分层**：`learn_trace.jsonl` 是学习主账（一次 learn() 一行，量 P1 承诺/P2 执行/取数/里程碑）；"
        "`run_log.jsonl` 只剩 planner/exam 等内部消费方在用，**面板和本导出都不再读它**。",
        "- **failed ≠ 没学到**：里程碑点亮走 fetched 判据（真取到数 + 维度闭合），"
        "证据机检（verify.refs）是另一道门。题做出来但证据面没写对时，轨迹行是 failed 但 `lit=✨`。",
        "- **真取到率 = 真取到 / 进执行**：分母不含 Phase1 就卡死的轮（那些轮结构上不可能取到数），"
        "所以它量的是「配方被用上时的成功率」，不是「注入了多少次」。",
        "- **课程曲线看 first_pass_round**：同一题重做的首过轮次变短，才是「学会了」的可见证据；"
        "反过来看 stuck 的维度（多轮 opens 不减）就是内容缺口，不是闸门坏。",
        "- **练习线不判分**：覆盖率/得分/掌握度是旧后端（run_mve.py）的字段，新循环有意不产"
        "（判分进反馈环会学成讨好裁判）。要考「真实水平」走撤支架考核（exam_log），本场景没考过就不展开。",
        "- **人导入不进掌握度**：validated_target=false，只进因果时间线。",
    ])

    md = "\n".join(lines).strip() + "\n"
    if len(md) > _MAX_MARKDOWN_CHARS:
        md = md[:_MAX_MARKDOWN_CHARS].rstrip() + "\n\n...[export truncated]\n"
    return md


def main() -> int:
    argv = sys.argv[1:]
    # 用错了 python（没装 duckdb）也能导出，但「数据源」会写成 unavailable，
    # 人拿到手会以为库坏了。出声提醒一次。
    try:
        import duckdb  # noqa: F401
    except Exception:
        print("警告：当前解释器没有 duckdb，导出里「数据源」会显示 unavailable。\n"
              "      请用项目环境跑："
              "/Users/zxydediannao/.workbuddy/binaries/python/envs/default/bin/python3 "
              "mve/export_md.py", file=sys.stderr)
    md = build_markdown()
    # 命令行导出也算一次「人按了键」，同样记进交互流 —— 否则回看时会以为
    # 这段时间的轮次是自己冒出来的。
    try:
        ui_events.append(ui_events.EXPORT,
                         detail={"方式": "命令行 export_md.py",
                                 "字符数": len(md)},
                         result="ok")
    except Exception:
        pass
    if "--stdout" in argv:
        print(md)
        return 0
    out = ROOT / "exports"
    out.mkdir(exist_ok=True)
    if "-o" in argv:
        path = Path(argv[argv.index("-o") + 1])
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out / f"mve_export_{stamp}.md"
    path.write_text(md, encoding="utf-8")
    print(f"已导出：{path}（{len(md):,} 字符）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
