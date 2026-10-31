# HANDOFF — MVE 项目交接（给下一个 agent）

> 作者：上一个 agent（全程参与开发到 2026-10-31）
> 面向：接手这个仓库的**下一个 agent**。读完这份文档 + `README.md` + `docs/`，你应当能独立改代码、跑实验、判对错，不重复踩已经踩过的坑。
> 本文件是**活的**：你踩到新坑、改完新东西，请直接往里加，并在文末「变更记录」留一行。

---

## 0. 三十秒上手

```bash
# 本地开发（代码在 ~/mve-repo，/tmp 里那些是旧快照，别用）
cd ~/mve-repo && ls mve/ voyager_vlml/ docs/

# 单测（本地能跑大部分；数据类要服务器）
python mve/test_reason_pass.py        # 45 项
python mve/test_evidence_material.py  # 34 项

# 服务器（43.161.203.50，ssh 私钥 ~/WorkBuddy/2026-09-24-23-06-19/outputs/neko_cloud/neko_deploy_key）
ssh -i <key> root@43.161.203.50
systemctl restart mve-panel           # 改完代码必须重启，否则跑的是旧进程
curl http://172.18.0.1:8777/mve/api/overview   # 面板 API（127.0.0.1 不通，必须走 docker 网桥 IP）
```

面板入口：`http://43.161.203.50:8443/mve/`（无鉴权，公网直开）
白皮书（中文，点开即看）：https://zxy876.github.io/voyager-vlml-mve/

---

## 1. 这个项目到底是什么

**一句话**：Voyager 是「Minecraft agent 学技能」，MVE 是把「学习者」换成**人**（心悦）之后的最小可执行原型——用同一套「拆维度 → 取证 → 判官把关 → 技能入库 → 复用」闭环，验证**一个外接 LLM 在这个闭环里的「会了」是真技能还是幻觉**。

**三个不能混淆的定位**：
1. **不训练模型**（2026-10-29 会议拍板，不做 RL/微调）。验证对象是**架构**，不是模型权重。换掉外接 LLM（flash ↔ glm-4.6）闭环照跑 → 它因此能当「任意小模型的学习能力测试台」。
2. **判官是另一个 LLM + 确定性闸门**，不是神经 reward model（DeepSeek R1 §2.2 同结论：推理任务只用规则可验信号）。
3. **同构不只是比喻**：episode=rollout / horizon=4 步 / policy=ActionAgent / reward model=critic / 经验复用=技能库 —— 这是 Tianshou 的 MDP 五元组落到产品上的形态。`reward` 恒 0 与原版 Voyager 一致，真信号在 `info["success"]` + 判词。

**核心闭环（从一次提问到技能入库）**：
```
场景(scene) → 里程碑拆维度 → 逐维度出题 → 执行(工具/MCP) → 判据+判官 → 闭合计数
   ↑                                                              ↓
   └── 技能库(配方/程序) ← generalize 去数值 ← 验对轨迹入库 ← 全闭合/部分闭合
```

---

## 2. 代码地图（先记这张表，别乱翻）

| 文件 | 行数 | 职责 | 改它之前必读 |
|---|---|---|---|
| `mve/voyager.py` | 2582 | 主循环（对应原版 `voyager.py` 的 learn/inference 两层） | §4 两层调度 |
| `mve/learn_orchestrate.py` | 2304 | 维度循环、执行账、语义收口、判据汇总 | §4、§5 |
| `mve/local_agent.py` | 1707 | 本机执行域（`run_saved_async` 重放 Python 程序、证据渲染） | §5「材料截断」 |
| `mve/reason_pass.py` | ~400 | 语义层：只依据材料出 `{verdict,quotes,answer,gap}`，引文逐字核验 | §5「引文核验」 |
| `mve/curriculum_auto.py` | — | 骨架拆维度（里程碑） | §4「骨架固定」 |
| `mve/scene_learn.py` | — | 驾驶舱调度：里程碑点亮、冷却换题、自由出题降级 | §4 |
| `mve/dashboard.py` | 4196 | 面板前端（`/mve/`，唯一入口，不建第二面板） | — |
| `mve/panel_data.py` | 1169 | 面板数据聚合（`build_state` 按 `scene.id` 过滤） | — |
| `mve/question_gen.py` | 2115 | 出题器（含 QA 攻略层、`reason_demos` 示范库） | §5 |
| `mve/coach.py` | — | 消费侧讲解（**注意：与学习侧是两把尺子，见 §6**） | — |
| `voyager_vlml/consume.py` | 4317 | 消费链路（VLML 接驳、SQL 回填、配方判定） | §5「配方白名单」 |
| `mve/failures.py` | — | 失败账（已归档判词） | — |
| `voyager_original/` | — | **原版 Voyager fork，只读参照物**，任何判断先来这里逐行核对 | 全文 |

**测试文件**（都是 `python mve/test_xxx.py` 直跑，没有 pytest 的地方就别装）：
`test_reason_pass.py`（45）、`test_evidence_material.py`（34）、`test_maieutic_scaffold.py`、`test_judge_entry_parity.py`、`test_skill_composition.py` 等。

---

## 3. 运行 / 部署 / 环境

### 3.1 部署三步（每次改完代码）
```bash
# 1. 本地提交（main 与 feature 两个分支都要推，服务器跟的是 feature）
git push origin main:main main:feature/1-to-n-pluggable-scenes
git ls-remote origin main feature/1-to-n-pluggable-scenes   # 必须核实 sha，别信 push 输出

# 2. 服务器同步
ssh ... root@43.161.203.50
cd /root/mve && git fetch && git reset --hard origin/feature/1-to-n-pluggable-scenes

# 3. 重启（必做）
systemctl restart mve-panel
```

**重启前必查**：`pgrep -af 'scene_learn|pilot'`。有活循环先 `curl -X POST http://172.18.0.1:8777/mve/api/pilot/stop`。
重启会杀子进程，但 `pilot_state.json` 仍写着 running —— 这是**陈旧状态**，不是 bug，别去修那个文件。

### 3.2 模型配置
- 主模型 = 服务器 `/root/mve/mve/.env` 的 `ZHIPU_MODEL`。**2026-10-31 起全局 `glm-4.6`**（原 flash），`llm_client.py:34` 用 `setdefault` 读，改 .env 即生效。
- 判官模型另配：systemd `Environment=MVE_CRITIC_MODEL=glm-4.6`。
- **为什么换 4.6**：flash 拿着「出错行号 + 返回形状」仍然 4 步重犯 `KeyError: 0`、里程碑点不亮；换 4.6 后 KeyError 0 次、里程碑**首次点亮**、`status=learned` 11 次。这是「模型能力差异被框架如实反映」的直接证据，别轻易退回 flash。

### 3.3 环境坑（都真踩过）
- **服务器没有 pytest**：只能 `python mve/test_xxx.py`。
- **本地没有 duckdb**：数据类测试必须服务器跑。
- **worktree 陷阱**：`git worktree add` 出来的目录**没有 `mve/.env`** → `chat_json` 全失败 → `generalize` 静默原样返回 → 测试「碰巧过」。用 worktree 对账前先 `cp mve/.env`。
- **探测地址**：容器内服务只认 `http://172.18.0.1:8777`（docker 网桥），`127.0.0.1` 不通。
- **commit message 用文件**：中文多行 `git commit -F 文件`，别用 `-m` 引号转义。

---

## 4. 两层调度：这是最容易抄错的地方（逐行核对过原版）

| 层 | 原版语义 | MVE 落地 |
|---|---|---|
| 整题层 | `learn()`：任务失败进 `failed_tasks`，**换题** | 里程碑：失败 → `note_milestone_round(new_closed=0)` 挂冷却 → 降级自由出题 |
| 子目标层 | `inference()`：`while sub_goals`，题内不跳过 | 维度：逐维度重试 |

**三个铁律**：

1. **原版没有「整题总验收」层**。`inference()` 的完成判据只是「最后一条子目标成功」（`voyager_original/voyager.py:417`，`progress = len(completed_tasks)`；`curriculum.py:133-135`）。别自己发明一个总验收。
2. **原版对做不出的任务从不原地无限重试**：单任务封顶 `action_agent_task_max_retries=4`（`voyager.py:30,277-280`）→ 记 failed + `"Skipping to next task."`（`curriculum.py:340-344`）→ 下一轮换题。把 `inference()` 的「失败不跳过」抄进训练循环 = 死循环（实测 26 轮同一个题）。MVE 已封顶 4 次 + 冷却（`MAX_RETRY_STREAK = 4`）。
3. **骨架拆一次就固定，永不因失败重拆**。曾按失败指纹重拆 → 维度 id 每轮变 → 闭合账全对不上。

其他常数：内层 `INNER_MAX_ITERS=4`；外层 `--max-retries` 默认 1；外层墙钟 90s > MCP 60s（别把 MCP 超时调到 >90s）。

**进度账**：`learned / codes / iterations` 三本账同住一个字段；`save_progress` **只改 learned 且并集累加**（不然会丢）。`completed/failed` 两本账**按题隔离**，做成场景级会串台。

---

## 5. 判定铁律 + 已修 bug 的根因（省你三天）

### 5.1 防幻觉三闸（架构原则，改判据前先理解）
```
真实观察面  →  LLM 判官（看材料账，不看自述）  →  确定性守卫
```
- 环境观察三方共用（出题器 / QA 层 / critic），必须是**同一份**。
- 判官的 `quotes` 逐字核对；**「无法判断 + gap」不算答到**。
- 判词要**可执行**：语义层的 `gap` 必须拼进 `crit1`（缺哪份材料 + 怎么取），否则下一轮拿着「没有指认任何引文」原地重撞。

### 5.2 四个「把对的判成错的」bug（2026-10-31 结案，85fa516）
| # | 症状 | 根因 | 修法 |
|---|---|---|---|
| ① | `license: ""`、`A.md, B.md` 这种**抄对了**的引文被判「编的」 | `_norm` 没抹平 JSON 结构标点 | 引号/括号/逗号/冒号当**分隔符**抹平（仍要求字面连续命中）；`_probe_hit` 支持 `A…B` 多段按段核验；`MIN_QUOTE=4` |
| ② | 语义层总判「与材料账不符」 | `_EVIDENCE_CHARS=4000` 静默砍掉 README 一半 | 上限 12000 + `_clip_ev` 截断标注「共 N 字只给前 M 字」+ `_render_lines`（dict→`key: value` 行）+ SYSTEM 明说「截断时只判已给部分，不许按常识补」 |
| ③ | 真取到正文仍判「真空成功」 | 旧的「执行账对齐」只改 `r1`，闸门看的是从 observations 重算的 `out` | `_material_real()`：正文类工具（`gh_*/web_*/read_file`）取回非空正文 ≥20 字 = 真取到；**SQL 工具不走这条**（空表/乘爆仍由 `is_vacuous` 判）。执行账对齐必须**同时改 `out`** |
| ④ | H 场景学一整轮技能库 0 涨 | `_RECIPE_CALL` 白名单漏 `gh_*` | 补 `gh_(?:repo_info|tree|read|api|search)`。**接新原语族时，白名单必须同步**（2026-10-22 纯计算 SELECT 漏判同型） |

修完实测：同场景 0/9 → 6/9 闭合、技能入库、示范库 5 条。

### 5.3 其他高频坑
- **登记表/缓存/骨架一律原子写**（`tmp + os.replace`），读失败要区分 `missing / ok / broken`。
- **判词等新字段必须进 `coach._log_import` 与 `save_progress_codes` 白名单**（已踩 6 次，加了字段没加白名单 = 字段蒸发）。
- **真空成功**：取到数必须含真实值——空表聚合出来的 `(NULL, 0)` 会骗过判据；`bool` 是 `int` 的子类，别用 `isinstance(x, int)` 判数值。
- **`needs_data(q, has_env=False)` 必须传第二参**，单参恒 False = 真空防线直接失效。
- **来源绑定按「最具体目标」取一类**：带扩展名文件名 > URL > owner/repo > 本库表名；判据看**调用 args 是否点到它**，不看返回文本。
- **局部 import 同名模块 = `UnboundLocalError`**（`consume.py:902` 踩过）。
- **注册 ≠ 注入**：执行域 `names = 启用条目 & set(full)`，接新原语族**两处都加**。
- **菜单要给说明书**：`query_sql → {columns, rows}`、`read_file → {text, path}`，各原语返回形状 + 能跑的示例。
- **仓库题证据链** = `gh_repo_info → gh_tree → gh_read → gh_api`；**不许用 web_search 当仓库证据**。
- **搜索链**：智谱 → Bing(rss) → DDG，必须过相关性闸（`_split_relevant` 零交集整组丢）。
- **安全**：可读根只剩场景目录（+`MVE_READ_ROOTS`）；`_is_sensitive` 挡 `.env`；DuckDB 禁外部访问。

### 5.4 技能入库
- **存算法不存数值**：入库前 `generalize` 成 `:slot`，消费侧 `_refill_sql` 回填。
- 三通道一个口径（`_absorb` / `learn_milestone_skills` / `learn_milestone_composite`）**都过 generalize**；`code` 存模板、`blueprint.rollout_sql` 存判对原文；`_absorb` 返回的是 `skill_key`，**别按 code 字符串认**。
- **当场结晶** `_crystallize_round_skills` 在 partial/failed 早退分支**也要调用**；⑥（整题入库）只在全闭合走。
- 检索键**必须传本题 `topic_id`**；命中强制 `solve(require_skills=True)`；重放前先 `ensure_mcp_tools`，网关绑死旧循环 → `stale_servers + disconnect_all` 重连。
- 技能形态双通道：VLML/数据 = SQL 配方；local/本机 = Python 程序（`run_saved_async` 重放）。

---

## 6. 当前状态（2026-10-31 收盘）

### 通了
- H（GitHub 仓库调研）场景：9 维度从 0 闭合 → 6 闭合、技能入库、示范库 5 条。
- 里程碑重试封顶 + 冷却换题（不再 26 轮死循环）。
- glm-4.6 下 KeyError 0 次、里程碑首次点亮。
- 中文白皮书 HTML 已上 GitHub Pages；deepwiki-open 已部署到 VPS（见 §8）。

### 没通 / 待办（按优先级）
1. **3 条维度闭不上**（dependencies_list / code_quality / community_support 类）：gap 已进判词，但没跑够轮次验证是否收敛 → 先跑 3~5 轮看闭合率是否上升，再决定是加工具还是改判据。
2. **「换值回测」佐证方案**（`docs/判官与技能真实性验证_会议纪要与论文对照.md` §4，已写待拍板）：技能入库后用 `:slot` 换真实实体值经 `_refill_sql` 重放 1~2 次 → `verified` / `unverified-variant`。对照 2010.02840 的 neighbor queries 思路，但**不做随机库 fuzzing**（我们没有 gold SQL）。
3. **消费侧与学习侧是两把尺子**：`coach.explain` 未接 `source_binding` / 搬运判据 / 示范召回。
4. **「组合多条配方」缺口**：学一堆原子技能答不出合成题（原版技能是可组合函数）。
5. **技能语义去重**：`verify_table_existence` 存了 10 份；检索 `topic` 恒空。
6. **EnvAdapter** 路线：天授 env 二分排除 → 修 → 判定作用域 → 回 SQL 验幻觉 → 消费同构（补联网工具线）。做完才能砍掉「知识型/数据型」分类，改用「环境指认」当判据。

### 运行中的环境（别重复搭）
- **deepwiki-open 中文白皮书**：`/root/deepwiki`，docker compose，Caddy 子域名 `wiki.zxthxinyue.xyz:8443`（自签证书），口令 `xinyue-wiki-2026`，生成走 glm-4.6 + 智谱 embedding-3。域名实名若未过审，解析会 `server hold`。
- Caddy 配置由 `/root/neko/加一层口令.sh` 生成 —— **改 Caddyfile 必须同步改脚本模板**，否则重跑脚本就覆盖。单文件 bind mount 必须 `cat` 回写（`sed -i` 换 inode、`docker cp` 报 device busy）。
- zread.ai 对本仓库索引失败（已弃用），GitHub Pages 是白皮书正路。

---

## 7. 场景速查

| 场景 | 是什么 | 关键真值 / 坑 |
|---|---|---|
| B | 合成 payroll | 技术部 9 月真值 **57120**（旧记 75120 是 JOIN 乘爆） |
| C | NYC Payroll FY2025 真库 | 数据量大，SQL 要防乘爆 |
| H | GitHub 仓库调研 | `gh_*` 四件套 + 本机 duckdb 记账；`web_facts.confirmed` 全 False ≠ 不属实（库本来就脏） |

- 入口只有 `/mve/`，**不建第二面板**；曲线只认消费侧；`build_state` 按 `scene.id` 过滤。
- 环境 ≠ duckdb：`scene_profile` 四种 kind `vlml / scene / local / missing`。
- 数据题 `_use_scaffold = is_local or (db_path and not use_vlml)`；scaffold + db_path 维度直接 reachable。
- 驾驶舱：有未完成里程碑时选「未闭合维度」，全闭合后才降级自由出题。**人在面板问一句就会 `set_current_target` 换锚**——这是设计，不是 bug。

---

## 8. 跟人（心悦）协作的方式

- **全中文**，结论前置，带证据（`file:line` 或实测输出）。他看重「你能不能自己验证」，不接受「应该可以」。
- **执行尽量外包给他之外的环节**：需要他点的步骤压到最简（一条命令 / 一次点击），并说明为什么这步不能代做（比如 DNS 控制台要账号登录）。
- 他会挑战结论（「我记得 zread 会读代码啊」「这个同构原版怎么修」）——**先回去读真实代码/原版，再回答**，别用常识补。
- 他在意「百分百复现」：删任何仓库文件前想清楚，删了要有恢复办法（2026-10-31 恢复过 18 个 vectordb 文件）。
- 提交信息写清楚「为什么」，他要的是可追溯的证据链。

---

## 9. 文档索引（按需深挖）

| 想了解 | 看 |
|---|---|
| 整体机制 | `README.md`、`docs/README-学习机制详解-*.md` |
| 判官 / 防幻觉 / 论文对照 | `docs/判官与技能真实性验证_会议纪要与论文对照.md` |
| 主循环与原版对照 | `docs/主循环对照-自修正与重试.md`、`docs/取证通路-原版Voyager对照.md` |
| 场景与部署 | `部署.md`、`环境体检.md` |
| 已知限制 | 白皮书「已知限制与诚实标注」章（`docs/index.html`） |

---

## 10. 变更记录（向下追加）

- 2026-10-31 上一个 agent 建此文件：整理代码地图、两层调度、判定铁律、四个判据 bug、当前状态、协作方式。
