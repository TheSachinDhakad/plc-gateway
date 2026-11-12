"""aiomysql connection pool with UTC sessions, transactions and error classification.

Credentials come only from environment variables named in settings.yaml and are never logged.
"""

from __future__ import annotations

import logging
import os
import ssl
import warnings
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import aiomysql
import pymysql

from models.config import DatabaseSection
from utils.exceptions import ConfigError, DatabaseError, DatabaseUnavailableError

log = logging.getLogger(__name__)

# VALUES() in ON DUPLICATE KEY UPDATE is deprecated in MySQL 8.0.20+ but still supported in 8.4 LTS.
# It is used deliberately: PyMySQL's executemany() only batches INSERT statements it can parse,
# and it cannot parse the row-alias form ("VALUES (...) AS new"); per-row inserts would be ~100x slower.
warnings.filterwarnings("ignore", message=".*'VALUES function' is deprecated.*", category=pymysql.Warning)

# PyMySQL error codes that mean "server/connection unavailable" (retryable)
_UNAVAILABLE_CODES = {2003, 2006, 2013, 2055, 1040, 1053, 1205, 1213, 2002}


@dataclass(frozen=True)
class MySQLParams:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    ssl_ca: str | None = None
    connect_timeout: float = 10.0

    @classmethod
    def from_settings(cls, db: DatabaseSection, *, maintenance: bool = False) -> MySQLParams:
        def env(name: str, required: bool = True) -> str | None:
            value = os.environ.get(name)
            if required and not value:
                raise ConfigError(f"environment variable {name} is not set")
            return value or None

        user_env, pw_env = (db.maint_user_env, db.maint_password_env) if maintenance else (db.user_env,
                                                                                          db.password_env)
        port = env(db.port_env, required=False) or "3306"
        try:
            port_num = int(port)
        except ValueError as exc:
            raise ConfigError(f"{db.port_env} must be an integer") from exc
        return cls(
            host=env(db.host_env) or "",
            port=port_num,
            database=env(db.database_env) or "",
            user=env(user_env) or "",
            password=os.environ.get(pw_env, ""),
            ssl_ca=env(db.ssl_ca_env, required=False),
            connect_timeout=db.connect_timeout_s,
        )

    @classmethod
    def maintenance_available(cls, db: DatabaseSection) -> bool:
        return bool(os.environ.get(db.maint_user_env))

    @classmethod
    def from_dsn(cls, dsn: str) -> MySQLParams:
        """mysql://user:password@host:port/database (tests and scripts)."""
        from urllib.parse import unquote, urlparse

        u = urlparse(dsn)
        if u.scheme not in ("mysql", "mysql+aiomysql"):
            raise ConfigError("DSN must start with mysql://")
        return cls(host=u.hostname or "127.0.0.1", port=u.port or 3306, database=(u.path or "/").lstrip("/"),
                   user=unquote(u.username or ""), password=unquote(u.password or ""))


def classify(exc: BaseException) -> DatabaseError:
    if isinstance(exc, DatabaseError):
        return exc
    code = exc.args[0] if getattr(exc, "args", None) and isinstance(exc.args[0], int) else None
    if isinstance(exc, pymysql.err.OperationalError | pymysql.err.InterfaceError | OSError | TimeoutError) and (
            code is None or code in _UNAVAILABLE_CODES):
        return DatabaseUnavailableError(f"MySQL unavailable: {exc}")
    return DatabaseError(f"MySQL error: {exc}")


class MySQLClient:
    def __init__(self, params: MySQLParams, *, pool_min: int = 1, pool_max: int = 5) -> None:
        self.params = params
        self.pool_min = pool_min
        self.pool_max = pool_max
        self._pool: aiomysql.Pool | None = None

    @property
    def available(self) -> bool:
        return self._pool is not None and not self._pool.closed

    async def start(self) -> None:
        if self.available:
            return
        ssl_ctx = None
        if self.params.ssl_ca:
            ssl_ctx = ssl.create_default_context(cafile=self.params.ssl_ca)
        try:
            self._pool = await aiomysql.create_pool(
                host=self.params.host, port=self.params.port, user=self.params.user,
                password=self.params.password, db=self.params.database,
                minsize=self.pool_min, maxsize=self.pool_max, autocommit=False, charset="utf8mb4",
                connect_timeout=self.params.connect_timeout, ssl=ssl_ctx, pool_recycle=3600,
                init_command="SET time_zone = '+00:00', SESSION sql_mode = 'STRICT_ALL_TABLES,NO_ZERO_DATE'",
            )
        except Exception as exc:
            self._pool = None
            raise classify(exc) from exc
        log.info("MySQL pool started (%s:%s/%s, pool %d-%d)", self.params.host, self.params.port,
                 self.params.database, self.pool_min, self.pool_max)

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()
            await pool.wait_closed()

    def _require(self) -> aiomysql.Pool:
        if self._pool is None or self._pool.closed:
            raise DatabaseUnavailableError("MySQL pool not started")
        return self._pool

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiomysql.Cursor]:
        pool = self._require()
        try:
            conn = await pool.acquire()
        except Exception as exc:
            raise classify(exc) from exc
        try:
            await conn.begin()
            async with conn.cursor() as cur:
                yield cur
            await conn.commit()
        except BaseException as exc:
            try:
                await conn.rollback()
            except Exception as rb_exc:
                log.debug("rollback failed: %s", rb_exc)
            if isinstance(exc, Exception) and not isinstance(exc, DatabaseError):
                raise classify(exc) from exc
            raise
        finally:
            pool.release(conn)

    async def fetch_all(self, sql: str, args: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        pool = self._require()
        try:
            async with pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, args)
                rows = await cur.fetchall()
                await conn.commit()
                return list(rows)
        except DatabaseError:
            raise
        except Exception as exc:
            raise classify(exc) from exc

    async def fetch_one(self, sql: str, args: Sequence[Any] | None = None) -> dict[str, Any] | None:
        rows = await self.fetch_all(sql, args)
        return rows[0] if rows else None

    async def execute(self, sql: str, args: Sequence[Any] | None = None) -> int:
        async with self.transaction() as cur:
            await cur.execute(sql, args)
            return cur.rowcount

    async def ping(self) -> bool:
        try:
            await self.fetch_one("SELECT 1 AS ok")
            return True
        except DatabaseError:
            return False
