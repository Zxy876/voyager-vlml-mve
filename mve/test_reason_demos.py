#!/usr/bin/env python3
"""语义示范库的测试：只收验得对、且"不乱"的轨迹（DeepSeek rejection sampling 同构）。

跑法：python mve/test_reason_demos.py（服务器无 pytest，同 test_*.py 口径）。

对应论文口径（已核对原文，见 reason_demos.py docstring）：
  · cold start：`retain only those with correct final answers and a readable format`
  · rejection sampling：`sample multiple responses and retain only the correct ones`，
    并 `filtered out ... mixed languages, long paragraphs, and code blocks`
落到 MVE：quotes 逐字核验通过（确定性「对」）+ 判官判过（调用方保证），
并滤掉代码块 / 刷屏 / 语言混杂 / 搬运原文。
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


GOOD = ("README 里对学习机制的描述与实现一致：材料写明系统在真实数据库环境中自我出题、"
        "取数、判卷，并把经验结晶成可重放的程序，因此「无梯度学习」这个说法站得住。")


def main() -> int:
    import reason_demos as rd
    import reason_pass as rp

    # 用独立场景 id，避免污染真账
    S = "_TEST_DEMOS"
    shutil.rmtree(rd.CKPT / f"{S}.json", ignore_errors=True)
    try:
        (rd.CKPT / f"{S}.json").unlink()
    except Exception:
        pass

    print("== 1. 只收「验得对」的（rejection sampling 第一关）==")
    check("ok=False 的不收（没通过校验的轨迹不许当示范）",
          rd.add(S, "q1", {"ok": False, "answer": GOOD, "verdict": "是"}) is False)
    check("verdict=无法判断 的不收（它没给出判断，学不到东西）",
          rd.add(S, "q2", {"ok": True, "answer": GOOD, "verdict": "无法判断"}) is False)
    check("答案太短的不收（「描述是准确的。」没有示范价值）",
          rd.add(S, "q3", {"ok": True, "answer": "描述是准确的。", "verdict": "是"}) is False)
    check("合格的一条收得进来",
          rd.add(S, "README 里关于学习机制的描述是否准确？",
                 {"ok": True, "answer": GOOD, "verdict": "是",
                  "quotes": [{"quote": "系统在真实数据库环境中自我出题"}]}) is True,
          f"当前 {rd.count(S)} 条")

    print("== 2. 过滤「乱」的输出（DeepSeek 也滤 code blocks / 语言混杂）==")
    check("代码块不收",
          rd.add(S, "q_code", {"ok": True, "verdict": "是",
                              "answer": GOOD + "\n```python\nprint(1)\n```"}) is False)
    check("刷屏重复不收（同一行刷 4 次）",
          rd.add(S, "q_rep", {"ok": True, "verdict": "是",
                             "answer": "\n".join(["这一行是重复刷屏的内容占位"] * 8 +
                                                 [GOOD])}) is False)
    check("中英混杂不收",
          rd.add(S, "q_mix", {"ok": True, "verdict": "是",
                             "answer": GOOD + "\n" +
                             "\n".join(["This is a very long english line " * 2] * 4)}) is False)

    print("== 3. 答案本身是材料搬运 → 不收（示范里教抄原文比不教更坏）==")
    _mat = ("【材料】" + GOOD * 3)
    _dump = {"ok": True, "verdict": "是", "answer": GOOD * 3}
    check("搬运原文的答案被拒", rd.add(S, "q_dump", _dump, material=_mat) is False)
    check("同一条答案、有材料但答案是自己写的 → 照收",
          rd.add(S, "q_ok", {"ok": True, "verdict": "部分", "answer": GOOD},
                 material="【材料】库里只有 base_salary 与 allowance 两列，没有加班费率。")
          is True)

    print("== 4. 同题覆盖 + 封顶 ==")
    _n0 = rd.count(S)
    rd.add(S, "README 里关于学习机制的描述是否准确？",
           {"ok": True, "verdict": "部分", "answer": GOOD + "（更新后的判断）"})
    check("同一问题再收一次 = 覆盖，不重复堆", rd.count(S) == _n0, f"{_n0} → {rd.count(S)}")
    for i in range(rd.MAX_PER_SCENE + 5):
        rd.add(S, f"批量问题 {i} 的描述是否准确？",
               {"ok": True, "verdict": "是", "answer": GOOD + f"（第 {i} 条）"})
    check(f"封顶 {rd.MAX_PER_SCENE} 条（示范是占 token 的，多了盖过材料）",
          rd.count(S) == rd.MAX_PER_SCENE, str(rd.count(S)))

    print("== 5. 召回：词面重叠才给，不硬塞 ==")
    rd.add(S, "README 里关于技能库的描述是否准确？",
           {"ok": True, "verdict": "是", "answer": GOOD})
    _hit = rd.recall(S, "README 里关于技能库的描述是否完整？", k=2)
    check("同题面能召回", any("技能库" in str(d.get("question")) for d in _hit),
          str([d.get("question")[:12] for d in _hit]))
    check("完全无关的问题不硬塞（没见过的类型别拿别的示范充数）",
          rd.recall(S, "今天天气如何", k=2) == [])
    check("k=0 时返回空", rd.recall(S, "README 里关于技能库的描述", k=0) == [])

    print("== 6. 渲染：教「怎么判」，不是给答案 ==")
    _txt = rd.render(rd.recall(S, "README 里关于技能库的描述是否准确？", k=1))
    check("示范文本非空且有「问/判断/答」三要素",
          all(k in _txt for k in ("问：", "判断：", "答：")))
    check("明说「不要照抄它结论里的具体内容」", "不要照抄" in _txt)
    check("空示范渲染成空串（不往 prompt 里塞空标题）", rd.render([]) == "")

    print("== 7. 接进语义层：示范只在前，材料仍是唯一事实来源 ==")
    _seen: dict = {}

    def _llm(messages):
        _seen["user"] = messages[1]["content"]
        return {"verdict": "是", "quotes": [{"quote": "系统在真实数据库环境中自我出题"}],
                "answer": GOOD, "gap": ""}

    rp.answer_over_material("README 里关于学习机制的描述是否准确？", _mat,
                            llm=_llm, demos_text=_txt)
    check("示范进 user 消息", "示范 1" in _seen.get("user", ""))
    check("示范排在材料前面（材料才是主体）",
          _seen.get("user", "").find("示范 1")
          < _seen.get("user", "").find("材料（取自本次取证"))
    _seen.clear()
    rp.answer_over_material("README 里关于学习机制的描述是否准确？", _mat, llm=_llm)
    check("不传示范时不多一行空标题", "示范 1" not in _seen.get("user", ""))

    print("== 8. 主循环接线（学习侧：召回 + 收库都挂上了）==")
    _src = (rd.__file__.rsplit("/", 1)[0] + "/learn_orchestrate.py")
    _code = open(_src, encoding="utf-8").read()
    check("语义收口前召回示范", "reason_demos" in _code and "_rd.recall(" in _code)
    check("收口成功后收进示范库", "_rd2.add(" in _code)
    check("只在验得对的分支里收（与判官判过同一分支）",
          _code.find("_rd2.add(") > _code.find("语义收口通过同一把尺子"))

    # 清理测试场景账
    try:
        (rd.CKPT / f"{S}.json").unlink()
    except Exception:
        pass

    print(f"\n== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
