#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A/B 探针 3：Phase1 侧 —— 「库里没那个量」还会不会当**缺口**无限追问？

背景：`maieutic_gate.GATE_SYSTEM` 里，原文把「库里没这个量」判据写在了
`questions`（= 还没答到的缺口，要追问）那一段 ⇒ 实测 2026-11-13 连问 14 轮
永远闭合不了（`maieutic_gate.py:71-73` 注释）。

改动：把它改投 `emergent_gaps`（清单外发现，原版 `spec-examiner.ts:18-22`
明文**不阻塞闭合**）。

本探针用**真 LLM**（判官模型 glm-4.6）跑两版 gate，同一条「库里没那个量」的
维度，比对：
  · 它落在 `questions`（缺口，要追问）还是 `emergent`（清单外发现，不阻塞）
  · `passed` 是否 true（闭合）

维度设计：场景 L 的 web_facts 里**确实没有** XHS/微博/B站 这些站点列
（前面已实查：468 个站点列，XHS/Xiaohongshu/小红书/Weibo/Bilibili 全部 False），
而 env_obs 里只列了真实存在的站点 ⇒ 判官面对「查 XHS 的 http 状态码」时，
这个量在环境实查清单里指认不了。

★ 独立脚本必须自己加载 .env（面板进程会加载，独立脚本不会）—— 否则
  MVE_CRITIC_MODEL 为空，会走默认模型而不是判官模型。

用法：python mve/_probe_phase1_gap.py <maieutic_gate.py 路径> <标签>
"""
import importlib.util
import json
import os
import sys

MOD_PATH = sys.argv[1]
LABEL = sys.argv[2] if len(sys.argv) > 2 else MOD_PATH

REPO = "/root/mve"
sys.path.insert(0, REPO + "/mve")
sys.path.insert(0, REPO)

# ── 先加载 .env（判官模型要从这里读）──────────────────────────────
with open(REPO + "/mve/.env", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
print("判官模型 MVE_CRITIC_MODEL =", os.environ.get("MVE_CRITIC_MODEL", ""))

spec = importlib.util.spec_from_file_location("gate_probe", MOD_PATH)
mg = importlib.util.module_from_spec(spec)
sys.modules["gate_probe"] = mg
spec.loader.exec_module(mg)

# ── 真实环境实查清单（只列 web_facts 里真实存在的站点）───────────────
import duckdb  # noqa: E402

con = duckdb.connect(REPO + "/scene_data/L/scene.duckdb", read_only=True)
cols = [c[0] for c in con.execute("describe web_facts").fetchall()]
sites = [c for c in cols if not c.endswith("http 状态码")]
env_obs = ("数据源 web_facts 的真实列（节选，共 %d 个站点列）：\n"
           % len(sites)) + "、".join(sites[:40]) + "\n（其余省略）\n"
ENV_OBS = env_obs

QUESTION = "查一下 XHS（小红书）这个站点的 http 状态码是多少？"

# 维度：这个量在环境实查清单里**根本不存在**
DIMS = [{
    "id": "d1",
    "question": "给出 XHS（小红书）站点的 http 状态码（从 web_facts 表里取）",
    "dimension": "给出 XHS（小红书）站点的 http 状态码（从 web_facts 表里取）",
    "required": True,
}]

# 学生交出的全文：它尝试去查，但库里没有 ⇒ 诚实说查不到
ANSWER = ("我查了 web_facts 表，想取 XHS（小红书）这一列的 http 状态码，"
          "但这个表里没有 XHS 这一列（表里是别的站点名），所以取不到这个量。\n"
          "结论：XHS（小红书）的 http 状态码在当前数据源里不存在，无法给出数值。")

res = mg.check(QUESTION, ANSWER, DIMS, env_obs=ENV_OBS,
               material_required=False, evidence=[])

print("═══ %s ═══" % LABEL)
print("passed       :", res.get("passed"))
print("origin       :", res.get("origin"))
print("addressed    :", json.dumps(res.get("addressed"), ensure_ascii=False)[:200])
print("open         :", json.dumps(res.get("open"), ensure_ascii=False)[:300])
print("questions    :", json.dumps(res.get("questions"), ensure_ascii=False)[:500])
print("emergent     :", json.dumps(res.get("emergent"), ensure_ascii=False)[:500])
print("")
if res.get("questions"):
    print("⇒ 判官把它当**缺口**了 → 会追问 → 可能无限追问")
else:
    print("⇒ 判官没把它当缺口 → 不追问")
print("⇒ 闭合（passed）:", res.get("passed"))
