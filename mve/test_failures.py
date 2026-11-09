#!/usr/bin/env python3
"""失败经验沉淀 + 理解层自检的测试（2026-10-29 用户拍板「搬」①+②）。

跑法：python mve/test_failures.py（服务器无 pytest，同 test_*.py 口径）。
"""

import importlib
import json
import os
import sys
import tempfile

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
    import failures

    # 账本指向临时文件（别碰真账）
    _tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    _tmp.close()
    os.unlink(_tmp.name)
    failures.LEDGER = __import__("pathlib").Path(_tmp.name)

    print("== 1. record / recall ==")
    os.unlink(failures.LEDGER) if os.path.exists(failures.LEDGER) else None
    check("空值不记", not failures.record("B", "", "why", "fit"))
    check("空判词不记", not failures.record("B", "问题", "", "fit"))
    check("记一条", failures.record("B", "这家公司待遇怎么样",
                                    "只给 infer 没走 db", "plan"))
    check("同 (q,why) 去重",
          not failures.record("B", "这家公司待遇怎么样",
                              "只给 infer 没走 db", "plan"))
    failures.record("B", "九月份要支付多少钱",
                    "主体答窄了：SQL 过滤了某部门", "fit")
    failures.record("C", "别的场景的问题", "别的场景的错", "fit")
    r = failures.recall("B", "这家公司的待遇好不好")
    check("词面相似召回", len(r) >= 1 and "infer" in r[0]["why"],
          str(r[:1]))
    r2 = failures.recall("B", "九月的总支付金额")
    check("场景隔离（C 的错不进 B）",
          all("别的场景" not in e["why"] for e in r2), str(len(r2)))
    check("无关问题不硬凑",
          not failures.recall("B", "什么是递归") or
          all(e.get("score", 1) < 0.5 for e in failures.recall("B", "什么是递归")))

    print("== 2. recall_block ==")
    blk = failures.recall_block("B", "这家公司的待遇好不好")
    check("有失败 → 返回注入块", "失败判词" in blk and "infer" in blk)
    check("无关问题 → 空串（不挂废话）",
          failures.recall_block("B", "什么是闭包") == "" or
          failures.recall_block("ZZZ不存在", "任何问题") == "")

    print("== 3. 封顶与持久化 ==")
    for i in range(60):
        failures.record("X", f"问题 {i}", f"错误 {i}", "fit")
    data = json.loads(failures.LEDGER.read_text(encoding="utf-8"))
    check("每场景封顶 50", len(data.get("X", [])) == 50,
          str(len(data.get("X", []))))
    check("新的挤旧的（最早 9 条被挤掉）",
          data["X"][0]["q"] == "问题 10", data["X"][0]["q"])

    print("== 4. 损坏留证 ==")
    failures.LEDGER.write_text("{broken json!", encoding="utf-8")
    check("损坏 → 空账不炸", failures.recall("B", "待遇") == [])
    broken = list(failures.LEDGER.parent.glob(
        failures.LEDGER.stem + ".broken.*"))
    check("损坏文件改名留证", len(broken) >= 1)
    for p in broken:
        p.unlink()

    print("== 5. clear ==")
    failures.record("Y", "问题", "错误", "fit")
    check("单场景清账", failures.clear("Y") == 1)
    failures.record("Y", "问题2", "错误2", "fit")
    failures.record("Z", "问题3", "错误3", "fit")
    check("全清（跨场景）", failures.clear() == 2)
    check("全清后账本空", failures.recall("B", "待遇") == [])

    print("== 6. consume 接线（源码断言，打桩前截取）==")
    src = open(__import__("voyager_vlml.consume", fromlist=["x"]).__file__,
               encoding="utf-8").read()
    check("规划注入失败经验块", "failures.recall_block" in src
          and "fail_block" in src)
    check("判官退回记账", '"fit"' in src and "判官退回" in src)
    check("重写仍败记账", '"rewrite"' in src)
    check("组合没取到数记账", '"compose"' in src)
    # 2026-11-09：规划员已整体删除（_plan_routes / _plan_selfcheck），
    # 信息路由改由**骨架**承担 —— 原版 maieutic 本来就没有「规划员」这层
    # （拆解即骨架）。下面断言的是新链路。
    check("路由由骨架承担（无规划员）",
          "_plan_from_scaffold" in src
          and "_plan_selfcheck(" not in src
          and "_plan_routes(" not in src)

    print("== 7. 骨架路由（_plan_from_scaffold）==")
    import voyager_vlml.consume as C
    _orig_dims = None
    try:
        import maieutic_scaffold as _ms
        _orig_dims = _ms.milestone_dimensions

        def _fake_dims(scene_id, question, *a, **kw):
            return [{"id": "d1", "question": "在 web_facts 表中查该用户名的判定",
                     "why": "x"}]

        _ms.milestone_dimensions = _fake_dims
        # 7a. 维度指向场景实有的表 → db 路
        out = C._plan_from_scaffold("某用户名在 GitHub 有没有账号", scene_id="L",
                                    has_file=False, scene_tables=["web_facts"])
        check("维度指向场景表 → 判 db 路",
              out.get("db") is True and out.get("source") == "scaffold")
        check("db 路不带 web", out.get("web") is False)

        # 7b. 维度指向外部 → web 路
        def _fake_web(scene_id, question, *a, **kw):
            return [{"id": "d1", "question": "该 username 在各站点的判定结论分别是什么",
                     "why": "x"}]

        _ms.milestone_dimensions = _fake_web
        out = C._plan_from_scaffold("某用户名在跨平台网站的账号存在性", scene_id="L",
                                    has_file=False, scene_tables=["web_facts"])
        check("维度指向外部站点 → 判 web 路", out.get("web") is True)

        # 7c. 没有骨架 → 如实不猜（不碰环境、不联网）
        _ms.milestone_dimensions = lambda scene_id, question, *a, **kw: []
        out = C._plan_from_scaffold("什么是递归", scene_id="L", has_file=False,
                                    scene_tables=["web_facts"])
        check("没有骨架 → 不猜题型（planned=False / db=False / web=False）",
              out.get("planned") is False and out.get("db") is False
              and out.get("web") is False)
        check("没有骨架 → 只做模型推断", out.get("infer") is True)
    finally:
        if _orig_dims is not None:
            import maieutic_scaffold as _ms2
            _ms2.milestone_dimensions = _orig_dims

    print(f"\n通过 {PASS} / 挂 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
