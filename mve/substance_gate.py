#!/usr/bin/env python3
"""取数守卫 —— 照抄 maieutic 的「实质 vs 空壳」口径（2026-11-09）。

为什么有这个模块
----------------
MVE 消费侧原来判「这轮有没有取到真数」用的是**形状绑定**：

    consume.py:2312
        elif not (data.get("rows") if isinstance(data, dict) else data):
            data = None

也就是「结果必须是 `{rows: [...]}` 这种 SQL 形状才算数」。这是 **Voyager 血** ——
原版 Voyager 的技能全是挖方块，产出必然是「物品栏 = 若干行」，所以形状判据成立。
但 MVE 的执行层已经接了 MCP 原语（sherlock / 搜索 / fetch），返回的是**任意 dict**
（没有 `rows` 键）→ 真取到的数被这道闸当成真空**丢掉**。

2026-11-09 服务器实测（场景 L，Sherlock 题）：
    traj           = ['mcp_sherlock_lookup_username']   ← 工具真调了
    rows           = 1                                    ← 真取到 1 行
    ok_tool_calls  = 0                                    ← 但被闸门打掉了
    judgment_result= 'fetched_empty'
模型那轮写的代码是 `RESULT = sherlock_result`，完全正确 —— 被丢掉的是**闸门**。

maieutic 的同构
---------------
maieutic **全仓没有任何形状绑定的判定**（搜 hasContent / nonEmpty /
emptyShell / isBlank / hasSubstance / mustContain 等 13 个关键词零命中）：
它的全部判定输入都是**文本**，判的是「这份文本有没有实质内容」。

它区分「形式上非空但没实质」和「真实但简短」用两条规则：

1. 三档语义（`math-solution-gate.ts:54-57`）：

       - "no":     absent or only planned
       - "partial": some actual work, but incomplete or missing required values
       - "yes":    actual result/reasoning satisfies the checkpoint

   配套硬约束（`math-solution-gate.ts:44-46`）：「我打算…」这类**计划态**不给分。

2. `isPlanOnly` 启发式（`math-runtime-evaluator.ts:77-80`）：

       const isPlanOnly =
         /\\b(i will|i plan|i need to|would|later)\\b|我会|计划|准备|稍后/.test(text)
         && !/(=|所以|therefore|故|得|解得|答案|结论|final)/i.test(text);

   **「我打算…」但没有结论 → 判 0；有结论就认。** 这正是我们要的：
   不判形状，判实质。

3. 反向配套（`math-coverage.ts:52-53`）：
   「长文本不得稀释一个强承诺，流畅的散文不得补偿一个缺失的点」
   —— 否掉「看长度 / 看非空」这种判法。

本模块做什么
------------
把上面三条搬成**确定性**判据（不调 LLM）：

    grade(substance) -> {"level": "yes"|"partial"|"no", "why": str}

`yes`   = 真取到东西（数据里有可核对的真实值）
`partial` = 取到了东西但不完整（部分真空值 / 只有部分维度有数）
`no`    = 没取到（空 / 全 NULL / 全零 / 只有计划态文本）

消费侧只拿 `yes` 当「取到真数」（原来的 `data is not None` 口径），
`partial` 如实披露但不判失败 —— 对应原版 maieutic「配置清单满足即 passed，
清单外的发现不阻塞闭合」。

形状无关怎么做到的
------------------
不判断「结果是不是某个键」，而是**遍历结果里的所有叶子值**，看有没有一个是
**真实值**（非 NULL / 非空串 / 非零 / 非空容器）。这与
`voyager_vlml/agents/critic.py:is_vacuous` 同一条铁律（「取到数 = 至少一个
真实值」），但那个函数的 `_iter_value_rows` 只认 `rows/data/results/records/
items` 五个键 + 把 dict 当一行 —— 对 MCP 的嵌套 dict 会误判。
本模块改成**递归下探到叶子**，任何形状一视同仁。
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# 计划态检测（照抄 math-runtime-evaluator.ts:77-80 的 isPlanOnly）
# ---------------------------------------------------------------------------
# 「我打算 / 我计划 / 我需要 / 我会 …」—— 只有计划、没有结论。
_PLAN_RE = re.compile(
    r"\b(i will|i plan|i need to|i would|would like|will try|later)\b"
    r"|我会|我将|我要|我打算|我计划|我准备|稍后|待会|下一步",
    re.I)
# 结论标记：出现任一个 → 不是纯计划（有结论就认，照抄原版的 `&&` 结构）。
_CONCL_RE = re.compile(
    r"(=|所以|因此|故|得|解得|答案|结论|最终|结果是|therefore|result)",
    re.I)


def is_plan_only(text: Any) -> bool:
    """这段话是不是「只有计划、没有结论」？

    照抄 maieutic `math-runtime-evaluator.ts:77-80`：

        isPlanOnly = 命中计划词 && **不**命中结论词

    —— 有结论就认（有结论的计划是「做了」，纯计划是「没做」）。
    """
    t = str(text or "").strip()
    if not t:
        return False
    return bool(_PLAN_RE.search(t)) and not bool(_CONCL_RE.search(t))


# ---------------------------------------------------------------------------
# 真实值判定
# ---------------------------------------------------------------------------
def _is_real_value(v: Any) -> bool:
    """这个叶子值算不算「真实值」（有内容、不空、不零）。

    与 `voyager_vlml/agents/critic.py:_is_zeroish` 同口径，但**bool 不算空**
    （True/False 是有信息的结论，不是空值）—— 原版注释明说「布尔值不算」。
    """
    if v is None:
        return False
    if isinstance(v, bool):
        return True              # bool 是结论，不是空
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple, set)):
        return len(v) > 0
    if isinstance(v, dict):
        return len(v) > 0
    return True


def _leaves(value: Any, _depth: int = 0) -> list[Any]:
    """把结果摊成**叶子值**列表（递归下探，任何形状一视同仁）。

    为什么不用 critic 的 `_iter_value_rows`：那个只认 `rows/data/results/
    records/items` 五个键，MCP 返回的嵌套 dict（`{"found": {...}}`）会被
    当成「一行 → 值全是 dict → 非空ish」或「空表 → 真空」，两种都错。
    这里**下探到叶子**：`{"found": {"github": "http://..."}}` 的叶子是那个
    字符串 URL → 真值 → 算数。

    ⚠️ **表头不是数据**（2026-11-09 回归测试抓到的）：
    `{"columns": ["a", "b"], "rows": [(None, 0)]}` 的 `columns` 是**列名**，
    不是取到的值 —— 把它算成真值，这条空聚合就会被判 partial（2/4 非空）
    而不是真空，等于绕过了「空表纯聚合 (NULL,0) 不算数」这条铁律。
    所以：**带行状字段（rows/data/results/records/items）时，只看那些行**，
    表头/元信息一律不算（照 maieutic「判官看材料账，判的是**值**不是
    字段名」）。
    """
    if _depth > 12:              # 防自引用 / 超深结构
        return []
    if isinstance(value, dict):
        out: list[Any] = []
        # 带行状字段 → 只看行（表头 columns 等元信息不算数）
        _rows = None
        for k in ("rows", "data", "results", "records", "items"):
            v = value.get(k)
            if isinstance(v, list):
                _rows = v
                break
        if _rows is not None:
            for r in _rows:
                out.extend(_leaves(r, _depth + 1))
            return out
        for v in value.values():
            out.extend(_leaves(v, _depth + 1))
        return out
    if isinstance(value, (list, tuple, set)):
        out = []
        for v in value:
            out.extend(_leaves(v, _depth + 1))
        return out
    return [value]


def _has_real(value: Any) -> bool:
    """这个结果里有没有至少一个真实值。"""
    return any(_is_real_value(v) for v in _leaves(value))


def _count_real(value: Any) -> tuple[int, int]:
    """(真实叶子数, 叶子总数) —— 用于区分 yes 与 partial。"""
    lv = _leaves(value)
    if not lv:
        return (0, 0)
    return (sum(1 for v in lv if _is_real_value(v)), len(lv))


# ---------------------------------------------------------------------------
# 主判据
# ---------------------------------------------------------------------------
def grade(result: Any, *, answer: str = "", evidence: Any = None) -> dict[str, Any]:
    """判「这一轮到底取到东西没有」。

    参数
    ----
    result   : 执行层交上来的结果（dict / list / 标量都认，形状无关）
    answer   : 执行层交上来的答案文本（只用来判「纯计划」）
    evidence : 执行层的证据条目列表（ok 的条目）—— **材料账**口径：
                判官看的是材料，不是结果对象的形状（照 maieutic
                `maieutic_gate._material_block` 的同一条口径）。

    返回
    ----
    {"level": "yes"|"partial"|"no", "why": str, "real": int, "total": int}

    口径（照 maieutic math-solution-gate.ts:54-57 的三档）
    -----------------------------------------------
    yes    = 有真实值，且不是「只有计划」
    partial= 有真实值但不全（有真空叶子 / 只有部分维度有数）
    no     = 没有真实值 / 只有计划态文本
    """
    # ① 材料账优先（maieutic 口径：判官看材料账，不看结果形状）
    mat = _material_text(evidence)
    if mat:
        real, total = _count_real(mat)
        if real:
            # 有材料且材料里有真实值 → yes（除非整段是纯计划）
            if is_plan_only(mat):
                return {"level": "no",
                        "why": "材料里只有计划态描述（没结论）—— 不算取到真数",
                        "real": real, "total": total}
            return {"level": "yes",
                    "why": f"材料账里有真实内容（{real}/{total} 处非空）",
                    "real": real, "total": total}

    # ② 没有材料账 → 退到结果本身（同样形状无关）
    if result is None:
        return {"level": "no", "why": "执行层没有产出任何结果",
                "real": 0, "total": 0}
    real, total = _count_real(result)
    if real == 0:
        # 全是空值（None / "" / 0 / 空容器）
        if total == 0:
            return {"level": "no", "why": "结果为空（没有任何值）",
                    "real": 0, "total": 0}
        return {"level": "no",
                "why": f"结果里 {total} 个值全是空（NULL/空串/0）—— 真空",
                "real": 0, "total": total}
    if is_plan_only(answer) and not is_plan_only(result):
        return {"level": "no",
                "why": "答案只有计划态描述（我打算/我将…）没有结论 —— 不算取到真数",
                "real": real, "total": total}
    if real < total:
        return {"level": "partial",
                "why": f"取到部分真值（{real}/{total} 处非空，其余是空值）",
                "real": real, "total": total}
    return {"level": "yes",
            "why": f"取到真值（{real}/{total} 处非空）",
            "real": real, "total": total}


def _material_text(evidence: Any) -> str:
    """证据条目 → 材料正文（只取 ok 的，照 maieutic `_material_block` 口径）。

    复用 `maieutic_gate._material_block` 的取舍逻辑（只列取成功的条目），
    但这里只要**正文**（判实质用），不排版、不截断成清单。
    """
    out: list[str] = []
    for e in (evidence or []):
        if not isinstance(e, dict) or not e.get("ok"):
            continue
        raw = e.get("text")
        if not raw:
            d = e.get("data")
            if isinstance(d, dict) and isinstance(d.get("text"), str):
                raw = d["text"]
            elif d is not None:
                try:
                    import json as _json
                    raw = _json.dumps(d, ensure_ascii=False)
                except Exception:
                    raw = str(d)
        s = str(raw or "").strip()
        if s:
            out.append(s)
    return "\n".join(out)


def took_real_number(g: dict[str, Any]) -> bool:
    """消费侧口径：这轮**取到东西了**没有。

    ★ 为什么 `partial` 也算（2026-11-09 回归测试抓到的设计错误）：
      maieutic 的三档语义（`math-solution-gate.ts:54-57`）是

          "no":      absent or only planned
          "partial": some actual work, but incomplete or missing required values
          "yes":     actual result/reasoning satisfies the checkpoint

      —— `partial` 是「**做了但不全**」，**不是「没做」**。它是**有进展**的
      判定。原版对 partial 的处理是「照样计分、只是不全」。

      我先前把口径写成「只有 yes 才算数」，结果 MCP 返回里只要混着一个
      `count: 0` 这样的零值字段（零是真空值但**不是没有数据**），整轮就被
      当成「没取到」→ 第五根因换个形状又回来了。
      正确的收口是照原版三档：**只有 `no` 才置空**（真空），
      `partial` 保留数据 + 如实披露「取到部分真值」。

    ⚠️ 但「空表上的纯聚合返回 1 行 (NULL, 0)」仍必须判 `no` ——
    那条铁律由 grade() 保证（整表没一个真实值 → no），本函数照单收口。
    """
    return str((g or {}).get("level") or "") != "no"
