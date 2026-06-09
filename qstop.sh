#!/usr/bin/env bash
#
# qstop.sh — DeerFlow 一键停止 + 清理
#
# 行为：
#   1) 通过 deerflow 自带的 scripts/serve.sh --stop 停止 Gateway/Frontend/Nginx
#   2) 兜底再按端口 / 关键字清理残留进程
#   3) 清理 PID 文件与本次运行的临时文件（保留 logs 供排错）
#
# 用法：
#   ./qstop.sh           # 正常停止
#   ./qstop.sh --clean   # 停止 + 删除 logs 与 temp 目录
#   ./qstop.sh --force   # 强杀（兜底 SIGKILL 残留进程）

set -uo pipefail

REPO_ROOT="$(builtin cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
cd "$REPO_ROOT"

LOG_DIR="$REPO_ROOT/logs"
PID_DIR="$REPO_ROOT/logs/pids"
TEMP_DIR="$REPO_ROOT/temp"

export PATH="/usr/sbin:/usr/local/sbin:$PATH"

CLEAN_LOGS=false
FORCE_KILL=false
for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN_LOGS=true ;;
        --force|-f) FORCE_KILL=true ;;
        --help|-h)
            cat <<EOF
用法：
  ./qstop.sh           正常停止 DeerFlow（Gateway/Frontend/Nginx）
  ./qstop.sh --clean   停止后清理 logs/ 与 temp/ 目录
  ./qstop.sh --force   强制清理残留进程（SIGKILL 兜底）
EOF
            exit 0 ;;
        *) echo "未知参数: $arg"; exit 1 ;;
    esac
done

is_port_listening() {
    local port=$1
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1 && return 0
    fi
    if command -v ss >/dev/null 2>&1; then
        ss -ltn "( sport = :$port )" 2>/dev/null | tail -n +2 | grep -q . && return 0
    fi
    return 1
}

_is_repo_pid() {
    local pid=$1
    lsof -p "$pid" 2>/dev/null | grep -F "$REPO_ROOT" >/dev/null 2>&1
}

_kill_pattern() {
    local pattern=$1
    local pids=""
    while IFS= read -r pid; do
        if [ -n "$pid" ] && _is_repo_pid "$pid"; then
            case " $pids " in *" $pid "*) ;; *) pids="$pids $pid" ;; esac
        fi
    done < <(pgrep -f "$pattern" 2>/dev/null || true)
    [ -n "$pids" ] && { [ "$FORCE_KILL" = true ] && kill -9 $pids 2>/dev/null || kill $pids 2>/dev/null; } || true
}

_kill_port() {
    local port=$1
    local pids=""
    while IFS= read -r pid; do
        if [ -n "$pid" ] && _is_repo_pid "$pid"; then
            case " $pids " in *" $pid "*) ;; *) pids="$pids $pid" ;; esac
        fi
    done < <(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)
    [ -n "$pids" ] && { [ "$FORCE_KILL" = true ] && kill -9 $pids 2>/dev/null || kill $pids 2>/dev/null; } || true
}

_kill_nginx() {
    if [ -f "$LOG_DIR/nginx.pid" ]; then
        local pid
        read -r pid < "$LOG_DIR/nginx.pid" || true
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            [ "$FORCE_KILL" = true ] && kill -9 "$pid" 2>/dev/null || kill "$pid" 2>/dev/null
        fi
        rm -f "$LOG_DIR/nginx.pid"
    fi
    _kill_pattern "nginx"
}

# ── 1) 优先走 deerflow 自己的 stop（最干净） ─────────────────────────────
SERVE="$REPO_ROOT/scripts/serve.sh"
if [ -x "$SERVE" ]; then
    echo "▶ 调用 scripts/serve.sh --stop"
    "$SERVE" --stop || true
else
    echo "⚠ 缺少 scripts/serve.sh，改用兜底清理"
fi

# ── 2) 兜底清理（如果上面没杀干净） ───────────────────────────────────────
echo "▶ 兜底清理：进程与端口"
_kill_pattern "uvicorn app.gateway.app:app"
_kill_pattern "next dev"
_kill_pattern "next start"
_kill_pattern "next-server"
_kill_nginx
for port in 8001 3000 2026; do _kill_port "$port"; done

# ── 3) 清理 PID 文件 ───────────────────────────────────────────────────
[ -d "$PID_DIR" ] && find "$PID_DIR" -type f -name "*.pid" -delete 2>/dev/null || true

# ── 4) 等待端口彻底释放 ───────────────────────────────────────────────
echo "▶ 等待端口释放"
for port in 8001 3000 2026; do
    elapsed=0
    while is_port_listening "$port"; do
        if [ "$elapsed" -ge 10 ]; then
            echo "  ⚠ 端口 $port 仍占用（10s 内未释放），可加 --force 强杀"
            break
        fi
        sleep 1
        elapsed=$((elapsed+1))
    done
done

# ── 5) 可选：清理 logs / temp ─────────────────────────────────────────
if [ "$CLEAN_LOGS" = true ]; then
    echo "▶ 清理 logs/ 与 temp/"
    rm -rf "$LOG_DIR" 2>/dev/null || true
    rm -rf "$TEMP_DIR" 2>/dev/null || true
fi

echo
echo "=========================================="
echo "  ✔ DeerFlow 已停止"
echo "=========================================="
if [ "$CLEAN_LOGS" = false ]; then
    echo "  日志保留在：$LOG_DIR"
    echo "  完全清理请执行：./qstop.sh --clean"
fi
echo "=========================================="
