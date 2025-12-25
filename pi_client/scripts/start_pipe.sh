#!/bin/bash

# MCP 管道启动脚本

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# 检查必要的环境变量
if [ -z "$MCP_ENDPOINT" ]; then
    echo "错误: 请设置 MCP_ENDPOINT 环境变量"
    echo "示例: export MCP_ENDPOINT='wss://your-server.com/ws'"
    exit 1
fi

cd "$PROJECT_ROOT"
export PYTHONPATH="$PROJECT_ROOT:$PYTHONPATH"
export MCP_CONFIG="$PROJECT_ROOT/config/mcp_config.json"

echo "🔗 启动 MCP 管道..."
echo "连接地址: $MCP_ENDPOINT"
echo "项目路径: $PROJECT_ROOT"

python -c "
import asyncio
import os
from moyurobot.mcp.pipe import MCPPipe

pipe = MCPPipe(
    endpoint_url=os.environ['MCP_ENDPOINT'],
    config_path=os.environ.get('MCP_CONFIG')
)
asyncio.run(pipe.run())
"
