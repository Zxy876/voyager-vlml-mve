#!/usr/bin/env python3
"""会话状态机 + append-only 事件流 —— 照抄原版 maieutic 的 `Session` / `SessionEvent`。

为什么需要（2026-11-09 用户判断）
--------------------------------
用户原话：「消费侧分不清什么是派任务，什么是验收（但是验收其实在学习侧就
已经跑完，只是没有答案披露）」「要做会话归档处理，召回会话而并非技能」。

原版 maieutic 的对照（已复核 `src/lib/sessions.ts:222-249` `advancePhase`）：

```ts
await prisma.session.update({ where: { id: sessionId },
  data: { currentPhase: to, ...(to === 4 ? { completedAt: new Date() } : {}) } });
await appendSessionEvent(sessionId, "phase_transition", { from, to });
```

⇒ 原版的「归档」= 三件事同时发生：
  ① `currentPhase = 4`（closed）
  ② `completedAt` 非空
  ③ 一条 `phase_transition` 事件

**没有归档表、没有归档文件、没有快照拷贝。** 全仓 `archive` / `归档` 零命中。
会话内容全在 `Session` 一行的 4 个 JSON 列里（`phase1Data` / `phase2Data` /
`phase3Data` / `liveSummaries`），唯一 append-only 的轨迹表是 `SessionEvent`
（`prisma/schema.prisma:76-86`）。

⇒ MVE 侧照抄对象**不是**「加一个归档表」，而是：
  ① 会话阶段（phase）—— 1 已拆骨架 / 2 学习中 / 3 已验待披露 / 4 已闭合归档
  ② append-only 事件流 —— 只追加，不覆盖
  ③ 归档 = completed_at 非空 + phase=4 + 一条 phase_transition 事件

**为什么事件流必须 append-only**（照抄原版 `SessionEvent`）：
`SessionEvent` 是原版**唯一**的 append-only 轨迹表。`advancePhase` 写事件时
用的是 `appendSessionEvent`（追加），从不改旧事件。
而 MVE 现有的三本账（learned / codes / iterations）是**覆盖式**的 ——
`save_progress` 明确注释「只改 learned 一个字段，不要整包替换 progress」。
那三本账是**状态**（当前值），事件流是**过程**（历史）。
两者都要：状态回答「现在怎么样」，过程回答「怎么走到这一步的」。

阶段语义（照抄原版 `currentPhase` 的 1/2/3/4）
---------------------------------------------
| phase | 原版 | MVE | 含义 |
|---|---|---|---|
| 1 | spec（追问澄清） | 已拆骨架 | 骨架已固定，维度清单在册 |
| 2 | writing（写代码） | 学习中 | 正在逐条闭合维度 |
| 3 | review（复盘） | 已验待披露 | 维度全闭合、验收跑完，**答案还没披露给消费侧** |
| 4 | closed | 已闭合归档 | completed_at 已写，可供召回 |

★ phase 3 是本刀的关键：**「验收跑完但没披露」在原版就是一个明确的阶段**。
MVE 之前没有这个概念 → 消费侧分不清「这题没学过」与「学过了但没告诉我」。

数据落点
--------
会话状态与事件流**住在骨架缓存同一个文件**里（同一把钥匙，`_cache_path`），
**不另开存储** —— 与 MVE 既有的「三本账同住 progress」完全同构：

    {
      "question": ..., "dimensions": [...],       # ← 已有
      "progress": {                               # ← 已有（三本账）
        "learned": [...], "codes": [...], "iterations": [...]
      },
      "session": {                                # ← 新增（刀 B）
        "phase": 2,
        "started_at": 1731...,
        "completed_at": null,
        "events": [                               # append-only
          {"kind": "phase_transition", "from": 1, "to": 2,
           "at": 1731...}
        ]
      }
    }

为什么与骨架同文件而不是独立文件：
- 骨架拆一次就固定（铁律），会话阶段也只在**同一个需求**上推进 ——
  分开存就会出现「骨架在这、会话在那」的串台风险
- 键已经是需求级的（刀 A），天然按需求隔离
- 少一份文件、少一次原子写、少一个坏文件面
"""

from __future__ import annotations

import time
from typing import Any

# 阶段常量（照抄原版 `Session.currentPhase` 的 1/2/3/4 语义）
PHASE_SPEC = 1        # 已拆骨架（维度清单固定）
PHASE_LEARNING = 2    # 学习中（逐条闭合维度）
PHASE_VERIFIED = 3    # 已验待披露（验收跑完，答案还没披露给消费侧）
PHASE_CLOSED = 4      # 已闭合归档（completed_at 已写）

PHASE_NAMES = {
    PHASE_SPEC: "已拆骨架",
    PHASE_LEARNING: "学习中",
    PHASE_VERIFIED: "已验待披露",
    PHASE_CLOSED: "已闭合归档",
}


def _session_of(data: dict[str, Any]) -> dict[str, Any]:
    """取（并就地建）会话段。返回的是**原对象引用**（改它等于改文件内容）。"""
    s = data.get("session")
    if not isinstance(s, dict):
        s = {}
        data["session"] = s
    return s


def read_session(data: dict[str, Any]) -> dict[str, Any]:
    """只读会话状态（不建、不改）。

    返回 `{"phase", "phase_name", "started_at", "completed_at",
    "events", "archived"}`。
    - `archived` = completed_at 非空（照抄原版「归档 = completedAt 非空」）
    """
    s = (data or {}).get("session")
    if not isinstance(s, dict):
        s = {}
    ph = int(s.get("phase") or 0)
    comp = s.get("completed_at")
    evs = [e for e in (s.get("events") or []) if isinstance(e, dict)]
    return {
        "phase": ph,
        "phase_name": PHASE_NAMES.get(ph, "未开始"),
        "started_at": float(s.get("started_at") or 0),
        "completed_at": float(comp) if comp else 0.0,
        "events": evs,
        "archived": bool(comp),
    }


def ensure_session(data: dict[str, Any]) -> dict[str, Any]:
    """确保会话段存在（第一次调用时记 started_at）。返回原对象引用。"""
    s = _session_of(data)
    if not s.get("started_at"):
        s["started_at"] = time.time()
    if not s.get("phase"):
        s["phase"] = PHASE_SPEC
    return s


def append_event(data: dict[str, Any], kind: str, payload: dict[str, Any]
                 | None = None) -> dict[str, Any]:
    """**追加**一条会话事件（append-only，照抄 `appendSessionEvent`）。

    ⚠️ 只追加，**从不覆盖或删除旧事件** —— 这是原版 `SessionEvent` 的语义，
    也是「会话归档」能作为证据链的前提。改写历史 = 毁掉证据。

    返回追加的事件 dict。
    """
    s = ensure_session(data)
    evs = s.get("events")
    if not isinstance(evs, list):
        evs = []
    ev = {"kind": str(kind or ""), "at": time.time(),
          "payload": dict(payload or {})}
    evs.append(ev)
    s["events"] = evs
    return ev


def set_phase(data: dict[str, Any], to: int, *,
              reason: str = "") -> dict[str, Any]:
    """推进会话阶段（照抄原版 `advancePhase`）。

    与原版同一条规则：
    - `from == to` → **直接返回，不写事件**（原版 `if (from === to) return;`）
    - 推进到 4（closed）→ **同时**写 `completed_at`（原版同一处 update）
    - 每次推进都**追加**一条 `phase_transition` 事件（append-only）

    ⚠️ 阶段**只许前进不许后退**：原版的 phase 语义就是流程推进。
      这里若发现 `to < from`（如实的倒退请求），**不执行**并如实返回原因 ——
      阶段倒退会让「归档」判断失效（completed_at 被清掉 = 证据消失）。

    返回 `{"ok", "from", "to", "why"}`。
    """
    s = ensure_session(data)
    frm = int(s.get("phase") or 0)
    to_i = int(to or 0)
    if to_i not in PHASE_NAMES:
        return {"ok": False, "from": frm, "to": to_i,
                "why": f"未知阶段 {to_i}"}
    if frm == to_i:
        return {"ok": True, "from": frm, "to": to_i, "why": "已在该阶段"}
    if frm and to_i < frm:
        return {"ok": False, "from": frm, "to": to_i,
                "why": (f"阶段不许后退（{frm} → {to_i}）—— "
                        "后退会让归档失效（completed_at 被清掉）")}
    s["phase"] = to_i
    if to_i == PHASE_CLOSED:
        s["completed_at"] = time.time()
    append_event(data, "phase_transition",
                 {"from": frm, "to": to_i, "reason": str(reason or "")})
    return {"ok": True, "from": frm, "to": to_i, "why": ""}


def note_dim_closed(data: dict[str, Any], dim_id: str, *,
                    r: float = -1.0, how: str = "") -> dict[str, Any]:
    """记一条「维度闭合」事件（append-only）。

    ⚠️ 这是**事件**，不改 `progress.learned` 状态 —— 状态由
      `save_progress` 写、事件由这里追加。两者分开，正是原版
      「状态（Session 的 JSON 列） vs 过程（SessionEvent）」的分层。
    """
    return append_event(data, "dim_closed",
                        {"dim_id": str(dim_id or ""),
                         "r": float(r), "how": str(how or "")})


def mark_verified(data: dict[str, Any], *, reason: str = "") -> dict[str, Any]:
    """标记「验收跑完、待披露」（phase 3）。

    ★ 这一步是用户那句「验收其实在学习侧就已经跑完，只是没有答案披露」
      的**显式化**：原版 phase 3 = review（复盘完成、结论已定），
      MVE 的 phase 3 = 验收跑完、答案待披露。消费侧读到 phase 3
      → 知道「这题学过了，别再派任务，去披露答案」。
    """
    return set_phase(data, PHASE_VERIFIED, reason=reason or "验收跑完")


def mark_archived(data: dict[str, Any], *, reason: str = "") -> dict[str, Any]:
    """归档（phase 4 + completed_at + phase_transition 事件）。

    照抄原版 `advancePhase(sid, 4)`：写 `currentPhase = 4` +
    `completedAt` + 一条 `phase_transition` 事件。**仅此而已** ——
    不写归档文件、不拷快照。
    """
    return set_phase(data, PHASE_CLOSED, reason=reason or "已闭合归档")
