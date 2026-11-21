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

观测面 —— mcp_gta_gta_get_obs() 返回的实时解包（**键名如实，代码按这些键取值**）：
  speed_ms      车速（m/s）
  speed_kmh     车速（km/h）
  pos           车辆世界坐标 [x, y, z]
  heading       朝向角（度）
  waypoint      地图路点 [x, y, z]（人按 M 在地图上设的原坐标；没有则 null）
  waypoint_drive  该路点在**路网上最近的可行驶位置** [x, y, z]——车能真正开到
                的就是这里。人点的坐标可能不在路上（死胡同/院子/山坡），GTA
                原生驾驶 AI 只能开到最近街道节点，开向原坐标会在附近绕圈。
                **开车的目标坐标一律用 waypoint_drive，不要用 waypoint。**
  waypoint_offroad_m  原路点离道路多远（米）。0~5 表示就在路上，两者基本重合；
                >20 说明人点的位置车开不进去，实际目标已被吸附到街上
  waypoint_dist_m  当前到原路点的直线距离（米；无路点则 null）
  obs_age_sec   这条观测的年龄（秒；>2 秒基本就是观测停流，别拿它当实时数据）
  running       MVE 是否在接管驾驶（drive 类动作到达会自动接管置 true，
                无需人按 F9；人按 F9=把方向盘拿回来，期间 drive 类动作
                被插件忽略防打架，stop/handback 仍生效）
  on_foot       是否在车外
  health_engine 引擎血量（满 1000；着火/冒烟的先兆）
  health_body   车体结构血量（满 1000；碰撞损伤看这个）
  is_engine_running  发动机是否在转（false=熄火/报废）
  in_vehicle_model  当前车辆型号名
  wanted_level  当前通缉星数（0-5；0=没被通缉。被警察追的直接读数）
  police_count  120m 内警用车辆数（含警用直升机；0=视野内没有警力）
  police_nearest_dist_m / police_nearest_pos [x,y,z] / police_nearest_siren
                最近警车的距离/坐标/是否鸣笛（siren=true 基本就是在追你；
                police_count=0 且 wanted_level>0 = 警察已看不见你，甩掉中）
  mission_active / mission_target [x,y,z] / mission_dist_m
                剧情任务面（任务激活时目标点与距离；两套坐标系统一为
                GTA 世界坐标，可直接算两点距离）
  events[]      自上次观测缓冲的真实事件：ped_hit / vehicle_damage /
                wanted（升星）/ wanted_cleared（降星） / rolled_over /
                vehicle_on_fire / engine_dead / died / busted /
                cutscene_start / 上下车

动作面 —— 可调用的工具（签名如实，不多不少）：
  mcp_gta_gta_get_obs()                       取上述实时观测
  mcp_gta_gta_drive_to(x, y, z=0, speed=8, style=1)   驶向世界坐标点。
                style 透传 GTA 原生驾驶风格位掩码（SHVDN DrivingStyle）：
                1074528293=Rushed（横冲直撞的逃逸驾驶，被追捕时用这个）、
                5=SometimesOvertakeTraffic（超车绕行，正常赶路）、
                6=AvoidTrafficExtremely（极度避让车流，安全逃离）、
                786603=Normal（守交规）、1=裸 flag（几乎无视一切，易撞）。
                躲避条子=选远坐标 + style=1074528293（或 6）
  mcp_gta_gta_wait_arrive(radius=5, target="waypoint"|"mission", timeout=…)
                                              判据由环境规则判定：距目标
                                              radius 内 = success（不经
                                              LLM），verdict 附真实事件清单。
                                              target=waypoint 时判的是
                                              obs.waypoint_drive（可行驶位置）
  mcp_gta_gta_cruise(on/off)                  自由巡航开关
  mcp_gta_gta_stop() / mcp_gta_gta_handback() 停车 / 把方向盘交还给人

落点面 —— 取证写进哪：
  scene_data/G/gta_obs.jsonl   观测流水
  scene_data/G/gta_trace.jsonl 动作 + verdict + 事件账本

承诺口径的规矩：起点=obs.pos、目标=obs.waypoint_drive（可行驶位置）或
mission_target、到达判据=wait_arrive 的 radius、驾驶能力=drive_to/cruise。
任何本题用到的字段必须在上面指认得到；指认不了 = 数据源上不可答，如实写
缺什么，不许编。"""


def env_block() -> str:
    """场景 G 的环境数据源结构（给 Phase1 学生 / 骨架生成器 / 判官共用）。"""
    return _GTA_ENV_BLOCK
