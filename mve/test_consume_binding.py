#!/usr/bin/env python3
"""消费侧来源绑定守卫（第二刀 A，2026-11-09）的测试。

跑法：python mve/test_consume_binding.py（服务器无 pytest，同 test_*.py 口径）。

这一层守什么
------------
**消费侧也要问「取的是不是这一份」**，不只是学习侧。

生产实证（H 场景，2026-10-31）：维度问「README.md 文件中提供的仓库**最后
更新时间**是什么时候？」→ 执行层调 `gh_repo_info`，把仓库元数据的
`updated_at` 当答案 → 旧判据只问「取到没有」（材料非空、措辞对得上）→
闭合 → 里程碑 3/5→4/5→5/5 全亮。**材料是真的、来源是错的。**

学习侧当时补了 `source_binding` 这一层（`learn_orchestrate.py:1716`），
**消费侧没有** —— 消费侧照样能把「取错来源」的答案当确定句交出去。

本测试钉死：
    · 消费侧确实调了守卫（接线在）；
    · 守卫用**同一份实现**（`mve/source_binding.py`），不是新写的判据；
    · 判据看**入参**（args）不看返回文本；
    · 消费侧的完成判定仍是**确定性的**（`critic_ok = data is not None`）
      —— 守卫只把「取错来源」标成未取到，**不新增 LLM 调用、不新增门槛**
      （守用户 2026-10-25 拍板：消费侧 LLM critic 判定删除）。
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
    import voyager_vlml.consume as C
    import source_binding as sb

    print("== 1. 守卫行为：取的是不是这一份 ==")
    # 题面点名 README.md；本轮只调了 gh_repo_info（入参是仓库 slug）
    _ev_wrong = [{"tool": "gh_repo_info", "ok": True,
                  "args": "owner/repo", "text": "a repo about things"}]
    ok, why = C._source_binding_gate(
        "README.md 里写的仓库最后更新时间是什么时候？", _ev_wrong, "")
    check("取错来源（调 gh_repo_info 答 README 的问题）→ 不绑定",
          ok is False, str(why)[:70])

    # 同一题面；本轮真的读了 README.md（入参点到它）
    _ev_right = [{"tool": "read_file", "ok": True,
                   "args": "README.md", "text": "Updated 2026-03-26 ..."}]
    ok2, why2 = C._source_binding_gate(
        "README.md 里写的仓库最后更新时间是什么时候？", _ev_right, "")
    check("取对来源（入参点到 README.md）→ 绑定", ok2 is True, str(why2)[:70])

    # 调了但失败 → 不算（材料是空的）
    _ev_fail = [{"tool": "read_file", "ok": False,
                  "args": "README.md", "error": "404"}]
    ok3, why3 = C._source_binding_gate(
        "README.md 里写的仓库最后更新时间是什么时候？", _ev_fail, "")
    check("调了但失败 → 不绑定", ok3 is False, str(why3)[:70])

    print("== 2. 不拦的三种情况 ==")
    # 题面没点名任何来源 → 无从判起
    ok4, _ = C._source_binding_gate("这个软件的作者是谁？",
                                   [{"tool": "web_search", "ok": True,
                                     "args": "author", "text": "x"}], "")
    check("题面没点名来源 → 不拦", ok4 is True)
    # 材料账里没有 args → 无法归因 → 不拦
    ok5, _ = C._source_binding_gate(
        "README.md 里的时间是什么？",
        [{"tool": "read_file", "ok": True, "text": "2026"}], "")
    check("材料账无 args（无法归因）→ 不拦", ok5 is True)
    # 守卫本身不可用 → 放行（不因守卫坏了拒掉正确答案）
    ok6, _ = C._source_binding_gate("README.md 里的时间是什么？", [], "")
    check("守卫不可用/无材料 → 放行", ok6 is True)

    print("== 3. 维度问句也要判（教师端的承诺更具体）==")
    # ⚠️ 表名这一类需要 db_path 里**真的**有那张表（`show tables` 直查）——
    #   认不出表名就无从判起（守卫只能判它看得见的东西）。本地无库时
    #   这一项自动跳过；服务器上有场景库，会真跑。
    _has_db = bool(sb.table_names("/root/mve/scene_data/L/scene.duckdb"))
    if _has_db:
        # 题面本身没点名表，维度问句点名了 → 仍要绑定
        ok7, why7 = C._source_binding_gate(
            "这个用户名在 GitHub 有没有账号？",
            [{"tool": "gh_repo_info", "ok": True, "args": "owner/repo",
              "text": "x"}],
            "/root/mve/scene_data/L/scene.duckdb",
            dims=[{"id": "d1",
                   "question": "在 web_facts 表中查该用户名的判定"}])
        check("维度问句点名了表、实际没查 → 不绑定", ok7 is False,
              str(why7)[:70])
        ok8, _ = C._source_binding_gate(
            "这个用户名在 GitHub 有没有账号？",
            [{"tool": "query_sql", "ok": True,
              "args": "SELECT * FROM web_facts WHERE item='ce'",
              "text": "CLAIMED"}],
            "/root/mve/scene_data/L/scene.duckdb",
            dims=[{"id": "d1",
                   "question": "在 web_facts 表中查该用户名的判定"}])
        check("维度问句点名了表、实际查了 → 绑定", ok8 is True)
    else:
        print("  ⚠️ 本地无场景库（L/scene.duckdb）→ 表名这一类跳过"
              "（守卫认不出表名就不判起）")

    # 不依赖真库的一类：**文件名**（source_binding 的 file 类纯靠扩展名识别）
    ok7f, why7f = C._source_binding_gate(
        "这个用户名在 GitHub 有没有账号？",
        [{"tool": "gh_repo_info", "ok": True, "args": "owner/repo",
          "text": "x"}],
        "", dims=[{"id": "d1", "question": "README.md 里写的更新时间是什么"}])
    check("维度问句点名了文件、实际没取 → 不绑定", ok7f is False,
          str(why7f)[:70])
    ok8f, _ = C._source_binding_gate(
        "这个用户名在 GitHub 有没有账号？",
        [{"tool": "read_file", "ok": True, "args": "README.md",
          "text": "Updated 2026-03-26"}],
        "", dims=[{"id": "d1", "question": "README.md 里写的更新时间是什么"}])
    check("维度问句点名了文件、实际取了 → 绑定", ok8f is True)

    print("== 4. 接线：消费侧确实调了守卫 ==")
    src = Path(__file__).resolve().parent.parent / "voyager_vlml" / "consume.py"
    _all = src.read_text(encoding="utf-8")
    check("consume 里定义了 _source_binding_gate",
          "def _source_binding_gate(" in _all)
    check("consume 主流程调用了它",
          "_source_binding_gate(" in _all)
    check("守卫用**同一份实现**（不是新写的判据）",
          "import source_binding as _sb" in _all
          and "_sb.check(" in _all)
    check("判据看入参（args）不看返回文本",
          "_sb.check(t, evidence, db_path)" in _all)

    print("== 5. 消费侧完成判定仍是确定性的（不新增 LLM critic）==")
    check("critic_ok = data is not None（确定性）",
          "critic_ok = data is not None" in _all)
    # 守卫本身不许调 LLM
    _gate_src = _all[_all.find("def _source_binding_gate("):
                     _all.find("def _source_binding_gate(") + 3000]
    check("守卫里没有 LLM 调用（不花判官钱）",
          "chat_json" not in _gate_src and "chat(" not in _gate_src)
    # 学习侧与消费侧调的是同一个 check
    _lo = (Path(__file__).resolve().parent / "learn_orchestrate.py").read_text(
        encoding="utf-8")
    check("学习侧也在用同一把尺子（source_binding.check）",
          "_sb2.check(" in _lo or "source_binding.check" in _lo)

    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
