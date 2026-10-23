#!/usr/bin/env python3
"""「技能存算法不存数值」单测（2026-10-23 用户定）。

覆盖三件事：
  1. generalize_sql_recipe：SQL 配方入库前泛化成 :slot 模板 ——
     LLM 挂了/产出不像 SQL/非 SQL 配方 → 原样返回（泛化是增强不是闸门）；
  2. learn_milestone_skills：维度闭合写技能前，SQL 先过泛化（技能库里
     落的是模板，不是带旧题数值的查询）；
  3. _REFILL_SYSTEM：消费侧回填 prompt 含槽位模板规则（模型知道怎么填槽）。

LLM 用桩替换（chat_json / 消费侧同款），不联网。运行：python mve/test_generalize_recipe.py
"""

from __future__ import annotations

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

import os
os.environ.setdefault("MVE_DB", ":memory:")     # 隔离，别碰真库

_FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f" —— {detail}" if detail and not ok else ""))
    if not ok:
        _FAILS.append(name)


def _stub_chat_json(reply: dict, calls: list):
    """给 consume 打桩：chat_json 返回固定值并记录调用。"""

    def _fake(messages, **kwargs):
        calls.append(messages)
        return json.loads(json.dumps(reply))

    return _fake


def main() -> int:
    print("== ① generalize_sql_recipe：泛化与降级 ==")
    import voyager_vlml.consume as consume

    sql = ("SELECT SUM(days) AS total FROM leave_records "
           "WHERE dept = '技术' AND month = '2026-09'")
    tpl = ("-- slots: dept=部门名; month=年月\n"
           "SELECT SUM(days) AS total FROM leave_records "
           "WHERE dept = :dept AND month = :month")

    calls: list = []
    consume.chat_json = _stub_chat_json(
        {"sql": tpl, "slots": ["dept", "month"], "note": "换了 2 个槽位"}, calls)
    got = consume.generalize_sql_recipe(sql, "2026年9月技术部请假多少天")
    check("SQL 配方被泛化成槽位模板", ":dept" in got and ":month" in got, got)
    check("泛化调用带上了旧题与原 SQL",
          len(calls) == 1 and "2026年9月" in calls[0][1]["content"]
          and "2026-09" in calls[0][1]["content"])

    # 非 SQL（本机 Python 配方）不调 LLM、原样返回
    calls.clear()
    py_code = "x = web_search('许嵩')\nRESULT = {'answer': x}"
    got = consume.generalize_sql_recipe(py_code, "许嵩")
    check("非 SQL 配方原样返回且不调 LLM", got == py_code and not calls)

    # LLM 挂 → 原样返回
    def _boom(messages, **kwargs):
        raise RuntimeError("llm down")
    consume.chat_json = _boom
    got = consume.generalize_sql_recipe(sql, "q")
    check("LLM 挂掉时原样返回（不阻塞入库）", got == sql)

    # LLM 产出不像 SQL → 原样返回
    calls.clear()
    consume.chat_json = _stub_chat_json({"sql": "DROP TABLE x", "slots": []}, calls)
    got = consume.generalize_sql_recipe(sql, "q")
    check("产出不是 SELECT/WITH 时原样返回", got == sql)

    # 纯结构查询：LLM 说没槽位 → 原样返回
    struct = "SELECT COUNT(*) FROM games"
    consume.chat_json = _stub_chat_json({"sql": struct, "slots": [], "note": "无槽位"}, [])
    got = consume.generalize_sql_recipe(struct, "q")
    check("纯结构查询原样返回", got == struct)

    # 槽位塞进列别名（glm-4-flash 实测踩过 `AS total_:kind`）→ 原样返回
    bad_alias = ("SELECT SUM(CASE WHEN a.kind = :subsidy_type THEN a.amount "
                 "ELSE 0 END) AS total_:subsidy_type FROM allowances a")
    calls.clear()
    consume.chat_json = _stub_chat_json({"sql": bad_alias, "slots": ["subsidy_type"]}, calls)
    got = consume.generalize_sql_recipe(sql, "q")
    check("槽位出现在标识符旁（列别名）→ 原样返回", got == sql)

    print("== ② learn_milestone_skills：入库前泛化 ==")
    import skill_store
    import learn_orchestrate as lo

    with tempfile.TemporaryDirectory() as td:
        skill_store.STORE = Path(td) / "skill_store.json"
        skill_store._RAW_CACHE.update({"sig": None, "data": None})
        # 恢复 consume 的桩为"泛化成功"形态
        consume.chat_json = _stub_chat_json(
            {"sql": tpl, "slots": ["dept", "month"], "note": "ok"}, [])
        out = lo.learn_milestone_skills(
            "P", "t_leave", "2026年9月技术部请假多少天",
            {"name": "请假", "achieved": True},
            [{"id": "total", "question": "技术部 2026-09 请假合计",
              "code": sql, "tools": ["query_sql"]}],
            import_id="imp_test")
        keys = [a["key"] for a in out.get("added", [])]
        check("维度闭合 → 技能入库", bool(keys), json.dumps(out, ensure_ascii=False)[:200])
        if keys:
            stored = skill_store._load_raw()["skills"][keys[0]]
            check("技能库里落的是槽位模板（不是带旧题数值的 SQL）",
                  ":dept" in str(stored.get("code"))
                  and "2026-09" not in str(stored.get("code")),
                  str(stored.get("code"))[:160])

    print("== ③ 消费侧回填 prompt 认识槽位模板 ==")
    check("_REFILL_SYSTEM 说明槽位模板怎么用",
          ":dept" in consume._REFILL_SYSTEM and "槽位" in consume._REFILL_SYSTEM)
    check("_GENERALIZE_SYSTEM 禁动方法本体",
          "方法本身" in consume._GENERALIZE_SYSTEM
          and "GROUP BY" in consume._GENERALIZE_SYSTEM)

    print()
    if _FAILS:
        print(f"失败 {len(_FAILS)} 项：{_FAILS}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
