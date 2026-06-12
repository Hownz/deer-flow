#!/usr/bin/env python3
"""
RAGFlow MCP Server for Agent-CAD
基于 RAGFlow 的 MCP 服务器代码适配，提供文档检索功能
"""

import json
import logging
import time
import random
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any, List, Optional, Dict

import click
import httpx
import mcp.types as types
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("RAGFlowMCP")

# 默认配置
RAG_BASE_URL = "http://127.0.0.1:9380"
DEFAULT_API_KEY = "sk-10a4d2754ea74dcf85ec4f4b6c35a650"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9382

CACHE_TTL = 300  # 缓存时间（秒）
MAX_DATASET_CACHE = 32


class RAGFlowConnector:
    """RAGFlow API 连接器"""
    
    def __init__(self, base_url: str, version: str = "v1"):
        self.base_url = base_url
        self.version = version
        self.api_url = f"{self.base_url}/api/{self.version}"
        self._async_client = None
        
        # 缓存
        self._dataset_metadata_cache = OrderedDict()  # dataset_id -> (metadata, expiry_ts)
        self._document_metadata_cache = OrderedDict()  # dataset_id -> ([(doc_id, metadata)], expiry_ts)
    
    async def _get_client(self) -> httpx.AsyncClient:
        """获取异步 HTTP 客户端"""
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        return self._async_client
    
    async def close(self):
        """关闭连接"""
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None
    
    async def _post(self, path: str, json_data: dict, api_key: str) -> Optional[httpx.Response]:
        """发送 POST 请求"""
        if not api_key:
            logger.error("API key is required")
            return None
        
        client = await self._get_client()
        try:
            return await client.post(
                url=self.api_url + path,
                json=json_data,
                headers={"Authorization": f"Bearer {api_key}"}
            )
        except Exception as e:
            logger.error(f"POST request failed: {e}")
            return None
    
    async def _get(self, path: str, params: dict, api_key: str) -> Optional[httpx.Response]:
        """发送 GET 请求"""
        if not api_key:
            logger.error("API key is required")
            return None
        
        client = await self._get_client()
        try:
            return await client.get(
                url=self.api_url + path,
                params=params,
                headers={"Authorization": f"Bearer {api_key}"}
            )
        except Exception as e:
            logger.error(f"GET request failed: {e}")
            return None
    
    def _is_cache_valid(self, ts: float) -> bool:
        """检查缓存是否有效"""
        return time.time() < ts
    
    def _get_expiry_timestamp(self) -> float:
        """获取过期时间戳"""
        offset = random.randint(-30, 30)
        return time.time() + CACHE_TTL + offset
    
    def _get_cached_dataset_metadata(self, dataset_id: str) -> Optional[dict]:
        """从缓存获取数据集元数据"""
        entry = self._dataset_metadata_cache.get(dataset_id)
        if entry:
            data, ts = entry
            if self._is_cache_valid(ts):
                self._dataset_metadata_cache.move_to_end(dataset_id)
                return data
        return None
    
    def _set_cached_dataset_metadata(self, dataset_id: str, metadata: dict):
        """设置数据集元数据缓存"""
        self._dataset_metadata_cache[dataset_id] = (metadata, self._get_expiry_timestamp())
        self._dataset_metadata_cache.move_to_end(dataset_id)
        if len(self._dataset_metadata_cache) > MAX_DATASET_CACHE:
            self._dataset_metadata_cache.popitem(last=False)
    
    async def list_datasets(self, api_key: str) -> List[dict]:
        """列出所有数据集"""
        params = {
            "page": 1,
            "page_size": 1000,
            "orderby": "create_time",
            "desc": True
        }
        
        res = await self._get("/datasets", params, api_key=api_key)
        if not res or res.status_code != 200:
            logger.error(f"Failed to list datasets: {res.status_code if res else 'No response'}")
            return []
        
        try:
            data = res.json()
            if data.get("code") == 0:
                datasets = []
                for item in data.get("data", []):
                    datasets.append({
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "description": item.get("description", "")
                    })
                return datasets
            else:
                logger.error(f"API error: {data.get('message')}")
                return []
        except Exception as e:
            logger.error(f"Failed to parse response: {e}")
            return []
    
    async def retrieval(
        self,
        api_key: str,
        question: str,
        dataset_ids: Optional[List[str]] = None,
        document_ids: Optional[List[str]] = None,
        page: int = 1,
        page_size: int = 10,
        similarity_threshold: float = 0.2,
        vector_similarity_weight: float = 0.3,
        keyword: bool = False,
        top_k: int = 1024,
        rerank_id: Optional[str] = None,
        force_refresh: bool = False
    ) -> List[types.TextContent]:
        """执行检索"""
        if dataset_ids is None:
            dataset_ids = []
        if document_ids is None:
            document_ids = []
        
        # 如果没有提供数据集ID，获取所有数据集
        if not dataset_ids:
            datasets = await self.list_datasets(api_key)
            dataset_ids = [dataset["id"] for dataset in datasets]
        
        # 准备请求数据
        request_data = {
            "question": question,
            "dataset_ids": dataset_ids,
            "document_ids": document_ids,
            "page": page,
            "page_size": page_size,
            "similarity_threshold": similarity_threshold,
            "vector_similarity_weight": vector_similarity_weight,
            "keyword": keyword,
            "top_k": top_k,
            "rerank_id": rerank_id
        }
        
        logger.info(f"Sending retrieval request: {json.dumps(request_data, ensure_ascii=False)}")
        
        # 发送请求
        res = await self._post("/retrieval", request_data, api_key=api_key)
        if not res or res.status_code != 200:
            error_msg = f"检索请求失败: {res.status_code if res else '无响应'}"
            logger.error(error_msg)
            return [types.TextContent(type="text", text=json.dumps({
                "error": error_msg,
                "success": False
            }, ensure_ascii=False))]
        
        try:
            data = res.json()
            if data.get("code") == 0:
                result_data = data.get("data", {})
                
                # 构建响应
                response = {
                    "success": True,
                    "chunks": result_data.get("chunks", []),
                    "total": result_data.get("total", 0),
                    "page": result_data.get("page", page),
                    "page_size": result_data.get("page_size", page_size),
                    "query_info": {
                        "question": question,
                        "dataset_ids": dataset_ids,
                        "similarity_threshold": similarity_threshold
                    }
                }
                
                return [types.TextContent(
                    type="text",
                    text=json.dumps(response, ensure_ascii=False, indent=2)
                )]
            else:
                error_msg = data.get("message", "未知错误")
                logger.error(f"API返回错误: {error_msg}")
                return [types.TextContent(
                    type="text",
                    text=json.dumps({
                        "error": error_msg,
                        "success": False
                    }, ensure_ascii=False)
                )]
        except Exception as e:
            error_msg = f"解析响应失败: {str(e)}"
            logger.error(error_msg)
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "error": error_msg,
                    "success": False
                }, ensure_ascii=False)
            )]


class RAGFlowContext:
    """RAGFlow 上下文"""
    
    def __init__(self, connector: RAGFlowConnector):
        self.conn = connector


@asynccontextmanager
async def server_lifespan(server: Server) -> AsyncIterator[Dict[str, Any]]:
    """服务器生命周期管理"""
    # 从环境变量或配置获取参数
    base_url = RAG_BASE_URL
    api_key = DEFAULT_API_KEY
    
    # 创建连接器
    connector = RAGFlowConnector(base_url=base_url)
    ctx = RAGFlowContext(connector)
    
    logger.info("RAGFlow MCP 服务器启动")
    logger.info(f"连接到 RAGFlow: {base_url}")
    
    try:
        yield {"ragflow_ctx": ctx}
    finally:
        await ctx.conn.close()
        logger.info("RAGFlow MCP 服务器关闭")


# 创建 MCP 服务器
app = Server("ragflow-mcp-server", lifespan=server_lifespan)


def with_api_key(func):
    """API 密钥装饰器"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        ctx = app.request_context
        ragflow_ctx = ctx.lifespan_context.get("ragflow_ctx")
        if not ragflow_ctx:
            raise ValueError("获取 RAGFlow 上下文失败")
        
        # 使用默认 API 密钥
        api_key = DEFAULT_API_KEY
        if not api_key:
            logger.warning("API 密钥未设置，部分功能可能不可用")
        
        return await func(*args, connector=ragflow_ctx.conn, api_key=api_key, **kwargs)
    
    return wrapper


@app.list_tools()
@with_api_key
async def list_tools(*, connector: RAGFlowConnector, api_key: str) -> List[types.Tool]:
    """列出可用工具"""
    # 获取数据集列表用于工具描述
    datasets = await connector.list_datasets(api_key)
    dataset_info = "\n".join([
        f"- {dataset['name']} (ID: {dataset['id']}): {dataset['description']}"
        for dataset in datasets[:5]  # 只显示前5个
    ])
    
    if datasets:
        dataset_description = f"\n\n可用数据集:\n{dataset_info}"
        if len(datasets) > 5:
            dataset_description += f"\n... 还有 {len(datasets) - 5} 个数据集"
    else:
        dataset_description = "\n\n未找到数据集，请先配置 RAGFlow 知识库。"
    
    return [
        types.Tool(
            name="ragflow_retrieval",
            description=(
                "从 RAGFlow 检索相关文档块。"
                "基于问题在指定的数据集中搜索相关信息。"
                "可以指定数据集ID进行搜索，或留空搜索所有数据集。" +
                dataset_description
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "要搜索的问题或查询"
                    },
                    "dataset_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要搜索的数据集ID列表（可选，留空则搜索所有数据集）"
                    },
                    "document_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要搜索的文档ID列表（可选）"
                    },
                    "page": {
                        "type": "integer",
                        "description": "页码（默认：1）",
                        "default": 1,
                        "minimum": 1
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "每页结果数（默认：10，最大：50）",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 50
                    },
                    "similarity_threshold": {
                        "type": "number",
                        "description": "相似度阈值（默认：0.2）",
                        "default": 0.2,
                        "minimum": 0.0,
                        "maximum": 1.0
                    },
                    "vector_similarity_weight": {
                        "type": "number",
                        "description": "向量相似度权重（默认：0.3）",
                        "default": 0.3,
                        "minimum": 0.0,
                        "maximum": 1.0
                    },
                    "keyword": {
                        "type": "boolean",
                        "description": "启用关键词搜索（默认：false）",
                        "default": False
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "检索数量上限（默认：1024）",
                        "default": 1024,
                        "minimum": 1,
                        "maximum": 1024
                    }
                },
                "required": ["question"]
            }
        )
    ]


@app.call_tool()
@with_api_key
async def call_tool(
    name: str,
    arguments: dict,
    *,
    connector: RAGFlowConnector,
    api_key: str
) -> List[types.TextContent]:
    """调用工具"""
    if name == "ragflow_retrieval":
        # 提取参数
        question = arguments.get("question", "")
        dataset_ids = arguments.get("dataset_ids", [])
        document_ids = arguments.get("document_ids", [])
        page = arguments.get("page", 1)
        page_size = arguments.get("page_size", 10)
        similarity_threshold = arguments.get("similarity_threshold", 0.2)
        vector_similarity_weight = arguments.get("vector_similarity_weight", 0.3)
        keyword = arguments.get("keyword", False)
        top_k = arguments.get("top_k", 1024)
        rerank_id = arguments.get("rerank_id")
        force_refresh = arguments.get("force_refresh", False)
        
        if not question:
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "error": "问题不能为空",
                    "success": False
                }, ensure_ascii=False)
            )]
        
        # 执行检索
        return await connector.retrieval(
            api_key=api_key,
            question=question,
            dataset_ids=dataset_ids,
            document_ids=document_ids,
            page=page,
            page_size=page_size,
            similarity_threshold=similarity_threshold,
            vector_similarity_weight=vector_similarity_weight,
            keyword=keyword,
            top_k=top_k,
            rerank_id=rerank_id,
            force_refresh=force_refresh
        )
    
    raise ValueError(f"未知工具: {name}")


class AuthMiddleware:
    """认证中间件"""
    
    def __init__(self, app: ASGIApp):
        self.app = app
    
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        # 这里可以添加认证逻辑
        await self.app(scope, receive, send)


def create_starlette_app() -> Starlette:
    """创建 Starlette 应用"""
    # 创建 SSE 传输
    sse = SseServerTransport("/messages/")
    
    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await app.run(
                streams[0],
                streams[1],
                app.create_initialization_options()
            )
        return Response()
    
    async def handle_root(request):
        """处理根路径请求"""
        return JSONResponse({
            "service": "RAGFlow MCP Server",
            "version": "1.0.0",
            "endpoints": {
                "sse": "/sse - SSE endpoint for MCP communication",
                "messages": "/messages/ - Message posting endpoint",
                "health": "/health - Health check endpoint"
            },
            "description": "MCP server for RAGFlow document retrieval"
        })
    
    async def handle_health(request):
        """健康检查端点"""
        return JSONResponse({
            "status": "healthy",
            "service": "ragflow-mcp-server",
            "timestamp": time.time()
        })
    
    # 配置路由
    routes = [
        Route("/", endpoint=handle_root, methods=["GET"]),
        Route("/health", endpoint=handle_health, methods=["GET"]),
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Mount("/messages/", app=sse.handle_post_message),
    ]
    
    # 配置中间件
    middleware = [Middleware(AuthMiddleware)]
    
    return Starlette(
        debug=True,
        routes=routes,
        middleware=middleware
    )


@click.command()
@click.option("--base-url", default=RAG_BASE_URL, help="RAGFlow API 基础URL")
@click.option("--host", default=DEFAULT_HOST, help="MCP 服务器主机")
@click.option("--port", default=DEFAULT_PORT, help="MCP 服务器端口")
@click.option("--api-key", default="", help="RAGFlow API 密钥")
def main(base_url: str, host: str, port: int, api_key: str):
    """主函数"""
    import os
    import uvicorn
    
    # 更新全局配置
    global RAG_BASE_URL, DEFAULT_API_KEY
    RAG_BASE_URL = base_url
    DEFAULT_API_KEY = api_key
    
    # 打印启动信息
    print("\n" + "="*60)
    print("RAGFlow MCP 服务器")
    print("="*60)
    print(f"RAGFlow API: {base_url}")
    print(f"MCP 服务器: {host}:{port}")
    print(f"SSE 端点: http://{host}:{port}/sse")
    print("="*60 + "\n")
    
    # 创建并启动应用
    starlette_app = create_starlette_app()
    
    # 启动服务器
    uvicorn.run(
        starlette_app,
        host=host,
        port=port,
        log_level="info"
    )


if __name__ == "__main__":
    main()
