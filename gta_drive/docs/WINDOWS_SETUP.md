# WINDOWS_SETUP.md — Windows 机（游戏机）操作指南

> 本指南写给 Windows 机上的 WorkBuddy（AI 助手）逐步执行，也适合人工照做。  
> 所有代码文件来自 GitHub 仓库 `Zxy876/voyager-vlml-mve` 的 **`gta-drive` 分支**  
> （只需 `gta_drive/` 目录）。目标：GTA V 剧情模式里跑起 MVEBridge 插件，  
> 并用 SSH 隧道把桥接到 VPS 中继，供 Mac mini 上的 MVE 调用。

## 0. 红线（每一步都适用）

1. **只进剧情模式**：装了 mod 进 GTA Online 会被封号。
2. **私钥不进仓库、不上 GitHub**：SSH 私钥只存在于本机 `%USERPROFILE%\.ssh\`。
3. 仓库里只需要 `gta_drive/plugin/MVEBridge.cs` 和 `gta_drive/relay/win_forwarder.py` 两个文件。

## 1. 拉文件（GitHub）

```
git clone -b gta-drive --depth 1 https://github.com/Zxy876/voyager-vlml-mve.git C:\gta\repo
```

（如果没装 git：`winget install Git.Git`，或直接在浏览器下载  
`https://github.com/Zxy876/voyager-vlml-mve/raw/gta-drive/gta_drive/plugin/MVEBridge.cs`  
和 `.../gta_drive/relay/win_forwarder.py` 两个文件存到 `C:\gta\`。）

验证：`C:\gta\repo\gta_drive\plugin\MVEBridge.cs` 存在。

## 2. 安装 Python（跑转发器用）

```
winget install Python.Python.3.12
```

安装时勾选 **Add python.exe to PATH**（winget 默认会加）。验证：

```
py --version     # 应输出 3.12.x
```

## 3. 安装游戏本体与 mod 框架

前提：已在 Steam/Epic/Rockstar 购买并安装 **GTA V Legacy 版**。

1. 启动一次游戏、进剧情模式（让游戏生成存档目录、确认能玩）。
2. 找到游戏根目录（Steam 库里右键 GTA V → 管理 → 浏览本地文件；  
   默认类似 `C:\Program Files (x86)\Steam\steamapps\common\Grand Theft Auto V`）。
3. 下载 **ScriptHookV**：`http://www.dev-c.com/gtav/scripthookv/`  
   （页面选对游戏版本的 zip）→ 解压出 `dinput8.dll` 和 `ScriptHookV.dll`  
   放进**游戏根目录**。
4. 下载 **ScriptHookVDotNet v3 nightly**：  
   `https://github.com/scripthookvdotnet/scripthookvdotnet/releases`  
   （资产名带 `nightly` 的 zip）→ 解压 `ScriptHookVDotNet3.asi` 和  
   `ScriptHookVDotNetCore.dlls`（整个文件夹）进**游戏根目录**。
5. 在游戏根目录**新建 `scripts` 文件夹**，把第 1 步拿到的  
   `MVEBridge.cs` 复制进去。
6. 启动游戏进剧情模式，画面右上角应出现 **「MVEBridge ON  port 9777」**  
   的通知。若没有，看游戏根目录的 `ScriptHookVDotNet.log`：
   - 有 `Loading script MVEBridge` + 编译错误 → 把错误原文报给用户；
   - 连 log 都没有 → ScriptHookV 版本与游戏版本不匹配（它启动时会弹窗提示支持的游戏版本）。

## 4. SSH 密钥（本机生成，公钥交回用户）

> VPS 只允许受限密钥建隧道。**不要**向任何人索要现成私钥——本机自己生成。



```
ssh-keygen -t ed25519 -f %USERPROFILE%\.ssh\gta_win -N "" -C gta-win-tunnel
type %USERPROFxianILE%\.ssh\gta_win.pub
```

把输出的**公钥一行**（`ssh-ed25519 AAAA... gta-win-tunnel`）原文交给用户，  
由用户在 Mac 侧装到 VPS（Mac 侧命令已备好，装完用户会回复"已装"）。

写本机 SSH 配置 `%USERPROFILE%\.ssh\config`（没有就新建）：

```
Host mve-vps
  HostName 43.161.203.50
  User root
  IdentityFile ~/.ssh/gta_win
  ServerAliveInterval 30
  ServerAliveCountMax 4
  ExitOnForwardFailure yes
```

## 5. 建隧道（游戏机只出不进，无需改防火墙）

```
ssh -N -L 19778:127.0.0.1:9778 mve-vps
```

- 期望表现：**窗口挂住不动**（没有输出就是对的）。首次连接会问  
  `Are you sure you want to continue connecting` → 输 `yes`。
- 若报 `Permission denied (publickey)` → 公钥还没在 VPS 上装好，等用户确认；
- 若报 `Administratively prohibited` → 密钥被装成受限的但 permitopen 不对，把报错原文给用户。
- 保持这个窗口挂着（这就是隧道）。做成开机自启可选：  
  `schtasks /create /tn mve-tunnel /tr "ssh -N -L 19778:127.0.0.1:9778 mve-vps" /sc onlogon`  
  （建议先手动跑通再考虑自启。）

## 6. 起转发器（把游戏里的桥接到隧道）

新开一个窗口：

```
cd C:\gta\repo\gta_drive\relay
py win_forwarder.py --relay 127.0.0.1:9778
```

期望输出：

```
[fwd] 游戏桥 127.0.0.1:9777 ↔ 中继 127.0.0.1:19778 已接通
```

（端口显示以实际参数为准；若打印 `连接失败...5s 后重试` 说明游戏没开或  
MVEBridge 没加载——游戏开着、剧情模式里即可。）

两处都通了之后：**这台机器的准备工作完成**。告诉用户  
「Windows 侧就绪，可以跑第一个自动化了」。

## 7. 与 Mac 侧协同跑第一个自动化（说明，无需本机操作）

1. 玩家（用户）在游戏里：**上车 → 按 M 键打开地图设一个路点** 即可，
   **不用按 F9**——watcher 检测到新路点自动投种子，学生代码的动作
   会让插件自动接管驾驶（右上角出现「MVE 自动驾驶：动作驱动，自动接管」）。
2. Mac mini 上执行：  
   `python run_first_automation.py --host 127.0.0.1 --port 9778 --relay`
3. MVE 会生成驾驶代码开这辆车去路点；到达 5m 内算成功。
4. 玩家随时 **按 F9 拿回方向盘**（之后 MVE 的 drive 类动作会被忽略，
   直到它下一个动作再自动接管）；按 W/A/S/D 也会打断驾驶。

## 8. 常见故障对照

| 现象                | 原因/处理                                                           |
| ----------------- | --------------------------------------------------------------- |
| 右上角无 MVEBridge 通知 | 查 `ScriptHookVDotNet.log`；确认 .cs 在 `scripts\`；SHV/SHVDN 版本配游戏版本 |
| 转发器一直重试           | 游戏没开 / 不在剧情模式 / MVEBridge 没加载（netstat -ano 看 9777 是否 LISTENING） |
| 隧道挂住但 Mac 连不上     | 确认公钥已装 VPS；确认转发器窗口开着（它连了 GAME 角色中继才配对）                          |
| 车不动               | 没上车（动作需要车）；路点没设（obs 里 waypoint 是 null）；刚按过 F9 拿回方向盘（drive 类动作被忽略，学生下一个动作会自动接管）        |
| 联机被踢/封号警告         | 立刻退 GTA Online——mod 只允许在剧情模式                                    |

## 9. 日常更新插件（Win 端唯一要做的常规操作）

首次装机完成后，隧道和转发器都不用再动。Mac 侧对插件代码的更新
（例如任务面解包版 commit `af5bc6c`：任务 blip 识别 + cutscene 自动交还
方向盘）只需在 Win 端做三步：

```
:: 1. 拉最新代码
git -C C:\gta\repo pull origin gta-drive

:: 2. 覆盖到游戏 scripts 目录（路径按你的游戏根目录调整）
copy /Y C:\gta\repo\gta_drive\plugin\MVEBridge.cs "C:\Program Files (x86)\Steam\steamapps\common\Grand Theft Auto V\scripts\MVEBridge.cs"
```

3. 让插件重编译（二选一）：
   - 游戏内按 **F4** 打开 SHVDN 控制台，输入 `Reload()`（**必须带括号**，
     不带括号会被当成一行 C# 表达式编译报错）；
   - 或者直接重启游戏进剧情模式。

生效标志：右上角再次出现「MVEBridge ON port 9777」通知。

任务面版生效验证：接一个剧情任务， obs 里会出现
`mission_active: true` / `mission_target` / `mission_dist_m` 字段
（Mac 侧或面板可见）；剧情演出（cutscene）开始时控制权自动交还、
演出结束后 MVE 的下一个 drive 类动作会再次自动接管——F9 只是人
手动拿回方向盘的开关，不再是激活前置。
