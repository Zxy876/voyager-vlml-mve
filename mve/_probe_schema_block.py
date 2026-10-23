"""打印模型写 SQL 时真正看到的那份 schema（`schema_block` 的原样输出）。"""
import asyncio
import sys

sys.path.insert(0, ".")

from voyager_vlml.env.vlml_mcp import VLMLMCPEnv  # noqa: E402


async def main() -> None:
    env = VLMLMCPEnv()
    block = await env.schema_block()
    print(block)
    print("\n==== 检查 ====")
    print("含 first_kill ?", "first_kill" in block)
    print("含 ext_player_round_stats ?", "ext_player_round_stats" in block)
    print("字符数:", len(block))


if __name__ == "__main__":
    asyncio.run(main())
