"""
tests/utils/afc/test_toolManager.py

测试 AFC 工具状态管理器（toolManager.py）
"""

import os

import pytest

import utils.afc.toolManager as toolManager
from utils.afc.toolManager import (
    loadAfcToolsConfig,
    saveAfcToolsConfig,
    loadBuiltinAfcTools,
    loadCustomAfcTools,
    saveCustomAfcTools,
    isAfcToolEnabled,
    invalidateAfcToolsCache,
    getAllAfcTools,
)


@pytest.fixture
def isolatedState(tmp_path, monkeypatch):
    """把三个状态文件路径重定向到临时目录，并重置模块级缓存。

    这样测试不读写真实 data/ 目录，且每个用例的缓存起点干净。
    """
    configPath = str(tmp_path / "afcTools.json")
    builtinPath = str(tmp_path / "afcToolsBuiltin.json")
    customPath = str(tmp_path / "afcToolsCustom.json")

    monkeypatch.setattr(toolManager, "_AFCTOOLS_CONFIG_PATH", configPath)
    monkeypatch.setattr(toolManager, "_AFCTOOLS_BUILTIN_PATH", builtinPath)
    monkeypatch.setattr(toolManager, "_AFCTOOLS_CUSTOM_PATH", customPath)
    monkeypatch.setattr(toolManager, "_afcToolsConfigCache", None)

    yield {
        "config": configPath,
        "builtin": builtinPath,
        "custom": customPath,
    }

    monkeypatch.setattr(toolManager, "_afcToolsConfigCache", None)


class TestLoadSaveConfig:
    """测试启用状态的加载与保存"""

    def test_load_missing_returns_empty(self, isolatedState):
        """状态文件不存在时返回空 dict"""
        assert loadAfcToolsConfig() == {}

    def test_save_and_load_roundtrip(self, isolatedState):
        """保存后能读回相同内容"""
        data = {"weather": {"enabled": True}, "datetime": {"enabled": False}}
        assert saveAfcToolsConfig(data) is True
        assert loadAfcToolsConfig() == data

    def test_save_writes_valid_json(self, isolatedState):
        """保存的文件是合法 JSON 且落在目标路径"""
        saveAfcToolsConfig({"calc": {"enabled": True}})
        assert os.path.exists(isolatedState["config"])
        assert loadAfcToolsConfig() == {"calc": {"enabled": True}}

    def test_load_corrupted_returns_empty(self, isolatedState):
        """损坏的 JSON 返回空 dict（降级为全部默认启用）"""
        with open(isolatedState["config"], "w", encoding="utf-8") as f:
            f.write("{not valid json")
        assert loadAfcToolsConfig() == {}


class TestIsAfcToolEnabled:
    """测试 isAfcToolEnabled 单一权威门控"""

    def test_default_true_when_missing_entry(self, isolatedState):
        """工具不在状态文件中时默认启用（新工具/误删条目）"""
        assert isAfcToolEnabled("weather") is True

    def test_respects_saved_config(self, isolatedState):
        """读取已保存的禁用状态"""
        saveAfcToolsConfig({"datetime": {"enabled": False}})
        invalidateAfcToolsCache()
        assert isAfcToolEnabled("datetime") is False
        assert isAfcToolEnabled("weather") is True

    def test_cache_hit_avoids_reread(self, isolatedState, monkeypatch):
        """缓存生效：多次调用只读盘一次"""
        saveAfcToolsConfig({"weather": {"enabled": True}})
        invalidateAfcToolsCache()

        loadCount = {"n": 0}
        realLoader = toolManager.loadAfcToolsConfig

        def countingLoader():
            loadCount["n"] += 1
            return realLoader()

        monkeypatch.setattr(toolManager, "loadAfcToolsConfig", countingLoader)

        assert isAfcToolEnabled("weather") is True
        assert isAfcToolEnabled("calc") is True
        assert isAfcToolEnabled("datetime") is True
        assert loadCount["n"] == 1  # 只读盘一次

    def test_save_invalidates_cache(self, isolatedState):
        """写入后缓存失效：save 之后 isAfcToolEnabled 反映新状态"""
        saveAfcToolsConfig({"datetime": {"enabled": True}})
        assert isAfcToolEnabled("datetime") is True

        # 改成禁用；save 内部会清缓存
        saveAfcToolsConfig({"datetime": {"enabled": False}})
        assert isAfcToolEnabled("datetime") is False

    def test_invalidate_forces_reread(self, isolatedState, monkeypatch):
        """手动 invalidateAfcToolsCache 后强制重读"""
        saveAfcToolsConfig({"x": {"enabled": True}})
        assert isAfcToolEnabled("x") is True

        # 直接改磁盘文件，再 invalidate，应读到新值
        import json
        with open(isolatedState["config"], "w", encoding="utf-8") as f:
            json.dump({"x": {"enabled": False}}, f)
        invalidateAfcToolsCache()
        assert isAfcToolEnabled("x") is False


class TestBuiltinAndCustom:
    """测试内置与第三方 manifest 的加载"""

    def test_builtin_missing_returns_empty(self, isolatedState):
        assert loadBuiltinAfcTools() == {}

    def test_custom_roundtrip(self, isolatedState):
        custom = {
            "timezone": {
                "enabled": True,
                "manifest": {"id": "timezone", "files": []},
                "source": "github:https://example.com/tz@main",
                "installedAt": "2026-07-16T00:00:00Z",
            }
        }
        assert saveCustomAfcTools(custom) is True
        assert loadCustomAfcTools() == custom


class TestGetAllAfcTools:
    """测试 getAllAfcTools 合并三个来源"""

    def test_merges_builtin_and_custom(self, isolatedState):
        import json
        with open(isolatedState["builtin"], "w", encoding="utf-8") as f:
            json.dump({"weather": {"id": "weather", "source": "builtin"}}, f)
        saveCustomAfcTools({
            "timezone": {"enabled": True, "manifest": {"id": "timezone"}, "source": "github:x"}
        })

        allTools = getAllAfcTools()
        assert allTools["weather"]["source"] == "builtin"
        assert allTools["weather"]["manifest"]["id"] == "weather"
        assert allTools["timezone"]["source"] == "github:x"
        assert allTools["timezone"]["manifest"]["id"] == "timezone"

    def test_discovers_local_draft(self, isolatedState, tmp_path, monkeypatch):
        """目录存在但既非 builtin 也非 custom → local-draft"""
        toolsDir = tmp_path / "utils" / "afc" / "tools"
        draftDir = toolsDir / "mydraft"
        draftDir.mkdir(parents=True)

        monkeypatch.setattr(toolManager, "_TOOLS_DIR_REL", os.path.join(*["utils", "afc", "tools"]))
        monkeypatch.setattr(toolManager, "PROJECT_ROOT", str(tmp_path))

        allTools = getAllAfcTools()
        assert "mydraft" in allTools
        assert allTools["mydraft"]["source"] == "local-draft"
        assert allTools["mydraft"]["manifest"] is None

    def test_local_draft_skips_underscore_and_dot_dirs(self, isolatedState, tmp_path, monkeypatch):
        """_ 和 . 前缀目录不被当作 local-draft"""
        toolsDir = tmp_path / "utils" / "afc" / "tools"
        (toolsDir / "_private").mkdir(parents=True)
        (toolsDir / ".hidden").mkdir(parents=True)
        (toolsDir / "real").mkdir(parents=True)
        monkeypatch.setattr(toolManager, "PROJECT_ROOT", str(tmp_path))

        allTools = getAllAfcTools()
        assert "_private" not in allTools
        assert ".hidden" not in allTools
        assert allTools["real"]["source"] == "local-draft"

    def test_enabled_state_applied_to_builtin(self, isolatedState):
        """getAllAfcTools 反映 afcTools.json 里的禁用状态"""
        import json
        with open(isolatedState["builtin"], "w", encoding="utf-8") as f:
            json.dump({"datetime": {"id": "datetime", "source": "builtin"}}, f)
        saveAfcToolsConfig({"datetime": {"enabled": False}})

        allTools = getAllAfcTools()
        assert allTools["datetime"]["enabled"] is False

    def test_enabled_state_applied_to_custom(self, isolatedState):
        """Q1 回归：custom 工具的禁用也以 afcTools.json 为准——

        custom json 里的 enabled 只是 install 时的初始值；disable 写 afcTools.json 后，
        list/show 不能再按 custom json 的初始值把工具显示回启用。
        """
        saveCustomAfcTools({
            "timezone": {"enabled": True, "manifest": {"id": "timezone"}, "source": "github:x"}
        })
        saveAfcToolsConfig({"timezone": {"enabled": False}})

        allTools = getAllAfcTools()
        assert allTools["timezone"]["enabled"] is False

    def test_custom_enabled_falls_back_to_custom_json(self, isolatedState):
        """afcTools.json 无条目时，custom 工具的 enabled 退回 custom json 初始值"""
        saveCustomAfcTools({
            "timezone": {"enabled": False, "manifest": {"id": "timezone"}, "source": "github:x"}
        })

        allTools = getAllAfcTools()
        assert allTools["timezone"]["enabled"] is False


class TestScanRespectsEnabled:
    """集成测试：扫描逻辑尊重 isAfcToolEnabled() 的禁用状态。

    注意：afcIntent / registry 的扫描缓存是模块级全局变量，本类用例通过
    显式重置缓存 + monkeypatch 状态文件路径来隔离。chatID 使用独立的
    test_disable_chat，并清理 _lastTriggeredTools，避免与其他用例的 L4
    上下文延续互相污染。

    restoreScanState 会在每个用例结束后用空配置（全部启用）重扫一次，
    把全局扫描缓存恢复到干净状态，避免污染同进程的其他测试文件。
    """

    @pytest.fixture(autouse=True)
    def restoreScanState(self, monkeypatch, tmp_path):
        """用例结束后恢复干净的扫描缓存（全部启用）。

        恢复扫描时用不存在的配置路径，让 isAfcToolEnabled 默认全部启用——
        避免开发机真实 data/afcTools.json 里的本地禁用状态污染后续测试文件。
        """
        yield
        import utils.afc.afcIntent as afcIntent
        import utils.afc.registry as registry

        monkeypatch.setattr(
            toolManager, "_AFCTOOLS_CONFIG_PATH", str(tmp_path / "nonexistent.json")
        )
        afcIntent._indexBuilt = False
        afcIntent._keywordIndex.clear()
        afcIntent._patternIndex.clear()
        afcIntent._allToolNames.clear()
        afcIntent._lastTriggeredTools.clear()
        invalidateAfcToolsCache()
        afcIntent._scanTriggers()

        registry._schemaBuilt = False
        registry._toolsSchema.clear()
        registry._toolsCallable.clear()
        registry._scanTools()

    def _resetScanCaches(self):
        """清空 afcIntent / registry 的扫描缓存，强制重扫。"""
        import utils.afc.afcIntent as afcIntent
        import utils.afc.registry as registry

        afcIntent._indexBuilt = False
        afcIntent._keywordIndex.clear()
        afcIntent._patternIndex.clear()
        afcIntent._allToolNames.clear()
        afcIntent._lastTriggeredTools.clear()

        registry._schemaBuilt = False
        registry._toolsSchema.clear()
        registry._toolsCallable.clear()

    def test_disabled_tool_not_scanned(self, isolatedState):
        """禁用 datetime 后，重扫不应注册其触发词 / schema"""
        import utils.afc.afcIntent as afcIntent
        import utils.afc.registry as registry

        saveAfcToolsConfig({"datetime": {"enabled": False}})
        self._resetScanCaches()

        afcIntent._scanTriggers()
        registry._scanTools()

        assert "datetime" not in afcIntent._allToolNames
        assert "datetime" not in registry._toolsSchema
        # datetime 的 L1 关键词 "今天几号" 未注册 → detectTools 返回空
        assert afcIntent.detectTools("今天几号", "test_disable_chat") == set()

    def test_enabled_tool_scanned(self, isolatedState):
        """默认启用时（无条目），datetime 正常注册"""
        import utils.afc.afcIntent as afcIntent
        import utils.afc.registry as registry

        # 空配置 = 默认全部启用
        saveAfcToolsConfig({})
        self._resetScanCaches()

        afcIntent._scanTriggers()
        registry._scanTools()

        assert "datetime" in afcIntent._allToolNames
        assert "datetime" in registry._toolsSchema

    def test_other_tools_unaffected_when_one_disabled(self, isolatedState):
        """禁用 datetime 不影响 weather / calc"""
        import utils.afc.afcIntent as afcIntent

        saveAfcToolsConfig({"datetime": {"enabled": False}})
        self._resetScanCaches()
        afcIntent._scanTriggers()

        assert "weather" in afcIntent._allToolNames
        assert "calc" in afcIntent._allToolNames
        assert "datetime" not in afcIntent._allToolNames