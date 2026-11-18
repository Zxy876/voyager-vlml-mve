#!/usr/bin/env python3
"""MVE 面板：看 Voyager 的进步曲线，并能在面板上一键启停它。

跑法：
    python mve/dashboard.py            # 然后打开 http://127.0.0.1:8777
    python mve/dashboard.py --port 9000

形态来源（重要）：
- VLML 仓库里没有任何前端文件（无 html/tsx/jsx/vue），它的"面板"就是 LLM 对话界面，
  没有可抄的 UI。所以面板形态取自猫娘伴学的 static/ 前端：
    · 布局：page-shell → hero（左标题+状态 / 右 summary-pills+控件）
            → workspace-nav（6 格 tab）→ workspace-stage（**一次只显示一个面板**）
    · 视觉：纸质卡 rgba 白 + 柔和阴影 + 8px 圆角；选中态用品牌色底 + 底部 3px 指示条
    · 色阶：直接复用伴学的 mastery 五档色（weak/progress/good/mastered/new）
  数据取自 MVE 自己的 run_log.jsonl / causal_timeline.jsonl / pilot_state.json。

为什么改成 tab 分区：上一版把 12 个 section 一路堆到底，信息密度过高，
"当前该看什么"完全靠人自己找。伴学的做法是**一次只展开一个工作区**。

面板不做任何推断，只显示真实发生过的东西。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import panel_data  # noqa: E402
import pilot  # noqa: E402

ROOT = Path(__file__).resolve().parent


# --- 知识图谱自检：避免 git pull 丢 json / 换库后反复 "not built" ---
import threading as _threading

_graph_build_lock = _threading.RLock()


def _do_build_graph():
    """在持有 _graph_build_lock 的前提下重建并落盘（不自带锁，调用方负责）。"""
    import knowledge_graph as kg
    # build() 内 `from tasks import TASKS` 需要仓库根；面板只注入了 mve/
    _repo_root = str(ROOT.parent)
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    g = kg.build()
    g.save()
    return g


def _ensure_knowledge_graph(verbose: bool = True) -> bool:
    """启动自检：图谱缺失或过期就就地重建。

    过期判据 = schema_version + 题指纹 + 建模文档指纹（复用 knowledge_graph._stale）
    + 默认 duckdb 比图谱新（换库）。这样 pull 丢掉的被 gitignore 的 json、
    以及换库后内容过期都会自动重建，不再需要人工跑 --build。重建失败不影响启动。
    """
    try:
        import knowledge_graph as kg
    except Exception as e:
        if verbose:
            print(f"[图谱自检] 无法导入 knowledge_graph：{type(e).__name__}: {e}")
        return False

    gp = kg.GRAPH_PATH
    need, reason = True, "未知"
    if gp.exists() and gp.stat().st_size > 0:
        try:
            raw = json.loads(gp.read_text(encoding="utf-8", errors="replace"))
            need = kg._stale(raw)
            reason = "schema/题/建模文档指纹过期" if need else ""
            if not need:
                # 换库检测：默认 duckdb 比图谱新 → 过期
                db = ROOT.parent / "vlml" / "data" / "vlml_rib.duckdb"
                if db.exists() and db.stat().st_mtime > gp.stat().st_mtime:
                    need, reason = True, f"数据库更新（{db.name}）"
        except Exception as e:
            need, reason = True, f"图谱读取失败：{type(e).__name__}"

    if not need:
        if verbose:
            print(f"[图谱自检] 图谱已是最新（{gp.name}）")
        return True

    if verbose:
        print(f"[图谱自检] {reason}，开始自动重建图谱…")
    try:
        with _graph_build_lock:
            g = _do_build_graph()
        s = g.to_dict()["summary"]
        if verbose:
            print(f"[图谱自检] 重建完成：维度 {s['dimensions']} · 工具 {s['tools']} "
                  f"· 表 {s['tables']} · 边 {s['edges']}")
        return True
    except Exception as e:
        if verbose:
            print(f"[图谱自检] 重建失败（面板照常启动，/api/graph 会回退提示）："
                  f"{type(e).__name__}: {e}")
        return gp.exists()


def _rebuild_graph_if_missing() -> bool:
    """热路径兜底：图谱确实缺失/空（如运行时被 baseline exam 清库删除）时重建。"""
    try:
        import knowledge_graph as kg
    except Exception:
        return False
    gp = kg.GRAPH_PATH
    try:
        if gp.exists() and gp.stat().st_size > 0 and kg.KnowledgeGraph.load().nodes:
            return True
    except Exception:
        pass
    with _graph_build_lock:
        try:
            if gp.exists() and gp.stat().st_size > 0 and kg.KnowledgeGraph.load().nodes:
                return True
        except Exception:
            pass
        try:
            _do_build_graph()
            return True
        except Exception as e:
            print(f"[图谱自检] 兜底重建失败：{type(e).__name__}: {e}")
            return False


def _restore_action_registry(verbose: bool = False) -> dict[str, int]:
    """启动自检：恢复可执行动作注册表（1→n 泛化）。

    ⚠️ 为什么需要：注册表是**进程内存态**，面板一重启就丢了。实测重启后
    `action_count {"A": 0}`，而同一份代码在独立进程里反射出 10 条 ——
    场景卡于是显示「⛔ 0 个可执行动作（不许插入）」，把明明能跑的场景
    误判成跑不起来（这是**假告警**，比没有告警更糟）。

    恢复两步走：
      1. `actions.restore()` 从 `action_registry.json` 快照恢复（含 impl 引用）；
      2. 快照缺失/过旧时，对每个已注册场景重跑 `register_builtin_actions()`
         反射真实签名（**不手写名单**）。
    """
    try:
        import actions
    except Exception as e:
        print(f"[动作自检] 无法导入 actions：{type(e).__name__}: {e}")
        return {}
    restored = actions.restore()
    try:
        import scenes as _sc
        for s in _sc.list_scenes():
            sid = str(s.get("id") or "")
            if not sid or actions.entry_count(sid) > 0:
                continue
            reg = actions.register_builtin_actions(sid)
            if verbose:
                print(f"[动作自检] {sid}: 反射注册 {reg.get('added')} 个原语"
                      f"（{str(reg.get('reason'))[:60]}）")
            restored[sid] = actions.entry_count(sid)
    except Exception as e:
        print(f"[动作自检] 反射注册失败（面板照常启动）：{type(e).__name__}: {e}")
    if verbose:
        print(f"[动作自检] 恢复后 = {restored}")
    return restored


def _graph_json() -> str:
    """知识图谱：读落盘的 json，**不 import vlml_env**（保持面板轻量、秒开）。

    照伴学 `graph_read_model.py` 的规矩：只返回结构，不在这一层做任何推断。
    没建过图谱时返回 empty 标记，前端据此提示"先跑 --build"。
    """
    try:
        import knowledge_graph
        g = knowledge_graph.KnowledgeGraph.load()
    except Exception as e:
        return json.dumps({"empty": True, "error": f"{type(e).__name__}: {e}"},
                          ensure_ascii=False)
    if not g.nodes:
        # 启动自检一般已重建；这里兜底（如运行时被 baseline exam 清库删除）
        if _rebuild_graph_if_missing():
            try:
                g = knowledge_graph.KnowledgeGraph.load()
            except Exception:
                pass
    if not g.nodes:
        return json.dumps({"empty": True, "error": "图谱未构建，先跑 python mve/knowledge_graph.py --build"},
                          ensure_ascii=False)
    out = g.to_dict()
    # ---- 1→n：图谱**按当前场景过滤**（每个场景一张图）----
    # A 的种子图谱（VLML Valorant：表→工具→维度，root_scene 缺省即 A）只属于 A；
    # Scene B 冷启动只有场景根节点，随本场景的导入动态挂载。
    # 不过滤的后果（2026-10-18 实测）：人在 B 里问"特斯拉股价"，图谱页照样画着
    # A 的 360 个 Valorant 节点，读起来像"这个问题归到了 VLML 图"—— 实际是串 scene。
    try:
        import scenes as _scn
        sid = str(_scn.current_scene_id() or "A")
        keep = {nid for nid, n in g.nodes.items()
                if str(getattr(n, "root_scene", "A") or "A") == sid}
        out["total_nodes"] = len(out.get("nodes") or [])
        out["nodes"] = [n for n in (out.get("nodes") or [])
                        if str(n.get("id")) in keep]
        out["edges"] = [e for e in (out.get("edges") or [])
                        if str(e.get("from")) in keep and str(e.get("to")) in keep]
        out["scene_id"] = sid
        out["scene_nodes"] = len(out["nodes"])
        out["scene_edges"] = len(out["edges"])
        # Δ 场景专属节点数（证明量：排除根占位与已删除）
        out["scene_specific_nodes"] = sum(
            1 for n in g.nodes.values()
            if str(getattr(n, "root_scene", "A") or "A") == sid
            and str(getattr(n, "kind", "")) != "scene_root"
            and str(getattr(n, "status", "")) != "deleted")
        # 摘要按本场景重算（前端计数行「N 维度 · M 工具 · K 表 · J 边」）
        sm = dict(out.get("summary") or {})
        kinds: dict[str, int] = {}
        for n in out["nodes"]:
            k = str(n.get("kind") or "")
            kinds[k] = kinds.get(k, 0) + 1
        sm.update({"dimensions": kinds.get("dimension", 0),
                   "tools": kinds.get("tool", 0),
                   "tables": kinds.get("table", 0),
                   "edges": len(out["edges"])})
        out["summary"] = sm
        # ── 课程图谱（2026-11-14）──────────────────────────────────
        # 课程场景（P 书单 / N 教材）的节点是 book + topic + exercise，起步就是
        # 648 个单元。前端只认 table / tool / dimension 三类，所以接口明明返回了
        # 720 个节点，图谱页还是显示「这个场景还没有可画的节点」。
        # 这里给一份**围绕当前关**的小图：当前这本书 → 它的单元 → 已学的题。
        # 全量不画：648 个单元铺开是一张目录墙，看不出自己学到哪。
        try:
            import graph_curriculum as _gcur
            _slots = list(_gcur.progress(sid) or [])
            if _slots:
                _cur = next((s for s in _slots
                             if not s.get("done") and not s.get("skipped")), None) \
                    or next((s for s in _slots if not s.get("done")), None)
                _bk = str((_cur or _slots[0]).get("book") or "")
                _units = [s for s in _slots if str(s.get("book") or "") == _bk]
                _ex: dict[str, list] = {}
                for _nid, _nd in g.nodes.items():
                    if str(getattr(_nd, "kind", "")) != "exercise":
                        continue
                    _pp = str(_nid).split(":")
                    if len(_pp) >= 5 and _pp[1] == "gen":
                        _ex.setdefault("unit:%s:%s" % (_pp[2], _pp[3]), []).append({
                            "id": _nid,
                            "status": str(getattr(_nd, "status", "") or ""),
                            "label": str(getattr(_nd, "label", "") or "")})
                out["course"] = {
                    "book": _bk,
                    "units": [{
                        "id": s.get("module_id") or "",
                        "label": s.get("title") or "",
                        "done": bool(s.get("done")),
                        "skipped": bool(s.get("skipped")),
                        "current": bool(_cur and s.get("module_id") == _cur.get("module_id")),
                        "learned": int(s.get("learned") or 0),
                        "need": int(s.get("need") or 1),
                        "exercises": _ex.get(str(s.get("module_id")), []),
                    } for s in _units],
                    "unit_total": len(_slots),
                    "unit_done": sum(1 for s in _slots if s.get("done")),
                    "unit_skipped": sum(1 for s in _slots if s.get("skipped")),
                    "current": str((_cur or {}).get("module_id") or ""),
                }
        except Exception as e:
            out["course_error"] = f"{type(e).__name__}: {e}"[:200]
    except Exception as e:
        out["scene_filter_error"] = f"{type(e).__name__}: {e}"[:160]
    # 待审候选：运行期题带进来的新维度名，**不建节点**（照伴学：运行期新建的
    # 知识点不进权威层）。单独返回，前端据此在图谱页顶上显示一条。
    try:
        import knowledge_graph
        out["pending_dims"] = knowledge_graph.load_pending_dimensions()
    except Exception:
        out["pending_dims"] = []
    # 学科登记册：人可填写的归属容器（换数据源后填赛事/战队，跑出的条目挂进去）
    try:
        import subjects
        subs = subjects.all_subjects()
        out["subjects"] = subs
        out["subject_counts"] = {s["id"]: len(subjects.entries_of(s["id"]))
                                 for s in subs}
        out["subject_unassigned"] = subjects.unassigned()
        out["data_source"] = subjects.data_source()
    except Exception:
        out["subjects"] = []
        out["subject_unassigned"] = []
    # ── 导入的题（req 节点）：将做 / 已做 ──────────────────────────
    # 已做的排前面（同构猫娘伴学：图上先看见自己做完的，进步可见）。
    # 状态语义沿用图谱词表：verified=已做，unverified=将做。只由
    # Phase2 过关翻面（learn_orchestrate.attach_phase_nodes），重做
    # 没过关不回翻。detail.question 存全题面，前端点选直接送去拆骨架。
    try:
        _reqs = [n for n in g.nodes.values()
                 if str(getattr(n, "kind", "")) == "req"
                 and str(getattr(n, "root_scene", "A") or "A") == sid
                 and str(getattr(n, "status", "")) != "deleted"]
        _req_rows = [{
            "id": str(getattr(n, "id", "")),
            "label": str(getattr(n, "label", "")),
            "question": str((getattr(n, "detail", {}) or {}).get("question")
                            or getattr(n, "label", "")),
            "req_id": str((getattr(n, "detail", {}) or {}).get("req_id") or ""),
            "status": str(getattr(n, "status", "") or ""),
            "solved_at": str((getattr(n, "detail", {}) or {}).get("solved_at") or ""),
        } for n in _reqs]
        _req_rows.sort(key=lambda r: (0 if r["status"] == "verified" else 1,
                                      r["label"]))
        out["req_nodes"] = _req_rows
        out["req_done"] = sum(1 for r in _req_rows if r["status"] == "verified")
    except Exception:
        out["req_nodes"] = []
        out["req_done"] = 0
    return json.dumps(out, ensure_ascii=False)

def _scope_json() -> str:
    """当前练习范围（只读）。照伴学 study_get_practice_scope。"""
    try:
        import practice_scope
        return json.dumps({"ok": True, "scope": practice_scope.get_scope()},
                          ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                          ensure_ascii=False)


# 默认只绑回环：面板没有任何鉴权，绑 0.0.0.0 等于把「启停进程」的接口裸奔到公网。
# 要放到服务器上从外面访问，显式给 --host（配合安全组只放行自己的 IP）。
PORT = int(os.environ.get("MVE_PANEL_PORT") or 8777)
HOST = os.environ.get("MVE_PANEL_HOST") or "127.0.0.1"
# 挂到反向代理子路径下时用（如 https://host:8443/mve/）：
# 服务端剥前缀，前端所有 fetch 也要跟着加前缀，否则会打到代理的根路径上去。
PREFIX = os.environ.get("MVE_PANEL_PREFIX") or ""

for _i, _a in enumerate(sys.argv[1:]):
    if _a == "--port" and _i + 2 <= len(sys.argv):
        PORT = int(sys.argv[_i + 2])
    if _a == "--host" and _i + 2 <= len(sys.argv):
        HOST = sys.argv[_i + 2]
    if _a == "--prefix" and _i + 2 <= len(sys.argv):
        PREFIX = "/" + sys.argv[_i + 2].strip("/")
def _strip_prefix(path: str) -> str:
    """剥掉反向代理挂在前面的子路径（/mve/api/state → /api/state）。

    设了前缀却没带前缀的请求一律打空串（走 404），避免代理后面被直接绕过前缀访问。
    """
    if PREFIX:
        if path == PREFIX:
            return "/"
        return path[len(PREFIX):] if path.startswith(PREFIX + "/") else ""
    return path


def _html() -> str:
    """前缀注入：前端 fetch('/api/...') 要带上前缀，否则请求会打到代理的根路径。"""
    if not PREFIX:
        return HTML
    return (HTML.replace("<head>", '<head>\n'
                         f'<script>window.MVE_PREFIX="{PREFIX}";</script>', 1)
                .replace("fetch('", "fetch(window.MVE_PREFIX+'"))


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MVE 面板 · Voyager 进步曲线</title>
<style>
:root{
  color-scheme: light;
  font-family:"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  --bg:#f3f7f1;
  --paper:rgba(253,255,250,.94);
  --paper-strong:rgba(255,255,255,.98);
  --ink:#1f2924;
  --muted:#607168;
  --line:rgba(31,41,36,.13);
  --brand:#2f7d57;
  --brand-strong:#17563d;
  --accent:#d58b2b;
  --accent-strong:#8a5317;
  --warning:#b7791f;
  --warning-bg:rgba(183,121,31,.10);
  /* 伴学 mastery 五档色，直接搬过来 */
  --mastery-new:#cbd5d0;
  --mastery-weak:#e89a90;
  --mastery-progress:#e2b85a;
  --mastery-good:#9bd9b8;
  --mastery-mastered:#82d99e;
  --shadow:0 10px 24px rgba(31,52,40,.07);
  --radius:8px;
  --radius-sm:6px;
  background:var(--bg);
  color:var(--ink);
}
*{box-sizing:border-box}
body{
  margin:0;min-height:100vh;
  background:
    linear-gradient(90deg,rgba(47,125,87,.045) 1px,transparent 1px),
    linear-gradient(rgba(47,125,87,.04) 1px,transparent 1px),
    linear-gradient(180deg,#f8fbf6 0%,#eef5f2 100%);
  background-size:36px 36px,36px 36px,auto;
}
h1,h2,p{margin:0}
button,textarea,select{font:inherit}

/* ---------- 骨架 ---------- */
.page-shell{width:min(1240px,calc(100vw - 24px));min-width:0;margin:0 auto;padding:28px 0 36px}
@media(max-width:980px){.page-shell{width:calc(100vw - 24px);padding:20px 0 28px}}
.main-view{display:grid;gap:18px}

/* ---------- hero ---------- */
.hero{
  position:relative;display:grid;gap:24px;align-items:center;
  grid-template-columns:minmax(300px,.9fr) minmax(520px,1.3fr);
  min-height:166px;padding:24px 24px 24px 28px;
  border:1px solid rgba(47,125,87,.20);
  border-left:6px solid rgba(47,125,87,.72);
  border-radius:var(--radius);
  background:linear-gradient(135deg,rgba(255,255,255,.96),rgba(244,249,241,.86)),var(--paper);
  box-shadow:var(--shadow);overflow:hidden;
}
@media(max-width:980px){.hero{grid-template-columns:1fr;min-height:0}}
.hero__copy,.hero__controls{position:relative;z-index:1}
.hero__copy{display:grid;gap:10px}
.hero__eyebrow{color:var(--brand-strong);font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}
.hero-title{display:inline-flex;align-items:center;gap:8px;font-size:26px;line-height:1.15}
.hero-title__cat{display:inline-grid;place-items:center;width:32px;height:32px;
  border:1px solid rgba(47,125,87,.20);border-radius:var(--radius);
  background:rgba(47,125,87,.10);font-size:18px;line-height:1}
.hero__status{color:var(--muted);font-size:14px}
.hero__controls{display:grid;gap:14px;justify-items:end}
.summary-pills{display:flex;gap:8px;align-items:center;justify-content:flex-end;
  width:100%;flex-wrap:wrap}
.summary-pill{display:grid;gap:3px;min-width:132px;max-width:230px;padding:9px 12px;
  border:1px solid rgba(47,125,87,.16);border-left:3px solid rgba(47,125,87,.48);
  border-radius:var(--radius-sm);background:rgba(253,255,250,.86);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.92)}
.summary-pill span{color:var(--muted);font-size:12px;font-weight:700;text-transform:uppercase}
.summary-pill strong{min-width:0;color:var(--ink);font-size:14px;line-height:1.35;overflow-wrap:anywhere}

/* ---------- 主控台（启动 / 停止） ---------- */
.runctl{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-top:2px}
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 16px;border-radius:var(--radius-sm);
  border:1px solid transparent;cursor:pointer;font-size:13.5px;font-weight:600;
  transition:transform 150ms ease,background 150ms ease,border-color 150ms ease}
.btn:hover{transform:translateY(-1px)}
.btn:disabled{opacity:.55;cursor:not-allowed;transform:none}
.btn-primary{background:var(--brand);color:#fff;box-shadow:0 1px 2px rgba(31,52,40,.12)}
.btn-primary:hover{background:var(--brand-strong)}
.btn-danger{background:#c0392b;color:#fff}
.btn-danger:hover{background:#96281a}
.btn-secondary{background:#fff;color:#374151;border-color:var(--line)}
.btn-secondary:hover{border-color:rgba(47,125,87,.42);background:rgba(237,248,240,.94)}
.runctl select{padding:8px 10px;border:1px solid var(--line);border-radius:var(--radius-sm);
  background:#fff;color:var(--ink);font-size:13px}
.runctl .lbl{font-size:12px;color:var(--muted);font-weight:700;text-transform:uppercase}
.runstate{display:inline-flex;align-items:center;gap:7px;font-size:13px;color:var(--muted)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mastery-new);display:inline-block}
.dot.on{background:#22c55e;box-shadow:0 0 0 3px rgba(34,197,94,.18);animation:pulse 1.6s ease-in-out infinite}
.dot.off{background:#9ca3af}
.dot.warn{background:#f59e0b;box-shadow:0 0 0 3px rgba(245,158,11,.18);animation:pulse 1.6s ease-in-out infinite}
/* 练习范围条：范围由知识图谱页设定，这里只显示 + 清除 */
.scopebar{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-top:8px;
  padding:7px 10px;border:1px dashed var(--line);border-radius:var(--radius-sm);background:rgba(255,255,255,.55)}
.scopebar .lbl{font-size:12px;color:var(--muted);font-weight:700;text-transform:uppercase}
.scopebar .hint{font-size:12px;color:var(--muted)}
.scope-act{display:inline-flex;gap:6px;align-items:center}
/* 场景栏（1→n 可插拔）：与练习范围条同款样式的最小入口 —— 切换/新建场景、
   显示当前场景的 Δ 专属节点数与导入曲线摘要。不另开页面（去重，2026-10-18）。 */
.scenebar{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-top:8px;
  padding:7px 10px;border:1px dashed var(--line);border-radius:var(--radius-sm);background:rgba(255,255,255,.55)}
.scenebar .lbl{font-size:12px;color:var(--muted);font-weight:700;text-transform:uppercase}
.scenebar .hint{font-size:12px;color:var(--muted)}
.scenebar select{font:inherit;font-size:13px;padding:3px 8px;border:1px solid var(--line);
  border-radius:8px;background:#fff;color:var(--ink)}
.tbl tr.now td{background:#eef8f3}
@keyframes pulse{50%{box-shadow:0 0 0 6px rgba(34,197,94,.06)}}

/* ---------- 工作区 tab ---------- */
.workspace-nav{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:10px;min-width:0;
  padding:10px;border:1px solid rgba(31,41,36,.12);border-radius:var(--radius);
  background:rgba(255,255,255,.88);box-shadow:var(--shadow)}
@media(max-width:980px){.workspace-nav{grid-template-columns:repeat(3,minmax(0,1fr))}}
.workspace-tab{position:relative;display:grid;grid-template-columns:auto minmax(0,1fr);
  grid-template-rows:auto auto;gap:5px 9px;align-items:center;min-width:0;min-height:68px;
  padding:11px 12px;color:var(--ink);text-align:left;overflow:hidden;
  border:1px solid rgba(31,41,36,.11);border-radius:var(--radius-sm);
  background:rgba(250,252,249,.94);box-shadow:0 1px 2px rgba(31,52,40,.04);
  cursor:pointer;transition:transform 150ms ease,border-color 150ms ease,background 150ms ease}
.workspace-tab::after{content:"";position:absolute;inset:auto 10px 0;height:3px;border-radius:999px 999px 0 0;
  background:var(--brand);opacity:0;transform:scaleX(.55);transition:opacity 150ms ease,transform 150ms ease}
.workspace-tab:hover{border-color:rgba(47,125,87,.30);background:#fff;transform:translateY(-2px)}
.workspace-tab[aria-selected="true"]{color:var(--brand-strong);
  border-color:rgba(47,125,87,.38);background:rgba(237,248,240,.92)}
.workspace-tab[aria-selected="true"]::after{opacity:1;transform:scaleX(1)}
.workspace-tab__icon{grid-row:1/-1;display:grid;place-items:center;width:34px;height:34px;
  color:var(--brand-strong);border:1px solid rgba(47,125,87,.20);border-radius:10px;
  background:rgba(237,248,240,.92);font-size:17px}
.workspace-tab__label{min-width:0;font-size:13.5px;font-weight:700;line-height:1.25}
.workspace-tab__status{min-width:0;font-size:11.5px;color:var(--muted);line-height:1.35;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ---------- 面板 ---------- */
.workspace-stage{display:grid;gap:16px}
.panel{border:1px solid var(--line);border-radius:var(--radius);background:var(--paper-strong);
  box-shadow:var(--shadow);padding:18px 20px}
.panel__head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:12px;
  padding-bottom:10px;border-bottom:1px solid var(--line)}
.panel__head h2{font-size:16px;line-height:1.3}
.panel__head .hint{font-size:12px;color:var(--muted)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:980px){.grid2{grid-template-columns:1fr}}

h3{margin:16px 0 8px;font-size:13px;color:var(--muted);font-weight:700;
   text-transform:uppercase;letter-spacing:.04em}
h3:first-child{margin-top:0}
.kpis{display:flex;gap:26px;flex-wrap:wrap}
.kpi .label{font-size:12px;color:var(--muted);font-weight:700;text-transform:uppercase}
.kpi .value{font-size:26px;font-weight:600;line-height:1.2}
.chip{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
      background:rgba(47,125,87,.12);color:var(--brand-strong);margin-right:6px}
.chip.warn{background:var(--warning-bg);color:#784910}
.chip.bad{background:rgba(192,57,43,.12);color:#8a2015}
.chip.ok{background:rgba(130,217,158,.28);color:#14532d}
.chip.grey{background:rgba(31,41,36,.07);color:#4b5563}
.chip.m-weak{background:var(--mastery-weak);color:#7a2318}
.chip.m-progress{background:var(--mastery-progress);color:#6b4308}
.chip.m-good{background:var(--mastery-good);color:#14532d}
.chip.m-mastered{background:var(--mastery-mastered);color:#0f4a2a}
.chip.m-new{background:var(--mastery-new);color:#3f4a44}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:700;font-size:12px}
td.num{font-variant-numeric:tabular-nums}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.empty{color:var(--muted);font-size:13px;padding:8px 0}
.note{border:1px solid #fde68a;border-radius:var(--radius-sm);padding:10px 12px;
      font-size:12.5px;color:#78350f;background:#fffbeb;margin-top:10px}
.note.info{border-color:rgba(47,125,87,.28);background:rgba(237,248,240,.92);color:#17563d}
.note.bad{border-color:rgba(220,38,38,.3);background:#fef2f2;color:#7f1d1d}
ul{margin:6px 0 0;padding-left:20px}
li{margin:3px 0}
.tool{display:inline-block;background:rgba(31,41,36,.05);border:1px solid var(--line);
      border-radius:5px;padding:1px 7px;margin:2px 3px 2px 0;font-size:12px}
.tool.new{background:rgba(130,217,158,.28);border-color:#86efac;color:#14532d}
.legend{font-size:12px;color:var(--muted);margin-top:6px}
.nocmp{border:1px dashed #9ca3af;border-radius:var(--radius-sm);padding:12px 14px;
       margin-top:10px;background:#fafafa}
.nocmp-tag{display:inline-block;font-size:11px;color:#6b7280;border:1px dashed #9ca3af;
           border-radius:4px;padding:1px 7px;margin-bottom:7px}
.nocmp-body{font-size:13.5px;line-height:1.75}
.nocmp-sub{font-size:12px;color:var(--muted);margin-top:7px}
.nocmp-sub ul{margin:4px 0 0}
.tasks{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:10px}
@media(max-width:980px){.tasks{grid-template-columns:1fr}}
.taskcard{border:1px solid var(--line);border-radius:var(--radius-sm);padding:11px 13px;background:#fcfcfd}
.taskcard.now{border-color:rgba(47,125,87,.42);background:rgba(237,248,240,.72)}
.taskcard .th{font-size:13px;margin-bottom:6px;line-height:1.45}
.taskcard .tm{font-size:12px;color:var(--muted);margin-top:4px}
.recmd{border:1px solid var(--line);border-radius:var(--radius-sm);padding:11px 13px;background:#fff;margin-top:10px}
.recmd.now{border-color:rgba(47,125,87,.42);background:rgba(237,248,240,.72)}
.recmd .rq{font-size:13.5px;line-height:1.5;margin:5px 0}
.recmd .rw{font-size:12.5px;color:#374151;margin-top:5px}
.changed{display:inline-block;font-size:11px;background:var(--warning-bg);color:#784910;
         border-radius:4px;padding:1px 7px;margin-left:6px}
.timeline{font-size:12.5px}
.timeline .tl{display:flex;gap:9px;padding:5px 0;border-bottom:1px solid rgba(31,41,36,.07)}
.timeline .tl:last-child{border-bottom:0}
.timeline .seq{color:#9ca3af;font-variant-numeric:tabular-nums;min-width:28px}
.timeline .kd{min-width:78px}
.timeline .ts{color:var(--muted);margin-left:auto;font-size:11.5px;white-space:nowrap}
.imp{border:1px solid rgba(47,125,87,.28);background:rgba(237,248,240,.62);
     border-radius:var(--radius);padding:14px 16px}
.imp textarea{width:100%;min-height:64px;resize:vertical;padding:9px 11px;
  border:1px solid var(--line);border-radius:var(--radius-sm);font:13.5px/1.6 inherit;
  color:var(--ink);background:#fff;outline:none}
.imp textarea:focus{border-color:var(--brand)}
.imp select{padding:7px 10px;border:1px solid var(--line);border-radius:var(--radius-sm);
  font:13px inherit;background:#fff;color:var(--ink);max-width:360px}
.imp-row{display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap;margin-top:10px}
.hit{font-size:12px;color:var(--muted);margin-top:8px;min-height:18px}
.hit b{color:var(--brand-strong)}
.hit.no b{color:#8a2015}
.fact{border-left:3px solid rgba(47,125,87,.38);padding-left:10px;margin:8px 0;font-size:13px}
.fact .fv{font-weight:600}
.spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(47,125,87,.24);
      border-top-color:var(--brand);border-radius:50%;animation:sp .7s linear infinite;
      vertical-align:-1px;margin-right:6px}
@keyframes sp{to{transform:rotate(360deg)}}
.console{background:#12211a;color:#d7e6dc;border-radius:var(--radius-sm);padding:12px 14px;
  font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;
  max-height:460px;overflow:auto;margin-top:10px}
.console .dim{color:#7f9a8c}
/* 「不停」开关：同构原版 Voyager 的 while True —— 学习循环本来就不带停，
   只有人点停止（或跑满上限）才退。面板以前写死 rounds:3，一次启动只跑 3 道题
   就退出，29 个节点的图谱根本跑不完。这个开关让"跑几题"变成人的显式选择。 */
.runctl-ck{display:inline-flex;align-items:center;gap:5px;font-size:12px;
  color:#5c6f66;cursor:pointer;user-select:none}
.runctl-ck input{accent-color:var(--brand);cursor:pointer;vertical-align:-1px}
.runctl-ck.off{opacity:.55;cursor:not-allowed}
/* ---------- 练习列表（同构助产士 Instructor · Exercises） ----------
   2026-11-14 用户：「概览应该是可以点击的 exercise」。以前概览是一张卡 + 下拉
   切题 —— 那是详情；列表页该是一排卡，每张卡一道题、几个数一目了然。 */
.exgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(272px,1fr));gap:12px;margin-top:10px}
.excard{position:relative;display:flex;flex-direction:column;gap:6px;text-align:left;
  border:1px solid var(--line);border-radius:var(--radius-sm);padding:13px 14px 12px;
  background:#fcfcfd;cursor:pointer;font:inherit;color:var(--ink);transition:transform .12s,border-color .12s,box-shadow .12s}
.excard:hover{transform:translateY(-2px);border-color:rgba(47,125,87,.34);
  box-shadow:0 6px 18px rgba(31,41,36,.07)}
.excard__unit{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.excard__title{font-size:13.5px;font-weight:700;line-height:1.4;min-height:38px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.excard__row{display:flex;justify-content:space-between;gap:8px;font-size:11.5px;
  color:var(--muted);border-top:1px dashed rgba(31,41,36,.10);padding-top:6px}
.excard__row b{color:var(--ink);font-weight:600}
.excard__bar{height:4px;border-radius:999px;background:rgba(31,41,36,.08);overflow:hidden}
.excard__bar i{display:block;height:100%;background:var(--brand);border-radius:999px}
.excard__miss{display:flex;flex-wrap:wrap;gap:4px;margin-top:2px}
.excard__miss span{font-size:10.5px;padding:1px 6px;border-radius:999px;
  background:rgba(47,125,87,.09);color:#2f5d49}
.exladder{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-top:8px}
.exstep{border:1px solid var(--line);border-radius:var(--radius-sm);padding:10px 12px;background:#fcfcfd}
.exstep h4{margin:0 0 3px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}
.exstep .v{font-size:13px;font-weight:700;line-height:1.35}
.exstep .m{font-size:11.5px;color:var(--muted);margin-top:4px;line-height:1.5}
.exstep.ok{border-color:rgba(47,125,87,.34);background:rgba(47,125,87,.05)}
.exstep.warn{border-color:rgba(196,132,42,.34);background:rgba(196,132,42,.06)}
.exstep.dim{opacity:.75}
pre.excode{margin:6px 0 0;padding:9px 11px;background:#12211a;color:#d7e6dc;border-radius:8px;
  font:11.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;max-height:72vh;overflow:auto;white-space:pre-wrap}
</style>
</head>
<body>
<div class="page-shell">
  <div class="main-view">
    <header class="hero">
      <div class="hero__copy">
        <p class="hero__eyebrow">MVE · Voyager × VLML</p>
        <h1 class="hero-title"><span class="hero-title__cat">🛰</span>Voyager 进步曲线</h1>
        <p class="hero__status" id="statusLine">加载中…</p>
        <div class="runctl">
          <span class="lbl">驾驶舱</span>
          <button class="btn btn-primary" id="runStart"
            title="自由学：它拿背包里的上下文记忆自己找下一件事，做完一格再找下一件，一直跑；点「■ 停止」才退。第一轮仍由人在「开始学」里促成。">▶ 启动 Voyager</button>
          <button class="btn btn-danger" id="runStop" disabled>■ 停止</button>
          <button class="btn btn-secondary" id="btnExport" title="把当前学习过程导出成一份 md">⬇ 导出 md</button>
          <!-- 对话页（2026-10-28）：OpenAI 兼容端点的零安装界面，判定口径与
               /api/coach 同一条 consume()，不新造第二把尺子。 -->
          <button class="btn btn-secondary" id="mcLearn" title="先跑 MC 思路学习（提炼域无关规划思维）并入库，再自动启动标准练习循环（正常 MVE 全流程）">🧠 启动 MC 思路学习 → MVE</button>
          <label class="runctl-ck" id="ckWrap"
                 title="同构原版 Voyager（voyager.py:326 主循环是 while True）：学习循环不带停，只有点停止 / 跑满上限才退。勾上 = 不停；去掉勾 = 只跑一轮看看。">
            <input type="checkbox" id="ckContinuous" checked>
            <span>不停（一直找下一件事 / 点停止才退）</span>
          </label>
          <span class="runstate"><i class="dot off" id="runDot"></i><span id="runState">未运行</span></span>
        </div>
        <!-- 背包（2026-11-18）：面板只服务一个工作区，不再有「场景」可选。
             口径照原版 curriculum.py:205 `Inventory (used/36): {inventory}` ——
             格 = 一次会话，内容物是跑出来的，不是预设清单。 -->
        <div class="scenebar" id="bagBar">
          <span class="lbl">背包</span>
          <span class="chip grey" id="bagChip">Inventory (0/36)</span>
          <span class="hint" id="bagHint">材料：加载中…</span>
        </div>
        <!-- 猫爪总开关（同构 neko）：一组能力一个开关，关掉该组「对模型不可见、
             也调不动」，拨回来即恢复（不删条目）。开关下面就是该组的条目，
             鼠标悬停可看。 -->
        <div class="scenebar" id="clawBar" style="gap:6px;flex-wrap:wrap">
          <span class="lbl">能力开关</span>
          <span id="clawBox" class="hint">加载中…</span>
        </div>
      </div>
      <div class="hero__controls">
        <div class="summary-pills" id="pills"></div>
      </div>
    </header>

    <nav class="workspace-nav" id="nav"></nav>
    <div class="workspace-stage" id="stage"><div class="empty">加载中…</div></div>
  </div>
</div>

<script>
const ESC = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

const TABS = [
  {id:'overview', icon:'📈', label:'概览',
   // 2026-11-14：概览 = 可点击的练习列表（同构助产士 Instructor · Exercises），
   // 进步口径是每道题的缺口序列，不再折一个总分。
   status:s=>{const ex=s.exercises||[];
     if(!ex.length) return '还没有练习';
     const done=ex.reduce((a,x)=>a+Number(x.completed||0),0);
     return (`${ex.length} 道 · ${done} 段完成`);}},
  {id:'practice', icon:'🎯', label:'练习',   status:s=>s.topic_id||'—'},
  {id:'import',   icon:'✍️', label:'开始学', status:s=>((s.exercises||[]).length+' 道已发布')},
  // 2026-10-23 用户否掉「外部任务」这一层：「没必要做外部 task，导入统一走人导入，
  // 相关度低就建新场景、绑新数据源」。它本来只是一条动作链（导入 → 学 → 重试），
  // 而导入/学习/重试在「人导入」和驾驶舱里都已经有入口 —— 再开一个页签就是重复。
  // 现在数据源边界只由**场景**决定（scene_profile 唯一定义），任务不再自成一套账。
  {id:'trace',    icon:'🧭', label:'轨迹',   status:s=>(s.trace_total||0)+' 轮'},
  {id:'skill',    icon:'🧠', label:'技能 · 因果', status:s=>(s.skill_stats&&s.skill_stats.skills||0)+' 条技能'},
  {id:'graph',    icon:'🕸', label:'知识图谱',
   status:s=>s.practice_scope&&s.practice_scope.active?('范围：'+s.practice_scope.label):'维度 → 工具'},
  {id:'voyager', icon:'🤖', label:'MC 真实学习',
   status:s=>(s.voyager&&s.voyager.running)?'学习中':((s.voyager&&s.voyager.completed_count)?('已学 '+(s.voyager.completed_count)+' 技能'):'空闲')},
  {id:'adaptive', icon:'🧩', label:'自适应练习',
   status:s=>(s.teaching&&s.teaching.target)?('目标难度'+s.teaching.target):'知识点图谱'},
  {id:'console',  icon:'🖥', label:'运行日志',
   status:s=>{const p=s.pilot||{}; return p.running?'运行中':(p.winding_down?'收尾中':'空闲');}},
  // 2026-11-18 用户：「人在面板上也需要一个 chat，时不时问现在 mve 的学习进度」。
  // 从 maieutic 抽：原版学生在 IDE chat 问自己的代码，教师位看进度 —— 这是教师位。
  // 零写账（不碰 phase2 账本，r_site 游程不被污染）、不判定（掌握度归消费侧）。
  {id:'pchat',    icon:'💬', label:'进度问答', status:s=>'只读 · 不记判定'},
];
let TAB = localStorage.getItem('mve_tab') || 'overview';

/* ---------------- 小工具 ---------------- */
function chips(items, cls){
  if(!items||!items.length) return '<span class="chip grey">无</span>';
  return items.map(i=>`<span class="chip ${cls||''}">${ESC(typeof i==='string'?i:i.label)}</span>`).join('');
}
function lineChart(pts, opts){
  const W=560,H=190,P={l:38,r:14,t:14,b:26};
  if(!pts.length) return '<div class="empty">无数据</div>';
  const iw=W-P.l-P.r, ih=H-P.t-P.b;
  // 纵轴刻度：默认 0..1 按百分比标；传 `opts.max` 时按**绝对数**标。
  //   学习曲线照原版 Voyager（curriculum.py:135）画的是 `len(completed_tasks)`
  //   —— 累计会做的题**有几道**，不是占比。
  const maxV = Number(opts.max) || 1;
  const fmtV = opts.fmt || (v=>Math.round(v*100)+'%');
  const X=i=>P.l+(pts.length===1?iw/2:iw*i/(pts.length-1));
  const Y=v=>P.t+ih*(1-Math.max(0,Math.min(1,v/maxV)));
  const grid=[0,.25,.5,.75,1].map(v=>
    `<line x1="${P.l}" y1="${Y(v*maxV)}" x2="${W-P.r}" y2="${Y(v*maxV)}" stroke="rgba(31,41,36,.08)"/>
     <text x="${P.l-8}" y="${Y(v*maxV)+4}" font-size="10" fill="#9ca3af" text-anchor="end">${fmtV(v*maxV)}</text>`).join('');
  const path=pts.map((p,i)=>`${i?'L':'M'}${X(i).toFixed(1)},${Y(p.y).toFixed(1)}`).join(' ');
  const dots=pts.map((p,i)=>
    `<circle cx="${X(i).toFixed(1)}" cy="${Y(p.y).toFixed(1)}" r="3.2" fill="${opts.color}">
     <title>第${p.x}次 · ${p.label}</title></circle>`).join('');
  const xlab=pts.map((p,i)=>
    `<text x="${X(i).toFixed(1)}" y="${H-8}" font-size="10" fill="#9ca3af" text-anchor="middle">${p.x}</text>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px">
    ${grid}
    <path d="${path}" fill="none" stroke="${opts.color}" stroke-width="2.2"
          stroke-dasharray="${opts.dash||''}" stroke-linejoin="round"/>
    ${dots}${xlab}</svg>`;
}
/* 判据区：布尔轨迹（绿=达成，红=未达成）。
   判据层用 done（Voyager 尺子：任务完成=跑通+拿到数+没翻车，不数覆盖点）；
   VLML 的 success（全评分点达成）是蓝灰观测线，不驱动学习。 */
function stepChart(pts, opts){
  const W=560,H=120,P={l:38,r:14,t:14,b:26};
  if(!pts.length) return '<div class="empty">无数据</div>';
  const iw=W-P.l-P.r, ih=H-P.t-P.b;
  const X=i=>P.l+(pts.length===1?iw/2:iw*i/(pts.length-1));
  const Y=v=>P.t+(v?0:ih);              // true 在顶，false 在底
  const cells=pts.map((p,i)=>{
    const x0=X(i), x1=i<pts.length-1?X(i+1):X(i)+10;
    return `<rect x="${x0.toFixed(1)}" y="${Y(p.v).toFixed(1)}" width="${(x1-x0).toFixed(1)}" height="${ih}" fill="${p.v?opts.yes:opts.no}" opacity="0.9">
      <title>第${p.x}次 · ${p.v?'达成':'未达成'}</title></rect>`;
  }).join('');
  const xlab=pts.map((p,i)=>
    `<text x="${X(i).toFixed(1)}" y="${H-8}" font-size="10" fill="#9ca3af" text-anchor="middle">${p.x}</text>`).join('');
  const labels=`<text x="${P.l+4}" y="${P.t+10}" font-size="10" fill="${opts.yes}">✓ 达成</text>
               <text x="${P.l+4}" y="${P.t+ih-4}" font-size="10" fill="${opts.no}">✗ 未达成</text>`;
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px">${cells}${labels}${xlab}</svg>`;
}

/* ---------------- hero ---------------- */
function renderHero(s){
  const pills=[];
  if(s.has_data){
    const tr=(s.exam_curve||{}).transfer;
    // 2026-11-18 换账本：判据区读 learn_trace（MVE 现账），不再读 run_log
    // （原版练习线的 success / 行数 —— 里程碑点亮了它还记 failed）。
    const tk=s.trace_kpi||{}, last=tk.last||null;
    const lastOk = !last
      ? '<span class="chip grey" title="learn_trace 还没有行 —— 起点学习循环后落账">还没有轮次</span>'
      : (last.lit
         ? '<span class="chip ok" title="里程碑点亮（真取到数 + 维度闭合）">达成 · 已点亮</span>'
         : (last.ms_achieved
            ? '<span class="chip warn" title="维度全闭合，证据门/点亮没过">维度已闭合</span>'
            : `<span class="chip bad" title="${ESC((last.why||last.status||'').slice(0,160))}">未达成</span>`));
    pills.push(['当前题判据', lastOk]);
    // 有迁移考数据就显示原指标；没有（本场景没考过）时该位显示现账的真取到率。
    if(tr){
      pills.push(['迁移成功率', tr.ok_count+' / '+tr.count]);
    }else{
      const ent=+tk.entered||0, fet=+tk.fetched||0;
      pills.push(['真取到率', ent
        ? `<span title="learn_trace：真取到 ${fet} 轮 / 进执行 ${ent} 轮">${Math.round(100*fet/ent)}% <span style="opacity:.6;font-size:11px">(${fet}/${ent})</span></span>`
        : '<span class="chip dim" title="还没有进入执行的轮次">—</span>']);
    }
    pills.push(['累计轮次', `<span title="learn_trace 行数（MVE 现账，一次 learn() 一行）">${+tk.rounds||0}</span>`]);
    pills.push(['技能库', (s.skill_stats&&s.skill_stats.skills||0)+' 条 / 膨胀率 '+(s.skill_stats&&s.skill_stats.bloat||1)]);
    // ⚠️ 「掌握度变化」「状态」「拦下编造」三块已下架（2026-10-13 按后端索引清理）。
    //    判据：线上 36 条 run_log 全部来自新后端（judge=db_evidence），这三个读的
    //    mastery / status_label / flags / hallucination_count **一条都没有** ——
    //    它们只由已冻结的旧后端 mve/run_mve.py 写入。
    //    留着它们，面板上就永远挂着一个「掌握度 None」和一排空 flags，
    //    看着像"它没学会"，其实是无人在生产的字段。
  }
  pills.push(['数据源', `<span class="chip ${s.env.state==='ready'?'ok':'bad'}">${s.env.state==='ready'?'VLML 就绪':'VLML 不可用'}</span>`]);
  document.getElementById('pills').innerHTML =
    pills.map(([l,v])=>`<div class="summary-pill"><span>${ESC(l)}</span><strong>${v}</strong></div>`).join('');

  const p=s.pilot||{};
  const lp=p.loop||{};
  const dot=document.getElementById('runDot');
  // 「已停止」但循环还在收尾：pilot 收到停止信号就退，loop.py 会把手头这道题
  // 跑完再退（60～90 秒）。这段时间数据在涨，人必须看得见。
  const winding = !!p.winding_down;
  dot.className='dot '+((p.running||winding)?(winding?'warn':'on'):'off');
  let runTxt;
  if(winding){
    runTxt = `收尾中 · 第 ${lp.round||'?'} 轮跑完就停 · ${ESC(p.why||'')}`;
  }else if(p.running){
    const kind = (p.rounds===0||lp.continuous) ? '不停（同构原版 while True）'
                                               : ('上限 '+(p.rounds||1)+' 轮');
    runTxt = `运行中 · ${kind} · 已跑 ${lp.round||p.iterations||0} 轮`;
  }else{
    runTxt = p.why||'未运行';
  }
  document.getElementById('runState').innerHTML = runTxt;
  document.getElementById('runStart').disabled = !!(p.running||winding);
  document.getElementById('runStop').disabled  = !(p.running||winding);

  // 「不停」开关：有练习范围时锁掉 —— 范围会把题钉死成一题，
  // 不停只会把同一道题反复跑（进度永远 0/1），所以那时只能跑一轮。
  const ck=document.getElementById('ckContinuous'), ckw=document.getElementById('ckWrap');
  if(ck){
    const scoped=!!(window.__scope&&window.__scope.active);
    ck.disabled = p.running||winding||scoped;
    if(scoped){ ck.checked=false; }
    ckw.className = 'runctl-ck'+(ck.disabled?' off':'');
    ckw.title = scoped
      ? '已设练习范围：题被钉成一道，不停只会反复跑同一题（进度永远 0/1）。清掉范围才能不停。'
      : '同构原版 Voyager：主循环 while True，学习不带停，只有点停止 / 跑满上限才退。';
  }

  window.__scope = {active:false};   // 练习范围已退役（2026-11-18）
  document.getElementById('statusLine').textContent = s.has_data
    ? (s.reading_text||'')
    : (s.hint||'还没有任何运行记录');

}

function renderNav(s){
  document.getElementById('nav').innerHTML = TABS.map(t=>`
    <button class="workspace-tab" role="tab" data-tab="${t.id}" aria-selected="${TAB===t.id}">
      <span class="workspace-tab__icon">${t.icon}</span>
      <span class="workspace-tab__label">${t.label}</span>
      <span class="workspace-tab__status">${ESC(t.status(s))}</span>
    </button>`).join('');
}

/* ---------------- 各分区 ---------------- */
/* 真实水平曲线：撤支架考核（学习曲线该看的那一列） */
/* 1→n 诚实披露（examCard / pPractice / pAdaptive 三处复用，不各写一份）：
   exam_log（摸底考核）/ topic_graph（教学图谱）/ planner（出题器）都不带
   场景字段 → 这些区块是**全局**数据，不随场景切换过滤。不假装隔离。 */
function globalDataNote(s, what){
  if(!((s.scene||{}).id && s.scene.id!=='A')) return '';
  return '';   // 练习范围/场景隔离已退役（2026-11-18）：不再有跨场景数据的说明
}

function examCard(s){
  const e=s.exam_curve||{};
  const p=s.pilot||{};
  const busy=!!p.running;
  const runningJob=busy?(p.job_label||p.job||'任务'):'';
  const sceneNote=globalDataNote(s,'摸底/考核走 exam_log');
  const ctl=`<div class="runctl">
      <button class="btn btn-primary" id="btnPlacement" ${busy?'disabled':''}>摸底（全库撤图谱考一遍）</button>
      <button class="btn btn-secondary" id="btnFinal" ${busy?'disabled':''}>结业考（带技能库再考一遍）</button>
      <span class="lbl">单元</span>
      <select id="learnUnits">${[2,4,6,8].map(n=>`<option value="${n}" ${n===4?'selected':''}>${n}</option>`).join('')}</select>
      <span class="lbl">迁移对照每</span>
      <select id="learnTransfer">${[0,2,3,4].map(n=>`<option value="${n}" ${n===3?'selected':''}>${n===0?'不考':n+' 单元'}</option>`).join('')}</select>
      <button class="btn btn-secondary" id="btnLearn" ${busy?'disabled':''}>跑学习单元</button>
      <button class="btn btn-danger" id="btnExamStop" ${busy?'':'disabled'}>■ 停止</button>
    </div>
    ${busy?`<div class="note info">正在跑：<b>${ESC(runningJob)}</b>
      ${p.iterations?`（第 ${p.iterations} 次）`:''} —— 进度看「运行日志」页。
      摸底 8 道题约 7 分钟，期间刷新页面不会丢。</div>`:''}`;
  if(!e.has_data) return `<div class="panel"><div class="panel__head"><h2>真实水平曲线 · 撤支架考核</h2>
      <span class="hint">练习那条线是带知识图谱跑的，量的是支架不是水平</span></div>
    <div class="empty">${ESC(e.hint||'还没有裸考记录')}</div>
    ${sceneNote}
    <div class="note">点「摸底」开始：把全库每道题**撤掉知识图谱**考一遍（约 7 分钟），
      得出零基础的真实水平。它要求技能库为空 —— 带着技能库考出来的是
      "练过之后的水平"，不是起点。</div>
    ${ctl}</div>`;
  const rowsHtml=(e.tracks||[]).map(t=>{
    const after=t.after.length
      ? t.after.map(a=>`<span class="chip ${a.cov>=1?'ok':(a.cov>0?'warn':'bad')}">${ESC(a.pct)}</span>`).join(' ')
      : '<span class="hint">还没重考过</span>';
    const d=t.delta_pp===null?'':(t.delta_pp>0
      ? `<span class="chip ok">+${t.delta_pp}pp</span>`
      : (t.delta_pp<0?`<span class="chip bad">${t.delta_pp}pp</span>`:'<span class="chip grey">持平</span>'));
    return `<tr><td class="mono">${ESC(t.topic_id)}${t.exhausted?' <span class="chip grey">已毕业让位</span>':''}</td>
      <td>${ESC(t.baseline_pct)} <span class="hint">${ESC(t.level)}</span></td>
      <td>${after}</td>
      <td>${t.final_pct?`<span class="chip ${t.final>=1?'ok':(t.final>0?'warn':'bad')}">${ESC(t.final_pct)}</span>`:'<span class="hint">—</span>'}</td>
      <td>${d}</td></tr>`;
  }).join('');
  const tr=e.transfer;
  const trHtml=tr?`<div class="note"><b>迁移对照（${tr.count} 次未练题考核）：</b>${ESC((tr.seq||[]).join(' → '))} ——
    ${tr.rose?'涨了，说明是真学会而不只是记住了这道题。':'<b>没涨</b>：上升的是「记住了这道题的解法」，不是可迁移的能力。'}</div>`:'';
  const fin=e.final;
  const finHtml=fin?`<div class="note"><b>结业考（${fin.count} 道${fin.covered_all?'·全库':'·部分'}）：</b>
    摸底均值 ${fin.avg_baseline}% → 结业均值 ${fin.avg}%
    （${fin.delta_pp>0?'+':''}${fin.delta_pp}pp，≥80% 的 ${fin.mastered} 道）
    —— 这是<b>带着技能库</b>撤图谱再考一遍的结果，和摸底同一个分母，可直接比。</div>`:'';
  return `<div class="panel"><div class="panel__head"><h2>真实水平曲线 · 撤支架考核</h2>
      <span class="hint">练习给图谱 / 考核撤图谱 —— 按题分行，不首尾相连</span></div>
    ${sceneNote}
    <div class="kpis">
      <div class="kpi"><div class="label">摸底均值</div><div class="value">${e.avg_baseline}%</div></div>
      <div class="kpi"><div class="label">最新均值</div><div class="value">${e.avg_latest}%</div></div>
      <div class="kpi"><div class="label">掌握 ≥80%（摸底→现在）</div><div class="value">${e.mastered} → ${e.mastered_now}</div></div>
      <div class="kpi"><div class="label">重考过 / 涨了</div><div class="value">${e.retested} / ${e.rose}</div></div>
      <div class="kpi"><div class="label">平均涨幅</div><div class="value">${e.avg_delta_pp===null?'—':(e.avg_delta_pp>0?'+':'')+e.avg_delta_pp+'pp'}</div></div>
    </div>
    <table><thead><tr><th>题</th><th>摸底（裸考）</th><th>练后重考</th><th>结业考</th><th>Δ</th></tr></thead>
      <tbody>${rowsHtml}</tbody></table>
    ${finHtml}
    ${trHtml}
    ${ctl}
    <div class="legend">摸底 = 清库后第一次全库裸考（基线，不会被顶掉）；重考 = 练完这一题后撤掉图谱再考；
      结业考 = <b>带着现有技能库</b>把全库再考一遍，和摸底同一个分母，可直接比。
      出题器按「最弱优先」选题，读的是摸底那一列。</div></div>`;
}

/* ================= 练习（Exercise）：同构助产士 Instructor · Exercises ========
   2026-11-14 用户：「概览应该是可以点击的 exercise」。
   列表页每张卡 = 一道题（跨全部会话汇总），点开才是这道题的账。
   数据全来自 s.exercises（panel_data._exercise_index 现算）——
   前端不另拼口径，卡上的数就是账上的数。 */
let EXSORT = localStorage.getItem('mve_exsort') || 'recent';

function exUnit(e){
  return ESC(e.unit || '本局');
}
function exDivLabel(e){
  if(!e.div_total) return 'none flagged';
  const c = e.div_counts || {};
  const parts = ['drift','revision','bug'].filter(k=>c[k]).map(k=>k+' '+c[k]);
  return parts.length ? parts.join(' · ') : (e.div_total + ' 条');
}
function exIterLabel(e){
  // 原版卡面：没有完成段就写「— no completions」，不要只画一条空横线
  if(e.iter_median===null || e.iter_median===undefined)
    return (e.sessions ? '— no completions' : '—');
  return e.iter_median + ' median';
}
function exCard(e){
  const done = Number(e.completed||0), all = Number(e.sessions||0), pct = Number(e.pct||0);
  const miss = (e.first_missed||[]).slice(0,3)
    .map(x=>`<span>${ESC(x.id)}${x.n>1?' ×'+x.n:''}</span>`).join('');
  return `<button class="excard" onclick="openExercise('${ESC(e.qid)}')">
    <div class="excard__unit">${exUnit(e)}${e.reqs>1?` · ${e.reqs} 次发布`:''}</div>
    <div class="excard__title">${ESC(e.title||e.question)} →</div>
    <div class="excard__row"><span>Sessions</span><b>${done} / ${all}</b></div>
    ${all?`<div class="excard__bar"><i style="width:${pct}%"></i></div>
      <div class="excard__row" style="border:none;padding-top:0"><span>${pct}% complete</span><span></span></div>`
      :`<div class="excard__row" style="border:none;padding-top:0"><span class="hint">no sessions yet</span><span></span></div>`}
    <div class="excard__row"><span>Spec iterations</span><b>${exIterLabel(e)}${
      (e.iter_max!==null && e.iter_max!==undefined) ? ` / max ${e.iter_max}` : ''}</b></div>
    <div class="excard__row"><span>Divergences</span><b>${ESC(exDivLabel(e))}</b></div>
    ${miss?`<div class="excard__miss">${miss}</div>`:''}
  </button>`;
}
function exList(s){
  const ex = (s.exercises||[]).slice();
  const cur = (s.scene&&s.scene.id)||'A';
  if(!ex.length){
    return `<div class="panel">
      <div class="panel__head"><h2>练习 · Exercises</h2>
        <span class="hint">还没有练习</span></div>
      <div class="empty">还没有练习。助产士的规矩：先在「人导入」发布一道题
        （写题面 → 生成设问骨架 → 发布），会话才会在它下面累积成一张卡。</div>
      <div class="note info">「练习」= 一个固定的题目系统（原版一道 Exercise）；
        一次把题做一遍 = 一段会话。进步不是分数，是<b>同一道题里缺口序列怎么收敛</b>
        （第几轮把缺口问空、重跑一遍少用几轮）。</div>
    </div>`;
  }
  if(EXSORT==='az') ex.sort((a,b)=>String(a.title||'').localeCompare(String(b.title||''),'zh'));
  else if(EXSORT==='unit') ex.sort((a,b)=>String(a.unit||a.root_scene||'').localeCompare(String(b.unit||b.root_scene||''),'zh'));
  else ex.sort((a,b)=>Number(b.sessions||0)-Number(a.sessions||0));
  const total = ex.length;
  const started = ex.reduce((a,x)=>a+Number(x.sessions||0),0);
  const done = ex.reduce((a,x)=>a+Number(x.completed||0),0);
  const btns = [['recent','最多尝试'],['unit','按单元'],['az','A → Z']].map(([k,l])=>
    `<button class="btn ${EXSORT===k?'btn-primary':'btn-secondary'}" style="padding:5px 11px;font-size:12px"
      onclick="switchExSort('${k}')">${l}</button>`).join(' ');
  return `<div class="panel">
    <div class="panel__head"><h2>练习 · Exercises</h2>
      <span class="hint">每张卡一道题，跨全部会话汇总；点开看它的账</span></div>
    <div class="imp-row" style="margin-top:0">
      <span class="chip">${total} 道练习</span>
      <span class="chip">${started} 段会话</span>
      <span class="chip ${done?'ok':''}">${done} 段完成</span>
      <span style="margin-left:auto">${btns}</span>
    </div>
    <div class="exgrid">${ex.map(exCard).join('')}</div>
    <div class="legend">Sessions = 这道题跑过几段会话（一段 = 从 Phase1 追问开始的一次完整重跑；
      完成 = 该段 spec 冻结并交过 finalCode）。Spec iterations = 完成段用了几轮追问（中位数 / 最大）。
      Divergences = Phase3 对账出来的分歧（drift 说法与实现不一致 / revision 换了做法但成立 / bug 没做到）。
      <b>这张表不打分</b> —— 原版全库没有 score / grade / mastery，进步看的是缺口序列的收敛。</div>
  </div>`;
}

function switchExSort(k){
  EXSORT = k; localStorage.setItem('mve_exsort',k); rerender();
}
function openExercise(qid){
  window.__exOpen = qid; rerender();
  const st=document.getElementById('stage'); if(st) st.scrollIntoView({block:'start'});
}
function closeExercise(){
  window.__exOpen = ''; rerender();
}
function rerender(){
  const st=document.getElementById('stage');
  if(st && window.__state) st.innerHTML = pOverview(window.__state);
}

/* 阶段阶梯：P1 spec → P2 finalCode → P3 对账 → P4 里程碑。
   这就是「phase1 到之后的 phase 的账」—— 原版一条会话记录里
   phase1/phase2/phase3 连在一起（`src_lib_opus_schemas.ts`），
   MVE 之前只有 P1 和 P3 落了盘，P2 活在内存里，进程一重启就断。 */
function exLadder(e){
  const p2 = e.phase2 || {}, p3 = e.phase3 || {};
  const q = e.quad || {};
  const p1runs = (e.runs||[]).filter(r=>r.rounds);
  const rounds = p1runs.map(r=>Number(r.rounds||0));
  const firstPass = p1runs.map(r=>r.first_pass_round).filter(x=>x!==null && x!==undefined);
  const p1ok = (e.runs||[]).some(r=>r.passed);
  const p1cls = p1ok ? 'ok' : (e.sessions ? 'warn' : 'dim');
  const p2sub = !!p2.submitted, p2ver = !!p2.verified;
  const p2cls = !e.phase2_recorded ? 'dim' : (p2ver ? 'ok' : (p2sub ? 'warn' : 'dim'));
  const dvn = Number(e.div_total||0);
  const p3cls = !dvn ? (e.phase2_recorded ? 'ok' : 'dim') : 'warn';
  const done = !!e.milestone_achieved;
  return `<div class="exladder">
    <div class="exstep ${p1cls}">
      <h4>Phase 1 · spec 追问</h4>
      <div class="v">${e.sessions?`${e.completed}/${e.sessions} 段通过`:'还没有会话'}</div>
      <div class="m">${rounds.length?`轮数中位 ${e.iter_median===null?'—':e.iter_median} / 最大 ${e.iter_max===null?'—':e.iter_max}`:'无追问轨迹'}
        ${firstPass.length?` · 首过轮 ${firstPass[firstPass.length-1]}`:''}</div>
      <div class="m">已答到 ${(e.phase1&&e.phase1.addressed||[]).length} / ${(e.dims||[]).length} 个维度${
        (e.phase1&&e.phase1.open_now||[]).length?` · 末轮仍开 ${e.phase1.open_now.length} 个`:''}</div>
    </div>
    <div class="exstep ${p2cls}">
      <h4>Phase 2 · finalCode</h4>
      <div class="v">${!e.phase2_recorded ? '未落盘' : (p2ver?'已交 · 与 spec 一致':(p2sub?'已交 · 仍有对不上的':'未交'))}</div>
      <div class="m">${e.phase2_recorded
        ? `${p2.code_chars||0} 字符 · 改 ${p2.rounds||0} 轮 · 分歧 ${p2.divergences||0} 条`
        : '这段会话跑在 2026-11-14 之前 —— 那时 Phase2 只活在内存里，没落盘'}</div>
    </div>
    <div class="exstep ${p3cls}">
      <h4>Phase 3 · intent-diff</h4>
      <div class="v">${dvn?`${dvn} 条分歧`:'none flagged'}</div>
      <div class="m">${dvn?ESC(exDivLabel(e)):(p3.skipped&&p3.skipped.length?ESC(String(p3.skipped[0]).slice(0,60)):'无分歧记录')}${
        p3.revision_choice?` · 修订 ${ESC(p3.revision_choice)}`:''}</div>
    </div>
    <div class="exstep ${done?'ok':'dim'}">
      <h4>Phase 4 · 归档</h4>
      <div class="v">${done?'里程碑已解锁':'未解锁'}</div>
      <div class="m">${ESC(e.milestone||'（这道题没有里程碑名）')}</div>
    </div>
  </div>`;
}

/* Voyager 的口头作答 —— 回答"在哪看得见 Voyager 说的话"。
   两种形态分两处：
     · 学习闭环交的是**程序**（phase2.finalCode，上面那块）；
     · 旧消费线答的是**人话**，只落在 coach_log.jsonl 的 answer。
   2026-11-14 把「交给 Voyager 回答」的按钮解绑后，第二种没有任何入口，
   它还在盘上（20 条有 answer）但没人读 —— 这里补回来，且明确标"不做账"：
   进步看缺口序列与 Phase3 分歧，不看它答得对不对。 */
function exAnswers(e){
  const ans=(e.answers||[]);
  if(!ans.length){
    return `<div class="empty">${e.phase2_recorded
      ? '这道题只有程序作答（见上面 Phase 2），没有旧消费线的口头作答。'
      : '这道题的作答没有落盘 —— 会话跑在 Phase2 落盘之前，旧消费线也没答过这句。'}</div>`;
  }
  return ans.map((a,i)=>{
    const cave=(a.caveats||[]);
    return `<div style="border:1px solid rgba(31,41,36,.12);border-radius:8px;padding:10px 12px;margin:8px 0;background:#fcfcfd">
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
        <span class="chip grey">第 ${ans.length-i} 条</span>
        ${a.ts?`<span class="chip grey">${ESC(a.ts)}</span>`:''}
        ${a.mode?`<span class="chip grey">${ESC(a.mode)}</span>`:''}
        ${(a.rows!==null&&a.rows!==undefined)?`<span class="chip grey">取数 ${ESC(a.rows)} 行</span>`:''}
        ${a.import_id?`<span class="chip grey">${ESC(a.import_id)}</span>`:''}
      </div>
      <div style="margin-top:6px;font-size:13px;line-height:1.65;white-space:pre-wrap">${ESC(a.answer)}</div>
      ${a.narrative?`<div class="legend" style="margin-top:6px;white-space:pre-wrap">叙述层：${ESC(a.narrative)}</div>`:''}
      ${cave.length?`<div class="legend" style="margin-top:4px">它自己标的不确定：${cave.map(x=>ESC(x)).join('；')}</div>`:''}
      ${a.sql?`<details style="margin-top:6px"><summary class="hint" style="cursor:pointer">它跑的 SQL / RESULT</summary>
        <pre class="excode">${ESC(a.sql)}</pre></details>`:''}
    </div>`;
  }).join('');
}

function exQuadBox(en,zh,items){
  const body=(items&&items.length)
    ? items.map(x=>`<div style="margin:4px 0;font-size:12.5px;line-height:1.5">${ESC(x)}</div>`).join('')
    : '<div style="margin:4px 0;font-size:12.5px;color:var(--muted)">（暂无）</div>';
  return `<div style="border:1px solid rgba(31,41,36,.12);border-radius:8px;padding:10px 12px;background:rgba(255,255,255,.5)">
    <div style="font-size:10.5px;letter-spacing:.07em;color:var(--muted);text-transform:uppercase">${ESC(en)}</div>
    <div style="margin-top:1px;font-size:12px;font-weight:600">${ESC(zh)}</div>
    <div style="margin-top:5px">${body}</div></div>`;
}

/* 进步 · 同一道题的各遍 —— 用户 2026-11-15 那条线：
   出题 → 学习 → 做出来 → 自由学习 → 做以前出过的题 → 做得更好 → 指标更好。

   「做得更好」不是分数，是同一道题里三个数一起看：
     轮数       问几轮才把缺口问空
     缺口峰值   一上来还差几个维度（第 1 轮的 opens）
     首过轮     第几轮第一次全部答到
   三个数一起降才是真变强；只有一个降、别的涨，通常是这一遍恰好撞对了路子。

   ⚠️ 为什么必须配「清背包」：不做这件事的重跑，跨段会继承 phase1.addressed
   （原版 src_lib_sessions.ts:264-266 的并集语义），第一轮就报"全答到了"，
   Δ 恒为 0 —— 看起来"每次都很快"，其实什么都没测到。 */
function exProgress(e){
  const runs = (e.runs||[]);
  const redos = (e.redos||[]);
  if(runs.length < 2){
    return `<div class="empty">这只有 1 遍，还没有"更好"可比。做完这一遍点上面的
      <b>↻ 再做一遍</b>：它会清掉背包（已答到的维度 / 上次跑通的代码 / 追问历史 /
      里程碑标记），账本留在册 —— 第 2 遍跑完，这里就是两遍的对照。
      <div style="margin-top:6px;font-size:12px">不清背包的重跑不算数：跨段继承
      「已经答到过哪些维度」，第一轮就报全答到，Δ 恒为 0。</div></div>`;
  }
  const peak = r => Math.max(0, ...((r.opens||[]).length?(r.opens||[0]):[0]));
  const rows = runs.map((r,i)=>{
    const rd = redos[i-1];
    const nsk = (r.skills||[]).length;
    return `<tr>
      <td class="num">第 ${i+1} 遍${rd?`<div class="hint">重做 · 清了 ${(rd.cleared||[]).length} 项</div>`:''}</td>
      <td class="num">${r.rounds}</td>
      <td class="num">${peak(r)}</td>
      <td class="num">${r.first_pass_round==null?'—':r.first_pass_round}</td>
      <td class="mono">${(r.opens||[]).join('→')||'—'}</td>
      <td>${r.skills_known
        ? (nsk?`<span class="chip">${nsk} 条配方</span>`
               :'<span class="chip grey">0 条配方</span>')
        : '<span class="chip grey">起点未记</span>'}${r.draft_from?'<span class="chip warn">沿用上一版</span>':''}</td>
      <td>${r.completed?'<span class="chip ok">做出来了</span>'
        :(r.passed?'<span class="chip warn">spec 冻结</span>'
        :'<span class="chip bad">没做完</span>')}</td>
    </tr>`;
  }).join('');
  const a = runs[runs.length-2], b = runs[runs.length-1];
  const dr = Number(b.rounds||0)-Number(a.rounds||0);
  const dp = peak(b)-peak(a);
  const df = (a.first_pass_round==null||b.first_pass_round==null)
    ? null : Number(b.first_pass_round)-Number(a.first_pass_round);
  const chip = (v,label)=>{
    if(v===null||v===undefined) return `<span class="chip grey">${label} 无对比</span>`;
    if(v===0) return `<span class="chip grey">${label} 持平</span>`;
    return `<span class="chip ${v<0?'ok':'bad'}">${label} ${v>0?'+':''}${v}</span>`;
  };
  return `<table><thead><tr><th>遍</th><th>轮数</th><th>缺口峰值</th><th>首过轮</th>
      <th>逐轮剩余缺口</th><th>起点</th><th>结果</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="imp-row" style="margin-top:8px">
      <span class="chip grey">第 ${runs.length-1} 遍 → 第 ${runs.length} 遍</span>
      ${chip(dr,'轮数')}${chip(dp,'缺口峰值')}${chip(df,'首过轮')}
    </div>
    <div class="legend">三个数都是<b>越小越好</b>。缺口峰值 = 一上来还差几个维度，
      首过轮 = 第几轮第一次全部答到。三个一起降才是真变强 ——
      只有一个降、别的涨，多半是这一遍恰好撞对了路子。
      <div style="margin-top:5px"><b>起点</b>：第 1 遍库里没有本题配方、也没有上一版，
      它就是<b>基线</b>；后面各遍带几条配方，就是这一遍能借到多少旧经验。重做不再沿用
      上一版的代码（那是在自己的成品上改两行，量到的是抄写），所以"配方数"是各遍之间
      唯一该保留的差别。</div></div>`;
}

/* 「再做一遍」：账本留在册，背包清成没学过。
   后端 /api/learn/start 带 redo:true → 先 reset_practice_state 再开跑
   （见 dashboard.py 该分支的注释：必须放在 start_learning **之前**）。 */
async function redoExercise(){
  const key = window.__exOpen || '';
  const ex = (window.__state && window.__state.exercises) || [];
  const e = ex.find(x=>x.qid===key) || ex.find(x=>(x.req_ids||[]).includes(key));
  if(!e) return;
  if(!e.sessions){
    alert('这道题还没有会话 —— 先用「发布并开始学习」跑第一遍，再来重做。');
    return;
  }
  const n = (e.runs||[]).length + 1;
  // req_id 与 qid 不是一个东西：qid = `{req_id}_{dbhash8}`。拿 qid 当 req_id
  // 传给后端会拼出另一个不存在的缓存文件名 —— reset 返回 missing、账却看着正常，
  // 于是"清了背包"是假的（panel_data._exercise_index 另给了干净的 req_id）。
  const rid = e.req_id || String(e.qid||'').replace(/_[0-9a-f]{8}$/,'');
  if(!confirm('再做一遍（第 '+n+' 遍）？\n\n'+
      '账本保留：前 '+(n-1)+' 遍的轮数、缺口、分歧、代码都在。\n'+
      '背包清零：已答到的维度、上次跑通的代码、追问历史、里程碑标记。\n'+
      '底稿清零：这一遍不从上一版的程序改起，从头写。\n'+
      '技能保留：库里已有的配方照旧可检索 —— 第 1 遍是 0 条配方，逐遍往上加。\n\n'+
      '这样它才会真再解一遍，而不是靠上一遍的记忆直接过。')) return;
  const r = await fetch('/api/learn/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({question:e.question, req_id:rid, redo:true, round:n,
      title:e.title, unit:e.unit, language:e.language})}).then(x=>x.json())
    .catch(err=>({error:String(err)}));
  if(r && (r.ok===false || r.error)){
    alert('没能开始：'+(r.error||''));
    return;
  }
  alert('第 '+n+' 遍已受理 —— 背包已清成没学过，学习在后台跑。跑完回这道题看对比。');
  setTimeout(load, 1500);
}

function exDetail(s,e){
  const q = e.quad || {}, p2 = e.phase2 || {}, p3 = e.phase3 || {};
  const pts = (e.points||[]).map(p=>({x:p.x,y:p.y,label:`第 ${p.x} 轮 · 剩余缺口 ${p.y}`}));
  const maxV = Math.max(1, ...(e.points||[]).map(p=>p.y));
  const runRows = (e.runs||[]).map(r=>{
    const seq = (r.opens||[]).join('→') || '—';
    const p2r = r.phase2||{};
    const p2txt = p2r.submitted ? (p2r.verified?'已交·一致':'已交·有分歧') : '—';
    return `<tr>
      <td class="num">第 ${r.run} 段${r.req?`<div class="hint">${ESC(String(r.req).slice(0,10))}</div>`:''}</td>
      <td class="num">${r.rounds}</td>
      <td class="mono">${seq}</td>
      <td class="num">${r.first_pass_round==null?'—':r.first_pass_round}</td>
      <td>${ESC(p2txt)}</td>
      <td>${r.passed?'<span class="chip ok">spec 冻结</span>':'<span class="chip bad">未通过</span>'}</td>
    </tr>`;
  }).join('');
  const trace = (e.phase1&&e.phase1.rounds||[]).map(x=>{
    const qs=(x.questions||[]).map(t=>`<div style="margin:2px 0">→ ${ESC(t)}</div>`).join('');
    const gaps=(x.gaps||[]).map(g=>`<span class="chip warn">${ESC(g)}</span>`).join(' ');
    const addr=(x.addressed||[]).map(g=>`<span class="chip ok">${ESC(g)}</span>`).join(' ');
    return `<div style="border-left:2px solid ${x.passed?'rgba(47,125,87,.45)':'rgba(31,41,36,.14)'};padding:2px 0 6px 10px;margin:6px 0">
      <div style="font-size:11.5px;color:var(--muted)">第 ${x.run} 段 · 第 ${x.round} 轮 ${x.passed?'· 通过':''}</div>
      ${qs||'<div style="font-size:12px;color:var(--muted)">（这一轮没提新问题）</div>'}
      ${gaps?`<div style="margin-top:3px">仍开：${gaps}</div>`:''}
      ${addr?`<div style="margin-top:3px">已答到（累计并集）：${addr}</div>`:''}
      ${x.spec?`<details style="margin-top:5px"><summary class="hint" style="cursor:pointer">
        它这一轮交的（${x.spec.length} 字符）</summary>
        <pre class="excode">${ESC(String(x.spec).slice(0,4000))}</pre></details>`
        :'<div class="hint" style="margin-top:3px">这一轮没有留下作答正文（spec_text 为空）</div>'}
    </div>`;
  }).join('');
  const dv = (p3.divergences||[]).map(d=>`<div style="border:1px solid rgba(31,41,36,.12);border-radius:8px;padding:9px 11px;margin:6px 0;background:#fcfcfd">
      <div><span class="chip ${d.cls==='bug'?'bad':(d.cls==='revision'?'warn':'grey')}">${ESC(d.cls||'未判')}</span>
        <span class="mono" style="font-size:12px">${ESC(d.id)}</span>
        ${d.alignment?`<span class="chip grey">对齐 ${ESC(d.alignment)}</span>`:''}</div>
      <div style="margin-top:4px;font-size:12.5px">${ESC(d.question||'')}</div>
      ${d.spec?`<div class="hint" style="margin-top:3px">spec：${ESC(d.spec)}</div>`:''}
      ${d.code?`<div class="hint">code：${ESC(d.code)}</div>`:''}
    </div>`).join('');
  const intended = (e.expected_drifts||[]).map(x=>
    `<span class="chip grey">${ESC(x.category)}：${ESC(String(x.pattern).slice(0,28))}</span>`).join(' ');
  const dims = (e.dims||[]).map(d=>`<span class="chip grey">${ESC(d)}</span>`).join(' ');
  return `<div class="panel">
    <div class="imp-row" style="margin-top:0">
      <button class="btn btn-secondary" onclick="closeExercise()" style="padding:5px 11px;font-size:12px">← 练习列表</button>
      <button class="btn btn-primary" onclick="redoExercise()" style="padding:5px 11px;font-size:12px"
        title="账本留在册，背包清成没学过（已答到的维度 / 上次的代码 / 追问历史 / 里程碑标记）—— 让它真再解一遍">↻ 再做一遍</button>
      <span class="chip grey">${exUnit(e)}</span>
      ${e.language?`<span class="chip grey">${ESC(e.language)}</span>`:''}
      ${e.reqs>1?`<span class="chip grey">${e.reqs} 次发布</span>`:''}
      ${e.provisional?'<span class="chip warn">Preliminary · 完成段不足 3</span>':'<span class="chip ok">样本 ≥3 段</span>'}
      ${e.llm_ready?'<span class="chip ok">✓ llm-ready</span>':'<span class="chip bad">无骨架</span>'}
    </div>
    <h2 style="margin:10px 0 4px;font-size:16px;line-height:1.45">${ESC(e.title||e.question)}</h2>
    <div class="mono" style="font-size:12.5px;color:var(--muted);white-space:pre-wrap">${ESC(e.question)}</div>
    ${e.milestone?`<div class="legend" style="margin-top:4px">里程碑：<b>${ESC(e.milestone)}</b></div>`:''}
    ${dims?`<div style="margin-top:6px">${dims}</div>`:''}
    ${intended?`<div class="legend" style="margin-top:6px">教师预置的模式：${intended}</div>`:''}
  </div>
  <div class="panel">
    <div class="panel__head"><h2>会话账 · P1 → P2 → P3 → P4</h2>
      <span class="hint">同一道题的每个 phase 记在同一份记录里；缺哪一段会如实标出来</span></div>
    ${exLadder(e)}
    ${!e.phase2_recorded && e.sessions?`<div class="note warn">这道题的会话跑在 Phase2 落盘之前
      —— 上表 P2 那一格是空的，不是"没交码"，而是当时没记。之后的会话会补上。</div>`:''}
  </div>
  <div class="panel">
    <div class="panel__head"><h2>每段会话（${(e.runs||[]).length} 段）</h2>
      <span class="hint">一段 = 一次完整的 spec 追问；重跑会另起一段，不接在上一段屁股后面</span></div>
    <table><thead><tr><th>会话</th><th>轮数</th><th>逐轮剩余缺口</th><th>首过轮</th><th>Phase2</th><th>结果</th></tr></thead>
      <tbody>${runRows||'<tr><td colspan="6" class="empty">还没有会话 —— 在「人导入」发布这道题后，点「开始学习」。</td></tr>'}</tbody></table>
    ${pts.length?`<div style="margin-top:10px">${lineChart(pts,{max:maxV,color:'#2f7d57',fmt:v=>String(Math.round(v))})}</div>
      <div class="legend">最近一段的逐轮剩余缺口：纵轴是还剩几个维度没答到，横轴是第几轮。
        形状往下收敛 = 在进步；横着不动 = 卡住（原版的进步账本长这样，不是一条永远向上的分数）。</div>`:''}
  </div>
  <div class="panel">
    <div class="panel__head"><h2>进步 · 同一道题的各遍</h2>
      <span class="hint">第 N 遍 vs 上一遍。重做会清背包（不是接着上一遍继续），账本留着</span></div>
    ${exProgress(e)}
  </div>
  <div class="panel">
    <div class="panel__head"><h2>追问轨迹（Phase 1）</h2>
      <span class="hint">每一轮：判官问了什么 / 哪些维度还开着；折叠块里是 <b>Voyager 这一轮交的作答</b>（spec 正文）</span></div>
    ${trace||'<div class="empty">没有追问轨迹 —— 这道题还没进过 Phase1，或骨架是空转通过的。</div>'}
  </div>
  <div class="panel">
    <div class="panel__head"><h2>Phase 2 · finalCode</h2>
      <span class="hint">spec 冻结后照它写的程序；"交了"与"过关"不是一回事</span></div>
    ${e.phase2_recorded?`
      <div class="imp-row" style="margin-top:0">
        <span class="chip ${p2.submitted?'ok':'grey'}">${p2.submitted?'已提交':'未提交'}</span>
        <span class="chip ${p2.verified?'ok':'warn'}">${p2.verified?'与 spec 一致':(p2.submitted?'仍有对不上的':'未对账')}</span>
        <span class="chip grey">${p2.code_chars||0} 字符</span>
        <span class="chip grey">改 ${p2.rounds||0} 轮</span>
        <span class="chip grey">分歧 ${p2.divergences||0} 条</span>
        ${p2.verified_by?`<span class="chip grey">过关依据 ${ESC(p2.verified_by)}</span>`:''}
        ${p2.submitted_at?`<span class="chip grey">${ESC(p2.submitted_at)}</span>`:''}
      </div>
      ${p2.why?`<div class="legend" style="margin-top:4px">${ESC(p2.why)}</div>`:''}
      ${(p2.open_items||[]).length?`<div class="legend" style="margin-top:4px">对不上的条目：${p2.open_items.map(x=>ESC(x)).join('、')}</div>`:''}
      ${p2.final_code?`${String(p2.final_code).length>400
        ? `<button class="btn btn-secondary" style="margin-top:6px;padding:3px 9px;font-size:11.5px"
             onclick="this.nextElementSibling.style.maxHeight='none';this.remove()">展开全文（${String(p2.final_code).length} 字符）</button>`:''}
        <pre class="excode">${ESC(String(p2.final_code).slice(0,4000))}</pre>`
        :'<div class="empty">这份记录里没有 finalCode 正文（只记了长度）。</div>'}
    `:`<div class="empty">这次会话没有 Phase2 的账（本版之前跑的会话只记了 P1 / P3）${
        (e.answers||[]).length?'；不过这道题有口头作答，见下一块':''}。</div>`}
  </div>
  <div class="panel">
    <div class="panel__head"><h2>Voyager 的作答 · 口头</h2>
      <span class="hint">旧消费线（「交给 Voyager 回答」）留在 coach_log.jsonl 的自然语言答案 —— 只读，不参与进步账</span></div>
    ${exAnswers(e)}
  </div>
  <div class="panel">
    <div class="panel__head"><h2>Phase 3 · 分歧（intent-diff）</h2>
      <span class="hint">spec 说的 vs code 做的，逐条对账；这里只摆证据，不判学生好坏</span></div>
    ${dv||`<div class="empty">${(p3.skipped&&p3.skipped.length)?ESC(String(p3.skipped[0])):'none flagged —— spec 与 finalCode 一致，或这段还没跑到 Phase3。'}</div>`}
  </div>
  <div class="panel">
    <div class="panel__head"><h2>能力模块 · Summary</h2>
      <span class="hint">同构 CohortNarrativeOutput：做法 / 常见偏差 / 强项 / 难点</span></div>
    ${q.narrative?`<div class="legend" style="margin:2px 0 8px">${ESC(q.narrative)}</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px">
        ${exQuadBox('Solution techniques','做法',q.solution_techniques)}
        ${exQuadBox('Common drifts','常见偏差',q.common_drifts)}
        ${exQuadBox('Strengths','强项',q.strengths)}
        ${exQuadBox('Difficulties','难点',q.difficulties)}
      </div>`:'<div class="empty">还没有一段会话完成 —— 按原版规矩，没有完成就不出 Summary。</div>'}
  </div>`;
}

function pOverview(s){
  window.__state = s;
  const ex = s.exercises || [];
  const key = window.__exOpen || '';
  let hit = null;
  if(key) hit = ex.find(x=>x.qid===key) || ex.find(x=>(x.req_ids||[]).includes(key)) || null;
  const head = hit ? exDetail(s,hit) : exList(s);
  // 判据区与考核线留在下面：它们是**尺子**（Voyager 的 done / 摸底考核），
  // 不是进步账 —— 进步看上面每道题的缺口序列。
  // 2026-11-18：判据区改读学习轨迹账（learn_trace）—— 绿 = 真取到数，
  // 蓝 = 过证据门（critic 判完成）。不再用原版 Voyager 的 done/success
  // （新后端恒 ✗，画出来像全失败，其实节点都点亮了）。
  const suc = (s.trace||[]).map((p,i)=>({x:i+1,v:!!p.fetch_ok}));
  const vlm = (s.trace||[]).map((p,i)=>({x:i+1,v:p.status==='learned'||p.status==='partial'}));
  // 详情页只讲这一道题：全局判据/摸底/环境留在列表层，不跟着滚进来
  // （原版学生页也只有题面、追问和 spec，没有课堂级统计）。
  if(hit) return head;
  const judge = s.has_data ? `<div class="panel">
      <div class="panel__head"><h2>判据区 · 任务完成（Voyager 尺子）</h2>
        <span class="hint">绿 = 注入后真取到数（跑通 + 拿到数）；蓝 = 过证据门（critic 判完成）—— 数据来自 learn_trace 现账</span></div>
      ${stepChart(suc,{yes:'#2f7d57',no:'#e5484d'})}
      <div class="legend">蓝 = 过证据门 —— critic 的尺子（决定轨迹 verdict 与技能入库）；技能的「真取到」记账不走它。</div>
      ${stepChart(vlm,{yes:'#3b82f6',no:'#c7ccd4'})}
    </div>` : '';
  return head + judge + examCard(s) + envCard(s);
}

function pPractice(s){
  const r=s.recommend||{};
  const changed = window.__changedFrom && r.topic_id && r.topic_id!==window.__changedFrom;
  const recmd = r.topic_id ? `<div class="recmd ${changed?'now':''}">
      <div><span class="chip ok">出题器当前推荐</span>
        <span class="chip grey">${ESC(r.reason_label||r.reason)}</span>
        <span class="chip grey">难度 ${r.difficulty}${r.difficulty_target?` → 目标 ${r.difficulty_target}`:''}</span>
        ${r.hint?`<span class="chip grey">支架 ${ESC(r.hint)}</span>`:''}
        ${changed?`<span class="changed">本次导入后改推此题（原 ${ESC(window.__changedFrom)}）</span>`:''}</div>
      <div class="rq">${ESC(r.question)}</div>
      <div class="rw"><b>为什么是这题：</b>${ESC(r.explanation)}</div>
      ${r.difficulty_why?`<div class="rw" style="margin-top:4px"><b>难度梯度：</b>${ESC(r.difficulty_why)}</div>`:''}
      ${r.hint_label?`<div class="rw" style="margin-top:4px"><b>支架档位：</b>${ESC(r.hint)} —— ${ESC(r.hint_label)}</div>`:''}
      <div class="tm mono" style="margin-top:6px;font-size:12px;color:var(--muted)">${ESC(r.cmd||'')}</div>
    </div>` : '';

  const taskCards=(s.tasks||[]).map(t=>{
    const done=(s.attempted||{})[t.topic_id]||0;
    const wrong=(s.failed||{})[t.topic_id]||0;
    const isNow=t.topic_id===s.topic_id;
    const tag = isNow ? '<span class="chip ok">当前</span>'
      : wrong>0 ? `<span class="chip bad">错题 ${wrong}</span>`
      : done>0 ? `<span class="chip grey">已做 ${done}</span>`
      : '<span class="chip">未做</span>';
    return `<div class="taskcard ${isNow?'now':''}">
      <div class="th">${ESC(t.question)} ${tag}</div>
      <div class="tm"><span class="chip grey">难度 ${t.difficulty}</span>
        <span class="chip grey">${t.key_points.length} 个评分点</span>
        <span class="chip grey">${t.has_answer_spec} 条确定性配方</span></div>
      <div class="tm">必用工具：${(t.requires_tools||[]).map(x=>`<span class="tool">${ESC(x)}</span>`).join('')||'—'}</div>
      <div class="tm">跑法：<span class="mono">python mve/run_mve.py --llm --topic ${ESC(t.topic_id)}</span></div>
    </div>`;
  }).join('');

  const ref=s.referee||{state:'pending',facts:[]};
  const refState={ready:['ok','就绪'],pending:['warn','待跑'],empty:['bad','未产出'],unavailable:['bad','不可用']}[ref.state]||['grey',ref.state];
  const refRows=ref.facts.length?ref.facts.map(f=>
    `<tr><td class="mono">${ESC(f.subject)}</td><td class="mono">${ESC(f.dimension)}</td>
     <td class="num"><b>${ESC(f.value)}</b></td><td class="num">${f.base===null?'—':f.base}</td>
     <td class="mono">${ESC(f.source)}</td></tr>`).join('')
    :`<tr><td colspan="5" class="empty">裁判还没跑过这道题。跑一次主循环会自动生成并缓存。</td></tr>`;

  return `<div class="panel">
      <div class="panel__head"><h2>出题器 · 下一题</h2>
        <span class="hint">优先级链照伴学：错题重试 &gt; 到期复习 &gt; 人导入方向 &gt; 补工具覆盖 &gt; 推进新题</span></div>
      ${globalDataNote(s,'出题器（planner）——推荐与题库')}
      ${recmd||'<div class="empty">出题器不可用</div>'}
    </div>
    <div class="panel">
      <div class="panel__head"><h2>当前题目</h2></div>
      <div class="mono" style="font-size:13px">${ESC(s.question||'—')}</div>
      <div class="legend">topic_id: ${ESC(s.topic_id||'—')}</div>
    </div>
    <div class="panel">
      <div class="panel__head"><h2>裁判 VLML0 · 标准答案</h2>
        <span class="hint">原版 VLML 独立跑出，非手写</span>
        <span class="chip ${refState[0]}">${ESC(refState[1])}</span></div>
      ${ref.trajectory&&ref.trajectory.length?`<div style="margin-bottom:8px"><span class="chip grey">编排：${ref.trajectory.map(t=>ESC(t)).join(' → ')}</span></div>`:''}
      <table><thead><tr><th>subject</th><th>dimension</th><th>标准值</th><th>base</th><th>来源</th></tr></thead>
        <tbody>${refRows}</tbody></table>
      <div class="note info">Voyager 永远看不到这一列的值，只看得到自己的覆盖率。
        答完判对即停，不会硬跑满轮数。</div>
    </div>
    <div class="panel">
      <div class="panel__head"><h2>题目</h2><span class="hint">本局所有题目</span></div>
      <div class="tasks">${taskCards}</div>
    </div>`;
}

function pImport(s){
  const ex=(s.exercises||[]);
  const exChips = ex.length ? ex.map(e=>`<button class="chip" style="cursor:pointer;border:none"
      onclick="gotoExercise('${ESC(e.qid)}')">${ESC((e.title||e.question).slice(0,26))}
      <span class="hint">${e.completed}/${e.sessions}</span></button>`).join(' ')
    : '<span class="hint">还没有发布过练习。</span>';
  return `<div class="panel">
    <div class="panel__head"><h2>开始学 · 一句话</h2>
      <span class="hint">第一轮由人促成：写下要学的材料/问题 → 拆骨架 → 直接跑 P1→P2→P3；之后要么在这里继续导入，要么点「▶ 启动 Voyager」自由学</span></div>
    ${learnBar(s)}
    <div class="imp">
      <div class="note info" style="margin-bottom:8px">
        一段会话 = 一次把这道题做到底：<b>P1</b> 追问到 spec 冻结 → <b>P2</b> 照 spec 交 finalCode →
        <b>P3</b> 把 spec 与 code 逐条对账（drift / revision / bug）。所以这里发布的不是"一句话"，
        是一道<b>固定的题目系统</b> —— 同一道题重跑才会在它自己名下累积成"第几轮收敛"的账。<br>
        第一轮的 123 必须由人促成（这一框）；后面两条路：<b>人导入</b>（继续在这一框写）或
        <b>自由学</b>（点顶部「▶ 启动 Voyager」——它拿背包里的上下文记忆自己找下一件事，不需要你再说话）。
      </div>
      <div class="imp-row" style="margin-top:0">
        <input id="exTitle" placeholder="练习标题（留空则取题面首句）"
          style="flex:1 1 260px;padding:7px 10px;border:1px solid var(--line);border-radius:var(--radius-sm);font:13px inherit">
        <input id="exUnit" placeholder="单元（可选，如 Unit IV · Functions）"
          style="flex:0 1 240px;padding:7px 10px;border:1px solid var(--line);border-radius:var(--radius-sm);font:13px inherit">
        <select id="exLang" title="编辑器类型：决定 P2 交什么形态的 finalCode">
          <option value="Python">Python</option>
          <option value="SQL">SQL</option>
          <option value="Markdown">Markdown / 数学证明</option>
        </select>
      </div>
      <textarea id="q" placeholder="例：已知函数 f(x)=x^3+ax+1/4，g(x)=-ln x。（i）当 a 为何值时，x 轴为曲线 y=f(x) 的切线？（ii）讨论 h(x)=min{f(x),g(x)} 零点的个数。&#10;支持 Markdown 与 LaTeX：行内 $...$、块级 $$...$$"></textarea>
      <div class="imp-row">
        <input type="file" id="exFile" multiple accept=".md,.txt,.csv,.xlsx,.json"
          onchange="exAttach(this)" style="font-size:12px">
        <span class="hint" id="exFileNote">.md / .txt 直接并入题面；表格类文件落进当前场景的库（学习者查得到）</span>
      </div>
      <div class="imp-row">
        <button class="btn btn-secondary" id="btnRoute" style="white-space:nowrap"
          title="让模型按这份题面拆出 spec-gate 维度与预置偏差；这一步会真跑一次模型（约半分钟）">🧩 生成设问骨架</button>
        <button class="btn btn-primary" id="go" style="white-space:nowrap"
          title="沿用刚生成的骨架直接开跑；没生成过就先拆一份">▶ 发布并开始学习</button>
        <button class="btn btn-secondary" id="clr">清空</button>
      </div>
      <div class="hit" id="hit">先写题面；点「生成设问骨架」可以先看这道题会被问成哪几个维度。</div>
      <div id="result"></div>
    </div>
    <h3 style="margin-top:14px">已发布的练习（${ex.length}）</h3>
    <div style="display:flex;flex-wrap:wrap;gap:6px">${exChips}</div>
    <div class="legend" style="margin-top:8px">这一个框就是全部入口：写 → 拆骨架 → 跑完 123。
      没有场景、没有书单、没有单元规划 —— 只有这一局材料和它跑出来的背包。</div>
  </div>`;
}

/* 骨架预览（同构 New exercise 的 "Opus generates the spec-gate scaffolding"）：
   返回 milestone + dimensions + expected_drifts，人先看清"这道题会被问成什么"，
   再决定发布。后端会把这次拆的骨架落在同一个 req 上，发布时直接沿用 —— 
   不出现「预览一份、学的是另一份」。 */
let exSkel = null;      // {req_id, question, dims}
let exBusy = false;

function exRenderSkel(r){
  const dims=(r.dimensions||[]).map((d,i)=>`<div style="margin:5px 0;font-size:12.5px">
      <span class="chip grey">${i+1}</span> <b>${ESC(d.id||'')}</b>
      <div style="margin-top:2px;color:#33443c">${ESC(d.question||'')}</div>
      ${d.why?`<div class="hint">为什么问这个：${ESC(d.why)}</div>`:''}
    </div>`).join('');
  const drifts=(r.expected_drifts||[]).map(x=>
    `<div style="font-size:12px;margin:3px 0"><span class="chip ${x.category==='bug'?'bad':(x.category==='revision'?'warn':'grey')}">${ESC(x.category||'')}</span>
      ${ESC(x.pattern||'')}</div>`).join('');
  return `<div class="note ${r.ok?'info':''}" style="margin-top:8px">
    <b>${r.ok?'设问骨架已生成':'没拆出维度'}</b>
    <span class="chip grey">req ${ESC(String(r.req_id||'').slice(-10))}</span>
    <span class="chip grey">origin ${ESC(r.origin||'')}</span>
    ${r.milestone&&r.milestone.name?`<span class="chip">里程碑：${ESC(r.milestone.name)}</span>`:''}
    ${r.reason?`<div class="hint" style="margin-top:4px">${ESC(r.reason)}</div>`:''}
    ${dims?`<div style="margin-top:8px">${dims}</div>`:''}
    ${drifts?`<div style="margin-top:8px"><b style="font-size:12px">预置的常见偏差：</b>${drifts}</div>`:''}
    ${r.material_note?`<div class="hint" style="margin-top:6px">材料：${ESC(r.material_note)}</div>`:''}
    ${r.ok?`<div style="margin-top:8px;font-size:12px;color:var(--muted)">
      这就是这道题 P1 会追问的几个维度。确认没问题 → 点「发布并开始学习」；
      不满意可以先改题面再生成一次（会另起一份，旧的留着）。</div>`:''}
  </div>`;
}

async function exPreview(){
  const q=document.getElementById('q'); const hit=document.getElementById('hit');
  const btn=document.getElementById('btnRoute'); if(!q) return;
  const v=q.value.trim();
  if(!v){ hit.innerHTML='<b>先写题面。</b>'; return; }
  if(exBusy) return;
  exBusy=true; if(btn){btn.disabled=true; btn.innerHTML='<span class="spin"></span>拆骨架…';}
  hit.textContent='正在按这份题面拆 spec-gate 维度（一次模型调用，约半分钟）…';
  document.getElementById('result').innerHTML='';
  try{
    const r=await fetch('/api/exercise/preview',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question:v,
        title:(document.getElementById('exTitle')||{}).value||'',
        unit:(document.getElementById('exUnit')||{}).value||'',
        language:(document.getElementById('exLang')||{}).value||''})}).then(x=>x.json());
    if(r.error){ hit.innerHTML='<b>拆骨架失败：</b>'+ESC(r.error); return; }
    exSkel={req_id:r.req_id, question:v, dims:(r.dimensions||[]).length};
    document.getElementById('result').innerHTML=exRenderSkel(r);
    hit.innerHTML=`骨架落在 <b>req ${ESC(String(r.req_id||'').slice(-10))}</b> 上；发布时直接沿用这一份。`;
  }catch(e){ hit.innerHTML='<b>拆骨架失败：</b>'+ESC(String(e)); }
  finally{ exBusy=false; if(btn){btn.disabled=false; btn.textContent='🧩 生成设问骨架';} }
}

async function exPublish(){
  if(exBusy) return;
  const q=document.getElementById('q'); const hit=document.getElementById('hit');
  if(!q) return;
  const v=q.value.trim();
  if(!v){ hit.innerHTML='<b>先写题面。</b>'; return; }
  const btn=document.getElementById('go');
  const meta={title:(document.getElementById('exTitle')||{}).value||'',
    unit:(document.getElementById('exUnit')||{}).value||'',
    language:(document.getElementById('exLang')||{}).value||''};
  exBusy=true; if(btn){btn.disabled=true; btn.innerHTML='<span class="spin"></span>已受理…';}
  try{
    // ★ 2026-11-15（用户：「导入出题这个，直接送去拆骨架送去学习，不要再过消费侧」）：
    //   发布 = 拆骨架 → 学习，一条直线。没预览过就在这里**先拆**（同构助产士
    //   New exercise 的 Publish：Opus 生成 spec-gate 脚手架，发布即开跑）。
    //   拆完把骨架摆出来，再起学习 —— 人看到的维度就是 P1 会追问的那几个，
    //   不存在"发出去的是一句话、学的是另一套维度"。
    if(!(exSkel && exSkel.question===v && exSkel.req_id)){
      hit.textContent='没预览过骨架 → 先拆一份（约半分钟），拆完直接开始学…';
      const pv=await fetch('/api/exercise/preview',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({question:v, ...meta})}).then(x=>x.json());
      if(pv.error){ hit.innerHTML='<b>拆骨架失败：</b>'+ESC(pv.error); return; }
      exSkel={req_id:pv.req_id, question:v, dims:(pv.dimensions||[]).length};
      document.getElementById('result').innerHTML=exRenderSkel(pv);
    }
    const payload={question:v, ...meta};
    // 预览过就沿用那份 req（骨架、维度、账都在它名下）
    if(exSkel && exSkel.req_id) payload.req_id=exSkel.req_id;
    const r=await fetch('/api/learn/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)}).then(x=>x.json());
    if(r.error||r.ok===false){ hit.innerHTML='<b>发布失败：</b>'+ESC(r.error||''); return; }
    hit.innerHTML=`<b>已发布，学习在后台跑。</b>${ESC(r.message||'')}
      这道题会出现在「概览 · 练习」里，点开能看到 P1→P2→P3 的账。`;
    // 标题/单元写进记录（发布时刻）；不阻塞
    fetch('/api/exercise/publish-meta',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question:v, title:payload.title, unit:payload.unit,
        language:payload.language, req_id:payload.req_id||''})}).catch(()=>{});
    q.value=''; exSkel=null;
    setTimeout(load, 1200);
  }catch(e){ hit.innerHTML='<b>发布失败：</b>'+ESC(String(e)); }
  finally{ exBusy=false; if(btn){btn.disabled=false; btn.textContent='▶ 发布并开始学习';} }
}

/* 附件：.md/.txt 并入题面（人写的东西原样进设问材料）；
   表格类（csv/xlsx/json）走场景库上传 —— 它们是**数据**，不是题面。 */
function exAttach(inp){
  const note=document.getElementById('exFileNote');
  const q=document.getElementById('q');
  const files=Array.from(inp.files||[]);
  files.forEach(f=>{
    const isText=/\.(md|txt)$/i.test(f.name);
    if(isText){
      const rd=new FileReader();
      rd.onload=()=>{
        q.value += `\n\n<!-- 附件：${f.name} -->\n` + String(rd.result||'').slice(0,8000);
        if(note) note.textContent=`已并入题面：${f.name}`;
      };
      rd.readAsText(f);
      return;
    }
    const rd=new FileReader();
    rd.onload=()=>{
      const b64=String(rd.result||'').split(',')[1]||'';
      fetch('/api/upload',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({name:f.name,data_b64:b64})})
        .then(x=>x.json()).then(d=>{
          if(note) note.textContent = d.ok
            ? `已落库：${f.name} → 表 ${d.table}（${d.rows} 行${d.visible?'，学习者查得到':'，当前库看不见'}）`
            : `上传失败：${f.name} —— ${d.why||''}`;
        }).catch(e=>{ if(note) note.textContent='上传失败：'+String(e); });
    };
    rd.readAsDataURL(f);
  });
  if(!files.length && note) note.textContent='没有选文件';
}

function gotoTab(id){
  TAB=id; localStorage.setItem('mve_tab',TAB); load();
}
function gotoExercise(qid){
  window.__exOpen=qid; TAB='overview'; localStorage.setItem('mve_tab','overview'); load();
}

/* 只判定命中（不执行）：双阈值判定这句话命中当前场景的哪个节点。
   未命中 → 新建图谱节点（先落待审区，解题成功才升活节点）；
   命中 → 归入已有节点。匹配只看文本相似度，与答案对错无关。 */
async function previewRoute(){
  const q=document.getElementById('q');
  const hit=document.getElementById('hit');
  const text=(q&&q.value||'').trim();
  if(!text){alert('先写一句想问的话');return;}
  hit.innerHTML='<span class="muted">判定中…</span>';
  try{
    const r=await fetch('/api/route',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question:text})}).then(x=>x.json());
    if(r.error){hit.innerHTML='<span class="muted">'+ESC(r.error)+'</span>';return;}
    const dec=r.decision==='HIGH_CONFIDENCE'?'<span class="chip ok">高置信命中</span>'
      :r.decision==='MEDIUM_CONFIDENCE'?'<span class="chip warn">中置信命中</span>'
      :'<span class="chip grey">未命中</span>';
    const act=r.action==='create_new_node'
      ? '→ <b>新建图谱节点</b>（先落待审区，解题成功才升活节点）'
      : '→ <b>归入已有节点</b> '+ESC(r.node_id||'');
    // 判定基准：相关度是拿这句话和**当前场景的图谱节点文本**比的。把它显示出来，
    // 人才知道"相关度低"是问题不好还是归错了场景（2026-10-18 特斯拉问题触发）。
    const basis=`<div class="hint" style="margin-top:4px">判定基准：这份材料的图谱 · `+
      `${ESC(r.scene_nodes==null?'?':r.scene_nodes)} 个节点`+
      (r.scene_sample&&r.scene_sample.length
        ? `（如：${r.scene_sample.map(ESC).join('、')}）` : '（空 —— 冷启动期）')+'</div>';
    const ood = r.out_of_domain ? `<div class="note warn" style="margin-top:6px">
      <b>域外问题</b>：这句话和当前材料<b>没有任何文本重合</b>（相关度 ${ESC(r.max_score)}/100）。
      它问的领域和这份材料不是一回事 —— 相关度低不是"答错了"，是<b>没在这个材料里</b>。
      <div style="margin-top:5px"><span class="hint">要学这个领域，直接导入对应材料再点「▶ 启动 Voyager」。</span></div>
    </div>`
      : (r.scene_nodes===0 ? `<div class="note info" style="margin-top:6px">图谱还是空的（冷启动期）——
        导入后会在这里落下第一个节点。</div>` : '');
    hit.innerHTML=`<div class="imp-row">${dec}<span class="hint">分数 ${ESC(r.max_score)} · 重复命中 ${ESC(r.chunk_hits)}（双阈值，不是人审、也不是答对）</span></div>
      <div style="font-size:12.5px;margin-top:4px">${act}</div>`+basis+ood;
  }catch(e){hit.innerHTML='<span class="muted">判定失败：'+ESC(String(e))+'</span>';}
}

/* 【已停用 2026-11-14】下面这组 cohortCard/cohortBody/switchCohortQuestion 是
   「概览 = 一张能力模块卡 + 下拉切题」那一版的渲染。现在概览是可点击的练习列表
   （exList / exDetail），四象限搬进了每道题的详情页 —— 同一份数据（s.cohort
   与 s.exercises 里都带 quad），但入口只有一个。函数留着只是因为
   consumeCurve（同样已停用）还引用 traceCurveBody，整块一起删是下一步的事。
   ── 原注释 ──
   能力模块（Summary）—— 同构助产士 `CohortNarrativeOutput`
   （`src_lib_opus_schemas.ts:391-398`）：
     narrative / solution_techniques / common_drifts / strengths / difficulties / provisional
   2026-11-14 用户：「把图上能力模块替掉消费线」。
   四项全部来自已有账（Phase1 缺口 + Phase3 分歧 + 教师预置模式），
   不打分、不新增判据；样本 <3 段会话时挂 Preliminary 徽标（原版同款规则）。 */
function cohortCard(s){
  const cg=s.cohort||[];
  const curScene=(s.scene&&s.scene.id)||'A';
  window.__cohort=cg;
  if(!cg.length) return `<h3>能力模块 · Summary</h3>
    <div class="empty">还没有已完成的会话。助产士的规矩：
    至少一个会话跑过 Phase1/Phase3 才出 Summary —— 没有可汇总的过程，就不编内容。</div>`;
  return `<h3>能力模块 · Summary（${cg.length} 道题）</h3>
    <div class="legend">按题汇总：做法 / 常见偏差 / 强项 / 难点，全部从这题自己的缺口账与分歧账读出来。</div>
    <div id="cohortBody">${cohortBody(cg,0)}</div>`;
}
function cohortBody(cg, idx){
  const t=cg[idx]||cg[0];
  if(!t) return '<div class="empty">无</div>';
  const box=(en,zh,items)=>{
    const body=(items&&items.length)
      ? items.map(x=>`<div style="margin:4px 0;font-size:12.5px;line-height:1.5">${ESC(x)}</div>`).join('')
      : '<div style="margin:4px 0;font-size:12.5px;color:var(--muted)">（暂无）</div>';
    return `<div style="border:1px solid rgba(31,41,36,.12);border-radius:8px;padding:10px 12px;background:rgba(255,255,255,.5)">
      <div style="font-size:10.5px;letter-spacing:.07em;color:var(--muted);text-transform:uppercase">${ESC(en)}</div>
      <div style="margin-top:1px;font-size:12px;font-weight:600">${ESC(zh)}</div>
      <div style="margin-top:5px">${body}</div></div>`;
  };
  const opts=cg.map((x,i)=>`<option value="${i}"${i===idx?' selected':''}>${ESC((x.question||'').slice(0,26))} · ${x.sessions} 段/${x.completed} 过</option>`).join('');
  const pts=(t.points||[]).map(p=>({x:p.x,y:p.y,label:`第 ${p.x} 轮 · 剩余缺口 ${p.y}`}));
  const maxV=Math.max(1,...(t.points||[]).map(p=>p.y));
  return `<div class="imp-row" style="margin:6px 0">
      <select id="cohortQ" onchange="switchCohortQuestion(this.value)">${opts}</select>
      <span class="hint">${t.sessions} 段会话 · ${t.completed} 段通过 · 最近一段 ${t.rounds} 轮</span>
      ${t.provisional?'<span class="chip warn">Preliminary · 样本不足 3 段</span>':'<span class="chip ok">样本 ≥3 段</span>'}
      ${t.passed?'<span class="chip ok">最近一段通过</span>':'<span class="chip bad">最近一段未通过</span>'}
      ${t.opens_now===0?'<span class="chip ok">缺口已清空</span>':(t.opens_now===null||t.opens_now===undefined?'':`<span class="chip warn">仍剩 ${t.opens_now} 个缺口</span>`)}
    </div>
    <div class="legend" style="margin:4px 0 8px"><b>${ESC(t.question)}</b><br>${ESC(t.narrative)}</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px">
      ${box('Solution techniques','做法',t.solution_techniques)}
      ${box('Common drifts & errors','常见偏差与错误',t.common_drifts)}
      ${box('Strengths','强项',t.strengths)}
      ${box('Difficulties','难点',t.difficulties)}
    </div>
    ${pts.length?`<div class="legend" style="margin-top:10px">规格迭代（这题逐轮剩余缺口，越往下越接近学会）</div>
      ${lineChart(pts,{color:'#059669',max:maxV,fmt:v=>Math.round(v)+' 个'})}`:''}
    ${(t.intended&&t.intended.length)?`<details style="margin-top:8px"><summary class="legend">教师预置的模式（INSTRUCTOR-INTENDED PATTERNS · ${t.intended.length} 条）</summary>
      ${t.intended.map(x=>`<div class="legend" style="margin:3px 0">· ${ESC(x)}</div>`).join('')}</details>`:''}`;
}
function switchCohortQuestion(i){
  const body=document.getElementById('cohortBody');
  if(!body) return;
  body.innerHTML=cohortBody(window.__cohort||[], Number(i)||0);
}
/* 【已停用 2026-11-14】原「消费进步曲线」视图。概览已由 cohortCard（能力模块）接管；
   这个函数保留只因 `switchCurveScene` / `/api/scene-curve` 还用它查旧账，不再被渲染。
   ── 原注释 ──
   概览页：题内缺口序列（主曲线）+ 消费判分明细（折叠区，审计用）。
   2026-11-14 用户拍板：进步**不再**由消费线的累计掌握数定义 ——
   那个数单调不减（答出来 +1、失败不减），记的是"做了多少题"不是"做得多好"；
   且实测 coach_log 24 条里 mastered=True 0 条、coverage 全空，本来就没产出量。
   现在的进步口径：同一道题逐轮剩余缺口在收敛（助产士 Phase1Iteration 原账）。 */
function consumeCurve(s){
  const cc=s.consume_curve||[];
  const tc=s.trace_curve||[];
  // 1→n：主面板数据 = 当前场景（build_state 已按场景过滤），曲线选择器
  // 默认落到当前场景；「全部场景」是对比视图，另走接口（见 switchCurveScene）。
  const curScene=(s.scene&&s.scene.id)||'A';
  if(!cc.length&&!tc.length) return `<h3>学习曲线 · 题内缺口序列</h3>
    <div class="empty">还没有题目轨迹。启动一次学习跑出
    Phase1 追问，这题的缺口序列就从这里开始记 —— 进步看的是同一道题逐轮还剩几个缺口，
    不再看"累计会做几道题"。</div>`;
  window.__consumeCurveAll=cc;   // 单工作区：曲线只画本局，不再有场景对比
  const tracePart = tc.length
    ? `<div id="traceBody">${traceCurveBody(tc,0)}</div>`
    : `<div class="empty">还没有题内轨迹（Phase1 一轮都没跑过）。</div>`;
  const consumePart = cc.length
    ? `<div id="curveBody">${consumeCurveBody(cc)}</div>`
    : `<div class="empty">无消费记录。</div>`;
  window.__traceCurve=tc;
  return `<h3>学习曲线 · 题内缺口序列（${tc.length} 道有轨迹）</h3>
    <div class="legend"><b>进步 = 同一道题逐轮剩余缺口在收敛</b>。横轴是这道题的追问轮次，
    纵轴是<b>还剩几个维度没闭合</b> —— 线往下走才是学会。这是助产士的原账：
    每交一次 spec 追加一条 <span class="mono">Phase1Iteration</span>
    （<span class="mono">opus/schemas.ts:126</span>），带当轮全量
    <span class="mono">gapsIdentified</span>。
    <b>重跑过的题分段列</b>（run 1/2/…）：每段 = 原版一条会话的
    <span class="mono">phase1.iterations</span>；<b>段起点继承上一段的累计已答集</b>
    （原版 <span class="mono">instructorConfiguredDimensionsAddressed</span>，
    <span class="mono">sessions.ts:264-266</span> 每轮并集写回、从不回退），
    所以后一段更短 = 上次答到的这部分这次不用重走；两段逐轮相同 = 这段没比上段少走一步。</div>
    ${tracePart}
    <details style="margin-top:12px"><summary class="legend">附：消费侧判分明细（审计用，不再作进步度量）</summary>
    ${consumePart}</details>`;
}
/* 题内缺口序列主体：主曲线（剩余缺口）+ 分段对比 + 缺口清单。
   进步口径（2026-11-14 取代消费线）：同一道题逐轮剩余缺口在收敛。
   重跑过的题按 run 分段并列 —— 每段是原版一条会话的 phase1.iterations，
   段起点继承上一段的累计已答集（instructorConfiguredDimensionsAddressed，
   每轮并集写回、从不回退）。所以段变短 = 记忆让同题收敛更快；逐轮相同 =
   这段没比上段少走一步。不需要分数，也不靠人猜。 */
function traceCurveBody(tc, idx){
  const t=tc[idx]||tc[0];
  if(!t) return '<div class="empty">无轨迹</div>';
  const maxV=Math.max(1,...(t.points||[]).map(p=>p.y));
  const pts=(t.points||[]).map(p=>({x:p.x,y:p.y,
    label:`第 ${p.x} 轮 · 剩余缺口 ${p.y}${p.y===0?'（已闭合）':''}`}));
  const d=t.delta;
  const dTxt = d ? (
      (d.rounds===0&&(d.first_pass_round===0||d.first_pass_round===null))
      ? '<span class="chip warn">Δ 0 · 这段没比上段少走一步（已答集已跨段继承，仍为 0 就是真的没有变化）</span>'
      : `<span class="chip ${d.rounds<0?'ok':'warn'}">Δ 轮数 ${d.rounds>0?'+':''}${d.rounds}</span>`+
        (d.first_pass_round===null?'':`<span class="chip ${d.first_pass_round<0?'ok':'grey'}">Δ 首过轮 ${d.first_pass_round>0?'+':''}${d.first_pass_round}</span>`)
    ) : '<span class="chip grey">只跑过一段，Δ 无从可比</span>';
  const rows=t.runs.map(r=>{
    const seq=(r.opens||[]).map(x=>`<span class="mono" style="font-weight:600">${x}</span>`).join(' → ')||'—';
    return `<tr>
      <td class="num">${r.run}</td>
      <td>${seq}</td>
      <td class="num">${r.rounds||0}</td>
      <td class="num">${r.first_pass_round==null?'—':r.first_pass_round}</td>
      <td>${r.passed?'<span class="chip ok">已通过</span>':'<span class="chip warn">未通过</span>'}</td>
    </tr>`;
  }).join('');
  const stuck=(t.stuck||[]);
  const qOpts=tc.map((x,i)=>`<option value="${i}"${i===idx?' selected':''}>${ESC((x.question||'').slice(0,26))} · ${x.n_runs} 段</option>`).join('');
  return `<div class="imp-row" style="margin:6px 0">
      <select id="traceQ" onchange="switchTraceQuestion(this.value)">${qOpts}</select>
      <span class="hint">每道题一条轨迹；重跑过的按 run 分段，看"第 2 次是不是少走几轮"</span>
    </div>
    <div class="legend"><b>${ESC(t.question)}</b>
      ${t.passed?'<span class="chip ok">最近一段已通过</span>':'<span class="chip warn">最近一段未通过</span>'}
      <span class="chip grey">${t.rounds} 轮</span>
      ${t.opens_now===0?'<span class="chip ok">缺口已清空</span>':`<span class="chip warn">仍剩 ${t.opens_now} 个缺口</span>`}
      ${dTxt}</div>
    ${lineChart(pts,{color:'#059669',max:maxV,fmt:v=>Math.round(v)+' 个'})}
    <div class="legend" style="margin-top:2px">纵轴 = 剩余缺口数（越往下越接近学会）</div>
    <table style="margin-top:8px"><thead><tr><th>重跑</th><th>逐轮剩余缺口</th><th>轮数</th><th>首过轮</th><th>结果</th></tr></thead>
      <tbody>${rows}</tbody></table>
    ${stuck.length?`<div class="legend" style="margin-top:6px">最近一轮仍未闭合：${stuck.map(x=>`<span class="chip warn">${ESC(x)}</span>`).join(' ')}</div>`:''}
    ${t.monotonic?'':'<div class="legend"><span class="chip bad">不是单调收缩</span> 某轮缺口比上一轮更多 —— 追问在打转或维度定义有问题。</div>'}`;
}
function switchTraceQuestion(i){
  const body=document.getElementById('traceBody');
  if(!body) return;
  body.innerHTML=traceCurveBody(window.__traceCurve||[], Number(i)||0);
}
/* 曲线主体（图表 + 摘要 + 比对明细）。场景切换只重渲这一块，
   不重渲整个面板 —— 与主面板其它页签同一套渲染函数复用，不另造第二份。 */
function consumeCurveBody(cc){
  if(!cc.length) return `<div class="empty">还没有消费记录。去「人导入」问一句，
    答完了曲线就从这里开始长。</div>`;
  // 【已降级为审计】主曲线 = 累计已掌握数（能力集合在变大）。
  // 2026-11-14 起概览的进步主曲线是上面的缺口序列，这里保留只为核对
  // "当初这条线记了什么"，不再作进步度量。
  //   y 轴画**绝对数**（会做的题有几道），不画百分比 —— 照原版 Voyager
  //   `curriculum.py:135 return len(self.completed_tasks)`。
  //   不画百分比是有理由的：一画百分比，读数就取决于"分母算哪几道题"
  //   （分母里该不该算练习线练过的题？）这种口径争议，而绝对数没有这个问题；
  //   且失败不计入 → 曲线单调不降，形状就是学习本身。
  const tot=cc[cc.length-1].ability_total;
  const masMax=Math.max(1,...cc.map(c=>c.mastered_total||0));
  // 可达证据：同一句话导入 ≥2 次且后一次高置信命中（用户要的「再导入该同一问题时，可达」）
  const seen={};const reachIdx={};let reachN=0;
  cc.forEach((c,i)=>{const q=(c.question||'').trim();if(!q)return;
    seen[q]=(seen[q]||0)+1;
    if(seen[q]>=2&&(c.route_decision||'').indexOf('HIGH')>=0){reachN++;reachIdx[i]=seen[q];}});
  const hitTag=c=>c.route_decision==='HIGH_CONFIDENCE'?'<span class="chip ok">高置信命中</span>'
    :c.route_decision==='MEDIUM_CONFIDENCE'?'<span class="chip warn">中置信命中</span>'
    :c.route_decision?'<span class="chip grey">未命中</span>':'';
  const judTag=c=>c.judgment_result==='written'?'<span class="chip ok">写入图谱</span>'
    :c.judgment_result==='insufficient_evidence'?'<span class="chip warn">冷启动单证据</span>'
    :c.judgment_result==='claim'?'<span class="chip warn">联网 claim</span>'
    :c.judgment_result==='fetched_empty'?'<span class="chip grey">取到真空</span>'
    :c.judgment_result==='failed'?'<span class="chip bad">失败</span>':'';
  const masPts=cc.map((c,i)=>({
    x:i+1,
    y: c.mastered_total||0,
    label:`累计掌握 ${c.mastered_total||0} 道 · ${c.question}`+
      (reachIdx[i]?`（🔁 第 ${reachIdx[i]} 次导入同一问题 · 命中）`:'')+
      (c.route_decision?` · ${c.route_decision}`:'')}));
  // 副线 = 覆盖率（这次答得全不全，质量分，不是掌握度）
  const covPts=cc.filter(c=>c.coverage!==null&&c.coverage!==undefined)
                 .map((c,i)=>({x:i+1, y:c.coverage, label:`覆盖率 ${Math.round(c.coverage*100)}% · ${c.question}`}));
  const last=cc[cc.length-1];
  const first=cc[0];
  const reused=cc.filter(c=>c.reused).length;
  // 每次消费的**比对明细**：判分是"两边做法的比对"得出来的，明细必须能查，
  // 不能只给一个百分比 —— 人看不见依据就没法判断这个分可不可信。
  const cmpRows=cc.map((c,i)=>`<tr>
      <td class="num">${c.i}</td>
      <td>${ESC(c.question)}
        <div style="margin-top:2px">${hitTag(c)} ${judTag(c)}</div></td>
      <td class="num">${ESC(c.rows||0)}</td>
      <td class="num">${ESC(c.referee_rows||0)}</td>
      <td class="num">${c.coverage===null||c.coverage===undefined?'—'
        :(c.judged?Math.round(c.coverage*100)+'%':'未判')}</td>
      <td>${c.coverage===null||c.coverage===undefined
            ? '<span class="chip grey">未判</span>'
            : (c.same_metric?'<span class="chip ok">是</span>':'<span class="chip warn">不是</span>')}</td>
      <td>${c.coverage===null||c.coverage===undefined
            ? '<span class="chip grey">—</span>'
            : (c.referee_compared
                ? `<span class="chip ${c.voyager_sound?'ok':'bad'}">${c.voyager_sound?'站得住':'有硬伤'}</span>`
                : '<span class="chip grey">未对照</span>')}</td>
      <td style="font-size:12px">${ESC(c.method_reason||c.verdict||'')}${c.method_diff?'（'+ESC(c.method_diff)+'）':''}</td>
    </tr>`).join('');
  const delta=(last.mastered_total||0)-(first.mastered_total||0);
  return `<div class="legend"><b>曲线上升</b>（本视图首末读数）：累计掌握
      ${first.mastered_total||0} → ${last.mastered_total||0} 道
      ${delta>0?`<span class="chip ok">Δ +${delta}</span>`:(delta<0?'<span class="chip bad">Δ '+delta+'</span>':'<span class="chip grey">Δ 0</span>')}
      ${reachN?`<span class="chip ok">🔁 可达证据 ${reachN} 次（同一问题二次导入后高置信命中）</span>`:''}</div>
    ${lineChart(masPts,{color:'#059669',max:masMax,fmt:v=>Math.round(v)+' 道'})}
    <div class="legend" style="margin-top:4px">累计掌握 ${last.mastered_total||0}${tot?` / ${tot} 题（${Math.round(100*(last.mastered_total||0)/tot)}%）`:' 道（本场景）'}</div>
    ${covPts.length?`<div class="legend" style="margin-top:8px">一致度（Voyager 与 VLML 两边做法的比对，只给人看）</div>${lineChart(covPts,{color:'#2563eb',dash:'4 3'})}`:''}
    <div class="legend" style="margin-top:6px">
      最近一次：${last.mastered?'<span class="chip ok">已掌握（进能力库）</span>':'<span class="chip bad">没答出来，未进能力库</span>'}
      ${last.coverage===null||last.coverage===undefined?'':`<span class="chip grey">一致度 ${Math.round(last.coverage*100)}% · ${ESC(last.verdict||'')}</span>`}
      <span class="chip grey">${reused}/${cc.length} 次复用已学配方</span>
      ${last.skill_action?`<span class="chip grey">${ESC(last.skill_action)}</span>`:''}
    </div>
    <h3 style="margin-top:10px">两边做法的比对明细</h3>
    <div class="legend">Voyager 取一路、VLML 独立取一路，把两条 SQL 和取到的东西摊开让 LLM 判
      —— 不再要求两边先把答案压成单值事实（答案是一张表也能判）。</div>
    <table><thead><tr><th>#</th><th>问句 · 场景 · 命中判定</th><th>Voyager 行</th><th>VLML 行</th>
      <th>一致度</th><th>同一个量</th><th>Voyager 做法</th><th>判据 / 差别</th></tr></thead>
      <tbody>${cmpRows}</tbody></table>`;
}
/* 场景筛选：选具体场景现取该场景曲线；「全部场景」是对比视图，
   也走同一个接口（scene=__all__ → 后端不过滤，各场景曲线同图）。
   同一个函数 _consume_curve 的场景参数，不新造尺子。 */
function switchCurveScene(sid){
  const body=document.getElementById('curveBody');
  if(!body) return;
  const allSid=(sid==='__all__')?'__all__':sid;
  body.innerHTML='<span class="muted">取 '+(allSid==='__all__'?'全部':'本局')+' 的曲线…</span>';
  fetch('/api/scene-curve?scene='+encodeURIComponent(allSid)).then(x=>x.json()).then(r=>{
    if(r.error){body.innerHTML='<span class="muted">'+ESC(r.error)+'</span>';return;}
    body.innerHTML=consumeCurveBody(r.curve||[]);
  }).catch(e=>{body.innerHTML='<span class="muted">取曲线失败：'+ESC(String(e))+'</span>';});
}

/* ---- 背包行（2026-11-18）----
   面板不再让人「选场景」：只有一个工作区，一个背包。
   数据来自 /api/scenes 的 inventory / material（后端照 curriculum_auto 口径算），
   这里只渲染，不新增取数逻辑。 */
async function loadSceneBar(){
  try{
    const r=await fetch('/api/scenes').then(x=>x.json());
    const inv=r.inventory||{used:0,cap:36,slots:[]};
    const chip=document.getElementById('bagChip');
    if(chip){
      chip.className='chip '+(inv.used?'ok':'grey');
      chip.textContent=`Inventory (${inv.used}/${inv.cap})`;
      chip.title=(inv.slots&&inv.slots.length)
        ? ('背包里这几格：\n'+inv.slots.join('\n'))
        : '空背包：一次会话占一格；攒满 33 格归档一批';
    }
    const hint=document.getElementById('bagHint');
    if(hint){
      const m=String(r.material||'');
      hint.innerHTML = m
        ? `材料 <code>${ESC(m)}</code>`
        : '材料：未设置（导入一个仓库文件夹就开始）';
    }
    // 猫爪总开关（同构 neko）：一组能力一个开关，关掉该组「对模型不可见、
    // 也调不动」，拨回来即恢复（不删条目）。与「场景」无关，纯能力组。
    const gs2=(r.groups||{})[r.current]||[];
    const box=document.getElementById('clawBox');
    if(box){
      const mOn = gs2.length ? !!gs2[0].master : true;
      const mchip = `<button class="chip ${mOn?'ok':'bad'}" style="cursor:pointer"
          title="猫爪总开关（NekoClaw Master Switch）&#10;关掉后下面的能力组全部不可用；拨回来即恢复"
          onclick="toggleGroup('${ESC(r.current)}','*',${mOn?'false':'true'})"
          >${mOn?'🐾 猫爪总开关 开':'🐾 猫爪总开关 关'}</button>`;
      const gchips = gs2.map(g=>{
        const cls = g.state==='ready' ? 'ok' : (g.state==='master_off'||g.state==='off') ? 'grey' : 'warn';
        const mark = g.state==='ready' ? '🟢' : (g.state==='master_off'?'🔒':'⚪');
        const tip = `能力组：${g.group}（${g.count} 条）&#10;`+
                    (g.why?g.why+'&#10;':'')+
                    `条目：${(g.entries||[]).join(' / ')}`;
        return `<button class="chip ${cls}" style="cursor:pointer"
           title="${tip}"
           onclick="toggleGroup('${ESC(r.current)}','${ESC(g.group)}',${g.switch?'false':'true'})"
           >${mark} ${ESC(g.group)} · ${g.count}</button>`;
      }).join(' ');
      box.innerHTML = mchip + (gs2.length?gchips
        : '<span class="hint">还没有可执行动作条目</span>');
    }
  }catch(e){ /* 背包行挂了不能拖垮主面板 */ }
}


/* 拨猫爪总开关：关掉整组 = 对模型不可见、也调不动（不删条目，拨回来即恢复）。 */
function toggleGroup(sid,group,on){
  fetch('/api/actions/switch',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({scene:sid,group:group,enabled:on})}).then(x=>x.json()).then(r=>{
    if(!r.ok){ alert('拨开关失败：'+(r.error||'')); return; }
    loadSceneBar();
  });
}
/* 判据中文名。以前这一列只有 VLML0 / rubric 两种，新后端的 `db_evidence`
   会被归到 "rubric"（≠referee 就走 else 分支）—— 那是**错的判据名字**，
   人看到会以为这一轮是 rubric 判的，其实是"有没有真的从 DB 取到数"。 */
const JUDGE_LABEL={db_evidence:'DB 取证', referee:'VLML0 裁判', rubric:'rubric',
                   method_compare:'两边做法比对', none:'未判'};

function pTrace(s){
  const tr=s.trace||[];
  if(!tr.length) return `<div class="panel"><div class="empty">
    学习轨迹账（learn_trace）还没有行 —— 学习循环跑完一轮 learn()，这里就有：
    Phase1 spec 冻结 / Phase2 finalCode 交付 / 取数真行数 / 里程碑闭合与点亮 / 证据门。
    旧账 run_log 已退役（它按 critic 门记账，里程碑点亮了它还记 failed，全是假 ✗）。</div></div>`;
  const rows=tr.slice().reverse().map((p,i)=>{
    const ms=p.ms_total?`<span class="chip ${p.ms_achieved?'ok':'grey'}">${p.ms_closed}/${p.ms_total}${p.ms_achieved?' ✅':''}</span>`:'<span class="chip grey">—</span>';
    return `<tr>
      <td class="num">${tr.length-i}</td>
      <td class="mono" style="font-size:12px">${ESC((p.question||'').slice(0,52))}</td>
      <td>${p.phase1_passed?'<span class="chip ok">冻结</span>':'<span class="chip bad">未冻结</span>'}</td>
      <td>${p.phase2_submitted?'<span class="chip ok">已交</span>':'<span class="chip bad">未交</span>'}</td>
      <td class="num">${p.fetch_calls||0}</td>
      <td>${p.fetch_ok?`<span class="chip ok">真取到 ${p.fetch_rows||0} 行</span>`:'<span class="chip bad">0</span>'}</td>
      <td>${ms}</td>
      <td>${(p.ms_achieved||p.lit)?'<span class="chip ok">已点亮</span>':'<span class="chip grey">—</span>'}</td>
      <td>${(p.gate_addressed>0&&p.gate_open===0)?'<span class="chip ok">门过</span>':`<span class="chip bad">开 ${p.gate_open||0}</span>`}</td>
      <td><b>${ESC(p.status||'')}</b></td>
      <td style="font-size:12px;opacity:.7">${ESC((p.why||'').slice(0,60))}</td></tr>`;
  }).join('');
  return `<div class="panel">
    <div class="panel__head"><h2>学习轨迹</h2>
      <span class="hint">MVE 现账（learn_trace）：一次 learn() 一行 —— Phase1 / Phase2 / 取数 / 里程碑 / 证据门，不再是原版 Voyager 的 coverage/score</span></div>
    <table><thead><tr><th>#</th><th>题面</th><th>Phase1</th><th>Phase2</th><th>取数</th><th>真取到</th><th>里程碑</th><th>点亮</th><th>证据门</th><th>status</th><th>为什么</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function pSkill(s){
  const st=s.skill_stats||{};
  // 复用健康度判据（与原溯源视图同一套规则，去重后并入技能卡）：
  //   hits>=2 且有效率 <0.5 → stale；hits==0 → unused；否则 ok。
  // 2026-11-18 换尺：分母用 runs（注入后真进了执行的轮数），不用 hits
  // （开题注入就 +1，Phase1 就死的轮永远取不到数，会把所有配方拖成"失效"）。
  const health=x=>{
    const hits=+x.hits||0, ok=+x.ok||0, runs=+x.runs||0;
    if(hits===0) return '<span class="chip grey">⚪ 未验证</span>';
    if(runs<2) return `<span class="chip grey" title="注入 ${hits} 次里只有 ${runs} 次真进了执行，样本不足不下判">⚪ 样本不足</span>`;
    if(ok/runs<0.5) return '<span class="chip bad">🔴 失效（进了执行没取到数）</span>';
    return '<span class="chip ok">🟢 正常复用</span>';};
  const cards=(s.skill_detail||[]).map(x=>`
    <div class="taskcard" style="margin-top:8px">
      <div class="th">${ESC(x.text)}</div>
      <div class="tm"><span class="chip grey" title="技能名（消费索引键的一部分）">${ESC(x.name||'—')}</span>
        <span class="chip grey" title="精确索引键：会话 topic::技能名 —— 消费按它直取">${ESC(x.key||'')}</span>
        <span class="chip grey">${ESC(x.topic||'—')}</span>
        ${x.import_id?`<span class="chip grey" title="这次人导入长的技能">来自批次 ${ESC(x.import_id)}</span>`:''}
        ${x.milestone?`<span class="chip grey">里程碑 ${ESC(x.milestone)}${x.dimension?' · '+ESC(x.dimension):''}</span>`:''}
        <span class="chip grey" title="被检索注入 prompt 的轮数（开题即计，不管后来死在哪）">注入 ${x.hits}</span>
        <span class="chip grey" title="注入后真的进入执行（Phase2 交付 / 真调过取数工具）的轮数 —— 真取到率的分母">进执行 ${x.runs||0}</span>
        <span class="chip ${x.ok>0?'ok':'grey'}" title="进入执行且这轮真取到数的轮数">真取到 ${x.ok}</span>
        ${x.runs>0
          ? `<span class="chip ${x.ok>0?'ok':'bad'}">真取到率 ${Math.min(100,Math.round(100*x.ok/x.runs))}%</span>`
          : '<span class="chip dim">还没进过执行</span>'}
        ${health(x)}
        ${x.versions>1?`<span class="chip warn">已覆盖 ${x.versions} 版</span>`:''}
        <button class="btn btn-danger" style="padding:1px 8px;font-size:12px;margin-left:6px"
          data-delkey="${ESC(x.key)}" data-delname="${ESC(x.name||'')}"
          title="复核发现错账（假阳性/冗余）时单条清除：本体+向量+结晶一并，删动作记审计事件，不可恢复">清账</button></div>
      ${x.code?`<details data-k="skill:${ESC(x.key)}:code" style="margin-top:4px"><summary style="cursor:pointer;font-size:12px;opacity:.75">配方（code，原样入库的取数做法）</summary><pre style="white-space:pre-wrap;background:rgba(127,127,127,.08);padding:6px;border-radius:6px;font-size:12px;max-height:240px;overflow:auto">${ESC(x.code)}</pre></details>`:''}
    </div>`).join('');
  // 2026-11-18：不再按场景分组（只有一个工作区，一份配方库）。
  const sceneLine = `<div class="legend"><span class="hint">同一份材料里攒下的配方，跨会话复用；不区分场景。</span></div>`;
  const c=s.causal||{};
  const kc={control:'ok',attempt:'',referee:'grey',narrative:'grey'};
  const mc=s.mc_thinking||{};
  const mcBlock = mc.count ? `
    <div class="note info" style="margin-top:10px">
      🧠 <b>MC 思路学习入库：${mc.count} 条</b>
      <span class="hint">（topic=mc-thinking，MVE 标准练习循环运行期可被检索）</span>
      <ul style="margin:6px 0 0 18px;padding:0">${(mc.items||[]).map(x=>`<li><b>${ESC(x.name)}</b> · 注入 ${x.hits} · 真取到 ${x.ok}${x.versions>1?` · 已覆盖 ${x.versions} 版`:''}</li>`).join('')}</ul>
      ${mc.skill_md?`<div class="hint">SKILL.md：${ESC(mc.skill_md)}（OpenClaw 考场直接注入）</div>`:''}
    </div>` : '';
  const tl=(c.rows||[]).length?`<div class="timeline">${(c.rows||[]).slice().reverse().map(r=>`
      <div class="tl"><span class="seq">#${r.seq}</span>
        <span class="kd"><span class="chip ${kc[r.kind]||'grey'}">${ESC(r.kind_label)}</span></span>
        <span>${ESC(r.summary)}</span>
        <span class="ts">${ESC(String(r.ts||'').replace('T',' ').slice(5,19))}</span></div>`).join('')}</div>`
    :`<div class="empty">时间线还是空的 ——
      人导入一个问题（哪怕没取到数）就会在这里落下一条 control 事实。</div>`;
  return `<div class="panel">
      <div class="panel__head"><h2>技能库</h2>
        <span class="hint">每轮<b>先检索</b>再执行（retrieve → rollout）；按真取到率排序</span></div>
      <div class="legend"><b>注入</b> = 被检索注入过几次 prompt · <b>真取到</b> = 注入后这轮真的取到数/真算过几次
        （2026-11-18 换尺：不再拿 critic 的证据门当技能的尺，与里程碑点亮的 fetched 口径同一条）·
        <b>注入涨、真取到不涨</b> = 注入了但没帮上，检索排序里自然沉底（命中加成已删，失败配方不再霸榜）。
      </div>
      <div class="legend">条目 <b>${st.skills||0}</b> · 累计写入 <b>${st.writes||0}</b> 次
        （覆盖 ${st.rewrites||0} · 丢弃 ${st.skipped||0}）·
        膨胀率 <span class="chip ${(st.bloat||1)>1?'ok':'warn'}">${st.bloat||1}</span>
        · 每题最多注入 <b>${st.top_k||5}</b> 条进 prompt</div>
      ${sceneLine}
      ${cards||'<div class="empty">空。Voyager 还没积累任何经验。</div>'}
      ${mcBlock}
    </div>
    ${pSessions(s)}
    <div class="panel">
      <div class="panel__head"><h2>行动因果时间线</h2>
        <span class="hint">control（人导入）与 attempt（Voyager 行动）在同一条单调序列上，只保序不评分</span></div>
      ${tl}
    </div>`;
}

/* ---------------- 需求 · 会话（第三刀 E）----------------
   会话视图**复用技能页**（用户 2026-11-09：「其实会话应该是可以复用技能页面的」）
   —— 不新开页面、不新增后端接口（数据走既有 /api/state 的 s.sessions）。

   照抄原版 maieutic：技能挂在 Session 上（靠 sessionId 关联），
   这里靠 `blueprint.req_id` **精确**连接。
   ⚠️ 老技能没有这一项 → **不按 milestone 标题猜**（标题可重名，猜=串台），
     如实说「还没有关联技能」。 */
function pSessions(s){
  const ss=(s.sessions||[]);
  const PH={0:['grey','未开始'],1:['grey','已拆骨架'],2:['warn','学习中'],
            3:['warn','已验待披露'],4:['ok','已闭合归档']};
  const ts=a=>{const n=Number(a||0);
    return n>0?new Date(n*1000).toISOString().replace('T',' ').slice(5,19):'';};
  if(!ss.length) return `<div class="panel">
      <div class="panel__head"><h2>需求 · 会话</h2>
        <span class="hint">照原版 maieutic：一个需求 = 一条会话，技能挂在会话上</span></div>
      <div class="empty">本场景还没有登记过需求 —— 去「✍️ 人导入」问一句，这里就会长出会话。</div>
    </div>`;
  const cards=ss.map(x=>{
    const ph=PH[x.phase]||['grey','未开始'];
    const total=x.total||0, closed=x.closed||0;
    const allClosed=total>0&&closed>=total;
    const ver=x.verified||{};
    const vkeys=Object.keys(ver);
    const dims=(x.dims||[]).map(d=>{
      const isC=(x.unclosed||[]).indexOf(d.id)<0;
      const vv=ver[d.id]||null;
      const dd=(x.dim_detail||[]).filter(z=>z.id===d.id)[0]||{};
      const rr=(dd.r!=null&&dd.r>=0)?dd.r:((vv&&vv.r>=0)?vv.r:null);
      return `<li><span class="chip ${isC?'ok':'grey'}">${isC?'已闭合':'未闭合'}</span>
        <b>${ESC(d.id)}</b>
        ${d.landing?`<span class="chip grey">落点 ${ESC(d.landing)}</span>`:''}
        ${dd.rounds?`<span class="chip ${dd.rounds>=3?'bad':'grey'}" title="这一维在闭环里被试过几轮">试了 ${dd.rounds} 轮</span>`:'<span class="chip dim">还没试</span>'}
        ${rr!=null?`<span class="chip ok">r=${rr}</span>`:''}
        ${dd.how?`<span class="chip grey">判定 ${ESC(dd.how)}</span>`:''}
        <span class="hint">${ESC(d.question||'')}</span>
        ${dd.why?`<div class="hint" style="margin-top:2px;opacity:.8">为什么要有这一维：${ESC(dd.why)}</div>`:''}
        ${(!isC&&dd.asked)?`<div class="hint" style="margin-top:2px;color:#c98a00">上一次的判词：${ESC(dd.asked)}</div>`:''}
        ${(!isC&&dd.gap)?`<div class="hint" style="margin-top:2px">还缺：${ESC(dd.gap)}</div>`:''}
        ${dd.answer?`<details data-k="sess:${ESC(x.req_id)}:${ESC(d.id)}:ans" style="margin-top:2px"><summary style="cursor:pointer;font-size:12px;opacity:.75">这一维验收交出的答案（原样）</summary><pre style="white-space:pre-wrap;background:rgba(127,127,127,.08);padding:6px;border-radius:6px;font-size:12px;max-height:220px;overflow:auto">${ESC(dd.answer)}</pre></details>`:''}
        ${dd.code?`<details data-k="sess:${ESC(x.req_id)}:${ESC(d.id)}:code" style="margin-top:2px"><summary style="cursor:pointer;font-size:12px;opacity:.75">取数配方（code，跑通的那次）</summary><pre style="white-space:pre-wrap;background:rgba(127,127,127,.08);padding:6px;border-radius:6px;font-size:12px;max-height:220px;overflow:auto">${ESC(dd.code)}</pre></details>`:''}
      </li>`;
    }).join('');
    const evs=(x.events||[]).slice(-8).map(e=>{
      const p=e.payload||{};
      const what=e.kind==='phase_transition'?('阶段 '+p.from+' → '+p.to)
        :(e.kind==='dim_closed'?('闭合 '+p.dim_id+(p.r>=0?'（r='+p.r+'）':''))
        :(e.kind||''));
      return `<div class="tl"><span class="seq">${ESC(ts(e.at))}</span>
        <span>${ESC(what)}</span></div>`;
    }).join('');
    const _skillRow=z=>`<li><span class="mono" style="font-size:12px">${ESC(z.key)}</span>
        ${z.dimension?`<span class="chip grey">维度 ${ESC(z.dimension)}</span>`:'<span class="chip ok">会话级</span>'}
        <span class="chip grey">命中 ${z.hits}</span>
        <span class="chip ${z.ok>0?'ok':'grey'}">有效 ${z.ok}</span>
        ${z.versions>1?`<span class="chip grey" title="这条技能被重写过几次（版本数）">v${z.versions}</span>`:''}
        ${z.sessions>1?`<span class="chip warn" title="这条技能被多个会话学过（会话↔技能是多对多）；本页按会话各列一次">${z.sessions} 个会话学过</span>`:''}
        ${(!z.dimension&&z.question)?`<span class="hint">${ESC(String(z.question).slice(0,60))}</span>`:''}
        ${z.code?`<details data-k="skill:${ESC(x.req_id)}:${ESC(z.key)}" style="margin-top:2px"><summary style="cursor:pointer;font-size:12px;opacity:.75">技能本体（跑通的那段程序/SQL）</summary><pre style="white-space:pre-wrap;background:rgba(127,127,127,.08);padding:6px;border-radius:6px;font-size:12px;max-height:260px;overflow:auto">${ESC(z.code)}</pre></details>`:''}
      </li>`;
    const _taskSk=(x.skills||[]).filter(z=>!z.dimension);
    const _fragSk=(x.skills||[]).filter(z=>z.dimension);
    return `<details class="nocmp" data-k="sess:${ESC(x.req_id)}" style="margin-top:8px">
      <summary class="th">${ESC(x.question||x.req_id)}</summary>
      <div class="tm">
        <span class="chip grey mono">${ESC(x.req_id)}</span>
        <span class="chip ${ph[0]}">${ph[1]}</span>
        ${vkeys.length?`<span class="chip ok">已验答案 ${vkeys.length} 条</span>`:''}
        ${(x.rounds&&x.rounds.rounds)?`<span class="chip grey" title="整题层重试账">学习 ${x.rounds.rounds} 轮</span>`:''}
        ${x.is_target?'<span class="chip warn" title="消费侧立的学习锚点：学习侧下一轮优先攻这句">当前锚点</span>':''}
        ${x.archived?'<span class="chip ok">已归档</span>':''}
        ${(x.known_questions||[]).length>1?`<span class="chip grey">${x.known_questions.length} 种问法</span>`:''}
      </div>
      ${x.has_session?'':`<div class="hint" style="margin-top:6px">${ESC(x.why||'还没有会话记录')}</div>`}
      ${dims?`<ul style="margin:6px 0 0 18px;padding:0">${dims}</ul>`:''}
      ${(x.skills||[]).length?`<div class="hint" style="margin-top:6px">这个会话学成的技能（${(x.skills||[]).length} 条：会话级 ${_taskSk.length} · 维度配方 ${_fragSk.length}）</div>
        ${_taskSk.length?`<div class="hint" style="margin-top:2px;opacity:.8">会话级（task 级：一次会话沉淀一条，整体可重放；同题再派会重写同一条）</div>
          <ul style="margin:4px 0 0 18px;padding:0">${_taskSk.map(_skillRow).join('')}</ul>`:''}
        ${_fragSk.length?`<div class="hint" style="margin-top:4px;opacity:.8">维度配方（会话的构件：每维一条，供单点召回/组合）</div>
          <ul style="margin:4px 0 0 18px;padding:0">${_fragSk.map(_skillRow).join('')}</ul>`:''}`
        :'<div class="hint" style="margin-top:6px">这个会话还没有关联技能（技能写入时还没有需求身份 → 不按标题猜）</div>'}
      ${evs?`<div class="hint" style="margin-top:6px">会话事件流（append-only）</div><div class="timeline">${evs}</div>`:''}
    </details>`;
  }).join('');
  return `<div class="panel">
      <div class="panel__head"><h2>需求 · 会话</h2>
        <span class="hint">照原版 maieutic：一个需求 = 一条会话，技能挂在会话上；换个问法回到同一条</span></div>
      <div class="legend">会话阶段：<span class="chip grey">已拆骨架</span> → <span class="chip warn">学习中</span> → <span class="chip warn">已验待披露</span> → <span class="chip ok">已闭合归档</span>
        <br>★ <b>已验待披露</b> = 学习侧验收已跑完、答案还没披露给消费侧 —— 这一档<b>不该再派任务</b>（重复派 = 重复拆骨架）。</div>
      <div class="legend">📖 <b>这是学习侧的账，纯读</b>：展开任一需求即可看到每一维<b>试了几轮、判官原话、还缺什么、验收交出的答案、跑通的取数配方</b>。
        看它不会派任务、不会改账 —— 要它学一句，去「✍️ 人导入」或消费侧问一句。
        <span class="hint">「试了 N 轮」标红 = 同一维反复关不上（判官每次给的理由若都一样，多半是维度落点选错了表/关键词，不是能力不够）。</span></div>
      ${cards}
    </div>`;
}

/* ---------------- Voyager 真实学习（MC 现场）----------------
   这是「在 MC 里真实学习」的面板控件，和 MVE 驾驶舱同款心智：
   启动 / 停止 / 清库 / 清记忆。停止 = 任务边界优雅退出（不硬杀，技能已落盘）；
   清库 = 删 ckpt/skill/；清记忆 = 删 ckpt/curriculum/ + ckpt/events/。 */
function pVoyager(s){
  const v=s.voyager||{};
  const running=!!v.running;
  const st = v.stop_requested ? '停止中（当前任务收尾）'
           : (running ? '学习中' : ('空闲 · '+(v.why||'')));
  const inv=`<div class="kpis">
    <div class="kpi"><div class="label">技能库</div><div class="value">${v.skill_dir_exists?'有':'空'}${v.completed_count?(' · 已学 '+v.completed_count+' 个'):''}</div></div>
    <div class="kpi"><div class="label">课程进度</div><div class="value">${v.curriculum_dir_exists?'有':'空'}</div></div>
    <div class="kpi"><div class="label">事件历史</div><div class="value">${v.events_dir_exists?'有':'空'}</div></div>
    <div class="kpi"><div class="label">失败任务</div><div class="value">${v.failed_count||0}</div></div>
    <div class="kpi"><div class="label">当前轮次</div><div class="value">${v.iterations||0} / ${v.max_iterations||'—'}</div></div>
  </div>`;
  const ctl=`<div class="runctl">
      <span class="lbl">MC 真实学习</span>
      <button class="btn btn-primary" id="voyStart" ${running?'disabled':''}>▶ 启动学习</button>
      <button class="btn btn-danger" id="voyStop" ${running?'':'disabled'}>■ 停止</button>
      <button class="btn btn-secondary" id="voyClearLog">清空日志</button>
    </div>
    <div class="runctl" style="margin-top:8px">
      <button class="btn btn-danger" id="voyClearSkill" title="删 ckpt/skill/：Voyager 已学的 JS 技能库">🗑 清库（删技能库）</button>
      <button class="btn btn-danger" id="voyClearMemory" title="删 ckpt/curriculum/ + ckpt/events/：课程进度 + 事件历史">🗑 清记忆（删进度+事件）</button>
    </div>
    <div class="note ${running?'info':''}">${
      running
        ? ('正在 MC 里跑 Voyager 自动课程（pid '+v.pid+'）· 已学 '+v.completed_count+' 个技能 · 失败 '+v.failed_count+' 个')
        : ('状态：'+st)
    }</div>`;
  const log = ESC((v.tail||'（暂无日志）').replace(/\u001b\[[0-9;]*m/g,''));
  return `<div class="panel">
    <div class="panel__head"><h2>MC 真实学习 · Voyager 自动课程</h2>
      <span class="hint">Voyager 接外部 MC 服务器，自动提任务 → bot 执行 → Critic 验成功 → JS 技能入库</span>
      <span class="chip ${running?'ok':'grey'}">${running?'学习中':'空闲'}</span></div>
    <div class="legend">和 MVE 面板一样可操作：<b>停止</b>（任务边界优雅退出，不硬杀）、<b>清库</b>（忘技能）、<b>清记忆</b>（忘进度）。技能按任务落盘，停止不会丢已学的。</div>
    ${inv}
    ${ctl}
    <div class="console">${log}</div>
    <div class="legend">日志来自 run_voyager_learn.py（venv）。启动前需：① VPS 内存已升级；② MC 服务器已起（25565）；③ /root/.voyager_secret 有 DEEPSEEK_API_KEY。停止后模型在当前任务收尾即停，不会半途强杀。</div>
  </div>`;
}

/* 循环心跳条 —— 读的是**另一个状态文件**（mve/loop_state.json，只有
   voyager_vlml/loop.py 写）。为什么要分两个文件：面板状态记的是"人做了什么"
   （谁点了启动/停止、pid 多少），循环状态记的是"循环自己跑到哪了"。写在一起
   会互相覆盖 —— 循环每轮重写会把人刚点的 stop_requested 抹掉，面板为显示 pid
   重写会把循环刚落的"这轮没取到数"抹掉。

   这里最重要的职责是把「它没学会」和「链路根本没跑通」**分成两条可见的信息**：
   —— 两种情况的处置完全相反，混成一句"没成功"会让人去改根本不用改的东西。 */
function loopHealthBar(s){
  const p=s.pilot||{}, lp=p.loop||{};
  if(!lp || !lp.round) return '';      // 循环一次都没跑过 → 不喧宾夺主

  const KIND={
    linkage:{c:'bad', t:'链路没跑通',
      d:'一次工具都没调成功 —— 不是"它没学会"，是**环境没转起来**。'
        +'先查：库文件在不在、VLML 的 10 个工具装没装（看上面「数据源与数据库」）、'
        +'mve/db_config.json 指的哪个库。改的是配置/依赖，不是它的水平。'},
    sql_error:{c:'warn', t:'SQL 写错了',
      d:'工具**跑起来了**，是它写的 SQL 有问题（Parser / Binder 错误、列名写错、'
        +'把布尔列当字符串比）。这是**它**的水平问题，链路是好的 —— '
        +'去下面日志里看具体哪一条 SQL 报的错，别去查库和工具。'},
    empty:{c:'warn', t:'取到了 0 行',
      d:'SQL 跑通了但没数据 —— 这是**它**的问题（过滤条件太严 / 题面在库里没这个数据），'
        +'链路是好的。看下面日志里它写的 SQL 卡在哪个 WHERE 上。'},
    unproven:{c:'warn', t:'判据没过',
      d:'取到数了，但 critic 判定这些观察不构成有效证据（多半取到的是错误对象或空壳表）。'},
  };
  const k=KIND[lp.no_evidence_kind]||null;
  const streak=+(lp.no_evidence_streak||0);
  const chips=[
    `第 ${ESC(lp.round)} 轮`,
    lp.continuous?'不停（同构原版 while True）':`上限轮次`,
    `进度 ${ESC(lp.progress||0)} 个里程碑`,
    `工具 ${ESC(lp.tool_ok||0)} 成功 / ${ESC(lp.tool_fail||0)} 失败`,
    `取到 ${ESC(lp.rows||0)} 行`,
  ].map(t=>`<span class="chip grey">${t}</span>`).join('');
  const warn = (streak>=3 && k)
    ? `<div class="note ${k.c==='bad'?'bad':'info'}" style="margin-top:8px">
         <b>⚠️ 连续 ${streak} 轮没拿到证据 —— ${ESC(k.t)}</b>
         <div style="font-size:12px;color:var(--muted);margin-top:4px">${k.d}</div>
         <div style="font-size:12px;color:var(--muted);margin-top:4px">
           注：这只是观测，**循环不会因此停下**（设计：只有点停止能改变节奏）。
           ${lp.rounds_since_progress?`距上次解锁里程碑已 ${ESC(lp.rounds_since_progress)} 轮。`:''}
         </div></div>`
    : '';
  const err = lp.last_error
    ? `<div class="legend mono" style="word-break:break-all;margin-top:6px">
         最近一次工具错误：${ESC(String(lp.last_error).slice(0,300))}</div>`
    : '';
  return `<div class="panel" style="margin-bottom:10px">
    <div class="panel__head"><h2>循环心跳</h2>
      <span class="hint">mve/loop_state.json —— 循环写、面板只读，与驾驶舱状态分开</span>
      <span class="chip ${lp.running?'ok':'grey'}">${lp.running?'循环中':'已停'}</span>
      ${lp.updated_at?`<span class="chip grey">${ESC(String(lp.updated_at).slice(11,19))}</span>`:''}</div>
    <div>${chips}</div>
    ${lp.topic?`<div class="legend">当前题：<span class="mono">${ESC(lp.topic)}</span>`
      +`${lp.generated?'（这题是它自己新生成的）':''}${lp.reason?` · ${ESC(lp.reason)}`:''}</div>`:''}
    ${warn}${err}
  </div>`;
}

function pConsole(s){
  const p=s.pilot||{};
  const log=ESC(p.tail||'（暂无日志）');
  const cont=(p.rounds===0)||((p.loop||{}).continuous);
  return `${loopHealthBar(s)}
  <div class="panel">
    <div class="panel__head"><h2>驾驶舱</h2>
      <span class="hint">面板只负责起停与读日志；进程状态 mve/pilot_state.json，循环状态 mve/loop_state.json</span>
      <span class="chip ${p.running?'ok':'grey'}">${p.running?'运行中':'未运行'}</span>
      ${p.job_label?`<span class="chip ${p.running?'ok':'grey'}">${ESC(p.job_label)}</span>`:''}
      <span class="chip ${cont?'ok':'grey'}">${cont?'不停（跑完图谱 / 点停止才退）':'单轮'}</span>
      ${p.topic?`<span class="chip grey">${ESC(p.topic)}</span>`:''}
      ${p.iterations?`<span class="chip grey">已跑 ${p.iterations} 次</span>`:''}
      ${p.started_at?`<span class="chip grey">起于 ${ESC(String(p.started_at).slice(11,19))}</span>`:''}</div>
    ${p.why&&!p.running?`<div class="legend">${ESC(p.why)}</div>`:''}
    <div class="note info">学习循环<b>不带停</b>（同构原版 voyager.py:326 的 while True）：
      跑完一题，课程从图谱里提下一题，一直跑到你点<b>停止</b>为止。
      停止是<b>优雅停</b>——把手头这道题跑完、技能落盘后才退，不是硬杀。
      选题权在里程碑课程手里（种子里程碑 → 树干推进），单轮上限只在你想"跑一轮看看"时用。</div>
    <div class="console">${log}</div>
    <div style="margin-top:10px">
      <button class="btn btn-secondary" id="clearLog">清空日志</button>
      <button class="btn btn-secondary" id="refreshNow">立即刷新</button>
    </div>
  </div>`;
}

function envCard(s){
  const env=s.env||{}, db=s.db||{};
  const map={ready:['ok','就绪'],degraded:['warn','降级'],unavailable:['bad','不可用']};
  const [c,label]=map[env.state]||['grey',env.state];
  const tables=(env.tables!==undefined&&env.tables!==null)?env.tables:'—';
  return `<div class="panel">
    <div class="panel__head"><h2>数据源与数据库</h2>
      <span class="hint">VLML 的库是一个 .duckdb 文件；换库 = 换这个文件</span>
      <span class="chip ${c}">${ESC(label)}</span></div>
    <div style="margin-bottom:8px">
      ${env.ok?`<span class="chip grey">${tables} 张表</span>`:''}
      ${(env.events!==undefined&&env.events!==null)?`<span class="chip grey">${env.events} 条事件</span>`:''}
      ${env.usage_tips?`<span class="chip grey">${env.usage_tips} 条编排提示</span>`:''}
      ${db.is_default?'<span class="chip grey">默认库</span>':'<span class="chip warn">已切换到自定义库</span>'}
    </div>
    <div class="legend mono" style="word-break:break-all">当前库：${ESC(db.path||'—')}</div>
    ${env.reason?`<div class="note">${ESC(env.reason)}</div>`:''}

    <h3>载入新的数据库</h3>
    <div class="imp-row" style="margin-top:4px">
      <input id="dbPath" class="mono" placeholder="/绝对路径/到/另一个.duckdb"
             style="flex:1;min-width:280px;padding:8px 10px;border:1px solid var(--line);
                    border-radius:var(--radius-sm);background:#fff;color:var(--ink)">
      <button class="btn btn-secondary" id="dbProbe">验货</button>
      <button class="btn btn-primary" id="dbSwitch" disabled>切换并重启进程</button>
      <label class="legend" style="display:flex;align-items:center;gap:5px;margin:0">
        <input type="checkbox" id="dbReset"> 切换后清档
      </label>
    </div>
    <div class="hit" id="dbHit">先点「验货」：会报出几张表、几条事件、原题依赖的 series 还在不在。</div>

    <div class="note info">
      换库后**必须重启面板进程**才生效 —— VLML 的工具里写死
      <span class="mono">EventDatabase(read_only=True)</span>（不带 db_path），
      MVE 靠在 <span class="mono">vlml_env.py</span> 里打补丁换掉默认库，
      而补丁只在进程启动时装一次。
      <br><br>
      ⚠️ 已知限制：题目注册表里的
      <span class="mono">SERIES / 队伍名</span> 是写死的，换库后这些常量要重配，
      否则所有题都查不到数据。验货会替你检查这一条。
    </div>
  </div>`;
}

/* ---------------- 知识图谱 ----------------
   照伴学 knowledge_map.tsx：只读地呈现结构，不做任何推断。
   布局用**分层**（表 → 工具 → 维度，从左到右）而不是力导向 ——
   图谱小且层次天然固定，分层比力导向可读，也不会每次刷新都跳。 */
let GRAPH=null;
async function loadGraph(force){
  if(GRAPH && !force) return GRAPH;
  try{ const r=await fetch('/api/graph?t='+Date.now()); GRAPH=await r.json(); }
  catch(e){ GRAPH={empty:true,error:String(e)}; }
  return GRAPH;
}
const REL={produced_by:['#2f7d5f','产出','实线'],derived_from:['#8a94a6','由表算出','虚线'],
           confusable:['#c2703a','易混','虚线'],co_occurs:['#b9c2ce','同题共现','虚线'],
           procedure_step:['#6d5bd0','编排顺序','实线']};
/* 课程图谱：书 → 单元 → 题（2026-11-14）。
   P 的 648 个单元全画出来是一张目录墙，看不出自己学到哪；只画当前这本书，
   进度靠颜色分辨（黄=当前关 / 绿=已学 / 灰=跳过 / 白=未学）。 */
function renderCourse(g){
  const c=g.course||{}, sid=g.scene_id||'?';
  const units=c.units||[];
  const banner=`<div class="note info" style="margin-bottom:8px">
    <b>课程图谱</b> · 《${ESC(c.book||'—')}》 ·
    本书 ${units.length} 个单元 · 全书 ${c.unit_total==null?'—':c.unit_total} 个单元
    （已学 ${c.unit_done==null?0:c.unit_done} · 跳过 ${c.unit_skipped==null?0:c.unit_skipped}）
    <span class="hint">（只画当前这本书；全书进度看「进度」页）</span></div>`;
  if(!units.length) return banner+`<div class="empty">这本书还没有单元</div>`;
  const NW=248, NH=26, ROW=38, PADY=36, BX=36, UX=316, EX=636, EW=186;
  const H=PADY*2+units.length*ROW, W=EX+EW+36;
  const posU={}, posE={};
  units.forEach((u,i)=>{ posU[u.id]={x:UX,y:PADY+i*ROW+ROW/2}; });
  units.forEach(u=>{ const j=(u.exercises||[]);
    j.forEach((e,k)=>{ posE[e.id]={x:EX,y:posU[u.id].y+(k-(j.length-1)/2)*30}; }); });
  const bookY=H/2-26;
  const head=(x,w,t)=>`<text x="${x+w/2}" y="${PADY-14}" font-size="11" font-weight="700"
      fill="#6b7686" text-anchor="middle">${t}</text>`;
  const es=[];
  es.push(`<path d="M${BX+NW},${bookY+26} C${BX+NW+50},${bookY+26} ${UX-50},${PADY+ROW/2} ${UX},${PADY+ROW/2}"
     fill="none" stroke="#48679c" stroke-width="1.4" opacity=".6"/>`);
  units.forEach((u,i)=>{
    if(i<units.length-1){
      const a=posU[u.id], b=posU[units[i+1].id];
      es.push(`<path d="M${a.x+NW/2},${a.y+NH/2} L${b.x+NW/2},${b.y-NH/2}"
        fill="none" stroke="#d5dbe3" stroke-width="1.1" stroke-dasharray="3 3"/>`);
    }
    (u.exercises||[]).forEach(e=>{
      const a=posU[u.id], b=posE[e.id];
      es.push(`<path d="M${a.x+NW},${a.y} C${a.x+NW+50},${a.y} ${b.x-50},${b.y} ${b.x},${b.y}"
        fill="none" stroke="#2f7d5f" stroke-width="1.3" opacity=".7"/>`);
    });
  });
  const ns=units.map(u=>{
    const p=posU[u.id];
    let fill='#fff', stroke='#b9c2ce', sw=1.2, mark='';
    if(u.current){ fill='#fff4cc'; stroke='#d9a900'; sw=2.2; mark='  ← 当前关'; }
    else if(u.done){ fill='#e8f2ee'; stroke='#2f7d5f'; }
    else if(u.skipped){ fill='#f4f5f7'; stroke='#cfd5dd'; }
    const full=String(u.label||u.id), txt=full.length>26?full.slice(0,25)+'…':full;
    const st=u.done?'已学完':(u.skipped?'已跳过':(u.current?'当前关':'未学'));
    return `<g><rect x="${p.x}" y="${p.y-NH/2}" width="${NW}" height="${NH}" rx="6"
      fill="${fill}" stroke="${stroke}" stroke-width="${sw}"/>
      <text x="${p.x+10}" y="${p.y+4}" font-size="11" fill="#22303a">${ESC(txt)}${mark}</text>
      <title>${ESC(u.id)}
${ESC(full)}
状态：${st} · 已学 ${u.learned||0}/${u.need||1} 道</title></g>`;
  }).join('');
  const ens=units.flatMap(u=>(u.exercises||[]).map(e=>{
    const p=posE[e.id];
    return `<g><rect x="${p.x}" y="${p.y-12}" width="${EW}" height="24" rx="5"
      fill="#e8f2ee" stroke="#2f7d5f"/><text x="${p.x+8}" y="${p.y+4}" font-size="10"
      fill="#22303a">${ESC(String(e.label||e.id).slice(0,24))}</text>
      <title>${ESC(e.id)}
${ESC(e.label||'')}</title></g>`;
  })).join('');
  const bookNode=`<g><rect x="${BX}" y="${bookY}" width="${NW}" height="52" rx="7"
    fill="#eef2f8" stroke="#48679c" stroke-width="1.5"/>
    <text x="${BX+10}" y="${bookY+21}" font-size="11" font-weight="700"
      fill="#22303a">${ESC(String(c.book||'').slice(0,24))}</text>
    <text x="${BX+10}" y="${bookY+39}" font-size="10" fill="#6b7686">
      本书 ${units.length} 单元 · 已学 ${units.filter(u=>u.done).length}</text></g>`;
  return banner+`<div class="panel__head"><h2>书 → 单元 → 题</h2>
      <span class="hint">黄=当前关，绿=已学，灰=跳过</span></div>
    <svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px;background:#fff;border-radius:8px">
      ${head(UX,NW,'单元（本书）')}${head(EX,EW,'已学的题')}
      ${es.join('')}${bookNode}${ns}${ens}</svg>`;
}

/* ---------------- 导入的题（req 节点）：将做 / 已做 ----------------
   已做排前面（同构猫娘伴学：先看见做完的，进步可见）。
   「出这道」= 人靠图谱影响出题的入口：点一下，题面进「新建练习」输入框，
   直接送去拆骨架、送去学习。 */
function graphReqHtml(g){
  const reqs=g.req_nodes||[];
  if(!reqs.length) return '';
  const done=g.req_done||0;
  const rows=reqs.map(r=>{
    const isDone=r.status==='verified';
    return `<div class="imp-row" style="margin:3px 0;gap:8px">
      <span class="chip ${isDone?'ok':'grey'}">${isDone?'✓ 已做':'○ 将做'}</span>
      <span style="font-size:13px;flex:1">${ESC(r.question||r.label)}</span>
      ${isDone&&r.solved_at?`<span class="hint">${ESC(String(r.solved_at).replace('T',' '))} 过</span>`
        :`<button class="btn btn-secondary" style="padding:2px 9px;font-size:12px"
           data-req-pick="${ESC(r.id)}">出这道</button>`}
    </div>`;
  }).join('');
  return `<div class="panel" style="margin-top:10px">
    <div class="panel__head"><h3>📝 题 · 图谱里的导入题</h3>
      <span class="hint">已做 ${done} / ${reqs.length}（已做排前面；Phase2 过关才把「将做」翻成「已做」）</span></div>
    ${rows}
    <div class="legend">这就是「人写图谱影响出题」：出题时题写进图谱，
      图谱里就有这份将做清单；点「出这道」把它送回出题口。</div>
  </div>`;
}
async function reqPick(id){
  const r=((GRAPH||{}).req_nodes||[]).find(x=>x.id===id);
  const q=document.getElementById('q');
  if(!r||!q) return;
  q.value=r.question||r.label||'';
  try{ q.scrollIntoView({behavior:'smooth',block:'center'}); }catch(e){}
  if(typeof exPreview==='function') exPreview();
}

function renderGraph(g){
  // 课程场景（书 → 单元 → 题）走专用视图：它的节点是 topic/book/exercise，
  // 不是表/工具/维度，硬塞进三列布局只会得到一张空图。
  if(g.course && (g.course.units||[]).length) return renderCourse(g);
  if(g.empty) return `<div class="empty">${ESC(g.error||'图谱为空')}</div>`;
  const nodes=g.nodes||[], edges=g.edges||[];
  // 1→n：当前场景的图谱横幅（2026-10-18）。每个场景一张图 —— A 的 360 个
  // Valorant 节点不画进 B 的图谱，反过来也一样。空图谱是冷启动的**正常态**
  // （只有场景根节点），不是"图谱坏了"。
  const sceneId=g.scene_id||'A';
  const sceneSpecific=g.scene_specific_nodes;
  const sceneBanner=`<div class="note info" style="margin-bottom:8px">
    <b>这份材料的图谱</b> ·
    ${sceneSpecific==null?'—':sceneSpecific+' 个专属节点'} · ${edges.length} 条边
    <span class="hint">（全库图谱共 ${g.total_nodes==null?'—':g.total_nodes} 个节点；
    <b>其它场景的节点不画在这里</b> —— 场景间按命名空间隔离，切场景看各自的图）</span></div>`;
  const reqHtml=graphReqHtml(g);
  const byId={}; nodes.forEach(n=>byId[n.id]=n);
  // 收集要画的节点（规则见下方 forEach 注释）：维度（含孤立）必画；
  // 工具/表只有连边才画（避免 machinery 把图变乱）。
  const linked=new Set();
  edges.forEach(e=>{linked.add(e.from);linked.add(e.to);});
  const cols={table:[],tool:[],dimension:[]};
  // 1→n 冷启动：本场景导入的问题落成的是**孤立维度**（kind=dimension，没被任何
  // 种子题的 rubric 连到工具/表）。维度是用户关心的知识点，无论是否连边都要画出来
  // —— 它是"问题插进场景"的可见证明（淡黄虚线框见下方 cold 样式；被再命中升级为
  // verified 后变成普通维度框，照样画）。工具/表是取数 machinery，没被任何维度用
  // 到（没连边）时不画，避免图变乱（VLML 有 9 个工具，多数没被题用到）。两者按
  // kind 区分，互不干扰。deleted 节点不画。
  nodes.forEach(n=>{
    if(n.status==='deleted') return;
    const isolated = !linked.has(n.id);
    if(!isolated || n.kind==='dimension')
      (cols[n.kind]||(cols[n.kind]=[])).push(n);
  });
  ['table','tool','dimension'].forEach(k=>cols[k].sort((a,b)=>a.label.localeCompare(b.label)));
  if(!cols.dimension.length && !cols.tool.length && !cols.table.length){
    // 场景图谱为空（只有场景根节点）= 冷启动期：说清楚"这是正常的、下一步干嘛"，
    // 不要报"图谱里还没有连上边的维度"那种像故障的文案。
    if(sceneSpecific===0) return sceneBanner+reqHtml+`<div class="empty">
      图谱还是<b>空的</b>（冷启动期只有根节点）。
      <br>去「人导入」问一个<b>属于这个场景</b>的问题：未命中会先落待审区，
      解题成功后第一个节点就挂上来，Δ 专属节点 0 → 1。</div>`;
    return sceneBanner+reqHtml+`<div class="empty">这个场景还没有可画的节点</div>`;
  }

  // 待审候选条：运行期题带来的新维度名**不占节点位**（照伴学：运行期新建的
  // 知识点不进权威层，只登记待审）。维度层只由种子题的 rubric 派生。
  const pendHtml = (g.pending_dims||[]).length ? `<div class="note info" style="margin-bottom:8px">
    <b>待审候选 ${(g.pending_dims||[]).length} 个</b>（运行期题带来的新维度，
    <b>未建节点</b>，只落在边上 —— 维度层只由种子题派生，不随出题增长）：
    ${(g.pending_dims||[]).map(p=>`<span class="chip grey">${ESC(p.dimension)}`
      + (p.nearest?` <span style="opacity:.7">≈ ${ESC(p.nearest)}</span>`:'')
      + `</span>`).join(' ')}
  </div>` : '';

  const COLW=262, ROW=30, PADX=54, PADY=26, NW=168, NH=22;
  const order=['table','tool','dimension'];
  const pos={};
  const maxN=Math.max(...order.map(k=>cols[k].length));
  const H=PADY*2+maxN*ROW, W=PADX*2+COLW*2+NW;
  order.forEach((k,i)=>{
    const items=cols[k], total=items.length*ROW;
    const top=PADY+(H-PADY*2-total)/2;
    items.forEach((n,j)=>{ pos[n.id]={x:PADX+i*COLW, y:top+j*ROW+ROW/2}; });
  });
  const colTitle=[['table','表（VLML 建模）'],['tool','工具（MCP）'],['dimension','维度']]
    .map(([k,t],i)=>`<text x="${PADX+i*COLW+NW/2}" y="${PADY-9}" font-size="11" font-weight="700"
        fill="#6b7686" text-anchor="middle">${t}</text>`).join('');

  const es=edges.filter(e=>pos[e.from]&&pos[e.to]).map(e=>{
    const a=pos[e.from], b=pos[e.to], [c,,dash]=REL[e.relation]||['#b9c2ce','','虚线'];
    const x1=a.x+NW, y1=a.y, x2=b.x, y2=b.y;
    const same=(a.x===b.x);
    const mx=(x1+x2)/2;
    // 同列（维度之间）的边走外侧弧，否则会压在节点上
    const d=same?`M${x1},${y1} C${x1+46},${y1} ${x2+46},${y2} ${x2},${y2}`
                :`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
    return `<path d="${d}" fill="none" stroke="${c}" stroke-width="${e.relation==='produced_by'?1.9:1.2}"
      ${dash==='虚线'?'stroke-dasharray="4 3"':''} opacity=".72">
      <title>${ESC(byId[e.from]&&byId[e.from].label||e.from)} → ${ESC(byId[e.to]&&byId[e.to].label||e.to)}
${ESC(REL[e.relation]&&REL[e.relation][1]||e.relation)}${e.origin==='observed'?'（实测）':''}</title></path>`;
  }).join('');

  // 维度节点可点：点了就把它设成练习范围（同「练习此知识点」按钮）
  const ns=Object.entries(pos).map(([id,p])=>{
    const n=byId[id]||{};
    const cold = n.status==='cold_start_guess';          // 🌱 冷启动：淡黄 + 金黄虚线
    let fill={dimension:'#e8f2ee',tool:'#eaf0fb',table:'#f2f0e8'}[n.kind]||'#eee';
    let stroke={dimension:'#2f7d5f',tool:'#48679c',table:'#a8976a'}[n.kind]||'#999';
    let sw=1.2;
    if(cold){ fill='#fff4cc'; stroke='#d9a900'; sw=1.5; }
    const deg=edges.filter(e=>e.from===id||e.to===id).length;
    const clickable = n.kind==='dimension'
      ? ` data-scope-set="${ESC(n.label||id)}" style="cursor:pointer"` : '';
    const isScope = n.kind==='dimension' && window.__scope && window.__scope.active
      && window.__scope.label===n.label;
    const labelTxt=(cold?'⚠ ':'')+ESC(n.label||id);
    const tip='\n状态：'+(cold?'冷启动猜测（单次证据支撑）':(n.status||'verified'))
      +'\n证据数：'+(n.evidence!=null?n.evidence:'?')
      +(n.root_scene?'\n归属场景：'+n.root_scene:'');
    return `<g${clickable}><rect x="${p.x}" y="${p.y-NH/2}" width="${NW}" height="${NH}" rx="5"
      fill="${isScope?'#d9efe3':fill}" stroke="${stroke}" stroke-width="${isScope?2.4:sw}"
      ${cold?'stroke-dasharray="4 3"':''}/>
      <text x="${p.x+8}" y="${p.y+4}" font-size="11" fill="#22303a">${labelTxt}</text>
      ${isScope?'<text x="'+(p.x+NW-6)+'" y="'+(p.y+4)+'" font-size="10" fill="#2f7d5f" '
        +'text-anchor="end">练习中</text>':''}
      <title>${ESC(n.label||id)}
类型：${ESC(n.kind)}
连边：${deg}${n.detail&&n.detail.topic_id?'\n题：'+ESC(n.detail.topic_id):''}${n.detail&&n.detail.required_by?'\n被题要求：'+ESC((n.detail.required_by).join('、')):''}${tip}</title></g>`;
  }).join('');

  // 下方位清单：照伴学 relation_groups —— 图看结构，清单看内容
  const dims=cols.dimension.map(n=>n.label);
  const rows=cols.dimension.map(n=>{
    const d=n.label;
    // ⚠️ 按节点 id 匹配，不拼 'dim:'+label：VLML 种子维度 id 恰好是
    // `dim:<维度名>`，但学习闭环落的维度 id 是 `dim:<topic>:<slug>`，label 是
    // 整句问题 —— 拼 label 永远匹配不上，「由谁产出/来源」就全是"无来源"
    // （2026-10-20 用户截图纠偏）。
    const prod=[...new Set(edges.filter(e=>e.to===n.id&&e.relation==='produced_by')
      .map(e=>(byId[e.from]||{}).label||e.from))];
    const tbl=[...new Set(edges.filter(e=>e.to===n.id&&e.relation==='derived_from')
      .map(e=>(byId[e.from]||{}).label||e.from))];
    const conf=[...new Set(edges.filter(e=>e.relation==='confusable'&&(e.to===n.id||e.from===n.id))
      .map(e=>(e.to===n.id?(byId[e.from]||{}):(byId[e.to]||{})).label||''))].filter(Boolean);
    const src=prod.length?`<span class="chip ok">${prod.map(ESC).join('、')}</span>`
                         :(tbl.length?`<span class="chip grey">表 ${tbl.map(ESC).join('、')}</span>`
                                     :`<span class="chip warn">无来源</span>`);
    const isNow = window.__scope && window.__scope.active && window.__scope.label===d;
    // 伴学四层：阶段（工具集梯度）/ 学科（实体域）/ 章节（主题域）→ 知识点
    const dt=(byId['dim:'+d]||{}).detail||{};
    const sv=(dt.subject_values||[]);
    const layer=`<td>${dt.stage_label?ESC(dt.stage_label):'—'}</td>`
      + `<td>${dt.subject_label?`<span class="chip grey" title="${ESC(sv.join('、')||'无可填值')}">${ESC(dt.subject_label)}`
          + (sv.length?` · ${sv.length} 个可填`:'')+`</span>`:'—'}</td>`
      + `<td>${dt.chapter?ESC(dt.chapter):'—'}</td>`;
    return `<tr${isNow?' class="now"':''}><td class="mono">${ESC(d)}</td>${layer}<td>${src}</td>
      <td>${conf.length?conf.map(c=>`<span class="chip warn">${ESC(c)}</span>`).join(''):'<span class="chip grey">—</span>'}</td>
      <td><button class="btn ${isNow?'':'btn-secondary'}" data-scope-set="${ESC(d)}"
        style="padding:4px 10px;font-size:12px">${isNow?'✓ 练习中':'练习此知识点'}</button></td></tr>`;
  }).join('');

  const sm=g.summary||{};
  return sceneBanner + reqHtml + pendHtml + `<div class="panel__head"><h2>维度 → 工具 → 表</h2>
      <span class="hint">${sm.dimensions||0} 维度 · ${sm.tools||0} 工具 · ${sm.tables||0} 表 · ${sm.edges||0} 边（本场景）</span></div>
    <svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px;background:#fff;border-radius:8px">
      ${colTitle}${es}${ns}
    </svg>
    <div class="note info" style="margin-top:10px">判错时反馈里的「这个维度由哪个工具产出」
      就来自这张图。来源有两种，都不是模型猜的：
      VLML 是 <span class="mono">answer_spec.tool</span> <b>声明</b>出来的
      （origin=declared）；本机/知识型场景没有 rubric，来源是<b>实测</b> ——
      闭合这个维度时执行层<span class="mono">tracer</span> 真的调用过的动作
      （origin=observed，学习回写时落的 produced_by 边）。
      冷启动期新建的维度（淡黄虚线框）即使还没连边也会画出来 —— 它是问题"插进场景"的
      可见证明（证据数=1、待验证）；VLML 那些没被任何题用到的工具仍不画（避免图变乱）。
      <br><b>点任意维度</b>看它对应哪些工具与表。</div>
    <table class="tbl" style="margin-top:12px">
      <thead><tr><th>知识点（维度）</th><th>阶段·工具集梯度</th><th>学科·实体域</th>
        <th>章节·主题域</th><th>由谁产出</th><th>易混</th><th>练习</th></tr></thead>
      <tbody>${rows}</tbody></table>
    <div class="note" style="margin-top:8px;font-size:12px;color:var(--muted)">
      层级照伴学 <b>阶段 → 学科 → 章节 → 知识点</b>：
      <b>阶段</b>＝拿下这个知识点最低要动用哪一级工具集（原始事件表 → 派生聚合
      → 洞察 SQL → 报告工具）；<b>学科</b>＝赛事 / 团队 / 选手 / 地图（从真实列
      派生，不手写）；<b>章节</b>＝实体域下的主题域（首血 / 经济 / 手枪局 / 连败…）。</div>
    ${subjectHtml(g)}`;
}

/* 可填值清单（**不是**"学科登记册"）
   ------------------------------------------------------------------
   伴学 `entry_tutor_question_entries.py:1043-1051`：
       store.list_topics(5000, scope.subject, scope.stage,
                         chapter=scope.chapter, unit=scope.unit,
                         course_family=scope.course_family)
   → 这些键**全是 list_topics 的筛选参数**，知识点库是共享的一份。
   所以换赛事 / 换队伍只是**换一次筛选**（也是迁移：同一知识点换个队伍
   算不算得出），**知识点与工具集不重建、不重新整理**。
   赛事号 ≈ 伴学 course_family（版本/来源）；队伍/选手/地图的具体值 ≈
   伴学运行期 `entity`（可空、不进 topics 表）。 */
function subjectHtml(g){
  const subs=g.subjects||[], sCnt=g.subject_counts||{}, sUn=g.subject_unassigned||[];
  const KIND={series:'赛事',team:'团队',map:'地图',player:'选手',
              tournament:'赛事总览',custom:'自定义'};
  const rows=subs.map(s=>`<tr>
      <td>${ESC(KIND[s.kind]||s.kind)}</td>
      <td class="mono">${ESC(s.value)}</td>
      <td>${s.source==='human'
            ?'<span class="chip ok">人填</span>'
            :'<span class="chip grey">实查</span>'}</td>
      <td>${s.note?ESC(s.note):'<span style="opacity:.5">—</span>'}</td>
      <td>${(sCnt[s.id]||0)||'<span style="opacity:.5">0</span>'}</td>
      <td>${s.source==='human'?`<button class="btn btn-secondary" data-subject-del="${ESC(s.id)}"
            style="padding:3px 8px;font-size:11px">删</button>`:''}</td></tr>`).join('');
  const un=sUn.length?`<div class="note warn" style="margin-top:8px">
      <b>未标记范围 ${sUn.length} 个</b>（跑出来过，但清单里还没有它那个值 ——
      在上面填一个，会当场补上）：
      ${sUn.slice(0,12).map(x=>`<span class="chip grey">${ESC(x)}</span>`).join(' ')}</div>`:'';
  return `<div style="margin-top:16px">
    <div class="panel__head"><h2>可填值（分析范围）</h2>
      <span class="hint">库 <span class="chip grey">${ESC(g.data_source||'—')}</span>
        · ${subs.length} 个可填值 · ${sUn.length} 个未标记范围</span></div>
    <div class="note info" style="margin-bottom:10px">
      这些是<b>筛选用的可填值</b>，不是学科层级：换赛事 / 换队伍只换一次取数范围
      （也是一次迁移：同一知识点换个队伍还算不算得出），
      <b>知识点与工具集是共享的一份，不会重建、也不需要重新整理</b>。
      <br>伴学对照：<span class="mono">list_topics(subject, stage, chapter, unit,
      course_family)</span> 全是<b>筛选参数</b>；团队/选手这类具体值同伴学运行期
      <span class="mono">entity</span>（可空、不进知识结构）。
      <br>「实查」是从当前库里查到的；「人填」是你自己加的（换库不会被同步覆盖）。</div>
    <table class="tbl"><thead><tr><th>类别</th><th>取值</th><th>来源</th>
      <th>说明</th><th>条目</th><th></th></tr></thead>
      <tbody>${rows||'<tr><td colspan="6" style="opacity:.6">（空 —— 先「从数据源同步」，或自己填一个）</td></tr>'}</tbody></table>
    <div style="display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap">
      <select id="subjKind">${['series','team','map','player','custom']
        .map(k=>`<option value="${k}">${KIND[k]}</option>`).join('')}</select>
      <input id="subjValue" placeholder="取值，如 NRG / 706349" style="width:200px">
      <input id="subjNote" placeholder="说明（可选）" style="width:200px">
      <button class="btn btn-primary" data-subject-add="1" style="padding:5px 12px">加为可填值</button>
      <button class="btn btn-secondary" data-subject-sync="1" style="padding:5px 12px">从当前库同步</button>
    </div>${un}</div>`;
}

function pGraph(s){
  const id='graphBody';
  loadGraph().then(g=>{ const el=document.getElementById(id); if(el) el.innerHTML=renderGraph(g); });
  return `<div class="panel" id="${id}"><div class="empty">加载图谱…</div></div>`;
}

/* ---------------- 练习范围操作 ----------------
   事件委托：图谱是异步渲染的（loadGraph().then 才写 innerHTML），
   在 render() 里给按钮挂 onclick 会挂到空节点上，所以监听 document。 */
async function setScope(dim){
  try{
    const r=await fetch('/api/practice-scope',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'set', dim:dim})});
    const d=await r.json();
    uiEvent('practice_scope_set',
      {维度: dim, 关联题: (d.scope&&d.scope.topics||[]).join('、')||'（无）',
       revision: d.scope&&d.scope.scope_revision},
      d.ok ? ('已设为练习范围 → '+(d.scope&&d.scope.topics||[]).join('、'))
           : ('失败：'+(d.error||d.code||'未知')));
    if(!d.ok) alert('设不了：'+(d.error||'未知'));
  }catch(e){ alert('设范围失败：'+e); }
  finally{ await load(); }
}
async function clearScope(){
  try{
    const r=await fetch('/api/practice-scope',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'clear'})});
    const d=await r.json();
    uiEvent('practice_scope_clear',{原范围: d.cleared||'—'},
      d.ok?'已清除，回到出题器自选':'失败');
  }catch(e){ alert('清除失败：'+e); }
  finally{ await load(); }
}
async function addSubject(){
  const kind=(document.getElementById('subjKind')||{}).value||'';
  const value=((document.getElementById('subjValue')||{}).value||'').trim();
  const note=((document.getElementById('subjNote')||{}).value||'').trim();
  if(!value){ alert('先填学科取值（比如 NRG、某个赛事号）'); return; }
  try{
    const r=await fetch('/api/subject/add',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({kind:kind,value:value,note:note})});
    const d=await r.json();
    uiEvent('subject_add',{学科:kind+'='+value,补归属条目:d.moved||0},
      d.ok?('已登记'+(d.existed?'（已存在）':'')
           +((d.moved)?'，顺带把 '+d.moved+' 个旧条目归位':'')
           +(!d.ok?('失败：'+(d.error||'')):''))
      :('失败：'+(d.error||'未知')));
    if(!d.ok) alert('登记失败：'+(d.error||'未知'));
  }catch(e){ alert('登记失败：'+e); }
  finally{ await load(); }
}
async function syncSubjects(){
  try{
    const r=await fetch('/api/subject/sync',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({})});
    const d=await r.json();
    uiEvent('subject_sync',{新登记:d.added||0},
      d.ok?('新增 '+d.added+' 个，共 '+d.total+' 个'):('失败：'+(d.reason||'')));
    if(!d.ok) alert('同步失败：'+(d.reason||'未知'));
  }catch(e){ alert('同步失败：'+e); }
  finally{ await load(); }
}
async function delSubject(id){
  try{
    const r=await fetch('/api/subject/del',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:id})});
    const d=await r.json();
    uiEvent('subject_del',{学科:id}, d.ok?'已删除':'没找到');
  }catch(e){ alert('删除失败：'+e); }
  finally{ await load(); }
}
document.addEventListener('click',e=>{
  const s=e.target.closest('[data-scope-set]');
  if(s){ setScope(s.dataset.scopeSet); return; }
  const rp=e.target.closest('[data-req-pick]');
  if(rp){ reqPick(rp.dataset.reqPick); return; }
  if(e.target.closest('[data-scope-clear]')){ clearScope(); return; }
  if(e.target.closest('[data-scope-practice]')){ pilotStart(); return; }
  if(e.target.closest('[data-subject-add]')){ addSubject(); return; }
  if(e.target.closest('[data-subject-sync]')){ syncSubjects(); return; }
  const sd=e.target.closest('[data-subject-del]');
  if(sd){ delSubject(sd.dataset.subjectDel); return; }
  // 域外问题一键迁场（新建场景 → 切换 → 重新导入同一个问题）
  const mq=e.target.closest('[data-migrate-q]');
});

/* ---------------- 自适应练习（伴学出题/图谱移植） ---------------- */
function pAdaptive(s){
  const tg=s.teaching||{};
  const nodes=tg.nodes||[];
  const mastered=nodes.filter(n=>n.mastery!==null&&n.mastery>=0.8).length;
  const blocked=nodes.filter(n=>n.blocked).length;
  // 卡片内容来自客户端状态 window.__oq / window.__oqGradeHTML，
  // 不写进服务器 state —— 否则每 4 秒轮询重建 stage 会被洗掉。
  const oq=window.__oq||null;
  const cardDisp=oq?'block':'none';
  const topic=oq?(oq.topic_name||oq.topic||'知识点'):'知识点';
  const meta=oq?(`难度 ${oq.difficulty||'?'} · 题型 ${oq.question_type||'?'} · `
                +((oq.selection&&oq.selection.reason)||'')):'';
  const ques=oq?(oq.question||'(无题干)'):'';
  const hint=oq?(ESC(oq.hint||'（无特别提示）')+'<br><b>解题步骤参考：</b><br>'
                +(oq.solution_steps||[]).map(ESC).join('<br>')):'';
  const sel=oq?((oq.selection&&oq.selection.explanation)||''):'';
  const gradeHTML=window.__oqGradeHTML||'';
  const gradeDisp=gradeHTML?'block':'none';
  const sceneNote=globalDataNote(s,'教学图谱与出题器（topic_graph / planner）——全局题库的种子题来自默认场景 A');
  return `<div class="panel">
  <div class="panel__head"><h2>🧩 自适应练习 · 教学图谱</h2>
    <span class="hint">前置门控 + 薄弱优先 + 题型轮换（移植自猫娘伴学）</span></div>
  <div class="row" style="gap:10px;flex-wrap:wrap">
    <span class="chip ${mastered?'ok':''}">已掌握 ${mastered}/${nodes.length}</span>
    <span class="chip ${blocked?'warn':''}">前置未达标 ${blocked}</span>
    <span class="chip">目标难度 ${tg.difficulty_target||1}</span>
    <span class="chip grey">知识点总数 ${tg.total||nodes.length}</span>
  </div>
  ${sceneNote}
  <div class="note info">出题围绕<b>当前库真实实体</b>（系列赛/队伍/地图跟着库走），不再考不存在的图；
    同一知识点多次练习会<b>轮换题型</b>并明确错开上次题目，解决"重复出一道题"。</div>
  <div class="row" style="margin-top:8px">
    <button class="btn btn-primary" id="oqNext">▶ 出下一题</button>
    <span class="hint" id="oqSel">${ESC(sel)||'选题依据会显示在这里'}</span>
  </div>
  <div id="oqCard" class="panel" style="margin-top:12px;display:${cardDisp}">
    <div class="panel__head"><h3 id="oqTopic">${ESC(topic)}</h3>
      <span class="hint" id="oqMeta">${ESC(meta)}</span></div>
    <div id="oqQues" class="q-text">${ESC(ques)}</div>
    <details style="margin:6px 0"><summary class="hint">📌 给模型的口径提示（误区/拿表步骤）</summary>
      <div id="oqHint" class="note">${hint}</div></details>
    <textarea id="oqAns" class="ta" rows="3" placeholder="在此作答（自由文本，可贴 SQL / 工具结果）"></textarea>
    <div class="row" style="margin-top:6px">
      <button class="btn btn-primary" id="oqSubmit">提交判分</button>
      <button class="btn btn-secondary" id="oqReveal">显示参考答案</button>
    </div>
    <div id="oqGrade" class="note" style="margin-top:8px;display:${gradeDisp}">${gradeHTML}</div>
  </div>
</div>`;
}
let _oqLastQuestion='';
async function oqLoadGraph(){
  try{ const r=await fetch('/api/teaching-graph?t='+Date.now()); return await r.json(); }
  catch(e){ return null; }
}
async function oqNext(){
  const btn=document.getElementById('oqNext'); btn.disabled=true; btn.textContent='出题中…';
  try{
    const r=await fetch('/api/open-quiz/next',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({last_question:_oqLastQuestion})});
    const d=await r.json();
    if(!d.topic_id){ alert('出题失败：'+(d.error||'未知')); return; }
    _oqLastQuestion=d.question||'';
    window.__oq=d;
    window.__oqGradeHTML='';   // 新题清空上一题判分
    const _a=document.getElementById('oqAns'); if(_a) _a.value='';  // 清空上一题作答（否则会被轮询的 vals 恢复机制回填）
    await load();              // 重渲染自适应页（卡片从 window.__oq 渲染，不怕轮询洗掉）
  }catch(e){ alert('请求失败：'+e); }
  finally{ btn.disabled=false; btn.textContent='出下一题'; }
}
async function oqGrade(){
  const d=window.__oq; if(!d){ return; }
  const ua=document.getElementById('oqAns').value.trim();
  if(!ua){ alert('先作答再提交'); return; }
  const btn=document.getElementById('oqSubmit'); btn.disabled=true;
  try{
    const r=await fetch('/api/open-quiz/grade',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question:d, user_answer:ua, topic_id:d.topic})});
    const j=await r.json();
    const g=j.grade||{};
    window.__oqGradeHTML=
      `<b>得分：${(+g.score*100).toFixed(0)}%</b> · ${ESC(g.feedback||'')}<br>`+
      (g.hits&&g.hits.length?('命中：'+g.hits.map(ESC).join('、')+'<br>'):'')+
      (g.misses&&g.misses.length?('漏/错：'+g.misses.map(ESC).join('、')):'');
    await load();  // 刷新掌握度统计 + 渲染判分（从 window.__oqGradeHTML，不怕轮询洗掉）
  }catch(e){ alert('判分失败：'+e); }
  finally{ btn.disabled=false; }
}
function oqReveal(){
  const d=window.__oq; if(!d) return;
  window.__oqGradeHTML=
    `<b>参考答案：</b>${ESC(d.answer||'（无）')}<br>`+
    `<b>可接受答案：</b>`+(d.accepted_answers||[]).map(ESC).join('、');
  // 直接写 DOM（即时反馈），同时存 window.__oqGradeHTML 供下次轮询重渲染
  const g=document.getElementById('oqGrade');
  if(g){ g.style.display='block'; g.innerHTML=window.__oqGradeHTML; }
}

/* ---------------- 进度问答（人问，讲解员只转述账面） ---------------- */
const PCHAT_LOG = window.__PCHAT_LOG || (window.__PCHAT_LOG = []);
function pChat(s){
  const hist = PCHAT_LOG.map(m => m.pending
    ? `<div class="card" style="margin-top:8px"><b>问：${ESC(m.q)}</b><div style="margin-top:4px;opacity:.6">讲解员正在读账…</div></div>`
    : (m.err
      ? `<div class="card" style="margin-top:8px"><b>问：${ESC(m.q)}</b><div style="margin-top:4px;color:#c0392b">答不上：${ESC(m.err)}</div></div>`
      : `<div class="card" style="margin-top:8px"><b>问：${ESC(m.q)}</b><div style="margin-top:4px;white-space:pre-wrap">${ESC(m.a)}</div><div style="margin-top:6px"><details data-k="pchat${m.ts}"><summary style="font-size:12px;opacity:.6">这轮读的账</summary><pre style="font-size:11px;white-space:pre-wrap">${ESC(m.ctx||'')}</pre></details></div></div>`)
  ).join('');
  const busy = PCHAT_LOG.length && PCHAT_LOG[PCHAT_LOG.length-1].pending;
  return `<div class="card">
    <h3>💬 进度问答</h3>
    <p style="opacity:.7;font-size:13px">问「现在学到哪了/学得怎么样/卡在哪」。讲解员只转述服务端账面（循环 / 背包 / 课程账 / 里程碑 / chat 游程 / 技能库 / 运行日志），不推断、不评分、不写账。</p>
    <div style="display:flex;gap:8px;margin-top:8px">
      <textarea id="pchatQ" rows="2" style="flex:1" placeholder="例如：现在学到哪了？卡在哪？"></textarea>
      <button class="btn" id="pchatGo" ${busy?'disabled':''} onclick="askProgress()">${busy?'读账中…':'问一句'}</button>
    </div>
    <div id="pchatOut">${hist || '<div style="opacity:.5;font-size:13px;margin-top:8px">还没有问过。</div>'}</div>
  </div>`;
}
async function askProgress(){
  const ta=document.getElementById('pchatQ'); if(!ta) return;
  const q=ta.value.trim(); if(!q) return;
  ta.value='';
  PCHAT_LOG.push({q, pending:true, ts:Date.now()});
  uiEvent('pchat_ask', {问题:q.slice(0,120)}, '已受理');
  await load();
  try{
    // ⚠️ 非 JSON 响应必须兜住（2026-11-18 实测）：面板重启 / nginx 504 时
    //   response body 是空或 HTML，直接 r.json() 会抛
    //   "Unexpected end of JSON input"，人只看到一句 JS 报错。
    const ctl=new AbortController(); const tmr=setTimeout(()=>ctl.abort(),55000);
    let r, txt='';
    try{
      r=await fetch('/api/progress-chat',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({question:q}), signal:ctl.signal});
      txt=await r.text();
    } finally { clearTimeout(tmr); }
    let d=null;
    try{ d=JSON.parse(txt); }catch(_){ d=null; }
    const m=PCHAT_LOG[PCHAT_LOG.length-1];
    if(d && d.ok){ m.a=d.answer; m.ctx=d.context; }
    else if(d && d.error){ m.err=d.error; }
    else { m.err=(r&&r.status)?('后端返回 '+r.status+'，不是 JSON（面板可能正在重启/网关超时）—— 稍后再问一次'):'没拿到响应（超时或面板正在重启）—— 稍后再问一次'; }
    m.pending=false;
  }catch(e){
    const m=PCHAT_LOG[PCHAT_LOG.length-1]; m.pending=false;
    m.err=(e && e.name==='AbortError')?'等了 55 秒还没答（后端超时）—— 稍后再问一次':String(e);
  }
  uiEvent('pchat_ask', {问题:q.slice(0,120)}, '已回答');
  await load();
}
const PANELS={overview:pOverview,practice:pPractice,import:pImport,trace:pTrace,skill:pSkill,graph:pGraph,voyager:pVoyager,adaptive:pAdaptive,console:pConsole,pchat:pChat};

/* 单条清账：技能·因果页每张卡的「清账」按钮（事件委托，重渲染不丢绑定） */
document.addEventListener('click', async ev=>{
  const b=ev.target.closest('button[data-delkey]');
  if(!b) return;
  const key=b.getAttribute('data-delkey')||'';
  const nm=b.getAttribute('data-delname')||'';
  if(!confirm('清掉这条技能账？\n\n'+nm+'\n'+key+
    '\n\n只删这一条；本体+向量+结晶文件（若无同名存活者）一并清理，\n删除动作本身会记审计事件。不可恢复。')) return;
  try{
    const r=await fetch('/api/skill/delete',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({key})});
    const d=await r.json().catch(()=>({}));
    if(d && d.ok) uiEvent('skill_delete',{key:key.slice(0,60)},
      '已清：'+(nm||key.slice(0,40))+(d.plugin_removed?('（结晶 '+d.plugin_removed+' 一并清）'):''));
    else alert('清账失败：'+((d&&d.error)||('HTTP '+r.status)));
  }catch(e){ alert('清账失败：'+e); }
  await load();
});

/* ---------------- 人导入（只在 import 分区里挂事件） ---------------- */
function fillTopics(s){
  // 驾驶舱已经没有「选题」下拉了（选题由练习范围决定），只剩人导入分区的归类下拉
  const sel=document.getElementById('topic');
  if(!sel) return;
  // dataset.loaded 挡重复填充，但**也挡住了新题**：导入生成一道新题后，
  // 下拉里永远没有它，直到手动刷新页面。改成「题数变了就重建」——
  // 平时照旧不闪（4 秒轮询不重填），新题一出现立刻能选。
  const n=(s.tasks||[]).filter(t=>t.topic_id&&t.topic_id!=='?').length;
  if(sel.dataset.loaded && String(n)===sel.dataset.taskn) return;
  const keep=sel.value;
  sel.innerHTML='<option value="">自动匹配（按关键词归类）</option>'+
    (s.tasks||[]).filter(t=>t.topic_id&&t.topic_id!=='?').map(t=>
      `<option value="${ESC(t.topic_id)}">${ESC(t.topic_id)} · 难度${t.difficulty}</option>`).join('');
  sel.value=keep; sel.dataset.loaded='1'; sel.dataset.taskn=String(n);
}

/* 【2026-11-15 删除】`importsList()` 原样搬在这里 —— 它是**消费侧**的账
   （人导入 → Voyager 回答 → 命中哪题 / 长出几条技能 / 两边做法比对），
   挂在「新建练习」页底下。用户拍板：「导入出题这个，直接送去拆骨架送去学习，
   不要再过消费侧」—— 这道页只留 Exercise 的循环，消费历史不再出现在这里。
   消费侧自己的入口（驾驶舱 / 跑分）照旧，不受影响。 */


/* 消费记录的专属块：回答 + 用了能力还是现做 + 掌握 + **两边做法的比对**。
   ⚠️ 必须显示在**历史里**，不能只放在提交回执：消费走后台执行（全链路 1～3 分钟），
   提交回执只说一句"已受理"，结果全靠 4 秒轮询刷进导入历史 ——
   历史里不显示 = 人永远看不到 Voyager 的回答和判分依据。 */
/* 叙事层的依据列表：编号 → 真实 title/url 是**程序**映射回来的
   （narrator.narrate_answer 校验 used 里的编号，越界的剔除并写进 caveats），
   所以这里可以直接当链接标出来。见 docs/叙事层同构-猫娘对照.md */
function sourcesBlock(im){
  const src = im.sources || [];
  if(!src.length) return '';
  const rows = src.map(s=>{
    const label = `[${s.n}] ${ESC(s.title || ('来源 ' + s.n))}`;
    return s.url
      ? `<li>${label} · <a href="${ESC(s.url)}" target="_blank" rel="noreferrer noopener">${ESC(s.url)}</a></li>`
      : `<li>${label}</li>`;
  }).join('');
  // 答案没逐句标编号时，标题必须说实话（narrator 会置 cited=false 并写进 caveats）
  const head = im.narrative_cited === false
    ? '依据（答案未逐句标编号，逐条可点）：' : '依据：';
  return `<div class="nocmp-sub" style="margin-top:5px"><b>${head}</b><ul style="margin:4px 0 0 18px">${rows}</ul></div>`;
}

function consumeBlock(im){
  if(im.mode!=='voyager_consume') return '';
  const pct=v=>(v===null||v===undefined)?'—':Math.round(v*100)+'%';
  const judged = im.coverage!==null && im.coverage!==undefined;
  return `
    <div style="margin-top:6px;border-top:1px dashed var(--line);padding-top:6px">
      <span class="chip ${im.reused?'ok':'warn'}">${im.reused?'复用已学配方':'现场现做'}</span>
      ${im.mastered
        // 分母只对默认场景 A 有意义（它的题库就是全局题库）；B/C 的记录拿
        // 全局 360 题当分母会算出"永远学不会"的假象（与场景曲线的
        // ability_total=None 同一口径，见 _consume_curve 的场景模式）。
        ? `<span class="chip ok">已掌握 · 累计 ${ESC(im.mastered_total)}${im.root_scene&&im.root_scene!=='A'?' 题':`/${ESC(im.ability_total)} 题`}</span>`
        : '<span class="chip bad">没答出来，未进能力库</span>'}
      ${judged
        ? (im.judged
            ? `<span class="chip ${im.coverage>=0.95?'ok':(im.coverage>0?'warn':'bad')}">一致度 ${pct(im.coverage)}</span>
               <span class="chip ${im.referee_compared?(im.voyager_sound?'ok':'bad'):'grey'}">${im.referee_compared?(im.voyager_sound?'做法站得住':'做法有硬伤'):'未对照（裁判没跑成）'}</span>`
            : `<span class="chip grey">口径不同，这一问没真正判到（多数是裁判自己跑偏）</span>
               <span class="chip ${im.referee_compared?(im.voyager_sound?'ok':'bad'):'grey'}">${im.referee_compared?(im.voyager_sound?'Voyager 做法站得住':'Voyager 做法有硬伤'):'未对照（裁判没跑成）'}</span>`)
        : '<span class="chip grey">未判（VLML 那一路没取到数）</span>'}
      ${(im.coverage_referee!==null&&im.coverage_referee!==undefined)
        ? `<span class="chip grey">硬尺子 ${pct(im.coverage_referee)}</span>`:''}
      <span class="chip grey">工具 ${ESC(im.ok_tool_calls||0)} 次 · ${ESC(im.rows||0)} 行</span>
      ${im.import_id?`<span class="chip grey" title="一次人导入=一个批次；它长的节点与技能都挂在这个 id 上">批次 ${ESC(im.import_id)}</span>`:''}
      ${(im.mcp_exposed||[]).length
        ? `<span class="chip grey">远端工具 ${(im.mcp_exposed||[]).length} 个</span>`
        : (im.mcp_note?`<span class="chip warn" title="${ESC(im.mcp_note)}">远端工具 0 个</span>`:'')}

      ${im.scene_kind==='local'?'<span class="chip grey">本机数据源</span>'
        :im.scene_kind==='scene'?'<span class="chip grey">场景自带库</span>':''}
      ${im.route_decision==='HIGH_CONFIDENCE'?'<span class="chip ok">高置信命中</span>'
        :im.route_decision==='MEDIUM_CONFIDENCE'?'<span class="chip warn">中置信命中</span>'
        :im.route_decision==='REJECTED'?'<span class="chip grey">未命中 → 新建节点</span>':''}
      ${im.judgment_result?`<span class="chip ${im.judgment_result==='written'?'ok':(im.judgment_result==='claim'?'warn':(im.judgment_result==='failed'?'bad':'grey'))}">${{written:'✅ 写入图谱',insufficient_evidence:'🌱 冷启动单证据',claim:'🌐 联网 claim',fetched_empty:'取到真空',failed:'失败'}[im.judgment_result]||ESC(im.judgment_result)}</span>`:''}
    </div>
    ${im.reuse_explain?`<div style="margin-top:5px;font-size:12px"><b>为什么算复用：</b>${ESC(im.reuse_explain)}</div>`:''}
    ${(im.reuse_errors&&im.reuse_errors.length)?`<div style="margin-top:5px;font-size:12px"><b>配方为什么没复用上：</b>${ESC(im.reuse_errors.join('；'))}</div>`:''}
    ${outOfDomainNote(im)}
    ${im.answer?`<div style="margin-top:6px"><b>Voyager 的回答：</b>
      <div class="nocmp-body" style="margin-top:4px">${ESC(im.answer)}</div></div>`:''}
    ${sourcesBlock(im)}
    ${im.narrative_skipped?`<div style="margin-top:5px;font-size:12px;color:var(--muted)">
      <b>叙事层没产出</b>（答案退回原始取数结果）：${ESC(im.narrative_skipped)}</div>`:''}
    ${im.method_reason?`<div style="margin-top:5px;font-size:12px;color:var(--muted)">
      <b>两边做法的比对：</b>${ESC(im.method_reason)}${im.method_diff?'；差别：'+ESC(im.method_diff):''}
      <br>Voyager 取到 ${ESC(im.rows||0)} 行，VLML 独立那一路取到 ${ESC(im.referee_rows||0)} 行 ——
      裁判只取数不整理，比对交给 LLM 直接看两条 SQL 和取到的东西。</div>`:''}
    ${im.skipped?`<div style="margin-top:5px;font-size:12px;color:var(--muted)">${ESC(im.skipped)}</div>`:''}`;
}

/* 域外问题引导（2026-10-18，用户实测「特斯拉股价」导入后相关度 0 触发）。
   判定签名：消费记录里 取到真空（fetched_empty）或（未命中 且 相关度<10 且 0 行）
   —— 这个问题和**当前场景**的图谱/数据源不是同一个领域。按设计它仍归当前场景
   （未命中 → 新建节点），但面板必须说清"为什么相关度低、去哪"，并给一键迁场。 */
function outOfDomainNote(im){
  if(im.mode!=='voyager_consume') return '';
  const score=+(im.route_score||0);
  const rows=+(im.rows||0);
  const emptyFetch = im.judgment_result==='fetched_empty';
  const noHit = im.route_decision==='REJECTED' && score<10 && !rows;
  if(!(emptyFetch||noHit)) return '';
  return `<div class="note warn" style="margin-top:6px">
    <b>这个问题和当前材料对不上。</b>这局材料的图谱里没有和它对得上的节点
    （相关度 ${score}/100，双阈值要 ≥10 才中置信、≥40 才高置信），当前数据源里也取不到数（${rows} 行）。
    它问的领域和这份材料不是一回事 —— <b>相关度低不是"答错了"，是"没在这个材料里"</b>。
    <div style="margin-top:5px">
      <span class="hint">要学这个领域，直接导入对应材料（仓库文件夹 / 文件）再点「▶ 启动 Voyager」——
      一次只服务一个工作区，不需要新建场景。</span>
    </div></div>`;
}
/* 人导入在图谱侧落了什么 —— 收录判据是「VLML 能不能用表内覆盖的工具集
   完成这个工作」，收录的落法是**建边、不建节点**。这一行让它在面板上可查，
   而不是只在终端日志里出现一次。 */
function graphLinkChips(gl){
  if(!gl || !Object.keys(gl).length) return '';
  if(!gl.covered)
    return '<span class="chip bad">图谱未收录：工具集覆盖不到</span>';
  return '<span class="chip ok">图谱已收录（建边，不建节点）</span>'
    + `<span class="chip grey">覆盖层 ${ESC(gl.cover_level||'')}`
    + `｜洞察 ${(gl.insights||[]).length}/${ESC(gl.insights_total||0)} 个`
    + `｜表 ${(gl.tables||[]).length} 张</span>`;
}

function learnBar(s){
  const R=(s.learn_recent||[])[0];
  const running=!!s.learn_running;
  if(!running && !R && !s.learn_error) return '';
  /* 四步证据逐条摆出来（用户验收线）：拆分 → 补缺口 → 取数真行数 → 技能/里程碑。
     只给一个 status 的话，人分不清断在第五步（没取到数）还是第二步（没抓手）。 */
  let ev='';
  if(R){
    const cn=R.counts||{}, fo=R.forged||{}, ex=R.execution||{};
    /* 用户 2026-11-12 口径：只摆两个**可预见**的闭合点。
       维度条数是动态的（同一句话换个问法就变），不能拿来当进度。 */
    const chips=[
      `<span class="chip ${R.phase1_passed?'ok':'grey'}">Phase1 ${R.phase1_passed?'spec 已冻结':'未冻结'}</span>`,
      `<span class="chip ${R.phase2_verified?'ok':(R.phase2_submitted?'warn':'grey')}">`
        + (R.phase2_verified?'Phase2 与 spec 一致'
            :(R.phase2_submitted?'Phase2 已交 · 仍有对不上的':'Phase2 未交'))
        + (R.phase2_rounds>1?`（改了 ${ESC(R.phase2_rounds)} 轮）`:'') + `</span>`,
      `<span class="chip ${ex.ok?'ok':'bad'}">取数 ${ESC(ex.rows||0)} 行` +
        `｜真调用 ${ESC(ex.real_calls||0)}/${ESC(ex.calls||0)}</span>`,
      `<span class="chip ${R.skill?'ok':'grey'}">技能 ${R.skill?ESC(R.skill):'未结晶'}</span>`,
      `<span class="chip ${(R.milestone||{}).unlocked?'ok':'grey'}">里程碑 ` +
        `${(R.milestone||{}).unlocked?'已解锁':'未解锁'}</span>`,
    ].filter(Boolean).join(' ');
    ev=`<div style="margin-top:6px">${chips}</div>`
      + (R.why?`<div style="font-size:12px;color:var(--muted);margin-top:4px">${ESC(R.why)}</div>`:'');
  }
  const st=R?String(R.status||''):'';
  const tone=running?'info':(st==='learned'?'info':(st==='no_data'||st==='unreachable'||st==='failed'?'':'info'));
  return `<div class="note ${tone}" style="margin:8px 0">
    <b>${running?'Voyager 正在自己学：':(R?'上一轮自学结果：':'学习闭环')}</b>
    ${ESC(running?(s.learn_question||''):(R?R.question:''))}
    ${running?`<span class="chip grey">已跑 ${ESC(s.learn_elapsed)} 秒</span>`:
      (st?`<span class="chip ${st==='learned'?'ok':(st==='no_data'?'warn':'bad')}">${ESC(st)}</span>`:'')}
    <div style="font-size:12px;color:var(--muted);margin-top:4px">
      点「▶ 发布并开始学习」就开跑，不走消费侧（不先派给 Voyager「答一遍」）。
      每一遍：拆骨架 → <b>Phase1</b> 追问到 spec 冻结 → <b>Phase2</b> 交出可跑的
      finalCode（跑不通或与 spec 对不上就<b>带着评论重写</b>，最多 3 轮）→
      <b>Phase3</b> 对账两端、逐条解释、修对不上的地方 → 配方进技能库 +
      解锁里程碑，<b>每轮落一行 run_log（轨迹页可见）</b>。
      失败照实报：没过关就不写技能库、不解锁，但提交物照样进 Phase3 解释。
    </div>${ev}</div>`;
}

/* 后台任务进度条：消费要 1～3 分钟，没有这一条人只看到按钮空转，
   分不清「在跑」和「死了」（用户实测就问过"怎么回答不出来"）。 */
function coachRunningBar(s){
  if(!s.coach_running)
    return s.coach_error
      ? `<div class="note" style="margin:8px 0"><b>上一次消费挂了：</b>${ESC(s.coach_error)}</div>`
      : '';
  return `<div class="note info" style="margin:8px 0">
    <b>Voyager 正在回答：</b>${ESC(s.coach_question||'')}
    <span class="chip grey">已跑 ${ESC(s.coach_elapsed)} 秒</span>
    <div style="font-size:12px;color:var(--muted);margin-top:4px">
      要串十几次模型调用（找配方 → 回填实体值 → 取数 → 让 VLML 独立取一遍 →
      比对两边做法 → LLM 产出叙事洞察），通常 1～3 分钟。
      跑完会自动出现在下面导入历史里。
    </div></div>`;
}

/* 受理回执：明确告诉人「它在跑，别盯着等」，并把它要去哪儿看结果说清楚。 */
function renderImportBusy(res){
  const box=document.getElementById('result'); if(!box) return;
  box.innerHTML=`<div class="note info" style="margin-top:10px">
    ${res.busy
      ? `<b>上一句还在回答中</b>：${ESC(res.error||'')}`
      : `<b>已受理，Voyager 正在回答</b>
         <div style="margin-top:5px">${ESC(res.message||'')}</div>
         <div style="margin-top:6px;font-size:12px;color:var(--muted)">
           它这会儿在串行做：找已学配方 → 回填这句话的实体值 → 取数 →
           让 VLML 独立取一遍 → 比对两边的做法 → LLM 产出叙事洞察。
           十几次模型调用，所以要一两分钟。</div>`}
  </div>`;
}

function renderImportResult(res){
  const box=document.getElementById('result'); if(!box) return;
  if(!res){ box.innerHTML=''; return; }
  if(!res.ok){ box.innerHTML=`<div class="note" style="margin-top:10px"><b>导入失败：</b>${ESC(res.error)}</div>`; return; }
  const facts=(res.facts||[]).map(f=>
    `<div class="fact"><b>${ESC(f.subject||'')}</b> · ${ESC(f.dimension||'')} =
       <span class="fv">${ESC(f.value)}</span>${f.base!==null&&f.base!==undefined?` <span style="color:var(--muted)">(base ${ESC(f.base)})</span>`:''}</div>`).join('');
  // Voyager 能力消耗披露（mode=voyager 时返回）：记忆召回了几条、复用了哪些技能、场景是否注入
  const vc=res.voyager_context||null;
  const vcChips=vc?`
      <span class="chip ok">Voyager 回答</span>
      <span class="chip grey">记忆召回 ${vc.memory_count||0} 条</span>
      ${(vc.skills||[]).length?`<span class="chip grey">复用技能：${vc.skills.map(k=>ESC(k.name)).join('、')}</span>`:''}
      ${vc.scene?'<span class="chip grey">场景已注入（工具/表 base·ext 分层）</span>':''}`:'';
  // —— 消费通路（voyager_vlml/consume.py）专属披露 ——
  // 关键区别：旧通路只是把技能库的**文字**塞进 VLML 编排的 prompt（注入话术）；
  // 消费通路是真的把已固化的 SQL 配方拿出来、把这句话的实体值回填进配方的计算、
  // 执行取数。所以「用了能力还是现做的」必须能一眼看出来 —— 自称用了能力但其实
  // 张冠李戴，比承认现做更糟。
  const isConsume = res.mode === 'voyager_consume';
  const consumeChips = isConsume ? `
      <span class="chip ${res.reused?'ok':'warn'}">${res.reused?'复用已学配方':'现场现做（库里没有可用配方）'}</span>
      <span class="chip grey">技能库命中 ${res.skills_found||0} 条</span>
      ${(res.used_skills||[]).length?`<span class="chip grey">用了：${res.used_skills.map(k=>ESC(k.name||k.topic||'')).join('、')}</span>`:''}
      <span class="chip grey">工具调用 ${res.ok_tool_calls||0}/${res.tool_calls||0} 成功 · ${res.rows||0} 行</span>` : '';
  const pct = v => (v===null||v===undefined) ? '—' : Math.round(v*100)+'%';
  // 覆盖率/掌握度**只在消费侧算**（练习不判分）—— 这是给人看的质量数。
  // ⚠️ 判法（2026-10-13 用户定）：不再要求两边各自整理成 {subject,dimension,value}
  // 再按键比对 —— 而是**直接比对 Voyager 与 VLML 的方法和做法**（两边各写一条 SQL
  // 取数，把 SQL 与结果摊开让 LLM 判是不是同一口径、有没有硬伤），一致度即覆盖率。
  // 旧判法最脆的就是"整理"那步：答案是一张表时压不进单值事实 → 两边都交白卷 →
  // 覆盖率落空 + 回答为空（实测取到 85 行却显示"回答不出来"）。
  const cmpBlock = isConsume ? `
    <div style="margin-top:9px">
      <b>两边做法的比对（判分的依据，得让人看见"凭什么判这个分"）：</b>
      ${res.method_compare
        ? `<div style="margin-top:4px">
             <span class="chip ${res.same_metric?'ok':'warn'}">${res.same_metric?'同一个量':'两边算的不是同一个量'}</span>
             <span class="chip ${res.referee_compared?(res.voyager_sound?'ok':'bad'):'grey'}">${res.referee_compared?(res.voyager_sound?'Voyager 做法站得住':'Voyager 做法有硬伤'):'未对照（裁判没跑成）'}</span>
             <span class="chip grey">一致度 ${pct(res.coverage)}</span>
           </div>
           ${res.method_reason?`<div style="margin-top:4px;font-size:12px"><b>依据：</b>${ESC(res.method_reason)}</div>`:''}
           ${res.method_diff?`<div style="margin-top:4px;font-size:12px"><b>两边差别：</b>${ESC(res.method_diff)}</div>`:''}
           <div style="margin-top:4px;font-size:12px;color:var(--muted)">
             Voyager 那一路取到 ${ESC(res.rows||0)} 行，VLML 独立那一路取到 ${ESC(res.referee_rows||0)} 行。
             裁判只取数、不整理 —— 比对交给 LLM 直接看两条 SQL 和取到的东西。
           </div>`
        : `<div style="margin-top:4px;font-size:12px;color:var(--muted)">
             ${ESC(res.skipped||'VLML 那一路没取到数，没有第二路做法可比 —— 覆盖率如实留空，不拿 0 分冒充')}</div>`}
      ${res.coverage_referee!==null&&res.coverage_referee!==undefined
        ? `<div style="margin-top:4px;font-size:12px"><b>硬尺子（题库 rubric）：</b>
             <span class="chip ${res.coverage_referee>=0.95?'ok':'warn'}">覆盖率 ${pct(res.coverage_referee)}</span>
             ${ESC(res.verdict_referee||'')} —— 与主尺子并列，不互相折算。</div>`:''}
    </div>` : '';
  const scoreBlock = isConsume ? `
    <div style="margin-top:9px">
      <b>覆盖率（质量分，只给人看，不回灌 Voyager）：</b>
      ${res.coverage===null||res.coverage===undefined
        ? `<div style="margin-top:4px">未计算 —— ${ESC(res.skipped||'无第二路做法可比')}</div>`
        : `<div style="margin-top:4px">
             ${res.judged
               ? `<span class="chip ${res.coverage>=0.95?'ok':(res.coverage>0?'warn':'bad')}">一致度 ${pct(res.coverage)}</span>`
               : `<span class="chip grey">口径不同，没真正判到</span>`}
             <span class="chip grey">得分 ${ESC(res.score)}</span>
             <span class="chip grey">判读 ${ESC(res.verdict||'')}</span>
             ${res.judge?`<span class="chip grey">尺子 ${ESC(res.judge)}</span>`:''}
           </div>
           ${res.judged?'':'<div style="margin-top:4px;font-size:12px;color:var(--muted)">两边算的不是同一个量（多数是裁判自己跑偏），这一问<b>没真正判到</b> —— 不是 Voyager 答错，不按 0 分算它。</div>'}
           ${(res.covered_points||[]).length?`<div style="margin-top:4px;font-size:12px"><b>覆盖到：</b>${res.covered_points.map(c=>ESC(c)).join('；')}</div>`:''}
           ${(res.missing_points||[]).length?`<div style="margin-top:4px;font-size:12px"><b>没覆盖：</b>${res.missing_points.map(c=>ESC(c)).join('；')}</div>`:''}
           ${(res.unjudgeable||[]).length?`<div style="margin-top:4px;font-size:12px"><b>硬伤：</b>${res.unjudgeable.map(c=>ESC(c)).join('；')}</div>`:''}`}
      ${cmpBlock}
    </div>` : '';
  box.innerHTML=`<div class="note info" style="margin-top:10px">
    <div style="margin-bottom:6px">
      ${res.topic_id?`<span class="chip ok">归入 ${ESC(res.topic_id)}</span>`:'<span class="chip bad">没归入任何题 → 不驱动出题</span>'}
      ${res.topic_hit?`<span class="chip grey">命中「${ESC(res.topic_hit)}」</span>`:''}
      <span class="chip grey">validated_target: ${ESC(res.validated_target)}</span>
      ${(res.trajectory||[]).length?`<span class="chip grey">VLML 解析：${(res.trajectory||[]).map(t=>ESC(t)).join(' → ')}</span>`:'<span class="chip bad">未取到数据</span>'}
      ${vcChips}
      ${consumeChips}
      ${graphLinkChips(res.graph_link)}
    </div>
    ${isConsume&&res.reuse_note?`<div style="margin-top:5px;font-size:12px"><b>配方怎么用的：</b>${ESC(res.reuse_note)}</div>`:''}
    ${isConsume?`<div style="margin-top:9px"><b>掌握（能力集合）：</b>
      ${res.mastered
        ? `<span class="chip ok">已掌握 —— 这题进能力库了</span>
           <span class="chip grey">累计 ${ESC(res.mastered_total)} / ${ESC(res.ability_total)} 题</span>
           ${res.skill_action?`<span class="chip grey">${ESC(res.skill_action)}</span>`:''}
           <div style="font-size:12px;color:var(--muted);margin-top:4px">
             掌握的判据是「答没答出来」（调通工具 + 取到数 + 产出回答），
             <b>不是覆盖率</b> —— 覆盖率只作质量参考，不参与"这题算不算学会"。</div>`
        : `<span class="chip bad">没答出来，未进能力库</span>
           <div style="font-size:12px;color:var(--muted);margin-top:4px">${ESC(res.skipped||'')}</div>`}
    </div>`:''}
    ${scoreBlock}
    ${res.skipped?`<div><b>未产出解释：</b>${ESC(res.skipped)}</div>`:''}
    ${(res.errors||[]).length?`<div style="margin-top:5px"><b>工具报错：</b>${(res.errors||[]).map(e=>ESC(e)).join('；')}</div>`:''}
    ${isConsume&&res.answer?`<div style="margin-top:9px"><b>Voyager 的回答：</b>
      <div class="nocmp-body" style="margin-top:4px">${ESC(res.answer)}</div></div>`:''}
    ${isConsume?sourcesBlock(res):''}
    ${isConsume&&res.narrative_skipped?`<div style="margin-top:5px;font-size:12px;color:var(--muted)">
      <b>叙事层没产出</b>（答案退回原始取数结果）：${ESC(res.narrative_skipped)}</div>`:''}
    ${facts?`<div style="margin-top:9px"><b>VLML 的解析 · 取到的事实（${(res.facts||[]).length} 条）：</b>${facts}
      <div style="font-size:12px;color:var(--muted);margin-top:5px">
        ${isConsume
          ? '这些事实是 Voyager 用已积累的能力取出来的（照旧披露，给你看它取到了什么）。<b>判分不用它按键比对</b> —— 上面那条一致度是把 Voyager 与 VLML 两边的 SQL 和取到的数摊开比出来的，答案是一张表、压不进单值事实也照样判。'
          : '注意：这条通路的 facts <b>没有经过裁判核对</b>，维度名也是开放式自取的 —— 它比 Voyager 那条过了裁判的事实软，不能拿来当判据。'}</div></div>`:''}
    ${(vc&&vc.memory_texts&&vc.memory_texts.length)?`<div style="margin-top:9px;font-size:12px;color:var(--muted)">
      <b>本次消费的记忆（mem0 召回）：</b><ul style="margin:4px 0 0 16px">${vc.memory_texts.map(m=>`<li>${ESC(String(m).slice(0,120))}</li>`).join('')}</ul></div>`:''}
    ${res.narrative?`<div class="nocmp" style="margin-top:9px">
        <div class="nocmp-tag">不参与比对 · comparable: false</div>
        <div class="nocmp-body">${ESC(res.narrative)}</div>
        ${(res.insights||[]).length?`<div class="nocmp-sub"><b>洞察：</b><ul>${res.insights.map(i=>`<li>${ESC(i)}</li>`).join('')}</ul></div>`:''}
        ${(res.caveats||[]).length?`<div class="nocmp-sub"><b>数据局限：</b>${res.caveats.map(c=>ESC(c)).join('；')}</div>`:''}
      </div>`:''}
    <div style="margin-top:8px;font-size:12px">这条已写进因果时间线（control fact），出题器看得见。
      ${isConsume
        ? '<b>消费这条线算覆盖率与掌握度</b>（练习不判分）—— 这两个数只给人看，不回灌 Voyager 的反馈。'
        : '<b>不计入掌握度</b> —— 出题器看得见，评分看不见。'}</div>
  </div>`;
}

let pvTimer=null;
function preview(){
  const q=document.getElementById('q'); const hit=document.getElementById('hit');
  if(!q||!hit) return;
  const v=q.value.trim();
  if(!v){ hit.className='hit'; hit.textContent='归类结果会在这里显示。'; return; }
  fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:v})})
    .then(r=>r.json()).then(d=>{
      if(d.matched){ hit.className='hit';
        hit.innerHTML=`将归入 <b>${ESC(d.topic_id)}</b>（命中「${ESC(d.hit)}」）· ${ESC(String(d.question).slice(0,40))}…`;
      }else{ hit.className='hit no';
        hit.innerHTML=`<b>没匹配到任何题</b> —— 仍会记录并解释，但 topic_id 为空，出题器读不到，<b>不驱动出题</b>。可在下拉里手动指定。`;
      }
    }).catch(()=>{});
}

/* 【已停用 2026-11-14】原「交给 Voyager 回答」＝消费线入口。
   用户拍板：进步改由**题内缺口序列**观测（同构助产士），人导入页改为
   New exercise（写题面 → 拆骨架 → 发布）。这条通路（/api/coach → consume）
   的按钮已解绑；函数留着只因为 renderImportResult / 埋点文案还引用它，
   删掉会连带断掉「导入历史」那段渲染。 */
let busy=false;
async function submitImport(){
  if(busy) return;
  const q=document.getElementById('q'); if(!q) return;
  const v=q.value.trim();
  if(!v){ document.getElementById('hit').innerHTML='<b>先写一句话再提交。</b>'; return; }
  const btn=document.getElementById('go');
  busy=true; btn.disabled=true; btn.innerHTML='<span class="spin"></span>Voyager 正在回答…';
  window.__changedFrom=(window.__lastRec||{}).topic_id||'';
  const sel=document.getElementById('topic');
  const picked=sel ? sel.value : '';
  try{
    const r=await fetch('/api/coach',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question:v, topic_id:picked, mode:'voyager'})});
    const res=await r.json();
    // 后台执行：受理了就**不要**同步等结果 —— 结果会随 4 秒轮询进导入历史。
    // 实测教训：同步等 2～5 分钟会把浏览器/Caddy 连接拖到超时，人只看到
    // 按钮卡在「正在回答」，以为 Voyager 答不出来（其实它在跑）。
    if(res.accepted||res.busy){
      renderImportBusy(res);
      uiEvent('human_import',{问题:v.slice(0,300),模式:'消费（后台执行）',
        手动指定的题: picked||'未指定'}, res.busy?('被拒：上一句还在回答中'):('已受理，后台跑'));
      if(!res.busy){ q.value=''; q.blur(); }
      document.getElementById('hit').textContent='归类结果会在这里显示。';
      await load();
      return;
    }
    renderImportResult(res);
    uiEvent('human_import',
      {问题: v.slice(0,300),
       模式: 'voyager（记忆+技能+场景注入）',
       手动指定的题: picked || '未指定（走关键词自动归类）'},
      res.ok
        ? ('归入 '+(res.topic_id||'未归入任何题')
           +(res.topic_hit?'（命中「'+res.topic_hit+'」）':'')
           +' · '+(res.facts||[]).length+' 条事实 · 编排 '+(res.trajectory||[]).join(' → ')
           +' · 记忆召回 '+(((res.voyager_context||{}).memory_count)||0)+' 条'
           +' · validated_target=false 不计掌握度')
        : ('失败：'+(res.error||'未知')));
    q.value=''; q.blur();   // 失焦：让下一轮轮询照常刷新导入历史（焦点在输入框上时渲染是被 hold 住的）
    document.getElementById('hit').textContent='归类结果会在这里显示。';
    await load();
  }catch(e){ renderImportResult({ok:false,error:String(e)}); }
  finally{ busy=false; btn.disabled=false; btn.textContent='交给 Voyager 回答'; }
}

/* ---------------- 驾驶舱控制 ---------------- */
/* ---------------- 界面交互埋点 ----------------
   用户要求导出的 md「一定要具体到界面交互（按了什么键，然后选了什么难度）」。
   所以每一次按键、每一个下拉选择都落盘成 ui_events.jsonl，导出时排在最前。 */
const TAB_LABEL = {overview:'概览', practice:'练习', import:'人导入',
                   trace:'轨迹', skill:'技能·因果', graph:'知识图谱', voyager:'MC 真实学习', adaptive:'自适应练习', console:'运行日志', pchat:'进度问答'};
function uiEvent(kind, detail, result){
  try{
    fetch('/api/ui-event',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({kind:kind, detail:detail||{}, result:result||''})});
  }catch(e){}
}

async function runJob(job, opts){
  opts=opts||{};
  const b=opts.btnId?document.getElementById(opts.btnId):null;
  if(b&&b.disabled) return;
  // ⚠️ rounds 的默认值必须是 **0（不停）**，不是 3。
  //    写成 `opts.rounds||3` 时，传 0 会被 `||` 当成假值换成 3 —— 面板上勾了
  //    「不停」，发过去的还是 3，一次启动只跑 3 道题就退出，29 个节点的图谱
  //    根本跑不完（用户原话：「原 voyager 学习是不带停的；你现在三轮就结束了」）。
  const body={job:job, mode:opts.mode||'once',
              rounds:(opts.rounds!==undefined&&opts.rounds!==null)?+opts.rounds:0,
              units:+(opts.units||1), transfer:+(opts.transfer||0),
              use_scope:opts.use_scope!==false};
  if(b){ b.disabled=true; b.innerHTML='<span class="spin"></span>'+ESC(opts.busyText||'启动中…'); }
  try{
    const r=await fetch('/api/pilot/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)});
    const d=await r.json();
    uiEvent('pilot_start',
      {任务:d.job_label||job, 单元:body.units, 迁移对照:body.transfer||'不考', 模式:body.mode},
      d.ok?('启动成功 pid '+d.pid+' · '+(d.job_label||job)):('失败：'+(d.error||'未知')));
    if(!d.ok){ alert('启动失败：'+(d.error||'未知')); }
    else { TAB='console'; localStorage.setItem('mve_tab',TAB); }
    await load();
  }catch(e){
    uiEvent('pilot_start',{任务:job},'异常：'+e);
    alert('启动失败：'+e);
  }
  finally{ if(b){ b.disabled=false; b.textContent=opts.btnText||'▶ 启动 Voyager'; } }
}
async function pilotStart(){
  // 没有难度 / 选题键钮了：选题由练习范围决定（伴学 onboarding.md:82）。
  //   rounds = 0 → **不停**（同构原版 voyager.py:326 的 while True：
  //     学习循环不带停，只有点停止 / 跑满 --max-iterations 才退）
  //   rounds = 1 → 只跑一轮看看
  // 有练习范围时强制 1：范围把题钉成一道，不停只会把同一道题反复跑，
  // 进度永远停在 0/1（pilot.py:_loop 里也有同一道闸，面板这里先锁掉开关）。
  const sc=window.__scope||{active:false};
  const ck=document.getElementById('ckContinuous');
  const rounds=(ck&&ck.checked&&!sc.active)?0:1;
  await runJob('practice',{mode:'loop', rounds:rounds, use_scope:true,
    btnId:'runStart', btnText:'▶ 启动 Voyager', busyText:'启动中…'});
}
/* MC 思路学习 → 自动启动 MVE 全流程：
   先 POST /api/mc-learn（提炼 + 入库，同步、快），成功后再触发标准练习循环
   （= 现有「▶ 启动 Voyager」做的事，pilot/start）。两步在同一按钮里串起来，
   对应「MC 学思维 → 入库 → 正常启动 MVE」。 */
async function mcLearnThenRun(){
  const b=document.getElementById('mcLearn');
  if(!b||b.disabled) return;
  if(window.__pilot&&window.__pilot.running){
    alert('MVE 已经在跑了（pid '+window.__pilot.pid+'）。\n\n'
      +'MC 思路学习已就绪，等它跑完再点，或先点「停止」。');
    return;
  }
  b.disabled=true; b.innerHTML='<span class="spin"></span>MC 学习中…';
  try{
    const r=await fetch('/api/mc-learn',{method:'POST',
      headers:{'Content-Type':'application/json'},body:'{}'});
    const d=await r.json();
    if(!d.ok){ alert('MC 思路学习失败：'+(d.error||'未知')); return; }
    uiEvent('mc_learn',
      {提炼原则:d.principles, 入库:d.store.mc_thinking_count, 密度:d.densities},
      'MC 思路学习完成，已入库');
    // 自动启动正常 MVE 全流程（标准练习循环）
    // 与「▶ 启动 Voyager」同一套规则：默认**不停**（rounds 0），
    // 有练习范围时才退化成一轮（范围把题钉死，不停等于反复跑同一道题）。
    const _ck=document.getElementById('ckContinuous');
    const _sc=window.__scope||{active:false};
    await runJob('practice',{mode:'loop',
      rounds:(_ck&&_ck.checked&&!_sc.active)?0:1,
      use_scope:true, btnId:'runStart', btnText:'▶ 启动 Voyager',
      busyText:'启动中…'});
  }catch(e){ alert('MC 学习失败：'+e); }
  finally{ if(b){ b.disabled=false; b.textContent='🧠 启动 MC 思路学习 → MVE'; } }
}
async function startPlacement(){
  const n=window.__skills||0;
  if(n>0){
    const ok=confirm('技能库里已经有 '+n+' 条程序。\n\n'
      +'摸底测的是「零基础上的真实水平」。带着技能库去考，考出来的是「练过之后的水平」，'
      +'而且会覆盖掉原来那份零基础的摸底值（画像就废了）。\n\n'
      +'点确定：先清库（自动备份到 mve/_backup_*）再摸底；点取消：不摸。');
    if(!ok) return;
    try{
      const r=await fetch('/api/reset',{method:'POST'});
      const d=await r.json();
      if(!d.ok){ alert('清库失败：'+(d.error||'未知')); return; }
      uiEvent('reset_all',{原因:'摸底前清库', 清库前技能数:n},'已清库（已备份）');
    }catch(e){ alert('清库失败：'+e); return; }
  }
  await runJob('placement',{btnId:'btnPlacement',
    btnText:'摸底（全库撤图谱考一遍）', busyText:'摸底中…'});
}
/* 结业考：和摸底**前提相反** —— 摸底要空库（测零基础），结业考要有库
   （测"学到现在，撤掉支架还剩多少"）。它落盘 kind="final"，不覆盖摸底基线，
   所以摸底那一列跑完结业考后还在。 */
async function startFinal(){
  const n=window.__skills||0;
  if(!n){
    alert('技能库是空的 —— 结业考测的是「学到现在还剩多少」，没学就没得考。\n\n'
      +'先跑「学习单元」攒技能，或先点「摸底」测零基础水平。');
    return;
  }
  await runJob('final',{btnId:'btnFinal',
    btnText:'结业考（带技能库再考一遍）', busyText:'结业考中…'});
}
async function pilotStop(){
  const b=document.getElementById('runStop'); if(b.disabled) return;
  b.disabled=true; b.textContent='停止中…';
  const p=window.__pilot||{};
  try{
    await fetch('/api/pilot/stop',{method:'POST'});
    await load();
    uiEvent('pilot_stop',
      {停止时模式: p.mode||'—', 已跑题数: p.iterations||0, 选题: p.topic||'—'},
      '已停止');
  }
  catch(e){ alert('停止失败：'+e); }
  finally{ b.disabled=false; b.textContent='■ 停止'; }
}

/* ---------------- Voyager 真实学习控制 ---------------- */
async function voyagerStart(){
  const b=document.getElementById('voyStart'); if(!b||b.disabled) return;
  b.disabled=true; b.innerHTML='<span class="spin"></span>启动中…';
  try{
    const r=await fetch('/api/voyager/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const d=await r.json();
    uiEvent('voyager_start',
      {mc_port:d.mc_port||25565, 上限:d.max_iterations||''},
      d.ok?('启动成功 pid '+(d.pid||'')):('失败：'+(d.error||'未知')));
    if(!d.ok){ alert('启动失败：'+(d.error||'未知')); }
    else { TAB='voyager'; localStorage.setItem('mve_tab',TAB); }
    await load();
  }catch(e){ uiEvent('voyager_start',{},'异常：'+e); alert('启动失败：'+e); }
  finally{ if(b){ b.disabled=false; b.textContent='▶ 启动学习'; } }
}
async function voyagerStop(){
  const b=document.getElementById('voyStop'); if(!b||b.disabled) return;
  b.disabled=true; b.textContent='停止中…';
  const v=window.__voyager||{};
  try{
    const r=await fetch('/api/voyager/stop',{method:'POST'});
    const d=await r.json();
    uiEvent('voyager_stop',{停止时轮次:v.iterations||0, 已学技能:v.completed_count||0},'已停止');
    await load();
  }catch(e){ alert('停止失败：'+e); }
  finally{ if(b){ b.disabled=false; b.textContent='■ 停止'; } }
}
async function voyagerClear(which, label){
  const ok=confirm('清'+(which==='skill'?'库':'记忆')+' = 删除 Voyager 的'
    +(which==='skill'?'已学 JS 技能库（ckpt/skill/）':'课程进度 + 事件历史（ckpt/curriculum/ + ckpt/events/）')
    +'。\n\n会自动备份到 voyager_backup_*。\n\n确定要清'+label+'？');
  if(!ok) return;
  try{
    const r=await fetch('/api/voyager/clear-'+which,{method:'POST'});
    const d=await r.json();
    uiEvent('voyager_clear_'+which,{清了:d.cleared||[], 备份:d.backup||'无'}, d.ok?('已清'+label):'失败');
    alert(d.ok?('已清'+label+'（备份：'+(d.backup||'无')+'）'):('失败：'+(d.error||'未知')));
    await load();
  }catch(e){ alert('清'+label+'失败：'+e); }
}

/* ---------------- 主渲染 ---------------- */
function render(s){
  // 当前场景 id 存全局：取数一律按场景（scene_profile 唯一定义），
  // 前端不再有第二个"数据源选择器"。
  window.__SCENE = ((s.scene||{}).id)||'';
  window.__SCENE_NAME = ((s.scene||{}).name)||'';
  window.__lastRec = s.recommend || {};
  window.__pilot   = s.pilot || {};
  window.__voyager = s.voyager || {};
  window.__skills  = (s.skill_stats||{}).skills || 0;
  // topic_id -> 难度（埋点里要把"选了什么难度"记下来）
  window.__diff = {};
  (s.tasks||[]).forEach(t=>{ if(t.topic_id) window.__diff[t.topic_id]=t.difficulty; });
  window.__difficultyOf = id => window.__diff[id];
  renderHero(s);
  renderNav(s);
  const stage=document.getElementById('stage');
  // ---- 有人在操作表单控件时，不能整页重建 ----
  // 轮询每 4 秒 render 一次，stage.innerHTML 一换，所有控件的值都会被打回
  // HTML 里的默认值。实测中招的控件（同一根因，一次修完）：
  //   learnUnits / learnTransfer —— 永远弹回 4 和 3，选不了别的
  //   q          —— 打到一半的问题消失（上一轮修的）
  //   topic      —— 人导入手动指定的归类被清回「自动匹配」
  //   dbPath     —— 数据库切换的路径输入被清空
  //   刚出的解释结果（#result）也被洗掉 —— 提交进行中（busy）同样跳过。
  // 人把焦点挪开（点了页面别处）后，下一轮轮询照常刷新，数据不会少。
  const af=document.activeElement;
  const FORM=['SELECT','INPUT','TEXTAREA'];
  const holdingForm = af && af.tagName && FORM.includes(af.tagName)
                      && stage.contains(af);
  if(!(TAB==='import' && busy) && !holdingForm){
    // 展开态也要带过去（2026-11-10 用户：「展开一下就收回去了，应该是我再点它
    // 它才关上」）。<details> 的 open 是 DOM 状态、不在 HTML 字符串里，轮询每 4 秒
    // 重建 stage.innerHTML 就会把展开的折叠块全部打回默认 —— 不是超时收回。
    // 这里按 data-k 稳定键（会话 req_id / 维度 id / 技能 key）快照，重建后补回。
    const openKeys=new Set(Array.from(stage.querySelectorAll('details[data-k][open]'))
                             .map(el=>el.getAttribute('data-k')));
    // 即便重建，也把用户改过的控件值带过去 —— 焦点在按钮上时轮询照常刷，
    // 不能因为刷新把人刚选的单元数/迁移间隔/草稿重置掉。
    const vals={}, afId=(af&&af.id&&stage.contains(af))?af.id:null;
    stage.querySelectorAll('select[id],input[id],textarea[id]')
      .forEach(el=>{
        // 顶层场景刚切换过：曲线选择器不保留人手选的值，重新默认到新当前场景
        if(window.__curveSceneReset && el.id==='curveScene') return;
        vals[el.id]=el.value;
      });
    window.__curveSceneReset=false;
    stage.innerHTML=(PANELS[TAB]||pOverview)(s);
    if(openKeys.size){
      stage.querySelectorAll('details[data-k]').forEach(el=>{
        if(openKeys.has(el.getAttribute('data-k'))) el.open=true;
      });
    }
    Object.entries(vals).forEach(([id,v])=>{
      const n=document.getElementById(id);
      if(n && v) n.value=v;
    });
    if(afId){
      const n=document.getElementById(afId);
      if(n && n.tagName && FORM.includes(n.tagName)) n.focus();
    }
    fillTopics(s);          // 重建后 #topic 是新元素，得重新填（在恢复值之后跑，keep 才保得住）
    // 曲线主体与选择器对齐：选择器还是本场景 → 面板数据就是本场景的，不用动；
    // 人手选过别的场景/「全部场景」→ 现取一次，别让身子和脑袋不一致。
    const cs3=document.getElementById('curveScene');
    if(cs3 && cs3.value && cs3.value!==((s.scene||{}).id)) switchCurveScene(cs3.value);

    // New exercise 的两个动作：先拆骨架（可审），再发布（沿用那份骨架）。
    // 旧的「交给 Voyager 回答」= 消费线，2026-11-14 起由练习账取代，
    // submitImport 保留但不再绑定按钮（见其注释）。
    const go=document.getElementById('go'); if(go) go.onclick=exPublish;
    const br=document.getElementById('btnRoute'); if(br) br.onclick=exPreview;
    const clr=document.getElementById('clr'); if(clr) clr.onclick=()=>{
      ['q','exTitle','exUnit'].forEach(id=>{const n=document.getElementById(id); if(n) n.value='';});
      const r0=document.getElementById('result'); if(r0) r0.innerHTML='';
      const h0=document.getElementById('hit');
      if(h0) h0.textContent='先写题面；点「生成设问骨架」可以先看这道题会被问成哪几个维度。';
      exSkel=null;
    };
    const qq=document.getElementById('q');
    if(qq){
      // 打字时**不**再自动跑归类（那是旧消费线的 /api/preview）；骨架要人显式点。
      qq.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter')exPublish();});
    }
  }
  const cl=document.getElementById('clearLog'); if(cl) cl.onclick=async()=>{
    try{
      const r=await fetch('/api/pilot/clear-log',{method:'POST'});
      const d=await r.json().catch(()=>({}));
      if(d && d.ok===false){
        // 运行中拒绝清屏（unlink 会把整轮日志连句柄一起丢掉）——必须让人看见
        alert(d.why || '循环还在跑，暂不能清屏');
        return;
      }
      uiEvent('clear_log',{},'运行日志已清空');
    }catch(e){ alert('清屏失败：'+e); }
    await load();
  };
  const bp=document.getElementById('btnPlacement');
  if(bp) bp.onclick=startPlacement;
  const bf=document.getElementById('btnFinal');
  if(bf) bf.onclick=startFinal;
  const bl=document.getElementById('btnLearn');
  if(bl) bl.onclick=()=>runJob('learn',{btnId:'btnLearn', btnText:'跑学习单元',
    busyText:'学习中…',
    units:+((document.getElementById('learnUnits')||{}).value||4),
    transfer:+((document.getElementById('learnTransfer')||{}).value||0)});
  const bes=document.getElementById('btnExamStop'); if(bes) bes.onclick=pilotStop;
  const rn=document.getElementById('refreshNow'); if(rn) rn.onclick=load;
  const ex=document.getElementById('btnExport'); if(ex) ex.onclick=exportMd;
  const dp=document.getElementById('dbProbe'); if(dp) dp.onclick=dbProbe;
  const ds=document.getElementById('dbSwitch');
  if(ds){ ds.onclick=dbSwitch; ds.disabled=!dbProbedOk; }
  const vs=document.getElementById('voyStart');
  if(vs) vs.onclick=voyagerStart;
  const vsp=document.getElementById('voyStop');
  if(vsp) vsp.onclick=voyagerStop;
  const vcl=document.getElementById('voyClearLog');
  if(vcl) vcl.onclick=async()=>{ await fetch('/api/voyager/clear-log',{method:'POST'}); await load(); };
  const vcs=document.getElementById('voyClearSkill');
  if(vcs) vcs.onclick=()=>voyagerClear('skill','库');
  const vcm=document.getElementById('voyClearMemory');
  if(vcm) vcm.onclick=()=>voyagerClear('memory','记忆');
  // 自适应练习页签按钮（按钮只在该页签存在，用 if 守卫，每次 render 都绑）
  const oqn=document.getElementById('oqNext');    if(oqn) oqn.onclick=oqNext;
  const oqs=document.getElementById('oqSubmit');  if(oqs) oqs.onclick=oqGrade;
  const oqr=document.getElementById('oqReveal');  if(oqr) oqr.onclick=oqReveal;
}

/* ---------------- 数据库切换：先验货，再切换 ----------------
   验货必须在切换**之前**：切过去才发现是空库/错库，人已经不知道刚才的数据去哪了。 */
let dbProbedOk=false;
async function dbProbe(){
  const hit=document.getElementById('dbHit'); if(!hit) return;
  const p=document.getElementById('dbPath').value.trim();
  hit.className='hit'; hit.innerHTML='验货中…';
  try{
    const r=await fetch('/api/db/probe',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:p})});
    const d=await r.json();
    dbProbedOk=!!d.ok;
    document.getElementById('dbSwitch').disabled=!dbProbedOk;
    if(!d.ok){ hit.className='hit no'; hit.innerHTML=`<b>不能用：</b>${ESC(d.error||'未知')}`; return; }
    hit.innerHTML=`能用 · <b>${d.tables.length}</b> 张表`
      + ((d.events!==undefined&&d.events!==null)?` · <b>${d.events}</b> 条事件`:'')
      + (d.series_ids&&d.series_ids.length?` · series: ${d.series_ids.map(x=>ESC(x)).join(', ')}`:'')
      + (d.warning?`<br><b style="color:#8a2015">⚠ ${ESC(d.warning)}</b>`:'');
  }catch(e){ hit.className='hit no'; hit.innerHTML='验货失败：'+ESC(e); }
}
async function dbSwitch(){
  const b=document.getElementById('dbSwitch'); if(!b||b.disabled) return;
  const p=document.getElementById('dbPath').value.trim();
  const reset=document.getElementById('dbReset')?.checked;
  if(!confirm((p?('切换到：'+p):'回到默认库')
      +(reset?'，并清空全部学习数据（含旧库派生的实体清单 / schema 缓存，清完自动按新库重查实体）':'')
      +'\n\n切换后需要重启面板进程才生效。继续？')) return;
  b.disabled=true;
  try{
    const r=await fetch('/api/db/switch',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:p, reset:reset})});
    const d=await r.json();
    const hit=document.getElementById('dbHit');
    hit.className='hit';
    hit.innerHTML = d.ok
      ? `<b>已写入配置</b>：${ESC(d.db_path)}`
        +(d.reset_detail
          ? `<br><span class="mono">${ESC(Object.entries(d.reset_detail)
              .filter(([k])=>k!=='切换到'&&k!=='说明')
              .map(([k,v])=>k+'：'+v).join(' · '))}</span>`
          : '')
        +`<br>现在请重启面板进程：
         <span class="mono">lsof -ti:8777 | xargs kill; python mve/dashboard.py</span>`
      : `<b>失败：</b>${ESC(d.error||'未知')}`;
    await load();
  }catch(e){ document.getElementById('dbHit').innerHTML='切换失败：'+ESC(e); }
  finally{ b.disabled=false; }
}

async function exportMd(){
  const b=document.getElementById('btnExport'); if(!b||b.disabled) return;
  b.disabled=true; const old=b.textContent; b.textContent='导出中…';
  try{
    const r=await fetch('/api/export',{method:'POST'});
    const d=await r.json();
    if(!d.ok){ alert('导出失败：'+(d.error||'未知')); return; }
    const blob=new Blob([d.markdown],{type:'text/markdown;charset=utf-8'});
    const a=document.createElement('a');
    a.href=URL.createObjectURL(blob); a.download=d.filename;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(a.href);
  }catch(e){ alert('导出失败：'+e); }
  finally{ b.disabled=false; b.textContent=old; }
}

let lastRev='';
async function load(){
  try{
    const r=await fetch('/api/state?t='+Date.now());
    const d=await r.json();
    if(d.rev) lastRev=d.rev;
    render(d);
  }catch(e){
    document.getElementById('stage').innerHTML='<div class="panel"><div class="empty">加载失败：'+ESC(e)+'</div></div>';
  }
}

document.getElementById('nav').addEventListener('click',e=>{
  const b=e.target.closest('[data-tab]'); if(!b) return;
  // 2026-11-18 导出治理：点在**当前**分区上不算一次交互——
  // 卡顿期连点会把 ui_events 灌成几十条同样的 tab_switch，导出全是噪音。
  if(b.dataset.tab===TAB) return;
  const from=TAB; TAB=b.dataset.tab; localStorage.setItem('mve_tab',TAB);
  uiEvent('tab_switch',{从: TAB_LABEL[from]||from, 切到: TAB_LABEL[TAB]||TAB}, '');
  load();
});
document.getElementById('runStart').onclick=pilotStart;
document.getElementById('mcLearn').onclick=mcLearnThenRun;
document.getElementById('runStop').onclick=pilotStop;

load();
// 2026-11-18 卡顿治理：轮询改轻量 rev 探测——指纹没变不拉全量；
// 任务在跑（busy）或指纹变了才 load()（动作后按钮自带的 await load() 不变）。
async function poll(){
  try{
    const r=await fetch('/api/rev?t='+Date.now());
    const d=await r.json();
    if(d.busy || d.rev!==lastRev){ lastRev=d.rev; load(); }
  }catch(e){}
}
setInterval(poll, 3000);
loadSceneBar();                       // 场景栏：一次性拉齐 + 之后 8s 跟一次（与 /api/scenes 同频）
setInterval(loadSceneBar, 8000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 1→n 场景管理页已撤（2026-10-18 去重）：人导入 + 学习曲线 + 场景切换/新建
# 全部并入主面板（头部场景栏 / 学习曲线场景筛选 / 技能页场景标签），
# 不再维护第二个页面与第二套入口。
# ---------------------------------------------------------------------------


# 消费通路的后台任务状态（进程内存；面板重启即清 —— 与 pilot_state 一个思路，
# 只是它轻到不值得落盘）。
COACH_JOB: dict = {"running": False, "question": "", "started_at": 0.0, "error": ""}

# 学习闭环的后台任务状态（learn_orchestrate.start_learning，分钟级）。
# 和 COACH_JOB 同一个模式：立即受理 → 后台线程 → 结果进 learn_orchestrate
# 的 _LAST + causal_timeline（面板时间线页签可见），前端轮询本状态。
LEARN_JOB: dict = {"running": False, "question": "", "started_at": 0.0,
                   "error": "", "last_status": "",
                   # 重做时记下"背包清了什么"（reset_practice_state 的返回），
                   # 面板据此告诉人"这一遍是从零开始的，不是接着上一遍"。
                   "redo": None}


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1：SSE 的分块编码（Transfer-Encoding: chunked）在 1.0 下无效，
    # 而流式回答必须能"边算边发"。普通接口仍逐个带 Content-Length，不受影响。
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        # 2026-11-18：面板自身 gzip。8443 走 neko-gateway(docker) 不经
        # nginx，此前只有 80 入口有压缩 —— 人到 VPS 链路 ~50KB/s，
        # /api/state 1MB 裸传 20 秒。在源头压，哪个入口进来都生效。
        # 全部响应都是整包 Content-Length（无活流式端点），就地压安全。
        if len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
            import gzip as _gz
            body = _gz.compress(body, 5)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _payload(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    # ---- OpenAI 兼容层已整体删除（2026-11-18 用户指令）----
    # 原 2026-10-28 那套（/v1/models、/v1/chat/completions、SSE 逐字下发、
    # _openai_chat/_openai_stream/_mve_ask/_openai_answer + 对话页
    # chat_page.py）是把「学习能力」包装成 OpenAI model 给外部客户端消费。
    # 现在**人的入口只剩「开始学」一句话**（/api/coach 拆骨架正线）；
    # chat 只保留两处内部通道：phase2_chat（学生内问，喂 r_site）与
    # progress_chat（人的进度问答，只读转述，零写账）。
    def do_GET(self) -> None:  # noqa: N802
        path = _strip_prefix(self.path.split("?")[0])
        # 2026-11-18 用户指令：旧「对话页」（人导入消费线的 R1 形态页）与
        # OpenAI 兼容消费端点（/v1/models、/v1/chat/completions）整体删除 ——
        # 人的入口只剩「开始学」一句话（/api/coach 拆骨架正线）。
        if path in ("/", "/index.html"):
            body = _html().encode("utf-8")
            ctype = "text/html; charset=utf-8"
        elif path == "/api/scene-curve":
            # 1→n：单场景学习曲线（主面板学习曲线的场景筛选口）。
            # 与面板全局曲线同一个函数 _consume_curve 的场景参数，不新造尺子。
            # scene=__all__（或空）= 对比视图：不过滤，各场景曲线同图返回。
            import urllib.parse as _up
            qs = _up.parse_qs(_up.urlsplit(self.path).query or "")
            sid = str((qs.get("scene") or [""])[0]).strip()
            scene_arg = None if sid in ("", "__all__") else sid
            try:
                curve = panel_data._consume_curve(limit=40, scene_id=scene_arg)
                body = json.dumps(
                    {"ok": True, "scene_id": sid or "__all__", "curve": curve,
                     "summary": (panel_data.curve_summary(sid) if scene_arg else {})},
                    ensure_ascii=False, default=str).encode("utf-8")
                ctype = "application/json; charset=utf-8"
            except Exception as e:
                self._json(500, {"error": f"{type(e).__name__}: {e}"[:200]})
                return
        elif path == "/api/rev":
            # 轻量轮询（2026-11-18 卡顿治理）：指纹没变前端就不拉全量 state。
            busy = bool(COACH_JOB["running"] or LEARN_JOB["running"])
            self._json(200, {"rev": panel_data.state_rev(), "busy": busy})
            return
        elif path == "/api/state":
            import time as _t
            # 消费通路在后台跑（要 1～3 分钟），前端靠 4 秒轮询看它跑没跑完。
            # 不把状态喂出来，人就只看到按钮卡在「正在回答」，不知道活着还是死了。
            st = panel_data.build_state()
            st["coach_running"] = bool(COACH_JOB["running"])
            st["coach_question"] = str(COACH_JOB["question"] or "")[:80]
            st["coach_elapsed"] = (int(_t.time() - COACH_JOB["started_at"])
                                   if COACH_JOB["running"] else 0)
            st["coach_error"] = str(COACH_JOB["error"] or "")
            st["rev"] = panel_data.state_rev()
            # 学习闭环（learn_orchestrate）：后台学的状态 + 最近结果摘要。
            st["learn_running"] = bool(LEARN_JOB["running"])
            st["learn_question"] = str(LEARN_JOB["question"] or "")[:80]
            st["learn_elapsed"] = (int(_t.time() - LEARN_JOB["started_at"])
                                   if LEARN_JOB["running"] else 0)
            st["learn_error"] = str(LEARN_JOB["error"] or "")
            st["learn_last_status"] = str(LEARN_JOB["last_status"] or "")
            try:
                import learn_orchestrate as _lo
                # 学习闭环的四步证据都要能在面板上看见（用户验收线）：
                # 拆分（子目标/可达性）→ 补缺口（forge 条数）→ 取数（真行数）
                # → 入库+里程碑（技能键 / unlocked）。只给 status 的话，人看不出
                # 断在第五步（no_data）还是第二步（unreachable）。
                st["learn_recent"] = [
                    {"question": str(r.get("question") or "")[:60],
                     "status": str(r.get("status") or ""),
                     "scene_id": str(r.get("scene_id") or ""),
                     "why": str(r.get("why") or "")[:120],
                     # 用户 2026-11-12 口径：只报两个**可预见**的闭合点，
                     # 不再报「拆分几个 / 闭合几维」——维度条数是动态的。
                     "phase1_passed": bool((r.get("spec_outcome") or {})
                                           .get("phase1_passed")),
                     "phase2_submitted": bool((r.get("spec_outcome") or {})
                                              .get("phase2_submitted")),
                     # 「交了」和「过关」不是一回事：原版学生随时能提交，
                     # 没过关的提交要走 Phase3 逐条解释（用户 2026-11-12）。
                     "phase2_verified": bool((r.get("spec_outcome") or {})
                                             .get("phase2_verified")),
                     "phase2_rounds": int((r.get("spec_outcome") or {})
                                          .get("phase2_rounds") or 0),
                     "phase_nodes": list(r.get("phase_nodes") or []),
                     "execution": {k: (r.get("execution") or {}).get(k)
                                   for k in ("ok", "sql", "calls", "real_calls",
                                             "rows")},
                     "skill": ((r.get("crystallized") or {}).get("entry")
                               or (r.get("crystallized") or {}).get("name") or ""),
                     "milestone": r.get("milestone") or {},
                     "stuck": len(r.get("stuck") or [])}
                    for r in _lo.last_learn_results(5)]
            except Exception:
                st["learn_recent"] = []
            body = json.dumps(st, ensure_ascii=False, default=str).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif path == "/api/graph":
            body = _graph_json().encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif path == "/api/practice-scope":
            body = _scope_json().encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif path == "/api/scenes":
            # 1→n 泛化：场景命名空间视图（列表 + 当前 + 每场景专属节点数）
            body = json.dumps(panel_data.scenes_view(), ensure_ascii=False).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif path == "/api/teaching-graph":
            # 教学图谱（伴学 29 知识点种子 + 前置门控 + 掌握度叠加）。
            # 与 /api/graph（判分侧维度图谱）并存：一个管"下一步学什么"，
            # 一个管"这个维度该调哪个工具"。
            try:
                import topic_graph
                body = json.dumps(topic_graph.get_graph().to_graph_payload(),
                                 ensure_ascii=False).encode("utf-8")
                ctype = "application/json; charset=utf-8"
            except Exception as e:
                self._json(500, {"error": f"{type(e).__name__}: {e}"[:200]})
                return
        else:
            self.send_error(404)
            return
        self._send(200, body, ctype)

    def do_POST(self) -> None:  # noqa: N802
        """写接口。分两组：

        人发意图：/api/preview（只归类，不拉 VLML）、/api/coach（真跑）
        驾驶舱  ：/api/pilot/start|stop|clear-log
        """
        path = _strip_prefix(self.path.split("?")[0])
        payload = self._payload()

        # ---- 外置上传模块（R1 客户端式的「把文件交给它」）----
        # 2026-10-28 用户拍板：「不能这样写死守卫解决，而是做 r1 客户端的
        # 多模态上传文件那些外置模块来让 agent 看到全貌」。
        # 协议刻意用 JSON+base64（不用 multipart）：面板/客户端/curl 都好发，
        # 也避开标准库里已废弃的 cgi 表单解析。
        if path in ("/api/upload", "/api/files"):
            try:
                import base64 as _b64

                import upload as _up
                import scenes as _sc4
                sid = str(payload.get("scene_id") or payload.get("scene")
                          or "").strip() or _sc4.current_scene_id()
                name = str(payload.get("name") or "").strip()
                raw = payload.get("data_b64") or payload.get("data") or ""
                if not raw:
                    self._json(400, {"ok": False, "why": "没有文件内容（data_b64）"})
                    return
                if len(raw) > _up.MAX_BYTES * 2:
                    self._json(413, {"ok": False, "why": "文件太大（>32MB）"})
                    return
                blob = _b64.b64decode(str(raw))
                r = _up.ingest(sid, name, blob)
                r["scene_id"] = sid
                self._json(200 if r.get("ok") else 400, r)
            except Exception as e:
                self._json(500, {"ok": False,
                                 "why": f"{type(e).__name__}: {e}"[:300]})
            return
        if path in ("/api/upload/list", "/api/files/list"):
            try:
                import upload as _up2
                import scenes as _sc5
                sid = str(payload.get("scene_id") or "").strip() \
                    or _sc5.current_scene_id()
                self._json(200, _up2.describe(sid))
            except Exception as e:
                self._json(500, {"error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/preview":
            try:
                import tasks
                topic, hit = tasks.infer_topic(str(payload.get("question") or ""))
                task = tasks.TASKS.get(topic)
                self._json(200, {
                    "topic_id": topic, "hit": hit, "matched": bool(topic),
                    "question": task.question if task else "",
                    "difficulty": task.difficulty if task else 0,
                })
            except Exception as e:
                self._json(500, {"matched": False, "error": f"{type(e).__name__}: {e}"[:200]})
            return

        if path == "/api/route":
            # 1→n 泛化：只做命中判定（双阈值），不执行取数
            # 设计案 §3.1：「命中」= 相似度分数阈值，**不是**人审通过、
            # **也不是**「答对且能对上」。匹配与答案对错无关。
            try:
                q = str(payload.get("question") or "").strip()
                if not q:
                    self._json(400, {"error": "问题是空的"})
                    return
                import routing as _rt
                import scenes as _sc
                sid = str(payload.get("scene_id") or "").strip() \
                    or _sc.current_scene_id()
                m = _rt.route(q, sid)
                # 判定基准：这句话是在跟**哪个场景的哪张图谱**比。不返回它，
                # 面板上就只有一个光秃秃的分数 —— 人无法判断"相关度低"是
                # 问题不好还是**归错了场景**（2026-10-18 实测特斯拉问题踩中）。
                scene_nodes, scene_sample = 0, []
                try:
                    import knowledge_graph as _kg
                    _g = _kg.KnowledgeGraph.load()
                    sn = [n for n in _g.nodes.values()
                          if str(getattr(n, "root_scene", "A") or "A") == sid
                          and str(getattr(n, "kind", "")) != "scene_root"
                          and str(getattr(n, "status", "")) != "deleted"]
                    scene_nodes = len(sn)
                    scene_sample = [str(getattr(n, "label", "") or n.id)
                                    for n in sn[:8]]
                except Exception:
                    pass
                # 知识型问题（怎么养育/如何理解…）本来就没有"库域"可命中 ——
                # 别把它当"归错场景"劝人换场景（2026-10-19 用户实测踩过）。
                _needs_data = True
                try:
                    import task_critic as _tc2
                    import reward as _rw2
                    # has_env 必须传（2026-10-31）：不传 = 恒 False = 所有场景
                    # 都当「不需要取数」，面板这份诊断跟着失真。
                    _needs_data = bool(_tc2.needs_data(
                        q, _rw2.scene_has_env(sid)))
                except Exception:
                    pass
                self._json(200, {
                    "scene_id": sid,
                    "decision": m["decision"],
                    "max_score": m["max_score"],
                    "chunk_hits": m["chunk_hits"],
                    "exact_match": m["exact_match"],
                    "node_id": m["node_id"],
                    "node_label": m.get("node_label", ""),
                    "action": m["action"],
                    "matched_on": m["matched_on"],
                    "scene_nodes": scene_nodes,
                    "scene_sample": scene_sample,
                    # 域外签名：本场景有节点、这句话却一个都对不上 → 大概率问的是
                    # 另一个领域（该引导新建场景，而不是硬塞进当前场景）。
                    "out_of_domain": bool(
                        m["decision"] == "REJECTED"
                        and int(m["max_score"]) < 10
                        and scene_nodes > 0
                        and _needs_data),
                })
            except Exception as e:
                self._json(500, {"error": f"{type(e).__name__}: {e}"[:200]})
            return

        if path in ("/api/scene/switch", "/api/scene/create"):
            try:
                import scenes as _sc
                if path == "/api/scene/switch":
                    sid = str(payload.get("scene_id") or "").strip()
                    if not sid:
                        self._json(400, {"ok": False, "error": "scene_id 为空"})
                        return
                    if sid not in [s["id"] for s in _sc.list_scenes()]:
                        self._json(400, {"ok": False, "error": f"场景 {sid} 不存在"})
                        return
                    _sc.switch_scene(sid)
                    self._json(200, {"ok": True, "current": _sc.current_scene_id()})
                else:  # create
                    name = str(payload.get("name") or "").strip()
                    if not name:
                        self._json(400, {"ok": False, "error": "场景名不能为空"})
                        return
                    desc = str(payload.get("description") or "")
                    ds = str(payload.get("data_source") or "")
                    new_id = _sc.create_scene(name, description=desc, data_source=ds)
                    # 硬拒绝项校验：可执行动作（取数原语）= 0 → 不许插入
                    # 设计案「可执行动作缺了不许插入」的原文含义：动作本身缺席，
                    # Voyager 在这个场景里什么数据都取不到。
                    try:
                        import orchestrate as _orch
                        av = _orch.init_scene(new_id, data_source=ds)
                        allowed = bool(av.get("insert_allowed"))
                        breason = str(av.get("blocking_reason") or "")
                    except Exception as e:
                        allowed, breason = True, f"动作注册检查失败：{type(e).__name__}: {e}"
                    if not allowed:
                        # 拒绝插入：撤掉刚建的命名空间，别留一个跑不起来的空场景
                        try:
                            _sc.delete_scene(new_id)
                        except Exception:
                            pass
                        self._json(400, {"ok": False,
                                         "error": f"拒绝插入场景 {new_id}：{breason}",
                                         "blocked": True})
                        return
                    self._json(200, {"ok": True, "scene_id": new_id,
                                     "current": _sc.current_scene_id(),
                                     "action_count": len(
                                         __import__("actions").entries(new_id))})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
            return

        if path == "/api/scene/reset":
            # 清当前场景的全部学习账（2026-10-26 用户：「清场景清彻底，连
            # 账本都清掉」后重学）。走 reset_all --scene：课程账/骨架/锚点
            # 目录整删、日志/图谱按场景过滤重写、本场景配方逐条 delete_skill；
            # 自带备份到 _backup_*；循环在跑会拒绝（rc!=0 → 409）。
            try:
                import scenes as _sc_r
                import reset_all as _ra
                sid = str(payload.get("scene_id") or
                          _sc_r.current_scene_id() or "A").strip()
                if sid not in [s["id"] for s in _sc_r.list_scenes()]:
                    self._json(400, {"ok": False, "error": f"场景 {sid} 不存在"})
                    return
                _old = sys.argv[:]
                sys.argv = ["reset_all.py", f"--scene={sid}"]
                try:
                    rc = _ra.main()
                finally:
                    sys.argv = _old
                if rc != 0:
                    self._json(409, {"ok": False,
                                     "error": "还有任务在跑，已拒绝清档 —— 先点「■ 停止」，"
                                              "等循环收尾完（约 1 分钟）再清"})
                    return
                self._json(200, {"ok": True, "scene_id": sid})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/actions/switch":
            # 猫爪总开关（同构 neko 的插件 toggle）：按能力组拨开/关。
            # 关掉不是删条目 —— 执行层拿不到、调用被拒，拨回来即恢复。
            try:
                import actions as _acts
                sid = str(payload.get("scene") or payload.get("scene_id") or "").strip()
                grp = str(payload.get("group") or "").strip()
                if not sid or not grp:
                    self._json(400, {"ok": False, "error": "scene/group 不能为空"})
                    return
                on = bool(payload.get("enabled", True))
                self._json(200, {"ok": True, **_acts.set_switch(sid, grp, on)})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
            return

        if path == "/api/coach":
            q = str(payload.get("question") or "").strip()
            if not q:
                self._json(400, {"ok": False, "error": "问题是空的"})
                return
            mode = str(payload.get("mode") or "voyager").strip().lower()
            if mode != "voyager":
                # 旧通路（纯 VLML 解释）快（~30s），保持同步。
                try:
                    import asyncio
                    import coach  # 会拉起 VLML 环境（chdir + 打补丁），按需加载
                    res = asyncio.run(coach.explain(q, voyager=False))
                    res["ok"] = True
                except Exception as e:
                    self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
                    return
                self._json(200, res)
                return

            # **消费通路改后台执行**（2026-10-13 实测教训）：全链路要串行跑
            # 十几次 LLM 调用（同题校验 + 评分点派生 + 配方回填 + 写 SQL 迭代
            # ×3 + 事实整理 + VLML 独立取数 ×3 + 两边做法比对 + 叙事洞察），
            # 实测 2～5 分钟。
            # 同步等有两个恶果：Caddy 网关超时切断连接 → 前端「正在回答」转圈到死；
            # 人以为 Voyager 挂了（用户实测就问了一句"怎么回答不出来"）。
            # 所以这里**立即受理**，后台线程跑完落 coach_log —— 前端 4 秒轮询
            # 会自动把结果刷进导入历史和进步曲线（与驾驶舱的 pilot 同一套模式）。
            import threading
            import time as _time
            if COACH_JOB["running"]:
                self._json(200, {
                    "ok": False, "busy": True,
                    "error": (f"上一句还在回答中（{COACH_JOB['question'][:40]}…，"
                              f"已跑 {int(_time.time() - COACH_JOB['started_at'])} 秒）。"
                              "等它出结果再问下一句。")})
                return
            COACH_JOB.update({"running": True, "question": q,
                              "started_at": _time.time(), "error": ""})

            def _run_consume_bg() -> None:
                try:
                    import asyncio
                    import sys as _sys
                    from pathlib import Path as _Path
                    _repo_root = str(_Path(__file__).resolve().parents[1])
                    if _repo_root not in _sys.path:
                        _sys.path.insert(0, _repo_root)
                    import voyager_vlml.consume as _consume
                    # ⚠️ consume 是 async，必须 asyncio.run —— 直接调用只会得到一个
                    # 从未执行的协程（实测日志里就是
                    # "RuntimeWarning: coroutine 'consume' was never awaited"，
                    # 表现是"受理了、进度条走完，但什么都没发生"）。
                    asyncio.run(_consume.consume(
                        q, topic_hint=str(payload.get("topic_id") or "")))
                    # consume 内部会 _log 落 coach_log —— 前端轮询自然能看到
                except Exception as e:  # 失败也要让人看见，不能静默吞掉
                    COACH_JOB["error"] = f"{type(e).__name__}: {e}"[:300]
                    try:
                        from coach import _log_import
                        _log_import({
                            "question": q, "mode": "voyager_consume",
                            "topic_id": "", "topic_hit": "", "source": "human_import",
                            "validated_target": False, "trajectory": [], "errors":
                            [COACH_JOB["error"]], "facts": [], "facts_count": 0,
                            "narrative": "", "insights": [], "caveats": [],
                            "skipped": f"消费通路挂了：{COACH_JOB['error']}",
                        })
                    except Exception:
                        pass
                finally:
                    COACH_JOB["running"] = False

            threading.Thread(target=_run_consume_bg, daemon=True).start()
            self._json(200, {"ok": True, "accepted": True, "running": True,
                             "message": "已受理，Voyager 正在回答（要串十几次模型调用，"
                                        "约 1～3 分钟）。结果会自动出现在下方导入历史里，"
                                        "不用盯着等。"})
            return

        if path == "/api/exercise/preview":
            # 同构助产士 Instructor · New exercise：
            #   写题面 → Opus 生成 spec-gate 骨架（维度 + 预置偏差）→ 人审 → 发布。
            # MVE 的对应物：先登记一份练习（req），拆骨架**落在这一份的账上**
            # （req_<id>.json），发布时沿用同一个 req —— 不重新登记，
            # 否则预览看到的维度与发布后学的那份不是同一份（原版 Exercise 是
            # 一次发布一份，不是预览一份、上线另一份）。
            q = str(payload.get("question") or "").strip()
            if not q:
                self._json(400, {"ok": False, "error": "题面不能为空"})
                return
            try:
                import time as _time
                import scenes as _sc
                import req_registry as _rr
                import maieutic_scaffold as _ms
                sid = str(payload.get("scene_id") or "").strip() \
                    or str(_sc.current_scene_id() or "A")
                rid = str(_rr.register(sid, q, force_new=True).get("req_id") or "")
                if rid:
                    try:
                        _rr.ensure_req_node(sid, q, rid)
                    except Exception:
                        pass
                r = _ms.scaffold(q, sid, req_id=rid)
                _ms.save_exercise_meta(sid, q, {
                    "title": str(payload.get("title") or ""),
                    "unit": str(payload.get("unit") or ""),
                    "target_language": str(payload.get("language") or ""),
                    "authored_at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
                })
                self._json(200, {
                    "ok": bool(r.get("dimensions")),
                    "req_id": rid, "scene_id": sid,
                    "dimensions": r.get("dimensions") or [],
                    "milestone": r.get("milestone") or {},
                    "expected_drifts": r.get("expected_drifts") or [],
                    "material_note": str(r.get("material_note") or ""),
                    "origin": str(r.get("origin") or ""),
                    "reason": str(r.get("reason") or ""),
                })
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/exercise/publish-meta":
            # 发布时把教师侧的元信息写进这道练习的账（原版 publishedAt 的形状）：
            # 标题 / 单元 / 编辑器类型 / 发布时刻。缺了它，卡片只能拿题面头几十
            # 个字当名字，也没法按单元分组。
            try:
                import time as _t5
                import scenes as _sc6
                import maieutic_scaffold as _ms6
                q = str(payload.get("question") or "").strip()
                if not q:
                    self._json(400, {"ok": False, "error": "题面为空"})
                    return
                sid = str(payload.get("scene_id") or "").strip() \
                    or str(_sc6.current_scene_id() or "A")
                _ms6.save_exercise_meta(sid, q, {
                    "title": str(payload.get("title") or ""),
                    "unit": str(payload.get("unit") or ""),
                    "target_language": str(payload.get("language") or ""),
                    "published_at": _t5.strftime("%Y-%m-%dT%H:%M:%S"),
                })
                self._json(200, {"ok": True, "scene_id": sid,
                                 "exercise": _ms6.load_exercise_meta(sid, q)})
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/learn/propose":
            # 自由学选题（用户 2026-11-15「将做的做完，接着做图谱上书的」）。
            # 口径只有**一份实现** → graph_books.next_task：驾驶舱「▶ 启动
            # Voyager」在非 VLML 场景（P/N/H…）走的也是它（scene_learn.py
            # 队列空了就调它）。面板上不再有独立的「自由学」按钮 ——
            # 用户原话「我点这个按钮其实就是自由学呀，不用多做一个按钮」。
            # 这个接口留作 CLI / 其它入口复用同一条口径。
            try:
                import scenes as _scp
                import graph_books as _gb
                self._json(200, _gb.next_task(str(_scp.current_scene_id() or "A")))
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/learn/start":
            # 启动学习闭环（用户 2026-10-19 指令的环）：拆分 → 补缺口 →
            # 写插件 → 执行取数 → 入库 → 结晶。分钟级 → 后台线程，立即受理。
            q = str(payload.get("question") or "").strip()
            if not q:
                self._json(400, {"ok": False, "error": "问题是空的"})
                return
            # 发布路径：New exercise 预览时已经登记并拆好骨架，发布要**沿用
            # 同一个 req**（否则面板上会出现两道同名练习，一道只有骨架、
            # 一道才有会话）。不带时仍是旧行为：每次点都新登记一份。
            _pub_req_in = str(payload.get("req_id") or "").strip()
            if LEARN_JOB["running"]:
                self._json(200, {"ok": False, "busy": True,
                                 "error": (f"上一轮学习还在跑（{LEARN_JOB['question'][:40]}…，"
                                           "等它出结果再启动下一轮）。")})
                return
            import threading
            import time as _lt

            def _run_learn_bg() -> None:
                LEARN_JOB.update({"running": True, "question": q,
                                  "started_at": _lt.time(), "error": ""})
                try:
                    import asyncio
                    import learn_orchestrate as _lo
                    # ★ 2026-11-11（拆骨架不做归一，用户拍板）：
                    #   人点「启动学习」= 再派一次 = 原版 maieutic 的一次
                    #   **发布**（Exercise）。同一句点第二次必须拆**另一份**
                    #   骨架：每次发布一份骨架 + 一本账，旧的原样保留
                    #   （面板按 req_id 各自成卡片，能逐份盯它的闭环）。
                    _pub_req = ""
                    try:
                        import scenes as _sc0
                        import req_registry as _rr0
                        _sid0 = str(_sc0.current_scene_id() or "A")
                        # 导入题面即定位：题面不自带单元 id，由内容认。
                        # 认到的单元就是这一轮的起点，规划层从这儿往下排。
                        try:
                            import graph_books as _gb0
                            _hit0 = (_gb0.locate(_sid0, q).get("candidates")
                                     or [])
                            if (_hit0 and float(_hit0[0].get("score") or 0)
                                    >= 0.45):
                                _gb0.set_anchor(_sid0, str(_hit0[0]["unit"]))
                                LEARN_JOB["anchor"] = _hit0[0]
                        except Exception:
                            pass
                        # ★ 发布路径：前端带 req_id 来 = 已经预览过骨架，
                        #   必须沿用那一份（否则「预览看到的维度」和「学的维度」
                        #   是两份骨架，账也对不上）。不带才新登记一份。
                        _want = str(payload.get("req_id") or "").strip()
                        if _want and _rr0.get(_sid0, _want):
                            _pub_req = _want
                        else:
                            _pub_req = str(_rr0.register(
                                _sid0, q, force_new=True).get("req_id") or "")
                        if _pub_req:
                            _rr0.ensure_req_node(_sid0, q, _pub_req)
                        # 发布即写元信息（标题 / 单元 / 编辑器类型 / 发布时刻）：
                        # 不依赖前端再补一刀 —— 命令行/脚本发布也要有名字，
                        # 否则卡片永远只能显示题面头几十个字。
                        try:
                            import maieutic_scaffold as _ms0
                            import time as _t0
                            _ms0.save_exercise_meta(_sid0, q, {
                                "title": str(payload.get("title") or ""),
                                "unit": str(payload.get("unit") or ""),
                                "target_language": str(payload.get("language") or ""),
                                "published_at": _t0.strftime("%Y-%m-%dT%H:%M:%S"),
                            })
                        except Exception:
                            pass
                    except Exception:
                        _pub_req = ""
                    # ★ 重做（2026-11-15 用户口径「出题—学习—做出来—自由学习—
                    #   做以前出过的题—做得更好—指标更好」）：
                    #   账本留在册，背包清成"没学过"。必须放在 start_learning
                    #   **之前** —— 它一进来就读 progress / phase1.addressed，
                    #   清晚了这一轮已经按"上一遍学过"的现状跑完，首轮就报
                    #   "全答到了"，delta 恒为 0（等于没重做）。
                    if bool(payload.get("redo")):
                        try:
                            import scenes as _sc1
                            import maieutic_scaffold as _ms1
                            _rno = payload.get("round")
                            LEARN_JOB["redo"] = _ms1.reset_practice_state(
                                str(_sc1.current_scene_id() or "A"), q,
                                req_id=str(payload.get("req_id") or ""),
                                reason=(f"第 {_rno} 遍" if _rno else "重做"))
                        except Exception as _e0:
                            LEARN_JOB["redo"] = {
                                "ok": False,
                                "why": f"{type(_e0).__name__}: {_e0}"[:200]}
                    # fresh=True（重做）→ Phase2 不带本题上一遍那份代码起步。
                    # 技能库照旧检索（跨遍的长期能力就该留着），清的只是"底稿"。
                    r = asyncio.run(_lo.start_learning(
                        q, req_id=_pub_req, fresh=bool(payload.get("redo"))))
                    LEARN_JOB["last_status"] = str(r.get("status") or "")
                    if r.get("status") in ("failed",):
                        LEARN_JOB["error"] = str(r.get("why") or "")[:300]
                    # 人启动学习=「我要学会这句」→ 设为该场景当前里程碑锚点，
                    # 驾驶舱从此优先闭合它的未闭合维度（见 scene_learn.py）。
                    try:
                        import scenes as _sc
                        import maieutic_scaffold as _ms
                        _ms.set_current_target(str(_sc.current_scene_id() or "A"), q)
                    except Exception:
                        pass
                except Exception as e:
                    LEARN_JOB["error"] = f"{type(e).__name__}: {e}"[:300]
                finally:
                    LEARN_JOB["running"] = False

            threading.Thread(target=_run_learn_bg, daemon=True).start()
            self._json(200, {"ok": True, "accepted": True, "running": True,
                             "message": "已受理，学习闭环正在后台跑（拆分→补缺口→"
                                        "取数→入库→结晶，约 1～3 分钟）。"
                                        "进度看下方学习状态，结果进技能库与因果时间线。"})
            return

        if path == "/api/milestone/current":
            # 手动设/取/清当前里程碑锚点（驾驶舱选题优先闭合它的未闭合维度）。
            # POST {"question": "..."} 设为当前目标；{"clear": true} 取消；
            # 不带 question/clear → 返回当前目标。
            try:
                import scenes as _sc
                import maieutic_scaffold as _ms
                sid = str(payload.get("scene_id") or "").strip() \
                    or _sc.current_scene_id() or "A"
                if "question" in payload and str(payload.get("question") or "").strip():
                    _ms.set_current_target(sid, str(payload["question"]).strip())
                elif bool(payload.get("clear")):
                    _ms.clear_current_target(sid)
                self._json(200, {"ok": True, "scene_id": sid,
                                "current_target": _ms.current_target(sid)})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
            return

        if path == "/api/milestone/invalidate":
            # **作废**一份骨架（2026-11-09 第二刀）：骨架的落点指向了本场景
            # 够不着的表（实测场景 L：维度写的是账本表 web_evidence —— 它在
            # 另一个 duckdb 文件里，SQL 连不到 → 这道题在消费侧什么都查不到）。
            #
            # ⚠️ 为什么要**显式**作废（人点一下）：
            #   骨架「拆一次就固定」是铁律（按失败指纹重拆 → 维度 id 每轮变
            #   → 闭合账全对不上，2026-10-26 生产实测）。所以**不能自动重拆**。
            #   但出题器那边已经修好了（可达表清单 + 账本表标注查不到），
            #   作废后重拆出来的维度就自带正确落点。
            # 作废 = 改名留证（不删）→ 下次拆骨架缓存未命中 → 按当前实况重来。
            try:
                import scenes as _sc
                import maieutic_scaffold as _ms
                sid = str(payload.get("scene_id") or "").strip() \
                    or _sc.current_scene_id() or "A"
                q = str(payload.get("question") or "").strip()
                if not q:
                    self._json(400, {"ok": False, "error": "缺少 question"})
                    return
                _r = _ms.invalidate_skeleton(sid, q)
                _need = _ms.resplit_needed(sid, q)
                self._json(200 if _r.get("ok") else 404,
                           {"ok": bool(_r.get("ok")),
                            "why": str(_r.get("why") or "")[:300],
                            "scene_id": sid, "question": q[:120],
                            "still_bad": bool(_need)})
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:200]})
            return

        if path == "/api/db/probe":
            try:
                import db_switch
                import ui_events
                p = str(payload.get("path") or "")
                pr = db_switch.probe(p)
                # 验货也要留痕：换库是唯一一个「点了之后可能全盘查不到数据」的操作，
                # 出问题时人要能回看当时体检到的是什么。
                ui_events.append(
                    ui_events.DB_PROBE,
                    detail={"目标库": pr.get("path") or "（默认库）",
                            "体检": (f"{len(pr.get('tables') or [])} 张表"
                                     f" · {pr.get('events')} 条事件")
                                    if pr.get("ok") else (pr.get("error") or "失败"),
                            "series 检查": pr.get("warning") or "原题 series 仍在库里"},
                    result="能换" if pr.get("ok") else "不能换",
                )
                self._json(200, pr)
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
            return

        if path == "/api/db/switch":
            try:
                import db_switch
                import ui_events
                p = str(payload.get("path") or "")
                res = db_switch.write(p)
                detail = {"切换到": res["db_path"]}
                if res["is_default"]:
                    detail["说明"] = "已回到默认库"
                # write() 现在会调 anchor.refresh()：旧锚点在新库里还在就沿用，
                # 不在就按"回合数最多的那场/队/图"重挑。把结论报出来。
                if res.get("anchor_note"):
                    detail["锚点"] = res["anchor_note"]
                if payload.get("reset"):
                    try:
                        import subprocess
                        # ⚠️ 带 --db-switch：换库清档要**额外**清掉旧库派生的
                        #   entity_catalog / tool_schemas —— 旧库的队名/图名/
                        #   series_id 会让出题器选出在新库里查不到的题。
                        _r = subprocess.run(
                            [sys.executable, str(ROOT / "reset_all.py"), "--db-switch"],
                            capture_output=True, timeout=180)
                        # ⚠️ 返回码必须看：循环还在跑时 reset_all 会拒绝清档（返回 1），
                        #   以前一律写"已清档"，人以为清了其实一个文件都没删。
                        detail["顺带"] = ("已清档（含旧库派生缓存）" if _r.returncode == 0
                                          else "已拒绝清档（还有任务在跑，请先停止）")
                        if _r.returncode == 0:
                            # 清而不补的话出题器连可填值都没有：按**新库**重查
                            # 实体清单（entity_catalog.py 自己读 db_config.json，
                            # 此时它已指向新库）。
                            _r2 = subprocess.run(
                                [sys.executable, str(ROOT / "entity_catalog.py")],
                                capture_output=True, timeout=180)
                            if _r2.returncode == 0:
                                detail["实体清单"] = "已按新库重查"
                            else:
                                detail["实体清单"] = ("重查失败，手动跑："
                                                      "python mve/entity_catalog.py")
                            # ⚠️ 以前这里只补了 entity_catalog，**漏了 tool_schemas**：
                            #   `--db-switch` 会把 tool_schemas.json 一起清掉，而
                            #   `load_schemas()` 读不到时只返回 {}、不会重建，
                            #   重建入口 probe_schemas() 又是 async 没有 CLI ——
                            #   结果换库后模型拿不到工具结构文档，只能猜字段路径
                            #   （实测：四个维度全 dig 到 None）。
                            #   所以补一条 CLI（`action_code.py --rebuild-schemas`）
                            #   并在这里调起来，跟 entity_catalog 同一处补齐。
                            _r3 = subprocess.run(
                                [sys.executable, str(ROOT / "action_code.py"),
                                 "--rebuild-schemas"],
                                capture_output=True, timeout=300)
                            if _r3.returncode == 0:
                                detail["工具结构"] = (
                                    "已按新库重建（CLI 输出里 ~ 标记的是"
                                    "本轮失败、沿用了旧库结构）")
                            else:
                                detail["工具结构"] = (
                                    "本轮一个都没探到（探针参数取自锚点，锚点不在"
                                    "新库里就会整轮扑空）；缓存留的是旧库结构。手动跑："
                                    "python mve/action_code.py --rebuild-schemas")
                    except Exception:
                        detail["顺带"] = "清档失败（数据库已切换）"
                # 切完之后**再核一遍**：refresh 是"按库重挑"，anchor_status 是
                # "拿新库实查这些值在不在"。两者都报，前者说做了什么，
                # 后者验做没做成 —— 只报一个的话，refresh 静默失败就看不出来。
                try:
                    _st = db_switch.anchor_status()
                    if _st.get("stale"):
                        detail["锚点核对"] = _st.get("hint") or "锚点不在新库里"
                    elif _st.get("hint"):
                        detail["锚点核对"] = _st.get("hint")
                    elif _st.get("checked"):
                        detail["锚点核对"] = (
                            f"已验：{_st.get('anchor', {}).get('team')} / "
                            f"{_st.get('anchor', {}).get('map')} / series "
                            f"{_st.get('anchor', {}).get('series')} 都在新库里")
                except Exception:
                    pass
                ui_events.append(ui_events.DB_SWITCH, detail=detail, result="ok")
                self._json(200, {**res, "need_restart": True,
                                 "reset_detail": detail})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path in ("/api/subject/add", "/api/subject/sync", "/api/subject/del"):
            # 学科登记册：人是**自己填**学科（换数据源后填新赛事 / 新战队域），
            # 之后 VLML / Voyager 跑出来的条目按实体值挂到该学科下。
            try:
                import subjects
                import knowledge_graph
                import ui_events
                if path.endswith("/sync"):
                    res = subjects.sync_from_catalog()
                    ui_events.append(ui_events.SUBJECT_SYNC,
                                     detail={"新登记": res.get("added", 0)},
                                     result="ok" if res.get("ok") else "失败")
                    self._json(200, res)
                    return
                if path.endswith("/del"):
                    sid = str(payload.get("id") or "")
                    ok = subjects.unregister(sid)
                    ui_events.append(ui_events.SUBJECT_DEL, detail={"学科": sid},
                                     result="已删除" if ok else "没找到")
                    self._json(200, {"ok": ok, "id": sid})
                    return
                kind = str(payload.get("kind") or "").strip()
                value = str(payload.get("value") or "").strip()
                res = subjects.register(kind, value,
                                        note=str(payload.get("note") or ""))
                # 填完**立刻重算归属**：此前跑出来却没归位的条目（人还没填这个
                # 学科时的那些）要当场挂上去 —— 不用等人去跑 --build。
                # moved 只数"这次**新**归位的"：重算后仍归位的旧条目不算，
                # 否则这个数字永远等于已归属总数，没有信息量。
                moved = 0
                if res.get("ok"):
                    try:
                        before = set(subjects.unassigned())
                        for r in (knowledge_graph.load_imported_links()
                                  + knowledge_graph.load_pending_dimensions()):
                            knowledge_graph._attach_entry(r)
                        moved = len(before - set(subjects.unassigned()))
                    except Exception:
                        pass
                ui_events.append(ui_events.SUBJECT_ADD,
                                 detail={"学科": f"{kind}={value}",
                                         "补归属条目": moved},
                                 result="已登记" if res.get("ok") else "失败")
                self._json(200, {**res, "moved": moved})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/practice-scope":
            # set = 把图谱上的某个维度设成练习范围；clear = 回到出题器自选
            # 形态照伴学 study_set_practice_scope：canonical 校验 + revision 递增
            try:
                import practice_scope
                import ui_events
                if str(payload.get("action") or "") == "clear":
                    res = practice_scope.clear_scope()
                    ui_events.append(ui_events.PRACTICE_SCOPE_CLEAR,
                                     detail={"原范围": res.get("cleared") or "—"},
                                     result="已清除，回到出题器自选")
                    self._json(200, {**res, "scope": practice_scope.get_scope()})
                    return
                dim = str(payload.get("dim") or "")
                scope = practice_scope.set_scope(dim)
                ui_events.append(
                    ui_events.PRACTICE_SCOPE_SET,
                    detail={"维度": dim,
                            "关联题": "、".join(scope.get("topics") or []),
                            "revision": scope.get("scope_revision")},
                    result="范围已生效 → " + "、".join(scope.get("topics") or []),
                )
                self._json(200, {"ok": True, "scope": scope})
            except Exception as e:
                code = str(getattr(e, "code", "") or "PRACTICE_SCOPE_ERROR")
                try:
                    import practice_scope
                    cur = practice_scope.get_scope()
                except Exception:
                    cur = {"active": False}
                self._json(200, {"ok": False, "code": code,
                                 "error": f"{type(e).__name__}: {e}"[:300],
                                 "scope": cur})
            return

        if path == "/api/open-quiz/next":
            # 选下一知识点并出一道开放式题（前置门控 + 薄弱优先 + 题型轮换）。
            try:
                import open_quiz
                scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else None
                explicit = str(payload.get("topic_id") or "") or None
                last = str(payload.get("last_question") or "")
                q = open_quiz.generate_next(scope=scope, explicit=explicit,
                                            last_question=last)
                body = json.dumps(q, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/open-quiz/grade":
            # 判分（accepted_answers + key_points 语义判分；math_exact 先走数值快判）
            # + 记录掌握度。
            try:
                import open_quiz
                q = payload.get("question") if isinstance(payload.get("question"), dict) else {}
                ua = str(payload.get("user_answer") or "")
                topic_id = str(q.get("topic") or payload.get("topic_id") or "")
                g = open_quiz.grade_answer(q, ua)
                if topic_id:
                    open_quiz.record_result(topic_id, score=g["score"])
                    # Task3：答错（score<0.6）把本题误区写成 mem0 纠正记忆，
                    # 下次出题/作答可召回——人消费路径也接记忆闭环。
                    corr_written = open_quiz.record_correction_on_failure(
                        topic_id, user_answer=ua, grade=g)
                else:
                    corr_written = 0
                self._json(200, {"ok": True, "grade": g, "topic_id": topic_id,
                                "correction_memories": corr_written})
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/ui-event":
            try:
                import ui_events
                ev = ui_events.append(
                    str(payload.get("kind") or "unknown"),
                    detail=payload.get("detail") or {},
                    result=str(payload.get("result") or ""),
                )
                self._json(200, {"ok": True, "seq": ev["seq"]})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
            return

        if path == "/api/export":
            try:
                import export_md
                import ui_events
                md = export_md.build_markdown()
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                ui_events.append(ui_events.EXPORT,
                                 detail={"字符数": len(md)},
                                 result=f"mve_export_{stamp}.md")
                self._json(200, {"ok": True, "markdown": md,
                                 "filename": f"mve_export_{stamp}.md"})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/progress-chat":
            # 人的进度问答（2026-11-18）：progress_chat 只读转述账面，零写账。
            # 不碰 phase2 账本（红线 C3/C4），不做判定。
            try:
                import progress_chat as _pcx
                _sid = str(payload.get("scene_id") or "").strip() or "S"
                self._json(200, _pcx.ask(_sid, str(payload.get("question") or "")))
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/pilot/start":
            # topic 一律留空：选题权在练习范围（或出题器）手里，不在驾驶舱键钮手里。
            # 面板上因此没有难度 / 选题下拉，只剩开始 / 停止。
            self._json(200, pilot.start(
                mode=str(payload.get("mode") or "once"),
                topic="",
                # ⚠️ 默认 0 = **不停**。写成 `or 3` 时，前端传 0 会被假值判断
                #    换成 3 —— 「不停」开关勾了也只跑 3 道题（用户原话：「原 voyager
                #    学习是不带停的；你现在三轮就结束了，图谱都没跑完」）。
                rounds=int(payload.get("rounds") or 0),
                use_scope=bool(payload.get("use_scope", True)),
                # 三种任务：practice（练题）/ placement（摸底）/ learn（学习单元）
                job=str(payload.get("job") or "practice"),
                units=int(payload.get("units") or 1),
                transfer=int(payload.get("transfer") or 0),
            ))
            return

        if path == "/api/pilot/stop":
            self._json(200, pilot.stop())
            return

        if path == "/api/pilot/clear-log":
            # 运行中拒绝清屏（pilot 还握着日志 fd，unlink = 整轮日志全丢）
            self._json(200, pilot.clear_log())
            return

        # 清档：摸底前要先把技能库清空（否则考出来的是"练过之后的水平"）。
        # reset_all 自己会先备份到 mve/_backup_* 再删，所以这个键钮不额外加确认。
        if path == "/api/reset":
            try:
                import reset_all
                _old = sys.argv[:]
                sys.argv = ["reset_all.py"]       # reset_all.main() 自己读 argv
                try:
                    # ⚠️ 返回码必须看：循环/驾驶舱还在跑时 reset_all 会**拒绝清档**
                    # （返回 1）。以前这里把返回值丢了、一律报 ok ——
                    # 面板显示"已清库（已备份）"，其实一个文件都没删。
                    rc = reset_all.main()
                finally:
                    sys.argv = _old
                if rc != 0:
                    self._json(409, {"ok": False,
                                     "error": "还有任务在跑，已拒绝清档 —— 先点「■ 停止」，"
                                              "等循环收尾完（约 1 分钟）再清"})
                    return
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
            return

        # MC 思路学习（即「启动在 mc 里的学习」）：提炼 + 入库。跑完之后前端
        # 会自动触发标准练习循环（正常 MVE 全流程，= pilot/start）。
        if path == "/api/mc-learn":
            try:
                import mc_learn
                import ui_events
                res = mc_learn.run()
                ui_events.append(
                    ui_events.MC_LEARN,
                    detail={"提炼原则": res["principles"],
                            "入库条数": res["store"]["mc_thinking_count"],
                            "SKILL.md": res["skill_md"]},
                    result="MC 思路学习完成 → 已入库",
                )
                self._json(200, res)
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        # ---- Voyager 真实学习（MC 现场）----
        # 启动 / 停止 / 清库 / 清记忆：与 MVE 驾驶舱同款，状态写文件、面板只读。
        if path == "/api/voyager/start":
            try:
                import voyager_pilot
                import ui_events
                res = voyager_pilot.start()
                ui_events.append(
                    ui_events.VOYAGER_START,
                    detail={"mc_port": res.get("mc_port", 25565),
                            "上限": res.get("max_iterations", "")},
                    result="启动成功" if res.get("ok") else ("失败：" + res.get("error", "")),
                )
                self._json(200, res)
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/voyager/stop":
            try:
                import voyager_pilot
                import ui_events
                res = voyager_pilot.stop()
                ui_events.append(
                    ui_events.VOYAGER_STOP,
                    detail={"已学技能": res.get("completed_count", 0)},
                    result="已停止",
                )
                self._json(200, res)
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/skill/delete":
            # 单条清账：人复核出假阳性/冗余条目后，在技能·因果页逐条清。
            # 不发明新函数：本体走 skill_store.delete_skill（连向量一起清）；
            # 结晶文件名用 plugin_forge._ident/PLUGIN_DIR 现成规则——
            # 同名条目还有存活者就保留结晶（共享文件），没有才跟着清；
            # 能力注册表同步摘除（同 crystal 注册时按 {源场景, 当前场景} 双挂）。
            try:
                import skill_store
                import ui_events
                key = str(payload.get("key") or "").strip()
                if not key:
                    self._json(400, {"ok": False, "error": "缺 key"})
                    return
                entry = next((one for one in skill_store.all_skills()
                              if str(one.get("key") or "") == key), None)
                if entry is None:
                    self._json(404, {"ok": False, "error": "key 不存在（可能已删过）"})
                    return
                name = str(entry.get("name") or "")
                scene = str(entry.get("source_scene") or "default")
                ok = skill_store.delete_skill(key)
                plugin_removed = ""
                if ok and name:
                    survivors = [one for one in skill_store.all_skills()
                                 if str(one.get("name") or "") == name]
                    if not survivors:
                        try:
                            import plugin_forge
                            import actions
                            mod = f"learned_{plugin_forge._ident(name)}"
                            ppath = plugin_forge.PLUGIN_DIR / (mod + ".py")
                            if ppath.exists():
                                ppath.unlink()
                                plugin_removed = ppath.name
                            for sid2 in {scene, str(payload.get("scene") or "")} - {""}:
                                bucket = actions._REGISTRY.get(sid2)
                                if isinstance(bucket, dict):
                                    bucket.pop(mod, None)
                        except Exception:
                            pass  # 结晶清理失败不拦删账本体（审计里如实记）
                ui_events.append(
                    ui_events.SKILL_DELETE,
                    detail={"key": key[:80], "name": name[:60], "scene": scene,
                            "结晶": plugin_removed or "无/保留"},
                    result="已清" if ok else "失败",
                )
                self._json(200, {"ok": bool(ok), "key": key, "name": name,
                                 "plugin_removed": plugin_removed})
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/voyager/clear-log":
            try:
                import voyager_pilot
                voyager_pilot.clear_log()
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/voyager/clear-skill":
            try:
                import voyager_learn_reset
                import ui_events
                res = voyager_learn_reset.clear_skill()
                ui_events.append(
                    ui_events.VOYAGER_CLEAR_SKILL,
                    detail={"清了": res.get("cleared", []),
                            "备份": res.get("backup") or "无"},
                    result="已清库" if res.get("ok") else "失败",
                )
                self._json(200, res)
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        if path == "/api/voyager/clear-memory":
            try:
                import voyager_learn_reset
                import ui_events
                res = voyager_learn_reset.clear_memory()
                ui_events.append(
                    ui_events.VOYAGER_CLEAR_MEMORY,
                    detail={"清了": res.get("cleared", []),
                            "备份": res.get("backup") or "无"},
                    result="已清记忆" if res.get("ok") else "失败",
                )
                self._json(200, res)
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": f"{type(e).__name__}: {e}"[:300]})
            return

        self.send_error(404)

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        pass


def main() -> None:
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    _ensure_knowledge_graph()   # 启动自检：图谱缺失/过期自动重建，杜绝 "not built"
    _restore_action_registry()  # 启动自检：可执行动作条目从快照恢复，杜绝 "0 个动作"误报
    print(f"MVE 面板已启动：http://{HOST}:{PORT}")
    print("停止：Ctrl-C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
