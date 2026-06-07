"""
股票业务服务

提供股票相关的业务逻辑
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
import json
from pathlib import Path

from sqlalchemy import func
from utils.database import get_db
from utils.logger import get_logger
from models import Stock
logger = get_logger("StockService")


class StockService:
    """股票业务服务"""
    
    def __init__(self):
        from data_fetcher import DataSourceManager
        self.data_source_manager = DataSourceManager()
    
    def get_stock_list(
        self,
        market: str | None = None,
        stock_type: str | None = None
    ) -> list[dict[str, Any]]:
        """
        获取股票列表（从数据库）
        
        Args:
            market: 市场过滤（SH/SZ/BJ）
            stock_type: 类型过滤（stock/index）
        
        Returns:
            股票列表
        """
        with next(get_db()) as session:
            query = session.query(Stock)
            
            if market:
                query = query.filter(Stock.market == market)
            if stock_type:
                query = query.filter(Stock.type == stock_type)
            
            stocks = query.all()
            
            return [
                {
                    "code": s.code,
                    "name": s.name,
                    "market": s.market,
                    "type": s.type,
                    "industry": s.industry,
                    "sector": s.sector,
                }
                for s in stocks
            ]
    
    def import_stocks_from_datasource(
        self,
        batch_size: int = 100,
        source_name: str | None = None
    ) -> dict[str, int]:
        """
        从数据源导入股票信息到数据库
        
        Args:
            batch_size: 批量插入大小
            source_name: 指定数据源（默认使用主数据源）
        
        Returns:
            导入结果统计
        """
        logger.info("开始从数据源导入股票信息...")
        
        # 通过数据源管理器获取数据
        all_stocks = self.data_source_manager.get_all_stocks(source_name=source_name)
        all_indices = self.data_source_manager.get_all_indices(source_name=source_name)
        all_data = all_stocks + all_indices
        
        logger.info(f"获取到 {len(all_data)} 条数据")
        
        # 导入数据
        result = self._import_to_db(all_data, batch_size)
        
        logger.success(f"导入完成: 新增 {result['inserted']}, 更新 {result['updated']}, 失败 {result['failed']}")
        return result
    
    def _import_to_db(
        self,
        stocks: list[dict[str, Any]],
        batch_size: int = 100
    ) -> dict[str, int]:
        """
        导入股票数据到数据库
        
        Args:
            stocks: 股票列表
            batch_size: 批量大小
        
        Returns:
            统计结果
        """
        inserted = 0
        updated = 0
        failed = 0
        
        with next(get_db()) as session:
            for i, stock_data in enumerate(stocks, 1):
                try:
                    existing = session.query(Stock).filter(
                        Stock.code == stock_data["code"]
                    ).first()
                    
                    if existing:
                        existing.name = stock_data["name"]
                        existing.market = stock_data["market"]
                        existing.type = stock_data["type"]
                        existing.industry = stock_data.get("industry", "")
                        existing.sector = stock_data.get("sector", "")
                        # type: ignore
                        existing.updated_at = datetime.now()  # type: ignore
                        updated += 1
                    else:
                        new_stock = Stock(
                            code=stock_data["code"],
                            name=stock_data["name"],
                            market=stock_data["market"],
                            type=stock_data["type"],
                            industry=stock_data.get("industry", ""),
                            sector=stock_data.get("sector", ""),
                            status="active"
                        )
                        session.add(new_stock)
                        inserted += 1
                    
                    if i % batch_size == 0:
                        session.commit()
                        logger.info(f"已处理 {i}/{len(stocks)} 条...")
                
                except Exception as e:
                    failed += 1
                    logger.error(f"导入 {stock_data.get('code')} 失败: {e}")
                    session.rollback()
                    continue
            
            try:
                session.commit()
            except Exception as e:
                logger.error(f"最终提交失败: {e}")
                session.rollback()
        
        return {"inserted": inserted, "updated": updated, "failed": failed}
    
    def get_statistics(self) -> dict[str, Any]:
        """
        获取股票统计信息
        
        Returns:
            统计信息
        """
        with next(get_db()) as session:
            stats = session.query(
                Stock.market,
                Stock.type,
                func.count(Stock.id).label('count')
            ).group_by(Stock.market, Stock.type).all()
            
            result: dict[str, Any] = {}
            total: int = 0
            for stat in stats:
                key = f"{stat.market}_{stat.type}"
                result[key] = stat.count
                total += int(stat.count)
            
            result["total"] = total
            return result
    
    def save_to_json(self, data: list[dict[str, Any]], filename: str) -> Path:
        """
        保存数据到JSON文件
        
        Args:
            data: 数据列表
            filename: 文件名
        
        Returns:
            文件路径
        """
        output_path = Path("data") / filename
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        logger.info(f"数据已保存到: {output_path}")
        return output_path
    
    # ========== 数据源查询方法 ==========
    
    def fetch_stock_list(
        self,
        market: str = "SH",
        stock_type: str = "stock",
        source_name: str | None = None
    ) -> list[dict[str, Any]]:
        """
        从数据源获取股票列表
        
        Args:
            market: 市场代码（SH/SZ/BJ）
            stock_type: 类型（stock/index）
            source_name: 指定数据源
        
        Returns:
            股票列表
        """
        return self.data_source_manager.get_stock_list(
            market=market,
            stock_type=stock_type,
            source_name=source_name
        )
    
    def fetch_all_stocks(self, source_name: str | None = None) -> list[dict[str, Any]]:
        """
        从数据源获取所有股票
        
        Args:
            source_name: 指定数据源
        
        Returns:
            所有股票列表
        """
        return self.data_source_manager.get_all_stocks(source_name=source_name)
    
    def fetch_stock_quote(
        self,
        stock_code: str,
        source_name: str | None = None
    ) -> dict[str, Any] | None:
        """
        从数据源获取股票实时行情
        
        Args:
            stock_code: 股票代码
            source_name: 指定数据源
        
        Returns:
            行情数据
        """
        return self.data_source_manager.get_stock_quote(stock_code, source_name=source_name)
    
    def fetch_realtime_quotes(
        self,
        stock_codes: list[str],
        source_name: str | None = None
    ) -> list[dict[str, Any]]:
        """
        批量获取实时行情
        
        Args:
            stock_codes: 股票代码列表
            source_name: 指定数据源
        
        Returns:
            行情数据列表
        """
        return self.data_source_manager.get_realtime_quotes(stock_codes, source_name=source_name)
    
    def get_data_source_status(self) -> dict[str, dict[str, Any]]:
        """
        获取数据源状态
        
        Returns:
            数据源状态信息
        """
        return self.data_source_manager.get_source_status()
