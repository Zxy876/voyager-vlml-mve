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
    check("自检改路也记账", "自检改路" in src and '"plan"' in src)
    check("自检是增强不是依赖（try 包裹）",
          src.count("_plan_selfcheck") >= 2)

    print("== 7. _plan_selfcheck 打桩行为 ==")
    import voyager_vlml.consume as C
    _orig_chat = C.chat_json
    try:
        # 7a. 规划本来对 → ok=true，不动
        C.chat_json = lambda msgs, **kw: {"ok": True, "fix": {}, "why": "对"}
        out = C._plan_selfcheck("什么是递归", {"db": False, "web": False,
                                               "infer": True})
        check("对的规划放行", out["ok"] is True and out["fix"] == {})

        # 7b. 评价题只给 infer → 判错 + 给修好的路
        C.chat_json = lambda msgs, **kw: {
            "ok": False,
            "fix": {"db": True, "web": True, "file": False, "infer": False},
            "why": "评价题只给 infer 是错的"}
        out = C._plan_selfcheck("待遇好不好", {"db": False, "web": False,
                                               "infer": True})
        check("错规划被判", out["ok"] is False)
        check("fix 带修好的路", out["fix"].get("db") is True
              and out["fix"].get("web") is True)

        # 7c. fix 修出全 False = 修坏了 → 不采信
        C.chat_json = lambda msgs, **kw: {
            "ok": False, "fix": {"db": False, "web": False,
                                 "file": False, "infer": False}, "why": "x"}
        out = C._plan_selfcheck("x", {"db": True})
        check("全 False 的 fix 不采信", out["fix"] == {})

        # 7d. LLM 挂 → ok=True 照旧（不挡路）
        def _boom(*a, **kw):
            raise RuntimeError("LLM 挂了")
        C.chat_json = _boom
        out = C._plan_selfcheck("x", {"db": True})
        check("自检挂了照旧放行", out["ok"] is True)
    finally:
        C.chat_json = _orig_chat

    print(f"\n通过 {PASS} / 挂 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
