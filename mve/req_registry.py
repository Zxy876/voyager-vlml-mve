#!/usr/bin/env python3
"""需求级 id 登记表 —— MVE 侧照抄原版 maieutic 的 `Exercise`。

为什么需要（2026-11-09 用户判断 + 服务器取证）
------------------------------------------------
用户原话：「同一个问题导入以及断点续学会重复拆骨架；其根源是消费侧分不清
什么是派任务，什么是验收（但是验收其实在学习侧就已经跑完，只是没有答案
披露）」。

服务器实况（场景 L，同一个需求三个问句）：

    2776f86718d1  「帮我查查用户名 ce 的这位女生…」   dims=4
    77636f315f0e  「username为ce的女生…用Sherlock找」  dims=5
    270be44dd119  「查一下ce这个用户都活跃在哪些…」    dims=4

三套骨架的**维度 id 互不相同** → 闭合账互不认 → 学习侧验过的成果
（`progress.codes` 里带 answer/r 的那条）消费侧读不到 → 重复拆骨架。

根因：**身份挂在问句上**。
`maieutic_scaffold._cache_path` = `md5(question)[:12] + "_" + md5(db_path)[:8]`
→ 换问法 = 换键 = 换骨架 + 换闭合账。

★ 关键设计：req id **不能由问句派生**
------------------------------------
第一版设计想用 `md5(规范化原句)`，**这是错的**：规范化后三个问句仍互不相同
（实测：「帮我查查用户名 ce 的这位女生…」vs「username为ce的女生…」vs
「查一下ce这个用户…」），派生的 id 必然不同 → 什么也解决不了。

原版 maieutic 怎么做（已复核 `prisma/schema.prisma:14`）：
- `Exercise.id` = **slug，教师发布时给的**，不是从题目文本算出来的
- 运行时**只读** `exercise.specGateDimensions`（`src/app/api/session/[sid]/spec/route.ts:48-70`）
- 复用靠 `(exerciseId, studentId)` **精确二元组匹配**（`src/lib/sessions.ts:159-170`）
- 全仓**无** md5 / 相似度 / embedding 检索

⇒ MVE 的对应物：**人导入那一刻系统分配一个一次性 req id**，之后永不改变；
「换个问法再问同一个需求」靠**归题（routing）找回这个 req**，不靠文本相同。

本模块职责（单一事实源）
----------------------
1. 分配 req id（`req_<时间戳><随机>`，一次性、与问句解耦）
2. 记录 **req ↔ 问句** 的映射（原始问句 + 后来归位到它的各种问法）
3. 供消费侧/学习侧按 req id 找同一份骨架与闭合账

**不发明新机制**：这里只存「身份映射」，不存任何判据、不打分、不设门槛。
所有判据仍在 maieutic_scaffold 的既有账上。

持久化
------
`mve/ckpt_maieutic/<scene>/_req_registry.json`

- **原子写**（tmp + os.replace）—— 登记表坏掉 = 身份丢失 = 重复拆骨架，
  与 scenes.json / 骨架缓存同一条铁律（`maieutic_scaffold._atomic_write`）。
- 读失败区分 missing / ok / broken：坏了改名留证后按空重建，
  **不静默吞掉**（否则身份无声漂移，问题会以「又重复拆骨架」的形式出现）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
CKPT = _HERE / "ckpt_maieutic"

# 登记表文件名（放在场景目录里，与骨架缓存同住 → 天然按场景隔离）
REGISTRY_NAME = "_req_registry.json"


# ---------------------------------------------------------------------------
# 原子写 / 读
# ---------------------------------------------------------------------------
def _atomic_write(path: Path, data: dict) -> None:
    """原子写（tmp + os.replace）。与骨架缓存同一把尺子。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def _registry_path(scene_id: str) -> Path:
    return CKPT / str(scene_id or "A") / REGISTRY_NAME


def _read_registry(scene_id: str) -> dict[str, Any]:
    """读登记表。返回 {} 表示空或读不到（调用方按「没有登记」处理）。"""
    p = _registry_path(scene_id)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        # 坏了 → 留证后按空重建。**不静默**：身份丢了会表现为「又拆了一遍
        # 骨架」，排查时必须看得到这份证据。
        try:
            p.replace(p.with_name(p.stem + f".broken{int(time.time())}"
                                  + p.suffix))
        except Exception:
            pass
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# 问句规范化（只用于「同一个问句」的判定，不用于生成 req id）
# ---------------------------------------------------------------------------
_PUNCT = re.compile(r"[\s　,，.。;；:：!！?？、~～\-—_()（）\[\]【】"
                    r"\"'“”‘’/\\|]+")


def normalize_question(q: str) -> str:
    """问句规范化（只用于查表，不用于生成 id）。

    只去空白与标点，**不改词序、不改字** —— 改了就变成「另一句话」，
    按铁律「相似的是另一句话，复用会答偏」，规范化必须保守。
    """
    return _PUNCT.sub("", str(q or "").strip()).lower()


# ---------------------------------------------------------------------------
# req id 的分配与查询
# ---------------------------------------------------------------------------
def new_req_id(scene_id: str = "", question: str = "") -> str:
    """分配一个 req id（一次性，与问句解耦）。

    格式：`req_<YYYYmmddTHHMMSS><4位随机>`。
    - 时间戳：人可读，便于面板按时间排
    - 随机后缀：同秒内两次导入不撞（时间戳单独用会撞）
    - **不含问句的哈希** —— 这是与原版 `Exercise.id`（slug，人给的名字）
      的关键差别：req id 与问句文本**完全无关**，换问法不会换 id。

    ⚠️ 不要改成 `md5(question)` 派生 —— 见模块头「★ 关键设计」。
    """
    t = time.strftime("%Y%m%dT%H%M%S")
    salt = hashlib.md5(f"{t}|{os.getpid()}|{time.time_ns()}".encode()
                       ).hexdigest()[:4]
    return f"req_{t}{salt}"


def register(scene_id: str, question: str, *,
             req_id: str = "", force_new: bool = False) -> dict[str, Any]:
    """登记一个需求（人导入/派发那一刻调用）。

    - 已登记过同一问句 → 复用**最近一次发布**的 req id（幂等）
    - 没登记过 → 分配新 req id

    ★ `force_new=True`（2026-11-11，拆骨架不做归一）：
      **派发即发布**。同一句第二次派发 = 一次新的发布，必须拿新 req id ——
      同构原版 maieutic 的 `Exercise`（教师可以对同一题面发布两次 exercise，
      各自一份 `specGateDimensions`）。
      跳过的只是「逐字幂等」与「归题找回」两道闸门，**不跳过任何判据**：
      骨架怎么拆、账怎么记，仍然全在 maieutic_scaffold 的既有账上。
      旧 req 原样保留（它是另一份骨架 + 另一本账，不是被覆盖的旧版本）。

    ★ 幂等分支取**最新**那次发布（2026-11-11 实测修正）：
      以前的 `for ...: return` 命中**最旧**那条 —— force_new 刚发布完，
      学习侧续跑一调 register 就被拽回旧账，重拆等于没发生。
      口径与 `find_by_question` 完全一致：created_at 最大者胜（并列取后插入）。

    返回 `{"req_id", "question", "created", "known_questions": [...]}`。
    """
    sid = str(scene_id or "A")
    q = str(question or "").strip()
    if not q:
        return {"req_id": "", "question": "", "created": False,
                "known_questions": []}
    reg = _read_registry(sid)
    nq = normalize_question(q)

    if not force_new:
        # ① 已有登记 → 复用**最新**那次发布（幂等）
        best_rid, best_at, best_known, best_q = "", float("-inf"), [], ""
        for rid, rec in reg.items():
            if not isinstance(rec, dict):
                continue
            known = list(rec.get("known_questions") or [])
            hit = False
            if rec.get("question") and normalize_question(
                    str(rec.get("question"))) == nq:
                hit = True
            else:
                for k in known:
                    if normalize_question(str(k)) == nq:
                        hit = True
                        break
            if not hit:
                continue
            at = float(rec.get("created_at") or 0)
            if at >= best_at:
                best_at = at
                best_rid = str(rid)
                best_known = known
                best_q = str(rec.get("question") or q)
        if best_rid:
            _add_known(sid, best_rid, q)
            return {"req_id": best_rid, "question": best_q,
                    "created": False, "known_questions": best_known}

        # ①.5 归题找回（换个问法）：不是同一句，但 routing 认为它属于某个已有需求
        # —— 与 `req_id_of` 的 ② 级同源（routing.match 双阈值，不发明新判据）。
        # 不接这一级 = 每换一种说法就建一个新 req（2026-11-09 服务器实测：同一
        # 需求的第二种问法被分配成 req_...193342968e，旧会话从此读不到）。
        try:
            _rid_k = req_id_of(sid, q)
        except Exception:
            _rid_k = ""
        if _rid_k:
            note_question(sid, q, _rid_k)
            _rec_k = (_read_registry(sid).get(_rid_k) or {})
            return {"req_id": _rid_k,
                    "question": str(_rec_k.get("question") or q),
                    "created": False,
                    "known_questions": list(_rec_k.get("known_questions") or [])}

    # ② 新需求 → 分配
    rid = str(req_id or "").strip() or new_req_id(sid, q)
    reg[rid] = {
        "req_id": rid,
        "question": q,               # 原始问句（这个需求的「题面」）
        "known_questions": [q],      # 后来归位到它的各种问法
        "scene": sid,
        "created_at": time.time(),
        "publish_seq": sum(1 for r in reg.values()
                           if isinstance(r, dict)
                           and normalize_question(str(r.get("question") or "")) == nq),
    }
    _atomic_write(_registry_path(sid), reg)
    return {"req_id": rid, "question": q, "created": True,
            "known_questions": [q]}


def _add_known(scene_id: str, req_id: str, question: str) -> None:
    """把一个新问法记到已有需求下（append，不覆盖）。"""
    sid = str(scene_id or "A")
    reg = _read_registry(sid)
    rec = reg.get(req_id)
    if not isinstance(rec, dict):
        return
    q = str(question or "").strip()
    if not q:
        return
    known = list(rec.get("known_questions") or [])
    nq = normalize_question(q)
    for k in known:
        if normalize_question(str(k)) == nq:
            return                      # 已在册，不重复记
    known.append(q)
    rec["known_questions"] = known
    rec["last_seen_at"] = time.time()
    reg[req_id] = rec
    try:
        _atomic_write(_registry_path(sid), reg)
    except Exception:
        pass


def note_question(scene_id: str, question: str, req_id: str) -> None:
    """把某个问法归到 req 下（换问法时调用；req 为空则什么都不做）。"""
    rid = str(req_id or "").strip()
    if not rid:
        return
    _add_known(str(scene_id or "A"), rid, question)


def get(scene_id: str, req_id: str) -> dict[str, Any]:
    """按 req id 读一条登记。"""
    rid = str(req_id or "").strip()
    if not rid:
        return {}
    rec = _read_registry(str(scene_id or "A")).get(rid)
    return dict(rec) if isinstance(rec, dict) else {}


def list_reqs(scene_id: str = "") -> list[dict[str, Any]]:
    """列出本场景（或全部场景）的需求登记（面板用）。"""
    sid = str(scene_id or "").strip()
    if sid:
        reg = _read_registry(sid)
    else:
        reg = {}
        try:
            for d in sorted(CKPT.iterdir()):
                if d.is_dir():
                    for k, v in _read_registry(d.name).items():
                        if isinstance(v, dict):
                            reg[k] = v
        except Exception:
            pass
    out = []
    for rid, rec in reg.items():
        if isinstance(rec, dict):
            r = dict(rec)
            r.setdefault("req_id", rid)
            out.append(r)
    out.sort(key=lambda r: float(r.get("created_at") or 0))
    return out


def find_by_question(scene_id: str, question: str) -> str:
    """按问句精确找 req id（规范化后比）。找不到返回 ""。

    ★ 2026-11-11（拆骨架不做归一）：同一问句可能**发布过多次**（人再派一次
    就是又一次发布，同构 maieutic 里教师可对同一题面发两次 Exercise）。
    命中多个时取 **created_at 最大** 的那个 —— 最近一次发布才是「当前
    exercise」，骨架/进度/技能都该挂它。旧的那份原样保留（它是**另一份**
    骨架 + 另一本账，面板按 req_id 各自成卡片，互不覆盖）。

    为什么不是「返回第一个」：dict 插入序 = 时间序，第一条是**最旧**的
    那次发布。force_new 刚发布完，所有按问句取路径的调用点（save_progress
    等 16 处）都会落回旧骨架文件 —— 重拆等于没发生。
    """
    nq = normalize_question(question)
    if not nq:
        return ""
    best_rid, best_at = "", float("-inf")
    for rid, rec in _read_registry(str(scene_id or "A")).items():
        if not isinstance(rec, dict):
            continue
        hit = False
        if rec.get("question") and normalize_question(
                str(rec.get("question"))) == nq:
            hit = True
        else:
            for k in (rec.get("known_questions") or []):
                if normalize_question(str(k)) == nq:
                    hit = True
                    break
        if not hit:
            continue
        at = float(rec.get("created_at") or 0)
        if at >= best_at:                  # 并列时取后插入的（dict 序 = 时间序）
            best_at, best_rid = at, str(rid)
    return best_rid


def _req_id_from_import(scene_id: str, import_id: str) -> str:
    """导入记录 → req id（人导入那一刻登记的身份，比问句文本稳定）。

    为什么要这一步（2026-11-09 服务器实测）：
      routing 命中的往往是 `import:<id>` 节点，而它的 label
      **就是那句问话自己** —— 拿 label 反查登记表查不到它自己的
      req（反查循环）。导入记录里的 req_id 是入口当刻写死的，
      不受问句怎么说影响。
    """
    iid = str(import_id or "").strip()
    if not iid:
        return ""
    try:
        import coach as _coach
        rows = _coach.recent_imports(2000)
    except Exception:
        return ""
    for r in reversed(rows):
        if not isinstance(r, dict):
            continue
        if str(r.get("import_id") or "") == iid:
            rid = str(r.get("req_id") or "")
            if rid and rid in _read_registry(str(scene_id or "A")):
                return rid
    return ""


def req_id_of(scene_id: str, question: str, *,
              allow_alloc: bool = False) -> str:
    """这个问句属于哪个需求（req id）？找不到返回 ""。

    ★ 三级解析（每一级都不发明新判据，只用既有的）：

      ① **精确命中**：问句（规范化后）与某需求的原句/已知问法完全相同
         → 直接用它。覆盖「同一句再问」与「同一句第二次导入」。

      ② **归题找回**（换了个问法）：走 MVE 已有的 `routing.match`
         （照 neko material_topic_mapper 的双阈值，见 mve/routing.py 模块头）
         把问句对回已有图谱节点。
         → 若命中的是**需求节点**（`req:` 前缀），直接拿它的 req id
         → 若命中的是**人导入原句**（`import:` 前缀节点），用它的 label
           去登记表里反查 req id
         ⚠️ **排除「问句自己」那个消费节点**（`consume:` 前缀，label = 问句自己）
            —— 它拿 exact_match 满分永远是最佳 → 找回的是问句自己 →
            算到自己 → 骨架永远读不到（2026-11-09 服务器实测，已修过一次）。
            这里用 `routing.match(exclude_ids=...)` 排除。

      ③ **分配新的**（真的是新需求）：`allow_alloc=True` 时分配一个 req id
         并登记。**不分配**时返回 ""（调用方据此说「这是新需求」）。

    ★ 为什么 ② 归题找回是照抄原版而不是发明：
      原版 maieutic **没有**「换个问法」这个场景 —— 需求是教师**预先发布**的
      固定 Exercise，学生打开的 URL 直接带着 `exerciseId`。
      MVE 的问句是自由文本，所以「同一个需求的第 N 种问法」必须靠归题找回
      —— 这是 MVE 独有的必要环节，用的是**既有的**归题机制（②），
      不新增任何「像不像」的门槛。

    ⚠️ **`allow_alloc` 默认 False**（2026-11-09 实测踩到）：
      默认 True 时，「读一次骨架路径」就会凭空造出一个需求 ——
      `_cache_path` 走的是读路径（16 处调用点全是读/写进度账），
      分配副作用会污染登记表，且让「没登记的题」也落到 req 键上（零回归被破坏）。
      **分配只在明确的写入口发生**：人导入那一刻（消费侧 `register` / `on_miss`）。

    返回 req id；找不到且 `allow_alloc=False` 时返回 ""。
    """
    sid = str(scene_id or "A")
    q = str(question or "").strip()
    if not q:
        return ""

    # ① 精确命中
    rid = find_by_question(sid, q)
    if rid:
        return rid

    # ② 归题找回（换了个问法）
    try:
        import routing as _rt
        m = _rt.match(q, sid)
        nid = str(m.get("node_id") or "")
        lbl = str(m.get("node_label") or "").strip()
        # 命中的正是「问句自己」那个消费节点 → 排除它再取下一个候选
        if nid.startswith("consume:"):
            m2 = _rt.match(q, sid, exclude_ids=(nid,))
            nid = str(m2.get("node_id") or "")
            lbl = str(m2.get("node_label") or "").strip()
        if nid.startswith("req:"):
            # 命中的就是需求节点 → 直接拿 req id
            _rid = nid.split(":", 1)[1]
            if _rid:
                note_question(sid, q, _rid)
                return _rid
        if nid.startswith("import:"):
            # 命中的是人导入节点 → 用它的 import_id 去导入记录里取 req（不靠 label）
            _rid_i = _req_id_from_import(sid, nid.split(":", 1)[1])
            if _rid_i:
                note_question(sid, q, _rid_i)
                return _rid_i
        if lbl:
            # 命中的是人导入原句 → 用原句反查 req id
            _rid = find_by_question(sid, lbl)
            if _rid:
                note_question(sid, q, _rid)
                return _rid
    except Exception:
        pass

    # ③ 新需求
    if not allow_alloc:
        return ""
    out = register(sid, q)
    return str(out.get("req_id") or "")


# ---------------------------------------------------------------------------
# 需求节点（让 req 在图谱里是一等实体 → 归题能直接命中它）
# ---------------------------------------------------------------------------
def ensure_req_node(scene_id: str, question: str,
                    req_id: str = "") -> str:
    """在图谱里建/复用一个 `req:<req_id>` 节点，返回节点 id。

    为什么要在图谱里也建一枚：
    - 归题（`routing.match`）只在**图谱节点**上做 → req 要是节点，
      「换个问法」才能直接命中它（否则只能靠 label 反查，脆）
    - 与既有的 `consume:` / `import:` 层**并存**（照原版：
      Exercise 是需求、Session 是过程，两者分开）

    与 `consume:` / `import:` 的区别：
    - `consume:<topic_id>` = 「这道题的**运行时**记录」（消费侧脏账）
    - `import:<批次>` = 「**哪一次**导入长的」（批次 ≠ 需求身份）
    - `req:<req_id>` = 「**这个需求**」（跨批次、跨问法恒定）
    """
    sid = str(scene_id or "A")
    q = str(question or "").strip()
    rid = str(req_id or "").strip() or req_id_of(sid, q)
    if not rid:
        return ""
    nid = f"req:{rid}"
    try:
        from knowledge_graph import KnowledgeGraph, Node
        g = KnowledgeGraph.load()
        if nid in (getattr(g, "nodes", {}) or {}):
            return nid
        g.ensure_scene_root(sid)
        g.add_node(Node(id=nid, kind="req", label=q[:60] or nid,
                        detail={"req_id": rid, "question": q,
                                "source": "human_import"},
                        root_scene=str(sid)))
        g.save()
    except Exception:
        return nid
    return nid


# ---------------------------------------------------------------------------
# 骨架缓存键（req id 版）
# ---------------------------------------------------------------------------
def skeleton_path(scene_id: str, req_id: str, db_path: str = "") -> Path:
    """需求级骨架缓存路径：`CKPT/<scene>/req_<req_id>_<dbhash>.json`。

    db_path 进键的理由与原 `_cache_path` **完全一致**（照抄，不改口径）：
    场景换了库 / 首次加 schema 观察面时，旧骨架（可能编了不存在的参数）
    不能继续命中，必须重拆。
    """
    rid = str(req_id or "").strip() or "unbound"
    if not db_path:
        try:
            import scene_profile as _sp
            db_path = str((_sp.profile(str(scene_id or "A")) or {})
                          .get("db_path") or "")
        except Exception:
            db_path = ""
    dh = hashlib.md5(str(db_path or "").encode("utf-8")).hexdigest()[:8]
    return CKPT / str(scene_id or "A") / f"req_{rid}_{dh}.json"


def legacy_skeleton_path(scene_id: str, question: str,
                         db_path: str = "") -> Path:
    """旧键（问句 md5）—— 只用于**读时兼容 + 迁移**，不再用于写。"""
    h = hashlib.md5(str(question or "").strip()
                    .encode("utf-8")).hexdigest()[:12]
    if not db_path:
        try:
            import scene_profile as _sp
            db_path = str((_sp.profile(str(scene_id or "A")) or {})
                          .get("db_path") or "")
        except Exception:
            db_path = ""
    dh = hashlib.md5(str(db_path or "").encode("utf-8")).hexdigest()[:8]
    return CKPT / str(scene_id or "A") / f"{h}_{dh}.json"


def migrate_legacy(scene_id: str, question: str, req_id: str, *,
                    db_path: str = "") -> Path | None:
    """旧键 → 新键的**读时迁移**。

    规则（照抄判据，不发明）：
    - 只按**当前问句的 md5** 回退找旧键 —— **不许扫目录取最近**
      （那会拿到别的需求的骨架 = 串台，比读不到更坏）
    - 命中 → 复制内容到新键 + 旧文件改名留证（`.migrated<ts>`）
    - 没命中 → 返回 None（如实说「没有旧账」，不猜）

    返回新键路径；没有可迁移的返回 None。
    """
    rid = str(req_id or "").strip()
    if not rid:
        return None
    new_p = skeleton_path(scene_id, rid, db_path)
    if new_p.exists():
        return new_p
    old_p = legacy_skeleton_path(scene_id, question, db_path)
    if not old_p.exists():
        return None
    try:
        data = json.loads(old_p.read_text(encoding="utf-8"))
    except Exception:
        return None
    # 迁移时补上 req_id —— 骨架文件从此知道自己属于哪个需求
    if isinstance(data, dict):
        data.setdefault("req_id", rid)
    try:
        _atomic_write(new_p, data)
        old_p.replace(old_p.with_name(old_p.stem + f".migrated{int(time.time())}"
                                      + old_p.suffix))
    except Exception:
        return None
    return new_p
