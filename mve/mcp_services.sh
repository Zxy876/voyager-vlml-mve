#!/usr/bin/env bash
# MCP 服务常驻化（VPS 初始化选项）—— 照猫娘的形态：服务挂在网关上，不随进程生死。
#
# 为什么需要它（2026-10-22 用户定方向）
# ------------------------------------
# 之前每个 MVE 进程（面板 / 学习循环 / 消费）**各自 spawn 一套 stdio MCP**：
#   · transport 绑在连接时的事件循环上 → 面板每次 asyncio.run() 换循环就得整批重连；
#   · 每个进程各有一份注册表/开关快照 → 长驻面板读到陈旧态，重放技能 NameError；
#   · 每次 connect_all 都要 npx -y 拉包，慢且容易被网络抖死。
# 现在改成 systemd 常驻 + streamable-http（由 mcp-proxy 把原 stdio server 转出来），
# MVE 侧只发 HTTP POST，连接不再绑任何东西。
#
# 用法（root）：
#   bash mcp_services.sh install     # 建 venv、装依赖、写 systemd、启动、自检
#   bash mcp_services.sh status      # 逐个看 systemd 状态 + HTTP 探活
#   bash mcp_services.sh restart     # 重启全部
#   bash mcp_services.sh logs fetch  # 看某个服务的日志
#   bash mcp_services.sh uninstall   # 停服务并删 unit（不删 venv）
#
# 绑 172.18.0.1（docker 网桥，与本仓库面板同一约定）：不绑 0.0.0.0，公网进不来。
set -euo pipefail

HOST="${MCP_HOST:-172.18.0.1}"
ENV_DIR="${MCP_ENV:-/root/mcp-env}"
MVE_DIR="${MVE_DIR:-/root/mve/mve}"
UNIT_PREFIX="mve-mcp"

# name|port|命令（空格分隔的参数用 :: 分隔，便于数组展开）
SERVICES=(
  "neko|8791|${ENV_DIR}/bin/python::${MVE_DIR}/mcp_neko_server.py"
  "fetch|8792|${ENV_DIR}/bin/mcp-server-fetch"
  "time|8793|${ENV_DIR}/bin/mcp-server-time"
  "ddg|8794|${ENV_DIR}/bin/duckduckgo-mcp-server"
  "sequential-thinking|8795|npx::-y::@modelcontextprotocol/server-sequential-thinking"
  "context7|8796|npx::-y::@upstash/context7-mcp"
)

need_root() {
  [ "$(id -u)" = "0" ] || { echo "需要 root（systemctl 要写 /etc/systemd/system）"; exit 1; }
}

install_deps() {
  if [ ! -x "${ENV_DIR}/bin/mcp-proxy" ]; then
    echo "== 建桥接 venv：${ENV_DIR}"
    python3 -m venv "${ENV_DIR}"
    "${ENV_DIR}/bin/pip" install -q --upgrade pip
    "${ENV_DIR}/bin/pip" install -q mcp-proxy mcp-server-fetch mcp-server-time duckduckgo-mcp-server
  else
    echo "== 桥接 venv 已存在：${ENV_DIR}"
  fi
  command -v npx >/dev/null || { echo "缺 npx（装 nodejs 再来）"; exit 1; }
}

write_unit() {
  local name="$1" port="$2" cmdline="$3"
  local unit="/etc/systemd/system/${UNIT_PREFIX}-${name}.service"
  local exec_args=()
  IFS='::' read -r -a exec_args <<< "$cmdline"
  # neko 的 server 要 import net_tools（在 mve/ 下）
  local extra_env=""
  if [ "$name" = "neko" ]; then
    extra_env="Environment=PYTHONPATH=${MVE_DIR}"
  fi
  cat > "$unit" <<UNIT
[Unit]
Description=MVE MCP service: ${name} (streamable-http ${HOST}:${port})
After=network.target docker.service
Wants=network-online.target

[Service]
Type=simple
Environment=PATH=${ENV_DIR}/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=/root
${extra_env}
WorkingDirectory=/root
ExecStart=/usr/bin/env ${ENV_DIR}/bin/mcp-proxy --host ${HOST} --port ${port} -- ${exec_args[*]}
Restart=always
RestartSec=3
# stdio 子进程可能残留：整组杀掉
KillMode=control-group

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now "${UNIT_PREFIX}-${name}.service" >/dev/null 2>&1 || true
  systemctl restart "${UNIT_PREFIX}-${name}.service"
  echo "  · ${name} → http://${HOST}:${port}/mcp"
}

probe() {
  local name="$1" port="$2"
  local state; state=$(systemctl is-active "${UNIT_PREFIX}-${name}.service" 2>/dev/null || true)
  local code
  code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' -X POST "http://${HOST}:${port}/mcp" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' 2>/dev/null || echo 000)
  printf '  %-20s systemd=%-9s http=%s\n' "$name" "$state" "$code"
}

cmd_install() {
  need_root
  install_deps
  for svc in "${SERVICES[@]}"; do
    IFS='|' read -r name port cmd <<< "$svc"
    echo "== 安装 ${name}（:${port}）"
    write_unit "$name" "$port" "$cmd"
    sleep 1
  done
  echo "== 自检"
  cmd_status
}

cmd_status() {
  for svc in "${SERVICES[@]}"; do
    IFS='|' read -r name port _ <<< "$svc"
    probe "$name" "$port"
  done
}

cmd_restart() {
  need_root
  for svc in "${SERVICES[@]}"; do
    IFS='|' read -r name _p _c <<< "$svc"
    systemctl restart "${UNIT_PREFIX}-${name}.service"
  done
  sleep 2; cmd_status
}

cmd_logs() {
  local name="${1:-fetch}"
  journalctl -u "${UNIT_PREFIX}-${name}.service" -n 60 --no-pager
}

cmd_uninstall() {
  need_root
  for svc in "${SERVICES[@]}"; do
    IFS='|' read -r name _p _c <<< "$svc"
    systemctl disable --now "${UNIT_PREFIX}-${name}.service" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/${UNIT_PREFIX}-${name}.service"
  done
  systemctl daemon-reload
  echo "已移除 unit（venv ${ENV_DIR} 保留）"
}

case "${1:-install}" in
  install)   cmd_install ;;
  status)    cmd_status ;;
  restart)   cmd_restart ;;
  logs)      shift; cmd_logs "$@" ;;
  uninstall) cmd_uninstall ;;
  *) echo "用法：$0 {install|status|restart|logs <name>|uninstall}"; exit 2 ;;
esac
