#!/usr/bin/env python3
"""语义收口（reason_pass）的测试：材料在手 → 关工具 → 只在材料上作答。

跑法：python mve/test_reason_pass.py（服务器无 pytest，同 test_*.py 口径）。

对应的生产事实（2026-10-31 Scene H）：
    13 条维度里 11 条是「README 里关于 X 的描述是否准确」——判断，不是取数。
    执行层每次都 gh_read(README) 然后 RESULT = 原文，判官连拒 5 条。
    这一步要做的不是"再取一次"，而是**让模型在已取到的材料上想**。

DeepSeek 的依据（公开材料，已核实）：
  · R1 §2.2/§2.3：只用规则化 reward（accuracy+format），不用神经奖励模型；
    只约束格式、不加内容偏好 —— 练的是「想」，不是「查」。
  · V3.2 "Thinking in Tool-Use"：在工具返回之上继续推理；
    V3.2-Speciale 只保留思考、不支持工具调用 —— 纯推理与推理+工具是两档。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0

MAT = ("【材料·gh_read(Zxy876/voyager-vlml-mve README.md)】\n"
       "# Voyager-VLML · MVE\n"
       "本仓库是一个 LLM Agent 的在线强化学习系统，技能库即参数更新。\n"
       "最后更新：2026-10-30。面板：https://43.161.203.50:8443/mve/\n")


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    import reason_pass as rp

    print("== 1. 材料账 → 可读正文（只取 ok 的，带来源）==")
    ev = [
        {"tool": "gh_read", "ok": True, "args": "Zxy876/voyager-vlml-mve README.md",
         "text": "# Voyager-VLML · MVE\n技能库即参数更新。"},
        {"tool": "gh_api", "ok": False, "args": "releases", "error": "403"},
    ]
    mat = rp.material_text(ev)
    check("取到正文进材料", "技能库即参数更新" in mat)
    check("来源写在材料头（便于指认）", "gh_read(Zxy876/voyager-vlml-mve README.md)" in mat)
    check("失败项不进材料", "403" not in mat)
    check("空材料 → 空串（不硬凑）", rp.material_text([]) == "")

    print("== 2. 引文核验：可规则验证，不听模型自称 ==")
    ok, why = rp.verify_quotes(
        [{"quote": "最后更新：2026-10-30", "why": "README 里写的时间"}], MAT)
    check("逐字引文 → 通过", ok is True and why == "", why)
    ok, why = rp.verify_quotes(
        [{"quote": "最后更新：2026-12-31", "why": "编的"}], MAT)
    check("编造引文 → 不通过", ok is False, why)
    check("编造判词说清「对不上」", "对不上" in why, why)
    check("一条都没指认 → 不通过", rp.verify_quotes([], MAT)[0] is False)
    _long = MAT.split("】\n", 1)[1][:70]          # 材料里的真实长句
    ok, _ = rp.verify_quotes(
        [{"quote": _long, "why": "长引文截断匹配"}], MAT)
    check("长引文按前 60 字截断匹配（容忍省略号/换行）", ok is True)

    print("== 3. 作答流程：没有材料就不调用模型 ==")
    called = {"n": 0}

    def _llm(_msgs):
        called["n"] += 1
        return {"verdict": "是", "quotes": [], "answer": "", "gap": ""}

    r = rp.answer_over_material("README 是否属实？", "", llm=_llm)
    check("没材料 → ok=False 且不叫模型", r["ok"] is False and called["n"] == 0,
          r["why"])
    check("没材料时说清原因", "没有材料" in r["why"], r["why"])

    print("== 4. 正常作答：判断 + 引文 + 完整回答 ==")

    def _llm_ok(_msgs):
        return {"verdict": "部分",
                "quotes": [{"quote": "最后更新：2026-10-30",
                            "why": "README 确实写了更新时间"}],
                "answer": "部分属实：更新时间与面板链接都能在 README 里指认；"
                          "但「技能库即参数更新」这句无法从仓库代码验证，需补证据。",
                "gap": ""}

    r = rp.answer_over_material("README 里写的属实吗？", MAT, llm=_llm_ok)
    check("判断 + 引文齐 → ok", r["ok"] is True, r["why"])
    check("回答不是材料原文（是判断）", "部分属实" in r["answer"]
          and "# Voyager-VLML · MVE" not in r["answer"])

    print("== 5. 编造引文 / 材料不足 都要如实判不通过 ==")

    def _llm_fake(_msgs):
        return {"verdict": "是", "quotes": [{"quote": "README 明确写了作者是张三"}],
                "answer": "属实。", "gap": ""}

    r = rp.answer_over_material("README 里写的属实吗？", MAT, llm=_llm_fake)
    check("引文对不上 → ok=False（不许拿自由发挥当判据）", r["ok"] is False, r["why"])
    check("并指出是没有材料支撑", "没有材料支撑" in r["why"], r["why"])

    def _llm_gap(_msgs):
        return {"verdict": "无法判断", "quotes": [{"quote": "最后更新：2026-10-30"}],
                "answer": "材料里没有代码内容，无法核对实现。",
                "gap": "缺仓库源码（gh_read src/…）"}

    r = rp.answer_over_material("README 里写的属实吗？", MAT, llm=_llm_gap)
    check("材料不足 → ok=False，但带上缺什么", r["ok"] is False
          and "缺仓库源码" in r["why"], r["why"])

    print("== 6. 模型/解析失败不崩 ==")
    r = rp.answer_over_material("q", MAT, llm=lambda m: "不是 JSON")
    check("返回非 JSON → ok=False 如实说", r["ok"] is False and "JSON" in r["why"],
          r["why"])

    def _boom(_m):
        raise RuntimeError("429")

    r = rp.answer_over_material("q", MAT, llm=_boom)
    check("调用异常 → ok=False 带原因", r["ok"] is False and "429" in r["why"], r["why"])

    print("== 7. 搬运材料 ≠ 回答（确定性判据，判官会抖判据不能抖）==")
    # 2026-10-31 实测：gh_read→RESULT=整篇 README 被判官连拒 5 次、又放过 2 次
    # （vlml_details r=1.0，答案是 15145 字节原文）。这条判据必须与判官无关。
    big = "".join(f"第{i}段：本项目是一个在线强化学习系统，技能库即参数更新。"
                  for i in range(40))
    mat_big = f"【材料·gh_read(README.md)】\n{big}"
    check("答案=材料全文 → 判搬运", rp.is_material_dump(big, mat_big)[0] is True)
    check("JSON 包裹材料原文 → 仍判搬运",
          rp.is_material_dump('{"answer": {"text": "' + big + '"}}',
                              mat_big)[0] is True)
    check("答案被截断（只搬了前 1/3）→ 仍判搬运（按答案覆盖率判）",
          rp.is_material_dump(big[: len(big) // 3], mat_big)[0] is True)
    check("正常判断（一句话）→ 不判搬运",
          rp.is_material_dump("部分属实：更新时间可指认，实现无法核对。",
                              mat_big)[0] is False)
    check("材料太短不触发（避免误伤短文）",
          rp.is_material_dump("x" * 400, "短材料。")[0] is False)
    # 真实踩坑（2026-10-31 10:5x）：运行时 answer 是 json.dumps 过的字符串，
    # 换行变成**两个字符** `\n`；材料是真换行 → 跨行片段永远匹配不上，
    # 判据形同失效，README 全文照样通过。这条钉死它。
    import json as _json
    big_nl = "\n".join(f"第{i}段：本项目是一个在线强化学习系统，技能库即参数更新。"
                       for i in range(40))
    mat_nl = f"【材料·gh_read(README.md)】\n{big_nl}"
    check("答案里换行被转义成 \\n 两字符 → 仍判搬运",
          rp.is_material_dump(_json.dumps(big_nl, ensure_ascii=False),
                              mat_nl)[0] is True)

    print("== 8. 接进学习主循环：只在来源已绑定时做，且过同一把尺子 ==")
    from pathlib import Path
    lo = (Path(__file__).resolve().parent / "learn_orchestrate.py").read_text(
        encoding="utf-8")
    check("失败且有材料才启动语义收口", "if not (ok1 and _inner_ok) and _bind_ok:" in lo)
    check("语义收口要过同一把尺子（不另设判据）",
          "_tc3.check_task_success(" in lo and "ok2, crit2" in lo)
    check("材料取错对象的轮次不启动（避免错上加错）", "and _bind_ok:" in lo)
    check("dim_codes 记事实：这条是取证闭合还是语义闭合", '"how": "reason"' in lo)
    check("搬运判据接进主循环（判官放过也拦得住）",
          "_rp.is_material_dump(" in lo and "答案是材料搬运" in lo)
    check("执行账按材料账对齐（否则真空防线抢在闸门前）",
          '_ex1["real_calls"] = max(' in lo and 'r1["execution"] = _ex1' in lo)
    # 工具确实关掉了：语义层不 import 执行层、不 exec
    _src = (Path(__file__).resolve().parent / "reason_pass.py").read_text(
        encoding="utf-8")
    check("语义层不碰执行原语（工具关闭）",
          "import local_agent" not in _src and "exec(" not in _src)

    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
