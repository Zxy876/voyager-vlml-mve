# -*- coding: utf-8 -*-
"""背包 + 档位回归验证 —— 不联网、不跑 LLM，只读账本。

说明书（全部可逐行核对）：
  curriculum.py:155      inventory = event["inventory"]     —— 背包是事件字段
  curriculum.py:205      Inventory ({inventory_used}/36)    —— 槽位容量是常量
  curriculum.py:244      progress == 0 -> 常量 "Mine 1 wood log"
  curriculum.py:251      inventoryUsed >= 33 -> "store items in chest"
  curriculum.py:322-330  manual 模式 -> input("Enter task: ") 人给题，一直等
  curriculum.py:337-339  只有 success 才 completed_tasks.append
  sessions.ts:257-262    iterations 都入账(背包)，passed 才写 finalSpecText(箱)

2026-11-18 口径（用户拍板）：**全世界只有一个背包**
  · 一格 = 一次会话（MVE 的 rollout），格内明细 = 会话事件流跑出来的东西
  · 没有 `sites()` 预设全集、没有 `free = sites - covered`、没有书单/单元规划
  · 没有「到顶」：没有全集就证不了「学完了」，只有能力全关/提不出题才停

用例：
  T-F  parse_sites 只认实查存在的 .py（不认编出来的文件名）
  T-A  清账本 -> 档 A，**不出题**，要求先导入（LLM 调用 0 次）
  T-P  旁路 propose_next_ai_task（turing 出题路径）同样被闸门拦住
  T-O  观察面 = `Inventory (0/36): Empty`，**没有「共 N」**（无预设清单）
  T-S  人给的种子原样采用（第一个 123 由人促成，LLM 0 次）
  T-K  会话成功 -> 装一格，格内有实查落点；progress=1 -> 档 N
  T-E  种子之后连续两圈都能出题（圈不断）
  T-C  装满 33 格 -> 档 C -> 存箱（chests+1、背包清空）并继续出题
  T-D  存箱后仍在同一世界里（没有"到顶"这个终态）
  T-L  非仓库材料 -> legacy（旧路径不动）

跑法：/root/mve/.venv/bin/python test_band.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import curriculum_auto as ca
import scene_profile as sp

SID = "S"
ok = []


class FakeLLM:
    """按题目序号返回不同落点，模拟真出题器挑落点（不联网）。"""

    def __init__(self, sites):
        self.sites = list(sites or [])
        self.i = 0
        self.calls = []

    def __call__(self, msgs):
        self.calls.append(msgs)
        if self.i >= len(self.sites):
            return "没有更多任务了"
        s = self.sites[self.i]
        self.i += 1
        return "Task: %s\nContext: 造一个输入让它表现不一样" % q_for(s)


def q_for(site):
    f, _sep, sym = site.partition("::")
    return (f"证明 B ≠ A：{f} 的 {sym}" if sym else f"证明 B ≠ A：{f} 这个模块")


def agent(tmp, name="x"):
    prof = sp.profile(SID) or {}
    return ca.CurriculumAgent(SID, scene_dir=str(prof.get("scene_dir") or ""),
                              state_dir=tmp / name)


def real_files(root, limit=40):
    """真实存在的 .py（相对 root）。测试用它出题面 —— 实查，不是预设清单。"""
    out = []
    for p in sorted(Path(root).rglob("*.py")):
        if "__pycache__" in p.parts or ".bak" in p.name or p.name.startswith("_probe"):
            continue
        out.append(str(p.relative_to(root)))
        if len(out) >= limit:
            break
    return out


def main():
    tmp = Path(tempfile.mkdtemp(prefix="band_"))
    try:
        a = agent(tmp, "a")
        root = a._material_root()
        assert root and Path(root).is_dir(), root
        files = real_files(root)
        assert files, "S 场景的仓库根没接上"

        # ---------------- T-F：parse_sites 只认实查存在的 .py
        assert ca.parse_sites(files[0], root) == [files[0]]
        assert ca.parse_sites("nope_xyz_not_exist.py 的 foo", root) == []
        ok.append("T-F parse_sites 实查（编出来的文件名不算落点）")

        # ---------------- T-A：清账本 -> 档 A 要种子，不自己发明
        assert a.progress == 0 and a.inventory_used == 0
        assert a.pick_band() == "A", a.pick_band()
        assert a.needs_seed is True
        fake = FakeLLM(files)
        a._llm = fake
        t, why = a.propose_next_task(max_retries=1)
        assert t == "", ("档A 不该出题", t)
        assert "导入" in why, why
        assert fake.calls == [], "档A 不许调 LLM（%d 次）" % len(fake.calls)
        ok.append("T-A 档A 无种子 -> 空 + 原因（LLM 0 次）")

        # ---------------- T-P：旁路也要拦住（turing 走 propose_for -> 这里）
        fake.calls.clear()
        t_p, why_p = a.propose_next_ai_task(max_retries=1)
        assert t_p == "" and "导入" in why_p, (t_p, why_p)
        assert fake.calls == [], "旁路调了 LLM（%d 次）" % len(fake.calls)
        ok.append("T-P 旁路 propose_next_ai_task 同样被闸门拦住")

        # ---------------- T-O：观察面没有预设总清单
        obs = a.render_observation()
        inv = obs["inventory"]
        assert "inventory" in obs, list(obs)
        assert "Inventory (0/36): Empty" in inv, inv
        assert "共 " not in inv and "未证" not in inv, inv
        ok.append("T-O 观察面 " + inv.strip()[:56])

        # ---------------- T-S：人给的种子原样采用（第一个 123 由人促成）
        seed = q_for(files[0])
        t2, ctx2 = a.propose_next_task(queue=[seed], max_retries=1)
        assert t2 == seed, (t2, seed)
        assert fake.calls == [], "采用人给的种子时不该调 LLM"
        assert "人导入" in ctx2, ctx2
        ok.append("T-S 人给的种子原样采用（LLM 0 次）")

        # ---------------- T-K：会话成功 -> 装一格（明细 = 跑出来的落点）
        a.update_exploration_progress({"task": t2, "success": True})
        assert a.progress == 1
        assert a.inventory_used == 1, a.inventory_used
        slot = next(iter(a.slots.values()))
        assert files[0] in (slot.get("sites") or []), slot
        assert a.pick_band() == "N", a.pick_band()
        assert a.needs_seed is False
        ok.append("T-K 会话装格：%d 格，明细 %s"
                  % (a.inventory_used, slot.get("sites")))

        # 观察面报的是「占几格/容量」，不是「共多少」
        inv2 = a.render_observation()["inventory"]
        assert "Inventory (1/36)" in inv2, inv2
        ok.append("T-K 观察面 " + inv2.strip()[:56])

        # ---------------- T-N：progress>0 -> 档 N，模型出题，无起步档
        fake.calls.clear()
        fake.i = 1          # 种子用的就是 files[0]，出题器从下一个落点开始
        t3, _ = a.propose_next_task(max_retries=1)
        assert t3 and fake.calls, "档N 应调 LLM 出题"
        assert "起步档" not in fake.calls[0][1]["content"], "档A 提示已删，不该再出现"
        ok.append("T-N progress=1 -> 档N，模型出题，无起步档")

        # ---------------- T-E：种子之后连续两圈都能出题（圈不断）
        a.update_exploration_progress({"task": t3, "success": True})
        t4, _ = a.propose_next_task(max_retries=1)
        assert t4, "圈断了：第二圈出不了题"
        a.update_exploration_progress({"task": t4, "success": True})
        t5, _ = a.propose_next_task(max_retries=1)
        assert t5, "圈断了：第三圈出不了题"
        ok.append("T-E 种子之后连续出题（progress=%d, 背包=%d 格）"
                  % (a.progress, a.inventory_used))

        # ---------------- T-C：装满 33 格 -> 档 C，存箱并继续出题
        # 直接造账 = 等价于又跑了 33 次会话（测试不跑 LLM/执行）
        for i in range(ca.INV_FULL):
            a.slots["seed-slot-%02d" % i] = {"sites": [], "dims": [],
                                             "rounds": 1, "at": ""}
        assert a.inventory_used >= ca.INV_FULL and a.pick_band() == "C"
        c0 = len(a.chests)
        fake.calls.clear()
        t6, _ = a.propose_next_task(max_retries=1)
        assert len(a.chests) == c0 + 1, "应存箱一批"
        assert a.inventory_used == 0, "存箱后背包应清空"
        assert a.batch == 1
        assert t6, "存箱后应继续出题"
        ok.append("T-C 背包满 %d/%d -> 存箱第 %d 批并继续出题"
                  % (ca.INV_FULL, ca.INV_CAP, len(a.chests)))

        # 落盘/读回一致
        b = agent(tmp, "a")
        assert b.batch == a.batch and b.slots == a.slots
        ok.append("落盘/读回一致")

        # ---------------- T-D：存箱不是终态（没有"到顶"）
        b._llm = FakeLLM(files[20:24])   # 未学过的落点（前面用的是 files[0..2]）
        b.completed_tasks = list(a.completed_tasks)
        again, _ = b.propose_next_task(max_retries=1)
        assert again, "存箱后应能继续学（没有到顶）"
        ok.append("T-D 存箱后继续（无「到顶」终态）")

        # ---------------- T-L：非仓库材料走 legacy
        pp = sp.profile("P") or {}
        c = ca.CurriculumAgent("P", scene_dir=str(pp.get("scene_dir") or ""),
                               db_path=str(pp.get("db_path") or ""),
                               state_dir=tmp / "P")
        assert c.pick_band() == "legacy", c.pick_band()
        ok.append("T-L 非仓库材料 -> legacy（旧路径不动）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("=== PASS", len(ok))
    for x in ok:
        print("  -", x)


if __name__ == "__main__":
    main()
