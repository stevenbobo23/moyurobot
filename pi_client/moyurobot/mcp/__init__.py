"""MCP (Model Context Protocol) 服务模块"""

from .server import mcp, get_service
from .pipe import MCPPipe

__all__ = [
    "mcp",
    "get_service",
    "MCPPipe",
]

