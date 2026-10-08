#!/usr/bin/env python3
"""vlr.gg 抓取客户端 —— GRID 过期之后接的**公开数据源**（无需 API key）。

为什么要这个模块
----------------
原来 MCP 背后的库（`vlml/data/vlml_events.duckdb`）是 **GRID** 的事件切片，
GRID 要付费 key，过期就没得下。vlr.gg 是公开站点，实测 HTTP 200、无需注册：
    赛事列表  https://www.vlr.gg/events
    赛事赛程  https://www.vlr.gg/event/matches/{event_id}
    比赛详情  https://www.vlr.gg/{match_id}/{slug}

⚠️ 与 GRID 的能力差（必须如实说，不能装作等价）
--------------------------------------------
GRID 给的是**逐事件流**（谁在什么坐标用什么枪打了谁、装备净值、技能），
vlr.gg 只有**回合级 + 聚合级**：

| VLML 表 | vlr.gg 能不能填 | 说明 |
|---|---|---|
| `series` | ✅ 能 | 赛事名 / 年份 / 两队 / 胜者 / 开赛时间 |
| `games` | ✅ 能 | 每张图的图名、比分、总回合数 |
| `rounds` | ✅ 能 | 每回合**胜者 + 结束原因**（elim/boom/defuse/time，与 `end_reason` 对齐） |
| `base_events` | ⚠️ 只有回合级事件 | **没有** kill / ability / damage / 坐标 / 装备净值 |
| 选手统计 | ✅ 聚合级（另表） | K/D/A、ACS、ADR、KAST、HS%、**FK/FD 首杀首死** —— 落 `ext_player_game_stats` |

所以换这个源之后：回合/赛况/地图/赛事层分析照跑，**逐击杀类分析没有数据**
（首血只能用聚合的 FK/FD，没有"这回合谁先开的枪"）。

为什么不用 bs4
--------------
服务器上的 `.venv` 不一定装了 bs4。这里全部用标准库（urllib + re）解析，
换机器/换环境不用装包 —— vlr 的 HTML 结构规整，正则够用（已实测）。

礼貌抓取
--------
每个请求之间默认睡 1.2 秒，带正常 UA。vlr.gg 没有任何 rate limit 声明，
但这是人家的站点，别把人家打挂（也别把自己封了）。
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from typing import Any

BASE = "https://www.vlr.gg"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
DEFAULT_SLEEP = 1.2

# vlr 的回合结束图标 → VLML `rounds.end_reason`（与 db_loader.infer_end_reason 同义）
REASON_MAP = {
    "elim": "eliminated",
    "boom": "detonated",
    "defuse": "defused",
    "time": "time",
}


class FetchError(RuntimeError):
    """抓不到 / 状态码不对。"""


def _get(path: str, *, sleep: float = DEFAULT_SLEEP,
         retries: int = 2) -> str:
    """GET 一个 vlr 页面，返回文本。带限速与重试。"""
    url = path if path.startswith("http") else f"{BASE}{path}"
    last = ""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read().decode("utf-8", errors="ignore")
            time.sleep(sleep)
            return body
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code in (403, 429):
                time.sleep(sleep * 3)
                continue
        except Exception as exc:                      # 超时 / DNS / 连接重置
            last = f"{type(exc).__name__}: {exc}"[:120]
        time.sleep(sleep)
    raise FetchError(f"抓不到 {url}：{last}")


def _txt(html: str, pattern: str, group: int = 1, default: str = "",
         flags: int = 0) -> str:
    m = re.search(pattern, html, flags)
    return (m.group(group).strip() if m else default)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


# --------------------------------------------------------------------------
# 发现：赛事 / 赛程
# --------------------------------------------------------------------------
def list_events(*, sleep: float = DEFAULT_SLEEP) -> list[dict[str, Any]]:
    """当前站点上的赛事（`/events`）。不需要 key。"""
    html = _get("/events", sleep=sleep)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in re.finditer(r'href="/event/(\d+)/([a-z0-9\-]+)"', html):
        eid, slug = m.group(1), m.group(2)
        if eid in seen:
            continue
        seen.add(eid)
        out.append({"event_id": eid, "slug": slug,
                    "name": slug.replace("-", " ").title()})
    return out


def list_matches(event_id: str, *, limit: int = 0,
                 sleep: float = DEFAULT_SLEEP) -> list[dict[str, str]]:
    """某赛事下的比赛（`?series_id=all` 拿全部，不只是最近一周）。"""
    html = _get(f"/event/matches/{event_id}/?series_id=all", sleep=sleep)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in re.finditer(r'href="/(\d{6,8})/([a-z0-9\-]+)"', html):
        mid, slug = m.group(1), m.group(2)
        if mid in seen:
            continue
        seen.add(mid)
        out.append({"match_id": mid, "slug": slug,
                    "path": f"/{mid}/{slug}"})
        if limit and len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# 比赛详情
# --------------------------------------------------------------------------
def _slice_div(html: str, start: int) -> str:
    """从 `html[start:]` 的那个 `<div ...>` 起，返回**配对闭合**的完整子串。

    为什么不能用正则：vlr 的块里 div 是**嵌套**的（`ovw-row` 里套 `ovw-cell`、
    `ovw-cell` 里又套 `ovw-player`），非贪婪正则会在第一个 `</div>` 就截断
    —— 实测图 2 只解析出 1 个回合（实际 24 个）、选手统计整张表丢空。
    这里老实按 `<div`/`</div>` 计数配对，代价是多写几行。
    """
    depth = 0
    i = start
    n = len(html)
    pat = re.compile(r"<(/?)div\b")
    while i < n:
        m = pat.search(html, i)
        if not m:
            break
        if m.group(1) == "/":
            depth -= 1
            if depth == 0:
                end = m.end()
                # 正则只吃到 "</div"，要把那个 ">" 也吃掉 —— 否则残留的
                # "</div" 没有右尖括号，`_clean` 的去标签正则匹配不到它，
                # 会当成正文留下（实测选手名变成 "Neon LEV </div"）。
                if end < n and html[end] == ">":
                    end += 1
                return html[start:end]
        else:
            depth += 1
        i = m.end()
    return html[start:n]


def _blocks(html: str, marker: str) -> list[str]:
    """找出所有 class 以 `marker` 开头的 div，返回各自的完整子串。"""
    out: list[str] = []
    for m in re.finditer(r'<div class="%s' % re.escape(marker), html):
        out.append(_slice_div(html, m.start()))
    return out


def _game_blocks(html: str) -> list[tuple[str, str]]:
    """把每图（`vm-stats-game`）的 HTML 切出来 → [(game_id, block_html)]。"""
    out: list[tuple[str, str]] = []
    for m in re.finditer(
            r'<div class="vm-stats-game\s*?"[^>]*data-game-id="(\d+)"', html):
        out.append((m.group(1), _slice_div(html, m.start())))
    return out


def _map_name(html: str, game_id: str, block: str = "") -> str:
    """图名：优先从图块内的 `.map` 取（<div class="map">Haven</div>），
    取不到再退到 gamesnav 的链接文本（形如 "1 Haven"，要去掉前导序号）。"""
    if block:
        m = re.search(r'<div class="map[^"]*">\s*(.*?)\s*</div>', block, re.S)
        if m:
            txt = _clean(m.group(1))
            # 文本形如 "Haven PICK" —— 只留图名，去掉 PICK/BAN/decider 等标记
            first = txt.split()[0] if txt else ""
            if first and first.lower() not in ("pick", "ban", "decider", "maps"):
                return first
    m = re.search(r'data-game-id="%s"[^>]*>(.*?)</a>' % re.escape(game_id),
                  html, re.S)
    if not m:
        return ""
    txt = _clean(m.group(1))
    # 文本形如 "1 Haven"（序号 + 图名），去掉前导序号
    return re.sub(r"^\d+\s+", "", txt).strip()


def _rounds(block: str, teams: list[str]) -> list[dict[str, Any]]:
    """回合序列：`vlr-rounds-row-col` 每格一回合，格内两个 `rnd-sq`（两队）。

    赢家 = 带 `mod-win` 的那格；结束原因 = 格内图标名（elim/boom/defuse/time）。
    """
    out: list[dict[str, Any]] = []
    for col in _blocks(block, "vlr-rounds-row-col"):
        num = re.search(r'class="rnd-num">(\d+)<', col)
        if not num:
            continue
        score_m = re.search(r'title="([^"]*)"', col)
        sqs = _blocks(col, "rnd-sq")
        winner_idx = None
        reason = ""
        for i, sq in enumerate(sqs):
            if "mod-win" in (re.search(r'class="rnd-sq([^"]*)"', sq).group(1)
                             if re.search(r'class="rnd-sq([^"]*)"', sq) else ""):
                winner_idx = i
                img = re.search(r"/round/([a-z_]+)\.webp", sq)
                reason = REASON_MAP.get(img.group(1), img.group(1)) if img else ""
                break
        if winner_idx is None or winner_idx >= len(teams):
            continue
        out.append({
            "round_number": int(num.group(1)),
            "winner": teams[winner_idx],
            "loser": teams[1 - winner_idx],
            "end_reason": reason or None,
            "score": (score_m.group(1) if score_m else None),
        })
    out.sort(key=lambda r: r["round_number"])
    return out


def _player_stats(block: str, map_name: str,
                  teams: list[str]) -> list[dict[str, Any]]:
    """每图的选手统计（`ovw-table`，每队一张表）。

    数值是三段式（All / Attack / Defend 合一），取**第一个数**＝全部回合。
    """
    out: list[dict[str, Any]] = []
    for tm in re.finditer(r'<div class="ovw-table"', block):
        table = _slice_div(block, tm.start())
        # 队名：表格前面最近的 `.team` 块（LEV / FUR 这类短标签）
        tag_m = re.findall(r'<div class="team">(.*?)</div>', block[:tm.start()], re.S)
        tag = _clean(tag_m[-1]) if tag_m else ""
        for rm in re.finditer(r'<div class="ovw-row"', table):
            row = _slice_div(table, rm.start())
            if "mod-head" in row:
                continue
            cells = [_clean(_slice_div(row, c.start()))
                     for c in re.finditer(r'<div class="ovw-cell', row)]
            if len(cells) < 10:
                continue
            name_raw = _clean(cells[0])
            if not name_raw:
                continue
            # 形如 "Neon LEV" —— 末个 token 是队标签，用来把队员归队
            parts = name_raw.split()
            team_tag = parts[-1] if len(parts) > 1 else tag
            player = " ".join(parts[:-1]) if len(parts) > 1 else name_raw
            if not player:
                continue

            def num(idx: int) -> str:
                """取第 idx 个数值列的第一个数（All）。"""
                if idx >= len(cells):
                    return ""
                vals = _clean(cells[idx]).replace("%", "").split()
                return vals[0] if vals else ""

            # 列序（vlr overview）：player | agents | R | ACS | K/D/A | +/- |
            #                       KAST | ADR | HS% | FK | FD | +/-
            kda = _clean(cells[4]) if len(cells) > 4 else ""
            kda_parts = [p.split() for p in kda.split("/")]
            k = kda_parts[0][0] if kda_parts and kda_parts[0] else ""
            d = kda_parts[1][0] if len(kda_parts) > 1 and kda_parts[1] else ""
            a = kda_parts[2][0] if len(kda_parts) > 2 and kda_parts[2] else ""
            out.append({
                "player_name": player, "team_tag": team_tag,
                "map_name": map_name,
                "rating": num(2), "acs": num(3),
                "kills": k, "deaths": d, "assists": a,
                "kast": num(6), "adr": num(7), "hs_pct": num(8),
                "fk": num(9), "fd": num(10),
            })
    return out


def fetch_match(path: str, *, sleep: float = DEFAULT_SLEEP) -> dict[str, Any]:
    """抓一场比赛 → 结构化字典。**只填 vlr 真有的字段，缺的就是缺的**。"""
    html = _get(path, sleep=sleep)

    # 队名：`<div class="wf-title-med ...">` —— 注意第二个队的 class 常常
    # **不带** `mod-single`（实测 ENVY vs Evil Geniuses 这场就是），
    # 写死 `wf-title-med mod-single` 会把第二支队解析成空。
    team_names = re.findall(r'<div class="wf-title-med[^"]*">\s*(.*?)\s*</div>',
                            html, re.S)
    teams = [_clean(t) for t in team_names[:2]]
    while len(teams) < 2:
        teams.append("")

    # 比分：⚠️ **页面顺序不固定** —— 败者的分可能排在前面（实测 ENVY 0:2 EG
    # 就是 loser span 在前）。所以不能按"第一个数字是胜者"解，要看
    # winner / loser 两个 span 在**计分区块内的先后顺序**：谁在前 = 左边的队。
    score_inner = ""
    sm = re.search(r'<div class="match-header-vs-score">\s*<div class="sp-hide">'
                   r'(.*?)</div>', html, re.S)
    if sm:
        score_inner = sm.group(1)
    wm = re.search(r'match-header-vs-score-winner">\s*(\d+)', score_inner)
    lm = re.search(r'match-header-vs-score-loser">\s*(\d+)', score_inner)
    winner_name = ""
    score_text = ""
    if wm and lm:
        w_left = (score_inner.find("match-header-vs-score-winner")
                  < score_inner.find("match-header-vs-score-loser"))
        winner_name = teams[0] if w_left else teams[1]
        left, right = (wm.group(1), lm.group(1)) if w_left else (lm.group(1), wm.group(1))
        score_text = f"{left}-{right}"

    tournament = _clean(_txt(
        html, r'class="match-header-event".*?<div style="font-weight: 700;">'
              r'\s*(.*?)\s*</div>', flags=re.S))
    started_at = _txt(html, r'data-utc-ts="([\d\-]{10} [\d:]{8})"')
    year = int(started_at[:4]) if started_at[:4].isdigit() else 0

    games: list[dict[str, Any]] = []
    players: list[dict[str, Any]] = []
    for gid, block in _game_blocks(html):
        map_name = _map_name(html, gid, block) or f"game-{gid}"
        rnds = _rounds(block, teams)
        if not rnds:
            continue
        # 图比分 = 最后一个回合的比分（title 形如 "13-11"）
        last_score = rnds[-1].get("score") or ""
        g_score = last_score.split("-") if "-" in last_score else []
        g_winner = ""
        if len(g_score) == 2 and g_score[0].isdigit() and g_score[1].isdigit():
            g_winner = (teams[0] if int(g_score[0]) > int(g_score[1])
                        else teams[1])
        games.append({
            "game_id": gid,
            "sequence": len(games) + 1,
            "map_name": map_name,
            "team1": teams[0], "team2": teams[1],
            "score": last_score or None,
            "winner": g_winner or None,
            "total_rounds": len(rnds),
            "rounds": rnds,
        })
        players += _player_stats(block, map_name, teams)

    return {
        "series_id": (path.strip("/").split("/")[0] or ""),
        "source": "vlr.gg",
        "url": (path if path.startswith("http") else f"{BASE}{path}"),
        "tournament_name": tournament,
        "tournament_slug": (path.strip("/").split("/")[-1]
                            if "/" in path.strip("/") else ""),
        "year": year,
        "started_at": started_at.replace(" ", "T") if started_at else "",
        "teams": teams,
        "score": score_text,
        "winner": winner_name,
        "games": games,
        "player_stats": players,
    }
