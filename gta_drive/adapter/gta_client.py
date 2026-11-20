"""gta_client.py — MVE ⇆ GTA 桥接客户端（行协议 JSON，与 MVEBridge.cs 对应）。

同一套代码既连真游戏（127.0.0.1:9777），也连本地 mock（test_loop_local.py 起）。
obs 结构见 MVEBridge.Obs；action 四种：drive_to / cruise / stop / handback。
"""
import json
import socket
import time


class GtaClient:
    def __init__(self, host="127.0.0.1", port=9777, timeout=5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""

    # ── 收 ──
    def recv_obs(self):
        """读一行 obs JSON；返回 dict 或 None（对端断开）。"""
        while b"\n" not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        try:
            return json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # ── 发 ──
    def send_action(self, action: dict):
        self.sock.sendall((json.dumps(action) + "\n").encode("utf-8"))

    def drive_to(self, x, y, z=0.0, speed=20.0, style=1):
        self.send_action({"t": "drive_to", "x": x, "y": y, "z": z,
                          "speed": speed, "style": style})

    def cruise(self, speed=15.0):
        self.send_action({"t": "cruise", "speed": speed})

    def stop(self):
        self.send_action({"t": "stop"})

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def dist2d(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
