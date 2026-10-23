"""VLML 同构的 action（对应原版 voyager/agents/action.py）。

原版：模型照 control_primitives/*.js 写 **JS 代码** → mineflayer 执行 → 观察原样回灌。
这里：模型照 control_primitives 的 **SQL 原语** 写 **SQL** → VLML MCP 环境执行
（DB / RIB / VLGG）→ 观察原样回灌 {ok, tool, rows, error, data}。

用户指令②「把 js 代码指定 sql」+ ④「迭代修正机制 prompt 里的语言改为 sql」。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from voyager_vlml.control_primitives import render_primitives  # noqa: E402
from voyager_vlml.env.vlml_mcp import VLMLMCPEnv  # noqa: E402


def _extract_sql(text: str) -> str:
    """从模型输出里抠出 SELECT。

    模型可能回三种形态，都要接住（实测只按 JSON 解析会漏掉裸 SQL，导致
    "模型没给出 SELECT" 空转三轮）：
      1. JSON      {"sql": "SELECT ..."}
      2. 代码围栏  ```sql SELECT ... ```
      3. 裸文本    SELECT ...
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    # 1) JSON（含 ```json 围栏）
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    blob = m.group(1) if m else raw
    s, e = blob.find("{"), blob.rfind("}")
    if s >= 0 and e > s:
        try:
            obj = json.loads(blob[s : e + 1])
            if isinstance(obj, dict) and obj.get("sql"):
                return str(obj["sql"]).strip()
        except json.JSONDecodeError:
            pass
    # 2) ```sql 围栏
    m = re.search(r"```(?:sql)?\s*(SELECT[\s\S]*?)```", raw, re.I)
    if m:
        return m.group(1).strip()
    # 3) 裸 SELECT
    m = re.search(r"(SELECT[\s\S]+?)(?:;|$)", raw, re.I)
    if m:
        return m.group(1).strip()
    return ""


def _render_last_result(obs: dict[str, Any], max_rows: int = 3,
                        max_cols: int = 8) -> str:
    """把上一轮**真的取回来的结果**渲染成文本（列名 + 前几行）。

    为什么非要有这一块（2026-10-23，对齐原版 `voyager.py:262-268`）：
      原版 `render_human_message(events=events)` 回灌的是**整个观察**
      （背包、位置、附近方块……），模型能看见「我这一步到底改变了什么」。
      这边原来只回灌 `critique`（= 行数 + 报错串），于是：
        · SQL 报错 → 能修（错误串里有列名）；
        · **SQL 没报错但取错了列/口径 → 修不了** —— 模型看不见返回了什么，
          只能凭记忆重写一遍差不多的，或干脆换一个说法。
      实测后果（见 docs/外部task闭环.md §7.2）：把 `round_number = 1` 当首血，
      取到数、critic 判成功、错误配方进技能库 —— 因为「取到了数」这条判据
      本身看不到口径，而模型也没有材料自查。
    给出列名 + 样例值之后，模型才有机会自己发现「我这几列不是题干要的量」。
    """
    data = (obs or {}).get("data")
    if data is None:
        return ""
    rows: list[Any] = []
    cols: list[str] = []
    saw_rows_key = False
    if isinstance(data, dict):
        for k in ("columns", "cols", "column_names"):
            v = data.get(k)
            if isinstance(v, list) and v and isinstance(v[0], str):
                cols = [str(x) for x in v]
                break
        for k in ("rows", "data", "results", "records", "items"):
            v = data.get(k)
            if isinstance(v, list):
                rows = v
                saw_rows_key = True
                break
        # 有 rows 键但它是空列表 = 真空集（**不能**再把 dict 当一行，
        # 否则会渲染出 `columns=['a']，rows=[]` 这种没用的文本）
        if not saw_rows_key:
            rows = [data]
    elif isinstance(data, list):
        rows = data
    else:
        rows = [data]
    if not rows:
        return "（结果为空集，0 行）"
    out: list[str] = []
    head = rows[0]
    if isinstance(head, dict):
        names = cols or [str(k) for k in head.keys()]
        out.append("列：" + "、".join(names[:max_cols]))
        for r in rows[:max_rows]:
            if isinstance(r, dict):
                out.append("行：" + "，".join(
                    f"{k}={str(v)[:24]}" for k, v in list(r.items())[:max_cols]))
            else:
                out.append(f"行：{str(r)[:200]}")
    elif isinstance(head, (list, tuple)):
        if cols:
            out.append("列：" + "、".join(cols[:max_cols]))
        for r in rows[:max_rows]:
            seq = list(r) if isinstance(r, (list, tuple)) else [r]
            if cols and len(cols) >= len(seq):
                out.append("行：" + "，".join(
                    f"{cols[i]}={str(v)[:24]}" for i, v in enumerate(seq[:max_cols])))
            else:
                out.append("行：" + "，".join(str(v)[:24] for v in seq[:max_cols]))
    else:
        out.append("行：" + str(head)[:200])
    if len(rows) > max_rows:
        out.append(f"（共 {len(rows)} 行，只列前 {max_rows} 行）")
    return "\n".join(out)


class VLMLActionAgent:
    """在 VLML MCP 环境里执行取数动作（SQL）。"""

    def __init__(self, env: VLMLMCPEnv | None = None) -> None:
        self.env = env or VLMLMCPEnv()

    async def run_sql(self, sql: str) -> dict[str, Any]:
        """执行一条 SQL，回一份可枚举观察。"""
        return await self.env.run_sql(sql)

    async def run_calls(self, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按序执行多个取数动作（call = {name, args}），回观察列表。"""
        out: list[dict[str, Any]] = []
        for c in calls:
            out.append(await self.env.call(str(c.get("name")), **(c.get("args") or {})))
        return out

    # ---------- LLM 写 SQL（用户指令：让闭环真正自己出题取数） ----------
    def write_sql(
        self,
        question: str,
        *,
        schema: str = "",
        critique: str = "",
        last_sql: str = "",
        last_result: str = "",
        skills: list[dict[str, Any]] | None = None,
        hints: list[str] | None = None,
    ) -> str:
        """让模型按题面写一条 SELECT；迭代修正时带上一轮的 critique 与 SQL 重写。

        只收 SELECT（不接受其它语句），拿不到就返回空串由调用方判失败。
        """
        from voyager_vlml.llm import chat

        system = self.render_system_message(skills=list(skills or []))
        # 知识点自带的取数要求（表名/字段/口径）必须进 prompt。
        # 2026-10-23 实测：只把它传给 critic、不传给写码那一步 → 模型在
        # `ext_player_round_stats` 上猜 `event_type` 并拿 `round_number=1` 当首血，
        # 连跑 3 轮全 empty；而节点 skills 里明明写着 "agg_first_blood_stats 的
        # 粒度是 round_id / 字段 fb_team 与 fb_team_won"。
        if hints:
            system += ("\n\n【这个知识点的取数要求（照它选表选字段，别自己另起一套）】\n"
                       + "\n".join(f"- {h}" for h in list(hints)[:5]))
        if schema:
            system += (
                "\n\n【当前库真实 schema（只能用这里的表名/列名，不要编造）】\n"
                f"{schema}\n"
            )
        user = self.render_iteration_prompt(
            task=question,
            critique=critique or "（首轮，直接写一条 SELECT 回答这个问题）",
            last_sql=last_sql,
            last_result=last_result,
        )
        text = chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.3,
        )
        sql = _extract_sql(text)
        if not sql.lower().startswith("select"):
            return ""
        return sql

    async def solve(
        self,
        question: str,
        *,
        max_iters: int = 3,
        hints: list[str] | None = None,
        skills: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """出题之后真正取数：写 SQL → VLML MCP 执行 → 失败带 critique 重写（迭代修正）。

        `skills` = 检索到的已学会配方（照原版 voyager.py:195 先 retrieve 再跑）。
        它进 system 区块，所以**迭代的每一轮都带着**，不是只在第一轮给。

        返回 (观察列表, 最后一条 SQL)。观察是**可枚举的**：工具成败 + 取到几行。
        """
        from voyager_vlml.agents.critic import VLMLCriticAgent

        critic = VLMLCriticAgent()
        try:
            schema = await self.env.schema_block()
        except Exception:
            schema = ""

        observations: list[dict[str, Any]] = []
        sql = ""
        prev_sql = ""
        critique = ""
        last_result = ""
        n_skills = len(list(skills or []))
        # 同构原版 voyager.py:196-198 —— 打印注入了多少条已学会配方（原版只打印数量，
        # 不打印整条 system 消息：把 schema + 配方库全打出来会把日志淹没）。
        print(f"[Action Agent] 用 {n_skills} 条已学会配方渲染 system 消息")
        print(f"[Action Agent] 任务: {question[:140]}")
        for i in range(max_iters):
            sql = self.write_sql(
                question, schema=schema, critique=critique, last_sql=prev_sql,
                last_result=last_result,
                skills=list(skills or []), hints=list(hints or []),
            )
            if not sql:
                observations.append(
                    {"ok": False, "tool": "write_sql", "rows": 0,
                     "error": "模型没给出 SELECT 语句"}
                )
                print(f"  [第 {i + 1}/{max_iters} 次] 没产出 SELECT，重试")
                critique = "上一轮没给出 SELECT。请只输出一条以 SELECT 开头的查询。"
                continue
            prev_sql = sql
            # 同构原版 voyager.py:218 —— 打印模型给出的动作（AI 消息）。
            print(f"  [第 {i + 1}/{max_iters} 次] 提案 SQL: {sql[:200]}")
            obs = await self.run_sql(sql)
            observations.append(obs)
            ok, critique = critic.check_task_success(
                observations=[obs],
                task={"task": question, "hints": list(hints or [])},
            )
            if ok:
                print(f"  [第 {i + 1}/{max_iters} 次] 成功 -> 工具={obs.get('tool')} 行数={obs.get('rows')}")
                print(f"[Action Agent] 任务在第 {i + 1} 次尝试成功")
                break
            # 下一轮要回灌「这一轮实际取回了什么」——不只是行数，是列名和值。
            last_result = _render_last_result(obs)
            # 同构原版 voyager.py:293 / 274 —— 展示环境反馈 + critic 判决，然后重试。
            print(f"  [第 {i + 1}/{max_iters} 次] 失败 -> {str(obs.get('error') or '没取回数据')[:160]}")
            print(f"  [critic] {critique[:240]}")
            if last_result:
                print(f"  [回灌] 上一轮取回：{last_result.splitlines()[0][:160]}")
            print("  -> 重试！")
        else:
            print(f"[Action Agent] 任务在 {max_iters} 次尝试后失败（最后 critique: {critique[:240]}）")
        return observations, sql

    # ---------- 迭代修正的 prompt（语言 = SQL） ----------

    # 已学会的技能（可复用 SQL）怎么用的说明。
    #
    # 照原版 `render_system_message(skills=...)`：原版把技能 code 直接拼在
    # control_primitives 后面（`action.py:94` 的 `+ skills`），**一句解释都没有**。
    # 这里必须多这一段，因为两边技能形态不同：
    #   原版技能 = **带参数的函数**（`async function mineBlock(bot, name, count)`），
    #     复用天然就是"换参数调用"，不需要教；
    #   我们的技能 = **一整条写死了字面量的 SQL**（`team_name = 'NRG'`），
    #     是实例不是函数。不写这段，模型会把上次的值原样抄过来，
    #     取到的就是上一题那一队的数 —— 看着像复用，其实是记答案。
    _SKILL_USAGE = """【已学会的取数配方】下面是你以前跑通过、已经存进技能库的 SQL。

怎么用（这几条就是复用的判据）：
1. 复用的是配方的**计算方法** —— 用了哪些表、怎么 join、怎么聚合、怎么筛选。
2. **配方里写死的实体字面量（队伍名 / 图名 / 赛事号 / 选手名）是上一次的值，
   必须换成当前这道题指的那个值。** 抄上一次算出来的数是记答案，不是复用。
3. 当前这道题没指定实体（比如问"哪支队伍最高"）→ 保留配方的分组结构，别自己造值。
4. 配方算的量与当前这道题问的量**不是同一个量** → **不要用**，照题面从零写。
   换队伍名算复用；换个完全不同的量（K/D 比 → 连败数）不算，那是张冠李戴。
5. 只输出一条 SELECT。"""

    def render_system_message(
        self, *, skills: list[dict[str, Any]] | None = None
    ) -> str:
        """写码阶段的 system：取数原语 + 已学会的配方。

        同构原版 `action.py:94`：技能拼在原语后面，占同一个 system 区块，
        迭代修正的每一轮都带着（不是只带第一轮）。
        """
        base = (
            "你是在 VLML 电竞数据库里取数的分析 agent。用下面的原语写 **SQL** 完成任务。"
            "\n\n"
            + render_primitives()
        )
        blocks: list[str] = []
        for i, s in enumerate(list(skills or [])[:5]):
            code = str(s.get("code") or "").strip()
            if not code:
                continue
            blocks.append(
                f"-- 配方 {i + 1}｜topic={s.get('topic') or ''}\n"
                f"-- 上次用它做的是：{str(s.get('text') or '')[:160]}\n"
                f"{code[:1200]}"
            )
        if not blocks:
            return base
        return base + "\n\n" + self._SKILL_USAGE + "\n\n" + "\n\n".join(blocks)

    def render_iteration_prompt(
        self, *, task: str, critique: str, last_sql: str = "",
        last_result: str = ""
    ) -> str:
        """迭代修正 prompt：带着上一轮的 critique 重写 SQL（语言 = SQL，不给答案）。"""
        parts = [f"任务：{task}"]
        if last_sql:
            parts.append(f"上一轮你写的 SQL：\n{last_sql}")
        if last_result:
            # 同构原版 voyager.py:262-268 `render_human_message(events=events)`：
            # 上一轮**真的取回来的东西**必须回灌，否则「没报错但取错列」修不了。
            parts.append(f"上一轮这条 SQL 实际取回的结果：\n{last_result}")
        parts.append(f"上一轮的环境反馈：\n{critique}")
        parts.append(
            "请**只改 SQL**（表名/列名/过滤条件/聚合方式），重新给出一条能在当前库执行的 SELECT。"
            "改之前先对照「任务」和「上一轮取回的结果」：如果取回的列不是题干要的那个量，"
            "说明表的选法或口径错了，别再在原句子上微调。"
        )
        return "\n\n".join(parts)


def _main() -> int:
    """冒烟：在真实库上跑一条 SQL，看观察与迭代 prompt。"""
    import asyncio

    agent = VLMLActionAgent()
    sql = "SELECT COUNT(*) AS n FROM games"

    async def go() -> None:
        obs = await agent.run_sql(sql)
        print("观察:", {k: obs[k] for k in ("ok", "tool", "rows", "error")})
        print("数据:", str(obs.get("data"))[:150])
        print("--- 迭代 prompt（SQL 语言）---")
        print(
            agent.render_iteration_prompt(
                task="数一下有几张图",
                critique="  ✗ query_sql：no such column: map",
                last_sql="SELECT COUNT(*) FROM games WHERE map='Lotus'",
            )[:400]
        )

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
