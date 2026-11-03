# 方案 (a) 调研：学习侧补上传文件通路——必要性与设计一致性

> 2026-11-03。背景：场景 J（心悦文集）学习对文集内容结构性失明，只能结晶
> web 缓存技能（见预期差台账 2026-11-03 行 + 开发项 #9-#11）。
> 方案 (a) 原始表述：「学习侧补上下文通路，把文集作为 learning material 喂给
> maieutic，同构消费侧」。本篇回答：有没有必要做、怎么做才与既有设计一致。

## 一、必要性：三条独立证据，缺一都成立

**1. 学习环境对场景型场景只绑了半个环境。**
- `learn_orchestrate.py:695`：`local_dir = prof["scene_dir"] if kind==LOCAL else ""`
  —— kind=scene 的场景 local_dir 恒为空串。
- `learn_orchestrate.py:780/:833`：`available_grabs(scene, "")` 里
  `if scene_dir:` 为假 → **register_local 根本不调用**。
- 场景 J 的学习抓手清单实际只有：query_sql（绑 db_path）+ net + gh。
- 服务器实测：`scene_profile.profile('J')` 的 `scene_dir` 字段为 **None**
  （scene_dir 只在 kind=local 的 profile 里带；scene-kind 靠
  `scene_profile.scene_dir(sid)` 函数推导，但学习/消费调用点都没用这个兜底）。

**2. read_file 原语本身完备，却够不着文集。**
- `local_agent.py` 已有 `read_file(scene_dir, path)`：带 `_resolve_under`
  路径守卫（只能读场景目录内 + 拒密钥文件）、`list_files` 可列 uploads/。
- 文集 sidecar 就在 `scene_data/J/uploads/20261103-140226_心悦文集.txt`
  ——**路径守卫本可覆盖，只差把原语注册进 kind=scene 的环境**。
- 消费侧同样：`consume.py:1390` kind=scene 走 `SceneDBEnv`（纯 SQL），
  else 分支才 register_local，且 `_scene_dir` 同样取到 None/""。

**3. 不做的代价已被场景 J 实证。**
- 学习器失明 → 把 web_docs/web_facts（联网抓的网页缓存）当唯一"可验证
  材料"结晶技能 → 错实体内容（腾讯心悦会员/小说角色/高校新闻）入库 →
  消费侧 reused=True 回访全部复用污染技能。
- 任何文档型场景（1→n 可插拔的主张范围）都会精确复现这条污染链。

## 二、设计一致性：逐条对既有拍板

| 既有设计 | 方案 (a) 推荐形态：**注册 read_file/list_files 进 kind=scene 环境** | 一致？ |
|---|---|---|
| 2026-10-28 拍板「上传文件当上下文，DeepSeek 同构，**不落表**」 | read_file 按需读 sidecar 原文件，不 CREATE TABLE、不进 schema——不会重演「up_工资明细 带歪 SQL」（10-28 实测）和指纹去重事故（10-29 实测） | ✅ 且比"塞 40k 上下文进学习 prompt"更一致：后者会稀释学习注意力、每轮烧 token |
| 技能即插件 / 动作注册契约（ActionSpec） | read_file 已是 origin=local 的 ActionSpec，零新增契约；`learn_orchestrate.py:238-244` 注释自己写着「学习侧与消费侧必须同一份工具菜单」——这次修复正是把这个原则补齐到场景型环境 | ✅ |
| 判据分层（LLM 判 + 确定性否决） | 真空防线 `_material_real`（learn_orchestrate.py:172-205）**已有文档侧口径**：正文类工具取回 ≥20 字正文算真材料——read_file 天然过真空防线，判官零改动。字面量守卫补文本对偶：技能里的引文必须在 sidecar 全文**字面可定位**（确定性检查，同 SQL 字面量守卫同构） | ✅ |
| 场景隔离 | uploads/ 在 scene_data/<sid>/ 下；read_file 的 _resolve_under 只认本场景目录 | ✅ |
| 「文件属于本次对话」语义（upload._fresh 30min） | read_file 读 sidecar 持久文件不受 30min 影响，但学习入口应只暴露**本轮对话上传的**文件（按 uploads_meta ts 过滤生成一份清单/提示），避免旧文件永久滞留 | ✅ 需要在实现里显式守 |
| 原版 Voyager 同构 | 原版学习靠环境观察反馈；MVE 场景型环境的"环境"= db + 场景文件。文档缺席 = 环境缺席——这才是场景 J 学习必然失败的原理层解释 | ✅（这是必要性最强的一条） |

## 三、风险与边界

1. **循环验证**：材料来自文集、验证也对着文集 → 必须配「引文字面可定位」
   守卫 + provenance=upload（接开发项 #11：不得标「库内可复现」），否则
   学习器自说自话结晶。
2. **工具说明书**：local_agent.tool_descriptions 注释（2026-10-29 实测）
   证明光给函数名模型不会用——清单/提示里要写明 uploads/ 里有
   什么文件（文件名+字数），否则重演「4 次重试全在 SELECT web_docs」。
3. **范围**：只动 kind=scene 的环境绑定（学习侧 local_dir 兜底 +
   consume.py:1390 的 else 条件），不碰表格落表路径，不碰 VLML 域。

## 四、结论

- **必要性：成立且强**——不是"锦上添花的通路"，而是文档型场景学习
  结构性不可能 → 必然污染技能库的根因修复。
- **一致性：推荐形态比原始表述更优**——不往 prompt 塞上下文，而是把
  **已存在的 read_file/list_files 原语注册进 kind=scene 环境** +
  学习入口给 uploads 文件清单 + 引文字面守卫 + provenance 标记。
  改动面：learn_orchestrate.py:695（local_dir 兜底 scene_profile.scene_dir）
  、consume.py:1390（scene 分支补注册原语）、learn 入口加 uploads 清单、
  字面量守卫加文本对偶、#11 provenance。共 5 处，全部顺既有契约。

## 五、落地记录（2026-11-03 实施）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `mve/scene_profile.py` | kind=scene（场景目录里有库）分支补 `scene_dir` —— 事故根因：这个分支原来只给 db_path，`profile('J')['scene_dir']` 恒 None |
| 2 | `mve/learn_orchestrate.py:695/:720` | `local_dir`/`exec_dir` 对非 VLML 场景一律取本场景目录（原来只有 kind=local 才有），available_grabs 于是能注册 read_file/list_files |
| 3 | `voyager_vlml/consume.py:1311/:1390` | `_scene_dir` 兜底 `scene_profile.scene_dir()`；kind=scene 分支在 SceneDBEnv 之外**补注册本机原语**（db_path 一起传，库仍是唯一可查的库） |
| 4 | `mve/upload.py` | 新增 `uploads_manifest()`（文件名+字数+read_file 路径+禁止编造）与 `uploads_corpus()`（全文拼接，给引文守卫做底本） |
| 5 | `mve/learn_orchestrate.py` | 学习侧 `_hint_ctx` 注入 uploads 清单（`out["uploads_hint"]` 留痕） |
| 6 | `mve/learn_orchestrate.py` | 新增 `_provenance_of()`（按实际调用工具判 db/upload/web）与 `_upload_quote_guard()`（引文必须全文可定位），两条技能写入通道（维度/组合）都接上 |
| 7 | `mve/skill_store.py` | `add()` 新增 `provenance` 参数，新条目与重写通道都落库 |
| 8 | `voyager_vlml/consume.py:2165` | 答案口径按被复用技能的 provenance 写：「上传文件半场：引文可定位（复现需同一份文件）」替代冒名的「库内已验证」 |
| 9 | `voyager_vlml/consume.py:1170` | `_file_ctx` 过期后不再变空：附上 uploads 清单，规划员的 file 路有据可依（30 分钟只限"自动贴上下文"，不限"读得到"） |

**测试**：新增 `mve/test_upload_learning.py`（20 项，全过）；回归
`test_upload`(30) / `test_ext_scene`(14) / `test_reachability` / `test_context_memory` /
`test_learn_orchestrate` / `test_learning_loop` / `test_skill_store_cache` /
`test_reply_contract`(67) / `test_literal_guard_boundary`(7) 全过；
`test_scene_backend` 改动前后同为 **89 过 / 22 挂**（挂的是既有环境问题：
测试进程内无事件循环、无真实 LLM），零回归。
