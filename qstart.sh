#!/usr/bin/env bash
#
# qstart.sh — DeerFlow 一键启动（dev 模式，daemon 后台运行）
#
# 行为：
#   1) 复用 deerflow 仓库的 scripts/serve.sh --dev --daemon --skip-install
#   2) 启动 Gateway (8001) + Frontend (3000) + Nginx (2026)
#   3) 入口仍是 http://localhost:2026
#
# 位置：项目根目录
#
# 用法：
#   ./qstart.sh            # 启动（如已在运行则先 stop 再 start）
#   ./qstart.sh --fg       # 前台运行（Ctrl+C 终止，输出到终端）
#   ./qstart.sh --status   # 查看端口与日志位置
#

set -euo pipefail

REPO_ROOT="$(builtin cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
cd "$REPO_ROOT"

LOG_DIR="$REPO_ROOT/logs"
PID_DIR="$REPO_ROOT/logs/pids"

mkdir -p "$LOG_DIR" "$PID_DIR" \
    "$REPO_ROOT/temp/client_body_temp" \
    "$REPO_ROOT/temp/proxy_temp" \
    "$REPO_ROOT/temp/fastcgi_temp" \
    "$REPO_ROOT/temp/uwsgi_temp" \
    "$REPO_ROOT/temp/scgi_temp"

# ── 工具兜底（nginx 不在 PATH 的常见情况） ────────────────────────────────
# 必须放在 check.py 之前——check.py 用 shutil.which 找 nginx，找不到就会判失败
export PATH="/usr/sbin:/usr/local/sbin:$PATH"
if ! command -v nginx >/dev/null 2>&1; then
    if [ -x /usr/sbin/nginx ]; then
        export PATH="/usr/sbin:$PATH"
    elif [ -x /usr/local/sbin/nginx ]; then
        export PATH="/usr/local/sbin:$PATH"
    else
        echo "✗ 未找到 nginx，请先安装：sudo apt install nginx" >&2
        exit 1
    fi
fi

# ── 自检：依赖 + 配置文件 ────────────────────────────────────────────────
# check.py 没有可执行位，用 python 显式调用；PATH 透传给它以便找到 nginx
if command -v python3 >/dev/null 2>&1; then
    CHECK_PY="python3"
elif command -v python >/dev/null 2>&1; then
    CHECK_PY="python"
else
    echo "✗ 找不到 python 解释器" >&2
    exit 1
fi
"$CHECK_PY" ./scripts/check.py >/dev/null 2>&1 || {
    echo "✗ 依赖缺失，请先执行 'make install' / uv sync / pnpm install" >&2
    echo "  (若 nginx 在 /usr/sbin 而非 PATH，可执行：export PATH=/usr/sbin:\$PATH)" >&2
    exit 1
}

if [ ! -f "$REPO_ROOT/config.yaml" ]; then
    echo "✗ 缺少 config.yaml，请先执行 ./scripts/configure.py 或 make setup" >&2
    exit 1
fi

# ── 参数解析 ───────────────────────────────────────────────────────────────
ACTION="start-daemon"
for arg in "$@"; do
    case "$arg" in
        --fg)        ACTION="start-fg" ;;
        --status)    ACTION="status" ;;
        --help|-h)   ACTION="help" ;;
        *)           echo "未知参数: $arg"; ACTION="help" ;;
    esac
done

show_help() {
    cat <<EOF
DeerFlow 一键启动脚本

用法：
  ./qstart.sh            后台启动（默认），返回后服务持续运行
  ./qstart.sh --fg       前台启动（Ctrl+C 终止，输出到终端）
  ./qstart.sh --status   查看端口 / PID / 日志位置
  ./qstart.sh --help     帮助

入口：
  http://localhost:2026   (Nginx 反代 → Gateway 8001 + Frontend 3000)

停止：
  ./qstop.sh
EOF
}

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

# 端口占用诊断：返回占用者的归属（"self"=本仓库、"other"=别的项目、"none"=没人）
# 仅当占用者进程的 cwd 命中本仓库路径时判为 self；命中其他项目则提示用户。
port_owner() {
    local port=$1
    local pid
    pid=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)
    [ -z "$pid" ] && { echo "none"; return; }

    local cwd
    cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
    case "$cwd" in
        "$REPO_ROOT")        echo "self" ;;
        "$REPO_ROOT"/*)      echo "self" ;;
        *)                   echo "other" ;;
    esac
}

show_status() {
    echo "=== DeerFlow 运行状态 ==="
    for port in 8001 3000 2026; do
        if is_port_listening "$port"; then
            owner=$(port_owner "$port")
            pid="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)"
            case "$owner" in
                self|other) echo "  ✔ 端口 $port  监听中  pid=${pid:-?}  归属=$owner" ;;
            esac
        else
            echo "  ✗ 端口 $port  未监听"
        fi
    done
    echo
    echo "日志: $LOG_DIR/{gateway,frontend,nginx}.log"
    echo "停止: $REPO_ROOT/qstop.sh"
}

case "$ACTION" in
    help) show_help; exit 0 ;;
    status) show_status; exit 0 ;;
esac

# ── 启动前：检查 2026 端口是否被本仓库外的进程占用 ───────────────────
# serve.sh 在 nginx 端口冲突时会调用 stop_all 把 gateway/frontend 一起杀掉，
# 这里前置判断可以避免无谓的回滚。如果只是 DeerFlow 自己的残留，
# qstop.sh 已经先清理过了；这里只需拦下"别人占了"的情况。
for port in 2026 8001 3000; do
    if is_port_listening "$port"; then
        owner=$(port_owner "$port")
        if [ "$owner" = "other" ]; then
            pid=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)
            cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
            echo "✗ 端口 $port 已被其他项目占用 (pid=$pid, cwd=$cwd)" >&2
            echo "  请先停掉那个项目，或修改 deerflow 的 docker/nginx/nginx.local.conf / serve.sh 改用别的端口" >&2
            exit 1
        fi
    fi
done
# ── 真正的启动 ────────────────────────────────────────────────────────────
# 复用 deerflow 自己的 serve.sh（处理 uv extras、uvicorn reload、nginx 启动参数等）
SERVE="$REPO_ROOT/scripts/serve.sh"
if [ ! -x "$SERVE" ]; then
    echo "✗ 找不到可执行的 scripts/serve.sh" >&2
    exit 1
fi

if [ "$ACTION" = "start-fg" ]; then
    echo "▶ 前台启动 DeerFlow（Ctrl+C 停止）…"
    exec "$SERVE" --dev
fi

# daemon 模式：先停旧的，再后台启动
"$REPO_ROOT/qstop.sh" >/dev/null 2>&1 || true

echo "▶ 后台启动 DeerFlow（dev 模式）…"
nohup "$SERVE" --dev --daemon --skip-install > "$LOG_DIR/serve-daemon.log" 2>&1 &
echo $! > "$PID_DIR/serve.pid"
disown || true

# 等待 3 个端口就绪
echo "  等待服务就绪："
for entry in "Gateway 8001 30" "Frontend 3000 120" "Nginx 2026 15"; do
    name=$(echo "$entry" | awk '{print $1}')
    port=$(echo "$entry" | awk '{print $2}')
    max=$(echo "$entry" | awk '{print $3}')
    elapsed=0
    while ! is_port_listening "$port"; do
        if [ "$elapsed" -ge "$max" ]; then
            echo "    ✗ $name (port $port) 等待超时"
            echo "      查看日志：tail -50 $LOG_DIR/$(echo $name | tr '[:upper:]' '[:lower:]').log"
            exit 1
        fi
        sleep 1
        elapsed=$((elapsed+1))
    done
    echo "    ✔ $name  →  http://localhost:$port"
done

cat <<EOF

==========================================
  ✔ DeerFlow 已就绪
==========================================
  入口：     http://localhost:2026
  Gateway：  http://localhost:8001
  Frontend： http://localhost:3000

  日志：     $LOG_DIR/{gateway,frontend,nginx}.log
  停止：     $REPO_ROOT/qstop.sh
==========================================
EOF
