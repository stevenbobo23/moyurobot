#!/usr/bin/env python
"""
摸鱼遥控车 - 命令行入口
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# 先加载 pi_client/.env，日志级别等配置才会认它（已有环境变量优先，不会被覆盖）
from moyurobot.env_file import load_env
from moyurobot.logging_setup import setup_logging

load_env()

# 统一日志配置：去重 + 退避 + 限速，避免 lerobot 摄像头刷屏写满磁盘
setup_logging()
logger = logging.getLogger('MoYuRobot')


def cmd_mcp(args):
    """启动 MCP 服务器"""
    from moyurobot.mcp.server import mcp
    
    logger.info("启动 MCP 服务器...")
    mcp.run()


def cmd_web(args):
    """启动 Web 控制器"""
    from moyurobot.web.controller import run_server
    
    host = args.host or "0.0.0.0"
    port = args.port or 8080
    
    logger.info(f"启动 Web 控制器: http://{host}:{port}")
    run_server(host=host, port=port, debug=args.debug)


def cmd_pipe(args):
    """启动 MCP 管道"""
    from moyurobot.mcp.pipe import MCPPipe
    
    endpoint = args.endpoint or os.environ.get("MCP_ENDPOINT")
    if not endpoint:
        logger.error("请指定 --endpoint 或设置 MCP_ENDPOINT 环境变量")
        sys.exit(1)
    
    config_path = args.config or os.environ.get("MCP_CONFIG")
    
    logger.info(f"启动 MCP 管道: {endpoint}")
    
    pipe = MCPPipe(endpoint_url=endpoint, config_path=config_path)
    asyncio.run(pipe.run())


def main():
    """主入口"""
    parser = argparse.ArgumentParser(
        description="🐟 摸鱼遥控车控制系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  moyurobot mcp              # 启动 MCP 服务器
  moyurobot web              # 启动 Web 控制器
  moyurobot web --port 9000  # 指定端口
  moyurobot pipe --endpoint wss://example.com/ws
        """
    )
    
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    parser.add_argument("--log-level", default=None,
                        help="日志级别（DEBUG/INFO/WARNING/ERROR），默认取环境变量 LOG_LEVEL")
    parser.add_argument("--log-file", action="store_true",
                        help="同时写入滚动日志文件（LOG_DIR/LOG_FILE，默认 ~/logs/moyurobot.log）")
    
    # MCP 子命令
    mcp_parser = subparsers.add_parser("mcp", help="启动 MCP 服务器")
    mcp_parser.set_defaults(func=cmd_mcp)
    
    # Web 子命令
    web_parser = subparsers.add_parser("web", help="启动 Web 控制器")
    web_parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    web_parser.add_argument("--port", type=int, default=8080, help="监听端口")
    web_parser.add_argument("--debug", action="store_true", help="调试模式")
    web_parser.set_defaults(func=cmd_web)
    
    # Pipe 子命令
    pipe_parser = subparsers.add_parser("pipe", help="启动 MCP 管道")
    pipe_parser.add_argument("--endpoint", help="WebSocket 端点地址")
    pipe_parser.add_argument("--config", help="MCP 配置文件路径")
    pipe_parser.set_defaults(func=cmd_pipe)
    
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # 命令行参数优先于环境变量，重新配置一次
    if args.log_level or args.log_file:
        setup_logging(level=args.log_level, log_to_file=True if args.log_file else None, force=True)

    args.func(args)


if __name__ == "__main__":
    main()

