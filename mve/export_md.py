#!/usr/bin/env python3
"""把 MVE 的学习过程导出成一份 md 产物（照猫娘伴学的 doc_exporter.py）。

伴学的 md 结构（doc_exporter.py:134 build_markdown）：
    # 标题 + 元信息（Exported at / Style / Range）
    ## Overview            —— 统计
    ## Recent Interactions —— Input/Output 对（**排第一，人的操作轨迹**）
    ## Knowledge Map
    ## Mastery
    ## Wrong Questions

原样搬过来的三件事：
  1. **Recent Interactions 排在最前** —— 伴学把它放在 Overview 之后的第一节，
     因为"人做了什么"是复现一次实验的起点。用户的要求也是这个：
     「md 一定要具体到界面交互（按了什么键，然后选了什么难度）」。
  2. `escape_markdown` 转义（doc_exporter.py:17 的 _MARKDOWN_ESCAPE_RE）——
     不转义的话，题目里的 `*` `_` `#` 会把表格和标题弄坏。
  3. 长度截断保护（_MAX_MARKDOWN_CHARS = 120_000），超出标注 [export truncated]。

跑法：
    python mve/export_md.py                # 写到 mve/exports/
    python mve/export_md.py --stdout       # 直接打印
    python mve/export_md.py -o /tmp/a.md   # 指定路径
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

import panel_data  # noqa: E402
import ui_events  # noqa: E402

# 照伴学 doc_exporter.py:17，但收窄了两处：
#   a) 去掉 `-` 和 `.` —— 伴学原样转义会把「0-100」「50.0」写成 `0\-100` `50\.0`，
#      行内的连字符和句点并不会破坏 md 结构，纯噪音。
#   b) 反引号内的片段不转义 —— `pistol_eco_pattern` 被写成 `pistol\_eco\_pattern` 没法读。
_MARKDOWN_ESCAPE_RE = re.compile(r"([\\`*_{}\[\]()#+!|])")
_CODE_SPAN_RE = re.compile(r"`[^`]*`")
_MAX_TEXT_CHARS = 2000
_MAX_MARKDOWN_CHARS = 120_000


def _escape_segment(text: str) -> str:
    return _MARKDOWN_ESCAPE_RE.sub(r"\\\1", text)


def escape_markdown(value: Any, limit: int = _MAX_TEXT_CHARS) -> str:
    """照伴学：转义会把 md 结构弄坏的字符，并限长。反引号内原样保留。"""
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
    r"""行内代码：反引号包裹，**不转义**。

    题名 / 工具名 / 难度这类字段本来就带下划线（`pistol_eco_pattern`、
    `match_summary_report`），套了反引号再转义就变成 `pistol\_eco\_pattern`，
    渲染出来是对的但源码里没法读。反引号内 md 不再解析，转义纯属多余。
    """
    text = "" if value is None else str(value)
    text = text.replace("`", "'").replace("\n", " ").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return f"`{text}`" if text else "`—`"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def build_markdown(state: dict[str, Any] | None = None) -> str:
    s = state if state is not None else panel_data.build_state()
    now = datetime.now().isoformat(timespec="seconds")

    env = s.get("env") or {}
    pilot = s.get("pilot") or {}
    rec = s.get("recommend") or {}
    stats = s.get("skill_stats") or {}
    events = ui_events.recent(60)

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
        f"- 驾驶舱: {'运行中' if pilot.get('running') else '未运行'}",
        "",
    ]

    # ---------------- 界面交互（用户明确要求：具体的按键与选择）----------------
    lines.extend(["", "## 界面交互（按了什么、选了什么）", ""])
    if events:
        for e in events:
            # 旧事件落盘时可能还没有中文标签（action 里存的是 kind 原文），
            # 导出的这一刻再按 kind 翻译一次，保证回看时始终是人话。
            action = ui_events.ACTION_LABEL.get(e.get("kind") or "", e.get("action"))
            lines.append(f"### #{e['seq']} · {e['ts'][11:19]} · {action}")
            d = e.get("detail") or {}
            bits = []
            for k, v in d.items():
                if v in (None, "", []):
                    continue
                bits.append(f"- {k}: {code(v, 200)}")
            if bits:
                lines.extend(bits)
            else:
                lines.append("- _（无附加参数）_")
            if e.get("result"):
                lines.append(f"- 结果: {code(e['result'], 400)}")
            lines.append("")
    else:
        lines.append("_还没有界面交互记录。面板上的启动 / 停止 / 导入 / 切分区都会记到这里。_")

    # ---------------- 概览 ----------------
    series = s.get("series") or []
    lines.extend([
        "", "## 概览", "",
        f"- 累计轮次: {s.get('total_rounds', 0)}",
        f"- 掌握度变化: {escape_markdown(s.get('mastery_delta', '—'))}",
        f"- 技能库: {stats.get('skills', 0)} 条"
        f"（累计写入 {stats.get('writes', 0)} 次 · 覆盖 {stats.get('rewrites', 0)}"
        f" · 丢弃 {stats.get('skipped', 0)} · 膨胀率 {stats.get('bloat', 1)}）",
        f"- 累计拦下编造: {s.get('hallucination_total', 0)} 条",
        f"- 人导入: {len(s.get('imports') or [])} 条",
        f"- 行动因果: {len((s.get('causal') or {}).get('rows') or [])} 条",
    ])
    if s.get("reading_text"):
        lines.append(f"- 判读: {escape_markdown(s['reading_text'])}")

    # ---------------- 掌握度 ----------------
    lines.extend(["", "## 掌握度（按题）", ""])
    by_topic: dict[str, dict[str, Any]] = {}
    for p in series:
        t = p.get("topic_id")
        if not t:
            continue
        r = by_topic.setdefault(t, {"n": 0, "best": None, "last": None, "wrongs": 0})
        r["n"] += 1
        if p.get("coverage") is not None:
            r["best"] = max(r["best"] or 0, p["coverage"])
            r["last"] = p["coverage"]
        if p.get("verdict") in ("wrong", "dont_know"):
            r["wrongs"] += 1
    if by_topic:
        lines.append("| 题目 | 轮次 | 最近覆盖率 | 最好覆盖率 | 错/不知 |")
        lines.append("|---|---|---|---|---|")
        for t, r in sorted(by_topic.items(), key=lambda kv: -kv[1]["n"]):
            lines.append(
                f"| {code(t, 60)} | {r['n']} | {_pct(r['last'])} "
                f"| {_pct(r['best'])} | {r['wrongs']} |"
            )
    else:
        lines.append("_还没有掌握度数据。_")

    # ---------------- 轮次明细 ----------------
    lines.extend(["", "## 轮次明细", ""])
    if series:
        lines.append("| # | 时间 | 题目 | 轮 | verdict | 覆盖率 | 得分 | 事实数 | 证据 | 判据 | 编排 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for p in series:
            lines.append(
                f"| {p['i']} | {escape_markdown((p.get('ts') or '')[11:19])} "
                f"| {code(p.get('topic_id'), 40)} | {p.get('round')} "
                f"| {p.get('verdict')} | {_pct(p.get('coverage'))} | {p.get('score')} "
                f"| {p.get('facts')} | {p.get('evidence')} | {p.get('judge')} "
                f"| {code(' → '.join(p.get('trajectory') or []) or '—', 120)} |"
            )
    else:
        lines.append("_还没有轮次记录。_")

    # ---------------- 技能库 ----------------
    lines.extend(["", "## 技能库（Voyager 自己写的教训）", ""])
    detail = s.get("skill_detail") or []
    if detail:
        for x in detail:
            lines.append(
                f"- {escape_markdown(x.get('text'))} "
                f"（题: {code(x.get('topic'), 40)} · "
                f"命中 {x.get('hits')} · 有效 {x.get('ok')}"
                + (f" · 已覆盖 {x.get('versions')} 版" if (x.get("versions") or 1) > 1 else "")
                + "）"
            )
    else:
        lines.append("_技能库为空。_")

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
                + f" · 编排: {code(' → '.join(im.get('trajectory') or []) or '—', 120)}"
                + f" · {im.get('facts_count')} 条事实 · validated_target={im.get('validated_target')}"
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

    # ---------------- 判读说明（照伴学对 mastery 的态度：它不能当证据）----------------
    lines.extend([
        "", "## 怎么读这份导出", "",
        "- **判据是覆盖率，不是掌握度**：mastery 的 confidence 项随「证据权重之和」"
        "上升（V2 模型），反复作答就会把它推高，用它说「学会了」是假阳性。",
        "- **掌握度是会忘的**：另有保持度模型 `baseline × 2^(-天数/半衰期)`，"
        "半衰期随每次作答的反馈伸缩（答对拉长、答错缩短）。",
        "- **叙事不参与比对**：comparable 恒为 false，指标由工具出、洞察由 LLM 出（VLML README:97）。",
        "- **人导入不进掌握度**：validated_target=false，只进因果时间线。",
    ])
    if s.get("mastery_warning"):
        lines.append(f"- {escape_markdown(s['mastery_warning'])}")

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
