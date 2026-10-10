#!/bin/bash
# FastAPI + Celery 服务管理脚本
# 用法: ./fastapi_service.sh [start|stop|restart|status]

SERVICE_NAME="fastapi"
PORT=8100
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$PROJECT_DIR/fastapi.pid"
CELERY_PID_FILE="$PROJECT_DIR/celery.pid"

# ============================================
# 加载 .env 配置（如果有）
# .env 中的 VENV_DIR / VENV_PYTHON 会覆盖下面的默认值
# ============================================
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env" 2>/dev/null
    set +a
fi

# ============================================
# 虚拟环境配置
# 优先级: VENV_PYTHON 环境变量 > VENV_DIR/.venv/bin/python > $PROJECT_DIR/.venv/bin/python > 系统 python
# ============================================
# 可通过环境变量配置虚拟环境目录（默认 $PROJECT_DIR/.venv）
VENV_DIR="${VENV_DIR:-$PROJECT_DIR/.venv}"
# 可通过环境变量直接指定 Python 路径（优先级最高）
VENV_PYTHON="${VENV_PYTHON:-$VENV_DIR/bin/python}"

# 如果虚拟环境不存在，使用系统 Python
if [ ! -f "$VENV_PYTHON" ]; then
    VENV_PYTHON="python"
fi

# ============================================

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# ============================================
# FastAPI 进程管理
# ============================================

get_pid() {
    if [ -f "$PID_FILE" ]; then
        cat "$PID_FILE"
    fi
}

is_running() {
    local pid=$(get_pid)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        return 0
    fi
    return 1
}

start_fastapi() {
    if is_running; then
        local pid=$(get_pid)
        log_warn "FastAPI 已在运行中 (PID: $pid)"
        return 1
    fi

    log_info "正在启动 FastAPI 服务..."
    cd "$PROJECT_DIR"

    nohup "$VENV_PYTHON" -m uvicorn app.main:app --port $PORT > "$PROJECT_DIR/logs/fastapi.log" 2>&1 &
    local new_pid=$!

    sleep 3

    if kill -0 "$new_pid" 2>/dev/null; then
        echo $new_pid > "$PID_FILE"
        log_info "FastAPI 启动成功 (PID: $new_pid, Port: $PORT)"
        return 0
    else
        log_error "FastAPI 启动失败，请查看日志: logs/fastapi.log"
        return 1
    fi
}

stop_fastapi() {
    if ! is_running; then
        log_warn "FastAPI 未运行"
        [ -f "$PID_FILE" ] && rm -f "$PID_FILE"
        return 0
    fi

    local pid=$(get_pid)
    log_info "正在停止 FastAPI (PID: $pid)..."
    kill "$pid"

    local count=0
    while kill -0 "$pid" 2>/dev/null && [ $count -lt 10 ]; do
        sleep 1
        count=$((count + 1))
    done

    if kill -0 "$pid" 2>/dev/null; then
        log_warn "强制终止 FastAPI..."
        kill -9 "$pid" 2>/dev/null
    fi

    rm -f "$PID_FILE"
    log_info "FastAPI 已停止"
    return 0
}

# ============================================
# Celery Worker 进程管理
# ============================================

get_celery_pid() {
    if [ -f "$CELERY_PID_FILE" ]; then
        cat "$CELERY_PID_FILE"
    fi
}

is_celery_running() {
    local pid=$(get_celery_pid)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        return 0
    fi
    return 1
}

start_celery() {
    if is_celery_running; then
        local pid=$(get_celery_pid)
        log_warn "Celery Worker 已在运行中 (PID: $pid)"
        return 1
    fi

    log_info "正在启动 Celery Worker..."
    cd "$PROJECT_DIR"

    nohup "$VENV_PYTHON" -m celery -A app.celery_app worker \
        --loglevel=info \
        --concurrency=4 \
        > "$PROJECT_DIR/logs/celery.log" 2>&1 &
    local new_pid=$!

    sleep 3

    if kill -0 "$new_pid" 2>/dev/null; then
        echo $new_pid > "$CELERY_PID_FILE"
        log_info "Celery Worker 启动成功 (PID: $new_pid)"
        return 0
    else
        log_error "Celery Worker 启动失败，请查看日志: logs/celery.log"
        return 1
    fi
}

stop_celery() {
    if ! is_celery_running; then
        log_warn "Celery Worker 未运行"
        [ -f "$CELERY_PID_FILE" ] && rm -f "$CELERY_PID_FILE"
        return 0
    fi

    local pid=$(get_celery_pid)
    log_info "正在停止 Celery Worker (PID: $pid)..."
    # 发送 SIGTERM，Celery 会等待当前任务完成后退出
    kill "$pid"

    local count=0
    while kill -0 "$pid" 2>/dev/null && [ $count -lt 15 ]; do
        sleep 1
        count=$((count + 1))
    done

    if kill -0 "$pid" 2>/dev/null; then
        log_warn "强制终止 Celery Worker..."
        kill -9 "$pid" 2>/dev/null
    fi

    rm -f "$CELERY_PID_FILE"
    log_info "Celery Worker 已停止"
    return 0
}

# ============================================
# 组合操作
# ============================================

start_service() {
    start_fastapi
    start_celery
}

stop_service() {
    stop_fastapi
    stop_celery
}

restart_service() {
    log_info "重启所有服务..."
    stop_service
    sleep 2
    start_service
}

show_status() {
    if is_running; then
        local pid=$(get_pid)
        log_info "FastAPI    : 运行中 (PID: $pid, Port: $PORT)"
    else
        log_warn "FastAPI    : 未运行"
    fi

    if is_celery_running; then
        local pid=$(get_celery_pid)
        log_info "Celery     : 运行中 (PID: $pid)"
    else
        log_warn "Celery     : 未运行"
    fi
}

# 确保日志目录存在
mkdir -p "$PROJECT_DIR/logs"

# 根据参数执行操作
case "${1:-start}" in
    start)
        start_service
        ;;
    stop)
        stop_service
        ;;
    restart)
        restart_service
        ;;
    status)
        show_status
        ;;
    *)
        echo "用法: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
