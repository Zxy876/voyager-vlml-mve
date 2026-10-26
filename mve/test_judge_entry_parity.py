#!/usr/bin/env python3
"""判定入口同构（2026-10-26）：维度汇总不花 LLM，就不该被 judge 形参管。

背景（服务器实测复验候选A 时浮出）：面板「▶ 学会这句」按钮走
`start_learning(question)`（judge=False），而 ⑤' 完成判定整段曾挂在
`if judge:` 下面 → 一条 0/6 维度全败的学习轮跳过汇总，被 ⑥ `_absorb`
的「有答案文本」判成 learned（run_log verdict=ok、面板显示成功）——
「有行≠答出来，判定过同一把尺子」的铁律被入口差异击穿。

每条维度的闭合判定在各自 rollout 内已经做完（步级双判定：内层 critic
× 闸门），轮级汇总只是数 failed 条数、**不花一次 LLM** —— 所以它必须
对两个入口同一个口径：

  ① 负路径：judge=False + 全部维度未闭合 → status=failed、
     critic.success=False、⑥ 之前早退（absorbed={}：不入库、不建点、
     不结晶）—— 修复前这里会报 learned；
  ② 负路径 parity：judge=True 同口径（汇总与 judge 无关）；
  ③ 正路径：judge=False + 维度闭合 → status=learned（汇总不拦好人），
     且闭合维度的技能带 blueprint.rollout_sql（泛化前原文，结晶重放用）。

场景必须注册成**带库场景**（kind≠vlml）：默认场景 A 是 VLML 数据域，
`_use_scaffold = is_local or (db_path and not use_vlml)` 为 False，不走
维度循环（2026-10-26 实测：面板场景 B 工资库才是 scaffold 路径）。

运行：python mve/test_judge_entry_parity.py
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
import maieutic_scaffold      # noqa: E402
import plugin_forge           # noqa: E402
import routing                # noqa: E402
import run_log                # noqa: E402
import scenes                 # noqa: E402
import skill_store            # noqa: E402
import topic_graph            # noqa: E402

QUESTION = "查一下 player 的 kills 按地图过滤"
SQL = "SELECT p.kills FROM players p JOIN maps m ON p.map_id = m.map_id"
SCENE = "T"                    # 带库场景（create_scene + data_source）

PASS: list[str] = []
FAIL: list[str] = []
_DB = ""                       # _isolate 里建的合成库路径


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" —— {detail}" if detail else ""))


class _FakeAgent:
    """假执行器：solve() 回真数观察 + 固定 SQL；闭合与否由类属性控制。"""

    last_success = False          # 内层 critic 末态（False = 每步都不过）
    last_gate_passed = None       # 闸门没跑（solve 不经过 step）
    last_gate_critique = ""
    last_gate_result: dict = {}
    critic = None
    critic_reasoning = ""
    # 步级闸门问过的句子（真 agent 由 step() 累积）—— 验证 asked_q 落盘
    _gate_asked = ["桩追问：请从 players 表取 kills 并按 maps 过滤"]
    vacuous = False
    last_grabs: list = []

    def __init__(self, env=None) -> None:
        pass

    async def solve(self, question, *, max_iters=3, hints=None, skills=None,
                    grabs=None, focus_tables=None, gate_check=None, **_kw):
        _FakeAgent.last_grabs = list(grabs or [])
        data = ([{"n": None}] if self.vacuous
                else [{"kills": 300}, {"kills": 180}])
        return ([{"ok": True, "rows": len(data), "data": data,
                  "tool": "query_sql"}], SQL)


def _fake_llm(_messages: list[dict]) -> str:
    # 唯一一次 LLM 调用 = scaffold 出维度（plan 在 scaffold 成功后不再调
    # decompose）。两条维度，形状同 maieutic_scaffold._clean_dims 的契约。
    return json.dumps({
        "dimensions": [
            {"id": "dim_kills", "question": "player 的 kills 怎么查？",
             "why": "kills 是本题的主量"},
            {"id": "dim_map", "question": "按地图过滤是什么意思？",
             "why": "地图是本题的筛选维度"}],
        "milestone": {"name": "kills 按地图过滤", "why": "两维都答清即达成"},
        "expected_drifts": []}, ensure_ascii=False)


class _FakeMCP:
    async def query_sql(self, sql_query: str = "", **_kw) -> dict:
        return {"rows": [{"kills": 300}, {"kills": 180}], "_sql": sql_query}


def _isolate(tmp: Path) -> None:
    """所有落盘路径指临时目录（同 test_learn_orchestrate 的隔离标准）。"""
    global _DB
    actions.SNAPSHOT = tmp / "action_registry.json"
    skill_store.STORE = tmp / "skill_store.json"
    plugin_forge.PLUGIN_DIR = tmp / "plugins"
    causal_timeline.LOG = tmp / "causal_timeline.jsonl"
    scenes.REGISTRY = tmp / "scenes.json"
    routing.PENDING_DIMENSIONS = tmp / "pending_dimensions.json"
    run_log.LOG = tmp / "run_log.jsonl"
    maieutic_scaffold.CKPT = tmp / "ckpt_maieutic"
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
    # 泛化打桩（技能入库前 SQL → :slot 模板）：固定返回合法模板，不断网。
    _consume.chat_json = lambda _messages, **_kw: {
        "sql": SQL + " WHERE m.name = :map",
        "slots": ["map"], "note": "test stub"}
    # 里程碑复合技能的组合 LLM 也打桩：返回空 → 如实 skipped（不联网）。
    import voyager_vlml.llm as _llm
    _llm.chat_json = lambda _messages, **_kw: {"sql": "", "note": "stub"}
    # 合成真 duckdb（information_schema 实查）
    import duckdb
    db = tmp / "synthetic.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE players (player_id INTEGER, map_id INTEGER, kills INTEGER)")
    con.execute("CREATE TABLE maps (map_id INTEGER, map_name VARCHAR)")
    con.close()
    _DB = str(db)
    db_switch.CONFIG = tmp / "db_config.json"
    db_switch.CONFIG.write_text(json.dumps({"db_path": _DB}),
                                encoding="utf-8")
    # 注册带库场景：kind≠vlml 才会走 scaffold 维度循环（场景 A 是 VLML 域）
    scenes.create_scene("kills 测试场景", scene_id=SCENE, data_source=_DB)


def _install_fake_agent() -> None:
    fake = types.ModuleType("voyager_vlml.agents.action")
    fake.VLMLActionAgent = _FakeAgent
    sys.modules["voyager_vlml.agents.action"] = fake


def _ms_progress() -> list[dict]:
    """读回落盘的追问历史（iterations 列表）。"""
    out: list[dict] = []
    try:
        for p in sorted(Path(maieutic_scaffold.CKPT).glob("*/*.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            its = ((d.get("progress") or {}).get("iterations")) or []
            if isinstance(its, list):
                out.extend(its)
    except Exception:
        pass
    return out


async def _main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mve_judge_"))
    _isolate(tmp)
    _install_fake_agent()
    import learn_orchestrate

    print("== ① judge=False（面板按钮入口）+ 全部维度未闭合 → 必须诚实报败 ==")
    _FakeAgent.last_success = False
    _FakeAgent.vacuous = False
    r = await learn_orchestrate.start_learning(
        QUESTION, SCENE, llm=_fake_llm, use_mcp=False)
    check("拆出了维度（走维度循环）",
          (r.get("dimension_results") or {}).get("total") == 2,
          json.dumps(r.get("dimension_results"), ensure_ascii=False)[:120])
    check("status=failed（不是 learned）", r["status"] == "failed",
          f"{r['status']}｜{str(r.get('why'))[:60]}")
    check("critic.success=False 且列了未闭合维度",
          (r.get("critic") or {}).get("success") is False
          and len(((r.get("critic") or {}).get("gate") or {}).get("open") or []) == 2,
          json.dumps(r.get("critic"), ensure_ascii=False)[:160])
    check("⑥ 之前早退：没入库、没建点、没结晶",
          r["absorbed"] == {} and r["crystallized"] == {},
          json.dumps({"absorbed": r.get("absorbed"),
                      "crystallized": r.get("crystallized")},
                     ensure_ascii=False)[:120])
    its = _ms_progress()
    check("追问历史落盘（asked_q，下一轮续学用）",
          any((it.get("asked_q") or []) for it in its),
          json.dumps([it.get("asked_q") for it in its], ensure_ascii=False)[:120])

    print("\n== ② judge=True（驾驶舱入口）同口径 ==")
    r2 = await learn_orchestrate.start_learning(
        QUESTION, SCENE, llm=_fake_llm, use_mcp=False, judge=True)
    check("judge=True 同样报 failed（汇总与 judge 形参无关）",
          r2["status"] == "failed", f"{r2['status']}｜{str(r2.get('why'))[:60]}")

    print("\n== ③ judge=False + 维度闭合 → 汇总不拦好人 ==")
    _FakeAgent.last_success = True
    r3 = await learn_orchestrate.start_learning(
        QUESTION, SCENE, llm=_fake_llm, use_mcp=False)
    check("status=learned", r3["status"] == "learned",
          f"{r3['status']}｜{str(r3.get('why'))[:60]}")
    check("两条维度都闭合", len((r3.get("dimension_results") or {}).get(
        "learned") or []) == 2,
        json.dumps(r3.get("dimension_results"), ensure_ascii=False)[:120])
    added = [a for a in ((r3.get("skills") or {}).get("added") or [])]
    check("闭合维度写进技能库（2 条）", len(added) == 2, str(len(added)))
    if added:
        keys = {str(a.get("key")) for a in added}
        stored = [s for s in skill_store.all_skills()
                  if str(s.get("key")) in keys]
        check("技能带 blueprint.rollout_sql（泛化前原文，结晶重放用它）",
              len(stored) == 2
              and all(str((s.get("blueprint") or {}).get("rollout_sql")
                          or "") == SQL for s in stored),
              json.dumps([str((s.get("blueprint") or {}).get("rollout_sql"))
                          for s in stored], ensure_ascii=False)[:120])

    print()
    if FAIL:
        print(f"失败 {len(FAIL)} 项：{FAIL}")
        return 1
    print(f"全部通过（{len(PASS)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
