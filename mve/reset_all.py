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
    print("  下一步：python mve/run_mve.py --llm （或面板上点「启动 Voyager」）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
