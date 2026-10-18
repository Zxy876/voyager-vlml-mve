#!/usr/bin/env python3
"""MVE 1→n 泛化：可插拔场景命名空间（Scene Namespace）。

设计定位
--------
这是比 `scene_context.py` 更高一层的概念。**两者正交**，不要混淆：

  · `scene_context.py` 的「scene」= **数据世界渐进披露**（biome / nearby_blocks /
    inventory / chests），作用于「单个数据源内部」—— 决定模型当前能看到哪些表、
    哪些工具、哪些实体取值。
  · 本文件的 `Scene` = **可插拔的业务场景命名空间**（设计里的 Scene A / Scene B）。
    它包住一整条学习资产：自己的知识图谱根节点 + 共享但带 `source_scene` 标记的
    技能库 + 独立的会话状态（loop_state / 游标）。

为什么需要它
------------
设计核心命题：「任意用户场景作为可插拔件」。一个场景插入时**不重启、不清库、
不重训**，只是向共享的 Skill Store 与 Knowledge Graph 注册一个新的命名空间 /
根节点。两边图谱与技能同时在库，切回 A 时 A 的会话状态与续跑点还在。

数据模型
--------
scenes.json（与 skill_store.json / knowledge_graph.json 同目录）：
    {
      "version": 1,
      "current": "A",                      # 当前激活场景
      "scenes": {                          # 命名空间注册表
        "A": {"id":"A","name":"Voyager学VLML","root_node":"scene:A",
              "description":"...","created_at":...},
        "B": {...}
      },
      "state": {                           # 每场景独立会话状态
        "A": {"loop_state": {}, "cursor": null, "last_topic": null},
        "B": {...}
      }
    }

约定
----
  · 默认场景固定为 "A"（Voyager学VLML）。首次运行自动建好，不清库即可用。
  · 技能库是**全局共享**的，每条技能带 `source_scene`（见 skill_store）。
  · 知识图谱是**动态生长**的，每个节点带 `root_scene`（见 knowledge_graph），
    只有 `root_scene == 该场景` 的节点才算「场景专属资产」。跨场景复用的技能
    即使被 B 用到，它的节点/技能仍归属 A。
  · 会话状态**独立维护**：切回 A 必须恢复 A 的 loop_state 与游标。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REGISTRY = HERE / "scenes.json"

# 默认场景（A）—— 不清库时的基座
DEFAULT_SCENE_ID = "A"
DEFAULT_SCENE_NAME = "Voyager学VLML"
DEFAULT_SCENE_DESC = "主场景：Voyager 学 VLML（Valorant 数据）"


def _empty() -> dict[str, Any]:
    now = time.time()
    return {
        "version": 1,
        "current": DEFAULT_SCENE_ID,
        "scenes": {
            DEFAULT_SCENE_ID: {
                "id": DEFAULT_SCENE_ID,
                "name": DEFAULT_SCENE_NAME,
                "root_node": f"scene:{DEFAULT_SCENE_ID}",
                "description": DEFAULT_SCENE_DESC,
                "created_at": now,
            }
        },
        "state": {
            DEFAULT_SCENE_ID: {
                "loop_state": {}, "cursor": None, "last_topic": None,
            }
        },
    }


def _load() -> dict[str, Any]:
    if not REGISTRY.exists():
        data = _empty()
        _save(data)
        return data
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = _empty()
        _save(data)
        return data
    if not isinstance(data, dict) or not isinstance(data.get("scenes"), dict):
        data = _empty()
    for k, v in _empty().items():
        data.setdefault(k, v)
    # 保证默认场景与默认状态键存在（老文件/部分初始化兼容）
    data["scenes"].setdefault(
        DEFAULT_SCENE_ID, _empty()["scenes"][DEFAULT_SCENE_ID])
    data["state"].setdefault(DEFAULT_SCENE_ID,
                             {"loop_state": {}, "cursor": None, "last_topic": None})
    return data


def _save(data: dict[str, Any]) -> None:
    REGISTRY.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- 注册表查询 ----------------

def list_scenes() -> list[dict[str, Any]]:
    """全部已注册场景（含默认 A）。"""
    data = _load()
    return [dict(v) for v in data["scenes"].values()]


def current_scene_id() -> str:
    data = _load()
    cur = data.get("current") or DEFAULT_SCENE_ID
    if cur not in data["scenes"]:
        cur = DEFAULT_SCENE_ID
    return cur


def current_scene() -> dict[str, Any]:
    data = _load()
    return dict(data["scenes"][current_scene_id()])


def get_scene(scene_id: str) -> dict[str, Any] | None:
    data = _load()
    return dict(data["scenes"].get(scene_id)) if scene_id in data["scenes"] else None


# ---------------- 场景操作 ----------------

def create_scene(name: str, *, description: str = "", scene_id: str | None = None) -> str:
    """插入一个新场景（Scene B / C / …）。

    设计原则：**不重启、不清库、不重训**。这里只是注册一个新的命名空间 /
    根节点。技能库与知识图谱是共享资产，新场景初始只有根节点（图谱为空），
    随消费侧提问动态挂载节点。

    返回新建场景的 id。若未指定 scene_id，按现有数量自动编号为 A、B、C…。
    """
    data = _load()
    if not scene_id:
        used = set(data["scenes"].keys())
        for cand in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if cand not in used:
                scene_id = cand
                break
        if not scene_id:
            scene_id = f"S{len(used) + 1}"
    scene_id = str(scene_id).strip() or DEFAULT_SCENE_ID
    if scene_id in data["scenes"]:
        raise ValueError(f"场景 {scene_id} 已存在")
    now = time.time()
    data["scenes"][scene_id] = {
        "id": scene_id,
        "name": name or f"场景 {scene_id}",
        "root_node": f"scene:{scene_id}",
        "description": description or "",
        "created_at": now,
    }
    # 独立会话状态初始化
    data["state"][scene_id] = {
        "loop_state": {}, "cursor": None, "last_topic": None,
    }
    _save(data)
    return scene_id


def switch_scene(scene_id: str) -> dict[str, Any]:
    """切到目标场景，并把该场景设为 current。

    设计承诺：切回 A 时 A 的 loop_state 与游标仍在 —— 因为它们各自独立存储。
    """
    data = _load()
    if scene_id not in data["scenes"]:
        raise KeyError(f"场景 {scene_id} 不存在")
    data["current"] = scene_id
    data["state"].setdefault(scene_id,
                            {"loop_state": {}, "cursor": None, "last_topic": None})
    _save(data)
    return dict(data["scenes"][scene_id])


# ---------------- 会话状态隔离 ----------------

def get_state(scene_id: str | None = None) -> dict[str, Any]:
    """读取某场景的会话状态（loop_state / cursor / last_topic）。

    不传 scene_id 取当前场景。
    """
    data = _load()
    sid = scene_id or current_scene_id()
    return dict(data["state"].get(
        sid, {"loop_state": {}, "cursor": None, "last_topic": None}))


def set_state(state: dict[str, Any], scene_id: str | None = None) -> None:
    """写回某场景的会话状态。切场景前先 set_state(当前)，切回时再 set_state(目标)。"""
    data = _load()
    sid = scene_id or current_scene_id()
    data["state"].setdefault(sid,
                            {"loop_state": {}, "cursor": None, "last_topic": None})
    merged = dict(data["state"][sid])
    merged.update(state)
    data["state"][sid] = merged
    _save(data)


def scene_root_node(scene_id: str | None = None) -> str:
    """该场景的根节点 id（用于挂载子图）。"""
    s = get_scene(scene_id or current_scene_id())
    return s["root_node"] if s else f"scene:{scene_id or DEFAULT_SCENE_ID}"


def delete_scene(scene_id: str) -> None:
    """删除一个非默认场景（只移除命名空间与状态，不删共享资产里它的节点/技能）。"""
    if scene_id == DEFAULT_SCENE_ID:
        raise ValueError("默认场景 A 不可删除")
    data = _load()
    data["scenes"].pop(scene_id, None)
    data["state"].pop(scene_id, None)
    if data["current"] == scene_id:
        data["current"] = DEFAULT_SCENE_ID
    _save(data)


# ---------------- 命令行自检 ----------------

def _main() -> int:
    print("=== MVE 场景命名空间 ===\n")
    print(f"当前场景：{current_scene_id()} → {current_scene().get('name')}")
    print("已注册场景：")
    for s in list_scenes():
        print(f"  [{s['id']}] {s['name']}  root={s['root_node']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
