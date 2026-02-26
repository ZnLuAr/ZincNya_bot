"""
utils/core/stateManager.py

线程安全的全局状态管理器，用于集中管理 Bot 的运行时状态。

管理的状态：
- interactiveMode: 是否处于交互模式（CLI 界面接管输入）
- messageQueue: 全局消息队列
- stickerCache: 表情包缓存（带 TTL）
"""

import time
import asyncio
import threading
from typing import Optional, Any
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

        # 贴纸缓存
        stickerSet = state.getCachedSticker("setName")
        state.setCachedSticker("setName", stickerSet)
    """

    def __init__(self, cacheTTL: int = 300):
        """
        参数:
            cacheTTL: 贴纸缓存的 TTL（秒），默认 5 分钟
        """
        self._cacheTTL = cacheTTL

        # 核心状态
        self._interactiveMode: bool = False
        self._messageQueue: Optional[asyncio.Queue] = None

        # 贴纸缓存: {setName: (stickerSet, timestamp)}
        self._stickerCache: dict[str, tuple[Any, float]] = {}

        # 锁
        self._stateLock = threading.RLock()
        self._stickerLock = threading.RLock()


    # ========================================================================
    # 交互模式管理
    # ========================================================================

    def setInteractiveMode(self, value: bool):
        """设置交互模式状态"""
        with self._stateLock:
            self._interactiveMode = value


    def isInteractive(self) -> bool:
        """检查是否处于交互模式"""
        with self._stateLock:
            return self._interactiveMode


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
    # 贴纸缓存管理（线程安全）
    # ========================================================================

    def getCachedSticker(self, setName: str) -> Optional[Any]:
        """
        获取缓存的贴纸集

        参数:
            setName: 贴纸集名称

        返回:
            贴纸集对象，如果不存在或已过期则返回 None
        """
        with self._stickerLock:
            if setName in self._stickerCache:
                data, timestamp = self._stickerCache[setName]
                if time.time() - timestamp < self._cacheTTL:
                    return data
                # 已过期，删除
                del self._stickerCache[setName]
            return None


    def setCachedSticker(self, setName: str, stickerSet: Any):
        """
        缓存贴纸集

        参数:
            setName: 贴纸集名称
            stickerSet: 贴纸集对象
        """
        with self._stickerLock:
            now = time.time()

            # 清理过期条目
            expired = [
                k for k, (_, ts) in self._stickerCache.items()
                if now - ts > self._cacheTTL
            ]
            for k in expired:
                del self._stickerCache[k]

            # 添加新条目
            self._stickerCache[setName] = (stickerSet, now)


    def clearStickerCache(self):
        """清空贴纸缓存"""
        with self._stickerLock:
            self._stickerCache.clear()


    # ========================================================================
    # 统计信息
    # ========================================================================

    def getStats(self) -> dict:
        """
        获取状态管理器的统计信息

        返回:
            {
                "interactiveMode": bool,
                "hasMessageQueue": bool,
                "stickerCacheSize": int
            }
        """
        with self._stateLock:
            with self._stickerLock:
                return {
                    "interactiveMode": self._interactiveMode,
                    "hasMessageQueue": self._messageQueue is not None,
                    "stickerCacheSize": len(self._stickerCache)
                }


# ============================================================================
# 全局单例
# ============================================================================

_stateManager: Optional[StateManager] = None


def getStateManager() -> StateManager:
    """
    获取状态管理器单例

    首次调用时创建实例，后续调用返回同一实例
    """
    global _stateManager

    if _stateManager is None:
        from config import CACHE_TTL
        _stateManager = StateManager(cacheTTL=CACHE_TTL)

    return _stateManager
