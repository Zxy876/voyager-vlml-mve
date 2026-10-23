# maieutic 外包课程规划：一个 Voyager 学习循环里的分工

> 回答的问题：`人导入 → maieutic 出骨架+调研 → Voyager 首轮回答（= maieutic round1）
> → maieutic 追问 → 追问进 Voyager 的任务拆解/课程规划` 这条流水线成不成立。
> 核心分歧：**Voyager 的抓手是调工具而非 LLM/code，它怎么继续回答 maieutic 的追问？**
> 以及：**课程规划被 maieutic 外包之后，Voyager 还拆不拆任务？**
>
> 取证基：`voyager_original/`（只读参照，MVE 运行时**不 import** 它，是照结构重写）、
> `mve/learn_orchestrate.py`、`mve/maieutic_scaffold.py`、`mve/local_agent.py`、`mve/task_critic.py`。

---

## 零 结论

1. **架构成立，而且不需要新增循环、不需要新增 API。** 原版自己就有「课程由外部给」的正式模式：
   `inference(sub_goals=[...])` 直接跳过拆解逐条执行（`voyager.py:406-407`），以及
   `mode="manual"` 用 `input()` 让人输 `task`/`context`（`curriculum.py:322-330`）。
   maieutic 外包 = 把这个 `input()` 换成另一个程序，**不违反 v4 的 P3**（换的是任务来源，不是决策者）。

2. **分歧点有确切答案：Voyager 不能「只说话」，但追问不需要它说话。**
   两版都强制代码块（原版 `action.py:214-218`，MVE `local_agent.py:673-676`）。
   但 `render_human_message` 的 `Task:` / `Context:` / `Critique:` 三个槽都是**自由文本**
   （`action.py:190`、`:192-195`、`:197-200`）——追问天然能进来；而 Voyager 的「回答」
   从来不是论述，是**一次可验证的取数动作**。
   **抓手是工具不是障碍，是必要条件**：maieutic 的追问本来就是「你还没查 X」。

3. **唯一真正的卡口不是「没代码块」，是「没取证」**：`local_agent.py:640-647` 的真空判据在
   学习环节恒生效（`require_tools=True` `learn_orchestrate.py:765` + `_can_fetch` `local_agent.py:468`
   恒真）→ **元认知类追问（「你为什么选这张表」）必被判失败**。但这不是"必须让它说话"的理由，
   而是"必须给它配可调用的检验"的理由——maieutic 的 phase 2 已经给了三件（sympy 符号等价 /
   spec↔代码 diff / 执行器分发），只是 MVE 一件都没移植（详见 §4.5）。

4. **缺口只有一个：`_dims` 是静态的。** MVE 现在已经是「一条维度一次 rollout」的子目标制
   （`learn_orchestrate.py:671`，注释自己就引了 `voyager.py:403-421`），但维度来自
   `maieutic_scaffold.scaffold()` 的**一次性**产物并按问题 md5 缓存（`maieutic_scaffold.py:181`、
   `:102-104`），它**不接收「上一轮 Voyager 答了什么」**。要让 maieutic 真的按 Voyager 情况
   重规划，只需用已有空位 `render_material(..., extras=)`（`:154-178`）+ `use_cache=False`，
   **不改函数签名**。

---

## 一 原版取证：课程规划可以外部给，而且这是原版自带的

### 1.1 三条入口，谁选任务一目了然

| 入口 | 谁选下一个任务 | 拆不拆任务 | 出处 |
| --- | --- | --- | --- |
| `learn()` | LLM 现问现答 `propose_next_task` | **不拆** | `voyager.py:326-348` |
| `inference(task="…")` | 人给一句自然语言 | `decompose_task` 拆成子目标 | `voyager.py:403-407` + `curriculum.py:372-385` |
| `inference(sub_goals=[…])` | **外部给列表** | **完全不拆** | `voyager.py:406-407` |

关键三行（`voyager.py:403-419`）：

```python
def inference(self, task=None, sub_goals=[], reset_mode="hard", reset_env=True):
    if not task and not sub_goals:
        raise ValueError("Either task or sub_goals must be provided")
    if not sub_goals:                      # ← 只有没给 sub_goals 才拆
        sub_goals = self.decompose_task(task)
    ...
    while self.curriculum_agent.progress < len(sub_goals):
        next_task = sub_goals[self.curriculum_agent.progress]
        context = self.curriculum_agent.get_task_context(next_task)
        messages, reward, done, info = self.rollout(task=next_task, context=context, ...)
```

**「课程规划充分」= 填了 `sub_goals`。此时拆解彻底消失，Voyager 只剩执行。**

### 1.2 `propose_next_task` 的返回形状 = 两个自由文本

`curriculum.py:288-304`：

```python
if self.mode == "auto":
    return self.propose_next_ai_task(messages=messages, max_retries=max_retries)
...
def propose_next_ai_task(self, *, messages, max_retries=5):
    curriculum = self.llm(messages).content
    response = self.parse_ai_message(curriculum)          # 只认一行 "Task: …"（:314-320）
    context = self.get_task_context(response["next_task"])
    return response["next_task"], context
```

而 `context` **本身又是一次 LLM 问答**（`curriculum.py:420-437`）：把 task 变成
`How to {task} in Minecraft?` 再去 `run_qa_step2_answer_questions`，结果拼成
`f"Question: {question}\n{answer}"`。

消费端是三个自由文本槽（`action.py:190-200`）：

```python
observation += f"Task: {task}\n\n"
if context:
    observation += f"Context: {context}\n\n"
if critique:
    observation += f"Critique: {critique}\n\n"
```

→ **maieutic 的 round-N 追问只要做成 `(task, context)` 二元组，就完全落在原版形状里，
prompt、render、循环一行都不用改。**

### 1.3 manual 模式是「外包」的合法先例

`curriculum.py:322-330`：

```python
def propose_next_manual_task(self):
    confirmed = False
    task, context = "", ""
    while not confirmed:
        task = input("Enter task: ")
        context = input("Enter context: ")
        confirmed = input("Confirm? (y/n)").lower() in ["y", ""]
    return task, context
```

原版明确允许「课程从外部程序来」。`mode == "manual"` 时 `propose_next_task` 直接走这条
（`:290-291`）。**maieutic 外包 = 把 `input()` 换成函数调用。** 这是本次架构最重要的合法性依据：
它不是改造 Voyager，是用 Voyager 已经留好的口子。

### 1.4 原版这个模式有个真缺陷：`progress` 只数完成

`curriculum.py:133-135`：

```python
@property
def progress(self):
    return len(self.completed_tasks)
```

`inference` 的循环条件是 `while progress < len(sub_goals)` —— **某个 sub_goal 反复失败时
`progress` 不增 → 死循环在同一句上**，永不前进。（原版没修，因为 `inference` 是研究脚本入口。）

→ 照搬这个结构会踩坑。**MVE 已经修了**：`SUBTASK_MAX_RETRIES=4`（`learn_orchestrate.py:169`，
值与原版 `action_agent_task_max_retries=4` `voyager.py:30` 相同）+ 到上限跳过这条维度
（`:811` 之后进 ④）+ 断点续学（`:684-685` 已闭合的直接跳过）。这条必须写进架构说明，
否则「外包课程规划」会把原版的死循环一起搬过来。

---

## 二 MVE 现在已经是这个形状——不是要新做

| 原版 | MVE 对应 | 出处 |
| --- | --- | --- |
| `inference(sub_goals=[…])` | `for _d in _dims` | `learn_orchestrate.py:671`（注释已引 `voyager.py:403-421`） |
| `sub_goals` 从哪来 | `plan["dimensions"]` ← `maieutic_scaffold.scaffold()` | `:626`、`maieutic_scaffold.py:181` |
| `action_agent_task_max_retries=4` | `SUBTASK_MAX_RETRIES=4` | `voyager.py:30` / `learn_orchestrate.py:169` |
| `Critique:` 回灌下一轮 | `crit1` 喂 `solve(critique=)` | `action.py:197-200` / `:751-798` |
| `completed_tasks` | `learned_dims` | `curriculum.py:133` / `:812` |
| `update_exploration_progress` | `save_progress` + `save_progress_codes` | `curriculum.py:332` / `:302`、`:343` |
| `add_new_skill`（仅 `success`） | `learn_milestone_skills` | `voyager.py:369-370` / `:210` |
| `clean_up_tasks`（精确去重） | `_prog` 按 `dim.id` 精确跳过 | `curriculum.py:349-370` / `:684` |

**所以「整件事发生在一个 Voyager 学习循环里」已经是现状。** 用户提的流水线里，
第 1→2→3→4→6 步全是现成代码，只有第 5 步（追问升格为新维度 + 重规划）是新的。

---

## 三 唯一的缺口：`_dims` 是静态的

### 3.1 现状取证

`maieutic_scaffold.py:181-209`：`scaffold(question, scene_id, *, llm, use_cache=True, extras="")`
—— 输入只有「问题 + 场景」，**没有「上一轮回答」这个参数**；命中缓存直接返回（`:200-209`），
缓存键是 `md5(question)[:12]`（`:102-104`），**与轮次无关**。

结论：**maieutic 目前只在导入时规划一次。第一轮之后「按 Voyager 情况规划」这件事，代码里没有。**

### 3.2 但两条缝已经在了，重规划不用改签名

1. `render_material(question, scene_id, *, extras="")`（`:154-178`）—— `extras` 是自由追加位
   （`:176-177`），可以把「上一轮答了什么 / 还差什么 / 哪些闭合了」塞进去。
2. `render_material` **已经会注入已学节点**（`:162-164`）：
   ```
   这个场景**已经学过**的节点（别重复拆成维度）：…
   ```
   数据来自 `task_decompose.scene_observation`（读图谱），所以只要第 6 步把维度节点落进图谱，
   下一轮重规划天然不会重拆已闭合的维度。

→ 重规划 = `scaffold(q, scene_id, extras=history, use_cache=False)`。**一行调用。**

### 3.3 两个陷阱（必须写死，否则旧账变孤儿）

| 陷阱 | 机制 | 修法 |
| --- | --- | --- |
| **重规划覆盖缓存** | `_cache_path` 只按 question 哈希（`:102-104`）→ `use_cache=False` **覆盖同一文件** → 新维度换 id，旧 `progress.codes` 里的答案再也对不上，`milestone.name` 也可能变 | `_cache_path(scene, q, rnd=n)` 按轮次分文件；v1 永不覆盖；新维度**只追加在尾部**，旧 `dim.id` 不复用 |
| **去重只能用精确** | 用户既有铁律：加 `Jaccard≥0.8` 曾把「stars 减少」判成已完成「增加至少 20000」，合法新题连拒 5 次 | 重规划去重按 `dim.id` 精确比对（同 `curriculum.py:349-370` `clean_up_tasks` 的口径：只做 `not in` 判定，不算相似度） |

---

## 四 分歧点的答案：Voyager 怎么「回答」追问

### 4.1 形式层：它不能只说话（两版都是）

原版 `action.py:214-218`：

```python
code_pattern = re.compile(r"```(?:javascript|js)(.*?)```", re.DOTALL)
code = "\n".join(code_pattern.findall(message.content))
parsed = babel.parse(code)
...
assert len(list(parsed.program.body)) > 0, "No functions found"
```

解析失败 → `return f"Error parsing action response (before program execution): {error}"`（`:259`）。

MVE `local_agent.py:673-676` 同一个正则思路；没有代码块 → `:616-620`：

```python
if not code:
    fb = "没给出 ```python 代码块"
    obs.append({"ok": False, "tool": "local_python", "rows": 0, "error": fb})
    continue
```

→ **「Voyager 生成纯文本对话」在形式上不成立。这不是 MVE 的缺陷，是原版的设计。**

原版**唯一**的说话通道：`bot` 在 eval 域里（`action.py:249` `exec_code = f"await {main['name']}(bot);"`），
所以 agent 代码可以 `await bot.chat("…")`；`OnChat` 在 obsList（`env/mineflayer/index.js:113`），
下一 step 的 `Chat log:` 读回（`action.py:147-152`）。
**代价：说一句话要花一个 step，而且当轮听不见自己说的。** 这不是为对话设计的通道。

### 4.2 实质层：真正的卡口是「没取证」

`local_agent.py:640-647`：

```python
if (not err and res.get("ok") and data is not None and not tracer
        and (_needs_data or (require_tools and _can_fetch))):
    err = ("这题要具体数据…但没调用任何数据源——答案疑似凭训练记忆编造，不算完成；…")
```

其中 `_can_fetch = bool(_probe) or bool(_skill_names)`（`:468`），学习环节恒真；
`require_tools=True` 写死在 `learn_orchestrate.py:765`。
注意判据是 **或** 关系：知识型题 `_needs_data=False` 也逃不掉。

→ **纯文本答案（哪怕规规矩矩写成 `RESULT={"answer": "…"}`）在学习环节必判失败。**

### 4.3 所以「追问能不能被回答」= 追问**能不能被取证**

| 追问类型 | 例子 | 能否直接作 `crit1`/下一个 `_d` | 原因 |
| --- | --- | --- | --- |
| 取数类 | 「`fb_team_won` 你按哪个字段算的？去 `plan_ref` 找它的 role 再重算」 | ✅ | 答案本来就要求调工具，天然过 `:640` |
| 验证类 | 「你说 3 场，把逐场 `game_id` 列出来」 | ✅ | 同上 |
| 溯源类 | 「这个数字从哪张表来的？把 SQL 和返回行贴出来」 | ✅ | 同上（`tracer` 非空即过） |
| 定义类 | 「你说的『翻盘』按什么标准」 | ⚠️ 需改写 | 改写成「去 `plan_ref` 读 note 再复述」才可取证 |
| 元认知类 | 「你为什么选这张表而不是那张」 | ❌ | 不需要取证 → 必被 `:640` 判真空 |

**结论：追问必须「可取证化」。这不是给 maieutic 加约束，而是它本来的形态**——原版 maieutic 的
追问就是「你还没查 X / 你还没对比 Y」，天然带动作。要在出追问那一层（`maieutic_gate.check()`
产出 `questions`，`maieutic_gate.py:172`）加一条**改写规则**：元认知/定义类追问一律改写为
「去哪张表/哪份文档找出什么」。这是追问器的责任，不是循环结构的改动。

### 4.4 如果一定要收元认知类（不靠开闸，见 §4.5）

唯一"直接"改法：那一轮 `require_tools=False`（`learn_orchestrate.py:765` 单轮开口子）。
**但代价必须写死**：该轮产物**不入** `progress.codes`、不 `add_new_skill`、不 `close_one_dimension`，
只落一条文本轨迹（`run_log`）。
**不建议这么做**——它会让「闭合」和「入技能库」脱钩，而这条耦合正是 P5 的地基。
正解见 §4.5：**给它配一个可调用的检验原语**，而不是给纯文本开口子。

### 4.5 追问的取证：能调 maieutic 的哪几件（逐件核实）

**先纠正一处误引：原版 maieutic 没有联网调研。**
`~/ maieutic_fork/maieutic` 全仓（排除 `node_modules`）`grep -rl "research"` **零命中**，
`src/lib` 下也没有任何 search/fetch 模块（只有 `run-python.ts` / `run-java.ts` /
`run-symbolic-math.ts` / `run-exercise-code.ts`）。
`双MCP架构方案.md` §5.1 那句「对应原版 `research/agent.ts`」是 v3 设计稿的遗留想象，已改。
**MVE 里真正的联网取证是 `actions.register_net` 注册的动作**（`web_search` / `mcp_*_fetch`），
它已经是动作 → 天然进 `tracer`。

maieutic 真正给的是 **phase 2 的执行器三件套 + phase 3 的偏离分类**：

| 原件 | 干什么 | 判据 | 出处 |
| --- | --- | --- | --- |
| `run-exercise-code.ts` | 按 `targetLanguage` 分发执行器 | — | `src/lib/run-exercise-code.ts:7-44` |
| `run-python.ts` | 浏览器 Pyodide（Web Worker + COOP/COEP）跑 Python | stdout / stderr | `src/lib/run-python.ts:1-8` |
| `run-symbolic-math.ts` | 预载 sympy；`verifyMathExpression(expr, expected)` | **`simplify(a-b) == 0`**（符号等价，不是字符串比对） | `src/lib/run-symbolic-math.ts:16-42`、`:46-77` |
| `intent-diff.ts`（Phase 3） | spec ↔ `finalCode` 偏离分类 | drift/revision/bug + **中性追问（不许泄露分类）** | `src/lib/opus/prompts/intent-diff.ts:1-45` |

→ 能当"取证"的是**三种**，而且**只有第一种走 `tracer`**。§4.3 的判定表据此升级：

| 追问问的是 | 取证手段 | 判据落在哪 | MVE 现状 |
| --- | --- | --- | --- |
| 数是多少 | SQL / 读文件 / 联网 | `tracer` 非空（`local_agent.py:640`） | ✅ 现成（`query_sql` / `web_search` / `mcp_*_fetch`） |
| 这个数学结论对不对 | sympy 符号等价 | `check_math(expr, expected)` 返 True + 化简式 | ❌ 零移植；且 `_run_restricted` 禁 `import`（`:242-246`）→ 只能做成**原语动作** |
| 你说的和你写的对不对 | spec ↔ 代码 diff | 逐条 divergence（**不要求 tracer**） | ❌ 零移植（只有事前 `expected_drifts` 预测，`maieutic_scaffold.py:68-91`） |

**三条落点，全部用现成机制，不开口子：**

1. **`check_math` 做成原语动作**（`actions.register(..., group="本机能力")`）。
   - 一被调用就进 `tracer` → `local_agent.py:640` 的真空判据**自动满足，判据本身不用改**；
   - 返回值（`True/False` + `simplify` 结果）就是材料 → 进 `_ev1` → `maieutic_gate._material_block`
     （`:92-104`）能列出来，闸门"以材料为准"那一条也过得去；
   - 这同时给出待拍板项「场景 B 硬闸用 SQL 还是 sympy」的答案：**sympy**，且原版已给实现。
   - **为什么必须做成动作、不能让模型自己 `import sympy`**：`_run_restricted` 的 ns 没有 builtins，
     `import` 直接 ImportError（`:242-246` 那段错误文案就是为这件事写的）。

2. **`intent-diff` 移植成第二道闸**（不替代 `maieutic_gate`，两道分工）。
   - 输入：`_d`（维度 = spec 里的一条承诺）+ `code1`（`learn_orchestrate.py:824` 落盘的那份）
   - 时机：`ok1` 判完、`dim_codes.append` 之前
   - 用途两条：
     a. `ok1=True` 但 diff 判 drift（代码比承诺少做）→ **不该闭合成技能** —— 这是「跑通 ≠ 口径对」
        的机器化判据，比 `RESULT.rows > 0` 强；
     b. `ok1=False` → diff 出的中性追问**比 `crit1` 准**。根因：`crit1` 只看「答案文本 + 材料」，
        **看不见代码**，而 Voyager 的产物主体就是代码。
   - 纪律照抄原版：`classification` **不进学生面**（`intent-diff.ts:16-18`
     "The question must NOT reveal the classification"），只进 `run_log` / 教师面 ——
     这与 `评审对照-v4原型.md` §二 的可见边界规则是同一口径，不是自创。

3. **联网调研归位到第 2 步，不放进追问。**
   - `maieutic_scaffold.render_material`（`:154-178`）现在注入的是「已学节点 + 实体 + 数据源」，
     **没有联网**；要"调 maieutic 的联网调研"，落点在这里（`extras` 或新增一段），
     产物进 `material_note` / 缓存 json；
   - 不建议放进追问：追问的取证靠执行层（Voyager 自己查）更准，且追问里联网会吃光
     `EXEC_TIMEOUT=90`（`learn_orchestrate.py:178`）的预算。

**净结论**：这句判断成立——取证确实可以调 maieutic，但要调的是 **phase 2 的执行器 + phase 3 的
`intent-diff`**，不是"联网调研"（原版没有）。
而 §4.4「元认知追问必挂」也随之有解：**不是放行纯文本，而是给追问配一个可调用的检验原语**
（`check_math`）**或换一道看得见代码的闸**（`intent-diff`）。这样 §4.4 那个"本期不做"的开口子
就不需要了。

---

## 五 完整流水线（每步：谁做 / 跑在哪 / 产物落哪 / 现成还是新做）

| # | 步骤 | 谁做 | 跑在哪 | 产物 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 1 | 人导入问题 | 人（面板「交给 Voyager 回答」`dashboard.py:1015`） | `coach` → `tasks.infer_topic()` | `topic_id`、`import_id` | 现成 |
| 2 | 出追问骨架 + 调研（调研追加进题面） | maieutic | `maieutic_scaffold.scaffold()` `:181`，材料 `render_material` `:154` | `ckpt_maieutic/<scene>/<hash>.json`：`dimensions` / `expected_drifts` / `milestone` / `material_note` | 现成 |
| 3 | Voyager 首轮回答（= maieutic round1，**必然不闭合**） | Voyager | `learn_orchestrate.py:671` 第一条 `_d` → `local_agent.solve` `:755` → `task_critic.check_task_success` `:793` | `obs1` + `crit1`；`maieutic_gate` 返 `complete=False` + `questions`（`maieutic_gate.py:171-174`） | 现成 |
| 4 | maieutic 追问（**内层**） | maieutic | `crit1` 回灌 `solve(critique=)`（`:751-798`），最多 `SUBTASK_MAX_RETRIES=4` 轮 | `progress.iterations[dim].{asked, rounds, passed}`（`maieutic_scaffold.py:418-441`） | 现成 |
| 5 | **追问升格为新维度 + 重规划**（**外层**） | maieutic | `scaffold(extras=history, use_cache=False, rnd=n)` → 新 `_d` 追加进 `_dims` | `ckpt_maieutic/<scene>/<hash>_r<n>.json` | **新做（小）** |
| 6 | 闭合 → 一条维度一条技能 | Voyager | `learn_milestone_skills`（`:210`）→ `skill_store.add(..., exact_only=True)` | `skill_store.json` 一条技能，`name=dim.id` | 现成 |
| 7 | 落 db（问题 / 表列角色 / 配方） | maieutic + Voyager | 见 `建模表与技能落库-可行性.md` §四 的三张表 | `problem` / `plan_ref` / `recipe` | 待做 |

**取证插在第 3、4 步之间（§4.5）**：`intent-diff` 在 `ok1` 判完、`dim_codes.append`（`:824`）之前跑一次，
`check_math` 是维度可以调用的原语动作（谁调谁进 `tracer`）。两者都不新增循环。

### 5.1 第 6 步为什么能「入 db」：技能名就是关联的问题

原版：任务成功 → `skill_manager.add_new_skill(info)`（`voyager.py:369-370`），
`info` 里带 `task`（自然语言）与 `program_code`。MVE 同构：维度闭合 → 一条维度一条技能，
`skill_store.add(topic_id, did, "<描述>", code=code, source="learn", exact_only=True)`
（`learn_orchestrate.py:257-268`）—— 第二参数 `did` 就是维度 id，`exact_only=True`
因为「维度 id 是精确主键：不许相似度合并」。

所以「技能名 = 关联的问题」在 MVE 已经是事实（`skill_store` 的 key = `topic_id::dim_id`），
落 db 只是把它从 `skill_store.json` 换成 `problem` 表一行 + `recipe` 一行（`problem.id = dim_id`）。

### 5.2 逐条对 v4 的形式约束（P1-P5）：一条都没破

| 约束 | 这条架构动它了吗 | 为什么 |
| --- | --- | --- |
| P1 不许改 messages 构造 | **没动** | 追问走已有的 `Critique:`/`Critique` 槽（`action.py:197-200` / `local_agent.py:609`） |
| P2 不许替 critic 判 success | **没动** | 唯一 `passed` 仍是 `task_critic.py:160` 的 `maieutic_gate`；追问只是它的输出（`maieutic_gate.py:172`） |
| P3 不许替 curriculum 选 task | **没破** | `inference(sub_goals=)` 与 `mode="manual"` 都是原版授权的输入源（`voyager.py:406-407`、`curriculum.py:322-330`）。**换的是输入源，不是决策者**——循环仍在 `learn()`/`for _d in _dims` 里推进 |
| P4 不许改 done 判据 | **没动** | `SUBTASK_MAX_RETRIES=4`、`maieutic_gate` 保持原样；重规划只改「下一轮跑哪些 `_d`」，不改「这一轮算不算完」 |
| P5 不许绕过 `program_code` 写技能 | **没动** | 仍只在 `ok1` 为真后写（`:811` 之后 ④）；元认知类追问如果开 `require_tools=False` 则**明确不写**（§4.4） |

**唯一新增的是第 5 步的「可变 `_dims`」**，而它是外层 `for` 的输入，不是第四层循环。

---

## 六 三个风险

| # | 风险 | 具体后果 | 处理 |
| --- | --- | --- | --- |
| 1 | 重规划覆盖缓存 | 旧维度 id 失联 → `progress.codes` 成孤儿账，续学丢成果 | 缓存按轮次分文件（§3.3） |
| 2 | 去重上相似度 | 合法新维度被当成已完成，重规划变空转（用户已踩过 5 次） | 只用 `dim.id` 精确比对 |
| 3 | 追问不可取证化 | 追问退化成「再答一遍」，4 轮全撞 `:640` → 维度永不闭合 | 两条：出追问时改写为「去哪找什么」（`maieutic_gate`），并给它配可调用的检验原语 `check_math` / 第二道闸 `intent-diff`（§4.5） |

第 3 条是本架构成败的关键，且**它是质量风险不是结构风险**——结构上追问通道是通的，
通不通取决于两件事：追问写得够不够「有动作」（`maieutic_gate` 的责任），
以及这类追问有没有对应的**可调用检验**（`check_math` / `intent-diff` 的责任，§4.5）。

---

## 七 验收线（可跑断言，6 条）

1. **「追问能进来」**：构造一次 `solve(critique="去 plan_ref 里找 fb_team_won 的 role 再重算")`，
   断言最终 `user` 文本里出现该句（`local_agent.py:609` 的 `上一轮没通过 critic 检查…`）。
2. **「纯文本不算」**：发一个只含 `RESULT = {"answer": "…"}` 的代码块，断言 `ok == False`
   且 `error` 命中真空文案（`local_agent.py:640-647`）。
3. **「重规划不覆盖旧账」**：同一问题跑两轮，断言 `ckpt_maieutic/<scene>/` 下有
   `<hash>.json` 与 `<hash>_r2.json` 两个文件，且第二轮 `dimensions` 里包含第一轮已闭合的 `dim.id`
   （或已被 `render_material` 的「已学过节点」排除）。
4. **「闭合即入技能」**：`milestone_state()` 由 `achieved=False` 变 `True` 后，
   `skill_store` 里出现 `name == dim.id` 的条目，`source == "learn"`。
5. **「`check_math` 能顶替纯文本」**（§4.5 落点 1）：数学型维度只调 `check_math("diff(x**2,x)", "2*x")`
   并写 `RESULT = {"answer": "2x"}`、不碰任何数据源，断言 `ok == True` —— 因为这次调用的
   函数名出现在 `obs["data_sources"]` 里（`local_agent.py:659`），`tracer` 非空，`:640` 不触发。
6. **「`intent-diff` 拦住漂移」**（§4.5 落点 2）：构造 `ok1=True` 但代码少做一条承诺的样例，
   断言不调 `learn_milestone_skills`、`dim_codes` 里该维度不落 `code`，且 `run_log` 里留下
   `classification`（drift），而面板学生面**看不到** `drift` 这个词。

---

## 附 术语对照

| 用户说法 | 原版 | MVE | 落 db 后 |
| --- | --- | --- | --- |
| 一个 Voyager 学习循环 | `learn()` / `inference()` | `learn_orchestrate.py` 主循环 | 不变 |
| 追问（round N） | `Critique:` 槽 | `crit1` / `critique=` | `problem.asked` 可存 |
| 课程规划（外包） | `propose_next_task` / `sub_goals` | `plan["dimensions"]` | `problem` 行序 |
| 闭合 spec | `success` | `maieutic_gate.passed` | `recipe.verified_rows > 0` |
| 轨迹 | `program_code` | `progress.codes[dim].code` | `recipe.sql` / `recipe.py` |
| 技能名 = 关联的问题 | `task`（自然语言） | `skill_store` key = `topic_id::dim_id` | `problem.id = dim_id` |
