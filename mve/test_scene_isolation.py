#!/usr/bin/env python3
"""验证「切场景后，各页签内容都属于那个场景」（2026-10-18 用户指令的可复现剧本）。

不依赖 duckdb / 大模型，纯测面板数据层的场景隔离：

  1. 建 Scene A（默认）+ Scene B，给两边各写合成数据（coach_log / run_log /
     causal_timeline / skill_store），旧记录不带 scene 键
  2. 当前场景 = A → build_state 断言：导入历史/学习曲线/因果时间线/轨迹/
     练习计数只含 A 的数据；技能卡含 A + default（跨场景复用资产）
  3. 切到 B → 同样的断言，只含 B 的数据，A 的一条不混进来
  4. 旧记录（无 scene/root_scene 键）归 A，不丢历史

运行：python mve/test_scene_isolation.py

⚠️ 全程用临时文件隔离，不污染真实的 coach_log/run_log/causal_timeline/skill_store/scenes。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import causal_timeline
import panel_data
import practice_scope
import run_log
import scenes
import skill_store


def _patch_all(tmp: Path) -> None:
    """把四个日志/注册表的模块级路径指到临时目录。"""
    panel_data.ROOT = tmp                       # coach_log.jsonl / exam_log.jsonl
    run_log.LOG = tmp / "run_log.jsonl"
    causal_timeline.LOG = tmp / "causal_timeline.jsonl"
    skill_store.STORE = tmp / "skill_store.json"
    scenes.REGISTRY = tmp / "scenes.json"
    scenes.HERE = tmp


def _stub_heavy_deps() -> None:
    """build_state 会拉起 VLML 环境 / planner / pilot 等重依赖 —— 测试里全部
    打桩，只留被测的数据通路（imports / consume_curve / causal / series /
    attempted / failed / skill_detail）。"""
    panel_data.probe_env = lambda: {"ok": True}
    panel_data._task_library = lambda: []
    panel_data._next_recommend = lambda: {"topic_id": "", "reason": "stub"}
    panel_data._pilot_view = lambda: {"running": False}
    panel_data._voyager_view = lambda: {"running": False}
    panel_data._db_view = lambda: {"path": "", "is_default": True}
    panel_data._exam_view = lambda: {"has_data": False}
    panel_data._teaching_payload = lambda: {"nodes": [], "edges": []}
    panel_data._mc_thinking_view = lambda: {}
    panel_data._referee_view = lambda _tid: {}
    practice_scope.get_scope = lambda: {}


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                            for r in rows), encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mve_scene_iso_"))
    _patch_all(tmp)
    _stub_heavy_deps()

    # ---- 0. 场景：A（默认）+ B ----
    scenes.REGISTRY.write_text("{}", encoding="utf-8")
    scenes.create_scene("特斯拉盘面", description="B：股价与新闻情绪")

    # ---- 1. 合成数据：A 两条 + B 两条导入；旧一条无 root_scene ----
    _write_jsonl(tmp / "coach_log.jsonl", [
        {"ts": "2026-10-18T20:00:00", "question": "A 的第 1 问", "topic_id": "t1",
         "mode": "voyager_consume", "root_scene": "A", "mastered": True,
         "route_decision": "HIGH_CONFIDENCE", "judgment_result": "written"},
        {"ts": "2026-10-18T20:10:00", "question": "A 的第 2 问", "topic_id": "t1b",
         "mode": "voyager_consume", "root_scene": "A", "mastered": False},
        {"ts": "2026-10-18T21:00:00", "question": "B 的第 1 问", "topic_id": "t2",
         "mode": "voyager_consume", "root_scene": "B", "mastered": True,
         "route_decision": "HIGH_CONFIDENCE", "judgment_result": "written"},
        {"ts": "2026-10-18T21:10:00", "question": "B 的第 2 问", "topic_id": "t2b",
         "mode": "voyager_consume", "root_scene": "B", "mastered": False},
        # 旧记录（1→n 改造前）：无 root_scene → 归 A，不丢历史
        {"ts": "2026-10-17T10:00:00", "question": "改造前的历史导入",
         "topic_id": "t0", "mode": "vlml"},
    ])

    # ---- 2. 合成练习轮：A 两条（含一条旧的无 scene）+ B 一条 ----
    _write_jsonl(tmp / "run_log.jsonl", [
        {"ts": "2026-10-18T20:01:00", "topic_id": "t1", "scene": "A",
         "verdict": "ok", "coverage": 0.5, "mode": "practice"},
        {"ts": "2026-10-17T09:00:00", "topic_id": "t0",
         "verdict": "ok", "coverage": 0.3, "mode": "practice"},   # 无 scene → A
        {"ts": "2026-10-18T21:01:00", "topic_id": "t2", "scene": "B",
         "verdict": "ok", "coverage": 1.0, "mode": "practice"},
    ])

    # ---- 3. 合成因果时间线：A 两条（含一条旧的）+ B 一条 ----
    _write_jsonl(tmp / "causal_timeline.jsonl", [
        {"seq": 1, "ts": "2026-10-18T20:00:30", "kind": "control",
         "topic_id": "t1", "summary": "A 的导入", "detail": {}, "scene": "A"},
        {"seq": 2, "ts": "2026-10-17T09:00:30", "kind": "control",
         "topic_id": "t0", "summary": "改造前的导入", "detail": {}},  # 无 scene → A
        {"seq": 3, "ts": "2026-10-18T21:00:30", "kind": "control",
         "topic_id": "t2", "summary": "B 的导入", "detail": {}, "scene": "B"},
    ])

    # ---- 4. 合成技能：A 一条 + B 一条 + 无归属（default）一条 ----
    (tmp / "skill_store.json").write_text(json.dumps({"skills": {
        "k_a": {"topic": "t1", "name": "A 的技能", "text": "算 A 的指标",
                "hits": 3, "ok": 2, "source_scene": "A"},
        "k_b": {"topic": "t2", "name": "B 的技能", "text": "算 B 的指标",
                "hits": 1, "ok": 0, "source_scene": "B"},
        "k_old": {"topic": "t0", "name": "改造前的技能", "text": "老技能",
                  "hits": 5, "ok": 1},
    }}, ensure_ascii=False), encoding="utf-8")

    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"  {'✅' if cond else '❌'} {name}" + (f" —— {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    # ---- 5. 当前场景 = A ----
    print("[当前场景 A]")
    st = panel_data.build_state()
    a_imports = [i["question"] for i in st["imports"]]
    a_curve_scenes = {c["root_scene"] for c in st["consume_curve"]}
    a_causal = [r["summary"] for r in st["causal"]["rows"]]
    a_topics = {s["topic_id"] for s in st["series"]}
    a_skills = {s["name"] for s in st["skill_detail"]}

    check("scene 块 = A 默认场景", st["scene"]["id"] == "A" and st["scene"]["is_default"] is True)
    check("导入历史只含 A（3 条，含 1 条旧记录）", len(a_imports) == 3
          and all("B 的" not in q for q in a_imports), str(a_imports))
    check("学习曲线只画 A 的点（2 条消费侧）", a_curve_scenes == {"A"}, str(a_curve_scenes))
    check("曲线首末读数：A 掌握 1（t1 答出、t1b 未答出）",
          st["consume_curve"][-1]["mastered_total"] == 1)
    check("因果时间线只含 A（2 条，含 1 条旧事实）", a_causal == ["A 的导入", "改造前的导入"],
          str(a_causal))
    check("轨迹/运行日志只含 A（t1 + 旧 t0）", a_topics == {"t1", "t0"}, str(a_topics))
    check("total_rounds = 2（按场景）", st["total_rounds"] == 2)
    check("练习计数只数 A 的题", set(st["attempted"]) == {"t1", "t0"}, str(st["attempted"]))
    check("技能卡 = A + default（跨场景复用资产，B 专属不混入）",
          a_skills == {"A 的技能", "改造前的技能"}, str(a_skills))
    check("has_data = True", st["has_data"] is True)

    # ---- 6. 切到 B ----
    print("[切到场景 B]")
    scenes.switch_scene("B")
    st2 = panel_data.build_state()
    b_imports = [i["question"] for i in st2["imports"]]
    b_curve_scenes = {c["root_scene"] for c in st2["consume_curve"]}
    b_causal = [r["summary"] for r in st2["causal"]["rows"]]
    b_topics = {s["topic_id"] for s in st2["series"]}
    b_skills = {s["name"] for s in st2["skill_detail"]}

    check("scene 块 = B 非默认", st2["scene"]["id"] == "B" and st2["scene"]["is_default"] is False)
    check("导入历史只含 B（2 条）", b_imports == ["B 的第 1 问", "B 的第 2 问"], str(b_imports))
    check("学习曲线只画 B 的点（2 条）", b_curve_scenes == {"B"}, str(b_curve_scenes))
    check("曲线掌握数从 B 自己的历史起算（首点=1 是 B1 自己答出的；若混入 A 会是 2）",
          st2["consume_curve"][0]["mastered_total"] == 1
          and st2["consume_curve"][-1]["mastered_total"] == 1)
    check("曲线不给全局分母（ability_total=None，免误读）",
          all(c["ability_total"] is None for c in st2["consume_curve"]))
    check("因果时间线只含 B（1 条）", b_causal == ["B 的导入"], str(b_causal))
    check("轨迹/运行日志只含 B（t2）", b_topics == {"t2"}, str(b_topics))
    check("total_rounds = 1（按场景）", st2["total_rounds"] == 1)
    check("练习计数只数 B 的题", set(st2["attempted"]) == {"t2"}, str(st2["attempted"]))
    check("技能卡 = B + default（A 专属不混入）",
          b_skills == {"B 的技能", "改造前的技能"}, str(b_skills))
    check("A 的数据一条都没混进 B", not any("A 的" in q for q in b_imports)
          and "A 的导入" not in b_causal)

    print("\n=== 结论 ===")
    if failures:
        print(f"❌ {len(failures)} 项失败：{failures}")
        return 1
    print("✅ 切场景后各页签内容都属于该场景：导入历史/学习曲线/因果时间线/轨迹/"
          "练习计数全部隔离，旧记录归 A 不丢历史，技能按「本场景+default」口径")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
