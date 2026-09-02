"""
数据库初始化与会话管理模块

对应 Java 原项目：
    - spring.datasource 配置
    - spring.sql.init (schema.sql 自动执行)
    - MyBatis-Plus 的 Session 管理

功能：
    - 创建异步数据库引擎和会话工厂
    - 应用启动时自动执行 schema.sql 初始化表结构
    - 提供 FastAPI 依赖注入用的异步数据库会话
"""

import os
import sqlite3
from collections.abc import AsyncGenerator

from loguru import logger
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import settings


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    if column not in _table_columns(conn, table):
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply small, ordered SQLite migrations without destructive rebuilds."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, "
        "applied_at DATETIME NOT NULL DEFAULT (datetime('now', 'localtime')))"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}

    if 1 not in applied:
        _add_column_if_missing(
            conn, "oci_create_task", "status", "varchar(32) NOT NULL DEFAULT 'pending'"
        )
        _add_column_if_missing(conn, "oci_create_task", "attempts", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(
            conn, "oci_create_task", "max_attempts", "INTEGER NOT NULL DEFAULT 1000"
        )
        _add_column_if_missing(conn, "oci_create_task", "last_error", "text")
        _add_column_if_missing(conn, "oci_create_task", "next_run_at", "datetime")
        _add_column_if_missing(
            conn,
            "oci_create_task",
            "updated_at",
            "datetime NOT NULL DEFAULT '1970-01-01 00:00:00'",
        )
        conn.execute(
            "UPDATE oci_create_task SET status = CASE "
            "WHEN status IN ('succeeded', 'failed', 'cancelled') THEN status "
            "WHEN paused = 1 THEN 'paused' ELSE 'pending' END, "
            "updated_at = COALESCE(NULLIF(updated_at, '1970-01-01 00:00:00'), create_time, datetime('now', 'localtime'))"
        )
        conn.execute("INSERT INTO schema_migrations(version) VALUES (1)")

    if 2 not in applied:
        conn.execute("INSERT INTO schema_migrations(version) VALUES (2)")

    if 3 not in applied:
        _add_column_if_missing(conn, "oci_create_task", "ssh_public_key", "text")
        conn.execute("INSERT INTO schema_migrations(version) VALUES (3)")

    if 4 not in applied:
        conn.execute(
            "UPDATE oci_create_task AS current SET status = 'cancelled', "
            "last_error = 'duplicate active task cancelled during migration' "
            "WHERE current.status IN ('pending', 'running', 'paused') AND EXISTS ("
            "SELECT 1 FROM oci_create_task AS newer "
            "WHERE newer.user_id = current.user_id "
            "AND newer.status IN ('pending', 'running', 'paused') "
            "AND (newer.create_time > current.create_time "
            "OR (newer.create_time = current.create_time AND newer.id > current.id)))"
        )
        conn.execute(
            "UPDATE oci_change_ip_task AS current SET status = 'cancelled', "
            "last_error = 'duplicate active task cancelled during migration' "
            "WHERE current.status IN ('pending', 'running', 'paused') AND EXISTS ("
            "SELECT 1 FROM oci_change_ip_task AS newer "
            "WHERE newer.user_id = current.user_id "
            "AND newer.instance_id = current.instance_id "
            "AND newer.status IN ('pending', 'running', 'paused') "
            "AND (newer.create_time > current.create_time "
            "OR (newer.create_time = current.create_time AND newer.id > current.id)))"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS oci_create_task_one_active_user "
            "ON oci_create_task(user_id) "
            "WHERE status IN ('pending', 'running', 'paused')"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS oci_change_ip_one_active_instance "
            "ON oci_change_ip_task(user_id, instance_id) "
            "WHERE status IN ('pending', 'running', 'paused')"
        )
        conn.execute("INSERT INTO schema_migrations(version) VALUES (4)")

    if 5 not in applied:
        from core.secrets import encrypt_secret, is_encrypted

        row = conn.execute(
            "SELECT id, value FROM oci_kv WHERE code = 'SYS_TG_BOT_TOKEN'"
        ).fetchone()
        if row and row[1] and not is_encrypted(row[1]):
            conn.execute(
                "UPDATE oci_kv SET value = ? WHERE id = ?",
                (encrypt_secret(row[1]), row[0]),
            )
        conn.execute("INSERT INTO schema_migrations(version) VALUES (5)")

    if 6 not in applied:
        conn.execute("INSERT INTO schema_migrations(version) VALUES (6)")

    if 7 not in applied:
        _add_column_if_missing(
            conn, "oci_create_task", "interval_max", "INTEGER NOT NULL DEFAULT 60"
        )
        _add_column_if_missing(conn, "oci_create_task", "availability_domain", "varchar(255)")
        _add_column_if_missing(
            conn,
            "oci_create_task",
            "availability_domain_index",
            "INTEGER NOT NULL DEFAULT 0",
        )
        conn.execute("UPDATE oci_create_task SET interval_max = interval")
        conn.execute("INSERT INTO schema_migrations(version) VALUES (7)")

    if 8 not in applied:
        _add_column_if_missing(
            conn,
            "oci_create_task",
            "operation_system_version",
            "varchar(128)",
        )
        conn.execute("INSERT INTO schema_migrations(version) VALUES (8)")

    if 9 not in applied:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS notification_outbox ("
            "id varchar(64) PRIMARY KEY, "
            "event_key varchar(512) NOT NULL, "
            "category varchar(64) NOT NULL, "
            "message text NOT NULL, "
            "status varchar(32) NOT NULL DEFAULT 'pending', "
            "attempts INTEGER NOT NULL DEFAULT 0, "
            "last_error text, "
            "next_attempt_at datetime, "
            "created_at datetime NOT NULL DEFAULT (datetime('now', 'localtime')), "
            "updated_at datetime NOT NULL DEFAULT (datetime('now', 'localtime')), "
            "sent_at datetime)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS notification_outbox_event_key "
            "ON notification_outbox(event_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS notification_outbox_status_next_attempt "
            "ON notification_outbox(status, next_attempt_at)"
        )
        conn.execute("INSERT INTO schema_migrations(version) VALUES (9)")

    if 10 not in applied:
        _add_column_if_missing(conn, "oci_create_task", "instance_name", "varchar(251)")
        _add_column_if_missing(
            conn,
            "oci_create_task",
            "initial_create_numbers",
            "INTEGER NOT NULL DEFAULT 1",
        )
        conn.execute(
            "UPDATE oci_create_task SET initial_create_numbers = "
            "CASE WHEN create_numbers > 0 THEN create_numbers ELSE 1 END"
        )
        conn.execute("INSERT INTO schema_migrations(version) VALUES (10)")

    if 11 not in applied:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ssh_public_key ("
            "id varchar(64) PRIMARY KEY, "
            "name varchar(128) COLLATE NOCASE NOT NULL, "
            "public_key text NOT NULL, "
            "fingerprint varchar(128) NOT NULL, "
            "create_time datetime NOT NULL DEFAULT (datetime('now', 'localtime')), "
            "update_time datetime NOT NULL DEFAULT (datetime('now', 'localtime')))"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ssh_public_key_name "
            "ON ssh_public_key(name)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ssh_public_key_fingerprint "
            "ON ssh_public_key(fingerprint)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ssh_public_key_create_time "
            "ON ssh_public_key(create_time DESC)"
        )
        conn.execute("INSERT INTO schema_migrations(version) VALUES (11)")

    if 12 not in applied:
        had_boot_volume_vpus = "boot_volume_vpus_per_gb" in _table_columns(
            conn, "oci_create_task"
        )
        _add_column_if_missing(
            conn,
            "oci_create_task",
            "boot_volume_vpus_per_gb",
            "INTEGER NOT NULL DEFAULT 20",
        )
        if not had_boot_volume_vpus:
            conn.execute("UPDATE oci_create_task SET boot_volume_vpus_per_gb = 10")
        conn.execute("INSERT INTO schema_migrations(version) VALUES (12)")

# ============================
#  创建异步数据库引擎
# ============================

# 创建 SQLAlchemy 异步引擎
# - echo=False: 不打印 SQL 语句（生产环境）
# - pool_pre_ping=True: 在使用连接前先测试连接是否有效
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)

# ============================
#  创建异步会话工厂
# ============================

# async_sessionmaker 创建异步会话工厂
# - expire_on_commit=False: 提交后不自动过期对象（避免懒加载问题）
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ============================
#  数据库会话依赖注入
# ============================


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI 依赖注入：提供异步数据库会话。

    使用方式:
        @router.get("/users")
        async def get_users(db: AsyncSession = Depends(get_db)):
            ...

    会话在请求处理完毕后（包括异常情况）自动关闭。
    """
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


# ============================
#  数据库初始化
# ============================


def init_database() -> None:
    """
    同步初始化数据库表结构。

    对应 Java 原项目中 spring.sql.init.mode=always 的行为：
    每次应用启动时执行 schema.sql 中的建表语句。
    由于使用了 "CREATE TABLE IF NOT EXISTS"，重复执行不会导致数据丢失。

    注意：这里使用原生 sqlite3 同步执行，因为在应用启动阶段
    异步事件循环可能尚未完全就绪。
    """
    # 查找 schema.sql 文件路径
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    if not os.path.exists(schema_path):
        logger.warning(f"数据库 Schema 文件不存在: {schema_path}，跳过初始化")
        return

    # 读取 SQL 脚本内容
    with open(schema_path, encoding="utf-8") as f:
        schema_sql = f.read()

    # 使用原生 sqlite3 执行建表脚本
    try:
        db_parent = os.path.dirname(os.path.abspath(settings.db_path))
        os.makedirs(db_parent, exist_ok=True)
        with sqlite3.connect(settings.db_path) as conn:
            conn.executescript(schema_sql)
            _apply_migrations(conn)
            conn.commit()
        logger.info(f"数据库初始化完成，数据库文件: {settings.db_path}")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")
        raise


# 异步包装函数，供 main.py 的 lifespan 中 await 调用
async def init_db() -> None:
    """
    异步初始化数据库（内部调用同步 init_database）。

    由于 init_database 使用原生 sqlite3 执行，本身不需要异步，
    但 main.py 中以 await 方式调用，故提供此包装函数。
    """
    init_database()


# async_session 别名，供 services 层在后台任务中创建独立会话
async_session = async_session_factory
