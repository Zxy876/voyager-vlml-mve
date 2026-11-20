"""relay_broker.py — MVE↔GTA 桥的 VPS 中继（配对管道）。

为什么要中继：Windows 游戏机可能不在 Mac mini 的局域网里（或防火墙不便开
入站）。Windows 侧只**出站**连接 VPS（游戏机插件仍是 localhost:9777 的服务端，
由 win_forwarder.py 转发出来），Mac 也连 VPS——本进程把两根连接按角色配对，
之后纯字节双向透传。

协议（每根连接的第一行）：
  "GAME\n"  ← Windows 侧 win_forwarder
  "MVE\n"   ← Mac 侧 run_first_automation
配对成功后透传；任一侧断开，另一侧连接关闭、回到等待。

部署（VPS）：systemd 服务 mve-bridge-relay，端口 9778。
"""
from __future__ import annotations

import asyncio
import os

HOST = os.environ.get("RELAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("RELAY_PORT", "9778"))

_pending: dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter] | None] = {
    "GAME": None, "MVE": None}


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            dst.close()
        except OSError:
            pass


async def _pair(a, b) -> None:
    (ra, wa), (rb, wb) = a, b
    peer = {id(wa): wb, id(wb): wa}
    tasks = [asyncio.create_task(_pipe(ra, wb)),
             asyncio.create_task(_pipe(rb, wa))]
    print(f"[pair] GAME↔MVE 已接通", flush=True)
    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        if not t.done():
            t.cancel()
    for w in peer.values():
        try:
            w.close()
        except OSError:
            pass
    print("[pair] 断开，等待重连", flush=True)


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        writer.write(b">ROLE?\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        role = line.decode("utf-8", "ignore").strip().upper()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        writer.close()
        return
    if role not in _pending:
        writer.write(b"ERR role\n")
        await writer.drain()
        writer.close()
        return

    old = _pending[role]
    _pending[role] = (reader, writer)
    if old is not None:
        try:
            old[1].close()      # 顶掉同角色的旧连接
        except OSError:
            pass
    other_role = "MVE" if role == "GAME" else "GAME"
    print(f"[conn] {role} 已连接（对端{'在线，配对' if _pending[other_role] else '未到，等待'}）",
          flush=True)
    other = _pending[other_role]
    if other is None:
        return                  # 等对端；对端到来时由它发起配对
    _pending[role] = None
    _pending[other_role] = None
    await _pair((reader, writer), other)


async def main() -> None:
    async def _h(r, w):
        try:
            await handle(r, w)
        finally:
            pass

    srv = await asyncio.start_server(_h, HOST, PORT)
    print(f"relay listening on {HOST}:{PORT}", flush=True)
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
