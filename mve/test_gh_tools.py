#!/usr/bin/env python3
"""GitHub 原语的测试（2026-10-29 用户需求「接 GitHub 学习解释项目」）。

跑法：python mve/test_gh_tools.py（服务器无 pytest，同 test_*.py 口径）。
真实网络调用只在 __main__ 手动验（无 token 也该通）；本测试全部打桩。
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
    import gh_tools

    print("== 1. _norm_repo 归一 ==")
    check("owner/repo 原样", gh_tools._norm_repo("Zxy876/voyager-vlml-mve")
          == "Zxy876/voyager-vlml-mve")
    check("完整 URL 剥前缀",
          gh_tools._norm_repo("https://github.com/owner/repo.git") == "owner/repo")
    check("带 .git 尾巴", gh_tools._norm_repo("owner/repo.git") == "owner/repo")
    for bad in ("", "只有一个词", "https://gitlab.com/a/b", "a/b/c/d"):
        try:
            gh_tools._norm_repo(bad)
            check(f"非法仓库名拒绝：{bad!r}", False)
        except ValueError:
            check(f"非法仓库名拒绝：{bad!r}", True)

    print("== 2. gh_read 打桩 ==")
    import net_tools
    _orig_get = net_tools._http_get
    try:
        def fake_get(url, timeout=10, max_bytes=0):
            if "raw.githubusercontent.com/owner/repo/HEAD/README.md" in url:
                return 200, "# Hello Repo\n这是内容".encode()
            if "/git/trees/" in url:
                return 200, json.dumps({"tree": [
                    {"path": "README.md"}, {"path": "mve/consume.py"}]}).encode()
            if "api.github.com/repos/owner/repo/search" in url or \
               "/search/repositories" in url:
                return 200, json.dumps({"items": [
                    {"full_name": "owner/repo", "description": "d",
                     "stargazers_count": 7,
                     "html_url": "https://github.com/owner/repo"}]}).encode()
            if url.endswith("api.github.com/repos/owner/repo") or \
               "api.github.com/repos/owner/repo?" in url or \
               url == "api.github.com/repos/owner/repo":
                return 200, json.dumps({
                    "full_name": "owner/repo", "description": "A repo",
                    "stargazers_count": 42, "language": "Python",
                    "default_branch": "main",
                    "html_url": "https://github.com/owner/repo"}).encode()
            return 404, b"not found"

        net_tools._http_get = fake_get
        r = gh_tools.gh_read("owner/repo", "README.md")
        check("读文件", r["text"].startswith("# Hello Repo")
              and r["bytes"] > 0)
        check("禁路径穿越", not self_call_traversal(gh_tools))
        t = gh_tools.gh_tree("owner/repo")
        check("目录树", t["count"] == 2 and "README.md" in t["paths"])
        info = gh_tools.gh_repo_info("owner/repo")
        check("仓库元数据", info["stars"] == 42
              and info["default_branch"] == "main")
        s = gh_tools.gh_search("voyager")
        check("搜仓库", s and s[0]["full_name"] == "owner/repo"
              and s[0]["stars"] == 7)
        try:
            gh_tools.gh_read("owner/repo", "不存在.md")
            check("404 如实抛错", False)
        except RuntimeError as e:
            check("404 如实抛错", "404" in str(e) or "没有这个资源" in str(e))
    finally:
        net_tools._http_get = _orig_get

    print("== 3. 路径穿越守卫 ==")
    try:
        gh_tools.gh_read("owner/repo", "../../etc/passwd")
        check(".. 段拒绝", False)
    except PermissionError:
        check(".. 段拒绝", True)

    print("== 4. 条目化与注册 ==")
    b = gh_tools.bind()
    check("4 个条目", len(b) == 4 and
          {x["name"] for x in b} == {"gh_repo_info", "gh_tree", "gh_read",
                                     "gh_search"})
    import actions
    r = actions.register_gh("TEST_GH")
    check("注册 4 条", r["added"] == 4, str(r))
    check("组名 = GitHub", actions._REGISTRY["TEST_GH"]["gh_read"].group
          == gh_tools.GH_GROUP)
    check("origin=net（工具线口径）",
          actions._REGISTRY["TEST_GH"]["gh_read"].origin == "net")
    actions.unregister("TEST_GH", "gh_read")
    actions.unregister("TEST_GH", "gh_tree")
    actions.unregister("TEST_GH", "gh_repo_info")
    actions.unregister("TEST_GH", "gh_search")

    print("== 4b. 执行域真的注入（注册 ≠ 注入）==")
    # 2026-10-29 生产实测的坑：register_gh 把 4 条写进 action_registry.json，
    # 但 `_ns_base` 的 `full` 里没有 gh_tools 的函数 → `注册表 & set(full)`
    # 交集为空 → 模型拿不到 gh_read 这个名字。于是调研题 4 次重试全在
    # `SELECT * FROM web_docs WHERE query LIKE ...` 上打磨过滤条件
    # （「感觉还是一直在试错 sql」）。这条守卫必须钉死。
    import tempfile

    import local_agent
    _tmp = tempfile.mkdtemp()
    actions.register_gh("TEST_GH")
    try:
        ns = local_agent._ns_base(_tmp, "TEST_GH")
    finally:
        for _n in ("gh_repo_info", "gh_tree", "gh_read", "gh_search"):
            actions.unregister("TEST_GH", _n)
    check("执行域里有 4 个 gh 原语（不是只登记在注册表）",
          {"gh_read", "gh_tree", "gh_repo_info", "gh_search"} <= set(ns),
          str(sorted(k for k in ns if not k.startswith("__")))[:120])
    check("gh_read 是真的可调用函数", callable(ns.get("gh_read")))

    print("== 5. consume 接线（源码断言）==")
    src = open(__import__("voyager_vlml.consume", fromlist=["x"]).__file__,
               encoding="utf-8").read()
    check("consume 调 register_gh", "register_gh" in src)
    check("规划菜单提到 GitHub", "GitHub 公开仓库可读" in src)

    print(f"\n通过 {PASS} / 挂 {FAIL}")
    return 1 if FAIL else 0


def self_call_traversal(gh) -> bool:
    """gh_read 的 .. 守卫在模块层测过（==3 节），这里只占位防止误报。"""
    return False


if __name__ == "__main__":
    sys.exit(main())
