#!/usr/bin/env python3
"""按需求 id 召回**会话**（照抄原版 maieutic 的 `findOrCreateSession` / `resolveSession`）。

用户 2026-11-09 的指令
----------------------
「所以要做会话归档处理，召回会话而并非技能」

为什么方向要反过来
------------------
MVE 现状（`consume.py:229-253` `_retrieve_skills` → `:1819` `_expand_session_skills`）：

    先按问句**向量检索技能** → 命中碎片技能 → 再**反查**它属于哪个会话
                                  → 把那个会话的组成全带出来

原版 maieutic（已复核 `src/lib/sessions.ts:132-170`）：

    先按 **(exerciseId, studentId) 精确匹配**找到会话 → 带出**全部组成**
    `findOrCreateSession` → `resolveSession`（纯函数）

差别不是细节，是**方向**：
- MVE 的检索键是**问句**（文本）→ 换个问法就检索不到
- 原版的检索键是**需求身份**（`Exercise.id`）→ 换问法不影响（会话 id 显式传递）

所以 MVE 现在「按会话检索」是**名义上的** —— 会话 key 是
`learn_<md5(question)[:8]>::milestone`（`learn_orchestrate.py:1295`），
**还是问句派生**，换问法照样断。

本模块做的事（照抄，不发明打分与门槛）
--------------------------------------
1. `recall(req_id, scene_id)` —— 按需求 id **精确**找到会话
   （对应原版 `findOrCreateSession` 的精确二元组匹配）
2. 会话 = 骨架缓存里那份**已验成果**（`progress.codes` 带 answer/r/how）
   + 骨架维度清单 + 会话状态（phase / 事件流）
3. 返回会话的全部组成（维度技能 key）+ **已验答案**（供刀 D 直接披露）

★ 为什么「会话」能当召回单位（用户那句「召回会话而并非技能」）：
  技能是**方法**（一段可复用的取数做法），会话是**过程**（为这个目标
  攒起来的一组方法 + 它们各自验过的结果）。
  - 方法可以跨需求复用 → 检索技能的思路没错，但**粒度不对**：
    消费侧要的是「这道题整体答得出来」，不是「有一条相关的方法」。
  - 过程自带**已验结果** → 召回会话 = 连答案一起召回（刀 D 的前提），
    不用重新跑一遍 SQL 再判一遍。
  这正是原版把「Session 内容全在 Session 一行的 4 个 JSON 列里」的原因：
  会话不是索引项，是**内容本体**。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def recall(req_id: str, scene_id: str, *,
           db_path: str = "") -> dict[str, Any]:
    """按需求 id 精确召回会话。

    返回：
        {
          "found": bool,
          "req_id": str,
          "question": str,          # 这个需求的原始问句
          "dims": [ {id, question, landing, why}, ... ],   # 骨架维度
          "learned": [dim_id, ...], # 已闭合
          "codes": {dim_id: {code, answer, r, how, tools}},# 已验结果
          "session": {phase, phase_name, archived, completed_at, events},
          "path": str,
          "why": str,               # 为什么没找到（如实交代）
        }

    ★ 找不到就**如实返回 found=False**，不猜、不拿别的需求的会话顶替
      （拿错会话 = 拿错答案，比答不出来危险得多）。
    """
    rid = str(req_id or "").strip()
    out: dict[str, Any] = {
        "found": False, "req_id": rid, "question": "", "dims": [],
        "learned": [], "codes": {}, "session": {}, "path": "", "why": "",
        # 过程账（2026-11-10 面板可观测）：每维试了几轮、判官说什么、
        # 缺口是什么；再加整题轮次账。原来只带 codes 的终值，
        # 人想知道「这条维度为什么关不上」只能靠猜。
        "iterations": {}, "rounds": {},
    }
    if not rid:
        out["why"] = "没有需求 id"
        return out
    try:
        import req_registry as _rr
    except Exception as e:
        out["why"] = f"需求登记表不可用：{type(e).__name__}: {e}"
        return out
    try:
        path = _rr.skeleton_path(str(scene_id or "A"), rid, db_path)
    except Exception as e:
        out["why"] = f"算不出会话路径：{type(e).__name__}: {e}"
        return out
    out["path"] = str(path)
    if not path.exists():
        out["why"] = f"这个需求还没有会话记录（{path.name}）"
        return out
    data = _read_json(path)
    if not data:
        out["why"] = f"会话记录读不出来（{path.name}）"
        return out

    out["question"] = str(data.get("question") or "")
    dims = []
    for d in (data.get("dimensions") or data.get("dims") or []):
        if not isinstance(d, dict):
            continue
        did = str(d.get("id") or "").strip()
        if not did:
            continue
        dims.append({"id": did,
                     "question": str(d.get("question") or ""),
                     "landing": str(d.get("landing") or ""),
                     "why": str(d.get("why") or "")})
    out["dims"] = dims

    pr = data.get("progress") or {}
    out["learned"] = [str(x) for x in (pr.get("learned") or [])]
    codes: dict[str, dict[str, Any]] = {}
    for c in (pr.get("codes") or []):
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()
        if not cid:
            continue
        codes[cid] = {"question": str(c.get("question") or ""),
                      "code": str(c.get("code") or ""),
                      "answer": str(c.get("answer") or ""),
                      "tools": list(c.get("tools") or []),
                      "r": float(c.get("r", -1.0)),
                      "how": str(c.get("how") or "")}
    out["codes"] = codes

    # 每维过程账：rounds（试了几轮）、asked（判官/闸门原话）、
    # gap（还缺什么）、passed（这一维过没过）。原样带出，不改写、不归纳 ——
    # 面板要给人看的是**证据**，不是转述。
    iters: dict[str, dict[str, Any]] = {}
    for it in (pr.get("iterations") or []):
        if not isinstance(it, dict):
            continue
        iid = str(it.get("id") or "").strip()
        if not iid:
            continue
        iters[iid] = {"rounds": int(it.get("rounds") or 0),
                      "asked": str(it.get("asked") or ""),
                      "gap": str(it.get("gap") or ""),
                      "passed": bool(it.get("passed")),
                      "tools": list(it.get("tools") or [])}
    out["iterations"] = iters
    out["rounds"] = dict(data.get("milestone_rounds") or {})

    try:
        import session_state as _ss
        out["session"] = _ss.read_session(data)
    except Exception:
        out["session"] = {}

    out["found"] = True
    return out


def recall_by_question(question: str, scene_id: str, *,
                        db_path: str = "") -> dict[str, Any]:
    """按问句召回会话（先解析需求 id，再精确召回）。

    这是消费侧的主入口：**换问法也能召回同一个会话**
    （需求 id 由 `req_registry.req_id_of` 归题找回）。
    """
    sid = str(scene_id or "A")
    try:
        import req_registry as _rr
        rid = _rr.req_id_of(sid, question)
    except Exception as e:
        return {"found": False, "req_id": "", "question": "",
                "dims": [], "learned": [], "codes": {}, "session": {},
                "path": "", "why": f"需求 id 解析失败：{type(e).__name__}: {e}"}
    out = recall(rid, sid, db_path=db_path)
    if not out.get("found"):
        out["question"] = str(question or "")
    return out


def verified_answers(sess: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从召回结果里挑出**已验过真值**的答案（供刀 D 直接披露）。

    判据照抄既有铁律，不新造：
    - 必须有 `code`（真跑通过的取数做法）或 `answer`（取到的结果）
    - `r` 缺失或 < 0 = 没有评分 = **不认**（既有语义：r=-1 表示无 r）
    - `answer` 为空 = 没取到数 = **不认**

    ⚠️ 返回的是**原样**的 answer —— 不改写、不摘要、不补全。
      「披露已验答案」= 把学习侧验过的东西原样交出去，
      加工过的东西就不是「已验过的」了。
    """
    out: dict[str, dict[str, Any]] = {}
    for cid, c in ((sess or {}).get("codes") or {}).items():
        if not isinstance(c, dict):
            continue
        ans = str(c.get("answer") or "").strip()
        code = str(c.get("code") or "").strip()
        if not ans and not code:
            continue
        if float(c.get("r", -1.0)) < 0:
            continue
        out[str(cid)] = dict(c)
    return out


def is_learned(sess: dict[str, Any]) -> bool:
    """这个会话里的维度**是不是全闭合**了（照抄 `milestone_state` 的达成判据）。

    口径与 `maieutic_scaffold.milestone_state` 完全一致：
    达成 = 骨架里的维度**全部**在 learned 集里；少一条就不算达成。
    """
    dims = [str(d.get("id") or "") for d in ((sess or {}).get("dims") or [])
            if isinstance(d, dict)]
    if not dims:
        return False
    learned = {str(x) for x in ((sess or {}).get("learned") or [])}
    return all(d in learned for d in dims)


def unclosed_dims(sess: dict[str, Any]) -> list[str]:
    """还没闭合的维度 id 列表（照抄 `milestone_unclosed` 的口径）。

    ★ 这是「派任务」与「验收」分开的依据：
      - 全部闭合 → 走披露（刀 D），**不派任务**
      - 还有未闭合 → 只对这部分派任务，学习侧继续攻
    """
    dims = [str(d.get("id") or "") for d in ((sess or {}).get("dims") or [])
            if isinstance(d, dict)]
    learned = {str(x) for x in ((sess or {}).get("learned") or [])}
    return [d for d in dims if d not in learned]
