#!/usr/bin/env python3
"""导出摘掉「界面交互」节（用户 2026-11-18 18:34 明确：不需要界面交互了）。

删三处：
  1. docstring 里的章节清单行
  2. build_markdown 里的 events = ui_events.recent(60)
  3. 整个「界面交互」小节（从注释行到「## 概览」之前）
ui_events 的 import 保留 —— main() 里导出动作本身还记一条 EXPORT 事件。
"""
from pathlib import Path

p = Path("export_md.py")
src = p.read_text(encoding="utf-8")
orig_len = len(src)

# 1) docstring 章节清单
old_doc = "    ## 界面交互（按了什么、选了什么）        —— ui_events，人的操作轨迹\n"
assert src.count(old_doc) == 1, ("doc", src.count(old_doc))
src = src.replace(old_doc, "")

# 2) events 取值
old_ev = "    events = ui_events.recent(60)\n"
assert src.count(old_ev) == 1, ("ev", src.count(old_ev))
src = src.replace(old_ev, "")

# 3) 整个小节
start_mark = "    # ---------------- 界面交互"
end_mark = "    # ---------------- 概览"
i = src.find(start_mark)
j = src.find(end_mark)
assert i != -1 and j != -1 and i < j, ("slice", i, j)
src = src[:i] + src[j:]

p.write_text(src, encoding="utf-8")
print(f"OK：删除界面交互节，{orig_len} -> {len(src)} 字符")
