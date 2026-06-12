"""
SSE (Server-Sent Events) MCP Server Example

这是一个使用 SSE 传输方式的 MCP 服务器示例。
与 stdio 不同，SSE 使用 HTTP 连接，适合远程访问。
"""

import json
import logging
import os
from datetime import datetime

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("SSEMCPserver")


# 创建 MCP 服务器实例
mcp = FastMCP("sse-demo-server")


@mcp.tool()
def get_current_time() -> str:
    """获取当前时间
    
    Returns:
        当前时间戳和格式化的时间字符串
    """
    now = datetime.now()
    result = {
        "timestamp": now.timestamp(),
        "iso_format": now.isoformat(),
        "formatted": now.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": "Asia/Shanghai"
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def calculate_sum(numbers: list[float]) -> str:
    """计算数字列表的总和
    
    Args:
        numbers: 数字列表
        
    Returns:
        计算结果
    """
    result = {
        "input": numbers,
        "sum": sum(numbers),
        "count": len(numbers),
        "average": sum(numbers) / len(numbers) if numbers else 0
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def greet_user(name: str, language: str = "Chinese") -> str:
    """用指定语言问候用户
    
    Args:
        name: 用户名
        language: 语言 (Chinese, English, Spanish, French, Japanese)
        
    Returns:
        问候消息
    """
    greetings = {
        "Chinese": f"你好，{name}！欢迎使用 SSE MCP 服务器。",
        "English": f"Hello, {name}! Welcome to SSE MCP server.",
        "Spanish": f"¡Hola, {name}! Bienvenido al servidor SSE MCP.",
        "French": f"Bonjour, {name}! Bienvenue sur le serveur SSE MCP.",
        "Japanese": f"こんにちは、{name}さん！SSE MCPサーバーへようこそ。"
    }
    
    message = greetings.get(language, greetings["Chinese"])
    
    result = {
        "name": name,
        "language": language,
        "message": message
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def process_text(text: str, operation: str = "uppercase") -> str:
    """处理文本
    
    Args:
        text: 要处理的文本
        operation: 操作类型 (uppercase, lowercase, reverse, length, word_count)
        
    Returns:
        处理结果
    """
    result = {
        "original": text,
        "operation": operation,
        "result": None
    }
    
    if operation == "uppercase":
        result["result"] = text.upper()
    elif operation == "lowercase":
        result["result"] = text.lower()
    elif operation == "reverse":
        result["result"] = text[::-1]
    elif operation == "length":
        result["result"] = len(text)
    elif operation == "word_count":
        result["result"] = len(text.split())
    else:
        result["result"] = f"Unknown operation: {operation}"
    
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def generate_fibonacci(n: int) -> str:
    """生成斐波那契数列
    
    Args:
        n: 要生成的项数
        
    Returns:
        斐波那契数列
    """
    if n <= 0:
        sequence = []
    elif n == 1:
        sequence = [0]
    elif n == 2:
        sequence = [0, 1]
    else:
        sequence = [0, 1]
        for i in range(2, n):
            sequence.append(sequence[-1] + sequence[-2])
    
    result = {
        "n": n,
        "sequence": sequence,
        "sum": sum(sequence)
    }
    
    return json.dumps(result, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="SSE MCP Server")
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to bind to (default: 8080)"
    )
    
    args = parser.parse_args()
    
    # 设置环境变量来配置 host 和 port
    os.environ["MCP_SSE_HOST"] = args.host
    os.environ["MCP_SSE_PORT"] = str(args.port)
    
    logger.info(f"Starting SSE MCP server on {args.host}:{args.port}")
    logger.info("SSE endpoint: http://{}:{}/sse".format(args.host, args.port))
    
    # 运行 SSE 服务器
    mcp.run(transport="sse")