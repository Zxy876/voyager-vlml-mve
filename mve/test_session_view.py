#!/usr/bin/env python3
"""第三刀 E：会话视图**复用技能页** —— 回归测试。

跑法：服务器 `python mve/test_session_view.py`。

用户 2026-11-09 指令
-------------------
> 「其实会话应该是可以复用技能页面的」

⇒ 刀 E **不新开页面**：会话的视图挂在既有「技能 · 因果」页里，
  数据走既有 `/api/state` 的 `s.sessions`，**不新增后端接口**。

钉死的行为：

 E.1 面板复用
  1. 技能页（pSkill）里就有会话区块（不新开 tab）
  2. state 里有 sessions 字段
  3. **没新增** `/api/sessions` 这类路由（不许为展示另开接口）

 E.2 会话视图聚合
  4. 登记过的需求 → 会话视图里有对应条目
  5. 维度闭合进度（closed/total）、会话阶段、已验答案数都如实
  6. 还没拆骨架的需求 → has_session=False 且**如实说为什么**（不拿别的顶替）

 E.3 会话 ↔ 技能**精确**连接
  7. 技能 blueprint.req_id 对上 → 会话卡里列出它
  8. 老技能（无 req_id）→ **不按 milestone 标题猜**（标题可重名，猜=串台）
  9. 别的需求的技能不会串进本会话

 E.4 技能写入点带 req_id
 10. 学习侧写技能时 blueprint 带 req_id（精确归属的唯一依据）
 11. 没传 req_id → 留空，**不编造**
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import req_registry as RR           # noqa: E402
import session_recall as SR         # noqa: E402
import skill_store as SS            # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {detail}")


def _tmp_scene(tag: str) -> str:
    return f"_SV{tag}{int(time.time() * 1000) % 100000}"


def _cleanup(sid: str) -> None:
    try:
        p = RR.CKPT / sid
        if p.exists():
            shutil.rmtree(p)
    except Exception:
        pass


DIMS = [{"id": "d1", "question": "定位目标行", "landing": "web_facts"},
        {"id": "d2", "question": "抽取可用平台", "landing": "web_facts"}]
CODES = [
    {"id": "d1", "question": "定位目标行", "code": "SELECT 1",
     "answer": '{"rows": [["ce"]]}', "tools": ["query_sql"], "r": 1.0,
     "how": "tool"},
    {"id": "d2", "question": "抽取可用平台", "code": "SELECT 2",
     "answer": '{"rows": [["github"]]}', "tools": ["query_sql"], "r": 0.8,
     "how": "tool"},
]


def _write_session(sid: str, q: str, dims: list[dict], learned: list[str],
                   codes: list[dict]) -> tuple[str, Path]:
    """登记需求 + 写骨架文件（走既有 _cache_path → req 键）。"""
    rid = RR.register(sid, q)["req_id"]
    import maieutic_scaffold as MS
    p = MS._cache_path(sid, q)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "question": q, "req_id": rid, "dimensions": dims,
        "progress": {"learned": learned, "codes": codes},
    }, ensure_ascii=False), encoding="utf-8")
    return rid, p


# ---------------------------------------------------------------------------
print("\n== 1. 面板复用（不新开页面）==")
# ---------------------------------------------------------------------------
dash = (_HERE / "dashboard.py").read_text(encoding="utf-8")
pdata = (_HERE / "panel_data.py").read_text(encoding="utf-8")

check("技能页 pSkill 里含会话区块",
      "pSessions(s)" in dash and "function pSessions(" in dash)
check("会话区块函数真的被调用",
      dash.count("pSessions(s)") >= 2,
      f"出现 {dash.count('pSessions(s)')} 次")
check("state 里有 sessions 字段", '"sessions":' in pdata)
check("会话视图函数在 panel_data 里",
      "def _sessions_view(" in pdata)
check("没有为展示另开 API 路由（不许 /api/sessions）",
      "/api/sessions" not in dash)

# ---------------------------------------------------------------------------
print("\n== 2. 会话视图聚合 ==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("a")
old_store = SS.STORE
tmp = Path(tempfile.mkdtemp())
SS.STORE = tmp / "skill_store.json"
try:
    q = "会话视图测试题"
    rid, sp = _write_session(sid, q, DIMS, ["d1"], CODES)

    import panel_data as PD
    views = PD._sessions_view(sid)
    check("登记过的需求 → 会话视图有条目", len(views) == 1, str(len(views)))
    v = views[0] if views else {}
    check("req_id 对上", v.get("req_id") == rid, str(v.get("req_id")))
    check("原始问句带上", v.get("question") == q, str(v.get("question")))
    check("has_session=True（骨架文件在）", v.get("has_session") is True)
    check("维度总数 2", v.get("total") == 2, str(v.get("total")))
    check("已闭合 1", v.get("closed") == 1, str(v.get("closed")))
    check("未闭合维度 = d2", v.get("unclosed") == ["d2"], str(v.get("unclosed")))
    check("已验答案 2 条（r>=0 且有 answer）",
          len(v.get("verified") or {}) == 2, str(list(v.get("verified") or {})))
    check("已验答案逐字未改",
          (v.get("verified") or {}).get("d1", {}).get("answer") == '{"rows": [["ce"]]}',
          str((v.get("verified") or {}).get("d1")))

    # 没拆骨架的需求 → 如实说，不拿别的会话顶替
    q2 = "还没学的题"
    RR.register(sid, q2)
    views2 = PD._sessions_view(sid)
    v2 = [x for x in views2 if x.get("question") == q2]
    check("没拆骨架的需求也在视图里", len(v2) == 1, str(len(v2)))
    if v2:
        check("has_session=False", v2[0].get("has_session") is False)
        check("如实说为什么", bool(v2[0].get("why")), str(v2[0].get("why")))
        check("维度数 0（不编造）", v2[0].get("total") == 0, str(v2[0].get("total")))
finally:
    SS.STORE = old_store
    _cleanup(sid)
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------------------
print("\n== 3. 会话 ↔ 技能精确连接 ==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("b")
old_store = SS.STORE
tmp = Path(tempfile.mkdtemp())
SS.STORE = tmp / "skill_store.json"
try:
    q = "技能挂接测试题"
    rid, sp = _write_session(sid, q, DIMS, ["d1", "d2"], CODES)
    other = "别的需求"
    rid2, _ = _write_session(sid, other, DIMS, [], [])

    SS.add("learn_aaa", "d1", "这是一条够长的技能描述文本用于测试",
           code="SELECT 1", source="learn", exact_only=True,
           blueprint={"from": "maieutic_milestone", "milestone": "里程碑甲",
                      "dimension": "d1", "req_id": rid, "scene": sid},
           source_scene=sid)
    SS.add("learn_bbb", "d2", "这也是一条够长的技能描述文本用于测试",
           code="SELECT 2", source="learn", exact_only=True,
           blueprint={"from": "maieutic_milestone", "milestone": "里程碑乙",
                      "dimension": "d2", "req_id": rid, "scene": sid},
           source_scene=sid)
    # 老技能：没有 req_id（字段引入前入库的）
    SS.add("learn_ccc", "d0", "这是一条老技能没有需求身份字段的描述",
           code="SELECT 3", source="learn", exact_only=True,
           blueprint={"from": "maieutic_milestone", "milestone": "里程碑甲",
                      "dimension": "d0", "scene": sid},
           source_scene=sid)
    # 别的需求的技能
    SS.add("learn_ddd", "d1", "这是属于别的需求的一条技能描述文本",
           code="SELECT 4", source="learn", exact_only=True,
           blueprint={"from": "maieutic_milestone", "milestone": "里程碑丙",
                      "dimension": "d1", "req_id": rid2, "scene": sid},
           source_scene=sid)

    import panel_data as PD
    views = PD._sessions_view(sid)
    mine = [x for x in views if x.get("req_id") == rid]
    check("本会话找得到", len(mine) == 1, str(len(mine)))
    keys = [s.get("key") for s in (mine[0].get("skills") or [])] if mine else []
    check("列出本会话的两条技能", len(keys) == 2, str(keys))
    check("技能带上了维度 id",
          all(s.get("dimension") for s in (mine[0].get("skills") or []))
          if mine else False)
    check("老技能（无 req_id）不被挂上",
          all("learn_ccc" not in str(k) for k in keys), str(keys))
    check("别的需求的技能不串进来",
          all("learn_ddd" not in str(k) for k in keys), str(keys))
finally:
    SS.STORE = old_store
    _cleanup(sid)
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------------------
print("\n== 4. 技能写入点带 req_id ==")
# ---------------------------------------------------------------------------
old_store = SS.STORE
tmp = Path(tempfile.mkdtemp())
SS.STORE = tmp / "skill_store.json"
try:
    import learn_orchestrate as L
    mst = {"name": "里程碑X", "total": 1, "closed": 1, "missing": [],
           "achieved": True}
    codes = [{"id": "prevalence", "question": "发病率是多少？",
              "code": 'r = web_search("Asperger prevalence")\nRESULT = r',
              "r": 1.0}]
    out = L.learn_milestone_skills("T", "learn_xyz", "什么是阿斯伯格",
                                   mst, codes, req_id="req_TEST123")
    check("技能入库成功", len(out.get("added") or []) == 1, str(out))
    raw = SS._load_raw()
    got = list((raw.get("skills") or {}).values())
    check("库里有一条", len(got) == 1, str(len(got)))
    check("blueprint 带 req_id",
          (got[0].get("blueprint") or {}).get("req_id") == "req_TEST123",
          str((got[0].get("blueprint") or {})))
    # 没传 req_id → 留空，不编造
    out2 = L.learn_milestone_skills("T", "learn_abc", "另一个问题",
                                    mst, [{"id": "d9", "question": "q",
                                           "code": 'read_file("a.json")\nRESULT = 1'}])
    raw2 = SS._load_raw()
    # 只看**本次新增**的那条（库里还有上一轮带 req_id 的）
    new_key = (out2.get("added") or [{}])[0].get("key")
    got2 = (raw2.get("skills") or {}).get(new_key) or {}
    check("没传 req_id → 留空（不编造）",
          not (got2.get("blueprint") or {}).get("req_id"),
          str((got2.get("blueprint") or {}).get("req_id")))
    check("但 milestone / dimension 照旧写（没丢原有字段）",
          (got2.get("blueprint") or {}).get("dimension") == "d9"
          and (got2.get("blueprint") or {}).get("milestone") == "里程碑X",
          str(got2.get("blueprint")))
finally:
    SS.STORE = old_store
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------------------
print("\n== 5. 零回归：空场景 / 老技能 ==")
# ---------------------------------------------------------------------------
old_store = SS.STORE
tmp = Path(tempfile.mkdtemp())
SS.STORE = tmp / "skill_store.json"
try:
    import panel_data as PD
    sid2 = _tmp_scene("c")
    check("空场景 → 空列表（不炸）", PD._sessions_view(sid2) == [],
          str(PD._sessions_view(sid2)))
    # 只有老技能（完全没有 req_id 字段）也不能炸
    SS.add("learn_eee", "x", "一条最老最老的技能没有 blueprint 的描述",
           code="SELECT 9", source="learn", exact_only=True,
           source_scene=sid2)
    q3 = "老技能场景的题"
    rid3, _ = _write_session(sid2, q3, DIMS, [], [])
    v3 = PD._sessions_view(sid2)
    check("只有老技能也不炸", len(v3) == 1, str(len(v3)))
    if v3:
        check("老技能不算进会话（不按标题猜）",
              (v3[0].get("skills") or []) == [], str(v3[0].get("skills")))
    _cleanup(sid2)
finally:
    SS.STORE = old_store
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n=== 刀 E 回归：{PASS} 过 / {FAIL} 败 ===")
sys.exit(1 if FAIL else 0)
