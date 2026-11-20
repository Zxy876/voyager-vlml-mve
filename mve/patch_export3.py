#!/usr/bin/env python3
"""2026-11-18 导出 md 改善三件套：
A) dashboard.py 前端：点在当前分区上不再记 tab_switch 事件也不再 load()
B) export_md.py：连续重复的 tab_switch 折叠 ×N；失败原因只留第一句话；
   表格里的题名用 code()（不再被 \\(\\) 转义得没法读）
"""
from pathlib import Path

def splice(path: str, old: str, new: str, note: str):
    p = Path(path)
    src = p.read_text(encoding="utf-8")
    n = src.count(old)
    assert n == 1, f"{path}: anchor count={n} for {note!r}"
    p.with_suffix(p.suffix + ".bak_export3").write_text(src, encoding="utf-8")
    p.write_text(src.replace(old, new), encoding="utf-8")
    print(f"OK {path}: {note}")

# ---------- A. 前端：同分区点击不记录不刷新 ----------
splice("dashboard.py",
       """document.getElementById('nav').addEventListener('click',e=>{
  const b=e.target.closest('[data-tab]'); if(!b) return;
  const from=TAB; TAB=b.dataset.tab;""",
       """document.getElementById('nav').addEventListener('click',e=>{
  const b=e.target.closest('[data-tab]'); if(!b) return;
  // 2026-11-18 导出治理：点在**当前**分区上不算一次交互——
  // 卡顿期连点会把 ui_events 灌成几十条同样的 tab_switch，导出全是噪音。
  if(b.dataset.tab===TAB) return;
  const from=TAB; TAB=b.dataset.tab;""",
       "nav 同分区点击不再记事件")

# ---------- B1. export_md.py：tab_switch 连续折叠 ----------
splice("export_md.py",
       """    if events:
        for e in events:
            action = ui_events.ACTION_LABEL.get(e.get("kind") or "", e.get("action"))
            lines.append(f"### #{e['seq']} · {e['ts'][11:19]} · {action}")""",
       """    if events:
        # 连续重复的 tab_switch 折叠成一条 ×N（修 2026-11-18 前端的连点灌水，
        # 旧事件文件里已经落盘的重复也在这里兜住）。
        collapsed: list[tuple[dict, int]] = []
        for e in events:
            if (e.get("kind") == "tab_switch" and collapsed
                    and collapsed[-1][0].get("detail") == e.get("detail")):
                collapsed[-1] = (collapsed[-1][0], collapsed[-1][1] + 1)
                continue
            collapsed.append((e, 1))
        for e, n in collapsed:
            action = ui_events.ACTION_LABEL.get(e.get("kind") or "", e.get("action"))
            times = f"（连点 {n} 次，折叠）" if n > 1 else ""
            lines.append(f"### #{e['seq']} · {e['ts'][11:19]} · {action}{times}")""",
       "tab_switch 连续折叠")

# ---------- B2. 失败原因只留第一句 ----------
splice("export_md.py",
       """            for r in failed_rows[-6:]:
                tag = "（题已做出来，卡证据面）" if r.get("lit") or r.get("ms_achieved") else ""
                lines.append(f"- {_short_q(r.get('question'))}: {escape_markdown(r.get('why'), 220)} {tag}")""",
       """            for r in failed_rows[-6:]:
                tag = "（题已做出来，卡证据面）" if r.get("lit") or r.get("ms_achieved") else ""
                # why 里「｜」后面全是机检模板话术，导出里只留第一句
                why1 = str(r.get("why") or "").split("｜")[0].strip()
                lines.append(f"- {_short_q(r.get('question'))}: {escape_markdown(why1, 160)} {tag}")""",
       "失败原因截断到第一句")

# ---------- B3. 表格题名用 code() ----------
splice("export_md.py",
       """def _short_q(q: Any, limit: int = 46) -> str:
    \"\"\"题目太长，表格里只留函数与入参那段（「证明 B ≠ A：xxx.py 的 f() —— …」）。\"\"\"
    t = "" if q is None else str(q)
    return escape_markdown(t[:limit].rstrip() + ("…" if len(t) > limit else ""))""",
       """def _short_q(q: Any, limit: int = 46) -> str:
    \"\"\"题目太长，表格里只留函数与入参那段（「证明 B ≠ A：xxx.py 的 f() —— …」）。
    用 code() 包反引号：题名里的 _() 是标识符的一部分，escape_markdown 会把它
    写成 \\_\\(\\)\\)，渲染出来满屏反斜杠没法读。\"\"\"
    t = "" if q is None else str(q)
    return code(t, limit)""",
       "题名改 code() 包裹")
print("ALL DONE")
