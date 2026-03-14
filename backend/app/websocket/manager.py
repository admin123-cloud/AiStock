"""
WebSocket连接管理
支持实时推送市场数据和情绪数据
"""
import logging
import json
import asyncio
from datetime import datetime
from typing import Set
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class ConnectionManager:
    """WebSocket连接管理器"""
    
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
    
    async def connect(self, websocket: WebSocket):
        """客户端连接"""
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info(f"客户端连接，当前连接数：{len(self.active_connections)}")
    
    def disconnect(self, websocket: WebSocket):
        """客户端断开"""
        self.active_connections.discard(websocket)
        logger.info(f"客户端断开，当前连接数：{len(self.active_connections)}")
    
    async def broadcast(self, message: dict):
        """广播消息给所有连接"""
        if not self.active_connections:
            return
        
        disconnected = set()
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"发送消息失败：{e}")
                disconnected.add(connection)
        
        # 移除断开的连接
        for conn in disconnected:
            self.disconnect(conn)
    
    async def send_to_one(self, websocket: WebSocket, message: dict):
        """发送消息给特定连接"""
        try:
            await websocket.send_json(message)
        except Exception as e:
            logger.error(f"发送消息失败：{e}")
            self.disconnect(websocket)


class RealTimeDataBroadcaster:
    """实时数据广播器"""
    
    def __init__(self, connection_manager: ConnectionManager):
        self.manager = connection_manager
    
    async def broadcast_kline_update(self, code: str, kline: dict):
        """广播K线更新"""
        message = {
            "type": "kline_update",
            "timestamp": datetime.now().isoformat(),
            "code": code,
            "data": kline
        }
        await self.manager.broadcast(message)
    
    async def broadcast_sentiment_update(self, sentiment: dict):
        """广播情绪数据更新"""
        message = {
            "type": "sentiment_update",
            "timestamp": datetime.now().isoformat(),
            "data": sentiment
        }
        await self.manager.broadcast(message)
    
    async def broadcast_market_summary(self, summary: dict):
        """广播市场摘要"""
        message = {
            "type": "market_summary",
            "timestamp": datetime.now().isoformat(),
            "data": summary
        }
        await self.manager.broadcast(message)
    
    async def broadcast_heartbeat(self):
        """发送心跳信号"""
        message = {
            "type": "heartbeat",
            "timestamp": datetime.now().isoformat()
        }
        await self.manager.broadcast(message)


# 全局连接管理器
connection_manager = ConnectionManager()
broadcaster = RealTimeDataBroadcaster(connection_manager)
