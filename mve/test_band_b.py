# -*- coding: utf-8 -*-
"""B 档（存包）回归验证 —— r_site 来自 chat 的 mode 游程，不联网。

说明书：
  chat/route.ts:85           只把 {studentMessage, opusMode, opusResponse} 追加进 opusExchanges
  phase2-chat.ts:12-30       interrogative=问自己的代码/推理；direct=问与本题无关的语法
  schemas.ts:397-401         mode 是 z.enum(["interrogative","direct"]) —— 二值由 schema 强制
  curriculum.py:264-291      progress==0 先 Mine 1 wood log；背包满才 store to chest

分工（2026-11-17 用户定）：人在人导入口拆骨架；agent 在 phase2 写码并提问。
所以 chat 账本按**题（topic）**落，`last_topic.json` 记住「刚做完哪道题」，
出题侧靠这个指针读到上一题的游程。

用例：
  T-B0  无 chat 账本              -> r_site=0, 档 N
  T-B1  三条 interrogative 同落点   -> r_site=3, 档 B
  T-B2  追加一条 direct           -> r_site=0, 档 N（游程断在末尾）
  T-B3  interrogative 但换落点    -> r_site=1, 档 N（不是"感觉连续"）
  T-B4  观察面照原版只报 Inventory (n/36)，不露 r_site 内部账
  T-B5  curriculum 只读 chat 账本（读三次内容字节不变）
  T-B6  换 topic 后 old topic 的游程不串味

跑法：/root/mve/.venv/bin/python test_band_b.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import curriculum_auto as ca  # noqa: E402
import phase2_chat as pc  # noqa: E402
import scene_profile as sp  # noqa: E402

SID = "S"
ok = []
fail = []


def chk(name, cond, extra=""):
    if cond:
        ok.append(name)
        print("  OK  %-42s %s" % (name, extra))
    else:
        fail.append(name)
        print("  !!  %-42s %s" % (name, extra))


def say(topic_msg, mode, agent, topic):
    """走 chat 的**唯一写口**（route.ts:85 的同构），并更新 last_topic 指针。"""
    ex = {"timestamp": "2026-11-17T23:30:00", "studentMessage": topic_msg,
          "opusMode": mode, "opusResponse": "…"}
    pc.append_exchange(agent._dir, topic, ex)
    pc.record_last_topic(agent._dir, topic,
                         r_site=pc.chat_streak(pc.load_exchanges(agent._dir, topic)))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="bandb_"))
    prof = sp.profile(SID) or {}
    a = ca.CurriculumAgent(SID, scene_dir=str(prof.get("scene_dir") or ""),
                           state_dir=tmp / "a")
    # 取真实 .py 当题面落点。必须与 parse_sites 同一把滤网（_probe/.bak），
    # 否则选到调试残渣 → 出题面定位为空 → 游程退化成「只按 mode 数」。
    sites = [str(p.relative_to(a._material_root()))
             for p in sorted(Path(a._material_root()).rglob("*.py"))
             if "__pycache__" not in p.parts and ".bak" not in p.name
             and not p.name.startswith("_probe")][:12]
    assert len(sites) >= 8, "仓库里真实 .py 不足 8 个：%d" % len(sites)
    a.completed_tasks.append("证明 B ≠ A：%s" % sites[0])
    topic = "learn_testtopic"   # 真实形态：phase2 传进来的 topic_id
    site = sites[3]
    q = "证明 B ≠ A：%s 这里为什么要多一步" % site

    # ---- T-B0 无 chat 账本
    chk("T-B0 无账本 r_site=0 档N", a.r_site == 0 and a.pick_band() == "N",
        "band=%s" % a.pick_band())

    # ---- T-B1 三条 interrogative 同落点
    for i in range(ca.CHAT_PASS_K):
        say(q + "（第%d问）" % i, "interrogative", a, topic)
    chk("T-B1 r_site=3", a.r_site == 3, "r_site=%d" % a.r_site)
    chk("T-B1 档位切到 B", a.pick_band() == "B", "band=%s" % a.pick_band())

    # ---- T-B4 观察面：照原版 curriculum.py:205 `Inventory (used/36): …`
    # 只报「占几格 + 格内是什么」；r_site 是档位判定用的内部账，原版
    # curriculum 看不到 opusMode 游程，观察面上也不该出现。
    obs = a.render_observation()
    inv = obs.get("inventory", "")
    chk("T-B4 观察面只报 Inventory (n/36)，不露内部游程",
        f"Inventory ({a.inventory_used}/" in inv
        and "连续自述" not in inv and "r_site" not in inv,
        inv.strip()[:62])

    # ---- T-B5 只读
    p = pc._chat_path(a._dir, topic)
    before = p.read_bytes()
    a.r_site
    a.pick_band()
    a.r_site
    chk("T-B5 读三次字节不变", p.read_bytes() == before)

    # ---- T-B2 末尾一条 direct -> 游程归零
    say("Python 里 dict 的 get 默认值怎么写", "direct", a, topic)
    chk("T-B2 direct 断游程 r_site=0", a.r_site == 0, "r_site=%d" % a.r_site)
    chk("T-B2 回到档 N", a.pick_band() == "N", "band=%s" % a.pick_band())

    # ---- T-B3 三条 interrogative 但最后一条换落点
    other = sites[7]
    for i in range(3):
        m = ("证明 B ≠ A：%s 换个点问" % other) if i == 2 else q
        say(m + "（第%d问）" % i, "interrogative", a, topic)
    chk("T-B3 换落点即断 r_site=1", a.r_site == 1, "r_site=%d" % a.r_site)
    chk("T-B3 档位仍 N", a.pick_band() == "N", "band=%s" % a.pick_band())

    # ---- T-B6 换 topic 不串味
    pc.record_last_topic(a._dir, "learn_another", r_site=0)
    chk("T-B6 换 topic 后 r_site=0", a.r_site == 0, "r_site=%d" % a.r_site)

    print("\n%d 项通过，%d 项失败 %s"
          % (len(ok), len(fail), ("→ " + ",".join(fail)) if fail else ""))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
