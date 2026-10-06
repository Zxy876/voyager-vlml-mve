# MVE：让 Voyager 在 VLML 上学会「取数编排」

> **线上面板**：<https://43.161.203.50:8443/mve/>（腾讯云，无鉴权，任何设备直接打开）
> **仓库**：<https://github.com/Zxy876/voyager-vlml-mve>

一个跑得起来的原型，回答一个问题：**当"学习者"不是人而是 agent 时，
一套给人用的学习机制还剩下哪些是能直接搬的？**

数据层是 **VLML**（Valorant 电竞数据，21 张表 / 46 个洞察 SQL / 8 个 MCP 工具）。
学习机制取自**猫娘伴学**（N.E.K.O `study_companion`）—— 它有成熟的知识图谱、
掌握度与判分契约。被考察的学习者是 **Voyager** 式的 agent。

---

## 一、对照旧原型：哪层继承、哪层替换、哪层新写

| 层 | 判定 | 内容 |
|---|---|---|
| 数据源 / 使用场景 / 问题背景 | **原样继承** | Valorant 赛事数据、教练视角的洞察需求 |
| 解决问题的方式与方案 | **替换件** | 整条链路换成 VLML：MCP 把模型直接连到数据库，要数字就查、不许猜，每条主张都有库里的记录支撑 |
| Voyager 主代理亲历学习、积累技能 | **本轮新写** | 在 VLML 链路里反复做同一批题，把「怎么组合这 21 张表」沉淀成技能 |
| 学习面板（衡量 Voyager 的进步） | **本轮新写** | 学习插件面板本来是给人用的；这里的学习者是 Voyager，学习内容只有一个标题：**洞察与分析瓦罗兰特的游戏数据** |
| DriftCoach 旧原型的技术与实现 | **本轮不做** | 不做任何技术讨论，只做概念映射 |

一句话：**继承场景，替换链路，新写「agent 当学习者」这一层。**

---

## 二、界面区域指认

面板七个分区，逐个标注它属于哪一类（形态取自伴学，数据取自 MVE）：

| 分区 | 判定 | 依据 |
|---|---|---|
| 📈 概览（掌握度 / 覆盖率轨迹） | 继承结构 + 新写内容 | 五档状态与 flags 徽标照 `ui_api.py`；内容换成 Voyager 的覆盖率与掌握度 |
| 🎯 练习（出题器派发） | 继承结构 + 新写触发者 | 照伴学 `practice_scope`：人在图谱上钉范围，界面上只剩开始/停止；触发者变成「给 Voyager 出题」 |
| ✍️ 人导入 | **替换** | 记忆与解释的供应方换成 VLML 与其系统 LLM，不是让 Voyager 跑 |
| 🧭 轨迹（行动序列） | **新写** | 伴学没有「工具调用序列」这个概念 |
| 🧠 技能 · 因果 | **新写** | 技能库是 Voyager 的；因果时间线与伴学 `root_fact_seq` 同构 |
| 🕸 知识图谱 | 继承形态 + 新写内容 | 形态照伴学；内容是**从 VLML 实查出来的 212 个事实**，不依赖题目 |
| 🖥 运行日志 | 工具性，非学习状态 | 只为排障，不进掌握度 |

---

## 三、组合：全系统唯一的数据契约

Voyager 的产出不是一段话，是**事实集**：

```jsonc
{ "subject":   {"series": "2843069", "map": "Corrode", "team": "Cloud9"},  // 交叉键字典
  "dimension": "key_metrics.team.consistency.kast.num",                    // 指标名，可点号嵌套
  "value": 109, "unit": "count",
  "base":  109,                                                            // 分母，百分比类必须有
  "scope":  {"rounds": 59, "confidence": "Strong"},                        // 借 VLML 算好的值
  "source": {"tool": "match_analysis_report", "section": "key_metrics"} }
```

四个决定性的设计点（都有代码与出处）：

| 点 | 为什么 | 实现 |
|---|---|---|
| `subject` 是**交叉键字典**不是字符串路径 | 实例里有 `mitch on Skye`、`Corrode R4` 这种多维交叉，字符串拼不出；且 `round:22` 里 22 是值不是键 | `loop_core.norm_subject()` 会把模型返回的字符串也归一成字典 |
| 必须有 `base`，且**参与评分** | `insights_reference.md:130` 是硬规则：67% (16/24) 与 67% (2/3) 数值相同但不是同一个事实 | `base < min_base` 直接判缺失，**数值对也不算数** |
| 能同时吃下两个极端 | `query_sql` 任意 SQL → `from_sql_result()`：一行一列 = 一个事实，只记结果不记这列属于哪张表；`pattern_detection_report` 聚合 → `from_key_metrics()`：每个 key 一个 dimension，scope 直接借 VLML 算好的 rounds/confidence，不自造 | JOIN、CASE、窗口派生列都不影响 |
| diff 单元 = `(subject, dimension)` 对 | 与 rubric 的 key_points 做**覆盖率**比对，不是路径一致性比对——避免惩罚殊途同归 | `loop_core.fact_key()` |

**判定次序（确定性层先跑，不调 LLM）**：

```
0. 工具调用闸 —— 一次工具都没调成功 → 整轮判「无证据」   ← 防编造
1. base 过闸   —— 分母不够，数值对也不算数
2. (subject, dimension) 命中
3. 值容差比对 —— 容差只从服务端 answer_spec 读
判不了的点 → unjudgeable，不静默算对也不静默算错
```

`confidence` 四档（`n≥100 Strong / 50-99 Moderate / 20-49 Weak / <20 Insufficient`，
`insights_reference.md:122-127`）**由 `base` 直接算出**，落进确定性层，不用问模型。

### 叙事性自然语言在哪、怎么声明不参与比对

| 位置 | 标记 |
|---|---|
| 解释输出区（VLML + 其 LLM 产出，人可覆盖） | `comparable: false` |
| Voyager 答卷末尾的 narrative 段 | `comparable: false` |
| 出题器生成的题干 | 参与比对的是 key_points，不是题干文本 |
| covered / missing points | `string[]`（评分点名字），不是散文，参与比对 |

双重标记：机器可读 `{"narrative": {"text": "...", "comparable": false}}`
＋ 面板上人可读的「不参与比对」标签。这条边界 VLML 自己就画好了
（`README.md:97`：All reports return metrics and evidence only. LLMs should generate insights.）

---

## 四、意图是对象，不是自由文本

出题器产出的是**对象**：`question + answer + key_points[] + rubric{评分点: 权重}
+ solution_steps[] + difficulty(1-5) + topic + target_topic_id`。

**人工导入与出题器产出不是同一种结构**，两者被一个布尔量切开 ——
伴学叫 `validated_target`（`practice_outcome.py:53-67`）：

| | 人工导入 | 出题器产出 |
|---|---|---|
| key_points / rubric | 可以有（能从题干抽） | 有 |
| 能批改出 verdict | 能 | 能 |
| `validated_target` | **false** | true |
| 计入掌握度 | **否**，强制 `insufficient_evidence` | 是 |

### 行动因果时间线

所有类型的事实进**同一条单调递增时间线**，只保证顺序，不产生分数
（伴学 `docs/认知引擎阶段文档.md:80` 的 `root_fact_seq` 同构）：

| fact kind | 谁产生 | 进因果 | 进掌握度 |
|---|---|---|---|
| `control`（人导入：出题、给解释、改范围） | 人 | ✅ | ❌ |
| `attempt`（Voyager 的编排与事实集） | Voyager | ✅ | ✅ |
| `referee`（裁判判定） | VLML0 | ✅ | ✅ |
| `narrative`（叙事产出） | LLM | ✅ | ❌ |

没有这条线，人导入就等于什么都没发生。出题器读的是**时间线（因果）+ 掌握度（评分）**两者，
不是只看分数。

---

## 五、三档粒度 = VLML 展开的三种深度

同一套结构，三种展开深度，不需要两套结构：

| 档 | VLML 展开到 | 面板位置 | 对应伴学 |
|---|---|---|---|
| 最粗 | 21 张表 / 45 个洞察 / 146 个工具段 = **212 个事实节点** | 知识图谱 | 知识图谱 |
| 中 | 展开到记录，算出每个组合归档成类的掌握程度 | 练习范围 | 练习范围详情 |
| 最细 | 完全展开，按组合评价 Voyager 每次给出的答案 | 概览 / 轨迹 | 判分 |

图谱的关键性质：**212 个事实是先于题目存在的全集**（从 VLML 实查，不依赖 rubric），
而不是题目派生出来的投影。新题没有图谱缓存时，按题干匹配仍能召回
（`graph_topics.match_facts`）。

---

## 五之二、出题器：自适应到什么程度（与伴学逐条对账）

### 先说结论：题目是硬编码的，难度也是写死的

6 道题全部写死在 `tasks.py` —— `question` / `rubric` / `answer_spec` / `difficulty`
都是常量，难度 1/2/2/3/3/4。出题器（`planner.py`）只在**选哪一道**上自适应
（优先级链 + 冷却闸 + 停滞毕业），**原本没有任何「难度调节」层**。

### 与伴学的对账

| 伴学 | MVE | 说明 |
|---|---|---|
| `select_practice_selection` 优先级链 retry>due>weak>blocked>recommended>default | **继承** | 每条都带 reason + explanation |
| 82 个种子知识点 | **替换** | 6 道硬编码题，每题 = 一个知识点 × 一个写死的难度 |
| `difficulty_policy.select` **算出**难度 2/3/4 | **新写（同构换维）** | 见下 |
| 题目按 (知识点, 难度) **生成** | **不做** | 判定靠 `answer_spec`（服务端配方）；LLM 生成的新题没有 answer_spec，裁判无从独立算出标准答案 —— 硬边界 |
| `apply_readiness_policy`（拿不到证据不推进） | **继承** | |
| `ordered_scope_topics`（错题、做过、难度、id） | **继承** | `tasks.next_topic` |
| `practice_scope` 范围模式 | **继承** | |
| 人导入的问题归到哪一题 | **继承** | `TOPIC_HINTS` |
| — | **MVE 自创** | `RETRY_COOLDOWN` 冷却闸、`STALE_LIMIT` 停滞毕业、`tool_coverage` 分支、`all_stalled` 如实报告 |

### 梯度怎么落地的：同构换了一维

伴学是「同一知识点，难度 2 → 3 → 4」（**题变难**）。MVE 的题难度写死，
且没有「同一知识点的更难版本」，所以伴学那句 `retry → difficulty -= 1`
在 MVE 里**无处落地**。同构关系换一维：

    伴学：同一知识点，难度 2 → 3 → 4（题变难）
    MVE ：同一道题，支架 full → partial → none（题不变，扶得越来越少）

支架只改 **prompt 给多少**，不改 `answer_spec` —— 所以评分点不变、
裁判照样判、掌握度照样算。这是它能成立的原因。

| 档位 | 给什么 |
|---|---|
| `full` | ★ 声明口径路径 + critic 回灌真实候选路径 + 聚焦后的结构文档 |
| `partial` | 聚焦结构文档，不给 ★、不给候选 |
| `none` | 不聚焦、不给 ★、不给候选 |

判据照伴学逐条同构：错题 / 停滞 / 拿不到证据 → 给足；证据不足 → 给足；
覆盖率 100% → 放开；连对两题 → 收一档；连错两题 → 加一档；掌握度 ≥0.80 → 放开。
另加一条 MVE 自创的硬约束：**库里没有可复用的程序就不收支架** ——
收了不是提高难度，只是让它重犯老病。

A/B（同一道题、同一输入，走不带技能库复用的干净通道）：

| 档位 | `kast_pct` | `kd_ratio` |
|---|---|---|
| `full` | `{109/109}` ✅ | 1.24 ✅ |
| `partial` | 1.0 ❌ | 1.0 ❌ |
| `none` | 100.0（形态错） | 1.0 ❌ |

→ **支架不是摆设**：撤掉就回到「把 num/denom 的套路套到标量上」的老病。

全局目标难度（MVE 自创，伴学无同构物）：`目标 = 1 + 已满分题数`，clamp [1,4]；
挑题按 `|题目难度 − 目标|` 最近。跑 `--next` 或看面板能看到
「题目 2 → 目标 4（已拿下 4 道）」与支架档位。`--hint=full|partial|none` 可强制档位做 A/B。

---

## 六、面板上可观测的状态清单

| 状态 | 判定 | 依据 |
|---|---|---|
| 任务派发触发点 | 继承结构 + 新写触发者 | 伴学 `practice_scope` + `mode: explicit_topic` |
| Voyager 行动序列 | **新写** | 伴学没有「工具调用序列」 |
| 解释输出 | **替换** | 供应方换 VLML + 其 LLM，人可覆盖 |
| covered / missing points | 继承结构 + 新写内容 | `string[]`，`evaluator_type: llm_rubric` |
| verdict | 继承 | 四档 `correct / partial / wrong / dont_know` |
| mastery + 等级标记 | 继承 | 三套阈值并存（见下） |
| false_mastery / low_confidence | 继承 | 见下 |
| FSRS / BKT / DKT / DriftCoach 技术层 | **本轮不做** | — |

**掌握度三套阈值（并存）**

| 层 | 取值 |
|---|---|
| 数值 mastery | 0.0–1.0 |
| 五级 level | 未接触 <0.20 / 薄弱 <0.40 / 进行中 <0.60 / 熟练 <0.80 / 掌握 ≥0.80 |
| UI status | `unassessed / weak / progress / good / mastered` |
| 三态 mastery_status | `insufficient_evidence / progressing / mastered` |

两个 flag：`false_mastery` = 平均分不低但波动大（**蒙对的、不稳定**）——
它的杀伤力在 UI 层：分数 0.95 但带 false_mastery，面板仍显示 `weak`；
`low_confidence` = 证据不足。

**必须继承的一条原则**（`ui_api.py:41`）：
**没有证据 = `unassessed` + `mastery: None`，不是 0%。**
Voyager 首次接触某个组合时面板不能显示 0%，否则掌握度曲线从第一题开始就是假的。

---

## 七、这一轮没拿到证据时，面板显示什么

| 情况 | 判定 | 面板表现 |
|---|---|---|
| 一次工具都没调成功 | `evidence_status: none`、`no_tool_calls: true`、verdict `dont_know` | 不计入掌握度 |
| 分母全不够 | `below_threshold`，点名进 `rejected_low_base` | 显示被拒的评分点及原因 |
| 裁判自己也没有这个点的答案 | 进 `unjudgeable` | 不静默算对也不静默算错 |
| VLML MCP 连不上 / 字段不全 | 同上，且不推进 | 出题器 readiness 闸门：**没拿到证据就不出下一题** |

怎么看出来：面板上掌握度显示成「掌握度 {before} → {after}」——
**两个数值没变化，就是这轮没拿到证据**，而且不会生成新题（会重复旧题直到拿到证据）。

实测样本（56 轮里正好有一轮）：

```
pistol_eco_pattern   verdict=dont_know  evidence=none  no_tool_calls=true  facts=0
```

---

## 八、现在跑到哪了（截至 2026-10-06 21:30，56 轮）

| 指标 | 值 |
|---|---|
| 总轮数 | 56（judge 全部为 `referee`，56/56 —— 标准答案全部由 VLML0 独立跑出） |
| verdict | correct 3 / partial 51 / dont_know 1 |
| 无证据轮 | 1（工具全失败，已按上节处理） |
| 技能库 | 4 条技能，33 次写入（21 次重写 / 8 次判为膨胀跳过） |
| 掌握度（按题，最新） | fb_conversion 0.60 / corrode 0.61 / pistol 0.57 / series_totals 0.55 / kast 0.47 / map_rounds 0.43 |

### 有效果的

| 项 | 结果 |
|---|---|
| 代码化取证（口径来自 answer_spec 的题走代码路径，值由解释器取） | `kast_adr_check` **50% → 100%**（根治后空库首轮即 100%）；pistol 100% / corrode 100% / series_totals 100% |
| 图谱（表结构 + 口径 + 参数陷阱） | 无图谱 0% → 有图谱 100% |
| 学习闭环接通 | 判错触发旁路学习，脱敏解法源码进技能库并注入 prompt |
| 自己跑通 → 存程序 → 下次直接跑 | `复用技能：kast_adr_check解法（本轮未重新生成代码）` → 100% |
| 图谱缓存过期守卫 | 加题后 `load()` 自动重建（此前 `kast_adr_check` 因图谱未重建，提示整段为空） |

### 没效果的（同样重要）

加了三层细节，A/B 测下来**在当前题集上全是冗余**：

| 层 | A/B | 结论 |
|---|---|---|
| section 层（取返回里的哪一段） | 开/关各 3 次，全 100% | 冗余 |
| 洞察层（这段由哪些 SQL 拼成） | 开/关各 3 次，全 100%，模型一次 SQL 都没多写 | 冗余 |
| 种子层（212 个事实节点） | 新题开/关各 3 次，全 50%，失败模式一样 | 解决了「有没有」，没解决「有没有用」 |

**真正被模型用上的仍然只有最早那一层：表结构 + 口径 + 参数陷阱。**

### 一个必须记下来的同构边界

**SQL 类不走代码路径。** 先按「全部走代码」跑，corrode 从 100% 掉到 0%。

理由不是调参：原版 Voyager 的 JS 是「调用 bot 上的 API」，对应这里调 MCP 工具；
而 SQL 是 `query_sql` 的**参数字符串**，对应原版 `bot.chat("...")` 的参数
—— **原版从不把 chat 的字符串当程序来学**。所以按口径源分流
（`_is_tool_path_task`）：`answer_spec.tool` 走代码路径，`answer_spec.sql` 走原路径
（那条有图谱骨架与分层断言，正是 SQL 需要的护栏）。

---

## 八之二、「连着 48 轮停在 50%」的根治（2026-10-06）

症状：`kast_adr_check` 反复停在 50%（kd 交 1.0，真值 1.24），其余未满分的题也一起停滞。
诊断过程见 `取证通路-原版Voyager对照.md`。**根因不是「取数能力不够」，是四个具体的 bug**：

| # | bug | 证据 | 修法 |
|---|---|---|---|
| 1 | 结构文档**按固定条数截断**：`_MAX_PATHS=60`，而 `match_analysis_report` 正好 61 条 —— 正确路径 `key_metrics.team.consistency.kast.num` 在**抽取阶段就被砍掉** | 缓存里 61 条止于 `team_comparison.NRG.assists` | 抽取上限提到 400，**展示**改按维度聚焦（命中 → 同父兄弟 → 顶层骨架，各父节点限 8 条） |
| 2 | 图谱是**落盘缓存且已过期**：`kast_adr_check` 是后加的题，图谱里根本没有 `kast_pct`/`kd_ratio` 节点，`graph_hints` 整段返回空 | 301 个节点全是老题种的 | `load()` 加指纹守卫（`_task_fingerprint` 变了就自动重建）；`graph_hints` 在图谱缺维度时**直接读题目自己的 rubric** |
| 3 | critic 只说「路径不对」，**不给候选**：模型三轮交出一模一样的错路径 | 三次回灌同一份代码 | 用 AST 抽出模型写过的路径，指明「这条不在返回里」，并列出返回里真实存在的候选 |
| 4 | 声明的口径只是**建议**不是约束：`team_comparison.*.consistency` 是一支占位值分支（Cloud9 与 NRG 的 `kd_ratio` 都是 1.0、`adr` 分母都是 59），照样能 dig 出数字 | 权威段 `key_metrics.team` 才是 1.24 / 109 | `declared_paths()` 把题干口径变成**硬约束**：prompt 里打 ★ 排最前；critic 单独判「取到值但取错分支」 |

修完的表现：**空技能库、第一轮即 100%**（`kast={109/109}`、`kd=1.24`），
工具类题（kast / pistol / corrode / series_totals）全部 100%，SQL 类无回退。

### 学习循环的后半环：程序是拿来**跑**的，不是拿来读的

原版 `add_new_skill` 存的是 `program_name + program_code`（自己写、跑通了的完整源码），
下次取出来**直接 exec**。此前 MVE 只有前半环，缺的三件事这次补齐：

| 原版 | 此前 MVE | 现在 |
|---|---|---|
| 任务完成才 `add_new_skill` | 自己跑通的那段代码从来没存过，只存观摩来的解法 | 覆盖率 ≥ 0.95 就存**自己写的程序**（`PRACTICE_SAVE_COVERAGE`），连主函数名一起存进 blueprint |
| 主键是函数名（稳定，同名走 Rewriting） | 主键是 LLM 每轮生成的一句话（「按 team 下钻」/「按 team 拆分」）→ 永远算新技能，versions 49、膨胀率 17.5 | 主键固定为 `{topic_id}解法`；`learn()` 那条文字经验也改用同一个 key，两条通道覆盖同一条 |
| 检索出来直接 exec | 检索出来拼进 prompt 让人**读**，模型每轮重写一遍（每次重写都是新的翻错机会） | `_reuse_skill()`：先直接执行库里的程序，维度齐全就采用（`复用技能` 打印），跑不通才让模型重写 |

另外修了一处统计口径：代码路径此前从不设置 `_injected_keys`，技能的
`hits` / `ok` / 有效性加权全是死的，面板上「本轮注入 0/5」不是没注入而是没记。

### SQL 侧顺手修的一个 bug

`map_rounds_split`：模型一条 `GROUP BY map_name` 查三行再按行号取值，交上
Corrode/Haven/Lotus = 21/24/14，真值是 14/21/24 —— **数字全对，只是配错了地图**。
这类错不报错、不取空，只有裁判比对才发现。已加 `_check_group_order` 断言
（GROUP BY 无 ORDER BY 即驳回，并给出「每个分组一条 SQL」的骨架）。
该题仍有 2 个评分点没覆盖（胜率两条压根没查），见「已知缺口」第 7 条。

顺带查出第二处：计划校验里写死的「每轮最多 3 次调用」。
`fb_conversion_analysis` 有 4 个评分点、`map_rounds_split` 有 5 个，
按图一条查询最少就要 4 / 5 条 —— 写死 3 等于**必然漏条**（模型写 4 条就被闸拦下，
三次重试全废在「条数超了」上，最后降级放行 3 条）。上限已改成
`max(3, len(rubric))`。放开之后模型仍然只规划 3 条，所以这只是解除了一个
**机制性封顶**，不是解法本身。

---

## 九、跑法

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # duckdb + httpx，其余全是标准库
cp .env.example mve/.env                 # 填 ZHIPU_API_KEY

python mve/verify_env.py                 # 环境体检（VLML 数据层要就位）
python mve/knowledge_graph.py --build    # 建图谱（可选 —— 见下）
python mve/run_mve.py --llm --topic kast_adr_check --rounds 3
python mve/dashboard.py                  # 面板 http://127.0.0.1:8777
```

> `--build` 其实可以不跑：`KnowledgeGraph.load()` 带指纹守卫，
> 发现落盘图谱与 tasks.py 对不上会**自动重建**（加过题忘了重建是踩过的坑）。

| 开关 | 默认 | 作用 |
|---|---|---|
| `MVE_ACTION_CODE` | **1** | 代码化取证（仅工具路径类题生效，SQL 类自动走原路径） |
| `MVE_GRAPH` / `_SEED` / `_SECTION` / `_INSIGHT` / `_STAGE` | 1 | 图谱各层，都能关掉做 A/B |
| `MVE_ASSERT` | 1 | 硬校验（关掉才能测出提示层的边际效果） |
| `--hint=full\|partial\|none` | 由出题器算 | 强制支架档位（A/B 用，见 README 五之二） |
| `MVE_PANEL_HOST` / `_PORT` / `_PREFIX` | `127.0.0.1` / 8777 / 空 | 面板部署用（见 `部署.md`） |

---

## 十、部署

已部署到腾讯云 `43.161.203.50`，面板 **https://43.161.203.50:8443/mve/**（无鉴权）。

这台机器只放行 22 / 8443，8443 已被猫娘的 Caddy 网关占着，所以 MVE 挂在它的子路径下：
`Caddy(handle /mve* → 172.18.0.1:8777)` → 面板只监听 docker 网桥地址，systemd 托管、开机自启。
为此面板支持 `--host / --prefix`：服务端剥前缀（不带前缀一律 404），
前端 13 处 `fetch('/api/...')` 注入 `window.MVE_PREFIX` 自动加前缀。
完整步骤、Caddy 片段与自检见 **`部署.md`**。

**仓库不含**：`vlml/`（数据层，独立 git 仓库）、`voyager-fork/`、`study_companion/`
（只读参照，不分发）、`mve/.env`、运行日志与状态文件。凭据全走 `os.getenv`，无硬编码。

---

## 十一、已知缺口（含与设计的失配）

1. **掌握度挂在 `topic_id` 上，不是「组合归档成的类」。**
   设计要的是「按组合类算掌握程度」，代码目前按题聚合。三档粒度里最细那档（按组合评价）
   已经做到，但账本仍挂在题上 —— 这是当前与纸面设计的**最大失配**，未修。
2. **双路去重解释不做。** 存在的是「人导入 → VLML 解释 → 进时间线 → 反哺出题器」；
   设计里问到的「用户提问、由 VLML 与 Voyager 去重后交 LLM 解释」那条通路**本轮不存在**，
   也不打算做。
3. **出题器会被错题卡住。** 实测 56 轮里 45 轮给了 `kast_adr_check`（覆盖率长期停在 50%），
   其余 5 道题加起来只拿到 11 轮。已有冷却闸与「没进展就毕业」机制，但仍会重现。
4. **段内定位**：`key_metrics` 下还有 `team`/`opponent` 两层，模型会翻错层。
   工具路径类已由「题干声明口径 = 硬约束」解决（`declared_paths()`）；
   **SQL 路径类**（靠自己写 SQL 的题）没有等价护栏。
5. **题集只有 6 道，且题面/难度全部硬编码**。梯度已由「支架收放」承担（见五之二），
   但**题目本身仍写死在 `tasks.py`**，不能按 (知识点, 难度) 生成 ——
   判定靠 `answer_spec`，生成新题就没有标准答案，这是硬边界。
   要测「学」，得先有人补题（补题 = 补 answer_spec，不是补题面文案）。
6. 面板的求助按钮与 help_decision 展示还没做。
7. **SQL 路径的「编排漏条」未解**：`map_rounds_split` 60%、`fb_conversion_analysis` 80%，
   失败形态都是「某个 subject 组合（某张图）的评分点根本没查」，而不是值取错。
   图谱上下文给全了口径骨架、feedback 也点名了缺哪个维度，模型下一轮仍只出 3 条查询。
   试过两条路都**没有奏效**，故如实记录：
   - 加提示要求「每个 subject 组合一条查询」→ **反而掉到 50%**（模型写 4 条被
     「最多 3 次调用」的闸拦下，三次重试全废）。已撤销该提示。
   - 把调用上限从写死的 3 改成 `max(3, len(rubric))` → 回到 80%，模型**仍然只规划 3 条**。
   结论：缺的不是提示措辞、也不只是条数上限，而是「按评分点逐条展开」的
   **结构性约束**（例如把评分点展开成强制的 call 模板），未做。

---

## 十二、文档

- `部署.md` —— 腾讯云部署形态、Caddy 片段、自检与更新
- `知识图谱与求助闭环.md` —— 图谱三层与学习闭环的完整取证（含负结果）
- `取证通路-原版Voyager对照.md` —— 「原件进判定」vs「誊抄本进判定」
- `裁判链路记录.md` / `反馈链路与旁路学习.md` / `掌握度调研-猫娘伴学对照.md`
- `进度对照.md` / `环境体检.md` / `MVE跑通记录.md`
