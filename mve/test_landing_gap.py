#!/usr/bin/env python3
"""「骨架落点够不着」这条链路（第二刀补洞，2026-11-09）的测试。

跑法：python mve/test_landing_gap.py（服务器无 pytest，同 test_*.py 口径）。

这一层守什么
------------
第二刀把「维度落点」固化成了骨架里的 `landing` 字段，消费侧只读它 →
路由不再猜。但**现场唯一存在的坏骨架是第二刀之前生成的**，它**没有**
`landing` 字段，于是：

    · `resplit_needed` 对它恒返回 False → 人看不到「该重拆」的信号
      → 坏骨架永远留在缓存里；
    · 消费侧旧骨架路径两条判据都不命中 → 整题静默落进 `infer`
      = 什么都不查，**且没有任何交代**（人只看到「答不出来」，
      不知道是骨架指向了一条走不通的路）。

本测试钉死：
    ① `unreachable_in_text` 只认「**真存在但不可达**」的表（账本表，
       实查得来）—— 不能把「场景里根本没有的表名」误判成不可达落点；
    ② 旧骨架（无 `landing`）点名了不可达表 → `resplit_needed` = True；
    ③ 旧骨架点名了**可达**表 → `resplit_needed` = False（不误伤）；
    ④ 消费侧旧骨架路径 → 落进 `unreachable_landings`（**如实交代**），
       而不是静默 infer；
    ⑤ 判据**无关键词表**：只靠实查到的表名做字面包含。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def _strip_docstrings(src: str) -> str:
    """去掉 docstring 与注释，只留**代码**（判据是否写死表名只看代码）。

    为什么要分清「代码」和「文档」：`reachable.py` 的文件头里写着
    「实测场景 L 的 web_evidence 在另一个库里」—— 那是**取证记录**
    （让人看懂为什么有这一层），不是判据。判据里写死表名才是硬编码。
    """
    import ast
    import io
    import tokenize
    try:
        # ① 记下所有 docstring 的行号范围
        lines = set()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr) and \
                        isinstance(body[0].value, ast.Constant) and \
                        isinstance(body[0].value.value, str):
                    d = body[0]
                    lines.update(range(d.lineno, (d.end_lineno or d.lineno) + 1))
        # ② 逐行输出：注释整行丢；docstring 行换成空串
        out: list[str] = []
        for i, line in enumerate(src.splitlines(), start=1):
            if i in lines:
                out.append("")
                continue
            # 去掉行内注释（不处理字符串里的 #，简单够用）
            code = line.split("#", 1)[0] if "#" in line else line
            out.append(code)
        return "\n".join(out)
    except Exception:
        return src


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


def _pick_scene_with_ledger() -> tuple[str, list[str]] | None:
    """找一个**真的有账本库**的场景（账本表 = 不可达表的样本）。"""
    import reachable as R
    for sid in ("L", "A", "B", "E", "H"):
        try:
            led = R.ledger_tables(sid) or []
        except Exception:
            continue
        if led:
            return sid, led
    return None


def main() -> int:
    import reachable as R
    import voyager_vlml.consume as C

    found = _pick_scene_with_ledger()
    if not found:
        print("  ⚠️ 本机没有带账本库的场景 → 跳过需要真库的断言")
        print(f"\n== 结果：{PASS} 过 / {FAIL} 挂 ==")
        return 1 if FAIL else 0

    sid, led = found
    t_led = led[0]
    reach = R.reachable_tables(sid) or []
    t_reach = reach[0] if reach else ""
    print(f"== 场景 {sid}：账本表 {led} / 可达表 {reach} ==")

    print("== 1. unreachable_in_text：只认「真存在但不可达」的表 ==")
    check(f"点名账本表 {t_led} → 命中",
          R.unreachable_in_text(sid, f"在 {t_led} 表中，item 为 'ce' 的记录")
          == [t_led])
    if t_reach:
        check(f"点名可达表 {t_reach} → 不命中（那张查得到）",
              R.unreachable_in_text(sid, f"在 {t_reach} 表中查") == [])
    check("空文本 → 不命中", R.unreachable_in_text(sid, "") == [])
    check("无关文本 → 不命中",
          R.unreachable_in_text(sid, "帮我查一下这个人注册了哪些平台") == [])
    check("**不存在的表名** → 不命中（不误判成不可达落点）",
          R.unreachable_in_text(sid, "在 definitely_not_a_table_zzz 表中") == [])

    print("== 2. 旧骨架（无 landing）：点名不可达表 → 该重拆 ==")
    import maieutic_scaffold as ms
    # ⚠️ 为什么用**真实场景** + 唯一测试问句，而不是造一个假场景 id：
    #   `_cache_path` 的键里含 `md5(db_path)[:8]`，db_path 来自
    #   `scene_profile.profile()` —— 对一个**还不存在**的场景，第一次调用时
    #   场景目录里还没有 .duckdb（kind=local，db_path=""），随后
    #   `ensure_source` 会**现场建**一个 scene.duckdb → 下一次调用 db_path
    #   变了 → **键也变了** → 写文件与读文件指向不同路径
    #   （2026-11-09 服务器实测踩到：`resplit_needed` 恒 False）。
    #   真实场景的 db_path 稳定（实测 L 两次调用同路径），所以测试直接用
    #   真实场景 + 唯一问句，测完把临时文件删掉。
    q = "【测试】落点够不着（旧骨架，无 landing 字段）"
    p = ms._cache_path(sid, q)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 造一份「旧骨架」：**故意不带 landing 字段**
        dims_bad = [{"id": "d1",
                     "question": f"在 {t_led} 表中，item 为 'ce' 的记录有哪些？",
                     "why": "查这个"}]
        dims_ok = [{"id": "d1",
                    "question": f"在 {t_reach} 表中，item 为 'ce' 的记录有哪些？",
                    "why": "查这个"}] if t_reach else []
        p.write_text(json.dumps({"question": q, "dimensions": dims_bad},
                                ensure_ascii=False), encoding="utf-8")
        check("旧骨架点名不可达表 → resplit_needed = True",
              ms.resplit_needed(sid, q) is True)

        p.write_text(json.dumps({"question": q, "dimensions": dims_ok},
                                ensure_ascii=False), encoding="utf-8")
        check("旧骨架点名可达表 → resplit_needed = False（不误伤）",
              ms.resplit_needed(sid, q) is False)

        p.write_text(json.dumps({"question": q, "dimensions": []},
                                ensure_ascii=False), encoding="utf-8")
        check("空维度 → resplit_needed = False",
              ms.resplit_needed(sid, q) is False)

        # 新骨架（**带** landing，但落点够不着）→ 也要判该重拆
        p.write_text(json.dumps(
            {"question": q,
             "dimensions": [{"id": "d1", "landing": "no_such_table_zzz",
                             "question": "随便问", "why": ""}]},
            ensure_ascii=False), encoding="utf-8")
        check("新骨架落点够不着 → resplit_needed = True",
              ms.resplit_needed(sid, q) is True)
        # 新骨架落点 = 可达表 → 不判
        p.write_text(json.dumps(
            {"question": q,
             "dimensions": [{"id": "d1", "landing": t_reach,
                             "question": "随便问", "why": ""}]},
            ensure_ascii=False), encoding="utf-8")
        check("新骨架落点可达 → resplit_needed = False",
              ms.resplit_needed(sid, q) is False)
        # 落点 = none（不需取数）→ 不判
        p.write_text(json.dumps(
            {"question": q,
             "dimensions": [{"id": "d1", "landing": "none",
                             "question": "随便问", "why": ""}]},
            ensure_ascii=False), encoding="utf-8")
        check("落点 = none（不需取数）→ resplit_needed = False",
              ms.resplit_needed(sid, q) is False)
    finally:
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass

    print("== 3. 消费侧旧骨架路径：落进 unreachable_landings（如实交代）==")
    q2 = "【测试】消费侧路由（旧骨架，点名不可达表）"
    p2 = ms._cache_path(sid, q2)
    p2.parent.mkdir(parents=True, exist_ok=True)
    try:
        p2.write_text(json.dumps(
            {"question": q2,
             "dimensions": [{"id": "d1",
                             "question": f"在 {t_led} 表中查 item='ce' 的记录",
                             "why": ""}]},
            ensure_ascii=False), encoding="utf-8")
        plan = C._plan_from_scaffold(q2, scene_id=sid, has_file=False)
        unreach = list(plan.get("unreachable_landings") or [])
        check("旧骨架点名不可达表 → 记进 unreachable_landings",
              len(unreach) >= 1, str(unreach)[:80])
        check("这条落点确实被点名了", t_led in str(unreach), str(unreach)[:80])
        check("不误判成 db 路（那张表 SQL 连不到）",
              plan.get("db") is False, f"db={plan.get('db')}")
        check("不误判成 web 路（没有远端抓手命中）",
              plan.get("web") is False, f"web={plan.get('web')}")
        check("交代里说得清是「骨架落点够不着」",
              "够不着" in str(plan.get("why") or ""),
              str(plan.get("why") or "")[:80])
    finally:
        try:
            if p2.exists():
                p2.unlink()
        except Exception:
            pass

    print("== 4. 判据无关键词表（只靠实查表名做字面包含）==")
    src = (Path(__file__).resolve().parent / "reachable.py").read_text(
        encoding="utf-8")
    check("reachable.py 用 ledger_tables 实查 + 字面包含",
          "ledger_tables(scene_id)" in src and "t in blob" in src)
    # 只查**代码**里有没有写死表名 —— 文档（docstring）里写「实测场景 L 的
    # web_evidence」是取证记录，不是判据；判据写死了表名才是硬编码。
    _code = _strip_docstrings(src)
    check("**代码**里没写死任何具体表名（web_evidence/web_facts 不该出现）",
          "web_evidence" not in _code and "web_facts" not in _code,
          _code[max(0, _code.find("web_evidence") - 40):
                _code.find("web_evidence") + 40] if "web_evidence" in _code else "")
    csrc = (Path(__file__).resolve().parent.parent / "voyager_vlml" /
            "consume.py").read_text(encoding="utf-8")
    check("消费侧用的是同一份实现（import reachable）",
          "import reachable as _reach" in csrc
          and "_reach.unreachable_in_text(" in csrc)
    msrc = (Path(__file__).resolve().parent / "maieutic_scaffold.py").read_text(
        encoding="utf-8")
    check("教师端也用同一把尺子（resplit_needed → unreachable_in_text）",
          "_reach.unreachable_in_text(" in msrc)

    print("== 5. 同一把钥匙（写与读必须指向同一份文件）==")
    # 2026-11-09 服务器实测踩到的坑：`_cache_path` 的键含 md5(db_path)，
    # 而 db_path 来自 `scene_profile.profile()` —— 对**还不存在**的场景，
    # 第一次调用时目录里没有 .duckdb（db_path=""），随后 `ensure_source`
    # 现场建一个 scene.duckdb → 下一次调用 db_path 变了 → **键变了**
    # → 写文件的路径与读文件的路径不同 → 判据永远读到「文件不存在」。
    # 这条断言守住「同一场景同一问句 → 同一个路径」。
    import maieutic_scaffold as ms2
    _q = "【测试】钥匙稳定性"
    _p1 = ms2._cache_path(sid, _q)
    _p2 = ms2._cache_path(sid, _q)
    check("同一场景同一问句 → 同一个缓存路径", _p1 == _p2,
          f"{_p1.name} vs {_p2.name}")
    check("真实场景的 db_path 非空（键里带库指纹）",
          len(_p1.stem.split("_")[-1]) == 8, _p1.stem)
    # invalidate_skeleton / resplit_needed / scaffold 必须用**同一个**算路径函数
    check("三个入口都用 _cache_path（同一把钥匙）",
          "_cache_path(" in msrc
          and msrc.count("_cache_path(") >= 2)

    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
