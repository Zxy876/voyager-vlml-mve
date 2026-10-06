# MVE：把 Voyager 的自演化闭环搬到 VLML 上

一个可跑的原型，回答一个问题：**Voyager 式的 agent 能不能在真实数据层上，
通过"判错 → 学标准解法 → 重做"这个循环，真的学会取数编排？**

数据层是 **VLML**（Valorant 电竞数据）：21 张表、46 个洞察 SQL、8 个 MCP 工具。
"学习"那一半的机制取自**猫娘伴学**（N.E.K.O `study_companion`）—— 它有成熟的
知识图谱、掌握度与判分契约，正好补上原版 Voyager 没有的部分。

---

## 一、三个源头，怎么拼的

| 原版 MineDojo/Voyager | 猫娘伴学 | 这里（MVE） |
|---|---|---|
| `render_system_message` 把 control_primitives **源码**拼进 system | — | 工具签名 + 返回结构 + 图谱子图 + **已学会的解法源码** |
| 产出 ```javascript 代码 | — | 产出 ```python（`async def xxx(mcp)`） |
| `process_ai_message` 用 babel 做 AST 断言 | — | `ast.parse` 断言（必须 async、唯一参数名 `mcp`）+ SQL 分层断言 |
| `SkillManager` 存 `program_code` | 掌握度 / FSRS | `skill_store` 存**解法源码** + hits/ok 有效性 |
| `retrieve_skills` 取回的是源码 | — | 检索带回 `code`，拼进 prompt |
| — | 知识图谱（82 个知识点种子，先于题目存在） | **212 个事实节点**（独立于题目，从 VLML 实查） |
| — | `deterministic_evaluators`：期望值只从服务端配方读 | 裁判 `answer_spec` + 值由代码按路径取出，**不经模型誊抄** |
| — | 讲解四段（题目解析/解题过程/答案/举一反三） | 旁路讲解同构，掌握度按"借助帮助"打 0.85 折 |

一句话概括三者的关系：**Voyager 给闭环，伴学给学与判的契约，VLML 给真实数据层。**

---

## 二、主循环

```
自适应出题  →  做题  →  判错（这就是学习信号）
                          ↓
              重试时发现知识点没掌握
                          ↓
              同一题跑 VLML0，观摩标准解法 → 存成"解法源码"进技能库
                          ↓
              再次做题（源码已进 prompt）→ 掌握度提升
```

判错本身就是信号，不需要另造。关键是把"学到了什么"**存成源码**而不是一句话——
原版存的是 `program_code`，不是"我下次要注意"这种话。

---

## 三、这一轮做了什么

### 3.1 种子层：让图谱对**所有题目**生效

之前图谱的维度节点全部从题目 rubric 派生（只有 13 个 = 5 道题），**新题的图谱块
是空字符串**。对照伴学源码才发现差距不在细节粒度，在方向：

| | 伴学 | 改之前 |
|---|---|---|
| 图谱是什么 | 82 个知识点种子，**先于题目存在** | 13 个维度，**从题目派生** |
| 题目扮演什么 | 用 `match_topics(query=题干)` 匹配焦点 | 硬绑 topic_id，没出过题 = 没有节点 |

照 `knowledge_graph_guidance.py:1269 / :481 / :1302` 与
`knowledge_graph_index.py:116 / :86 / :1073` 移植：212 个事实节点（45 洞察 +
146 工具段 + 21 张表，全部 VLML 实查）、`match_facts` 文本匹配、逐关系限流、
`plan/explain/judge/minimal` 四档分流、压成语义桶、`raw_seed_included=False`。

### 3.2 学习闭环：旁路学到的解法源码进技能库

`learn_from_referee` 此前**只在 CLI 手动跑过，主循环一次都没调**——旁路学到的
东西从来没进过技能库。现在判错 + 求助时触发，存的是**脱敏后的解法源码**
（SQL 字面量 → `'?'`，工具实参抹掉；给口径与组合，不给答案值）。

### 3.3 代码化取证：值由解释器取，不由模型誊抄

`async def xxx(mcp)` → `ast.parse` 断言 → 执行 → facts 直接由代码产出。

---

## 四、实测（含负结果）

### 4.1 有效果的

| 题 | 原路径（plan JSON） | 代码路径 |
|---|---|---|
| `kast_adr_check` | **50%**（六次全 50%，把 kast 当 kd） | **100%** |
| `pistol_eco_pattern` | 100% | 100% |
| `corrode_collapse` | 100% | 100% |
| `fb_conversion_analysis` | 80% | 80% |

`kast_adr_check` 那道题：源码里路径 `key_metrics.team.consistency.kd` 写得清清楚楚，
模型照样交 1.0（kast=109/109 未乘 100）—— 它是**用眼睛在返回的 JSON 里翻**的。
交由解释器执行路径后一次就对。

### 4.2 没效果的（同样重要）

加了三层细节，A/B 测下来**在当前题集上全都是冗余的**：

| 层 | A/B | 结论 |
|---|---|---|
| section 层（"取返回里的哪一段"） | 开/关各 3 次，全 100% | 冗余 |
| 洞察层（"这段由哪些 SQL 拼成"） | 开/关各 3 次，全 100%，且模型一次 SQL 都没多写 | 冗余 |
| 种子层（212 个事实节点） | 新题开/关各 3 次，全 50%，失败模式一样 | 解决了"有没有"，没解决"有没有用" |

**真正被模型用上的，仍然只有最早那一层：表结构 + 口径 + 参数陷阱**
（那次 A/B：无图谱 0% → 有图谱 100%）。

行为证据：洞察层开着时，四次跑的 SQL 数全是 0、`calls` 只有一个工具——
洞察行点名的表从未进入模型任何行为。

### 4.3 一个必须记下来的同构边界

**SQL 类不走代码路径。** 先按"全部走代码"跑，corrode 从 100% 掉到 0%。

理由不是调参：原版的 JS 是"调用 bot 上的 API"，对应这里调 MCP 工具；而 SQL 是
`query_sql` 的**参数字符串**，对应原版 `bot.chat("...")` 的参数——**原版从不把
chat 的字符串当程序来学**，技能库里也没有这一类技能。

所以按口径源分流（`_is_tool_path_task`）：`answer_spec.tool` 走代码路径，
`answer_spec.sql` 走原路径（那条有图谱骨架与分层断言，正是 SQL 需要的护栏）。

---

## 五、跑法

```bash
# 1) 依赖与凭据
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt       # 见下
echo 'ZHIPU_API_KEY=xxx' > mve/.env

# 2) 环境体检（VLML 数据层要就位，见第六节）
python mve/verify_env.py

# 3) 建知识图谱
python mve/knowledge_graph.py --build

# 4) 跑一轮
python mve/run_mve.py --llm --topic pistol_eco_pattern --rounds 3
python mve/run_mve.py --llm --topic kast_adr_check --rounds 3 --help-policy=always

# 5) 面板
python mve/dashboard.py
```

开关（都能关掉做 A/B）：

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `MVE_GRAPH` | 1 | 图谱注入总开关 |
| `MVE_GRAPH_SECTION` / `_STAGE` / `_INSIGHT` | 1 | 图谱内各层单独开关 |
| `MVE_GRAPH_SEED` | 1 | 种子层兜底（老路径拿不到时按题干匹配） |
| `MVE_ASSERT` | 1 | 硬校验（关掉才能测出提示层的边际效果） |
| `MVE_ACTION_CODE` | 0 | 代码化取证（仅工具路径类题生效） |
| `MVE_DEBUG_PROMPT` | 0 | 落盘 prompt 到 /tmp |

---

## 六、仓库里不包含什么

| 目录 | 为什么 |
|---|---|
| `vlml/` | VLML 数据层，独立 git 仓库（18M）。本仓库的图谱构建依赖它，请自行放置 |
| `voyager-fork/` | 原版 Voyager 本地 fork，只做对照阅读 |
| `study_companion/` | 伴学插件副本，只读参照（不修改、不分发） |
| `mve/.env` | API 凭据 |

代码里所有凭据都走 `os.getenv`，**没有任何硬编码**。

---

## 七、已知缺口

1. **段内定位**：`key_metrics` 下还有 `team`/`opponent` 两层，模型会翻错层。
   这不在伴学图谱的职责内（它管知识点关系，不管工具返回结构），要自己补——
   候选是把工具返回的结构骨架（有哪些子键、每个是 `num/denom` 还是标量）
   作为契约进图，仍然只给结构不给数值。
2. **SQL 类的代码化**：目前 SQL 类走原路径。要让代码路径也能覆盖，需要把
   `_check_sql_layering` 那套分层断言搬到代码里的 SQL 上。
3. `procedure_step` 边默认不建（`--build --observe` 才跑裁判补实测边）。
4. 面板的求助按钮与 help_decision 展示还没做。
5. 题集太小（6 道），且 4 道首轮就满分 —— 测不出学习曲线。要测"学"，得先有梯度。

---

## 八、文档

- `知识图谱与求助闭环.md` —— 本次全部改动的取证与实测（12 节，含负结果）
- `取证通路-原版Voyager对照.md` —— "原件进判定" vs "誊抄本进判定"
- `裁判链路记录.md` / `反馈链路与旁路学习.md` / `掌握度调研-猫娘伴学对照.md`
- `进度对照.md` / `环境体检.md` / `MVE跑通记录.md`
