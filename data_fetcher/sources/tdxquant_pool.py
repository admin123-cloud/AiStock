"""
通达信量化平台连接器连接池

使用单例模式确保整个项目中只初始化一次，并且时刻保持连接活跃
服务启动时主动初始化，随时准备被调用
"""

from typing import Optional, Dict, Any, List
import threading
import time
import sys
import os
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText

# 添加项目根目录到Python搜索路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils.logger import get_logger
from core.base import DataSourceStatus

# 添加通达信SDK路径到Python搜索路径
tdx_sdk_path = r'D:\TDX\PYPlugins\user'
if tdx_sdk_path not in sys.path:
    sys.path.insert(0, tdx_sdk_path)

logger = get_logger("TdxQuantPool")


def _tdxquant_init_path() -> str:
    default_tdx_plugin_identity = r"D:\TDX\PYPlugins\user\aistock_gateway.py"
    default_identity = default_tdx_plugin_identity if os.path.exists(default_tdx_plugin_identity) else os.path.join(
        project_root, "scripts", "tdxquant_bridge.py"
    )
    path = os.path.abspath(
        os.environ.get("AISTOCK_TDXQ_INIT_PATH") or default_identity
    )
    if not path.endswith(".py") or not os.path.exists(path):
        raise RuntimeError(f"TdxQuant 初始化路径无效: {path}")
    return path


class TdxQuantPool:
    """
    通达信量化平台连接器连接池
    
    使用单例模式确保整个项目中只初始化一次，并且保持连接活跃
    服务启动时主动初始化，随时准备被调用
    """
    
    _instance = None
    _lock = threading.Lock()
    _init_lock = threading.Lock()
    
    def __new__(cls):
        """单例模式实现"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(TdxQuantPool, cls).__new__(cls)
                    cls._instance._init_pool()
                    if cls._instance._eager_init:
                        init_thread = threading.Thread(target=cls._instance._initialize, daemon=True)
                        init_thread.start()
        return cls._instance
    
    def _init_pool(self):
        """初始化连接池"""
        self.tq = None
        self._initialized = False
        self._initializing = False
        self._init_attempts = 0
        self._max_init_attempts = 5
        self._last_activity = None
        self._keepalive_interval = 30  # 30秒
        self._retry_interval = 5  # 5秒
        self._lock = threading.Lock()
        self._status = DataSourceStatus.UNAVAILABLE
        self._api_lock = threading.Lock()
        self._supports_realtime_quotes = None
        self._realtime_quotes_fallback_logged = False
        self._gateway_url = os.environ.get("AISTOCK_TDX_GATEWAY_URL", "").strip()
        self._gateway = None
        self._gateway_failure_cooldown_sec = int(os.environ.get("AISTOCK_TDX_GATEWAY_FAILURE_COOLDOWN_SEC", "15"))
        self._gateway_next_request_at = None
        self._eager_init = os.environ.get("AISTOCK_TDXQ_EAGER_INIT", "0").strip().lower() in {"1", "true", "yes", "on"}
        self._init_cooldown_sec = int(os.environ.get("AISTOCK_TDXQ_INIT_COOLDOWN_SEC", "120"))
        self._next_init_retry_at = None
        self._last_init_error = None
        self._last_init_error_at = None
        self._last_down_alert_at = None
        self._down_alert_interval_sec = int(os.environ.get("AISTOCK_TDXQ_DOWN_ALERT_INTERVAL_SEC", "1800"))
        
        # 启动保活线程
        self._keepalive_thread = threading.Thread(target=self._keepalive, daemon=True)
        self._keepalive_thread.start()
        
        if self._eager_init:
            logger.info("TdxQuantPool initialized; eager TdxQuant initialization is enabled")
        else:
            logger.info("TdxQuantPool initialized; TdxQuant will initialize lazily on first use")

    def _use_gateway(self) -> bool:
        return bool(self._gateway_url)

    def _gateway_client(self):
        if not self._gateway:
            from data_fetcher.sources.tdx_gateway_client import TdxGatewayClient

            self._gateway = TdxGatewayClient(self._gateway_url)
        return self._gateway

    def _gateway_request_allowed(self) -> bool:
        if not self._gateway_next_request_at:
            return True
        now = datetime.now()
        if now >= self._gateway_next_request_at:
            self._gateway_next_request_at = None
            return True
        wait_seconds = int((self._gateway_next_request_at - now).total_seconds())
        logger.debug(f"TdxGateway request skipped during cooldown ({wait_seconds}s left).")
        return False

    def _mark_gateway_up(self) -> None:
        self._initialized = True
        self._status = DataSourceStatus.AVAILABLE
        self._last_activity = datetime.now()
        self._last_init_error = None
        self._last_init_error_at = None
        self._gateway_next_request_at = None

    def _mark_gateway_down(self, reason: str) -> None:
        self._initialized = False
        self._status = DataSourceStatus.UNAVAILABLE
        self._last_init_error = reason
        self._last_init_error_at = datetime.now()
        self._gateway_next_request_at = datetime.now() + timedelta(seconds=self._gateway_failure_cooldown_sec)
        logger.warning(
            f"TdxGateway request failed; suppressing gateway retries for "
            f"{self._gateway_failure_cooldown_sec}s: {reason}"
        )

    @staticmethod
    def _normalize_tdx_date(value: Optional[str]) -> Optional[str]:
        if isinstance(value, str) and len(value) == 10 and value[4] == "-" and value[7] == "-":
            return value.replace("-", "")
        return value

    @staticmethod
    def _is_available_status(status: Any) -> bool:
        if isinstance(status, DataSourceStatus):
            return status == DataSourceStatus.AVAILABLE
        return str(status or "").lower() == str(DataSourceStatus.AVAILABLE).lower()

    def _reset_tq_client(self, reason: str = "") -> None:
        """Drop the in-process TdxQuant handle before reconnecting after TDX restarts."""
        if self.tq and hasattr(self.tq, "close"):
            try:
                with self._api_lock:
                    self.tq.close()
                logger.info(f"TdxQuant client closed before reconnect: {reason}")
            except Exception as exc:
                logger.warning(f"TdxQuant client close before reconnect failed: {exc}")
        self.tq = None
        self._initialized = False
        self._status = DataSourceStatus.UNAVAILABLE
    
    def _initialize(self, force: bool = False):
        """初始化通达信量化平台客户端"""
        with self._init_lock:
            if self._use_gateway():
                try:
                    if force:
                        initialized = self._gateway_client().initialize()
                    else:
                        if not self._gateway_request_allowed():
                            return False
                        health = self._gateway_client().health()
                        initialized = bool(health.get("ok")) and self._is_available_status(health.get("status"))
                    if initialized:
                        self._mark_gateway_up()
                        self._next_init_retry_at = None
                    else:
                        self._initialized = False
                        self._status = DataSourceStatus.UNAVAILABLE
                    return self._initialized
                except Exception as e:
                    self._next_init_retry_at = datetime.now() + timedelta(seconds=self._init_cooldown_sec)
                    self._mark_gateway_down(str(e))
                    return False
            if force:
                if self._initialized and self.tq is not None:
                    try:
                        if self._probe_connection():
                            self._last_activity = datetime.now()
                            self._status = DataSourceStatus.AVAILABLE
                            self._next_init_retry_at = None
                            self._last_init_error = None
                            return True
                    except Exception as exc:
                        logger.warning(f"TdxQuant existing handle probe before force initialize failed: {exc}")
                self._reset_tq_client("force initialize")
                self._next_init_retry_at = None
            elif self._initialized:
                return True
            now = datetime.now()
            if (not force) and self._next_init_retry_at and now < self._next_init_retry_at:
                wait_seconds = int((self._next_init_retry_at - now).total_seconds())
                logger.warning(
                    f"TdxQuant init skipped during cooldown ({wait_seconds}s left). "
                    f"last_error={self._last_init_error}"
                )
                return False
            
            attempt = 0
            self._initializing = True
            while attempt < self._max_init_attempts:
                try:
                    if attempt > 0:
                        self._reset_tq_client(f"retry initialize attempt {attempt + 1}")
                    from tqcenter import tq
                    
                    logger.info(f"尝试初始化 TdxQuant (尝试 {attempt+1}/{self._max_init_attempts})...")
                    
                    self.tq = tq
                    init_path = _tdxquant_init_path()
                    self.tq.initialize(init_path)
                    logger.info(f"TdxQuant 初始化成功，使用路径: {init_path}")
                    
                    # 测试连接是否成功
                    test_result = self._probe_connection()
                    if test_result:
                        self._initialized = True
                        self._init_attempts = 0
                        self._last_activity = datetime.now()
                        self._status = DataSourceStatus.AVAILABLE
                        self._next_init_retry_at = None
                        self._last_init_error = None
                        self._last_init_error_at = None
                        self._initializing = False
                        logger.info("TdxQuant 初始化成功并验证连接")
                        return True
                    else:
                        self._last_init_error = "health probe returned no data"
                        self._last_init_error_at = datetime.now()
                        logger.warning("TdxQuant health probe returned no data; retrying after interval")
                        logger.warning("TdxQuant 初始化成功但连接验证失败")
                        self._reset_tq_client("health probe returned no data")
                        attempt += 1
                        time.sleep(self._retry_interval)
                        
                except ImportError as e:
                    logger.warning(f"TdxQuant 导入失败: {e}")
                    self._last_init_error = str(e)
                    self._last_init_error_at = datetime.now()
                    self._next_init_retry_at = datetime.now() + timedelta(seconds=self._init_cooldown_sec)
                    self._status = DataSourceStatus.UNAVAILABLE
                    self._initializing = False
                    return False
                except Exception as e:
                    attempt += 1
                    self._last_init_error = str(e)
                    self._last_init_error_at = datetime.now()
                    logger.error(f"TdxQuant 初始化失败 (尝试 {attempt}/{self._max_init_attempts}): {e}")
                    time.sleep(self._retry_interval)
            
            self._status = DataSourceStatus.UNAVAILABLE
            self._next_init_retry_at = datetime.now() + timedelta(seconds=self._init_cooldown_sec)
            logger.error(f"TdxQuant 初始化失败，已达到最大尝试次数")
            self._initializing = False
            return False

    def require_available_for_startup(self, notify: bool = True) -> bool:
        """服务启动硬依赖：TdxQuant 不可用时阻止 FastAPI 启动。"""
        self._next_init_retry_at = None
        if self._initialize(force=True):
            return True
        reason = self._last_init_error or "TdxQuant startup health check failed"
        if notify:
            self._send_down_alert(reason, force=True)
        raise RuntimeError(f"TdxQuant startup check failed: {reason}")
    
    def _ensure_initialized(self):
        """确保客户端已初始化"""
        with self._lock:
            if not self._initialized:
                return self._initialize()
            return True

    def get_client(self):
        """统一获取底层 tq 客户端；全局禁止在连接池外直接 import tqcenter。"""
        if not self._ensure_initialized():
            raise RuntimeError(f"TdxQuant 未初始化: {self._last_init_error or 'unknown error'}")
        return self.tq

    def _mark_down(self, reason: str, notify: bool = True) -> None:
        was_available = self._status == DataSourceStatus.AVAILABLE
        self._reset_tq_client(reason)
        self._last_init_error = reason
        self._last_init_error_at = datetime.now()
        self._next_init_retry_at = datetime.now() + timedelta(seconds=self._init_cooldown_sec)
        if notify and was_available:
            self._send_down_alert(reason)

    def _send_down_alert(self, reason: str, force: bool = False) -> None:
        now = datetime.now()
        if (
            not force
            and self._last_down_alert_at
            and (now - self._last_down_alert_at).total_seconds() < self._down_alert_interval_sec
        ):
            return
        try:
            from utils.config import config

            email_config = config.get("email", default={}, config_file="settings.yaml") or {}
            if not email_config.get("enabled"):
                logger.warning("email.enabled 未开启，无法发送 TdxQuant 掉线告警")
                return

            smtp_server = str(email_config.get("smtp_server") or "").strip()
            smtp_port = int(email_config.get("smtp_port") or 0)
            smtp_user = str(email_config.get("smtp_user") or "").strip()
            smtp_password = str(email_config.get("smtp_password") or "").strip()
            from_email = str(email_config.get("from_email") or "").strip()
            to_emails = email_config.get("to_emails") or []
            if isinstance(to_emails, str):
                to_emails = [to_emails]
            to_emails = [str(item).strip() for item in to_emails if str(item).strip()]
            if not all([smtp_server, smtp_port, smtp_user, smtp_password, from_email, to_emails]):
                logger.warning("SMTP 配置不完整，无法发送 TdxQuant 掉线告警")
                return

            subject = "[AiStock] TdxQuant 服务不可用"
            body = (
                f"时间: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"原因: {reason}\n"
                f"初始化路径: {_tdxquant_init_path()}\n"
                "处理建议: 检查通达信/TQ 客户端是否启动并登录，确认 TQ 策略管理器中没有同路径外部策略残留。\n"
            )
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = from_email
            msg["To"] = ", ".join(to_emails)

            use_ssl = bool(email_config.get("use_ssl")) or smtp_port == 465
            if use_ssl:
                server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30)
            else:
                server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
            try:
                if not use_ssl:
                    server.starttls()
                server.login(smtp_user, smtp_password)
                server.sendmail(from_email, to_emails, msg.as_string())
            finally:
                server.quit()
            self._last_down_alert_at = now
            logger.info("TdxQuant 掉线告警邮件已发送")
        except Exception as exc:
            logger.error(f"TdxQuant 掉线告警邮件发送失败: {exc}")

    def _invoke_tq(self, method_name: str, *args, **kwargs):
        """串行调用底层 SDK，避免并发触发 native 崩溃"""
        if self.tq is None:
            raise RuntimeError("TdxQuant 未初始化")
        method = getattr(self.tq, method_name)
        with self._api_lock:
            try:
                return method(*args, **kwargs)
            except AttributeError:
                raise
            except Exception as exc:
                self._mark_down(f"{method_name} failed: {exc}", notify=True)
                raise

    def _probe_connection(self) -> bool:
        try:
            result = self._invoke_tq(
                "get_market_data",
                field_list=[],
                stock_list=["999999.SH"],
                period="1d",
                count=1,
                dividend_type="none",
                fill_data=False,
            )
            if not isinstance(result, dict):
                return False
            close_df = result.get("Close")
            return close_df is not None and not getattr(close_df, "empty", True)
        except Exception as exc:
            logger.warning(f"TdxQuant health probe failed: {exc}")
            return False
    
    def _keepalive(self):
        """保活线程，定期检查连接状态"""
        while True:
            time.sleep(self._keepalive_interval)
            self._check_connection()
    
    def _check_connection(self):
        """检查连接状态"""
        with self._lock:
            if self._initializing:
                logger.debug("TdxQuant initialization already in progress; keepalive skipped")
                return
            if not self._initialized:
                if not self._eager_init and self.tq is None:
                    logger.debug("TdxQuant 未初始化，等待首次业务调用触发懒初始化")
                    return
                if self._next_init_retry_at and datetime.now() < self._next_init_retry_at:
                    logger.debug("TdxQuant 未初始化，仍在初始化冷却期内")
                    return
                logger.warning("TdxQuant 未初始化，尝试重新初始化...")
                self._initialize()
                return
            
            try:
                # 执行一个简单的操作来检查连接是否活跃
                if self.tq:
                    # 只获取少量数据，避免占用太多资源
                    result = self._probe_connection()
                    if result:
                        self._last_activity = datetime.now()
                        self._status = DataSourceStatus.AVAILABLE
                        logger.debug("TdxQuant 连接活跃")
                    else:
                        reason = "TdxQuant health probe returned no data"
                        logger.error(reason)
                        self._mark_down(reason, notify=True)
            except Exception as e:
                logger.error(f"检查 TdxQuant 连接状态失败: {e}")
                self._mark_down(str(e), notify=True)
    
    def get_stock_list(self, market: str = "ALL", stock_type: str = "stock", list_type: int = 1) -> Optional[list]:
        """
        获取股票列表

        Args:
            market: 市场代码（ALL/SH/SZ/BJ/9/10等）
            stock_type: 股票类型
            list_type: 列表类型 (0=股票列表, 1=其他)

        Returns:
            股票列表
        """
        if self._use_gateway():
            return self._gateway_client().get_stock_list(market=market, stock_type=stock_type, list_type=list_type)
        if not self._ensure_initialized():
            logger.warning("TdxQuant 未初始化，无法获取股票列表")
            return None

        try:
            # 根据接口文档，market=5 表示所有A股
            market_param = '5' if market.upper() == "ALL" else market.upper()
            result = self._invoke_tq("get_stock_list", market=market_param, list_type=list_type)
            
            if not result:
                logger.warning("获取股票列表失败: 无数据")
                return []
            
            self._last_activity = datetime.now()
            logger.info(f"获取 {market} 市场股票列表: {len(result)} 只")
            return result
        except Exception as e:
            logger.error(f"获取股票列表失败: {e}")
            # 标记为未初始化，下次会重新初始化
            self._initialized = False
            return None
    
    def get_stock_info(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """
        获取股票详细信息
        
        Args:
            stock_code: 股票代码
        
        Returns:
            股票详细信息
        """
        if self._use_gateway():
            return self._gateway_client().get_stock_info(stock_code)
        if not self._ensure_initialized():
            logger.warning("TdxQuant 未初始化，无法获取股票详细信息")
            return None
        
        try:
            result = self._invoke_tq("get_stock_info", stock_code)
            self._last_activity = datetime.now()
            return result
        except Exception as e:
            logger.error(f"获取股票详细信息失败: {e}")
            # 标记为未初始化，下次会重新初始化
            self._initialized = False
            return None
    
    def _legacy_get_realtime_quotes(self, stock_codes: list) -> Optional[list]:
        """
        获取实时行情
        
        Args:
            stock_codes: 股票代码列表
        
        Returns:
            实时行情列表
        """
        if not self._ensure_initialized():
            logger.warning("TdxQuant 未初始化，无法获取实时行情")
            return None
        
        try:
            result = self._invoke_tq("get_realtime_quotes", stock_codes)
            self._last_activity = datetime.now()
            return result
        except Exception as e:
            logger.error(f"获取实时行情失败: {e}")
            # 标记为未初始化，下次会重新初始化
            self._initialized = False
            return None
    
    def get_realtime_quotes(self, stock_codes: list) -> Optional[list]:
        """
        获取实时行情（优先 SDK 原生接口，不支持时自动回退到 1d 快照批量模式）。
        """
        if self._use_gateway():
            return self._gateway_client().get_realtime_quotes(stock_codes)
        if not self._ensure_initialized():
            logger.warning("TdxQuant 未初始化，无法获取实时行情")
            return None
        if not stock_codes:
            return []

        try:
            if self._supports_realtime_quotes is False:
                return self._get_realtime_quotes_by_daily_snapshot(stock_codes)

            result = self._invoke_tq("get_realtime_quotes", stock_codes)
            self._last_activity = datetime.now()
            self._supports_realtime_quotes = True
            if result:
                return result
            return self._get_realtime_quotes_by_daily_snapshot(stock_codes)
        except AttributeError:
            self._supports_realtime_quotes = False
            if not self._realtime_quotes_fallback_logged:
                logger.warning("tqcenter 不支持 get_realtime_quotes，已自动回退到 get_market_data 批量快照模式")
                self._realtime_quotes_fallback_logged = True
            return self._get_realtime_quotes_by_daily_snapshot(stock_codes)
        except Exception as e:
            logger.error(f"获取实时行情失败: {e}")
            fallback = self._get_realtime_quotes_by_daily_snapshot(stock_codes)
            if fallback:
                return fallback
            self._initialized = False
            return None

    def _get_realtime_quotes_by_daily_snapshot(self, stock_codes: list) -> Optional[list]:
        """回退路径：使用 get_market_data(period=1d,count=1) 拼装快照行情。"""
        try:
            result = self.get_market_data(
                field_list=[],
                stock_list=stock_codes,
                period='1d',
                count=1,
                dividend_type='none',
                fill_data=False,
            )
            if not result or 'Close' not in result:
                return None

            close_df = result.get('Close')
            open_df = result.get('Open')
            high_df = result.get('High')
            low_df = result.get('Low')
            volume_df = result.get('Volume')
            amount_df = result.get('Amount')
            if close_df is None or close_df.empty:
                return None

            def _safe_num(df, col_idx, default=0.0):
                try:
                    if df is None or df.empty or col_idx >= len(df.columns):
                        return float(default)
                    value = df.iloc[0, col_idx]
                    return float(value) if value is not None else float(default)
                except Exception:
                    return float(default)

            tick_time = str(close_df.index[0]) if len(close_df.index) else str(datetime.now())
            quotes = []
            for idx, code in enumerate(stock_codes):
                close_price = _safe_num(close_df, idx, 0.0)
                if close_price <= 0:
                    continue
                open_price = _safe_num(open_df, idx, close_price) or close_price
                high_price = _safe_num(high_df, idx, close_price) or close_price
                low_price = _safe_num(low_df, idx, close_price) or close_price
                volume = _safe_num(volume_df, idx, 0.0)
                amount = _safe_num(amount_df, idx, 0.0)
                quotes.append({
                    'Code': code,
                    'Name': '',
                    'Price': close_price,
                    'Open': open_price,
                    'High': high_price,
                    'Low': low_price,
                    'PreClose': 0.0,
                    'Volume': volume,
                    'Amount': amount,
                    'Change': 0.0,
                    'ChangePercent': 0.0,
                    'Time': tick_time,
                })
            return quotes
        except Exception as e:
            logger.error(f"get_market_data 回退实时快照失败: {e}")
            return None

    def get_market_data(self, field_list: list, stock_list: list, period: str, start_time: str = None, 
                      end_time: str = None, count: int = -1, dividend_type: str = 'none', 
                      fill_data: bool = True) -> Optional[Dict[str, Any]]:
        """
        获取市场数据
        
        Args:
            field_list: 字段列表
            stock_list: 股票代码列表
            period: 周期 ('1d', '1w', '1mo', '1m', '5m', '15m', '30m', '60m')
            start_time: 开始时间
            end_time: 结束时间
            count: 返回数据条数，-1表示返回所有
            dividend_type: 复权类型 ('none', 'front', 'back')
            fill_data: 是否填充数据
        
        Returns:
            市场数据字典
        """
        start_time = self._normalize_tdx_date(start_time)
        end_time = self._normalize_tdx_date(end_time)
        if self._use_gateway():
            if not self._gateway_request_allowed():
                return None
            try:
                result = self._gateway_client().get_market_data(
                    field_list=field_list,
                    stock_list=stock_list,
                    period=period,
                    start_time=start_time,
                    end_time=end_time,
                    count=count,
                    dividend_type=dividend_type,
                    fill_data=fill_data,
                )
                if result is None:
                    self._mark_gateway_down("market-data returned no data")
                    return None
                self._mark_gateway_up()
                return result
            except Exception as e:
                self._mark_gateway_down(str(e))
                return None
        if not self._ensure_initialized():
            logger.warning("TdxQuant 未初始化，无法获取市场数据")
            return None
        
        try:
            result = self._invoke_tq(
                "get_market_data",
                field_list=field_list,
                stock_list=stock_list,
                period=period,
                start_time=start_time,
                end_time=end_time,
                count=count,
                dividend_type=dividend_type,
                fill_data=fill_data
            )
            self._last_activity = datetime.now()
            return result
        except Exception as e:
            logger.error(f"获取市场数据失败: {e}")
            # 标记为未初始化，下次会重新初始化
            self._initialized = False
            return None

    def refresh_cache(self, force: bool = False, market: str = "") -> Optional[Any]:
        """Refresh TdxQuant quote/K-line cache through the native SDK."""
        if self._use_gateway():
            return self._gateway_client().refresh_cache(force=force, market=market)
        if not self._ensure_initialized():
            logger.warning("TdxQuant not initialized, cannot refresh cache")
            return None

        try:
            result = self._invoke_tq("refresh_cache", force=force, market=market)
            self._last_activity = datetime.now()
            logger.info(f"TdxQuant refresh_cache done: force={force}, market={market}, result={result}")
            return result
        except Exception as e:
            logger.error(f"TdxQuant refresh_cache failed: {e}")
            self._initialized = False
            return None

    def refresh_kline(self, stock_list: list, period: str) -> Optional[Any]:
        """Refresh TdxQuant historical K-line cache. SDK supports 1m/5m/1d."""
        if self._use_gateway():
            return self._gateway_client().refresh_kline(stock_list=stock_list, period=period)
        if not self._ensure_initialized():
            logger.warning("TdxQuant not initialized, cannot refresh kline")
            return None
        if not stock_list:
            return None

        try:
            result = self._invoke_tq("refresh_kline", stock_list=stock_list, period=period)
            self._last_activity = datetime.now()
            logger.info(f"TdxQuant refresh_kline done: period={period}, stocks={len(stock_list)}, result={result}")
            return result
        except Exception as e:
            logger.error(f"TdxQuant refresh_kline failed: period={period}, stocks={len(stock_list)}, error={e}")
            self._initialized = False
            return None

    def get_gb_info(self, stock_code: str, date_list: list, count: int = -1) -> Optional[list]:
        """Get share-capital history from the native TdxQuant SDK."""
        if self._use_gateway():
            return self._gateway_client().get_gb_info(stock_code=stock_code, date_list=date_list, count=count)
        if not self._ensure_initialized():
            logger.warning("TdxQuant not initialized, cannot fetch gb info")
            return None

        try:
            result = self._invoke_tq(
                "get_gb_info",
                stock_code=stock_code,
                date_list=date_list,
                count=count,
            )
            self._last_activity = datetime.now()
            return result
        except Exception as e:
            logger.error(f"get_gb_info failed ({stock_code}): {e}")
            self._initialized = False
            return None

    def get_gb_info_by_date(self, stock_code: str, start_date: str, end_date: str) -> Optional[list]:
        """Get share-capital history by date range.

        TdxQuant documents this as get_gb_info_by_date(stock_code, start_date, end_date).
        Some installed SDK versions only expose get_gb_info(date_list=...), so this wrapper
        prefers the documented API and falls back to explicit trading dates from local daily
        bars when needed.
        """
        if self._use_gateway():
            return self._gateway_client().get_gb_info_by_date(stock_code, start_date, end_date)
        if not self._ensure_initialized():
            logger.warning("TdxQuant not initialized, cannot fetch gb info by date")
            return None

        try:
            result = self._invoke_tq(
                "get_gb_info_by_date",
                stock_code=stock_code,
                start_date=start_date,
                end_date=end_date,
            )
            self._last_activity = datetime.now()
            return result
        except AttributeError:
            logger.warning("TdxQuant SDK does not expose get_gb_info_by_date; falling back to get_gb_info(date_list)")
        except Exception as e:
            logger.error(f"get_gb_info_by_date failed ({stock_code}): {e}")
            self._initialized = False
            return None

        try:
            import pandas as pd

            from utils.market_warehouse import clickhouse_client

            ch = clickhouse_client()
            dates = ch.query_df(
                f"""
                SELECT trade_date
                FROM kline_daily
                WHERE code = '{stock_code}'
                  AND trade_date BETWEEN '{start_date}' AND '{end_date}'
                ORDER BY trade_date
                """
            )
            if dates.empty:
                return []
            date_list = pd.to_datetime(dates["trade_date"], errors="coerce").dt.strftime("%Y%m%d").dropna().tolist()
            return self.get_gb_info(stock_code=stock_code, date_list=date_list, count=len(date_list))
        except Exception as e:
            logger.error(f"get_gb_info_by_date fallback failed ({stock_code}): {e}")
            return None
    
    def get_financial_data(self, stock_code: str, report_type: int = 1, report_period: str = None) -> Optional[Dict[str, Any]]:
        """
        获取财务数据
        
        Args:
            stock_code: 股票代码
            report_type: 报告类型 (1: 年报, 2: 中报, 3: 季报)
            report_period: 报告期
        
        Returns:
            财务数据字典
        """
        if self._use_gateway():
            return self._gateway_client().get_financial_data(stock_code, report_type, report_period)
        if not self._ensure_initialized():
            logger.warning("TdxQuant 未初始化，无法获取财务数据")
            return None
        
        try:
            result = self._invoke_tq(
                "get_financial_data",
                stock_code=stock_code,
                report_type=report_type,
                report_period=report_period
            )
            self._last_activity = datetime.now()
            return result
        except Exception as e:
            logger.error(f"获取财务数据失败: {e}")
            # 标记为未初始化，下次会重新初始化
            self._initialized = False
            return None
    
    def get_sector_data(self, sector_code: str = None) -> Optional[Any]:
        """
        获取板块数据
        
        Args:
            sector_code: 板块代码（可选）
        
        Returns:
            板块数据字典或列表
        """
        if self._use_gateway():
            return self._gateway_client().get_sector_data(sector_code)
        if not self._ensure_initialized():
            logger.warning("TdxQuant 未初始化，无法获取板块数据")
            return None
        
        try:
            if sector_code:
                result = self._invoke_tq("get_sector_data", sector_code=sector_code)
            else:
                result = self._invoke_tq("get_sector_data")
            self._last_activity = datetime.now()
            return result
        except AttributeError:
            try:
                if sector_code:
                    result = self._invoke_tq("get_board_data", board_code=sector_code)
                else:
                    result = self._invoke_tq("get_board_data")
                self._last_activity = datetime.now()
                return result
            except Exception as e:
                logger.error(f"获取板块数据失败: {e}")
                self._initialized = False
                return None
        except Exception as e:
            logger.error(f"获取板块数据失败: {e}")
            # 标记为未初始化，下次会重新初始化
            self._initialized = False
            return None
    
    def get_sector_list(self) -> Optional[List[Dict[str, Any]]]:
        """
        获取板块列表
        
        Returns:
            板块列表
        """
        if self._use_gateway():
            return self._gateway_client().get_sector_list()
        if not self._ensure_initialized():
            logger.warning("TdxQuant 未初始化，无法获取板块列表")
            return None
        
        try:
            result = self._invoke_tq("get_sector_list", list_type=1)
            self._last_activity = datetime.now()
            return result
        except AttributeError:
            try:
                result = self._invoke_tq("get_board_list")
                self._last_activity = datetime.now()
                return result
            except Exception as e:
                logger.error(f"获取板块列表失败: {e}")
                self._initialized = False
                return None
        except Exception as e:
            logger.error(f"获取板块列表失败: {e}")
            # 标记为未初始化，下次会重新初始化
            self._initialized = False
            return None
    
    def get_stock_list_in_sector(self, sector_code: str) -> Optional[List[Dict[str, Any]]]:
        """
        获取板块成分股
        
        Args:
            sector_code: 板块代码
        
        Returns:
            板块成分股列表
        """
        if self._use_gateway():
            return self._gateway_client().get_stock_list_in_sector(sector_code)
        if not self._ensure_initialized():
            logger.warning("TdxQuant 未初始化，无法获取板块成分股")
            return None

        try:
            # 使用get_stock_list接口，market参数为板块代码
            result = self._invoke_tq("get_stock_list_in_sector", block_code=sector_code, block_type=0, list_type=1)
            self._last_activity = datetime.now()
            logger.info(f"获取板块 {sector_code} 成分股: {len(result)} 只")
            return result
        except Exception as e:
            logger.error(f"获取板块成分股失败: {e}")
            # 标记为未初始化，下次会重新初始化
            self._initialized = False
            return None
    
    def get_status(self) -> DataSourceStatus:
        """
        获取连接池状态
        
        Returns:
            连接池状态
        """
        if self._use_gateway():
            try:
                health = self._gateway_client().health()
                if health.get("ok") and self._is_available_status(health.get("status")):
                    self._status = DataSourceStatus.AVAILABLE
                    self._last_activity = datetime.now()
                else:
                    self._status = DataSourceStatus.UNAVAILABLE
            except Exception:
                self._status = DataSourceStatus.UNAVAILABLE
        return self._status
    
    def get_last_activity(self) -> Optional[datetime]:
        """
        获取最后活动时间
        
        Returns:
            最后活动时间
        """
        return self._last_activity
    
    def close(self):
        """关闭连接"""
        with self._lock:
            if self.tq and hasattr(self.tq, 'close'):
                try:
                    with self._api_lock:
                        self.tq.close()
                    logger.info("TdxQuant 连接已关闭")
                except Exception as e:
                    logger.error(f"关闭 TdxQuant 连接失败: {e}")
            self._initialized = False
            self.tq = None
            self._status = DataSourceStatus.UNAVAILABLE
    
    def __del__(self):
        """析构函数，关闭连接"""
        self.close()


# 全局实例
tdxquant_pool = TdxQuantPool()


if __name__ == "__main__":
    """测试连接池"""
    # 测试获取股票列表
    stock_list = tdxquant_pool.get_stock_list(market="ALL")
    if stock_list:
        print(f"获取到 {len(stock_list)} 只股票")
        print(f"前5只股票: {stock_list[:5]}")
    else:
        print("获取股票列表失败")

    # 测试获取股票详细信息
    if stock_list:
        stock_code = stock_list[0].get('Code')
        if stock_code:
            stock_info = tdxquant_pool.get_stock_info(stock_code)
            if stock_info:
                print(f"股票 {stock_code} 详细信息: {stock_info}")
            else:
                print(f"获取股票 {stock_code} 详细信息失败")

    # 测试保活功能
    print("测试保活功能，等待 5 分钟...")
    time.sleep(300)

    # 再次测试获取股票列表
    stock_list = tdxquant_pool.get_stock_list(market="SH")
    if stock_list:
        print(f"保活后获取到 {len(stock_list)} 只上海市场股票")
    else:
        print("保活后获取股票列表失败")

    # 关闭连接
    tdxquant_pool.close()
    print("测试完成")
