#!/usr/bin/env python3
"""MCP 能力边界的测试（2026-10-29 用户问「MCP 挂进来的工具不受这套守卫管，怎么办」）。

三层边界，能在这侧管的都锁住：
  A. **协议形状**：`structuredContent` 必须是 dict —— 我们自己的 standin server
     曾把 `web_search` 的 list 直接塞进去，桥接层 pydantic 校验拒绝，整条结果变
     isError，模型连点 3 轮拿到同一串报错（H 实跑现场）。
  B. **子进程环境**：stdio server 不再继承面板的全部环境变量（含 API key）。
  C. **挂几个工具进来**：配置里的 expose / deny 白名单；以及「连续失败 ≥2 次
     的工具从菜单摘掉」——把「报错就换工具」从软约束变成硬事实。

跑法：python mve/test_mcp_boundary.py（服务器无 pytest，同 test_*.py 口径）。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    import mcp_gateway
    import mcp_neko_server
    import local_agent

    print("== A. structuredContent 必须是 dict（MCP 规范） ==")
    r = mcp_neko_server._format_result([{"title": "x", "url": "u"}])
    check("list 结果被包成对象（不是裸 list）",
          isinstance(r["structuredContent"], dict), str(type(r["structuredContent"])))
    check("包成 results 键（客户端 _mcp_listify 正好剥回列表）",
          isinstance(r["structuredContent"].get("results"), list))
    check("isError 不为真（不再被 SDK 校验拒掉）", r["isError"] is False)
    check("text 兜底仍是原始列表的 JSON",
          json.loads(r["content"][0]["text"])[0]["title"] == "x")
    r2 = mcp_neko_server._format_result({"a": 1})
    check("dict 结果原样进 structuredContent", r2["structuredContent"] == {"a": 1})
    r3 = mcp_neko_server._format_result("裸字符串")
    check("标量结果也有对象壳", isinstance(r3["structuredContent"], dict))
    r4 = mcp_neko_server._format_result({"__error__": "取不到"})
    check("错误仍是 isError + error 字段",
          r4["isError"] is True and "取不到" in r4["structuredContent"]["error"])

    print("== A2. 客户端侧：剥壳后模型拿到的是列表 ==")
    payload = {"result": {"content": r["content"], "isError": False,
                          "structuredContent": r["structuredContent"]}}
    got = local_agent._unwrap_mcp(payload)
    check("_unwrap_mcp 把 {results:[...]} 剥成列表",
          isinstance(got, list) and got[0]["title"] == "x", str(type(got)))
    bad = {"result": {"content": [], "isError": True,
                      "structuredContent": ["不是对象"]}}
    try:
        local_agent._unwrap_mcp(bad)
        check("isError 且 structuredContent 是 list 时不崩、如实抛错", False)
    except RuntimeError as e:
        check("isError 且 structuredContent 是 list 时不崩、如实抛错",
              "MCP 工具报错" in str(e), str(e))
    except Exception as e:  # AttributeError 就是把「工具报错」变成「执行崩了」
        check("isError 且 structuredContent 是 list 时不崩、如实抛错",
              False, f"{type(e).__name__}: {e}")

    print("== B. 子进程环境不继承面板的密钥 ==")
    os.environ["ZHIPU_API_KEY"] = "sk-should-not-leak"
    try:
        cfg = mcp_gateway.MCPServerConfig(name="t")
        env = mcp_gateway._child_env(cfg)
        check("ZHIPU_API_KEY 没传给 MCP server", "ZHIPU_API_KEY" not in env)
        check("PATH 保留（npx / 解释器要靠它）", "PATH" in env)
        check("HOME 保留（npx 缓存目录）", "HOME" in env)
        cfg2 = mcp_gateway.MCPServerConfig(
            name="t2", env={"SOME_TOKEN": "explicit"})
        check("配置里显式声明的变量照给（要什么自己写）",
              mcp_gateway._child_env(cfg2).get("SOME_TOKEN") == "explicit")
        os.environ["MVE_MCP_ENV_PASSTHROUGH"] = "1"
        try:
            full = mcp_gateway._child_env(cfg)
            check("逃生口 MVE_MCP_ENV_PASSTHROUGH=1 恢复旧行为（全继承）",
                  "ZHIPU_API_KEY" in full)
        finally:
            os.environ.pop("MVE_MCP_ENV_PASSTHROUGH", None)
    finally:
        os.environ.pop("ZHIPU_API_KEY", None)

    print("== C. 工具准入：expose 白名单 / deny 黑名单 ==")
    gw = mcp_gateway.MCPGateway()
    gw._configs = {
        "neko": mcp_gateway.MCPServerConfig(
            name="neko", expose=["web_fetch"]),
        "ddg": mcp_gateway.MCPServerConfig(
            name="ddg", deny=["fetch_content"]),
        "time": mcp_gateway.MCPServerConfig(name="time"),
    }
    gw._clients = {
        "neko": type("C", (), {"connected": True})(),
        "ddg": type("C", (), {"connected": True})(),
        "time": type("C", (), {"connected": True})(),
    }
    gw._clients["neko"].tools = [
        mcp_gateway.MCPTool(name="web_search", server_name="neko"),
        mcp_gateway.MCPTool(name="web_fetch", server_name="neko")]
    gw._clients["ddg"].tools = [
        mcp_gateway.MCPTool(name="search", server_name="ddg"),
        mcp_gateway.MCPTool(name="fetch_content", server_name="ddg")]
    gw._clients["time"].tools = [
        mcp_gateway.MCPTool(name="get_current_time", server_name="time")]
    ok, why = gw.tool_allowed("mcp_neko_web_search")
    check("expose 白名单外的不放行", not ok, why)
    ok, _ = gw.tool_allowed("mcp_neko_web_fetch")
    check("expose 白名单内的放行", ok)
    ok, why = gw.tool_allowed("mcp_ddg_fetch_content")
    check("deny 黑名单挡住", not ok, why)
    ok, _ = gw.tool_allowed("mcp_ddg_search")
    check("deny 之外照常放行", ok)
    ok, _ = gw.tool_allowed("mcp_time_get_current_time")
    check("两边都没配 = 全放行（老配置行为不变）", ok)

    print("== D. 连续失败的工具从菜单摘掉（硬事实，不是软约束） ==")
    local_agent._MCP_FAILS.clear()
    check("没失败过 → 不判死", local_agent._mcp_dead("mcp_neko_web_search") == "")
    local_agent._note_mcp_fail("mcp_neko_web_search",
                               "1 validation error for CallToolResult")
    check("失败 1 次还不判死（可能是偶发）",
          local_agent._mcp_dead("mcp_neko_web_search") == "")
    local_agent._note_mcp_fail("mcp_neko_web_search",
                               "1 validation error for CallToolResult")
    d = local_agent._mcp_dead("mcp_neko_web_search")
    check("失败 2 次 → 判死并带上原因", "连续失败 2 次" in d and "validation" in d, d)
    local_agent._MCP_FAILS.pop("mcp_neko_web_search", None)
    check("成功一次就清零（不是永久拉黑）",
          local_agent._mcp_dead("mcp_neko_web_search") == "")

    print(f"== 结果：{PASS} 过 / {FAIL} 挂 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
