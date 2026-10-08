#!/usr/bin/env python3
"""rib.gg 抓取客户端 —— 用来补 vlr.gg **缺的那一层**：逐回合 × 逐选手。

为什么还要第二个源
------------------
vlr.gg 只到「回合谁赢 + 怎么赢」和「整图聚合的选手统计」。rib.gg 的比赛页
（Next.js，数据内嵌在 RSC payload 里）给的是**每回合 × 每选手**：

    roundStats   [[{name, team:"A", side:"attack", agent:"Chamber", kills:0,
                    deaths:0, assists:1, damage:55, acs:55, hsPct:0,
                    alive:true, firstKill:false}, ...]]   ← 每回合 10 人
    roundEconomy [{A:{bank:100, loadout:4000, buyTier:"eco"}, B:{...}}, ...]
    economy      {pistolWonA:1, ecoTotalA:3, fullBuyWonA:11, ...}

于是 vlr 的两个洞补上了：**首血**（`firstKill` 标记）和**经济局**
（`bank` / `loadout` / `buyTier`）。

⚠️ 合规先说清楚（这是能不能做的前提）
--------------------------------------
rib.gg 的 `robots.txt`：`Allow: /`，只 `Disallow: /admin` 和 `/api/`。
**我们抓的是 `/matches/{id}` 的 HTML 页面，不在禁止列表里**（对 ClaudeBot /
GPTBot 同样 `Allow: /`）。所以这与"绕过访问控制"是两回事。
但仍按 vlr 同款礼貌抓取：1.2 秒一个请求、正常 UA。

⚠️ 仍然不能造的东西
--------------------
`firstKill` 只告诉我们**这回合谁拿了首杀**，没说**杀了谁**（payload 里没有
killer→victim 的配对）。所以**不造 `player-killed-player` 事件** —— 那等于
编造"谁杀了谁"。首血统计走 `ext_player_round_stats.first_kill` 这个布尔位。

解析方式
--------
Next.js 的 flight payload 不是合法 JSON 整体，但里面的数据片段是。做法：
把 `self.__next_f.push([1,"…"])` 全部拼起来，再用**括号配对**把需要的
对象/数组抠出来（要跳过字符串里的括号，不然会提前收尾）。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

BASE = "https://rib.gg"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
DEFAULT_SLEEP = 1.2

# rib 的 winType → VLML `rounds.end_reason`
WIN_TYPE_MAP = {
    "elimination": "eliminated",
    "defusal": "defused",
    "detonation": "detonated",
    "time": "time",
    "timeout": "time",
    "bomb_detonated": "detonated",
    "bomb_defused": "defused",
}


class FetchError(RuntimeError):
    """抓不到 / 状态码不对。"""


def _get(path: str, *, sleep: float = DEFAULT_SLEEP,
         retries: int = 2) -> str:
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
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"[:120]
        time.sleep(sleep)
    raise FetchError(f"抓不到 {url}：{last}")


def _payload(html: str) -> str:
    """把 Next.js 的 flight payload 拼成一个大字符串。

    每个 `self.__next_f.push([1,"…"])` 里是一段转义过的字符串，
    `json.loads` 还原后顺序拼接即可。
    """
    out: list[str] = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,\s*(".*?")\]\)', html, re.S):
        try:
            out.append(json.loads(m.group(1)))
        except Exception:
            continue
    return "".join(out)


def _grab(blob: str, key: str, opener: str) -> list[str]:
    """抠出 `"key":{...}` 或 `"key":[...]` 的完整片段（可多处）。

    必须**跳过字符串内部的括号**，否则遇到 `{"map":"Ascent (B)"}` 之类会提前收尾。
    """
    closer = "}" if opener == "{" else "]"
    out: list[str] = []
    # ⚠️ 别写成 `"%s":\%s` —— 那个前导反斜杠会让正则去找**字面反斜杠**再跟
    # 括号，结果什么都匹配不到（实测 initial/roundStats 全空）。
    for m in re.finditer(r'"%s":%s' % (re.escape(key), re.escape(opener)), blob):
        j = m.end() - 1
        depth, k = 0, j
        while k < len(blob):
            ch = blob[k]
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    break
            elif ch == '"':                       # 进入字符串：整段跳过
                k += 1
                while k < len(blob) and blob[k] != '"':
                    if blob[k] == "\\":
                        k += 1
                    k += 1
            k += 1
        out.append(blob[j:k + 1])
    return out


def _undef(v: Any) -> bool:
    """RSC 的空占位：`"$undefined"` / `"$"` 引用 / Python None 都算没有。

    实测踩过：没打的第三张图（Lotus 0-0）里 `roundStats` **不是空数组**，
    而是一串 `"$"` 引用（`{"A":{"bank":...}}` 的位置放着字符串 `"$"`）。
    不加这道判断就会把字符串当 dict 迭代 → 选手表被灌进垃圾行。
    """
    return v is None or (isinstance(v, str) and v.startswith("$"))


def _obj(blob: str, key: str) -> dict:
    """抠一个 `"key":{...}` 对象并 json 化；没有/是占位就返回 {}。"""
    for raw in _grab(blob, key, "{"):
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if isinstance(d, dict):
            return d
    return {}


def _teams_and_event(html: str) -> tuple[list[str], str]:
    """从 `<title>` 取队名与赛事名（**兜底用**，主路径是 `teamA`/`teamB`）。

    形如：`Team Vitality vs LOUD Live Score & Stats – Valorant Champions 2026`
    → 队 A = Team Vitality、队 B = LOUD、赛事 = Valorant Champions 2026。
    """
    teams: list[str] = []
    event = ""
    m = re.search(r'<title>(.*?)</title>', html, re.S)
    title = re.sub(r"\s+", " ", (m.group(1) if m else "")).strip()
    if " vs " in title:
        left, right = title.split(" vs ", 1)
        for sep in ("–", "—", " - "):
            if sep in right:
                right, event = right.split(sep, 1)
                break
        # "LOUD Live Score & Stats" → 去掉尾巴上的营销词
        right = re.sub(r"\s*(Live\s*Score|Stats|&).*$", "", right).strip(" -–")
        teams = [left.strip(), right.strip()]
        event = re.sub(r"\s*(Live\s*Score|Stats|&).*$", "", event).strip(" -–")
    return teams, event


def _team_names(blob: str, fallback: list[str]) -> list[str]:
    """payload 里的 `teamA` / `teamB` 对象 → 队名。

    实测：rib 同时给了 `teamA={"slug":"team-vitality","name":"Team Vitality",
    "shortName":"VIT",...}`，且 `scoreA` 与 A 同向（Vitality 2-0 → scoreA=2）。
    比从 title 里切字符串可靠得多（title 还带营销尾巴）。
    """
    out = []
    for key in ("teamA", "teamB"):
        t = _obj(blob, key)
        name = str(t.get("name") or t.get("shortName") or t.get("slug") or "")
        out.append(name.strip())
    if not any(out) and len(fallback) == 2:
        return [str(x) for x in fallback]
    return out


def _start_date(blob: str) -> str:
    """比赛开赛时间。rib 给的键叫 `startDate`（不是 startTime/scheduledAt）。

    取自 `"startDate":"2026-09-30T09:00:00+00:00"`。没取到返回空串 ——
    **不拿现在的时间冒充开赛时间**（会影响 `{year}` 目录与 series.start_time）。
    """
    m = re.search(r'"startDate":"([^"]+)"', blob)
    return m.group(1) if m else ""


def _agg_row(p: dict, team: str, map_name: str) -> dict[str, Any]:
    """rib 的 `statsA/statsB` 一行 → 与 vlr `player_stats` **同形**的一行。

    这样 `ext_player_game_stats` 一张表能吃两个源（列对齐、`source` 列区分）。
    rib 多出来的（`firstKills` = FK、`firstDeaths` = FD、`clutches`、`multiKills`、
    `opKills`）直接对上 vlr 也有 FK/FD；clutches 等塞进 `extras` 不另开列，
    免得两源列不齐。
    """
    return {
        "map_name": map_name, "player_name": str(p.get("name") or "").strip(),
        # rib 直接给了队（statsA→teamA），所以 `team_tag` 就填队全名，
        # `vlr_to_grid._team_of()` 拿同一份值去比对能原样命中 —— 两个源共用
        # 一条入库路径。⚠️ 别填 `p["slug"]`：那是**选手**的 slug（jamppi），
        # 不是队标签，填进去会被当成队名写进表里。
        "team_tag": team, "team_name": team,
        "player_slug": str(p.get("slug") or ""),
        "agent": str(p.get("agent") or ""),
        "rating": p.get("rating"), "acs": p.get("acs"),
        "kills": p.get("kills"), "deaths": p.get("deaths"),
        "assists": p.get("assists"), "kast": p.get("kast"),
        "adr": p.get("adr"), "hs_pct": p.get("hsPct"),
        "fk": p.get("firstKills"), "fd": p.get("firstDeaths"),
        "extras": {
            "plus_minus": p.get("plusMinus"),
            "headshots": p.get("headshots"),
            "clutches": p.get("clutches"),
            "clutch_wins_by_size": p.get("clutchWinsBySize"),
            "multi_kills": p.get("multiKills"),
            "op_kills": p.get("opKills"),
            "country": p.get("country"),
        },
    }


def fetch_match(path: str, *, sleep: float = DEFAULT_SLEEP) -> dict[str, Any]:
    """抓一场 rib 比赛 → 结构化字典。**只填 rib 真有的字段**。"""
    html = _get(path, sleep=sleep)
    blob = _payload(html)
    teams = _team_names(blob, [t for t in _teams_and_event(html)[0]])
    _, event = _teams_and_event(html)

    raw_initial = _grab(blob, "initial", "{")
    if not raw_initial:
        raise FetchError(f"{path}：payload 里没有比赛数据（页面结构变了？）")
    try:
        initial = json.loads(raw_initial[0])
    except Exception as exc:
        raise FetchError(f"{path}：比赛数据解析失败 {type(exc).__name__}: {exc}")

    # `matchId` 在 payload 里是**与 initial 平级**的键（{"matchId":"1027",
    # "initial":{...}}），不在 initial 内部 —— 直接从 blob 里取，取不到再
    # 从 URL 里抠数字段（/matches/1027/… → 1027）。
    series_id = ""
    mid = re.search(r'"matchId":"(\d+)"', blob)
    if mid:
        series_id = mid.group(1)
    if not series_id:
        seg = [p for p in path.split("/") if p.isdigit()]
        series_id = seg[0] if seg else path.strip("/").split("/")[0]

    started = _start_date(blob)
    year = int(started[:4]) if re.match(r"\d{4}", started) else 0
    score_a, score_b = int(initial.get("scoreA") or 0), int(initial.get("scoreB") or 0)
    winner = ""
    if len(teams) == 2:
        winner = teams[0] if score_a > score_b else (teams[1] if score_b > score_a else "")

    # 全局那份 `economy` 只当兜底：真正准的是**每个 map 对象里**的那份
    econ_fallback = _obj(blob, "economy")

    games: list[dict[str, Any]] = []
    player_stats: list[dict[str, Any]] = []          # 聚合级（同 vlr 那层）
    player_rounds: list[dict[str, Any]] = []         # 逐回合 × 逐选手（rib 独有）
    round_econ: list[dict[str, Any]] = []            # 逐回合 × 逐队经济（rib 独有）
    game_economy: list[dict[str, Any]] = []          # 每图经济汇总（rib 独有）
    side_stats: list[dict[str, Any]] = []            # 攻防拆分（rib 独有）

    for idx, m in enumerate(initial.get("maps") or [], start=1):
        if not isinstance(m, dict):
            continue
        map_name = str(m.get("map") or "")
        gid = str(m.get("id") or f"{series_id}_map_{idx}")
        rounds_raw = list(m.get("rounds") or [])
        if not rounds_raw:
            continue                                   # 没打的图（Lotus 0-0）

        # ⚠️ `roundStats` / `roundEconomy` / `statsA` **在每个 map 对象里**，
        # 不是全局数组。早先按"回合数配对全局块"的写法有两个坑：
        #   1) 两张图回合数相同时会拿到同一个块（串图）；
        #   2) 没打的图里这些键是 `"$"` 引用，会被当成真数据。
        rs_raw = m.get("roundStats")
        re_raw = m.get("roundEconomy")
        incomplete = bool(m.get("dataIncomplete"))

        rounds: list[dict[str, Any]] = []
        for i, r in enumerate(rounds_raw, start=1):
            if not isinstance(r, dict):
                continue
            w = str(r.get("winner") or "")
            rounds.append({
                "round_number": i,
                "winner_side": w,                      # "A" / "B"
                "winner": (teams[0] if w == "A" else teams[1])
                          if len(teams) == 2 and w in ("A", "B") else "",
                "loser": (teams[1] if w == "A" else teams[0])
                         if len(teams) == 2 and w in ("A", "B") else "",
                "end_reason": WIN_TYPE_MAP.get(str(r.get("winType") or "").lower(),
                                               str(r.get("winType") or "")),
                "winner_side_of": str(r.get("attackerTeam") or ""),
            })

        games.append({
            "game_id": gid, "sequence": len(games) + 1, "map_name": map_name,
            "score": f"{m.get('scoreA')}-{m.get('scoreB')}",
            "winner": (teams[0] if str(m.get("winner")) == "A"
                       else teams[1]) if len(teams) == 2 else "",
            "total_rounds": len(rounds), "rounds": rounds,
            "has_round_stats": isinstance(rs_raw, list) and bool(rs_raw),
            "has_round_economy": isinstance(re_raw, list) and bool(re_raw),
            "data_incomplete": incomplete,
        })

        # ── 聚合级选手统计（与 vlr 的 player_stats 同形）──
        for side_key, team in (("statsA", 0), ("statsB", 1)):
            rows = m.get(side_key)
            if not isinstance(rows, list) or len(teams) != 2:
                continue
            for p in rows:
                if not isinstance(p, dict):
                    continue
                player_stats.append(_agg_row(p, teams[team], map_name))

        # ── 逐回合 × 逐选手（vlr 给不了的一层）──
        if isinstance(rs_raw, list):
            for ri, row in enumerate(rs_raw, start=1):
                if not isinstance(row, list):
                    continue
                for p in row:
                    if not isinstance(p, dict):
                        continue
                    side = p.get("team")
                    player_rounds.append({
                        "series_id": series_id, "game_id": gid, "map_name": map_name,
                        "round_number": ri,
                        "player_name": str(p.get("name") or "").strip(),
                        "team_name": (teams[0] if side == "A" else teams[1])
                                     if len(teams) == 2 and side in ("A", "B") else "",
                        "side": str(p.get("side") or ""), "agent": str(p.get("agent") or ""),
                        "kills": p.get("kills"), "deaths": p.get("deaths"),
                        "assists": p.get("assists"), "damage": p.get("damage"),
                        "acs": p.get("acs"), "hs_pct": p.get("hsPct"),
                        "alive": p.get("alive"),
                        "first_kill": bool(p.get("firstKill")),
                    })

        # ── 逐回合 × 逐队经济 ──
        if isinstance(re_raw, list):
            for ri, row in enumerate(re_raw, start=1):
                if not isinstance(row, dict):
                    continue
                for side in ("A", "B"):
                    v = row.get(side)
                    if _undef(v) or not isinstance(v, dict):
                        continue
                    round_econ.append({
                        "series_id": series_id, "game_id": gid, "map_name": map_name,
                        "round_number": ri,
                        "team_name": (teams[0] if side == "A" else teams[1])
                                     if len(teams) == 2 else "",
                        "bank": v.get("bank"), "loadout": v.get("loadout"),
                        "buy_tier": str(v.get("buyTier") or ""),
                    })

        # ── 每图的经济汇总（手枪局 / eco / 半买 / 全买 各赢了几局）──
        econ = m.get("economy")
        if _undef(econ) or not isinstance(econ, dict):
            econ = econ_fallback if len(games) == 0 else {}
        for side, ti in (("A", 0), ("B", 1)):
            if len(teams) != 2:
                continue
            game_economy.append({
                "series_id": series_id, "game_id": gid, "map_name": map_name,
                "team_name": teams[ti],
                "pistol_won": econ.get(f"pistolWon{side}"),
                "eco_total": econ.get(f"ecoTotal{side}"),
                "eco_won": econ.get(f"ecoWon{side}"),
                "semi_eco_total": econ.get(f"semiEcoTotal{side}"),
                "semi_eco_won": econ.get(f"semiEcoWon{side}"),
                "semi_buy_total": econ.get(f"semiBuyTotal{side}"),
                "semi_buy_won": econ.get(f"semiBuyWon{side}"),
                "full_buy_total": econ.get(f"fullBuyTotal{side}"),
                "full_buy_won": econ.get(f"fullBuyWon{side}"),
            })

        # ── 攻防拆分（sideStats.attack/defense 各带一份 statsA/statsB）──
        ss = m.get("sideStats")
        if isinstance(ss, dict):
            for side_name in ("attack", "defense"):
                blk = ss.get(side_name)
                if not isinstance(blk, dict) or len(teams) != 2:
                    continue
                for side_key, ti in (("statsA", 0), ("statsB", 1)):
                    for p in (blk.get(side_key) or []):
                        if not isinstance(p, dict):
                            continue
                        row = _agg_row(p, teams[ti], map_name)
                        row["side"] = side_name
                        row["game_id"] = gid
                        side_stats.append(row)

    return {
        "series_id": series_id, "source": "rib.gg",
        "url": (path if path.startswith("http") else f"{BASE}{path}"),
        "tournament_name": event,
        "tournament_slug": re.sub(r"[^a-z0-9]+", "-", event.lower()).strip("-"),
        "teams": teams, "score": f"{score_a}-{score_b}", "winner": winner,
        # `upcoming` / `completed`。实测踩过：rib 的赛事页把**未开打**的比赛排在
        # 前面（Champions 2026 的头三场 status 全是 upcoming、maps 全是 TBD），
        # 按"取前 N 场"抓会一场数据都拿不到。入库前必须看这个字段。
        "status": str(initial.get("status") or ""),
        "started_at": started, "year": year,
        "games": games,
        "player_stats": player_stats,
        "player_round_stats": player_rounds,
        "round_economy": round_econ,
        "game_economy": game_economy,
        "side_stats": side_stats,
        "economy_summary": econ_fallback,
    }


def list_events(*, limit: int = 0, sleep: float = DEFAULT_SLEEP
                ) -> list[dict[str, str]]:
    """站点上的赛事列表（`/events` 页里的 `/events/{id}/{slug}` 链接）。

    实测：rib 的 `/events` 页能列出 24 个赛事（vct-2026-emea-stage-1、
    esports-world-cup-2026、challengers-2026-…），**不需要任何 key**。
    """
    html = _get("/events", sleep=sleep)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in re.finditer(r'href="/events/(\d+)/([a-z0-9\-]+)"', html):
        eid, slug = m.group(1), m.group(2)
        if eid in seen:
            continue
        seen.add(eid)
        out.append({"event_id": eid, "slug": slug})
        if limit and len(out) >= limit:
            break
    return out


def list_matches(event_id: str, *, limit: int = 0,
                 sleep: float = DEFAULT_SLEEP) -> list[dict[str, str]]:
    """某赛事下的比赛（`/events/{id}/{slug}` 页面里的 `/matches/...` 链接）。"""
    html = _get(f"/events/{event_id}", sleep=sleep)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in re.finditer(r'href="/matches/(\d+)/([a-z0-9\-]+)"', html):
        mid, slug = m.group(1), m.group(2)
        if mid in seen:
            continue
        seen.add(mid)
        out.append({"match_id": mid, "slug": slug, "path": f"/matches/{mid}/{slug}"})
        if limit and len(out) >= limit:
            break
    return out
