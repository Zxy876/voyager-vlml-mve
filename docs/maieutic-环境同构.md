# maieutic 作为「学习环境」的同构设计（调研，2026-10-23）

> 起因：v4 把 maieutic 的 examine 放进了**执行域**（模型写代码去调）。那是"工具"，不是"环境"——
> 工具可以不用，环境不能不看。你问的"持续给学习信号、持续交互"，本质是问：
> **原版 Voyager 要求的"学习环境"到底是个什么形状，maieutic 怎么长成那个形状。**
>
> 三个约束先摆清：
> 1. 原 Voyager 主循环不变（`learn → rollout → step` 三层，`done` 判据不变）；
> 2. MVE 学习循环的 **prompt 一行不改**（`local_agent.PROMPT_SYSTEM` 与 solve 的拼装串都不动）；
> 3. maieutic 每步都回话（不是判完就完）。

## 结论（四句）

1. **原版的"环境"不是 `step()`，是 `obsList + event()`**：环境 = 每一步都必然产出的、形状固定的一坨观察。
2. 观察分两类：**状态型**（每件事都带上）与**事件型**（`on*`，只在触发时出现）。prompt 里对应两类槽：
   状态 → `Biome/Inventory/…`，事件 → `Execution error:` / **`Chat log:`**（`action.py:147-152`，多条累积）。
3. **`Chat log:` 就是"环境说话"的槽**。原版里 `bot.chat()` → `onChat` → 下一轮 prompt 的 `Chat log:`，
   **prompt 零改动**。maieutic 要持续回话，就该长成"另一个 Observation"，而不是一组工具。
4. **MVE 里已经有这个位置**：`task_critic.check_task_success(..., dimensions=[_d])` 在**每次**重试都跑
   （`learn_orchestrate.py:793-794`）——**它已经是每步必跑的观察者**。v4 把 examine 放执行域是放错了半边。

---

## 一、原版「学习环境」的契约（取证）

### 1.1 三个函数定义一个环境

`voyager/env/mineflayer/lib/observation/base.js` 的 `inject()`：

```js
bot.obsList = [ new Status(bot), new Inventory(bot), ..., new OnChat(bot), new OnError(bot) ]
bot.event = function (event_name) {
    let result = {};
    bot.obsList.forEach((obs) => {
        if (obs.name.startsWith("on") && obs.name !== event_name) return;   // 事件型：只采触发那个
        result[obs.name] = obs.observe();                                   // 状态型：每次都采
    });
    bot.cumulativeObs.push([event_name, result]);
};
bot.observe = function () {
    bot.event("observe");                    // 末尾补一条 observe
    const result = bot.cumulativeObs;
    bot.cumulativeObs = [];
    return JSON.stringify(result);
};
```

于是 `env.step()` 返回的是一个**元组列表** `[[event_name, {status, inventory, voxels, chests, <事件名>: payload}], …]`。

三条硬约束由此而来：

| # | 约束 | 谁在依赖 |
|---|---|---|
| E1 | **最后一条必须是 `observe`** | `action.py:112` `assert events[-1][0] == "observe"`；`:131` `assert i == len(events)-1` |
| E2 | 每条事件都带**全量状态快照** | `render_human_message` 从 `event["status"]/["inventory"]/["voxels"]` 取值（`:121-130`） |
| E3 | 事件型条目**只在触发时出现**，可 0 条也可多条 | `onChat` 用 `+=` 累积后 `event()`，`observe()` 后清空（`onChat.js`） |

### 1.2 两类 Observation，语义完全不同

| 类 | `name` | 何时被采 | 例子 | 语义 |
|---|---|---|---|---|
| 状态型 | 不以 `on` 开头 | **每一件事都带上** | `status` / `inventory` / `voxels` / `chests` | 环境**现在长什么样** |
| 事件型 | `on*` | 只在它自己触发时 | `onChat` / `onError` / `onSave` | 环境**刚说了什么** |

### 1.3 prompt 里因此有两类槽

`agents/action.py:105-202` 的 `render_human_message`：

| 槽 | 来源 | 行 | 形态 |
|---|---|---|---|
| `Execution error:` | `onError` 累积 | `:140-145` | `"\n".join(error_messages)` |
| **`Chat log:`** | `onChat` 累积 | `:147-152` | `"\n".join(chat_messages)`，可多条 |
| `Biome / Time / Nearby blocks / … / Inventory` | `observe` 的状态快照 | `:154-182` | 格式化单值 |
| `Critique:` | **critic**（不是 events） | `:197-200` | 单条字符串 |

**关键一条**：`Chat log:` 的渲染是 `"\n".join(chat_messages)` —— **它天生是个事件流**。
原版里 `bot.chat()` 说十句话，下一轮 prompt 里就是十行。

### 1.4 所以"环境"的形式定义

> **环境 = 每一步都必然产出、且形状固定的一坨观察。模型不能决定要不要看，只能看。**

对照"工具"：工具是模型想调才调，调不到就没有。**这就是 v4 放错位置的判据。**

---

## 二、MVE 学习循环的 prompt 槽位（逐段点出来）

### 2.1 system 侧

| 段 | 位置 | 谁填 | 每步都有？ |
|---|---|---|---|
| `PROMPT_SYSTEM`（静态） | `local_agent.py:186-231` | 常量 | ✓ |
| `_mcp_prompt_section` | `:883-915`，`:481-484` 拼接 | `actions` 注册表里 `origin=mcp` 的条目 | 有工具才出现 |
| 已学技能清单 | `:501-521` | `skills` 参数 | 有技能才出现 |
| 命中即复用 | `:522-538` | `require_skills` | 条件 |
| 学习环节取证 | `:539-540` | `require_tools` | 条件 |

### 2.2 user 侧（`local_agent.py:607-612`）

```python
user = (f"问题：{question}\n\n{inv_txt}\n" + fetch_txt
        + dim_txt + avoid_txt
        + (f"\n上一轮没通过 critic 检查，它的评语（务必针对它改）：{critique}\n" if critique else "")
        + (f"\n上一次的代码：\n{code}\n执行反馈：{fb}\n请修正后重写。" if fb else ""))
```

| 段 | 位置 | 文案 | 谁填 |
|---|---|---|---|
| `问题：` | `:607` | — | `question` |
| 文件清单 | `:607` `inv_txt` | — | 本机场景 |
| 取证要求 | `:592-606` | — | `require_tools` |
| **必须回答的维度** | `:543-549` | "逐条答到；漏一条这轮就算没完成" | `dimensions` |
| **常见答偏方式** | `:550-554` | "明确别这么答" | `avoid` |
| **上一轮评语** | `:609` | "上一轮**没通过** critic 检查" | `critique` |
| 上次代码 + 执行反馈 | `:611-612` | — | solve 内部重写 |

### 2.3 三个槽已经是 maieutic 的形状

| maieutic 产物 | 现有槽 | 文案语义 | 能否承载"持续信号" |
|---|---|---|---|
| `scaffolding.dimensions` | `dim_txt` | 必须答到，漏一条算没过 | 否（固定清单，改成"已完成"要改文案） |
| `expected_drifts.pattern` | `avoid_txt` | **常见答偏，别这么答** | **能**（预防式，可无限追加，成功失败都适用） |
| `spec-examiner.questions` | `critique` | **上一轮没通过** | 只在失败步能（成功步塞进去自相矛盾） |

**这不是巧合**：这三段就是照 maieutic 抄进去的（v3 调研）。所以"同构"已经完成 2/3，
**缺的不是通道，是频率**。

---

## 三、结论一：maieutic 的主体在**判分侧**，不在执行域

v4 §3 说"maieutic 只从执行域进"。按 §1.4 的定义复核，这一条**错了一半**：

| 半边 | 在哪 | 类比 | 每步必跑？ |
|---|---|---|---|
| 取材料 `research` | 执行域（模型写代码调） | control primitive / `bot` 的 API | 否，按需 |
| **判 + 回话 `examine`** | **判分侧** | `observe()` —— 环境每步必采 | **是** |
| 记账 `note/diagnose` | 落盘点 | `onSave` | 是 |

证据：`learn_orchestrate.py:793-794` 在**每一次** `_t` 重试里都调
`task_critic.check_task_success(..., dimensions=[_d])` → 里面就是 `maieutic_gate.check`（`task_critic.py:146-160`）。
**它已经是每步必跑的观察者了**，只是输出形状是 `(success, critique)`，不是 events。

所以真正要做的不是"新增 maieutic 环境"，而是**把已有的每步观察长成环境的样子**：

```
每步 → obs = {dimensions[1], avoid[…], critique, addressed[], open[], emergent[]}
     → 失败：critique 槽（已有）+ avoid 追加（新增一行）
     → 成功：avoid 追加 + 落账（新增一行）
```

---

## 四、结论二：「持续」不靠新通道，靠**每步必产**

现在每步已经有 critique，但**成功那一步没有信号**：`if ok1: break`（`:797-798`），
而 `critique` 槽的引导语写死"上一轮**没通过**"（`:609`）—— 成功信号塞进去会自相矛盾。

零改动下只有两条路：

| 路 | 机制 | 能持续吗 | 代价 |
|---|---|---|---|
| (a) **avoid 追加** | `_avoid = _avoid + [新偏差模式]`（`learn_orchestrate.py:627` 之后），下一轮生效 | ✓ 成功/失败都行，单调增长 | 无（文案中立） |
| (b) **critique 扩充** | 把多条事件按 `"\n".join` 拼（同 `Chat log:` 的做法） | 只限失败步 | 引导语会误导 |

**所以"持续学习信号"在零改动下 = avoid 单调增长 + 失败步 critique。**
成功步的信号只能进 avoid 或账本，**进不了 critique**。

### 4.1 顺带一个已存在的 bug（它降低了"改那一行"的成本）

`critique` 在**第一次** solve 时也可能非空 —— 断点续学把上次的追问灌进来（`:741-746`）。
那时模型会读到"上一轮**没通过** critic 检查"，可这一轮**根本还没跑过**。
**那句引导语现在就已经在说谎了**，所以它不是"不能碰的正确文本"，而是"本来就该修的一句"。

## 五、结论三：若允许改 1 行，改 `local_agent.py:609`

```diff
- + (f"\n上一轮没通过 critic 检查，它的评语（务必针对它改）：{critique}\n" if critique else "")
+ + (f"\n环境回话（针对它改）：{critique}\n" if critique else "")
```

改完之后 `critique` 从"失败评语"升级成"**环境回话**"，等价于原版 `Chat log:`：
成功步也能说话，且语义不自相矛盾。

**但这一改就不是"一行不改"了**，所以列为待拍板（§八 第 1 条）。

---

## 六、落地：三条只改数据流、不改 prompt 字符串

| # | 位置 | 改动 | 改了 prompt 字符串吗 |
|---|---|---|---|
| 1 | `learn_orchestrate.py` 在 `:757` 调 solve 前 | `_avoid = _avoid + [e["question"] for e in (r1.get("critic_gate") or {}).get("emergent") or []]`（去重、限长） | 否（avoid 本来就是 list 参数） |
| 2 | `_record()` | run_log 白名单加 `critic_gate`（或复用 `hint` 字段），把 `addressed/open/questions/emergent` 落盘 | 否 |
| 3 | `ckpt_maieutic` | 每步追加一条 `obs`（`{t, addressed, open, emergent, critique, at}`） | 否 |

第 1 条是"持续"的**唯一实现**：它让环境每步都往 `avoid` 里加一条，下一轮模型就看到新的一行
"别这么答" —— **和原版 `bot.chat()` 十句话在 `Chat log:` 里给十行，是同构的**。

---

## 七、验收（5 条，全部可 grep）

| # | 验收项 | 判据 |
|---|---|---|
| 1 | **prompt 真没改** | `git diff -U0 mve/local_agent.py` 里没有任何 `+`/`-` 行含 `必须回答的维度`、`常见答偏方式`、`上一轮没通过`、`取证要求` |
| 2 | **追问真的进了下一步** | 第 2 次 exec 的 user 里出现第 1 次 `examine.questions` 的**原文**（不是重写的另一段） |
| 3 | **avoid 持续增长** | 第 2 次 exec 的 `常见答偏方式` 段比第 1 次**多出**第 1 次 `emergent` 的条目；两次都出现的不重复 |
| 4 | **成功步也留痕** | 成功那一轮的 ckpt 里有 `obs` 记录；prompt 里**没有**多出东西（承认这个代价，别假装有） |
| 5 | **主循环没被打断** | 同 v4：L1/L2/L3 三层不变；`done` 判据不变；无新增循环包住 `solve` |

---

## 八、待拍板

1. **§五 那一行改不改**：改 → `critique` 变成"环境回话"槽，成功步也能持续；不改 → 成功步只能进 avoid。
   我建议**改**，理由是那句引导语现在就在说谎（§4.1）。
2. **`emergent` 要不要进 avoid**：进了 = 环境每步教一条新偏差；不进 = 只在失败时靠 critique。
   建议进，但要限 3 条并去重（否则 `avoid_txt` 越滚越长，`solve` 的 prompt 会膨胀）。
3. **成功步的回话写到哪儿**：ckpt（评审翻文件）/ 面板 chip（评审看得见但要多一个 UI 元素）/ 两者都写。
   建议 ckpt + `learnBar` 一个灰字，不新开区域。

---

## 九、对 v4 的三处修正（两份文件要能对得上）

| v4 的位置 | v4 原话 | 修正 |
|---|---|---|
| 结论 2 / §3 | "maieutic 只能挂在执行域" | **只对 `research` 成立**。`examine` 属判分侧（每步必跑），挂执行域就退化成"工具"（§三） |
| §5「3 + 1 个函数」 | 都当"可被模型调用的函数" | 实际是 **1 个函数**（`research`）+ **1 个观察者**（`examine`，模型不调，判分时自动跑）+ **2 个账本写手**（`diagnose` / `note`，只写不喂 prompt） |
| §一 结论 3 | "没闭合 → critic 判 false → Voyager 自己重试" | 方向对，但漏了**成功步也要回话**（现在 `if ok1: break` 直接断了信号）。补法见 §四 |

这三处不冲突、是同一件事的两面：**执行域管"取材料"，判分侧管"回话"，主循环管"下一步"**。

---

## 附：与原版对照（一张表看完）

| 原版 Voyager | MVE 现在 | 同构还差什么 |
|---|---|---|
| `obsList`（状态型每次都采） | `obs1`（每步执行观察） | 差"状态类"的固定形状（MVE 是自由 dict） |
| `onChat` → `Chat log:` | `critique` 槽（单条，且文案绑定"没通过"） | **差"多条累积"和语义中性**（§五 那一行） |
| `onError` → `Execution error:` | `obs.error` → solve 内部重写提示 | 差"错误也进 prompt 的固定槽"（MVE 只进重写提示，不进 user） |
| `observe` → `Biome/Inventory/…` | `dim_txt`（维度清单） | 差"已答到/未答到"的增量视图（现在只给静态清单） |
| `Critique:` 槽 | `critique`（同一个槽承担） | 原版这两个槽是分开的，MVE 合并成一个 → **这是 MVE 的简化，也是它的瓶颈** |
| `add_new_skill(info)` | `learn_milestone_skills` | 一致（都只在 success 时写） |
