#!/usr/bin/env python3
"""验证「缺口 → 锻造插件 / 技能 → 结晶条目」闭环（plugin_forge 的可复现剧本）。

对应用户指令「用猫娘计划的 mcp 去写插件，编排插件使用两个层面来完成任务」的
第一层（写插件）。全程不依赖真实 rib.gg / 大模型 / vlml/ 数据层：

  1. 合成一个真 duckdb（information_schema 实查，与线上同一把尺子）
  2. task_decompose.plan() 假 LLM 拆分 → 两个子目标都判 gap（场景无抓手）
  3. plugin_forge.forge_gaps() → 写插件文件 + 动态注册（origin=forged）
  4. 重判 reachability → reachable 且 grab 正是锻造出来的条目（闭环证据）
  5. invoke 条目：SQL 透传 / 默认 LIMIT / 传输层不可用时如实 RuntimeError
  6. crystallize_skill：可执行技能 → 条目（origin=learned）+ 真重放；
     旁路伪代码技能 → 照实 ok=False（学话术的不假装是程序）
  7. actions.restore() 重启恢复：forged/learned 条目都能从 impl 引用找回
  8. 幂等：再 forge 一次不产生重复条目；跨场景注册 scene_id 不互相污染

运行：python mve/test_plugin_forge.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import actions          # noqa: E402
import db_switch        # noqa: E402
import plugin_forge     # noqa: E402
import skill_store      # noqa: E402
import task_decompose   # noqa: E402


class FakeMCP:
    """记录 query_sql 收到的 sql_query（传输层的最小真身）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def query_sql(self, sql_query: str = "", **_kw) -> dict:
        self.calls.append(sql_query)
        return {"rows": [{"n": 1}]}


def _fake_llm(_messages: list[dict]) -> str:
    return '["Get player kills", "Filter by map name"]'


PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mve_forge_"))
    # --- 隔离：注册表快照 / 技能库 / 当前库 / 插件目录全指临时目录 ---
    actions.SNAPSHOT = tmp / "action_registry.json"
    skill_store.STORE = tmp / "skill_store.json"
    plugin_forge.PLUGIN_DIR = tmp / "plugins"
    db_switch.CONFIG = tmp / "db_config.json"

    # --- 合成真 duckdb：表清单/列名全部实查 information_schema ---
    db = tmp / "synthetic.duckdb"
    import duckdb
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE players (player_id INTEGER, nickname VARCHAR, kills INTEGER)")
    con.execute("CREATE TABLE maps (map_id INTEGER, map_name VARCHAR)")
    con.execute("CREATE TABLE matches (match_id INTEGER, map_name VARCHAR, series_id VARCHAR)")
    con.execute("INSERT INTO players VALUES (1, 'aspas', 300), (2, 'boaster', 180)")
    con.close()
    db_switch.CONFIG.write_text(json.dumps({"db_path": str(db)}), encoding="utf-8")

    SCENE = "T"
    print("\n== 1. 拆分 + 可达性判定（场景无抓手 → gap）==")
    plan = task_decompose.plan("查一下 player 的 kills 按地图过滤", SCENE,
                               llm=_fake_llm)
    check("拆出 2 个子目标（LLM origin）", len(plan["subgoals"]) == 2
          and plan["origin"] == "llm")
    check("两个子目标都判 gap", plan["counts"]["gap"] == 2,
          json.dumps(plan["counts"], ensure_ascii=False))
    check("gap_hint 命中实查表（复数词干：player→players）",
          plan["subtasks"][0].get("gap_hint") == ["players"],
          str(plan["subtasks"][0].get("gap_hint")))

    print("\n== 2. forge_gaps：写插件文件 + 动态注册 ==")
    forged = plugin_forge.forge_gaps(plan, SCENE)
    check("两个 gap 都 forge 成功", forged["counts"]["forged"] == 2
          and not forged["failed"])
    names = set(actions.names(SCENE))
    check("注册表多了 query_players / query_maps",
          {"query_players", "query_maps"} <= names, str(sorted(names)))
    entries = {e["name"]: e for e in actions.entries(SCENE)}
    check("forge 条目 origin=forged",
          entries.get("query_players", {}).get("origin") == "forged")
    pfile = Path(forged["forged"][0]["file"])
    check("插件文件落盘 mve/plugins/", pfile.exists()
          and pfile.parent == plugin_forge.PLUGIN_DIR)
    src = pfile.read_text(encoding="utf-8")
    check("条目描述带真实列名（结构实查派生）",
          "nickname" in src and "player_id" in src)
    check("条目描述带需求原文（否则重判匹配不上）",
          "Get player kills" in src)

    print("\n== 3. 重判可达性（闭环证据：forge 后抓得到）==")
    check("rechecked 里两个子目标都变 reachable",
          forged["counts"]["reachable_after"] == 2,
          json.dumps(forged["rechecked"], ensure_ascii=False))
    r = task_decompose.reachability("Get player kills", SCENE)
    check("grab 正是锻造出的 query_players",
          r["status"] == "reachable"
          and (r.get("grab") or {}).get("name") == "query_players")

    print("\n== 4. invoke 条目 ==")
    mcp = FakeMCP()
    asyncio.run(actions.invoke(SCENE, "query_players", mcp=mcp,
                               sql="SELECT * FROM players WHERE kills > 200"))
    check("SQL 透传到传输层", mcp.calls == ["SELECT * FROM players WHERE kills > 200"])
    mcp2 = FakeMCP()
    asyncio.run(actions.invoke(SCENE, "query_players", mcp=mcp2))
    check("不传 SQL 时用表默认 LIMIT 200",
          mcp2.calls == ["SELECT * FROM players LIMIT 200"])
    # 不传 mcp → 走 _new_handle()；这里注入假 action_code 让路径确定
    fake_ac = types.ModuleType("action_code")
    fake_ac.MCPHandle = FakeMCP
    old_ac = sys.modules.get("action_code")
    sys.modules["action_code"] = fake_ac
    try:
        mcp3 = FakeMCP()
        res = asyncio.run(actions.invoke(SCENE, "query_maps"))
        check("不传 mcp 时自建传输层句柄", res == {"rows": [{"n": 1}]})
        sys.modules["action_code"] = None      # None → import 必失败
        try:
            asyncio.run(actions.invoke(SCENE, "query_maps"))
            check("传输层不可用时如实 RuntimeError", False, "居然没报错")
        except RuntimeError as e:
            check("传输层不可用时如实 RuntimeError",
                  "传输层不可用" in str(e), str(e)[:60])
    finally:
        sys.modules["action_code"] = old_ac if old_ac is not None else fake_ac
        sys.modules.pop("action_code", None) if old_ac is None else None

    print("\n== 5. crystallize_skill：可执行技能 → 条目 ==")
    code = ("# 自己跑通的编排（t1）—— 重放即可，口径已由裁判验证\n"
            "async def demo_solve(mcp):\n"
            "    r = await mcp.query_sql(sql_query='SELECT count(*) AS n FROM players')\n"
            "    return {'_obs': [r]}\n")
    act, key = skill_store.add(
        "t1", "demo_skill", "自己跑通的编排（评分 100%）：查 player kills",
        source="practice", code=code, source_scene=SCENE)
    skills = {s["key"]: s for s in skill_store.all_skills()}
    cry = plugin_forge.crystallize_skill(skills[key], into_scene=SCENE)
    check("可执行技能结晶成功", cry["ok"], cry["why"])
    check("learned 条目注册进场景",
          "learned_demo_skill" in actions.names(SCENE))
    lentries = {e["name"]: e for e in actions.entries(SCENE)}
    check("learned 条目 origin=learned",
          lentries.get("learned_demo_skill", {}).get("origin") == "learned")
    mcp4 = FakeMCP()
    res4 = asyncio.run(actions.invoke(SCENE, "learned_demo_skill", mcp=mcp4))
    check("invoke learned 条目 = 真重放技能程序",
          mcp4.calls == ["SELECT count(*) AS n FROM players"]
          and res4 == {"_obs": [{"rows": [{"n": 1}]}]})

    print("\n== 6. 旁路伪代码技能：照实不结晶 ==")
    pseudo = "[first_blood] 首杀\n    SELECT count(*) FROM roundStats\n    # 取第 0 列"
    act2, key2 = skill_store.add(
        "t2", "pseudo_skill", "旁路观摩：按 roundStats 取首杀", code=pseudo,
        source="bypass", source_scene=SCENE)
    skills2 = {s["key"]: s for s in skill_store.all_skills()}
    cry2 = plugin_forge.crystallize_skill(skills2[key2], into_scene=SCENE)
    check("伪代码技能 ok=False 且说明原因",
          not cry2["ok"] and "伪代码" in cry2["why"], cry2["why"][:50])
    check("没有偷偷注册 learned_pseudo_skill",
          "learned_pseudo_skill" not in actions.names(SCENE))

    print("\n== 6b. 裸 SQL 配方：机械包成程序契约后结晶 ==")
    SQL_RECIPE = "SELECT p.kills FROM players p WHERE p.kills > 100"
    act3, key3 = skill_store.add(
        "t3", "sql_recipe", "消费侧答出来的取数配方：按 kills 阈值筛玩家",
        code=SQL_RECIPE,
        source="consume", source_scene=SCENE)
    # add 可能因去重返回 ("skipped", "")，key 为空 → 不信返回值，按 code 回查真实那条
    skills3 = {s["key"]: s for s in skill_store.all_skills()}
    recipe = next((s for s in skills3.values() if s.get("code") == SQL_RECIPE), None)
    check("6b 前置：配方已在技能库里（add=%s）" % act3, recipe is not None)
    cry3 = plugin_forge.crystallize_skill(recipe, into_scene=SCENE)
    check("SQL 配方结晶成功", cry3["ok"], cry3["why"])
    if cry3["ok"]:
        mcp5 = FakeMCP()
        asyncio.run(actions.invoke(SCENE, cry3["entry"], mcp=mcp5))
        check("invoke 时 SQL 原文重放（一字不改）",
              mcp5.calls == ["SELECT p.kills FROM players p WHERE p.kills > 100"],
              str(mcp5.calls))

    print("\n== 6c. 泛化模板 + rollout_sql：结晶重放判对后固化的原文 ==")
    # 2026-10-26：技能库里 code 存的是 :slot 模板（检索+回填通道用），
    # blueprint.rollout_sql 存泛化前判对后固化的原文 —— 结晶 embed 后者，
    # 否则带槽位的模板原样重放必错。
    RAW = "SELECT p.kills FROM players p WHERE p.kills > 100"
    TPL = "SELECT p.kills FROM players p WHERE p.kills > :min_kills"
    act4, key4 = skill_store.add(
        "t4", "tpl_recipe", "泛化后的槽位模板（原文在 blueprint.rollout_sql）",
        code=TPL, source="consume", source_scene=SCENE,
        blueprint={"source": "consume", "question": "kills 超过 100 的玩家",
                   "rollout_sql": RAW})
    skills4 = {s["key"]: s for s in skill_store.all_skills()}
    tpl_sk = next((s for s in skills4.values()
                   if s.get("code") == TPL), None)
    check("6c 前置：模板已在技能库里", tpl_sk is not None)
    cry4 = plugin_forge.crystallize_skill(tpl_sk, into_scene=SCENE)
    check("模板技能结晶成功（用 rollout_sql 不用 code）", cry4["ok"], cry4["why"])
    if cry4["ok"]:
        mcp6 = FakeMCP()
        asyncio.run(actions.invoke(SCENE, cry4["entry"], mcp=mcp6))
        check("invoke 重放的是判对后固化的原文（不是带槽位的模板）",
              mcp6.calls == [RAW], str(mcp6.calls))

    print("\n== 6d. 泛化模板无 rollout_sql（老技能）：如实拒绝，不注册坏条目 ==")
    act5, key5 = skill_store.add(
        "t5", "tpl_old", "泛化上线前入库的模板技能（没有 rollout_sql）",
        code=TPL, source="consume", source_scene=SCENE)
    skills5 = {s["key"]: s for s in skill_store.all_skills()}
    old_tpl = next((s for s in skills5.values()
                    if s.get("name") == "tpl_old"), None)
    check("6d 前置：老模板已在技能库里", old_tpl is not None)
    cry5 = plugin_forge.crystallize_skill(old_tpl, into_scene=SCENE)
    check("老模板 ok=False 且说清原因（槽位不能原样重放）",
          not cry5["ok"] and "rollout_sql" in cry5["why"], cry5["why"][:60])
    check("没有偷偷注册 learned_tpl_old",
          "learned_tpl_old" not in actions.names(SCENE))

    print("\n== 7. 跨场景注册：scene_id 不互相污染 ==")
    cry3 = plugin_forge.crystallize_skill(skills[key], into_scene="U")
    check("同一技能注册进 T 与 U",
          "learned_demo_skill" in actions.names("T")
          and "learned_demo_skill" in actions.names("U"))
    ue = {e["name"]: e for e in actions.entries("U")}
    te = {e["name"]: e for e in actions.entries(SCENE)}
    check("U 的条目 scene_id=U、T 的仍=T（spec 别名已修）",
          ue["learned_demo_skill"]["scene_id"] == "U"
          and te["learned_demo_skill"]["scene_id"] == "T")

    print("\n== 8. actions.restore() 重启恢复 ==")
    # 期望集**派生**自前面各节真实结晶/forge 出的条目（6b/6c 又结晶了
    # learned_sql_recipe / learned_tpl_recipe 两条）—— 写死条数的话，
    # 每加一个契约测试这里就要挂一次（2026-10-26 实测踩到）。
    expect_T = {"query_players", "query_maps",
                "learned_demo_skill", "learned_sql_recipe",
                "learned_tpl_recipe"}
    actions._REGISTRY.clear()
    restored = actions.restore()
    check("T 恢复全部条目（forged×2 + learned×3 都走 impl 引用）",
          set(actions.names(SCENE)) == expect_T, str(sorted(actions.names(SCENE))))
    check("U 恢复 1 条", actions.names("U") == ["learned_demo_skill"])
    re_entries = {e["name"]: e for e in actions.entries(SCENE)}
    check("恢复后 origin 不丢",
          re_entries["query_players"]["origin"] == "forged"
          and re_entries["learned_demo_skill"]["origin"] == "learned")

    print("\n== 9. 幂等：再 forge 一次不重复 ==")
    forged2 = plugin_forge.forge_gaps(plan, SCENE)
    check("再 forge 仍成功（覆盖同文件）", forged2["counts"]["forged"] == 2)
    check("条目数不变（无重复注册）",
          set(actions.names(SCENE)) == expect_T)

    print(f"\n===== 结果：{len(PASS)} 过 / {len(FAIL)} 挂 =====")
    if FAIL:
        for f in FAIL:
            print(f"  ❌ {f}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
