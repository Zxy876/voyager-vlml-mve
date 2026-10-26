#!/usr/bin/env python3
"""学习闭环验收（用户 2026-10-19 澄清的验收线，逐步断言）：

    人导入（未命中）→ 建图谱节点 → voyager 规划课程自主学习
    （拆解任务，任务有抓手用工具可达）→ 计入技能库 → 解锁里程碑（不再重复出题）
    → 人再导入相同问题（命中且解决）

全程临时文件隔离 + 假 LLM + 假执行器（VLMLActionAgent 用注入的假模块顶替，
本地没有 vlml/ 数据层）。每一步都断言**真实状态变化**，不看声明：

  0. 新问题 routing.match = REJECTED（未命中）
  1. on_miss：图谱多一枚 cold_start_guess 节点（root_scene 正确）+ 待审区有记录
  2. start_learning：拆出子目标、每个子目标都有抓手（reachable）、
     执行取到真数、配方进技能库、结晶成可 invoke 条目
  3. 里程碑解锁：用**原 MVE 已有披露** `scene_context.unlocked_milestones()`
     断言该能力项从「未解锁」变「已解锁」（规划/出题按 is_completed 门控，
     见 topic_graph.py:582，不再重复推这题）；节点 evidence 同时升到 verified
  4. 再导入同一问题：routing.match 命中（非 REJECTED）+ 技能库检索到配方
     + 配方条目 invoke 真能重放出数据（命中且解决）
  5. 负路径：没取到真数 → 不入库不结晶不建点；硬不可达 → 如实报
  6. kick_learning：循环内起后台任务 / 同步上下文如实返回 False

运行：python mve/test_learn_orchestrate.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for p in (str(HERE), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

import actions                # noqa: E402
import causal_timeline        # noqa: E402
import db_switch              # noqa: E402
import knowledge_graph        # noqa: E402
import learn_orchestrate      # noqa: E402
import plugin_forge           # noqa: E402
import routing                # noqa: E402
import scene_context          # noqa: E402
import scenes                 # noqa: E402
import skill_store            # noqa: E402
import topic_graph            # noqa: E402

QUESTION = "查一下 player 的 kills 按地图过滤"
SQL = "SELECT p.kills FROM players p JOIN maps m ON p.map_id = m.map_id"
SCENE = "A"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" —— {detail}" if detail else ""))


def _fake_llm(_messages: list[dict]) -> str:
    return '["Get player kills", "Filter by map name"]'


class _FakeAgent:
    """假执行器：solve() 回真数观察 + 固定 SQL（形状同真 agent）。"""

    vacuous = False

    def __init__(self, env=None) -> None:
        pass

    async def solve(self, question, *, max_iters=3, hints=None, skills=None,
                    grabs=None, focus_tables=None):
        # 抓手清单必须真的传进来（2026-10-19 服务器实测：不传 → 执行层凭想象
        # join → Binder Error → no_data，闭环停在第五步）。这里记下来给断言看。
        _FakeAgent.last_grabs = list(grabs or [])
        _FakeAgent.last_focus = list(focus_tables or [])
        data = ([{"n": None}] if self.vacuous
                else [{"kills": 300}, {"kills": 180}])
        return ([{"ok": True, "rows": len(data), "data": data,
                  "tool": "query_sql"}], SQL)


class _FakeMCP:
    async def query_sql(self, sql_query: str = "", **_kw) -> dict:
        return {"rows": [{"kills": 300}, {"kills": 180}], "_sql": sql_query}


def _isolate(tmp: Path) -> None:
    """所有落盘路径指临时目录（同 test_scene_isolation 的隔离标准）。"""
    actions.SNAPSHOT = tmp / "action_registry.json"
    skill_store.STORE = tmp / "skill_store.json"
    plugin_forge.PLUGIN_DIR = tmp / "plugins"
    causal_timeline.LOG = tmp / "causal_timeline.jsonl"
    scenes.REGISTRY = tmp / "scenes.json"
    routing.PENDING_DIMENSIONS = tmp / "pending_dimensions.json"
    # KnowledgeGraph.load/save 的默认参数是定义时绑定的 → 包装换临时路径。
    # 从类上取到的 classmethod 已绑 cls，包装里只传 path。
    graph_tmp = tmp / "knowledge_graph.json"
    _KG = knowledge_graph.KnowledgeGraph
    _real_load, _real_save = _KG.load, _KG.save
    knowledge_graph.KnowledgeGraph.load = classmethod(
        lambda cls, path=graph_tmp: _real_load(path))
    knowledge_graph.KnowledgeGraph.save = lambda self, path=graph_tmp: \
        _real_save(self, path)
    mastery_tmp = tmp / "topic_mastery.jsonl"
    _RealStore = topic_graph.MasteryStore

    class _TmpStore(_RealStore):
        def __init__(self, path=None):
            super().__init__(path=mastery_tmp)

    topic_graph.MasteryStore = _TmpStore
    import voyager_vlml.consume as _consume
    _consume._RUNTIME_TOPICS_FILE = tmp / "runtime_topics.json"
    # 合成真 duckdb（information_schema 实查，与线上同一把尺子）
    import duckdb
    db = tmp / "synthetic.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE players (player_id INTEGER, map_id INTEGER, kills INTEGER)")
    con.execute("CREATE TABLE maps (map_id INTEGER, map_name VARCHAR)")
    con.close()
    db_switch.CONFIG = tmp / "db_config.json"
    db_switch.CONFIG.write_text(json.dumps({"db_path": str(db)}),
                                encoding="utf-8")


def _nodes() -> dict:
    return dict(knowledge_graph.KnowledgeGraph.load().nodes)


async def _main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mve_learn_"))
    _isolate(tmp)
    fake = types.ModuleType("voyager_vlml.agents.action")
    fake.VLMLActionAgent = _FakeAgent
    sys.modules["voyager_vlml.agents.action"] = fake
    # 泛化（generalize_sql_recipe）打桩：技能入库前要把 SQL 泛化成 :slot
    # 模板（2026-10-23「技能存算法不存数值」），真调 LLM 输出不稳定
    # （2026-10-26 实测：同一输入五次三种写法）→ 测试结果会随机挂。
    # 固定成「原 SQL + WHERE m.name = :map」（合法模板：槽位只在 WHERE
    # 值位），入库/结晶/重放的契约全部可确定断言。
    import voyager_vlml.consume as _consume_mod
    _consume_mod.chat_json = lambda _messages, **_kw: {
        "sql": SQL + " WHERE m.name = :map",
        "slots": ["map"], "note": "test stub"}

    print("== 0. 人导入（未命中）==")
    m0 = routing.match(QUESTION, SCENE)
    check("新问题判 REJECTED（未命中）", m0["decision"] == "REJECTED",
          json.dumps(m0, ensure_ascii=False)[:80])
    check("此时图谱里没有这个问题", len(_nodes()) == 0)

    print("\n== 1. 建图谱节点（未命中当场）==")
    miss = learn_orchestrate.on_miss(QUESTION, SCENE, reason="双阈值未命中")
    nodes = _nodes()
    node = nodes.get(str(miss.get("node_id")))
    check("建了节点且是新建", miss["node_created"] is True
          and node is not None, str(miss.get("node_id")))
    check("节点归属当前场景 A", getattr(node, "root_scene", "") == SCENE,
          str(getattr(node, "root_scene", "")))
    check("状态=cold_start_guess（猜测项，待解题升级）",
          str(getattr(node, "status", "")) == "cold_start_guess")
    check("label 就是这句话（路由能认回来）",
          QUESTION[:20] in str(getattr(node, "label", "")))
    check("待审区有登记（candidate_knowledge_items 同构）",
          bool(miss.get("pending_id")) and
          routing.pending_counts().get("pending_review", 0) >= 1,
          str(routing.pending_counts()))
    check("再导同一问题已能命中节点（不再是陌生问题）",
          routing.match(QUESTION, SCENE)["decision"] != "REJECTED")

    print("\n== 2. voyager 规划课程自主学习（拆解 → 抓手可达 → 取数 → 入库）==")
    tid = str(miss["pending_id"])
    check("学前：里程碑未解锁（unlocked_milestones 里没有它）",
          tid not in scene_context.unlocked_milestones())
    r = await learn_orchestrate.start_learning(QUESTION, SCENE, llm=_fake_llm)
    check("status=learned", r["status"] == "learned", str(r.get("why"))[:80])
    check("拆解出子目标（2 个）", len(r["plan"]["subgoals"]) == 2,
          str(r["plan"]["subgoals"]))
    check("任务有抓手：forge 后全部 reachable",
          r["forged"]["counts"].get("reachable_after") == 2
          and r["stuck"] == [], json.dumps(r["forged"]["counts"], ensure_ascii=False))
    check("执行取到真数（任务达成）",
          r["execution"].get("ok") is True and r["execution"].get("sql") == SQL)
    check("抓手条目真的交给了执行层（同构 MCP tools/list，非空）",
          len(getattr(_FakeAgent, "last_grabs", []) or []) > 0
          and r.get("grabs_offered", 0) > 0,
          f"grabs_offered={r.get('grabs_offered')}")
    items = skill_store.all_skills()
    # 入库的是**槽位模板**（技能存算法不存数值）：code 带 :map；判对后
    # 固化的原文（泛化前）留 blueprint.rollout_sql —— 结晶条目重放它，
    # 检索+回填通道用模板（_refill_sql 填槽）。2026-10-26 修：旧断言
    # 「code == 本次 SQL」在泛化上线后必败（库里存的是模板不是原文）。
    check("计入技能库（入库的是槽位模板，原文留 blueprint.rollout_sql）",
          any(":map" in str(s.get("code") or "") for s in items)
          and any(str((s.get("blueprint") or {}).get("rollout_sql") or "") == SQL
                  for s in items),
          json.dumps([str(s.get("code"))[:70] for s in items], ensure_ascii=False))
    check("_absorb 指认了入库的那一条（skill_key，不按 code 匹配）",
          bool(str((r["absorbed"] or {}).get("skill_key") or "")),
          str((r["absorbed"] or {}).get("skill_key")))
    learned = [n for n in actions.names(SCENE) if n.startswith("learned_")]
    check("配方结晶成可 invoke 条目", r["crystallized"].get("ok") is True
          and len(learned) == 1, str(learned))

    print("\n== 3. 里程碑解锁（不再重复出题）==")
    unlocked = scene_context.unlocked_milestones()
    check("原披露口径：该能力项已解锁（completed=True，规划不再推它）",
          tid in unlocked, f"unlocked={len(unlocked)} 项")
    node2 = _nodes().get(str(miss.get("node_id")))
    check("节点证据升级：cold_start_guess → verified",
          str(getattr(node2, "status", "")) == "verified"
          and int(getattr(node2, "evidence", 0) or 0) >= 2,
          f"{getattr(node2, 'status', '')}/evidence={getattr(node2, 'evidence', 0)}")
    check("学习结果用同一口径披露解锁", r["milestone"].get("unlocked") is True,
          json.dumps(r["milestone"], ensure_ascii=False))
    check("学习记录进因果时间线（面板可见）",
          any(str(f.get("kind") or "") == "learn"
              for f in causal_timeline.load()))

    print("\n== 4. 人再导入相同问题（命中且解决）==")
    m1 = routing.match(QUESTION, SCENE)
    check("命中（不再 REJECTED）", m1["decision"] != "REJECTED",
          f"{m1['decision']} / {m1.get('node_label')}")
    found = skill_store.retrieve("", QUESTION)
    check("检索到配方（解决的前提）", len(found) >= 1, f"{len(found)} 条")
    res = await actions.invoke(SCENE, learned[0], mcp=_FakeMCP())
    check("配方条目能重放出数据（解决）",
          isinstance(res, dict) and bool(res.get("rows")), str(res)[:60])
    check("重放用的就是学到的 SQL", res.get("_sql") == SQL)

    print("\n== 5. 负路径（原版铁律：没取到真数不写技能库）==")
    _FakeAgent.vacuous = True
    q2 = "查一下 matches 的 series 分布"
    learn_orchestrate.on_miss(q2, SCENE)
    nodes_before = len(_nodes())
    r2 = await learn_orchestrate.start_learning(q2, SCENE, llm=_fake_llm)
    _FakeAgent.vacuous = False
    check("status=no_data", r2["status"] == "no_data", str(r2.get("why"))[:50])
    check("没入库/没结晶/没建新节点",
          r2["absorbed"] == {} and r2["crystallized"] == {}
          and len(_nodes()) == nodes_before)
    r3 = await learn_orchestrate.start_learning(
        "特斯拉股价今天是多少", SCENE, llm=lambda m: '["Get Tesla stock price"]')
    check("库里无相关表 → unreachable 如实报", r3["status"] == "unreachable",
          str(r3.get("why"))[:60])

    print("\n== 6. kick_learning：导入就把学习踢起来（异步上下文）==")
    # 用假实现替掉真学习，只验「踢没踢起来 / 真的跑到了」这条接线，
    # 不然会真调 LLM（慢且不可控）。
    ran: list[str] = []

    async def _fake_start(q, sid="", **kw):
        ran.append(q)
        return {"question": q, "scene_id": sid, "status": "learned"}

    _real_start = learn_orchestrate.start_learning
    learn_orchestrate.start_learning = _fake_start
    try:
        k1 = learn_orchestrate.kick_learning(QUESTION, SCENE)
        check("异步上下文里 kick 成功", k1.get("kicked") is True, str(k1))
        t1 = learn_orchestrate._BG.get(QUESTION)
        if t1 is not None and hasattr(t1, "join"):
            t1.join(timeout=5)
        check("后台真的跑起来了（不是起了个立刻被回收的协程）",
              ran == [QUESTION], str(ran))
        k2 = learn_orchestrate.kick_learning(QUESTION, SCENE) \
            if (t1 is not None and getattr(t1, "is_alive", lambda: False)()) \
            else {"kicked": False, "why": "已跑完，不重复踢"}
        check("同一问题不重复起两份", k2.get("kicked") is False, str(k2))
    finally:
        learn_orchestrate.start_learning = _real_start

    print(f"\n===== 结果：{len(PASS)} 过 / {len(FAIL)} 挂 =====")
    if FAIL:
        for f in FAIL:
            print(f"  ❌ {f}")
        return 1
    print("全部通过 ✅")
    return 0


def main() -> int:
    code = asyncio.run(_main())
    # 同步上下文（面板 consume 线程之外、coach 之类）也必须能踢起来：
    # 学习挂在**独立线程的自己的事件循环**上，不受调用方循环退出影响
    # （2026-10-19 实测：原来用 loop.create_task，asyncio.run 收尾会把任务
    #  cancel 掉 —— 表现为「点了导入、说已启动学习，但从来没学过」）。
    ran: list[str] = []

    async def _fake_start(q, sid="", **kw):
        ran.append(q)
        return {"question": q, "scene_id": sid, "status": "learned"}

    _real_start = learn_orchestrate.start_learning
    learn_orchestrate.start_learning = _fake_start
    try:
        k = learn_orchestrate.kick_learning("循环外的问题", SCENE)
        t = learn_orchestrate._BG.get("循环外的问题")
        if t is not None and hasattr(t, "join"):
            t.join(timeout=5)
    finally:
        learn_orchestrate.start_learning = _real_start
    ok = bool(k.get("kicked")) and ran == ["循环外的问题"]
    print(f"\n== 7. 同步上下文 kick ==\n  {'✅' if ok else '❌'} "
          f"kicked={k.get('kicked')} 且真的跑到了 —— {ran}")
    return 0 if (ok and code == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
