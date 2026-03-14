"""
StockPy - 大盘情绪监控系统
FastAPI主应用入口
"""
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.config import settings
from app.database import init_db
from app.api import api_router
from app.tasks.scheduler import scheduler
from app.websocket import connection_manager

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动和关闭事件处理"""
    # 启动时执行
    logger.info("=" * 60)
    logger.info("StockPy 大盘情绪监控系统启动中...")
    logger.info("=" * 60)
    
    # 初始化数据库
    init_db()
    logger.info("✓ 数据库初始化完成")
    
    # 启动定时任务
    if settings.debug:
        logger.info("⚠ 调试模式：定时任务处于暂停状态")
    else:
        scheduler.start()
        logger.info("✓ 定时任务调度器已启动")
    
    logger.info("=" * 60)
    logger.info(f"服务启动完成！监听地址：{settings.host}:{settings.port}")
    logger.info("=" * 60)
    
    yield
    
    # 关闭时执行
    logger.info("=" * 60)
    logger.info("StockPy 大盘情绪监控系统关闭中...")
    logger.info("=" * 60)
    
    scheduler.shutdown()
    logger.info("✓ 定时任务调度器已关闭")


# 创建FastAPI应用
app = FastAPI(
    title="StockPy - 大盘情绪监控系统",
    description="集成实时市场数据、技术分析、自动回测和情绪监控的A股量化交易平台",
    version="0.1.0",
    lifespan=lifespan
)

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 在生产环境应配置具体的域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册API路由
app.include_router(api_router)


# ======================== WebSocket 路由 ========================

@app.websocket("/ws/market")
async def websocket_market_endpoint(websocket: WebSocket):
    """
    WebSocket端点：实时市场数据推送
    
    连接后可接收：
    - kline_update: K线更新
    - sentiment_update: 情绪数据更新
    - market_summary: 市场摘要
    - heartbeat: 心跳信号
    """
    await connection_manager.connect(websocket)
    logger.info("新的WebSocket连接：实时市场数据")
    
    try:
        while True:
            # 接收客户端消息（如果有的话）
            try:
                data = await asyncio.wait_for(websocket.receive_json(), timeout=300)
                logger.debug(f"收到客户端消息：{data}")
            except asyncio.TimeoutError:
                # 长连接，心跳保活
                logger.debug("WebSocket心跳保活")
                continue
            except Exception as e:
                logger.error(f"接收消息错误：{e}")
                break
    
    except WebSocketDisconnect:
        connection_manager.disconnect(websocket)
        logger.info("WebSocket连接断开")


# ======================== 静态文件和单页应用 ========================

@app.get("/")
async def root():
    """根路由重定向到前端"""
    return {"message": "欢迎使用 StockPy！", "api_docs": "/docs"}


@app.get("/docs-ui", tags=["文档"])
async def swagger_ui():
    """交互式API文档"""
    return FileResponse("swagger-ui.html")


# ======================== 错误处理 ========================

@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    """全局异常处理"""
    logger.error(f"未处理的异常：{exc}")
    return {
        "error": str(exc),
        "status_code": 500
    }


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )
