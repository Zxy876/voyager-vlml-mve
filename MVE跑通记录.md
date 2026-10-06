# MVE 第一次跑通记录

> 2026-10-05 · `python mve/run_mve.py`

## 跑通了什么

```
出题 → Voyager 取数 → 事实集 → base 过闸 → 覆盖率比对 → verdict → mastery → 技能库更新
```

用一道**必须下钻到分图才答得全**的题（"Cloud9 输掉这个 series，首血转换上出了什么问题"），连考三轮：

| 轮 | 轨迹 | 事实数 | 覆盖 | verdict | mastery | status |
|---|---|---|---|---|---|---|
| 1 | `match_summary_report` | 19 | **50%** | partial | 0.387 薄弱 | insufficient_evidence |
| 2 | `+ execute_custom_sql`（下钻） | 25 | **100%** | correct | 0.479 进行中 | insufficient_evidence |
| 3 | 同上 | 25 | **100%** | correct | 0.565 进行中 | **progressing** |

第 1 轮技能库只有「整体概览」→ 覆盖不了分图维度 → 缺 2 个评分点。
`learn()` 从 missing 里识别出缺 `map_*` 维度 → 补一条「分图下钻」技能 → 第 2 轮覆盖率跳到 100%。

**这就是「学会编排」在本原型里的可观测形态：技能库变化 → 行为变化 → 覆盖率变化。**

## 抓到一个假阳性（值得记下来）

第一版跑出来，结论打印的是"✅ 学会了吗 —— mastery 0.387 → 0.461，上升了"，但**技能库里根本没有新技能**，覆盖率一直卡在 50%。

两个 bug：

1. **`learn()` 判断错了对象**：我用 `any("map_" in m for m in missing)` 判断，而 `missing` 里是**评分点的中文名**（"Corrode 图首血转换率"），不含 `map_`。技能永远学不会。改成按 **dimension** 判断。
2. **结论判据错了**：我用 `mastery` 上升来判定"学会了"。但 mastery 公式里 `confidence = 1 - exp(-attempts/5)` 是 attempts 的单调函数——**重复作答也会把 mastery 推高，跟有没有学会无关**。用它会得出系统性的假阳性。

现在判据改成**覆盖率轨迹**，并在输出里显式标注"mastery 会随 attempts 自然上升，不能当学习证据"。

> 这两条正是助产士第三轮追问过的东西（"用户怎么区分没拿到证据 vs 拿到了但没推动"）。纸面上讨论了，代码里还是踩了——说明写下来不等于做对。

## 纸面设计哪些被验证了

| 设计 | 验证结果 |
|---|---|
| fact = {subject, dimension, value, base, scope, source} | ✅ 跑通 |
| diff 单元 = (subject, dimension) | ✅ 跑通，没有惩罚殊途同归 |
| `base` 取 VLML 的 `denom` | ✅ 直接用，没自算 |
| 题目自声明 `min_base`（而非全局 20） | ✅ 分图题设 min_base=3，能过闸；设 20 会全灭 |
| verdict 四档 | ✅ partial → correct |
| mastery 三套阈值 + 两个 flag | ✅ low_confidence 前两轮出现，第三轮消失 |
| 叙事段 `comparable:false` | ✅ 结构与评分隔离 |
| `validated_target` | ⚠️ 结构就位，本轮未测 False 分支 |

## 还没验证的

- **真正的「学会编排」**：现在用的是 `ScriptedVoyager`，技能库是规则式的。要测模型自己能不能学，得换成 `LLMVoyager`——**需要 LLM key**。
- `evidence_status = none`（MCP 连不上）的降级路径没跑过。
- `validated_target = False`（人工导入题不计掌握度）没跑过。

## 下一步

给我一个 LLM key（`ZHIPU_API_KEY` 或 `OPENAI_API_KEY` 都行），我实现 `LLMVoyager`：
让模型自己读工具描述、自己决定调用序列、自己从 `missing_points` 里学。
**同样的循环、同样的评分，就能测出「模型自己能否学会编排」这条最致命的假设。**
