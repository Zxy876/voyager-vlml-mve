# -*- coding: utf-8 -*-
"""T-Q 来源闸门：系统出的题不能顶「人导入的种子」位（2026-11-18）。

这条漏洞的形状（2026-11-18 取证，不是猜测）：
  scene_learn.py:655-666          graph_books.next_task 的图谱/书单题 -> queued
  curriculum_auto.py:1006-1016    queued 里任何字符串都当「人导入」，且在档 A 之前
  => progress==0 时第一个 123 由系统自证。

说明书：curriculum.py:322-330 manual 是人 input 给的题，一直等人；
        :264-266 auto 给的是常量 "Mine 1 wood log"，两者都不经过 LLM。

跑法：/root/mve/.venv/bin/python test_origin.py
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
        f, _sep, sym = s.partition("::")
        return "Task: 证明 B ≠ A：%s 的 %s\nContext: 造一个输入" % (f, sym or "模块")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="origin_"))
    try:
        prof = sp.profile(SID) or {}
        a = ca.CurriculumAgent(SID, scene_dir=str(prof.get("scene_dir") or ""),
                               state_dir=tmp / "a")
        root = a._material_root()
        sites = [str(p.relative_to(root)) for p in sorted(Path(root).rglob("*.py"))
                 if "__pycache__" not in p.parts and ".bak" not in p.name
                 and not p.name.startswith("_probe")][:12]
        assert sites, "sites 为空：S 场景仓库根没接上"
        assert a.progress == 0 and a.pick_band() == "A"
        fake = FakeLLM(sites)
        a._llm = fake
        q0 = "证明 B ≠ A：%s 这个模块" % sites[0]

        # T-Q1 系统题（origin=auto）进不了种子位
        t1, why1 = a.propose_next_task(
            queue=[{"question": q0, "origin": "auto"}], max_retries=1)
        assert t1 == "", ("系统题冒充了种子", t1)
        assert "导入" in why1, why1
        assert "系统" in why1, why1
        assert fake.calls == [], "被挡住的系统题不该再调 LLM"
        ok.append("T-Q1 origin=auto 在 progress==0 时被挡（LLM 0 次）")

        # T-Q2 人导入的 origin 才进（且不调 LLM）
        t2, why2 = a.propose_next_task(
            queue=[{"question": q0, "origin": "human_import"}], max_retries=1)
        assert t2 == q0, (t2, q0)
        assert "人导入" in why2, why2
        assert fake.calls == [], "采用人给的种子不该调 LLM"
        ok.append("T-Q2 origin=human_import 原样采用（LLM 0 次）")

        # T-Q3 裸 str 仍是人导入（向后兼容：老调用点/test_curriculum_critic）
        t3, why3 = a.propose_next_task(queue=[q0], max_retries=1)
        assert t3 == q0 and "人导入" in why3, (t3, why3)
        ok.append("T-Q3 裸 str 仍按人导入处理（向后兼容）")

        # 让种子这一圈闭合 -> progress=1
        a.update_exploration_progress({"task": q0, "success": True})
        assert a.progress == 1 and a.pick_band() in ("N", "B")

        # T-Q4 档 A 之外，系统题照用，但标注非人导入
        q1 = "证明 B ≠ A：%s 的 %s" % (sites[3].partition("::")[0],
                                       sites[3].partition("::")[2] or "模块")
        fake.calls.clear()
        t4, why4 = a.propose_next_task(
            queue=[{"question": q1, "origin": "auto"}], max_retries=1)
        assert t4 == q1, (t4, q1)
        assert "非人导入" in why4, why4
        assert fake.calls == [], "已有系统题时不该再调 LLM"
        ok.append("T-Q4 档N 下系统题可用且标注「非人导入」")

        # T-Q5 scene_learn 必须显式标 auto（防回归：这里塞回裸字符串就又漏了）
        sl = (Path(__file__).resolve().parent / "scene_learn.py").read_text("utf-8")
        assert '"origin": "auto"' in sl, "scene_learn 没标来源，漏洞会复发"
        ok.append("T-Q5 scene_learn 显式标 origin=auto")

        # T-Q6 混合队列：系统题在前，人题在后 -> 仍取人题
        b = ca.CurriculumAgent(SID, scene_dir=str(prof.get("scene_dir") or ""),
                               state_dir=tmp / "b")
        b._llm = FakeLLM(sites)
        qh = "证明 B ≠ A：%s 的 %s" % (sites[1].partition("::")[0],
                                       sites[1].partition("::")[2] or "模块")
        t6, why6 = b.propose_next_task(
            queue=[{"question": q0, "origin": "auto"},
                   {"question": qh, "origin": "human_import"}], max_retries=1)
        assert t6 == qh and "人导入" in why6, (t6, why6)
        ok.append("T-Q6 混合队列里系统题排前面也抢不到种子位")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("=== PASS", len(ok))
    for x in ok:
        print("  -", x)


if __name__ == "__main__":
    main()
