"""
数据获取服务模块
负责从各种数据源获取股票行情数据
支持：本地文件、腾讯财经API、东方财富API、网页爬取
"""
import logging
from datetime import datetime, date
from typing import List, Optional, Dict
import aiohttp
import pandas as pd
from pathlib import Path
import struct

logger = logging.getLogger(__name__)


class DataFetcher:
    """数据获取器基类"""
    
    async def fetch_kline(
        self,
        code: str,
        period: str = "D",
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[Dict]:
        """
        获取K线数据
        
        Args:
            code: 股票代码
            period: 周期 D/W/M
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            K线数据列表
        """
        raise NotImplementedError
    
    async def fetch_sentiment_data(self, date: date) -> Dict:
        """获取大盘情绪数据"""
        raise NotImplementedError
    
    async def fetch_all_stocks(self) -> List[Dict]:
        """获取所有股票基础信息"""
        raise NotImplementedError


class LocalDataFetcher(DataFetcher):
    """本地数据源获取器（同花顺、通达信等）"""

    def __init__(self, data_path: str = "./data"):
        self.data_path = Path(data_path)
        self.tdx_path = Path("D:/通达信/vipdoc")
        logger.info(f"本地数据源初始化，数据路径：{self.data_path}")
        logger.info(f"通达信数据路径：{self.tdx_path}")

    def _parse_tdx_day_file(self, file_path: Path) -> List[Dict]:
        """
        解析通达信日线数据文件（优化版）
        .day 文件格式：每条记录32字节
        - 日期：4字节（YYYYMMDD）
        - 开盘价：4字节（int，单位为分，需要除以100）
        - 最高价：4字节（int，单位为分，需要除以100）
        - 最低价：4字节（int，单位为分，需要除以100）
        - 收盘价：4字节（int，单位为分，需要除以100）
        - 成交量：4字节（unsigned int）
        - 成交额：4字节（unsigned int，单位为元，需要除以10000）
        - 保留：4字节
        """
        try:
            if not file_path.exists():
                return []

            with open(file_path, 'rb') as f:
                data = f.read()

            record_size = 32
            num_records = len(data) // record_size

            # 预分配列表空间，提高性能
            dates = []
            open_prices = []
            high_prices = []
            low_prices = []
            close_prices = []
            volumes = []
            amounts = []

            # 批量解析二进制数据
            for i in range(num_records):
                offset = i * record_size
                record = data[offset:offset + record_size]

                # 解析二进制数据
                date_int = struct.unpack('I', record[0:4])[0]
                date_str = str(date_int)

                # 检查日期格式是否正确
                if len(date_str) != 8 or not date_str.isdigit():
                    continue

                try:
                    trade_date = date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
                except ValueError:
                    continue

                # 通达信价格存储为4字节整数，单位为分，需要除以100
                open_price = struct.unpack('i', record[4:8])[0] / 100
                high_price = struct.unpack('i', record[8:12])[0] / 100
                low_price = struct.unpack('i', record[12:16])[0] / 100
                close_price = struct.unpack('i', record[16:20])[0] / 100
                volume = struct.unpack('I', record[20:24])[0]
                amount = struct.unpack('I', record[24:28])[0] / 10000  # 转换为万元

                # 跳过无效数据
                if close_price == 0 or volume == 0:
                    continue

                dates.append(trade_date)
                open_prices.append(round(open_price, 2))
                high_prices.append(round(high_price, 2))
                low_prices.append(round(low_price, 2))
                close_prices.append(round(close_price, 2))
                volumes.append(volume)
                amounts.append(round(amount, 2))

            # 使用pandas构建DataFrame，提高处理效率
            if not dates:
                return []

            df = pd.DataFrame({
                'date': dates,
                'open_price': open_prices,
                'high_price': high_prices,
                'low_price': low_prices,
                'close_price': close_prices,
                'volume': volumes,
                'amount': amounts
            })

            # 计算涨跌幅
            df['change_percent'] = df['close_price'].pct_change() * 100
            df['change_percent'] = df['change_percent'].fillna(0).round(2)

            # 按日期排序
            df = df.sort_values('date')

            # 转换为字典列表
            return df.to_dict('records')

        except Exception as e:
            logger.error(f"解析通达信数据文件失败 {file_path}: {e}")
            import traceback
            traceback.print_exc()
            return []

    def _get_tdx_market(self, code: str) -> str:
        """根据股票代码判断通达信市场"""
        # 指数代码
        if code in ['sh000001', 'sh000300', 'sh000688', 'sz399001', 'sz399006']:
            if code.startswith('sh'):
                return 'sh'
            elif code.startswith('sz'):
                return 'sz'
        # 普通股票代码
        if code.startswith('60') or code.startswith('68') or code.startswith('688') or code.startswith('51'):
            return 'sh'  # 上海市场
        elif code.startswith('00') or code.startswith('30') or code.startswith('300'):
            return 'sz'  # 深圳市场
        elif code.startswith('8') or code.startswith('43'):
            return 'bj'  # 北京市场
        return 'sh'  # 默认上海市场

    async def fetch_kline(
        self,
        code: str,
        period: str = "D",
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[Dict]:
        """从本地通达信文件读取K线数据"""
        try:
            # 尝试从通达信读取数据
            market = self._get_tdx_market(code)
            tdx_file = self.tdx_path / market / 'lday' / f'{market}{code}.day'

            if tdx_file.exists():
                logger.info(f"从通达信读取数据：{tdx_file}")
                klines = self._parse_tdx_day_file(tdx_file)

                # 根据日期范围过滤
                if start_date:
                    klines = [k for k in klines if k['date'] >= start_date]
                if end_date:
                    klines = [k for k in klines if k['date'] <= end_date]

                return klines
            else:
                # 回退到CSV格式
                logger.warning(f"通达信数据文件不存在，尝试CSV格式：{tdx_file}")
                kline_file = self.data_path / f"{code}_{period}.csv"
                if not kline_file.exists():
                    logger.warning(f"本地K线数据文件不存在：{kline_file}")
                    return []

                df = pd.read_csv(kline_file)

                # 转换日期格式
                if 'date' in df.columns:
                    df['date'] = pd.to_datetime(df['date']).dt.date

                # 根据日期范围过滤
                if start_date:
                    df = df[df['date'] >= start_date]
                if end_date:
                    df = df[df['date'] <= end_date]

                # 转换为字典列表
                return df.to_dict('records')

        except Exception as e:
            logger.error(f"读取本地K线数据失败 {code}: {e}")
            return []
    
    async def fetch_sentiment_data(self, date: date) -> Dict:
        """从本地文件读取大盘情绪数据"""
        try:
            sentiment_file = self.data_path / f"sentiment_{date.isoformat()}.json"
            if not sentiment_file.exists():
                logger.warning(f"本地情绪数据文件不存在：{sentiment_file}")
                return {}
            
            import json
            with open(sentiment_file, 'r') as f:
                return json.load(f)
        
        except Exception as e:
            logger.error(f"读取本地情绪数据失败：{e}")
            return {}
    
    async def fetch_all_stocks(self) -> List[Dict]:
        """从通达信数据读取所有股票信息"""
        try:
            stocks = []

            # 读取上海市场股票
            sh_day_dir = self.tdx_path / 'sh' / 'lday'
            if sh_day_dir.exists():
                for day_file in sh_day_dir.glob('*.day'):
                    code = day_file.stem.replace('sh', '')
                    # 过滤指数代码（通常6位数字，但指数通常有特殊前缀）
                    if len(code) == 6 and code.isdigit():
                        stocks.append({
                            'code': code,
                            'name': f'股票{code}',  # 通达信文件中没有股票名称
                            'market': 'SH'
                        })

            # 读取深圳市场股票
            sz_day_dir = self.tdx_path / 'sz' / 'lday'
            if sz_day_dir.exists():
                for day_file in sz_day_dir.glob('*.day'):
                    code = day_file.stem.replace('sz', '')
                    # 过滤指数代码
                    if len(code) == 6 and code.isdigit():
                        stocks.append({
                            'code': code,
                            'name': f'股票{code}',
                            'market': 'SZ'
                        })

            # 读取北京市场股票
            bj_day_dir = self.tdx_path / 'bj' / 'lday'
            if bj_day_dir.exists():
                for day_file in bj_day_dir.glob('*.day'):
                    code = day_file.stem.replace('bj', '')
                    if len(code) == 6 and code.isdigit():
                        stocks.append({
                            'code': code,
                            'name': f'股票{code}',
                            'market': 'BJ'
                        })

            logger.info(f"从通达信读取到 {len(stocks)} 只股票")
            return stocks

        except Exception as e:
            logger.error(f"读取本地股票列表失败：{e}")
            return []


class TencentDataFetcher(DataFetcher):
    """腾讯财经数据源获取器"""
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self.base_url = "http://data.gtimg.cn"
        logger.info("腾讯财经数据源初始化")
    
    async def fetch_kline(
        self,
        code: str,
        period: str = "D",
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[Dict]:
        """从腾讯财经API获取K线数据"""
        try:
            # TODO: 实现腾讯财经API调用逻辑
            logger.info(f"从腾讯财经获取K线数据：{code} {period}")
            # 示例：
            # response = await self._request(f"/kline/{code}/{period}")
            return []
        
        except Exception as e:
            logger.error(f"腾讯财经获取K线数据失败 {code}: {e}")
            return []
    
    async def _request(self, endpoint: str, params: Optional[Dict] = None) -> Dict:
        """发起HTTPS请求"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}{endpoint}"
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    return await resp.json()
        except Exception as e:
            logger.error(f"腾讯财经API请求失败：{e}")
            return {}


class EastmoneyDataFetcher(DataFetcher):
    """东方财富数据源获取器"""
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self.base_url = "https://www.eastmoney.com"
        logger.info("东方财富数据源初始化")
    
    async def fetch_kline(
        self,
        code: str,
        period: str = "D",
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[Dict]:
        """从东方财富获取K线数据"""
        try:
            # TODO: 实现东方财富API调用逻辑
            logger.info(f"从东方财富获取K线数据：{code} {period}")
            return []
        
        except Exception as e:
            logger.error(f"东方财富获取K线数据失败 {code}: {e}")
            return []


class DataFetcherFactory:
    """数据源工厂类"""
    
    _fetchers = {
        "local": LocalDataFetcher,
        "tencent": TencentDataFetcher,
        "eastmoney": EastmoneyDataFetcher,
    }
    
    @classmethod
    def create_fetcher(cls, source_type: str, **kwargs) -> DataFetcher:
        """创建指定类型的数据获取器"""
        fetcher_class = cls._fetchers.get(source_type)
        if not fetcher_class:
            raise ValueError(f"不支持的数据源类型：{source_type}")
        
        return fetcher_class(**kwargs)
    
    @classmethod
    def register_fetcher(cls, name: str, fetcher_class: type):
        """注册自定义数据获取器"""
        cls._fetchers[name] = fetcher_class
