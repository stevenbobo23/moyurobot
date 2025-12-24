#!/bin/sh

# ============================================
# 摸鱼遥控车 - 一键启动脚本
# 包含 Web 控制器 + MCP 服务 + MCP 管道
# ============================================

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# 确保日志目录存在
mkdir -p "$HOME/logs"

# 日志函数 (兼容 sh)
log_info() {
    printf "[INFO] %s\n" "$1"
}

log_warn() {
    printf "[WARN] %s\n" "$1"
}

log_error() {
    printf "[ERROR] %s\n" "$1"
}

log_header() {
    printf "\n========================================\n"
    printf "  %s\n" "$1"
    printf "========================================\n\n"
}

# 清理函数
cleanup() {
    log_header "正在停止所有服务..."
    
    # 终止所有已保存的进程
    if [ -n "$WEB_PID" ]; then
        kill $WEB_PID 2>/dev/null && log_info "Web 控制器已停止"
    fi
    if [ -n "$PIPE_PID" ]; then
        kill $PIPE_PID 2>/dev/null && log_info "MCP 管道服务已停止"
    fi
    if [ -n "$TUYA_PID" ]; then
        kill $TUYA_PID 2>/dev/null && log_info "Tuya MCP 服务已停止"
    fi
    
    log_info "清理完成"
    exit 0
}

# 设置信号处理
trap cleanup INT TERM

# 检查公网连接
check_internet() {
    if curl -s --connect-timeout 5 https://www.baidu.com > /dev/null 2>&1; then
        return 0
    else
        return 1
    fi
}

# 检查 Python 环境
check_python() {
    log_info "检查 Python 环境..."
    
    if ! command -v python >/dev/null 2>&1; then
        log_error "未找到 Python，请先安装 Python 3.10+"
        exit 1
    fi
    
    PYTHON_VERSION=$(python --version 2>&1 | cut -d' ' -f2)
    log_info "Python 版本: $PYTHON_VERSION"
}

# 检查依赖
check_dependencies() {
    log_info "检查依赖..."
    
    cd "$PROJECT_ROOT"
    
    python -c "import flask" 2>/dev/null || {
        log_warn "Flask 未安装，正在安装..."
        pip install flask flask-cors
    }
    
    python -c "import fastmcp" 2>/dev/null || {
        log_warn "FastMCP 未安装，正在安装..."
        pip install fastmcp
    }
}

# 启动 Web 控制器（带 MCP HTTP 模式）
start_web_controller() {
    log_header "启动 Web 控制器 (MCP HTTP 模式)"
    
    cd "$PROJECT_ROOT/src"
    
    export PYTHONPATH="$PROJECT_ROOT/src:$PYTHONPATH"
    export WEB_PASSWORD="${WEB_PASSWORD:-moyu123}"
    export ROBOT_ID="${ROBOT_ID:-my_awesome_kiwi}"
    
    log_info "机器人ID: $ROBOT_ID"
    
    # 记录日志
    echo "--- Starting new session $(date) ---" >> "$HOME/logs/moyurobot_web.log"
    
    python -c "
import os
from moyurobot.web.controller import run_server
robot_id = os.environ.get('ROBOT_ID', 'my_awesome_kiwi')
run_server(host='0.0.0.0', port=8080, robot_id=robot_id)
" >> "$HOME/logs/moyurobot_web.log" 2>&1 &
    WEB_PID=$!
    
    log_info "Web 控制器已启动 (PID: $WEB_PID)"
    log_info "访问地址: http://localhost:8080"
    log_info "日志文件: $HOME/logs/moyurobot_web.log"
}

# 等待 Web 服务器就绪
wait_for_web_server() {
    log_info "等待 Web 服务器启动..."
    
    MAX_RETRIES=30
    RETRY_COUNT=0
    
    while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
        # 检查进程是否还在运行
        if ! kill -0 $WEB_PID 2>/dev/null; then
            log_error "Web 服务器进程意外退出！"
            log_error "查看日志: cat $HOME/logs/moyurobot_web.log"
            tail -20 "$HOME/logs/moyurobot_web.log"
            exit 1
        fi
        
        # 检查端口是否可用
        if curl -s http://localhost:8080/ > /dev/null 2>&1; then
            log_info "✓ Web 服务器已就绪!"
            return 0
        fi
        
        RETRY_COUNT=$((RETRY_COUNT + 1))
        printf "  等待中... (%d/%d)\r" "$RETRY_COUNT" "$MAX_RETRIES"
        sleep 1
    done
    
    log_error "Web 服务器启动超时"
    exit 1
}

# 启动 MCP 管道服务
start_mcp_pipe() {
    log_header "启动 MCP 管道服务"
    
    # 设置 MCP 端点（小智 AI）
    export MCP_ENDPOINT="${MCP_ENDPOINT:-wss://api.xiaozhi.me/mcp/?token=eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOjEzNTM4MSwiYWdlbnRJZCI6Njg4NzYyLCJlbmRwb2ludElkIjoiYWdlbnRfNjg4NzYyIiwicHVycG9zZSI6Im1jcC1lbmRwb2ludCIsImlhdCI6MTc2MDI0OTEzNSwiZXhwIjoxNzkxODA2NzM1fQ.CbO0We-fo_qO5DmlP3ugu6G2jehfP_fAzTxoLUngp0htPyWQUbNF9WebLfhZNzAwX_IUiSLb0MkC-hgoF78c3w}"
    
    log_info "MCP 端点已配置"
    
    cd "$PROJECT_ROOT/src"
    
    echo "--- Starting new session $(date) ---" >> "$HOME/logs/moyurobot_pipe.log"
    
    python -m moyurobot.mcp.pipe >> "$HOME/logs/moyurobot_pipe.log" 2>&1 &
    PIPE_PID=$!
    
    log_info "MCP 管道服务已启动 (PID: $PIPE_PID)"
    log_info "日志文件: $HOME/logs/moyurobot_pipe.log"
}

# 启动 Tuya MCP SDK（如果存在）
start_tuya_mcp() {
    TUYA_DIR="/home/bobo/tuya-mcp-sdk"
    
    if [ -d "$TUYA_DIR" ]; then
        log_header "启动 Tuya MCP SDK"
        
        cd "$TUYA_DIR"
        
        echo "--- Starting new session $(date) ---" >> "$HOME/logs/tuya_quick_start.log"
        
        python mcp-python/examples/quick_start.py >> "$HOME/logs/tuya_quick_start.log" 2>&1 &
        TUYA_PID=$!
        
        log_info "Tuya MCP SDK 已启动 (PID: $TUYA_PID)"
        log_info "日志文件: $HOME/logs/tuya_quick_start.log"
        
        cd "$PROJECT_ROOT"
    else
        log_warn "Tuya MCP SDK 目录不存在: $TUYA_DIR"
        log_warn "跳过 Tuya MCP SDK 启动"
    fi
}

# 显示使用说明
show_usage() {
    log_header "🐟 摸鱼遥控车 - 服务已启动"
    
    echo "服务列表："
    echo "  - Web 控制器: http://localhost:8080"
    echo "  - MCP 管道服务: 连接小智 AI"
    if [ -n "$TUYA_PID" ]; then
        echo "  - Tuya MCP SDK: 智能家居控制"
    fi
    echo ""
    echo "默认登录: 输入用户名即可"
    echo ""
    echo "日志文件:"
    echo "  - Web: $HOME/logs/moyurobot_web.log"
    echo "  - MCP 管道: $HOME/logs/moyurobot_pipe.log"
    if [ -n "$TUYA_PID" ]; then
        echo "  - Tuya: $HOME/logs/tuya_quick_start.log"
    fi
    echo ""
    echo "进程 ID:"
    echo "  - Web PID: $WEB_PID"
    echo "  - Pipe PID: $PIPE_PID"
    if [ -n "$TUYA_PID" ]; then
        echo "  - Tuya PID: $TUYA_PID"
    fi
    echo ""
    echo "按 Ctrl+C 停止所有服务"
}

# 检查网络连接
check_network() {
    log_info "检查网络连接..."
    if check_internet; then
        log_info "✓ 公网连接正常"
    else
        log_warn "⚠ 无法连接公网，MCP 管道服务可能无法正常工作"
    fi
}

# 主函数
main() {
    log_header "🐟 摸鱼遥控车 启动中..."
    
    log_info "项目路径: $PROJECT_ROOT"
    
    check_python
    check_dependencies
    check_network
    
    # 启动服务
    start_web_controller
    wait_for_web_server
    
    start_mcp_pipe
    start_tuya_mcp
    
    show_usage
    
    # 等待所有后台进程
    wait
}

main "$@"
