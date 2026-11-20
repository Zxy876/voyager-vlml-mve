# -*- coding: utf-8 -*-
"""2026-11-18 Phase1 对齐原版 maieutic（对照 ~/Downloads/maieutic_副本 2）：

原版证据（本机源码逐行核对）：
  · spec/route.ts:14-17 —— Body 只有 specText（自然语言 spec），无任何证据字段；
  · spec-examiner.ts —— 判官口径："A dimension is 'addressed' if the student's
    spec makes a concrete commitment about it"；passed = gaps_still_open 为空，
    纯承诺判定，无执行、无取证；
  · scaffolding.ts:4-8 —— spec_gate_dimensions = "the concrete commitments the
    student's natural-language specification must address BEFORE THEY ARE
    ALLOWED TO WRITE CODE"。

MVE 两处走样，本补丁修掉：
  A. learn_orchestrate Phase1 题面挂着【证据要求】（要求真实行号、"用代码扫
     def 行号"）—— 与同轮 _p1_sys「这一轮不执行、不取数」直接矛盾。证据要求
     归 Phase2（local_agent.py:737-746 自带注入），机检闸在 ⑤' 完成判定统一跑。
  B. maieutic_scaffold SCAFFOLD_SYSTEM_SOURCE 末条：「观察面没取到 → 出取证
     维度」—— 这正是 open_quiz 卡死（source_retrieval 维连 4 轮闭合不了）的
     上游。改成承诺口径：出「打算用哪个工具从哪个路径取」的计划承诺维度。
"""
import shutil, sys, os

BASE = "/root/mve/mve"
BAK = ".bak_p1commit"


def splice(path, start_marker, end_marker, replacement):
    p = os.path.join(BASE, path)
    src = open(p, encoding="utf-8").read()
    shutil.copy2(p, p + BAK)
    i = src.find(start_marker)
    j = src.find(end_marker)
    if i < 0 or j < 0 or j <= i:
        print(f"  [FAIL] {path}: 区间定位失败 i={i} j={j}")
        sys.exit(1)
    src = src[:i] + replacement + src[j + len(end_marker):]
    open(p, "w", encoding="utf-8").write(src)
    print(f"  [ok] {path}")


# ---------- A. learn_orchestrate.py：摘掉 Phase1 的【证据要求】 ----------
REPL_A = """                            # ★ 2026-11-18（对照原版 maieutic 撤回）：Phase1 不带【证据要求】。
                            # 原版 Phase1 = textarea 承诺（spec/route.ts:14-17 只收 specText；
                            # spec-examiner.ts 判「concrete commitment」，编辑器 Phase2 才解锁），
                            # 本函数上方 _p1_sys 也明写「这一轮不执行、不取数」—— 行号只有执行
                            # 才能产生，把它要求进 Phase1 与自己的口径直接矛盾（open_quiz 卡死
                            # 的上游之一：学生被逼在承诺轮里「用代码扫 def 行号」）。
                            # verify refs 归 Phase2 finalCode：local_agent.py:737-746 对 verify_claim
                            # 场景自带同款注入；机检闸在 ⑤' 完成判定统一跑，证据从 Phase2 的
                            # RESULT 里读 —— Phase1 不需要也不会有。
"""

splice(
    "learn_orchestrate.py",
    "                            # 证据块要求（2026-11-17）：verify_claim 场景在作答",
    "没有 verify 字段的答案判过也不算完成。'\n"
    "                            except Exception:\n"
    "                                pass\n",
    REPL_A,
)

# ---------- B. maieutic_scaffold.py：取证维度 → 计划承诺维度 ----------
patch_b_old = (
    "- 观察面写着「没取到」时，只许出「去取它 / 确认它是否存在」这类**取证维度**，\n"
    "  不许出以它的内容为前提的判断维度。\"\"\""
)
patch_b_new = (
    "- 观察面写着「没取到」时（2026-11-18 对照原版 scaffolding.ts 改口径）：\n"
    "  材料还没取回 = 学生还没进执行（原版：编辑器 Phase2 才解锁），所以\n"
    "  **不许出「是否已取回 / 是否取到了」这类只有执行才能证实的事实型维度**\n"
    "  —— 它在 Phase1 永远闭合不了（实测 open_quiz 的 source_retrieval 维\n"
    "  连续 4 轮卡死，判官自己都点了「问题在维度口径本身」）。要出就出\n"
    "  **计划承诺型**维度（「你打算用哪个工具、从哪个路径把 X 取回来」——\n"
    "  学生答出工具+路径就是完整承诺，判官按承诺闭合）；同样不许出以\n"
    "  未取回内容为前提的判断维度。\"\"\""
)
p = os.path.join(BASE, "maieutic_scaffold.py")
src = open(p, encoding="utf-8").read()
if src.count(patch_b_old) != 1:
    print(f"  [FAIL] maieutic_scaffold.py: B 锚点出现 {src.count(patch_b_old)} 次")
    sys.exit(1)
shutil.copy2(p, p + BAK)
open(p, "w", encoding="utf-8").write(src.replace(patch_b_old, patch_b_new))
print("  [ok] maieutic_scaffold.py")

print("patch_p1_commit done")
