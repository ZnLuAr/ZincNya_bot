"""LLM 任务与审核重试共用的临时工作上下文存储。"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet

from utils.core import crypto
from utils.core.database import Database
from utils.llm import task as taskModule
from utils.llm.context.store import ContextStore




@pytest.fixture
async def contextStore(tmp_path, monkeypatch):
    """每个用例使用临时数据库和密钥，执行前后的保存走真实事务。"""
    monkeypatch.setattr(crypto, "KEY_PATH", str(tmp_path / ".chatKey"))
    monkeypatch.setattr(crypto, "_fernetCache", Fernet(Fernet.generate_key()))
    monkeypatch.setattr("utils.core.database.getResourceManager", lambda: MagicMock())
    monkeypatch.setattr("utils.llm.context.store.logSystemEvent", AsyncMock())
    store = ContextStore(Database(str(tmp_path / "context.db"), "TaskTest"))
    await store.initialize()
    monkeypatch.setattr(taskModule, "getContextStore", lambda: store)
    return store


@pytest.fixture(autouse=True)
def cleanRuntimeState():
    """清空运行任务登记与上下文执行权（模块级状态），避免跨用例泄漏。"""
    from utils.llm.context import runtime

    runtime._runningTasks.clear()
    runtime._contextGates.clear()
    yield
    runtime._runningTasks.clear()
    runtime._contextGates.clear()
