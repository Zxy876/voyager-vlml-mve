#!/usr/bin/env python3
"""「换个问法读不到骨架」这条链路（2026-11-09 第四根因的补洞）的测试。

跑法：python mve/test_scaffold_lookup.py（服务器无 pytest，同 test_*.py 口径）。

这一层守什么
------------
骨架缓存键 = md5(问句)[:12] + md5(db_path)[:8]，而**问句是「人导入时
那句原话」**。消费时人往往换个问法再问：

    人导入原句 = "我查查用户名 ce 的这位女生（2007年3月26日出生）都活跃在哪些社交平台上"
    消费问句   = "帮我查查用户名 ce 的这位女生（2007年3月26日出生）都活跃在哪些社交平台上"

两者 md5 不同 → 按消费问句找不到骨架 → 路由判「没有骨架 → 不猜题型」
→ 不碰环境、不联网 → **换个问法就答不出来**（不是模型不会答）。

修法是**复用既有归题能力**（`routing.match`）：消费问句跑一次会在图谱里
留下一个消费节点（id=`consume:consume_<hash>`，label=**问句自己**），
它拿 exact_match 满分 → 永远是最佳 → 「找回原句」找回的是问句自己 →
md5 算到自己 → 骨架永远读不到。

⇒ `routing.match` 加 `exclude_ids`：排除「问句自己」那个节点后，
**取下一个候选**，那个才是人导入原句。不改打分口径、不加门槛
（候选本来就在 match 的循环里算过，以前只暴露 top-1）。

本测试钉死：
    ① `exclude_ids` 排除最佳后能取到下一个候选；
    ② 默认不传 `exclude_ids` → 行为与以前**完全一致**（零回归）；
    ③ `_resolved_import_question` 命中「问句自己」时 → 找回人导入原句；
    ④ 找不到第二个候选 → 如实返回空（不拿别的题目的骨架来套）；
    ⑤ 打分口径没被改（排除只影响「取谁」，不影响「怎么打分」）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0

Q_CONSUME = "帮我查查用户名 ce 的这位女生（2007年3月26日出生）都活跃在哪些社交平台上"
Q_IMPORT = "我查查用户名 ce 的这位女生（2007年3月26日出生）都活跃在哪些社交平台上"


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


class _Node:
    """够 match 打分用的最小节点（label + id + root_scene + kind + detail）。"""

    def __init__(self, nid: str, label: str, scene: str = "L",
                 kind: str = "dimension") -> None:
        self.id = nid
        self.label = label
        self.root_scene = scene
        self.kind = kind
        self.detail: dict = {}
        self.status = "verified"
        self.evidence = 1


class _Graph:
    def __init__(self, nodes: dict) -> None:
        self.nodes = nodes


def _graph_with_self_and_import() -> _Graph:
    """真实形态（服务器场景 L 实测）：消费节点（问句自己）+ 导入节点（原句）。"""
    return _Graph({
        "consume:consume_2776f867": _Node("consume:consume_2776f867", Q_CONSUME),
        "import:imp_20261109T134643": _Node("import:imp_20261109T134643",
                                            Q_IMPORT, kind="import"),
        "dim:learn_b078a2ba:x": _Node("dim:learn_b078a2ba:x",
                                      "在 web_facts 表中，item 包含 'ce' 的记录有哪些？"),
    })


def main() -> int:
    import routing
    import maieutic_scaffold as ms

    print("== 1. match 的 exclude_ids：排除后能取到下一个候选 ==")
    g = _graph_with_self_and_import()
    m1 = routing.match(Q_CONSUME, "L", graph=g)
    check("默认命中的确是「问句自己」那个消费节点",
          m1.get("node_id") == "consume:consume_2776f867")
    m2 = routing.match(Q_CONSUME, "L", graph=g,
                        exclude_ids=("consume:consume_2776f867",))
    check("排除它之后取到下一个候选（人导入原句）",
          m2.get("node_id") == "import:imp_20261109T134643"
          and m2.get("node_label") == Q_IMPORT)
    check("排除后判定仍算归上了（不是 REJECTED）",
          str(m2.get("decision")) != "REJECTED")

    print("\n== 2. 默认不传 exclude_ids → 行为与以前完全一致（零回归）==")
    m3 = routing.match(Q_CONSUME, "L", graph=g)
    check("结果与 m1 一致", m3.get("node_id") == m1.get("node_id")
          and m3.get("max_score") == m1.get("max_score"))
    check("排除不存在的节点 id 不影响结果",
          routing.match(Q_CONSUME, "L", graph=g,
                        exclude_ids=("dim:learn_b078a2ba:x",)).get("node_id")
          == m1.get("node_id"))

    print("\n== 3. _resolved_import_question：命中自己时找回人导入原句 ==")
    import knowledge_graph as kg
    _orig_load = kg.KnowledgeGraph.load
    kg.KnowledgeGraph.load = classmethod(lambda cls, path=None: g)
    try:
        got = ms._resolved_import_question(Q_CONSUME, "L")
    finally:
        kg.KnowledgeGraph.load = _orig_load
    check("找回了人导入原句（不再是问句自己）", got == Q_IMPORT)
    check("找回的原句不等于消费问句", got != Q_CONSUME)

    print("\n== 4. 没有第二个候选 → 如实返回空（不拿别的题目的骨架来套）==")
    g_only_self = _Graph({
        "consume:consume_x": _Node("consume:consume_x", Q_CONSUME),
    })
    kg.KnowledgeGraph.load = classmethod(lambda cls, path=None: g_only_self)
    try:
        got2 = ms._resolved_import_question(Q_CONSUME, "L")
    finally:
        kg.KnowledgeGraph.load = _orig_load
    check("只有「自己」一个候选 → 返回空", got2 == "")

    print("\n== 5. 排除只影响「取谁」，不影响「怎么打分」 ==")
    g2 = _graph_with_self_and_import()
    a = routing.match(Q_CONSUME, "L", graph=g2)
    b = routing.match(Q_CONSUME, "L", graph=g2,
                       exclude_ids=("consume:consume_2776f867",))
    check("两个候选各自的分值都可复现（不是重算的）",
          b.get("max_score") > 0 and a.get("max_score") >= b.get("max_score"))
    # 直接验证打分函数本身没被动过
    score, exact, meaningful, chunk = routing._score_one(
        routing._norm(Q_CONSUME), routing._tokens(Q_CONSUME),
        routing._node_texts(g2.nodes["import:imp_20261109T134643"]))
    check("_score_one 口径不变（exact=False 因为不是原句）",
          score > 0 and exact is False and meaningful is True and chunk > 0)

    print("\n== 6. 消费问句 == 导入原句（照原句再问）时也能找回 ==")
    g3 = _Graph({
        "consume:consume_y": _Node("consume:consume_y", Q_IMPORT),
        "import:imp_1": _Node("import:imp_1", Q_IMPORT, kind="import"),
    })
    kg.KnowledgeGraph.load = classmethod(lambda cls, path=None: g3)
    try:
        got3 = ms._resolved_import_question(Q_IMPORT, "L")
    finally:
        kg.KnowledgeGraph.load = _orig_load
    check("照原句再问 → 找回的还是原句（不是空）",
          got3 == Q_IMPORT)

    print("\n== 7. 消费脏账（consume: 前缀）不当原句用 ==")
    g4 = _Graph({
        "consume:consume_z": _Node("consume:consume_z", Q_CONSUME),
        "consume:consume_y": _Node("consume:consume_y", Q_IMPORT),
    })
    kg.KnowledgeGraph.load = classmethod(lambda cls, path=None: g4)
    try:
        got4 = ms._resolved_import_question(Q_CONSUME, "L")
    finally:
        kg.KnowledgeGraph.load = _orig_load
    check("排除自己后只剩消费脏账 → 如实返回空", got4 == "")

    print(f"\n== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
