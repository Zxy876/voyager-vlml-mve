#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-11-18 面板响应 gzip（源头压，入口无关）。

实锤：人访问走 8443（neko-gateway docker 容器 TLS 入口），不经 nginx ——
上午的 nginx gzip 只救了 80 端口。人到 VPS 链路 ~50KB/s，/api/state 1MB
裸传要 20 秒，"按钮点击响应慢"的主因就是它。

治法：在 Handler._send（全部响应的唯一漏斗）里，body>1KB 且客户端带
Accept-Encoding: gzip 时就地压缩。无活流式端点（旧对话页已删），全部
响应都是整包 Content-Length，安全。
"""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def splice(fname, old, new):
    p = HERE / fname
    src = p.read_text(encoding="utf-8")
    n = src.count(old)
    if n != 1:
        print(f"[SKIP] {fname}: 锚文本出现 {n} 次（期望 1）→ {old[:70]!r}")
        return False
    shutil.copy2(p, str(p) + ".bak_gzip2")
    p.write_text(src.replace(old, new), encoding="utf-8")
    print(f"[OK] {fname}: {old[:60]!r} …")
    return True


ok = splice(
    "dashboard.py",
    '''    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)''',
    '''    def _send(self, code: int, body: bytes, ctype: str) -> None:
        # 2026-11-18：面板自身 gzip。8443 走 neko-gateway(docker) 不经
        # nginx，此前只有 80 入口有压缩 —— 人到 VPS 链路 ~50KB/s，
        # /api/state 1MB 裸传 20 秒。在源头压，哪个入口进来都生效。
        # 全部响应都是整包 Content-Length（无活流式端点），就地压安全。
        if len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
            import gzip as _gz
            body = _gz.compress(body, 5)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)''',
)

if not ok:
    sys.exit(1)
print("ALL PATCHED")
