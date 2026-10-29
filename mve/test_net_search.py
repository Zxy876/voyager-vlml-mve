#!/usr/bin/env python3
"""搜索后端链的测试（2026-10-29 修复「页面里解析不出结果」盲点）。

背景（当天实测）：智谱 web-search-pro 对天气/实时类 query 返回空
search_result（大多数 query 正常），错误被 DDG 的报错覆盖，最后只看到
「页面里解析不出结果（可能被限流）」，智谱为什么空完全没交代。
修复：① 后端链补 Bing 兜底（VPS 实测 200 稳定、b_algo 可解析）；
     ② 全灭时报错列全链错误，不再吞智谱的。

跑法：python mve/test_net_search.py（服务器无 pytest，同 test_*.py 口径）。
"""

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


_BING_RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>北京天气预报</title><link>https://www.weather.com.cn/bj.shtml</link>
<description>今天北京晴转多云，气温 8~16 ℃。</description></item>
<item><title>第二条</title><link>http://www.nmc.cn/bj</link>
<description>另一条摘要</description></item>
</channel></rss>"""

_BING_HTML_ZH = """<html><body><ol>
<li class="b_algo"><h2><a href="https://weather.example.cn/bj">北京天气预报</a></h2>
<div class="b_caption"><p>今天北京晴转多云，气温 8~16 ℃。</p></div></li>
<li class="b_algo"><h2><a href="https://www.example.com/x?a=1&amp;b=2">示例二</a></h2>
<div class="b_caption"><p>第二条摘要。</p></div></li>
</ol></body></html>"""

_BING_HTML_CK = """<html><body><ol>
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;p=xx&amp;u=a1aHR0cHM6Ly9yZWFsLmV4YW1wbGUuY24vcGFnZQ==&amp;ntb=1">跳转形态</a></h2></li>
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;p=yy">解不开 u= 的</a></h2></li>
</ol></body></html>"""


def main() -> int:
    import base64
    import net_tools

    print("== 1. _parse_bing：明文链接形态（setmkt=zh-CN）==")
    r = net_tools._parse_bing(_BING_HTML_ZH)
    check("解析出 2 条", len(r) == 2, str(len(r)))
    check("标题去标签", r and r[0]["title"] == "北京天气预报", r[0]["title"] if r else "")
    check("链接 &amp; 还原", r and r[1]["url"] == "https://www.example.com/x?a=1&b=2",
          r[1]["url"] if len(r) > 1 else "")
    check("snippet 取到", r and "8~16" in r[0]["snippet"], r[0]["snippet"][:40] if r else "")

    print("== 1b. _parse_bing_rss：RSS 形态（首选，明文链接+摘要）==")
    r0 = net_tools._parse_bing_rss(_BING_RSS)
    check("解析出 2 条", len(r0) == 2, str(len(r0)))
    check("明文链接", r0 and r0[0]["url"] == "https://www.weather.com.cn/bj.shtml",
          r0[0]["url"] if r0 else "")
    check("snippet 取到", r0 and "8~16" in r0[0]["snippet"], r0[0]["snippet"][:40] if r0 else "")

    print("== 2. _parse_bing：ck/a 跳转形态 ==")
    want = base64.urlsafe_b64decode("aHR0cHM6Ly9yZWFsLmV4YW1wbGUuY24vcGFnZQ==").decode()
    r2 = net_tools._parse_bing(_BING_HTML_CK)
    check("u=a1<base64> 解出真实 URL", r2 and r2[0]["url"] == want,
          r2[0]["url"] if r2 else "")
    check("解不开 u= 的丢弃", len(r2) == 1, str(len(r2)))

    print("== 3. _search_once：后端链与全链报错 ==")
    _orig_zhipu, _orig_get = net_tools._zhipu_search, net_tools._http_get
    try:
        # 3a. 智谱成功 → 不碰 Bing/DDG
        calls = []

        def _ok_zhipu(q, n):
            calls.append("zhipu")
            return [{"title": "t", "url": "https://a.example/", "snippet": "s"}]

        net_tools._zhipu_search = _ok_zhipu
        out = net_tools._search_once("q")
        check("智谱成功直接返回", len(out) == 1 and calls == ["zhipu"])

        # 3b. 智谱空 → **DDG 接住**（2026-10-29 顺序调整：DDG 提到 Bing 之前，
        #     因为实测 Bing 的中文市场会把 "mve" 类 query 解析成「扫雷游戏」）
        calls.clear()

        def _empty_zhipu(q, n):
            calls.append("zhipu")
            raise RuntimeError("智谱 web-search-pro 没返回 search_result 条目")

        _DDG_HIT = ('<a class="result__a" href="https://www.weather.com.cn/bj.shtml">'
                    '北京天气</a>')

        def _ddg_hit(url, timeout=10):
            calls.append(url.split("/")[2] if "://" in url else url)
            if "duckduckgo.com" in url:
                if "html" in url:
                    return 200, _DDG_HIT.encode()
                raise AssertionError("html 版已接住，不该再请求 lite：" + url[:60])
            raise AssertionError("不该走到 " + url[:60])

        net_tools._zhipu_search = _empty_zhipu
        net_tools._http_get = _ddg_hit
        out = net_tools._search_once("北京今天天气")
        check("智谱空 → DDG 接住（顺序：智谱→DDG→Bing）",
              len(out) == 1 and calls[:1] == ["zhipu"]
              and "duckduckgo" in calls[1], str(calls))

        # 3b'. DDG 也没结果 → 落到 Bing（RSS 优先）
        calls.clear()

        def _ddg_empty_bing_hit(url, timeout=10):
            calls.append(url.split("/")[2] if "://" in url else url)
            if "duckduckgo.com" in url:
                return 200, b"<html><body>No results.</body></html>"
            if "bing.com" in url:
                if "format=rss" in url:
                    return 200, _BING_RSS.encode()
                raise AssertionError("RSS 已接住，不该再请求 HTML：" + url[:60])
            raise AssertionError("不该走到 " + url[:60])

        net_tools._http_get = _ddg_empty_bing_hit
        out = net_tools._search_once("北京今天天气")
        # 注意只剩 1 条：_BING_RSS 第 2 条（「第二条」/「另一条摘要」）与 query
        # 零交集，被相关性闸筛掉 —— 这正是闸该有的样子。
        check("DDG 空 → 落 Bing(rss)（无关条目被闸筛掉）", len(out) == 1
              and any("bing.com" in c for c in calls), str(calls))

        # 3c. 全灭 → 报错列全链（智谱错误不被覆盖）
        def _all_fail(url, timeout=10):
            raise OSError("HTTP Error 403: Forbidden")

        net_tools._http_get = _all_fail
        try:
            net_tools._search_once("x")
            check("全灭要抛错", False)
        except RuntimeError as e:
            msg = str(e)
            check("全灭要抛错", "后端全灭" in msg, msg[:60])
            check("报错带智谱的原因", "智谱" in msg and "没返回 search_result" in msg)
            check("报错带 Bing/DDG 的原因", "Bing" in msg and "DDG" in msg)

        # 3d. 智谱没配 key → errors 里如实说，仍走兜底
        def _no_backend(q, n):
            raise net_tools._NoBackend()

        net_tools._zhipu_search = _no_backend
        net_tools._http_get = _ddg_empty_bing_hit
        out = net_tools._search_once("北京今天天气")
        check("无 key 仍走兜底（DDG→Bing）", len(out) == 1)
    finally:
        net_tools._zhipu_search, net_tools._http_get = _orig_zhipu, _orig_get

    print("== 4. web_search：空 query / 退避结构 ==")
    try:
        net_tools.web_search("   ")
        check("空搜索词抛 ValueError", False)
    except ValueError:
        check("空搜索词抛 ValueError", True)
    src = open(net_tools.__file__, encoding="utf-8").read()
    check("保留退避重试（别撞限流原地重打）", "_RETRY_WAITS" in src)
    check("保留节流", "_throttle" in src)

    print("== 5. 相关性闸：整组跑偏的搜索结果不许充数 ==")
    # 2026-10-29 生产实测原样（query → 三个后端）：
    #   智谱 → 空；Bing(rss, zh-CN) → 「扫雷游戏网页版 - Minesweeper」×3；
    #   DDG → GitHub Zxy876/voyager-vlml-mve（正确）
    # 原来是「谁先解析出条目谁赢」，扫雷被原样交给模型 → 判官只能说材料里没这项目。
    _q = "voyager vlml mve github"
    _junk = [{"title": "扫雷游戏网页版 - Minesweeper", "url": "http://www.minesweeper.cn/",
              "snippet": "扫雷"},
             {"title": "扫雷网页版 - 扫雷网", "url": "https://www.saoleiwang.com/",
              "snippet": ""}]
    _good = [{"title": "GitHub - Zxy876/voyager-vlml-mve",
              "url": "https://github.com/Zxy876/voyager-vlml-mve", "snippet": ""}]
    kept, dropped = net_tools._split_relevant(_q, _junk)
    check("扫雷那一组被整组挡掉（零交集）", kept == [] and dropped == 2, str(kept))
    kept, dropped = net_tools._split_relevant(_q, _good)
    check("真结果放行", len(kept) == 1 and dropped == 0)
    kept, _ = net_tools._split_relevant("今天天气如何",
                                        [{"title": "北京天气", "url": "https://weather.com.cn/",
                                          "snippet": "今天天气晴"}])
    check("纯中文 query 用 2-gram 判（别把天气题误杀）", len(kept) == 1)
    check("URL 碎片不算命中（com/www 之类）",
          "com" not in net_tools._query_terms("https://github.com/a/b")
          and "github.com" in net_tools._query_terms("https://github.com/a/b"))
    # 全链都跑偏 → 如实报「没搜到相关内容」，不拿无关页面充数
    _oz, _og = net_tools._zhipu_search, net_tools._http_get
    try:
        net_tools._zhipu_search = lambda q, n: [
            {"title": "扫雷", "url": "http://www.minesweeper.cn/", "snippet": ""}]

        def _junk_all(url, timeout=10):
            if "duckduckgo.com" in url:
                return 200, ('<a class="result__a" href="https://www.saoleiwang.com/">'
                             '扫雷网页版</a>').encode()
            return 200, _BING_RSS.encode()   # 天气 RSS，与 voyager 无关
        net_tools._http_get = _junk_all
        try:
            net_tools._search_once(_q)
            check("全链跑偏时要抛错（不拿无关页面充数）", False)
        except RuntimeError as e:
            check("全链跑偏时要抛错（不拿无关页面充数）",
                  "全与问题无关" in str(e), str(e)[:120])
    finally:
        net_tools._zhipu_search, net_tools._http_get = _oz, _og

    print(f"\n通过 {PASS} / 挂 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
