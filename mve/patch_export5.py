#!/usr/bin/env python3
"""技能库条目里的教训正文截断：整条维度清单上千字，导出里读不动。"""
from pathlib import Path

p = Path("export_md.py")
src = p.read_text(encoding="utf-8")

old = """            lines.append(
                f"- {escape_markdown(x.get('text'))} \""""
new = """            lines.append(
                f"- {escape_markdown(x.get('text'), 160)} \""""
assert src.count(old) == 1, src.count(old)
p.write_text(src.replace(old, new), encoding="utf-8")
print("OK skill text 截到 160 字")
