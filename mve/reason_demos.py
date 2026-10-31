"""语义示范库：把**验得对**的 reason_pass 轨迹存成示范（2026-10-31）。

参考的是 DeepSeek 的哪一步
------------------------
R1 论文的三段式里，能力**不靠**"把答案存起来检索重放"：

  · Cold start（B.3.2）：`we construct and collect a small amount of long CoT
    data to fine-tune the model as the initial RL actor` —— 采一批轨迹，
    按 `correct final answers and a readable format` 过滤，再改写成人类可读。
  · Rejection sampling（B.3.3）：`For each prompt, we sample multiple responses
    and retain only the correct ones` —— **只留验得对的**；同时
    `filtered out chain-of-thought with mixed languages, long paragraphs,
    and code blocks`（把"乱"的输出也滤掉）。
  · 落点是**示范**（进权重），不是答案库 —— 存的是"怎么想"，不是"想出了什么"。

MVE 没有权重可训 → 等价物只能是 **in-context 示范**：把验对的轨迹按题召回、
注入语义层的 prompt。三条口径照抄：

  ① 只收验得对的：`quotes` 全部逐字核验通过（确定性）+ critic 判过（调用方保证）；
  ② 过滤"乱"的输出：大段代码块、重复刷屏、答案本身就是材料搬运 → 不收；
  ③ 存判断过程（verdict + 引文要点 + 结论），**不存答案原文当检索命中物**
     —— 材料一变答案就过期（同 75120 的入库污染教训）。

为什么不只是"存取样路径"（2026-10-31 用户问「参考 DeepSeek」时的判断）：
    路径是工具的用法，模型本来就会（`gh_read(README.md)` 谁都想得到）；
    DeepSeek 的冷启动数据存的是**推理过程**。只存路径等于没有冷启动。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

CKPT = Path(__file__).resolve().parent / "ckpt_demos"

# 每个场景封顶 12 条：DeepSeek 的冷启动也只有"数千条"（相对百万级语料），
# 关键是**质**而不是量 —— 示范塞进 prompt 是占 token 的，太多会盖过材料。
MAX_PER_SCENE = 12
MIN_ANSWER = 40          # 太短的判断（"描述是准确的。"）没有示范价值


def _path(scene_id: str) -> Path:
    return CKPT / f"{str(scene_id or 'A').strip() or 'A'}.json"


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _load(scene_id: str) -> list[dict[str, Any]]:
    p = _path(scene_id)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []


def _norm_q(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def _looks_messy(answer: str) -> str:
    """DeepSeek 那种"乱"的过滤：代码块 / 刷屏重复 / 语言混杂。返回原因，空=干净。"""
    a = str(answer or "")
    if "```" in a:
        return "答案里有代码块（示范要的是判断，不是代码）"
    lines = [ln.strip() for ln in a.splitlines() if ln.strip()]
    if len(lines) >= 6:
        # 同一行重复 4 次以上 = 刷屏（实测模型偶尔这样自我重复）
        from collections import Counter
        if Counter(lines).most_common(1)[0][1] >= 4:
            return "答案在自我重复（同一行刷 4 次以上）"
    # 语言混杂：以中文为主却夹着超过 1/2 的连续 ASCII 行
    cjk = len(re.findall(r"[\u4e00-\u9fff]", a))
    if cjk >= 20:
        asc_long = [ln for ln in lines
                    if len(ln) > 50
                    and len(re.findall(r"[A-Za-z]", ln)) > len(ln) * 0.6]
        if len(asc_long) * 2 > max(1, len(lines)):
            return "中英文混杂（大段英文行夹在中文判断里）"
    return ""


def add(scene_id: str, question: str, result: dict[str, Any], *,
        material: str = "") -> bool:
    """收一条示范。返回是否真的入库（收不收得住要看三条过滤，见模块 docstring）。"""
    if not isinstance(result, dict) or not result.get("ok"):
        return False
    q = str(question or "").strip()
    ans = str(result.get("answer") or "").strip()
    verdict = str(result.get("verdict") or "").strip()
    if not q or len(ans) < MIN_ANSWER or verdict in ("", "无法判断"):
        return False
    why = _looks_messy(ans)
    if why:
        return False
    # 答案本身是材料搬运 → 示范里就教"抄原文"，比不教还坏
    try:
        import reason_pass as _rp
        if material:
            _dump, _ = _rp.is_material_dump(ans, str(material))
            if _dump:
                return False
    except Exception:
        pass
    quotes = [str((x or {}).get("quote") or "")[:120]
              for x in (result.get("quotes") or []) if isinstance(x, dict)]
    item = {"question": q, "verdict": verdict,
            "quotes": [x for x in quotes if x][:3],
            "answer": ans[:600],
            "gap": str(result.get("gap") or "")[:200]}
    rows = _load(scene_id)
    old = None
    for i, r in enumerate(rows):
        if _norm_q(r.get("question")) == _norm_q(q):
            old = i
            break
    if old is not None:
        rows[old] = item          # 同题覆盖：后来的（更近的）判断优先
    else:
        rows.append(item)
    if len(rows) > MAX_PER_SCENE:
        rows = rows[-MAX_PER_SCENE:]
    try:
        _atomic_write(_path(scene_id), rows)
    except Exception:
        return False
    return True


def _tokens(s: str) -> set[str]:
    t = str(s or "").lower()
    out = set(re.findall(r"[a-z0-9_]{3,}", t))
    cjk = re.findall(r"[\u4e00-\u9fff]", t)
    out |= {"".join(cjk[i:i + 2]) for i in range(max(0, len(cjk) - 1))}
    return out


def recall(scene_id: str, question: str, k: int = 2) -> list[dict[str, Any]]:
    """按题面词面重叠召回示范（不引 embedding：示范量小，词面够用且可解释）。"""
    rows = _load(scene_id)
    if not rows or k <= 0:
        return []
    qs = _tokens(question)
    if not qs:
        return rows[-k:]
    scored = []
    for i, r in enumerate(rows):
        ov = len(qs & _tokens(r.get("question")))
        scored.append((ov, i, r))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    hit = [r for ov, _, r in scored if ov > 0][:k]
    if not hit:                    # 一条都不重叠 → 不硬塞（同类的都没见过）
        return []
    return hit


def render(demos: list[dict[str, Any]] | None) -> str:
    """示范 → prompt 片段。刻意写成"学它怎么判"，防止模型把示范当答案来源。"""
    rows = [d for d in (demos or []) if isinstance(d, dict) and d.get("answer")]
    if not rows:
        return ""
    lines = ["【示范：同类问题已经验对的作答（学它怎么下判断，"
             "不要照抄它结论里的具体内容 —— 那是别人的材料）】"]
    for i, d in enumerate(rows, 1):
        lines.append(f"示范 {i}｜问：{str(d.get('question') or '')[:120]}")
        if d.get("quotes"):
            lines.append("  指认的依据：" + "；".join(f"「{x}」" for x in d["quotes"]))
        lines.append(f"  判断：{d.get('verdict')}")
        lines.append(f"  答：{str(d.get('answer') or '')[:300]}")
    return "\n".join(lines)


def count(scene_id: str) -> int:
    return len(_load(scene_id))
