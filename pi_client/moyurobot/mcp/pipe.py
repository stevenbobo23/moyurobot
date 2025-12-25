"""
MCP stdio <-> WebSocket 管道服务

用于连接本地 MCP 服务器和远程 WebSocket 端点
"""

import asyncio
import websockets
import subprocess
import logging
import os
import signal
import sys
import json
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('MoYu_MCP_PIPE')

# 重连设置
INITIAL_BACKOFF = 1  # 初始等待时间（秒）
MAX_BACKOFF = 600  # 最大等待时间（秒）


class MCPPipe:
    """MCP 管道服务类"""
    
    def __init__(self, endpoint_url: str, config_path: str = None):
        """
        初始化 MCP 管道
        
        Args:
            endpoint_url: WebSocket 端点 URL
            config_path: MCP 配置文件路径
        """
        self.endpoint_url = endpoint_url
        self.config_path = config_path or os.environ.get("MCP_CONFIG")
        self._config = None
    
    def load_config(self):
        """加载 MCP 配置文件"""
        if self._config is not None:
            return self._config
            
        path = self.config_path or os.path.join(os.getcwd(), "mcp_config.json")
        if not os.path.exists(path):
            self._config = {}
            return self._config
            
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._config = json.load(f)
        except Exception as e:
            logger.warning(f"加载配置文件失败 {path}: {e}")
            self._config = {}
        
        return self._config
    
    def build_server_command(self, target: str = None):
        """
        构建服务器启动命令
        
        Args:
            target: 服务器名称或脚本路径
            
        Returns:
            tuple: (命令列表, 环境变量字典)
        """
        cfg = self.load_config()
        servers = cfg.get("mcpServers", {}) if isinstance(cfg, dict) else {}
        
        if target in servers:
            entry = servers[target] or {}
            if entry.get("disabled"):
                raise RuntimeError(f"服务器 '{target}' 已禁用")
            
            typ = (entry.get("type") or entry.get("transportType") or "stdio").lower()
            
            child_env = os.environ.copy()
            for k, v in (entry.get("env") or {}).items():
                child_env[str(k)] = str(v)
            
            if typ == "stdio":
                command = entry.get("command")
                args = entry.get("args") or []
                if not command:
                    raise RuntimeError(f"服务器 '{target}' 缺少 'command' 配置")
                return [command, *args], child_env
            
            if typ in ("sse", "http", "streamablehttp"):
                url = entry.get("url")
                if not url:
                    raise RuntimeError(f"服务器 '{target}' (类型 {typ}) 缺少 'url' 配置")
                cmd = [sys.executable, "-m", "mcp_proxy"]
                if typ in ("http", "streamablehttp"):
                    cmd += ["--transport", "streamablehttp"]
                headers = entry.get("headers") or {}
                for hk, hv in headers.items():
                    cmd += ["-H", hk, str(hv)]
                cmd.append(url)
                return cmd, child_env
            
            raise RuntimeError(f"不支持的服务器类型: {typ}")
        
        # 回退到脚本路径模式
        script_path = target
        if not os.path.exists(script_path):
            raise RuntimeError(f"'{target}' 既不是配置的服务器也不是存在的脚本")
        return [sys.executable, script_path], os.environ.copy()
    
    async def connect_with_retry(self, target: str):
        """带重试机制连接到 WebSocket 服务器"""
        reconnect_attempt = 0
        backoff = INITIAL_BACKOFF
        
        while True:
            try:
                if reconnect_attempt > 0:
                    logger.info(f"[{target}] 等待 {backoff}s 后重试第 {reconnect_attempt} 次...")
                    await asyncio.sleep(backoff)
                
                await self._connect_to_server(target)
                
            except Exception as e:
                reconnect_attempt += 1
                logger.warning(f"[{target}] 连接关闭 (第 {reconnect_attempt} 次): {e}")
                backoff = min(backoff * 2, MAX_BACKOFF)
    
    async def _connect_to_server(self, target: str):
        """连接到 WebSocket 服务器并建立管道"""
        try:
            logger.info(f"[{target}] 连接 WebSocket 服务器...")
            async with websockets.connect(self.endpoint_url) as websocket:
                logger.info(f"[{target}] ✓ WebSocket 连接成功")
                
                # 启动服务器进程
                cmd, env = self.build_server_command(target)
                process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    encoding='utf-8',
                    text=True,
                    env=env
                )
                logger.info(f"[{target}] 启动服务器进程: {' '.join(cmd)}")
                
                # 创建双向管道任务
                await asyncio.gather(
                    self._pipe_websocket_to_process(websocket, process, target),
                    self._pipe_process_to_websocket(process, websocket, target),
                    self._pipe_process_stderr_to_terminal(process, target)
                )
        except websockets.exceptions.ConnectionClosed as e:
            logger.error(f"[{target}] WebSocket 连接关闭: {e}")
            raise
        except Exception as e:
            logger.error(f"[{target}] 连接错误: {e}")
            raise
        finally:
            if 'process' in locals():
                logger.info(f"[{target}] 终止服务器进程")
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                logger.info(f"[{target}] 服务器进程已终止")
    
    async def _pipe_websocket_to_process(self, websocket, process, target: str):
        """从 WebSocket 读取数据并写入进程 stdin"""
        try:
            while True:
                message = await websocket.recv()
                logger.debug(f"[{target}] << {message[:120]}...")
                
                if isinstance(message, bytes):
                    message = message.decode('utf-8')
                process.stdin.write(message + '\n')
                process.stdin.flush()
        except Exception as e:
            logger.error(f"[{target}] WebSocket->进程管道错误: {e}")
            raise
        finally:
            if not process.stdin.closed:
                process.stdin.close()
    
    async def _pipe_process_to_websocket(self, process, websocket, target: str):
        """从进程 stdout 读取数据并发送到 WebSocket"""
        try:
            while True:
                data = await asyncio.to_thread(process.stdout.readline)
                
                if not data:
                    logger.info(f"[{target}] 进程输出结束")
                    break
                
                logger.debug(f"[{target}] >> {data[:120]}...")
                await websocket.send(data)
        except Exception as e:
            logger.error(f"[{target}] 进程->WebSocket管道错误: {e}")
            raise
    
    async def _pipe_process_stderr_to_terminal(self, process, target: str):
        """从进程 stderr 读取数据并输出到终端"""
        try:
            while True:
                data = await asyncio.to_thread(process.stderr.readline)
                
                if not data:
                    logger.info(f"[{target}] 进程 stderr 输出结束")
                    break
                
                sys.stderr.write(data)
                sys.stderr.flush()
        except Exception as e:
            logger.error(f"[{target}] 进程 stderr 管道错误: {e}")
            raise
    
    async def run(self, target: str = None):
        """
        运行 MCP 管道
        
        Args:
            target: 目标服务器名称。如果不指定，运行配置中所有启用的服务器
        """
        if not target:
            cfg = self.load_config()
            servers_cfg = cfg.get("mcpServers") or {}
            all_servers = list(servers_cfg.keys())
            enabled = [name for name, entry in servers_cfg.items() if not (entry or {}).get("disabled")]
            skipped = [name for name in all_servers if name not in enabled]
            
            if skipped:
                logger.info(f"跳过禁用的服务器: {', '.join(skipped)}")
            if not enabled:
                raise RuntimeError("配置中没有启用的 mcpServers")
            
            logger.info(f"启动服务器: {', '.join(enabled)}")
            tasks = [asyncio.create_task(self.connect_with_retry(t)) for t in enabled]
            await asyncio.gather(*tasks)
        else:
            if os.path.exists(target):
                await self.connect_with_retry(target)
            else:
                logger.error("参数必须是本地 Python 脚本路径。要运行配置的服务器，请不带参数运行。")
                sys.exit(1)


def signal_handler(sig, frame):
    """处理中断信号"""
    logger.info("收到中断信号，正在关闭...")
    sys.exit(0)


def main():
    """主入口函数"""
    signal.signal(signal.SIGINT, signal_handler)
    
    endpoint_url = os.environ.get('MCP_ENDPOINT')
    if not endpoint_url:
        logger.error("请设置 MCP_ENDPOINT 环境变量")
        sys.exit(1)
    
    target_arg = sys.argv[1] if len(sys.argv) >= 2 else None
    
    pipe = MCPPipe(endpoint_url)
    
    try:
        asyncio.run(pipe.run(target_arg))
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"程序执行错误: {e}")


if __name__ == "__main__":
    main()

