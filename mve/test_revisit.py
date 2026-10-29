#!/usr/bin/env python3
"""学习完成 → 自动回访（revisit.py）单测 —— 2026-10-29 用户拍板。

痛点原话：「缺一个信号 —— MVE 什么时候学好了、可以来消费了；学好了就在
对话里重新回答人排队待解决的问题，不需要人去总是调试帮它学习（浪费时间）」。

闭环：
  consume 没答出来且已拉起学习 → 排队（revisit_queue）
  学习真学出东西（learned/partial）→ 信号 → 自动重答（auto_learn=False）
  答出来 → 出队 + 落回访账（对话页拉取展示）+ run_log(mode=revisit)
  仍答不出 → attempts+1，满 3 出队（不无限烧钱）

本测试钉住：排队/出队账目、attempts 上限、重入保护、消费接入点
（auto_learn 门控 + mark_pending）、信号挂点（_record 唯一收账处）、
面板拉取口（/api/revisit + 对话页 pullRevisit）。
跑法：python mve/test_revisit.py
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(HERE), str(REPO), str(REPO / "voyager_vlml")):
    if p not in sys.path:
        sys.path.insert(0, p)

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    import revisit as R

    # ⚠️ 信号/门控的源码断言要在**打桩之前**做 —— patch 之后
    # inspect.getsource(C.consume) 拿到的是桩函数的源码（吃过这个亏）。
    import voyager_vlml.consume as C
    import learn_orchestrate as L
    import dashboard as D
    import chat_page as CP
    _sig = str(inspect.signature(C.consume))
    _src_consume = inspect.getsource(C.consume)

    tmp = Path(tempfile.mkdtemp())
    R.QUEUE = tmp / "revisit_queue.json"
    R.LOG = tmp / "revisit_log.json"
    _logrows: list[dict] = []

    class _FakeRunlog:
        @staticmethod
        def append(row):
            _logrows.append(row)

    R._runlog = _FakeRunlog

    print("== ① 排队账：同题只记一次，无历史不重复 ==")
    R.mark_pending("T", "九月份一共要支付多少钱")
    R.mark_pending("T", "九月份一共要支付多少钱")
    R.mark_pending("T", "技术部津贴多少")
    check("两条排队（同题去重）", len(R.pending("T")) == 2, f"{len(R.pending('T'))}")
    check("attempts 初始 0", all(e["attempts"] == 0 for e in R.pending("T")))
    check("场景过滤", len(R.pending("ZZZ")) == 0)

    print("== ② 自动重答：答出来 → 出队 + 落账 + run_log ==")
    calls: list[dict] = []

    async def _ok_consume(q, **kw):
        calls.append({"q": q, **kw})
        return {"judgment_result": "written", "answer": "163,100 元"}

    C.consume = _ok_consume
    _s = R.revisit_pending("T", limit=5)
    check("两条都试了", _s.get("checked") == 2, str(_s))
    check("都解决了", _s.get("resolved") == 2)
    check("队列清空", len(R.pending("T")) == 0)
    check("回访用 auto_learn=False（失败不连环拉学习）",
          calls and all(kw.get("auto_learn") is False for kw in calls))
    check("回访账落了 ok 条目（待对话页拉取）",
          len([e for e in json.loads(R.LOG.read_text("utf-8")) if e["ok"]]) == 2)
    check("run_log 有 mode=revisit 行", len(_logrows) == 2
          and all(r.get("mode") == "revisit" for r in _logrows))
    check("成功行 verdict=ok/success=True",
          all(r.get("verdict") == "ok" and r.get("success") for r in _logrows))

    print("== ③ 拉走即标记已送达（对话页不重复弹） ==")
    first = R.deliver("T")
    check("首次拉到 2 条", len(first) == 2)
    check("再次拉为空（已标记 delivered）", len(R.deliver("T")) == 0)

    print("== ④ 仍答不出 → attempts+1；满 3 出队（不无限烧钱） ==")
    R.mark_pending("T", "这道题很难")

    async def _fail_consume(q, **kw):
        return {"judgment_result": "fetched_empty", "answer": "没答出来"}

    for i in range(5):
        C.consume = _fail_consume
        R.revisit_pending("T", limit=5)
        n = len(R.pending("T"))
        if n == 0:
            check(f"第 {i + 1} 次回访后出队（MAX_ATTEMPTS={R.MAX_ATTEMPTS}）", True)
            break
    else:
        check("满 3 次后出队", False, "仍在队列里")
    check("失败也落了账（verdict 不是 ok）",
          any(not e["ok"] for e in json.loads(R.LOG.read_text("utf-8"))))

    print("== ⑤ 重入保护：同时刻只跑一个回访 ==")
    R.mark_pending("T", "排队题乙")
    C.consume = _ok_consume
    R._RUNNING = True
    _s = R.revisit_pending("T", limit=5)
    check("正在跑时直接跳过", _s.get("skipped") == "已有回访在跑", str(_s))
    R._RUNNING = False

    print("== ⑥ 消费接入点：auto_learn 门控 + 排队（源码在打桩前已截取） ==")
    check("consume 签名带 auto_learn", "auto_learn" in _sig, _sig[:120])
    check("两处 kick 都被 auto_learn 门控",
          _src_consume.count("and not auto_learn") >= 2,
          f"{_src_consume.count('and not auto_learn')} 处")
    check("拉起学习成功 → mark_pending 排队",
          _src_consume.count("mark_pending") >= 2)
    src_rec = inspect.getsource(L._record)
    check("信号挂在 _record（所有学习路径唯一收账点）",
          "revisit_pending" in src_rec and "learned" in src_rec)
    check("只在 learned/partial 触发", '"learned", "partial"' in src_rec
          or "'learned', 'partial'" in src_rec)

    print("== ⑦ 面板拉取口与对话页 ==")
    check("GET /api/revisit 路由存在",
          '"/api/revisit"' in inspect.getsource(D))
    src_chat = inspect.getsource(CP)
    check("对话页会拉回访结果", "pullRevisit" in src_chat
          and "/api/revisit" in src_chat)
    check("回访气泡带「自动回访」前缀（人知道这是学习后补答的）",
          "自动回访" in src_chat)
    check("pending 提示不重复堆（记过就不再记）", "some(function(m)" in src_chat)

    if FAILS:
        print(f"✗ 失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
