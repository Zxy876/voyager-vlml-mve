#!/usr/bin/env python3
"""场景后端（数据源）验收：场景隔离 + 数据源就是本机。

用户 2026-10-19 三条原话就是本文件的验收线：

  1. 「启动 voyager 学习时场景隔离，只学习当前场景」
     → 非默认场景学习/消费**不许**连 VLML、不许读 rib
  2. 「而且在别的场景下，voyager 的学习和 VLML 没有关系」
     → 断言：整条链路上 `VLMLMCPEnv` 一次都没被构造
  3. 「对于普通用户而言，数据源就是本机不是吗」
     → 场景目录里的文件 + 本机只读原语就是数据源，不需要用户配库

对照物：截图实测（Scene B 问「现在最前沿的世界模型是什么」→ 答案列出一串
G2 Esports / LOUD 战队）。所以最后一条断言专门检查**答案里不许出现 rib 的东西**。

全程临时目录隔离 + 假 LLM（写代码的那一步由队列喂固定代码），本机没有
vlml/ 数据层也能跑。

运行：python mve/test_scene_backend.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
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
import local_agent            # noqa: E402
import plugin_forge           # noqa: E402
import routing                # noqa: E402
import scene_profile          # noqa: E402
import scenes                 # noqa: E402
import skill_store            # noqa: E402
import topic_graph            # noqa: E402

# coach 模块在 import 期就 `import vlml_env`（chdir 到 vlml/），本机没有 vlml/
# 数据层 → 直接 import 会失败。面板侧它由 vlml 环境引导；测试里只用到
# `_log_import`（人导入日志），用桩顶替即可，断言反而更直接。
import types                  # noqa: E402
LOGS: list[dict] = []
_coach_stub = types.ModuleType("coach")
_coach_stub._log_import = lambda r: LOGS.append(dict(r))
sys.modules.setdefault("coach", _coach_stub)


QUESTION = "现在最前沿的世界模型是什么"
LOCAL_ANSWER = "V-JEPA 2"

# 截图里出现过的东西：答案里一个都不许有
FORBIDDEN = ("G2", "LOUD", "players", "maps", "match_id", "kd_ratio")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" —— {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 假 LLM：本地执行层要的是「模型写一段 Python」，这里按队列喂
# ---------------------------------------------------------------------------
class FakeLLM:
    def __init__(self) -> None:
        self.queue: list[str] = []
        self.prompts: list[list[dict]] = []

    def push(self, *texts: str) -> None:
        self.queue.extend(texts)

    def __call__(self, messages, **_kw):
        self.prompts.append(list(messages))
        return self.queue.pop(0) if self.queue else "无"


FAKE = FakeLLM()


def _code_read_metrics() -> str:
    return (
        "```python\n"
        "f = read_file('metrics.json')\n"
        "RESULT = {'file': f['path'], 'content': f['text'],"
        " 'level': len(f['text'])}\n"
        "```"
    )


def _code_self_report_error() -> str:
    return '```python\nRESULT = {"error": "本机找不到需要的数据"}\n```'


def _isolate(tmp: Path) -> None:
    """所有落盘路径 + 场景目录指临时目录（同 test_learn_orchestrate 标准）。"""
    actions.SNAPSHOT = tmp / "action_registry.json"
    skill_store.STORE = tmp / "skill_store.json"
    plugin_forge.PLUGIN_DIR = tmp / "plugins"
    causal_timeline.LOG = tmp / "causal_timeline.jsonl"
    scenes.REGISTRY = tmp / "scenes.json"
    routing.PENDING_DIMENSIONS = tmp / "pending_dimensions.json"
    scene_profile.SCENE_DATA_DIR = tmp / "scene_data"

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

    # 假 VLML 库（rib 的替身）：如果哪个场景偷偷读了它，答案里就会出现
    # players / maps —— 这正是截图里那次事故的形态。
    import duckdb
    db = tmp / "vlml_like_rib.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE players (player_id INTEGER, map_id INTEGER, kills INTEGER)")
    con.execute("CREATE TABLE maps (map_id INTEGER, map_name VARCHAR)")
    con.execute("INSERT INTO maps VALUES (1, 'Ascent')")
    con.close()
    db_switch.CONFIG = tmp / "db_config.json"
    db_switch.CONFIG.write_text(json.dumps({"db_path": str(db)}), encoding="utf-8")

    # 消费侧的模型调用全部由假 LLM 顶替（派生评分点 / 写本机代码）
    import voyager_vlml.consume as _consume
    import voyager_vlml.llm as _llm
    _consume._RUNTIME_TOPICS_FILE = tmp / "runtime_topics.json"
    _consume.chat_json = lambda *_a, **_k: {"points": []}
    _llm.chat = FAKE
    # 后台学习线程不在这里起（本文件单独测学习链路）
    learn_orchestrate.kick_learning = lambda *_a, **_k: {
        "kicked": False, "why": "测试内不起后台线程"}


def _scene_dir(sid: str) -> Path:
    return Path(scene_profile.scene_dir(sid))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mve_backend_"))
    _isolate(tmp)

    # =====================================================================
    print("== 1. 场景 → 后端四分（数据源判定）==")
    # =====================================================================
    reg = scenes._load()
    scenes.create_scene("本机场景", scene_id="B")          # 普通场景：什么都不配
    scenes.create_scene("特斯拉", scene_id="C", data_source=str(tmp / "tesla.duckdb"))
    scenes.create_scene("缺库", scene_id="D", data_source=str(tmp / "nope.duckdb"))
    scenes.create_scene("自带库", scene_id="E")
    import duckdb
    con = duckdb.connect(str(tmp / "tesla.duckdb"))
    con.execute("CREATE TABLE vehicles (model VARCHAR, range_km INTEGER)")
    con.execute("INSERT INTO vehicles VALUES ('Model 3', 606)")
    con.close()
    con = duckdb.connect(str(_scene_dir("E") / "own.duckdb"))
    con.execute("CREATE TABLE notes (t VARCHAR)")
    con.close()

    pA = scene_profile.profile("A")
    check("A（默认场景）= VLML 后端", pA["kind"] == "vlml",
          f"{pA['kind']} · {pA['db_path']}")
    check("A 的表来自当前 VLML 库（players/maps）",
          "players" in pA["tables"] or "maps" in pA["tables"], str(pA["tables"]))

    pB = scene_profile.profile("B")
    check("B（没配库）= 本机后端，不是 VLML", pB["kind"] == "local", pB["kind"])
    check("B 的场景目录被建出来（普通用户不用配库）",
          Path(pB["scene_dir"]).is_dir() and pB["scene_dir"].endswith("/B"),
          pB["scene_dir"])
    check("B 不继承默认库（db_path 为空）", not pB["db_path"], pB["db_path"])

    pC = scene_profile.profile("C")
    check("C（配了自己的库）= 场景库，且能实查到自己的表",
          pC["kind"] == "scene" and "vehicles" in pC["tables"],
          f"{pC['kind']} · {pC['tables']}")

    pD = scene_profile.profile("D")
    check("D（配了不存在的路径）= missing，如实说", pD["kind"] == "missing",
          pD["why"][:40])

    pE = scene_profile.profile("E")
    check("E（没配库但场景目录里放了 .duckdb）= 用那个库",
          pE["kind"] == "scene" and "notes" in pE["tables"], str(pE["tables"]))

    # =====================================================================
    print("\n== 2. 本机原语（数据源 = 本机）==")
    # =====================================================================
    d = str(_scene_dir("B"))
    Path(d, "metrics.json").write_text(
        json.dumps({"latest": LOCAL_ANSWER, "task": "video prediction"},
                   ensure_ascii=False), encoding="utf-8")
    Path(d, "notes.txt").write_text("备注：只在本机", encoding="utf-8")

    names = [r["name"] for r in local_agent.list_files(d)]
    check("list_files 列出场景目录文件", "metrics.json" in names and
          "notes.txt" in names, str(sorted(names)))
    got = local_agent.read_file(d, "metrics.json")
    check("read_file 读到真内容", LOCAL_ANSWER in got["text"], got["path"])

    try:
        local_agent.read_file(d, "/etc/passwd")
        blocked = False
    except PermissionError:
        blocked = True
    check("越界路径被拒（只许场景目录 / 家目录）", blocked)

    try:
        local_agent.query_sql(d, "SELECT 1")
        no_db = False
    except FileNotFoundError:
        no_db = True
    check("场景目录没有 .duckdb 时 query_sql 如实报没有", no_db)

    # =====================================================================
    print("\n== 3. 本机执行层：模型写代码 → 跑 → 取到本机真数 ==")
    # =====================================================================
    FAKE.queue.clear()
    FAKE.push(_code_read_metrics())
    obs, code = asyncio.run(local_agent.solve(QUESTION, d, llm=FAKE))
    check("本机取到真数（ok=True）", bool(obs and obs[0]["ok"]),
          json.dumps(obs, ensure_ascii=False)[:90] if obs else "无观察")
    check("取到的是本机文件里的真值",
          LOCAL_ANSWER in json.dumps(obs[0].get("data"), ensure_ascii=False),
          json.dumps(obs[0].get("data"), ensure_ascii=False)[:70])
    check("用的是本机原语（trajectory=local_python）", obs[0]["tool"] == "local_python")

    # 模型自报取不到：必须当失败（真空成功的老毛病），且把反馈喂回去重写
    FAKE.queue.clear()
    FAKE.push(_code_self_report_error(), _code_read_metrics())
    obs2, _ = asyncio.run(local_agent.solve(QUESTION, d, llm=FAKE, max_iters=2))
    check("RESULT 自报 error 不算取到数（不入『真空成功』坑）",
          len(obs2) == 2 and obs2[0]["ok"] is False, str([o["ok"] for o in obs2]))
    check("失败原因如实带回去（不是静默重试）",
          "本机找不到需要的数据" in str(obs2[0].get("error")),
          str(obs2[0].get("error"))[:50])
    check("第二次改对了才算成功", obs2[-1]["ok"] is True)
    check("重写时把上一次的代码与反馈给了模型",
          "上一次的代码" in json.dumps(FAKE.prompts[-1], ensure_ascii=False))

    # 路径越界：模型想读别人的文件 → 受限命名空间里没有 open，读不到
    FAKE.queue.clear()
    FAKE.push("```python\nRESULT = open('/etc/passwd').read()\n```")
    obs3, _ = asyncio.run(local_agent.solve(QUESTION, d, llm=FAKE, max_iters=1))
    check("模型代码里没有 open/import（受限命名空间）", obs3[0]["ok"] is False,
          str(obs3[0].get("error"))[:60])

    # =====================================================================
    print("\n== 4. 本机技能结晶：重放（不是把答案写死）==")
    # =====================================================================
    FAKE.queue.clear()
    FAKE.push(_code_read_metrics())
    obs4, code4 = asyncio.run(local_agent.solve(QUESTION, d, llm=FAKE))
    skill_store.add("本机世界模型", "local_world_model",
                    "读场景目录里的 metrics.json，取出最前沿的世界模型名字",
                    code=code4, source="consume", source_scene="B")
    sk = next(s for s in skill_store.all_skills()
              if str(s.get("code")) == str(code4))
    cry = plugin_forge.crystallize_skill(sk, into_scene="B", local_scene_dir=d)
    check("本机技能结晶成功（第三形态：run_saved 重放）", cry["ok"], cry["why"])
    if cry["ok"]:
        v1 = asyncio.run(actions.invoke("B", cry["entry"]))
        check("invoke 重放取到真值",
              LOCAL_ANSWER in json.dumps(v1, ensure_ascii=False),
              json.dumps(v1, ensure_ascii=False)[:70])
        Path(d, "metrics.json").write_text(
            json.dumps({"latest": "Genie 3"}, ensure_ascii=False), encoding="utf-8")
        v2 = asyncio.run(actions.invoke("B", cry["entry"]))
        check("改了本机文件后重放结果跟着变（证明是重放，不是写死答案）",
              "Genie 3" in json.dumps(v2, ensure_ascii=False),
              json.dumps(v2, ensure_ascii=False)[:70])
        Path(d, "metrics.json").write_text(
            json.dumps({"latest": LOCAL_ANSWER}, ensure_ascii=False), encoding="utf-8")
        check("本机技能没被注册成 SQL 形态（不含 query_sql 包裹）",
              "run_saved" in Path(cry["file"]).read_text(encoding="utf-8"))

    # =====================================================================
    print("\n== 5. 消费侧：Scene B 全程不碰 VLML（截图那次事故）==")
    # =====================================================================
    import voyager_vlml.consume as consume_mod
    built: list[str] = []

    class _BombEnv:
        def __init__(self, *a, **kw):
            built.append("VLMLMCPEnv")
            raise AssertionError("非 VLML 场景竟然构造了 VLMLMCPEnv")

    real_env = consume_mod.VLMLMCPEnv
    consume_mod.VLMLMCPEnv = _BombEnv
    try:
        scenes.switch_scene("B")
        check("当前场景 = B", scenes.current_scene_id() == "B")
        FAKE.queue.clear()
        FAKE.push(_code_read_metrics())
        res = asyncio.run(consume_mod.consume(QUESTION))
    finally:
        consume_mod.VLMLMCPEnv = real_env

    check("整条链路没构造过 VLML 环境", built == [], str(built))
    check("记为本机后端", res.get("scene_kind") == "local", str(res.get("scene_kind")))
    check("答案是本机真值", LOCAL_ANSWER in str(res.get("answer")),
          str(res.get("answer"))[:60])
    blob = json.dumps(res, ensure_ascii=False)
    leaked = [t for t in FORBIDDEN if t in blob]
    check("答案里没有 rib 的任何痕迹（不再列战队）", not leaked, str(leaked))

    # 本机没数据时：如实说「没取到」，不编、也不偷偷去读 rib
    scenes.create_scene("空场景", scene_id="F")
    scenes.switch_scene("F")
    FAKE.queue.clear()
    FAKE.push(_code_self_report_error())
    empty = asyncio.run(consume_mod.consume("量子计算机现在的纠错水平"))
    check("本机取不到 → judgment_result=fetched_empty",
          empty.get("judgment_result") == "fetched_empty",
          str(empty.get("judgment_result")))
    check("如实告诉人怎么办（放数据文件 / 换个问法）",
          "本机" in str(empty.get("skipped")) and empty.get("answer") == "",
          str(empty.get("skipped"))[:70])

    # =====================================================================
    print("\n== 6. 学习侧同样按后端分流（A 走 VLML，B 走本机）==")
    # =====================================================================
    FAKE.queue.clear()
    FAKE.push('["找到本机的模型榜单", "读出最前沿的那个"]',
              _code_read_metrics())
    scenes.switch_scene("B")
    outB = asyncio.run(learn_orchestrate.start_learning(
        QUESTION, "B", llm=FAKE, use_mcp=False))
    check("B 的后端 = local", outB["backend"]["kind"] == "local",
          json.dumps(outB["backend"], ensure_ascii=False)[:80])
    check("B 执行层 = 本机（不构造 VLML）",
          str(outB.get("execution_backend", "")).startswith("local:"),
          str(outB.get("execution_backend")))
    check("子目标抓手 = 本机只读原语（不是 rib 的表）",
          all(s["status"] == "reachable" for s in outB["plan"]["subtasks"]),
          json.dumps(outB["plan"]["counts"], ensure_ascii=False))
    check("B 没去 forge rib 的插件", not outB["forged"], str(outB["forged"])[:60])
    check("B 学到东西后状态是 learned/partial",
          outB["status"] in ("learned", "partial"), outB["status"])
    check("B 结晶出来的是本机形态（run_saved）",
          bool(outB["crystallized"].get("ok"))
          and "run_saved" in Path(outB["crystallized"]["file"]).read_text(
              encoding="utf-8"),
          str(outB["crystallized"].get("entry")))
    check("B 学到的技能归属 B（不记到 A 头上）",
          all(s.get("source_scene") in ("B", "default")
              for s in skill_store.all_skills()),
          str([(s.get("name"), s.get("source_scene"))
               for s in skill_store.all_skills()])[:80])

    # A 回归：默认场景照旧走 VLML（假执行器，证明没被一刀砍掉）
    import types
    called: dict = {}

    class _FakeAgent:
        def __init__(self, env=None):
            called["env"] = env

        async def solve(self, question, *, max_iters=3, hints=None, skills=None,
                        grabs=None, focus_tables=None):
            called["grabs"] = list(grabs or [])
            return ([{"ok": True, "tool": "query_sql", "rows": 1,
                      "data": [{"kills": 300}]}],
                    "SELECT p.kills FROM players p")

    fake_mod = types.ModuleType("voyager_vlml.agents.action")
    fake_mod.VLMLActionAgent = _FakeAgent
    _real_mod = sys.modules.get("voyager_vlml.agents.action")
    sys.modules["voyager_vlml.agents.action"] = fake_mod
    try:
        scenes.switch_scene("A")
        FAKE.queue.clear()
        FAKE.push('["查 players 的 kills"]')
        outA = asyncio.run(learn_orchestrate.start_learning(
            "查一下 player 的 kills", "A", llm=FAKE, use_mcp=False))
    finally:
        if _real_mod is not None:
            sys.modules["voyager_vlml.agents.action"] = _real_mod

    check("A 的后端仍是 vlml（默认场景没被改成本机）",
          outA["backend"]["kind"] == "vlml", str(outA["backend"]["kind"]))
    check("A 的执行层是 VLML agent", "env" in called, str(list(called)))
    check("A 的候选表是 rib 的表（players）",
          "players" in (outA.get("focus_tables") or []),
          str(outA.get("focus_tables")))

    print(f"\n===== 结果：{len(PASS)} 过 / {len(FAIL)} 挂 =====")
    if FAIL:
        for f in FAIL:
            print(f"  ❌ {f}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
