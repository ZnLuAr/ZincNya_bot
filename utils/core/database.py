"""
utils/core/database.py

轻量 SQLite 数据库封装，统一管理连接配置和线程池调度。

职责：
    - 封装连接创建 + PRAGMA 配置（WAL、busy_timeout、row_factory）
    - 提供 asyncio.to_thread 包装的 run() 方法
    - 管理 schema 初始化（启动时调用一次）
    - 注册 ResourceManager 清理回调（退出时 WAL checkpoint）

使用方式：
    # 模块级声明（不做 I/O）
    db = Database(DB_PATH, "MyModule")

    # 启动时初始化 schema（由 appLifecycle 调用）
    def initDatabase():
        db.initSchema(_initSchema)

    # 异步数据库操作
    async def getData():
        def _query(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ...")
            return cursor.fetchall()
        return await db.run(_query)
"""




import os
import sqlite3
import asyncio
from typing import TypeVar, Callable

from utils.core.resourceManager import getResourceManager


T = TypeVar("T")




class Database:
    """
    SQLite 数据库封装

    每次 run() 调用创建独立连接（SQLite 连接创建 <0.1ms，不做池化）。
    连接统一配置 WAL 模式、busy_timeout、row_factory。
    """


    def __init__(self, dbPath: str, name: str):
        self._dbPath = dbPath
        self._name = name
        self._initialized = False


    def _connect(self) -> sqlite3.Connection:
        """创建并配置 SQLite 连接"""
        conn = sqlite3.connect(self._dbPath)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn


    def initSchema(self, schemaFunc: Callable[[sqlite3.Connection], None]):
        """
        同步初始化表结构（启动时调用一次，幂等）

        参数:
            schemaFunc: 接收 Connection 参数的函数，执行 CREATE TABLE 等 DDL
        """
        if self._initialized:
            return

        dbDir = os.path.dirname(self._dbPath)
        if dbDir:
            os.makedirs(dbDir, exist_ok=True)

        conn = self._connect()
        try:
            schemaFunc(conn)
            conn.commit()
        finally:
            conn.close()

        self._initialized = True

        # 注册 WAL checkpoint 清理回调（退出时合并 WAL 到主库）
        async def _cleanup():
            def _sync():
                c = self._connect()
                try:
                    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    c.close()
            await asyncio.to_thread(_sync)

        getResourceManager().register(
            f"Database({self._name})",
            _cleanup,
            priority=20
        )


    async def run(self, func: Callable[[sqlite3.Connection], T]) -> T:
        """
        在线程池中执行数据库操作，自动管理连接生命周期

        连接在操作完成后显式关闭（修复 with sqlite3.connect() 不关闭连接的问题）。
        事务语义：成功自动 commit，异常自动 rollback。

        参数:
            func: 接收 Connection 参数的同步函数
        """
        def _sync():
            conn = self._connect()
            try:
                with conn:  # commit on success, rollback on exception 👀
                    return func(conn)
            finally:
                conn.close()
        return await asyncio.to_thread(_sync)
