"""
监控和告警服务

提供系统监控和告警功能。
"""

from typing import Any, Dict, List
import time
import threading
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from utils.database import get_db
from utils.logger import get_logger
from utils.config import config

logger = get_logger("MonitoringService")


class MonitoringService:
    """监控和告警服务。"""

    def __init__(self):
        """初始化监控服务。"""
        from data_fetcher.manager import DataSourceManager

        self.data_source_manager = DataSourceManager()
        self.alert_history = []
        self.alert_lock = threading.Lock()
        self.last_health_check = datetime.now()

    def start_monitoring(self):
        """启动监控服务。"""
        logger.info("启动监控服务")

        # 启动健康检查线程。
        health_check_thread = threading.Thread(target=self._health_check_loop, daemon=True)
        health_check_thread.start()

        # 启动性能监控线程。
        performance_thread = threading.Thread(target=self._performance_monitor_loop, daemon=True)
        performance_thread.start()

    def _health_check_loop(self):
        """健康检查循环。"""
        while True:
            try:
                self.check_data_sources_health()
                self.check_database_health()
            except Exception as e:
                logger.error(f"健康检查失败: {e}")

            # 每 60 秒检查一次。
            time.sleep(60)

    def _performance_monitor_loop(self):
        """性能监控循环。"""
        while True:
            try:
                self.check_system_performance()
            except Exception as e:
                logger.error(f"性能监控失败: {e}")

            # 每 5 分钟检查一次。
            time.sleep(300)

    def check_data_sources_health(self):
        """检查数据源健康状态。"""
        status = self.data_source_manager.get_source_status()
        health_results = self.data_source_manager.health_check()

        for source_name, is_healthy in health_results.items():
            source_status = status.get(source_name, {})
            health_score = source_status.get("health_score", 100)

            if not is_healthy:
                self._trigger_alert(
                    level="ERROR",
                    category="DATA_SOURCE",
                    message=f"数据源 {source_name} 健康检查失败",
                    details={
                        "source": source_name,
                        "status": source_status.get("status"),
                        "error": source_status.get("last_error_message"),
                    },
                )
            elif health_score < 50:
                self._trigger_alert(
                    level="WARNING",
                    category="DATA_SOURCE",
                    message=f"数据源 {source_name} 健康评分过低",
                    details={
                        "source": source_name,
                        "health_score": health_score,
                        "status": source_status.get("status"),
                    },
                )

    def check_database_health(self):
        """检查数据库健康状态。"""
        try:
            with next(get_db()) as session:
                # 测试数据库连接。
                result = session.execute("SELECT 1").scalar()
                if result != 1:
                    self._trigger_alert(
                        level="ERROR",
                        category="DATABASE",
                        message="数据库连接测试失败",
                        details={"result": result},
                    )
                else:
                    logger.debug("数据库连接正常")
        except Exception as e:
            self._trigger_alert(
                level="ERROR",
                category="DATABASE",
                message="数据库连接异常",
                details={"error": str(e)},
            )

    def check_system_performance(self):
        """检查系统性能。"""
        try:
            import psutil

            memory = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=1)

            if memory.percent > 80:
                self._trigger_alert(
                    level="WARNING",
                    category="SYSTEM",
                    message="内存使用过高",
                    details={
                        "memory_percent": memory.percent,
                        "cpu_percent": cpu,
                    },
                )

            if cpu > 80:
                self._trigger_alert(
                    level="WARNING",
                    category="SYSTEM",
                    message="CPU 使用率过高",
                    details={
                        "cpu_percent": cpu,
                        "memory_percent": memory.percent,
                    },
                )
        except ImportError:
            logger.warning("psutil 库未安装，无法进行系统性能监控")
        except Exception as e:
            logger.error(f"系统性能监控失败: {e}")

    def _trigger_alert(self, level: str, category: str, message: str, details: Dict | None = None):
        """触发告警。"""
        alert = {
            "timestamp": datetime.now(),
            "level": level,
            "category": category,
            "message": message,
            "details": details or {},
        }

        with self.alert_lock:
            self.alert_history.append(alert)
            # 保留最近 100 条告警。
            if len(self.alert_history) > 100:
                self.alert_history = self.alert_history[-100:]

        logger.info(f"[{level}] {category}: {message}")
        self._send_alert_notification(alert)

    def _send_alert_notification(self, alert: Dict):
        """发送告警通知。"""
        email_config = config.get("email", {})
        enabled = email_config.get("enabled", True)

        if not enabled:
            return

        try:
            smtp_server = email_config.get("smtp_server")
            smtp_port = email_config.get("smtp_port")
            smtp_user = email_config.get("smtp_user")
            smtp_password = email_config.get("smtp_password")
            from_email = email_config.get("from_email")
            to_emails = email_config.get("to_emails", [])

            if not all([smtp_server, smtp_port, smtp_user, smtp_password, from_email, to_emails]):
                logger.warning("邮件配置不完整，无法发送告警")
                return

            msg = MIMEMultipart()
            msg["From"] = from_email
            msg["To"] = ", ".join(to_emails)
            msg["Subject"] = f"[AiStock] {alert['level']}: {alert['category']} - {alert['message']}"

            body = f"""
            告警时间: {alert['timestamp']}
            告警级别: {alert['level']}
            告警类别: {alert['category']}
            告警消息: {alert['message']}
            详细信息: {alert['details']}
            """

            msg.attach(MIMEText(body, "plain", "utf-8"))

            # 发送邮件。
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)

            logger.info(f"告警邮件已发送: {alert['message']}")
        except Exception as e:
            logger.error(f"发送告警邮件失败: {e}")

    def get_alert_history(self, limit: int = 50) -> List[Dict]:
        """获取告警历史。"""
        with self.alert_lock:
            return self.alert_history[-limit:]

    def get_system_status(self) -> Dict[str, Any]:
        """获取系统状态。"""
        status = {
            "timestamp": datetime.now(),
            "data_sources": self.data_source_manager.get_source_status(),
            "database": {
                "status": "unknown",
            },
            "system": {
                "status": "unknown",
            },
            "alerts": self.get_alert_history(10),
        }

        # 检查数据库状态。
        try:
            with next(get_db()) as session:
                session.execute("SELECT 1")
                status["database"]["status"] = "healthy"
        except Exception as e:
            status["database"]["status"] = "unhealthy"
            status["database"]["error"] = str(e)

        # 检查系统状态。
        try:
            import psutil

            memory = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=0.1)
            status["system"]["status"] = "healthy"
            status["system"]["memory_percent"] = memory.percent
            status["system"]["cpu_percent"] = cpu
        except Exception as e:
            status["system"]["status"] = "unknown"
            status["system"]["error"] = str(e)

        return status


# 全局监控服务实例
monitoring_service = MonitoringService()
