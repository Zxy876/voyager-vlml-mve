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

---

## 七 首次联机实跑（2026-10-23，rib 库）

### 7.1 已成立的（真跑，不是设计）

| 环节 | 实测 |
| --- | --- |
| 导入即失败 | `ask` → `ok=false`，`reason="技能库里没有与它相关的配方（还没学过）"`，直接记账不烧调用 |
| 拆子目标 | `subtopics=["vlml_fb_conversion","vlml_first_blood"]`（先前置后目标） |
| 课程围着失败信号 | 连跑 3 轮全是 `reason=external_task`，不是原来的里程碑课程序 |
| 学会一条 | `done_subtopics` 从 `[]` → `["vlml_first_blood"]`，`skill_store` 从 0 → 1 条 |
| 失败后换子目标 | 加了 `attempts → difficulty → depth` 排序，失败过的子目标排到后面 |

### 7.2 卡住的（诚实）

1. **`retry` 不可能成功**：目标子目标 `vlml_fb_conversion` 依赖 `agg_first_blood_stats`，
   实查该表 **21 列都在、行数 = 0**（`SELECT COUNT(*)` → 0）。空表 → 每轮都取不到数 →
   永远学不完 → `ready_for_retry` 永不置真。
   这不是代码 bug，是**这个库里首血数据是空的**——正好印证「数据源必须有，且必须有数据」。
2. **学到的配方口径是错的**：`vlml_first_blood` 那条成功配方写的是
   `WHERE map_name='Split' AND round_number=1`（把"第一回合"当"首血"），取自
   `ext_player_round_stats`（2850 行）→ 取到数 → 判成功入库。
   **「跑通 ≠ 口径对」原样复现**：判据只看"取到数"，看不出口径错了。

### 7.3 两条下一刀（按性价比排序）

| # | 改什么 | 为什么 |
| --- | --- | --- |
| 1 | 学习前预检"表有没有行"：空表节点直接标 `db_unreachable` 并跳过 | 现在空表要烧 3 轮 SQL + 3 次 LLM，还会把它写进 failed 账把课程堵死 |
| 2 | 成功判据加一道"口径检验"（`intent-diff`：SQL 的过滤/聚合 vs 题干与节点 skills 的承诺） | 否则"第一回合"会被当成"首血"永久入库，下次复用即错 |

### 7.4 顺手修掉的一个真 bug

`hints`（节点 skills 里的取数要求，如"`agg_first_blood_stats` 的粒度是 round_id / 字段
`fb_team` 与 `fb_team_won`"）以前**只传给 critic、没有进写码 prompt**
（`voyager_vlml/agents/action.py` 的 `solve` 收了 `hints` 却只在 `check_task_success` 用它）。
后果是模型全程瞎猜列名：连跑 3 轮都在 `ext_player_round_stats` 上写 `event_type` /
`round_number=1`，全 `empty` 失败。
修法：`write_sql(..., hints=)` 把要求渲染进 system（每一轮迭代都带着）。
修完 `vlml_first_blood` 立刻转正，进度 0 → 1。

### 7.5 第二刀已落：action 主循环换成原版 `reset/step/rollout` 的移植

§7.2 第 2 条（错口径入库）查下去，根因不在重试次数，在**重试时模型看不见自己取回了什么**：

- 原版每步回灌**整个观察**（`voyager_original/voyager/voyager.py:262-268`
  `render_human_message(events=events)`：背包、位置、`Execution error:` 全给）；
- 这边原来只回灌「取到几行 + 报错串」→ **SQL 报错能修，取错列但没报错就修不了**。

第一版修法是往自造的迭代 prompt 里塞结果（`last_result` 参数）——**那是加工发明**。
按用户要求撤回，改成逐段移植原版：`reset / step / rollout / render_system_message /
render_human_message / process_ai_message`，只把 JS 换成 SQL、`env.step` 换成 `run_sql`、
背包位置换成行列；`hints` 不再单开通道，它就是原版的 `context`；上限 3→4 对齐
`voyager.py:30`。结构由 `mve/test_action_loop.py` 6 条断言钉住（含"解析失败不重建消息"）。

实跑差别（rib，同一题、不给技能）：自造版三轮都死在 0 行的 `agg_first_blood_stats`；
移植版**第 2 轮**自己摸到 `ext_player_round_stats.first_kill`，取到 106 行。

完整对照（含 14 行逐步表、重试层次、解析失败计数、对"无建模表"验收线的影响）见
`docs/主循环对照-自修正与重试.md`。

---

## 八 第一刀已落：学不动的子目标按原版语义「跳过」（2026-10-23）

### 8.1 原版是怎么处理"太难"的（取证）

不是"跳过某个节点"——原版没有节点。它只有两个清单 + 一句话：

| 位置 | 机制 |
|---|---|
| `agents/curriculum.py:332-347` | `update_exploration_progress(info)`：成功 → `completed_tasks.append`；失败 → `failed_tasks.append` **+ 打印 "Skipping to next task"** |
| `agents/curriculum.py:349-370` | `clean_up_tasks`：去重、把已完成的从 `failed_tasks` 里剔除（**只在变成功时剔除**） |
| `prompts/curriculum.txt:22-23` | 每轮把两个清单塞进 curriculum 的输入：`Completed tasks so far:` / `Failed tasks that are too hard:` |
| `prompts/curriculum.txt:29` | `The next task should not be too hard since I may not have the necessary resources or have learned enough skills` |
| `voyager.py:326-385` | `learn()` 的 `while True`：**失败不阻塞**，记完账就进下一轮；从不等所有任务成功 |

一句话：**原版的"跳过"= 记一笔"太难"账 + 下一轮换个别的，"完成"不是继续的条件。**

### 8.2 MVE 原来的偏差

`ready_for_retry` 用的是「`subtopics` 全都在 `done_subtopics` 里」。而一条学不动的
子目标（练够 `MAX_ATTEMPTS_PER_TOPIC=3` 次 → `stale`）永远不会进 `done` →
`pending_subtopics` 已经把它排除了，`ready_for_retry` 却永远是 false。
实测 `vlml_fb_conversion` 就卡在这儿：`pending=[]`，`ready_for_retry=false`。

### 8.3 改法（三处，全部是把原版的账搬到图谱上）

| 改了什么 | 对应原版 |
|---|---|
| `_ready_now(row)` = `not pending_subtopics(row)` —— 门槛是"没有还能学的了"，不是"全做完" | `learn()` 从不等全部成功；失败即 "Skipping to next task" |
| `skipped_subtopics(row)`：**反查** `mastery.stale`，不另存账；`next_focus` 的 `why` 带上"已放弃 N 条" | `failed_tasks` + curriculum 输入里的 `Failed tasks that are too hard:` |
| `note_subtopic_result(tid, success)`：无论成败都回写 | `:372` 每轮无条件调 `update_exploration_progress(info)` |
| `ready_for_retry` 改成**读时现算** | 状态反查，不重复记账（两本账必然对不上） |
| `next_focus` 改用 `graph.curriculum_key`（从 `select_next` 抽出的同一个键），删掉上一轮自造的 `attempts/difficulty` 排序 | 「下一题」只有一套顺序，两条线不能各排一套 |

### 8.4 顺手修掉的复用断点（不修 retry 永远"还没学过"）

技能是按「学它的那个节点」存的，`skill_store.retrieve` 只有 `topic` **完全相等**才给
同题分；跨题靠词面相似度过 `CROSS_TOPIC_MIN_SIM=0.12` 的门槛。

实测这条 task：`sim=0.021` → 传空 topic、传 `vlml_fb_conversion` 都召回不到，
只有传 `vlml_first_blood` 才拿到。而账本里 `topic_id` 是空串 → retry 直接判"还没学过"。

改：`ask` 先 `resolve_topic` 再答，把归题结果写进账本；`_answer_once` 按
「归题结果 + 该 task 的子目标」逐个检索再合并（子目标就是"这题需要哪些知识点"）。

### 8.5 服务器实测（真实账本 `t745700`，rib）

```
$ python mve/task_intake.py sub t745700
  subtopics       : [vlml_fb_conversion, vlml_first_blood]
  done            : [vlml_first_blood]
  skipped         : [vlml_fb_conversion]     ← 0 行表上练不动，反查 stale
  pending         : []                       ← 没有能学的了
  ready_for_retry : true                     ← 以前恒 false

$ python mve/task_intake.py retry t745700
  ok=true  tries=3  reused_skills=[vlml_first_blood::vlml_first_blood]
  [第 1/3 次] 失败 · query_sql 取到 1 行
  [第 2/3 次] 成功 · query_sql 取到 1 行
```

它自己写出来的（第 2 轮，复用那条学过的配方后改的）：

```sql
SELECT COUNT(CASE WHEN winning_team_name = 'Karmine Corp' THEN 1 END) AS wins,
       COUNT(*) AS total_first_blood_rounds
FROM ext_player_round_stats
JOIN rounds ON ext_player_round_stats.game_id = rounds.game_id
WHERE ext_player_round_stats.team_name = 'Karmine Corp'
  AND ext_player_round_stats.map_name = 'Sunset'
  AND ext_player_round_stats.first_kill = TRUE
```

`9 / 13` = **69.2%** 是这道题的**真值**（正确 JOIN：`game_id` + `round_number`）。

⚠️ 但这条 SQL **没有**算出 9/13：它 `JOIN rounds ON ... game_id` 只连了 `game_id`，
`rounds` 一行一回合 → 13 个回合被乘成 286 行，实际返回 **169/286 = 59.1%**。
当时写成"与手工核算一致"是**归因错误** —— 手工核算的是正确的 9/13，模型算的是 169/286。
详细纠正见 §9.4。

**验收线至此通过**：人导入 task → 初始 fail（技能库空）→ 拆课 → 学 → 学不动的
子目标被跳过而不是卡死 → retry 复用技能成功。

### 8.6 仍然没解决的（诚实）

1. **判据只认"取到数"，不认"答案是结论"**。上面 retry 返回的 `answer` 是那条 SQL，
   `facts: 0` —— 库里取到了 13 行，但没人把它变成"69.2%"这句话。口径这一维还是靠人看。
2. `vlml_fb_conversion` 被跳过后**不会自动复活**：`mastery.stale` 一旦置位就永久排除，
   除非调 `unstale()`（图表节点只有几十个，全 stale 后会走"生成新题"）。
   原版也没复活机制（`clean_up_tasks` 只在它变成功时剔除），这一点算同构，但要记着。

---

## 九 第二刀已落：成功判据加口径检验（原版 critic 的 LLM 判决移植）

### 9.1 原版哪里在做这件事（取证）

原版的"判成功"从来不是数行数，是**问模型**：

| 位置 | 机制 |
|---|---|
| `agents/critic.py:26-28` | system = `prompts/critic.txt` |
| `agents/critic.py:30-80` | human = 观察（末态：biome/背包/位置）+ `Task:` + `Context:` |
| `agents/critic.py:94-117` | `ai_check_task_success`：LLM 回 JSON `{reasoning, success, critique}`，解析不了重试 `max_retries=5` |
| `agents/critic.py:119-141` | `check_task_success`：组装两条消息，`mode=auto` 走 LLM |
| `voyager.py:232-238` | **每一步**判一次，成功即停、critique 回灌下一轮 |
| `prompts/critic.txt:3` | "Exceeding the task requirements is also considered a success while failing to meet them requires you to provide critique" |

一句话：**原版判的是「任务要求有没有被满足」，不是「执行有没有报错」。**
MVE 旧判据（`至少一次 ok` ∧ `rows≥1` ∧ `有真实值`）只覆盖"执行没报错"。

### 9.2 改法：两级判据，第一级保留、第二级移植

| 级 | 判什么 | 来源 |
|---|---|---|
| 第一级（保留） | 真的从库里取到数：ok 调用 + 行数达标 + 结果非真空 | SQL 特有补丁。原版没有这一级，因为 Minecraft 的观察不会出现"空表返回 1 行 (NULL,0)"这种骗局 |
| 第二级（新增） | 任务要求满足了没有（口径） | 原版 `ai_check_task_success` 整段移植 |

落点：`voyager_vlml/agents/critic.py`（`judge()` = 两级；`check_task_success()` 是兼容壳）、
`voyager_vlml/prompts/critic.txt`（原版提示词的翻译 + 字段映射：背包→结果行列、
代码→SQL；JSON 契约一字不改：`{reasoning, success, critique}`）。

第二级**只给两样新东西**：
- `SQL:` 字段（原版靠末态世界状态看"我做成了什么"，SQL 世界的末态只有几行数字，
  过滤了哪个列只能从 SQL 本身看出来 —— 字段映射，不是新规则）；
- `reasoning` 直接被当**答案句**（原版 JSON 里 reasoning 本来写的就是结论，
  例子里 "2 oak + 2 spruce add up to 4"）。`mve/task_intake.py` 的 `answer`
  优先取它，取不到才退回 SQL —— 这修掉了 §8.6 第 1 条：`answer` 以前恒为 SQL、`facts=0`。

第二级跑不成（接口抖动 / JSON 重试用尽）时的行为：**接口抖动** → 退回第一级 + critique
里明写"口径检验没跑成"（不把整轮判死）；**JSON 重试用尽** → 判失败（**同原版**返回 False）。

代价：每一步多一次 LLM 调用（**原版 `voyager.py:232-238` 就是这样，每一步都判**）。
要关掉时用环境变量 `MVE_CRITIC_LLM=0`（测试与离线冒烟用），默认开。

### 9.3 实跑取证（2026-10-23，服务器 `/root/mve-rebuild`，rib，`mve/_probe_critic.py`）

四条固定 SQL 直接喂 critic，不经过出题/写码模型（排除模型抖动）：

| 例 | SQL | 真值 | critic 判决 | 说明 |
|---|---|---|---|---|
| A1 | `ext_player_round_stats` JOIN `rounds` ON **game_id + round_number** | 9/13 = 69.2% | ✅ 成功，reasoning：「赢了 9 场，总共 13 场，9/13 ≈ 69.2%」 | 数字读得准 |
| A2 | 同上，但只连 `game_id` | **应 9/13**，算出 169/286 | ❌ **判成功**，reasoning：「169/286」 | **漏判**（见 9.4） |
| B | `round_number=1` 当首血（历史坏配方） | 与题干无关 | ✅ 失败，critique：「地图应是 Sunset 不是 Split，且缺首血方赢下的回合数」 | 旧判据会放它过 |
| C | `agg_first_blood_stats`（0 行） | 无 | ✅ 失败，`gate=vacuous`，**没调 LLM** | 第一级拦下 |

B 就是 §7.2 第 2 条那个「第一回合当首血」的复现 —— 旧判据判成功并入库，新判据判失败并指出该看哪一列。
`mve/test_critic_caliber.py` 10 条断言把两级判据、JSON 重试、抖动降级、critique 回灌、reasoning→answer 钉住。

**接进主循环也跑了一遍**（`python -m voyager_vlml.loop --topic vlml_first_blood --iters 4`，rib）：

```
[Action Agent] 任务: 在 Split 地图上，哪支队伍在第一回合中获得了首杀？
  [第 1/4 次] 成功 · query_sql 取到 1 行
    critic 判词：查询结果显示在 Split 地图的第一回合中，Global Esports 队获得了首杀。
```

critic 每步都在跑（没有 JSON 解析失败、没有拖慢到超时），判词也进了日志。
但这一条恰好暴露了 §9.5 的 ②③：题干本身把"第一回合"当首杀，SQL 又用
`ORDER BY team_name LIMIT 1` 从多场比赛里任意挑一支队，critic 照样判成功，技能被写回库。

### 9.4 顺手纠正上一轮的一笔错账（重要）

§8.5 写着"`9/13` = 69.2%，与手工核算一致 —— 口径也对"。**归因错了**：

- 模型那条 SQL 是 `JOIN rounds ON ext_player_round_stats.game_id = rounds.game_id`，
  **只连 game_id**；而 `rounds` 是**一行一回合**（`WHERE game_id='1058_game_1'` → 22 行 / 22 个 round_number），
  于是 13 个首血回合被乘成 286 行，算出 **169/286 = 59.1%**。
- 9/13 是**正确 JOIN（再加 `round_number`）**下的真值：实查 `13 回合 / 9 胜`。
- 所以上一轮通过验收的那条答案，数值是错的；我们当时用"手工核算"给它背书，等于没核对模型那条 SQL 的 JOIN 键。

教训与 §7.2 第 2 条同源：**「取到数 + 语义口径对」还不够，行数被放大看起来一样"合理"。**

### 9.5 于是还剩三个洞（诚实，未修）

**① 行放大（fan-out）LLM 判不出来**：A2 与 A1 在 critic 眼里长得一模一样 —— 都是 1 行两个数。
判它的前提是知道"`rounds` 一行一回合、`ext_*` 一行一选手一回合"，即**表的粒度**。

**② 多答案被压成一行也判不出**（2026-10-23 集成跑实测）：
`--topic vlml_first_blood --iters 4` 跑一轮，出的题是「在 Split 地图上，哪支队伍在第一回合中获得了首杀？」，
模型写 `WHERE map_name='Split' AND round_number=1 AND first_kill=TRUE ORDER BY team_name LIMIT 1`
—— 多场比赛都有第一回合，`ORDER BY team_name LIMIT 1` 只是取字母序第一个队，
critic 却给了成功判词「Global Esports 队获得了首杀」。这行 SQL 还被当技能写回了库
（`vlml_first_blood` 的配方被覆盖成它）。行数问题（①）与答案唯一性问题（②）同族。

**③ 题干本身口径错时，critic 只会忠于题干**：上面那道题把"第一回合"当"首杀"，
源头是图谱节点的 examples/hints 里就是这么写的。critic 判的是"SQL 满足不满足题干"，
**不判题干对不对** —— 这一层的责任在节点定义与出题器，不在 critic。

> ①②③ 的补法见 §十：不是加判据，是照原版把口径治在**出题**和**观察**两层。

不选 `intent-diff` 那种"把题干承诺与 SQL 过滤逐项对齐"的写法 —— A2 的过滤条件与题干**完全一致**，
错在 JOIN，逐项对齐查不出来。补法见 §十。

---

## 十 口径漏洞：原版治在哪一层，我们同构到哪一层（2026-10-23）

### 10.1 原版取证：它根本没有"口径判据"

把原版翻遍，没有任何一处做"SQL/动作与题干口径逐项对齐"的检查。口径是在**四个位置**管的：

| 位置 | 原版怎么做的 | 治的是哪个洞 |
|---|---|---|
| `prompts/curriculum.txt:33` | "Tasks that require information beyond the player's status to verify should be avoided. For instance, *Placing 4 torches* and *Dig a 2x1x2 hole* are not ideal since they require visual confirmation from the screen." → **只出「观察面能验」的任务** | ③ 题干不可验 |
| `prompts/curriculum.txt:28` | 题面固定成 "Mine [quantity] [block]" / "Craft [quantity] [item]" 这种**带数量、单动作、一句话**的格式 → 答案唯一、判据就是一个数 | ② 多答案 / ① 分母不明 |
| `curriculum.py:305-318` + `get_task_context()` | 出题后**再给一段 context**：这题怎么做、什么算成功（如 deposit 任务写明 "After depositing, your inventory should only have 20 occupied slots"） | ③ 口径没写明 |
| `critic.py:30-80` + `prompts/critic.txt` | critic 看的是**完整末态**（背包/装备/血量/附近方块/箱子）+ Task + Context，提示词里 6 个例子教的全是"怎么把末态读数翻译成任务语义"（挖 iron_ore 得 raw_iron、吃东西看 hunger=20、种植看 nearby blocks） | ① 读数与语义的对应关系 |

一句话：**原版把口径治在"出题"和"给证据"两端，判据本身保持极简（LLM 判）。**
它没有"行放大检测"，因为 Minecraft 里不存在"同一份末态被复制 22 遍"这种事 —— 那是 SQL 世界独有的。

### 10.2 我们的同构落点（三处，一一对应）

| 洞 | 原版对应物 | 我们照做什么 |
|---|---|---|
| ③ 题干口径错/不可验 | `curriculum.txt:33` | `agents/question.py` 的 system 加三条硬约束：**可验**（只要结果里能直接看出来的数或行）、**唯一答案**（不要"哪一支/哪一场"，除非题面自带钉死条件）、**写明口径**（比例必须写分子/分母各自的取数口径，计数必须写单位） |
| ③ 口径没写进任务 | `get_task_context()` → critic 的 `Context:` | 出题 JSON 多要一个 `context` 字段（"这题怎么算做出来"），随题面一起进 ActionAgent 的 `Context:` 槽、进 critic 的 `Context:` 槽、并写进节点 `skills` 落盘（口径随知识点沉淀，不另开账） |
| ① 行放大 | critic 的观察里"背包有几个木头"就是一目了然的证据 | 给 critic 补同类证据：`env.check_grain(sql)` 实查「JOIN 之后多少行 / 驱动表多少行不同」，渲染成 `Grain check: …放大 22×`，同时进 action 的观察（模型下一轮能改）和 critic 的观察（判有据） |

`check_grain` 的做法（不需要任何领域知识）：把 SELECT 列表换成
`COUNT(*)` 与 `COUNT(DISTINCT 驱动表别名)`，其余 FROM/WHERE 原样保留，跑一次；
`distinct < rows` 就是放大，倍数 = rows/distinct。CTE / 子查询驱动表 / 解析不出来 → 返回 None
（如实说"这次没测"，不假装测过）。

**判还是 LLM 判**，没有新增确定性闸门 —— 这一步是刻意的：原版的判据就是 LLM，
我们只把"它判不出来的原因（缺证据）"补上。

### 10.3 实测（服务器 rib，`mve/_probe_critic.py` 四例）

| 例 | Grain check | 判决 | 判词 |
|---|---|---|---|
| A1 正确 JOIN | 13 行 = 13 行不同（无放大） | ✅ 成功 | 「赢了 9 场，总共 13 场，9/13 ≈ 69.2%」 |
| A2 只连 game_id | **286 行 / 13 行不同（放大 22×）** | ✅ **失败**（补证据前是成功） | 「Grain check 显示…行被错误地关联了 22 倍…」critique 直接给出 `e.round_number = r.round_number` |
| B 第一回合当首血 | 30 行 = 30 行不同 | ✅ 失败 | 点名地图应是 Sunset、要算首血方赢下的回合数 |
| C agg 空表 | 0 行 | ✅ 失败（`gate=vacuous`，不调 LLM） | 真空提示 |

补证据前后 A2 从"成功"变"失败"，判词还直接给出该补哪个键 —— 这就是"证据给足，LLM 自己会判"。

### 10.4 还没证的（诚实）

- **出题侧三条约束是 prompt 级的**，只证了"能跑、会返回 `context`"，还没统计过"题面唯一答案"的执行率
  （同一晚又出过一道「在所有比赛中，哪支队伍获得的首血次数最多？」—— 这道勉强算唯一，
  但库里 `base_events.is_first_blood` 取回 0 行，那轮失败在选表，不在口径）。
- `check_grain` 只测**驱动表**方向；若右表是驱动表、左表被放大（`FROM rounds r JOIN stats s`），
  检测到的是右表行数，仍能发现放大（因为驱动行 d 会小于总行数 n），但"哪张表被乘"要靠 critic 读 SQL。
- 出题器仍会复用节点里人写的错口径 examples（③ 的根），这一层要等"节点定义体检"，
  现在只在 critique 里如实暴露。


---

## 十一、入口统一到 `/mve/`：把面板本身变成这套代码（2026-10-23）

### 11.1 用户指令与它否掉的做法

验收线早就只有 CLI（`task_intake.py ask/retry/sub`、`voyager_vlml.loop`），人没法自己点。
我先起了一个薄壳页 `/mve-ext/`（8779 + Caddy 前缀路由，见 `mve/ext_task_web.py`）。
用户否掉：「**那你把 /mve/ 面板变成这套代码啊，不要再起页面**」。

所以正确做法不是"再加一个入口"，而是**让主面板就是这套代码**。

### 11.2 为什么是 merge，不是把服务指到 rebuild 目录

两条分支在 `ac45d72` 分叉：rebuild 领先 12 个提交，主库 `feature/1-to-n-pluggable-scenes`
领先 **115** 个提交（场景隔离、清库、猫爪开关、MCP 网关常驻、面板性能修复…）。
`mve-panel.service` 的 `WorkingDirectory` 一改指 `/root/mve-rebuild`，那 115 个提交的能力
在面板上全部消失。所以只能把 rebuild 合进主库。

合并冲突只有 2 处（`git merge rebuild/fail-driven`）：

| 文件 | 冲突性质 | 解法 |
|---|---|---|
| `voyager_vlml/agents/action.py` | 主库是**纯增量**（`render_grabs` + `grabs/focus_tables` 透传，2026-10-19 实测得来）；rebuild 整个文件重写过（原版 reset/step/rollout 移植 + 口径检验三级） | 取 rebuild 版为准，把 `render_grabs` 与 `grabs/focus_tables` 按新结构补回：存进实例（`reset(grabs=…)`），**每步重建 system 时一并带上**（同原版 tools/list 在整个 rollout 里恒可见） |
| `.gitignore` | 两边各加一条 | 两条都留（场景本机数据 / 外部 task 账本） |

判据/口径/技能/账本逻辑一行没重写 —— 合并只搬文件。

### 11.3 面板里的入口长什么样

- 页签：`TABS` 新增 `{id:'ext', label:'外部任务'}`，`PANELS` 新增 `pExt`。
- 五个动作：`ask`（导入并试答）/ `learn`（学一轮）/ `retry`（重试这条）/ `sub`（看子目标）/ `list`（列账本）。
- 账本表格直接从 `failed_tasks.json` 读（每 10s 跟一次），显示 task_id / 状态 / 次数 /
  子目标 完成·共 / **可重试** / 问题 / 答案或上次失败原因。
- 后端执行器：`mve/ext_task.py`，**起子进程**跑已有 CLI。

为什么坚持子进程（这条踩过，写进代码注释了）：`task_intake` / `voyager_vlml.loop` 都自己
`asyncio.run()` 并连 MCP 网关（进程级单例）。在 web 进程里直接 import 调，会把连接绑到 web
的事件循环上 —— 实测「第一次点击能通，之后全部 60s 超时」。子进程各自一个干净环境，
崩了也只崩那一次点击。

### 11.4 撤掉的东西

| 撤掉 | 怎么撤的 |
|---|---|
| `mve/ext_task_web.py`（薄壳页 HTTP 服务端） | `git rm`（同构逻辑整段搬成 `ext_task.py` 的库函数） |
| `mve-ext.service`（172.18.0.1:8779） | `systemctl disable --now mve-ext` + 删 unit + `daemon-reload` |
| Caddy `handle /mve-ext*` | 从 `/root/neko/Caddyfile` 删掉该段（留了 `Caddyfile.bak-*`），`docker exec neko-gateway caddy reload` |

### 11.5 实测

| 检查 | 结果 |
|---|---|
| 本地起面板（8799） | `/` 200 · `/api/ext/tasks` 200 · `run(list)` → job `done` 输出 `[]` |
| 入参校验 | `action=nope` → 「不认识的动作」；`ask` 空问题 → 「先把问题填上」 |
| 服务器 5 套测试 | `test_scene_backend` 110 过 / `test_learn_orchestrate` 30 过 / `test_critic_caliber` 13 过 / `test_action_loop` / `test_task_intake` 全过 |
| 公网 `/mve/` | 200，页面含「外部任务」页签，`window.MVE_PREFIX="/mve"` |
| 公网 `/mve/api/ext/tasks` | 200，返回迁移过来的账本（t745700 / t750467） |
| 公网起一次 `list` | job `done` code=0，输出两条 task 的完整账 |
| 旧地址 `/mve-ext/` | **404**（页面真的没了） |
| 黑客松面板 `/mve-hackathon/` | 200（没被波及） |

账本是运行数据，从 `/root/mve-rebuild/mve/failed_tasks.json` 拷到 `/root/mve/mve/`；
技能库**没有**覆盖（`/root/mve` 自己那份是有真实积累的）。

### 11.6 诚实边界

- `ask` / `learn` / `retry` 的真实链路（LLM + MCP 取数）**没有由我跑**：那要几分钟一次、
  且会改技能库。面板上的链路已证到「起进程 → 出输出 → 落账」，剩下的是人点。
- `retry` 命中的是 **`/root/mve` 的技能库**（不是 rebuild 那个只有 1 条坏配方的库），
  所以 `t745700` 说「已学 `vlml_first_blood`」，但这条配方在主库技能库里未必同名同内容 ——
  第一次 retry 的结果要按真实表现看，别按账本推断。
- `/root/mve-rebuild` 目录还在（没有服务、没有路由），留作对照；要清可以整个删掉。
