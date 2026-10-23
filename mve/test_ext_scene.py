#!/usr/bin/env python3
"""换数据源（工资场景）的接线测试：场景判定 → 执行环境 → CLI 分流。

只验接线，不烧 LLM、不连 MCP：
  · scene_profile 认不认「场景目录里的 duckdb」这条判定；
  · scene_env.env_for_scene 三种 kind 各自给出什么（vlml / scene / local）；
  · task_intake 在「没有可用 SQL 数据源」时如实拒绝，而不是回退 rib；
  · ext_task 的学一轮在非 VLML 场景走 scene_learn（围着失败的题学），
    VLML 场景仍走 voyager_vlml.loop（课程来自 VLML 图谱）。

跑法：python mve/test_ext_scene.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path[:0] = [str(HERE), str(REPO)]

PASS = 0


def ok(cond: bool, label: str, extra: str = "") -> None:
    global PASS
    if not cond:
        print("FAIL ", label, ("| " + extra) if extra else "")
        raise SystemExit(1)
    PASS += 1
    print("PASS ", label, ("| " + extra) if extra else "")


def _tmp_scene(root: Path, sid: str, duckdb: bool) -> Path:
    """在 root/<sid>/ 下造一个场景目录（有/没有 duckdb 两种）。"""
    d = root / sid
    d.mkdir(parents=True, exist_ok=True)
    if duckdb:
        try:
            import duckdb as _dd

            con = _dd.connect(str(d / "payroll.duckdb"))
            con.execute("CREATE TABLE employees(emp_id VARCHAR, base_salary DOUBLE)")
            con.execute("INSERT INTO employees VALUES ('E01', 22000)")
            con.close()
        except ImportError:
            # 本机没装 duckdb：判定只看「文件在不在」，建个空文件即可
            (d / "payroll.duckdb").write_bytes(b"")
    else:
        (d / "readme.txt").write_text("没有库，只有文件", encoding="utf-8")
    return d


def main() -> int:
    import scene_env
    import scene_profile

    # ---- ① 场景目录里有 duckdb → kind=scene，环境是 SceneDBEnv ----
    root = Path(tempfile.mkdtemp(prefix="scenes_"))
    _tmp_scene(root, "P", True)
    _tmp_scene(root, "Q", False)
    # 默认场景（VLML）必须是 A：profile 的第一条分支就是「sid == 默认场景」
    orig_scene = scene_profile._scene
    orig_default = scene_profile._default_scene_id
    orig_dir = scene_profile.SCENE_DATA_DIR
    try:
        scene_profile.SCENE_DATA_DIR = root
        scene_profile._scene = lambda sid: {"id": sid, "name": f"场景{sid}",
                                            "data_source": ""}
        scene_profile._default_scene_id = lambda: "A"

        prof = scene_profile.profile("P")
        ok(prof["kind"] == scene_profile.SCENE, "有 duckdb → kind=scene", str(prof["why"]))
        ok(prof["db_path"].endswith("payroll.duckdb"), "db_path 指向场景库", prof["db_path"])
        if (prof.get("tables") or []):
            ok("employees" in prof["tables"], "表名是实查来的", str(prof["tables"]))
        else:
            print("SKIP  表名实查（本机没装 duckdb，服务器上会验）")

        env, p2, why = scene_env.env_for_scene("P")
        ok(type(env).__name__ == "SceneDBEnv", "env = SceneDBEnv", type(env).__name__)
        ok(why == "", "有环境时不该给 why", why)
        ok(scene_env.kind_of("P") == "scene", "kind_of = scene")

        # ---- ② 场景目录里没有 duckdb → 没有 SQL 环境，如实说原因 ----
        prof2 = scene_profile.profile("Q")
        ok(prof2["kind"] == scene_profile.LOCAL, "没库 → kind=local", str(prof2["why"]))
        env2, _p, why2 = scene_env.env_for_scene("Q")
        ok(env2 is None, "本机场景不给 SQL 环境")
        ok("没有 .duckdb" in why2, "why 说清缺什么", why2[:60])

        # ---- ③ 配了不存在的路径 → missing，也如实报 ----
        scene_profile._scene = lambda sid: {"id": sid, "name": "坏的",
                                           "data_source": "nope/x.duckdb"}
        env3, p3, why3 = scene_env.env_for_scene("Z")
        ok(env3 is None and p3["kind"] == scene_profile.MISSING,
           "配了不存在的库 → missing + 无环境", str(why3)[:60])
    finally:
        scene_profile._scene = orig_scene
        scene_profile._default_scene_id = orig_default
        scene_profile.SCENE_DATA_DIR = orig_dir

    # ---- ④ ext_task：学一轮按 kind 分流 ----
    import ext_task

    kinds = {"P": "scene", "A": "vlml"}
    orig_kind = scene_env.kind_of
    scene_env.kind_of = lambda sid="": kinds.get(str(sid), "local")
    try:
        cmd_scene = ext_task._cmd_learn(4, "P", "技术部 2026-09 应发合计多少")
        ok("mve/scene_learn.py" in cmd_scene[1], "非 VLML 场景 → scene_learn",
           " ".join(cmd_scene[1:4]))
        ok("--question" in cmd_scene and "技术部" in " ".join(cmd_scene),
           "把失败的题带下去（围着它学）")
        ok("--scene" in cmd_scene and "P" in cmd_scene, "带 --scene P")

        cmd_vlml = ext_task._cmd_learn(4, "A", "随便")
        ok("-m" in cmd_vlml and "voyager_vlml.loop" in cmd_vlml,
           "VLML 场景 → voyager_vlml.loop（课程来自 VLML 图谱）", " ".join(cmd_vlml[1:3]))

        cmd_ask = ext_task._cmd_ask("问题", 4, "P")
        ok("--scene" in cmd_ask and cmd_ask[-1] == "P", "ask 带 --scene")
        cmd_ask0 = ext_task._cmd_ask("问题", 4, "")
        ok("--scene" not in cmd_ask0, "空场景不加 --scene（走当前场景）")
    finally:
        scene_env.kind_of = orig_kind

    # ---- ⑤ task_intake：没有 SQL 数据源时拒绝，不回退 rib ----
    import task_intake

    orig_env = scene_env.env_for_scene
    scene_env.env_for_scene = lambda sid="": (None, {"kind": "local", "scene_id": sid},
                                              "本机场景：没有 .duckdb")
    try:
        r = task_intake.ask("技术部 2026-09 应发合计多少", scene="Q")
    finally:
        scene_env.env_for_scene = orig_env
    ok(r.get("ok") is False, "无数据源时判失败")
    ok("没有可用的 SQL 数据源" in str(r.get("reason") or ""),
       "失败原因说的是数据源，不是『学不会』", str(r.get("reason"))[:60])

    print(f"\n全部通过（{PASS} 项）：换数据源的接线 —— 场景判定 / 执行环境 / CLI 分流")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
