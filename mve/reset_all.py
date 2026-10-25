#!/usr/bin/env python3
"""一次性清空 MVE 的全部学习状态。

为什么单开一个文件：`run_mve.py --reset` 只清技能库 + 运行日志 + 裁判缓存，
但「学习状态」还散落在十几处。只清一部分会留下**对不上的曲线**：
比如 run_log 空了但因果时间线还在，出题器就会照着旧的时间线选题，
而掌握度视图里什么都看不到。

⚠️ 2026-10-26 补齐（用户：「清库按钮要清得彻底，连账本都清掉」）：
  全清原来只删 TARGETS 整文件，**不删按场景分目录的 ckpt** ——
  ckpt_curriculum/<场景>/（课程两本账）和 ckpt_maieutic/<场景>/
  （追问骨架 + 维度进度 + _current_target 锚点）全残留。实测
  （2026-10-25 面板全清后重学场景 B）：毒化的里程碑骨架照旧点亮、
  旧锚点照旧把学习循环拽回旧目标。全清现在把这两个目录**整删**
  （所有场景一起归零，先备份）；按场景清早就走 SCENE_DIRS，不受影响。

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
  skill_emb.json         技能向量索引（embedding 检索用，跟 skill_store 走 ★补）
  ckpt_curriculum/<场景>/ 课程两本账（completed / failed，按场景分目录 ★补）
  ckpt_maieutic/<场景>/   追问骨架缓存 + 维度进度 + _current_target（★补）
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
    python mve/reset_all.py            # 先备份再清（**全库**：所有场景一起清）
    python mve/reset_all.py --dry-run  # 只看会清什么
    python mve/reset_all.py --no-backup
    python mve/reset_all.py --force    # 循环还在跑也硬清（不推荐）
    python mve/reset_all.py --db-switch  # 换库清档：额外清旧库派生缓存
                                         # （entity_catalog / tool_schemas ——
                                         #   旧库的队名/图名在新库查不到）

    # **只清一个场景**（2026-10-19 加）：其它场景的学习成果、数据源、场景注册
    # 全保留；课程账 / 骨架缓存本来按场景分目录，日志与图谱按 root_scene 过滤。
    python mve/reset_all.py --scene D
    python mve/reset_all.py --scene D --dry-run

⚠️ 按场景清**不动**这些（不是学习成果，或属于其它场景）：
    scene_data/<场景>/       人的本机文件 —— 那是**数据源**（同 .duckdb）
    scenes.json              场景注册（清了场景就没了）
    action_registry.json     猫爪总开关 / 组开关
    exam_log.jsonl           裸考记录，不带场景字段
    技能库中其它场景/default 的配方
⚠️ 2026-10-26 起，按场景清**会清** `source_scene=本场景` 的技能配方
   （用户：「清场景清彻底，连账本都清掉」后重学 —— 旧配方残留会被
   embedding 盲取检索回来，含毒配方，等于没从零学）。
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
    # 向量索引（2026-10-26 embedding 检索引入）：跟 skill_store.json 走，
    # 全清不清它 → 重建的技能库带着旧指纹缓存，虽然 _load_embs 会按指纹
    # 懒补，但孤儿向量留着就是第二个 110MB 的隐患（2026-10-21 面板挂死同款）。
    ("skill_emb.json", "技能向量索引（跟 skill_store.json 走）"),
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


def _scene_ids() -> list[str]:
    """已注册的场景 id（给 `--scene` 做校验，防止打错字清了个寂寞）。"""
    try:
        import scenes
        return [str(s.get("id") or "") for s in scenes.list_scenes()]
    except Exception:
        try:
            d = json.loads((ROOT / "scenes.json").read_text(encoding="utf-8"))
            return [str(k) for k in (d.get("scenes") or d)]
        except Exception:
            return []


# 按场景清：**目录**直接删（天然按场景分目录）
SCENE_DIRS = [
    ("ckpt_curriculum/{s}", "课程两本账（completed / failed / failed_why）"),
    ("ckpt_maieutic/{s}", "Maieutic 追问骨架缓存 + 维度进度（断点续学）"),
]

# 全清（不带 --scene）要**整删**的目录（2026-10-26 补）：所有场景的课程账 /
# 骨架缓存一起归零。以前全清只删 TARGETS 整文件，这两个目录残留 —— 实测
# （2026-10-25 面板全清后重学场景 B）毒化的里程碑骨架照旧点亮、旧
# _current_target 锚点照旧把学习循环拽回旧目标，用户被迫手工补刀
# reset_all.py --scene B。按场景清走上面的 SCENE_DIRS，不受影响。
FULL_RESET_DIRS = [
    ("ckpt_curriculum", "课程两本账（所有场景）"),
    ("ckpt_maieutic", "追问骨架 + 维度进度 + _current_target 锚点（所有场景）"),
]

# 按场景清：**逐条过滤重写**（只删这个场景的行/条目，其它场景原样留着）
#   (文件名, 场景字段名, 说明)
SCENE_ROWS = [
    ("run_log.jsonl", "scene", "每轮作答（练习线）"),
    ("causal_timeline.jsonl", "scene", "行动因果时间线"),
    ("coach_log.jsonl", "root_scene", "人导入历史 + 消费记录"),
    ("topic_mastery.jsonl", "root_scene", "掌握库（每个节点的进度 / 完成情况）"),
    ("runtime_topics.json", "root_scene", "运行期生成的图谱节点"),
    ("pending_dimensions.json", "root_scene", "待审维度候选"),
]


def _scene_matches(rec: dict, key: str, scene: str) -> bool:
    """这条记录属于该场景吗？

    ⚠️ 口径与 `build_state` 一致（2026-10-19 定的全页签场景化）：**旧记录没有
    场景字段 → 一律归默认场景 A**。所以清 A 会连"没标场景的历史记录"一起清，
    清其它场景不会误伤它们。
    """
    val = str(rec.get(key) or "").strip()
    if not val:
        return scene == "A"
    return val == scene


def _clear_scene(scene: str, *, dry: bool, backup: bool) -> int:
    """只清**一个场景**的学习成果，其它场景 / 数据源 / 场景注册原样保留。

    为什么单开这条路径（用户 2026-10-19 问「能清掉对应场景的库吗」）：
    `TARGETS` 那套是**整文件删**，做不到"只清 D 不碰 A"。而课程账
    （`ckpt_curriculum/<scene>/`）和骨架缓存（`ckpt_maieutic/<scene>/`）
    本来就按场景分目录，图谱/日志里每条也带 `root_scene` / `scene` ——
    按场景清是能做到的，只是要换成"过滤重写"而不是"删文件"。

    **有意不动**（打印出来让人看见）：
      · `scene_data/<scene>/` —— 人的本机文件，那是**数据源**（同 .duckdb，
        不在清库范围），清了等于删人东西；
      · `scenes.json` / `action_registry.json` —— 场景注册与猫爪开关，是配置；
      · `exam_log.jsonl` —— 不带场景字段。
      · `skill_store.json` 里 **source_scene=其它场景/default 的技能** ——
        跨场景复用资产，别的场景照样用。

    ⚠️ 2026-10-26 改（用户：「清场景清彻底，连账本都清掉」后重学）：
      `source_scene=本场景` 的技能**现在要清**。以前按场景清不动
      skill_store（技能被当跨场景资产整体保留）—— 但重学场景时旧配方
      残留会被 embedding 盲取 top_k 检回来（检索已无词面门槛，2026-10-26
      对齐原版），含当年漏放的毒配方（total_leave 伪日薪口径），等于没
      从零学。逐条走 `skill_store.delete_skill`（铁律：不手写 JSON）。
    """
    print("=" * 68)
    print(f"  MVE 清档 · 只清场景 {scene}"
          + ("（DRY RUN，不会真的删）" if dry else ""))
    print("=" * 68)

    # 1. 目录类
    dirs = [(ROOT / p.format(s=scene), d) for p, d in SCENE_DIRS
            if (ROOT / p.format(s=scene)).exists()]
    # 2. 条目类：先算清楚会删多少条
    row_stats: list[tuple[Path, str, str, int, int]] = []
    for name, key, desc in SCENE_ROWS:
        p = ROOT / name
        if not p.exists():
            continue
        try:
            if name.endswith(".jsonl"):
                recs = [json.loads(l) for l in p.read_text(
                    encoding="utf-8").splitlines() if l.strip()]
            else:
                d = json.loads(p.read_text(encoding="utf-8"))
                recs = list(d.values()) if isinstance(d, dict) else list(d)
        except Exception as e:
            print(f"  ⚠️ 读不了 {name}（跳过）：{type(e).__name__}: {e}")
            continue
        hit = sum(1 for r in recs if isinstance(r, dict)
                  and _scene_matches(r, key, scene))
        row_stats.append((p, key, desc, len(recs), hit))
    # 3. 图谱：这个场景的节点 + 挂在它们上的边
    gpath = ROOT / "knowledge_graph.json"
    drop_nodes, drop_edges = 0, 0
    if gpath.exists():
        try:
            g = json.loads(gpath.read_text(encoding="utf-8"))
            ns = g.get("nodes") or []
            es = g.get("edges") or []
            ids = {str(n.get("id")) for n in ns
                   if isinstance(n, dict)
                   and _scene_matches(n, "root_scene", scene)}
            drop_nodes = len(ids)
            drop_edges = sum(1 for e in es if isinstance(e, dict)
                             and (str(e.get("from") or "") in ids
                                  or str(e.get("to") or "") in ids))
        except Exception as e:
            print(f"  ⚠️ 读不了图谱（跳过）：{type(e).__name__}: {e}")
    # 4. 技能：source_scene == 本场景 的配方（2026-10-26 补，重学要从零）。
    #    只收集统计，真删走 delete_skill（含向量连带清理），不手写 JSON。
    sk_del: list[tuple[str, str]] = []
    try:
        import skill_store as _ss
        _skdata = _ss._load_raw()
        sk_del = [(k, str(s.get("name") or "")) for k, s in
                  (_skdata.get("skills") or {}).items()
                  if str(s.get("source_scene") or "default") == scene]
    except Exception as e:
        print(f"  ⚠️ 读不了技能库（跳过）：{type(e).__name__}: {e}")

    print("\n  会清掉：")
    for d, desc in dirs:
        print(f"    目录 {d.relative_to(ROOT)}/  {desc}")
    for p, _k, desc, total, hit in row_stats:
        if hit:
            print(f"    {p.name:26s} {hit}/{total} 条   {desc}")
    if drop_nodes:
        print(f"    {'knowledge_graph.json':26s} {drop_nodes} 个节点 + "
              f"{drop_edges} 条边（root_scene={scene}）")
    if sk_del:
        print(f"    {'skill_store.json':26s} {len(sk_del)} 条配方  "
              f"（source_scene={scene}："
              f"{', '.join(n or k for k, n in sk_del[:6])}"
              f"{'…' if len(sk_del) > 6 else ''}）")
    if (not dirs and not any(h for *_x, h in row_stats) and not drop_nodes
            and not sk_del):
        print("    （这个场景没有可清的东西）")
        return 0
    print("\n  有意不动：scene_data/<场景>/（人的本机数据 = 数据源）、"
          "scenes.json（场景注册）、")
    print("            action_registry.json（猫爪开关）、exam_log.jsonl、"
          "技能库中其它场景/default 的配方")

    if dry:
        return 0

    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = ROOT / f"_backup_{stamp}_scene{scene}"
        dest.mkdir(parents=True, exist_ok=True)
        for d, _ in dirs:
            # ⚠️ 必须保留相对 ROOT 的目录结构：ckpt_curriculum/B 与
            # ckpt_maieutic/B 的 d.name 都是 "B"，直接 dest/d.name 会撞名
            # FileExistsError —— 导致 --scene 清档永远卡在备份、清不掉（实测
            # 2026-10-25 场景 B 清库失败、脏账残留）。
            _rel = d.relative_to(ROOT)
            _bd = dest / _rel
            _bd.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(d, _bd)
        for p, *_ in row_stats:
            shutil.copy2(p, dest / p.name)
        if gpath.exists():
            shutil.copy2(gpath, dest / gpath.name)
        # 技能库也备份（2026-10-26 起按场景清会删本场景配方）
        sk_path = ROOT / "skill_store.json"
        if sk_del and sk_path.exists():
            shutil.copy2(sk_path, dest / sk_path.name)
            emb_path = ROOT / "skill_emb.json"
            if emb_path.exists():
                shutil.copy2(emb_path, dest / emb_path.name)
        print(f"\n  已备份到：{dest}")

    # 目录：整删
    for d, _ in dirs:
        shutil.rmtree(d)
    # 条目：过滤重写（清干净了就删文件，不留空壳）
    for p, key, _desc, total, hit in row_stats:
        if not hit:
            continue
        if p.name.endswith(".jsonl"):
            keep = []
            for l in p.read_text(encoding="utf-8").splitlines():
                if not l.strip():
                    continue
                try:
                    r = json.loads(l)
                except Exception:
                    keep.append(l)          # 坏行原样留着，别顺手吞掉
                    continue
                if isinstance(r, dict) and _scene_matches(r, key, scene):
                    continue
                keep.append(l)
            if keep:
                p.write_text("\n".join(keep) + "\n", encoding="utf-8")
            else:
                p.unlink()
        else:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d = {k: v for k, v in d.items()
                     if not (isinstance(v, dict)
                             and _scene_matches(v, key, scene))}
            else:
                d = [v for v in d if not (isinstance(v, dict)
                                          and _scene_matches(v, key, scene))]
            if d:
                p.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                             encoding="utf-8")
            else:
                p.unlink()
    # 图谱：用模块自己的 API 重写（save() 带残图防呆，不会把 A 的图写坏）
    if drop_nodes:
        try:
            sys.path.insert(0, str(ROOT))
            from knowledge_graph import KnowledgeGraph
            g = KnowledgeGraph.load(gpath)
            keep_ids = {i for i, n in g.nodes.items()
                        if (str(getattr(n, "root_scene", "") or "").strip()
                            or "A") != scene}
            if not keep_ids:
                # 清的是默认场景（A）：整图都归它 → 删文件交给面板自愈重建，
                # 否则 save() 的"残图不许覆盖真图"防呆会拒绝写。
                gpath.unlink()
                print("  · 图谱：节点全部属于该场景 → 整份删除"
                      "（面板下次启动用种子题重建）")
            else:
                ng = KnowledgeGraph()
                for i in keep_ids:
                    ng.add_node(g.nodes[i])
                for e in g.edges:
                    if e.src in keep_ids and e.dst in keep_ids:
                        ng.add_edge(e)
                ng.save(gpath)
                print(f"  · 图谱：剩 {len(ng.nodes)} 节点 / {len(ng.edges)} 边")
        except Exception as e:
            print(f"  ⚠️ 图谱按场景清理失败（未改动）：{type(e).__name__}: {e}")
    # 技能：逐条走 delete_skill（本体 + 向量连带清，铁律：不手写 JSON）
    if sk_del:
        try:
            import skill_store as _ss2
            _n = sum(1 for k, _ in sk_del if _ss2.delete_skill(k))
            print(f"  · 技能：删掉 source_scene={scene} 的 {_n} 条配方"
                  "（其它场景/default 保留）")
        except Exception as e:
            print(f"  ⚠️ 技能清理失败（未改动）：{type(e).__name__}: {e}")
    print(f"\n  已清场景 {scene}。其它场景的学习成果、数据源、场景注册都原样保留。")
    print("  下一步：面板切到该场景点「练题」，或 "
          f"`python mve/scene_learn.py --scene {scene} --max-iterations N`")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    backup = "--no-backup" not in argv
    force = "--force" in argv
    # 只清某一个场景（其余场景 / 数据源 / 场景注册都保留）
    scene = ""
    for a in argv:
        if a.startswith("--scene="):
            scene = a.split("=", 1)[1].strip()
        elif a == "--scene" and argv.index(a) + 1 < len(argv):
            scene = argv[argv.index(a) + 1].strip()
    if scene:
        known = _scene_ids()
        if known and scene not in known:
            print(f"  ⚠️ 场景 {scene!r} 不存在（已注册：{', '.join(known)}）"
                  " —— 不猜、不自动创建，防止打错字清了别的地方。")
            return 1
        busy = _running()
        if busy and "--force" not in argv:
            print("  ⚠️ 拒绝清档：还有东西在跑 —— " + "、".join(busy))
            print("     先停（面板「■ 停止」），或加 --force 硬清。")
            return 1
        return _clear_scene(scene, dry=dry, backup=backup)
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
    # 全清目录（2026-10-26 补）：课程账 / 骨架缓存按场景分目录，TARGETS 整
    # 文件删够不着它们 —— 这里整删（所有场景一起归零）。
    dirs_existing = [(name, desc) for name, desc in FULL_RESET_DIRS
                     if (ROOT / name).exists()]
    for name, desc in dirs_existing:
        n = sum(1 for _ in (ROOT / name).rglob("*"))
        print(f"  {name + '/':26s} {n:>9,} 项   {desc}")
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

    if dry or (not existing and not dirs_existing):
        if not existing and not dirs_existing:
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
        # 目录也要备份（2026-10-26：ckpt 进了全清范围，不备份 = 课程账
        # 误删无解）。保留相对结构防 ckpt_curriculum/A 与 ckpt_maieutic/A
        # 撞名（同 _clear_scene 备份的教训）。
        for name, _ in dirs_existing:
            _bd = dest / name
            _bd.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(ROOT / name, _bd)
        print(f"\n  已备份到：{dest}")

    for name, _ in existing:
        (ROOT / name).unlink()
    for name, _ in dirs_existing:
        shutil.rmtree(ROOT / name)
    print(f"\n  已清 {len(existing)} 个文件 + {len(dirs_existing)} 个目录。"
          "现在的状态：零技能、零记录、零因果、零进度、零课程账、零骨架。")

    # 清完第一件事是确认"我到底在哪个数据源上" —— 学习记录清了，数据源没清，
    # 两者不同步就是"清空了但取到的还是旧库的数"这种最难查的状态。
    _report_db()

    print("\n  下一步：python mve/run_mve.py --llm （或面板上点「启动 Voyager」）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
