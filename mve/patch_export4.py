#!/usr/bin/env python3
"""export_md.py 收尾三处：docstring raw、题面 code()、失败原因再截断。"""
from pathlib import Path

p = Path("export_md.py")
src = p.read_text(encoding="utf-8")

lines = src.split("\n")
for i, l in enumerate(lines):
    if l.strip() == '"""题目太长，表格里只留函数与入参那段（「证明 B ≠ A：xxx.py 的 f() —— …」）。':
        lines[i] = l.replace('"""题目', 'r"""题目')
        break
else:
    raise SystemExit("docstring anchor not found")
src = "\n".join(lines)

old2 = "lines.append(f\"- 题面: {escape_markdown(last.get('question'), 400)}\")"
assert src.count(old2) == 1, ("q2", src.count(old2))
src = src.replace(old2, "lines.append(f\"- 题面: {code(last.get('question'), 400)}\")")

old3 = 'why1 = str(r.get("why") or "").split("｜")[0].strip()'
assert src.count(old3) == 1, ("q3", src.count(old3))
src = src.replace(old3, 'why1 = str(r.get("why") or "").split("｜")[0].split(" —— ")[0].strip()')

p.write_text(src, encoding="utf-8")
print("patched 3/3")
