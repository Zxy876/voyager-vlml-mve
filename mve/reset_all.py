#!/usr/bin/env python3
"""一次性清空 MVE 的全部学习状态。

为什么单开一个文件：`run_mve.py --reset` 只清技能库 + 运行日志 + 裁判缓存，
但「学习状态」还散落在十几处。只清一部分会留下**对不上的曲线**：
比如 run_log 空了但因果时间线还在，出题器就会照着旧的时间线选题，
而掌握度视图里什么都看不到。

⚠️ 2026-10-14 补齐（用户指出「清库没有改对应的 voyager 学习记录」）：
  原来只清 10 项，而服务器上真实存在的状态文件有 20 个。漏掉的那批里
  `topic_mastery.jsonl` 最要命 —— 它记着「哪些节点已经规划过了」，
  `select_next` 据此排除，不清就等于**课程永远跑不完**：

    实测（服务器，清库后模拟）：可选题从 61 道掉到 **6 道**，
    `select_next()` 直接返回 `reason=exhausted`
    —— "范围内无「可拿下」任务（完成 55 / 卡死 8）"。
    也就是清库之后 Voyager 不是"从零开始学"，而是判定"课程已经跑完了"。

清的三组：

  【学习成果】
  skill_store.json       技能库（Voyager 学的取数配方 / 教训）
  topic_mastery.jsonl    掌握库（每个节点"规划过了没有 / 做得多好"）★本轮补
  runtime_topics.json    运行期生成的图谱节点（练习线 + 消费线共用）★本轮补
  generated_tasks.json   出题器生成的题 ★本轮补
  pending_dimensions.json 待审维度候选 ★本轮补
  imported_links.json    人导入建的边（图谱缓存的输入）★本轮补
  knowledge_graph.json   图谱缓存（可由上面两项重建）★本轮补

  【曲线与记录】
  run_log.jsonl          每轮作答（判据层曲线数据源）
  coach_log.jsonl        人导入历史 + 消费记录（学习曲线数据源）
  exam_log.jsonl         裸考记录（撤支架考核）
  causal_timeline.jsonl  行动因果时间线
  mastery_retention.jsonl 掌握度保持度（baseline / 半衰期）
  referee_store.json     裁判标准答案缓存
  referee_judge_store.json 裁判比对结论缓存 ★本轮补
  learn.log              学习单元日志

  【进程状态】
  pilot.log / pilot_state.json         MVE 驾驶舱日志与状态
  loop_state.json                      练习循环心跳 ★本轮补
  voyager_pilot_state.json             Voyager 驾驶舱状态 ★本轮补

**有意不清**（不是学习成果，清了反而要人重配）：
  db_config.json      当前指向哪个库
  entity_catalog.json 从库实查的实体可填值（跟着库走，不是学出来的）
  tool_schemas.json   工具 schema 缓存（同上）
  subjects.json       人填的学科登记册（人的标注）
  practice_scope.json 人设的练习范围
  ui_events.jsonl     人的操作日志

**数据源（.duckdb）完全不在清库范围**：本脚本 `ROOT = <repo>/mve`，
而三个库在 `<repo>/vlml/data/`（`vlml_events` / `vlml_rib` / `vlml_vlr`），
`raw_events` 软链也在那儿。清库删不到数据源 —— 但**"文件还在" ≠ "访问得到"**：
能不能访问只取决于 `db_config.json` 的 `db_path`；为空就静默退回默认库
（2026-10-13 那天默认库还是 GRID 切片，于是"悄悄换库"；**默认库已于
2026-10-13 改成 rib.gg**，见 `db_switch.DEFAULT_DB`）。所以清完脚本会
**打印当前连的是哪个库 + 库里的地图/队伍样例 + 锚点是否过期**，不让人猜。
详见 `_report_db()`。

⚠️ 与 Voyager（MC 真实学习）那侧**保持独立**：/root/voyager/ckpt 的
  skill / curriculum / events 由 `mve/voyager_learn_reset.py` 管，
  面板上「MC 真实学习」页有自己的「清库 / 清记忆」两个按钮。
  用户 2026-10-14 定：两侧各管各的。
  想一次归零就两个都点，或命令行各跑一次。

用法：
    python mve/reset_all.py            # 先备份再清
    python mve/reset_all.py --dry-run  # 只看会清什么
    python mve/reset_all.py --no-backup
    python mve/reset_all.py --force    # 循环还在跑也硬清（不推荐）
    python mve/reset_all.py --db-switch  # 换库清档：额外清旧库派生缓存
                                         # （entity_catalog / tool_schemas ——
                                         #   旧库的队名/图名在新库查不到）
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

TARGETS = [
    # ---- 学习成果 ----
    ("skill_store.json", "技能库 / 记忆"),
    # ⚠️ 本轮补：这是「清库没清干净」里最要命的一个。
    # `topic_mastery.jsonl` 上的 completed 是**去重位**（「规划过了，别再规划」），
    # `select_next` 据此排除节点。不清它 → 清库后 55 道题被判"已经练过"，
    # 只剩 6 道可选，跑两轮就 exhausted。实测见文件头。
    ("topic_mastery.jsonl", "掌握库（每个节点的进度 / 完成情况）"),
    # 练习线（loop.py:_save_runtime_topic）与消费线（consume.py:_runtime_topic_id）
    # **共用**的同一份文件。不清 → 重开后又被注入回内存图谱，
    # 61 个节点里 32 个是上一轮练出来的。
    ("runtime_topics.json", "运行期生成的图谱节点（练习 + 消费共用）"),
    ("generated_tasks.json", "出题器生成的题"),
    ("pending_dimensions.json", "待审维度候选"),
    ("imported_links.json", "人导入建的边（图谱缓存的输入）"),
    ("knowledge_graph.json", "图谱缓存（可由种子 + 上面两项重建）"),
    # ---- 曲线与记录 ----
    ("run_log.jsonl", "运行日志（判据层曲线数据源）"),
    ("coach_log.jsonl", "人导入历史 + 消费记录（学习曲线数据源）"),
    # 裸考记录：**考核线的真正数据源**。不清它，清档后"真实水平"还留着
    # 上一轮的成绩 —— 摸底就白摸了，出题器会照着旧画像选题，而技能库是空的。
    ("exam_log.jsonl", "裸考记录（真实水平 · 考核线数据源）"),
    ("causal_timeline.jsonl", "行动因果时间线"),
    # 保持度（baseline + 半衰期）。不清它，清档后"学会了多少、能撑多久"
    # 还留着上一轮的参数 —— 又是一条对不上的曲线。
    ("mastery_retention.jsonl", "掌握度保持度（baseline / 半衰期）"),
    ("referee_store.json", "裁判标准答案缓存"),
    # 本轮补：以前只清了 referee_store.json，这个 judge 缓存漏了 ——
    # 里面存的是「两边做法比对」的旧结论，留着就是给新的一轮发旧分。
    ("referee_judge_store.json", "裁判比对结论缓存"),
    ("learn.log", "学习单元日志"),
    # ---- 进程状态 ----
    ("pilot.log", "驾驶舱日志"),
    ("pilot_state.json", "驾驶舱状态"),
    # 本轮补：循环心跳由 loop.py 每轮写，面板「循环心跳条」读它。
    # 不清 → 清档后面板还挂着上一轮的「第 26 轮 · 进度 55」。
    ("loop_state.json", "练习循环心跳"),
    ("voyager_pilot_state.json", "Voyager 驾驶舱状态"),
]

# 有意保留：不是学习成果，清了要人重配（打印出来让人看得见"哪些没清"）
KEEP = [
    ("db_config.json", "当前指向哪个库"),
    ("entity_catalog.json", "从库实查的实体可填值（跟着库走）"),
    ("tool_schemas.json", "工具 schema 缓存（跟着库走）"),
    ("subjects.json", "人填的学科登记册"),
    ("practice_scope.json", "人设的练习范围"),
    ("ui_events.jsonl", "人的操作日志"),
]

# ⚠️ 换库清档（--db-switch）要**额外**清的：这三份是从库里**派生**的缓存，
#    库换了它们就是毒药 —— 旧库的队名 / 图名 / series_id 会让出题器选出
#    在新库里查不到的题（实测踩过：rib 库的 team 是 LOUD / NRG，GRID 库
#    是 Cloud9，两边根本对不上）。普通清库（库没换）它们留着没问题，
#    所以不在主 TARGETS 里，只在换库清档时进 TARGETS。
#    entity_catalog.json 清掉之后由 /api/db/switch 顺手重跑
#    `entity_catalog.py` 按新库重查 —— 清而不补的话出题器连可填值都没了。
DB_DERIVED = [
    ("entity_catalog.json", "实体可填值清单（旧库实查的，换库后旧值全错）"),
    ("tool_schemas.json", "工具 schema 缓存（新库的表名/列名可能变）"),
]

# 清档前必须确认没人还在写这些文件 —— 循环继续跑的话，
# 清完它下一轮立刻把 mastery / runtime_topics / run_log 全写回去，清了等于没清。
_RUNNING_FILES = [
    ("loop_state.json", "练习循环"),
    ("pilot_state.json", "MVE 驾驶舱"),
    ("voyager_pilot_state.json", "Voyager 驾驶舱"),
]


def _pid_alive(pid: object) -> bool:
    """这个 pid 还活着吗（信号 0 = 只探活，不发信号）。

    判据同构 `voyager_pilot.read()`（`voyager_pilot.py:139`）——
    面板那边就是这么把"僵尸 running"识别掉的，两处行为必须一致，
    否则会出现"面板说空闲、清档却说在跑"这种对不上的情况。
    """
    if not isinstance(pid, int) or pid <= 0:
        return True          # 没有 pid 可核 → 保守：当作在跑
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, PermissionError, OSError):
        return False


def _report_db() -> None:
    """清完（或 dry-run 时）报一句：**现在连的是哪个数据源**。

    ⚠️ 为什么要有这一段（用户 2026-10-14 问：「重启清库之后，voyager 和 VLML
      还可以访问到 vlr 和 rib 的数据源吗」）：

      1. **数据源文件不在清库范围里**。本脚本 `ROOT = /root/mve/mve`，
         而三个库在 `/root/mve/vlml/data/`（`vlml_events` / `vlml_rib` /
         `vlml_vlr`），`raw_events` 软链也在那儿。实测 dry-run 清单里
         一个 duckdb 都没有 —— 清库删不到数据源，这一步是安全的。

      2. **但"文件还在" ≠ "访问得到"**。能不能访问只取决于
         `db_config.json` 的 `db_path`：
           - 写了绝对路径 → `vlml_env` 在 import 时给 `EventDatabase` 打补丁，
             重启后照新文件重读，访问得到（实测：指 rib → LOUD 4 行 / Split 3 行；
             指 vlr → Split 2 行，均随新进程生效）。
           - **为空 → 落到默认库** `db_switch.DEFAULT_DB`。2026-10-13 那天
             默认库还是 GRID 切片 `vlml_events.duckdb`（只有 Cloud9 / NRG、
             Haven / Corrode / Lotus，没有 LOUD / Split），表现为"数据源
             悄悄换了"；**默认库当天已改成 rib.gg**，所以现在为空 = 落 rib，
             是预期行为 —— 但"落默认库"仍是兜底而非显式选择，照样要打出来。

      3. 这个"悄悄换"已经发生过（服务器 2026-10-13 实测）：
         db_config 在 06:56 被切回默认库，但面板进程 10:42 才重启 ——
         07:01~07:05 那几条消费记录读的仍是**重启前内存里的 rib 连接**
         （`mada`/`Haven` 返回 0 行），而同一条 SQL 在 GRID 库里有 15 行。
         所以清完必须把"我现在到底在哪个库上"打出来，不能让人猜。
    """
    print("\n  " + "-" * 64)
    print("  数据源（不在清库范围，原样保留）")
    print("  " + "-" * 64)
    try:
        import db_switch
    except Exception as e:
        print(f"  （读不到 db_switch，跳过：{type(e).__name__}: {e}）")
        return

    raw = ""
    try:
        raw = str((json.loads((ROOT / "db_config.json").read_text(
            encoding="utf-8")) or {}).get("db_path") or "")
    except Exception:
        pass
    path = db_switch.current_path()
    is_def = db_switch.is_default()
    print(f"  db_config.db_path : {raw!r}" + ("（空 → 用默认库兜底）" if not raw else ""))
    print(f"  实际连接的库      : {path}")
    # 标签跟着默认库走，别写死"GRID 单场切片" —— 默认库 2026-10-13 已改成 rib.gg。
    print(f"  是默认库吗        : {'是（' + Path(db_switch.DEFAULT_DB).stem + '）' if is_def else '否'}")

    if not raw:
        print("  ⚠️ 当前 db_path 为空 → 用的是**默认库**兜底"
              f"（{Path(db_switch.DEFAULT_DB).stem}），不是你显式选的库。")
        print("     要显式指定：面板「数据源与数据库」里选库，"
              "或 `python mve/db_switch.py set <绝对路径>`。")

    # 库里到底有什么 —— 报实体样例，人一眼能认出是哪个源
    try:
        import duckdb
        con = duckdb.connect(path, read_only=True)
    except Exception as e:
        print(f"  ⚠️ 连不上当前库：{type(e).__name__}: {e}")
        return
    try:
        tabs = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main'").fetchall()]
        print(f"  表               : {len(tabs)} 张")
        for tbl, col, tag in (("games", "map_name", "地图"),
                              ("agg_team_series_stats", "team_name", "队伍")):
            if tbl not in tabs:
                continue
            try:
                # ⚠️ 必须 ORDER BY：没有排序键时 DuckDB 对并列/超出 LIMIT 的行
                #    不保证顺序，同一个库两次 dry-run 会报出不同的 top-6
                #    （2026-10-13 实测：清库前后两次报告的队伍列表不一样，
                #    看着像数据变了，其实是 12 支队里任意 6 支的显示顺序）。
                #    和 anchor.pick() 的并列摇摆同一品类：不可复现的展示
                #    会让人照着错的线索排查。
                vals = [str(r[0]) for r in con.execute(
                    f"SELECT DISTINCT {col} FROM {tbl} "
                    f"WHERE {col} IS NOT NULL ORDER BY {col} LIMIT 6").fetchall()]
                print(f"  {tag:<16}: {', '.join(vals)}")
            except Exception:
                continue
    finally:
        con.close()

    # 锚点和库对不对得上（anchor 是独立字段，切库时不会跟着动）
    try:
        st = db_switch.anchor_status()
    except Exception:
        st = {}
    if st.get("stale"):
        print(f"  ⚠️ 锚点过期       : {st.get('hint')}")
    elif st.get("hint"):
        print(f"  · 锚点           : {st.get('hint')}")


def _running() -> list[str]:
    """还在跑的东西（读状态文件 + 核 pid，不 import pilot —— 本脚本要能独立跑）。

    ⚠️ 为什么光读 `running` 不够，还得核 pid：
      状态文件里的 `running` 是进程**自己写**的，进程被 `kill -9` 或被
      OOM 杀掉之后，没人把它改回 false。实测（服务器 2026-10-13）：

        voyager_pilot_state.json → running=true、why="学习中"、
        started_at 是**两天前**、pid 2817783 早就不存在了。

      只信 `running` 的话，这个僵尸状态会让清档**永远被拒**（面板上
      「MC 真实学习」那个 tab 也永远显示"学习中"）。
      所以 running=true 之后还要看 pid 是不是真活着；pid 没了 = 僵尸，不算在跑。
    """
    out: list[str] = []
    for name, label in _RUNNING_FILES:
        p = ROOT / name
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not d.get("running"):
            continue
        if not _pid_alive(d.get("pid") or d.get("child_pid")):
            continue          # 僵尸状态：进程早没了，只是没人改回 false
        out.append(f"{label}（{name}）")
    return out


def main() -> int:
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    backup = "--no-backup" not in argv
    force = "--force" in argv
    # 换库清档：额外清掉从旧库派生的缓存（entity_catalog / tool_schemas）
    db_switch = "--db-switch" in argv
    targets = list(TARGETS) + (DB_DERIVED if db_switch else [])

    print("=" * 68)
    print("  MVE 清档" + ("（DRY RUN，不会真的删）" if dry else "")
          + ("（换库模式：额外清旧库派生缓存）" if db_switch else ""))
    print("=" * 68)

    existing = [(name, desc) for name, desc in targets if (ROOT / name).exists()]
    for name, desc in existing:
        size = (ROOT / name).stat().st_size
        print(f"  {name:26s} {size:>9,} B   {desc}")
    missing = [n for n, _ in targets if not (ROOT / n).exists()]
    if missing:
        print(f"  （本来就不存在：{', '.join(missing)}）")

    keep_here = [n for n, _ in KEEP
                 if (ROOT / n).exists()
                 and not (db_switch and n in {k for k, _ in DB_DERIVED})]
    if keep_here:
        print(f"\n  有意保留（不是学习成果）：{', '.join(keep_here)}")

    # 数据源报告 dry-run 也要打 —— 人往往就是先 dry-run 看一眼"会动到什么"，
    # 这时候还没真删，正是确认"我在哪个库上"的最后机会。
    _report_db()

    if dry or not existing:
        if not existing:
            print("\n  没有可清的东西。")
        return 0

    # ⚠️ 守卫：还有进程在写这些文件就拒绝 —— 清完立刻被写回，等于没清，
    #   而且会留下"清了一半"的脏状态（run_log 空了但 mastery 是新的）。
    busy = _running()
    if busy and not force:
        print("\n  ⚠️ 拒绝清档：还有东西在跑 —— " + "、".join(busy))
        print("     清完它们下一轮就把状态写回去了，清了等于没清。")
        print("     先停（面板「■ 停止」），再清档。")
        print("     确实要硬清：python mve/reset_all.py --force")
        return 1

    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = ROOT / f"_backup_{stamp}"
        dest.mkdir(exist_ok=True)
        for name, _ in existing:
            shutil.copy2(ROOT / name, dest / name)
        print(f"\n  已备份到：{dest}")

    for name, _ in existing:
        (ROOT / name).unlink()
    print(f"\n  已清 {len(existing)} 个文件。现在的状态：零技能、零记录、零因果、零进度。")

    # 清完第一件事是确认"我到底在哪个数据源上" —— 学习记录清了，数据源没清，
    # 两者不同步就是"清空了但取到的还是旧库的数"这种最难查的状态。
    _report_db()

    print("\n  下一步：python mve/run_mve.py --llm （或面板上点「启动 Voyager」）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
