"""
Database connection utilities.
"""

from __future__ import annotations

import time
from typing import Generator, Optional

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from sqlalchemy.pool import QueuePool

from utils.config import ConfigManager
from utils.exceptions import DatabaseException
from utils.logger import get_logger

config = ConfigManager()
Base = declarative_base()
logger = get_logger(__name__)


class DatabaseManager:
    _instance = None
    _engine: Optional[Engine] = None
    _session_factory: Optional[sessionmaker] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._init_engine()

    def _init_engine(self):
        db_config = config.get_database_config()
        db_type = str(db_config.get("type", "sqlite")).strip().lower()
        if db_type != "clickhouse":
            raise RuntimeError(f"Hard-cut mode: only ClickHouse is allowed, current database.type={db_type}")
        db_url = self._build_clickhouse_url(db_config)
        default_pool_size = 1 if db_type == "clickhouse" else 20
        default_max_overflow = 0 if db_type == "clickhouse" else 10

        self._engine = create_engine(
            db_url,
            pool_pre_ping=True,
            echo=db_config.get("echo", False),
            pool_size=db_config.get("pool_size", default_pool_size),
            max_overflow=db_config.get("max_overflow", default_max_overflow),
            pool_timeout=db_config.get("pool_timeout", 30),
            poolclass=QueuePool,
        )

        self._session_factory = sessionmaker(bind=self._engine, autocommit=False, autoflush=False)

    def _build_clickhouse_url(self, db_config: dict) -> str:
        host = db_config.get("host", "127.0.0.1")
        port = db_config.get("port", 8123)
        username = db_config.get("username", "default")
        password = db_config.get("password", "")
        database = db_config.get("database", "stock")
        return f"clickhousedb://{username}:{password}@{host}:{port}/{database}"

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            raise DatabaseException("Database engine not initialized")
        return self._engine

    def get_session(self) -> Generator[Session, None, None]:
        if self._session_factory is None:
            raise DatabaseException("Session factory not initialized")
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_tables(self):
        # ClickHouse SQLAlchemy requires per-table ENGINE declarations.
        # This codebase currently mixes ORM models that are not ClickHouse-native,
        # so running metadata.create_all() will fail at startup.
        # Table lifecycle is managed by migration/bootstrap SQL paths instead.
        db_type = str(config.get_database_config().get("type", "")).strip().lower()
        if db_type == "clickhouse":
            from utils.clickhouse_schema import ensure_clickhouse_tables

            ensure_clickhouse_tables(self.engine)
            return
        Base.metadata.create_all(self._engine)

    def drop_tables(self):
        db_type = str(config.get_database_config().get("type", "")).strip().lower()
        if db_type == "clickhouse":
            return
        Base.metadata.drop_all(self._engine)

    def _startup_retry_config(self) -> tuple[int, float]:
        db_cfg = config.get_database_config() or {}
        db_type = str(db_cfg.get("type", "")).strip().lower()
        attempts = 1
        sleep_sec = 0.0
        if db_type == "clickhouse":
            attempts = int(db_cfg.get("startup_test_retries", 15) or 15)
            sleep_sec = float(db_cfg.get("startup_test_retry_interval_sec", 3) or 3)
        return max(1, attempts), max(0.0, sleep_sec)

    @staticmethod
    def _is_clickhouse_saturated(exc: Exception) -> bool:
        message = str(exc)
        return "TOO_MANY_SIMULTANEOUS_QUERIES" in message or "code: 202" in message

    def test_connection(self) -> bool:
        attempts, sleep_sec = self._startup_retry_config()
        last_exc = None
        for i in range(attempts):
            try:
                with self._engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
                return True
            except Exception as exc:
                last_exc = exc
                if i < attempts - 1 and sleep_sec > 0:
                    if self._is_clickhouse_saturated(exc):
                        logger.warning(
                            "ClickHouse is saturated during startup connection check "
                            f"(attempt {i + 1}/{attempts}); retrying in {sleep_sec:g}s"
                        )
                    time.sleep(sleep_sec)
        raise DatabaseException(f"Database connection test failed: {last_exc}", {"error": str(last_exc)})

    def ensure_ready_for_startup(self) -> None:
        attempts, sleep_sec = self._startup_retry_config()
        last_exc = None
        for i in range(attempts):
            try:
                with self._engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
                self.create_tables()
                return
            except Exception as exc:
                last_exc = exc
                if i < attempts - 1 and sleep_sec > 0 and self._is_clickhouse_saturated(exc):
                    logger.warning(
                        "ClickHouse is saturated while ensuring startup tables "
                        f"(attempt {i + 1}/{attempts}); retrying in {sleep_sec:g}s"
                    )
                    time.sleep(sleep_sec)
                    continue
                break
        raise DatabaseException(f"Database startup readiness check failed: {last_exc}", {"error": str(last_exc)})


db = DatabaseManager()


def get_db() -> Generator[Session, None, None]:
    yield from db.get_session()
