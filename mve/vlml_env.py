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
# 于是一律落到默认库 vlml/data/vlml_events.duckdb。要换库只能在这里打补丁 ——
# 和上面的 mkdir 补丁同一套路：MVE 侧绕过，不改 VLML 仓库源码。
#
# 用配置文件而不是环境变量：run_mve 是 pilot 起的、pilot 是面板起的，
# 环境变量要层层传递；写文件则所有进程读到的都是同一份。
DB_CONFIG = ROOT / "mve" / "db_config.json"


def _read_db_config() -> dict:
    try:
        import json
        obj = json.loads(DB_CONFIG.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


ACTIVE_DB_PATH = str(_read_db_config().get("db_path") or "")
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
