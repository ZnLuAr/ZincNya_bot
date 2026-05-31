"""
tests/utils/core/test_resourceManager.py

测试 utils/core/resourceManager.py 全局资源管理器
"""

import pytest
import threading
from unittest.mock import AsyncMock, MagicMock, patch

from utils.core.resourceManager import (
    ResourceManager,
    getResourceManager,
    cleanupAllResources,
)


# ============================================================================
# Fixture: 重置全局 resourceManager 状态
# ============================================================================

@pytest.fixture(autouse=True)
def reset_resource_manager():
    """每个测试前重置全局 resourceManager 状态"""
    from utils.core import resourceManager as rm_module
    rm_module._resourceManager = None
    yield
    rm_module._resourceManager = None


# ============================================================================
# 测试 ResourceManager.__init__() — 初始化
# ============================================================================

def test_resource_manager_init():
    """初始化空资源列表"""
    manager = ResourceManager()

    assert manager._resources == []


# ============================================================================
# 测试 ResourceManager.register() — 注册资源
# ============================================================================

def test_register_adds_resource():
    """添加资源到列表"""
    manager = ResourceManager()

    cleanup_func = AsyncMock()
    manager.register("TestResource", cleanup_func, priority=10)

    assert len(manager._resources) == 1
    assert manager._resources[0] == (10, "TestResource", cleanup_func)


def test_register_multiple_resources():
    """注册多个资源"""
    manager = ResourceManager()

    cleanup1 = AsyncMock()
    cleanup2 = AsyncMock()
    cleanup3 = AsyncMock()

    manager.register("Resource1", cleanup1, priority=10)
    manager.register("Resource2", cleanup2, priority=20)
    manager.register("Resource3", cleanup3, priority=0)

    assert len(manager._resources) == 3


def test_register_default_priority():
    """默认优先级为 0"""
    manager = ResourceManager()

    cleanup_func = AsyncMock()
    manager.register("TestResource", cleanup_func)

    assert manager._resources[0][0] == 0


# ============================================================================
# 测试 ResourceManager.cleanupAll() — 清理所有资源
# ============================================================================

@pytest.mark.asyncio
async def test_cleanup_all_calls_functions():
    """调用所有清理函数"""
    manager = ResourceManager()

    cleanup1 = AsyncMock()
    cleanup2 = AsyncMock()

    manager.register("Resource1", cleanup1)
    manager.register("Resource2", cleanup2)

    await manager.cleanupAll()

    cleanup1.assert_called_once()
    cleanup2.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_all_priority_order():
    """按优先级降序清理"""
    manager = ResourceManager()

    call_order = []

    async def cleanup1():
        call_order.append("Resource1")

    async def cleanup2():
        call_order.append("Resource2")

    async def cleanup3():
        call_order.append("Resource3")

    manager.register("Resource1", cleanup1, priority=10)
    manager.register("Resource2", cleanup2, priority=30)
    manager.register("Resource3", cleanup3, priority=20)

    await manager.cleanupAll()

    # 优先级降序：30 → 20 → 10
    assert call_order == ["Resource2", "Resource3", "Resource1"]


@pytest.mark.asyncio
async def test_cleanup_all_single_failure_continues():
    """单个资源清理失败不影响其他"""
    manager = ResourceManager()

    cleanup1 = AsyncMock()
    cleanup2 = AsyncMock(side_effect=ValueError("Cleanup failed"))
    cleanup3 = AsyncMock()

    manager.register("Resource1", cleanup1, priority=30)
    manager.register("Resource2", cleanup2, priority=20)
    manager.register("Resource3", cleanup3, priority=10)

    with patch("utils.core.resourceManager.safePrint") as mock_print:
        await manager.cleanupAll()

    # 验证所有清理函数都被调用
    cleanup1.assert_called_once()
    cleanup2.assert_called_once()
    cleanup3.assert_called_once()

    # 验证错误被输出
    mock_print.assert_called_once()
    call_text = mock_print.call_args[0][0]
    assert "Resource2" in call_text
    assert "Cleanup failed" in call_text


@pytest.mark.asyncio
async def test_cleanup_all_empty_list():
    """空资源列表清理不抛出"""
    manager = ResourceManager()

    await manager.cleanupAll()  # 不应该抛出异常


@pytest.mark.asyncio
async def test_cleanup_all_same_priority():
    """相同优先级按注册顺序"""
    manager = ResourceManager()

    call_order = []

    async def cleanup1():
        call_order.append("Resource1")

    async def cleanup2():
        call_order.append("Resource2")

    manager.register("Resource1", cleanup1, priority=10)
    manager.register("Resource2", cleanup2, priority=10)

    await manager.cleanupAll()

    # 相同优先级时，Python 的 sorted 是稳定排序，保持原顺序
    assert call_order == ["Resource1", "Resource2"]


# ============================================================================
# 测试 ResourceManager.getRegisteredResources() — 查询资源
# ============================================================================

def test_get_registered_resources_empty():
    """空列表返回空"""
    manager = ResourceManager()

    resources = manager.getRegisteredResources()

    assert resources == []


def test_get_registered_resources_returns_names():
    """返回资源名称列表"""
    manager = ResourceManager()

    cleanup1 = AsyncMock()
    cleanup2 = AsyncMock()
    cleanup3 = AsyncMock()

    manager.register("Resource1", cleanup1, priority=10)
    manager.register("Resource2", cleanup2, priority=20)
    manager.register("Resource3", cleanup3, priority=0)

    resources = manager.getRegisteredResources()

    assert set(resources) == {"Resource1", "Resource2", "Resource3"}


# ============================================================================
# 测试 getResourceManager() — 单例模式
# ============================================================================

def test_get_resource_manager_singleton():
    """首次创建，重复返回同一实例"""
    manager1 = getResourceManager()
    manager2 = getResourceManager()

    assert manager1 is manager2


def test_get_resource_manager_thread_safe():
    """线程安全（double-checked locking）"""
    managers = []

    def create_manager():
        managers.append(getResourceManager())

    threads = [threading.Thread(target=create_manager) for _ in range(10)]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    # 所有线程应该获得同一个实例
    assert len(set(id(m) for m in managers)) == 1


# ============================================================================
# 测试 cleanupAllResources() — 顶层函数
# ============================================================================

@pytest.mark.asyncio
async def test_cleanup_all_resources_function():
    """cleanupAllResources 函数路由到 manager.cleanupAll()"""
    manager = getResourceManager()

    cleanup_func = AsyncMock()
    manager.register("TestResource", cleanup_func)

    await cleanupAllResources()

    cleanup_func.assert_called_once()


# ============================================================================
# 测试集成场景
# ============================================================================

@pytest.mark.asyncio
async def test_integration_register_and_cleanup():
    """完整的注册和清理流程"""
    manager = getResourceManager()

    # 模拟多个资源
    session_closed = False
    db_closed = False
    file_closed = False

    async def close_session():
        nonlocal session_closed
        session_closed = True

    async def close_db():
        nonlocal db_closed
        db_closed = True

    async def close_file():
        nonlocal file_closed
        file_closed = True

    manager.register("aiohttp Session", close_session, priority=10)
    manager.register("Database", close_db, priority=20)
    manager.register("File Handle", close_file, priority=30)

    # 验证注册成功
    resources = manager.getRegisteredResources()
    assert "aiohttp Session" in resources
    assert "Database" in resources
    assert "File Handle" in resources

    # 清理
    await cleanupAllResources()

    # 验证所有资源都被清理
    assert session_closed
    assert db_closed
    assert file_closed


@pytest.mark.asyncio
async def test_integration_partial_failure():
    """部分资源清理失败的集成场景"""
    manager = getResourceManager()

    success_count = 0

    async def cleanup_success():
        nonlocal success_count
        success_count += 1

    async def cleanup_fail():
        raise RuntimeError("Cleanup failed")

    manager.register("Resource1", cleanup_success, priority=30)
    manager.register("Resource2", cleanup_fail, priority=20)
    manager.register("Resource3", cleanup_success, priority=10)

    with patch("utils.core.stateManager.safePrint"):
        await cleanupAllResources()

    # 验证成功的资源都被清理
    assert success_count == 2


@pytest.mark.asyncio
async def test_integration_cleanup_order_matters():
    """清理顺序影响结果的场景"""
    manager = getResourceManager()

    state = {"db_open": True, "session_open": True}

    async def close_session():
        # Session 依赖 DB，必须先关闭 Session
        if not state["db_open"]:
            raise RuntimeError("DB already closed!")
        state["session_open"] = False

    async def close_db():
        # DB 必须在 Session 关闭后才能关闭
        if state["session_open"]:
            raise RuntimeError("Session still open!")
        state["db_open"] = False

    # 正确的优先级：Session (20) > DB (10)
    manager.register("Session", close_session, priority=20)
    manager.register("Database", close_db, priority=10)

    await cleanupAllResources()

    # 验证清理成功
    assert not state["session_open"]
    assert not state["db_open"]


# ============================================================================
# 测试边界情况
# ============================================================================

def test_register_duplicate_names():
    """重复注册同名资源（允许，不去重）"""
    manager = ResourceManager()

    cleanup1 = AsyncMock()
    cleanup2 = AsyncMock()

    manager.register("Resource", cleanup1, priority=10)
    manager.register("Resource", cleanup2, priority=20)

    resources = manager.getRegisteredResources()
    assert resources.count("Resource") == 2


@pytest.mark.asyncio
async def test_cleanup_all_multiple_times():
    """多次清理（资源不会被移除，会重复清理）"""
    manager = ResourceManager()

    cleanup_func = AsyncMock()
    manager.register("Resource", cleanup_func)

    await manager.cleanupAll()
    await manager.cleanupAll()

    # 验证被调用两次
    assert cleanup_func.call_count == 2