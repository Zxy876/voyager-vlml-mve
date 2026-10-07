#!/usr/bin/env python3
"""自适应学习驱动：清库 → 摸底 → 练最弱的 → 撤支架重考 → 曲线上升。

为什么单开一个文件
------------------
`run_mve.py` 是「跑一次」，`exam.py` 是「考一次」。而要的那条线是
**两者的交替** —— 交替的节奏（练几轮考一次、考哪道、什么时候换题）
既不属于出题器也不属于考核器，它是第三个东西：学习进程调度。

    学习单元 = 练一题（**给**知识图谱） + 重考同一题（**撤掉**知识图谱）

曲线画的是**重考**那一列的覆盖率。练习那一列恒 100%（图谱把口径、列含义、
结构骨架全喂进 prompt），把它连成线只会得到一条从第一行就贴顶的平线 ——
好看，但是假的。

伴学这边对应的节奏是「选最弱的知识点 → 出题 → 判 → 更新掌握度 → 再选」。
它不需要"撤支架"这一步，因为它的题本来就不喂图谱；MVE 多了一层图谱支架，
所以必须多一步才能测出真水平。这是本文件唯一的自创部分。

用法
----
    python mve/learn.py --clear              清库（含裸考记录）
    python mve/learn.py --place              摸底：没考过的题全考一遍
    python mve/learn.py --units 8            跑 8 个学习单元
    python mve/learn.py --progress           学习进程（按题分行）
    python mve/learn.py --go 8               一键：清库 → 摸底 → 8 单元 → 进程
    python mve/learn.py --go 8 --transfer 3  每 3 单元加考一道本单元没练的题

`--transfer` 是防自欺的：**曲线上升有两种解释** —— 记住了这道题，
或者真学会了。前者在没练过的题上不涨，后者会涨。不加这组对照，
画出来的上升证明不了任何东西。宁可跑出来是「没迁移」然后如实写，
也不能只挑好看的那组数据。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

LOG_PATH = HERE / "learn.log"

import vlml_env  # noqa: F401,E402  必须先引导环境

import exam  # noqa: E402
import planner  # noqa: E402
import run_log  # noqa: E402
from tasks import TASKS  # noqa: E402


def _log(text: str) -> None:
    """练习轮的完整输出进日志，控制台只留摘要 —— 否则一个单元上百行。"""
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(text)


def _clear() -> None:
    import reset_all
    old = sys.argv[:]
    sys.argv = ["reset_all.py"]
    try:
        reset_all.main()
    finally:
        sys.argv = old


async def _practice(topic_id: str, rounds: int = 1) -> None:
    """练一题（带图谱）。直接调 run_mve.main()，不另写一套。"""
    import run_mve
    old = sys.argv[:]
    sys.argv = ["run_mve.py", "--llm", "--topic", topic_id,
                "--rounds", str(rounds), "--no-gen"]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            await run_mve.main()
    finally:
        sys.argv = old
        # 每单元开头都该打印一次裁判答案，否则日志里只有第一个单元可核对
        if hasattr(run_mve.main, "_ref_shown"):
            delattr(run_mve.main, "_ref_shown")
        _log(f"\n{'=' * 70}\n[{topic_id}] 练习轮（给图谱）\n{'=' * 70}\n"
             + buf.getvalue())


def _practice_count(topic_id: str) -> int:
    return sum(1 for r in run_log.load_all()
               if str(r.get("topic_id") or "") == topic_id)


def _last_practice_cov(topic_id: str) -> float | None:
    rows = [r for r in run_log.load_all()
            if str(r.get("topic_id") or "") == topic_id]
    if not rows:
        return None
    v = rows[-1].get("coverage")
    return None if v is None else float(v)


def _transfer_pick(exclude: str) -> tuple[str, int]:
    """挑一道**对照题**：优先从来没练过，其次练得最少（且不是刚练的那道）。

    它的用途是分辨"记住了这道题"和"真学会了"，所以必须避开本单元刚练的题。
    """
    counts = {t: _practice_count(t) for t in TASKS}
    fresh = [t for t in TASKS if t != exclude and counts.get(t, 0) == 0]
    if fresh:
        # 未练过的里挑裸考最低的（有裸考记录的优先；没有就按难度最低）
        prof = exam.profile()
        fresh.sort(key=lambda t: (float(prof[t]["coverage"])
                                  if t in prof else 1.0,
                                  int(getattr(TASKS[t], "difficulty", 2) or 2), t))
        return fresh[0], 0
    rest = [t for t in TASKS if t != exclude]
    rest.sort(key=lambda t: (counts.get(t, 0), t))
    return rest[0], counts.get(rest[0], 0)


async def unit(i: int, n: int, *, rounds: int = 1,
               transfer_every: int = 0) -> None:
    sel = planner.select_next()
    topic = str(sel["topic_id"])
    reason = planner.REASON_LABEL.get(str(sel["reason"]), str(sel["reason"]))
    base = exam.true_level(topic)

    print(f"\n{'─' * 74}")
    print(f"单元 {i}/{n}  选题 {topic}")
    print(f"  [{reason}] {sel.get('explanation', '')}")
    print(f"  难度 {TASKS[topic].difficulty} → 目标 {sel.get('difficulty_target')}"
          f" · 支架 {sel.get('hint')}")

    await _practice(topic, rounds)
    prac = _last_practice_cov(topic)
    rec = await exam.exam(topic, verbose=False, kind="practice_exam")
    cov = float(rec.get("coverage") or 0.0)
    d = "" if base is None else f"  Δ {cov - base:+.0%}"
    print(f"  练习 {prac if prac is None else f'{prac:.0%}'}（带图谱） → "
          f"裸考重考 {cov:.0%}"
          f"{'' if base is None else f'（摸底 {base:.0%}）'}{d}"
          + ("  ⟲复用技能库里的程序" if rec.get("reused_skill") else ""))
    if rec.get("missing"):
        print(f"     仍缺：{rec['missing'][:2]}")

    if transfer_every and i % transfer_every == 0:
        t, k = _transfer_pick(topic)
        trec = await exam.exam(t, verbose=False, kind="transfer",
                               extra={"practiced_before": k})
        tcov = float(trec.get("coverage") or 0.0)
        tbase = exam.true_level(t)
        print(f"  ⇄ 迁移对照 {t}（本单元没练"
              + (f"，此前也没练过" if k == 0 else f"，此前练过 {k} 次")
              + f"）：{tcov:.0%}"
              + (f"（摸底 {tbase:.0%}）" if tbase is not None else ""))


async def run_units(n: int, *, rounds: int = 1, transfer_every: int = 0) -> None:
    for i in range(1, n + 1):
        await unit(i, n, rounds=rounds, transfer_every=transfer_every)
    print()


def _place() -> int:
    # 摸底的前提是技能库为空（零基础上的水平）—— 这道闸是硬的，
    # 因为假摸底会把真摸底覆盖掉，画像无声无息就废了（exam.placement_blocked）。
    why = exam.placement_blocked()
    if why:
        print(f"摸底中止：{why}")
        return 1
    ids = exam.unplaced()
    if not ids:
        print("题库里每道题都已经有裸考记录了（要重测先 --clear，并想清楚"
              "重测会用「练过之后的水平」覆盖掉零基础的摸底值）。")
        return 0
    print(f"摸底：{len(ids)} 道题，全部撤掉知识图谱考一遍\n")
    for t in ids:
        rec = asyncio.run(exam.exam(t, verbose=True, kind="placement"))
        if rec.get("error"):
            print(f"\n摸底中止：{rec['error']}")
            return 1
    print()
    exam.print_profile()
    return 0


def _main() -> int:
    ap = argparse.ArgumentParser(description="自适应学习驱动：摸底 → 练最弱 → 撤支架重考")
    ap.add_argument("--clear", action="store_true", help="清库（含裸考记录）")
    ap.add_argument("--place", action="store_true", help="摸底：没考过的题全考一遍")
    ap.add_argument("--units", type=int, default=0, help="跑几个学习单元")
    ap.add_argument("--rounds", type=int, default=1, help="每单元练几轮（默认 1）")
    ap.add_argument("--transfer", type=int, default=0,
                    help="每 N 单元加考一道本单元没练的题（迁移对照）")
    ap.add_argument("--progress", action="store_true", help="打印学习进程")
    ap.add_argument("--curve", action="store_true", help="打印考核流水")
    ap.add_argument("--go", type=int, default=0,
                    help="一键：清库 → 摸底 → 跑 N 个单元 → 打印进程")
    args = ap.parse_args()

    if args.go:
        print("=" * 74)
        print(f"  一键跑：清库 → 摸底 → {args.go} 个学习单元")
        print("=" * 74)
        _clear()
        _place()
        asyncio.run(run_units(args.go, rounds=args.rounds,
                              transfer_every=args.transfer))
        exam.progress()
        return 0

    did = False
    if args.clear:
        _clear()
        did = True
    if args.place:
        _place()
        did = True
    if args.units:
        asyncio.run(run_units(args.units, rounds=args.rounds,
                              transfer_every=args.transfer))
        exam.progress()
        did = True
    if args.progress:
        exam.progress()
        did = True
    if args.curve:
        exam.curve()
        did = True
    if not did:
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
