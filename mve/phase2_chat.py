"""Phase2 chat —— 原版 maieutic chat 的逐行同构物（2026-11-17）。

说明书（全部逐字对照，不许改写）：
  src/app/api/session/[sid]/chat/route.ts      —— 门 / 五段输入 / 只追加 exchange
  src/lib/opus/prompts/phase2-chat.ts          —— system 全文 + user 消息拼装
  src/lib/opus/schemas.ts:151-158              —— Phase2Exchange
  src/lib/opus/schemas.ts:183-189              —— Phase2Data
  src/lib/opus/schemas.ts:397-401              —— Phase2ChatOutput = {mode, response}

四条契约（一条都不许加）：
  C1 门：只有 **phase == 2** 才允许（spec 已冻结、finalCode 未提交）。route.ts:37-42
     原版错相位返回 409 wrong_phase；这里返回 {"refused": "wrong_phase"}，不抛异常。
  C2 输入五段（route.ts:57-63 → phase2-chat.ts:82-105）：
     题面 / 学生水平 / 冻结的 spec / 当前 code / 最近 6 条 exchange / 新消息。
  C3 输出 {mode: interrogative|direct, response}，**mode 由 chat 判**。
     ★ 红线：调用方不许自己声明 mode。一旦让发问者自报 mode，r_site 就不可信了。
  C4 状态影响：**只 append exchange**。不推进 phase、不改 code、不写技能库。
     route.ts:85 只有 appendPhase2Exchange 一行 —— 这是它「可以无限调用而状态机不动」
     的全部原因，也是它在本系统里唯一合法的用途：一个把不确定性产出的黑盒。

与 MVE 主循环的关系（r_site 的数据来源）：
  `chat_streak()` 只读 exchanges 末尾的 `opusMode` 游程，不改任何状态。
  它是 curriculum 侧（大循环）的**外置读数**，不是 chat 的一部分 ——
  所以给 chat 加读数是允许的，给 chat 加写账是违约的。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

# ─────────────────────────── 提示词：逐字照抄 ───────────────────────────
# phase2-chat.ts:6-62 原文。改一个词就不再是同构物（要改就改 MVE 的调用方）。
PHASE2_CHAT_SYSTEM = """You are the coding assistant for a CS1 student in a pedagogical IDE called
Maieutic. The student has frozen their specification. They are now writing
code, and you are available in a chat panel.

You have TWO modes. For each message, pick one and answer in that mode.

INTERROGATIVE mode — triggered when the student asks about THEIR OWN CODE OR
REASONING about the current problem. Examples:
  - "Why does my loop terminate early?"
  - "Is my approach correct?"
  - "Can you check this function?"
  - "Why isn't my output right?"

In interrogative mode:
  - Do NOT rewrite or show code.
  - Do NOT give the answer.
  - Ask one or two counter-questions or point to a diagnostic technique.
  - Keep the student in the reasoning seat.

DIRECT mode — triggered when the student asks a FACTUAL LANGUAGE/LIBRARY
reference question, independent of their reasoning about their own code.
Examples:
  - "What is the syntax of a dictionary in Python?"
  - "How do I read a file line by line?"
  - "What does the sorted() function return?"

In direct mode:
  - Answer directly and concisely.
  - Show a minimal code example if it clarifies the syntax (NOT tied to the
    student's current problem).
  - Refusing reference questions drives students to an external LLM and
    defeats the tool. Answer them.

EDGE CASES:
  - "What is the syntax for a list comprehension that filters evens?" → DIRECT
    (generic language reference)
  - "Write me a list comprehension for filtering evens in my problem" →
    INTERROGATIVE (thinly disguised implementation request)
  - "How do I count occurrences in a string?" → usually DIRECT if the
    student is asking about the tool (e.g., str.count), INTERROGATIVE if
    they are asking about their current counting problem.

When ambiguous, lean INTERROGATIVE only when answering directly would
substitute for the student's own reasoning about their current problem.

You will be shown: the exercise prompt, the student's spec, their CURRENT
CODE, and recent chat history.

OUTPUT FORMAT — a single JSON object, no preamble, no markdown fences:

{
  "mode": "interrogative" | "direct",
  "response": "<text shown to student>"
}

Keep responses short. DIRECT answers: at most a short paragraph + small
example. INTERROGATIVE answers: one to three questions/pointers maximum."""

# phase2-chat.ts:75 —— 只带最近 6 条
HISTORY_WINDOW = 6
# route.ts:66 —— maxTokens: 1024
MAX_TOKENS = 1024
# 原版 OpusMode = z.enum(["interrogative","direct"])；别的值一律判失败
MODES = ("interrogative", "direct")


def build_user_message(*, prompt_text: str, student_level: str = "CS1",
                       spec_text: str = "", current_code: str = "",
                       recent_exchanges: list[dict[str, Any]] | None = None,
                       student_message: str = "") -> str:
    """phase2-chat.ts:64-105 逐行同构。段序、措辞、history 格式全部照原文。"""
    exs = list(recent_exchanges or [])[-HISTORY_WINDOW:]
    if not exs:
        history_block = "  (no prior messages in this session)"
    else:
        history_block = "\n\n".join(
            "  student: %s\n  you (%s): %s"
            % (json.dumps(str(e.get("studentMessage") or ""), ensure_ascii=False),
               str(e.get("opusMode") or "?"),
               json.dumps(str(e.get("opusResponse") or ""), ensure_ascii=False))
            for e in exs)
    return (
        "EXERCISE PROMPT:\n" + str(prompt_text or "") + "\n\n"
        "STUDENT LEVEL: " + str(student_level or "CS1") + "\n\n"
        "STUDENT'S SPEC (frozen):\n\"\"\"\n" + str(spec_text or "") + "\n\"\"\"\n\n"
        "STUDENT'S CURRENT CODE:\n\"\"\"\n" + (str(current_code) or "(empty)")
        + "\n\"\"\"\n\n"
        "RECENT CHAT HISTORY:\n" + history_block + "\n\n"
        "NEW STUDENT MESSAGE:\n\"\"\"\n" + str(student_message or "") + "\n\"\"\"\n\n"
        "Pick a mode. Answer. Output JSON per schema."
    )


# ─────────────────────────── 账本：Phase2Data 同构 ───────────────────────────
# 原版 Phase2Data 有 5 个字段，chat 只碰 opusExchanges 一个（route.ts:85）。
# 为不免发明，这里只存 chat 会读会写的那一列，其余字段由调用方自己管。


def _chat_path(ckpt_dir: str | Path, topic: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_"
                   for ch in str(topic or "adhoc"))[:80]
    return Path(ckpt_dir) / "chat" / ("%s.json" % safe)


def load_exchanges(ckpt_dir: str | Path, topic: str) -> list[dict[str, Any]]:
    p = _chat_path(ckpt_dir, topic)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    exs = d.get("opusExchanges") if isinstance(d, dict) else d
    return [e for e in (exs or []) if isinstance(e, dict)]


def append_exchange(ckpt_dir: str | Path, topic: str,
                    exchange: dict[str, Any],
                    *, spec_text: str = "", code: str = "") -> None:
    """route.ts:79-85 + sessions.ts:318-332：只 push 一条到 opusExchanges。
    另一个字段（spec/code）是**快照**，方便事后核对「他当时问的是哪一版」，
    不改变 chat 的语义。"""
    p = _chat_path(ckpt_dir, topic)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            d = {"opusExchanges": d if isinstance(d, list) else []}
    except Exception:
        d = {"opusExchanges": []}
    d.setdefault("opusExchanges", []).append(exchange)
    d["topic"] = str(topic or "")
    d["specSnapshot"] = str(spec_text or "")[:4000]
    d["codeSnapshot"] = str(code or "")[:4000]
    d["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


# ─────────────────────────── 唯一入口 ───────────────────────────


def chat(*, prompt_text: str, spec_text: str, current_code: str = "",
         student_message: str, recent_exchanges: list[dict[str, Any]] | None = None,
         student_level: str = "CS1",
         phase: int = 2, llm: Callable | None = None) -> dict[str, Any]:
    """原版 chat 的一问一答。

    phase 必须是 2 —— 调用方给的是**它自己的相位**，不是 chat 改的。
    返回 {"mode","response","exchange"} / {"refused": "..."}。
    """
    if int(phase or 0) != 2:
        # route.ts:37-42 的 409 wrong_phase
        return {"refused": "wrong_phase", "currentPhase": int(phase or 0)}
    msg = str(student_message or "").strip()
    if not msg:
        return {"refused": "empty_message"}

    user = build_user_message(prompt_text=prompt_text, student_level=student_level,
                              spec_text=spec_text, current_code=current_code,
                              recent_exchanges=recent_exchanges,
                              student_message=msg)
    if llm is None:
        return {"refused": "no_llm"}
    try:
        text = llm([{"role": "system", "content": PHASE2_CHAT_SYSTEM},
                    {"role": "user", "content": user}])
    except Exception as e:
        return {"refused": "llm_failed", "why": "%s: %s" % (type(e).__name__, e)}

    data: Any = None
    try:
        import maieutic_gate as _mg
        data = _mg._parse(text)
    except Exception:
        data = None
    if not isinstance(data, dict):
        try:
            s = str(text or "")
            i, j = s.find("{"), s.rfind("}")
            data = json.loads(s[i:j + 1]) if i >= 0 and j > i else None
        except Exception:
            data = None
    if not isinstance(data, dict):
        return {"refused": "parse_failed", "raw": str(text or "")[:400]}
    mode = str(data.get("mode") or "").strip().lower()
    resp = str(data.get("response") or "").strip()
    # schema 是真约束：mode 不在枚举里就当这轮不成立（不猜、不默认）
    if mode not in MODES:
        return {"refused": "bad_mode", "raw": str(text or "")[:400]}
    ex = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
          "studentMessage": msg, "opusMode": mode, "opusResponse": resp}
    return {"mode": mode, "response": resp, "exchange": ex}


# ─────────────────── r_site：游程读数（只读，不改 chat） ───────────────────


def chat_streak(exchanges: list[dict[str, Any]], *,
                locate: Callable[[str], str] | None = None,
                site: str = "", k: int = 0) -> int:
    """末尾「连续 interrogative 且同一落点」的条数 = r_site。

    依据 phase2-chat.ts:12-18：interrogative 的定义就是「问自己的代码或推理」，
    direct 是「与当前问题无关的语言/库事实」。所以：
      · 连续 interrogative ＝ 同一处推理被反复审视 ＝ 在挖同一个点（r_site 递增）
      · 出现 direct      ＝ 离开当前问题去查外部语法 ＝ 换落点（r_site 归零）
    落点由调用方给的 `locate(问句) -> site` 决定；不给就只按 mode 数游程。

    k>0 时返回**截断值**（min(streak,k)），方便直接和阈值比较。
    """
    n = 0
    for e in reversed(list(exchanges or [])):
        if str(e.get("opusMode") or "") != "interrogative":
            break
        if site and locate is not None:
            if str(locate(str(e.get("studentMessage") or "")) or "") != site:
                break
        n += 1
    return min(n, k) if k > 0 else n


def passable(exchanges: list[dict[str, Any]], *, k: int = 3,
             locate: Callable[[str], str] | None = None,
             site: str = "") -> bool:
    """可推进性：r_site >= k。

    k 必须是**常量**（默认 3）。交给模型决定就退化成「感觉连续」，
    不可判定 —— 与 Voyager `curriculum.py:264-291` 的世界规则同理：
    阈值写在结构里，不写在判断里。
    """
    return chat_streak(exchanges, locate=locate, site=site) >= int(k)


# ───────────── 发问器：agent 在 phase2 里就是原版那个「学生」 ─────────────
# 2026-11-17 用户定分工：**人**在人导入口拆骨架（= 原版 authoring/scaffolding），
# **agent** 在 phase2 写码并向 chat 发问（= 原版学生的位置）。
# 于是发问器不需要新设计「该问什么」—— 它问的就是学生此刻真实的困惑，
# 而困惑只有两种来源，都在这一轮的执行记忆里：
#   ① 跑出错了          → 「问题出在哪」
#   ② 跑通了但对不上 spec → 「我的推理哪里有问题」
# ★ 这里**不判断 mode**。mode 永远由 chat 判（§12.3 红线）；
#   发问器只负责把执行记忆里的缺口原样说出来。


# 缺口的两种形态（原版学生的两种真实困惑）。这不是给 mode 分类 ——
# 它只决定**问句的措辞**，mode 仍由 chat 判（§12.3）。
#   external：外部世界的名字/签名不存在 → "这东西怎么用、返回什么"
#   internal：知道用什么，但自己的类型假设/推理不对 → "我哪想错了"
# 界线依据 phase2-chat.ts:12-30。注意 AttributeError 要分两种（见下）：
#   `module 'x' has no attribute 'y'`    → 接口不存在        → external
#   `'dict' object has no attribute 'split'` → 我以为它返回 str → internal
def build_student_message(*, err: str = "", critique: str = "",
                          code: str = "", spec: str = "") -> str:
    """学生这一轮的一句真话：把观测到的原始事实照抄一遍，再问一句。

    刻意**不按报错类型改写措辞**。原版 phase2-chat.ts 的 mode 判据是
    「问的是自己的代码/推理，还是与本题无关的语言事实」（:12-30），
    且 :39-46 的 EDGE CASES 明确指出：把实现请求换个说法（
    "Write me a list comprehension for filtering evens in my problem"）
    仍然是 INTERROGATIVE —— **换措辞买不到另一个 mode**。

    发问侧一旦按 ImportError / TypeError 分支出两种句式，就等于把分类器
    搬到了发问侧（违反 §12.3「mode 必须由 chat 判」），r_site 立刻变成
    那张表的读数、而不是世界的读数。故此处只有一种句式：抄事实 + 问一句。
    """
    e = str(err or "").strip()
    c = str(critique or "").strip()
    fact = ""
    if e:
        fact = e.splitlines()[0].strip()[:200]
    elif c:
        fact = c.splitlines()[0].strip()[:240].rstrip("。．.;；,， ")
    if fact:
        # 「Why isn't my output right?」的同构（phase2-chat.ts:17）
        return "我按 spec 写完了，真跑的结果是：%s。这和我以为的不一样，问题出在哪？" % fact
    if str(code or "").strip():
        # 「Is my approach correct?」的同构（phase2-chat.ts:15）
        return "我这段代码跑通了，但它真的算证明了我声称的东西吗？"
    return "这里我还没有思路，该从哪下手？"


# ───────── last_topic 指针：出题侧才知道「上一道题是哪道」 ─────────
# 原版这个指针就是 session 本身（Phase2Data 挂在 session 下）。MVE 自由学里
# 没有 session 对象，所以显式存一个 —— 它是**指针**，不是新账本。


def record_last_topic(ckpt_dir: str | Path, topic: str, *,
                      r_site: int = 0, question: str = "") -> None:
    p = Path(ckpt_dir) / "last_topic.json"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"topic": str(topic or ""),
                                 "r_site": int(r_site or 0),
                                 "question": str(question or "")[:200],
                                 "at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def read_last_topic(ckpt_dir: str | Path) -> dict[str, Any]:
    try:
        d = json.loads((Path(ckpt_dir) / "last_topic.json").read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def state_dir_for(scene_id: str) -> Path:
    """与 curriculum_auto.STATE_DIR 同源，绝不另建一套账本目录。"""
    try:
        import curriculum_auto as _ca
        return Path(_ca.STATE_DIR) / str(scene_id)
    except Exception:
        return Path(__file__).resolve().parent / "ckpt_curriculum" / str(scene_id)


def enabled() -> bool:
    """总开关。关掉 = 回到 2026-11-17 之前的纯 rollout（一行不跑）。"""
    return str(os.environ.get("MVE_P2_CHAT", "1")).strip().lower() \
        not in ("0", "off", "false", "no")

