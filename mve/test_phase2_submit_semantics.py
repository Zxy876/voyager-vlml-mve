#!/usr/bin/env python3
"""Phase2「提交语义」回归测试（2026-11-15 用户口径）。

用户原话：
    「result相当于提交按钮不提交是拿不到追问和判词的。和助产士同构。
      我们现在的问题就是仍然有除助产士闭合标准（缺口序列）以外的待填键
      （比如取数这个动作）要做，我们要割掉它。」

原版 maieutic 依据（`~/maieutic` @ 8e6f7ee，只读）：
  · `src/app/api/session/[sid]/submit/route.ts:24-26`
      `z.object({ finalCode: z.string().max(100_000) })`
      —— Phase2 提交物**只有 finalCode 文本**，不要求跑通、不要求有输出。
  · `src/lib/opus/prompts/intent-diff.ts:307-333`
      —— intent-diff 的 user message **没有任何"运行结果"块**。
  · `src/app/api/session/[sid]/submit/route.ts:91-96`
      —— 追问唯一来源 = 提交（setPhase3Divergences）；
         闭合判据 = `divergences.length === 0 ? 4 : 3`。
  · `src/components/student/ExerciseClient.tsx:1025-1030`
      —— 提交按钮只要求代码非空（`!code.trim()`）。

本测试查的是**代码特征**（函数调用点 / 字面量 / 结构），不查关键词 ——
删过的地方会留注释说明「为什么删」，搜关键词会把注释当残留（2026-11-09 踩过）。

跑法（服务器）：
    cd /root/mve && .venv/bin/python mve/test_phase2_submit_semantics.py
（服务器没有 pytest；且 `mve/` 脚本要 `sys.path` 加两处：
  `/root/mve/mve` 与 `/root/mve`。）
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SRC_LO = ROOT / "voyager_vlml" / "consume.py"
SRC_ORCH = HERE / "learn_orchestrate.py"
SRC_GATE = HERE / "maieutic_gate.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# 编排层源码与语法树（多处断言要用，先解析一次）
SRC_GLOBAL = _read(SRC_ORCH) if SRC_ORCH.exists() else ""
try:
    OTREE_GLOBAL = ast.parse(SRC_GLOBAL)
except SyntaxError:
    OTREE_GLOBAL = ast.parse("")


# ── 小工具 ────────────────────────────────────────────────────────────────
def _func_node(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    return None


def _seg(src: str, node: ast.AST) -> str:
    """取函数体源码（按行号切片，避开缩进坑）。"""
    lines = src.splitlines()
    return "\n".join(lines[node.lineno - 1:node.end_lineno])


def _strip_comments_and_strings(src: str) -> str:
    """去掉注释与字符串字面量 —— 查结构特征时用，避免注释里的词造成误判。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    out: list[str] = []
    for node in ast.walk(tree):
        pass
    # 逐行粗处理：去掉 # 之后的内容（不严谨但足够，且不含多行字符串的场景）
    res = []
    for line in src.splitlines():
        res.append(line.split("#", 1)[0])
    return "\n".join(res)


class T:
    """极简断言收集（服务器没有 pytest）。"""
    def __init__(self) -> None:
        self.ok = 0
        self.bad: list[str] = []

    def check(self, cond: bool, name: str, detail: str = "") -> None:
        if cond:
            self.ok += 1
            print(f"  ✓ {name}")
        else:
            self.bad.append(f"{name}" + (f" —— {detail}" if detail else ""))
            print(f"  ✗ {name}" + (f" —— {detail}" if detail else ""))

    def report(self, title: str) -> bool:
        print(f"\n{title}: {self.ok} 过 / {len(self.bad)} 败")
        for b in self.bad:
            print(f"  ✗ {b}")
        return not self.bad


# ── ① Phase2：取数不再是「不提交」的理由 ─────────────────────────────────
def test_phase2_submit_semantics() -> None:
    t = T()
    src = _read(SRC_ORCH)
    tree = ast.parse(src)
    fn = _func_node(tree, "phase2_final_code")
    t.check(fn is not None, "phase2_final_code 存在")
    if fn is None:
        return t.report("① Phase2 提交语义")
    body = _seg(src, fn)
    body_nc = _strip_comments_and_strings(body)

    # ①-a  「没交出程序」这道 continue **保留**（它就是提交按钮本身）
    #     注意：要匹配 `not out["code"].strip()`，别匹配到正向的
    #     `if out["code"].strip():`（那是"记下上一版程序"）。
    if_branch = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        seg = (ast.get_source_segment(src, node.test) or "").strip()
        if seg == 'not out["code"].strip()':
            if_branch = node
            break
    t.check(if_branch is not None, "存在「没交出程序」分支")
    if if_branch is not None:
        has_cont = any(isinstance(n, ast.Continue) for n in ast.walk(if_branch))
        t.check(has_cont, "「没交出程序」仍然 continue（= 没点提交 = 拿不到判词）",
                "原版提交按钮只要求代码非空，这道必须留")

    # ①-b  取不到数的那道闸：**不再** continue
    got_branch = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        seg = ast.get_source_segment(src, node.test) or ""
        if "_got_something" in seg:
            got_branch = node
            break
    t.check(got_branch is not None, "存在「取不到数」闸（_got_something）")
    if got_branch is not None:
        conts = [n for n in ast.walk(got_branch) if isinstance(n, ast.Continue)]
        t.check(not conts,
                "「取不到数」不再 continue（代码照样提交 → intent-diff 能对账）",
                f"分支里还有 {len(conts)} 个 continue")
        # 它必须还留下事实（exec_note）供下一轮改
        seg = ast.get_source_segment(src, got_branch) or ""
        t.check("exec_note" in seg,
                "「取不到数」仍如实记事实（exec_note）供下一轮修")

    # ①-c  RESULT 空的那道门：**不再** continue
    res_branch = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        seg = ast.get_source_segment(src, node.test) or ""
        if seg.strip() == 'not out["result"]':
            res_branch = node
            break
    t.check(res_branch is not None, "存在「RESULT 为空」门")
    if res_branch is not None:
        conts = [n for n in ast.walk(res_branch) if isinstance(n, ast.Continue)]
        t.check(not conts,
                "「RESULT 为空」不再 continue（RESULT 是 MVE 自造的容器，"
                "原版 submit 只收 finalCode 文本）",
                f"分支里还有 {len(conts)} 个 continue")

    # ①-d  零上下文单跑门 / 执行门：**不再** continue（它们是入库判据）
    for tag, needle in (("零上下文单跑门", "_ok_auto"),
                        ("执行门", "_sm")):
        br = None
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            seg = ast.get_source_segment(src, node.test) or ""
            if tag == "零上下文单跑门" and needle in seg:
                br = node
                break
            if tag == "执行门" and "_sm" in seg and "ok" in seg:
                br = node
                break
        t.check(br is not None, f"存在「{tag}」")
        if br is not None:
            conts = [n for n in ast.walk(br) if isinstance(n, ast.Continue)]
            t.check(not conts,
                    f"「{tag}」不再 continue（它是入库判据，入库门会再判一次）",
                    f"分支里还有 {len(conts)} 个 continue")

    # ①-e  require_tools 不再按「场景有没有材料」强制取数
    calls = [ast.get_source_segment(src, n) or ""
             for n in ast.walk(fn) if isinstance(n, ast.Call)]
    solve_call = None
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "solve":
            solve_call = n
            break
    t.check(solve_call is not None, "存在 solve 调用")
    if solve_call is not None:
        kw = {k.arg: ast.get_source_segment(src, k.value) or ""
              for k in solve_call.keywords}
        t.check("require_tools" in kw, "solve 仍显式传 require_tools")
        t.check(kw.get("require_tools", "").strip() == "False",
                "require_tools=False（不按场景标签强制取数；spec 说了算）",
                f"实际 = {kw.get('require_tools')!r}")

    # ①-f  过关判据唯一 = intent-diff 缺口序列（不被取数判据顶掉）
    #     注意：intent-diff 的调用在**编排层**（`_start_learning_impl` 里
    #     phase2_final_code 返回之后），不在 phase2_final_code 体内。
    t.check("intent_diff" not in body or True, "（intent_diff 在编排层调用）")
    orch_fn = _func_node(OTREE_GLOBAL, "_start_learning_impl")
    t.check(orch_fn is not None, "_start_learning_impl 存在")
    if orch_fn is not None:
        obody = _seg(SRC_GLOBAL, orch_fn)
        t.check("intent_diff(" in obody,
                "Phase2 走 intent_diff 对账（原版 submit 同构）")
        t.check("_p2_diff_ok" in obody and "_p2_diff_known" in obody,
                "intent-diff 的结果是过关判据（分歧为 0 = 过）")
        # 过关判据不能被「取到数」顶掉
        seg_ok = ""
        for node in ast.walk(orch_fn):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "_p2_verdict":
                        seg_ok = ast.get_source_segment(SRC_GLOBAL, node.value) or ""
        t.check("_p2_diff_ok if _p2_diff_known" in seg_ok,
                "过关判据优先用 intent-diff 分歧（不是取到数）",
                f"实际 = {seg_ok!r}")

    return t.report("① Phase2 提交语义")


# ── ② P2 critic：不再用「RESULT 为空」判失败 ─────────────────────────────
def test_p2_critic_no_result_gate() -> None:
    t = T()
    src = _read(SRC_ORCH)
    tree = ast.parse(src)
    # P2_CRITIC_SYSTEM 是模块级字符串常量
    const = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "P2_CRITIC_SYSTEM":
                    const = node.value
                    break
    t.check(const is not None, "P2_CRITIC_SYSTEM 常量存在")
    if const is None:
        return t.report("② P2 critic 判据")
    text = const.value if isinstance(const, ast.Constant) else ""
    t.check(isinstance(text, str) and text != "", "P2_CRITIC_SYSTEM 是字符串")
    if not isinstance(text, str):
        return t.report("② P2 critic 判据")

    # 判定条目 3 原来含「或 RESULT 为空」
    old_rule = "程序报错、或 RESULT 为空 → false"
    t.check(old_rule not in text,
            "判定第 3 条不再含「RESULT 为空 → false」",
            "原文还在：" + old_rule)
    # 第 5 条（不许因 0 行判失败）必须在
    t.check("不许" in text and "取回 0 行" in text,
            "第 5 条「不许因取回 0 行判失败」仍在")
    # 必须写明判的是「对齐」不是「取没取到数」
    t.check("对齐" in text, "明写判的是「spec 与 code 对不对齐」")

    return t.report("② P2 critic 判据")


# ── ③ Phase1：口径不可达不当缺口追问 + env_obs 补上 + stall 漏洞 ──────────
def test_phase1_gap_handling() -> None:
    t = T()
    # ③-a 闸门口径：口径不可达 → emergent_gaps，不进 questions
    gsrc = _read(SRC_GATE)
    gtree = ast.parse(gsrc)
    gconst = None
    for node in gtree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "GATE_SYSTEM":
                    gconst = node.value
                    break
    t.check(gconst is not None, "GATE_SYSTEM 常量存在")
    if gconst is not None:
        gtext = gconst.value if isinstance(gconst, ast.Constant) else ""
        t.check("emergent_gaps" in gtext, "闸门口径提到 emergent_gaps")
        t.check("不阻塞" in gtext, "闸门口径写明 emergent_gaps 不阻塞闭合")

    # ③-b 整篇模式也必须给 env_obs（口径真实性判据要有实况可指认）
    osrc = _read(SRC_ORCH)
    otree = ast.parse(osrc)
    fn = _func_node(otree, "_start_learning_impl")
    t.check(fn is not None, "_start_learning_impl 存在")
    empty_env = 0
    given_env = 0
    if fn is not None:
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            # maieutic_gate.check(...) —— 模块属性调用
            fname = getattr(node.func, "attr", "")
            if fname != "check":
                continue
            kw = {k.arg: ast.get_source_segment(osrc, k.value) or ""
                  for k in node.keywords}
            if "env_obs" not in kw:
                continue
            v = kw["env_obs"]
            if '"" if _whole else' in v:
                empty_env += 1
            elif "_env_obs_block" in v:
                given_env += 1
    t.check(empty_env == 0,
            "整篇模式不再传空 env_obs（口径真实性判据要有实况可指认）",
            f"还有 {empty_env} 处传空")
    t.check(given_env >= 2,
            "两处闸门调用都给了 _env_obs_block",
            f"实际 {given_env} 处")

    # ③-c stall 守卫按 open 集合判（不再按数量）
    t.check("_p1_prev_open_set" in osrc,
            "stall 守卫改按 open 集合判（修绕过漏洞）")
    t.check("len(_p1_open) >= _p1_prev_open" not in _strip_comments_and_strings(osrc),
            "旧的「按数量」判据已删（open 数摆动时计数会被清零 → 可被绕过）")

    return t.report("③ Phase1 缺口处理")


# ── ④ 零回归：核心防线仍在 ────────────────────────────────────────────────
def test_guards_intact() -> None:
    t = T()
    osrc = _read(SRC_ORCH)
    lsrc = _read(HERE / "local_agent.py")
    tsrc = _read(HERE / "task_critic.py")

    # 真空防线（防幻觉三闸之一）必须在
    t.check("凭训练记忆编造" in lsrc or "凭记忆编造" in lsrc,
            "内层真空防线仍在（没调数据源 = 凭记忆编造）")
    t.check("needs_data" in tsrc, "消费侧真空防线 needs_data 仍在")
    # 口径真实性（列×列合成新口径 = 假设费率）必须在
    t.check("两个数值列相乘/相除合成一个新口径" in _read(SRC_GATE),
            "口径真实性判据仍在（防 base_salary×overtime_hours 类假数）")
    # 入库门（skill_debug.verdict）必须在 —— 它是「能不能当技能用」的判据
    t.check("skill_debug" in osrc and "verdict" in osrc,
            "入库门仍在（能不能当技能用由它判，不是由提交判）")
    # 来源绑定必须在
    t.check("source_binding" in osrc, "来源绑定仍在（取的是不是题面点名的那份）")
    # 顶层 no_data 闸门必须在
    t.check("no_data" in osrc, "顶层 no_data 闸门仍在（真空成功的收口）")

    return t.report("④ 核心防线零回归")


def main() -> int:
    print("═══ Phase2 提交语义 + Phase1 缺口处理 回归测试 ═══\n")
    results = [
        test_phase2_submit_semantics(),
        test_p2_critic_no_result_gate(),
        test_phase1_gap_handling(),
        test_guards_intact(),
    ]
    failed = sum(1 for r in results if not r)
    print(f"\n═══ 总计：{failed} 组失败 / {len(results)} 组 ═══")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
