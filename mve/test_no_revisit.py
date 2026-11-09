#!/usr/bin/env python3
"""拆掉「排队 + 学习完成自动回访」—— 回归测试（2026-11-09 用户指令）。

用户指令
--------
> 「【⏳ 还有 5 个排队问题没答上，学会后我会自动回来重答，不用你盯着。】
>  这个对应的能力要删掉，不要回访了，也不要排队，会搞乱掉」

拆掉的范围（整套，不是只删文案）
------------------------------
1. `mve/revisit.py`（排队队列 + 回访执行）—— **文件删除**
2. `mve/test_revisit.py` —— 一并删除
3. `voyager_vlml/consume.py`：两处 `mark_pending` 排队 + 两处
   `auto_learn=False` 回访门控 + `auto_learn` 参数本身 —— 全删
4. `mve/learn_orchestrate.py`：学习完成信号 → `revisit_pending` 触发 —— 删
5. `mve/dashboard.py`：`GET /api/revisit` 路由 —— 删
6. `mve/chat_page.py`：`pullRevisit()` + 「⏳ 还有 N 个排队问题…」提示条
   + 「🔁 学习完成后自动回访」气泡 —— 删

**保留**（与排队/回访无关，别误删）：
- 「没答出来 → 拉起学习」这条单向链路（2026-10-28 用户改的设计）
- 待审区（`pending_dimensions.json`）—— 那是**图谱待审**，不是回访队列

替代路径（用户删回访不会让功能倒退）
------------------------------------
人再问同一句（或换个问法）→ **会话召回 + 已验答案披露**（第三刀）
→ 原样交出学习侧验过的答案，**不必**回头重跑一遍判定。

跑法：服务器 `python mve/test_no_revisit.py`
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

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


CONSUME = (_ROOT / "voyager_vlml" / "consume.py").read_text(encoding="utf-8")
CHAT = (_HERE / "chat_page.py").read_text(encoding="utf-8")
DASH = (_HERE / "dashboard.py").read_text(encoding="utf-8")
LEARN = (_HERE / "learn_orchestrate.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
print("\n== 1. 模块与路由已删 ==")
# ---------------------------------------------------------------------------
# ⚠️ 断言查的是**代码特征**（函数定义 / 调用 / 字面量），**不是关键词**：
#   删掉的地方留了注释说明「为什么删」，注释里会出现这些词 ——
#   查关键词会把注释当成残留（第一版就是这么误报 9 条的）。
check("mve/revisit.py 已删除", not (_HERE / "revisit.py").exists())
check("mve/test_revisit.py 已删除", not (_HERE / "test_revisit.py").exists())
check("面板没有 /api/revisit 路由", '"/api/revisit"' not in DASH)
check("对话页不再拉 /api/revisit", "fetch(P + '/api/revisit')" not in CHAT)
check("对话页没有 pullRevisit 函数", "function pullRevisit(" not in CHAT)
check("对话页没有「⏳ 还有 N 个」提示条", "var t = '⏳ " not in CHAT)
check("对话页不再读 pending 数", "d.pending" not in CHAT)
check("对话页没有「🔁 自动回访」气泡",
      "'🔁 学习完成后自动回访：" not in CHAT)

# ---- 产物级检查（比查源码更准：用户看的就是这份 HTML）----
try:
    import chat_page
    _HTML = chat_page.CHAT_HTML
    check("★ 页面产物里没有「排队」提示语", "排队问题" not in _HTML)
    check("★ 页面产物里没有「不用你盯着」", "不用你盯着" not in _HTML)
    check("★ 页面产物里没有「自动回访」气泡", "自动回访" not in _HTML)
    check("★ 页面产物里没有回访拉取", "/api/revisit" not in _HTML)
    check("★ 页面产物里没有 pullRevisit", "pullRevisit" not in _HTML)
except Exception as e:
    check("对话页产物可读", False, f"{type(e).__name__}: {e}")

# ---------------------------------------------------------------------------
print("\n== 2. 源码里没有残留调用 ==")
# ---------------------------------------------------------------------------
check("consume 不再 import revisit", "import revisit" not in CONSUME)
check("consume 不再 mark_pending 调用", ".mark_pending(" not in CONSUME)
check("consume 签名里没有 auto_learn", "auto_learn: bool" not in CONSUME)
check("consume 没有 auto_learn 门控", "not auto_learn" not in CONSUME)
check("学习侧不再触发回访", "_rv.revisit_pending" not in LEARN)
check("学习侧不再 import revisit", "import revisit" not in LEARN)

# ---------------------------------------------------------------------------
print("\n== 3. 行为：学习完成不拉回访（打桩验证）==")
# ---------------------------------------------------------------------------
# 往 sys.modules 塞一个「一旦被 import 就打标记」的假 revisit 模块：
# 如果还有任何路径会去 import 它 → 标记为真 → 测试失败。
_imported = {"hit": False}


class _Tripwire(types.ModuleType):
    def __getattr__(self, name):        # pragma: no cover - 只在被 import 时触发
        _imported["hit"] = True
        raise AssertionError(f"回访机制被触发了：访问了 revisit.{name}")


sys.modules["revisit"] = _Tripwire("revisit")

try:
    import learn_orchestrate as LO
    # 把落账的写入打桩掉（别污染服务器的 run_log / 因果时间线）
    try:
        import run_log as RL
        import causal_timeline as CT
        old_rl, old_ct = RL.append, CT.append
        RL.append = lambda *a, **k: None
        CT.append = lambda *a, **k: None
    except Exception:
        old_rl = old_ct = None
    try:
        LO._record({"question": "打桩测试题", "scene_id": "T",
                    "status": "learned", "why": "", "milestone": {},
                    "absorbed": {}, "crystallized": {}})
    finally:
        if old_rl is not None:
            RL.append, CT.append = old_rl, old_ct
    check("status=learned 时不拉回访", not _imported["hit"],
          "有代码路径 import 了 revisit")
except Exception as e:
    check("_record 能正常执行（没因删回访而炸）", False,
          f"{type(e).__name__}: {e}")

# ---------------------------------------------------------------------------
print("\n== 4. 残留数据文件不再有读取方 ==")
# ---------------------------------------------------------------------------
check("仓库里没有回访队列文件",
      not list(_HERE.glob("revisit_queue*.json")),
      str(list(_HERE.glob("revisit_queue*.json"))))
check("仓库里没有回访日志文件",
      not list(_HERE.glob("revisit_log*.json")),
      str(list(_HERE.glob("revisit_log*.json"))))
# 机制已删 → 残留文件（服务器上可能有旧数据）只是**死数据**，不会被读，
# 也不会复活排队。留着无害（是历史留痕），不必删。

# ---------------------------------------------------------------------------
print("\n== 5. 保留项没被误删 ==")
# ---------------------------------------------------------------------------
check("「没答出来 → 拉起学习」还在（kick_learning 仍被调用）",
      "kick_learning(" in CONSUME)
_ROUTING = (_HERE / "routing.py")
check("待审区还在（routing.pending_for_scene 没被误删）",
      _ROUTING.exists() and "def pending_for_scene" in _ROUTING.read_text(
          encoding="utf-8"))
check("第三刀的会话召回还在（披露路径没被牵连）",
      "session_recall" in CONSUME)

print(f"\n=== 拆回访回归：{PASS} 过 / {FAIL} 败 ===")
sys.exit(1 if FAIL else 0)
