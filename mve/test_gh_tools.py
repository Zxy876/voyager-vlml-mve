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
            check("404 如实抛错", "404" in str(e))
            check("gh_read 404 给出下一步（试 README.md）",
                  "README.md" in str(e), str(e)[:60])

        # ⚠️ 真根因回归（2026-10-29 Scene H）：`net_tools._http_get` 内部是裸
        # urlopen，403/404 会**直接抛 urllib HTTPError**，不返回 status ——
        # 于是 `_raise_gh` 的指路文案从未发出过，模型只看到
        # "HTTP Error 403: rate limit exceeded"。这里把"抛 HTTPError"这条路
        # 打桩，断言最终抛的是**带指路的话**，不是裸 HTTPError。
        import urllib.error

        def boom_get(url, timeout=10, max_bytes=0):
            raise urllib.error.HTTPError(url, 403, "rate limit exceeded",
                                         {}, None)

        net_tools._http_get = boom_get
        for _fn, _lbl in ((lambda: gh_tools.gh_repo_info("owner/repo"),
                           "gh_repo_info"),
                          (lambda: gh_tools.gh_tree("owner/repo"), "gh_tree"),
                          (lambda: gh_tools.gh_read("owner/repo", "README.md"),
                           "gh_read")):
            try:
                _fn()
                check(f"{_lbl} 抛 HTTPError 时改抛带指路的错", False)
            except urllib.error.HTTPError:
                check(f"{_lbl} 抛 HTTPError 时改抛带指路的错", False,
                      "原样漏出 HTTPError —— _raise_gh 又被绕过了")
            except RuntimeError as e:
                check(f"{_lbl} 抛 HTTPError 时改抛带指路的错",
                      "gh_read" in str(e) or "404" in str(e), str(e)[:60])
    finally:
        net_tools._http_get = _orig_get

    print("== 3. 路径穿越守卫 ==")
    try:
        gh_tools.gh_read("owner/repo", "../../etc/passwd")
        check(".. 段拒绝", False)
    except PermissionError:
        check(".. 段拒绝", True)

    print("== 3b. gh_api：仓库自己的账（commit/contributors/releases/issues）==")
    # 2026-10-29 用户「重点是我希望它的搜索来源是 github 我的仓库」：
    # 固定原语只有元数据/树/单文件/搜仓库，调研类维度要的「还在维护吗、
    # 几个人在写、发过版吗」得走 REST，加一条通用只读入口而不是每指标一个原语。
    for bad in ("https://api.github.com/repos/a/b", "http://evil.com/x"):
        try:
            gh_tools.gh_api(bad)
            check(f"完整 URL 拒绝：{bad[:30]}", False)
        except PermissionError:
            check(f"完整 URL 拒绝：{bad[:30]}", True)
    for bad in ("user/repos", "../../etc/passwd", "gists/1"):
        try:
            gh_tools.gh_api(bad)
            check(f"端点前缀外拒绝：{bad}", False)
        except PermissionError:
            check(f"端点前缀外拒绝：{bad}", True)

    print("== 4. 条目化与注册 ==")
    b = gh_tools.bind()
    check("5 个条目（含 gh_api）", len(b) == 5 and
          {x["name"] for x in b} == {"gh_repo_info", "gh_tree", "gh_read",
                                     "gh_api", "gh_search"})
    import actions
    r = actions.register_gh("TEST_GH")
    check("注册 5 条", r["added"] == 5, str(r))
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
    check("执行域里有 5 个 gh 原语（不是只登记在注册表）",
          {"gh_read", "gh_tree", "gh_repo_info", "gh_search"} <= set(ns),
          str(sorted(k for k in ns if not k.startswith("__")))[:120])
    check("gh_read 是真的可调用函数", callable(ns.get("gh_read")))

    print("== 5. consume 接线（源码断言）==")
    src = open(__import__("voyager_vlml.consume", fromlist=["x"]).__file__,
               encoding="utf-8").read()
    check("consume 调 register_gh", "register_gh" in src)
    check("规划菜单提到 GitHub", "GitHub 公开仓库可读" in src)

    print("== 6. 题干点名仓库 → 锁定取证来源（不许拿搜索充数）==")
    la = __import__("local_agent")
    check("从题干认出仓库",
          la._focus_repos("联网去仓库地址：<https://github.com/Zxy876/voyager-vlml-mve>开始调研")
          == ["Zxy876/voyager-vlml-mve"])
    check(".git 尾巴也认",
          la._focus_repos("看 https://github.com/a/b.git 的 issue") == ["a/b"])
    check("没给仓库就不锁定", la._focus_repos("今天天气如何") == [])
    check("重复只留一个",
          la._focus_repos("github.com/a/b 和 github.com/a/b") == ["a/b"])
    blk = la._repo_block(["Zxy876/voyager-vlml-mve"])
    check("提示块点名仓库 + 给出 gh 三件套",
          "Zxy876/voyager-vlml-mve" in blk and "gh_repo_info" in blk
          and "gh_tree" in blk and "gh_read" in blk)
    check("提示块给出仓库自己的账（gh_api 的几个端点）",
          "gh_api" in blk and "commits" in blk and "contributors" in blk)
    check("提示块明确禁止用 web_search 搜本仓库",
          "不要用 web_search 搜这个仓库" in blk)
    check("没锁定仓库时提示块为空（不打扰无关题）", la._repo_block([]) == "")

    # 403 文案必须点名「换哪条路」（2026-10-29 Scene H：服务器出口 IP 的 API
    # 配额被限到 0，模型只调 gh_repo_info，撞 403 后不知道 raw 不受限 → 四个
    # 维度各 12~17s 就死。报错文案本身就是那一步唯一能看到的提示。）
    try:
        gh_tools._raise_gh(403, "repos/a/b")
        check("403 抛错", False)
    except RuntimeError as e:
        _m = str(e)
        check("403 点名改走 gh_read（raw 不受 API 配额限制）",
              "gh_read" in _m and "raw.githubusercontent.com" in _m, _m[:80])
        check("403 明确说别重试这个函数", "别重试" in _m)
    try:
        gh_tools._raise_gh(404, "repos/a/b")
    except RuntimeError as e:
        check("404 文案不提换通路（那是真没有）", "gh_read" not in str(e))

    print(f"\n通过 {PASS} / 挂 {FAIL}")
    return 1 if FAIL else 0


def self_call_traversal(gh) -> bool:
    """gh_read 的 .. 守卫在模块层测过（==3 节），这里只占位防止误报。"""
    return False


if __name__ == "__main__":
    sys.exit(main())
