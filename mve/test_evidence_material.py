#!/usr/bin/env python3
"""材料（观察面）长什么样，决定了语义层能不能指着它说话。

跑法：python mve/test_evidence_material.py（服务器无 pytest，同 test_*.py 口径）。

对应的生产事实（2026-10-31 Scene H，面板 11:36 那一轮，9 条维度 0 闭合）：

    ① `gh_repo_info` 的返回被压成一行 JSON `{"license": "", ...}`，语义层按人话
       写引文 `license: ""` → 逐字核对对不上 → 正确的「没有声明许可证」被判成
       「引文像是编的」。
    ② `gh_tree` 的返回是 `["A.md", "B.md", ...]`，模型引用写成 `A.md, B.md` →
       3 条真引文全 miss，判词：「3 条引文在材料里都对不上（像是编的）」。
    ③ README 8000 字被 `_EVIDENCE_CHARS=4000` 静默砍半 → 语义层在残料上做整体
       判断 → 判官判「你的结论与材料账不符，材料账中只展示了部分README内容」。
    ④ 同一轮里材料账明明躺着 12004 字，却因 solve 最终步没写 RESULT 而记成
       calls=4 / 真取到=0 / 行数=0 → status=no_data → 维度判死、0 技能入库。

这一套测的就是「取回来的东西，能不能被人指着引用」和「取到了算不算取到」。
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
    import local_agent as L
    import learn_orchestrate as lo

    print("== 1. 结构化返回要一行一条（引文才抄得对）==")
    info = {"name": "voyager-vlml-mve", "license": "", "description": "在线强化学习"}
    txt = L._flat_text(info)
    check("dict → key: value 行", "license: " in txt and txt.count("\n") >= 2, txt)
    check("空值如实留空（不许写 None）", "None" not in txt, txt)
    tree = ["MVE跑通记录.md", "README.md", "docs"]
    txt2 = L._flat_text(tree)
    check("list[str] → 一行一个（- 前缀）",
          "- README.md" in txt2 and "- docs" in txt2, txt2)
    check("不再输出 JSON 数组（消除 \" 与 , 的引用噪声）",
          "[" not in txt2 and '"' not in txt2, txt2)
    check("文本返回原样透出（READ 类工具不受影响）",
          L._flat_text({"text": "# 标题\n正文", "path": "README.md"}) == "# 标题\n正文")
    check("MCP 网关的 {result:...} 仍能剥壳",
          "README" in L._flat_text({"result": {"files": ["README.md"]}}))

    print("== 2. 正文被剪时必须标明剪了多少（模型不知道就会猜）==")
    long = "段" * (L._EVIDENCE_CHARS + 500)
    item = L._evidence_item("gh_read", {"text": long, "path": "README.md"},
                            args="Zxy876/voyager-vlml-mve README.md")
    check("剪到上限", len(item["text"]) <= L._EVIDENCE_CHARS + 120,
          f"{len(item['text'])} 字")
    check("带「只给前 N 字」标注且给出原文总长",
          "只给前" in item["text"] and str(len(long)) in item["text"])
    check("没超长就不加标注",
          "只给前" not in L._evidence_item("gh_read", {"text": "短正文"})["text"])
    check("README 常见体量放得下（上限不再砍半）", L._EVIDENCE_CHARS >= 12000,
          f"{L._EVIDENCE_CHARS}")

    print("== 3. 「取到了」的文档侧口径：正文非空就算真取到 ==")
    doc = [{"tool": "gh_read", "ok": True, "text": "README 正文" + "。" * 30,
            "args": "README.md"}]
    sql = [{"tool": "query_sql", "ok": True, "text": "NULL, 0"}]
    empty = [{"tool": "gh_read", "ok": True, "text": "  "}]
    failed = [{"tool": "gh_read", "ok": False, "text": "x" * 50}]
    check("gh_read 取回正文 → 算真取到", len(lo._material_real(doc)) == 1)
    check("SQL 空结果 → 不许靠正文自救（真空防线保留）",
          len(lo._material_real(sql)) == 0)
    check("空正文 → 不算", len(lo._material_real(empty)) == 0)
    check("失败条目 → 不算", len(lo._material_real(failed)) == 0)
    check("没有材料账 → 不算（不硬凑）", lo._material_real(None) == [])

    print("== 4. 学习主循环接线：真空闸门要看材料账 ==")
    from pathlib import Path
    src = (Path(__file__).resolve().parent / "learn_orchestrate.py").read_text(
        encoding="utf-8")
    check("r1 与整轮口径都对齐（只对齐 r1 等于没对齐）",
          'r1["execution"] = _ex1' in src and 'out["_mat_real"]' in src)
    check("闸门的 real 认材料账", "real = True" in src and "_mat_n" in src)
    check("轮内口径不进 run_log", 'out.pop("_mat_real", None)' in src)

    print("== 5. 仓库取证也算配方（否则 H 学一整轮技能库 0 涨）==")
    # 面板 11:36 那一轮：4 条维度靠 gh_* 取证闭合，技能入库 0 条 —— 判词是
    # 「没调取数动作，不是配方」。因为配方白名单只认 list_files/query_sql/web_*，
    # 不认 gh_*（与 2026-10-22「纯计算式 SELECT」漏判同一类错）。
    from voyager_vlml.consume import _looks_like_recipe as _recipe
    check("gh_read 取证代码算配方", _recipe('r = gh_read("o/r", "README.md")') is True)
    check("gh_tree / gh_repo_info / gh_api 也算",
          all(_recipe(c) for c in ('t = gh_tree("o/r")',
                                   'i = gh_repo_info("o/r")',
                                   'a = gh_api("o/r", "commits")')))
    check("纯答案仍不算（凭记忆不许入库）",
          _recipe('RESULT = {"answer": "许可证是 MIT"}') is False)

    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
