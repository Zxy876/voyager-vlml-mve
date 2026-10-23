#!/usr/bin/env python3
"""按场景选执行环境（外部 task 闭环与学习循环共用的一处判定）。

为什么需要这一层
----------------
`voyager_vlml/loop.py:389` 与 `mve/task_intake.py` 原来各自 `VLMLMCPEnv()` ——
**默认库 = 全局 db_config（rib）**。于是「外部任务」页签上问一条工资题，
它去查的是 Valorant 的表：必然失败，而且失败原因会被记成「它学不会」，
真实原因是**查错了库**。这类"错在取数侧、账落在学习侧"的账最误导人。

判定沿用场景三分（`mve/scene_profile.py`，唯一定义处，不另立标准）：

    kind=vlml   → VLMLMCPEnv()                （Scene A：VLML 数据域）
    kind=scene  → SceneDBEnv(db_path)          （场景目录里有 .duckdb）
    kind=local  → 无 SQL 环境（只有本机文件原语，走「人导入」那条链路）
    kind=missing/none → 无环境，如实报原因

本模块只做「选哪个」，不复制任何取数逻辑。
"""

from __future__ import annotations

from typing import Any


def kind_of(scene_id: str = "") -> str:
    """场景的数据源类型（vlml / scene / local / missing），判定只走 scene_profile。"""
    import scene_profile

    return str(scene_profile.profile(scene_id).get("kind") or "")


def env_for_scene(scene_id: str = "") -> tuple[Any | None, dict[str, Any], str]:
    """返回 (env, profile, why)。env=None 时 why 说明为什么没有环境。"""
    import scene_profile

    prof = scene_profile.profile(scene_id)
    kind = str(prof.get("kind") or "")

    if kind == scene_profile.VLML:
        try:
            from voyager_vlml.env.vlml_mcp import VLMLMCPEnv

            return VLMLMCPEnv(), prof, ""
        except Exception as e:  # noqa: BLE001
            return None, prof, f"VLML 环境装载失败：{type(e).__name__}: {e}"

    if kind == scene_profile.SCENE:
        try:
            from scene_db import SceneDBEnv

            return SceneDBEnv(str(prof.get("db_path") or "")), prof, ""
        except Exception as e:  # noqa: BLE001
            return None, prof, f"场景库环境装载失败：{type(e).__name__}: {e}"

    if kind == scene_profile.LOCAL:
        return None, prof, ("本机场景：场景目录里没有 .duckdb，只有文件原语"
                            "（list_files / read_file）。要用 SQL 学，把 .duckdb 放进 "
                            f"{prof.get('scene_dir') or 'scene_data/<场景>/'}；"
                            "纯文件/知识型问题请走「人导入 → 交给 Voyager 回答」。")

    return None, prof, str(prof.get("why") or f"场景 {prof.get('scene_id')} 没有可用数据源")
