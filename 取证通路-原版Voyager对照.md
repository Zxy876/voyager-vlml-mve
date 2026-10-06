# 取证通路：对照原版 Voyager（MineDojo/Voyager）看差在哪

用户追问：「为什么会『抄错数字』？意思是 Voyager 没有如实记录自己的行动吗？
如果是这样你就回去看 voyager main，原来怎么写日记与 action。」

回原仓库（`https://github.com/MineDojo/Voyager`）核对后的结论：

> **Voyager 如实记录了行动。问题不在记录，在「谁产出进入判定的材料」。**
> 原版里，进判定的是**解释器/环境的原始输出**；
> MVE 里，进判定的是 **LLM 整理出来的事实数组**。
> 而 LLM 生成的东西在原版里只出现在判定**之后**（critique / chatlog 摘要）。

顺带修正我自己前一轮的判断（见文末「修正」）。

---

## 一、原版 Voyager 的结构（照仓库代码）

### action 是**可执行代码**，不是 JSON、也不是数字

`voyager/agents/action.py`：模型输出 ```javascript 代码块 → **babel 解析成 AST** →
取出 `program_code`（函数体）+ `program_name` + `exec_code`（`await mainFn(bot)`）。
执行由 Node 解释器完成，执行结果由解释器产出。

### 观察回灌 prompt：**原样读原始字段**

`action.py` 的 `render_human_message(events=...)` 直接取 events 的原始字段：

```python
biome = event["status"]["biome"]
voxels = event["voxels"]
inventory = event["inventory"]
...
observation += f"Inventory ({inventory_used}/36): {inventory}\n\n"
```

**没有"让模型把观察整理成结构化事实"这一步。**

### 判定（critic）读的是原始 events

`voyager/voyager.py`：

```python
events = self.env.step(code, programs=self.skill_manager.programs)
self.recorder.record(events, self.task)          # 原始事件原样落盘
...
success, critique = self.critic_agent.check_task_success(
    events=events, task=self.task, context=self.context,
    chest_observation=self.action_agent.render_chest_observation(),
)
```

注意 `summarize_chatlog(events)`（模型生成的摘要）**在判定之后**才出现，
且只用来拼技能检索的 query，**不参与判定**。

### 落盘

- `U.EventRecorder` → `ckpt_dir`（默认 `ckpt`）：`recorder.record(events, task)`
- 长期观察记忆：`{ckpt_dir}/action/chest_memory.json`（原样存 dict）

### 技能库存的是**代码**

`program_code` + `program_name`；`retrieve_skills(query=...)` 语义检索，
渲染进 Action Agent 的 system message。

### 「日记」这个说法

仓库里**没有叫 journal 的东西**。最接近的是两样：
`EventRecorder`（事件记忆，源码里还挂着 `# TODO: remove event memory`）
和 `self.conversations`（内存里的 system/human/ai 三元组）。
所以"日记"在原版就是**原始事件流水**，不是模型写的总结。

---

## 二、MVE 差在哪（一条一条对）

| 环节 | 原版 Voyager | MVE（修改前） |
|---|---|---|
| action | 可执行代码，解释器执行 | 调工具，返回 JSON |
| 观察怎么用 | **原样读 events 字段**拼进 prompt | `_extract` 让 LLM **整理成事实数组**（`voyager.py:822-879`） |
| 判定读什么 | **原始 events** | **LLM 生成的 facts** |
| 日记存什么 | 原始 events 落 ckpt_dir | `trajectory` 只存 tool+args，**result 丢了**（`voyager.py:791`） |
| LLM 文本的地位 | 判定之后（critique / 检索 query） | **判定之前**（facts 就是判定的输入） |
| 技能库 | 可执行代码 | 自然语言教训（prompt 注入） |

一句话：**原版是「原件进判定」，MVE 是「誊抄本进判定」。**

---

## 三、所以「抄错数字」到底是什么

加了诊断后（把 got/ref 的类型与原型写进日志），实测这一轮：

```
缺失: eco 局胜率(值不可比: got=None<NoneType>≠ref=42.9<float>)
     支撑这个结论的样本回合数(值不可比: got=None<NoneType>≠ref=59<int>)
```

**不是形态抄错，是 `value = None`** —— 模型没看到这个数，交了白卷。

而本轮轨迹是 `['match_summary_report']`（只调了 1 个工具）；裁判的标准答案
轨迹是 `['<deterministic-sql>', 'match_summary_report', 'pattern_detection_report']`。
也就是说：**Voyager 压根没调那个能给出 eco_win_rate / pattern_rounds 的工具。**

所以真实病因是**取证不全**：

1. Voyager 只调了 1 个工具 → 两个评分点的数不在它的观测里；
2. 模型按 prompt 要求"取不到就别给"，但还是给了 `value: null`；
3. 判定把这条 null 当成一条**作答**去比对 → 记成"值不可比"。

**第 3 步是最不该发生的**：交白卷被粉饰成了"取到了但取错"，
于是真正的病（工具编排不够）被"值不可比"这个措辞盖住了。

已修（无损）：`got_value is None` → 直接记 `未取到值：模型交了 null`，
不再进值比对（评分结果不变，原因变准了）。

---

## 四、按原版该怎么改（待定）

1. **判定的输入换回原件**（对应 B 方案，最强的一刀）：
   模型不输出数字，只指出「答案在哪个工具的返回、哪个路径」，
   **值由代码 `dig` 取**。取不到就是取不到 —— 与原版一致：
   events 里有什么就是什么，不存在"填 null 交白卷"。
2. **日记补回 result**（已做）：`trajectory` 现在带原始 `result`，
   事后能回答"当时工具到底返回了什么"（对应原版 `recorder.record(events, task)`）。
3. **观察回灌改成原样**：`_extract` 之前，先把工具返回的原文直接喂给模型
   （现在其实已经喂了 `obs`，但同时又要求它誊抄一遍 —— 二选一即可）。
4. 技能库形态（代码 vs 自然语言）是另一件事，本轮不动。

---

## 五、修正我前一轮的判断

前一轮我把 8 条缺失笼统归为「LLM 复述数字抄变形」，举的例子是
`"50.0%"` / `"Moderate"` 这类形态问题。**这个定性只对了一部分**：

- 历史日志里确实出现过形态问题（那时记的是 `值不可比`，没有类型信息，我据此推测）；
- 但加上类型诊断后，当前这一轮的两条缺全是 `got=None` —— **是没取到，不是抄错**。

也就是说：**我此前把一个"取证不全"的问题，说成了"誊抄不准确"的问题。**
两者的修法不同：形态问题靠归一化；取证不全得靠编排（多调一个工具）
或靠"取不到就如实记取不到"。现已按后者修正判定语义。
