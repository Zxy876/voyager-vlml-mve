// MVEBridge.cs — MVE 自动驾驶线 · GTA V 桥接插件（ScriptHookVDotNet v3）
//
// 作用：游戏内起一个本地 TCP 服务（127.0.0.1:9777），
//   - 每秒 ~10Hz 向外发一行 JSON 观测（obs）：车速/位置/朝向/路点/附近车辆
//   - 接收一行 JSON 动作（action）：drive_to / cruise / stop
// 玩家侧完全不受影响：不按键时插件零干预；F9 激活后也只是"驾驶任务"，
// 下车、按 W/A/S/D 或再按 F9 即交还控制权。
//
// 安装：编译为 MVEBridge.dll 放进 GTA V 目录 scripts/（SHVDN v3 nightly）。
// 仅限剧情模式！联机会被判定作弊封号。

using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Web.Script.Serialization;
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
    JavaScriptSerializer _json = new JavaScriptSerializer();
    bool _running = false;                // MVE 是否在接管驾驶
    int _lastObs = 0;

    public MVEBridge()
    {
        // TCP 服务随游戏启动常驻；是否接管驾驶由 F9 决定
        try
        {
            _listener = new TcpListener(IPAddress.Loopback, PORT);
            _listener.Start();
            Tick += OnTick;
            KeyDown += OnKeyDown;
            Aborted += (s, e) => Cleanup();
        }
        catch (Exception ex)
        {
            Notification("MVEBridge 启动失败: " + ex.Message);
        }
    }

    void OnKeyDown(object sender, KeyEventArgs e)
    {
        if (e.KeyCode != TOGGLE_KEY) return;
        _running = !_running;
        if (_running) Notification("MVE 自动驾驶：已激活（再次按 F9 交还）");
        else
        {
            GiveBackControl();
            Notification("MVE 自动驾驶：已交还玩家");
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

        // ── 观测节流 ──
        if (Game.GameTime - _lastObs < OBS_HZ_MS) return;
        _lastObs = Game.GameTime;

        // ── 发观测 ──
        SendLine(BuildObs());

        // ── 收动作（非阻塞，一次读一行）──
        string line = ReadLine();
        if (line != null && _running)
        {
            try { ApplyAction(_json.Deserialize<ScriptAction>(line)); }
            catch (Exception ex) { SendLine("{\"err\":\"" + ex.Message + "\"}"); }
        }
    }

    // ── 观测构造 ──
    string BuildObs()
    {
        var o = new Obs();
        o.ts = Game.GameTime;
        o.running = _running;
        Ped p = Game.Player.Character;
        o.on_foot = !p.IsInVehicle();
        Vehicle v = p.CurrentVehicle;
        if (v != null && v.Exists())
        {
            o.speed = v.Speed;                       // m/s
            o.pos = V(v.Position);
            o.heading = v.Heading;
            o.health_engine = v.EngineHealth;
            o.in_vehicle_model = v.LocalizedName;
        }
        else
        {
            o.speed = 0f;
            o.pos = V(p.Position);
            o.heading = p.Heading;
        }
        if (Game.IsWaypointActive)
            o.waypoint = V(World.WaypointPosition);
        else
            o.waypoint = null;

        return _json.Serialize(o);
    }

    static float[] V(Vector3 v3) { return new[] { v3.X, v3.Y, v3.Z }; }

    // ── 动作应用（SHVDN TaskInvoker 高层 API，不用裸 native）──
    void ApplyAction(ScriptAction a)
    {
        Ped p = Game.Player.Character;
        Vehicle v = p.CurrentVehicle;
        if (v == null || !v.Exists()) { SendLine("{\"err\":\"not_in_vehicle\"}"); return; }

        switch ((a.t ?? "").ToLower())
        {
            case "drive_to":   // 开到坐标：MVE 主动作
                float sp = a.speed <= 0 ? 20f : a.speed;         // 默认 ~72km/h
                var style = (DrivingStyle)(a.style == 0 ? (int)DrivingStyle.Normal : a.style);
                p.Task.DriveTo(v, new Vector3(a.x, a.y, a.z), 3f, sp, style);
                break;
            case "cruise":     // 沿路巡航
                p.Task.CruiseWithVehicle(v, a.speed <= 0 ? 15f : a.speed, DrivingStyle.Normal);
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

    // ── JSON 载体 ──
    class Obs
    {
        public long ts;
        public bool running;
        public bool on_foot;
        public float speed;
        public float[] pos;
        public float heading;
        public float health_engine;
        public string in_vehicle_model;
        public float[] waypoint;
    }

    class ScriptAction
    {
        public string t;      // drive_to | cruise | stop | handback
        public float x, y, z;
        public float speed;
        public int style;     // 1=Normal 2=IgnoreLights 3=Rushed 4=AvoidObstacles
    }
}
