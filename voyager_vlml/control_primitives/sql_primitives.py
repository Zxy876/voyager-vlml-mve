"""VLML 的 SQL 原语清单（取代原版 control_primitives/*.js）。

设计取舍：
  · 原版原语是「动作」(mineBlock/craftItem)，VLML 的同构物是「取数动作」。
  · 一层是**裸取数**：query_sql（模型自己写 SELECT）—— 这是主要形态，
    因为用户明确要求技能存成 SQL、迭代修正的 prompt 语言也改成 SQL。
  · 另一层是**报告工具**（match_summary / scouting / …），同构原版里那些
    封装好的高阶原语（如 craftItem 内部调用了多个低阶动作）。报告工具内部
    已经按指标口径封装好，能用就不必裸写 SQL。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Primitive:
    name: str
    kind: str                 # "sql" 裸 SQL | "report" 报告工具
    signature: str
    description: str
    example: str = ""
    notes: list[str] = field(default_factory=list)


PRIMITIVES: list[Primitive] = [
    Primitive(
        name="query_sql",
        kind="sql",
        signature="query_sql(sql_query: str)",
        description="在当前库上执行一条只读 SELECT，返回行与列。**主要取数手段**。",
        example="SELECT COUNT(*) AS n FROM <schema 里真有的表>",
        notes=[
            "只能 SELECT；表名列名**只能来自下面 schema 清单里出现过的**，不许凭印象编",
            "不知道该用哪张表/哪一列时，先探查再写正式查询（见下面【探查】三条）",
            "口径不确定时先看样例值：布尔列别当字符串比",
        ],
    ),
    Primitive(
        name="get_database_info",
        kind="report",
        signature="get_database_info()",
        description="列出库里有哪些表、多少行，用于先摸清能取什么。",
        example="get_database_info()",
    ),
    Primitive(
        name="match_summary_report",
        kind="report",
        signature="match_summary_report(series_id: str)",
        description="系列赛概览（队伍、地图、比分）。系列赛级问题的入口。",
        example="match_summary_report(series_id='<schema 样例里那种 id>')",
    ),
    Primitive(
        name="match_rounds_report",
        kind="report",
        signature="match_rounds_report(series_id: str)",
        description="系列赛逐回合明细。回合级问题优先用它。",
        example="match_rounds_report(series_id='<当前库的系列赛 id>')",
    ),
    Primitive(
        name="match_players_report",
        kind="report",
        signature="match_players_report(series_id: str)",
        description="系列赛选手统计。选手级问题优先用它。",
        example="match_players_report(series_id='<当前库的系列赛 id>')",
    ),
    Primitive(
        name="match_economy_report",
        kind="report",
        signature="match_economy_report(series_id: str)",
        description="系列赛经济局数据（买枪/装备净值）。经济类问题用它。",
        example="match_economy_report(series_id='<当前库的系列赛 id>')",
    ),
    Primitive(
        name="scouting_report",
        kind="report",
        signature="scouting_report(team_name: str)",
        description="队伍侦察报告（回合/图/队伍层分析）。",
        example="scouting_report(team_name='<当前库里的队伍名>')",
    ),
    Primitive(
        name="player_profile_report",
        kind="report",
        signature="player_profile_report(player_name: str)",
        description="选手画像报告。",
        example="player_profile_report(player_name='<当前库里的选手名>')",
    ),
    Primitive(
        name="pattern_detection_report",
        kind="report",
        signature="pattern_detection_report(...)",
        description="模式识别报告（连败、爆弹等模式）。",
    ),
]


def render_primitives(available: set[str] | None = None) -> str:
    """渲染成给模型 prompt 用的文本块（同构原版把 JS 源码拼进 prompt 那一手）。

    `available` = 当前库里**真的装载成功**的工具名集合。报告工具是 VLML 建模层
    的东西（match_*_report / scouting_report …），换成没有建模层的库就跑不起来；
    不按 available 过滤，模型会照着清单去调一个根本不存在的能力，白烧一轮
    （`env.call` 只会回"未装载的工具"）。传 None = 不过滤（老行为）。
    """
    lines = ["【可用取数原语（写 SQL 时照这些来）】"]
    for p in PRIMITIVES:
        if available is not None and p.kind == "report" and p.name not in available:
            continue
        lines.append(f"- {p.signature}  [{p.kind}]")
        lines.append(f"    {p.description}")
        if p.example:
            lines.append(f"    例：{p.example}")
        for n in p.notes:
            lines.append(f"    注意：{n}")
    return "\n".join(lines)


def _main() -> int:
    print(render_primitives())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
