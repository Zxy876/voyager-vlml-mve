#!/usr/bin/env python3
"""Maieutic 的「提问骨架」移植 —— 把一段材料拆成**必须回答的维度** + **预判的漂移**。

来源（本机 `~/ maieutic_fork/maieutic`，逐条对照，不是凭印象）
---------------------------------------------------------------
  `src/lib/opus/prompts/scaffolding.ts:6-186`  SCAFFOLDING_SYSTEM
    · 产出 spec_gate_dimensions：学生写代码前必须做出**承诺**的具体问题
      （"Handle edge cases" FAILS / "空字符串时返回什么？" PASSES）
    · 产出 expected_divergences：drift / revision / bug 三类偏差模式
    · 硬要求：维度数**与复杂度成比例**（简单 2-3、复杂 5-7，不许固定条数）；
      每条必须针对这道题；材料太模糊时给 prompt_quality_note，不拒绝。
  `src/lib/opus/prompts/intent-diff.ts:10-45`  三类定义（本模块沿用同一份口径）
    drift    = 做的比承诺少 / 承诺过的东西没做
    revision = 换了一种仍然满足承诺的做法（**不是**错误）
    bug      = 按承诺做了但机械地失败
  `src/lib/opus/prompts/phase2-chat.ts:20-34`  追问模式：不给答案，只给反问/诊断手法

MVE 里的落点（同构映射，不新造概念）
----------------------------------
    原版概念                     MVE 对应
    学生写的 spec          →    人导入的那句话（材料）
    spec_gate_dimensions   →    课程要逐个回答的**维度**（= 学习子目标）
    expected_divergences   →    执行层的「别这么答」清单 + critic 的判据
    drift 判定             →    critic：答案少了某个维度 = drift = 这轮没学会

为什么不直接塞进 DECOMPOSE_SYSTEM
---------------------------------
原版 Voyager 的 decompose 出的是**动宾格式的操作步骤**（"Get X" / "Rank by Y"），
那是为 `reachability()` 的词面匹配服务的（拿它去撞表名/条目名）。知识型问题
没有表可撞，用动宾格式只会拆出 "Get definition of X" 这种空壳（实测就 1 条）。
所以：**数据/库场景照旧用原版 decompose，本机/知识型场景用 Maieutic 维度**。
两条路都在 task_decompose.plan 里汇合，执行层/critic 拿到的形状是一样的。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

CKPT = Path(__file__).resolve().parent / "ckpt_maieutic"

# 照 scaffolding.ts 的 SCAFFOLDING_SYSTEM 重写：把「CS1 代码题」换成
# 「领域问题/材料」，其余骨架（三条硬要求、drift 三分类、模糊材料不拒绝）原样保留。
SCAFFOLD_SYSTEM = """你是一位擅长「产婆术」（Maieutic）的课程规划助手。人会给你一句**想弄清楚的问题**
（材料可能很粗糙），你的任务不是回答它，而是产出一份**追问骨架**，让一个助手
一个问题一个问题地把这块知识答清。

你要产出四样东西：

0. milestone —— 这整份骨架本身就是一个**里程碑**：一个能被命名、能被判"达成了没有"
   的能力单元（同构课程里的一章/一个单元）。
   · name：这句短语，像教材里的单元标题（如"阿斯伯格与孤独症谱系的边界"），
     **不许**是"回答以下问题"这种空话，也不许把原问题原样抄一遍；
   · why：为什么把它算一个里程碑（答清它之后，能力上多了什么）。
   ⚠️ 里程碑**只在这里生成一次**（骨架生成时），运行时不再生成 —— 运行时做的
   只是把下面的 dimensions 一条条闭合。所以 name 要能撑住"以后复用它"这件事。

1. dimensions —— 要真正答清楚这句问题，**必须先做出承诺 / 必须回答**的具体问题。
   **每条维度都是关于「这份材料要断言的到底是什么」的一个具体问题，
   不是一个笼统的标签。**

2. expected_drifts —— 这题最可能出现的偏差模式，分三类（定义不许改）：
   · drift    —— 答得比问题要求的少，或漏掉了上面某个维度（最常见）
   · revision —— 换了一种仍然成立的答法（**不是错误**，不要当缺点报）
   · bug      —— 想答对但事实上错了（编数字、张冠李戴、把相似概念混为一谈）

3. material_note —— 材料模糊时如实说明模糊在哪；材料清楚时给 null。

硬要求（照原版 scaffolding_pre_math.ts:90-103，逐条对齐）：

- **维度条数与复杂度成比例。** 一句话能答清的题，2-3 条就够；需要多角度
  （定义 / 机制 / 区别 / 适用条件 / 常见误解）的，5-7 条。**不许产出固定条数**：
  简单题上多拆是仪式感，复杂题上少拆则失去教学意义。
- **每条维度必须具体、可判定。** 原版原文：`"Handle edge cases" FAILS.
  "What should the function return when the input string is empty?" PASSES.`
  说得出「答成什么样算答到」才算一条维度；说不出的就不要出它。
- **每条 expected_drift 必须针对这道题。** 原版原文：`"Student might write
  inefficient code" FAILS.` 「可能回答不完整」同理不合格。
- **材料太模糊、太短、或自相矛盾时，照样产出你能产出的最好骨架，并在
  material_note 里说明模糊在哪 —— 不要拒绝。**（原版 prompt_quality_note
  同一条路径。）

MVE 补充（原版没有、这个场景需要，不改变上面四条）：

- 一条维度的答案可以是**取数、推导、判断，或一条明确写出的假设承诺**
  （"假设输入为正""令 x=0，求对应的 y"都是合法答案）。
  **不许**因为"这条在库里查不到"就删掉它、或把它改写成另一个能查的问题
  —— 那只改变了你问什么，没有改变它该答什么。
- 一条维度的答案也可以是一份**清单**：「有哪些列 / 字段 / 状态 / 参数」这类
  问题，**清单整体就是一条维度的答案**，不许按项逐条展开成 N 条维度。
  实测（2026-11-12 场景 N）：问「math_examples 有哪些列、各是什么类型」被拆成
  12～13 条，其中 9 条是「example_id 列的类型是什么」—— 与「每列的类型是什么」
  完全同义，判官只能围着同一点反复追问，等于把一条维度重复做九遍。
  判据（上面第 1 条）已经说了"条数与复杂度成比例"：逐项展开是**把一条复杂维度
  改写成九条同义维度**，不比原来更细，只是更长。
- 只出「答清**这句问题**必须先回答的」。上面第 1 条的定义是"必须先做出承诺 /
  必须回答"，不是"与它相关的都算"：题面只问列名与类型，就不要另出默认值、约束、
  索引、外键依赖这类**题面没问**的面向 —— 出了就是替人加题，而且每多一条就多一轮
  追问。想记下来的写进 expected_drifts 或 material_note，不要变成维度。
- 不要造出材料里没有的具体数字/专名。维度问的是判断与承诺本身，
  不是先预设一个答案再让下游去凑。
- **维度的答案必须能在「不执行」的前提下用文字给出。** Phase1（spec 阶段）
  学生拿不到执行结果 —— 原版 maieutic 的代码编辑器要到 Phase2 才解锁。
  所以不要出「某表有多少行 / 某列非空多少行 / 两表 join 命中率是多少 /
  这道题的答案是多少」这类**只能靠真跑一次才知道数值**的维度：学生只能回答
  "我还差一次执行"，判官只能围着它追问，永远闭合不了（2026-11-13 场景 N
  实测：exercise_pool / id_answer_join 两维连问 14 轮仍未闭合）。
  该问的是**方法承诺**：「按哪一列筛、什么条件、分子分母各是什么」；
  数值留给 Phase2 执行取回、Phase3 对账。

只输出一个 JSON 对象，不要前言、不要 markdown 代码围栏。Schema：
{
  "milestone": {"name": "<里程碑名，一句短语>", "why": "<为什么它是一个里程碑>"},
  "dimensions": [
    {"id": "<snake_case 短标识>", "question": "<具体问题，一句话>",
     "why": "<为什么答清这句问题必须先回答它>"}
  ],
  "expected_drifts": [
    {"category": "drift" | "revision" | "bug", "pattern": "<这道题上具体的偏差样子>"}
  ],
  "material_note": "<字符串或 null>"
}"""

# 三档 few-shots（照 scaffolding_pre_math.ts:118-181 的用法：放在 user 轮、
# 真题面之前，用来锚定 register —— 尤其是比例原则、维度之间互不重叠、
# 以及模糊材料走 material_note 这条路径）。
#
# ★ 为什么必须有（2026-11-12 用户拍板「和助产士一样的拆题 prompt」）：
#   原版 prompt 里**没有**任何「禁止出同义维度」的条款，也没写「先别重复」；
#   它靠的是三个例子里维度彼此不重叠、每条 why 都说得出独立价值。
#   实测 MVE 缺这一段时拆出 `table_structure` 与 `record_structure` 两条问
#   同一件事的维度 → 判官在同一点上连追 4 轮 → 整篇逐字不变 → 守卫摘维、
#   `status=partial`。这不是判官的问题，是拆题时就没有「一条维度一个独立承诺」
#   的示范。
SCAFFOLD_FEW_SHOTS = """下面三个例子演示的是**拆题的粒度与比例**，不是要你照搬它的问法。
题目落在文档 / 仓库上时，把例子里的「表 / 列」换成「文件 / 字段」即可。

Example A — 简单题：

PROMPT: "math_examples 表里一共有多少条记录？"

OUTPUT:
{
  "milestone": {"name": "math_examples 表的规模与计数口径", "why": "答清它之后，后续任何按比例、按覆盖率的判断才有确定的分母。"},
  "dimensions": [
    {"id": "row_vs_distinct", "question": "数的是表的行数，还是 example_id 去重后的条数？", "why": "两者在 id 有重复时结果不同；不先说清，算出来的数别人复现不了。"},
    {"id": "empty_table", "question": "表为空时，这个数应当报 0，还是报「取不到」？", "why": "空表和查询失败是两件事，混在一起会把「没有数据」误当成「没查成」。"}
  ],
  "expected_drifts": [
    {"category": "drift", "pattern": "只写 COUNT(*)，没有交代重复 id 怎么算，等于把这个口径留给读者去猜"},
    {"category": "bug", "pattern": "在 WHERE 里顺手加了章节筛选，数出来的已经不是「整张表」"}
  ],
  "material_note": null
}

Example B — 复杂题：

PROMPT: "在 Functions 章节里，按 module_id 分组，统计 hint 非空的例子各有多少条。"

OUTPUT:
{
  "milestone": {"name": "按模块统计带提示的例题覆盖", "why": "它同时练到筛选口径、空值判定、分组与计数四件事，是这条链路的完整闭环。"},
  "dimensions": [
    {"id": "chapter_match", "question": "「属于 Functions 章节」是按 chapter 字段等于 'Functions' 判定，还是按别的字段？", "why": "章节可能有多列表示；判定用哪一列，分母就不同。"},
    {"id": "hint_not_null", "question": "「hint 非空」指 IS NOT NULL，还是还要求去掉空白串之后非空？", "why": "空串在库里不是 NULL，两种写法结果不同，这是这类统计最常见的口径分歧。"},
    {"id": "group_key", "question": "按 module_id 分组时，module_id 为空的例子算作一组，还是排除？", "why": "不交代这一点，各组的数加起来会对不上总数。"},
    {"id": "count_basis", "question": "每组的数是行数，还是去重后的 example_id 数？", "why": "与 Example A 同一类分歧；分组之后更容易被忽略。"},
    {"id": "zero_groups", "question": "某个 module 一条都不满足时，结果里要出现 0，还是干脆不出现？", "why": "决定下游读成「缺少该模块」还是「该模块为 0」，影响后续任何覆盖率计算。"},
    {"id": "result_order", "question": "结果按 module_id 升序，还是按计数降序？", "why": "只要下游要做 Top-N 或两两对比，顺序就是结论的一部分。"}
  ],
  "expected_drifts": [
    {"category": "drift", "pattern": "写了 chapter 筛选和分组，却漏掉「空 module_id 算不算一组」，各组之和与总数差几条"},
    {"category": "revision", "pattern": "用 COUNT(DISTINCT example_id) 代替 COUNT(*)，在 id 唯一时结果相同 —— 这是等价的合理写法，不是错误"},
    {"category": "bug", "pattern": "用 hint <> '' 代替 IS NOT NULL，把 NULL 行整片漏掉"}
  ],
  "material_note": null
}

Example C — 模糊题（示范 material_note 这条路径）：

PROMPT: "看看这个库。"

OUTPUT:
{
  "milestone": {"name": "这个库的可用面", "why": "在弄清「要解决什么」之前，先把「手上有什么」固定下来，是唯一能推进的一步。"},
  "dimensions": [
    {"id": "task_intent", "question": "要拿这个库回答的是哪一类问题 —— 取数、核对某个说法，还是找异常？", "why": "题面没有给任务；不先定这一条，后面拆出来的维度都是替人想象出来的。"}
  ],
  "expected_drifts": [
    {"category": "drift", "pattern": "自己假定了一个任务并据此作答，但从不把这个假定写进材料，导致判定无从对照"}
  ],
  "material_note": "题面没有说明要解决什么问题。骨架是按「先确认库里有什么」防御性生成的；在说清任务之前，教学价值有限。"
}"""


# 数据场景专用的第四档 few-shot（2026-11-12）：只示范一件事 —— **列清单型的题
# 不按列展开**。
#
# ★ 为什么单列一条（实测依据）：同题连拆 5 次，system 里写了「条数与复杂度成
#   比例（简单 2-3 条）」，也写了「列清单是一条维度的答案」，模型**照样**按表里
#   的真实列逐条展开成 12～13 条（9 条是「example_id 列的类型是什么」这种）。
#   改 prompt 规则无效 —— 它看得见 render_material 里的 9 个列名，就顺着往下写。
#   原版教粒度靠的本来就是**例子**（本文件 SCAFFOLD_FEW_SHOTS 的注释：原版没有
#   任何"禁止同义维度"的条款，靠三个例子里维度彼此不重叠），所以这里补一个
#   同型的例子，而不是再加一条规则。
SCAFFOLD_FEW_SHOTS_DB = """

Example D — 列清单型（数据场景：**不按列展开**）：

PROMPT: "math_examples 表有哪些列，各是什么类型？"

OUTPUT:
{
  "milestone": {"name": "math_examples 表的字段面", "why": "把这张表能提供什么固定下来，之后任何取数都不必再猜列名与类型。"},
  "dimensions": [
    {"id": "columns_and_types", "question": "math_examples 一共有几列，每列叫什么、在库里的真实类型是什么？", "why": "这三件事一条 information_schema 查询就能一起取回；拆成三条只会得到三份逐字相同的回答。"},
    {"id": "column_scope", "question": "「这张表的列」指它自己定义的列，还是也包括视图/关联表带出来的列？", "why": "口径不同，清单的长度就不同；不先说清，别人数出来的列数和你不一致。"}
  ],
  "expected_drifts": [
    {"category": "drift", "pattern": "只报了列数或只报了列名，类型一栏空着，或者拿「都是字符串」一句带过"},
    {"category": "bug", "pattern": "凭印象写类型（如把 VARCHAR 写成 TEXT / STRING），没有去库里的元数据核对"}
  ],
  "material_note": null
}

注意 Example D：这张表真的有 9 列，**没有**写成 9 条维度。列清单是**一条**维度的
答案 —— 逐列展开不是更细，是把同一条维度重复九遍（判官会围着同一个点反复追问）。
"""
# 数据/SQL 场景专属约束：Maieutic 拆出的维度必须有**真实的 DB 操作落点**，否则会
# 凭空编出库里不存在的业务参数（实测：对着 payroll.duckdb 编出「时薪/加班费/税率」
# 全 unreachable）。这条指令只在场景有 db_path 时追加到 system 后面。
SCAFFOLD_SYSTEM_DB = """

【数据 / SQL 场景专属约束 —— 必须严格遵守】
这道问题发生在一个**真实数据库**里，下面的「数据库结构」列出了实际存在的表和列，
以及关键列的真实取值。你拆维度时：
- **断言库内事实**的维度（某表某列的值、条数、某行内容）必须指向下面真实存在的
  表/列，且能被一条 SQL 直接答出来（如「技术部在职员工的 base_salary 合计是多少」）；
- **对题面的推导、判断、或一条明确写出的假设承诺**，不需要指向任何表 ——
  「令 x=0 求出对应的 y」「假设取值均为正」都是合法维度，**不要**为了让它
  "能查"而改写它，也不要因此删掉它；
- **只引用清单里实际存在的表名和列名**，绝不许编造表或列；
- **绝对不许**发明数据库里不存在的业务参数（如时薪、加班费率、税率、缺勤扣款、
  计件单价等）——如果某条维度需要这类参数而清单里没有对应列，**就不要出这条维度**；
- 聚合类问题（如「某部门某月应发工资」）应拆成「筛选目标人群」「取工资基数」
  「取补贴/津贴」「求和」这类指向真实列的**原子维度**，不要枚举单个实体
  （如"Get salary of Zhou Lin"），也不要把整道大题当成一条维度；
- 「这张表有哪些列 / 各是什么类型」同理**不按列展开**：列清单本身就是一条
  维度的答案（上面 MVE 补充第 2 条）；列名与类型也合成一条问（"每列叫什么、
  是什么类型"），不要拆成「列名」+「类型」两条同义维度。
- **写完自己数一遍**：若某一条维度的答案就是「一个结果集」（例：一条
  information_schema 查询同时给出列数、列名、类型），那么"列名是什么""类型是
  什么"就是同一条维度的碎片，合并成一条；题面没问的（默认值 / 约束 / 主键 /
  外键 / 索引）不要另立维度，写进 expected_drifts 或 material_note。"""


# 文档/仓库场景专属约束（2026-10-31）：只有在**真的把来源观察面取到手**时才追加。
# 对应原版 Voyager 的 render_observation —— 出题器看得见环境，才不会拆出
# 「README 里写的最后更新时间」这种环境里根本不存在的事实（H 实测踩到）。
SCAFFOLD_SYSTEM_SOURCE = """

【来源观察面专属约束 —— 必须严格遵守】
下面给出了「题面点名的那个来源里**实际存在**的内容」（文件清单 + 正文节选，
或明确写着没取到）。你拆维度时：
- 每条维度断言的事实，必须在观察面里**找得到落点**（文件里确实有这段、清单里确实
  有这个文件）；找不到落点的不许出；
- 观察面里**看不见**的东西，不许假设它存在 —— 例如观察面里没有任何日期/时间字样，
  就不许出「文件里写的更新时间是什么时候」；正文被截断时，只许检查"已给出的这部分"；
- 验证类问题要写成「**说法与观察面对照**」的形式（「X 的描述与文件里写的 Y 是否一致」），
  不要写成「X 是什么」（那会把取数当成作答，实测模型会交原文）；
- 观察面写着「没取到」时（2026-11-18 对照原版 scaffolding.ts 改口径）：
  材料还没取回 = 学生还没进执行（原版：编辑器 Phase2 才解锁），所以
  **不许出「是否已取回 / 是否取到了」这类只有执行才能证实的事实型维度**
  —— 它在 Phase1 永远闭合不了（实测 open_quiz 的 source_retrieval 维
  连续 4 轮卡死，判官自己都点了「问题在维度口径本身」）。要出就出
  **计划承诺型**维度（「你打算用哪个工具、从哪个路径把 X 取回来」——
  学生答出工具+路径就是完整承诺，判官按承诺闭合）；同样不许出以
  未取回内容为前提的判断维度。"""


# 场景**可用数据源清单**专属约束（2026-11-09，删规划员的配套）。
#
# ★ 为什么要有这一段（照原版教师端同构）：
#   原版 maieutic 的教师端骨架生成 `scaffolding.ts:262 buildScaffoldingUserMessage`
#   会把 **targetUnit / targetLanguage** 一并告诉骨架生成器 —— 也就是
#   「这个学生已经学过哪些工具」这条**环境约束**。维度因此**必然**落在
#   学生够得着的工具上（原版原文：「Tools available: … NOT yet: …」，
#   并明写「Do NOT include dimensions whose only defensible answer
#   requires tools the student hasn't learned」）。
#
#   MVE 之前把这条**环境约束**漏在了骨架之外，另设了一个「规划员」
#   （consume.py 的 _plan_routes）去判「这题走 db 还是 web」—— 两套分解，
#   互相打架：实测 Sherlock 那道题，规划员判 db:False（于是不查库、
#   只搜网页，搜回一堆应用宝介绍页），而骨架三条维度**明明写着**
#   「在 web_facts 表中查」—— 骨架已经指认了落点，规划员却把它否掉了。
#
#   现在把环境约束**放回骨架生成里**（同原版）：告诉教师端「这个场景
#   有哪些数据源可用」；消费侧直接读骨架，不再另设规划员。原版的判据是
#   「维度必须落在学生够得着的工具上」。
#
#   ★ 2026-11-11 校正（用户拍板，照原版 8e6f7ee9 的 204 行同构）：
#   环境约束只划**边界**，不是「每条维度都要取数」。原版同时明写
#   「assume valid input 也是合法承诺」。MVE 曾把它读成「每条维度必须在
#   数据源上有落点」，等于把「可判定」换成「可取证」——维度要么被逼去查库、
#   要么被标 none，而 none 在执行侧没有任何特权（require_tools 恒 True），
#   两头都答不了：查不到的卡 16 轮，查得到的在垃圾材料上通过。
SCAFFOLD_SYSTEM_SOURCES = """

【本场景可用数据源清单 —— 只划边界，不规定每条维度都去取数】
下面列出这个场景**现在真的能取到**的数据源（每一条都是事实，不是建议）。
原版教师端在这个位置给的是 targetUnit（"这个学生学过哪些工具"），同样只说明
**环境边界**，不规定每条维度都用某个工具。所以：
- 一条维度如果**必须**靠本场景之外的事实才能回答，而清单里没有能答它的数据源
  → **不要出这条维度**（原版：Do NOT include dimensions whose only defensible
  answer requires tools the student hasn't learned）；
- 一条维度如果是对题面的**判断、推导、或明确写出的假设承诺** → **不受此限**，
  照常出（原版：DO include dimensions where the valid answer is
  "assume valid input" … Those are legitimate commitments）；
- 数据源清单里没有的东西，**不许假设它存在**（例如清单里没有「小红书」，
  就不许出「她在小红书上是否活跃」这种只能靠假设的维度）；
- **不要**把「去取某个外部来源」本身当成维度，除非那个来源已经取回并
  落在下面的数据源里（这时写成「已取回的资料里，…」的对照形式）。"""

# 教材 / 数学题场景专属约束（2026-11-14）。
#
# ★ 为什么要有这一段：P 场景（72 本数学书单）此前为了绕开"空库被当成 SQL 场景"
#   的坑，直接把 scaffold 关了 —— 结果 phase1/phase2 整条助产士链路都不走，
#   每道题只做一次单解，没有维度闭合、没有 finalCode、没有技能沉淀（用户
#   2026-11-14 指出："后面的出题任务完全没走助产士拆骨架 phase1phase2"）。
#   正确做法不是关掉骨架，而是把骨架的**材料与约束**换成本场景真实的：
#   没有库、没有文件，答案靠推，不靠查。
SCAFFOLD_SYSTEM_COURSE = """

【无外部材料场景的约束 —— 必须严格遵守】
这道题的**材料全在题面里**：本场景没有数据库、没有可查的数据源（有没有外部
材料是场景的环境事实，与学科无关）。答案不是"查出来"的，是**推出来 / 算出来**的。
你拆维度时：

- 维度只能落在这些面上：
  · 题面给了哪些已知量 / 条件（分别对应哪个对象）；
  · 要求的是什么（哪个量、哪种关系）；
  · 该用哪个定义 / 定理 / 公式（把式子和适用条件写出来，
    如"相似三角形对应边成比例 ⇒ AB/DE = BC/EF"只是一个例子，
    换成语法规则、化学计量关系、逻辑等价式同样成立）；
  · 每一步怎么算（先求什么、再代入什么、单位与量纲怎么处理）；
  · 答案的合理范围与边界情况（量的取值域、方向不能反、舍去不合题意的解）。
- **不要**出「查表 / 查目录 / 查页码 / 查章节编号 / 调用哪个工具 / 读教材原文」
  这类维度：场景里没有这些材料，出了就是永远闭合不了的维度。
- 这类题的结论**不需要执行就能写出来**，所以维度应当直接问**结论**：
  「这个量等于多少」「把式子写出来并代入」「这个结果怎么验」。
  **不要**问「你将如何计算」这种只问方法的维度 —— 学生只会回「我将使用…」，
  判官无法判闭合（2026-11-14 实测：calculate_ef 连追 4 轮原地打转）。
  式子和数值由 Phase1 写出来，Phase2 再用代码**独立复核**（不是第一次求值）。
- 涉及比值 / 对应关系的维度，必须写清**方向**（谁比谁、哪两个是对应项）——
  这是这类题最常见的错法，expected_drifts 里应有一条 bug 记它。
- 维度必须落在**当前单元**的考点范围内；不要越到别的单元去出题。"""



# 原版 curriculum.propose_next_ai_task(max_retries=5)：解析失败不是"这题拆不出来"，
# 是"这次没拆成"，重发再试。MVE 原先一次失败就降级自由出题 —— 2026-10-31 加了
# 来源观察面之后（prompt 变长、why 字段变长）更容易撞上输出被截断，实测一次
# 「骨架无维度 → 释放锚点降级」就是这么来的。
SCAFFOLD_LLM_RETRIES = 5
# 原版对失败是**原样重发**，但实测同一个截断会稳定重现；重试时补一句纠正更有效，
# 这不改变判据（仍然是"拆不出就如实报 failed"，只是先把话说完）。
_RETRY_HINT = ("上一次输出不是合法 JSON（很可能被截断）。只输出那个 JSON 对象本身，"
               "不要代码围栏，why / material_note 各写一句话，维度控制在你认为必要的条数。")


def _default_llm(messages: list[dict[str, str]]) -> str:
    from llm_client import chat
    # 1200 → 2400：观察面进来后 why 写得更长，1200 会把 JSON 砍断（实测）。
    # ★ 2026-11-09 再加 `thinking=disabled`（服务器实测，场景 L 重拆失败）：
    #   GLM-4.6 默认开着思维链，**思考段与正文共用 max_tokens**。出题器是
    #   「把材料翻译成结构化维度」的活儿 —— 不需要深思，但模型的思考吃光了
    #   2400 的预算 → finish_reason=length、content 一字没有 → 解析必然失败
    #   （5 次重试全败，实测 reasoning_tokens=2400 / content=0）。
    #   关掉思考后：reasoning_tokens=0、content=1781 → 4 条维度，解析成功，
    #   且维度**全部落在可达表上**（web_facts / web_docs），material_note
    #   里模型自己写明「web_evidence 是审计账本不在可用数据源中」。
    #   ⚠️ 只关**出题器**的思考 —— 判官（reason_pass/task_critic）需要它。
    # ★ 2026-11-10 恢复出题器思考（换回 enabled + 放大预算）：观察面升级
    #   「基数+取值×计数+样本行」后，粒度判断需要整合这些事实
    #   （「item: ce(×459)」⇒ 一键多行 ⇒ 聚合表述）—— 2400 预算下
    #   思考吃光正文才截断，根因是**预算小**不是**思考有害**。
    #   教师端拆一次就固定（低频），预算给足：thinking enabled + 8000。
    #   实测（场景 L 同题对照）：disabled 拆出「item='ce' 的那行里…」
    #   （无视 ce(×459) 的单行假设）；enabled 后待验证。
    # 2026-11-18（实测）：拆骨架这一条原来没传 timeout、内部 retries=3，
    # 再套 _call_llm 的 5 次 → 最坏 15 × 90s ≈ 22 分钟才报错。期间人只看到
    # 「Voyager 正在回答」，无法判断死活（实测 00:52 那一轮卡到 11 分钟，
    # py-spy 停在 _receive_response_headers）。现在单次 90s、内部不重试，
    # 重试交给 _call_llm —— 那一层每次失败都打印原因，人能看见。
    # ★ 2026-11-18 同日实测（同一个真实题面 + 同一份 messages，四组对照）：
    #     glm-4.6    th=enabled   → ReadTimeout 60s（服务端一个字都不开始返回）
    #     glm-4.6    th=disabled  → 200，20.2s，1139 字符，解析出 3 条维度
    #     glm-4-plus th=enabled   → 200，5.0s
    #     glm-4-plus th=disabled  → 200，4.3s
    #   面板由 systemd 起（`Environment=ZHIPU_MODEL=glm-4.6`，覆盖 .env 的
    #   glm-4-plus），所以线上跑的就是 4.6 + enabled → 拆骨架必然超时，
    #   5 次重试全败后人只看到「卡住」。11-10 那条「恢复 enabled」是在
    #   4-plus 上验的，不适用于 4.6 —— 这里按 4.6 的实测回到 disabled。
    #   这也与 11-09 的原始结论一致：拆骨架是「把材料翻译成结构化维度」，
    #   不需要深思；关掉思考后维度全部落在可达表上。
    return chat(messages, temperature=0.3, max_tokens=8000,
                thinking={"type": "disabled"}, timeout=90.0, retries=1)


def _call_llm(llm: Callable | None, messages: list[dict[str, str]]) -> tuple[dict, list, str]:
    """调出题器并解析，失败按原版重试（返回 (data, dims, 最后一次失败原因)）。

    ⚠️ 失败原因要**说清是什么**（2026-11-09 服务器实测）：
      模型开着思维链时，思考段会把 max_tokens 吃光 → 正文一字没有 →
      `_parse` 找不到 JSON → 报「解析失败/空列表」。这句话会让人以为是
      prompt 写坏了，其实根因在模型适配层（thinking 吃光预算）。
      所以这里把「文本里像不像思考」也报出来，让人一眼看出方向。
    """
    last = ""
    msgs = list(messages)
    for _ in range(SCAFFOLD_LLM_RETRIES):
        try:
            text = (llm or _default_llm)(msgs)
        except Exception as e:
            last = f"LLM 调用失败：{type(e).__name__}: {e}"
            # 失败必须当场可见：原来只在 5 次全败后返回一个字符串，
            # 中间十几分钟日志一片空白（人以为卡死）。
            print(f"  · 拆骨架第 {_ + 1}/{SCAFFOLD_LLM_RETRIES} 次失败：{last[:160]}",
                  flush=True)
            continue
        data = _parse(text)
        dims = _clean_dims(data)
        if dims:
            return data, dims, ""
        # 没解析出维度 —— 看看模型是不是只说了「思考」没给 JSON
        _t = str(text or "")
        _hint = ""
        if len(_t) > 400 and not _t.lstrip().startswith("{"):
            _hint = ("（模型没给 JSON，输出像散文/思考稿 —— "
                     "多半是思考段吃光了 max_tokens；"
                     "出题器应传 thinking={'type':'disabled'}）")
        last = "LLM 输出里没有可用维度（解析失败/空列表）" + _hint
        msgs = list(messages) + [{"role": "user", "content": _RETRY_HINT}]
    return {}, [], last


def _cache_path(scene_id: str, question: str,
                db_path: str = "", req_id: str = "") -> Path:
    """骨架 + 进度账的**统一钥匙**（2026-11-09 第三刀 A：键挂在需求 id 上）。

    ★ 为什么键要换（用户 2026-11-09 判断 + 服务器取证）：
      原键 = `md5(question)[:12] + "_" + md5(db_path)[:8]` → **身份挂在问句上**。
      场景 L 实测：同一个需求三个问句 → 三套骨架并存，**维度 id 互不相同**
      （`locate_target_item` vs `filter_target_item` vs `target_item_identification`）
      → 闭合账互不认 → 学习侧验过的成果（`progress.codes` 里带 answer/r 的那条）
      消费侧读不到 → 重复拆骨架、重复学。

    ★ 照抄原版 maieutic 的 `Exercise.id`（已复核 `prisma/schema.prisma:14`）：
      原版需求身份 = **slug，教师发布时给的**，不是从题目文本算出来的；
      复用靠 `(exerciseId, studentId)` 精确二元组（`sessions.ts:159-170`）；
      全仓**无** md5 / 相似度 / embedding 检索。
      ⇒ MVE 对应物 = **req id**：人导入那一刻分配，**与问句文本完全无关**，
      之后永不改变；换问法靠归题找回同一个 req（见 `req_registry`）。

    ★ 为什么不改签名（16 处调用点零改动）：
      `_cache_path` 是骨架与进度账的**唯一入口**（progress 三本账、
      iterations、rounds、milestone_state 全走它）。改签名 = 改 16 处调用点
      = 每处都要传 req id = 传错就写到别的需求的账上。改内部实现 = 一处改，
      语义全局生效。

    ★ 键的构成不变（照抄，不改口径）：
      db_path 进键的理由与原版完全一致 —— 场景换了库 / 首次加 schema 观察面时，
      旧骨架（可能编了不存在的参数）不能继续命中，必须重拆。

    解析顺序：
      ① 有 req id → 新键 `req_<req_id>_<dbhash>.json`
      ② 新键不存在 → **按当前问句的 md5** 回退找旧键 → 命中则迁移（读时兼容）
         ⚠️ 回退**只能用当前问句的 md5**，不许扫目录取最近
            （那会拿到别的需求的骨架 = 串台，比读不到更坏）
      ③ 都没有 → 返回新键路径（首次拆骨架时写它）
    """
    sid = str(scene_id or "A")
    q = str(question or "")
    if not db_path:
        # 进度/追问历史的读写常省略 db_path；但它们必须与 scaffold 写出的缓存文件
        # 同把钥匙，否则数据场景（db_path 非空）的进度会被写到一个不存在的文件、
        # 被 save_progress 的「读不到就静默返回」吞掉 —— 断点续学与里程碑「全闭合
        # 解锁」对数据场景名存实亡。这里回退到场景规范 db_path，和 scaffold 对齐。
        try:
            import scene_profile as _sp
            db_path = str((_sp.profile(sid) or {}).get("db_path") or "")
        except Exception:
            db_path = ""
    try:
        import req_registry as _rr
    except Exception:
        _rr = None
    # ★ 2026-11-11（拆骨架不做归一）：**发布方给的身份优先**。
    #   同一句第二次发布时，按问句反查虽然也能取到最新，但两个派发并行时会
    #   互相踩；显式身份是不可争辩的那一把钥匙。（未给时才回退到问句反查）
    if _rr is not None and str(req_id or "").strip():
        try:
            return _rr.skeleton_path(sid, str(req_id).strip(), db_path)
        except Exception:
            pass
    if _rr is not None:
        try:
            _rid = _rr.req_id_of(sid, q)
            if _rid:
                _new_p = _rr.skeleton_path(sid, _rid, db_path)
                if not _new_p.exists():
                    # 读时迁移：旧键（问句 md5）有账 → 迁到新键
                    _rr.migrate_legacy(sid, q, _rid, db_path=db_path)
                return _new_p
        except Exception:
            pass
    # 没有 req 登记（自由提问 / 旧数据）→ 旧键（保持旧行为，零回归）
    h = hashlib.md5(q.strip().encode("utf-8")).hexdigest()[:12]
    dh = hashlib.md5(str(db_path or "").encode("utf-8")).hexdigest()[:8]
    return CKPT / sid / f"{h}_{dh}.json"


def _atomic_write(path: Path, data: dict) -> None:
    """原子写（tmp + os.replace）。

    为什么必须原子：骨架缓存文件的**读者**（scaffold 的缓存命中判断）和
    **写者**（scaffold 重拆 / save_progress* 三个）是不同进程（面板主进程 +
    scene_learn 子进程）。非原子写时读者可能读到半截 JSON → `json.loads`
    抛错 → 被 `except: pass` 吞掉 → 判定「缓存不可用」→ 重新调 LLM 拆骨架
    → **顺带把 progress（learned/codes/iterations）整份清空** —— 已闭合的
    维度会被重新跑一遍（2026-10-26 生产实测：base_salary_sum 明明已闭合，
    下一轮又当作未闭合重跑）。与 scenes.json 同一条铁律：登记表必须原子写。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def _read_cache(path: Path) -> tuple[dict, str]:
    """读骨架缓存，返回 (数据, 状态)：ok / missing / broken。

    ⚠️ 以前这里 `except Exception: pass` 把「文件不存在」和「文件坏了」
    一视同仁地吞掉，坏文件因此**无声无息**地触发重拆（连坐清空进度账）。
    现在分开：坏了就留证（改名存档），让下次排查有据，不靠猜。
    """
    if not path.exists():
        return {}, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), "ok"
    except Exception as e:                      # 半截文件 / 编码坏了
        try:
            _bak = path.with_name(
                f"{path.stem}.broken{int(time.time())}{path.suffix}")
            path.replace(_bak)
        except Exception:
            pass
        print(f"[maieutic] 骨架缓存读坏了（{type(e).__name__}: {e}）"
              f"→ 已留证 {path.name}.broken*，本次重新拆")
        return {}, "broken"


# ─────────────────────────────────────────────────────────────────────────────
# 原版同构：失败的子目标/任务要**跳过**，不是把同一道题反复重拆（2026-10-26 定稿）
#
#   原版两层语义（voyager.py）：
#     · 任务内：sub_goals **拆一次就固定**，失败是原地重试（inference 的 while 循环
#       不重新拆解），重试无上限但骨架不变 —— `progress = len(completed_tasks)`
#       靠的是同一份子目标清单，换一份清单这个计数就没意义了；
#     · 任务间：整个 rollout 失败 → 进 `failed_tasks` → curriculum.py:340-344
#       "Skipping to next task"，**换一个新题**再试。
#
#   MVE 此前缺的是「这条已经试过几次了」这个信号：`_iters` 每轮整体覆盖、rounds
#   恒为 1（2026-10-26 实测：一个自造口径的维度连挂 19 轮，系统完全不知道）。
#
#   ⚠️ 中途踩过的坑（保留记录，别再走回去）：曾把信号接到「指纹变化 → 重拆骨架」，
#   想用「换个拆法」实现「换个题」。生产实测 4 轮里骨架被拆了 4 次，维度 id 每次都
#   变（emp_active → active_employees → emp_active_status → emp_active_september，
#   条数从 4 涨到 5），而闭合账记的是 id → 老账全对不上 → 已闭合的 base_salary_sum
#   被当成新维度重跑还连挂 3 轮，里程碑从 0/4 退到 0/5。**骨架必须固定**，否则
#   `progress` 这个计数没有意义。
#
#   正确的落点：试满 N 轮仍不闭合的维度 → ~~按原版「跳过这条子目标」处理~~
#   ⚠️ 2026-10-29 更正：原版**没有"跳过子目标"**（见下方注），这里只把轮数当
#   「这条很顽固」的观测信号（`_exhausted_dims`），**不参与选题**；里程碑
#   只认「全部维度闭合」这一条达成口径，不假装、也不靠放弃来点亮。
# ─────────────────────────────────────────────────────────────────────────────
# 2026-10-29（用户「调度层和原 voyager 同构就好啦」）：这个阈值**不再用来
# 跳过维度**，只用来标记「这条很顽固」（面板/日志看得见）。原版
# `voyager.py:394` 的子目标循环是 `while progress < len(sub_goals)`，失败不
# 推进 progress → 下一轮还是同一条，**没有"试满 N 次就放弃"这回事**；
# 止损只有外层 `learn()` 的 `if iteration > max_iterations`。按同构，
# `milestone_unclosed` 不再排除这里算出来的维度。
GIVE_UP_AFTER_ROUNDS = 3


def _exhausted_dims(scene_id: str, question: str,
                    db_path: str = "") -> list[tuple[str, int, str]]:
    """试过 ≥ GIVE_UP_AFTER_ROUNDS 轮**仍未闭合**的维度 = 「这条很顽固」的观测信号。

    ⚠️ 2026-10-29 起**不再用于跳过**（原版没有"跳过子目标"这回事）：只是让面板/
    日志能说清"哪几条已经试了很多轮"，选题仍由 `milestone_unclosed` 全量给出。

    只读信号，不做任何词面判断：闭合与否看 progress 账，轮数看 _iters 账，
    判词取判官最后一次的原话。返回 [(维度 id, 已试轮数, 判词)]。
    """
    try:
        learned = load_progress(str(scene_id or "A"), str(question or ""))
    except Exception:
        learned = set()
    try:
        iters = load_progress_iterations(str(scene_id or "A"), str(question or ""))
    except Exception:
        iters = {}
    out: list[tuple[str, int, str]] = []
    for did, rec in (iters or {}).items():
        if not isinstance(rec, dict):
            continue
        if str(did) in learned:                  # 已闭合的不算卡住
            continue
        try:
            n = int(rec.get("rounds") or 0)
        except (TypeError, ValueError):
            n = 0
        if n >= GIVE_UP_AFTER_ROUNDS:
            out.append((str(did), n, str(rec.get("asked") or "").strip()[:120]))
    out.sort(key=lambda x: (-x[1], x[0]))
    return out


def _skeleton_dims_from_file(p: Path) -> list[dict]:
    """读一份骨架缓存里的维度清单。"""
    try:
        import json as _json
        data = _json.loads(p.read_text(encoding="utf-8"))
        return [d for d in (data.get("dimensions") or [])
                if str(d.get("id") or "").strip()]
    except Exception:
        return []


def resplit_needed(scene_id: str, question: str) -> bool:
    """这份骨架**需要重拆**吗（教师端落点写错了）？

    判据：骨架里存在一条维度，它的落点指向一个**本场景够不着的
    落点**（实测场景 L：维度写的是账本表 `web_evidence`，它在另一个
    duckdb 文件里，SQL 连不到 → 这道题在消费侧什么都查不了）。

    这不是「维度没闭合」，是**教师端把落点约束漏了** —— 原版教师端
    （`scaffolding.ts:262`）会把「学生学过哪些工具」当环境约束一次性
    说清，运行时只读不重拆；MVE 这里的落点由出题器写，出错就要修。

    两条判据（都只用**实况**，无关键词表）：
      ① 维度带 `landing`（第二刀之后生成的骨架）→ 落点不在「可达表 ∪
         远端抓手」里 ⇒ 够不着；
      ② 维度**不带** `landing`（第二刀之前的旧骨架）→ 维度文本里字面
         点名了一张**本场景存在但查不到**的表（账本表，实查得来，
         `reachable.unreachable_in_text`）⇒ 够不着。
         ★ 这条是补「现场唯一存在的坏骨架正好是旧的」这个缺口 ——
           原来只看 `landing`，对旧骨架恒返回 False，人看不到
           「该重拆」的信号，坏骨架就永远留在缓存里。

    重拆**必须由人显式触发**（不能自动重拆）：骨架「拆一次就固定」是
    铁律（按失败指纹重拆 → 维度 id 每轮变 → 闭合账全对不上）。
    """
    try:
        import reachable as _reach
        reach = set(_reach.reachable_tables(str(scene_id or "A")) or [])
        remote = set(_remote_primitive_names(str(scene_id or "A")))
    except Exception:
        return False
    try:
        p = _cache_path(str(scene_id or "A"), str(question or ""))
        if not p.exists():
            return False
        data, st = _read_cache(p)
        if st != "ok":
            return False
        for d in (data.get("dimensions") or []):
            if not isinstance(d, dict):
                continue
            ld = str(d.get("landing") or "").strip()
            if ld:
                if ld.lower() == "none":
                    continue
                if ld in reach or ld in remote:
                    continue
                # 落点写的是一个「像表名的东西」但场景里没有 → 够不着
                return True
            # ---- 旧骨架（无 landing）：字面点名了**不可达的表** → 够不着 ----
            try:
                blob = f"{d.get('question') or ''} {d.get('why') or ''}"
                if _reach.unreachable_in_text(str(scene_id or "A"), blob):
                    return True
            except Exception:
                pass
    except Exception:
        return False
    return False


def invalidate_skeleton(scene_id: str, question: str) -> dict[str, Any]:
    """**作废**一份骨架（显式动作，人触发）—— 下次拆骨架会按当前实况重来。

    为什么需要（2026-11-09 第二刀，服务器实测场景 L）：
        骨架维度写的是「在 web_evidence 表中查…」—— 那张表在**另一个**
        duckdb 文件（web_ledger.duckdb）里，SQL 连不到 → 整道题在消费侧
        落进 infer = 什么都不查。教师端（出题器）当时把账本表也展示给了
        它，拆出了走不通的维度。
        现在出题器已经被约束好（可达表清单 + 账本表标注查不到），但
        **旧骨架不会自己重拆**（「拆一次就固定」是铁律）—— 所以要有一条
        显式的作废入口。

    作废怎么做（不是删，是**改名留证**，与坏文件处理同一套）：
        `xxx.json` → `xxx.invalidated<ts>.json`
        · 留证：出问题能回溯（旧文件的进度账也一起留着，不丢历史）；
        · 生效：下次 `scaffold()` 按当前问句**缓存未命中** → 重新拆 →
          新的维度自带 `landing`（教师端约束已修好）。

    ⚠️ **进度账不迁移**（这是有意的）：那批「已闭合」是对着**错落点**
        做的（查的是够不着的表），本就不该算数；而且新拆出来的维度 id
        几乎必然与旧的不同（id 是出题器给的），迁移过去只会造成
        「已闭合的维度被当成未闭合重跑」—— 那正是「按指纹重拆」的老坑。

    返回 {"ok": bool, "why": str, "path": str}
    """
    try:
        p = _cache_path(str(scene_id or "A"), str(question or ""))
    except Exception as e:
        return {"ok": False, "why": f"算不出缓存路径：{type(e).__name__}: {e}",
                "path": ""}
    if not p.exists():
        return {"ok": False, "why": "这份题没有骨架缓存（不用作废）",
                "path": str(p)}
    try:
        bak = p.with_name(f"{p.stem}.invalidated{int(time.time())}{p.suffix}")
        p.replace(bak)
        return {"ok": True,
                "why": (f"已作废（留证：{bak.name}）—— 下次拆骨架会按当前"
                        "可达的数据源重来一遍；旧进度账留在留证文件里，"
                        "不迁移（那批闭合是对着错落点做的）"),
                "path": str(p)}
    except Exception as e:
        return {"ok": False, "why": f"作废失败：{type(e).__name__}: {e}",
                "path": str(p)}


def _remote_primitive_names(scene_id: str) -> list[str]:
    """本场景的远端/联网抓手名字（origin=net | origin=mcp）。"""
    try:
        import actions as _act
        bucket = getattr(_act, "_REGISTRY", {}).get(str(scene_id)) or {}
        if not bucket:
            try:
                _act.restore()
            except Exception:
                pass
            bucket = getattr(_act, "_REGISTRY", {}).get(str(scene_id)) or {}
        return sorted({str(a.name) for a in bucket.values()
                       if str(getattr(a, "origin", "")) in ("net", "mcp")})
    except Exception:
        return []


def _resolved_import_question(question: str, scene_id: str) -> str:
    """把「当前这句问话」找回它对应的**人导入原句**（找骨架用）。

    ★ 为什么需要这一步（2026-11-09 服务器实测，第四个根因）：
      骨架缓存键 = md5(问句)[:12] + md5(db_path)[:8]，而**问句是「人导入
      时的那句原话」**。消费时人往往换个问法再问（实测：
        导入原句 = "username为ce的女生是2007年3，26的生日 她在哪些平台活跃？用Sherlock找"
        消费问句 = "帮我查查用户名 ce 的这位女生（2007年3月26日出生）都活跃在哪些社交平台上"
      两者 md5 不同 → 按当前问句找不到骨架 → 路由判「没有骨架 → 不猜题型」
      → 不碰环境、不联网 → 只能做模型推断 → **这正是「换个问法就答不出来」
      的直接原因**（不是模型不会答）。

    ★ 怎么找回（**不发明新的打分/门槛机制**）：
      MVE **已经有**一个归题机制 —— `routing.route(question, scene_id)`
      （照 neko material_topic_mapper 的双阈值，见 mve/routing.py 模块头）。
      它把消费问句对回已有图谱节点，返回 `node_label` = **人导入原句**
      （实测 score=62 / MEDIUM_CONFIDENCE，exact_match=False 但
      meaningful 命中 12 个 chunk）。拿到原句就能算出同一个 md5 → 命中
      同一个骨架缓存。

      这是**复用**既有归题能力，不是新增一层「像不像」的判断 ——
      阈值、评分口径全在 routing 里，本函数只是把它的结论接到骨架键上。
    """
    try:
        import routing as _rt
    except Exception:
        return ""
    try:
        ri = _rt.route(str(question or ""), str(scene_id or ""))
    except Exception:
        return ""
    # 判 REJECTED = 没归上（如实不猜）→ 不拿别的题目的骨架来套
    if str(ri.get("decision") or "") == "REJECTED":
        return ""
    _q = str(question or "").strip()
    nid = str(ri.get("node_id") or "")
    lbl = str(ri.get("node_label") or "").strip()
    if not lbl:
        return ""
    if nid.startswith("import:"):
        return lbl                     # 命中的就是**人导入原句**
    # ⚠️ 2026-11-09 服务器实测（场景 L）：命中的是**问句自己**那个
    # 消费节点（id=consume:consume_<hash>，label=问句原样）—— 它拿
    # exact_match 满分，永远是最佳 → 找回的「原句」等于问句自己 →
    # md5 算到自己 → 骨架读不到 → 路由判「没有骨架」→ 整题落进 infer。
    # 修法：**排除这个节点再取下一个候选**（那个才是人导入原句）。
    # 只认 `import:` 前缀的节点 —— 骨架永远是学习侧按「人导入原句」
    # 建的，消费节点（consume: 前缀）是会话级脏账，拿它算 md5 会算到
    # 别的题目的骨架上去。
    # 不改打分口径、不加门槛（候选本来就在 match 的循环里算过）。
    try:
        ri2 = _rt.match(_q, str(scene_id or ""), exclude_ids=(nid,))
    except Exception:
        return ""
    if str(ri2.get("decision") or "") == "REJECTED":
        return ""
    nid2 = str(ri2.get("node_id") or "")
    lbl2 = str(ri2.get("node_label") or "").strip()
    if not lbl2 or not nid2.startswith("import:"):
        return ""
    return lbl2


def milestone_dimensions(scene_id: str, question: str) -> list[dict]:
    """读里程碑骨架的维度清单（只读，不调 LLM、不重新生成）。

    供消费侧「维度覆盖守卫」判定"这道题拆了几维"：导入式里程碑题（人导入的那句
    话被 scaffold 拆成多维度）的权威维度数就在这里。没骨架（自由提问、没建里程碑）
    返回空列表，守卫便回退到 _derive_dims 的维度判定，不会误伤原子题。

    ★ 找骨架是**两级**（2026-11-09 修第四个根因）：
      ① 按当前问句直接命中（同一句再问，最快）；
      ② 换了个问法 → 用**已有的** routing 归题找回「人导入原句」再命中
         （见 `_resolved_import_question` 的取证与理由）。
      两级都空 → 真的没骨架 → 如实返回空（不拿别的题目的骨架来套）。
    """
    sid = str(scene_id or "A")
    q = str(question or "")
    try:
        p = _cache_path(sid, q)
        if p.exists():
            dims = _skeleton_dims_from_file(p)
            if dims:
                return dims
    except Exception:
        pass
    # ② 换个问法 → 归题找回导入原句
    try:
        _orig = _resolved_import_question(q, sid)
        if _orig:
            p2 = _cache_path(sid, _orig)
            if p2.exists():
                dims2 = _skeleton_dims_from_file(p2)
                if dims2:
                    return dims2
    except Exception:
        pass
    return []


def _parse(text: str) -> dict[str, Any]:
    """容错解析（同构原版 fix_and_parse_json 的角色）：允许 ```json 围栏/多余前言。"""
    raw = str(text or "")
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.S)
    if m:
        raw = m.group(1)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _clean_milestone(data: dict[str, Any], question: str = "") -> dict[str, str]:
    """里程碑块（骨架**生成时**产出一次，运行时只读不生成）。

    老缓存 / 模型没给 → 用问题本身当名字（**不重建骨架**，去重名一样能用），
    判据不受影响：里程碑是否达成 = 维度有没有全闭合。
    """
    m = data.get("milestone")
    if isinstance(m, dict):
        name = str(m.get("name") or "").strip()
        if name:
            return {"name": name[:40],
                    "why": str(m.get("why") or "").strip()[:120]}
    if isinstance(m, str) and m.strip():
        return {"name": m.strip()[:40], "why": ""}
    return {"name": str(question or "").strip()[:40], "why": ""}


def _clean_dims(data: dict[str, Any]) -> list[dict[str, str]]:
    """清洗维度。

    ★ 2026-11-11：**不再产出 `landing`**（用户拍板，照原助产士同构）。
      原版 scaffolding.ts:109 的 SpecDimension 只有 id/description/rationale，
      全 204 行里 source/evidence/query/SQL 计数均为 0 —— 出题器不该知道
      「这条维度从哪儿取数」，那是把「可判定」偷换成「可取证」。
      消费侧对空 landing 已有回退（`_plan_from_scaffold` 走字面判据），
      `resplit_needed` 同样回退旧骨架分支，不会误判。
    """
    out: list[dict[str, str]] = []
    for i, d in enumerate(data.get("dimensions") or []):
        if not isinstance(d, dict):
            continue
        q = str(d.get("question") or "").strip()
        if not q:
            continue
        out.append({"id": str(d.get("id") or f"dim_{i + 1}").strip(),
                    "question": q,
                    "why": str(d.get("why") or "").strip()})
    return out


# 喂给出题器的失败账封顶（原版 `failed_tasks` 全量拼进 observation；这里是
# 一条条中文问句，比 "Mine 1 wood log" 长得多，按条数封顶而不是按字符截断，
# 截断会把问句砍半反而误导）。
FAILED_SHOWN = 8


def _ledger(scene_id: str, question: str) -> tuple[list[str], list[tuple[str, str]]]:
    """**这道题**的两本账 → (已完成问句, [(没做成的问句, 为什么)])。

    为什么作用域是「一道题」而不是整个场景（2026-10-31 两次实测定下来的）：

      · 原版两本账挂在 CurriculumAgent 上，看着是"全局"，但那个 agent 的
        生命周期里所有任务同域（都在同一个 Minecraft 世界里挖东西）；而且
        `inference()` 一进来就 `completed_tasks = []` / `failed_tasks = []`
        —— **一次调用一个域，域内才共享**。
      · MVE 的一个 scene 里问题彼此异构（抓 README 真实性 vs 面板是否上线）。
        先按"场景级"实现过一版，实测立刻串台：新题「面板是否已上线」拆出的
        6 条维度里 5 条是 README/仓库的 —— 失败账把出题器带跑了。
      · 所以作用域 = 一个里程碑（= 原版一次 inference 调用）= 一道题。
        ⚠️ 代价：骨架命中缓存就不重拆，所以这份账只在**重拆同一题**时生效
        （缓存损坏 / 面板清库 / 首次）。这是原版"decompose 只做一次"的直接
        后果，不是漏做。
    """
    path = _cache_path(scene_id, question)
    data, _st = _read_cache(path)
    if not isinstance(data, dict) or not data.get("dimensions"):
        return [], []
    pr = data.get("progress") or {}
    qmap = {str(d.get("id") or ""): str(d.get("question") or "")
            for d in (data.get("dimensions") or []) if isinstance(d, dict)}
    learned = {str(x) for x in (pr.get("learned") or [])}
    done: list[str] = []
    for did in (pr.get("learned") or []):
        q = str(qmap.get(str(did)) or "").strip()
        if q and q not in done:
            done.append(q)
    failed: list[tuple[str, str]] = []
    for it in (pr.get("iterations") or []):
        if not isinstance(it, dict):
            continue
        did = str(it.get("id") or "")
        if not did or did in learned:
            continue                       # clean_up_tasks：做成的移出 failed
        q = str(qmap.get(did) or "").strip()
        if not q or any(q == x for x, _ in failed):
            continue
        why = str(it.get("asked") or "").strip() or str(it.get("gap") or "").strip()
        failed.append((q, why))
    return done, failed


def completed_dims(scene_id: str, question: str, *,
                   limit: int = 10) -> list[str]:
    """已经闭合的维度问句（原版 `Completed tasks so far:` 的对应物，场景级）。"""
    done, _ = _ledger(scene_id, question)
    return done[:max(1, int(limit))]


def failed_dims(scene_id: str, question: str,
                *, limit: int = FAILED_SHOWN) -> list[tuple[str, str]]:
    """试过但**没做成**的维度 + 最后一轮判词 → [(问句, 为什么失败)]（场景级）。

    为什么要有这一条（原版对照，2026-10-31 核对源码后补）：

        voyager.py:inference() 里 `decompose_task(task)` 排在
        `completed_tasks = []` / `failed_tasks = []` **之前**，所以拆子目标时
        看到的正是上一阶段留下的两本账；而 `decompose_task` 的 messages 是

            [system(拆解 prompt), render_human_message(events), "Final task: ..."]

        `render_human_message` → `render_observation` 里带着
        `Completed tasks so far:` 与 `Failed tasks that are too hard:`，且这两项
        `warm_up` 都是 0（`should_include = True`）—— **每次必带、不随机**。

    所以「失败账进拆解观察面」不是新设计，是原版一直有的东西；MVE 的骨架拆解器
    此前只有 completed 侧（「已学过的节点」），failed 侧空着。

    口径（照 `clean_up_tasks`）：**已闭合的从 failed 里删掉**。
    """
    _, failed = _ledger(scene_id, question)
    return failed[:max(1, int(limit))]


def scene_sources(question: str, scene_id: str) -> list[str]:
    """这个场景**现在真的能取到**的数据源清单（事实，不是建议）。

    ★ 这是原版教师端「targetUnit / targetLanguage」的同构物（2026-11-09）：
      原版 `scaffolding.ts:262 buildScaffoldingUserMessage` 把「这个学生学过
      哪些工具」告诉骨架生成器，维度因此必然落在够得着的工具上。
      MVE 同构 = 把「这个场景有哪些数据源」告诉骨架生成器。

    为什么必须有它（删规划员的配套）：此前 MVE 把这条环境约束漏在骨架之外，
    另设「规划员」判 db/web 两套分解互相打架 —— 实测 Sherlock 那道题规划员
    判 db:False，而骨架三条维度明明写着「在 web_facts 表中查」。

    事实源全部复用**已有的**观察面（不另造）：
      · 场景库 → `curriculum_auto.db_observation`（真查表/列/取值）
      · 取证落点表 → `local_agent._ingest_tables`（web_docs/web_facts/…）
      · 联网与远端抓手 → `actions` 注册表里 origin=net / origin=mcp 的条目
    每一项都是**实查**的；查不到就不列（清单里没有 = 本场景真没有）。

    ★ 2026-11-09（第二刀，服务器实测场景 L）新增一段「可达表清单」：
      实测场景 L 的骨架有一条维度「在 web_evidence 表中查…」—— 而
      `web_evidence` 在**账本库** `web_ledger.duckdb` 里，不是数据源表。
      消费侧只连数据源库 → 那条维度查不到 → 判不出 db 路 → 整道题落进
      infer = 什么都不查。根因在教师端：**把不可达的表也展示给出题器了**。
      现在单独列一段可达表（原版 `targetUnit` 的同构物）：出题器拆维度时
      只能落在这些表上；账本表的价值（属性名与取值域最全）仍保留，
      但标明它不是落点。
    """
    out: list[str] = []
    # ⓪ 可达表清单（**原版 targetUnit 的同构物**：这个场景的「工具」= 表）
    #    放在最前面 —— 教师端第一眼就该知道「哪些表能当落点」。
    try:
        import reachable as _reach
        _reach_tbls = list(_reach.reachable_tables(str(scene_id or "A")) or [])
        if _reach_tbls:
            out.append("可查询的表（**维度只能落在这些表上**；别的表不在数据源里，"
                       "查不到，不许当落点）：" + "、".join(_reach_tbls[:20]))
    except Exception:
        pass
    # ① 场景库（真查）
    try:
        import scene_profile as _sp
        prof = _sp.profile(str(scene_id or "A")) or {}
        _dp = str(prof.get("db_path") or "")
        if _dp:
            from curriculum_auto import db_observation
            _obs = db_observation(_dp)
            if _obs:
                out.append("场景数据源（数据库，只读；下面的表/列是真有的）：\n"
                           + str(_obs))
    except Exception:
        pass
    # ② 取证落点表（联网资料落库的那几张）—— 给**当前状态**，不给一句过时的话
    # 「初始是空的」在表早就装满后就是**假信息**（场景 L 实测 web_docs 3796 行
    # / web_facts 3736 行）。而且必须带**样本**：web_docs 里装的是搜索引擎
    # 垃圾还是真探测结果，只能让模型自己看样本判断，我们不替它下结论。
    try:
        import web_ingest as _wi
        _stt = _wi.ingest_state(str(scene_id or "A"))
        if _stt:
            out.append("取证落点表（联网/远端工具取回的东西落在这里；下面是"
                       "**此刻的真实状态**：行数 / 属性 / 取值域 / 样本）：\n"
                       + "\n".join(_stt))
            out.append("⚠️ 表里装的是**上一次取回**的东西，不等于「现在还成立」；"
                       "表里没有的，不等于「不存在」（那是真空否定）；"
                       "要确认现在的情况，得再调一次对应的工具。")
    except Exception:
        pass
    # ③ 联网 / 远端抓手（origin=net / origin=mcp 的条目）
    try:
        import actions as _act
        bucket = getattr(_act, "_REGISTRY", {}).get(str(scene_id or "")) or {}
        if not bucket:
            try:
                _act.restore()
            except Exception:
                pass
            bucket = getattr(_act, "_REGISTRY", {}).get(str(scene_id or "")) or {}
        _nets = sorted({str(a.name) for a in bucket.values()
                        if str(getattr(a, "origin", "")) in ("net", "mcp")})
        if _nets:
            out.append("联网 / 远端工具（能取到场景外的信息；调它们取回的结果"
                       "会落进上面的取证落点表）：" + "、".join(_nets[:12]))
    except Exception:
        pass
    # ④ 上传的文件（人给的一次性材料）
    try:
        import upload as _upl
        _mf = str(_upl.uploads_manifest(str(scene_id or "")) or "")
        if _mf.strip():
            out.append("用户上传的文件（一次性材料，维度可以指向它）：\n"
                       + _mf[:1200])
    except Exception:
        pass
    return out


def render_material(question: str, scene_id: str, *, extras: str = "",
                    state: dict[str, Any] | None = None) -> str:
    """把「材料」渲染给模型：人问的那句话 + 这个场景现在有什么（复用观测，不另造）。

    `state` 是出参：把「这次到底喂进了哪份观察面」带回给 scaffold，用来决定
    要不要追加来源观察面专属约束（同构原版：喂了环境观察，才配得上"看得见环境"的规则）。
    """
    lines = [f"人想弄清楚的问题：{question}"]
    # 课程场景（教材书单）判一次：它没有数据源，联网/MCP 清单对它只是噪声
    # （会让骨架以为可以"上网查教材"，实测 2026-11-14）。
    _is_course = False
    try:
        import scene_profile as _spc_m
        _is_course = not bool(_spc_m.grounded(scene_id))
    except Exception:
        _is_course = False
    _done: list[str] = []
    try:
        _done += completed_dims(scene_id, question)
    except Exception:
        pass
    try:
        import task_decompose
        obs = task_decompose.scene_observation(question, scene_id)
        nodes = obs.get("nodes") or []
        ents = obs.get("entities") or {}
        for n in nodes:
            if str(n).strip() and str(n) not in _done:
                _done.append(str(n))
        if ents:
            lines.append("已确认的实体：" +
                         "；".join(f"{k}: {', '.join(v[:6])}" for k, v in list(ents.items())[:5]))
    except Exception:
        pass
    if _done:
        # 原版措辞：`Completed tasks so far: ...`（拆解时必带、不随机）
        lines.insert(1, "Completed tasks so far（已经做过/学过的，别重复拆成维度）："
                     + "；".join(_done[:24]))
        if state is not None:
            state["completed"] = len(_done[:24])
    # ---- 失败账（原版 `Failed tasks that are too hard:`，decompose_task 必带）----
    # 2026-10-31 核对源码后补：voyager.py:inference() 的 decompose_task 排在清空
    # 两本账之前，所以拆子目标时看得到上一阶段失败的账；render_observation 里
    # completed/failed 的 warm_up 都是 0 → 每次必带、不随机。MVE 的拆解器此前
    # 只有 completed 侧，"太硬的"一律看不见 → 换个新里程碑还会重蹈同一类不可答。
    try:
        _fd = failed_dims(scene_id, question)
        if _fd:
            lines.append("Failed tasks that are too hard（试过没做成的，别原样重出）："
                         + "；".join(q for q, _ in _fd))
            _why = "；".join(f"{q[:40]}（{w[:80]}）" for q, w in _fd if w)
            if _why:
                lines.append("为什么失败：" + _why)
            lines.append("（按原版规则处理失败题：**不要**原样或换个说法重出。要么拆出"
                         "它前置的、现在就能做成的子维度；要么把问法换成「说法与"
                         "观察面对照」这种可指认的形式。）")
            if state is not None:
                state["failed"] = len(_fd)
    except Exception:
        pass
    try:
        import scene_profile
        prof = scene_profile.profile(scene_id)
        lines.append(f"数据源：{prof.get('kind')}（{prof.get('why') or '本机/知识型'}）")
    except Exception:
        pass
    # 文档/仓库场景：把题面点名的**那个来源里真有什么**喂进去（2026-10-31）。
    # 数据场景靠上面那段 db_observation；文档场景一直没有同构物，于是 H 拆出了
    # 「README.md 里写的仓库最后更新时间」——README 里根本没有时间这回事。
    # 原版 Voyager 的 propose_next_task / decompose_task 都喂 render_observation，
    # 出题器看得见环境才不会拆出不存在的子目标，这里补的就是那一份。
    try:
        import source_snapshot as _ss
        _snap = _ss.snapshot(question, scene_id)
        if _snap.get("text"):
            lines.append("题面点名的来源里**实际存在**的内容（实查；维度只能指向"
                         "这里面看得见的东西）：\n" + str(_snap["text"]))
            if state is not None:
                state["snapshot"] = _ss.describe(_snap)
    except Exception:
        pass
    # ---- 场景可用数据源清单（2026-11-09，删规划员的配套）----
    # 原版教师端把「学生学过哪些工具」告诉骨架生成器（scaffolding.ts:262），
    # 维度因此必然落在够得着的工具上。MVE 同构：告诉它「这个场景有哪些
    # 数据源」，维度因此自带落点 → 消费侧直接读骨架，不再另设规划员。
    try:
        _srcs = [] if _is_course else scene_sources(question, scene_id)
        if _srcs:
            lines.append("本场景可用数据源（实查；维度只能落在这些上面）：\n"
                         + "\n".join(_srcs))
            if state is not None:
                state["sources"] = len(_srcs)
    except Exception:
        pass
    # ---- 课程场景：这道题该落在哪一课（2026-11-14）----
    # 骨架必须知道"这一题属于哪本教材的哪个单元、考什么"，否则拆出来的维度
    # 与教材进度无关（实测：当前单元 Similarity，模型却按勾股定理拆）。
    try:
        if _is_course:
            import graph_books as _gbm
            _cur = str(_gbm.render_text(scene_id, limit=1) or "").strip()
            if _cur:
                lines.append("【这道题所属的教材单元（维度必须落在这一单元的"
                             "考点范围内）】\n" + _cur)
                if state is not None:
                    state["course"] = 1
            # 真有 PDF 的书：把这一节的原文也给骨架。只给章节名时拆出来的
            # 维度是照标题猜的（实测当前单元 Similarity，维度却按勾股定理走）。
            _reading = ""
            try:
                import book_text as _bt
                _plan = _gbm.plan(scene_id)
                if _plan:
                    _bid, _i = _gbm.parse_unit_key(
                        str(_plan.get("module_id") or ""))
                    if _bid and _i:
                        # 骨架只用来定「考什么」，2000 字符够，别把 prompt 撑爆
                        _reading = _bt.unit_reading(_bid, _i, max_chars=2000)
            except Exception:
                _reading = ""
            if _reading:
                lines.append(
                    "【本单元教材原文摘录（维度要落在这一段实际讲的东西上；"
                    "OCR 的上标会丢，`4?`/`3°`/`42 = 4×4` 按幂理解，"
                    "`©…ª` 是花括号）】\n" + _reading)
                if state is not None:
                    state["reading"] = len(_reading)
    except Exception:
        pass
    if extras:
        lines.append(str(extras))
    # ⚠️ 这里**不再**再拼一次 db_observation（2026-11-09 修）：
    # 库结构已经由上面的 scene_sources ① 给过了。原来两处都拼 →
    # 场景 L（web_facts 910 列）同一份观察面出现**两次** = 114k 字符，
    # 骨架 prompt 直接被撑爆，模型看不到人导入的那句话。
    return "\n".join(lines)


def scaffold(question: str, scene_id: str = "A", *, llm: Callable | None = None,
             use_cache: bool = True, extras: str = "", db_path: str = "",
             req_id: str = "") -> dict[str, Any]:
    """生成一份追问骨架。

    返回：
        {"dimensions": [{"id","question","why"}],
         "expected_drifts": [{"category","pattern"}],
         "material_note": str|"", "origin": "llm"|"cache"|"failed", "reason": ""}

    ⚠️ 失败时不编维度（origin="failed" + reason），调用方据此退回原版 decompose。
    """
    q = str(question or "").strip()
    out: dict[str, Any] = {"dimensions": [], "expected_drifts": [],
                           "milestone": {}, "material_note": "",
                           "origin": "failed", "reason": ""}
    if not q:
        out["reason"] = "空问题"
        return out
    # 场景自带库时，把 db_path 算出来：① 决定要不要追加数据场景专属约束；
    # ② 进缓存键，避免换了库/首次加观察面时命中旧的（可能编了不存在参数的）骨架。
    if not db_path:
        try:
            import scene_profile as _sp
            db_path = str((_sp.profile(scene_id) or {}).get("db_path") or "")
        except Exception:
            db_path = ""
    path = _cache_path(scene_id, q, db_path, req_id=str(req_id or ""))
    # ⚠️ 骨架**拆一次就固定**（原版 voyager.py 同构：sub_goals 拆一次，失败的维度
    # 原地重试 / 试满 N 轮就跳过，不重拆）。曾经按「失败账指纹变化」重拆过一版，
    # 生产实测维度 id 每轮都变、闭合账全对不上（详见 GIVE_UP_AFTER_ROUNDS 的注释），
    # 已撤掉 —— 命中缓存就返回，判据只在维度层（_exhausted_dims）。
    if use_cache:
        cached, _state = _read_cache(path)
        dims = _clean_dims(cached)
        if dims:
            return {"dimensions": dims,
                    "expected_drifts": list(cached.get("expected_drifts") or []),
                    "milestone": _clean_milestone(cached, q),
                    "material_note": str(cached.get("material_note") or ""),
                    "origin": "cache", "reason": ""}
    # 先取观察面再拼 prompt：喂进了来源观察面，才配得上「看得见环境」的规则
    # （同构原版 render_observation → propose_next_task 的先后顺序）。
    _st: dict[str, Any] = {}
    _material = render_material(q, scene_id, extras=extras, state=_st)
    try:
        import scene_profile as _spc
        _course = not bool(_spc.grounded(scene_id))
    except Exception:
        _course = False
    system_content = SCAFFOLD_SYSTEM + (SCAFFOLD_SYSTEM_DB if db_path else "") \
        + (SCAFFOLD_SYSTEM_COURSE if _course else "") \
        + (SCAFFOLD_SYSTEM_SOURCE if _st.get("snapshot") else "") \
        + (SCAFFOLD_SYSTEM_SOURCES
           if (_st.get("sources") and not _course) else "")
    if _st.get("snapshot"):
        print(f"  · 拆骨架前先看来源观察面：{_st['snapshot']}", flush=True)
    if _st.get("sources"):
        print(f"  · 拆骨架前先看场景可用数据源（{_st['sources']} 组）",
              flush=True)
    if _st.get("failed"):
        print(f"  · 拆骨架前带上失败账：{_st['failed']} 条试过没做成的"
              "（原版 Failed tasks that are too hard）", flush=True)
    messages = [{"role": "system", "content": system_content},
                {"role": "user", "content": SCAFFOLD_FEW_SHOTS
                 + (SCAFFOLD_FEW_SHOTS_DB if db_path else "")
                 + "\n\n现在轮到你。下面是人给的真实题面与材料。\n\n"
                 + _material
                 + "\n\n产出追问骨架。只输出那个 JSON 对象，"
                   "不要前言、不要 markdown 代码围栏。"}]
    data, dims, _fail = _call_llm(llm, messages)
    if not dims:
        # ⚠️ 2026-10-26 拆掉「关键词兜底分解」（原 _schema_decompose）：它靠
        # 三张关键词表（聚合意图词 / 「月」→month+date+period / 「薪」→salary+wage…）
        # 把问题映射成维度 —— 那是**针对特定词面的硬编码**，与项目铁律冲突，而且
        # 它正是「哪些员工在九月是活跃的？」这类**限定词库里根本落不了地**的错位
        # 维度的源头之一。拆维度的活儿本就该由出题器（LLM）看着**实查到的库结构**
        # 做（render_material 已经把 db_observation 喂进去了）。
        # 现在：**按原版重试 5 次**（propose_next_ai_task 同口径）仍拆不出，才如实
        # 报 failed，调用方（task_decompose.plan）据此退回原版 decompose —— 不编、不猜。
        out["reason"] = _fail or "LLM 输出里没有可用维度（解析失败/空列表）"
        return out
    drifts = [d for d in (data.get("expected_drifts") or []) if isinstance(d, dict)]
    out.update(dimensions=dims, expected_drifts=drifts,
               milestone=_clean_milestone(data, q),
               material_note=str(data.get("material_note") or ""),
               origin="llm", reason="")
    try:
        # ⚠️ 新骨架写盘**不许清掉同文件里的进度账**（原版同构：completed_tasks
        # 不会因为骨架重算归零）。正常路径下骨架拆过一次就固定、不会走到这里；
        # 但缓存文件坏掉（_read_cache 判 broken，已改名留证）或人手动删过时，
        # 这里会重建 —— 若旧文件还在，progress 必须跟着搬过去，否则已闭合的
        # 维度会被当成未闭合重跑（2026-10-26 生产实测踩到）。
        _prev, _st = _read_cache(path)
        _carry = _prev.get("progress") if _st == "ok" else None
        # ★ 2026-11-14：以前只搬 `progress`，于是「重拆骨架」这一下会把
        #   phase1（追问轨迹）/ phase2（finalCode）/ phase3（分歧账）连同
        #   session（事件流）一起抹掉 —— 账本比骨架更需要保住：骨架重拆只
        #   影响往后怎么问，历史会话是既成事实。这里按白名单整段搬过去。
        _keep: dict[str, Any] = {}
        if _st == "ok":
            for _k in ("phase1", "phase2", "phase3", "session",
                       "milestone_achieved", "milestone_lit_at",
                       "milestone_verified_real", "exercise"):
                if _k in _prev:
                    _keep[_k] = _prev[_k]
        _atomic_write(path, {**data, "question": q,
                             **({"progress": _carry} if isinstance(_carry, dict)
                                else {}), **_keep})
    except Exception:
        pass
    return out


def milestone_state(scene_id: str, question: str,
                    learned_ids: list[str] | set[str], *,
                    scaffold_result: dict[str, Any] | None = None) -> dict[str, Any]:
    """里程碑达成了没有 —— **判定只读清单，不重新生成骨架**。

    口径（照 Maieutic spec-examiner 的闭合定义，`maieutic_gate` 已在用的同一条）：
      · 达成 = `gaps_still_open` 为空 = 骨架里的维度**全部闭合**；
      · 少一条就不算达成（不作废已闭合的部分 —— 它们各自已经是技能单位）。

    用户 2026-10-20 定的语义：里程碑骨架**每次跑生成一次**，运行时拆出来的
    子任务只是"闭合骨架的追问"。所以这里不调 LLM、不碰缓存，纯粹记账。
    """
    res = scaffold_result
    if res is None or not (res.get("dimensions") or []):
        _dp = ""
        try:
            import scene_profile as _sp
            _dp = str((_sp.profile(str(scene_id or "A")) or {}).get("db_path") or "")
        except Exception:
            _dp = ""
        res = scaffold(str(question or ""), str(scene_id or "A"), db_path=_dp)
    dims = [d for d in (res.get("dimensions") or [])
            if str(d.get("id") or "").strip()]
    total = len(dims)
    got = {str(x) for x in (learned_ids or [])}
    missing = [str(d.get("id")) for d in dims if str(d.get("id")) not in got]
    # 试过多轮仍不闭合的维度 = 「很顽固」的观测信号（2026-10-29 起**不用于跳过**：
    # 原版 `while progress < len(sub_goals)` 里失败不推进 progress，下一轮还攻它）。
    # 它们仍算 missing（**不假装达成**）；missing 全落在这批里 → exhausted=True，
    # 只表示"这题眼下很硬"，调用方据此**多打备注**、不据此判死。
    _ex = {d for d, _n, _c in _exhausted_dims(str(scene_id or "A"),
                                              str(question or ""))}
    given_up = [d for d in missing if d in _ex]
    ms = res.get("milestone") or {}
    # 持久化点亮标记（驾驶舱 phase-3 写）：维度全闭合 + 重跑合成题确认后落盘，
    # 让"点亮变实"在缓存里可查，而非每次惰性重算。等价于原版 inference 重试里程碑
    # 后把节点标记为达成。
    achieved_flag = bool(res.get("milestone_achieved"))
    return {
        "name": str(ms.get("name") or "").strip() or str(question or "")[:40],
        "why": str(ms.get("why") or "").strip(),
        "total": total,
        "closed": total - len(missing),
        "missing": missing,
        # 「很顽固」的维度（试过多轮仍不闭合）—— 仍是 missing，不算闭合、不跳过
        "given_up": given_up,
        # 剩下的**全是**顽固维度：只作观测（面板/日志多打一句），不据此判死
        "exhausted": bool(total) and bool(missing) and len(given_up) == len(missing),
        # 0 个维度的骨架不构成里程碑（没有可闭合的东西就别报"达成"）
        "achieved": (bool(total) and not missing) or achieved_flag,
        "lit_at": res.get("milestone_lit_at"),
        "verified_real": bool(res.get("milestone_verified_real")),
    }


def mark_milestone_achieved(scene_id: str, question: str, *,
                            fetched: bool = False) -> None:
    """把里程碑节点**持久化点亮**（对应原版 inference 重试里程碑后把子目标标记达成）。

    落盘到骨架缓存同一个文件（同一把钥匙）：`milestone_achieved` /
    `milestone_lit_at` / `milestone_verified_real`。`milestone_state` 会读回它，
    让"点亮变实"在缓存里可查、图谱/面板可渲染，而不是每次惰性重算。
    """
    path = _cache_path(scene_id, question)
    data, _state = _read_cache(path)
    data["milestone_achieved"] = True
    data["milestone_lit_at"] = time.time()
    data["milestone_verified_real"] = bool(fetched)
    # ---- 会话归档（2026-11-09 第三刀 B，照抄原版 advancePhase(sid, 4)）----
    # 原版「结束」= `currentPhase = 4` + `completedAt` + 一条 phase_transition 事件
    # （`src/lib/sessions.ts:222-249`）。**没有归档表、没有归档文件、没有快照拷贝** ——
    # 全仓 `archive` / `归档` 零命中。
    # MVE 同构：里程碑达成 = 这个需求的会话闭合 → 写 completed_at + phase=4。
    try:
        import session_state as _ss
        _ss.ensure_session(data)
        _ss.mark_archived(data, reason="里程碑达成（维度全闭合）")
    except Exception:
        pass
    try:
        _atomic_write(path, data)
    except Exception:
        pass
def drift_notes(scaffold_result: dict[str, Any], limit: int = 4) -> list[str]:
    """把预判偏差渲染成执行层能看懂的「别这么答」清单（只取 drift/bug）。"""
    out: list[str] = []
    for d in (scaffold_result.get("expected_drifts") or []):
        cat = str(d.get("category") or "").lower()
        pat = str(d.get("pattern") or "").strip()
        if not pat or cat == "revision":
            continue
        if cat in ("drift", "bug"):
            out.append(pat)
        if len(out) >= limit:
            break
    return out


def load_progress(scene_id: str, question: str) -> set[str]:
    """读这道题的维度学习进度（已答到的维度 id 集）。

    为什么需要：原版子目标制（voyager.py:403-421）里 `while progress <
    len(sub_goals)` 是**断点续学** —— 重试时已完成的子目标不重跑。
    进度就存在骨架缓存同一个文件里（同一把钥匙），不另开存储。
    """
    path = _cache_path(scene_id, question)
    data, _state = _read_cache(path)
    pr = data.get("progress") or {}
    return {str(x) for x in (pr.get("learned") or [])}


def save_progress(scene_id: str, question: str, learned_ids: list[str]) -> None:
    """写维度学习进度（断点续学用；失败静默 —— 进度丢了顶多重学）。

    ⚠️ 必须是**并集累加**，不是覆盖：逐维度闭合时每个维度一次 start_learning
    只把「这一条」传进来，若直接覆盖会把上一条已闭合的维度洗掉（实测：preset
    路径下缓存进度恒只剩最后闭合的那一条，里程碑永远翻不到 achieved）。与
    priorIterations / codes 的合并口径一致。

    ⚠️ 只改 `learned` 一个字段，**不要整包替换 progress**（2026-10-26 修）：
    以前写成 `data["progress"] = {"learned": ..., "updated": ...}`，等于把同一
    个 progress 里的 `codes`（断点续学要复用的上次跑通代码）和 `iterations`
    （跨轮追问历史 + 轮数）一起洗掉。生产实测后果：闭合一条维度后，其余维度
    的轮次账与已跑通代码全没了 → 断点续学与里程碑「全闭合解锁」再次名存实亡。
    """
    path = _cache_path(scene_id, question)
    data, _state = _read_cache(path)
    if _state == "missing":
        return
    pr = data.get("progress")
    if not isinstance(pr, dict):
        pr = {}
    _prev = set(str(x) for x in (pr.get("learned") or []))
    _new = set(str(x) for x in learned_ids)
    if _new <= _prev:        # 没有新增，不必重写
        return
    pr["learned"] = sorted(_prev | _new)
    pr["updated"] = time.time()
    data["progress"] = pr
    try:
        _atomic_write(path, data)
    except Exception:
        pass


def load_progress_codes(scene_id: str, question: str) -> dict[str, dict[str, str]]:
    """读断点续学里**上次跑通的代码**（维度 id → {"question","code"}）。

    为什么必须存代码：原版技能 = 一次成功 rollout 的 `program_code`。
    续学时这一轮**不重跑**（断点续学的本意），于是没有任何新代码 ——
    2026-10-20 实测：7/7 全闭合、里程碑达成，技能却入库 0 条。
    把上次跑通的代码留下来，续学时才复用得回那次 rollout 的成果。
    """
    path = _cache_path(scene_id, question)
    data, _state = _read_cache(path)
    pr = data.get("progress") or {}
    out: dict[str, dict[str, str]] = {}
    for c in (pr.get("codes") or []):
        if isinstance(c, dict) and str(c.get("id") or ""):
            out[str(c["id"])] = {"question": str(c.get("question") or ""),
                                 "code": str(c.get("code") or ""),
                                 "answer": str(c.get("answer") or ""),
                                 "tools": list(c.get("tools") or []),
                                 "r": float(c.get("r", -1.0))}
    return out


def save_progress_codes(scene_id: str, question: str,
                        codes: list[dict[str, Any]]) -> None:
    """把这一轮跑通的代码写进进度（与已闭合维度 id 同存一份）。

    2026-10-28 起 codes 条目带 **r**（reward.compute_r 的标量）——断点续学
    复用这份账时能分清哪条候选更可信（组内择优的账本基础）。
    """
    if not codes:
        return
    path = _cache_path(scene_id, question)
    data, _state = _read_cache(path)
    if _state == "missing":
        return
    pr = data.get("progress")
    if not isinstance(pr, dict):
        pr = {}
    merged: dict[str, dict[str, Any]] = {}
    for c in (pr.get("codes") or []):
        if isinstance(c, dict) and str(c.get("id") or ""):
            merged[str(c["id"])] = {"question": str(c.get("question") or ""),
                                    "code": str(c.get("code") or ""),
                                    "answer": str(c.get("answer") or ""),
                                    "tools": list(c.get("tools") or []),
                                    "r": float(c.get("r", -1.0)),
                                    # 事实记录：取证当轮闭合 / 语义收口闭合
                                    # ⚠️ 新字段必须进这个白名单，否则静默丢失
                                    # （"how" 2026-10-31 就丢了一次）
                                    "how": str(c.get("how") or "")}
    for c in codes:                       # 本轮新跑的覆盖旧的
        cid = str(c.get("id") or "").strip()
        if cid and (str(c.get("code") or "").strip()
                    or str(c.get("answer") or "").strip()):
            merged[cid] = {"question": str(c.get("question") or "")[:200],
                           "code": str(c.get("code") or ""),
                           "answer": str(c.get("answer") or "")[:800],
                           "tools": [str(x) for x in (c.get("tools") or [])
                                     if str(x).strip()],
                           "r": float(c.get("r", -1.0)),
                           "how": str(c.get("how") or "")}
    pr["codes"] = [{"id": k, "question": v["question"], "code": v["code"],
                    "answer": v["answer"], "tools": v["tools"],
                    "r": v["r"], "how": v["how"]}
                   for k, v in merged.items()]
    data["progress"] = pr
    # ---- 会话事件（2026-11-09 第三刀 B，照抄原版 SessionEvent）----
    # 这里是「维度闭合 + 验收跑完」的落点（code/answer/r 都已写进去），
    # 所以正好是 phase 3「已验待披露」的触发点 —— 用户原话：
    # 「验收其实在学习侧就已经跑完，只是没有答案披露」。
    # ⚠️ 事件是 **append-only**（原版 `appendSessionEvent` 语义）：
    #   只追加，从不覆盖旧事件 —— 事件流是证据链，改写历史 = 毁掉证据。
    # ⚠️ 状态与过程分开：learned/codes 是**状态**（当前值，覆盖式），
    #   session.events 是**过程**（历史，追加式）。
    try:
        import session_state as _ss
        for c in codes:                       # 本轮闭合的维度各记一条
            cid = str(c.get("id") or "").strip()
            if cid and (str(c.get("code") or "").strip()
                        or str(c.get("answer") or "").strip()):
                _ss.note_dim_closed(data, cid,
                                    r=float(c.get("r", -1.0)),
                                    how=str(c.get("how") or ""))
        _ss.ensure_session(data)
        # ★ 2026-11-11（A 原版）：这里只是**记进度**（哪几维已经取证作答完），
        #   不是"交付完成"。原来在这里 mark_verified(3) → 事件流 1→3，
        #   Phase2（写 finalCode）整段被跳过 —— 实测 Scene N 的
        #   phase_transition 只有 from=1 to=3。阶段推进交给编排层：
        #   spec 冻结 → note_phase(2)，finalCode 跑通 → note_phase(3)。
    except Exception:
        pass
    try:
        _atomic_write(path, data)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 背包回滚 —— 「再做一遍」= 账本留在册，环境恢复成没学过
# ---------------------------------------------------------------------------
# 原版取证（voyager_original/voyager/agents/curriculum.py:330-365）：
#   `update_exploration_progress` 成功就 append `completed_tasks`，失败 append
#   `failed_tasks`；`clean_up_tasks` 只做**去重 + 把完成的从 failed 里删掉**，
#   从不回滚环境。真正让"重做"有意义的是 checkpoint：背包恢复成那次之前，
#   agent 手上没有上次挖到的矿，只能真再解一遍。
#   —— 账本是历史，背包是现状。两者混在一起，"再做一遍"就退化成"照答案
#      再抄一遍"，测出来的是记忆，不是能力（用户 2026-11-15 口径）。
#
# MVE 里这道题的"背包"就是这份缓存的现状字段（图谱写的是课程节点，
# 练习维度不落图，所以背包全在 ckpt_maieutic 这一份 json 里）：
#   progress.learned     哪些维度已闭合成"会了"  → 断点续学直接跳过
#   progress.codes       上次跑通的代码          → 复用 = 抄
#   progress.iterations  每维撞了几轮墙、上次问了什么 → 卡点计数
#   phase1.addressed     追问并集                → 跨段继承 → 首轮就报"全答到了"
#   session              本段会话事件流           → 归档，新一段从 phase1 开始
#   milestone_achieved   里程碑达成标记           → 面板显示已解锁
#
# 一律**不动**的（这些是账本，面板要靠它算第 N 遍的 delta）：
#   phase1.iterations / phase2 / phase3 / exercise / dimensions / milestone
def reset_practice_state(scene_id: str, question: str, *,
                         req_id: str = "", reason: str = "") -> dict[str, Any]:
    """清空这道题的背包（现状），保留全部账本（历史）。返回清了什么。

    `req_id` 必须由调用方带上：缓存文件的钥匙是 `req_<req_id>_<dbhash>.json`
    （见 `_cache_path` 文档 ①）。不带就会走 ② 的 md5 回退 —— 那是**另一份
    骨架**，清错文件比不清更坏（把别人的账清了）。
    """
    path = _cache_path(scene_id, question, req_id=req_id)
    data, _state = _read_cache(path)
    if _state != "ok":
        return {"ok": False, "why": f"缓存不可用（{_state}）"}

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    cleared: list[str] = []
    pr = data.get("progress")
    if not isinstance(pr, dict):
        pr = {}
    for k in ("learned", "codes", "iterations"):
        if pr.get(k):
            cleared.append(f"progress.{k}")
        pr[k] = []
    pr["reset_at"] = now
    data["progress"] = pr

    p1 = data.get("phase1")
    if isinstance(p1, dict) and p1.get("addressed"):
        cleared.append("phase1.addressed")
        p1["addressed"] = []

    # 会话归档：一次重做 = 助产士意义上的一段**新会话**（Sessions +1）。
    # 事件流是 append-only 的证据链，不能删 —— 移到 sessions_past 里留证。
    s = data.get("session")
    if isinstance(s, dict) and (s.get("events") or s.get("started_at")):
        past = data.get("sessions_past")
        if not isinstance(past, list):
            past = []
        past.append({"archived_at": now, "phase": s.get("phase"),
                     "events": s.get("events") or []})
        data["sessions_past"] = past[-20:]
        data["session"] = {}
        cleared.append("session")

    if data.get("milestone_achieved"):
        cleared.append("milestone_achieved")
    data["milestone_achieved"] = False
    data["milestone_lit_at"] = None

    # 账本上记一笔"这一遍是重做"（照原版 completed_tasks 的口径：去重保留，
    # 但要知道它又跑了一次）。
    try:
        runs_before = len(runs_from_iterations(
            [x for x in ((data.get("phase1") or {}).get("iterations") or [])
             if isinstance(x, dict)]))
    except Exception:
        runs_before = 0
    hist = data.get("redos")
    if not isinstance(hist, list):
        hist = []
    hist.append({"at": now, "reason": str(reason or "")[:120],
                 "cleared": cleared, "runs_before": runs_before})
    data["redos"] = hist[-40:]

    try:
        _atomic_write(path, data)
    except Exception as e:
        return {"ok": False, "why": f"写盘失败：{type(e).__name__}: {e}"}
    return {"ok": True, "cleared": cleared, "runs_before": runs_before,
            "at": now, "redos": len(hist)}


def load_redos(scene_id: str, question: str, *,
               req_id: str = "") -> list[dict[str, Any]]:
    """读回"重做过几次、每次清了什么"（面板标「第 N 遍」用）。"""
    path = _cache_path(scene_id, question, req_id=req_id)
    data, _state = _read_cache(path)
    out = data.get("redos")
    return [x for x in out if isinstance(x, dict)] if isinstance(out, list) else []


# ---------------------------------------------------------------------------
# 追问历史（同构 maieutic 的 Phase1Iteration）
# ---------------------------------------------------------------------------
# maieutic 把每一轮记成一条 `Phase1Iteration`（schemas.ts:148-158：
# studentSpecText / opusQuestions / gapsIdentified / passed），下一轮 examiner
# 的输入里原样带上「PRIOR ITERATIONS IN THIS SESSION」
# （spec-examiner.ts:227-234 的 "you asked:\n… gaps still open: […]"）。
# 于是追问是**跨轮累积**的，学生不会在原地打转。
#
# MVE 之前只有「本轮 4 步内的 critique 回灌」（learn_orchestrate 的重试循环），
# 一跨次学习（重跑同一问题）历史就全丢 —— 同一批维度反复撞同一堵墙，看不出来
# "上次卡在哪、当时问了什么"。这里补齐那本账：
#   iterations[dim_id] = {"rounds": n, "asked": 上次追问, "gap": 上次缺口,
#                         "passed": bool, "tools": [上次调用过的动作]}
def load_progress_iterations(scene_id: str, question: str) -> dict[str, dict]:
    """读追问历史：维度 id → 上次那一轮的记录。读不到返回 {}（首次学习）。"""
    path = _cache_path(scene_id, question)
    data, _state = _read_cache(path)
    pr = data.get("progress") or {}
    out: dict[str, dict] = {}
    for it in (pr.get("iterations") or []):
        if isinstance(it, dict) and str(it.get("id") or ""):
            out[str(it["id"])] = {
                "rounds": int(it.get("rounds") or 0),
                "asked": str(it.get("asked") or ""),
                "gap": str(it.get("gap") or ""),
                "passed": bool(it.get("passed")),
                "tools": [str(x) for x in (it.get("tools") or [])
                          if str(x).strip()],
                # 步级闸门（候选A）：本次闭合问过的**干净追问清单**——
                # 闸门每步都判，跨轮的 prior_questions 从这取（同构
                # spec-examiner 的 priorIterations）。asked 是合并判词
                # （人看的），asked_q 是问句列表（机器复用），两份都要。
                "asked_q": [str(x)[:200] for x in (it.get("asked_q") or [])
                            if str(x).strip()],
            }
    return out


def save_progress_iterations(scene_id: str, question: str,
                             iterations: dict[str, dict]) -> None:
    """写追问历史（整轮结束写一次；与 learned/codes 同在一个 progress 里）。"""
    if not iterations:
        return
    path = _cache_path(scene_id, question)
    data, _state = _read_cache(path)
    if _state == "missing":
        return
    pr = data.get("progress")
    if not isinstance(pr, dict):
        pr = {}
    merged: dict[str, dict] = {}
    for it in (pr.get("iterations") or []):
        if isinstance(it, dict) and str(it.get("id") or ""):
            merged[str(it["id"])] = dict(it)
    for did, rec in iterations.items():
        if not str(did).strip():
            continue
        old = merged.get(str(did)) or {}
        merged[str(did)] = {
            "id": str(did),
            # 轮次**累加**：maieutic 的 priorIterations 是按轮累积的，
            # 重跑第 N 次时模型得知道"这题已经问过 N-1 次都没闭合"。
            "rounds": int(old.get("rounds") or 0) + int(rec.get("rounds") or 0),
            "asked": str(rec.get("asked") or "")[:300],
            "gap": str(rec.get("gap") or "")[:300],
            "passed": bool(rec.get("passed")),
            "tools": [str(x) for x in (rec.get("tools") or []) if str(x).strip()],
            # asked_q：本次闭合问过的干净追问清单（步级闸门跨轮防重复问）。
            # 与 asked（合并判词）分工：一个给机器复用，一个给人回看。
            "asked_q": [str(x)[:200] for x in (rec.get("asked_q") or [])
                        if str(x).strip()],
        }
    pr["iterations"] = list(merged.values())
    data["progress"] = pr
    try:
        _atomic_write(path, data)
    except Exception:
        pass


def attach_dimensions(scene_id: str, topic_id: str,
                      dimensions: list[dict[str, Any]], *,
                      verified: bool = False, graph: Any = None,
                      bump: bool = True,
                      tools: dict[str, list[str]] | None = None,
                      import_id: str = "",
                      import_label: str = "") -> list[str]:
    """把追问骨架的维度**挂进知识图谱**（这是"图谱靠不停学习长大"的那一步）。

    每个维度 = 一枚 kind="dimension" 的节点，边 `问题节点 -has_dimension-> 维度`。
    · 幂等：同一 (topic, dim_id) 重复学不重复建，只把证据数 +1；
      `bump=False` = 只是"补挂确认它在"（断点续学跳过重跑时用），**不重复
      加证据** —— 没有新证据就不许把证据数刷上去（2026-10-19）。
    · 学成功才 verified（verified=True），没学成功就别冒充掌握；
    · parent 找不到时挂到场景根上（边不许悬挂，add_edge 会直接丢弃）。

    `tools` = {维度id: [这一维**实测调用过**的动作名]}（执行层 tracer 记的，
    不是模型说的）。给了就为每个动作建/复用 `tool:<名>` 节点并连
    `tool:<名> -produced_by-> 维度`，**origin="observed"** —— 这是 VLML 之外
    场景的"由谁产出"来源（那边是 rubric 的 answer_spec.tool 声明式
    `knowledge_graph.py:1556`，origin="declared"；本机/知识型没有 rubric，
    只能靠实测，与 `knowledge_graph.py:2831` 裁判实测编排的 observed 同款）。
    ⚠️ 只连**这个维度自己真的调过**的动作 —— 题级/骨架级的"可用工具"不许
    连 produced_by，否则 who_produces 就废了（knowledge_graph.py:1423 的警告）。

    返回新建的节点 id 列表（面板/日志据此显示 Δ 节点数）。
    """
    dims = [d for d in (dimensions or []) if isinstance(d, dict)
            and str(d.get("question") or "").strip()]
    if not dims:
        return []
    try:
        from knowledge_graph import KnowledgeGraph, Node, Edge, PRODUCED_BY
    except Exception:
        return []
    g = graph if graph is not None else KnowledgeGraph.load()
    try:
        g.ensure_scene_root(scene_id)
    except Exception:
        pass
    tid = str(topic_id or "").strip()
    parent = f"consume:{tid}" if tid.startswith("consume_") else f"topic:{tid}"
    if parent not in g.nodes:
        parent = f"scene:{scene_id}"
    # ---- 导入批次层（用户 2026-10-20）----
    # 「每次人导入-运行学习一批内容，这批知识图谱上长出来的内容就是那次人导入
    #   的子节点，技能归属也是如此」。
    # 所以图谱要能回答"这批是哪一次导入长的"：建 `import:<批次id>` 节点，
    # 每条维度连 `import:<id> -has_dimension-> dim:...`。
    # 与 `consume:<tid>` 那层**并存**：topic 层是"这题的知识"，import 层是
    # "哪一次导入把它学出来的" —— 第二次导入同一问题时，一眼能看出复用。
    # 维度节点已存在也要补这条边（幂等去重）：第二次导入确实覆盖/复用了它。
    imp = str(import_id or "").strip()
    imp_node = f"import:{imp}" if imp else ""
    if imp_node and imp_node not in g.nodes:
        g.add_node(Node(id=imp_node, kind="import",
                        label=(str(import_label or "").strip() or (tid or imp))[:60],
                        detail={"import_id": imp, "topic": tid,
                                "source": "human_import"},
                        root_scene=str(scene_id)))
    added: list[str] = []
    for d in dims:
        label = str(d.get("question") or "").strip()
        slug = str(d.get("id") or "").strip() or \
            hashlib.md5(label.encode("utf-8")).hexdigest()[:8]
        did = f"dim:{tid or 'scene'}:{slug}"
        old = g.nodes.get(did)
        if old is not None:
            if verified:
                old.status = "verified"
                if bump:
                    old.evidence = max(int(old.evidence or 0), 1) + 1
        else:
            g.add_node(Node(id=did, kind="dimension", label=label[:90],
                            detail={"why": str(d.get("why") or ""),
                                    "from": "maieutic_spec_gate",
                                    "topic": tid},
                            root_scene=str(scene_id),
                            status=("verified" if verified else "unverified"),
                            evidence=1))
            if parent in g.nodes:
                g.add_edge(Edge(src=parent, dst=did, relation="has_dimension",
                                origin="observed", reason="Maieutic 追问骨架"))
            added.append(did)
        # 导入批次 → 维度（**节点已存在也补边**）：第二次导入同一问题时，
        # 复用到的维度也会挂在这次导入下面 —— 「这批内容是哪次导入长的」
        # 和「这次导入学到了/复用了哪些」是同一张图上的两层事实。
        if imp_node and imp_node in g.nodes:
            g.add_edge(Edge(src=imp_node, dst=did, relation="has_dimension",
                            origin="observed",
                            reason=f"导入批次 {imp} 长出/复用的维度"))
        # 实测"由谁产出"：这一维闭合时真的调用过的动作（不是猜的）。
        for t in sorted({str(x).strip() for x in (tools or {}).get(
                str(d.get("id") or ""), []) if str(x).strip()}):
            tn = f"tool:{t}"
            if tn not in g.nodes:
                g.add_node(Node(id=tn, kind="tool", label=t,
                                root_scene=str(scene_id),
                                detail={"origin": "learn_loop"}))
            g.add_edge(Edge(src=tn, dst=did, relation=PRODUCED_BY,
                            origin="observed",
                            reason=f"实测：闭合「{label[:36]}」时真的调用过",
                            confidence=0.95))
    if graph is None and (added or tools):
        try:
            g.save()
        except Exception:
            pass
    return added


def imports_of(scene_id: str, topic_id: str, dim_id: str, *,
               graph: Any = None) -> list[dict[str, str]]:
    """这条维度被**哪几次人导入**学过/复用过 —— Obsidian 式反链，不用另记账。

    用户 2026-10-20：「这批图谱上长出来的内容就是那次人导入的子节点……第二
    次导入同一问题的时候能够用上之前学的东西」，并且「掌握度上升要能被解释」。

    所以"解释"不该靠一张单独的归属表（记漏一次就说不清），而是**图上算出来**：
    `import:<批次> -has_dimension-> dim:<topic>:<slug>` 的入边就是答案。
    第二次导入时这条维度的反链里会出现两个 import 节点 —— 一眼看出"复用"。
    """
    try:
        from knowledge_graph import KnowledgeGraph
    except Exception:
        return []
    g = graph if graph is not None else KnowledgeGraph.load()
    tid = str(topic_id or "").strip()
    slug = str(dim_id or "").strip()
    if not tid or not slug:
        return []
    did = f"dim:{tid}:{slug}"
    out: list[dict[str, str]] = []
    for src in g.sources_of(did, "has_dimension"):
        if not src.startswith("import:"):
            continue
        node = g.nodes.get(src)
        out.append({"import_id": src.split(":", 1)[1],
                    "label": str(getattr(node, "label", "") or ""),
                    "node": src})
    return out


# ---------------------------------------------------------------------------
# 当前里程碑目标（驾驶舱选题锚点）
# ---------------------------------------------------------------------------
# 用户 2026-10-24 定的驾驶舱行为：「场景有未完成里程碑时，驾驶舱的选题源从
# 『LLM 自由出题』改成『该里程碑的未闭合维度』，逐维度 solve→落技能→全闭合
# 解锁；全部完成后才降级回自由出题」。这条登记就是「当前要优先闭合哪个里程碑」。
#
# 写入时机：① 人在面板点「启动 Voyager / 导入学习」时就把它设为当前目标
# （见 dashboard.py /api/learn/start）—— 人导入的意图天然是"我要学会这个"；
# ② 也提供显式 set/clear 给面板「设为当前目标」按钮用。
# 读取时机：scene_learn.py 每轮循环开头，优先于自由出题。
def _current_target_path(scene_id: str) -> Path:
    return CKPT / str(scene_id or "A") / "_current_target.json"


def set_current_target(scene_id: str, question: str) -> None:
    """把某句问句设为该场景的当前里程碑目标（人导入/手动指定时调用）。"""
    q = str(question or "").strip()
    if not q:
        return
    p = _current_target_path(str(scene_id or "A"))
    try:
        _atomic_write(p, {"question": q, "set_at": time.time()})
    except Exception:
        pass


def clear_current_target(scene_id: str) -> None:
    """取消当前里程碑锚点（此后驾驶舱降级回自由出题）。"""
    p = _current_target_path(str(scene_id or "A"))
    try:
        if p.exists():
            p.unlink()
    except Exception:
        pass


def current_target(scene_id: str) -> str:
    """当前要优先闭合的里程碑目标问句。

    优先级：① 显式 set_current_target 设的；② 没有显式设置时，默认取本场景
    **最近一次**生成的骨架缓存（即人最近导入的那句）—— 这样人刚导入的意图
    不用额外点按钮就能自动成为驾驶舱的锚点。返回 "" 表示没有可用里程碑
    （驾驶舱降级回自由出题）。
    """
    scene_id = str(scene_id or "A")
    p = _current_target_path(scene_id)
    try:
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            q = str(d.get("question") or "").strip()
            if q:
                return q
    except Exception:
        pass
    # 默认：最近一次生成的骨架缓存（按修改时间倒序，跳过寄存器文件本身）
    try:
        d = CKPT / scene_id
        files = sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime,
                       reverse=True)
        for f in files:
            if f.name.startswith("_"):
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            q = str(data.get("question") or "").strip()
            if q:
                return q
    except Exception:
        pass
    return ""


def milestone_unclosed(scene_id: str, question: str, *,
                       db_path: str = "") -> list[dict[str, Any]]:
    """该目标里程碑**还没闭合**的维度（驾驶舱优先学这些）。

    靠 load_progress（已闭合维度 id 集）与 scaffold（全部维度）对账得出 ——
    不复用 start_learning 内部状态，纯只读，驾驶舱每轮都能安全调用。

    ⚠️ 2026-10-29 改：**不再排除**「试满若干轮仍不闭合」的维度。
    原版 `voyager.py:394` 的子目标循环是

        while self.curriculum_agent.progress < len(sub_goals):
            next_task = sub_goals[self.curriculum_agent.progress]   # 失败不推进
            rollout(...)                                            # → 下一轮还是它
            update_exploration_progress(info)                       # 只记 failed

    而 `progress = len(completed_tasks)`（curriculum.py:131-132）—— **子目标失败
    不跳过、无重试上限**，止损只有外层 `learn()` 的 `if iteration > max_iterations`。
    原来这里按 `GIVE_UP_AFTER_ROUNDS=3` 把维度排除出选题，是我们自己发明的取舍
    （当时的理由是「场景 B 卡在库里没有月份的维度上」），但它把两件事混成一件：
    「这条维度不可达」和「前置能力还没长出来」（用户 2026-10-29：「查 webdoc
    可能是我们让它做的里程碑的前置能力」）。前置会在一次次重试里被技能库补上
    （`已学配方回注执行层`），跳过就等于把这条学习曲线掐断。

    `_exhausted_dims` 仍保留，但只作**观测**（面板/日志显示"这条很顽固"），
    不参与选题。
    """
    q = str(question or "").strip()
    if not q:
        return []
    try:
        sc = scaffold(q, str(scene_id or "A"), db_path=db_path)
        dims = sc.get("dimensions") or []
        if not dims:
            return []
        done = load_progress(str(scene_id or "A"), q)
        return [d for d in dims if str(d.get("id") or "") not in done]
    except Exception:
        return []


def milestone_lit(scene_id: str, question: str) -> dict[str, Any]:
    """里程碑「重试成功过没有」的**只读**查询（驾驶舱重试循环的判据）。

    只读骨架缓存文件（与 mark_milestone_achieved 同一把钥匙），不调 LLM、
    不重新生成骨架 —— 驾驶舱每轮都要查，必须便宜且无副作用。返回：
      · achieved：重跑合成题取到真数后点亮过（用户 2026-10-25 语义：
        「重试里程碑直至里程碑任务成功为止」的成功标记）；
      · verified_real：点亮时是否取到真数；
      · total_dims：骨架维度数（0 = 没骨架 / 拆不出维度 → 不构成里程碑，
        驾驶舱应释放锚点而不是空转重试）。
    """
    empty = {"achieved": False, "verified_real": False, "total_dims": 0,
             "given_up": [], "exhausted": False}
    try:
        p = _cache_path(str(scene_id or "A"), str(question or ""))
        data, _state = _read_cache(p)
        if not isinstance(data, dict) or not data:
            return dict(empty)
        dims = [d for d in (data.get("dimensions") or [])
                if isinstance(d, dict) and str(d.get("id") or "").strip()]
        _q = str(question or "")
        _done = load_progress(str(scene_id or "A"), _q)
        _ex = {d for d, _n, _c in _exhausted_dims(str(scene_id or "A"), _q)}
        _missing = [str(d.get("id")) for d in dims
                    if str(d.get("id")) not in _done]
        _gu = [d for d in _missing if d in _ex]
        return {
            "achieved": bool(data.get("milestone_achieved")),
            "verified_real": bool(data.get("milestone_verified_real")),
            "total_dims": len(dims),
            # 试过多轮仍不闭合的维度：**只作观测**（2026-10-29 起不用于跳过，
            # 原版 inference 里子目标失败不推进 progress、下一轮还攻它）
            "given_up": _gu,
            # 剩下的未闭合维度全是顽固维度 → 只表示"这题眼下很硬"，
            # 驾驶舱**多打一句备注**、继续走「重试里程碑直至成功」的循环
            "exhausted": bool(dims) and bool(_missing) and len(_gu) == len(_missing),
        }
    except Exception:
        return dict(empty)


# ---------------------------------------------------------------------------
# 整题（里程碑）层的 rollout 记账 —— 原版 learn() 的 update_exploration_progress
# ---------------------------------------------------------------------------
# 为什么必须有（2026-10-31 用户按原版指出）：原版 `voyager.py:313-336` 的
# learn() 是
#
#     while True:
#         task, context = curriculum.propose_next_task(...)   # 每轮出新题
#         rollout(task)
#         if info["success"]: skill_manager.add_new_skill(info)
#         curriculum.update_exploration_progress(info)        # 失败只记 failed
#
# 失败 **不重跑同一题**，下一轮照旧让 curriculum 出新题；只有
# `inference(task, sub_goals)` 那一层是 `while progress < len(sub_goals)`
# —— 同一题的子目标才不跳过。
#
# MVE 此前把两层混成一层：里程碑（= 一个 task）每轮把所有未闭合维度重跑一遍，
# 既不换题、也没有"整题失败"这个信号 → 一个跑不动的里程碑会永远吃掉一半预算
# （实测 H：dim3/dim4 各撞 10 轮，每轮固定占掉 6~7 轮 iteration）。
#
# 映射：一轮"把所有未闭合维度试一遍" = 一次 rollout。
#   · 本轮有维度新闭合 → 有进展（≈ progress+1），继续攻剩下的维度；
#   · 本轮 0 闭合 → 这次 rollout 失败 → 记 failed，**下一轮换题**
#     （落到自由出题），并挂一个 cooldown：先做 N 道自由出题把前置能力长出来，
#     再回来重试一次（MVE_MILESTONE_COOLDOWN，默认 1）。
#   这与"子目标不跳过"不冲突：cooldown 是整题层的节流，不是把维度判死。
MILESTONE_COOLDOWN = int(os.environ.get("MVE_MILESTONE_COOLDOWN") or 1)


def _rounds_rec(scene_id: str, question: str) -> tuple[Path, dict, dict]:
    path = _cache_path(str(scene_id or "A"), str(question or ""))
    data, _st = _read_cache(path)
    if not isinstance(data, dict):
        data = {}
    rec = data.get("milestone_rounds")
    return path, data, (dict(rec) if isinstance(rec, dict) else {})


def note_milestone_round(scene_id: str, question: str, *,
                         new_closed: int = 0) -> dict[str, Any]:
    """记一次整题 rollout 的结果（见上方原版对照）。返回记账后的 dict。

    只写 `milestone_rounds` 这一个键 —— 不碰 `progress`（learned/codes/
    iterations 三本账同住 progress，整包替换会连坐，见 save_progress 的教训）。
    """
    q = str(question or "").strip()
    if not q:
        return {}
    path, data, rec = _rounds_rec(scene_id, q)
    zero = int(rec.get("zero_rounds") or 0)
    fails = int(rec.get("fail_rounds") or 0)
    tries = int(rec.get("rounds") or 0) + 1
    if int(new_closed or 0) > 0:
        zero = 0                       # 有进展 → 不是"打不动的题"
    else:
        zero += 1
        fails += 1
        rec["cooldown"] = max(0, MILESTONE_COOLDOWN)
    rec.update({"rounds": tries, "zero_rounds": zero, "fail_rounds": fails})
    data["milestone_rounds"] = rec
    try:
        _atomic_write(path, data)
    except Exception:
        pass
    return rec


def milestone_cooldown(scene_id: str, question: str) -> int:
    """还要先做几道自由出题才回来重试这个里程碑（0 = 可以试）。"""
    try:
        _p, _d, rec = _rounds_rec(scene_id, str(question or ""))
        return max(0, int(rec.get("cooldown") or 0))
    except Exception:
        return 0


def tick_milestone_cooldown(scene_id: str, question: str) -> int:
    """自由出题做完一道 → cooldown 减一（原版"学完相邻任务再回来"的节流）。"""
    q = str(question or "").strip()
    if not q:
        return 0
    try:
        path, data, rec = _rounds_rec(scene_id, q)
        if not rec:
            return 0
        cur = max(0, int(rec.get("cooldown") or 0))
        if cur <= 0:
            return 0
        rec["cooldown"] = cur - 1
        data["milestone_rounds"] = rec
        _atomic_write(path, data)
        return rec["cooldown"]
    except Exception:
        return 0


# ─── 整篇作答（同构 maieutic Phase1Data）───────────────────────────────
# 原版：学生每一轮交的是**整篇 spec**（spec/route.ts:23-24 全量 specText，
# 上限 10k），examiner 拿「全部维度 + 当前全文 + priorIterations」判一次。
# MVE 原来只有「一问一答」的单点判定，判官看不到全文，于是单句像在答就算过
# （2026-11-11 实证：4 维全 passed、答案全错）。
# 这里只补存储与读取；判定仍走 maieutic_gate（同一条尺子）。
# 只写 `phase1` 这一个键 —— 不碰 progress（learned/codes/iterations 三本账
# 同住 progress，整包替换会连坐，见 save_progress 的教训）。


def _phase1_rec(scene_id: str, question: str) -> tuple[Path, dict, dict]:
    path = _cache_path(str(scene_id or "A"), str(question or ""))
    data, _st = _read_cache(path)
    if not isinstance(data, dict):
        data = {}
    rec = data.get("phase1")
    return path, data, (dict(rec) if isinstance(rec, dict) else {})


def load_spec_text(scene_id: str, question: str) -> str:
    """读上一轮提交的**整篇作答全文**（学生 spec 的同构物）。"""
    try:
        _p, _d, rec = _phase1_rec(scene_id, str(question or ""))
        return str(rec.get("final_spec_text") or "")
    except Exception:
        return ""


def save_spec_text(scene_id: str, question: str, spec_text: str) -> None:
    """写整篇作答全文（每轮提交覆盖；历史留在 iterations 里）。"""
    q = str(question or "").strip()
    if not q:
        return
    path, data, rec = _phase1_rec(scene_id, q)
    rec["final_spec_text"] = str(spec_text or "")[:20000]
    data["phase1"] = rec
    try:
        _atomic_write(path, data)
    except Exception:
        pass


def load_spec_addressed(scene_id: str, question: str) -> list[str]:
    """累计已答到的维度（同构 `phase1.instructorConfiguredDimensionsAddressed`）。

    原版把它挂在 phase1Data 上，每轮 append 时做并集写回（`src_lib_sessions.ts:264-266`），
    **永不回退** —— 判官原话「一旦答到就不再变回未答」。MVE 的调用方要在每次
    learn() 开头拿它当起点，否则同一道题重跑会把已答到的维度整批重新打开。

    历史文件没有这个字段 → 用各轮 `gaps_addressed` 的并集兜底（同一口径，
    只是当时没落盘）。
    """
    try:
        _p, _d, rec = _phase1_rec(scene_id, str(question or ""))
    except Exception:
        return []
    got: list[str] = [str(x) for x in (rec.get("addressed") or [])
                      if str(x or "").strip()]
    for it in (rec.get("iterations") or []):
        if not isinstance(it, dict):
            continue
        for x in (it.get("gaps_addressed") or []):
            s = str(x)
            if s and s not in got:
                got.append(s)
    return got


def load_spec_rounds(scene_id: str, question: str) -> list[dict]:
    """读轮级提交历史（同构 priorIterations，按轮序）。

    每条：{run, round, spec_text, questions[], gaps_identified[],
           gaps_addressed[], emergent[], passed, ts}
    """
    try:
        _p, _d, rec = _phase1_rec(scene_id, str(question or ""))
        return [dict(x) for x in (rec.get("iterations") or [])
                if isinstance(x, dict)]
    except Exception:
        return []


def _run_of(rec: dict) -> int:
    """取一条轮记录的 run 号；历史数据没有该字段 → 0。"""
    try:
        return int(rec.get("run") or 0)
    except Exception:
        return 0


def next_spec_run(scene_id: str, question: str) -> int:
    """下一次 Phase1 记成「第几次重跑」（原版一条 session 一段的同构物）。

    为什么需要：原版 `findOrCreateSession` 按 (exerciseId, studentId)
    只维护一条活跃会话，所以 Phase1Iteration 天然按会话分段；MVE 的
    缓存文件按 question 长期复用，重跑会把新轮**直接接在**旧数组后面，
    于是「passed 之后又冒出 4 个缺口」看起来像退步，实际是第二次重跑
    的第一轮（实测 req_20261114T155633 的 6 条就是两段拼成的）。
    不分段，缺口序列就不是序列，是两段叠在一起的假曲线。
    """
    mx = 0
    # ⚠️ 不能直接扫 `run` 字段：历史数据没有这个字段，扫出来恒为 0，
    # 重跑就会拿到 1，和旧数据的隐式第 1 段混号。走 load_spec_runs 的
    # 分段结果（它会把历史数据按 passed 切段），段号才是真实的。
    for r in load_spec_runs(scene_id, question):
        try:
            mx = max(mx, int(r.get("run") or 0))
        except Exception:
            pass
    return mx + 1


def runs_from_iterations(its: list[dict]) -> list[dict]:
    """把轮记录切段（纯函数：只吃 iterations，不碰缓存文件）。

    为什么单独抽出来（2026-11-14）：面板按**文件**遍历练习（一份 req 一个文件），
    但 `load_spec_runs(scene, question)` 是按**问句**解析缓存路径的
    （`_cache_path` → `req_id_of` 拿"这句话最新的那个 req"）。同一句发布过两次时，
    按问句读会把两份记录都读成最新那份 —— 实测：旧记录明明有 6 段会话，
    面板却显示 0 段（新 req 还没跑过）。按文件读就必须用文件自己的 iterations。
    """
    if not its:
        return []
    has_explicit = any(_run_of(it) for it in its)
    runs: list[dict] = []
    cur: dict | None = None
    prev_passed = False
    implicit = 0
    for it in its:
        r = _run_of(it)
        if not has_explicit or r <= 0:
            if cur is None or prev_passed:
                implicit += 1
            r = implicit if implicit else 1
        if cur is None or r != cur["run"]:
            cur = {"run": r, "rounds": [], "passed": False}
            runs.append(cur)
        cur["rounds"].append(it)
        cur["passed"] = bool(cur["passed"] or it.get("passed"))
        prev_passed = bool(it.get("passed"))
    for x in runs:
        x["n"] = len(x["rounds"])
        x["opens"] = [len(r.get("gaps_identified") or []) for r in x["rounds"]]
        x["closed"] = [len(r.get("gaps_addressed") or []) for r in x["rounds"]]
    return runs


def load_spec_runs(scene_id: str, question: str) -> list[dict]:
    """把轮记录按「第几次重跑」切段（原版：一条 session 一段）。

    分段规则：
      1) 记录带 `run` 就用它（本版之后写入的新数据）；
      2) 没有 `run`（历史数据）时，见到 `passed=True` 之后再出现的记录
         视为下一次重跑 —— 原版 passed 即冻结退出，同一段里不会再有
         下一轮，所以这条启发式是安全的。

    返回每段：{run, rounds[], n, opens[], closed[], passed}
    opens/closed 是该段逐轮的「缺口个数 / 本轮闭合个数」，即曲线本身。
    """
    return runs_from_iterations(load_spec_rounds(scene_id, question))


def spec_progress(scene_id: str, question: str) -> dict:
    """这道题的「缺口序列」观测 —— 同构助产士的进步账本。

    原版不看分数（`score/grade/mastery` 全库零命中），进步量是两件事：
    达到 passed 用了**几轮**、第几轮首次闭合。这里只把已有轨迹摊平成
    这两个数，不新增判据、不判答案对错。

    `delta` 只在有 ≥2 段（真的重跑过）时给，语义是「本次 - 上次」：
    轮数变负 = 收敛更快，首次闭合提前 = 更早摸到答案。
    """
    return spec_progress_of(load_spec_rounds(scene_id, question))


def spec_progress_of(its: list[dict]) -> dict:
    """`spec_progress` 的纯函数版：只吃 iterations（面板按文件读时用这个）。"""
    runs = runs_from_iterations(its)
    rows: list[dict] = []
    for r in runs:
        first = None
        for i, it in enumerate(r["rounds"], 1):
            if it.get("passed"):
                first = i
                break
        rows.append({
            "run": r["run"],
            "rounds": r["n"],
            "opens": r["opens"],
            "closed": r["closed"],
            "passed": bool(r["passed"]),
            "first_pass_round": first,
        })
    delta = None
    if len(rows) >= 2:
        a, b = rows[-2], rows[-1]
        delta = {
            "rounds": b["rounds"] - a["rounds"],
            "first_pass_round": (
                None if (a["first_pass_round"] is None
                         or b["first_pass_round"] is None)
                else b["first_pass_round"] - a["first_pass_round"]),
        }
    stuck: list[str] = []
    monotonic = True
    if runs:
        tail = runs[-1]
        if tail["rounds"]:
            stuck = [str(x) for x in
                     (tail["rounds"][-1].get("gaps_identified") or [])]
        ops = tail["opens"]
        monotonic = all(ops[i] <= ops[i - 1] for i in range(1, len(ops)))
    return {
        "runs": rows,
        "delta": delta,
        "stuck": stuck,
        "monotonic": monotonic,
    }


def save_spec_progress(scene_id: str, question: str) -> dict:
    """把缺口序列的观测快照写进 `phase1.progress`。

    为什么要落盘：`out` 只活在这一次调用的内存里（scene_learn 只打日志），
    而「第 N 次重跑比上次少用几轮」这件事要能被面板/复盘脚本直接读到，
    否则记了也没人看得见。派生量，可随时由 iterations 重算，写它只是
    省一次重算、并给外部一个稳定入口。
    """
    pr = spec_progress(scene_id, question)
    q = str(question or "").strip()
    if not q:
        return pr
    try:
        path, data, rec = _phase1_rec(scene_id, q)
        rec["progress"] = pr
        data["phase1"] = rec
        _atomic_write(path, data)
    except Exception:
        pass
    return pr


def _trim_rounds(its: list[dict], limit: int = 60) -> list[dict]:
    """截断旧轮记录时保住段边界，别把一段砍成半截。

    `its[-30:]` 那种硬截会把一段的开头切掉，`opens` 序列就缺了头，
    看起来像「一上来就只有 1 个缺口」。这里最多保留 limit+30 条，
    且从被保留段的段首开始。
    """
    if len(its) <= limit:
        return list(its)
    cut = len(its) - limit
    seg = _run_of(its[cut])
    while (cut > 0 and _run_of(its[cut - 1]) == seg
           and (len(its) - cut) < limit + 30):
        cut -= 1
    return its[cut:]


def append_spec_round(scene_id: str, question: str, rec_in: dict) -> None:
    """追加一条轮级提交记录（不覆盖、不重算）。

    `run` = 第几次重跑（原版一条 session 一段），`round` = 该段内第几轮。
    两者都由调用方给；没给就按现有轨迹推，保证旧调用点不改也能写对。
    """
    q = str(question or "").strip()
    if not q or not isinstance(rec_in, dict):
        return
    path, data, rec = _phase1_rec(scene_id, q)
    its = [dict(x) for x in (rec.get("iterations") or [])
           if isinstance(x, dict)]
    try:
        _run = int(rec_in.get("run") or 0)
    except Exception:
        _run = 0
    if _run <= 0:
        _run = next_spec_run(scene_id, q)
    _same = [x for x in its if _run_of(x) == _run]
    try:
        _rd = int(rec_in.get("round") or 0)
    except Exception:
        _rd = 0
    if _rd <= 0:
        _rd = len(_same) + 1
    its.append({
        "run": _run,
        "round": _rd,
        "spec_text": str(rec_in.get("spec_text") or "")[:20000],
        "questions": [str(x)[:200] for x in (rec_in.get("questions") or [])][:3],
        "gaps_identified": [str(x) for x in (rec_in.get("gaps_identified") or [])],
        "gaps_addressed": [str(x) for x in (rec_in.get("gaps_addressed") or [])],
        "emergent": [g for g in (rec_in.get("emergent") or [])
                     if isinstance(g, dict)][:3],
        "passed": bool(rec_in.get("passed")),
        "ts": str(rec_in.get("ts") or ""),
    })
    rec["iterations"] = _trim_rounds(its)
    # ★ 累计已答集（同构 `phase1.instructorConfiguredDimensionsAddressed`）：
    #   `src_lib_sessions.ts:264-266` 每轮 append 时
    #   `new Set(旧) ∪ gapsAddressedThisRound` 写回 phase1Data，**从不重置**。
    #   MVE 原来只把它放在 learn() 的局部变量里，调用一结束就没了 —— 重跑时
    #   上一段已答到的维度全部变回「还开着」（实测 r3 passed 后 r4 又 open=4）。
    #   落成字段还顺带解决了截断问题：`_trim_rounds` 砍掉的老轮次不会让并集缩水。
    _acc: list[str] = [str(x) for x in (rec.get("addressed") or [])]
    for _x in (rec_in.get("gaps_addressed") or []):
        _s = str(_x)
        if _s and _s not in _acc:
            _acc.append(_s)
    rec["addressed"] = _acc
    rec["final_spec_text"] = str(rec_in.get("spec_text") or "")[:20000]
    data["phase1"] = rec
    try:
        _atomic_write(path, data)
    except Exception:
        pass


def append_session_event(scene_id: str, question: str, kind: str,
                         payload: dict | None = None) -> None:
    """追加一条会话事件（append-only，原版 `appendSessionEvent` 同款）。

    为什么要它（用户 2026-11-11）：「把当前 voyager 在跑这个 session 的
    记忆、日志当作整篇 —— 这个是增量的。」

    ⇒ 「整篇」不是轮末从几个 dict 现拼的一份 Q&A，而是**这个 session
      自己走过的全过程**：每条维度问了什么、调了什么工具、跑了什么代码、
      拿到什么结果、被判成什么，一条条追加，只增不改。

    这样判官核的是**过程**（代码 + 实际结果），而不是要求模型把 175 个
    ID 逐字写进一句话里 —— 后者在 answer 通道（2500 字上限）上根本做不到，
    于是只能在「用省略号」和「整段搬运材料」之间反复被拒（2026-11-11 实测
    Scene N 三条轮次都是这两条拒绝交替）。
    """
    q = str(question or "").strip()
    if not q:
        return
    try:
        path, data, rec = _phase1_rec(scene_id, q)
        import session_state as _ss
        _ss.append_event(data, kind, payload or {})
        data["phase1"] = rec
        _atomic_write(path, data)
    except Exception:
        pass


def load_session_events(scene_id: str, question: str) -> list[dict]:
    """读会话事件流（append-only，按发生顺序，不截断）。"""
    try:
        _p, data, _rec = _phase1_rec(scene_id, str(question or ""))
        s = data.get("session") or {}
        return [e for e in (s.get("events") or []) if isinstance(e, dict)]
    except Exception:
        return []


def note_phase(scene_id: str, question: str, to: int, *,
               reason: str = "") -> None:
    """推进会话阶段（原版 `advancePhase` 同款，只许前进）。

    为什么要这个入口（2026-11-11 用户：「phase2 对 voyager 来说才是真正
    沉淀积累技能的」）：

      原版 4 个阶段 = 1 spec 追问 / 2 **写代码（技能沉淀）** / 3 复盘
      （spec vs finalCode 的 divergence）/ 4 归档。MVE 之前只在
      `save_progress_codes` 里调 `mark_verified(3)` → 事件流里只有
      `1→3`，「学习中」这一段**从来没出现过**（实测 Scene N 的
      phase_transition 只有一条 from=1 to=3）。

      补上 `1→2`：拆完骨架、开始逐条闭合维度 = 进入学习中；
      里程碑组合出的**整题程序**跑通入库 = mark_verified(3)。
      这样时间线才是 1→2→3，面板上"学习中"这一段有真实内容。

    失败静默（阶段推进不该把学习流程带崩）。
    """
    q = str(question or "").strip()
    if not q:
        return
    try:
        path, data, rec = _phase1_rec(scene_id, q)
        import session_state as _ss
        _ss.ensure_session(data)
        _ss.set_phase(data, int(to), reason=reason or "")
        data["phase1"] = rec
        _atomic_write(path, data)
    except Exception:
        pass


# ─── Phase 2（finalCode 落盘）───────────────────────────────────────────────
# ★ 2026-11-14（用户：「开始修 phase1 到之后的 phase 的帐」）：
#   原版 `Phase2Data`（`src_lib_opus_schemas.ts`）有
#   `opusExchanges / revisions / currentCode / finalCode / submittedAt` ——
#   Phase1 冻结的 spec 与 Phase3 对账的 code 都在**同一条会话记录**上。
#   MVE 这边 Phase1 写 `phase1`、Phase3 写 `phase3`，唯独 Phase2 只活在
#   `learn()` 的返回值 `out["phase2"]` 里：run_log / coach_log 都不收它，
#   面板 `learn_recent` 读的也是内存 `_LAST` —— **进程一重启就没了**。
#   后果实测（Scene P 三条记录）：phase1 有 4~10 轮追问轨迹、phase3 有分歧，
#   中间的 finalCode 却是空白，"这道题到底交没交码、跑通没跑通"无从查证，
#   学员卡的「Spec iterations / Divergences」也没法与 Phase2 对上。
#   所以补这个入口：只写 `phase2` 一个键（不碰 phase1 的冻结面）。
#   字段名照原版（final_code / submitted_at），verified / divergences 是
#   MVE 的判据（submit 路由那一次 intent-diff），另记 run 号以便按段对齐。


def _phase2_rec(scene_id: str, question: str) -> tuple[Path, dict, dict]:
    path = _cache_path(str(scene_id or "A"), str(question or ""))
    data, _st = _read_cache(path)
    if not isinstance(data, dict):
        data = {}
    rec = data.get("phase2")
    return path, data, (dict(rec) if isinstance(rec, dict) else {})


def load_phase2(scene_id: str, question: str) -> dict[str, Any]:
    """读 Phase2 的账（finalCode 提交与对账结果）。没记过 → 空 dict。"""
    try:
        _p, _d, rec = _phase2_rec(scene_id, str(question or ""))
        return rec
    except Exception:
        return {}


def save_phase2(scene_id: str, question: str, rec_in: dict[str, Any]) -> None:
    """写 Phase2 的账（只写 `phase2` 一个键）。

    与 `save_phase3` 同款：读改写整份缓存，但只替换自己那个键 ——
    phase1 的 iterations / addressed / final_spec_text 是 Phase3 的对账面，
    必须冻结（原版 `phase3` 的注释同一条口径）。
    """
    q = str(question or "").strip()
    if not q or not isinstance(rec_in, dict):
        return
    path, data, _old = _phase2_rec(scene_id, q)
    code = str(rec_in.get("final_code") or rec_in.get("code") or "")
    rec = {
        "final_code": code[:20000],
        "code_chars": len(code),
        "submitted": bool(rec_in.get("submitted")),
        "submitted_at": str(rec_in.get("submitted_at") or ""),
        "verified": bool(rec_in.get("verified")),
        "verified_by": str(rec_in.get("verified_by") or ""),
        "rounds": int(rec_in.get("rounds") or 0),
        "divergences": int(rec_in.get("divergences") or 0),
        "diff_known": bool(rec_in.get("diff_known")),
        "open_items": [str(x)[:80] for x in (rec_in.get("open_items") or [])][:8],
        "critique": str(rec_in.get("critique") or "")[:1200],
        "smoke": dict(rec_in.get("smoke") or {}),
        # 这一遍的"起点"（2026-11-15）：注入了几条配方、是不是从上一版代码起步。
        # 白名单不写这两项，上面 Phase2 算得再准也存不进来（`save_phase2` 是
        # 逐字段挑的，不是整份透传）。
        "skills": [str(x)[:60] for x in (rec_in.get("skills") or [])][:8],
        "draft_from": str(rec_in.get("draft_from") or "")[:120],
        "why": str(rec_in.get("why") or "")[:300],
        "run": int(rec_in.get("run") or 0),
    }
    # ★ 覆盖前先把上一份推进历史（2026-11-15）：`phase2` 只有一个槽位，
    #   「再做一遍」会把上一遍的账**覆盖掉** —— 第 1 遍明明交过码，
    #   面板却因为找不到它的 phase2 判成"没做完"（实测：重做后卡面
    #   完成 1/2，第 1 遍被标红）。账本要按段留，现状才允许只有一个槽位。
    old = dict(_old or {})
    if old and int(old.get("run") or 0) != int(rec.get("run") or 0):
        hist = data.get("phase2_history")
        if not isinstance(hist, list):
            hist = []
        hist.append(old)
        data["phase2_history"] = hist[-20:]
    data["phase2"] = rec
    try:
        _atomic_write(path, data)
    except Exception:
        pass


def load_case(scene_id: str, question: str) -> dict[str, Any]:
    """**只读**读一整道题的账（教师监控位 / 面板用，2026-11-18）。

    为什么要这个入口：`progress_chat`（人的进度问答）要回答「学生交了什么、
    critic 怎么评的、第几遍、机检过没过」，而现有公开接口只有 `load_phase2`
    和 `load_phase3`——phase1 的冻结 spec / 追问轨迹、phase2_history 的历史
    提交、骨架维度都拿不到，于是模型只能说「看不到提交内容」（实测截图）。

    ⚠️ 只读：不写账、不改键、不迁移。取不到就返回空 dict，调用方自行降级。
    ⚠️ 这是**教师位**的口径：原版 maieutic 教师看学生的 attempt 历史 + 交付物
       本来就是全可见的；不可见的是「判定权」（那仍在 critic / 消费侧）。
    """
    try:
        path = _cache_path(str(scene_id or "A"), str(question or ""))
        data, _st = _read_cache(path)
        if not isinstance(data, dict):
            return {}
        return {
            "question": str(data.get("question") or question or ""),
            "dimensions": data.get("dimensions") or [],
            "expected_drifts": data.get("expected_drifts") or [],
            "milestone": data.get("milestone") or {},
            "phase1": data.get("phase1") or {},
            "phase2": data.get("phase2") or {},
            "phase2_history": data.get("phase2_history") or [],
            "phase3": data.get("phase3") or {},
            "progress": data.get("progress") or {},
        }
    except Exception:
        return {}


# ─── Phase 3（intent-diff 复盘）─────────────────────────────────────────────
# 原版 `phase3Data`：divergences / startedAt / completedAt / revisionChoice /
# revisedCode / revisedAt（`opus/schemas.ts:214-226`）。只写 `phase3` 一个键
# —— 不碰 phase1（spec 全文与 iterations 是复盘的对账面，必须冻结）。


def _phase3_rec(scene_id: str, question: str) -> tuple[Path, dict, dict]:
    path = _cache_path(str(scene_id or "A"), str(question or ""))
    data, _st = _read_cache(path)
    if not isinstance(data, dict):
        data = {}
    rec = data.get("phase3")
    return path, data, (dict(rec) if isinstance(rec, dict) else {})


def load_phase3(scene_id: str, question: str) -> dict[str, Any]:
    """读上一轮 Phase3 的复盘账（分歧 + 回答 + alignment + 修订选择）。"""
    try:
        _p, _d, rec = _phase3_rec(scene_id, str(question or ""))
        return rec
    except Exception:
        return {}


def save_phase3(scene_id: str, question: str, rec_in: dict[str, Any]) -> None:
    """写 Phase3 的复盘账（字段对齐原版 `Phase3Data`，只写这一个键）。"""
    q = str(question or "").strip()
    if not q or not isinstance(rec_in, dict):
        return
    path, data, _old = _phase3_rec(scene_id, q)
    divs: list[dict[str, Any]] = []
    for d in (rec_in.get("divergences") or []):
        if not isinstance(d, dict):
            continue
        divs.append({
            "divergence_id": str(d.get("divergence_id") or "")[:60],
            "initial_classification": str(
                d.get("initial_classification") or ""),
            "initial_confidence": str(d.get("initial_confidence") or ""),
            "predicted_justification": str(
                d.get("predicted_justification") or "")[:600],
            "student_facing_question": str(
                d.get("student_facing_question") or "")[:600],
            "evidence_from_spec": str(d.get("evidence_from_spec") or "")[:600],
            "evidence_from_code": str(d.get("evidence_from_code") or "")[:600],
            "student_response": (str(d.get("student_response"))[:1500]
                                 if d.get("student_response") else None),
            "alignment": (str(d.get("alignment"))
                          if d.get("alignment") in
                          ("aligned", "partial", "diverged") else None),
            "final_classification": (str(d.get("final_classification"))
                                     if d.get("final_classification") in
                                     ("drift", "revision", "bug") else None),
            "final_classification_reason": (
                str(d.get("final_classification_reason") or "")[:400] or None),
            "responded_at": str(d.get("responded_at") or "") or None,
        })
    rec = {
        "divergences": divs[:10],
        "started_at": str(rec_in.get("started_at") or ""),
        "completed_at": str(rec_in.get("completed_at") or ""),
        "revision_choice": (rec_in.get("revision_choice")
                            if rec_in.get("revision_choice") in
                            ("skipped", "revised", "revise_failed") else None),
        "revision_note": str(rec_in.get("revision_note") or "")[:400] or None,
        "revised_code": str(rec_in.get("revised_code") or "")[:20000] or None,
        "revised_at": str(rec_in.get("revised_at") or "") or None,
        "counts": dict(rec_in.get("counts") or {}),
        "skipped": [str(x)[:300] for x in (rec_in.get("skipped") or [])][:6],
    }
    data["phase3"] = rec
    try:
        _atomic_write(path, data)
    except Exception:
        pass


# ─── Exercise 元信息（同构原版 `ExerciseRecord`）────────────────────────────
# `src_lib_opus_schemas.ts`（ExerciseRecord）：
#   id / title / instructorPromptText / authoredAt / publishedAt /
#   targetLanguage / specGateDimensions / expectedDivergences / studentLevel / unit
# MVE 的对应物：req id = id，question = instructorPromptText，
# dimensions = specGateDimensions，expected_drifts = expectedDivergences。
# 少的正是**教师侧**那几项：标题、单元、编辑器类型、发布时刻 ——
# 面板上「每张卡一道题」要有个名字，不能永远显示题面头 40 个字；
# 「按单元分组」也需要 unit。这里只补元信息，不碰判定。


def load_exercise_meta(scene_id: str, question: str) -> dict[str, Any]:
    try:
        _p, data, _r = _phase1_rec(scene_id, str(question or ""))
        meta = data.get("exercise")
        return dict(meta) if isinstance(meta, dict) else {}
    except Exception:
        return {}


def save_exercise_meta(scene_id: str, question: str, meta: dict[str, Any]) -> None:
    """写练习元信息（标题 / 单元 / 编辑器类型 / 发布时刻）。

    只补缺、不覆盖已有标题：同一题重复发布时，教师第一次起的名字保留
    （原版 Exercise 发布后 title 不随会话变化）。
    """
    q = str(question or "").strip()
    if not q or not isinstance(meta, dict):
        return
    path, data, p1 = _phase1_rec(scene_id, q)
    old = data.get("exercise") if isinstance(data.get("exercise"), dict) else {}
    rec = dict(old)
    for k in ("title", "unit", "target_language", "student_level"):
        v = str(meta.get(k) or "").strip()
        if v and not str(rec.get(k) or "").strip():
            rec[k] = v[:120]
    if meta.get("authored_at") and not rec.get("authored_at"):
        rec["authored_at"] = str(meta["authored_at"])
    if meta.get("published_at"):
        rec["published_at"] = str(meta["published_at"])
    if not str(rec.get("title") or "").strip():
        rec["title"] = q[:60]
    data["exercise"] = rec
    data["phase1"] = p1
    try:
        _atomic_write(path, data)
    except Exception:
        pass


def milestone_rounds(scene_id: str, question: str) -> dict[str, Any]:
    """只读：整题层的记账（面板/日志用）。"""
    try:
        _p, _d, rec = _rounds_rec(scene_id, str(question or ""))
        return rec
    except Exception:
        return {}
