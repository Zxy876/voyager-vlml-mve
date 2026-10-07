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

### 先说结论：题面仍是硬编码的，但**难度梯度层已经在跑**

6 道题写死在 `tasks.py` —— `question` / `rubric` / `answer_spec` 是常量。
`difficulty` 字段也是写死的（1/2/2/3/3/4），但它是**种子难度**，不是最终难度：
出题器（`planner.py`）在**选哪一道**上自适应（优先级链 + 冷却闸 + 停滞毕业），
`difficulty.select()` 又在**出到什么难度、给多少支架**上自适应 ——
每次都算出 `difficulty_target` 与支架档位，并把推导过程打印出来（见八之三 ①）。
题面本身要**自动生成**则走 `question_gen.py`（见下）。

### 与伴学的对账

| 伴学 | MVE | 说明 |
|---|---|---|
| `select_practice_selection` 优先级链 retry>due>weak>blocked>recommended>default | **继承** | 每条都带 reason + explanation |
| 82 个种子知识点 | **替换** | 6 道硬编码题（+ `question_gen` 自动生成的），每题 = 一个知识点 × 一个**种子**难度 |
| `difficulty_policy.select` **算出**难度 2/3/4 | **新写（同构换维）** | 见下 |
| 题目按 (知识点, 难度) **生成** | **已做（`question_gen.py`）** | 见下「自动生成题目」 |

### 自动生成题目：生成的是**配方**，不是答案

> 我上一版在这里写的是「不做 —— LLM 生成的新题没有 answer_spec，
> 裁判无从独立算出标准答案，硬边界」。**这个判断是错的**，下面这节是更正。

错在把「不能信模型给的答案数值」等同于「不能让模型出题」。
`loop_core.py:165` 确实写死了：expected 与 tolerance 只从服务端私有的
`answer_spec` 读 —— 但这只禁止**信模型给的数**，不禁止**让模型出题**。

正确做法：让 LLM 出的是**可执行的取数配方**（`answer_spec`：SQL，或
工具 + 参数 + 取值路径），而不是答案数值。配方是可执行的 → 裁判跑一遍
就从 VLML 拿到真值。实测：

```
[裁判 VLML0] series=2843069/team=Cloud9/map=Lotus · map_win_rate = 45.8 (base=24)
             series=2843069/team=Cloud9/map=Lotus · rounds_won   = 11
```

**裁判能独立算出自动生成题的标准答案** —— 硬边界不成立。

#### 四段闭环（照伴学 `question_generate` + `question_validate`）

| # | 伴学 | MVE | 文件:行 |
|---|---|---|---|
| 1 | `resolve_target_question_type` 题型由服务端定 | `_dim_specs()` 口径由服务端定（percent/count/ratio/label + 要不要分母），并**枚举维度**给模型挑 | `question_gen.py` |
| 2 | `_normalize_question` LLM 出题面 + 答案 | `propose()` 出题面 + **取数配方**（禁写答案数值，写了判废） | 同上 |
| 3 | `question_validate` **第二个 LLM** 判三布尔 | `validate()` **确定性验题**：跑得出吗 / 值非空吗 / 量纲对吗 / 重复吗 | 同上 |
| 4 | `enforce_mapped_question_type` 强制改回 | `enforce()` 强制改回：维度、工具、参数、percent、分母 | 同上 |

第 3 步是 MVE 比伴学**强**的地方：伴学「答案是否被支持」是另一个模型的
意见（所以它还得再加一致性标志防自相矛盾）；MVE 是数据事实，跑一遍就知道。

#### 验题的七道闸（每条都能复盘）

| 闸 | 判什么 | 实测抓到过什么 |
|---|---|---|
| 维度在服务端声明里吗 | 不许发明口径 | 模型把**工具名** `pattern_detection_report` 当维度填 |
| 题面与维度定义相关吗 | 伴学的 `relevant` | 问「强起局胜率」却填 `pattern_confidence`（置信度标签） |
| 与已有题 (subject, dimension) 重复吗 | 不出重复的题 | `pistol_win_rate` × {series,team} 已有 |
| 题内两个评分点重复吗 | 两个点写成同一条查询 | 两点 SQL 一模一样 |
| 百分比口径给分母了吗 | 问胜率却取个数 | 只给 `pistol.num`=3，真值是 3/6=50% |
| 算出来是合法百分比吗 | 0–100 | — |
| 计数量纲超上限吗 | ≤ 全场总回合数 59 | `SUM(round_number)`=145 被当成「打了多少回合」 |

#### 失败会回灌，而且是「错在哪 + 怎么改」

伴学 `entry_tutor_question_entries.py:1885` 把上一轮的 `validation_failure`
塞进 `generation_feedback`。MVE 同构，但多给一句**怎么改** —— 只报症状
实测三轮都改不动（模型会换一种方式犯同一个错）：

```
修法：SQL 里这些不是 `rounds` 的列：team_name。 rounds 表没有 team_name 列，
      筛队伍用 winning_team_name
修法：按题面看，更可能是：eco_win_rate, map_win_rate, pistol_win_rate
```

#### 服务端能补的就不废题（照伴学 enforce 的精神）

模型漏写**格式**不该判废，口径归属本来就由服务端定：

- 维度填错但配方对 → 按 `value_path` 反查维度改回来（实测把 `kast_pct` 纠正成 `pistol_win_rate`）
- 工具名错 → 按图谱 `who_produces` 改回来
- 必填参数忘传 → 从 `subject` 照签名补（`team` → `team_name`）
- 百分比口径给了分母却没写 `percent` → 补 `true`
- SQL 算出两列却忘填 `base_column` → 运行时补最后一列

#### 调度：什么时候才生成新题（伴学 `weak_topic`）

伴学答完一题后（`entry_tutor_answer_entries.py:174`）：
`reason == "due_review"` → 复习旧卡；否则 → `action = "generate_question"`。

MVE 补齐了这一层 —— 这正是「连着 10 次停在 50%、没有可推进的题了」的出口：

```
planner.py  all_stalled
   → suggestion {about, focus_dimension, subject_key, difficulty, why}   ← 只选点，不调 LLM
run_mve.py  看到 action=generate_question → question_gen.generate_adopt()
   → 验过 → 落盘 generated_tasks.json → tasks.TASKS 自动合并
```

分工照伴学：**planner 只说清「练什么」，生成题面是 entry 层的活**。

跑法：

```bash
python mve/question_gen.py --about "Cloud9 在 Lotus 图上的胜率" --difficulty 2 --adopt
python mve/run_mve.py --no-gen      # 关掉自动生成
```

⚠️ 诚实的成功率：当前 LLM 走**工具路径**出题一次就能写对；走 **SQL 路径**
时约三分之一能过（最常见的是把工具名当表名、或造一个不存在的列）。
七道闸会把它挡下来并回灌，但三轮都改不动的情况确实存在 ——
这是模型能力问题，不是链路问题。
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

## 五之三、人导入文本 → 生成解释 → 影响出题（与伴学对账）

### 伴学这条链怎么走

1. 人录入文本（粘贴 / 导入 / OCR）→ `study_explain_text`（`entry_tutor_explain_entries.py:169`）
2. LLM 生成解释（`concept_explain`）
3. 导入的**材料**经 `MaterialTopicMapper` 映射到图谱里**已存在**的知识点
   （`material_topic_mapper.py:227`：映射到不存在的 topic 一律 rejected）
4. 这些知识点成为练习范围 → 出题器按 `weak_topic` 出题
5. **归不上时**：提示人去图谱里选一个知识点（`ui.practice.baseline_topic_prompt`）

注意第 3 步：**伴学也不是从文本凭空长出新知识点，而是映射到已有知识点**。

### MVE 的对应

| 环节 | 伴学 | MVE | 状态 |
|---|---|---|---|
| 录入文本 | `study_explain_text` | `coach.explain()` | ✅ 同构 |
| 生成解释 | `concept_explain`（LLM） | VLML 取数 + `narrator` 出解释 | ✅ 同构 |
| 解释写进记录 | learning record | `causal_timeline` CONTROL fact | ✅ 同构 |
| 归到已有知识点 | `MaterialTopicMapper` | `infer_topic()` | ✅ 同构 |
| 记录影响出题 | practice_scope / weak_topic | planner `human_focus` | ✅ 已通（实测推荐理由就是 human_focus） |
| **归不上时** | 提示人选知识点 | **原本断线**（topic_id 空 → 出题器跳过） | ✅ 本轮补上 |

### 本轮修的两个断口

**断口一：`infer_topic` 漏了后加的题。**
手写表 `TOPIC_HINTS` 里没有 `kast_adr_check`（加题时漏补），
实测「Cloud9 这场比赛的 KAST 是多少」直接归不上 —— 题库里明明有这道题。
改成**从 TASKS 自动派生**（`_auto_hints()`）：

```
kast_adr_check  ← kast, pct, ratio, k/d      （dimension 词干 + point 里的缩写）
series_totals   ← rounds, total, won
```

手写表优先（人工精修的更准），自动派生作兜底且要求 ≥3 字符
（长度闸只管自动派生 —— 手写的中文单字「崩」也是有效关键词）。

**断口二：归不上就断线。**
`coach.explain()` 加了 `auto_task`：归不上已有题时，拿这段文本去
`question_gen` **生成一道新题**，成功就写进 `TASKS` 并挂到 control fact 上，
出题器于是看得见。这比伴学多一条路 —— 伴学只能让人去选，MVE 能自己长出一道。
生成失败也**不影响解释照常返回**（人看到的内容不受影响）。

### 诚实的限制

维度枚举是从已有题派生出来的，所以**超出已有维度的新方向**
（如「进攻方胜率」「道具使用效率」）既归不上、也生成不了 ——
实测这类导入只能拿到解释，长不出新题。
要突破得让维度枚举能从数据层派生（`match_analysis_report`
返回的 `attack_rounds_won` 之类），这是下一步的事。

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

计划校验里写死的「每轮最多 3 次调用」当时也改成了 `max(3, len(rubric))`，
但**这一处当时判断错了**：当时认为「放开上限后模型仍然只规划 3 条」，
于是记下"缺的是结构性约束"。真实情况是 —— 计划层放开了，
**执行层还写着 `[:3]`**（见八之三）。模型早就在规划 4 / 5 条，
只是第四条之后在执行时就被静默丢弃，日志里看不出来。
现象相同，根因不同，结论也就跟着错了。

---

## 八之三、「跑得越久越该学会，但有两题一直学不会」（2026-10-07）

用户提了两个问题：**① 难度梯度自适应出题现在做不做得到？**
**② 跑得越久按理学得越多，可有些东西一直学不会，看日志。**

### ① 梯度层已在运行（不是待办）

`difficulty.select()` 每轮都算，吃七个信号：种子难度 × 掌握度（取平均）、
未解决的前置阻塞（压到最低档）、错题重试（−1）、最近连对（+1 且收支架）/
连错（−1 且加支架）、覆盖率（满分才放开）、证据量与置信度（不足就封顶 3、
支架给足）、以及「本题是否已存下可复用的程序」。每次都把推导过程打出来可复盘：

```
难度梯度 : 题目 2 → 目标 4（已拿下 5 道 → 目标难度 4｜种子难度 2 与掌握度 0.55
          平均 → 难度 3；覆盖率 60% → 半扶；但这题还没有可复用的程序 → 支架先不收）
```

支架档位不是摆设：`--hint=full / partial / none` 三档 A/B，覆盖率随档位下降。
（同构换了一维：伴学按 `depth/difficulty` 排序选题，MVE 按**覆盖率**收放支架 ——
主判据用覆盖率而不是掌握度，因为掌握度低可能只是「证据还少」，
用它会误收已会的题的支架。）

### ② 看日志：不是学不会，是三个 bug

88 轮日志的覆盖率分布一眼就能看出分野 —— 工具路径的题全 100%，
**SQL 路径的两道长期卡死**：

```
题                       轮数  最好   最后   形态
map_rounds_split          47   100%   100%   ▃█▅▅▅… 长期 60%
fb_conversion_analysis    34   100%   100%   ▆▆▆▆▆… 长期 80%
kast_adr_check            10   100%   100%   ██████
pistol_eco_pattern         6   100%   100%   ██████
其余                       —    —      —     100%
```

评分点级更清楚：`Haven 图首血转换率` **0/23 一次都没取到**；
`Corrode 上胜率` 5/43、`Haven 上胜率` 2/43。
三个 bug，按被发现的顺序：

| # | bug | 取证 | 修法 |
|---|---|---|---|
| 1 | **执行层硬截断 3 条调用** —— `voyager._execute` 里 `(calls or [])[:3]`。`map_rounds_split` 5 个评分点、`fb_conversion_analysis` 4 个，模型规划了 5 / 4 条，第 4 条起被**静默丢弃** | 40 多轮里轨迹永远 ≤3 条；改完当轮即变 4 / 5 条 | `_call_limit()`：`max(3, min(8, len(rubric)))`，执行层、计划校验层、裁判 `_execute`、coach `_execute` 四处统一 |
| 2 | **critique 永远是 None** —— `_render_observation()` 读 `last_round["critique"]`，而 `last_round` 是上一轮 `run()` 末尾拍的快照，**那一刻 `learn()` 还没跑**，critique 恒为空字符串 | `MVE_DEBUG_PROMPT=1` 落盘的 prompt 里白纸黑字：`上一轮你的自我反思 Critique：None` | 改读**当前** `self.last_critique`（`learn()` 结束时才写入的那个） |
| 3 | **反馈只点维度、不点评分点** —— `map_fb_conv` 挂着 Corrode 与 Haven 两条评分点，只说「缺 map_fb_conv」，模型以为自己已经交过（它确实交了 Corrode） | 日志里 43 轮同一句空话 | 新增 `failed_points`：回灌**点名的评分点**并写明「每个都要单独一条查询」；跨运行也从 `run_log` 恢复 |

顺带修的第四个（不在上面两次追问里，但同批暴露）：
保持度层 `mastery_retention.py` 用了 `math.exp2` —— **Python 3.11 才有**，
本机 venv 是 3.10，`AttributeError` 不在调用方的 `(ValueError, TypeError)`
捕获范围内，于是**每一轮都在打印「保持度未更新」**，半衰期从未动过。
`mastery_model.py` 早先踩过同一个坑并改用 `2.0 ** x`，这一处漏改。

**修完的表现（同一道题、同一模型、无其它改动）：**

| 题 | 修前 | 修后 | 调用条数 |
|---|---|---|---|
| `map_rounds_split` | 60%（47 轮） | **100%** 第 1 轮 | 3 → 5 |
| `fb_conversion_analysis` | 80%（34 轮） | **100%** 第 1 轮 | 3 → 4 |

回归：其余五题（series_totals / pistol_eco_pattern / kast_adr_check /
lotus_win_rate / corrode_collapse）**无回退**。保持度开始真的落盘
（半衰期出现 3.13 / 3.14 / 3.34 的变化，此前恒为 3.0）。

**一句要记下的教训**：这三处都不是「模型学不会」，是**链路断了**。
反馈写得再准（配方、表名、WHERE 该带什么键，全写对了），
只要 critique 进不了下一轮的 prompt、第四条调用发不出去，它就只能原地打转 ——
而且从日志上看，长得跟"学不会"一模一样。
区分二者的办法只有落盘验伪（`MVE_DEBUG_PROMPT=1` 看 prompt 原文），
不能靠"我觉得反馈写了应该就到了"。

---

### 八之四：出题器照**知识图谱**出题（2026-10-07）

用户追问：「伴学插件是不是根据知识图谱出的题？去查查，然后改」——
去伴学取证后确认：**是，而且比 MVE 做到的彻底得多。**

#### 伴学的做法（取证自 `static/knowledge_seeds/math.json`，457 个数学知识点）

图谱节点**每个都自带**（出现率均 100%）：
`difficulty`（节点自带难度，如 0.25）· `question_types`（该知识点该出什么题型）·
`typical_misconceptions`（典型错误）· `prerequisites` / `related` / `depth`（依赖与图谱深度）·
`skills` / `examples` / `unit`。

`question_type_mapping.py:131` 的 docstring 是最直白的证据：

> "Choose the first declared teaching style and **map it without LLM input**.
> Seed ordering is preserved as the **author-provided priority**."

**题型由节点声明决定，不交给 LLM；难度是节点自带的，不是生成时要求模型"出个难度 4 的题"。
伴学的题库是「知识点 × 难度」二维铺开（892 个知识点），不是靠生成时临时发挥。**

#### MVE 此前的差距

| 环节 | 伴学 | MVE（此前） |
|---|---|---|
| 难度从哪来 | 图谱节点自带 | 梯度算出来了，但**只用于选旧题**，进不了出题器 |
| 题型从哪来 | 节点 `question_types` 声明 | LLM 自由发挥 |
| 出题点谁选 | 图谱 | LLM 自由发挥 → 编造表名、把维度名当列名 |

#### 改了什么

1. **新题难度接上梯度**：`_new_topic_suggestion(settled, target=..., mode=...)`。
   此前是 `int(task.difficulty)` 抄停滞题的种子难度 —— 梯度算出的 `target`
   到这里就断了。`cleared`（全被拿下）时难度 = `min(4, target + 1)`，即**推进一档**。
2. **难度分档下了定义并真的去数**：`DIFFICULTY_RUBRIC` 把每档写成**可数的 SQL 特征**
   （1=单表聚合，2=分组，3=JOIN/条件分支，4=窗口函数/嵌套子查询），生成后逐条数，
   不达标就判废。**判废独立于其它闸** —— 否则模型在"表名写错"那道闸就先挂了，
   永远收不到"难度不达标"这句反馈（实测三次生成全挂在同一个错上）。
3. **`_server_skeleton` 把图谱声明渲染出来**：SQL 维度此前只给一句
   「需自己写 SQL，取第 N 列」，把 detail 里**本来就有的**
   `tables` / `columns_by_table` / `semantics` / `typical_errors` 全吞了。
   现在给出 `FROM **rounds`，可用列…，口径形态…，典型错法…。
4. **服务端从图谱挑出题点**（`_graph_blueprint`）：照伴学，题点归服务端、
   题面归模型。挑定 (维度 × subject 键 × 表 × 列 × 口径) 后**写死进 prompt**，
   模型只填 WHERE 的值和题面文案。

   为什么必须走到这一步（实测教训）：只把「真实列清单」放进 critique **不够** ——
   三次生成里 critique 每次都把 `fb_player` 列在真实列中，模型照样坚持写不存在的
   `player_name`。**让它挑，它就会按常识编**；唯一可靠的办法是不让它挑。
5. **`about` 与出题点冲突时以出题点为准**：planner 说"用队员级范围"，
   而图谱挑中的 `max_losing_streak` 是**回合级**口径（rounds 表没有队员列），
   模型夹在中间把 `fb_player` 写进了 rounds 表，三次全挂。

#### 实测（同一条链路跑通）

```
⚠ 这道题连着 13 次满分（全库 3 道已被拿下，目标难度 4）→ 往上一档出题
图谱出题点 : max_losing_streak × map｜表 rounds｜列 losing_team_name、map_name、round_number、series_id
第 1 次 ❌ SQL 里这些不是 rounds 的列：team_name → 筛队伍用 winning_team_name
第 2 次 ✅ 验题通过（跑出 'Haven'，base=8）→ 新题已入闱
```

新题（难度 4，gaps-and-islands）交给 Voyager：**第 1 轮 0%**，
计划校验两次拦下经典陷阱（把 `losing_team_name` 写在最内层 WHERE —— 编号前就筛掉队伍，
剩下的回合编号必然连续，整段会被当成一块）。

**这才是关键结果：不再是「首轮即满分」，学习曲线终于有的测了。**
此前那个"题库太简单测不出学习"的死结，靠的是**系统自己上难度**解开的。

顺手修的三处：`topic_id` 中文（会进日志/面板 URL）→ 服务端规范化成英文小写下划线；
取值不是数字（取到过 `'Haven'` —— `value_column` 指到了标签列）→ 判废而非放行；
`_bad_columns` 把 `over` / `partition` / `subquery` 等 SQL 关键字当成列名报给模型，
属于误导性反馈 → 补进关键字表。

---

## 八之六、学习曲线现在测不测得出：**测得出，但测出来是平线**

直接回答这个问题，因为答案反直觉。

跑了 5 轮 `max_losing_streak_map`（难度 4），覆盖率轨迹 `['0%','0%','0%','0%','0%']`，
critique **五轮一字不变**。看起来是"学不会"。查下去发现不是 —— 四处基础设施
各自在把真信号伪装成假 0%：

| # | 断点 | 症状 | 修法 |
|---|---|---|---|
| 1 | 坏题在库里 | `value_column: 0` 指到 `map_name`，真值是地图名 `'Lotus'`；题干问"最长连败回合数"、`tolerance=0.05` → 交任何数字都错 | 闸 3b（取出的值必须是数字）+ 闸 9（"连续"语义必须有 gaps-and-islands 形状：`COUNT(*) OVER (ORDER BY)` 是累计计数，不算）+ `revalidate_store()` 复核存量 |
| 2 | critique 把失败讲成成功 | `if not items:` 把"missing 为空"当成"全覆盖"，输出「覆盖了裁判的全部 **0** 个评分点，编排方式有效，可以固化写进技能库」 | 判据改看 `verdict`/`coverage`；新增 `not_accepted` 分支：「交了 ≠ 拿到证据」 |
| 3 | 比对层判成不可判 | subject 里的 `'?'` 占位符无法回填，`fact_key` 与评分点声明永远对不上 → 记 `unjudgeable`，`coverage=0` 且 `missing=[]`/`rejected=[]` | `'?'` 按**通配符**匹配 |
| 4 | 裁判缓存不过期 | 题重新生成了，裁判仍拿旧真值 `'Corrode'` 比对 —— 模型交的 `3` **是对的**，却被判"值不可比" | 缓存加 `spec_fingerprint`（配方指纹） |

修完之后：**`max_losing_streak_map` 首轮 100%，`corrode_collapse` 首轮 100%**
（后者历史是 `100/100/100/20/20/100`）。

### 所以结论是什么

- **能测了**：基础设施现在如实记录，不再有假 0%。上面四条任意一条存在，
  曲线都是假的 —— 你会以为是"模型学不会"，其实是系统在骗它。
- **但测出来是平线**：题库里**没有它还不会的题**。难度上限是 4，
  而难度 4 的题在图谱声明 + 结构骨架面前一轮就做对了。
- **撤支架也还是 100%**（`--hint=none`）—— 它已经会了，不是靠支架蒙对的。

要真画出上升段，得满足其一：**难度 5+ 的题**（出题器 `DIFFICULTY_RUBRIC` 现在
最高 4）、或**更难的数据口径**（多表 JOIN + 窗口函数 + 分组，当前题库没有）、
或**换更大的数据集**（现在只有 1 个 series、59 个回合，口径空间本来就窄）。

顺带一个发现：出题器给不给**结构骨架**差别很大 —— 不给骨架连试 4 次全废
（写成累计计数、编造 `team_name` 列），给了之后第 2 次就出对。
骨架就在图谱里（`recipe`，脱敏过，还标了 WHERE 该放哪一层）。

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
| `--no-gen` | 关 | 关掉「没有可推进的题时自动生成新题」（见五之二） |
| `MVE_DEBUG_PROMPT` | 关 | 置 1 把本轮**完整 prompt 原文**落到 `/tmp/mve_plan_prompt.txt`。链路问题只能看原文定位，不能靠猜 —— 八之三那三个 bug 全靠它取证 |
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
`mve/knowledge_graph.json` 也在 `.gitignore` 里 —— 它是 `build()` 的落盘缓存，
每台机器自己建（三判据指纹守卫会让它在需要时自动重建，见八之五）。

### 同步到服务器（**没有**自动同步）

之前以为服务器会自动拉代码 —— **这是误判**，实测它一直停在旧的 `4562de7`。
要手动三步，缺哪步都不算同步完：

```bash
ssh root@43.161.203.50
cd /root/mve && git pull                       # 1. 拉代码
.venv/bin/python mve/knowledge_graph.py --build  # 2. 重建图谱（缓存是各机自己的）
systemctl restart mve-panel                    # 3. 重启面板（旧进程用的是旧代码）
```

第 2 步不能省：图谱是落盘缓存且被 gitignore，`git pull` 带不过去；
不过就算省了，`load()` 的三判据守卫也会在下次访问时自动重建（只是那一刻慢一点）。

**坑**：服务器上 `vlml/database/schema/*.sql` 有一个是 **GBK**（0xa3 开头，中文注释），
本机副本恰好全是 UTF-8，所以 `read_text(encoding="utf-8")` 只在线上炸
（`UnicodeDecodeError`，整个 build 失败）。列名全是 ASCII，加 `errors="replace"` 即可。

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
5. **题目不再写死，但生成成功率有限。** `question_gen.py` 已能自动生成题目
   （生成的是**取数配方**不是答案，裁判跑一遍拿真值），并能在「没有可推进的题」
   与「全被拿下」两种情形自动生成（见五之二、八之四）。实测：
   生成的 `lotus_win_rate` 被判 100% 正确；生成的 `max_losing_streak_map`
   （难度 4）让 Voyager 首轮 0% —— 后者正是能测出学习曲线的题。
   出题点现由**服务端从图谱挑定**（`_graph_blueprint`），模型只填 WHERE 与题面，
   所以"编造表名/列名"这类错误已基本消失；剩下的失败主要是
   **口径写错**（如 gaps-and-islands 的过滤层级），会被计划校验与验题拦下并回灌。
   另外生成的题默认 `validated_target=False`（**不进掌握度**）——
   照伴学：生成 ≠ 生效，先要被确认口径。
   **已补（八之五）**：图谱节点现在自带声明并持久化。表节点带 VLML 建模文档写的
   `purpose / grain / pk / column_desc / metrics / upstream / layer`，
   维度节点带伴学式的 `difficulty / unit / skills / prerequisites / related /
   typical_misconceptions / examples / chapter / depth`。
   **没填的字段（7 个）**：`aliases` / `curriculum_tags` / `curriculum_version` /
   `exam_region` / `exam_type` 等在 MVE 里没有同构物 —— 不编、不填。

---

## 八之五、图谱补成伴学那样：把 VLML 的建模声明写进节点并持久化

上一节的结尾留了个真缺口：**MVE 的图谱节点没有"作者声明"**。伴学的 457 个知识点
每个自带 19 个字段（难度、题型、先修、典型错法…），出题器读节点声明定题型与口径，
模型只写题面。MVE 这边呢 —— `table:rounds` 的 `pk` 是空数组，派生表的粒度只写着
"聚合表"三个字，列**只有名字没有含义**。

VLML 里同层的"作者声明"一直都在，就在 `database/` 下四份建模文档里：

| 文档 | 提供什么 |
|---|---|
| `DATA_MODEL.md` | 每张表的 `**Grain:**` + `**Use cases:**`；主干血缘 `series → games → rounds → base_events` |
| `DERIVED_TABLES.md` | 7 张派生表的粒度 / 上游 Source / 关键列 / **示例查询** |
| `metadata/column_definitions.yaml` | 列级口径（141 行手写） |
| `DATA_DICTIONARY.json` | 主键 / 列类型 / flag / **指标公式**（`avg_survival_time = survival_time_sum_s / survival_time_denom`） |

新增 `vlml_schema.model_specs()` 解析这四份（**读文件，断库也在**），
`_add_model_declarations()` 写进表节点，`_add_dimension_declarations()` 给 15 个维度
节点补伴学式声明，随 `save()` 落盘 `knowledge_graph.json`。

### 声明真的被用上了吗（三处消费点）

1. **出题器不再各数各的难度。** `_graph_blueprint` 以前自己数脱敏骨架的关键字，
   而骨架常常没有 `GROUP BY` → 明明要分组的 `map_fb_conv` 被判成 1 档，排在最后。
   现在难度长在节点上（`map_fb_conv=1`、`map_win_rate=3`、`max_losing_streak=4`），
   判据与 `DIFFICULTY_RUBRIC` 同一套，只是**算一次存下来**。
2. **写 SQL 时知道每列是什么。** prompt 里现在有
   `列的含义：fb_team_won=Conversion flag (1/0)`、`主键：round_id（一行 = 每回合一行）`、
   `这张表是干什么的：first blood conversion analysis…`。
   以前只有列名 —— 于是模型把 `fb_player` 写成 `player_name`，critique 每轮都列真名，
   它照样按常识编三次。**给真名不够，得给含义。**
3. **单位。** 伴学知识点自带 `unit`。MVE 以前没有，模型交过 `0.55` 和 `55` 两种答案。
   现在维度节点带 `unit: %`，prompt 里明写"百分数，不是小数"。

### 三个坑（都踩了）

- **持久化 = 缓存过期。** 写完代码图里还是旧的 —— 因为 `_stale()` 只比 tasks.py 的指纹。
  加了 `SCHEMA_VERSION`（2）与建模文档指纹，凑成三判据：格式版本 / 题指纹 / 建模指纹。
  缺任何一个都会漏：改了代码但题没变、文档也没变的情况，只有版本号能抓住。
- **难度要从完整 SQL 数，不能从 `semantics` 数。** `semantics` 只是 SELECT 投影
  （`ROUND(AVG(fb_team_won)*100, 1) AS conv`），里面没有 `GROUP BY`。
- **`_s` 的误判。** `max_losing_streak` 里的 "losing_**s**treak" 含 `_s`，
  先判时间就把它错标成"秒"（实测就是这么错的）。判据顺序：先连败/回合，再时间。
  同理 `One row per round` 这种英文散文不能被硬翻成键列表 —— 会翻出
  「每（one × row × per × 回合）一行」。

### 实测

- 图谱：维度 15 · 工具 9 · **表 22**（多出 `ability_types`）· 边 767（+3 条建模声明的上游边）。
- 无回退：`fb_conversion_analysis` 100%、`map_rounds_split` 100%、
  难度 4 的生成题 `max_losing_streak_map` 首轮 0%（与改动前一致）。
6. 面板的求助按钮与 help_decision 展示还没做。
7. **~~SQL 路径的「编排漏条」~~ （2026-10-07 已解，见八之三）** —— 曾是 60% / 80% 长期卡死，
   根因是执行层 `[:3]` 硬截断 + critique 永远为 None + 反馈只点维度不点评分点。
   修后两题均第 1 轮 100%。此处保留是为了记下当时走过的两条**死路**：
   - 加提示要求「每个 subject 组合一条查询」→ 掉到 50%。原因不是提示错，
     是执行层只跑 3 条，写 4 条反而把重试全废在条数超限上。
   - 只改计划层的条数上限 → 没变。因为**执行层还在截**（当时误判为"模型只规划 3 条"）。
   剩下真实未解的：`corrode_collapse`（gaps-and-islands，难度 4）**不稳定** ——
   历史 5 次是 100/100/100/20/20/100，同轨迹、同提示，纯看模型当轮是否写对窗口函数。
   图谱里存着正确的参考 SQL，但**事前只渲染被截断的口径骨架**（不能给完整 SQL，
   那等于给答案）。要不要为这类题单独给"算法骨架提示"，未定。

---

## 十二、文档

- `部署.md` —— 腾讯云部署形态、Caddy 片段、自检与更新
- `知识图谱与求助闭环.md` —— 图谱三层与学习闭环的完整取证（含负结果）
- `取证通路-原版Voyager对照.md` —— 「原件进判定」vs「誊抄本进判定」
- `裁判链路记录.md` / `反馈链路与旁路学习.md` / `掌握度调研-猫娘伴学对照.md`
- `进度对照.md` / `环境体检.md` / `MVE跑通记录.md`
