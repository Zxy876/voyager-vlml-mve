# 双 MCP 架构方案（v4）：maieutic 只能挂在执行域里

> 2026-10-23 第四次修订。v3 把 maieutic 设计成**有状态会话对端**（8 个工具、一人一句、
> `open → spec_submit → answer → finalize`），那是照人类学习者画的：人在会话里会等，Voyager 不会。
> 这版按你的硬约束重写——**学习主循环不能断，也不能被干预**——回到原版
> `voyager_original/voyager/voyager.py` 解剖，先定死"哪些位置不许动"，再谈 maieutic 挂哪儿。
>
> 配套：`评审对照-v4原型.md` —— 本期哪几段真点得动、哪些是裁剪/没做、可见边界规则、
> 「当时怎么答/被追问什么」的账记在哪。评审看那份，不看这份。

## 结论（五句话）

1. **主循环的原子是 `step()`，不是"一问一答"**。`learn() → rollout() → step()` 三层各只有一个出口，
   任何"等外部返回再决定下一步"的接入点都只能插在 step 之间 → 那就是断。
2. **唯一合法的接入点是 `env.step(code, programs=...)` 的执行域**（`voyager.py:226-229`）——
   和 control primitive、已学技能同一个位置。工具必须是 **code 里能同步调用的函数**，
   一个 step 内可以调任意次，不占"轮"。
3. 所以 maieutic 的"多轮追问"必须**折叠进同一个 program 的循环**，不许跨越 step。
   没闭合 = 返回非空 gaps（或抛异常）→ critic 判 false → Voyager 自己的 retry 就是追问环。
   **一条环，不是两条。**
4. **判官唯一**。spec-examiner 降级为「材料完备性检查器」，只出 `gaps/questions`，永不出 `success`。
   MVE 已经把这个位置定死了（`mve/task_critic.py:146-160` 把 `maieutic_gate` 装在 critic 里），v4 不动它。
5. 接口从 8 个会话工具收敛到 **3 个纯函数 + 1 个落盘注记**：
   `maieutic_research` / `maieutic_examine` / `maieutic_diagnose`（+ `maieutic_note`）。
   没有 sid 生命周期，没有 `finalize`——**`finalize` 就是 `add_new_skill`**。

---

## 一、原版主循环解剖（取证）

### 1.1 四层，每层一个出口

| 层 | 位置 | 循环 | 出口条件 | 谁决定"下一轮做什么" |
|---|---|---|---|---|
| `learn()` | `voyager.py:326-385` | `while True` | 迭代上限（`:327`）、`_stop_requested`（`:332-334`，**任务边界**优雅退出） | `curriculum_agent.propose_next_task`（`:335-339`） |
| `rollout()` | `:298-304` | `while True: step()` | `done` | 没人——`done` 由 step 返回 |
| `step()` | `:214-296` | 无循环 | `done = iter >= task_max_retries or success`（`:277-280`） | 无 |
| `env.step()` | `:226-229` | 无（JS 侧一次 eval） | 返回 events | 无 |

注意 `learn()` 里那个 `_stop_requested`：**停止也只在任务边界生效**（`:332-334`）。
原版连"停"都不肯打断一个 step——这就是"不能断"的原始尺度。

### 1.2 `step()` 的五步（`voyager.py:214-296`）

```python
ai_message = self.action_agent.llm(self.messages)                      # :217
parsed = self.action_agent.process_ai_message(ai_message)              # :222
code   = parsed["program_code"] + "\n" + parsed["exec_code"]           # :225
events = self.env.step(code, programs=self.skill_manager.programs)     # :226-229  ← 执行域
success, critique = self.critic_agent.check_task_success(..., max_retries=5)  # :232-238
self.messages = [render_system_message(skills=new_skills),             # :256-261
                 render_human_message(..., critique=critique)]         # :262-268
```

一个 step = **一次 LLM 生成 + 一次代码执行 + 一次判分**。中间没有"等"的位置。

### 1.3 三个唯一（信息论意义上的窄接口）

| 唯一 | 位置 | 含义 |
|---|---|---|
| **唯一成功出口** | `:286-291` → `:369-370` `add_new_skill(info)` | 只有 `success` 才把 `info["program_code"] / ["program_name"]` 写进技能库。skill 是**具名 async 函数**（`agents/skill.py:115`），从此进 `programs` 被执行域看得见（`:55-62`） |
| **唯一回灌通道** | `:262-268` `render_human_message(..., critique=critique)` | critic 的 `critique` 是唯一能影响下一轮的东西。`prompts/action_template.txt` 把 `Critique: ...` 列成一等字段 |
| **唯一技能入口** | `:226-229` `programs=`（**全部**技能）+ `:256-260` `retrieve_skills`（**top-k** 进 prompt） | prompt 里只给 5 条（`skill_manager_retrieval_top_k=5`，`:46`），执行域里给全部。**"技能是新的 primitive"指执行域，不是指 prompt** |

### 1.4 「不能断 / 不能干预」的形式定义

**不能断**（结构约束）：

| # | 不许 | 后果 |
|---|---|---|
| B1 | 在 `step()` 之间插入需要等待**外部主体**返回的调用（人类确认、另一个 agent 的回合、会话轮询） | `rollout()` 的 `while True` 变成事件调度器，`done` 不再由一次执行决定 |
| B2 | 让 `env.step()` 阻塞在"等一个跨进程会话"上 | 上层 `EXEC_TIMEOUT`（`learn_orchestrate.py:178`，90s）会把整轮判死，且 Python 杀不掉线程（`local_agent.py:254-262`） |
| B3 | 新增第三层循环 | 现已有三层（见 §3.5），再加一层 = 一维度 4×2×N 次 LLM 调用，颗粒无收 |

**不能干预**（语义约束）：

| # | 不许 | 对应代码 |
|---|---|---|
| P1 | 改 `messages` 的构造（`render_system_message` / `render_human_message`） | `agents/action.py:78-103`、`action.py:105-202` |
| P2 | 替 critic 判 `success` | `agents/critic.py:119-139` |
| P3 | 替 curriculum 选下一个 task | `agents/curriculum.py` `propose_next_task` |
| P4 | 改 `done` 的判据 | `voyager.py:277-280` |
| P5 | 绕过 `info["program_code"]` 这条路上技能库 | `voyager.py:286-291, 369-370` |

**这 9 条就是本方案的验收边界。** maieutic 的所有能力都只能从"执行域里一个可调函数"这一条缝进来。

---

## 二、v3 为什么必然打断主循环

| v3 工具 | 为什么非得插在 step 之间 | v4 处置 |
|---|---|---|
| `maieutic_open` | 拿维度清单是"下一步做什么"的前提 | **删**。维度由 curriculum 侧 `maieutic_scaffold` 出（`mve/maieutic_scaffold.py:181`），进 `task`/`context`，不进 step |
| `maieutic_spec_submit` | 交计划 → 等判 → 再改计划，天然多回合 | **折叠进 program**：`examine` 返回 gaps，程序内 `for` 循环改 SPEC |
| `maieutic_revise` | 承诺演进的载体 | **降级**为 `maieutic_note(kind="revision")`，一次函数调用 |
| `maieutic_work_submit` | 交作答 → 等硬闸 | **就是 `RESULT`**。硬闸由 `task_critic` 的真数判据判（`_has_real` → `critic.has_real_data`；**本期没有「每条 SQL 必须返回行」的闸**），原版位置不动 |
| `maieutic_answer` | Phase 3 逐条解释偏离 | **降级为 critique 回灌**：追问进 `critique` 字段，下一 step 的 human message 自带 |
| `maieutic_finalize` | 闭合后写技能 | **删**。`finalize` 与 `add_new_skill` 是同一件事，重复入口 |
| `maieutic_research` | 取材料 | **保留**（执行域） |
| `maieutic_diagnose` | 卡住检测 | **保留**（执行域，纯规则） |

**净结果：8 → 3 + 1。** 被删的 5 个不是"功能丢了"，是**被主循环自己吸收了**：
它们本来就是在描述"谁在什么时候决定下一轮"，而在 Voyager 里那个"谁"只能是
`rollout` 的 `done` 和 `learn` 的下一圈。

---

## 三、接法：只从执行域进

### 3.1 原版这里放的是什么

`skill_manager.programs`（`agents/skill.py:55-62`）= **全部技能代码 + 全部 control primitive**，
拼成一整段 JS，`env/mineflayer/index.js:256` 用 `eval(programs + code)` 把新代码放在**同一作用域**里跑。
所以"技能调技能"是语言级的事（trial1 的 51 条技能里 7 处技能调技能）。

### 3.2 MVE 的同构已经在了

| 原版 | MVE | 位置 |
|---|---|---|
| `programs` 字符串（全部技能 + primitives） | `_ns_base()` 的 `full` 字典 + `_make_skill_fn()` | `mve/local_agent.py:1011-1108`、`:303-356` |
| 执行域同名调用（`eval(programs+code)`） | `exec(code, ns)`（受限命名空间，无 `import`） | `mve/local_agent.py:233-262` |
| 技能 = 具名 async 函数 | `skill_<name>()` 注入执行域 | `mve/local_agent.py:286-300` |
| `retrieve_skills` top-k 进 prompt | `skills=` 参数 / `_skill_names` 段 | `mve/local_agent.py:483-500` |

**结论：maieutic 只要做成"注册进 `actions` + 注入 `_ns_base`"的一组函数，就已经在正确位置了。**
不需要新机制。

### 3.3 三类能力一张表

| 类 | 例子 | 注册 | 组开关 | 谁调 |
|---|---|---|---|---|
| 数据面 | `query_sql` / `mcp_vlml_*` | `register_local` / `expose_as_actions` | 本机能力 / MCP 远端工具 | 模型写的代码 |
| 联网面 | `web_search` / `web_fetch` | `register_net`（`actions.py:405-431`） | 联网能力 | 模型写的代码 |
| **助产面（新）** | `maieutic_research` / `examine` / `diagnose` / `note` | `register(..., origin="maieutic")` | **助产士** | 模型写的代码（同一个 ns） |

联网面在助产面里是**内部调用**：`maieutic_research` 自己用 `net_tools`，
模型侧连 `web_search` 这个名字都拿不到（组关着 → `enabled_entries` 过滤掉，`actions.py:563`）。
这比 v3「把 neko/ddg 并进 maieutic」更彻底：**管道名不出现在模型视野里**（正是 2026-10-22
那批坏配方的根因——技能学的是搜索后端名，换后端即废）。

### 3.4 一个 program 长什么样（模型写出来的东西）

```python
# 学一条维度时，模型在**一次 exec** 里跑完"取材料 → 看差什么 → 补 → 再取"
SPEC = "我的方案：先查 games 表拿比赛 id，再查 ext_* 取首血事件"
m = maieutic_research(SPEC, need=["首血时间", "队伍名", "赛事号"])
g = maieutic_examine(SPEC, m["gathered"])          # 只核材料，不判成功
if g["open"]:                                       # 差东西 → program 内补一轮（最多 1 次）
    SPEC = SPEC + "；补：" + "；".join(g["questions"])
    m = maieutic_research(SPEC, need=g["open"])
    g = maieutic_examine(SPEC, m["gathered"])
maieutic_note("revision", amendment=SPEC, justification="首查的表没有首血列")
RESULT = {"answer": "...", "covered": g["addressed"], "gathered": m["gathered"]}
```

要点：
- `for/if` 是 **program 内**的，一次 `env.step` 完成，**不占轮**；
- `examine` 的 `open` 非空**不是失败**——程序照常跑完、`RESULT` 照常交；
- 真正的失败由 critic 判（`task_critic.check_task_success` → `maieutic_gate`），不由程序自判。

### 3.5 三层循环已经够了，不要第四层

| 层 | 位置 | 次数 | 干什么 |
|---|---|---|---|
| L1 | `learn_orchestrate.py:671` `for _d in _dims` | 每条维度一次 | 换维度 |
| L2 | `:751` `for _t in range(SUBTASK_MAX_RETRIES)` (=4) | ≤4 | critic 判 false → 带 critique 重来 |
| L3 | `local_agent.py` solve 内 `max_iters=2` | ≤2 | 代码跑挂了重写 |

**maieutic 只许出现在 L3 内部**（program 里），且 `examine` **一次 program 最多 2 次**
（首判 + 补一轮）。否则 `EXEC_TIMEOUT=90s` 会把整轮判超时——而超时是**不重试直接跳维度**的
（`learn_orchestrate.py:803-810`），等于白烧。

L2 那一层**就是** maieutic 的追问环，不用另建：`critique` 已经是 `gaps` 的追问文本
（`task_critic.py:160` 返回 `questions` 拼串）。

---

## 四、判官唯一（双闸门问题的解）

### 4.1 冲突在哪

maieutic 的 `spec-examiner` 会判 `passed`，Voyager 的 `critic` 也判 `success`。
两个判官并行 → 口径必然分叉（一个说闭合一个说失败，谁改 messages？）。这就是"干预"。

### 4.2 解：examine 不出 success

| 谁 | 判什么 | 输出 | 能不能改 messages |
|---|---|---|---|
| `maieutic_examine` | **材料**答没答到维度（完备性） | `addressed / open / questions / emergent`（**没有 `passed`**） | 不能 |
| `task_critic.check_task_success` | 任务达没达成（`maieutic_gate` 在里面，`task_critic.py:146-160`） | `(success, critique)` | **只有它能**，经 `critique` 字段 |

`examine` 的返回字段叫 `complete`（材料完备），**不叫 `passed`**。这是本次改动里唯一一个
"改名"级的约定，但它是防干预的关键：字段一旦叫 `passed`，早晚有人拿它当 success 用。
验收线里有一条就是 `grep -r '"passed"' mve/maieutic_tools.py` 无输出。

### 4.3 追问怎么回到下一轮

原版唯一通道是 `Critique:` 字段（`prompts/action_template.txt`）。MVE 已经同构：

```
maieutic_examine → open/questions
        ↓（同一次 exec 内，程序自己用或不用）
RESULT + 材料账 evidence
        ↓
task_critic.check_task_success(dimensions=[维度])   learn_orchestrate.py:793-794
        ↓  maieutic_gate → open 非空 → success=False, critique=questions
local_agent.solve(critique=crit1)                  learn_orchestrate.py:755-758
```

**闭环成立，且只经过一个判官。**

---

## 五、3 + 1 个函数（精确签名）

全部**同步、可重入、无 sid**。`trace` 由调用方给（缺省 = 当前维度 id），只用于落盘审计。

### 5.1 `maieutic_research(plan, need=None, *, trace="", max_sources=3)`

| 项 | 内容 |
|---|---|
| 对应原版 | **原版无此能力**（`~/ maieutic_fork/maieutic` 全仓 `grep -rl "research"` 零命中，`src/lib` 下无任何 search/fetch 模块）。此处是 v3 设计稿的遗留想象，落地只能依赖 MVE 自己的联网动作（`actions.register_net` → `web_search` / `mcp_*_fetch`） |
| 返回 | `{"gathered":[{"source","chars","digest","ok":true}], "report":[{"source","status":"failed","why"}], "answer":"…"}` |
| 关键 | **两本账**：失败项进 `report` 不进 `gathered`（同构 `agent.ts` 的 `if (result.ok && result.text) gathered.push(...) else report.push(...)`）。正文不出进程，只回 digest |
| 为什么同步 | 内部 `net_tools` + LLM，单次 5~20s；一次 program 最多调 2 次，落在 90s 预算内 |

### 5.2 `maieutic_examine(plan, gathered, *, dimensions=None, trace="")`

| 项 | 内容 |
|---|---|
| 对应原版 | `spec-examiner.ts`（`passed ⟺ gaps_still_open 为空`） |
| 返回 | `{"addressed":[…], "open":[…], "emergent":[…], "questions":[…≤3], "complete":bool}` |
| 关键 | ① **无 `passed` 字段**（字段级白名单见 `评审对照-v4原型.md` §2.2）；② `emergent` **不阻塞** `complete`（原版口径）；③ `questions` ≤3 条、是追问不是答案；④ 材料为空 → 全部维度算没答到 |

### 5.3 `maieutic_diagnose(trace="", *, window=5)`

| 项 | 内容 |
|---|---|
| 对应原版 | `live-summary.ts:43-51` 的 `stuck_signal`（>5min 无变化 或 同一 gap 3+ 轮未补） |
| 返回 | `{"stuck":0-1, "reason":"…", "hint_depth":"first|deeper|scaffold|micro", "hint":"…"}` |
| 关键 | **纯规则**（读 `ckpt_maieutic/<scene>/*.json` 的历史，不调 LLM）。可每轮跑、零成本 |

### 5.4 `maieutic_note(kind, *, amendment="", justification="", trace="")`

| 项 | 内容 |
|---|---|
| 对应原版 | `revise`（`amendment + justification` 两字段）与 `IN-SESSION REVISIONS RULE`（`intent-diff.ts:140-153`） |
| 返回 | `{"ok":true, "note_id":"…"}` |
| 关键 | 只**追加**一条注记，不改任何既有记录（学习信号冻结，`schemas.ts:270-276`）。没有 `justification` 的 revision 拒收 |

---

## 六、状态放哪：不是会话文件

v3 要 `ckpt_maieutic/<scene>/<sid>.json` 存一个 4 阶段状态机。v4 **不建这个状态机**，三条理由：

1. **一个 program = 一个 session**。`SPEC` 是 program 里的一个变量，生命期就是一次 exec。
   跨 step 时它已经不存在了，留个 `sid` 只是留一具尸体。
2. **面板每次 `asyncio.run()` 起新循环**（进程级单例网关踩过这个坑），
   有状态会话对象必须每次重建，等于没有。
3. **跨 step 续学的载体已经有了**：`save/load_progress_codes`（上次跑通的代码 + 答案），
   原版对应 `info["program_code"]`。要加字段就加在这儿，不新开一本账。

需要落盘的只有审计用的三样，追加写、不改写：

| 文件 | 内容 | 谁写 |
|---|---|---|
| `ckpt_maieutic/<scene>/<topic>__<dim>.json` | `{spec, gathered_digest, addressed, open, notes[], attempts}` | `research` / `examine` / `note` |
| `skill_store.json` 条目 | 技能代码 + `source="learn"` + `notes` | `learn_milestone_skills`（critic 判 success 后） |
| `coach_log.jsonl` | `critique`（追问原文）、`reuse_errors` | 现有链路 |

---

## 七、学习期 / 消费期 = 组开关，不是代码分支

| 期 | VLML 工具集 | 本机能力 | 联网能力 | **助产士** | 学到的技能 |
|---|---|---|---|---|---|
| **学习**（场景 A） | ✅ | ✅ | ❌（模型侧关，`research` 内部可用） | ✅ | ✅ |
| **学习**（本机场景） | — | ✅ | ❌ | ✅ | ✅ |
| **消费** | ✅ | ✅ | ❌ | **❌** | ✅ |

开关是现成的：

```python
actions.set_switch(scene, "助产士", False)   # actions.py:489-502，落 action_registry.json
actions.invoke(...)                          # 关掉的组 → raise「能力组「助产士」的总开关是关的」:587-590
actions.enabled_entries(scene)               # 关掉的组 → 条目不出现在 ns 里 :563
```

因为 `_ns_base` 按 `enabled_entries` 过滤（`local_agent.py:1066`），
**关掉之后模型连 `maieutic_*` 这个名字都拿不到** —— 这是"物理无联网/无助产"的验收方式
（不是"提示词里说别用"）。

---

## 八、四处必改（v3 三条不变，第 4 条改）

| # | 位置 | 现状 | v4 改法 |
|---|---|---|---|
| 1 | `voyager_vlml/consume.py:679-691` | 非 VLML 场景消费期 `register_net` + `ensure_mcp_tools` | 消费期只开 VLML + learned 两组 |
| 2 | `mve/local_agent.py:1038-1043` | `web_search/web_fetch` 无条件进 `full` | 保留（靠注册表过滤兜着），但 v4 之后联网组在学习期**也该关**（`research` 内部直连 `net_tools`，不经这层） |
| 3 | `mve/local_agent.py:1155-1157` | `run_saved` 自愈里 `register_net` | 删。自愈只补缺的名字，不许顺手开组 |
| 4 | ~~`learn_milestone_skills` 挪到 `finalize`~~ | v3 的说法作废 | **不改**。技能入库的唯一判据是 `critic 判 success`（原版 `voyager.py:369-370`）。加 `finalize` 等于造第二个判官，违反 P2 |

---

## 九、退役清单

| 动作 | 对象 | 说明 |
|---|---|---|
| 删 | `fetch` / `time` / `context7` / `sequential-thinking` 常驻服务 | 无业务调用（v1 结论不变） |
| 删 | `neko` / `ddg` 作为**模型可见**的组 | 并入 `maieutic_research` 内部（`net_tools`） |
| **不新增** | ~~`maieutic` MCP 服务（:8797）~~ | v4 取消。见下 |
| 不动 | 数据面（`VLMLMCPEnv` / `SceneDBEnv`） | 唯一真正需要的 MCP |

**关于「两个 MCP」这个目标**：数据面（VLML）必须是 MCP —— 它是外部世界。
助产面（maieutic）**不能是 MCP**，因为 MCP 调用要桥回事件循环（`local_agent.py:1075-1097`
的 `_make_mcp_callable`），而 exec 跑在 worker 线程里（`run_saved_async` → `asyncio.to_thread`）——
同步等一个跨进程往返，正是 2026-10-21 那次 60s 超时的形态。

**一份实现，两个壳**：
- 实现：`mve/maieutic_tools.py`（3+1 函数，纯 Python，无 IO 依赖注入）
- 壳 A（MVE 内部，必用）：`actions.register(origin="maieutic")` → 进 `_ns_base` 执行域
- 壳 B（可选，给猫娘/其它进程）：`mve/mcp_maieutic_server.py` stdio 包一层，**MVE 自己不用**

---

## 十、改动清单（三期）

### MVP-1 · 执行域三函数（能跑通一个维度）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `mve/maieutic_tools.py` | **新**：`research / examine / diagnose / note`；落盘 `ckpt_maieutic/`；无会话对象 |
| 2 | `mve/actions.py` | `register_maieutic(scene)` + `_ORIGIN_GROUP["maieutic"]="助产士"` |
| 3 | `mve/local_agent.py` | `_ns_base` 的 `full` 加 4 个 `maieutic_*`；`allowed_origins` 加 `"maieutic"` |
| 4 | `mve/local_agent.py` `PROMPT_SYSTEM` | 加一段"不确定够不够材料时先 `maieutic_research` 再 `maieutic_examine`"，**明确 2 次上限** |
| 5 | `mve/learn_orchestrate.py` | 学习期 `set_switch(scene, "助产士", True)`，收尾关回 |
| 6 | `mve/test_maieutic_tools.py` | **新**：无 `passed` 字段、`open` 非空不阻断执行、失败源只进 `report`、关组后 ns 里没有 `maieutic_*` |

### MVP-2 · 材料账与追问质量

| # | 文件 | 改动 |
|---|---|---|
| 7 | `mve/net_tools.py` | `maieutic_research` 用 gather-两本账（403/521/DNS 进 `report`，判据复用 `_looks_like_tool_error`） |
| 8 | `mve/maieutic_gate.py` | prompt 加一段"材料为空 = 全部没答到"（已有，`GATE_SYSTEM:47-49`），补 `emergent` 不阻塞用例 |
| 9 | `mve/skill_store.py` | 条目附 `notes`（revision 理由），同名覆盖改**追加历史版本**（冻结旧版） |

### MVP-3 · 卡住识别与提示梯度（本期**移出范围**，见 `评审对照-v4原型.md` §七）

| # | 文件 | 改动 |
|---|---|---|
| 10 | `mve/maieutic_tools.stuck()` | 纯规则：`_t>=2`（同一次学习内第 3 次未闭合）→ `hint_depth`。**计数器用 L2 的 `_t`**（`learn_orchestrate.py:751`），**不用** `progress.iterations.rounds`（它不区分 passed、跨次累加、永不归零） |
| 11 | `mve/learn_orchestrate.py:751-810` | 按 `hint_depth` **展开成不同的提示文本**（first/deeper/scaffold/micro，枚举名不进 prompt）；`pilot.log` 打一行 `· 卡住：…`；整轮 `run_log` 的 `hint` 字段写 `scaffold@3`（字段已存在，`:1174`，不动白名单） |
| 12 | `mve/maieutic_scaffold.py` | 预判偏离补 `recovery_policy{first,deeper,scaffold,micro}` |

---

## 十一、验收线（9 条，前 3 条是"没打断"的硬判据）

| # | 验收项 | 判据 |
|---|---|---|
| 1 | **step 数没涨** | 同一维度闭合所需 `action_agent_rollout_num_iter`（L2 次数）改造前后分布不变；涨了就是 maieutic 在抢决策 |
| 2 | **单次 exec 不超预算** | `pilot.log` 里单 step 墙钟 P95 < `EXEC_TIMEOUT`（90s）；出现 `timed_out` 即 program 内 `examine` 超过 2 次 |
| 3 | **循环层数没变** | `learn_orchestrate.py` 仍是 L1/L2、`local_agent.solve` 仍是 L3；无新增 `while`/`for` 包住 `solve` |
| 4 | **判官唯一** | `grep -r '"passed"' mve/maieutic_tools.py` 无输出；`task_critic.py` 的 `return (not bool(g.get("open")))`（:160）是唯一的 `passed` 来源 |
| 5 | **追问真回灌** | `coach_log.jsonl` 的 `critique` 里出现 `examine` 返回的 `questions` 原文（不是重新生成的另一段话） |
| 6 | **消费期物理无助产** | `set_switch(scene,"助产士",False)` 后重放同题：`_tools_in_code` 里 `maieutic_*` 计数 0，`audit_skill_replay.py` 仍 `OK / 坏 0` |
| 7 | **学习信号冻结** | 同名技能重做后，旧条目 `answers / notes / classification` 逐字节未变 |
| 8 | **面板没新按钮** | 入口仍是「交给 Voyager 回答」；只加状态条（v2 起就是这条，v4 不变） |
| 9 | **stuck 本期未接线（反向项）** | 同一维度第 4 次 exec 的 prompt 与第 1 次**逐字相同**；`pilot.log` 无 `卡住` 行；该轮 `run_log` 的 `hint` 为空。三条同时成立 = 确认移出范围、无半截实现（正向项见 §十 MVP-3） |

> 第 9 条是 v3 第 6 条（`hint_depth=scaffoldHint`）的**替代物**，不是补充：stuck 本期归
> **移出范围**（不是裁剪——`progress.iterations.rounds` 是为追问回灌存的，不能当承载点），
> 定论与"若要做时用哪个计数器"见 `评审对照-v4原型.md` §七。

---

## 十二、待拍板（v3 五条 + 新增三条）

**继承**：

1. **维度双轨**：人导入题时是否允许手写维度（原版 `instructorConfiguredDimensionsAddressed`）？建议允许。
2. **`maieutic_diagnose` 要不要 LLM**：原版 `stuck_signal` 是纯规则，建议保持。
3. **场景 B 硬闸**：SQL 实跑（现有）还是加 sympy 数值/符号校验？
4. **数据面要不要也常驻 MCP**：唯一收益是把 `vlml_env` 的 `os.chdir` + `Path.mkdir` 全局补丁挪出主进程。
5. **`live-summary` 式一句话摘要**要不要进面板：建议 MVP 之后。

**新增**：

6. **`examine` 必须一次判全部维度**（不要逐维度调）——一次 program 内 2 次 `examine` × 每维度 1 次
   LLM 调用会直接把 90s 吃光。这不只是性能建议，是 B2 的推论。
7. **`note` 算不算第 4 个函数**：我倾向算（它有独立落盘语义），但对外只暴露 3 个 `maieutic_*` 主函数
   加 1 个辅助函数，prompt 里只教 3 个。
8. **助产面要不要留一个 MCP 壳**（给猫娘用）：留壳不影响 MVE，但要让 MVE 明确"自己不连"。

---

## 附：术语对照（v4 增"谁判"一列）

| maieutic（fork 原版） | MVE | 谁判 |
|---|---|---|
| exercise | 人导入/自动生成的题（`tasks.infer_topic`） | — |
| spec_gate_dimensions | 维度（`maieutic_scaffold.dimensions`，图谱 `kind=dimension`） | — |
| student's spec | **program 里的 `SPEC` 变量** | `examine` 只判材料完备 |
| code / finalCode | `RESULT` / SQL / 答案文本 | `task_critic` + 真数判据（`has_real_data`） |
| revise(amendment, justification) | `maieutic_note("revision", …)` | 不判，只落盘 |
| drift / revision / bug | `note.kind` + `pending_dimensions.json` | 教师侧（不进运行时判定） |
| gaps_still_open | `examine.open` | `maieutic_gate`（在 critic 里）→ **唯一 `passed` 来源** |
| questions | `critique` 字段（`action_template.txt: Critique:`） | critic 输出，回灌下一 step |
| revisionChoice / frozen signal | `skill_store` 条目版本化 | 落盘约束，不判 |
| live-summary flags（含 stuck_signal） | `maieutic_diagnose`（纯规则） | 只提示，不改判 |
| Phase 4 finalize | **`add_new_skill(info)`**（`voyager.py:369-370`） | 只在 `success` 时执行 |
