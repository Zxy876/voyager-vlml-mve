#!/usr/bin/env python3
"""来源绑定的「取证落点表 → 上游抓手」等价放宽（2026-11-09 服务器实测加）。

跑法：python mve/test_upstream_binding.py（服务器无 pytest，同 test_*.py 口径）。

这一层守什么
------------
第二刀 A 把来源绑定守卫接进消费侧后，服务器实跑暴露了一个**误伤**：

    维度问「在 **web_facts** 表中，当 item='ce' 时，所有平台判定列中值为
    'CLAIMED' 的列名有哪些？」
    模型做的是 `mcp_sherlock_lookup_username(username="ce", ...)`
    → 守卫判 kind=table、tok=web_facts，要求调用的**入参**点到 web_facts
    → 没点到 → 判「来源未绑定」→ `data=None` → `ok_tool_calls=0`
    → `judgment_result=fetched_empty` → 这道题永远答不出来。

**但 web_facts 恰恰就是 Sherlock 结果落库后的摊平宽表**
（`web_ingest.save_evidence` → 账本 → `_refresh_facts` 摊平成宽表）——
「取的是不是这一份」的答案是**是**：同一份数据，只是从源头取。

修法 = 放宽**一类**情形（不是取消守卫）：
    · 只对 kind=="table" 生效（文件名 / URL / 仓库 slug 一律不放宽）；
    · 只对**取证落点表**生效（实查：web_docs / web_facts / web_evidence）；
    · 只认**远端抓手**（actions 注册表 origin=net|mcp 的条目）——
      H 场景那个反例调的是 `gh_repo_info`（origin=github）→ 仍判不绑定；
    · 仍要求那条远端调用**真的 ok**（材料账里有内容）。

本测试钉死这四条边界（尤其是最后两条 —— 放宽不能变成放水）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    import source_binding as sb

    # 真实库（服务器上有场景 L 的库，表名实查）→ table 类判定才起得了
    _db = ""
    for _cand in ("/root/mve/scene_data/L/scene.duckdb",
                  "/root/mve/scene_data/B/payroll.duckdb"):
        if os.path.exists(_cand):
            _db = _cand
            break
    _tables = sb.table_names(_db) if _db else []

    print("== 1. 上游等价：取证落点表 + 远端抓手 → 绑定 ==")
    if not _tables:
        print("  ⚠️ 本机无场景库 → 跳过需要真表的断言")
    else:
        # 找一张**取证落点表**（web_facts / web_docs）当判据对象
        _up = [t for t in ("web_facts", "web_docs") if t in _tables]
        if not _up:
            print("  ⚠️ 本机库里没有取证落点表 → 跳过")
        else:
            _t = _up[0]
            _dim = f"在 {_t} 表中，当 item 为 'ce' 的记录共有多少条？"
            # 本轮只调了远端抓手（args 里没有表名）→ 放宽前不绑定，放宽后绑定
            _ev_remote = [{"tool": "mcp_sherlock_lookup_username", "ok": True,
                           "args": "username='ce'", "text": "claimed 123"}]
            ok_before, _w = sb.check(_dim, _ev_remote, _db)
            check("放宽前：只调远端抓手 → 不绑定（原行为）",
                  ok_before is False, str(_w)[:70])
            ok_after, _w2 = sb.check(
                _dim, _ev_remote, _db,
                upstream_tables=["web_docs", "web_facts", "web_evidence"],
                upstream_tools=["mcp_sherlock_lookup_username"])
            check("放宽后：取证落点表 + 远端抓手 → 绑定", ok_after is True)

            print("== 2. 放宽的边界（不能变成放水）==")
            # ① 非远端抓手（gh_repo_info 是 H 场景那个反例用的）
            _ev_gh = [{"tool": "gh_repo_info", "ok": True,
                       "args": "owner/repo", "text": "a repo"}]
            ok_gh, _ = sb.check(
                _dim, _ev_gh, _db,
                upstream_tables=["web_docs", "web_facts"],
                upstream_tools=["mcp_sherlock_lookup_username"])
            check("H 场景反例（gh_repo_info 不是远端抓手）→ 仍不绑定",
                  ok_gh is False, str(_)[:70])

            # ② 远端抓手调用**失败** → 不算
            _ev_fail = [{"tool": "mcp_sherlock_lookup_username", "ok": False,
                         "args": "username='ce'", "error": "timeout"}]
            ok_fail, _ = sb.check(
                _dim, _ev_fail, _db,
                upstream_tables=["web_facts"],
                upstream_tools=["mcp_sherlock_lookup_username"])
            check("远端抓手调用失败 → 不算绑定", ok_fail is False)

            # ③ 不是取证落点表的普通表 → 不放宽
            _other = [t for t in _tables if t not in _up]
            if _other:
                _dim2 = f"在 {_other[0]} 表中查一下"
                ok_other, _ = sb.check(
                    _dim2, _ev_remote, _db,
                    upstream_tables=["web_docs", "web_facts"],
                    upstream_tools=["mcp_sherlock_lookup_username"])
                check(f"普通表 {_other[0]} → 不放宽", ok_other is False)

            # ④ 没给远端抓手清单 → 不放宽（拿不到实况就不猜）
            ok_nolist, _ = sb.check(_dim, _ev_remote, _db,
                                    upstream_tables=["web_facts"],
                                    upstream_tools=[])
            check("没给远端抓手清单 → 不放宽", ok_nolist is False)

    print("== 3. 文件名类一律不放宽（README 反例必须仍被拦）==")
    _ev_any = [{"tool": "mcp_sherlock_lookup_username", "ok": True,
                "args": "username='ce'", "text": "x"}]
    ok_file, _w3 = sb.check(
        "README.md 里写的仓库最后更新时间是什么时候？", _ev_any, _db,
        upstream_tables=["web_docs", "web_facts"],
        upstream_tools=["mcp_sherlock_lookup_username"])
    check("README.md 类（file）→ 不放宽", ok_file is False, str(_w3)[:70])

    print("== 4. 两侧接线（消费侧 + 学习侧都传实况清单）==")
    here = Path(__file__).resolve().parent
    csrc = (here.parent / "voyager_vlml" / "consume.py").read_text(
        encoding="utf-8")
    check("消费侧传 upstream_tables / upstream_tools",
          "upstream_tables=_up_tables" in csrc
          and "upstream_tools=_up_tools" in csrc)
    check("消费侧的取证落点表来自 _ingest_tables（实查）",
          "_la._ingest_tables(" in csrc)
    check("消费侧的远端抓手来自 origin=net|mcp（实况）",
          '"net", "mcp"' in csrc)
    lsrc = (here / "learn_orchestrate.py").read_text(encoding="utf-8")
    check("学习侧也传（同一把尺子）",
          "upstream_tables=_up_tables" in lsrc
          and "upstream_tools=_up_tools" in lsrc)
    check("学习侧没有引用不存在的 _scene_id 变量",
          "_scene_id" not in lsrc.replace("current_scene_id()", ""))

    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
