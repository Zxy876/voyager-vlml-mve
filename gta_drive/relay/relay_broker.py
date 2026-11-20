"""relay_broker.py — MVE↔GTA 桥的 VPS 中继（1×GAME ↔ N×MVE 广播组）。

为什么要中继：Windows 游戏机可能不在 Mac mini 的局域网里（或防火墙不便开
入站）。Windows 侧只**出站**连接 VPS（游戏机插件仍是 localhost:9777 的服务端，
由 win_forwarder.py 转发出来），MVE 侧（Mac 隧道 / VPS 本机面板、种子监视器、
MCP server）也连 VPS——本进程把它们按角色组织：

  GAME 侧：**单槽**（新连接顶掉旧的）——游戏插件只有一份
  MVE 侧：**多连接广播组** —— obs 从 GAME 广播给每一个 MVE；
          动作从任何一个 MVE 汇聚转发给 GAME（同一时刻只有一个学习循环在
          发动作，单游戏语义下安全；种子监视器只读，MCP server 发动作）

  2026-11-21 改造原因：种子监视器（游戏侧事件→自动投种子）与 Phase2 的
  MCP 工具执行都要连桥，旧版 1↔1 配对二者互踢。MVE 侧多路之后：
  监视器看 obs 投种子（人设路点+F9=种子），面板循环经 MCP 工具真开车。

协议（每根连接的第一行）：
  "GAME\n"  ← Windows 侧 win_forwarder
  "MVE\n"   ← MVE 侧任意连接（可多条同时在线）

GAME 断开不清 MVE 侧：广播组留守，新 GAME 上线 obs 自动恢复。
部署（VPS）：systemd 服务 mve-bridge-relay，端口 9778。
"""
from __future__ import annotations

import asyncio
import os

HOST = os.environ.get("RELAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("RELAY_PORT", "9778"))

# GAME 单槽：(reader, writer)；MVE 广播组：{(reader, writer), ...}
_game: tuple | None = None
_mves: set = set()
_game_tx_lock = asyncio.Lock()          # 多 MVE→GAME 汇聚的写锁


def _log(msg: str) -> None:
    print(msg, flush=True)


async def _drop_mve(w) -> None:
    for item in list(_mves):
        if item[1] is w:
            _mves.discard(item)
    try:
        w.close()
    except OSError:
        pass


async def _broadcast_obs(chunk: bytes) -> None:
    """GAME → 所有 MVE（慢/死连接超时即剔除，不拖累全组）。"""
    for item in list(_mves):
        w = item[1]
        try:
            w.write(chunk)
            await asyncio.wait_for(w.drain(), timeout=2.0)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            _mves.discard(item)
            try:
                w.close()
            except OSError:
                pass


async def _send_game(line: bytes) -> bool:
    """任一 MVE → GAME（动作汇聚；GAME 不在就丢弃并如实回 False）。"""
    async with _game_tx_lock:
        if _game is None:
            return False
        w = _game[1]
        try:
            w.write(line)
            await asyncio.wait_for(w.drain(), timeout=2.0)
            return True
        except (ConnectionError, OSError, asyncio.TimeoutError):
            return False


async def _game_loop(reader, writer) -> None:
    global _game
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            await _broadcast_obs(chunk)
    except (ConnectionError, OSError):
        pass
    finally:
        _log("[game] GAME 断开（MVE 广播组留守等待重连）")
        _game = None
        try:
            writer.close()
        except OSError:
            pass


async def _mve_loop(reader, writer) -> None:
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            await _send_game(line)
    except (ConnectionError, OSError):
        pass
    finally:
        _log("[mve] 一个 MVE 侧连接断开")
        await _drop_mve(writer)


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    global _game
    try:
        writer.write(b">ROLE?\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        role = line.decode("utf-8", "ignore").strip().upper()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        writer.close()
        return
    if role == "GAME":
        if _game is not None:
            _log("[conn] 新 GAME 顶掉旧 GAME")
            try:
                _game[1].close()
            except OSError:
                pass
        _game = (reader, writer)
        _log(f"[conn] GAME 在线（MVE 广播组 {_mves and len(_mves) or 0} 个等待）")
        await _game_loop(reader, writer)
    elif role == "MVE":
        _mves.add((reader, writer))
        _log(f"[conn] MVE 侧接入（广播组 {len(_mves)} 个"
             f"{'，GAME 在线' if _game else '，GAME 未到——obs 待 GAME 上线'}）")
        await _mve_loop(reader, writer)
    else:
        writer.write(b"ERR role\n")
        try:
            await writer.drain()
        except OSError:
            pass
        writer.close()


async def main() -> None:
    async def _h(r, w):
        await handle(r, w)

    srv = await asyncio.start_server(_h, HOST, PORT)
    _log(f"relay listening on {HOST}:{PORT}（1×GAME ↔ N×MVE）")
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
