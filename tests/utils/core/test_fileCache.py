"""
tests/utils/core/test_fileCache.py

CachedFile（utils/core/fileCache.py）缓存行为：TTL 过期 / 文件外部修改失效 /
深拷贝隔离 / set 后命中 / invalidate。三个命名缓存（whitelist/quotes/operators）
只测单例语义，不测数据内容。
"""

import json
import time

import pytest

from utils.core.fileCache import CachedFile



def _makeCache(tmp_path, ttl=300):
    p = tmp_path / "data.json"
    p.write_text(json.dumps({"v": 1}), encoding="utf-8")
    cache = CachedFile(
        str(p),
        loader=lambda fp: json.load(open(fp, encoding="utf-8")),
        saver=lambda fp, d: json.dump(d, open(fp, "w", encoding="utf-8")),
        ttl=ttl,
    )
    return cache, p



class TestBasics:
    def test_first_get_loads(self, tmp_path):
        cache, _ = _makeCache(tmp_path)
        assert cache.get() == {"v": 1}
        assert cache.misses == 1

    def test_second_get_hits(self, tmp_path):
        cache, _ = _makeCache(tmp_path)
        cache.get()
        assert cache.get() == {"v": 1}
        assert cache.hits == 1 and cache.misses == 1

    def test_missing_file_loader_raises_through(self, tmp_path):
        """文件不存在时 loader 自己抛 FileNotFoundError——缓存不吞"""
        cache = CachedFile(str(tmp_path / "nope.json"), loader=lambda fp: (_ for _ in ()).throw(FileNotFoundError(fp)), saver=lambda fp, d: None)
        with pytest.raises(FileNotFoundError):
            cache.get()



class TestTTLExpiry:
    def test_ttl_expiry_reloads(self, tmp_path):
        cache, p = _makeCache(tmp_path, ttl=0.05)
        assert cache.get() == {"v": 1}
        p.write_text(json.dumps({"v": 2}), encoding="utf-8")
        time.sleep(0.08)
        assert cache.get() == {"v": 2}
        assert cache.misses == 2

    def test_within_ttl_stale(self, tmp_path):
        """TTL 内文件变了也读旧值（mtime 检测另测）——这里锁 TTL 优先语义"""
        cache, p = _makeCache(tmp_path, ttl=60)
        cache.get()
        # mtime 检测是另一个失效条件（见 TestMtime）；本用例保证 TTL 内且 mtime 未变时命中
        assert cache.get() == {"v": 1}
        assert cache.hits == 1



class TestMtimeInvalidation:
    def test_external_modification_reloads(self, tmp_path):
        """文件被外部修改（mtime 变新）→ 即使 TTL 未到也重载"""
        cache, p = _makeCache(tmp_path, ttl=60)
        assert cache.get() == {"v": 1}
        p.write_text(json.dumps({"v": 99}), encoding="utf-8")
        assert cache.get() == {"v": 99}



class TestDeepCopyIsolation:
    def test_get_returns_copy(self, tmp_path):
        cache, _ = _makeCache(tmp_path)
        d = cache.get()
        d["v"] = 42
        assert cache.get()["v"] == 1    # 缓存本体未被污染

    def test_set_copies_input(self, tmp_path):
        cache, _ = _makeCache(tmp_path)
        src = {"v": 7}
        cache.set(src)
        src["v"] = 99
        assert cache.get()["v"] == 7



class TestSet:
    def test_set_updates_cache_and_file(self, tmp_path):
        cache, p = _makeCache(tmp_path)
        cache.set({"v": 5})
        assert cache.get() == {"v": 5}          # 缓存
        assert json.loads(p.read_text(encoding="utf-8"))["v"] == 5   # 落盘

    def test_set_then_immediate_get_hits(self, tmp_path):
        cache, _ = _makeCache(tmp_path)
        cache.set({"v": 3})
        assert cache.hits == 0
        cache.get()
        assert cache.hits == 1 and cache.misses == 0    # set 不算 miss



class TestInvalidate:
    def test_invalidate_forces_reload(self, tmp_path):
        cache, p = _makeCache(tmp_path)
        cache.get()
        p.write_text(json.dumps({"v": 8}), encoding="utf-8")
        cache.invalidate()
        assert cache.get() == {"v": 8}
        assert cache.misses == 2



class TestNamedCaches:
    def test_operators_cache_singleton(self):
        from utils.core.fileCache import getOperatorsCache
        assert getOperatorsCache() is getOperatorsCache()

    def test_stats_shape(self, tmp_path):
        cache, _ = _makeCache(tmp_path)
        cache.get()
        stats = cache.getStats()
        assert set(stats.keys()) >= {"hits", "misses", "hitRate", "cacheAge"}
