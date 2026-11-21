// MVEBridge.cs — MVE 自动驾驶线 · GTA V 桥接插件（ScriptHookVDotNet v3）
//
// 作用：游戏内起一个 TCP 服务（默认监听所有网卡，供局域网里的 Mac mini 连），
//   - 每秒 ~10Hz 向外发一行 JSON 观测（obs）：车速/位置/朝向/路点/发动机健康
//     + events：自上一条 obs 以来游戏里**真实发生**的事件（Voyager 的
//       bot.on(事件)→events 回灌同构）：撞人/碰撞损伤/被警察通缉/翻车/
//       着火/下车/死亡被捕等——事件由游戏 tick 探测，不是 LLM 编的
//   - 接收一行 JSON 动作（action）：drive_to / cruise / stop / handback
// 玩家侧完全不受影响：drive 类动作（drive_to/cruise）到达时插件自动
// 接管驾驶（无需按键，cutscene 演出期间除外）；人按 F9 随时拿回方向盘
// （期间 MVE 的 drive 类动作被忽略防打架）；下车、按 W/A/S/D 也会打断驾驶。
//
// 零依赖：不用 System.Web.Extensions（SHVDN 运行时编译 .cs 不一定引用它），
// JSON 全部手工拼/正则解析——本插件的 obs/action 格式固定，够用且更稳。
//
// 安装：把本文件放进 GTA V 目录 scripts/（SHVDN v3 nightly 自动编译 .cs）。
// 仅限剧情模式！联机会被判定作弊封号。
// 安全：监听 0.0.0.0 意味着局域网内任何机器可连；家用网可接受，
// 公网/共享网络请改成 IPAddress.Loopback + 端口转发。

using System;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Forms;
using GTA;
using GTA.Math;
using GTA.Native;

public class MVEBridge : Script
{
    // ── 配置 ──
    const int PORT = 9777;
    const Keys TOGGLE_KEY = Keys.F9;      // 激活/交还自动驾驶
    const int OBS_HZ_MS = 100;            // 观测节流 10Hz

    TcpListener _listener;
    TcpClient _client;
    NetworkStream _stream;
    bool _running = false;                // MVE 是否在接管驾驶
    int _lastObs = 0;

    // ── 事件面（Voyager bot.on 同构）：游戏 tick 里探测真实事件，攒缓冲，
    //    每条 obs 带走并清空。全部是探测事实，没有一条是猜的。──
    System.Collections.Generic.List<string> _events =
        new System.Collections.Generic.List<string>();
    int _lastWanted = 0;                  // 上次通缉等级（被警察追）
    int _wanted = 0;                      // 当前通缉等级（obs 直读字段，不靠抓事件）
    int _policeCount = 0;                 // 警力面：120m 内警用车辆数（含警用直升机）
    float[] _policePos = null;            // 最近警车坐标
    float _policeDist = -1f;              // 与最近警车直线距离（-1=视野内没有）
    bool _policeSiren = false;            // 最近警车是否鸣笛（追击中的强信号）
    float _lastVehHealth = -1f;           // 上次车体血量（碰撞损伤代理）
    bool _wasOnFoot = true;               // 上次是否步行（上/下车）
    bool _wasDead = false;                // 上次是否死亡
    bool _wasUpsideDown = false;          // 上次是否翻车
    bool _wasCutscene = false;            // 上次是否在过场演出（任务演出期间交还控制）
    bool _missionActive = false;          // 任务面：当前有任务目标 blip
    float[] _missionTarget = null;        // 任务目标坐标（黄色 Standard blip，最近一个）
    float _missionDist = -1f;             // 与任务目标的直线距离
    float[] _wpDrive = null;              // 路点在路网上的最近可达位置（吸附后）
    float _wpOffroad = 0f;                // 人手点的路点离路网多远（0=就在路上）
    int _lastEventTick = 0;               // 行人扫描节流（重活放 10Hz）

    void Ev(string json)                  // 事件入缓冲（封顶防刷屏）
    {
        if (_events.Count < 20) _events.Add(json);
    }

    // 每个 tick 都跑的轻探测（通缉/死亡/上下车/血量跳变）
    void DetectFast()
    {
        Ped p = Game.Player.Character;
        Vehicle v = p.CurrentVehicle;

        int w = Game.Player.WantedLevel;
        _wanted = w;
        if (w > _lastWanted) Ev("{\"e\":\"wanted\",\"level\":" + w + "}");
        else if (w < _lastWanted)
            Ev("{\"e\":\"wanted_cleared\",\"level\":" + w + "}");
        _lastWanted = w;

        bool dead = p.IsDead;
        if (dead && !_wasDead) Ev("{\"e\":\"died\"}");
        _wasDead = dead;
        if (Game.Player.IsBeingArrested && !_wasDead)
            Ev("{\"e\":\"busted\"}");

        bool onFoot = (v == null || !v.Exists());
        if (onFoot != _wasOnFoot)
            Ev(onFoot ? "{\"e\":\"left_vehicle\"}" : "{\"e\":\"entered_vehicle\"}");
        _wasOnFoot = onFoot;

        if (v != null && v.Exists())
        {
            // 车体血量下降 = 真实碰撞/损伤（行人碰撞在 DetectSlow 里细判）
            float h = v.Health;
            if (_lastVehHealth >= 0 && h < _lastVehHealth - 1f)
                Ev("{\"e\":\"vehicle_damage\",\"delta\":" +
                   F(h - _lastVehHealth) + ",\"health\":" + F(h) + "}");
            _lastVehHealth = h;
        }
        else
            _lastVehHealth = -1f;

        // 任务过场演出：插件不该在演出里开车，出现即自动交还玩家
        bool cs = Game.IsCutscenePlaying;
        if (cs && !_wasCutscene)
        {
            Ev("{\"e\":\"cutscene_start\"}");
            if (_running)
            {
                GiveBackControl();
                _running = false;
                Ev("{\"e\":\"auto_handback\",\"reason\":\"cutscene\"}");
                Notification("MVE 自动驾驶：任务演出，已交还玩家");
            }
        }
        else if (!cs && _wasCutscene)
            Ev("{\"e\":\"cutscene_end\"}");
        _wasCutscene = cs;
    }

    // 10Hz 节流的重探测（行人碰撞/翻车/着火/任务目标 blip）
    void DetectSlow(Ped p, Vehicle v)
    {
        if (v != null && v.Exists())
        {
            // 翻车（滚转接近/超过 90°；GTA Rotation：X 俯仰 / Y 滚转 / Z 偏航）
            bool up = Math.Abs(NormalizeAngle(v.Rotation.Y)) > 60f;
            if (up && !_wasUpsideDown) Ev("{\"e\":\"rolled_over\"}");
            _wasUpsideDown = up;

            if (v.IsEngineOnFire) Ev("{\"e\":\"vehicle_on_fire\"}");
            if (v.EngineHealth <= 0f && v.Health > 0)
                Ev("{\"e\":\"engine_dead\"}");

            // 行人碰撞：车在动 && 近处有倒地/被撞飞的行人
            float spd = v.Speed;
            if (spd > 2f)
            {
                Ped[] near = World.GetNearbyPeds(p.Position, 12f);
                foreach (Ped q in near)
                {
                    if (q == p || !q.Exists()) continue;
                    if (q.IsRagdoll || q.IsDead)
                    {
                        Ev("{\"e\":\"ped_hit\",\"speed\":" + F(spd) +
                           ",\"dead\":" + (q.IsDead ? "true" : "false") + "}");
                        break;            // 一条 obs 一条就够，别刷屏
                    }
                }
            }
        }
        else
            _wasUpsideDown = false;

        // 任务目标解包：任务几乎都把开车目标挂在雷达 blip 上。
        // 只认 Standard(1) + Yellow(3) 的任务标记（自由模式地图上常驻的
        // 商店/车库/服务图标是专用 sprite，不会误报；玩家手动路点是
        // Waypoint(4)，另有 waypoint 字段，不在这里混）。取距玩家最近一个。
        Blip mission = null; float bestD = float.MaxValue;
        foreach (Blip b in World.GetAllBlips())
        {
            if (!b.Exists()) continue;
            int sp = (int)b.Sprite;
            if (sp == 4) continue;                    // Waypoint：玩家手动路点
            if (sp != 1) continue;                    // Standard：任务类标记
            if ((int)b.Color != 3) continue;          // Yellow：任务目标色
            float d = p.Position.DistanceTo(b.Position);
            if (d < bestD) { bestD = d; mission = b; }
        }
        _missionActive = mission != null;
        _missionTarget = mission != null ? V(mission.Position) : null;
        _missionDist = mission != null ? bestD : -1f;

        // 路点可达面：人手点在地图上的路点可能根本不在路上（院子/死胡同/
        // 山坡），GTA 原生驾驶 AI 只能开到最近的街道节点，于是车会在目标
        // 附近绕圈永远到不了。这里把路点吸附到路网，暴露「可开到的那个点」，
        // 判据按它算才可能成立；偏差量一并给出，人一眼能看出点歪了多远。
        if (Game.IsWaypointActive)
        {
            Vector3 wpm = World.WaypointPosition;
            Vector3 st = World.GetNextPositionOnStreet(wpm, false);
            if (st != Vector3.Zero)
            {
                _wpDrive = new float[] { st.X, st.Y, st.Z };
                _wpOffroad = wpm.DistanceTo2D(st);
            }
            else { _wpDrive = null; _wpOffroad = 0f; }
        }
        else { _wpDrive = null; _wpOffroad = 0f; }

        // 警力面解包：120m 内的警用车辆（IS_THIS_MODEL_A_POLICE_VEHICLE
        // 覆盖警车与警用直升机 polmav）。取最近一台：坐标/距离/鸣笛。
        // 这是「被追捕」的几何事实——通缉等级只有数字，追不追得掉要看这个。
        _policeCount = 0; _policePos = null; _policeDist = -1f; _policeSiren = false;
        float bestP = float.MaxValue;
        foreach (Vehicle q in World.GetNearbyVehicles(p.Position, 120f))
        {
            if (q == null || !q.Exists()) continue;
            if (!Function.Call<bool>(Hash.IS_THIS_MODEL_A_POLICE_VEHICLE, q.Model))
                continue;
            _policeCount++;
            float d = p.Position.DistanceTo(q.Position);
            if (d < bestP)
            {
                bestP = d;
                _policePos = V(q.Position);
                _policeSiren = q.IsSirenActive;
            }
        }
        if (_policeCount > 0) _policeDist = bestP;
    }

    static float NormalizeAngle(float a)   // 归一到 [-180,180]
    {
        a %= 360f;
        if (a > 180f) a -= 360f;
        if (a < -180f) a += 360f;
        return a;
    }

    public MVEBridge()
    {
        try
        {
            _listener = new TcpListener(IPAddress.Any, PORT);   // 局域网可达（Mac mini 连这里）
            _listener.Start();
            Tick += OnTick;
            KeyDown += OnKeyDown;
            Aborted += (s, e) => Cleanup();
            Notification("MVEBridge ON  port " + PORT);
        }
        catch (Exception ex)
        {
            Notification("MVEBridge FAIL: " + ex.Message);
        }
    }

    void OnKeyDown(object sender, KeyEventArgs e)
    {
        if (e.KeyCode != TOGGLE_KEY) return;
        // F9 现在只当「方向盘开关」：MVE 动作会自动接管驾驶，人按 F9 是
        // 主动把方向盘拿回来（或再交出去），不再是学习的激活前置。
        _running = !_running;
        if (_running) Notification("MVE 自动驾驶：MVE 接管中（再按 F9 拿回方向盘）");
        else
        {
            GiveBackControl();
            Notification("MVE 自动驾驶：已交还玩家（下次动作会自动接管）");
        }
    }

    void OnTick(object sender, EventArgs e)
    {
        // ── 接客户端（一次一个）──
        if (_client == null && _listener.Pending())
        {
            _client = _listener.AcceptTcpClient();
            _client.NoDelay = true;
            _stream = _client.GetStream();
        }

        // ── 事件探测：轻探测每个 tick 都跑（不漏通缉/死亡）， ──
        DetectFast();

        // ── 观测节流 ──
        if (Game.GameTime - _lastObs < OBS_HZ_MS) return;
        _lastObs = Game.GameTime;

        // ── 重探测（行人扫描等）放 10Hz ──
        Ped p = Game.Player.Character;
        DetectSlow(p, p.CurrentVehicle);

        // ── 发观测（带走本窗口内攒下的真实事件）──
        SendLine(BuildObs());

        // ── 收动作（非阻塞，一次读一行）──
        // 动作到达即自动接管：地图上选完路点直接学习，无需先按 F9。
        // drive 类动作（drive_to/cruise）在未激活时自动激活（cutscene
        // 演出期间除外，守门逻辑已交还）；人按 F9 拿回方向盘期间
        // （_running=false）drive 类动作被忽略防打架，stop/handback
        // 仍放行——学生停车不抢人手里的方向盘。
        string line = ReadLine();
        if (line == null) return;
        string actT = Grab(line, "t");
        bool isDrive = actT != null &&
                       (actT.Contains("drive_to") || actT.Contains("cruise"));
        if (_running || (isDrive && !Game.IsCutscenePlaying))
        {
            if (!_running && isDrive)
            {
                _running = true;
                Notification("MVE 自动驾驶：动作驱动，自动接管");
            }
            ApplyAction(line);
        }
    }

    // ── 观测构造（手工拼 JSON，格式固定）──
    string BuildObs()
    {
        var sb = new StringBuilder(256);
        Ped p = Game.Player.Character;
        Vehicle v = p.CurrentVehicle;
        float[] pos; float speed; float heading; float eng; string model;
        if (v != null && v.Exists())
        {
            pos = V(v.Position); speed = v.Speed; heading = v.Heading;
            eng = v.EngineHealth; model = v.LocalizedName ?? "";
        }
        else
        {
            pos = V(p.Position); speed = 0f; heading = p.Heading;
            eng = 0f; model = "";
        }
        sb.Append("{\"ts\":").Append(Game.GameTime)
          .Append(",\"running\":").Append(_running ? "true" : "false")
          .Append(",\"on_foot\":").Append(v == null ? "true" : "false")
          .Append(",\"speed\":").Append(F(speed))
          .Append(",\"pos\":[").Append(F(pos[0])).Append(',').Append(F(pos[1]))
          .Append(',').Append(F(pos[2])).Append(']')
          .Append(",\"heading\":").Append(F(heading))
          .Append(",\"health_engine\":").Append(F(eng))
          .Append(",\"in_vehicle_model\":\"").Append(Escape(model)).Append('"')
          .Append(",\"wanted_level\":").Append(_wanted);
        if (v != null && v.Exists())
            sb.Append(",\"health_body\":").Append(F(v.Health))
              .Append(",\"is_engine_running\":").Append(v.IsEngineRunning ? "true" : "false");
        sb.Append(",\"police_count\":").Append(_policeCount);
        if (_policePos != null)
            sb.Append(",\"police_nearest_dist_m\":").Append(F(_policeDist))
              .Append(",\"police_nearest_pos\":[").Append(F(_policePos[0]))
              .Append(',').Append(F(_policePos[1])).Append(',').Append(F(_policePos[2])).Append(']')
              .Append(",\"police_nearest_siren\":").Append(_policeSiren ? "true" : "false");
        if (Game.IsWaypointActive)
        {
            float[] wp = V(World.WaypointPosition);
            sb.Append(",\"waypoint\":[").Append(F(wp[0])).Append(',')
              .Append(F(wp[1])).Append(',').Append(F(wp[2])).Append(']')
              .Append(",\"waypoint_offroad_m\":").Append(F(_wpOffroad));
            if (_wpDrive != null)
                sb.Append(",\"waypoint_drive\":[").Append(F(_wpDrive[0])).Append(',')
                  .Append(F(_wpDrive[1])).Append(',').Append(F(_wpDrive[2])).Append(']');
        }
        else
            sb.Append(",\"waypoint\":null");

        // ── 任务面：目标来自任务 blip（DetectSlow 10Hz 刷新）──
        sb.Append(",\"mission_active\":").Append(_missionActive ? "true" : "false");
        if (_missionTarget != null)
            sb.Append(",\"mission_target\":[").Append(F(_missionTarget[0])).Append(',')
              .Append(F(_missionTarget[1])).Append(',').Append(F(_missionTarget[2])).Append(']')
              .Append(",\"mission_dist_m\":").Append(F(_missionDist));

        // ── events：自上一条 obs 以来真实发生的事件，带走并清空 ──
        sb.Append(",\"events\":[");
        for (int i = 0; i < _events.Count; i++)
        {
            if (i > 0) sb.Append(',');
            sb.Append(_events[i]);
        }
        sb.Append(']');
        _events.Clear();
        sb.Append('}');
        return sb.ToString();
    }

    static float[] V(Vector3 v3) { return new[] { v3.X, v3.Y, v3.Z }; }

    // 浮点 → 不变区域固定格式（避免区域设置把 . 写成 ,）
    static string F(float f) { return f.ToString("0.##",
        System.Globalization.CultureInfo.InvariantCulture); }

    static string Escape(string s)
    {
        return s.Replace("\\", "\\\\").Replace("\"", "\\\"")
                .Replace("\n", " ").Replace("\r", " ");
    }

    // ── 动作解析（正则取键，格式：{"t":"drive_to","x":..,"y":..,...}）──
    static string Grab(string json, string key)
    {
        Match m = Regex.Match(json, "\"" + key + "\"\\s*:\\s*(-?[0-9.]+|true|false|\"[^\"]*\")");
        return m.Success ? m.Groups[1].Value : null;
    }
    static float Num(string json, string key, float dflt)
    {
        string s = Grab(json, key);
        float v;
        return (s != null && float.TryParse(s, System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out v)) ? v : dflt;
    }

    // ── 动作应用（SHVDN TaskInvoker 高层 API，不用裸 native）──
    void ApplyAction(string line)
    {
        Ped p = Game.Player.Character;
        Vehicle v = p.CurrentVehicle;
        string t = Grab(line, "t");
        if (t != null && t.Length >= 2) t = t.Substring(1, t.Length - 2);  // 去引号

        if (v == null || !v.Exists())
        { SendLine("{\"err\":\"not_in_vehicle\"}"); return; }

        switch ((t ?? "").ToLower())
        {
            case "drive_to":   // 开到坐标：MVE 主动作
                float sp = Num(line, "speed", 20f);              // 默认 ~72km/h
                float st = Num(line, "style", 1f);
                var style = (DrivingStyle)(st < 1 ? 1 : (int)st);
                p.Task.DriveTo(v, new Vector3(Num(line, "x", 0f), Num(line, "y", 0f),
                                              Num(line, "z", 0f)), 3f, sp, style);
                break;
            case "cruise":     // 沿路巡航
                p.Task.CruiseWithVehicle(v, Num(line, "speed", 15f), DrivingStyle.Normal);
                break;
            case "stop":       // 停车
                p.Task.ClearAll();
                v.Handbrake = true;
                break;
            case "handback":   // 交还控制
                GiveBackControl();
                _running = false;
                break;
            default:
                SendLine("{\"err\":\"unknown_action\"}");
                break;
        }
    }

    void GiveBackControl()
    {
        Ped p = Game.Player.Character;
        if (p.IsInVehicle())
        {
            p.Task.ClearAll();
            p.CurrentVehicle.Handbrake = false;
        }
    }

    // ── 网络读写（行协议，非阻塞）──
    readonly StringBuilder _rbuf = new StringBuilder();

    void SendLine(string s)
    {
        if (_stream == null) return;
        try
        {
            byte[] b = Encoding.UTF8.GetBytes(s + "\n");
            _stream.Write(b, 0, b.Length);
            _stream.Flush();
        }
        catch { DropClient(); }
    }

    string ReadLine()
    {
        if (_stream == null || !_stream.DataAvailable) return null;
        while (_stream.DataAvailable)
        {
            int c = _stream.ReadByte();
            if (c < 0) { DropClient(); return null; }
            if (c == '\n')
            {
                string line = _rbuf.ToString();
                _rbuf.Length = 0;
                return line;
            }
            _rbuf.Append((char)c);
            if (_rbuf.Length > 8192) { DropClient(); return null; }
        }
        return null;
    }

    void DropClient()
    {
        try { _stream?.Close(); } catch { }
        try { _client?.Close(); } catch { }
        _stream = null; _client = null; _rbuf.Length = 0;
    }

    void Cleanup()
    {
        DropClient();
        try { _listener?.Stop(); } catch { }
    }

    void Notification(string msg)
    {
        Function.Call(Hash._SET_NOTIFICATION_TEXT_ENTRY_2, "STRING");
        Function.Call(Hash._ADD_TEXT_COMPONENT_STRING, msg);
        Function.Call(Hash._DRAW_NOTIFICATION_4, false, true);
    }
}
