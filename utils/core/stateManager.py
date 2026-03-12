"""
utils/core/stateManager.py

线程安全的全局状态管理器，用于集中管理 Bot 的运行时状态。

管理的状态：
- interactiveMode: 是否处于交互模式（CLI 界面接管输入）
- messageQueue: 全局消息队列
- shutdownEvent / restartRequested: 远程关机/重启信号
"""

import asyncio
import threading
from typing import Optional
from contextlib import contextmanager




class StateManager:
    """
    Bot 全局状态的线程安全管理器

    使用示例：
        state = getStateManager()

        # 交互模式
        with state.interactiveContext():
            # 在此期间 interactiveMode = True
            await doSomething()
    """

    def __init__(self):
        # 核心状态
        self._interactiveMode: bool = False
        self._messageQueue: Optional[asyncio.Queue] = None

        # 远程关机/重启信号
        self._shutdownEvent: asyncio.Event = asyncio.Event()
        self._restartRequested: bool = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # 交互模式 Event（非交互时 set，交互时 clear）
        self._nonInteractiveEvent: asyncio.Event = asyncio.Event()
        self._nonInteractiveEvent.set()  # 初始状态：非交互模式

        # 锁
        self._stateLock = threading.RLock()


    def setEventLoop(self, loop: asyncio.AbstractEventLoop):
        """注册事件循环引用（启动时调用一次）"""
        self._loop = loop


    # ========================================================================
    # 交互模式管理
    # ========================================================================

    def setInteractiveMode(self, value: bool):
        """设置交互模式状态"""
        with self._stateLock:
            self._interactiveMode = value
            if value:
                # 进入交互模式 → 阻塞 waitForNonInteractive
                if self._loop and self._loop.is_running():
                    self._loop.call_soon_threadsafe(self._nonInteractiveEvent.clear)
                else:
                    self._nonInteractiveEvent.clear()
            else:
                # 退出交互模式 → 解除阻塞
                if self._loop and self._loop.is_running():
                    self._loop.call_soon_threadsafe(self._nonInteractiveEvent.set)
                else:
                    self._nonInteractiveEvent.set()


    def isInteractive(self) -> bool:
        """检查是否处于交互模式"""
        with self._stateLock:
            return self._interactiveMode


    async def waitForNonInteractive(self):
        """阻塞直到退出交互模式（零 CPU 等待）"""
        await self._nonInteractiveEvent.wait()


    @contextmanager
    def interactiveContext(self):
        """
        交互模式的上下文管理器

        使用示例：
            with state.interactiveContext():
                # 在此期间 interactiveMode = True
                await runInteractiveUI()
            # 退出后自动恢复为 False
        """
        self.setInteractiveMode(True)
        try:
            yield
        finally:
            self.setInteractiveMode(False)


    # ========================================================================
    # 远程关机/重启管理
    # ========================================================================

    def _setShutdownEvent(self):
        """线程安全地设置关机事件"""
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._shutdownEvent.set)
        else:
            self._shutdownEvent.set()


    def requestShutdown(self):
        """请求关机（可从任意线程调用）"""
        with self._stateLock:
            self._restartRequested = False
            self._setShutdownEvent()


    def requestRestart(self):
        """请求重启（可从任意线程调用）"""
        with self._stateLock:
            self._restartRequested = True
            self._setShutdownEvent()


    def getShutdownEvent(self) -> asyncio.Event:
        """获取关机事件（由 bot.py 监听）"""
        return self._shutdownEvent


    def isRestartRequested(self) -> bool:
        """检查是否请求重启"""
        with self._stateLock:
            return self._restartRequested


    # ========================================================================
    # 消息队列管理
    # ========================================================================

    def setMessageQueue(self, queue: asyncio.Queue):
        """设置全局消息队列"""
        with self._stateLock:
            self._messageQueue = queue


    def getMessageQueue(self) -> Optional[asyncio.Queue]:
        """获取全局消息队列"""
        with self._stateLock:
            return self._messageQueue


    # ========================================================================
    # 统计信息
    # ========================================================================

    def getStats(self) -> dict:
        """获取状态管理器的统计信息"""
        with self._stateLock:
            return {
                "interactiveMode": self._interactiveMode,
                "hasMessageQueue": self._messageQueue is not None,
            }




# ============================================================================
# 全局单例
# ============================================================================

_stateManager: Optional[StateManager] = None
_stateManagerLock = threading.Lock()


def getStateManager() -> StateManager:
    """
    获取状态管理器单例

    首次调用时创建实例，后续调用返回同一实例。
    使用双重检查锁定确保线程安全。
    """
    global _stateManager

    if _stateManager is None:
        with _stateManagerLock:
            if _stateManager is None:
                _stateManager = StateManager()

    return _stateManager
