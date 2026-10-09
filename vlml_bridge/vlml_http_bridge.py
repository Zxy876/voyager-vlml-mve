#!/usr/bin/env python3
"""VLML0 的 HTTP 桥 —— 给猫娘伴学插件当取数后端。

为什么存在：宿主（projectneko_server）fork 不安全（实测 7 份崩溃报告：
AtForkPrepare 锁腐坏 / os_log SIGSEGV），插件进程里 subprocess=自杀。
所以取数挪到这个独立常驻进程，插件用纯 socket（HTTP）调它。

接口（标准库 http.server，零第三方依赖）：
  GET  /health -> {"ok": true, "tools": 10}
  POST /query  body {"sql": "..."}            -> DuckDB 行数据（只读 SELECT）
  POST /tool   body {"name": "...", "args": {...}} -> 任意 MCP 工具
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# vlml_mcp.py 与本文件同目录（由 LaunchAgent 的 WorkingDirectory 决定）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from vlml_mcp import VlmlMcpClient, VlmlMcpError  # noqa: E402

PORT = 39481
_client: VlmlMcpClient | None = None


def get_client() -> VlmlMcpClient:
    global _client
    if _client is None or _client._proc is None or _client._proc.poll() is not None:
        _client = VlmlMcpClient(timeout=60.0)
        _client.start()
    return _client


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") in ("/health", ""):
            try:
                n = len(get_client().list_tools())
                self._send(200, {"ok": True, "tools": n})
            except Exception as exc:
                self._send(500, {"ok": False, "error": str(exc)})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path.rstrip("/") == "/query":
                sql = str(payload.get("sql") or "")
                if not sql.lstrip().lower().startswith("select"):
                    self._send(400, {"ok": False, "error": "只读，仅允许 SELECT"})
                    return
                result = get_client().call("query_sql", {"sql_query": sql})
            elif self.path.rstrip("/") == "/tool":
                result = get_client().call(str(payload.get("name")), dict(payload.get("args") or {}))
            else:
                self._send(404, {"ok": False, "error": "not found"})
                return
            self._send(200, {"ok": True, "result": result})
        except VlmlMcpError as exc:
            self._send(502, {"ok": False, "error": f"VLML MCP: {exc}"})
        except Exception as exc:
            self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt, *args):  # 安静
        pass


if __name__ == "__main__":
    # 启动即预热：把 MCP 子进程和 DuckDB 连接先建好
    try:
        n = len(get_client().list_tools())
        print(f"[vlml-bridge] 预热完成，MCP 工具数 = {n}", flush=True)
    except Exception as exc:
        print(f"[vlml-bridge] 预热失败（服务仍会起，请求时重试）: {exc}", flush=True)
    print(f"[vlml-bridge] listening on 127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
