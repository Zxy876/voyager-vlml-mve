#!/usr/bin/env python3
"""Voyager 式自动课程（非 VLML 场景）—— 原版 CurriculumAgent 的同构实现。

原版取证（voyager_original/voyager/agents/curriculum.py，逐条照抄）
------------------------------------------------------------------
  :29-93   `__init__`：`completed_tasks` / `failed_tasks` 两本账，落
           ckpt/curriculum/*.json（resume 时读回）
  :126-136 `progress` = len(completed_tasks)
  :150-224 `render_observation`：把环境状态拼成字段字典，其中
           `completed_tasks` → "Completed tasks so far: ..."
           `failed_tasks`    → "Failed tasks that are too hard: ..."
  :226-262 `render_human_message`：按 warm_up 渐进披露字段（进度不够就不放某些项）
  :264-291 `propose_next_task(max_retries=5)`：inventory 满等硬编码分支 → 否则走 LLM
  :293-311 `propose_next_ai_task`：LLM 输出里 parse `Task:` 行；**解析失败递归
           重试 max_retries=5 次**（这是课程侧的重试机制，不是任务重试）
  :330-345 `update_exploration_progress(info)`：success → completed_tasks.append，
           否则 → failed_tasks.append + "Skipping to next task"
  :347-365 `clean_up_tasks`：completed 去重；**completed 的任务从 failed 里删掉**

MVE 同构映射（字段 → 本机场景）
-----------------------------
  biome / nearby_blocks  → 数据源 = 本机工作区（scene_data/<id>）
  nearby_entities        → **刻意不放**（候选答案清单，会把重心拉偏，同 VLML 版决定）
  inventory / chests     → 工作区文件清单（带小文件结构预览）+ 已学技能数
  completed_tasks        → 学成功的问题（含 topic_id）
  failed_tasks           → 学过没学出来的问题（含原因）—— 出题时明确告诉模型「别出这些」
  "Mine 1 wood log"      → 队列里的第一条人导入题（人给的第一题 = 种子）

两种任务来源（对应原版 auto / manual）
------------------------------------
  · 人导入队列（routing.pending 里本场景的题）优先 —— 等价原版 manual 模式的人给题；
  · 队列空 → LLM 自动出题（原版 auto 模式），**不落 pending**（那是人导入的账），
    只进 completed/failed 两本账 —— 混账会让失败的自动题下次被当"人导入"重试。

自欺防线
--------
  · 出题必须能真取数：把文件清单 + 结构预览递给模型，让它指出依据（basis）；
  · 目录为空 / LLM 挂 / 解析 5 次失败 → 如实返回 ("", why)，循环照实退出；
  · 出的题不许和 completed/failed 重复（clean_up_tasks 语义的反向使用）。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

STATE_DIR = Path(__file__).resolve().parent / "ckpt_curriculum"

_SYSTEM = """你是 Voyager 式自动课程规划器（Minecraft 版 Voyager 的 curriculum agent 的本机同构）。

环境：一个本机工作区（用户自己的文件）或一个场景自己的 SQL 库。
你要做的：根据「数据源的真实结构」+「已完成任务」+「失败过的任务及其原因」，
规划**下一个**任务（一句自然语言问题），让一个只能读文件/查本地 duckdb 的助手去完成。

硬约束（照原版：任务必须"当前可完成"）：
1. 任务必须能从数据源真取数回答（读文件、数数、排序、求和、求平均、对比……）；
   不要时效性题（最新/今年），不要数据源里没有的外部知识；
2. 已完成的任务不要再出；失败过的任务换一个更简单/不同的角度；
3. 任务要具体（点明表/文件与数据范围），要有可判定的完成标准；
4. 严格按格式输出两行（原版 parse_ai_message 只认 Task: 行）：
Task: <任务，一句话，不要句号结尾>
Context: <一句话说明：用哪个文件/表、算什么、完成标准是什么>

重新规划的规则（2026-10-23 用户实测「同一道必败题反复出」之后加）：
5. 失败过的题是**里程碑**：它值得学，但当前水平达不到。你的职责不是原样或换个
   说法再出它 —— 是规划它**前置的、现在就能做对**的子任务（一次只查一张表、
   只算一个数），学会了再逐步逼近。看「为什么失败」：缺哪块能力，就先补哪块。
6. 题里的字面量（部门名、月份、类型、人名……）必须**逐字**取自观察里列出的
   「真实取值」；清单里没有的说法不要用（写了也查不到，必然 0 行）。
7. 已学会的技能还很少时（看「已学会的技能」计数），只出**单步题**：
   一张表、一个数或一行 —— 原版 "Mine [quantity] [block]" 的同构。
8. 题目只能引用观察里 schema 实查出现的**表名和列名**；数据源里不存在的
   业务参数（费率、汇率、税率、政策比例……）**不许假设** —— 出了这种题，
   助手只能编一个数（实测 2026-10-24：编出"每小时加班费率 150 元"还入库成
   了技能）。库里没有的参数，要么不出这类题，要么把「数据源缺少 X，
   无法计算」本身作为任务。
"""

# 代码取证场景的**自由学**系统提示（2026-11-17 用户拍板）。
#
# 口径来源：Voyager 原版 prompts/curriculum.txt —— 人不打扰的自动课程只有
#   ① 实查观察面（Biome / Nearby blocks / Inventory）
#   ② 两本状态账（Completed tasks / Failed tasks）
#   ③ 一句话任务（`Task: Mine 1 wood log`）+ 8 条 criteria
# **没有关卡、没有章节、没有进度百分比。** 我 2026-11-14 给书单场景写的
# _SYSTEM_BOOKS 是「按当前单元出数学题」，那是地图；自由学不能用它。
#
# 本场景唯一的判据是一句话（用户原话）：
#   B ⊆ A，证明 B ≠ A —— A=原机（仓库跑起来）的真实运行，
#   B=仓库对自己的声明（docstring / README / 类型签名 / 测试断言 / CLI help）。
# 数学上这是 Cantor 对角化，停机问题只是取「会不会停」这个属性时的特例：
# 停机性不可判定 ⇒ 任何有限声明集都预测不全原机行为 ⇒ B ≠ A 必然成立，
# 任务只是找**哪一个**见证。所以不需要题库、不需要考点，永远有下一句。
_SYSTEM_GROUNDED = """你是 Voyager 式自动课程规划器（原版 curriculum agent 的同构），
运行在一个**真实代码仓库**上。玩家是一个能读文件、能真跑 python 的助手；
它的世界就是这个仓库跑起来会发生什么。

你要做的：给出**下一句**任务。整场只有一个目标，不分关卡、不分章节、不报进度：
  A = 原机（这个仓库跑起来）的真实运行
  B = 仓库对自己的声明（docstring / README / 类型签名 / 测试断言 / CLI 帮助）
  已知 B ⊆ A。**证明 B ≠ A**：给出一个原机真实行为，它不在 B 的声明里。

硬约束（照原版 8 条 criteria 的角色）：
1. 一次只提**一个**任务，`Task:` 行就是这句话本身，不解释、不列清单。
2. 这句话必须带一个**具体落点**（某个文件里的某个函数/类/分支/参数），
   落点逐字取自下面「仓库实查」里的文件名与符号名 —— 原版
   `Mine [quantity] [block]` 的同构是：`证明 B ≠ A：<文件> 的 <符号>`。
   实查里没有的名字一律不许写（写了就是编的文件名，必然失败）。
3. **优先停机方向**（最强见证，机器一跑就判得出，不需要人看）：
   · 无圈：B 读起来会正常结束/返回，实跑在限定步数内不返回（超时、死循环、
     递归爆栈前的无进展）；
   · 单圈：B 读起来不会正常结束/会抛错/会拒绝，实跑却正常返回。
   次选：B 声称的返回值、异常类型、副作用，与实跑不同。
4. 必须**一圈落一个判定**：要么印证（这条声明确实是 A 的真实行为），
   要么证伪（B 漏了这个行为）。不许出「还需要更多信息才能判」的任务。
5. 不要重复 Completed tasks 里已经证过的落点；Failed tasks 里失败过的落点，
   换一个更小的切口（更小的函数、更窄的输入），不要原样或换个说法再出。
6. 不要出「这个仓库是做什么的 / 有几个文件 / 某个模块大概干什么」这类
   元信息题 —— 那不需要真跑，验不了。
7. 不要提单元、章节、第几关、进度百分比：**这个场景没有这些东西**。
8. 题面用简体中文（代码标识符、文件名保持原样）。

严格按两行输出：
Task: 证明 B ≠ A：<落点> —— <要构造的那个行为>
Context: <B 读起来会怎样 / 实跑要构造什么输入 / 凭什么判 B 漏了它>
"""


# 观察面的**体量上限**（2026-11-09 服务器实测踩到的）：
# 场景 L 的 web_facts 是宽表（3736 行 × 910 列），逐列打「真实取值」会产出
# 909 行 / 57,254 字符 —— 骨架 prompt 直接被撑爆。更糟的是 render_material 里
# 同一份 db_observation 出现**两次**（scene_sources 里一次 + 末尾又拼一次）
# → 114k 字符，模型根本看不到人导入的那句话。
# 所以这里按**体量**收敛（不是按内容删）：
#   · 列名：超过阈值就**等距抽样**（保住头尾与中段；只列前 40 列会全落在
#     前几个站点上，看不出这张表铺了多宽）；
#   · 取值：每张表最多 _MAX_VALUE_LINES 行；
#   · 整份：最多 _MAX_OBS_CHARS 字符（超出就明说截断了多少）。
_MAX_COLS_PER_TABLE = 40
_MAX_VALUE_LINES = 24
_MAX_OBS_CHARS = 12000
# 2026-11-10：观察面升级「摘要 + 原始事实」（取值×计数 + 样本行），
# 上限随之放宽；双拼 bug 已修（render_material 只拼一次），16k 安全。
_MAX_OBS_CHARS = 16000
# 样本行上限：每表倒几行**真实数据**（仅非空单元格），同构原版
# render_observation 的「只倾倒」，一行代表什么由模型自己看出来。
_MAX_SAMPLE_ROWS = 3
_MAX_SAMPLE_CELLS = 14


def repo_observation(root: str, *, max_files: int = 60,
                     max_chars: int = 7000) -> str:
    """仓库实查（原版 `Nearby blocks` 的同构）：真实文件 + 顶层符号。

    只给「能碰到什么」，不给「该走哪条路」—— 没有单元、没有章节、没有进度。
    原版 agent 看到的是 `nearby_blocks: grass, oak_log, ...`；这里看到的是
    `llm_client.py：chat()、_post()`。出题器据此挑落点，模型没法脑补文件名。
    """
    import ast
    import os as _os
    if not root or not _os.path.isdir(root):
        return "None（数据源不是一个可读目录 —— 绝对不要编造文件名）"
    files = []
    for dirpath, dirnames, filenames in _os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "__pycache__", ".venv", "venv",
                                    "node_modules", ".mypy_cache")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            # 调试残渣不进实查（2026-11-17 实测：出题器挑中 _probe_* 的假桩
            # 当落点 —— 那是调试现场，不是这个世界里真实可用的行为）。
            if fn.startswith("_probe") or ".bak" in fn:
                continue
            files.append(_os.path.join(dirpath, fn))
    files.sort()
    total = len(files)
    out, used = [], 0
    for pth in files[:max_files]:
        rel = _os.path.relpath(pth, root)
        syms = []
        try:
            with open(pth, encoding="utf-8", errors="ignore") as f:
                tree = ast.parse(f.read())
            for n in tree.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    syms.append(n.name + "()")
                elif isinstance(n, ast.ClassDef):
                    syms.append(n.name)
        except Exception:
            pass
        line = "- " + rel + (("：" + "、".join(syms[:10])) if syms else "")
        used += len(line) + 1
        if used > max_chars:
            out.append("- …（还有 %d 个 .py 文件未列出）"
                       % (total - len(out)))
            break
        out.append(line)
    return ("这个场景的数据源是一个**真实代码仓库**（%d 个 .py）。下面是实查到的"
            "文件与顶层符号（全部真实；任务里的落点必须逐字来自这里）：\n"
            % total + "\n".join(out))

def db_observation(db_path: str) -> str:
    """实查场景库：表 → 行数 → 列 → 低基数字符串/布尔列的真实取值。

    为什么提成模块级（2026-10-24 用户定「用原 voyager 看环境的方法来抄」）：
    原版所有 agent（curriculum / action / critic）看到的是**同一份实查渲染的
    环境末态**（背包物品×数量、附近方块）—— critic 之所以能验证「挖到铁矿」，
    是因为末态清单里看得见 `iron_ore ×1`。SQL 场景的同构物就是这份清单：
    出题器拿它保证「只出观察面能验证的题」，critic 拿它指认「任务假设的
    表/列/业务参数是否真的存在」（实测 2026-10-24：模型编出"每小时加班费率
    150 元"，critic 没有环境清单可对照，把幻觉当判据放行了）。

    只用 DuckDB 标准语句，不需要任何领域知识；打不开/查不动就返回空串
    （上层如实说"实查失败"，不编造）。
    """
    out = ""
    try:
        import duckdb
        con = duckdb.connect(str(db_path or ""), read_only=True)
        try:
            tables = [r[0] for r in con.execute("show tables").fetchall()]
            lines: list[str] = []
            # ★ 目录优先（2026-11-13）：**表清单 + 全部列名**单独一份，永不被
            # 字数上限截掉；后面的取值/样本才是可截断的细节。
            # 事故：场景 N 有 9 张表、观察面 16028 字，上限 16000 → 按字母序排在
            # 最后的 math_toc 被整段砍掉 → Phase2 的 critic 断言「数据源里不存在
            # math_toc 表，该步骤不可答」，Phase1 学生也一直说「数据源结构未提供
            # 这两个模块的分布」——**表其实存在**，只是清单被截断了。
            # 判官/学生看到的环境面不全 = 它们判"不可答"是对的（按它们看到的），
            # 所以修法必须是"清单完整、细节有上限"，而不是放宽某个阈值。
            catalog: list[str] = []
            for t in tables:
                try:
                    cnt = con.execute(f'select count(*) from "{t}"').fetchone()[0]
                    cols = con.execute(f'describe "{t}"').fetchall()
                except Exception:
                    continue
                cnames = [str(c[0]) for c in cols]
                _cl = ", ".join(cnames)
                if len(_cl) > 600:
                    _cl = _cl[:600] + f" …（共 {len(cnames)} 列）"
                catalog.append(f"- 表 {t}（{cnt} 行）：" + _cl)
            for t in tables:
                try:
                    cnt = con.execute(f'select count(*) from "{t}"').fetchone()[0]
                    cols = con.execute(f'describe "{t}"').fetchall()
                except Exception:
                    continue
                cnames = [str(c[0]) for c in cols]
                # 详细段只留短表头（全部列名在目录段，那里不截断）
                if len(cnames) > _MAX_COLS_PER_TABLE:
                    head = cnames[:8]
                    rest = cnames[8:]
                    step = max(1, len(rest) // max(1, _MAX_COLS_PER_TABLE - len(head)))
                    picked = list(head) + list(rest[::step])[: _MAX_COLS_PER_TABLE - len(head)]
                    lines.append(f"- 表 {t}（{cnt} 行）：")
                    shown_cols = {str(c[0]): str(c[1]).upper() for c in cols
                                  if str(c[0]) in picked}
                else:
                    lines.append(f"- 表 {t}（{cnt} 行）：")
                    shown_cols = {str(c[0]): str(c[1]).upper() for c in cols}
                # ---- 取值：基数 + 取值×计数（2026-11-10）----
                # 计数是原始事实（同构原版 iron_ore ×1），不是解读：
                # 「item 2 个不同取值: ce(×1868)」让拆解器自己看出一键多行
                # （分片宽表），「source_url 13+ 个不同取值」让它看出近似唯一。
                _lines = 0
                for cname, ctype in shown_cols.items():
                    if _lines >= _MAX_VALUE_LINES:
                        break
                    if ctype not in ("VARCHAR", "TEXT", "STRING", "BOOLEAN"):
                        continue
                    try:
                        _pairs = con.execute(
                            f'select "{cname}", count(*) from "{t}" '
                            f'where "{cname}" is not null '
                            f'group by 1 order by 2 desc limit 13').fetchall()
                    except Exception:
                        continue
                    if not _pairs:
                        continue
                    try:
                        _nd = con.execute(
                            f'select count(distinct "{cname}") from "{t}"'
                        ).fetchone()[0]
                    except Exception:
                        _nd = len(_pairs)
                    _parts = []
                    for _v, _c in _pairs:
                        _vs = str(_v)
                        if len(_vs) > 60:
                            _vs = _vs[:57] + "..."
                        _parts.append(f"{_vs}(×{_c})")
                    _head = f"    · {t}.{cname} {_nd} 个不同取值"
                    if _nd <= 12:
                        lines.append(_head + ": " + "、".join(_parts))
                    else:
                        # 高基数列也列**最多的几个**取值×计数（原始事实，让拆解器
                        # 看见「ce 占 459 行」这类集中度；长尾不列）
                        lines.append(_head + "（列最多的几个）: " + "、".join(_parts))
                    _lines += 1
                # ---- 样本行：原始末态倾倒（2026-11-10，同构原版 render_observation）----
                # 原版不解读环境：只倒「背包 iron_ore ×1、附近 grass」。
                # 这里只倒几行真实数据（仅非空单元格），一行代表什么、
                # 要不要聚合，由拆解器自己看出来。
                try:
                    _rc = con.execute(f'select * from "{t}" limit {_MAX_SAMPLE_ROWS}')
                    _all_cols = [str(d[0]) for d in _rc.description]
                    _rows = _rc.fetchall()
                    _prev_row = ""
                    for _ri, _row in enumerate(_rows, 1):
                        _cells = [f"{c}: {v}" for c, v in zip(_all_cols, _row)
                                  if v is not None]
                        if not _cells:
                            continue
                        _rtext = ", ".join(str(v) for v in _cells)
                        if _rtext == _prev_row:
                            continue
                        _prev_row = _rtext
                        if len(_cells) > _MAX_SAMPLE_CELLS:
                            _rtext = (", ".join(str(v) for v in _cells[:_MAX_SAMPLE_CELLS])
                                      + f" …（全行 {len(_cells)} 个非空列）")
                        lines.append(f"    · {t} 样本行{_ri}/{len(_rows)}: {_rtext}")
                except Exception:
                    pass
            out = "\n".join(lines)
            if len(out) > _MAX_OBS_CHARS:
                out = (out[:_MAX_OBS_CHARS]
                       + f"\n…（观察面共 {len(out)} 字，只给前 {_MAX_OBS_CHARS} 字）")
            # 目录在前、细节在后，整体再按上限收（先砍细节）：
            _cat = "【全部表与列（完整，不截断）】\n" + "\n".join(catalog)
            _budget = max(2000, _MAX_OBS_CHARS - len(_cat))
            if len(out) > _budget:
                out = (out[:_budget]
                       + f"\n…（取值与样本共 {len(out)} 字，只给前 {_budget} 字）")
            out = _cat + "\n\n【取值与样本】\n" + (out or "（无）")
        finally:
            con.close()
    except Exception:
        out = ""
    return out


SEED_REQUIRED_MSG = (
    "还没有第一个个例（progress == 0）。原版这一圈不由系统发明：manual 模式是"
    "人给题（curriculum.py:322-330 一直等到人输入），auto 模式给的是常量 "
    "\"Mine 1 wood log\"（:264-266），两者都不经过 LLM。"
    "先在「导入」里给一个真实问题或数据，第一圈 123 由它促成；"
    "之后 progress>0 才会自动接着出题。"
)


def _log_band(band: str, msg: str) -> None:
    """档位迁移留痕（只写日志，不改流程）。多天排查时靠它区分
    「学完了」和「卡死了」—— 2026-11-17 之前这两者日志里长得一样。"""
    import time as _t
    try:
        print(f"[{_t.strftime('%H:%M:%S')}] [课程档位] {band} {msg}", flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 背包（2026-11-18 收敛：**全世界只有一个背包**）
#
# 原版背包不是「世界方块清单」，是**事件里的一个字段**：
#   curriculum.py:155   `inventory = event["inventory"]`      ← 从 observe 事件读
#   curriculum.py:205   `Inventory ({inventory_used}/36): …`  ← 槽位容量是常量
#   curriculum.py:251   `if inventoryUsed >= 33:`            ← 满了才存箱
# 世界上有多少方块从来没出现在观察面上（只有 `nearby_blocks` 视野，:198）。
#
# MVE 的对应物 = **会话**：一次 session（phase 1→2→3→4，`session_state.py`）
# 就是原版的一次 rollout；它的 append-only 事件流
# （`maieutic_scaffold.py:2469 append_session_event` / `:2498 load_session_events`）
# 就是 `event["inventory"]` 那个读取口。所以：
#
#   slots       背包格子 = 已跑完的会话，一格一条
#                {session: {"sites": [...], "dims": [...], "rounds": n, "at": ts}}
#   INV_CAP     36 格（原版 :205 的常量，不是数出来的）
#   INV_FULL    33 格满 → 存箱（原版 :251）
#   chests      归档批次 = 存箱；原版 maieutic 的「归档」= phase 4 +
#               completed_at + 一条 phase_transition（`session_state.py` 头注释）
#
# **已删除**：`sites()` 预设全集 + `free = sites - covered`。那是「书单」的
# 另一种形态（先把世界上所有方块列出来，再算还剩几个没挖），Voyager 没有
# 这个东西；它也正是 §16.5 里 `graph_books.next_task` 迟迟不能删的原因。
# 现在候选只来自 `repo_observation()` 的实读窗口（= nearby_blocks，动态）。
# ---------------------------------------------------------------------------

INV_CAP = 36           # 背包槽位（curriculum.py:205 `{inventory_used}/36`）
INV_FULL = 33          # 满格阈值（curriculum.py:251 `inventoryUsed >= 33`）
# 存包阈值（档 B）。**必须是常量**：交给模型决定就退化成「感觉连续」，不可判定。
# 依据 Voyager `curriculum.py:264-291`：世界规则（progress==0 / inventory full）
# 是写死的两条 if，不是判官的自由裁量。
CHAT_PASS_K = 3

# 种子位（第一个 123 的入口）只认这几种来源。见 propose_next_task 的来源闸门。
# 空串 = 老调用口径（裸 str 传 queue），按人导入处理，保持向后兼容。
_SEED_ORIGINS = ("human", "human_import", "manual")


_SITE_RE = re.compile(
    r"(?<![\w/.-])((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.py)"
    r"(?:::[A-Za-z_][A-Za-z0-9_]*)?")
_SYM_TAIL_RE = re.compile(r"\.py\s*(?:::|的\s*)([A-Za-z_][A-Za-z0-9_]*)")


def parse_sites(text: str, root: str) -> list[str]:
    """一段文本（会话事件里的 code/result/题面）→ 真实存在的落点。

    **不查任何预设清单**：只在文本里找 `.py` 路径，再拿 `os.path.isfile`
    实查确认。这就是原版 `event["inventory"]` 的口径 —— 采集到了才有，
    没采到就没有；世界里总共多少从来不用知道。

    为什么不再全仓扫描（2026-11-18 用户口径）：「内容物是跑出来的，不是预设
    的」。旧实现 `repo_sites()` 会先把 1480 个符号列成全集再减已证，
    这在语义上等于「书单」，也正是 `graph_books.next_task` 需要存在的理由。

    返回形如 `a/b.py::func`（能定位到符号）或 `a/b.py`；去重保序。
    """
    import os as _os
    s = str(text or "")
    if not s or not root or not _os.path.isdir(root):
        return []
    out: list[str] = []
    for m in _SITE_RE.finditer(s):
        rel = m.group(1).replace("\\", "/").lstrip("./")
        # 与 repo_observation 同一过滤：调试残渣不当落点（2026-11-17 实测）
        base = rel.rsplit("/", 1)[-1]
        if base.startswith("_probe") or ".bak" in base:
            continue
        if not _os.path.isfile(_os.path.join(root, rel)):
            continue
        hit = rel
        tail = _SYM_TAIL_RE.search(s, m.end() - 4)
        if tail:
            sym = tail.group(1)
            try:
                import ast as _ast
                with open(_os.path.join(root, rel),
                          encoding="utf-8", errors="ignore") as f:
                    tree = _ast.parse(f.read())
                names = {n.name for n in tree.body
                         if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                                           _ast.ClassDef))}
                if sym in names:
                    hit = f"{rel}::{sym}"
            except Exception:
                pass
        if hit not in out:
            out.append(hit)
    return out


def _norm(q: str) -> str:
    return re.sub(r"[\s，。？！,.?!：:；;“”\"'（）()]+", "", str(q or "")).lower()


def _too_similar(q: str, others: Iterable[str]) -> bool:
    """去重：**只做归一化后的精确相等**（原版 clean_up_tasks 就是精确去重）。

    ⚠️ 不要加相似度阈值（我一度加了"字符集 Jaccard ≥ 0.8"，2026-10-19 实测炸了：
    「…比 2025 年**减少**了？」与「…**增加**了至少 20,000？」长句共享字太多，
    被判成同一题 → 合法新题连续被拒 5 次 → 原版 RuntimeError。自然语言里
    "换个问法"本来就是新任务，相似度挡不住语义差异，只会误杀。
    归一化只去空白/标点（中英文标点混用很常见），不做同义词归一。
    """
    n = _norm(q)
    if not n:
        return True
    return any(n == _norm(o) for o in others)


class CurriculumAgent:
    """原版 CurriculumAgent 的本机场景同构版（只做课程规划，不管执行）。"""

    def __init__(self, scene_id: str, *, scene_dir: str = "",
                 db_path: str = "",
                 llm=None, state_dir: str | Path | None = None) -> None:
        self.scene_id = str(scene_id)
        self.scene_dir = str(scene_dir or "")
        # 场景自有 SQL 库（kind=scene 时非空）。2026-10-23 实测（Scene P 反复出
        # 「技术部应发工资」这道必败题）的根因之一：出题器的观察面里**没有库的
        # 真实结构** —— 它只看到"工作区有一个 payroll.duckdb 文件"，既不知道有
        # 哪些表，也不知道 dept 的真实取值是「技术」不是「技术部」。原版出题器
        # 的观察面是**完整末态**（背包/附近方块），我们至少要给它 schema + 枚举值。
        self.db_path = str(db_path or "")
        self._db_obs_cache: str | None = None
        self._llm = llm
        self._dir = Path(state_dir) if state_dir else (STATE_DIR / self.scene_id)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.completed_tasks: list[str] = []
        self.failed_tasks: list[str] = []
        self._failed_why: dict[str, str] = {}
        # 背包（2026-11-18）：一格 = 一次会话，格内明细 = 这次会话跑出来的
        # 落点/闭合维度。与原版两本账**分开存**，绝不混账。
        self.slots: dict[str, dict[str, Any]] = {}
        self.batch: int = 0              # 归档批次（只作 chest 标签，不当档位判据）
        self.chests: list[dict[str, Any]] = []
        # 已封到第几条 chat exchange（per topic）。没有它，同一段 interrogative
        # 游程会在每次出题时被反复封格（实测：slots 涨、r_site 不降 → 每轮封一格）。
        # 记「封点」而不是删 exchange：exchange 是 append-only（route.ts:85）。
        self.sealed_marks: dict[str, int] = {}
        # chat 账本归属（原版 Phase2Data 是 per-session；自由学里 per-topic）。
        # 空 = 用 scene_id。只决定读哪个文件，不改变 chat 的任何语义。
        self.chat_topic: str = ""
        self._load()

    # ---------- 持久化（原版 :44-60 resume 读回两本账） ----------
    def _load(self) -> None:
        try:
            c = json.loads((self._dir / "completed_tasks.json").read_text("utf-8"))
            if isinstance(c, list):
                self.completed_tasks = [str(x) for x in c]
        except Exception:
            pass
        try:
            f = json.loads((self._dir / "failed_tasks.json").read_text("utf-8"))
            if isinstance(f, list):
                self.failed_tasks = [str(x) for x in f]
        except Exception:
            pass
        try:
            w = json.loads((self._dir / "failed_why.json").read_text("utf-8"))
            if isinstance(w, dict):
                self._failed_why = {str(k): str(v) for k, v in w.items()}
        except Exception:
            pass
        try:
            inv = json.loads((self._dir / "inventory.json").read_text("utf-8"))
            if isinstance(inv, dict):
                self.slots = {str(k): dict(v or {})
                              for k, v in (inv.get("slots") or {}).items()}
                self.batch = int(inv.get("batch") or 0)
                self.chests = list(inv.get("chests") or [])
                self.sealed_marks = {str(k): int(v or 0)
                                     for k, v in (inv.get("sealed") or {}).items()}
        except Exception:
            pass
        # 旧账迁移（2026-11-18）：covered_sites.json 是「落点全集 - 已证」时代的
        # 账本，形如 {site: [task,…]}。折成一格一格（task 当会话键），读一次就
        # 不再回写 —— 不丢历史，也不让两套口径并存。
        if not self.slots:
            try:
                cs = json.loads((self._dir / "covered_sites.json").read_text("utf-8"))
                for site, tasks in (cs.get("sites") or {}).items():
                    for t in (tasks or []):
                        key = str(t)[:80] or str(site)
                        slot = self.slots.setdefault(
                            key, {"sites": [], "dims": [], "rounds": 0,
                                  "at": ""})
                        if str(site) not in slot["sites"]:
                            slot["sites"].append(str(site))
                self.batch = int(cs.get("granularity") or 0)
                self.chests = list(cs.get("chests") or [])
            except Exception:
                pass

    def _save(self) -> None:
        (self._dir / "completed_tasks.json").write_text(
            json.dumps(self.completed_tasks, ensure_ascii=False, indent=2), "utf-8")
        (self._dir / "failed_tasks.json").write_text(
            json.dumps(self.failed_tasks, ensure_ascii=False, indent=2), "utf-8")
        (self._dir / "failed_why.json").write_text(
            json.dumps(self._failed_why, ensure_ascii=False, indent=2), "utf-8")
        (self._dir / "inventory.json").write_text(
            json.dumps({"slots": self.slots,
                        "batch": self.batch,
                        "chests": self.chests,
                        "sealed": self.sealed_marks},
                       ensure_ascii=False, indent=2), "utf-8")

    @property
    def progress(self) -> int:
        return len(self.completed_tasks)

    # ---------- 背包（一格 = 一次会话；明细 = 会话跑出来的东西） ----------
    @property
    def inventory_used(self) -> int:
        """占了几格（原版 `inventoryUsed`，curriculum.py:205）。一格 = 一个会话。"""
        return len(self.slots)

    @property
    def inventory_full(self) -> bool:
        """原版 :251 `if inventoryUsed >= 33:` —— 满了就去存箱。"""
        return self.inventory_used >= INV_FULL

    def slot_key(self, task: str) -> str:
        """一格的键 = 这次会话。MVE 的 session 本来就按 question 落
        （`session_state._cache_path`），所以题面就是会话键，不需要另造 id。"""
        return _norm(task)[:80] or f"slot{len(self.slots)}"

    def slot_content(self, task: str) -> dict[str, Any]:
        """读这次会话**跑出来的**东西 —— 原版 `event["inventory"]` 的读取口。

        数据来源：`maieutic_scaffold.load_session_events(scene, question)` 的
        append-only 事件流。不猜、不扫全集：
          sites  ← `dim_attempt` 的 code/result 里出现、且 `os.path.isfile` 实查
                   存在的 .py（有符号就记 `::符号`）
          dims   ← `dim_attempt` 里 `closed=True` 的 dim_id
          rounds ← 事件里最大的 round
        读不到事件（旧账 / 外部题）才退回只读题面；再没有就是空格子。
        """
        import time as _t
        sites: list[str] = []
        dims: list[str] = []
        rounds = 0
        try:
            import maieutic_scaffold as _ms
            evs = _ms.load_session_events(self.scene_id, task) or []
        except Exception:
            evs = []
        root = self._material_root() if self._is_grounded() else ""
        for e in evs:
            try:
                rounds = max(rounds, int(e.get("round") or 0))
            except Exception:
                pass
            if str(e.get("kind") or "") == "dim_attempt":
                did = str(e.get("dim_id") or "")
                if did and bool(e.get("closed")) and did not in dims:
                    dims.append(did)
                if root:
                    for s in parse_sites(str(e.get("code") or "") + "\n"
                                         + str(e.get("result") or ""), root):
                        if s not in sites:
                            sites.append(s)
        if root and not sites:
            # 题面里的落点。出题约束本来要求「点明要考的模块/函数/文件」
            # （_SYSTEM_GROUNDED 第 2 条），所以这一笔仍是实查，不是反查清单。
            sites = parse_sites(task, root)
        return {"q": str(task)[:80], "sites": sites, "dims": dims,
                "rounds": rounds,
                "at": _t.strftime("%Y-%m-%d %H:%M:%S")}

    def recall_session(self, task: str) -> dict[str, Any]:
        """会话跑完 → 装一格进背包（原版 :337-339 `completed_tasks.append`
        的同一步：**只有 success 才记**）。"""
        slot = self.slot_content(task)
        self.slots[self.slot_key(task)] = slot
        return slot

    def _locate(self, task: str) -> str:
        """题面 -> 落点（chat 游程判定用）。只读题面 + 实查，不查全集。"""
        if not self._is_grounded():
            return ""
        got = parse_sites(task, self._material_root())
        return max(got, key=len) if got else ""

    @property
    def needs_seed(self) -> bool:
        """还没有第一个个例：原版 manual 的「人给题」尚未发生。"""
        try:
            return self.pick_band() == "A"
        except Exception:
            return False

    def pick_band(self) -> str:
        """档位。说明书 = Voyager curriculum.py:244-291 的两条硬编码（顺序照抄）：
          "legacy"  非仓库材料 / 未迁移 → 原路径
          "A"       progress == 0         → "Mine 1 wood log"（要人给种子，:244）
          "C"       inventoryUsed >= 33   → "store items in chest"（存箱，:251）
          "B"       r_site >= K           → 存包：这段连续自述已成，该冻结交付
          "N"       其余                   → 正常出题

        B 的数据来源是 chat（`chat/route.ts:85` 的 `opusMode` 游程），不是新造的
        分类器 —— 2026-11-17 用户口径：「人或 agent 与 chat 交互，判断可推进性，
        从而分割 phase1/2/3 流转」。

        顺序照原版：progress==0 时背包必然是空的，原版一定先给
        `Mine 1 wood log`（:244 在 :251 之前），不会先存箱 —— 存箱的前提是
        包里已有货。旧实现把 C 放在 A 前面，是因为当时 C 的判据是
        `free == 0`（预设全集已证完），不是「背包满」。
        """
        if not self._is_grounded():
            return "legacy"
        if self.progress == 0:
            return "A"
        if self.inventory_full:
            return "C"
        if self.r_site >= CHAT_PASS_K:
            return "B"
        return "N"

    def _seal_site_batch(self) -> None:
        """档 B：chat 在同一落点上连续 K 轮 interrogative —— 这段已经成形，
        冻结成一次交付（装一格），游程清零。

        契约边界（一条都不许越）：chat 自己**不推进任何状态**
        （`chat/route.ts:85` 只 append exchange），"这段算做完了"是大循环
        读 `opusMode` 游程得出的判断。人看不到 chat，但它在内部决定
        「什么时候收这一格」—— 这就是自由学的节拍器。

        与档 C 的区别：C 是背包**满**（容量硬约束，:251），B 是这一段**成形**
        （内容判据）。两者都只归档，不换世界。
        """
        try:
            import phase2_chat as _pc
            lt = _pc.read_last_topic(self._dir) or {}
            topic = str(lt.get("topic") or "") or str(self.scene_id)
            if topic and topic not in self.slots:
                self.slots[topic] = self.slot_content(topic)
            # 记封点：这段游程已经交付过，下次不再从它身上数（append-only，
            # 不能删 exchange，只能记「数到这里为止」）。
            try:
                self.sealed_marks[topic] = len(
                    _pc.load_exchanges(self._dir, topic) or [])
            except Exception:
                pass
            _pc.record_last_topic(self._dir, topic, r_site=0)
            self._save()
            _log_band("档B", f"chat 连续 {CHAT_PASS_K} 轮同落点 → 存包一格"
                              f"（{self.inventory_used}/{INV_CAP}），游程归零")
        except Exception:
            pass

    def _escalate(self) -> bool:
        """存箱。原版 :251-279「inventory full -> store items in chest」：
        把这一批会话整包归档（chest），清空背包，继续跑。

        原版存完箱**还在同一个世界里**（不换维度、不换地图），所以这里不再
        「换粒度」；`batch` 只是归档批次的编号。永远返回 True：
        没有预设全集，就没有「挖完了」这个可证明的状态 —— 只有环境不可达
        （本机+联网都关）或出题器提不出新题时才停。
        """
        import time as _t
        self.chests.append({"batch": self.batch,
                            "used": len(self.slots),
                            "slots": dict(self.slots),
                            "at": _t.strftime("%Y-%m-%d %H:%M:%S")})
        self.slots = {}
        self.batch += 1
        self._save()
        return True

    def chat_exchanges(self) -> list[dict[str, Any]]:
        """读 chat 账本里的 `opusExchanges`（原版 Phase2Data 的同一列）。

        **只读**。写由 `phase2_chat.append_exchange` 负责 —— 两边不互踩，
        与「一个组件只有一个提交点」同一条规矩。
        """
        try:
            import phase2_chat as _pc
        except Exception:
            return []
        try:
            if self.chat_topic:
                return _pc.load_exchanges(self._dir, self.chat_topic)
            # 出题侧没有「当前题」，但下一题该选哪儿取决于**上一题挖透了没有**。
            # 原版这个指针就是 session 本身；MVE 显式存成 last_topic.json。
            tid = str(_pc.read_last_topic(self._dir).get("topic") or "")
            return _pc.load_exchanges(self._dir, tid) if tid else []
        except Exception:
            return []

    @property
    def r_site(self) -> int:
        """r_site = chat 的 mode 游程（2026-11-17 用户口径）。

        数据来源：**人与 agent 与 chat 的交互**（原版 `chat/route.ts:85` 只把
        `{studentMessage, opusMode, opusResponse}` 追加进 `opusExchanges`）。
        原版语义（`phase2-chat.ts:12-30`）：
          · interrogative = 问**自己的代码或推理**  → 在同一落点上深挖
          · direct        = 问**与本题无关的语法/库** → 离开当前问题

        所以「末尾连续 interrogative 且同一落点」的条数，就是「这个点挖了多深」。
        它不需要新造任何分类器 —— chat 的 schema 已经强制了二值。

        落点取**最近一条问句**反查的结果：换落点即断（不用外部指定 site，
        否则调用方可以「声明」自己在哪，又变成自报）。
        """
        exs = self.chat_exchanges()
        if not exs:
            return 0
        try:
            import phase2_chat as _pc
        except Exception:
            return 0
        # 只数「上次封点之后」的那一段：exchange 是 append-only（route.ts:85），
        # 封过的段不能删，只能记「数到这里为止」，否则同一段会被反复封格。
        try:
            _topic = str(_pc.read_last_topic(self._dir).get("topic") or "")
            _sealed = int(self.sealed_marks.get(_topic) or 0)
            if _sealed > 0:
                exs = exs[_sealed:]
                if not exs:
                    return 0
        except Exception:
            pass
        last = str(exs[-1].get("studentMessage") or "")
        site = self._locate(last)
        if site:
            return _pc.chat_streak(exs, locate=self._locate, site=site)
        return _pc.chat_streak(exs)

    def recall_site(self, task: str) -> bool:
        """兼容旧名：把这次会话装进背包（一格）。见 `recall_session`。"""
        return bool(self.recall_session(task))

    # ---------- 观察（原版 :150-224） ----------
    def local_enabled(self) -> bool:
        """本机取数能力这一组还开着吗（= 猫爪总开关 ∧ 本机组开关）。

        原版没有那么细的开关，对应物是「环境里有没有可用的 control primitive」：
        原版 observation 里能看到 chests / nearby_blocks，是因为 agent 确实能
        采集它们。MVE 里能力关掉后模型根本调不到本机原语，再围绕本机文件出题
        就是出一道**必然失败**的题（用户 2026-10-19 实测）。
        所以「能不能读本机」必须体现在环境观察里，而不是只体现在执行层。
        """
        try:
            import actions
            actions.restore_switches()          # 子进程：读面板拨过的开关
            return bool(actions.group_enabled(self.scene_id, "本机能力"))
        except Exception:
            return True                          # 读不到就按默认开（不假装关）

    def net_enabled(self) -> bool:
        """联网检索能力这组还开着吗（组名与 actions.register_net 一致）。

        为什么课程层要关心它：**知识型学习不依赖本机文件**。工作区空 +
        本机关，只要联网还开着，Voyager 就该继续围绕人问过的主题出更深的
        子问题（Maieutic 追问式拓展）—— 用户 2026-10-19 实测纠偏：
        「本机能力关了之后课程直接退出」是错的，不停（while True）才是原版语义；
        只有**所有取数手段都关了**才谈得上"没得学"。
        """
        try:
            import actions
            actions.restore_switches()
            return bool(actions.group_enabled(self.scene_id, "联网能力"))
        except Exception:
            return True

    def render_observation(self) -> dict[str, str]:
        import local_agent
        local_ok = self.local_enabled()
        net_ok = self.net_enabled()
        # 代码取证场景（2026-11-17 用户拍板「自由学就是 Voyager 那套：人不
        # 打扰、一句话、无单元无地图」）：观察面 = 仓库实查（nearby_blocks
        # 同构），状态 = 背包 + completed/failed 两本账。
        # 2026-11-18：书单分支已删 —— 材料入口只剩「背包」一个（见文件头
        # 背包段注释）。其它材料一律倒进仓库文件夹当材料，不再走单元/书单。
        if self._is_grounded():
            done_g = ("；".join(self.completed_tasks[-20:])
                      if self.completed_tasks else "None")
            failed_g = ("；".join(self.failed_tasks[-20:])
                        if self.failed_tasks else "None")
            wo_g = "；".join(f"{k[:40]}（{v[:80]}）"
                            for k, v in list(self._failed_why.items())[-8:])
            # 背包行照抄原版 :205 `Inventory ({inventory_used}/36): {inventory}`：
            # 只报「占了几格 + 格里是哪些会话」，**不报共多少** —— 世界方块
            # 总数原版从来没有出现在观察面上。
            _recent = list(self.slots.values())[-6:]
            inv_line = "; ".join(str(v.get("q") or "?")[:26]
                                 for v in _recent if isinstance(v, dict)) or "Empty"
            return {
                "biome": ("环境：一个**真实代码仓库**（原机）。没有 SQL 库、没有"
                          "教材书单；任务是**证明仓库的声明与它的真实行为不一致**。\n\n"),
                "nearby_blocks": repo_observation(self._material_root()) + "\n\n",
                "chests": ("整场只有一个目标：B ⊆ A，证明 B ≠ A。"
                           "不分关卡、不分章节、不报进度。\n"
                           f"已学会的技能（可复用配方）: {self._skill_count()} 条\n\n"),
                # 逐字照原版 :205，不追加任何内部状态：r_site（chat 游程）
                # 是档位判定用的账，原版 curriculum 看不到 opusMode 游程，
                # 观察面上也不该出现 —— 让出题器按「背包里有什么」出题。
                "inventory": (f"Inventory ({self.inventory_used}/{INV_CAP}): "
                              f"{inv_line}\n\n"),
                "completed_tasks": f"Completed tasks so far: {done_g}\n\n",
                "failed_tasks": (f"Failed tasks that are too hard: {failed_g}\n\n"
                                 + (f"为什么失败: {wo_g}\n\n" if wo_g else "")),
            }

        # kind=scene（场景自有 duckdb）→ 观察面给**实查的表结构 + 枚举值**，
        # 不给文件清单（那只会让模型把 .duckdb 当文本文件编）。这是原版
        # "nearby_blocks 能看到附近有什么"的同构：出题器必须看得见数据长什么样。
        if self.db_path and local_ok:
            db_obs = self._db_observation()
            if db_obs:
                files = ("这个场景的数据源是一个 SQL 库，实查结构如下"
                         "（表/列/取值全部真实，题里的字面量必须逐字来自「真实取值」）：\n"
                         + db_obs)
            else:
                files = ("这个场景的数据源是一个 SQL 库，但本次实查失败（库打不开？）。"
                         "不要编造表名或列名。")
        elif self.db_path and not local_ok:
            files = ("None（本机取数能力已被关掉，SQL 库读不了 —— 不要出需要查库的题）")
        else:
            inv = (local_agent.inventory(self.scene_dir, limit=12)
                   if (self.scene_dir and local_ok) else [])
            if inv:
                lines = []
                for r in inv:
                    if r.get("kind") == "dir":
                        lines.append(f"- {r['name']}/（目录）")
                        continue
                    lines.append(f"- {r['name']}（{r.get('size', 0)} 字节）")
                    if r.get("preview"):
                        pv = str(r["preview"]).replace("\n", " ")
                        lines.append(f"    开头内容（结构预览）：{pv[:300]}")
                files = "工作区文件:\n" + "\n".join(lines)
            elif not local_ok:
                files = ("None（本机能力已被关掉读不到文件，而且工作区里本来也没有"
                         "任何文件 —— 不要出需要读本机文件的题，也**绝对不要编造"
                         "文件名**）")
            else:
                files = ("None（这个场景的工作区里没有任何文件 —— **绝对不要编造"
                         "文件名**）")
        done = "；".join(self.completed_tasks[-20:]) if self.completed_tasks else "None"
        failed = "；".join(self.failed_tasks[-20:]) if self.failed_tasks else "None"
        wo = "；".join(f"{k[:40]}（{v[:80]}）" for k, v in list(self._failed_why.items())[-8:])
        return {
            # 原版 biome 只说「这是什么地形」（plains / forest），**不报路径**。
            # 我上一版写成 "数据源: 本机工作区 /root/mve/scene_data/D"，
            # 实测模型据此编出 room_data.txt（空目录里根本没这个文件），
            # 连出 3 道必失败的题。环境描述只给类别，不给可被脑补成文件名的路径。
            "biome": self._biome_text(local_ok, net_ok) + "\n\n",
            "nearby_blocks": f"{files}\n\n",
            "chests": (self._curriculum_text()
                       + f"已学会的技能（可复用配方）: {self._skill_count()} 条\n\n"),
            "completed_tasks": f"Completed tasks so far: {done}\n\n",
            "failed_tasks": (
                f"Failed tasks that are too hard: {failed}\n\n"
                + (f"为什么失败: {wo}\n\n" if wo else "")
                + ("（这些失败题多半是人导入的目标，是**里程碑**。按规则 5："
                   "围绕它出**前置的、现在就能做对**的子任务，不要原样重出。）\n\n"
                   if self.failed_tasks else "")),
        }

    def _curriculum_text(self) -> str:
        """课程进度投影。**2026-11-18 起恒为空**。

        原来这里注入书单单元/单元规划进度（`graph_books.render_text` /
        `graph_curriculum.render_text`），让出题器知道「该出哪一关」。那是
        「地图」，不是 Voyager：原版 curriculum 的观察面只有 nearby_blocks +
        inventory + 两本账（curriculum.py:194-209），没有关卡。
        材料入口收敛成「一个背包」后，这一段自然退场；保留方法名是为了
        调用方不用改（render_observation 的通用分支还在拼它）。
        """
        return ""

    def _biome_text(self, local_ok: bool, net_ok: bool = True) -> str:
        if not local_ok and not net_ok:
            return "数据源: 本机取数能力与联网能力**都已被关掉** —— 这个场景现在没有任何可用的取数手段。"
        if self.db_path:
            if local_ok:
                return ("数据源: 这个场景自己的 SQL 库（只读，连接由执行层负责）。"
                        "出题范围就是下面实查列出的表和列。")
            return "数据源: 场景 SQL 库存在，但本机取数能力已被关掉，读不了。"
        if not local_ok:
            return ("数据源: 本机取数能力已被关掉；联网检索可用。"
                    "只能出由联网检索回答的知识型问题，不要碰本机文件。")
        return "数据源: 本机工作区（应用工作区 + 场景目录），只读；联网检索也可用"

    def _db_observation(self) -> str:
        """实查场景库：表 → 行数 → 列 → 低基数字符串/布尔列的真实取值。

        只用 DuckDB 标准语句，不需要任何领域知识；打不开/查不动就返回空串
        （上层如实说"实查失败"，不编造）。缓存到实例 —— 一次进程里枚举值
        不会变，不必每轮出题都连库。
        """
        if getattr(self, "_db_obs_cache", None) is None:
            self._db_obs_cache = db_observation(self.db_path)
        return self._db_obs_cache

    def _skill_count(self) -> int:
        try:
            import skill_store
            return len([s for s in skill_store.all_skills()
                        if str(s.get("source_scene") or "") in ("", self.scene_id)])
        except Exception:
            return 0

    def render_human_message(self) -> str:
        obs = self.render_observation()
        # 原版按 warm_up 渐进披露；本机只有 4 项且都便宜，全放（等价 warm_up 全 0）
        return "".join(obs[k] for k in
                       ("biome", "nearby_blocks", "chests",
                        "completed_tasks", "failed_tasks"))

    # ---------- 出题（原版 :264-311） ----------
    def propose_next_task(self, *, max_retries: int = 5,
                          queue: list[str] | None = None
                          ) -> tuple[str, str]:
        """返回 (task, context)。队列里有人导入的题 → 直接取（原版 manual 的人给题）；
        队列空 → LLM 出题（原版 auto），解析失败重试 max_retries 次。

        环境三态（2026-10-19 用户纠偏后定）：
          · 有文件 → 原版模式：围绕工作区数据出题；
          · 没文件/本机关，但联网开 → **知识型拓展模式**：围绕 Completed tasks
            里人问过的主题出更深的子问题（Maieutic 追问式），由联网检索回答
            ——「不停」是原版 while True 的本义，本机关不等于没得学；
          · 本机 + 联网**都关** → 才是"没有可学的环境"，如实返回空。
        """
        # ---- 来源闸门（2026-11-18）：种子位只认「人给的题」 ----
        # 原版 `curriculum.py:322-330` 的 manual 是 input("Enter task: ") 人给；
        # auto 给的是常量 "Mine 1 wood log"（:264-266）—— 两条都不经过 LLM。
        # 所以「系统自己出的题」不能顶这个位，否则第 0 圈由系统自证。
        # 之前这里把 queue 里**任何**字符串都当人导入，而 scene_learn 会把
        # graph_books.next_task 的图谱/书单题塞进 queue → 系统题冒充种子。
        # 现在来源是**结构字段**，不靠调用方自觉：
        #   str                        → 人导入（向后兼容）
        #   {"question":…,"origin":…}  → origin 非空且不在 _SEED_ORIGINS 即非种子
        _human_q, _auto_q, _refused = "", "", 0
        for _it in (queue or []):
            if isinstance(_it, dict):
                q = str(_it.get("question") or "").strip()
                origin = str(_it.get("origin") or "").strip()
            else:
                q, origin = str(_it or "").strip(), ""
            if origin and origin not in _SEED_ORIGINS:
                # 系统出的题：不占种子位，但也不丢 —— 档 A 之外仍可用它出题
                _refused += 1
                if q and q not in self.completed_tasks and not _auto_q:
                    _auto_q = q
                continue
            # ⚠️ 这里只查 completed，**不查 failed**（2026-10-24 机制修复）：
            #   queue 是 scene_learn 从 coach_log 待学账里筛出来的人导入题
            #   （原版 manual 的人给题对应物）。原版 failed 账约束的是**自动
            #   课程**别再提太难的题（:330-345 skipping to next task）；
            #   人给的题不该被它上一次的失败记录永久拉黑——否则「导入→学→
            #   重试」闭环第 3 步永远走不通（实测工资题就是这样被屏蔽的）。
            #   失败题的重试节奏（沉底、防单轮死循环）由 scene_learn 管。
            if q and q not in self.completed_tasks and not _human_q:
                _human_q = q
        if _human_q:
            return _human_q, "人导入的待学问题（原版 manual 模式对应物）"
        # 原版 :264-291 在交给 LLM 之前先走硬编码分支（inventory 满 → 固定任务）。
        # MVE 的对应物就是这条：先看**有没有任何可用的取数手段**。
        # 2026-10-19 第一版这里把"工作区没文件"也当成没得学，用户实测纠偏：
        # Scene D（阿斯伯格）本机关、联网开着，点「练题」课程直接退 ——
        # 但知识型拓展根本不需要本机文件。
        # ---- 档 A：progress == 0 → **要种子，不自己发明第一个题** ----
        # 原版两条路都不让系统编第一题：manual 是人 input 给的（:322-330），
        # auto 给的是常量 "Mine 1 wood log"（:264-266）。人导入的题在上面的
        # queue 循环已被取走；到这里还没有，就说明**还没有种子**。
        if self.pick_band() == "A":
            return "", SEED_REQUIRED_MSG + (
                f"\n（另有 {_refused} 道**系统自己出的**题被挡在种子位之外："
                "原版 manual 的第一个题只能由人给，系统出的题不能自证第 0 圈。）"
                if _refused else "")
        if not self.local_enabled() and not self.net_enabled():
            return "", ("可学的抓手全被关掉了：本机取数能力和联网能力都处于关闭"
                        "状态。把至少一组能力打开（面板「能力开关」），或导入要学"
                        "的问题后再启动。")
        # ---- 档 C：背包满（inventoryUsed >= 33）→ 存箱 ----
        # 原版 :250-279 `if inventoryUsed >= 33: return "store items in chest"`。
        # 存完箱还在同一个世界里继续挖（不换粒度、不到顶）。没有预设全集，
        # 也就没有「这里学完了」这个可证明状态 —— 停只有两种：能力全关、
        # 或出题器连续提不出新题。
        if self.pick_band() == "C":
            _used = self.inventory_used
            self._escalate()
            _log_band("档C", f"背包满（{_used}/{INV_CAP}）→ 存箱第 "
                             f"{len(self.chests)} 批，背包清空继续")
        # ---- 档 B：chat 游程满 → 这一段成形，存包一格继续 ----
        # 2026-11-18：人看不到 chat（它是内部传感器），但它的 mode 游程在内部
        # 决定「什么时候收这一格」。之前这里只是返回一个标签，没有任何动作。
        if self.pick_band() == "B":
            self._seal_site_batch()
        if _auto_q:
            # 档 A 之外，系统出的题照用（原版 auto 的常态）；只是标注来源，
            # 让日志/观察面能分清「人给的」与「自己出的」。
            return _auto_q, "自主出题（非人导入）"
        return self.propose_next_ai_task(max_retries=max_retries)

    def _is_grounded(self) -> bool:
        """代码取证场景：数据源是一棵树，不是书单、也不是 SQL 库。

        判据用场景声明（verify_claim），不用目录猜 —— 声明比探测可靠。
        """
        try:
            import scenes as _sc_gr
            d = _sc_gr.get_scene(str(self.scene_id or "")) or {}
            return bool(d.get("verify_claim")) and bool(self.scene_dir)
        except Exception:
            return False

    def _material_root(self) -> str:
        """学习材料仓库根 —— 与机检器 verify_code_ref._repo_root 同一口径。

        机检认的 file 相对 data_source/mve（S 的仓库在 scene_data/S/repo，
        模块在 repo/mve）；实查清单必须用同一个根，否则出题器给的落点带
        `repo/` 前缀，学生照抄就被机检拒（2026-11-17 实测）。
        """
        try:
            from verify_code_ref import _repo_root
            return str(_repo_root(str(self.scene_id or "S")))
        except Exception:
            return str(self.scene_dir or "")

    def has_environment(self) -> bool:
        """工作区里有没有东西可探索（原版：nearby_blocks 非空才谈得上出任务）。"""
        if not self.scene_dir or not self.local_enabled():
            return False
        try:
            import local_agent
            return bool(local_agent.inventory(self.scene_dir, limit=1))
        except Exception:
            return True          # 探不动就按有环境（不因探测失败而假装没得学）

    def propose_next_ai_task(self, *, max_retries: int = 5,
                             _feedback: str = "") -> tuple[str, str]:
        """原版 :293-311：LLM 出 `Task:` 行；解析失败重试 5 次后 raise。

        多做的两件小事（都不改原版语义）：
          · 重复/没格式时，把原因拼进下一轮 user 消息（迭代提示，同原版 action
            agent 把 critic 的 critique 回灌下一次的做法）；
          · raise 时把最后原因带进消息 —— 原版只有一句 "Max retries reached"，
            线上看到它根本不知道是"模型没按格式"还是"全是重复"。
        """
        # 档 A 闸门（2026-11-17）：progress == 0 且没有种子 -> 不出题。
        # 放在这里而不是只放 propose_next_task，是因为 propose_for（turing 模式
        # 的出题路径）直接调本函数；只在上面拦，旁路照样能自己发明第一题。
        if self.pick_band() == "A":
            return "", SEED_REQUIRED_MSG

        if max_retries <= 0:
            raise RuntimeError(
                "Max retries reached, failed to propose ai task."
                + (f"（最后原因：{_feedback}）" if _feedback else ""))
        # 环境模式：没文件（或本机关）时切换到知识型拓展 —— 不然模型只能对着
        # "工作区"这个词编文件名（2026-10-19 实测连出 3 道 room_data.txt）。
        mode = ""
        if not self.has_environment():
            local_ok, net_ok = self.local_enabled(), self.net_enabled()
            mode = ("\n\n本次环境模式（重要）：\n"
                    f"- 本机取数能力：{'开' if local_ok else '关'}；"
                    f"联网检索能力：{'开' if net_ok else '关'}。\n"
                    "- 工作区里**没有任何文件**。所以**禁止**出「读/计算某个文件"
                    "里的数据」这类题 —— 那种文件名都是编出来的，必然失败。\n"
                    "- 你要出的是**知识型问题**：定义/区别/机制/标准/方法/常见"
                    "误解这类，执行层会用联网检索来回答。\n")
            if self.completed_tasks:
                mode += ("- 优先围绕上面 Completed tasks 里已经回答过的主题，出"
                         "**同一主题更深入的子问题**（更细的维度，比如从「是什么」"
                         "深入到「怎么判断/怎么做/有什么争议」），不要漂到不相关"
                         "的领域。\n")
        user = (self.render_human_message() + mode
                + "\n请按系统要求输出 Task: 与 Context: 两行。")
        # 可取证约束（2026-11-17）：verify_claim 场景的判据是「引用仓库
        # 里真实 file:line 并通过机检」。实测出题器爱造纯假设数据的计算题
        # （"三层图谱里列出所有 A→D 路径"）—— 这类题与仓库无关、天生
        # 没有可引用的代码，判据不可满足（977s 真跑 + critic 判做到，
        # 最后被机检拒收）。所以按声明把出题形态锁在「考真实代码行为」。
        try:
            import scenes as _sc_ca
            if bool((_sc_ca.get_scene(str(self.scene_id or "")) or {}).get(
                    "verify_claim")):
                user += ("\n\n【出题形态硬约束】本场景的答案必须能被"
                         "**仓库里真实的 file:line 引用**验证（判卷会机检"
                         "行号存在且内容匹配）。所以：\n"
                         "1. 题目必须考**仓库里某个模块的真实代码行为**"
                         "（某函数怎么判重、某分支返回什么、某字段谁写谁读、"
                         "某守卫在什么条件下生效）；\n"
                         "2. 题面里点明要考的模块/函数/文件，让答题者能"
                         "read_file 去核对；\n"
                         "3. **禁止**出只依赖题面假设数据的纯计算题/推演题"
                         "（例如「假设有节点 A、B，请枚举所有路径」）——"
                         "那种题没有代码可引，判据不可满足。")
        except Exception:
            pass
        # 已登记问题避重（2026-11-17）：见上——精确去重挡不住换措辞，
        # 把清单交给模型判，别加相似度阈值（会误杀合法新题）。
        try:
            import req_registry as _rr_cal
            _seen = [str((x or {}).get("question") or "")
                     for x in (_rr_cal.list_reqs(str(self.scene_id or ""))
                               or [])]
            _seen = [q for q in _seen if q]
            # 同一问句重复登记（里程碑重试/多次派发）只占一个名额：
            # 实测 23 次派发同一题后，12 个名额被同名刷满，清单形同虚设。
            _dd, _seen = set(), []
            for _q in _seen:
                if _q not in _dd:
                    _dd.add(_q)
                    _seen.append(_q)
            _seen = _seen[-12:]
            if _seen:
                user += ("\n\n本场景已登记过这些问题（含正在学的，别重复）：\n"
                         + "\n".join("- " + q[:140] for q in _seen)
                         + "\n\n**不要**再出考同一个点、同一问法的题——换个"
                           "措辞不算新题；要出就出上面这些题没覆盖的部分。")
        except Exception:
            pass
        # 档 A 不在这里：progress == 0 时 propose_next_task 已按原版口径
        # 返回「要种子」，不再让模型编一道最小题。
        if _feedback:
            user += f"\n\n上一次不行：{_feedback}。请务必换一个**不同**的任务。"
        _grounded_mode = self._is_grounded()
        msgs = [{"role": "system",
                 "content": (_SYSTEM_GROUNDED if _grounded_mode else _SYSTEM)},
                {"role": "user", "content": user}]
        try:
            if self._llm is not None:
                text = str(self._llm(msgs) or "")
            else:
                from voyager_vlml.llm import chat
                text = str(chat(msgs, temperature=0.8) or "")
        except Exception:
            # 模型调用失败 = 这一轮出不了题（原版是抛异常让上层处理）；
            # 这里返回空让循环如实退出，别把异常变成"学到一半"的假象。
            return "", ""
        task, context = self.parse_ai_message(text)
        if task and not _too_similar(
                task, self.completed_tasks + self.failed_tasks):
            return task, context
        why = ("模型输出里没有 `Task:` 行（没按格式）" if not task
               else f"提的题与已完成/失败过的重复：{task[:40]}")
        # 解析失败 / 重复 → 重试（原版 :304-311 的递归重试）
        return self.propose_next_ai_task(max_retries=max_retries - 1,
                                         _feedback=why)

    # ---------- QA 攻略层（原版 curriculum.py:387-437 run_qa / get_task_context） ----------
    _QA_SYSTEM = """你是执行前的攻略助手（原版 Voyager qa_llm 的同构）。
任务马上要交给一个只能查本地 SQL 库 / 读本地文件 / 联网检索的助手去执行。
你的职责：给 **几条简短的执行攻略（tips）**，帮它一次做对 —— 不是替它做，也不给完整 SQL。

硬规则（2026-10-24 实测事故后钉死）：
1. 只能引用下面「环境实查」里真实出现的表名、列名、取值；实查里没有的一律不许提；
2. 数据源里不存在的业务参数（费率、汇率、税率、每单位价格、折算规则……）**绝对不许出现**
   —— 之前有任务被建议"计算加班费(如有)"后，模型编造了"每小时 150 元"还入库成了技能；
3. 主动提醒常见口径陷阱（如果与本任务相关）：INNER JOIN 会把匹配不上的行静默丢掉、
   对「主表 + 每行都带同一金额」的表直接 SUM 会把金额乘出行数倍、NULL 会让 SUM/AVG 静默少算；
4. 每条攻略一句话，总共 3~5 条，不给完整 SQL、不算具体数字。
"""

    def get_task_context(self, task: str) -> str:
        """原版 voyager.py:419-424
        `rollout(task=next_task, context=get_task_context(next_task))` 的同构：
        执行前先让攻略 LLM 出 tips，拼进 rollout 的 context。

        原版 qa_cache 用向量相似（阈值 0.05）做近似去重；这里课程账本里的任务
        本来就是精确字符串（completed/failed 两本账都以原文为键），归一化后
        **精确去重**即可，不引入向量依赖 —— 与原版的这一差异是刻意的。
        LLM 挂 / 没有数据源 → 返回空串，调用方照常跑（攻略层挂掉不能拦住学习）。
        """
        task = str(task or "").strip()
        if not task:
            return ""
        cache_path = self._dir / "qa_cache.json"
        try:
            cache = json.loads(cache_path.read_text("utf-8"))
            if not isinstance(cache, dict):
                cache = {}
        except Exception:
            cache = {}
        key = _norm(task)
        if key in cache:
            return str(cache.get(key) or "")
        # 攻略的地面真话 = 环境实查清单（同出题器/critic 三方共用的那一份）。
        ground = ""
        if self.db_path:
            ground = self._db_observation()
            if not ground:
                return ""          # 库实查失败 → 没有地面真话就不出攻略（防编造）
        user = ("[环境实查（数据源真实的表/列/取值）]：\n" + (ground or "（无 SQL 库；"
                "任务可能由联网检索回答，攻略只谈方法不谈具体数据）")
                + f"\n\n[即将执行的任务]：{task}\n\n请输出 3~5 条攻略。")
        msgs = [{"role": "system", "content": self._QA_SYSTEM},
                {"role": "user", "content": user}]
        try:
            if self._llm is not None:
                text = str(self._llm(msgs) or "")
            else:
                from voyager_vlml.llm import chat
                text = str(chat(msgs, temperature=0.0) or "")
        except Exception:
            return ""              # 攻略层失败如实返回空，不假装有攻略
        tips = text.strip()
        if not tips:
            return ""
        ctx = "执行前攻略（qa agent 的 tips，仅供参考；攻略里提到的表/取值以环境实查为准）：\n" + tips
        # ⚠️ 原子写（2026-10-24 教训：并发/中断下的非原子写曾把 scenes.json 截断）。
        try:
            cache[key] = ctx
            tmp = cache_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(cache_path)
        except Exception:
            pass                   # 缓存写失败不影响本次（下次会重新生成）
        return ctx

    @staticmethod
    def parse_ai_message(message: str) -> tuple[str, str]:
        """原版 :312-322：只认 `Task:` 行（原版还可见 `.replace(".", "")`）。"""
        task, context = "", ""
        for line in str(message or "").split("\n"):
            s = line.strip()
            if s.startswith("Task:"):
                task = s[5:].strip()
            elif s.startswith("Context:"):
                context = s[8:].strip()
        return task, context

    # ---------- 记账（原版 :330-365） ----------
    def update_exploration_progress(self, info: dict[str, Any]) -> None:
        task = str(info.get("task") or "").strip()
        if not task:
            return
        if info.get("success"):
            if task not in self.completed_tasks:
                self.completed_tasks.append(task)
            # 落点记进背包（P1）：证成了才算，题面反查不到落点就不记账。
            self.recall_site(task)
        else:
            if task not in self.failed_tasks:
                self.failed_tasks.append(task)
            why = str(info.get("critique") or info.get("why") or "")
            if why:
                self._failed_why[task] = why[:200]
        self.clean_up_tasks()
        self._save()

    def clean_up_tasks(self) -> None:
        """原版 :347-365：completed 去重（保序），并把已完成的从 failed 里删掉。"""
        seen: list[str] = []
        for t in self.completed_tasks:
            if t not in seen:
                seen.append(t)
        self.completed_tasks = seen
        self.failed_tasks = [t for t in self.failed_tasks
                            if t not in self.completed_tasks]
        for t in list(self._failed_why):
            if t not in self.failed_tasks:
                self._failed_why.pop(t, None)

    def reset(self) -> None:
        self.completed_tasks, self.failed_tasks, self._failed_why = [], [], {}
        self._save()


# ---------------------------------------------------------------------------
# 便捷入口（scene_learn 用；也保留给脚本/测试）
# ---------------------------------------------------------------------------
def agent_for(scene_id: str, *, llm=None) -> CurriculumAgent:
    import scene_profile
    prof = scene_profile.profile(scene_id)
    return CurriculumAgent(scene_id,
                           scene_dir=str(prof.get("scene_dir") or ""),
                           db_path=str(prof.get("db_path") or ""),
                           llm=llm)


def propose_for(scene_id: str, *, exclude: Iterable[str] = (), llm=None,
                last_why: str = "") -> dict[str, Any]:
    """一层薄适配（老的调用口径）：返回 {"question","basis","why"}。

    真实机制在 CurriculumAgent 里（两本账 + Task: 解析 + 5 次重试）；
    这里只是把 exclude 当"额外失败项"临时喂进去，不落盘。
    """
    a = agent_for(scene_id, llm=llm)
    merged = list(a.failed_tasks)
    for x in exclude:
        if x and x not in merged:
            merged.append(str(x))
    if last_why and merged:
        a._failed_why[merged[-1]] = str(last_why)[:200]
    try:
        task, context = a.propose_next_ai_task(max_retries=5)
    except Exception as e:
        return {"question": "", "basis": [], "why": f"{type(e).__name__}: {e}"}
    if not task:
        return {"question": "", "basis": [],
                "why": str(context or ("自动课程提不出下一题"
                                       "（LLM 不可用 / 连续 5 次解析失败 / 全都重复）"))}
    return {"question": task, "basis": [], "why": f"自主出题（{context or '无 context'}）"}
