"""win_forwarder.py — Windows 游戏机侧的小转发器（只出站，免开防火墙入站）。

职责：把本机游戏里的 MVEBridge（127.0.0.1:9777）转发到 VPS 中继，
以 GAME 角色握手。之后 Mac mini 那边连 VPS 同一端口（MVE 角色）即可通话。

用法（Windows 机上，装好 Python 后）：
    py win_forwarder.py --relay 43.161.203.50:9778
游戏必须在跑（MVEBridge 已加载），否则本进程会重试连接。
断线自动重连（5s），长跑无感。
"""
from __future__ import annotations

import argparse
import socket
import threading
import time


def pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def once(relay_host, relay_port) -> None:
    game = socket.create_connection(("127.0.0.1", 9777), timeout=5)
    game.settimeout(None)
    rl = socket.create_connection((relay_host, relay_port), timeout=10)
    rl.settimeout(10)
    rl.sendall(b"GAME\n")
    # 吞掉中继的握手行（">ROLE?\n"），不让它透传进游戏插件被当成动作
    buf = b""
    while b"\n" not in buf:
        chunk = rl.recv(1024)
        if not chunk:
            raise ConnectionError("中继在握手期断开")
        buf += chunk
    rl.settimeout(None)
    # 握手行之后若已有对端数据，塞回流（极少发生，但防丢）
    rest = buf.split(b"\n", 1)[1]
    if rest:
        game.sendall(rest)
    # 等中继确认配对（>ROLE? 后发 GAME，会收到透传的另一侧数据）
    print(f"[fwd] 游戏桥 127.0.0.1:9777 ↔ 中继 {relay_host}:{relay_port} 已接通")
    t1 = threading.Thread(target=pump, args=(game, rl), daemon=True)
    t2 = threading.Thread(target=pump, args=(rl, game), daemon=True)
    t1.start(); t2.start()
    t1.join()      # 任一方向断开即返回，走重连


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay", default="43.161.203.50:9778")
    a = ap.parse_args()
    host, _, port = a.relay.partition(":")
    while True:
        try:
            once(host, int(port or 9778))
        except OSError as e:
            print(f"[fwd] 连接失败（{e}），5s 后重试（游戏要开着）")
        time.sleep(5)


if __name__ == "__main__":
    main()
