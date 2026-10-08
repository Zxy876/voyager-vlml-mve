#!/usr/bin/env python3
"""同构生成测试题工具（照会议纪要：测试集从已跑通项目同构生成，仅替换赛事编号、成员名称）。

原则（纪要原文）：
  "所有测试题目均从已跑通的项目中同构生成，仅替换赛事编号、成员名称等数据，
   题目逻辑严谨，不存在不合理的泛化要求。"

做法（确定性，不经模型）：
  1. 取源题（已跑通的 TASKS 题，或 --topic 指定）—— 逻辑骨架 = rubric 评分点 + answer_spec
  2. 从当前库**实查**目标实体：新 series（回合数最多、且 ≠ 源 series）、
     该 series 内回合数最多的 team、该 series 的 map 列表
  3. 同构替换：subject 实体值、question 文本、answer_spec.sql 引号内实体值、tool_args
     —— 只换"数据"，不换"逻辑"
  4. 跑 VLML0 裁判校验：每个评分点的 dimension 都要在标准答案 facts 里出数
     （min_cover 门槛）才算"测试题成立"；出不了数的直接丢弃 —— 这保证测试集
     没有"换库就查不到数"的假题（rib 库种子题 17 个评分点 0 个跑得出数的教训）

用法：
  python mve/gen_testset.py --topic fb_conversion_analysis          # 生成一道
  python mve/gen_testset.py --all                                    # 为全部题生成
  python mve/gen_testset.py --topic fb_conversion_analysis --emit-code   # 打印可入闱代码
  python mve/gen_testset.py --topic fb_conversion_analysis --json testsets/x.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from loop_core import AnswerSpec, RubricPoint, Task  # noqa: E402

HERE = Path(__file__).resolve().parent

# SQL 关键字集合 —— 裸词替换时的护栏（引号内替换优先，裸词只用于非关键字）
_SQL_KEYWORDS = {
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "ORDER", "LIMIT", "AND", "OR",
    "NOT", "IN", "AS", "ON", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER",
    "CASE", "WHEN", "THEN", "ELSE", "END", "COUNT", "SUM", "AVG", "MIN", "MAX",
    "ROUND", "CAST", "VARCHAR", "UNION", "ALL", "DISTINCT", "OVER", "PARTITION",
    "ROW_NUMBER", "IS", "NULL", "ASC", "DESC",
}


def _db_path() -> Path:
    """当前库路径（与 anchor._db_path 同一来源）。"""
    try:
        import anchor as _anchor
        return Path(_anchor._db_path())
    except Exception:
        return HERE.parent / "vlml" / "data" / "vlml_events.duckdb"


def _connect():
    import duckdb
    return duckdb.connect(str(_db_path()), read_only=True)


def _series_candidates(con, exclude: str) -> list[str]:
    """库内全部 series，按回合数降序（数据最全的优先），排除源 series。"""
    try:
        rows = con.execute(
            "SELECT CAST(series_id AS VARCHAR) AS sid, COUNT(*) AS n "
            "FROM rounds GROUP BY 1 ORDER BY n DESC").fetchall()
    except Exception:
        rows = []
    return [str(r[0]) for r in rows if str(r[0]) != exclude]


def _team_of(con, sid: str) -> str | None:
    """该 series 内回合数最多的队（与 anchor.pick 同一口径）。"""
    try:
        r = con.execute(
            "SELECT t FROM (SELECT winning_team_name AS t FROM rounds "
            "WHERE CAST(series_id AS VARCHAR)=? UNION ALL "
            "SELECT losing_team_name FROM rounds "
            "WHERE CAST(series_id AS VARCHAR)=?) "
            "WHERE t IS NOT NULL AND t<>'' GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1",
            [sid, sid]).fetchone()
        return str(r[0]) if r else None
    except Exception:
        return None


def _maps_of(con, sid: str) -> list[str]:
    """该 series 内 map 列表（回合数降序）。"""
    try:
        rows = con.execute(
            "SELECT map_name FROM rounds WHERE CAST(series_id AS VARCHAR)=? "
            "AND map_name IS NOT NULL AND map_name<>'' "
            "GROUP BY 1 ORDER BY COUNT(*) DESC", [sid]).fetchall()
        return [str(r[0]) for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 实体抽取与映射
# ---------------------------------------------------------------------------

def _collect_entities(task: Task) -> dict[str, list[str]]:
    """从 rubric subject 收集实体值：{实体键: [值...]}。"""
    out: dict[str, list[str]] = {}
    for p in task.rubric:
        for k, v in (p.subject or {}).items():
            if v is None:
                continue
            s = str(v)
            if s and s not in out.setdefault(k, []):
                out[k].append(s)
    return out


def _build_map(task: Task, con, new_series: str) -> dict[str, dict[str, str]]:
    """构建 {实体键: {旧值: 新值}}。

    - series：一律换新 series（源 series 已在候选里排除）
    - team：换新 series 内回合数最多的队
    - map：同名图优先（同构数据通常图名一致）；否则按出现顺序配对
      （保持源题"多图对比"的结构 —— 不同旧图 → 不同新图）
    """
    entities = _collect_entities(task)
    m: dict[str, dict[str, str]] = {}

    old_series = entities.get("series") or []
    m["series"] = {s: new_series for s in old_series}

    new_team = _team_of(con, new_series)
    old_teams = entities.get("team") or []
    if new_team:
        m["team"] = {t: new_team for t in old_teams}

    old_maps = entities.get("map") or []
    if old_maps:
        new_maps = _maps_of(con, new_series)
        used: set[str] = set()
        mm: dict[str, str] = {}
        for i, om in enumerate(old_maps):
            if om in new_maps and om not in used:
                mm[om] = om
                used.add(om)
            else:
                cand = [nm for nm in new_maps if nm not in used]
                if cand:
                    mm[om] = cand[0]
                    used.add(cand[0])
                else:                       # 新 series 图太少，保留旧图名
                    mm[om] = om
        m["map"] = mm

    # 其他 subject 键（player 等）：能查到对应实体的就查，查不到保持原值
    for k, vals in entities.items():
        if k in ("series", "team", "map"):
            continue
        for v in vals:
            m.setdefault(k, {})[v] = v
    return m


def _replace_text(text: str, mapping: dict[str, dict[str, str]]) -> str:
    """文本替换：所有实体键的 old→new，先长后短（避免子串误伤）。"""
    pairs: list[tuple[str, str]] = []
    for k, kv in mapping.items():
        for old, new in kv.items():
            if old != new:
                pairs.append((str(old), str(new)))
    pairs.sort(key=lambda p: -len(p[0]))
    out = text
    for old, new in pairs:
        out = out.replace(old, new)
    return out


def _replace_sql(sql: str, mapping: dict[str, dict[str, str]]) -> str:
    """SQL 里的实体替换。

    优先替换引号内的精确值（`'{old}'` / `"{old}"`），再兜底裸词替换
    （仅当值不是 SQL 关键字 —— 校验阶段还会拦截出不了数的假题）。
    """
    pairs: list[tuple[str, str]] = []
    for k, kv in mapping.items():
        for old, new in kv.items():
            if old != new:
                pairs.append((str(old), str(new)))
    pairs.sort(key=lambda p: -len(p[0]))

    out = sql
    # 1) 引号内精确替换
    for old, new in pairs:
        out = out.replace(f"'{old}'", f"'{new}'")
        out = out.replace(f'"{old}"', f'"{new}"')
    # 2) 裸词兜底（护栏：跳过 SQL 关键字；单字符/短词不碰）
    for old, new in pairs:
        if len(old) < 3 or old.upper() in _SQL_KEYWORDS:
            continue
        out = out.replace(old, new)
    return out


def _clone_task(task: Task, new_topic_id: str, mapping: dict[str, dict[str, str]]) -> Task:
    """按实体映射克隆一道同构题：换数据，不换逻辑。"""
    rubric = []
    for p in task.rubric:
        spec = p.answer_spec
        new_spec = None
        if spec is not None:
            new_spec = AnswerSpec(
                sql=_replace_sql(str(spec.sql or ""), mapping) if spec.sql else "",
                value_column=spec.value_column,
                base_column=spec.base_column,
                numeric_tolerance=spec.numeric_tolerance,
                closed_world=spec.closed_world,
                tool=str(spec.tool or ""),
                tool_args={k: _replace_text(str(v), mapping)
                           for k, v in (spec.tool_args or {}).items()},
                value_path=str(spec.value_path or ""),
                base_path=str(spec.base_path or ""),
                percent=spec.percent,
            )
        rubric.append(RubricPoint(
            point=_replace_text(str(p.point), mapping),
            subject={k: _replace_text(str(v), mapping) for k, v in (p.subject or {}).items()},
            dimension=str(p.dimension),
            weight=p.weight,
            min_base=p.min_base,
            expected_value=p.expected_value,
            tolerance=p.tolerance,
            answer_spec=new_spec,
        ))
    return Task(
        topic_id=new_topic_id,
        question=_replace_text(task.question, mapping),
        rubric=rubric,
        difficulty=task.difficulty,
        validated_target=task.validated_target,
        min_base=task.min_base,
        requires_tools=list(task.requires_tools or []),
    )


# ---------------------------------------------------------------------------
# 裁判校验（测试题成立判据）
# ---------------------------------------------------------------------------

async def _verify(task: Task, min_cover: float) -> dict[str, Any]:
    """跑 VLML0 裁判，校验每个评分点都出数。

    覆盖判据：facts 里出现的 dimension 覆盖评分点比例 ≥ min_cover。
    （维度=口径，事实的 subject/dimension 就是"这道题有没有解"的直接证据。）
    """
    import vlml0_referee
    ref = await vlml0_referee.answer(task, force=True)
    facts = ref.facts or []
    dims = {str(f.get("dimension") or "") for f in facts}
    want = [p.dimension for p in task.rubric]
    hit = [d for d in want if d in dims]
    cover = len(hit) / len(want) if want else 0.0
    return {
        "ok": cover >= min_cover and bool(facts),
        "facts": len(facts),
        "trajectory": len(ref.trajectory or []),
        "covered_dims": hit,
        "missing_dims": [d for d in want if d not in dims],
        "cover": round(cover, 3),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _emit_code(task: Task, src: str, series: str, mapping: dict[str, dict[str, str]]) -> str:
    """打印可入闱的 Task 构造代码（粘贴进题库或测试集即可用）。"""
    lines = [
        f"# 同构测试题：源题 {src} → series {series}",
        f"# 实体映射：{json.dumps(mapping, ensure_ascii=False)}",
        f"Task(",
        f"    topic_id={task.topic_id!r},",
        f"    question={task.question!r},",
        f"    difficulty={task.difficulty},",
        f"    validated_target={task.validated_target},",
        f"    min_base={task.min_base},",
        f"    requires_tools={list(task.requires_tools or [])!r},",
        f"    rubric=[",
    ]
    for p in task.rubric:
        spec = p.answer_spec
        spec_lines = ["        AnswerSpec("]
        if spec and spec.sql:
            spec_lines.append(f"            sql={spec.sql!r},")
        if spec:
            spec_lines.append(f"            value_column={spec.value_column},")
            if spec.base_column is not None:
                spec_lines.append(f"            base_column={spec.base_column},")
            if spec.numeric_tolerance:
                spec_lines.append(f"            numeric_tolerance={spec.numeric_tolerance},")
            if spec.tool:
                spec_lines.append(f"            tool={spec.tool!r},")
                spec_lines.append(f"            tool_args={spec.tool_args!r},")
            if spec.value_path:
                spec_lines.append(f"            value_path={spec.value_path!r},")
            if spec.base_path:
                spec_lines.append(f"            base_path={spec.base_path!r},")
            if spec.percent:
                spec_lines.append("            percent=True,")
        spec_lines.append("        ),")
        lines.append(
            f"        RubricPoint(\n"
            f"            point={p.point!r},\n"
            f"            subject={p.subject!r},\n"
            f"            dimension={p.dimension!r},\n"
            f"            weight={p.weight},\n"
            f"            min_base={p.min_base},\n"
            f"            answer_spec=" + "\n".join(spec_lines) + "\n        ),"
        )
    lines.append("    ],\n)")
    return "\n".join(lines)


async def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="同构生成测试题工具")
    ap.add_argument("--topic", default="", help="源题 topic_id（空=用当前 TASKS 全部题）")
    ap.add_argument("--all", action="store_true", help="为全部已注册题生成测试题")
    ap.add_argument("--series", default="", help="指定目标 series（默认自动挑回合数最多的）")
    ap.add_argument("--min-cover", type=float, default=0.8,
                    help="测试题成立门槛：评分点出数比例（默认 0.8）")
    ap.add_argument("--json", default="", help="输出 JSON 路径（默认 testsets/<src>__<series>.json）")
    ap.add_argument("--emit-code", action="store_true", help="打印可入闱 Task 代码")
    ap.add_argument("--keep-failed", action="store_true", help="连出不了数的假题也写进 JSON")
    args = ap.parse_args(argv)

    import tasks as tasks_mod
    import anchor as _anchor

    con = _connect()
    try:
        if args.all:
            sources = [t for t in tasks_mod.TASKS]
        elif args.topic:
            sources = [args.topic]
        else:
            print("需 --topic <topic_id> 或 --all")
            return 2

        out_dir = HERE / "testsets"
        out_dir.mkdir(exist_ok=True)
        results = []

        for src in sources:
            if src not in tasks_mod.TASKS:
                print(f"⚠ 题库里没有 {src}，跳过")
                continue
            task = tasks_mod.TASKS[src]

            # ── 选目标 series：指定优先，否则回合数最多的 ──
            old_series = ""
            for p in task.rubric:
                if (p.subject or {}).get("series"):
                    old_series = str(p.subject["series"])
                    break
            if args.series:
                new_series = args.series
            else:
                cands = _series_candidates(con, exclude=old_series)
                if not cands:
                    print(f"⚠ 库内没有除 {old_series} 外的 series，{src} 无法同构")
                    continue
                new_series = cands[0]

            mapping = _build_map(task, con, new_series)
            new_id = f"{src}__{new_series}"
            new_task = _clone_task(task, new_id, mapping)

            v = await _verify(new_task, args.min_cover)
            rec = {
                "source_topic_id": src,
                "topic_id": new_id,
                "target_series": new_series,
                "entity_map": mapping,
                "question": new_task.question,
                "validated": v,
                "task": new_task.as_dict(),
            }
            results.append(rec)

            if v["ok"]:
                print(f"✅ {src} → {new_id}（series={new_series}）"
                      f" 裁判出数 {v['facts']} 条 · 维度覆盖 {v['cover']:.0%} "
                      f"· 轨迹 {v['trajectory']} 步")
                if args.emit_code:
                    print()
                    print(_emit_code(new_task, src, new_series, mapping))
            else:
                print(f"❌ {src} → {new_id} 出数 {v['facts']} 条，"
                      f"覆盖 {v['cover']:.0%} < {args.min_cover:.0%} —— 丢弃"
                      f"（缺维度：{v['missing_dims']}）")
                if not args.keep_failed:
                    results.pop()

        good = [r for r in results if r["validated"]["ok"]]
        print()
        print(f"测试题成立 {len(good)}/{len(results)}（门槛 min_cover={args.min_cover}）")
        if results:
            # 相对路径一律解析到 out_dir 下（避免后台任务 cwd 漂移导致写失败）
            if args.json:
                p = Path(args.json)
                out_path = p if p.is_absolute() else out_dir / p.name
            else:
                out_path = out_dir / "testset.json"
            out_path.write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"已写入 {out_path}")
        return 0
    finally:
        try:
            con.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main(sys.argv[1:])))
    except KeyboardInterrupt:
        sys.exit(130)
