# 外部 task 闭环：泛化线与 VLML 线的同构与差异

> 基线：`rebuild/fail-driven` @ `ac45d72`（场景化 / maieutic / 远端 MCP 网关**之前**的 MVE：
> VLML MCP 数据面 + 里程碑课程 + SQL 技能库 + 导入与曲线）。
> 目标验收：**接 rib 数据源，人导入的 task 一开始 fail；学习一轮后重试同一 task 成功。**

---

## 零 结论

1. **算工资与 VLML 世界同构**，同构轴是「问题 → 拆子目标 → 逐条取数取证 → 存配方 → 复用」，
   不是业务领域。所以能复制流程。
2. **但两者不同**：VLML 线的课程是**自发的**（没有外部 task，学到哪算哪）；泛化线的课程由
   **外部 task 的失败信号驱动**（task 是目标，课程是手段）。
3. **数据源不是可选项**：学习方向（VLML 的 MCP 工具集）不变，换的是数据源与阶段状态。
   不带数据源时 task 必 fail、且**必然学不动**——因为「取到数」是唯一的成功判据。
   所以泛化 = **同一套学习方向 + 换一个数据源**，不是"去掉数据源"。

---

## 一 同构轴（为什么算工资能套这条流程）

| 轴 | VLML 世界（rib.gg） | 算工资世界（2000 人 Excel） |
| --- | --- | --- |
| 问题 | "Cloud9 在 Corrode 上首血率多少" | "算这个月的工资" |
| 子目标 | 里程碑树节点（首血/经济/回合结构…） | 口径维度（架构项、社保基数、个税档、考勤…） |
| 取证 | `query_sql` 取到行（rows>0 且值非空） | 表里查得到、且重算与样例一致 |
| 成功判据 | **真取到数**（不比对标准答案） | 同左（再加对账，泛化线增量） |
| 配方 | 跑通的 SQL = 一条技能 | 跑通的 SQL = 一条技能（一种架构一条） |
| 复用 | 同题/变体题命中 → 重放 SQL | 下月新表 → 重放 SQL + 换参数 |
| 失败 | 取不到数 = 这条子目标没学会，换更简单的 | 同左 |

**不同点只有一个，但它决定了架构**：VLML 线的"问题"是自己出的（课程即目标）；泛化线的"问题"
是**外面给的**（目标是给定的，课程是围绕它自己拆的）。

---

## 二 两条流程

### 2.1 VLML 线（现状，`ac45d72` 已跑通）

```
① 开始学习（自规划 VLML 相关内容）
     curriculum.propose_next_task()  → 里程碑树选题（seed → 树干，已完成排除）
② 沉淀技能进库
     critic 判"真取到数" → skill_store.add(topic_id, ...)（code = SQL）
③ 人导入（导入技能库相关的变体问题）
     /api/coach → infer_topic → 检索技能 → 重放
④ Voyager 复用技能答出问题
⑤ 能力曲线上升（结束）
```

### 2.2 泛化线（目标）

```
① 人导入 task ← **新**：外部目标入口（不是自选题）
     → 先用已学技能答（消费态）；fail 就把 task 记账
     → 拆子目标（本题相关的图谱节点）→ 写进课程队列
② 自规划学习「这个 task fail 的课程内容」← **新**：课题由失败信号决定
③ 沉淀技能进库（同 2.1 ②，不变）
④ 重试 task.fail ← **新**：显式重试同一 task，成功则从失败账移除
⑤ Voyager 复用技能 → 任务成功
⑥ 人导入变体问题 → 复用答出（同 2.1 ③④）
⑦ 能力曲线上升（侧重 ① 的 task 是否被解决）
```

差异**只在 ①②④ 三处**，③⑤⑥⑦ 完全复用。这也是为什么"泛化线基本可以复制这个流程下来改动"。

---

## 三 数据源争议：三条判定

| 命题 | 判定 | 依据 |
| --- | --- | --- |
| 学习方向不变（VLML 的那几个 MCP） | **成立** | 8 个工具是"取数取证"的形态，与业务域无关；换域后形态不变 |
| 同一套 MCP 可用于别的数据源与阶段状态 | **成立，但靠换库实现** | `vlml_env.ACTIVE_DB_PATH` 是进程级全局；`db_switch.py` 只把 VLML 指向另一个 duckdb。**换的是一条路径，不是一层抽象**——所以 `tasks.py` 的 `SERIES='2843069'` 这类硬编码必须跟着换（`db_switch.py:6-12` 自己就写了「换库不换问题」这个坑） |
| 不带数据源导入 task，fail 后开始学 | **不成立** | 成功判据是「真取到数」（`voyager_vlml/loop.py:472-476`）；没有数据源 → 每一轮都取不到 → 学习循环空转，且**学不到任何技能**（`skill_store.add` 只在 success 后调） |

⇒ **结论：必须带数据源或本地库。** 这不算妥协，因为"取到数才算学会"正是这套学习区别于
"让模型背答案"的地基；去掉它，学到的就是记忆而不是技能。

---

## 四 原版取证：失败信号驱动课程，是原版机制不是新发明

| 机制 | 原版位置 | 说明 |
| --- | --- | --- |
| prompt 里带失败清单 | `voyager_original/voyager/prompts/curriculum.txt:22-23` | `Completed tasks so far:` / `Failed tasks that are too hard:` 两行直接进 prompt |
| 失败清单的拼装 | `agents/curriculum.py:181-208` | `render_observation` 把 `completed_tasks` / `failed_tasks` 拼进 human message |
| 失败的处置 | `:332-347` `update_exploration_progress` | fail → `failed_tasks.append(task)`，打印 "Skipping to next task" |
| 完成后从失败账剔除 | `:349-370` `clean_up_tasks` | 精确 `not in` 去重；completed 的从 failed 删掉 |
| 降难度规则 | `curriculum.txt:29` | "The next task should not be too hard since I may not have ... learned enough skills" |
| 外部 task 入口 | `voyager.py:403-407` `inference(task=)` → `decompose_task`；`:406-407` 给了 `sub_goals` 就不拆 | 见 `maieutic-外包课程规划.md` §1.3 |

**MVE 侧现状（`ac45d72`）缺的正是这三条**：`VLMLCurriculumAgent.propose_next_task()`
（`voyager_vlml/agents/curriculum.py:228-270`）只走 `graph.select_next()`，**不吃失败清单**；
`update_exploration_progress`（`:283-`）只写节点状态，不维护"外部 task 的失败账"。

---

## 五 本次改造清单（4 项，全部落在基线上，不引入新概念）

| # | 改哪 | 做什么 | 与谁同构 |
| --- | --- | --- | --- |
| 1 | 新模块 `mve/task_intake.py` | 外部 task 账本 + 子目标拆分（用现成 `graph_topics.match_facts`）+ 重试入口 | 原版 `failed_tasks.json` + `decompose_task` |
| 2 | `voyager_vlml/agents/curriculum.py:228` `propose_next_task` | 选中之前先看"有未解决的外部 task 吗" → 有就从它的 pending 子目标里选 | `curriculum.py:181-208`（把 failed 放进决策） |
| 3 | `:283` `update_exploration_progress` | 成功/失败回写 task 账（子目标完成度；全完成 → `ready_for_retry`） | `:332-370`（fail 记账 + 完成剔除） |
| 4 | CLI `python mve/task_intake.py ask/retry/list` | 人导入 task → 答一次（fail 记账）→ 重试 | `voyager.py:403` `inference(task=)` |

不做的（保持基线干净）：maieutic 维度、场景隔离、猫爪动作组、远端 MCP 网关、联网取证。

---

## 六 验收

### 6.1 离线（结构与记账，已通过）

```bash
python mve/test_task_intake.py
```

5 条断言，覆盖：`ask` 失败 → 记账 + 拆课；课程规划返回 `reason=external_task`；
子目标全完成 → `ready_for_retry` → `retry` 成功且 `reused_skills` 非空；
真空成功（只有轨迹没事实 / 只有事实没轨迹）被挡住；**无数据源时 task 永远 pending**。

### 6.2 联机（真取数与复用，需接 rib）

```bash
# 1) 导入 task（先答一次；取不到数 → 记 pending + 拆子目标）
python mve/task_intake.py ask "Cloud9 在 Corrode 上首血率多少"
python mve/task_intake.py list          # 应看到 1 条 pending，subtopics 非空
python mve/task_intake.py sub <task_id> # 看拆出的课程（前置在前）
# 2) 学（每轮围绕 pending 子目标；跑通即存一条 SQL 技能）
python voyager_vlml/loop.py --rounds 6
python mve/task_intake.py list          # done_subtopics 在长，ready_for_retry 变 true
# 3) 重试同一 task（验收点：用的是学会的技能）
python mve/task_intake.py retry <task_id>
```

通过判据：`retry` 输出 `ok=true` 且 `reused_skills` 非空（说明用的是学会的配方，不是现编）。
