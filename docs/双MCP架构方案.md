# 双 MCP 架构方案：把「学习」和「消费」的能力面物理切开

> 2026-10-23 · 依据用户判断「MVE 只需要 maieutic MCP + VLML MCP；学习期用前者调研闭合，
> 消费期只开后者用入库的知识和技能作答」整理，全部结论有 file:line 或实测支撑。

## 结论

判断成立，而且比"多接几个 MCP"更彻底：**MVE 真正需要的不是 N 个工具，而是两个能力面**——
「联网调研」和「数据访问」。前者收进 `maieutic` MCP，后者已经是 `VLMLMCPEnv` / `SceneDBEnv`。
剩余 4 个 server（fetch / ddg / neko / time / context7 / sequential-thinking）**没有任何业务在用**，
应当退役。

但只改 `mcp_servers.json` 是不够的——现有代码有**三处会让"关掉的能力"自己回来**，必须一起改，
否则"消费期只开 VLML"只是配置文件上的假象（见第四节）。

## 一、现状取证

### 1.1 六个 server 的实际使用情况

| server | 提供的工具 | 谁在用 | 结论 |
|---|---|---|---|
| `neko` | `web_search` / `web_fetch` | 学习期执行层（实测 21:05 日志） | 能力保留，**形态并入 maieutic** |
| `ddg` | `search` / `fetch_content` | 同上；技能库里 6 处引用 | 同上 + **structuredContent 不合规会崩** |
| `fetch` | `fetch` | 未见业务调用 | 退役 |
| `time` | `get_current_time` / `convert_time` | 无人调用（只在 `exposed` 名单里挂过） | 退役 |
| `context7` | 库文档查询 | 无人调用 | 退役 |
| `sequential-thinking` | 思维链 | 无人调用 | 退役 |

实测（服务器 `/root/mve/mve/skill_store.json`）：

```
技能总数: 5        场景分布: {'B': 5}        来源分布: {'learn': 5}
引用远端工具的: {web_search: 4, mcp_ddg_fetch_content: 6, mcp_neko_web_search: 1, mcp_neko_web_fetch: 1}
```

**这 5 条全是"学后端名"的配方**，其中 4 条的答案是 403/521/DNS 错误页
（`mve/audit_skills.py` 体检结果）。这暴露了一个比错误更根本的问题：**技能学的是搜索后端的名字，
后端一换技能就废**。所以本次改动必须顺带把技能依赖的接口收窄成管道名。

### 1.2 「VLML MCP」在仓库里的真实形态

不是 MCP 协议服务，而是**进程内直调的数据面**：

| 文件 | 是什么 | 使用方 |
|---|---|---|
| `voyager_vlml/env/vlml_mcp.py:36` `VLMLMCPEnv` | 装载 VLML 的 10 个工具（`query_sql` / `get_database_info` / 8 个 report），返回 `{ok,tool,rows,error,data}` | `voyager_vlml/consume.py:973`、`loop.py:380`、`agents/action.py:65` |
| `mve/vlml_env.py:14-31` | 引导 VLML 环境：`os.chdir(vlml)` + `Path.mkdir` 补丁 + 可切换 db | 被 `VLMLMCPEnv.__init__` 调用 |
| `mve/scene_db.py` `SceneDBEnv` | **同构替身**：接口与 `VLMLMCPEnv` 对齐，读场景自己的 duckdb | `voyager_vlml/consume.py:672-674` |
| `vlml/src/vlml/server.py`（服务器） | `FastMCP("vlml-valorant-analytics")`，10 个 `@mcp.tool` | 目前**没有** MCP 客户端用它 |

也就是说：**A 场景的数据面已经存在且在用，B 场景有同构替身**。用户的"VLML MCP"= 这套数据面。

### 1.3 消费期现在的真实能力面（漏点所在）

| 场景形态 | 消费期实际连了什么 | 证据 |
|---|---|---|
| A（VLML 库） | 只构造 `VLMLMCPEnv`，**不连任何 MCP** ✅ | `consume.py:973`，`ensure_mcp_tools` 只在非 VLML 分支 |
| B（场景库 / 本机） | `register_net` + `ensure_mcp_tools` → **全套 MCP + 联网原语都在** ❌ | `consume.py:679-691` |

所以用户要的"消费期只开 VLML"在 A 场景已经成立，**漏点只在 B 场景这条泛化路径上**。

## 二、目标架构

```
                       ┌────────────────────────────────────┐
  学习期（联想学习）    │  Voyager 执行层（课程规划在这里）    │
  learn_orchestrate ──▶│  · 出维度 / 出搜索词 / 决定下一步    │
                       │  · 唯一取数入口：maieutic_research   │
                       └───────────────┬────────────────────┘
                                       │ MCP (streamable-http :8797)
                       ┌───────────────▼────────────────────┐
                       │  maieutic MCP（新）                  │
                       │  gather → 材料账 → synthesize → gate │
                       │  正文不出这个进程，只回摘要 + gaps    │
                       │  内部用 net_tools（智谱搜索/抓正文）  │
                       └────────────────────────────────────┘
                                       ┊ 学习成果：技能库 + 图谱节点
                       ┌───────────────▼────────────────────┐
  消费期（作答）        │  Voyager 执行层                      │
  consume ────────────▶│  · 检索技能 → 重放 / 现写            │
                       │  · 唯一取数入口：数据面（按场景路由） │
                       └───────────────┬────────────────────┘
                                       │ 进程内直调（A/B 同构）
                       ┌───────────────▼────────────────────┐
                       │  数据面（已有）                      │
                       │  A: VLMLMCPEnv（VLML 库，10 工具）   │
                       │  B: SceneDBEnv（场景 duckdb + 文件） │
                       └────────────────────────────────────┘
                       ✗ 消费期拿不到 maieutic，也拿不到 web_search
```

用户原话里的"VLML mcp"在这个图里是下半部分的**数据面**。它**不必**做成常驻 MCP 服务
（理由见第六节第 3 条），但把它常驻化有一个真实收益：`vlml_env` 的 `os.chdir` + `Path.mkdir`
全局补丁目前会污染调用进程（面板进程一旦构造过 `VLMLMCPEnv`，cwd 就变了）。
这件事需要单独拍板，见第九节。

## 三、maieutic MCP 的接口

只暴露两个工具，**不暴露任何搜索后端**——这是关键，否则技能又会学回 `mcp_ddg_fetch_content`。

### `maieutic_research`

```jsonc
{
  "name": "maieutic_research",
  "arguments": {
    "question":  "这道题本身问什么（用于理解上下文）",
    "dimension": "这一轮要闭合的那一个维度的 question（一次只闭合一个）",
    "query":     "本轮搜索词；留空则内部按 dimension 生成",
    "urls":      ["可选：Voyager 已经挑好的 URL，直接读，不再搜索"],
    "max_sources": 3
  }
}
```

返回（**正文不出现**，只有摘要 + 材料账 + 缺口）：

```jsonc
{
  "answer":   "只用材料里能印证的话写成的要点（抽取式，不是整页正文）",
  "closed":   false,                    // 该维度是否答到
  "gaps":     ["还差什么"],              // 最多 3 条，给 Voyager 决定下一步
  "evidence": [{"source": "https://...", "chars": 1840}]   // 只有成功项
}
```

语义与助产士 `src/lib/research/agent.ts` 一致：`gathered` / `report` 两本账，
失败项结构上进不了材料（`if (result.ok && result.text)`），所以不需要任何 403 特判。

### `maieutic_gate`

```jsonc
{ "question": "...", "answer": "...", "dimensions": [{"id":"d1","question":"..."}] }
```

直接复用 `mve/maieutic_gate.py:126` `check()`，返回 `{passed, open, questions}`。
给 Voyager 一个"我自己先核一遍"的入口，避免每轮都等 MCP 内部再判一次。

### 内部实现（`mve/research_agent.py`，新文件）

| 段 | 做什么 | 复用 |
|---|---|---|
| plan | query 为空时由 LLM 按 dimension 出 2~3 个搜索词 | `voyager_vlml.llm.chat` |
| gather | 逐源 fetch，`_evidence_item` 判 ok，失败**不入材料**；单源异常只跳过该源 | `mve/net_tools.py:web_search/web_fetch` |
| synthesize | "只写材料里有证据的内容；证据不足就明说" | LLM |
| gate | 维度闭合判定 | `mve/maieutic_gate.py` |

gather 内部逐源 try/except 顺带解决 `ddg` 那个 `structuredContent=list` 让 SDK 校验崩溃的问题
（实测 `convergence_test` 就是这样挂掉的）。

## 四、只改配置不够：三处会让关掉的能力自己回来

| # | 位置 | 现状 | 后果 | 改法 |
|---|---|---|---|---|
| 1 | `voyager_vlml/consume.py:679-691` | 非 VLML 场景消费期调 `register_net` + `ensure_mcp_tools` | 消费期联网能力全在 | 只 `register_local`；`ensure_mcp_tools` 改成只挂数据面 |
| 2 | `mve/local_agent.py:986-989` | `full["web_search"] = net_tools.web_search` **无条件**进命名空间 | 靠注册表过滤才不注入，一处漏就露 | 保留过滤，但补一条断言：消费期 `names` 里出现 `web_*` 就报错 |
| 3 | `mve/local_agent.py:1155-1157` | `run_saved` 自愈里重跑 `register_net` | **技能重放缺名字 → 自愈把联网又打开**（最隐蔽的一处） | 自愈只 `register_local`；给 `run_saved(..., allow_net=False)`，缺的只有联网名字就如实报"消费期不提供联网" |

第 3 条是这次方案里最值得改的一行：它意味着"关掉联网"在重放路径上会被静默撤销，
如果不改，用户"关远端还能答"的验收永远测不出真相。

## 五、退役清单

| 动作 | 对象 | 说明 |
|---|---|---|
| 删 | `fetch` / `time` / `context7` / `sequential-thinking` | 无业务调用 |
| 并入 | `neko` / `ddg` | 联网能力整个收进 maieutic 内部，MVE 侧不再有 `web_search` 这个名字 |
| 新增 | `maieutic`（:8797） | 见第三节 |
| 不动 | 数据面（`VLMLMCPEnv` / `SceneDBEnv`） | 消费期唯一取数入口 |

`mcp_services.sh` 的 `SERVICES` 从 6 项缩到 1 项（maieutic）；旧 6 个 unit 用
`bash mve/mcp_services.sh uninstall` 清掉。

## 六、改动清单（按依赖顺序）

| # | 文件 | 改动 | 规模 |
|---|---|---|---|
| 1 | `mve/research_agent.py` | **新**：plan/gather/synthesize/gate 管道 | ~220 行 |
| 2 | `mve/mcp_maieutic_server.py` | **新**：手搓 stdio server，照 `mcp_neko_server.py` 骨架，2 个工具 | ~150 行 |
| 3 | `mve/mcp_services.sh` | `SERVICES` 换成 maieutic（`/root/mve/.venv` 跑，需 `voyager_vlml` + `duckdb`） | 改 6 行 |
| 4 | `mve/mcp_servers.json` | 只留 maieutic + 数据面占位 | 重写 |
| 5 | `voyager_vlml/consume.py:679-691` | 停掉 `register_net` / `ensure_mcp_tools`；只留本机原语 | 改 8 行 |
| 6 | `mve/local_agent.py:1155` | 自愈不再 `register_net`；新增 `allow_net` 参数 | 改 10 行 |
| 7 | `mve/local_agent.py` `PROMPT_SYSTEM` | 学习期示例从"for 循环抓正文"改成"调 `maieutic_research`，按 `gaps` 决定下一步" | 改 15 行 |
| 8 | `mve/actions.py:457` `_ORIGIN_GROUP` | 组按能力面分：`MCP·maieutic` / `数据面`，面板一眼看出该关哪个 | 改 4 行 |
| 9 | `mve/audit_skills.py` | 改名 `audit_skill_replay.py`（根目录已有同名 SQL 审计脚本，避免混淆） | 重命名 |

技能库迁移（必须）：现有 5 条引用 `web_search` / `mcp_ddg_fetch_content` 的配方在新能力面下全部失效，
且本来 4/5 是错误页。做法：`python mve/audit_skill_replay.py --scene B --fix` 清掉，重学。
长期约束：**技能代码里只许出现管道名**（`maieutic_research` / `query_sql`），不许出现搜索后端名。

## 七、执行顺序与回滚

1. 先在本地实现 1、2，单测跑通（不碰服务器，不影响现有学习）
2. 部署 maieutic 服务 → 服务器 `connect_all` 只看到 maieutic，工具 2 个
3. 改 5、6（关掉消费期联网），跑 `test_scene_backend` + `test_mcp_callable`
4. 清 5 条坏技能 → 用场景 B 一个题**重新学一遍**（验证学习期只靠 maieutic 也能闭合）
5. 消费同一题 → 验收

回滚：`mcp_services.sh install` 装回原 6 个 / `git revert`；`mcp_servers.json` 由 git 管，
`skill_store.json` 清理前自动备份。

## 八、验收线（要硬证据，不要"应该可以"）

| # | 验收项 | 判据（可核） |
|---|---|---|
| 1 | 学习期能闭合 | `ckpt_maieutic/<scene>/*.json` 里维度 `achieved=true`，且 `evidence` 里有 ≥1 条 `ok=true` |
| 2 | 技能只依赖管道 | `skill_store.json` 里新技能的 code 中出现 `maieutic_research`，**不出现** `web_search`/`mcp_ddg_*` |
| 3 | **消费期物理无联网** | 把 maieutic 服务 `systemctl stop` 后，同一问题仍答得出；`_tools_in_code` 里 `web_*` / `maieutic_*` 计数为 0 |
| 4 | 图谱生效 | 消费期 `graph` 命中 topic（`root_scene == B`），不是靠词面 |
| 5 | 不再有错误页答案 | `audit_skill_replay.py --scene B` 输出 `OK 5 / 坏 0` |

第 3 条是关键：**停服务**比开关更硬——它排除"开关是假的、代码里还留着后门"这种可能。

## 九、需要拍板的两件事

1. **数据面要不要也常驻成 MCP？**
   - 不做（推荐）：保持进程内直调，少一跳网络、少一个 SQL 注入面；A/B 已经同构。
   - 做：唯一收益是把 `vlml_env` 的 `os.chdir` + `Path.mkdir` 全局补丁挪出主进程。
     如果面板里出现过"相对路径读错文件"，就该做。
2. **maieutic 内部要不要读知识图谱**（跳过本场景已收录的 topic，省一次 LLM）？
   - 推荐**不读**：图谱归 Voyager 的规划环节，maieutic 保持无状态纯管道，才能跨场景复用。

## 附：与助产士的对应关系（同构对照）

| 助产士（maieutic-1） | 本方案 |
|---|---|
| `plan` 出 searchQueries / urls | `research_agent.plan`（query 为空时） |
| `fetchUrlText` 每源截 4000 字 | `gather` → `net_tools.web_fetch` + `_evidence_item` |
| `gathered` / `report` 两本账 | 材料账（失败项结构上不进材料） |
| `SYNTHESIZE`「证据不足就明说」 | `synthesize` |
| `spec-examiner` 的 `gaps_still_open` | `maieutic_gate.check` |
| 人拿追问去改 | Voyager 拿 `gaps` 决定下一个课程规划 |
