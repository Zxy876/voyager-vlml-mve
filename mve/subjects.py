#!/usr/bin/env python3
"""**可填值清单**：换赛事 / 换队伍时，能往里填什么值。

⚠️ 2026-10-08 纠了一次方向 —— 这里**不是**"学科登记册"
--------------------------------------------------------
用户原话：「其实学科下的这些知识点/这些工具集都是一样的啊，没有必要填完
学科之后自己再整理工具集呀，参考一下猫娘伴学里真正改映射到赛事号和团队
的键是什么吧」。

去伴学取证，答案是 `entry_tutor_question_entries.py:1043-1051`：

    store.list_topics(5000, scope.subject, scope.stage,
                      chapter=scope.chapter, unit=scope.unit,
                      course_family=scope.course_family)

→ subject / stage / chapter / unit / course_family **全是 `list_topics`
的筛选参数**，不是知识点身上的层级归属。知识点库是**共享的一份**，
换学科、换课程族只是"筛哪些出来练"，**知识点与工具集本身不重建**。

所以赛事号 / 团队名不是"学科"，而是：

| MVE 的值 | 伴学对应 | 性质 |
|---|---|---|
| **赛事号（系列赛 / 数据源版本）** | `course_family`（哪套教材/版本） | 作用域筛选参数，知识点共享 |
| **团队 / 选手 / 地图的具体值** | `_semantic_routing.py:149` 的 `entity`（`"max 120 chars; empty when absent"`，**不在 topics 表里**） | 运行期取数参数，不进知识结构 |
| **接的是哪个库** | 伴学没有对应物 | MVE 自己的 `data_source` |

于是这个模块的定位是**"可填值清单"**：给作用域（course_family / entity）
提供候选值，并记下"这条目**这次**是在哪个范围下跑出来的"。它不定义知识
结构 —— 换值不会、也不需要重建工具集。

同构对照（伴学源码取证）
------------------------
* `study_companion/store.py:1504-1506`
      subject = CASE WHEN topics.source='seed' THEN topics.subject
                     ELSE excluded.subject END
  → seed 的学科锁死，运行期条目的学科**可写可改**（这是学科字段本身可改，
    与"换值要重建工具集"是两回事）。
* `study_companion/_semantic_routing.py:23` `ALLOWED_SUBJECTS` 是硬编码
  frozenset：伴学的学科是**预置枚举**，人不能随便填。
  MVE 这里**故意更宽松**：数据源是会换的（GRID → vlr.gg → …），
  预置枚举必然腐坏。所以值可人填，只做空值/去重校验，不做白名单。
* `study_companion/_semantic_routing.py:149` `"entity": "string, max 120
  chars; empty when absent"` → entity 是**运行期**字段，可空、**不进 topics
  表**。团队/选手/地图的具体值就是这一档。

范围标记（不是"归属"）
----------------------
条目（人导入的边 / Voyager 跑出的待审维度）携带的 SQL 里有实体字面量
（`series_id = 'xxx'` / `team_name = 'NRG'`），或出题器显式传的
`subject={"team": "NRG"}`。拿这些值去清单里找同 kind 同 value 的项；
找不到 → 记为**未标记范围**，等人填，不硬塞。
这只是"这条目这次跑的范围"，**不改变它在知识结构里的位置**。

跑法
----
    python mve/subjects.py                 # 列出可填值 + 未标记范围的条目
    python mve/subjects.py --sync          # 从 entity_catalog 登记实查到的值
    python mve/subjects.py --add team NRG --note "…"   # 人填一个可填值
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUBJECTS = HERE / "subjects.json"
DB_CONFIG = HERE / "db_config.json"

# 学科类别 → 中文名（与 knowledge_graph.SUBJECT_LABEL 保持一致）
KIND_LABEL = {"series": "赛事", "team": "团队", "map": "地图", "player": "选手",
              "tournament": "赛事总览", "round": "回合", "game": "对局",
              "custom": "自定义"}

# 默认值：当前接的是 VLML 的 duckdb 切片
DEFAULT_FAMILY = "vlml0"
# 已知库文件名 → 代号（换数据源时在这里加一行；认不出就用文件名本身）
_FAMILY_BY_STEM = {"vlml_events": "vlml0",        # GRID 切片（逐事件，key 过期）
                   "vlml_vlr": "vlr.gg"}          # 公开源（回合级 + 聚合级）


# --------------------------------------------------------------------------
# 存取
# --------------------------------------------------------------------------
def _load() -> dict:
    try:
        raw = json.loads(SUBJECTS.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw.setdefault("subjects", [])
            raw.setdefault("attached", {})
            return raw
    except Exception:
        pass
    return {"subjects": [], "attached": {}}


def _save(data: dict) -> None:
    SUBJECTS.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                        encoding="utf-8")


def _norm(v: str) -> str:
    """归一化：比较用。大小写/空格/引号都不该造成两条学科。"""
    return re.sub(r"[\s'\"`]+", "", str(v or "")).strip().lower()


def _sid(kind: str, value: str) -> str:
    return f"{kind}:{_norm(value)}"


# --------------------------------------------------------------------------
# 登记（人填 / 从库同步）
# --------------------------------------------------------------------------
def register(kind: str, value: str, *, label: str = "", note: str = "",
             source: str = "human") -> dict:
    """登记一个学科。已存在就只补 label/note，不重复建。

    `source="human"` 的人填学科**不会被 `--sync` 覆盖** —— 同构伴学
    `topics.source='seed'` 那条 CASE：预置的锁死，运行期可改。
    """
    kind = str(kind or "").strip().lower()
    value = str(value or "").strip()
    if not kind or not value:
        return {"ok": False, "reason": "类别与取值都不能为空"}
    data = _load()
    sid = _sid(kind, value)
    for s in data["subjects"]:
        if s.get("id") == sid:
            if label and not s.get("label"):
                s["label"] = label
            if note:
                s["note"] = note
            _save(data)
            return {"ok": True, "id": sid, "existed": True}
    data["subjects"].append({
        "id": sid, "kind": kind, "value": value,
        "label": label or value, "note": note, "source": source,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    _save(data)
    return {"ok": True, "id": sid, "existed": False}


def unregister(sid: str) -> bool:
    """删掉一个**人填**的学科（实查来的删了也会被 --sync 拉回来）。"""
    data = _load()
    before = len(data["subjects"])
    data["subjects"] = [s for s in data["subjects"] if s.get("id") != sid]
    data["attached"] = {k: [x for x in v if x != sid]
                        for k, v in (data.get("attached") or {}).items()}
    data["attached"] = {k: v for k, v in data["attached"].items() if v}
    _save(data)
    return len(data["subjects"]) < before


def sync_from_catalog() -> dict:
    """把 `entity_catalog.json` 实查到的实体值登记成 `source="catalog"`。

    换数据源后跑这个：新库里的赛事 / 队伍会自动出现在学科可填值里，
    不用人一个个敲。人填过的同名学科保留不动。
    """
    try:
        cat = json.loads((HERE / "entity_catalog.json").read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "reason": f"读不到 entity_catalog.json：{exc}"}
    ents = (cat or {}).get("entities") or {}
    if not ents:
        return {"ok": False, "reason": "entity_catalog 里没有实体（"
                                       f"{(cat or {}).get('error') or '空'}）"}
    data = _load()
    human_ids = {s.get("id") for s in data["subjects"]
                 if s.get("source") == "human"}
    known = {s.get("id") for s in data["subjects"]}
    added = 0
    for kind, items in ents.items():
        for it in items or []:
            v = str((it or {}).get("value") or "").strip()
            if not v:
                continue
            sid = _sid(kind, v)
            if sid in known or sid in human_ids:
                continue
            data["subjects"].append({
                "id": sid, "kind": kind, "value": v, "label": v,
                "note": "", "source": "catalog",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            })
            known.add(sid)
            added += 1
    _save(data)
    return {"ok": True, "added": added, "total": len(data["subjects"])}


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------
def all_subjects() -> list[dict]:
    return list(_load().get("subjects") or [])


def by_kind(kind: str) -> list[dict]:
    k = str(kind or "").strip().lower()
    return [s for s in all_subjects() if s.get("kind") == k]


def values(kind: str) -> list[str]:
    """这个类别下**可填的值**：实查到的 + 人填的（去重，人填优先在后面
    补）。给 UI 下拉和出题器用。"""
    return [str(s.get("value") or "") for s in by_kind(kind)
            if s.get("value")]


# --------------------------------------------------------------------------
# 归属
# --------------------------------------------------------------------------
# SQL 里的实体字面量：series_id = 'xxx' / team_name='NRG' / map_name IN (...)
_LIT = re.compile(
    r"(series_id|team_name|map_name|player_name|tournament_name)"
    r"\s*(?:=|IN)\s*\(?\s*'([^']{1,80})'", re.I)
_COL2KIND = {"series_id": "series", "team_name": "team", "map_name": "map",
             "player_name": "player", "tournament_name": "tournament"}


def entities_in_sql(sql: str) -> dict[str, str]:
    """从真实跑过的 SQL 里认出实体（赛事号 / 队伍名 / …）—— 不猜，只认字面量。"""
    out: dict[str, str] = {}
    for col, val in _LIT.findall(str(sql or "")):
        kind = _COL2KIND.get(str(col).lower())
        if kind and val and kind not in out:
            out[kind] = val
    return out


def resolve(entities: dict[str, str] | None) -> list[str]:
    """实体 → 学科 id 列表。认不出来就返回空（不硬塞）。"""
    out: list[str] = []
    for kind, val in (entities or {}).items():
        sid = _sid(kind, val)
        if any(s.get("id") == sid for s in all_subjects()) and sid not in out:
            out.append(sid)
    return out


def attach(entry_id: str, subject_ids: list[str]) -> int:
    """把一个条目（洞察 / 维度 / 人导入边 / 待审维度）挂到学科下。"""
    eid = str(entry_id or "").strip()
    if not eid:
        return 0
    data = _load()
    cur = [s for s in (data["attached"].get(eid) or [])]
    for sid in subject_ids or []:
        if sid and sid not in cur:
            cur.append(sid)
    data["attached"][eid] = cur
    _save(data)
    return len(cur)


def of_entry(entry_id: str) -> list[str]:
    return list((_load().get("attached") or {}).get(str(entry_id or "")) or [])


def entries_of(subject_id: str) -> list[str]:
    sid = str(subject_id or "")
    return [e for e, v in (_load().get("attached") or {}).items() if sid in v]


def unassigned(entry_ids: list[str] | None = None) -> list[str]:
    """还没归属的条目。传 None 就返回登记册里记着的全部"未归属"条目。"""
    att = _load().get("attached") or {}
    if entry_ids is None:
        return sorted(e for e, v in att.items() if not v)
    return [e for e in entry_ids if not (att.get(e) or [])]


# --------------------------------------------------------------------------
# 接的是哪个库（MVE 自己的键，伴学没有对应物）
# --------------------------------------------------------------------------
def data_source() -> str:
    """当前接的是哪个库。换库它自己变，不手写。

    不叫 `course_family`：伴学的 course_family 是"哪套教材/版本"，是
    **筛选参数**，对应我们的**赛事号**；"接的哪个库"是更外面一层
    （vlml0 = GRID 切片 / vlml_vlr = vlr.gg），伴学里没有这个东西。
    """
    path = ""
    try:
        cfg = json.loads(DB_CONFIG.read_text(encoding="utf-8"))
        path = str((cfg or {}).get("db_path") or "").strip()
    except Exception:
        pass
    if not path:
        return DEFAULT_FAMILY
    stem = Path(path).stem
    return _FAMILY_BY_STEM.get(stem, stem or DEFAULT_FAMILY)


def course_family() -> str:
    """兼容旧调用：== `data_source()`。新代码请用 `data_source()`。"""
    return data_source()


def _main() -> int:
    args = sys.argv[1:]
    if "--add" in args:
        i = args.index("--add")
        rest = args[i + 1:]
        note = ""
        if "--note" in rest:
            j = rest.index("--note")
            note = " ".join(rest[j + 1:])
            rest = rest[:j]
        if len(rest) < 2:
            print("用法：--add <类别> <取值> [--note 说明]")
            return 2
        r = register(rest[0], " ".join(rest[1:]), note=note)
        print(f"{'已存在' if r.get('existed') else '已登记'}：{r.get('id')}"
              if r.get("ok") else f"❌ {r.get('reason')}")
        return 0 if r.get("ok") else 1
    if "--sync" in args:
        r = sync_from_catalog()
        if not r.get("ok"):
            print(f"❌ {r.get('reason')}")
            return 1
        print(f"从 entity_catalog 新登记 {r['added']} 个，共 {r['total']} 个学科")
        return 0
    if "--json" in args:
        print(json.dumps(_load(), ensure_ascii=False, indent=1))
        return 0

    data = _load()
    print(f"数据源（data_source）：{data_source()}")
    subs = data.get("subjects") or []
    if not subs:
        print("\n（登记册为空 —— 跑 `--sync` 从库里登记，或 `--add series xxx`）")
    for kind in sorted({str(s.get("kind")) for s in subs}):
        items = by_kind(kind)
        print(f"\n{KIND_LABEL.get(kind, kind)}（{kind}）· {len(items)} 个")
        for s in items:
            tag = "人填" if s.get("source") == "human" else "实查"
            n = len(entries_of(s.get("id") or ""))
            print(f"  [{tag}] {s.get('value')}"
                  + (f"  （{s['note']}）" if s.get("note") else "")
                  + f"  · {n} 个条目")
    ua = unassigned()
    if ua:
        print(f"\n未归属条目 {len(ua)} 个：")
        for e in ua[:20]:
            print(f"  - {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
