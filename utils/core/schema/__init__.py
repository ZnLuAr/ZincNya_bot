"""
utils/core/schema/

集中存放各模块的 SQLite 建表 DDL（*.sql 快照）。

设计：
    - 每个 .sql 文件描述一个数据库的"初始幂等 schema"，全部用 CREATE TABLE / INDEX IF NOT EXISTS
    - 旧库的 ALTER TABLE 兼容迁移仍保留在各模块的 Python 代码中
    - Python 端通过 loadSchema(name) 读取后交给 conn.executescript() 执行

使用：
    from utils.core.schema import loadSchema
    conn.executescript(loadSchema("chatHistory"))
"""

import os


_SCHEMA_DIR = os.path.dirname(os.path.abspath(__file__))


def loadSchema(name: str) -> str:
    """读取 schema 目录下的 {name}.sql 文件。"""
    path = os.path.join(_SCHEMA_DIR, f"{name}.sql")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
