# -*- coding: utf-8 -*-
"""GTA 环境数据源结构块（场景 G 专用，2026-11-21）。

为什么要有它（用户 04:40/10:47 两问「学生 MVE 看得到解包数据吗」）：
  Phase1（maieutic 追问）的输入面 = 题面 + 【数据源的真实结构】。
  SQL 场景的同构物是 curriculum_auto.db_observation（SHOW TABLES/DESCRIBE）；
  场景 G 的数据源不是库，是**游戏环境**（obs 字段 + mcp_gta_* 工具）。
  没有这块结构，critic 会拿 SQL 口径逼学生交「表名/列名」——场景 G 的
  scene.duckdb 是空库，5 轮追问全卡死在这（req_...0abf 实测）。

用法：
  · learn_orchestrate._env_obs_block：db_path 落在 scene_data/G 时返回本块；
  · maieutic_scaffold.render_material：scene_id == "G" 时把本块作为
    场景数据源段（骨架维度因此落在真实工具/字段上，不再空转）。
"""

# obs 字段 = MVEBridge.cs DetectFast/DetectSlow 解包面的结构（单一定义源：
# gta_drive/plugin/MVEBridge.cs），这里如实转述，不许加字段。
_GTA_ENV_BLOCK = """【数据源的真实结构 —— GTA 游戏环境（本场景不是 SQL 库；场景目录里的
scene.duckdb 是空的属正常，不存在任何表/列，不许承诺 SQL 口径）】

观测面 —— mcp_gta_gta_get_obs() 每次调用返回的实时解包（字段全集）：
  ts            Unix 时间（秒）
  running       自动驾驶是否已激活（人按 F9 激活/收回）
  on_foot       是否在车外
  speed         车速（m/s）
  pos           车辆世界坐标 [x, y, z]
  heading       朝向角（度）
  health_engine 引擎血量（满 1000）
  in_vehicle_model  当前车辆型号名
  waypoint      地图路点 [x, y, z]（人按 M 在地图上设的；没有则 null）
  mission_active / mission_target [x,y,z] / mission_dist_m
                剧情任务面（任务激活时目标点与距离；两套坐标系统一为
                GTA 世界坐标，可直接算两点距离）
  events[]      自上次观测缓冲的真实事件：ped_hit / vehicle_damage /
                wanted / rolled_over / vehicle_on_fire / engine_dead /
                died / busted / cutscene_start / 上下车

动作面 —— 可调用的工具（签名如实，不多不少）：
  mcp_gta_gta_get_obs()                       取上述实时观测
  mcp_gta_gta_drive_to(x, y, z=0, speed=8, style=1)   驶向世界坐标点
  mcp_gta_gta_wait_arrive(radius=5, target="waypoint"|"mission", timeout=…)
                                              判据由环境规则判定：距目标
                                              radius 内 = success（不经
                                              LLM），verdict 附真实事件清单
  mcp_gta_gta_cruise(on/off)                  自由巡航开关
  mcp_gta_gta_stop() / mcp_gta_gta_handback() 停车 / 把方向盘交还给人

落点面 —— 取证写进哪：
  scene_data/G/gta_obs.jsonl   观测流水
  scene_data/G/gta_trace.jsonl 动作 + verdict + 事件账本

承诺口径的规矩：起点=obs.pos、目标=waypoint 或 mission_target、到达判据=
wait_arrive 的 radius、驾驶能力=drive_to/cruise。任何本题用到的字段必须在
上面指认得到；指认不了 = 数据源上不可答，如实写缺什么，不许编。"""


def env_block() -> str:
    """场景 G 的环境数据源结构（给 Phase1 学生 / 骨架生成器 / 判官共用）。"""
    return _GTA_ENV_BLOCK
