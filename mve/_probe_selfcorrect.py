"""一次性取证：同一道题、不给技能，看迭代修正环在真实库上跑几轮、每轮改了什么。

验的是「回灌上一轮结果」有没有让修正变准，不看最终答案对不对。
"""
import asyncio
import sys

sys.path.insert(0, ".")

from voyager_vlml.agents.action import VLMLActionAgent  # noqa: E402
from voyager_vlml.env.vlml_mcp import VLMLMCPEnv  # noqa: E402

Q = "在各张地图上，哪支队伍拿到首血（first blood）的次数最多？请给出地图、队伍、次数。"
HINTS = [
    "首血数据的表是 agg_first_blood_stats，字段 fb_team（首血队）、fb_team_won、map_name",
    "`round_number=1` 是**第一回合**，不是首血；首血要用 FB 相关字段",
]


async def main() -> None:
    env = VLMLMCPEnv()
    act = VLMLActionAgent(env=env)
    obs, sql = await act.solve(Q, max_iters=3, hints=HINTS, skills=[])
    print("\n==== 结果 ====")
    print("最终 SQL:", sql)
    for i, o in enumerate(obs, 1):
        print(f"  第 {i} 轮: ok={o.get('ok')} rows={o.get('rows')} "
              f"err={str(o.get('error') or '')[:120]}")


if __name__ == "__main__":
    asyncio.run(main())
