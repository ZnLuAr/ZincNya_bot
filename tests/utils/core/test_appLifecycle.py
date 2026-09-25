"""测试资源清理前的停接标记与重启意图，不创建真实Telegram应用。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from utils.core import appLifecycle
from utils.core.stateManager import StateManager




@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("cleanupFailure", [False, True])
async def test_stopAppSignalsBeforeCleanupAndPreservesRestart(monkeypatch, restart, cleanupFailure):
    """普通关闭及清理失败都先设置事件，已请求重启不能被覆盖为关机。"""
    state = StateManager()
    if restart:
        state.requestRestart()
    state.requestShutdown = MagicMock(side_effect=AssertionError("restart flag would be erased"))
    events = []

    async def cleanup():
        """清理入口必须已观察到停接，不依赖后续updater停止。"""
        assert state.getShutdownEvent().is_set()
        assert state.isRestartRequested() is restart
        events.append("cleanup")
        if cleanupFailure:
            raise RuntimeError("synthetic cleanup failure")

    async def stopUpdater():
        """记录updater只在资源清理之后停止。"""
        events.append("updater")

    async def stop():
        """记录应用停止且不依赖上一步成功。"""
        events.append("stop")

    async def shutdown():
        """记录应用最终释放。"""
        events.append("shutdown")

    app = SimpleNamespace(updater=SimpleNamespace(stop=stopUpdater), stop=stop, shutdown=shutdown)
    monkeypatch.setattr(appLifecycle, "getStateManager", lambda: state)
    monkeypatch.setattr(appLifecycle, "cleanupAllResources", cleanup)
    await appLifecycle.stopApp(app)
    assert events == ["cleanup", "updater", "stop", "shutdown"]
    assert state.isRestartRequested() is restart
    state.requestShutdown.assert_not_called()
