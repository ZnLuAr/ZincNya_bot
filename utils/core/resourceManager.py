"""
utils/core/resourceManager.py

全局资源管理器，统一管理需要在程序退出时清理的资源。

设计原则：
    - 单例模式，全局唯一实例
    - 支持优先级（高优先级资源先清理）
    - 支持异步清理函数
    - 失败不中断其他资源清理

使用方式：
    # 注册资源
    getResourceManager().register("aiohttp Session", closeSession, priority=10)

    # 程序退出时清理
    await cleanupAllResources()

优先级参考：
    0  - 默认优先级（一般资源）
    10 - 网络连接（aiohttp Session 等）
    20 - 数据库连接
    30 - 文件句柄
"""



import threading
from typing import Callable, Awaitable




class ResourceManager:
    """全局资源管理器"""

    def __init__(self):
        self._resources: list[tuple[int, str, Callable[[], Awaitable[None]]]] = []


    def register(
        self,
        name: str,
        cleanupFunc: Callable[[], Awaitable[None]],
        priority: int = 0
    ):
        """
        注册需要清理的资源

        参数:
            name: 资源名称（用于日志）
            cleanupFunc: 异步清理函数
            priority: 清理优先级（数值越大越先清理）
        """
        self._resources.append((priority, name, cleanupFunc))


    async def cleanupAll(self):
        """
        清理所有已注册的资源

        按优先级从高到低清理，单个资源清理失败不影响其他资源。
        """
        # 按优先级降序排列
        sorted_resources = sorted(self._resources, key=lambda r: r[0], reverse=True)

        for priority, name, cleanupFunc in sorted_resources:
            try:
                await cleanupFunc()
            except Exception as e:
                # 静默失败，不中断其他资源清理
                print(f"清理 {name} 时出错: {e}")


    def getRegisteredResources(self) -> list[str]:
        """获取所有已注册资源的名称列表（调试用）"""
        return [name for _, name, _ in self._resources]




# 全局单例
_resourceManager: ResourceManager | None = None
_resourceManagerLock = threading.Lock()


def getResourceManager() -> ResourceManager:
    """获取全局资源管理器单例（线程安全）"""
    global _resourceManager
    if _resourceManager is None:
        with _resourceManagerLock:
            if _resourceManager is None:
                _resourceManager = ResourceManager()
    return _resourceManager




async def cleanupAllResources():
    """清理所有已注册的资源（程序退出时调用）"""
    manager = getResourceManager()
    await manager.cleanupAll()
