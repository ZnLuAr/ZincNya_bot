"""
utils/core package

核心工具模块，提供可复用的基础设施：
- fileCache: 文件缓存系统
- stateManager: 全局状态管理
- tuiBase: TUI 控制器基类
"""

from .fileCache import CachedFile, getWhitelistCache, getQuotesCache
from .stateManager import StateManager, getStateManager

__all__ = [
    "CachedFile",
    "getWhitelistCache",
    "getQuotesCache",
    "StateManager",
    "getStateManager",
]
