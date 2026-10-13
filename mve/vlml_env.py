#!/usr/bin/env python3
"""引导 VLML 运行环境（cwd + 沙箱 mkdir 补丁），供其它 MVE 模块复用。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VLML = ROOT / "vlml"

# VLML 内部查询依赖 cwd 在仓库根
os.chdir(VLML)
sys.path.insert(0, str(VLML / "src"))

# 沙箱对已存在目录的 mkdir(exist_ok=True) 会抛 PermissionError(EEXIST)，
# 而 vlml/db/manager.py 每次建连接都调一次。MVE 侧绕过，不改仓库源码。
_orig_mkdir = Path.mkdir


def _safe_mkdir(self, *args, **kwargs):  # type: ignore[no-untyped-def]
    try:
        if self.exists() and self.is_dir():
            return None
    except OSError:
        pass
    return _orig_mkdir(self, *args, **kwargs)


Path.mkdir = _safe_mkdir  # type: ignore[assignment]

# ---- 数据库可切换 ----
# VLML 的工具里全部写死 `EventDatabase(read_only=True)`（不带 db_path），
# 于是一律落到它自带的默认库 vlml/data/vlml_events.duckdb（GRID 切片）。
# 要换库只能在这里打补丁 —— 和上面的 mkdir 补丁同一套路：MVE 侧绕过，
# 不改 VLML 仓库源码。
#
# 用配置文件而不是环境变量：run_mve 是 pilot 起的、pilot 是面板起的，
# 环境变量要层层传递；写文件则所有进程读到的都是同一份。
#
# ⚠️ db_path 为空时的兜底也必须是本项目的默认库（rib.gg，见 db_switch.DEFAULT_DB），
#    不能留着 VLML 自带的 GRID 默认 —— 否则配置一丢，六个读库模块里这个
#    悄悄落回 GRID，别的模块按新默认走，"数据源悄悄换了"就是这么来的
#    （2026-10-13 服务器实测：db_config 被切空，工具实际连的是 GRID 库，
#    而 db_switch.current_path() 报的是 rib）。
DB_CONFIG = ROOT / "mve" / "db_config.json"

# 默认库的唯一定义在 db_switch（mve/ 在 sys.path 里：面板第 33 行插入，
# 脚本直跑时 sys.path[0] 就是 mve/）。import 失败要**响**——静默兜底
# 就等于默认库可以悄悄分叉，那正是要防的事。
from db_switch import DEFAULT_DB  # noqa: E402


def _read_db_config() -> dict:
    try:
        import json
        obj = json.loads(DB_CONFIG.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _resolve_db_path(raw: str) -> str:
    """把配置里的库路径解析成**绝对路径**。

    ⚠️ 为什么必须显式解析（2026-10-13 服务器实测）：
      本模块第 14 行做了 `os.chdir(VLML)`（VLML 内部查询依赖 cwd 在 vlml/ 下）。
      于是配置里写相对路径 `vlml/data/vlml_rib.duckdb` 时，DuckDB 会拿当前
      cwd 去拼 → `/root/mve/vlml/vlml/data/vlml_rib.duckdb` —— **路径被拼了两遍**，
      报的是 `IO Error: database does not exist`。
      现象极具迷惑性：它在循环侧表现为"连续三轮一次工具都没调成功"
      （分诊给出 `linkage`），看着像"学不会"，其实是**配置里的一个相对路径**。
      所以：相对路径一律按**仓库根**解析（人写配置时想的是仓库根，不是 vlml/）。
    """
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    return str(p)


_RAW_DB_PATH = str(_read_db_config().get("db_path") or "")
# db_path 为空 → 落到**本项目的**默认库（rib.gg），不是 VLML 自带的 GRID 库。
ACTIVE_DB_PATH = (_resolve_db_path(_RAW_DB_PATH) if _RAW_DB_PATH
                  else str(DEFAULT_DB))

# ⚠️ 库文件不在 —— **当场喊出来**（配置写的 / 默认兜底的都要喊）。
#   不喊的代价（2026-10-13 服务器实测）：循环连跑三轮，每轮三次工具调用
#   全部失败，日志里只有一行 `IO Error: database does not exist`；而 panel
#   的分诊把它归到 `linkage`（"链路没跑通"）—— 归类是对的，但人得先看见它。
#   有这一行，启动瞬间就知道是路径问题，不用等三道题跑完再倒推。
if not Path(ACTIVE_DB_PATH).exists():
    _src = (f"db_config 里写的是 {_RAW_DB_PATH!r}（相对路径按仓库根 {ROOT} 解析）"
            if _RAW_DB_PATH else "db_config 的 db_path 为空，用的是默认库兜底")
    print(f"[vlml_env] ⚠️ 要连的库文件不存在：{ACTIVE_DB_PATH}（{_src}）",
          file=sys.stderr, flush=True)
if ACTIVE_DB_PATH:
    from vlml.db.manager import EventDatabase  # noqa: E402

    _orig_db_init = EventDatabase.__init__

    def _patched_db_init(self, db_path=None, read_only=False):  # type: ignore[no-untyped-def]
        # 显式传了 db_path 的照原样走；没传的换成本项目配置的库
        _orig_db_init(self, db_path or ACTIVE_DB_PATH, read_only=read_only)

    EventDatabase.__init__ = _patched_db_init  # type: ignore[assignment]

# MCP 对外暴露的 10 个工具，全部原样导入。
# 注意命名：MCP 工具名 == Python 函数名，唯一例外是自定义 SQL ——
#   对外叫 query_sql（vlml/src/vlml/server.py:93），实现函数叫 execute_custom_sql
#   （vlml/src/vlml/tools/db_query_tools.py:239）。两个名字都要能分派。
from vlml.tools.db_query_tools import execute_custom_sql, get_database_info  # noqa: E402
from vlml.tools.insights_tools import (  # noqa: E402
    match_analysis_report,
    match_economy_report,
    match_players_report,
    match_rounds_report,
    match_summary_report,
    pattern_detection_report,
    player_profile_report,
    scouting_report,
)

__all__ = [
    "VLML",
    "execute_custom_sql",
    "get_database_info",
    "match_analysis_report",
    "match_economy_report",
    "match_players_report",
    "match_rounds_report",
    "match_summary_report",
    "pattern_detection_report",
    "player_profile_report",
    "scouting_report",
]
