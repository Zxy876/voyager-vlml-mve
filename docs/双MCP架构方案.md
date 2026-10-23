# 双 MCP 架构方案（v3）：Voyager 当学生，maieutic 当助产士

> 2026-10-23 第三次修订。v2 调研的是 `~/maieutic-1`——**那是改过的版本**（加了数学闸、research、
> student-recovery）。本版按 **`~/ maieutic_fork/maieutic`（main 分支，原版）** 重新调研，并把
> 你的增量单独列一节。结论没变，但**环节归属变了**：v2 里有 4 个环节其实是你的定制，不是原版设计。
>
> 这个区分很关键：**原版是"代码作业"型伴学，你的增量是"数学"型伴学** —— 正好对应 MVE 的两类场景。

## 结论（四句话）

1. **maieutic 不是工具，是一条 4 阶段会话状态机**：写承诺（spec）→ 作答 → 被追问偏离 → 解释清楚才闭合。
   MVE 现在只用了最后一步（判维度闭合）。
2. **原版 6 个 prompt 组成闭环**：scaffolding（备课）→ spec-examiner（Phase 1）→
   intent-diff（Phase 2）→ post-hoc（Phase 3）→ phase2-chat（Phase 2 答疑）→ live-summary（教师看板）。
3. **两个版本对应 MVE 两类场景**：fork 原版是"说好的行为 vs 写出的代码"（→ 场景 A 取数/SQL）；
   你的数学增量是"说好的解法 vs 写出的解答"（→ 场景 B 数学）。两套都要，按场景选。
4. 所以 MCP 必须暴露**有状态会话**，不是无状态 `research(query)`（v1 设计作废）；技能入库时机从
   "跑通"改成"`finalize` 闭合"。

## 一、maieutic 原版（fork）的完整链路

### 1.1 六个 prompt 各管一段

| # | 环节 | 何时 | 输入 → 输出 | 位置 |
|---|---|---|---|---|
| 1 | **scaffolding** | 备课（出题时一次） | 题目 → `spec_gate_dimensions[{id,description,rationale}]` + `expected_divergences[{category,pattern}]` + `student_level` | `lib/opus/prompts/scaffolding.ts:11-25`、schema `:187-195` |
| 2 | **spec-examiner** | Phase 1，多轮 | 学生 spec + 历史轮次 → `gaps_addressed / gaps_still_open / emergent_gaps / questions / passed` | `lib/opus/prompts/spec-examiner.ts:7-22`、输出 schema `:138-146` |
| 3 | **intent-diff** | Phase 2 提交时 | spec + 最终代码 + 中途修订 → `divergences[{三分类, 预测, 中性追问, 双侧证据}]` | `lib/opus/prompts/intent-diff.ts:16-153` |
| 4 | **post-hoc** | Phase 3 每条回答 | 初判 + 预测的理由 + 学生实际回答 → `alignment` + `final_classification` | `lib/opus/prompts/post-hoc.ts:7-44` |
| 5 | **phase2-chat** | Phase 2 随时 | 学生提问 → `{mode: interrogative\|direct, response}` | `lib/opus/prompts/phase2-chat.ts:20-63` |
| 6 | **live-summary** | 教师看板（每轮） | 会话全态 + 近期事件 → 一句话认知摘要 + flags | `lib/opus/prompts/live-summary.ts:14-65` |

### 1.2 四阶段状态机（`currentPhase` 1→2→3→4）

| 阶段 | 准入 | 出口 | 位置 |
|---|---|---|---|
| **1 · spec 闸** | — | `passed = gaps_still_open.length === 0`（`:85`）→ 进 Phase 2；每轮 `appendPhase1Iteration` | `api/session/[sid]/spec/route.ts:85-99` |
| **2 · 作答** | 有 `finalSpecText` | 提交 → intent-diff → **无偏离直接跳 Phase 4**，有偏离进 Phase 3 | `submit/route.ts:64-96` |
| **3 · 追问** | 有 divergences | 每条必须回答（`studentResponse` 非 null）；全答完置 `completedAt` | `divergence-response/route.ts:64-111`、`lib/sessions.ts:413-455` |
| **4 · 收尾** | `completedAt` 非空 | 记录"改 / 不改"（`revisionChoice`），不阻塞 | `finalize/route.ts`、`lib/sessions.ts:461+` |

### 1.3 原版里有四条设计，v2 漏了，但对 MVE 很关键

**① 教师配置维度 vs LLM 生成维度（双轨）**

`Phase1Data` 里有 `instructorConfiguredDimensionsAddressed`（`schemas.ts:169-182`），
`appendPhase1Iteration` 把它按**跨轮累计并集**维护（`sessions.ts:253-272`）。
对应 prompt 口径（`spec-examiner.ts:13-22`）：

> 维度是"spec 必须回答的承诺清单"；**配置清单答完就能过闸**，你额外发现的 emergent gap 不能单独拦门。

MVE 现状：维度全由 LLM 生成（`maieutic_scaffold.py:181`），**没有人工可配轨道**。
而 MVE 恰恰有"人导入题"（`imports`）—— 人工导入时该带人工维度，自动生成的只是兜底。

**② 承诺是可演进的，但每次改动要写理由**

`IN-SESSION REVISIONS RULE`（`intent-diff.ts:140-153`）：

> 有效意图 = 原始 spec **按顺序被每一次修订修改之后**的结果。
> 实现修订意图的代码**不算偏离** —— 学生已经披露了改动、给了理由。
> 只对"修订后仍未兑现"的部分报偏离。

配套 `revise` 接口要求 `amendment + justification` 两个字段（`revise.ts:5-8`）。
MVE 现状：完全没有"承诺演进"—— 一条技能被同名覆盖（`skill_store.add` 的 Rewriting）就悄悄换了，没有理由记录。

**③ 学习信号冻结，修订不重写它**

`Phase3Data` 注释（`schemas.ts:270-276`）：

> 全部偏离答完之后学生可选择修订；**phase2.finalCode 与偏离分类保持冻结**——
> "revision is a coda, not a rewrite of the learning signal."

`recordFinalRevision` 也只写 `phase3.revisedCode`，不动 `finalCode`、不动分类（`sessions.ts:457-473`）。
这条对 MVE 是硬约束：**技能条目一旦记录了"当时怎么答、被追问什么、怎么解释的"，后来的重做不能覆盖这段历史**，
否则掌握度曲线和归因全失真。

**④ live-summary 的 flags 里有现成的卡住判据**

`live-summary.ts:43-51` 的 5 个 flag：
`help_requested` / `alignment_failure` / `proactive_revision` / `stuck_signal` / `high_performer`。
其中 `stuck_signal` 的定义正是 MVE 缺的：

> 该有进展的阶段 **>5 分钟无状态变化**，或 **同一个 gap 连续 3+ 轮没补上**。

## 二、你的增量（maieutic-1 相对 fork 多了什么）

按 git 文件清单 diff 核实，`~/maieutic-1` 是 fork + 以下 6 类：

| 类别 | 文件 | 作用 | 对 MVE 的意义 |
|---|---|---|---|
| **数学三件套** | `lib/opus/prompts/math-coverage.ts`（四级覆盖 0/0.4/0.7/1）、`math-solution-gate.ts`（checkpoint 评分，**禁止给 "I will…" 计分**）、`lib/server-math-oracle.ts`（sympy 符号等价，不通过直接 409） | 把"数学解答"变成可判的 | **场景 B 直接用** |
| **数学 prompt 规则段** | `scaffolding.ts`（MATH SPEC-GATE CALIBRATION）、`spec-examiner.ts`（mathSpecExaminerRules）、`intent-diff.ts`（mathIntentDiffRules：符号等价通过后只剩解释覆盖） | 防止把数学题当编程题判（"不要要求 SymPy / 不要问 API"） | **场景 B 直接用** |
| **数学答疑人设** | `lib/opus/prompts/phase2-math-chat.ts` | Phase 2 换成"数学复盘引导师" | 场景 B |
| **联网调研** | `lib/research/agent.ts`（plan→gather→synthesize）、`lib/research/dashscope-search.ts` | 真正取材料 | 收进 `maieutic_research` |
| **卡住检测** | `lib/student-recovery/{stuck-or-drift,drift-diagnosis,drift-map,solution-frontier}.ts` | 纯规则打分 0-5 + **四级提示梯度** `first/deeper/scaffold/micro` | **治 MVE「7 维度 0 闭合」** |
| **备课增强** | `lib/opus/prompts/teaching-model-generator.ts` | 从**教师标准解法**反推 rubric + 基线期望点 + `PredictedDriftPattern`（含四级提示策略） | MVE 的 `bypass_learn` 可以接 |
| 其他 | `lib/aplos-date-index.ts`（知识库注入）、`lib/prototype-runtime.ts`（可运行原型）、`lib/ingest/*`（URL/GitHub/OCR 导入） | — | 图谱注入可参考 aplos |

**所以 v2 那份"9 个环节"里，research / student-recovery / teaching-model / 数学三件套 这 4 类是你的定制。**
原版只有"承诺→作答→偏离→追问→收尾"这一条。

## 三、两个版本 ↔ MVE 两类场景

| | fork 原版 | 你的数学增量 |
|---|---|---|
| "承诺"是什么 | 程序行为（"空输入返回 0"） | 解题计划（"用乘积法则，不需要链式"） |
| "作答"是什么 | 代码 | 解答过程 |
| 硬闸 | 无（只靠 LLM 判偏离） | **sympy 符号等价**（不过直接 409） |
| 追问重点 | 行为与承诺不一致 | 解释覆盖（用了什么规则 / 顺序 / 边界） |
| **映射到 MVE** | **场景 A（VLML 取数）**：承诺=取数方案（查哪张表、怎么过滤），作答=SQL + 答案，硬闸=SQL 实跑 rows>0 | **场景 B（数学）**：承诺=解题计划，作答=解答，硬闸=数值/表达式校验 |

这条对应关系让 MVE 不用二选一：**同一套会话骨架，两套"承诺校验规则"按场景挂**。

## 四、MVE 现状对照

| 环节 | MVE 现状 | 差距 |
|---|---|---|
| scaffolding 维度 | ✅ `maieutic_scaffold.py:181` 出 `dimensions[{id,question,why}]`，缓存 `ckpt_maieutic/` | 缺**人工可配轨道**（原版的 instructor dimensions）；缺按"已学工具"校准维度数 |
| 预判偏离 | 🟡 `maieutic_scaffold.py:193` 有 `expected_drifts` | 只当"别这么答"清单用；缺三分类、缺四级提示策略 |
| Phase 1 多轮 spec | 🟡 `maieutic_gate.check`（`mve/maieutic_gate.py:126`）二元 `passed/open` | 缺"提交计划→被追问→补"的**多轮**（现在每条维度各 solve 一次）；缺跨轮累计"已答维度" |
| Phase 2 硬闸 | ✅ `task_critic.has_real_data` / `is_vacuous` + SQL 实跑 | 是 MVE 的优势，保留 |
| intent-diff | ❌ 完全没有 | 不区分 drift / revision / bug |
| 承诺演进（revise） | ❌ 没有 | 技能同名覆盖无理由记录 |
| Phase 3 逐条追问 | ❌ 没有 | `critique` 是一整段话 |
| Phase 4 finalize | ❌ 没有 | 跑通就写技能 |
| 学习信号冻结 | ❌ 没有 | 技能可被覆盖，历史归因失真 |
| 卡住检测 / 提示梯度 | ❌ 同 prompt 重试 4 次 | 「7 维度 × 4 次 = 0 闭合」的根因 |
| 双模式答疑 | ❌ 单一 `solve()` | — |
| live-summary 式认知摘要 | ❌ 面板只有状态与日志 | 缺"现在卡在哪、要不要介入"的一句话 |

**一句话**：MVE 已有的（维度、闸门、真数校验）都是"判分"；缺的（承诺多轮、偏离分类、追问、提示梯度、冻结）全是"教学"。

## 五、Voyager 当学生：会话时序

```
Voyager（学生）                              maieutic MCP（助产士）
──────────────                              ─────────────────────
open(question)          ────────────────▶   出维度 + 预判偏离 + 提示策略
                        ◀──── dimensions ──
写"我打算怎么答"（计划）  ── spec_submit ──▶  逐条核 → gaps + 追问
按 gaps 补（research）   ◀──── gaps ──────  （多轮；累计已答维度）
真去做（数据面取数/SQL）  ── work_submit ──▶  硬闸（rows>0 / 数值校验）
                                            → intent-diff 三分类
                        ◀── divergences ───
逐条解释为什么这么做      ── answer ───────▶  post-hoc 判 alignment
决定改不改              ── finalize ─────▶  closed（学习信号冻结）
                                            ✦ 此刻才允许写技能
```

三个不能省的约束：

1. **先承诺，才有偏离**。没写计划就上交，intent-diff 无从对比 → 只能退化成"判对错"。
2. **追问是准入门槛，不是礼貌**。答不出"为什么这么做"就不算学会 —— 这条正好把"碰巧跑通"和"真空成功"挡在库外。
3. **revision 要回写、要留理由**。Voyager 换了更好的做法 → 记为 revision（附理由）→ 维度/图谱更新；
   但**原答案与当时的分类冻结不动**。

## 六、maieutic MCP 接口（有状态）

状态落 `ckpt_maieutic/<scene>/<sid>.json`，进程重启不丢。

| 工具 | 入参 | 出参 | 对应 |
|---|---|---|---|
| `maieutic_open` | `question, scene_id, dimensions?` | `{sid, dimensions, expected_divergences, hint_policies}` | 备课 |
| `maieutic_spec_submit` | `sid, spec_text` | `{passed, gaps, questions[], addressed[]}` | Phase 1 |
| `maieutic_revise` | `sid, amendment, justification` | `{ok, effective_intent_digest}` | 承诺演进 |
| `maieutic_research` | `sid, dimension, query?/urls?` | `{answer, evidence[{source,chars}], closed}` | 取材料 |
| `maieutic_work_submit` | `sid, work_text` | `{gate:{passed,reason,rows}, divergences[{id,category_hint,question,evidence}]}` | Phase 2 |
| `maieutic_answer` | `sid, divergence_id, response` | `{alignment, final_classification, all_answered}` | Phase 3 |
| `maieutic_finalize` | `sid, revised?` | `{closed, revision_choice, frozen_at, skills_eligible}` | Phase 4 |
| `maieutic_diagnose` | `sid` | `{stuck_level, reason, hint_depth, hint}` | 监护 |

设计约束：

- `divergences[].question` **不含分类**（同原版：分类给教师，学生只看到中性提问）；但额外给 `category_hint`，
  并在 prompt 里写明"只用于你调策略，别在答案里复述"。
- `maieutic_research` 的**正文不出进程**，只回摘要 + 材料账。
- `work_submit` 的 `gate` 是**硬闸**（场景 A：SQL 实跑 rows>0；场景 B：数值/符号校验），
  不过就直接返回、不生成 divergences、不允许 finalize。

## 七、四处必改（v1 三条 + 新增一条）

| # | 位置 | 现状 | 后果 |
|---|---|---|---|
| 1 | `voyager_vlml/consume.py:679-691` | 非 VLML 场景消费期 `register_net` + `ensure_mcp_tools` | 消费期联网全在 |
| 2 | `mve/local_agent.py:986-989` | `full["web_search"]` 无条件进命名空间 | 靠注册表过滤兜着 |
| 3 | `mve/local_agent.py:1155-1157` | `run_saved` 自愈重跑 `register_net` | 重放缺名字时把联网又打开（最隐蔽） |
| 4 | `mve/learn_orchestrate.py` `learn_milestone_skills` | 维度闭合即写技能 | **改到 `maieutic_finalize` 闭合才写** |

## 八、退役清单

| 动作 | 对象 |
|---|---|
| 删 | `fetch` / `time` / `context7` / `sequential-thinking`（无业务调用） |
| 并入 | `neko` / `ddg` → `maieutic_research` 内部（`net_tools`），MVE 侧不再出现 `web_search` 这个名字 |
| 新增 | `maieutic`（:8797） |
| 不动 | 数据面（`VLMLMCPEnv` / `SceneDBEnv`） |

## 九、改动清单（三期）

### MVP-1 · 会话骨架 + 多轮 spec

| # | 文件 | 改动 |
|---|---|---|
| 1 | `mve/maieutic_session.py` | **新**：8 个动作的状态机，落 `ckpt_maieutic/`；`frozen` 字段保证学习信号不被覆盖 |
| 2 | `mve/research_agent.py` | **新**：gather + synthesize（材料账 + 只写有证据的） |
| 3 | `mve/mcp_maieutic_server.py` | **新**：手搓 stdio server（照 `mcp_neko_server.py`），第六节 8 个工具 |
| 4 | `mve/mcp_services.sh` / `mcp_servers.json` | SERVICES 换成 maieutic（`/root/mve/.venv`：需 `voyager_vlml.llm` + `duckdb`） |
| 5 | `mve/local_agent.py` `PROMPT_SYSTEM` | 学习期示例：open → 写计划 → 按 gaps 补 → 再动手 |

### MVP-2 · 偏离分类 + 追问 + 承诺演进

| # | 文件 | 改动 |
|---|---|---|
| 6 | `mve/intent_diff.py` | **新**：移植三分类 + bias 规则 + "一致≠偏离" + 占位符规则 + **IN-SESSION REVISIONS RULE** |
| 7 | `mve/maieutic_session.py` | work_submit → 硬闸 → intent_diff；answer → post-hoc；revise 记 amendment+justification |
| 8 | `mve/learn_orchestrate.py` | 技能入库钩子挪到 finalize；技能条目附 `divergence_answers` 且不可覆盖 |
| 9 | `mve/skill_store.py` | 同名覆盖改为**追加历史版本**（冻结旧版），不再 Rewriting 掉 |

### MVP-3 · 卡住检测 + 提示梯度

| # | 文件 | 改动 |
|---|---|---|
| 10 | `mve/student_recovery.py` | **新**：`stuck-or-drift` 规则打分（含"同 gap 连续 N 轮"）+ `depthFromStuckLevel` |
| 11 | `mve/maieutic_scaffold.py` | 预判偏离补 `recovery_policy{first,deeper,scaffold,micro}` |
| 12 | `mve/learn_orchestrate.py` | 重试按 `hint_depth` 换提示；同 gap 连续未补 → 判定卡住、换策略 |

## 十、验收线

| # | 验收项 | 判据 |
|---|---|---|
| 1 | 学习期真被"教"过 | `ckpt_maieutic/<scene>/*.json` 有 ≥2 轮 spec_iterations，末轮 `gaps` 为空 |
| 2 | 技能入库前过了追问 | 技能条目带 `divergence_answers`（≥1 条 alignment） |
| 3 | 学习信号冻结 | 同名技能重做后，旧条目的 `answers/classification` 未被改写 |
| 4 | **消费期物理无联网** | `systemctl stop mve-mcp-maieutic` 后同题仍答得出；`_tools_in_code` 里 `web_*`/`maieutic_*` 计数为 0 |
| 5 | 不再有错误页答案 | `audit_skill_replay.py --scene B` → `OK / 坏 0` |
| 6 | 卡住会被识别 | 同一维度连续 3 轮不闭合 → 出现 `hint_depth=scaffoldHint`，而不是第 4 次同 prompt |

## 十一、待拍板

1. **维度双轨**：人导入题时是否允许手写维度（原版 `instructorConfiguredDimensionsAddressed` 的做法）？
   我建议**允许**——这正是 MVE `imports` 与自动生成的分界。
2. **`maieutic_diagnose` 要不要 LLM**：原版是纯规则，建议保持（可每轮跑、零成本）。
3. **场景 B 的硬闸用什么**：SQL 实跑（现有）还是加数值/符号校验（你 maieutic-1 里的 sympy oracle）。
4. **数据面要不要也常驻 MCP**：唯一收益是把 `vlml_env` 的 `os.chdir` + `Path.mkdir` 全局补丁挪出主进程。
5. **`live-summary` 式认知摘要**要不要进 MVE 面板（一句话 + 5 个 flags）。建议 MVP 之后再做。

## 附：术语对照

| maieutic（fork 原版） | MVE |
|---|---|
| exercise | 人导入/自动生成的题（`tasks.infer_topic`） |
| spec_gate_dimensions（含 instructor 配置） | 维度（`maieutic_scaffold.dimensions`，图谱 `kind=dimension`） |
| student's spec | Voyager 的"打算怎么答" |
| code / finalCode | `RESULT` / SQL / 答案文本 |
| revise(amendment, justification) | 无（技能同名覆盖，无理由） |
| drift / revision / bug | 无（只有 闭合/未闭合） |
| gaps_still_open | `gaps`（现为二元 `open`） |
| questions | `critique`（一整段话） |
| revisionChoice / frozen signal | 无 |
| live-summary flags（含 stuck_signal） | 无（面板只有状态与日志） |
