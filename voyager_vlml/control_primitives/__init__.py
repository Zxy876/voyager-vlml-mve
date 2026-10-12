"""control_primitives：原版是 Minecraft 的 JS 原语（mineBlock / craftItem / …），
这里换成 **VLML 同构的 SQL 原语**（用户指令②「把 js 代码指定 sql」）。

原版做法：control_primitives/*.js 的源码被拼进 action 的 system prompt，
模型照着这些原语写 JS。这里同构：把 SQL 原语（query_sql + 报告工具）的签名与
用法拼进 prompt，模型照着写 **SQL**。
"""

from .sql_primitives import PRIMITIVES, render_primitives

__all__ = ["PRIMITIVES", "render_primitives"]
