#!/bin/bash

# MCP 服务器启动脚本

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"
export PYTHONPATH="$PROJECT_ROOT:$PYTHONPATH"

# 机器人ID - 需要与校准文件名匹配
export ROBOT_ID="${ROBOT_ID:-my_awesome_kiwi}"

echo "🤖 启动 MCP 服务器..."
echo "机器人ID: $ROBOT_ID"
echo "项目路径: $PROJECT_ROOT"

python -m moyurobot.mcp.server
