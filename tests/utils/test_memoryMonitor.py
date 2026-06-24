"""
tests/utils/test_memoryMonitor.py

测试 utils/memoryMonitor.py
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.memoryMonitor import (
    alertMessageConstructor,
    cancelAllStickerTasks,
    isMemoryLow,
    registerStickerTask,
    unregisterStickerTask,
    _activeStickerTasks,
)


@pytest.fixture(autouse=True)
def clearActiveTasks():
    """每个测试前后清空全局任务列表，避免污染"""
    _activeStickerTasks.clear()
    yield
    _activeStickerTasks.clear()


# ============================================================================
# isMemoryLow() 测试
# ============================================================================

def test_isMemoryLow_sufficient():
    """可用内存充足时返回 False"""
    with patch('utils.memoryMonitor.psutil.virtual_memory') as m:
        m.return_value.available = 500 * 1024 * 1024
        assert isMemoryLow() is False


def test_isMemoryLow_insufficient():
    """可用内存不足时返回 True"""
    with patch('utils.memoryMonitor.psutil.virtual_memory') as m:
        m.return_value.available = 100 * 1024 * 1024
        assert isMemoryLow() is True


# ============================================================================
# alertMessageConstructor() 测试
# ============================================================================

def test_alertMessageConstructor_no_jobs():
    """无下载任务时，keyboard 为 None，文本含可用内存"""
    # _activeStickerTasks 为空时
    text, keyboard = alertMessageConstructor(150)
    assert "150" in text
    assert keyboard is None


def test_alertMessageConstructor_with_jobs():
    """有下载任务时，附带终止按钮"""
    # 直接模拟任务列表非空（不需要真创建 asyncio.Task）
    _activeStickerTasks.append("mock_task")

    try:
        text, keyboard = alertMessageConstructor(150)
        assert keyboard is not None
        assert keyboard.inline_keyboard[0][0].callback_data == "sticker:kill:all"
        assert "150" in text
    finally:
        _activeStickerTasks.clear()


# ============================================================================
# registerStickerTask / unregisterStickerTask 测试
# ============================================================================

@pytest.mark.asyncio
async def test_task_register_unregister_cycle():
    """注册→取消→注销的完整周期"""
    async def dummy():
        await asyncio.sleep(0.1)

    task = asyncio.create_task(dummy())
    registerStickerTask(task)
    assert len(_activeStickerTasks) == 1

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    unregisterStickerTask(task)
    assert len(_activeStickerTasks) == 0


def test_unregisterStickerTask_not_present():
    """注销不存在的任务不报错"""
    fakeTask = MagicMock()
    unregisterStickerTask(fakeTask)  # 不应抛异常


# ============================================================================
# cancelAllStickerTasks() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_cancelAllStickerTasks_empty():
    """无任务时返回 0，不报错"""
    with patch('utils.memoryMonitor.logSystemEvent', new_callable=AsyncMock):
        count = await cancelAllStickerTasks()
    assert count == 0


@pytest.mark.asyncio
async def test_cancelAllStickerTasks_multiple():
    """取消多个并发任务，全部 cancelled 且列表清空"""
    async def dummy():
        await asyncio.sleep(10)

    tasks = [asyncio.create_task(dummy()) for _ in range(3)]
    for t in tasks:
        registerStickerTask(t)

    with patch('utils.memoryMonitor.logSystemEvent', new_callable=AsyncMock):
        count = await cancelAllStickerTasks()

    assert count == 3
    assert len(_activeStickerTasks) == 0
    assert all(t.cancelled() for t in tasks)