"""端到端复测：消费线回答用户原题，验证修复生效。

题目：在 'Split' 地图上，G2 Esports 队在赢得首血之后，平均每回合能赢下多少比例的回合？请同时给出样本量。
期望：100%、样本量 7（已用 SQL 实查验证）。
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mve"))

QUESTION = ("在 'Split' 地图上，G2 Esports 队在赢得首血之后，"
            "平均每回合能赢下多少比例的回合？请同时给出样本量。")


async def main():
    from voyager_vlml.consume import consume
    r = await consume(QUESTION)
    print("=" * 70)
    print("题目：", QUESTION)
    print("-" * 70)
    print("归题 topic_id:", r.get("topic_id"))
    print("topic_hit:", r.get("topic_hit"))
    print("复用配方:", r.get("reused"), "|", r.get("reuse_note"))
    print("used_skills:", r.get("used_skills"))
    print("-" * 70)
    print("tool_calls:", r.get("tool_calls"), "ok:", r.get("ok_tool_calls"),
          "rows:", r.get("rows"), "real_rows:", r.get("real_rows"),
          "vacuous:", r.get("vacuous"))
    print("SQL:", (r.get("sql") or "")[:200])
    print("-" * 70)
    print("裁判 rows:", r.get("referee_rows"))
    print("比对:", r.get("method_compare"))
    print("覆盖率:", r.get("coverage"), "掌握:", r.get("mastered"),
          "ability_total:", r.get("ability_total"))
    print("skipped:", r.get("skipped"))
    print("-" * 70)
    print("回答：")
    print(r.get("answer") or "（空）")
    print("=" * 70)
    # 判定
    ans = r.get("answer") or ""
    ok_100 = "100" in ans
    ok_7 = "7" in ans
    real = (r.get("real_rows") or 0) > 0
    print(f"判定：含 100%={ok_100} 含样本量7={ok_7} 取到真数={real}")
    if ok_100 and ok_7 and real:
        print("✅ 修复生效")
    else:
        print("❌ 仍未答对，需继续排查")
        Path("/root/mve/consume_result.json").write_text(
            json.dumps(r, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")


asyncio.run(main())
