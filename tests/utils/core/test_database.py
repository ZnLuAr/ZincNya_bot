"""
tests/utils/core/test_database.py

测试 utils/core/database.py SQLite 数据库封装
"""

import os
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

from utils.core.database import Database
from tests.conftest import DB_BUSY_TIMEOUT


# ============================================================================
# 测试 Database.__init__() — 状态初始化
# ============================================================================

def test_database_init():
    """初始化不做 I/O"""
    db = Database("/fake/path.db", "TestDB")

    assert db._dbPath == "/fake/path.db"
    assert db._name == "TestDB"
    assert db._initialized is False


# ============================================================================
# 测试 Database._connect() — PRAGMA 设置
# ============================================================================

def test_connect_sets_pragma(tmp_path):
    """连接时设置 WAL 模式和 busy_timeout"""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path), "TestDB")

    conn = db._connect()

    # 验证 WAL 模式
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode")
    journal_mode = cursor.fetchone()[0]
    assert journal_mode.upper() == "WAL"

    # 验证 busy_timeout
    cursor.execute("PRAGMA busy_timeout")
    timeout = cursor.fetchone()[0]
    assert timeout == DB_BUSY_TIMEOUT

    # 验证 row_factory
    assert conn.row_factory == sqlite3.Row

    conn.close()


# ============================================================================
# 测试 Database.initSchema() — 幂等性
# ============================================================================

def test_init_schema_first_call(tmp_path):
    """首次调用执行 schema 函数"""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path), "TestDB")

    schema_called = False

    def schema_func(conn):
        nonlocal schema_called
        schema_called = True
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")

    db.initSchema(schema_func)

    assert schema_called
    assert db._initialized is True
    assert Path(db_path).exists()


def test_init_schema_idempotent(tmp_path):
    """重复调用幂等"""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path), "TestDB")

    call_count = 0

    def schema_func(conn):
        nonlocal call_count
        call_count += 1
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")

    db.initSchema(schema_func)
    db.initSchema(schema_func)

    assert call_count == 1


def test_init_schema_creates_directory(tmp_path):
    """自动创建目录"""
    db_path = tmp_path / "subdir" / "test.db"
    db = Database(str(db_path), "TestDB")

    def schema_func(conn):
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")

    db.initSchema(schema_func)

    assert db_path.parent.exists()
    assert db_path.exists()


def test_init_schema_registers_cleanup(tmp_path):
    """注册 ResourceManager 清理回调"""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path), "TestDB")

    def schema_func(conn):
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")

    with patch("utils.core.database.getResourceManager") as mock_rm:
        mock_manager = MagicMock()
        mock_rm.return_value = mock_manager

        db.initSchema(schema_func)

    mock_manager.register.assert_called_once()
    call_args = mock_manager.register.call_args
    assert "Database(TestDB)" in call_args[0][0]
    assert call_args[1]["priority"] == 20


# ============================================================================
# 测试 Database.run() — 事务语义
# ============================================================================

@pytest.mark.asyncio
async def test_run_success_commits(tmp_path):
    """成功时自动 commit"""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path), "TestDB")

    def schema_func(conn):
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value TEXT)")

    db.initSchema(schema_func)

    def insert_func(conn):
        conn.execute("INSERT INTO test (value) VALUES (?)", ("test_value",))

    await db.run(insert_func)

    # 验证数据已提交
    def verify_func(conn):
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM test")
        return cursor.fetchone()[0]

    result = await db.run(verify_func)
    assert result == "test_value"


@pytest.mark.asyncio
async def test_run_exception_rollback(tmp_path):
    """异常时自动 rollback"""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path), "TestDB")

    def schema_func(conn):
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value TEXT)")

    db.initSchema(schema_func)

    def failing_func(conn):
        conn.execute("INSERT INTO test (value) VALUES (?)", ("test_value",))
        raise ValueError("Test error")

    with pytest.raises(ValueError):
        await db.run(failing_func)

    # 验证数据未提交
    def verify_func(conn):
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM test")
        return cursor.fetchone()[0]

    count = await db.run(verify_func)
    assert count == 0


@pytest.mark.asyncio
async def test_run_closes_connection(tmp_path):
    """连接在操作完成后关闭"""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path), "TestDB")

    def schema_func(conn):
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")

    db.initSchema(schema_func)

    conn_ref = None

    def capture_conn(conn):
        nonlocal conn_ref
        conn_ref = conn
        return "result"

    result = await db.run(capture_conn)

    assert result == "result"
    # 验证连接已关闭（尝试操作会抛出异常）
    with pytest.raises(sqlite3.ProgrammingError):
        conn_ref.execute("SELECT 1")


@pytest.mark.asyncio
async def test_run_returns_value(tmp_path):
    """返回值传递"""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path), "TestDB")

    def schema_func(conn):
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO test (value) VALUES (?)", ("test_value",))

    db.initSchema(schema_func)

    def query_func(conn):
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM test")
        return cursor.fetchone()[0]

    result = await db.run(query_func)

    assert result == "test_value"


# ============================================================================
# 测试端到端 CRUD 操作
# ============================================================================

@pytest.mark.asyncio
async def test_crud_operations(tmp_path):
    """完整的 CRUD 操作"""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path), "TestDB")

    # 初始化 schema
    def schema_func(conn):
        conn.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT UNIQUE
            )
        """)

    db.initSchema(schema_func)

    # Create
    def insert_user(conn):
        conn.execute("INSERT INTO users (name, email) VALUES (?, ?)", ("Alice", "alice@example.com"))
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    user_id = await db.run(insert_user)
    assert user_id == 1

    # Read
    def get_user(conn):
        cursor = conn.cursor()
        cursor.execute("SELECT name, email FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    user = await db.run(get_user)
    assert user["name"] == "Alice"
    assert user["email"] == "alice@example.com"

    # Update
    def update_user(conn):
        conn.execute("UPDATE users SET email = ? WHERE id = ?", ("alice.new@example.com", user_id))

    await db.run(update_user)

    user = await db.run(get_user)
    assert user["email"] == "alice.new@example.com"

    # Delete
    def delete_user(conn):
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    await db.run(delete_user)

    user = await db.run(get_user)
    assert user is None


@pytest.mark.asyncio
async def test_concurrent_writes(tmp_path):
    """并发写入（busy_timeout 保护）"""
    import asyncio

    db_path = tmp_path / "test.db"
    db = Database(str(db_path), "TestDB")

    def schema_func(conn):
        conn.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, value INTEGER)")
        conn.execute("INSERT INTO counter (id, value) VALUES (1, 0)")

    db.initSchema(schema_func)

    # 使用原子操作（UPDATE 直接递增）
    async def increment():
        def _increment(conn):
            conn.execute("UPDATE counter SET value = value + 1 WHERE id = 1")

        await db.run(_increment)

    # 并发执行 10 次
    await asyncio.gather(*[increment() for _ in range(10)])

    # 验证最终值
    def get_value(conn):
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM counter WHERE id = 1")
        return cursor.fetchone()[0]

    final_value = await db.run(get_value)
    assert final_value == 10


@pytest.mark.asyncio
async def test_wal_checkpoint_cleanup(tmp_path):
    """WAL checkpoint 清理回调"""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path), "TestDB")

    def schema_func(conn):
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")

    # 捕获注册的清理回调
    cleanup_func = None

    with patch("utils.core.database.getResourceManager") as mock_rm:
        mock_manager = MagicMock()
        mock_rm.return_value = mock_manager

        def capture_cleanup(name, func, priority):
            nonlocal cleanup_func
            cleanup_func = func

        mock_manager.register.side_effect = capture_cleanup

        db.initSchema(schema_func)

    assert cleanup_func is not None

    # 执行清理回调
    await cleanup_func()

    # 验证 WAL 文件被清理（TRUNCATE 模式）
    wal_file = Path(str(db_path) + "-wal")
    # WAL 文件可能不存在（已被 checkpoint 清理）或大小为 0
    if wal_file.exists():
        assert wal_file.stat().st_size == 0


# ============================================================================
# 测试边界情况
# ============================================================================

@pytest.mark.asyncio
async def test_run_with_empty_database(tmp_path):
    """空数据库查询"""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path), "TestDB")

    def schema_func(conn):
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")

    db.initSchema(schema_func)

    def query_func(conn):
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM test")
        return cursor.fetchall()

    result = await db.run(query_func)
    assert result == []


@pytest.mark.asyncio
async def test_run_with_constraint_violation(tmp_path):
    """约束违反时抛出异常"""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path), "TestDB")

    def schema_func(conn):
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value TEXT UNIQUE)")

    db.initSchema(schema_func)

    def insert_func(conn):
        conn.execute("INSERT INTO test (value) VALUES (?)", ("unique_value",))

    await db.run(insert_func)

    # 重复插入应该失败
    with pytest.raises(sqlite3.IntegrityError):
        await db.run(insert_func)


def test_init_schema_with_no_directory(tmp_path):
    """数据库路径无目录时（当前目录）"""
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)

        db = Database("test.db", "TestDB")

        def schema_func(conn):
            conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")

        db.initSchema(schema_func)

        assert Path("test.db").exists()
    finally:
        os.chdir(original_cwd)