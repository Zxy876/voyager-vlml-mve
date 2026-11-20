# gta_drive — MVE 自动驾驶线（GTA V + ScriptHookVDotNet）

目标：把 MVE 作为 GTA V 剧情模式的插件——玩家开车时可激活，MVE 输出的驾驶
代码接管车辆；玩家任务不受影响（任务里开到目的地也由 MVE 接管完成）。
**整个环境就是 MVE 的一个场景：obs → 学生代码 → 受控执行 → 环境判据。**

## 双机架构（实际部署：经 VPS 中继，游戏机只出不进）

```
Windows 机（游戏机）                 VPS 43.161.203.50                    Mac mini（这台）
├─ GTA V 剧情模式                    ├─ gta_relay.py（systemd 服务         ├─ MVE-GTA-Drive 仓库
├─ SHV + SHVDN                       │   mve-bridge-relay，监听 9778）     ├─ ssh -N -L 9778:… 隧道
├─ scripts/MVEBridge.cs (:9777)      └─ GAME/MVE 两角色配对→纯字节透传     └─ run_first_automation.py
└─ py win_forwarder.py --relay 127.0.0.1:19778 ──ssh隧道──┘      └──ssh隧道── Mac 连 127.0.0.1:9778
```

- 云安全组只开了 22/80，9778 不对外——两侧都走 **SSH 隧道**进城。Windows 用
  专用受限密钥 `gta_win`（authorized_keys 带 `restrict,command=/bin/false,permitopen=127.0.0.1:9778`，
  只能建 9778 的转发，不能登 shell）。
- 直连备选：两机同局域网时可跳过 VPS，`run_first_automation.py --host <Windows机IP>`，
  此时 Windows 放行 9777 入站即可。
- 握手行 `>ROLE?` 由转发器吞掉，不会透传进游戏插件。

## 硬前提（先说清，免得白折腾）

| 事项 | 结论 |
|---|---|
| 平台 | **必须是 Windows PC**。ScriptHookVDotNet 只支持 PC 版 GTA V |
| PS5 | **跑不了这套系统**。主机不开放 mod（无法装 SHV/SHVDN），PS5 上只能原版游玩 + 手柄开车。这条线与 PS5 不兼容 |
| 联机 | 只进**剧情模式**。装了 mod 进 GTA Online 会被判定作弊封号 |
| 正版 | 必须正版（SHVDN 需要 ScriptHookV 依赖正版游戏版本） |

## 第一步：买正版 GTA V（Windows PC）

任选一家，买**一次**即可（都是 Rockstar 同一套游戏）：

- **Steam**（store.steampowered.com，搜 Grand Theft Auto V）：中国区常价约
  ¥120–¥190，常打折；买 Grand Theft Auto V Legacy 即可（兼容性最稳、教程最多）。
- **Epic**（store.epicgames.com）：同样有 Legacy 版，打折时常到 ¥100 以下。
- **Rockstar 官方启动器**（store.rockstargames.com）：直连 Rockstar 账号。

不推荐一上来买 PC Enhanced 版（2025 年新出）：ScriptHookV/SHVDN 的适配
以 Legacy 版为主，等这条线跑通了再考虑升级。

（PS5 正版供参考：PlayStation Store 买 Grand Theft Auto V Enhanced &
Enhanced / GTA Online 独立版——但那上面跑不了本系统，只是能玩原版。）

## 第二步：装 mod 框架（游戏装好后，一次性）

1. 启动一次游戏进剧情模式，记下游戏目录（如 `C:\Program Files\Rockstar Games\Grand Theft Auto V`）。
2. 装 **ScriptHookV**（Alexander Blade，dev-c.com/gtav/scripthookv）：
   把 `dinput8.dll` 和 `ScriptHookV.dll` 解压进游戏根目录。版本必须和游戏
   版本匹配（装完启动一次，它会在日志里报支持与否）。
3. 装 **ScriptHookVDotNet v3 nightly**（github.com/scripthookvdotnet/scripthookvdotnet/releases）：
   把 `ScriptHookVDotNet3.asi` + `ScriptHookVDotNetCore.dlls` 解压进游戏根目录。
4. 在游戏根目录建 `scripts/` 文件夹，把本目录 `plugin/MVEBridge.cs` 丢进去
   （SHVDN 加载时会自动编译 .cs 源码脚本；若你的版本不认 .cs，用
   `plugin/INSTALL.md` 的说明编译成 dll 再放）。
5. 启动剧情模式，画面右上角应看到「MVEBridge 启动」提示。

## 第三步：跑第一个自动化（MVE 输出的，Mac mini 上执行）

```
游戏内（Windows 机）：上车 → 地图上设一个路点 → 按 F9（激活桥）
Mac mini：           cd /Users/zhangbing/WorkBuddy/MVE-GTA-Drive/gta_drive
                     python run_first_automation.py --host <Windows机IP>
```

发生的事：
1. 脚本连上插件（127.0.0.1:9777）
2. **MVE 学生**（`mve/llm_client.py`，glm-4-flash，密钥在 `mve/.env`）
   读完 obs 格式 + 动作空间说明，写出一段 `act(obs)` 策略代码 —— **这是 MVE 的输出**
3. `mve_gta_env.py` 在受控沙箱执行该代码，逐帧下发 drive_to/cruise/stop
4. 环境自己判据（不经 LLM）：到达路点 5m 内 = success
5. 账本落 `run_first_automation.trace.jsonl`（同构 learn_trace）

玩家随时可以：再按 F9 交还控制权 / 按任意移动键打断驾驶 / 直接下车。
车撞了、开岔了，都是学生代码的失败案例，进 trace，等下一轮学习迭代。

## 文件

| 文件 | 角色 |
|---|---|
| `plugin/MVEBridge.cs` | 游戏内插件：obs/action 行协议 TCP 服务（10Hz） |
| `adapter/gta_client.py` | Python 侧传输客户端（行协议） |
| `mve_gta_env.py` | MVE 同构环境：obs 文本化 + 沙箱执行学生代码 + 环境判据 |
| `run_first_automation.py` | 第一个自动化：MVE 生成驾驶策略 → 执行 → 判据 → 落账 |
| `adapter/INSTALL.md` | MVEBridge 编译成 dll 的备选说明 |

## 与 MVE 场景契约的对应（后续正式插场景时用）

- 硬拒绝项：可执行动作 ✓（drive_to/cruise/stop/handback）、成功判据 ✓（到达 5m）、
  状态表示 ✓（obs dict，字段固定）
- 可降级项：场景知识图谱（待建）、实体可填值（车名/路点/导航）
- 学习闭环的下一步：把 run_first_automation 的 verdict 接进 curriculum
  （重做老题→指标更好），以及把学生代码的失败原因喂回下一轮生成的 prompt
