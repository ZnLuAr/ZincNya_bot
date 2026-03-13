"""
utils/core/fileCache.py

线程安全的文件缓存系统，用于减少重复的磁盘 I/O 操作。

特性：
- TTL（生存时间）过期机制
- 文件修改时间跟踪（自动检测外部修改）
- 线程安全的读写操作
- 延迟加载（首次访问时才加载）
- 统计数据跟踪（命中率监控）
- 深拷贝保护（防止调用者修改缓存）
"""



import os
import json
import copy
import time
import threading
from pathlib import Path
from typing import Optional, Callable, TypeVar, Generic

from config import DEFAULT_FILE_CACHE_TTL


T = TypeVar('T')



class CachedFile(Generic[T]):
    """
    通用文件缓存类

    使用示例：
        def loadJson(path: str) -> dict:
            with open(path) as f:
                return json.load(f)

        def saveJson(path: str, data: dict):
            with open(path, 'w') as f:
                json.dump(data, f)

        cache = CachedFile("data.json", loadJson, saveJson, ttl=300)
        data = cache.get()  # 从缓存读取
        cache.set(newData)  # 更新缓存并保存
    """

    def __init__(
        self,
        filePath: str,
        loader: Callable[[str], T],
        saver: Callable[[str, T], None],
        ttl: int = DEFAULT_FILE_CACHE_TTL
    ):
        """
        参数:
            filePath: 文件路径
            loader: 加载函数 (filePath) -> data
            saver: 保存函数 (filePath, data) -> None
            ttl: 缓存生存时间（秒），默认 8 分钟
        """
        self.filePath = Path(filePath)
        self.loader = loader
        self.saver = saver
        self.ttl = ttl

        self._cache: Optional[T] = None
        self._cachedAt: float = 0
        self._fileMtime: float = 0
        self._lock = threading.RLock()

        # 统计数据
        self.hits = 0
        self.misses = 0


    def get(self) -> T:
        """
        获取缓存数据，必要时重新加载

        缓存失效条件：
        1. 缓存为空（首次访问）
        2. TTL 过期
        3. 文件被外部修改

        返回深拷贝，防止调用者修改缓存
        """
        with self._lock:
            now = time.time()

            # 检查文件是否被外部修改
            try:
                currentMtime = self.filePath.stat().st_mtime
            except FileNotFoundError:
                currentMtime = 0

            # 判断是否需要重新加载
            shouldReload = (
                self._cache is None or
                (now - self._cachedAt) > self.ttl or
                currentMtime > self._fileMtime
            )

            if shouldReload:
                self._cache = self.loader(str(self.filePath))
                self._cachedAt = now
                self._fileMtime = currentMtime
                self.misses += 1
            else:
                self.hits += 1

            # 返回深拷贝，防止调用者修改缓存
            return copy.deepcopy(self._cache)


    def set(self, data: T):
        """
        更新缓存并保存到文件

        传入数据会被深拷贝，防止调用者后续修改影响缓存
        """
        with self._lock:
            self.saver(str(self.filePath), data)
            self._cache = copy.deepcopy(data)
            self._cachedAt = time.time()

            # 更新文件修改时间
            try:
                self._fileMtime = self.filePath.stat().st_mtime
            except FileNotFoundError:
                self._fileMtime = 0


    def invalidate(self):
        """强制失效缓存（下次 get() 会重新加载）"""
        with self._lock:
            self._cache = None
            self._cachedAt = 0


    def getStats(self) -> dict:
        """
        获取缓存统计信息

        返回:
            {
                "hits": int,        # 命中次数
                "misses": int,      # 未命中次数
                "hitRate": str,     # 命中率（百分比）
                "cacheAge": float   # 缓存年龄（秒）
            }
        """
        with self._lock:
            total = self.hits + self.misses
            hitRate = (self.hits / total * 100) if total > 0 else 0
            cacheAge = time.time() - self._cachedAt if self._cachedAt > 0 else 0

            return {
                "hits": self.hits,
                "misses": self.misses,
                "hitRate": f"{hitRate:.1f}%",
                "cacheAge": cacheAge
            }




# ============================================================================
# JSON 文件加载/保存辅助函数
# ============================================================================

def _loadJson(filePath: str) -> dict | list:
    """通用 JSON 加载函数（EAFP 模式，避免 TOCTOU）"""
    # 确保父目录存在
    parentDir = os.path.dirname(filePath)
    if parentDir:
        os.makedirs(parentDir, exist_ok=True)

    # 直接尝试打开，文件不存在时返回空结构
    try:
        with open(filePath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _saveJson(filePath: str, data: dict | list):
    """通用 JSON 保存函数（原子写入，防止中途崩溃导致文件损坏）"""
    # 确保父目录存在
    parentDir = os.path.dirname(filePath)
    if parentDir:
        os.makedirs(parentDir, exist_ok=True)

    # 先写入临时文件，再原子替换
    tmpPath = filePath + ".tmp"
    with open(tmpPath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmpPath, filePath)




# ============================================================================
# 全局缓存实例（单例模式）
# ============================================================================

_whitelistCache: Optional[CachedFile] = None
_quotesCache: Optional[CachedFile] = None
_operatorsCache: Optional[CachedFile] = None
_cacheLock = threading.Lock()


def getWhitelistCache() -> CachedFile:
    """
    获取白名单缓存单例（线程安全）

    缓存 data/whitelist.json，TTL 5 分钟
    """
    global _whitelistCache

    if _whitelistCache is None:
        with _cacheLock:
            if _whitelistCache is None:
                from config import WHITELIST_PATH

                _whitelistCache = CachedFile(
                    filePath=WHITELIST_PATH,
                    loader=_loadJson,
                    saver=_saveJson,
                    ttl=300   # 5 分钟
                )

    return _whitelistCache


def getQuotesCache() -> CachedFile:
    """
    获取语录缓存单例（线程安全）

    缓存 data/ZincNyaQuotes.json，TTL 8 分钟
    """
    global _quotesCache

    if _quotesCache is None:
        with _cacheLock:
            if _quotesCache is None:
                from config import QUOTES_PATH

                _quotesCache = CachedFile(
                    filePath=QUOTES_PATH,
                    loader=_loadJson,
                    saver=_saveJson,
            ttl=DEFAULT_FILE_CACHE_TTL  # 8 分钟
        )

    return _quotesCache


def getOperatorsCache() -> CachedFile:
    """
    获取 operators 缓存单例（线程安全）

    缓存 data/operators.json，TTL 5 分钟
    """
    global _operatorsCache

    if _operatorsCache is None:
        with _cacheLock:
            if _operatorsCache is None:
                from config import OPERATORS_PATH

                _operatorsCache = CachedFile(
                    filePath=OPERATORS_PATH,
                    loader=_loadJson,
                    saver=_saveJson,
                    ttl=300   # 5 分钟
                )

    return _operatorsCache


def getAllCacheStats() -> dict:
    """
    获取所有缓存的统计信息

    返回:
        {
            "whitelist": {...},
            "quotes": {...}
        }
    """
    stats = {}

    if _whitelistCache is not None:
        stats["whitelist"] = _whitelistCache.getStats()

    if _quotesCache is not None:
        stats["quotes"] = _quotesCache.getStats()

    if _operatorsCache is not None:
        stats["operators"] = _operatorsCache.getStats()

    return stats
