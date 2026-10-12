"""VLML 执行环境（对应原版 voyager/env/ 的 mineflayer Minecraft 环境）。

原版里 env 是 Minecraft（mineflayer）：observation 是方块/实体/血量/背包，
action 是 JS 代码。这里换成 VLML MCP（DB / RIB / VLGG）：
observation 是「工具调用成败 + 取到几行 + 数据长什么样」，action 是 **SQL**。
"""
