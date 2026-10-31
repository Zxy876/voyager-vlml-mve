"""语义收口：材料已经取到手，就用模型能力在维度上作答（2026-10-31）。

为什么单独一层（用户 2026-10-31 的判断，DeepSeek 同构）：

    「这一轮其实是取证（但取的不是数）是语义；纵使 readme 和代码在 agent
      视野里，它只用取数逻辑，也是万万不行。」

    生产实证（Scene H）：13 条维度里 11 条是「README 里关于 X 的描述是否准确/
    是否清晰」——这是**判断**，不是取数。执行层每次都 `gh_read(README)` 然后
    `RESULT = 原文`：材料确实取到了，但它把"取证"当成了"作答"。

DeepSeek 的做法（已核实的公开材料）：
  · R1 §2.2 `we abstain from applying neural reward models—whether outcome-based
    or process-based—to reasoning tasks`：只用规则化 reward（accuracy + format），
    理由是 reward hacking + 重训成本；§2.3 只约束**格式**、不加内容偏好
    （`avoiding any content-specific biases`）—— 它练的是「想」，不是「查」。
  · 方法论落点：能规则验证的领域，RL 才能把模型推到上限；验证不了的（开放写作）
    不给能力型 reward。
  · V3.2（2025-12）"Thinking in Tool-Use"：思考嵌在工具调用之间，模型在
    **工具返回之上**继续推理；而 V3.2-Speciale 干脆只保留思考、**不支持工具调用**
    —— 官方自己把「纯推理」与「推理+工具」分成两档。

→ 落到 MVE 的职责划分：
    骨架（拆维度）      = 我们给的任务结构
    取证（MCP/原语）    = 把材料搬进来，**不负责语义**
    语义（本模块）      = 模型在材料上作答，**不负责再取数**
    判据（critic/闸门） = 同一把尺子，判"答到了没有"

判据仍然是**可规则核验**的（DeepSeek 那条铁律）：
  模型必须交出 `quotes`，每条判断都要指认材料里的原句/字段值；本模块逐条在
  材料里做字面核对，一条都对不上 = 引文是编的 → 不算答到。这样"语义"这一步
  也有确定性闸门，不用再靠 LLM 判官兜底。
"""

from __future__ import annotations

import re
from typing import Any

SYSTEM = """你是判定者，不是检索器。材料已经取到手，工具已关闭。

只准依据下面给出的材料作答。输出 JSON：
{
  "verdict": "是 | 否 | 部分 | 无法判断",
  "quotes": [{"quote": "材料里的原句或字段值（逐字复制，不许改写）",
              "why": "它支持或反驳了什么"}],
  "answer": "对问题的完整回答：先给判断，再给依据，问到改法就给改法",
  "gap": "材料不足以判断时写明缺哪一块；否则空串"
}

规则：
- 必须给出判断，不能只复述材料；
- 每条判断都要指认材料里的具体句子/字段值，quote 必须能在材料里**逐字**找到；
- 引文要**照材料的样子抄**：材料写成 `license: ` 就抄 `license:`，材料一行一个
  文件名就抄那一行，不要自己拼接、不要加解释；
- 材料注明「只给前 N 字」时，只对已给出的部分下判断，未给出的部分写进 gap，
  **不许按常识补**；
- 材料不足以判断 → verdict 写「无法判断」，gap 写清**缺哪一份材料**并说明下一步
  怎么取（哪个工具、哪个文件/接口，例如「gh_read requirements.txt」），
  **不许猜**；
- answer 不许整段照抄材料；引用只许短句。
"""

# 引文核验的最小长度：一两个字的引文（「是」「有」）满材料都是，不构成「指认」，
# 但也不能因此就把「作者是张三」这种**编造短句**归到「太短」里去 —— 4 个字
# （一个中文词组）是能指认的最小单位。
MIN_QUOTE = 4

# 材料预算：与 local_agent._EVIDENCE_CHARS（单条证据 12000）配套。
# 一条证据刚好放得下；两条证据（如 gh_tree + gh_read）时按此预算依次放，
# 放不下的部分**标注**出来，不静默丢。
MATERIAL_LIMIT = 20000

_TRUNC_MARK = "\n…（这里已到材料预算上限，后面还有内容没放进来，不许假设）"

# 引文没过时的第二次提示（同构原版 propose_next_ai_task 的重试：
# 一次没写好 ≠ 这题不可答；但**只照材料重抄**，不许换说法蒙过核对）
_QUOTE_HINT = (
    "\n\n上一次的引文没有一条能在材料里逐字找到（或根本没给引文）。\n"
    "请重做一次，并且：\n"
    "- quotes 至少给 1 条，quote 必须是材料里**连着的原字**，直接复制，不要改写、"
    "不要拼接、不要在引文里写你自己的解释；\n"
    "- 材料里若写成 `key: value`，就抄 `key: value`；若是一行一个文件名，就抄那一行；\n"
    "- 材料确实没有支撑这条判断的内容 → verdict 写「无法判断」，gap 写缺什么。"
)


def material_text(evidence: list[dict[str, Any]] | None, *,
                  limit: int = MATERIAL_LIMIT) -> str:
    """把材料账压成一段可读正文（只取 ok 的；带来源标注，便于指认）。

    只放**真取到的内容**，失败项不进材料（与 _evidence_item 同一口径）。
    """
    parts: list[str] = []
    used = 0
    for e in (evidence or []):
        if not isinstance(e, dict) or not e.get("ok"):
            continue
        body = str(e.get("text") or "").strip()
        if not body:
            continue
        head = str(e.get("tool") or "?")
        q = str(e.get("args") or "").strip()
        src = str(e.get("source") or "").strip()
        label = head + (f"({q[:80]})" if q else "") + (f" [{src}]" if src else "")
        chunk = f"【材料·{label}】\n{body}"
        if used + len(chunk) > limit:
            keep = max(0, limit - used)
            if keep <= 0:
                break
            # 预算不够时**说清楚**：静默截断等于让模型「没看到=不存在」
            chunk = chunk[:keep] + _TRUNC_MARK
        parts.append(chunk)
        used += len(chunk)
        if used >= limit:
            break
    return "\n\n".join(parts)


def _norm(s: str) -> str:
    """归一化到「可比对」的形态：转义序列还原 + 标记剥离 + JSON 标点当分隔符。

    ⚠️ 三次生产实测踩到的（2026-10-31，Scene H）：
      ① 运行时交给判据的 answer 常是 `json.dumps` 过的字符串，里面的换行是
         **两个字符** `\\n`；材料是真换行。只压空白 → 跨行片段永远匹配不上
         → 搬运判据形同失效（README 全文照样通过）。
      ② 模型引用 Markdown 时常把 `>` `**` 这类标记丢掉，或反过来加回来。
         留着标记就变成"引文对不上"，会误伤真引文。
      ③ **材料是 JSON**（`gh_repo_info` / `gh_tree` 的返回）时，字段值被
         `"` `,` `{}` 包着，模型按人话抄成 `license: ""`、`A.md, B.md` →
         逐字对不上，「没有声明许可证」这种**正确**判断被判成「引文像是编的」。
         引号/逗号/括号/冒号是结构标点，不是内容 —— 两边都抹平成分隔符即可，
         抹平后仍然要求字面连续命中，不引入任何语义相似。
    """
    t = str(s or "")
    for a, b in (("\\r\\n", " "), ("\\n", " "), ("\\t", " "),
                 ('\\"', '"'), ("\\/", "/")):
        t = t.replace(a, b)
    t = re.sub(r"(?m)^\s*[-*+]\s+", "", t)   # 行首列表符号（材料一行一条时用）
    t = re.sub(r"[>#*`|]+", " ", t)          # Markdown 标记/表格竖线不算内容
    t = re.sub(r"[\"'“”‘’]", " ", t)          # 引号只是 JSON/Markdown 的包装
    t = re.sub(r"[{}\[\](),，、;；:：]", " ", t)  # JSON 标点、顿号、冒号同理
    return re.sub(r"\s+", " ", t).strip()


# 多段引文的切分：模型常把「A…B…C」写成一条引文（省略号表示中间略过）
_SEG_SPLIT = re.compile(r"…+|\.{3,}|。{2,}")


def _window_hit(q: str, material: str, span: int) -> bool:
    if len(q) < span:
        return False
    step = max(1, (len(q) - span) // 10)
    for i in range(0, len(q) - span + 1, step):
        if q[i:i + span] in material:
            return True
    return False


def _probe_hit(quote: str, material: str, *, span: int = 30) -> bool:
    """引文是否**有材料支撑**：整句命中，或任一 span 字滑窗命中。

    滑窗是为了容忍省略号、截断、前后缀（模型复述时常带"根据材料…"）。
    只认逐字，不做语义相似 —— 语义相似就等于把判据交回给模型自称。

    多段引文（`A…B`）按段核验：任一**够长**的段落在材料里逐字命中即算有支撑
    （省略号本来就表示"中间省略"，不能要求整条连着出现）。
    """
    q = _norm(quote)
    if not q:
        return False
    if q in material:
        return True
    for seg in _SEG_SPLIT.split(str(quote or "")):
        s = _norm(seg)
        if len(s) < MIN_QUOTE:
            continue
        if s in material or _window_hit(s, material, span):
            return True
    return len(q) >= MIN_QUOTE and _window_hit(q, material, span)


def is_material_dump(answer: str, material: str, *,
                     win: int = 60, floor: float = 0.6) -> tuple[bool, int]:
    """答案是不是**材料原文的搬运**（确定性判据，不花 LLM）。

    为什么必须有一条确定性判据（2026-10-31 生产实证）：
        同一个行为「gh_read → RESULT = 整篇 README」，10:0x 被 flash 判官连拒
        5 次，10:3x 又被放过 2 次（`vlml_details` r=1.0）。**判官会抖，判据不能抖。**

    判法（覆盖率高就是搬运）：把**答案**切成 `win` 字的窗口，逐窗看能否在材料
    里逐字找到；命中率 ≥ floor 判搬运。
    为什么按"答案的覆盖率"而不是"材料的切片"：答案在上游被截到 1200 字
    （`_answer_text`），材料上万字 —— 按材料切片抽查时只会命中第 1 段，
    永远判不出搬运（2026-10-31 实测踩到）。
    """
    a = _norm(answer)
    m = _norm(material)
    if len(a) < 200 or len(m) < 200:
        return False, 0
    probes = [a[i:i + win] for i in range(0, max(1, len(a) - win + 1), win)]
    probes = [p for p in probes if len(p) >= win // 2]
    if not probes:
        return False, 0
    hits = sum(1 for p in probes if p in m)
    pct = int(100 * hits / len(probes))
    return (hits / len(probes)) >= floor, pct


def verify_quotes(quotes: list[dict[str, Any]], material: str) -> tuple[bool, str]:
    """确定性核验：引文必须能在材料里逐字找到（可规则验证，不花 LLM）。

    为什么要有这一层：语义层一旦允许"自由发挥"，就等于把判据交回给判官的心情；
    DeepSeek 的做法是只信规则可验的东西。引文对不上 = 这一点没材料支撑。
    """
    mat = _norm(material)
    if not mat:
        return False, "没有材料"
    if not quotes:
        return False, "没有指认任何引文（判断没有材料支撑）"
    valid = [q for q in quotes
             if len(_norm((q or {}).get("quote") or "")) >= MIN_QUOTE]
    if not valid:
        return False, (f"引文都太短（不足 {MIN_QUOTE} 字），不能当作指认"
                       "（要抄材料里的一句话/一个字段值）")
    hit, miss, first_miss = 0, 0, ""
    for q in valid:
        s = _norm((q or {}).get("quote") or "")
        if _probe_hit(str((q or {}).get("quote") or ""), mat):
            hit += 1
        else:
            miss += 1
            if not first_miss:
                first_miss = s[:60]
    if hit == 0:
        # 把第一条对不上的引文带出去：日志/判词里能看出是"编的"还是"抄歪了"
        return False, (f"{miss} 条引文在材料里都对不上（像是编的）"
                       + (f"；例：{first_miss}" if first_miss else ""))
    return True, ""


def answer_over_material(task: str, material: str, *,
                         critique: str = "", llm: Any = None,
                         max_tokens: int = 1800, demos_text: str = "") -> dict[str, Any]:
    """在材料上作答一次（工具已关，不重取数）。

    返回 {"ok", "answer", "verdict", "quotes", "gap", "why"}。
    `ok=False` 表示这一步没产出可用答案（材料空/调用失败/引文核验不过），
    调用方据此保持"未闭合"，不要拿它当成功。

    `demos_text` = 同类问题**验得对**的示范（见 reason_demos.py，DeepSeek 的
    cold-start / rejection sampling 同构）。它只教"怎么下判断"，材料仍然是唯一
    事实来源 —— 所以放在材料**前面**、并且明说"别照抄它的内容"。
    """
    mat = str(material or "").strip()
    if not mat:
        return {"ok": False, "why": "没有材料可读（取证阶段没取到内容）",
                "answer": "", "verdict": "", "quotes": [], "gap": ""}
    base = ((str(demos_text).strip() + "\n\n") if str(demos_text or "").strip() else "") \
        + (f"问题：{task}\n\n"
            + (f"上一轮被判未闭合的原因（供参考）：{str(critique)[:300]}\n\n"
               if str(critique or "").strip() else "")
            + f"材料（取自本次取证，未能再取新的）：\n{mat[:MATERIAL_LIMIT]}\n\n"
            "请按系统要求输出 JSON。")
    # 引文没过就**重来一次**（同构原版 propose_next_ai_task 的 max_retries：
    # 一次没写好不等于这道题不可答）。2026-10-31 实测 dependencies_list 直接交了
    # 「没有指认任何引文」的空结论，而材料就在手上 —— 那是没照格式做，不是不能做。
    last: dict[str, Any] = {}
    for attempt in range(2):
        user = base if attempt == 0 else (base + _QUOTE_HINT)
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": user}]
        try:
            if llm is not None:
                out = llm(msgs)
            else:
                import llm_client
                out = llm_client.chat_json(msgs, max_tokens=max_tokens, retries=2)
        except Exception as e:
            return {"ok": False, "why": f"语义层调用失败：{type(e).__name__}: {e}",
                    "answer": "", "verdict": "", "quotes": [], "gap": ""}
        if not isinstance(out, dict) or not out:
            return {"ok": False, "why": "语义层没返回可解析的 JSON",
                    "answer": "", "verdict": "", "quotes": [], "gap": ""}
        verdict = str(out.get("verdict") or "").strip()
        quotes = [q for q in (out.get("quotes") or []) if isinstance(q, dict)]
        answer = str(out.get("answer") or "").strip()
        gap = str(out.get("gap") or "").strip()
        last = {"answer": answer, "verdict": verdict, "quotes": quotes,
                "gap": gap}
        if not answer:
            return {"ok": False, "why": "语义层没给出回答正文", **last}
        good, why = verify_quotes(quotes, mat)
        if not good:
            last["why"] = f"语义层的判断没有材料支撑：{why}"
            if attempt == 0:
                print(f"  · 引文没通过（{why[:60]}）→ 让它照材料重抄一遍",
                      flush=True)
                continue
            return {"ok": False, **last}
        if verdict == "无法判断" and gap:
            # 如实说材料不够 —— 这不是答到，但它是**有用的**下一步线索
            return {"ok": False, "why": f"材料不足以判断：{gap[:160]}", **last}
        return {"ok": True, "why": "", **last}
    return {"ok": False, **last}
