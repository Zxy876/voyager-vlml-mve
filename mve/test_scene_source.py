#!/usr/bin/env python3
"""「每个场景分配一个数据源」的验收（2026-10-28 用户拍板）。

用户原话：「我看了日志，我觉得我们是不是没做好数据源管理？给每个场景分配
一个数据源，会存放我们对话传上来的东西，检索逻辑一致。」

日志里的病灶：场景 G 配的 `/root/mve/心悦文集` 不存在 → 每道维度 0 秒
unreachable，循环跑满 160 轮、0 个里程碑。也就是说「场景有没有数据源」这件事
被留给了用户，用户没配/配错就等于没有环境，学习只能空转。

本文件的验收线：
  ① 每个非 VLML 场景都**有**数据源（`ensure_source`：<场景目录>/scene.duckdb）；
  ② 幂等且不制造第二个库（已有库就复用、显式配了就用配的）；
  ③ 配了但不存在的路径**不再判 missing**：降级到分配的数据源，并把
     「你配的那个不存在」如实写进 why（不假装配置有效）；
  ④ 上传的表落进的就是这个数据源 —— 学习/消费/出题/判官读同一个库，
     这就是「检索逻辑一致」；
  ⑤ 空库能被看出来（tables == []）—— 数据源分配好了但还没数据；
     **不额外塞"空库就早退"的补丁**（用户 2026-10-28：「数据源不空就不会
     空转，你不要刻意」）：根因是把数据放进去，不是给症状打补丁。

跑法：python mve/test_scene_source.py（本机缺 duckdb 会跳过，服务器上跑）。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(HERE), str(REPO), str(REPO / "voyager_vlml")):
    if p not in sys.path:
        sys.path.insert(0, p)

PASS = FAIL = 0
SID = "__src_test__"          # 专用测试场景（结束时整个目录删掉）


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    try:
        import duckdb                      # noqa: F401
    except Exception:
        print("（本机没有 duckdb —— 数据源分配用例跳过，服务器上跑）")
        return 0
    import scene_profile as SP

    d = Path(SP.scene_dir(SID))
    shutil.rmtree(d, ignore_errors=True)
    try:
        print("== 1. 分配：每个场景都有数据源 ==")
        p1 = SP.ensure_source(SID)
        check("返回了库路径", bool(p1), p1)
        check("落在场景目录里", Path(p1).parent == d, p1)
        check("文件真的建出来了", Path(p1).exists(), p1)
        p2 = SP.ensure_source(SID)
        check("幂等：再调一次还是同一个库", p2 == p1, p2)
        check("没制造第二个库",
              len(list(d.glob("*.duckdb"))) == 1,
              str([x.name for x in d.glob("*.duckdb")]))

        print("== 2. 复用：已有库 / 显式配置 ==")
        # 场景目录里只有别的库（老场景的 uploads.duckdb 之类）→ 复用它，
        # 不另建 scene.duckdb（一个场景一个库）
        (d / "scene.duckdb").unlink()
        other = d / "someone.duckdb"
        duckdb.connect(str(other)).close()
        check("已有库就复用（不另建）", SP.ensure_source(SID) == str(other),
              SP.ensure_source(SID))
        check("没多出 scene.duckdb",
              not (d / "scene.duckdb").exists(),
              str([x.name for x in d.glob("*.duckdb")]))
        other.unlink()
        # 显式 data_source 指向存在的文件 → 用户说了算
        explicit = d / "mine.duckdb"
        duckdb.connect(str(explicit)).close()
        orig = SP._scene
        SP._scene = lambda sid: {"data_source": str(explicit)}
        try:
            check("显式配置优先", SP.ensure_source(SID) == str(explicit),
                  SP.ensure_source(SID))
        finally:
            SP._scene = orig

        print("== 3. 配了但不存在 → 降级，不判 missing ==")
        SP._scene = lambda sid: {"data_source": "/root/根本没有这个目录/心悦文集"}
        try:
            prof = SP.profile(SID)
            check("kind 是 scene（不是 missing）", prof["kind"] == "scene",
                  str(prof["kind"]))
            check("给了分配的数据源",
                  Path(str(prof["db_path"])).parent == d, str(prof["db_path"]))
            check("如实写明你配的不存在",
                  "不存在" in str(prof.get("why") or ""), str(prof.get("why"))[:70])
            check("标了 source_missing（面板/日志能看见）",
                  bool(prof.get("source_missing")))
            check("空库能被看出来（还没数据，不是没数据源）",
                  prof.get("tables") == [], str(prof.get("tables")))
        finally:
            SP._scene = orig

        print("== 4. 上传进的就是这个数据源（检索逻辑一致） ==")
        import upload as U
        db_path, sdir, _kind = U._scene_db_path(SID)
        check("上传落库 == 分配的数据源",
              Path(db_path).resolve() == Path(SP.ensure_source(SID)).resolve(),
              f"{db_path} vs {SP.ensure_source(SID)}")
        r = U.ingest(SID, "测试.csv", "a,b\n1,2\n".encode("utf-8"))
        check("上传成功", bool(r.get("ok")), str(r.get("why") or "")[:60])
        tabs = SP.source_tables(SID)
        check("学习/消费侧看到的表里就有它", r.get("table") in tabs, str(tabs))
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
