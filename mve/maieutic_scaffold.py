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
（材料可能很粗糙），你的任务不是回答它，而是产出一份**追问骨架**，让一个只会取数/
检索的助手能一个问题一个问题地把这块知识补齐。

你要产出三样东西：

0. milestone —— 这整份骨架本身就是一个**里程碑**：一个能被命名、能被判"达成了没有"
   的能力单元（同构课程里的一章/一个单元）。
   · name：这句短语，像教材里的单元标题（如"阿斯伯格与孤独症谱系的边界"），
     **不许**是"回答以下问题"这种空话，也不许把原问题原样抄一遍；
   · why：为什么把它算一个里程碑（答清它之后，能力上多了什么）。
   ⚠️ 里程碑**只在这里生成一次**（骨架生成时），运行时不再生成 —— 运行时做的
   只是把下面的 dimensions 一条条闭合。所以 name 要能撑住"以后复用它"这件事。

1. dimensions —— 要真正答清楚这句问题，**必须先做出承诺/必须回答**的具体问题。
   每条都是一个可以被回答、也可以被判对错的具体问题，不是一个笼统的标签。
   · 「处理边界情况」→ **不合格**
   · 「阿斯伯格与孤独症在语言发育上的区别是什么？」→ **合格**
   维度数量**与问题复杂度成比例**：一句话能答清的 2-3 条；需要多角度（定义/机制/
   区别/适用条件/常见误解）的 5-7 条。**不要固定条数**，凑数就是仪式感。

2. expected_drifts —— 这题最可能出现的偏差模式，分三类（定义不许改）：
   · drift    —— 答得比问题要求的少，或漏掉了上面某个维度（最常见）
   · revision —— 换了一种仍然成立的答法（**不是错误**，不要当缺点报）
   · bug      —— 想答对但事实上错了（编数字、张冠李戴、把相似概念混为一谈）
   每条必须**针对这道题**：「可能回答不完整」→ 不合格；
   「把阿斯伯格当成孤独症的一个'轻度版本'来答」→ 合格。

硬约束：
- 维度必须**可回答**：要么能靠检索/取数回答，要么能靠明确的定义与权威说法回答。
  如果一个维度连"答成什么样算对"都说不清，就不要出它。
- 不要造出材料里没有的具体数字/专名。需要外部事实时，维度应该写成
  「X 的官方诊断标准是怎么规定的？」，而不是预设答案。
- 如果材料太模糊（比如只有两个字），照样产出你能产出的骨架，并在
  material_note 里如实说明模糊在哪 —— **不要拒绝**。

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

# 数据/SQL 场景专属约束：Maieutic 拆出的维度必须有**真实的 DB 操作落点**，否则会
# 凭空编出库里不存在的业务参数（实测：对着 payroll.duckdb 编出「时薪/加班费/税率」
# 全 unreachable）。这条指令只在场景有 db_path 时追加到 system 后面。
SCAFFOLD_SYSTEM_DB = """

【数据 / SQL 场景专属约束 —— 必须严格遵守】
这道问题发生在一个**真实数据库**里，下面的「数据库结构」列出了实际存在的表和列，
以及关键列的真实取值。你拆维度时：
- 每条维度都必须是能用**一条 SQL 查询直接回答**的具体问题，且明确指向下面真实
  存在的表/列（如「技术部在职员工的 base_salary 合计是多少」）；
- **只引用清单里实际存在的表名和列名**，绝不许编造表或列；
- **绝对不许**发明数据库里不存在的业务参数（如时薪、加班费率、税率、缺勤扣款、
  计件单价等）——如果某条维度需要这类参数而清单里没有对应列，**就不要出这条维度**；
- 聚合类问题（如「某部门某月应发工资」）应拆成「筛选目标人群」「取工资基数」
  「取补贴/津贴」「求和」这类指向真实列的**原子维度**，不要枚举单个实体
  （如"Get salary of Zhou Lin"），也不要把整道大题当成一条维度。"""


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
- 观察面写着「没取到」时，只许出「去取它 / 确认它是否存在」这类**取证维度**，
  不许出以它的内容为前提的判断维度。"""


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
    return chat(messages, temperature=0.3, max_tokens=2400)


def _call_llm(llm: Callable | None, messages: list[dict[str, str]]) -> tuple[dict, list, str]:
    """调出题器并解析，失败按原版重试（返回 (data, dims, 最后一次失败原因)）。"""
    last = ""
    msgs = list(messages)
    for _ in range(SCAFFOLD_LLM_RETRIES):
        try:
            text = (llm or _default_llm)(msgs)
        except Exception as e:
            last = f"LLM 调用失败：{type(e).__name__}: {e}"
            continue
        data = _parse(text)
        dims = _clean_dims(data)
        if dims:
            return data, dims, ""
        last = "LLM 输出里没有可用维度（解析失败/空列表）"
        msgs = list(messages) + [{"role": "user", "content": _RETRY_HINT}]
    return {}, [], last


def _cache_path(scene_id: str, question: str, db_path: str = "") -> Path:
    h = hashlib.md5(str(question or "").strip().encode("utf-8")).hexdigest()[:12]
    # db_path 进缓存键：场景换了库 / 首次加 schema 观察面，旧骨架（可能编了不存在
    # 的参数）不能继续命中，必须重拆。知识型场景 db_path="" 不影响旧键。
    if not db_path:
        # 进度/追问历史的读写常省略 db_path；但它们必须与 scaffold 写出的缓存文件
        # 同把钥匙，否则数据场景（db_path 非空）的进度会被写到一个不存在的文件、
        # 被 save_progress 的「读不到就静默返回」吞掉 —— 断点续学与里程碑「全闭合
        # 解锁」对数据场景名存实亡。这里回退到场景规范 db_path，和 scaffold 对齐。
        try:
            import scene_profile as _sp
            db_path = str((_sp.profile(str(scene_id or "A")) or {}).get("db_path") or "")
        except Exception:
            db_path = ""
    dh = hashlib.md5(str(db_path or "").encode("utf-8")).hexdigest()[:8]
    return CKPT / str(scene_id or "A") / f"{h}_{dh}.json"


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


def milestone_dimensions(scene_id: str, question: str) -> list[dict]:
    """读里程碑骨架的维度清单（只读，不调 LLM、不重新生成）。

    供消费侧「维度覆盖守卫」判定"这道题拆了几维"：导入式里程碑题（人导入的那句
    话被 scaffold 拆成多维度）的权威维度数就在这里。没骨架（自由提问、没建里程碑）
    返回空列表，守卫便回退到 _derive_dims 的维度判定，不会误伤原子题。
    """
    try:
        import json as _json
        p = _cache_path(str(scene_id or "A"), str(question or ""))
        if not p.exists():
            return []
        data = _json.loads(p.read_text(encoding="utf-8"))
        return [d for d in (data.get("dimensions") or [])
                if str(d.get("id") or "").strip()]
    except Exception:
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


def failed_dims(scene_id: str, question: str,
                *, limit: int = FAILED_SHOWN) -> list[tuple[str, str]]:
    """试过但**没做成**的维度 + 最后一轮判词 → [(问句, 为什么失败)]。

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

    口径（照 `clean_up_tasks`）：**已闭合的从 failed 里删掉** —— 同一条维度后来
    做成了就不该再算"太硬"。
    """
    try:
        qmap = {str(d.get("id") or ""): str(d.get("question") or "")
                for d in milestone_dimensions(scene_id, question)}
    except Exception:
        qmap = {}
    try:
        iters = load_progress_iterations(scene_id, question)
    except Exception:
        iters = {}
    if not iters:
        return []
    try:
        done = load_progress(scene_id, question)      # clean_up_tasks：完成的移出 failed
    except Exception:
        done = set()
    rows: list[tuple[str, str]] = []
    for did, it in iters.items():
        if did in done:
            continue
        q = (qmap.get(did) or "").strip()
        if not q:
            continue
        why = str(it.get("asked") or "").strip() or str(it.get("gap") or "").strip()
        rows.append((q, why))
    return rows[-max(1, int(limit)):]


def completed_dims(scene_id: str, question: str, *,
                   limit: int = 10) -> list[str]:
    """已经闭合的维度问句（原版 `Completed tasks so far:` 的 MVE 对应物）。

    原版拆解时两本账一起喂（failed 靠 `failed_tasks`，completed 靠
    `completed_tasks`）。MVE 的"已完成任务"就是 progress.learned，问句从骨架里取。
    """
    try:
        qmap = {str(d.get("id") or ""): str(d.get("question") or "")
                for d in milestone_dimensions(scene_id, question)}
    except Exception:
        return []
    try:
        done = load_progress(scene_id, question)
    except Exception:
        return []
    out = [qmap[d].strip() for d in done if str(qmap.get(d) or "").strip()]
    return out[-max(1, int(limit)):]


def render_material(question: str, scene_id: str, *, extras: str = "",
                    state: dict[str, Any] | None = None) -> str:
    """把「材料」渲染给模型：人问的那句话 + 这个场景现在有什么（复用观测，不另造）。

    `state` 是出参：把「这次到底喂进了哪份观察面」带回给 scaffold，用来决定
    要不要追加来源观察面专属约束（同构原版：喂了环境观察，才配得上"看得见环境"的规则）。
    """
    lines = [f"人想弄清楚的问题：{question}"]
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
    if extras:
        lines.append(str(extras))
    _dp = ""
    # 数据/SQL 场景：把**真实 DB 观察面**也喂进去，让骨架只拆出有落点的维度
    # （避免编出库里不存在的业务参数）。场景没有库时 db_path 为空，这一段不触发。
    try:
        import scene_profile as _sp
        _dp = str((_sp.profile(scene_id) or {}).get("db_path") or "")
        if _dp:
            from curriculum_auto import db_observation
            _obs = db_observation(_dp)
            if _obs:
                lines.append("数据库结构（实查，只列真实存在的表/列/取值，"
                             "维度只能指向这里面的表/列）：\n" + _obs)
    except Exception:
        pass
    return "\n".join(lines)


def scaffold(question: str, scene_id: str = "A", *, llm: Callable | None = None,
             use_cache: bool = True, extras: str = "", db_path: str = "") -> dict[str, Any]:
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
    path = _cache_path(scene_id, q, db_path)
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
    system_content = SCAFFOLD_SYSTEM + (SCAFFOLD_SYSTEM_DB if db_path else "") \
        + (SCAFFOLD_SYSTEM_SOURCE if _st.get("snapshot") else "")
    if _st.get("snapshot"):
        print(f"  · 拆骨架前先看来源观察面：{_st['snapshot']}", flush=True)
    if _st.get("failed"):
        print(f"  · 拆骨架前带上失败账：{_st['failed']} 条试过没做成的"
              "（原版 Failed tasks that are too hard）", flush=True)
    messages = [{"role": "system", "content": system_content},
                {"role": "user", "content": _material
                 + "\n\n请产出追问骨架，只输出 JSON。"}]
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
        _atomic_write(path, {**data, "question": q,
                             **({"progress": _carry} if isinstance(_carry, dict)
                                else {})})
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
    try:
        _atomic_write(path, data)
    except Exception:
        pass


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


def milestone_rounds(scene_id: str, question: str) -> dict[str, Any]:
    """只读：整题层的记账（面板/日志用）。"""
    try:
        _p, _d, rec = _rounds_rec(scene_id, str(question or ""))
        return rec
    except Exception:
        return {}
