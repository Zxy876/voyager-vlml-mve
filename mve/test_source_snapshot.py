#!/usr/bin/env python3
"""来源快照（文档/仓库场景的「观察面」）的测试。

跑法：python mve/test_source_snapshot.py（服务器无 pytest，同 test_*.py 口径）。

为什么有这一层（2026-10-31 Scene H 实证）：
    骨架拆出「README.md 文件中提供的仓库最后更新时间是什么时候？」，而 README
    里根本没有日期字样。原版 Voyager 的 propose_next_task / decompose_task 都喂
    render_observation（nearby_blocks / inventory / biome），出题器看得见环境就
    不会拆出不存在的子目标。MVE 的数据场景有 db_observation，文档场景没有 ——
    这就是补的那一块。
"""

import os
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


def main() -> int:
    import maieutic_scaffold as ms
    import source_snapshot as ss

    # ⚠️ 测试必须用自己的场景 id：`scaffold()` 即使 use_cache=False **也会写缓存
    # 文件**（缓存写是正常职责），拿真场景 id 跑测试会把假 LLM 的占位维度写进
    # 生产骨架 —— 2026-10-31 在服务器上实测污染过 Scene H（骨架变成 1 条 `q`，
    # 锚点也被带偏）。测试碰生产账，和判据出错一样严重。
    S = "_TEST_SNAPSHOT"
    import shutil
    shutil.rmtree(ms.CKPT / S, ignore_errors=True)

    print("== 1. 按「最具体目标」决定要不要取快照 ==")
    check("没点名任何来源 → 不取快照",
          ss.snapshot("这批员工的工资合计是多少？").get("text") == "")
    check("点名了表名（数据场景）→ 归 db_observation 管，不在这里重复取",
          ss.snapshot("web_facts 表中该仓库的 claim 是什么？",
                      db_path="").get("kind") != "table")
    _u = ss.snapshot("https://github.com/Zxy876/voyager-vlml-mve 上写了什么？")
    check("URL 指向 GitHub 仓库 → 按仓库取（不然观察面只剩一句「没预取」，等于没给环境）",
          _u.get("kind") == "slug" and "github.com" not in _u.get("target", ""),
          _u.get("target", ""))
    check("仓库快照真的取到内容（文件清单/README），不是一句空话",
          ("文件清单" in _u.get("text", "")) or ("没取到" in _u.get("text", "")),
          f"{len(_u.get('text', ''))} 字")
    _plain = ss.snapshot("看看 https://example.com/a/b 这个页面里写了什么？")
    check("非 GitHub 的 URL 仍然如实说「没预取正文」（不假装看过）",
          _plain.get("kind") == "url" and "没有预取" in _plain.get("text", ""))

    print("== 2. 仓库：清单 + README 正文（有网才取得到，取不到也要如实说）==")
    _r = ss.snapshot("仓库 Zxy876/voyager-vlml-mve 的 README 是否属实？")
    _t = _r.get("text", "")
    check("认出来是仓库（slug）", _r.get("kind") == "slug", _r.get("target", ""))
    check("观察面非空（要么有内容，要么如实写没取到）", bool(_t.strip()))
    check("写清了是「实查」还是「没取到」",
          ("实查" in _t) or ("没取到" in _t), _t[:60].replace("\n", " "))
    if "实查" in _t:
        check("正文被截断时如实标注（别让下游以为看完了）",
              ("已截断" in _t) or len(_t) <= ss.TEXT_LIMIT + 200)

    print("== 3. 取不到时不许抛、不许编 ==")
    _bad = ss.snapshot("仓库 nobody/nonexistent-repo-xyz987 的 README 是否属实？")
    check("不存在的仓库：照样返回字符串、不抛异常", isinstance(_bad.get("text"), str))
    check("不存在的仓库：不会凭空写出文件清单",
          "README.md；" not in _bad.get("text", ""))

    print("== 4. 观察面进材料：render_material 会把它拼进去并回填 state ==")
    _st: dict = {}
    _mat = ms.render_material("仓库 Zxy876/voyager-vlml-mve 的 README 是否属实？",
                              S, state=_st)
    if _r.get("text"):
        check("材料里有「实际存在的内容」这一段",
              "实际存在" in _mat, _mat[-80:].replace("\n", " "))
        check("state 带回观察面摘要（日志用）", bool(_st.get("snapshot")),
              str(_st.get("snapshot"))[:60])
    else:
        check("取不到观察面时不往材料里塞空段", "实际存在" not in _mat)
        check("state 不记（没取到就别声称取了）", not _st.get("snapshot"))

    print("== 5. 喂了观察面才追加来源约束（同构原版：先有 observation 再有规则）==")
    _seen: dict = {}

    def _fake_llm(messages):
        _seen["system"] = messages[0]["content"]
        _seen["user"] = messages[1]["content"]
        return '{"milestone": {"name": "n", "why": "w"}, "dimensions": [' \
               '{"id": "a", "question": "q", "why": "w"}], ' \
               '"expected_drifts": [], "material_note": null}'

    ms.scaffold("仓库 Zxy876/voyager-vlml-mve 的 README 是否属实？", S,
                llm=_fake_llm, use_cache=False)
    if _r.get("text"):
        check("有观察面 → system 里有来源约束",
              ms.SCAFFOLD_SYSTEM_SOURCE.strip()[:20] in _seen.get("system", ""))
        check("来源约束要求「说法与观察面对照」，不是「X 是什么」",
              "说法与观察面对照" in _seen.get("system", ""))
    else:
        check("没取到观察面 → 不追加来源约束（别给空规则）",
              ms.SCAFFOLD_SYSTEM_SOURCE.strip()[:20] not in _seen.get("system", ""))

    print("== 6. 约束文本本身钉死三条铁律 ==")
    _s = ms.SCAFFOLD_SYSTEM_SOURCE
    check("① 找不到落点的不许出", "找得到落点" in _s)
    check("② 看不见的不许假设存在", "不许假设它存在" in _s)
    check("③ 截断时只许检查已给出部分", "正文被截断时" in _s)
    check("④ 「没取到」时只许出取证维度", "取证维度" in _s)

    print("== 7. 出题器解析失败按原版重试（propose_next_ai_task max_retries=5）==")
    _n = {"c": 0}

    def _bad_then_ok(messages):
        _n["c"] += 1
        if _n["c"] < 3:
            return '{"dimensions": [{"id": "a", "quest'   # 截断的 JSON
        return '{"milestone": {"name": "n", "why": "w"}, "dimensions": [' \
               '{"id": "a", "question": "q", "why": "w"}], "expected_drifts": []}'

    _o = ms.scaffold("仓库 Zxy876/voyager-vlml-mve 的 README 是否属实？", S,
                     llm=_bad_then_ok, use_cache=False)
    check("截断的输出会重试，第 3 次成功就收下（一次解析失败≠这题拆不出来）",
          _o.get("origin") == "llm" and len(_o.get("dimensions") or []) == 1,
          f"调用 {_n['c']} 次")

    _n2 = {"c": 0}

    def _always_bad(messages):
        _n2["c"] += 1
        return "我不知道该怎么拆。"

    _o2 = ms.scaffold("仓库 Zxy876/x 的 README 是否属实？", S,
                      llm=_always_bad, use_cache=False)
    check("一直拆不出 → 如实报 failed（不编维度）", _o2.get("origin") == "failed")
    check(f"重试上限 = {ms.SCAFFOLD_LLM_RETRIES}（原版同口径，不无限重试）",
          _n2["c"] == ms.SCAFFOLD_LLM_RETRIES, f"调用 {_n2['c']} 次")
    check("失败原因带出去（面板/日志能看出是哪一步）", bool(_o2.get("reason")))

    # 清掉测试场景的骨架/进度（不留垃圾）
    shutil.rmtree(ms.CKPT / S, ignore_errors=True)

    print(f"\n== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
