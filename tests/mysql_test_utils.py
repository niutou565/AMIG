"""测试用 MySQL 数据库辅助工具。

设计：
- 每个测试类创建独立数据库（amig_test_<随机后缀>），完全隔离，不接触开发库；
- 通过修改 app.database.MYSQL_DATABASE 并重置连接池来接管连接；
- 测试结束（tearDownClass）删除该库；
- 若本机没有可用 MySQL，抛出 unittest.SkipTest 并明确说明，而不是假装通过。

用法：
    from tests.mysql_test_utils import create_test_database, drop_test_database

    @classmethod
    def setUpClass(cls):
        cls.db_name = create_test_database()

    @classmethod
    def tearDownClass(cls):
        drop_test_database(cls.db_name)
"""
import secrets
import unittest

import pymysql

import app.database as database
from app.config import (
    MYSQL_CHARSET,
    MYSQL_COLLATION,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_USER,
)

# 测试库名后缀只允许字母数字，防止拼接 DDL 时注入
_TEST_DB_REQUIRED = "amig_test_"


def _admin_connection(database_name=None):
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=database_name,
        charset=MYSQL_CHARSET,
        autocommit=True,
    )


def mysql_available():
    """探测测试用 MySQL 是否可用。"""
    try:
        conn = _admin_connection()
        conn.close()
        return True
    except Exception:
        return False


def require_mysql():
    """没有可用 MySQL 时跳过测试并给出明确原因。"""
    if not mysql_available():
        raise unittest.SkipTest(
            "未检测到可用的 MySQL（请配置 MYSQL_HOST/MYSQL_USER/MYSQL_PASSWORD "
            "并启动 MySQL 8.0+ 后重跑测试）"
        )


def create_test_database(prefix=_TEST_DB_REQUIRED):
    """创建独立测试库、切换到该库并初始化表结构，返回库名。"""
    require_mysql()
    name = prefix + secrets.token_hex(6)
    conn = _admin_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE `{name}` "
                f"CHARACTER SET {MYSQL_CHARSET} COLLATE {MYSQL_COLLATION}"
            )
    finally:
        conn.close()

    database.MYSQL_DATABASE = name
    database._reset_pool()
    database.init_db(max_wait_seconds=30)
    return name


def drop_test_database(name):
    """关闭连接池并删除测试库（只允许删除 amig_test_ 前缀的库，防误删）。"""
    if not name or not name.startswith(_TEST_DB_REQUIRED):
        raise RuntimeError(f"拒绝删除非测试库: {name!r}")
    database._reset_pool()
    conn = _admin_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
    finally:
        conn.close()


def clean_tables():
    """清空全部业务表（保留表结构与 schema_migrations），用于用例间隔离。"""
    db = database.get_db()
    tables = [t for t in database.EXPECTED_TABLES if t != "schema_migrations"]
    db.execute("SET FOREIGN_KEY_CHECKS=0")
    try:
        for table in tables:
            db.execute(f"TRUNCATE TABLE `{table}`")
    finally:
        db.execute("SET FOREIGN_KEY_CHECKS=1")
