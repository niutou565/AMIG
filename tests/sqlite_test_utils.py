"""SQLite 后端的测试隔离工具。

与 tests/mysql_test_utils.py 对应：
- 每个测试类使用独立临时目录中的 app.db，互不影响，也不接触真实数据库；
- 通过修改 app.database.DATABASE_PATH 并重置连接来接管连接；
- 当前进程不是 SQLite 后端时抛出 unittest.SkipTest（明确跳过而不是假装通过）。

用法：
    from tests.sqlite_test_utils import create_test_database, drop_test_database

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = create_test_database()

    @classmethod
    def tearDownClass(cls):
        drop_test_database(cls.tmpdir)
"""
import os
import shutil
import tempfile
import unittest

import app.database as database

_TMP_PREFIX = "amig_test_sqlite_"


def require_sqlite():
    """当前后端不是 SQLite 时跳过测试。"""
    if not database.IS_SQLITE:
        raise unittest.SkipTest(
            "当前数据库后端为 %s；设置 DB_TYPE=sqlite 后重跑 SQLite 专项测试"
            % database.DB_TYPE
        )


def create_test_database():
    """创建临时 SQLite 测试库并初始化表结构，返回临时目录路径。"""
    require_sqlite()
    tmpdir = tempfile.mkdtemp(prefix=_TMP_PREFIX)
    database.DATABASE_PATH = os.path.join(tmpdir, "app.db")
    database._reset_pool()
    database.init_db(max_wait_seconds=10)
    return tmpdir


def drop_test_database(tmpdir):
    """关闭连接并删除临时测试目录（只允许删除本工具创建的目录）。"""
    if not tmpdir or not os.path.basename(tmpdir).startswith(_TMP_PREFIX):
        raise RuntimeError(f"拒绝删除非测试目录: {tmpdir!r}")
    database._reset_pool()
    shutil.rmtree(tmpdir, ignore_errors=True)


def clean_tables():
    """清空全部业务表（保留表结构与 schema_migrations），用于用例间隔离。"""
    db = database.get_db()
    tables = [t for t in database.EXPECTED_TABLES if t != "schema_migrations"]
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        for table in tables:
            db.execute(f"DELETE FROM `{table}`")
    finally:
        db.execute("PRAGMA foreign_keys=ON")
    db.commit()
