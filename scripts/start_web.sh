#!/bin/bash

# Web 控制器启动脚本

# 获取脚本所在目录（兼容不同 shell）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT/src"
export PYTHONPATH="$PROJECT_ROOT/src:$PYTHONPATH"
export WEB_PASSWORD="${WEB_PASSWORD:-moyu123}"

# 机器人ID - 需要与校准文件名匹配
# 校准文件路径: ~/.cache/huggingface/lerobot/calibration/robots/lekiwi/{ROBOT_ID}.json
export ROBOT_ID="${ROBOT_ID:-my_awesome_kiwi}"

echo "🌐 启动 Web 控制器..."
echo "访问地址: http://localhost:8080"
echo "默认密码: moyu123"
echo "机器人ID: $ROBOT_ID"
echo "项目路径: $PROJECT_ROOT"

# 检查校准文件是否存在
CALIB_FILE="$HOME/.cache/huggingface/lerobot/calibration/robots/lekiwi/${ROBOT_ID}.json"
if [ -f "$CALIB_FILE" ]; then
    echo "✓ 找到校准文件: $CALIB_FILE"
else
    echo "⚠️  未找到校准文件: $CALIB_FILE"
    echo "   首次运行需要进行校准，或设置正确的 ROBOT_ID 环境变量"
    echo ""
    # 列出已有的校准文件
    CALIB_DIR="$HOME/.cache/huggingface/lerobot/calibration/robots/lekiwi"
    if [ -d "$CALIB_DIR" ]; then
        echo "   已有的校准文件:"
        ls -la "$CALIB_DIR"/*.json 2>/dev/null || echo "   (无)"
    fi
    echo ""
fi

python -c "
import os
from moyurobot.web.controller import run_server
robot_id = os.environ.get('ROBOT_ID', 'my_awesome_kiwi')
run_server(host='0.0.0.0', port=8080, robot_id=robot_id)
"
