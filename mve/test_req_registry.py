#!/usr/bin/env python3
"""第三刀 A：需求级 id（req id）与骨架键迁移 —— 回归测试。

跑法：服务器 `python mve/test_req_registry.py`（无 pytest）。

钉死的行为（全部来自 2026-11-09 服务器取证 + 原版 maieutic 复核）：

 1. req id **不由问句派生**（原版 `Exercise.id` = slug，不从题目文本算）
 2. 同一句问两次 → 同一个 req id（幂等，不产生两个需求）
 3. 换问法 → 归题找回**同一个** req id → 骨架**不重拆**、闭合账延续
 4. 新键 = `req_<req_id>_<dbhash>.json`；旧键（问句 md5）读时兼容 + 自动迁移
 5. 回退找旧账**只能用当前问句的 md5**，不许扫目录取最近（不许串台）
 6. 消费侧登记 req id 后，骨架/进度账落在**同一个** req 键上
 7. 登记表坏掉 → 留证后重建（身份丢失必须看得见，不能无声漂移）
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import req_registry as RR          # noqa: E402

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
    """造一个临时场景 id（登记表与骨架都写在 ckpt_maieutic/<scene>/ 下）。"""
    sid = f"_T{tag}{int(time.time() * 1000) % 100000}"
    return sid


def _cleanup(sid: str) -> None:
    p = RR.CKPT / sid
    try:
        if p.exists():
            shutil.rmtree(p)
    except Exception:
        pass


# ---------------------------------------------------------------------------
print("\n== 1. req id 的性质：与问句解耦（照抄原版 Exercise.id）==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("a")
try:
    r1 = RR.register(sid, "查一下ce这个用户都活跃在哪些社交平台上")
    rid = r1["req_id"]
    check("登记产出 req id", bool(rid) and rid.startswith("req_"), str(r1))
    check("req id 不含问句的 md5",
          rid.split("req_", 1)[1] !=
          __import__("hashlib").md5("查一下ce这个用户都活跃在哪些社交平台上"
                                     .encode()).hexdigest()[:12],
          rid)
    # 换个问法 → 登记 → **不同的** req id（因为是新需求）
    r2 = RR.register(sid, "帮我查查用户名 ce 的这位女生都活跃在哪些平台")
    check("不同问法 → 不同 req id（此时还没归题）",
          r2["req_id"] != rid, f"{r2['req_id']} vs {rid}")
    check("两次登记 → 两个需求都在册",
          len(RR.list_reqs(sid)) == 2, str(RR.list_reqs(sid)))
finally:
    _cleanup(sid)


# ---------------------------------------------------------------------------
print("\n== 2. 幂等：同一句登记两次 → 同一个 req id ==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("b")
try:
    a = RR.register(sid, "同一句话")
    b = RR.register(sid, "同一句话")
    check("同一句再登记 → 同一个 req id", a["req_id"] == b["req_id"],
          f"{a['req_id']} vs {b['req_id']}")
    check("第二次不新建（created=False）", b["created"] is False, str(b))
    check("需求数仍是 1", len(RR.list_reqs(sid)) == 1, str(RR.list_reqs(sid)))
    # 空白差异不算另一句
    c = RR.register(sid, "  同一句话  ")
    check("首尾空白差异 → 仍是同一个 req id", c["req_id"] == a["req_id"],
          f"{c['req_id']} vs {a['req_id']}")
    # 标点差异也不算
    d = RR.register(sid, "同一句话？")
    check("标点差异 → 仍是同一个 req id（规范化只去标点）",
          d["req_id"] == a["req_id"], f"{d['req_id']} vs {a['req_id']}")
finally:
    _cleanup(sid)


# ---------------------------------------------------------------------------
print("\n== 3. 键：req 键 vs 旧键（问句 md5）+ 读时迁移 ==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("c")
try:
    reg = RR.register(sid, "迁移测试题")
    rid = reg["req_id"]
    new_p = RR.skeleton_path(sid, rid, "/tmp/x.duckdb")
    old_p = RR.legacy_skeleton_path(sid, "迁移测试题", "/tmp/x.duckdb")
    check("新键 = req_<req_id>_<dbhash>.json",
          new_p.name.startswith(f"req_{rid}_"), new_p.name)
    check("旧键 = <md5(question)[:12]>_<dbhash>.json",
          old_p.name.startswith("req_") is False and old_p.name.endswith(
              ".json") and "_" in old_p.name, old_p.name)
    check("新键 ≠ 旧键", new_p != old_p, f"{new_p} vs {old_p}")

    # 旧键上有一份账 → 迁移后新键拿到它
    old_p.parent.mkdir(parents=True, exist_ok=True)
    old_p.write_text(json.dumps({"question": "迁移测试题",
                                 "progress": {"learned": ["d1", "d2"]}},
                                ensure_ascii=False), encoding="utf-8")
    got = RR.migrate_legacy(sid, "迁移测试题", rid, db_path="/tmp/x.duckdb")
    check("迁移返回新键路径", got is not None and got == new_p, str(got))
    check("新键拿到旧账（learned 保住）",
          json.loads(new_p.read_text(encoding="utf-8"))
          .get("progress", {}).get("learned") == ["d1", "d2"],
          new_p.read_text(encoding="utf-8")[:200])
    check("旧文件留证（改名不删）",
          not old_p.exists() and any(old_p.parent.glob("*.migrated*")),
          str(list(old_p.parent.glob("*"))))
    # 幂等：再迁一次不炸
    got2 = RR.migrate_legacy(sid, "迁移测试题", rid, db_path="/tmp/x.duckdb")
    check("迁移幂等（再迁一次仍返回新键）", got2 == new_p, str(got2))
finally:
    _cleanup(sid)


# ---------------------------------------------------------------------------
print("\n== 4. 不许串台：回退找旧账只能用当前问句的 md5 ==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("d")
try:
    reg = RR.register(sid, "题甲")
    rid = reg["req_id"]
    # 场景里另有一份「题乙」的旧账
    other = RR.legacy_skeleton_path(sid, "题乙", "/tmp/x.duckdb")
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text(json.dumps({"question": "题乙"}), encoding="utf-8")
    got = RR.migrate_legacy(sid, "题甲", rid, db_path="/tmp/x.duckdb")
    check("题甲不迁移题乙的账（不许扫目录取最近）", got is None, str(got))
    check("题乙的旧账原封不动", other.exists(), str(other))
finally:
    _cleanup(sid)


# ---------------------------------------------------------------------------
print("\n== 5. req_id_of：精确命中 → 归题找回 → 分配 ==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("e")
try:
    r = RR.register(sid, "精确命中题")
    rid = r["req_id"]
    got = RR.req_id_of(sid, "精确命中题")
    check("① 精确命中 → 原 req id", got == rid, f"{got} vs {rid}")
    got2 = RR.req_id_of(sid, "精确命中题", allow_alloc=False)
    check("① 精确命中不新建", got2 == rid, got2)
    # 未登记的问句 + allow_alloc=False → ""（如实说「没有」）
    got3 = RR.req_id_of(sid, "从没登记过的新题", allow_alloc=False)
    check("未登记 + 不允许分配 → 返回空（如实不猜）", got3 == "", repr(got3))
    # 未登记 + 允许分配 → 新 req id
    # ⚠️ 必须**显式**传 allow_alloc=True：默认值是 False
    #    （读路径不许有分配副作用 —— 实测踩到过：`_cache_path` 走读路径时
    #     凭空造出需求，污染登记表且破坏零回归）
    got4 = RR.req_id_of(sid, "从没登记过的新题", allow_alloc=True)
    check("未登记 + 允许分配 → 新 req id",
          bool(got4) and got4 != rid, f"{got4} vs {rid}")
    got4b = RR.req_id_of(sid, "另一个没登记的题")
    check("默认不许分配（读路径零副作用）", got4b == "", repr(got4b))
    # 归位：把新问法挂到已有需求下
    RR.note_question(sid, "换个说法的问法", rid)
    got5 = RR.req_id_of(sid, "换个说法的问法")
    check("归位后新问法 → 同一个 req id", got5 == rid, f"{got5} vs {rid}")
    rec = RR.get(sid, rid)
    check("归位的问法记进 known_questions",
          any("换个说法" in str(k) for k in (rec.get("known_questions") or [])),
          str(rec.get("known_questions")))
finally:
    _cleanup(sid)


# ---------------------------------------------------------------------------
print("\n== 6. 登记表坏掉 → 留证后重建（身份丢失必须看得见）==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("f")
try:
    RR.register(sid, "坏表测试")
    rp = RR._registry_path(sid)
    rp.write_text("{ 这不是合法 JSON", encoding="utf-8")
    got = RR._read_registry(sid)
    check("坏表 → 按空处理", got == {}, str(got))
    check("坏表留证（改名 .broken*）",
          any(RR.CKPT.joinpath(sid).glob("_req_registry.broken*")),
          str(list(RR.CKPT.joinpath(sid).glob("*"))))
finally:
    _cleanup(sid)


# ---------------------------------------------------------------------------
print("\n== 7. 骨架键切换：maieutic_scaffold._cache_path 走 req 键 ==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("g")
try:
    import maieutic_scaffold as MS
    reg = RR.register(sid, "骨架键测试题")
    rid = reg["req_id"]
    # ★ 断言的**真实不变量**：`scaffold` 写的路径 == `save_progress` 写的路径。
    #   这两个入口的 db_path 传法本来就不同（scaffold 显式传、save_progress
    #   省略后回退到场景规范 db_path），生产环境两者都从 scene_profile 取，
    #   所以必须相等 —— 不等就是「两把钥匙」，进度账会写丢。
    import scene_profile as _sp
    _dbp = str((_sp.profile(sid) or {}).get("db_path") or "")
    p_scaffold = MS._cache_path(sid, "骨架键测试题", _dbp)
    p_progress = MS._cache_path(sid, "骨架键测试题")
    check("scaffold 与 save_progress 同一把钥匙",
          p_scaffold == p_progress,
          f"{p_scaffold.name} vs {p_progress.name}")
    check("键落在 req 上", p_scaffold.name.startswith(f"req_{rid}_"),
          p_scaffold.name)
    p = p_scaffold
    # 写一份骨架 → 进度账必须写到**同一个**文件（同一把钥匙）
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"question": "骨架键测试题"}, ensure_ascii=False),
                 encoding="utf-8")
    MS.save_progress(sid, "骨架键测试题", ["d1"])
    data = json.loads(p.read_text(encoding="utf-8"))
    check("进度账写进同一个 req 键文件",
          (data.get("progress") or {}).get("learned") == ["d1"],
          str(data.get("progress")))
    check("load_progress 读得回来",
          MS.load_progress(sid, "骨架键测试题") == {"d1"},
          str(MS.load_progress(sid, "骨架键测试题")))
finally:
    _cleanup(sid)


# ---------------------------------------------------------------------------
print("\n== 8. 零回归：没登记的题仍走旧键 ==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("h")
try:
    import maieutic_scaffold as MS
    p = MS._cache_path(sid, "没登记过的题", "/tmp/x.duckdb")
    legacy = RR.legacy_skeleton_path(sid, "没登记过的题", "/tmp/x.duckdb")
    check("未登记 → 旧键（行为不变）", p == legacy, f"{p.name} vs {legacy.name}")
    check("未登记 → 没有凭空造出需求",
          RR.list_reqs(sid) == [], str(RR.list_reqs(sid)))
finally:
    _cleanup(sid)


# ---------------------------------------------------------------------------
print("\n== 9. 核心目标：换问法 → 同一个 req → 不重拆骨架、闭合账延续 ==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("i")
try:
    import maieutic_scaffold as MS
    # 人导入原句
    orig = "帮我查查用户名 ce 的这位女生都活跃在哪些社交平台上"
    reg = RR.register(sid, orig)
    rid = reg["req_id"]
    # 学习侧拆骨架 + 闭合一条维度
    p = MS._cache_path(sid, orig)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "question": orig,
        "dimensions": [{"id": "locate_target_item", "question": "定位 ce",
                        "landing": "web_facts"}],
        "progress": {"learned": ["locate_target_item"],
                     "codes": [{"id": "locate_target_item",
                                "question": "定位 ce", "code": "SELECT 1",
                                "answer": '{"rows": [[1]]}', "r": 1.0,
                                "tools": ["query_sql"], "how": "tool"}]},
    }, ensure_ascii=False), encoding="utf-8")
    check("骨架已建，闭合一条",
          MS.load_progress(sid, orig) == {"locate_target_item"},
          str(MS.load_progress(sid, orig)))

    # 换个问法 → 归位到同一个需求（模拟消费侧 note_question）
    alt = "查一下ce这个用户都活跃在哪些社交平台上"
    RR.note_question(sid, alt, rid)
    check("换问法 → 同一个 req id",
          RR.req_id_of(sid, alt) == rid, RR.req_id_of(sid, alt))
    check("换问法 → 读到同一份骨架（不重拆）",
          MS._cache_path(sid, alt) == p,
          f"{MS._cache_path(sid, alt).name} vs {p.name}")
    check("换问法 → 闭合账延续（learned 不丢）",
          MS.load_progress(sid, alt) == {"locate_target_item"},
          str(MS.load_progress(sid, alt)))
    check("换问法 → 已验答案还在（codes 保住）",
          "locate_target_item" in (MS.load_progress_codes(sid, alt) or {}),
          str(list((MS.load_progress_codes(sid, alt) or {}).keys())))
finally:
    _cleanup(sid)


# ---------------------------------------------------------------------------
print("\n== 10. db_path 进键的口径不变（照抄，不改）==")
# ---------------------------------------------------------------------------
sid = _tmp_scene("j")
try:
    import maieutic_scaffold as MS
    RR.register(sid, "换库测试")
    p1 = MS._cache_path(sid, "换库测试", "/tmp/a.duckdb")
    p2 = MS._cache_path(sid, "换库测试", "/tmp/b.duckdb")
    check("不同 db_path → 不同键（旧骨架不误命中）", p1 != p2,
          f"{p1.name} vs {p2.name}")
finally:
    _cleanup(sid)


print(f"\n=== 刀 A 回归：{PASS} 过 / {FAIL} 败 ===")
sys.exit(1 if FAIL else 0)
