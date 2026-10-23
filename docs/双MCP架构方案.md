# 双 MCP 架构方案（v2）：Voyager 当学生，maieutic 当助产士

> 2026-10-23 修订。v1 把 maieutic 当成"联网调研管道"，调研源码后确认这是**把它用成了搜索引擎**。
> maieutic 真正的价值不在 `research`，而在它外围的一整套教学机制：4 阶段会话状态机、
> 预判偏离（drift/revision/bug）、逐条追问、四级提示梯度、卡住检测。
> 用户判断「让 Voyager 在 maieutic 学一通再回来消费它」成立，且这恰好能堵住 MVE
> 现有三个漏斗（坏配方入库、真空成功、7 维度 0 闭合）。

## 结论（三句话）

1. **maieutic 不是工具，是一个有状态的教学会话**——它要求"学生"先写承诺（spec），
   做出来，被追问偏离，解释清楚，才准闭合。这条链路 MVE 现在只用了最后一步（判维度闭合）。
2. 所以 MCP 必须暴露**会话**（open / spec_submit / work_submit / answer / finalize），
   而不是一个无状态 `research(query)`。v1 的接口设计作废。
3. 角色要摆正：**Voyager 是学生，maieutic 是助产士**。Voyager 的"课程规划"= 学生决定下一步学什么；
   maieutic 负责判定"这一步是否真学会了"。技能入库的时机从"跑通"改成"maieutic 闭合"。

## 一、maieutic 的完整环节（调研结果）

源码：`~/maieutic-1`。共 **9 个环节**，分三层：备课期 → 会话期 → 全程监护。

### 1.1 备课期（出题时一次性生成，运行时不再生成）

| 环节 | 做什么 | 输出 | 位置 |
|---|---|---|---|
| ① Scaffolding | 从题目生成**三个产物**：维度清单、预判偏离、学生水平 | `spec_gate_dimensions[{id,description,rationale}]`、`expected_divergences[{category,pattern}]`、`student_level` | `src/lib/opus/prompts/scaffolding.ts:11-25`、输出 schema `:187-195` |
| ② Teaching Model | 从**教师标准解法**反推评分 rubric + 基线期望点 + 预判卡点（带四级提示） | `solutionRubric` / `BaselineExpectationSet` / `PredictedDriftPattern[]` | `src/lib/opus/prompts/teaching-model-generator.ts:80+`、类型 `src/lib/student-recovery/types.ts:15-24` |

两个细节值得单独记：

- **维度必须按"学生已学的工具"校准**（`scaffolding.ts:55-76`）：不许出"唯一正确答法需要用到学生没学过的东西"的维度；
  但允许出"假设输入合法"这类**合法承诺**。MVE 现在出 7 维度大题、第一条就卡死，正是缺这条校准。
- **`PredictedDriftPattern` 带 `recoveryHintPolicy`**（`types.ts:23`）：四种深度
  `firstHint / deeperHint / scaffoldHint / microStepHint`，在**出题时就把"卡住时该给什么级别的提示"写好了**，
  运行时只做匹配（`drift-diagnosis.ts:71-79`）。

### 1.2 会话期（四阶段状态机，`currentPhase` 推进）

| 阶段 | 学生做什么 | 系统做什么 | 位置 |
|---|---|---|---|
| **Phase 1 · spec 闸** | 写"我打算怎么做"（`reasoningText`），多轮补 | `spec-examiner` 逐条核维度 → `gaps_still_open`；**`passed` 当且仅当 gaps 为空**；最多 3 条追问；每轮存 `Phase1Iteration` | `api/session/[sid]/spec/route.ts:99-124` |
| **Phase 2 · 作答** | 写最终答案/代码 | ① **硬 oracle**（数学：sympy 符号等价）不通过**直接 409 卡在 Phase 2**，不让 LLM 参与；② `intent-diff` 对比 spec↔作答，产出 divergences | `submit/route.ts:86-98`（硬闸）、`:106-149`（intent-diff） |
| **Phase 3 · 追问回合** | 逐条解释每条 divergence | `post-hoc` 判 `alignment`（aligned/partial/diverged）+ 终判分类 | `divergence-response/route.ts:64-111` |
| **Phase 4 · 收尾** | 选择"改"或"不改"；不改也要说清楚 | 记录 `revisionChoice`，推进到 closed | `finalize/route.ts:66-129` |

**intent-diff 是整个系统最高价值的 prompt**（源码注释原话：`intent-diff.ts:5`），三分类：

| 分类 | 含义 | 对学习闭环的意义 |
|---|---|---|
| **drift** | 做得比承诺的少 / 漏了 | 要补 → 换搜索词 / 增加维度 |
| **revision** | 换了一种同样成立的做法（常常更好） | **spec/维度该被回写**（学生改主意了） |
| **bug** | 想做的没错，但机械失败 | 要修 → 同一策略重试 |

判定偏置：**drift 与 revision 难分时判 revision**（`intent-diff.ts:37-39`）——假 drift 伤信任，
假 revision 最多少一次干预。还有两条硬规则：**"一致 ≠ 偏离"**（大量反例，`:63-90`）、
**占位符规则**（spec 写 `Hello NAME!`、代码用 f-string 填值不算偏离，`:92-115`）。

### 1.3 全程监护（每个回合都跑）

| 环节 | 做什么 | 关键设计 | 位置 |
|---|---|---|---|
| ③ 卡住检测 | 纯规则打分 0~5：显式卡住语言、浅计划、重复浅提交、**同一个 gap 连续几轮没补上**、有未解决求助 | **不调用 LLM**，可每轮跑 | `student-recovery/stuck-or-drift.ts:27-78` |
| ④ 分级提示 | 卡住等级 + 匹配分 → 提示深度：`firstHint → deeperHint → scaffoldHint → microStepHint` | 越卡提示越具体，**最后拆成微步骤**，而不是同一难度重复 | `drift-diagnosis.ts:154-159` |
| ⑤ 双模式答疑 | 学生提问分两类：**interrogative**（问自己的做法 → 不给答案，只反问 1~2 句）/ **direct**（问语法/库事实 → 直接答） | 拒绝 direct 会把学生推去外部 LLM，等于工具失效 | `prompts/phase2-chat.ts:20-63` |
| ⑥ 求助登记 | 记 `HelpRequest{stateAtRequest,message,resolution:null}` | 未解决的求助直接给卡住等级 +2 | `help/route.ts:25-30` |
| ⑦ 改主意要写理由 | `amendment` + `justification` 都要 | 改答案不是"重交"，是一次可追溯的修订 | `revise/route.ts:33-37` |

### 1.4 数学场景的两道专用闸

| 闸 | 判什么 | 分级 | 位置 |
|---|---|---|---|
| Problem Understanding Gate | Phase 1 的**计划**覆盖度 | `0 / 0.4 / 0.7 / 1` 四级，0.4 以上必须有原文引用 | `prompts/math-coverage.ts:41-63` |
| Solution Process Gate | Phase 2 的**解答**是否真做完每个 checkpoint | `no / partial / yes` + `awardedScore`；**明确禁止给"I will / I plan to"计分** | `prompts/math-solution-gate.ts:35-59` |

`math-solution-gate.ts:44-46` 那条规则几乎是为 MVE 量身写的：
> 不要因为学生写了 "I will..."、"I plan to..."、"I need to..." 就给完成分。

MVE 现在的问题正是这个：模型写 `RESULT = {"answer": "..."}`（打算做什么），就被当成做完了。

## 二、MVE 现状对照：哪些已有、哪些真缺

| maieutic 环节 | MVE 现状 | 差距 |
|---|---|---|
| ① Scaffolding 维度 | ✅ `mve/maieutic_scaffold.py:181` 出 `dimensions[{id,question,why}]`，缓存到 `ckpt_maieutic/` | 缺**按已学工具校准维度数**；缺 `rationale`（内部理由不给孩子看） |
| ① 预判偏离 | 🟡 `maieutic_scaffold.py:193` 有 `expected_drifts`，但只用作"别这么答"清单 | 缺三分类标签、缺 `likelyStudentText`、缺**四级提示策略** |
| ② Teaching Model | 🟡 `bypass_learn.py` 观摩标准解法；图谱里有 rubric SQL | 缺"从标准解法反推卡点 + 提示梯度" |
| Phase 1 多轮 spec 闸 | 🟡 `maieutic_gate.check`（`mve/maieutic_gate.py:126`）判 `passed/open` | **只有二元**，没有四级覆盖度、没有逐点 `missingReason`/`followupQuestion`；**没有"提交计划→被追问→补"的多轮**（现在是每条维度各 solve 一次） |
| Phase 2 硬 oracle | ✅ `task_critic.has_real_data` / `is_vacuous` + SQL 实跑 rows>0 | 已有雏形，是 MVE 的优势 |
| Phase 2 intent-diff | ❌ **完全没有** | MVE 只有"闭合/未闭合"，不区分 drift / revision / bug |
| Phase 3 逐条追问 | ❌ **没有** | `critique` 是一整段话，不是"N 条 divergence 各带一个中性追问" |
| Phase 4 revise/finalize | ❌ **没有** | 跑通就写技能，没有"解释清楚再收尾"这一关 |
| ③ 卡住检测 | ❌ 只有 `SUBTASK_MAX_RETRIES=4` 机械重试 + "连续 2 条超时收工" | 没有分级、没有"同一 gap 连续未补"判据 |
| ④ 分级提示 | ❌ **每次重试同一个 prompt** | 这就是场景 B「7 维度 × 4 次 = 十几分钟 0 闭合」的根因 |
| ⑤ 双模式答疑 | ❌ 没有（单一 `solve()`） | — |
| ⑥⑦ 求助/修订 | ❌ 没有 | — |
| research（联网） | 🟡 `net_tools` + prompt 教模型写 for 循环 | 收进 MCP，变成"取材料"这一步 |

一句话：**MVE 已经有的（维度、闸门、真数校验）都是"判分"；缺的（偏离分类、追问、提示梯度、卡住检测）全是"教学"。**

## 三、角色分工：Voyager 当学生

```
Voyager（学生 / 学习者）                    maieutic MCP（助产士 / 教师）
─────────────────────────                  ──────────────────────────────
出"我打算怎么做"  ──── spec_submit ────▶   逐条核承诺 → gaps + 追问
补计划（用 research 取材料） ◀── gaps ───
                                           （多轮，直到 gaps 空）
真去做（数据面取数） ─── work_submit ───▶  硬 oracle + intent-diff
                                           → divergences（drift/revision/bug）
逐条解释为什么这么做 ─── answer ────────▶  post-hoc 判 alignment + 终判
决定改不改 ────────── finalize ────────▶  closed（或 blocked）
                                           ✦ 此刻才允许写技能
```

三个关键点，缺一个就退化成"查资料"：

1. **先有承诺，才有偏离**。Voyager 必须先说"我打算怎么答"，maieutic 才能判它做少了吗。
   现在 MVE 直接解题，没有"承诺"这一步，所以 intent-diff 无从下手。
2. **追问不是额外礼貌，是准入门槛**。回答不了"你为什么这么做"就不算学会 ——
   这正好把"碰巧跑通"和"真空成功"挡在技能库外。
3. **revision 要回写**。Voyager 用了更好的做法 → 被标成 revision → **维度/图谱该更新**，
   而不是当成没发生过。这是 MVE 现在完全没有的学习收益。

## 四、maieutic MCP 接口（有状态会话）

会话状态落盘（复用 `ckpt_maieutic/<scene>/`），进程重启不丢；每次调用带 `sid`。

| 工具 | 入参 | 出参 | 对应 maieutic 环节 |
|---|---|---|---|
| `maieutic_open` | `question, scene_id, unit/level?` | `{sid, dimensions[{id,question,why}], expected_drifts, recovery_policies}` | ① ② |
| `maieutic_spec_submit` | `sid, spec_text` | `{passed, coverage[{point,coverage,missing_reason,followup_question}], gaps, questions[]}` | Phase 1 |
| `maieutic_research` | `sid, dimension, query?/urls?` | `{answer, evidence[{source,chars}], closed}` | Phase 2 取材料 |
| `maieutic_work_submit` | `sid, work_text` | `{oracle:{passed,reason}, divergences[{id,category_hint,question}]}` | Phase 2 |
| `maieutic_answer` | `sid, divergence_id, response` | `{alignment, final_classification, all_answered}` | Phase 3 |
| `maieutic_finalize` | `sid, revised?` | `{closed, revision_choice, skills_eligible}` | Phase 4 |
| `maieutic_diagnose` | `sid` | `{stuck_level, reason, hint_depth, hint}` | ③ ④ |

两条设计约束：

- **`divergences[].question` 不含分类**（同 maieutic：分类只给教师看，学生只看到中性提问）。
  但 MVE 里 Voyager 既是学生又要用分类决定策略 —— 所以额外返回 `category_hint`，
  并在 prompt 里明确"这是给你调策略的，别在答案里复述"。
- **`maieutic_research` 的正文不出进程**（v1 的设计保留）：只回摘要 + 材料账。

## 五、必须同时改的四处（v1 三条 + 新增一条）

| # | 位置 | 现状 | 后果 |
|---|---|---|---|
| 1 | `voyager_vlml/consume.py:679-691` | 非 VLML 场景消费期 `register_net` + `ensure_mcp_tools` | 消费期联网全在 |
| 2 | `mve/local_agent.py:986-989` | `full["web_search"]` 无条件进命名空间 | 靠注册表过滤兜着，一处漏就露 |
| 3 | `mve/local_agent.py:1155-1157` | `run_saved` 自愈里重跑 `register_net` | **重放缺名字时会把联网又打开**（最隐蔽） |
| 4 | `mve/learn_orchestrate.py` `learn_milestone_skills` | 维度闭合即写技能 | **改为 `maieutic_finalize` 闭合才写**（否则"跑通就算学会"永远挡不住坏配方） |

第 4 条是本轮新增，也是最关键的一条：不接这一步，前面整套会话只是装饰。

## 六、退役清单

| 动作 | 对象 | 说明 |
|---|---|---|
| 删 | `fetch` / `time` / `context7` / `sequential-thinking` | 无业务调用 |
| 并入 | `neko` / `ddg` | 联网能力收进 `maieutic_research` 内部（`net_tools`），MVE 侧不再有 `web_search` 这个名字 |
| 新增 | `maieutic`（:8797） | 见第四节 |
| 不动 | 数据面（`VLMLMCPEnv` / `SceneDBEnv`） | 消费期唯一取数入口 |

## 七、改动清单（分三期，每期可独立验收）

### MVP-1 · 会话骨架 + 多轮 spec（解决"取不到内容闭合"）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `mve/maieutic_session.py` | **新**：会话状态（open/spec/work/answer/finalize），落盘 `ckpt_maieutic/` |
| 2 | `mve/research_agent.py` | **新**：gather + synthesize（v1 设计），供 `maieutic_research` |
| 3 | `mve/mcp_maieutic_server.py` | **新**：手搓 stdio server（照 `mcp_neko_server.py`），第四节 7 个工具 |
| 4 | `mve/mcp_services.sh` | SERVICES 换成 maieutic（用 `/root/mve/.venv`：需要 `voyager_vlml.llm` + `duckdb`） |
| 5 | `mve/mcp_servers.json` | 只留 maieutic |
| 6 | `mve/local_agent.py` `PROMPT_SYSTEM` | 学习期示例改成"先 open → 写计划 → 按 gaps 补 → 再动手" |

### MVP-2 · 偏离分类 + 逐条追问（解决"坏配方入库"）

| # | 文件 | 改动 |
|---|---|---|
| 7 | `mve/intent_diff.py` | **新**：移植 `intent-diff.ts` 三分类（含 bias 规则、"一致≠偏离"、占位符规则） |
| 8 | `mve/maieutic_session.py` | work_submit → 硬 oracle → intent_diff → divergences；answer → post-hoc alignment |
| 9 | `mve/learn_orchestrate.py` | 技能入库钩子挪到 `finalize` |

### MVP-3 · 卡住检测 + 提示梯度（解决"7 维度 0 闭合"）

| # | 文件 | 改动 |
|---|---|---|
| 10 | `mve/student_recovery.py` | **新**：移植 `stuck-or-drift` 规则打分 + `depthFromStuckLevel` |
| 11 | `mve/maieutic_scaffold.py` | 预判偏离补 `recovery_policy{first,deeper,scaffold,micro}` |
| 12 | `mve/learn_orchestrate.py` | 重试改成"按 `hint_depth` 换提示"，同 gap 连续 N 轮未补 → 判定卡住、换策略 |

## 八、验收线（要硬证据）

| # | 验收项 | 判据 |
|---|---|---|
| 1 | 学习期真被"教"过 | `ckpt_maieutic/<scene>/*.json` 里有 ≥2 轮 `spec_iterations`，且最后一轮 `gaps` 为空 |
| 2 | 技能入库前过了追问 | 技能条目里带 `divergence_answers`（≥1 条 alignment 记录） |
| 3 | **消费期物理无联网** | `systemctl stop mve-mcp-maieutic` 后同题仍答得出；`_tools_in_code` 里 `web_*`/`maieutic_*` 计数为 0 |
| 4 | 不再有错误页答案 | `audit_skill_replay.py --scene B` 输出 `OK 5 / 坏 0` |
| 5 | 卡住会被识别 | 人为让同一维度连续 3 轮不闭合 → 日志出现 `hint_depth=scaffoldHint` 而不是第 4 次同 prompt |

## 九、待拍板

1. **会话状态存哪**：复用 `ckpt_maieutic/<scene>/<hash>.json`（推荐，已有 `load/save_progress_iterations`）
   还是新建 `maieutic_sessions/`？
2. **`maieutic_diagnose` 要不要 LLM**：maieutic 原版是纯规则（可每轮跑、零成本）。
   建议保持纯规则，LLM 只用在 `drift-diagnosis` 的匹配上（也可先不做）。
3. **数学硬 oracle 用什么**：MVE 现在靠"SQL 实跑 rows>0 + 数值比对"（已有优势）；
   要不要像 maieutic 那样引入 sympy（`server-math-oracle`）做符号等价？
4. **数据面要不要也常驻 MCP**（v1 遗留）：唯一收益是把 `vlml_env` 的 `os.chdir`+`Path.mkdir`
   全局补丁挪出主进程。

## 附：术语对照

| maieutic | MVE |
|---|---|
| exercise / instructor prompt | 人导入的问题（`tasks.infer_topic`） |
| spec_gate_dimensions | 维度（`maieutic_scaffold.dimensions`，进图谱 `kind=dimension` 节点） |
| student's spec | Voyager 的"打算怎么做"（执行层计划） |
| code / final solution | `RESULT` / 答案文本 |
| drift / revision / bug | 无对应（**要新增**） |
| gaps_still_open | `gaps`（现为二元 `open`） |
| questions（追问） | `critique`（现为一整段话） |
| recoveryHintPolicy 四级 | 无（现为同 prompt 重试） |
| finalize / closed | 维度 `achieved=true`（现为闸门 `passed`） |
| instructor | （MVE 无；对应的"教师"= 知识图谱里的 rubric / bypass 裁判） |
