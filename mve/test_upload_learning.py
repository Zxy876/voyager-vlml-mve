#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""学习侧上传通路（方案 a，2026-11-03 场景 J 事故后）的契约测试。

事故形状（服务器实证）：场景 J 上传了 3 万字《心悦文集》，学习侧却只看得见
库里那张联网缓存表 web_docs → 把腾讯"心悦会员"/同名小说的网页内容当材料
结晶成 4 条技能 → 消费侧回访标着"库内已验证"复用错误内容。

契约（本文件逐条钉住）：
  ① kind=scene 的 profile **必须带 scene_dir**（否则学习/消费两侧读不到
     uploads/ 下的 sidecar，本机原语也不会注册）；
  ② uploads_manifest 要给出「文件名 + 字数 + read_file 路径」，让执行层
     知道场景里有什么可读文件（工具说明书就是能力边界）；
  ③ uploads_corpus 返回上传文档全文（引文字面守卫的定位底本）；
  ④ 技能来源按**实际调用的工具**判：read_file → upload / web_* → web /
     其余 → db；
  ⑤ 引文字面守卫：技能里的引文在文档全文中定位不到 → 拒绝入库
     （与 SQL 字面量守卫同构：可确定性证伪的部分不赌 LLM）；
  ⑥ skill_store.add 落 provenance 字段（新条目 + 重写两条通道）。

跑法：.venv/bin/python mve/test_upload_learning.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path[:0] = [str(HERE), str(REPO)]

PASS = 0


def ok(cond: bool, label: str, extra: str = "") -> None:
    global PASS
    if not cond:
        print("FAIL ", label, ("| " + extra) if extra else "")
        raise SystemExit(1)
    PASS += 1
    print("PASS ", label, ("| " + extra) if extra else "")


def main() -> int:
    import scene_profile
    import upload
    import skill_store

    root = Path(tempfile.mkdtemp(prefix="upl_learn_"))
    (root / "J").mkdir(parents=True, exist_ok=True)
    # 造一个场景库（kind=scene 的判定依据），文本上传本身不需要 duckdb
    (root / "J" / "scene.duckdb").write_bytes(b"")

    orig_dir = scene_profile.SCENE_DATA_DIR
    orig_scene = scene_profile._scene
    orig_default = scene_profile._default_scene_id
    try:
        scene_profile.SCENE_DATA_DIR = root
        scene_profile._scene = lambda sid: {"id": sid, "name": f"场景{sid}",
                                            "data_source": ""}
        scene_profile._default_scene_id = lambda: "A"

        # ---- ① kind=scene 必须带 scene_dir（事故根因）----
        prof = scene_profile.profile("J")
        ok(prof["kind"] == scene_profile.SCENE, "有库 → kind=scene", str(prof["why"]))
        ok(bool(prof.get("scene_dir")), "kind=scene 的 profile 带 scene_dir",
           str(prof.get("scene_dir")))
        ok(Path(str(prof.get("scene_dir"))).name == "J",
           "scene_dir 指向本场景目录（隔离）", str(prof.get("scene_dir")))

        # ---- ②③ 上传一份 txt → 清单 / 全文语料 ----
        text = ("五月赛事即将到来，桃子在赛道上练习飘移。\n"
                "「只要敢踩油门，就成了」——他把手刹当成断点标记。\n"
                "投射现实：驾驶位闪现书桌前，手刹变成了笔。\n")
        r = upload.ingest("J", "心悦文集.txt", text.encode("utf-8"))
        ok(bool(r.get("ok")), "文本上传成功", str(r.get("why")))
        ok(r.get("kind") == "text", "文本按上下文处理（不落表）", str(r.get("kind")))
        ok(r.get("visible") is False, "文本不冒充库内可见", str(r.get("visible")))

        man = upload.uploads_manifest("J")
        ok("心悦文集.txt" in man, "清单里有文件名", man.splitlines()[1][:60])
        ok("read_file(" in man, "清单给出 read_file 用法", man.splitlines()[1][:60])
        ok("字" in man, "清单带字数（人/模型都能判断量级）")
        ok("绝不编造" in man, "清单写明不许编造（工具说明书即边界）")
        ok("不是正文" in man, "清单点明 meta 只是索引、正文要 read_file")

        corpus = upload.uploads_corpus("J")
        ok("桃子在赛道上练习飘移" in corpus, "全文语料能定位到正文")

        # ---- ④ 来源判定 ----
        import learn_orchestrate as LO
        ok(LO._provenance_of(["read_file"], "read_file('uploads/x.txt')") == "upload",
           "read_file → upload")
        ok(LO._provenance_of([], "web_search('心悦')") == "web",
           "web_search → web")
        ok(LO._provenance_of(["query_sql"], "query_sql('select 1')") == "db",
           "query_sql → db")

        # ---- ⑤ 引文字面守卫 ----
        ok(LO._upload_quote_guard("「只要敢踩油门，就成了」", "", "J") == "",
           "真引文放行（全文可定位）")
        bad = LO._upload_quote_guard("「心悦会员V3专属客服礼遇」", "", "J")
        ok(bool(bad), "幻觉引文被拒", bad[:40])
        ok(LO._upload_quote_guard("没有任何引文的技能描述", "", "J") == "",
           "无引文可查时放行（不扩大否决面）")

        # ---- ⑥ provenance 落库 ----
        store = root / "skill_store.json"
        store.write_text(json.dumps({"version": 2, "skills": {}, "writes": 0,
                                     "added": 0, "rewrites": 0, "skipped": 0}),
                         encoding="utf-8")
        orig_store = skill_store.STORE
        orig_emb = getattr(skill_store, "EMB_STORE", None)
        skill_store.STORE = store
        skill_store.EMB_STORE = root / "skill_emb.json"
        try:
            skill_store._RAW_CACHE["sig"] = None
            skill_store._RAW_CACHE["data"] = None
            act, key = skill_store.add(
                "learn_t", "quota", "读上传文档里的车队配额口径与算法",
                code="read_file('uploads/x.txt')", source="learn",
                provenance="upload", exact_only=True)
            ok(act in ("added", "rewritten"), "技能写入成功", act)
            rec = json.loads(store.read_text(encoding="utf-8"))["skills"][key]
            ok(rec.get("provenance") == "upload", "新条目记下来源", str(rec.get("provenance")))
            skill_store.add("learn_t", "quota", "读上传文档里的车队配额口径与算法",
                            code="read_file('uploads/x.txt') + 注释", source="learn",
                            provenance="upload", exact_only=True)
            rec2 = json.loads(store.read_text(encoding="utf-8"))["skills"][key]
            ok(rec2.get("provenance") == "upload", "重写通道保住来源",
               str(rec2.get("provenance")))
        finally:
            skill_store.STORE = orig_store
            if orig_emb is not None:
                skill_store.EMB_STORE = orig_emb
            skill_store._RAW_CACHE["sig"] = None
            skill_store._RAW_CACHE["data"] = None
    finally:
        scene_profile.SCENE_DATA_DIR = orig_dir
        scene_profile._scene = orig_scene
        scene_profile._default_scene_id = orig_default

    print(f"== 全部通过（{PASS} 项）：学习侧上传通路 —— 场景目录 / 清单 / 语料 / "
          f"来源 / 引文守卫 / provenance ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
