"""
tests/utils/afc/test_toolManager.py

测试 AFC 工具状态管理器（toolManager.py）
"""

import os
import json

import pytest

import utils.afc.registry as registry
import utils.afc.toolManager as toolManager
from utils.afc.toolManager import (
    loadAfcToolsConfig,
    saveAfcToolsConfig,
    loadBuiltinAfcTools,
    saveBuiltinAfcTools,
    loadCustomAfcTools,
    saveCustomAfcTools,
    isAfcToolEnabled,
    invalidateAfcToolsCache,
    getAllAfcTools,
    _manifestDefaultEnabled,
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
    monkeypatch.setattr(toolManager, "_builtinDefaultsCache", None)

    yield {
        "config": configPath,
        "builtin": builtinPath,
        "custom": customPath,
    }

    monkeypatch.setattr(toolManager, "_afcToolsConfigCache", None)
    monkeypatch.setattr(toolManager, "_builtinDefaultsCache", None)


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
        with open(isolatedState["config"], "w", encoding="utf-8") as f:
            json.dump({"x": {"enabled": False}}, f)
        invalidateAfcToolsCache()
        assert isAfcToolEnabled("x") is False


class TestManifestDefaultEnabled:
    """测试 _manifestDefaultEnabled：内置工具 manifest 声明的默认启用状态"""

    def test_missing_key_defaults_true(self):
        assert _manifestDefaultEnabled({"id": "weather"}) is True

    def test_explicit_true(self):
        assert _manifestDefaultEnabled({"defaultEnabled": True}) is True

    def test_explicit_false(self):
        assert _manifestDefaultEnabled({"defaultEnabled": False}) is False

    @pytest.mark.parametrize("value", [None, 0, "", "false", "no", [], {}])
    def test_only_literal_false_disables(self, value):
        """只有 JSON 的 false 才算声明停用；其他写法（含 0、null、"false"）都按默认启用"""
        assert _manifestDefaultEnabled({"defaultEnabled": value}) is True

    @pytest.mark.parametrize("manifest", [None, "weather", ["weather"], 1, True])
    def test_non_dict_manifest_defaults_true(self, manifest):
        assert _manifestDefaultEnabled(manifest) is True


class TestDefaultEnabled:
    """测试 isAfcToolEnabled 对内置 manifest defaultEnabled 的处理"""

    def _writeBuiltin(self, isolatedState, manifests: dict):
        with open(isolatedState["builtin"], "w", encoding="utf-8") as f:
            json.dump(manifests, f)

    def test_manifest_false_disables_by_default(self, isolatedState):
        """manifest 声明 false、又没写过开关：默认停用"""
        self._writeBuiltin(isolatedState, {"web": {"id": "web", "defaultEnabled": False}})
        assert isAfcToolEnabled("web") is False

    def test_switch_overrides_manifest_false(self, isolatedState):
        """用户显式启用后生效"""
        self._writeBuiltin(isolatedState, {"web": {"id": "web", "defaultEnabled": False}})
        saveAfcToolsConfig({"web": {"enabled": True}})
        assert isAfcToolEnabled("web") is True

    def test_switch_entry_without_enabled_falls_back_to_manifest(self, isolatedState):
        """开关条目里没有 enabled 字段时仍取 manifest 的默认值"""
        self._writeBuiltin(isolatedState, {"web": {"id": "web", "defaultEnabled": False}})
        saveAfcToolsConfig({"web": {}})
        assert isAfcToolEnabled("web") is False

    def test_manifest_without_key_enabled_by_default(self, isolatedState):
        """manifest 没写 defaultEnabled：默认启用"""
        self._writeBuiltin(isolatedState, {"calc": {"id": "calc"}})
        assert isAfcToolEnabled("calc") is True

    def test_manifest_true_enabled_by_default(self, isolatedState):
        self._writeBuiltin(isolatedState, {"calc": {"id": "calc", "defaultEnabled": True}})
        assert isAfcToolEnabled("calc") is True

    def test_switch_overrides_manifest_true(self, isolatedState):
        self._writeBuiltin(isolatedState, {"calc": {"id": "calc", "defaultEnabled": True}})
        saveAfcToolsConfig({"calc": {"enabled": False}})
        assert isAfcToolEnabled("calc") is False

    @pytest.mark.parametrize("value", [None, 0, "false"])
    def test_invalid_value_enabled_by_default(self, isolatedState, value):
        """defaultEnabled 不是 JSON 的 false 时按默认启用"""
        self._writeBuiltin(isolatedState, {"web": {"id": "web", "defaultEnabled": value}})
        assert isAfcToolEnabled("web") is True

    def test_non_dict_manifest_enabled_by_default(self, isolatedState):
        self._writeBuiltin(isolatedState, {"web": "不是对象"})
        assert isAfcToolEnabled("web") is True

    def test_tool_outside_builtin_enabled_by_default(self, isolatedState):
        """不在内置 manifest 里的工具（第三方 / 本地草稿）默认启用"""
        self._writeBuiltin(isolatedState, {"web": {"id": "web", "defaultEnabled": False}})
        assert isAfcToolEnabled("timezone") is True

    def test_corrupted_builtin_file_enables_all(self, isolatedState):
        """内置 manifest 文件损坏时没有默认值可取，全部默认启用"""
        with open(isolatedState["builtin"], "w", encoding="utf-8") as f:
            f.write("{not valid json")
        assert isAfcToolEnabled("web") is True

    def test_builtin_defaults_read_once(self, isolatedState, monkeypatch):
        """内置默认值有缓存：多次查询只读一次 manifest"""
        self._writeBuiltin(isolatedState, {"web": {"id": "web", "defaultEnabled": False}})

        loadCount = {"n": 0}
        realLoader = toolManager.loadBuiltinAfcTools

        def countingLoader():
            loadCount["n"] += 1
            return realLoader()

        monkeypatch.setattr(toolManager, "loadBuiltinAfcTools", countingLoader)

        assert isAfcToolEnabled("web") is False
        assert isAfcToolEnabled("calc") is True
        assert isAfcToolEnabled("web") is False
        assert loadCount["n"] == 1

    def test_invalidate_rereads_builtin_defaults(self, isolatedState):
        """manifest 改动后要 invalidate 才生效"""
        self._writeBuiltin(isolatedState, {"web": {"id": "web", "defaultEnabled": False}})
        assert isAfcToolEnabled("web") is False

        self._writeBuiltin(isolatedState, {"web": {"id": "web"}})
        assert isAfcToolEnabled("web") is False  # 仍是缓存里的旧默认值

        invalidateAfcToolsCache()
        assert isAfcToolEnabled("web") is True

    def test_save_switches_rereads_builtin_defaults(self, isolatedState):
        """保存开关会连同内置默认值的缓存一起清掉"""
        self._writeBuiltin(isolatedState, {"web": {"id": "web", "defaultEnabled": False}})
        assert isAfcToolEnabled("web") is False

        self._writeBuiltin(isolatedState, {"web": {"id": "web"}})
        saveAfcToolsConfig({})
        assert isAfcToolEnabled("web") is True

    def test_repo_manifest_disables_only_web(self, tmp_path, monkeypatch):
        """仓库里的 data/afcToolsBuiltin.json：web 默认停用，其余内置工具默认启用"""
        monkeypatch.setattr(toolManager, "_AFCTOOLS_CONFIG_PATH", str(tmp_path / "nonexistent.json"))
        monkeypatch.setattr(toolManager, "_afcToolsConfigCache", None)
        monkeypatch.setattr(toolManager, "_builtinDefaultsCache", None)

        builtin = loadBuiltinAfcTools()
        assert {"weather", "calc", "datetime", "web"} <= set(builtin)

        assert isAfcToolEnabled("web") is False
        for toolName in set(builtin) - {"web"}:
            assert isAfcToolEnabled(toolName) is True


class TestBuiltinAndCustom:
    """测试内置与第三方 manifest 的加载"""

    def test_builtin_missing_returns_empty(self, isolatedState):
        assert loadBuiltinAfcTools() == {}

    def test_builtin_roundtrip(self, isolatedState):
        builtin = {"web": {"id": "web", "source": "builtin", "defaultEnabled": False}}
        assert saveBuiltinAfcTools(builtin) is True
        assert loadBuiltinAfcTools() == builtin

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

    def test_local_draft_respects_switch(self, isolatedState, tmp_path, monkeypatch):
        """local-draft 的 enabled 以 afcTools.json 为准"""
        (tmp_path / "utils" / "afc" / "tools" / "mydraft").mkdir(parents=True)
        monkeypatch.setattr(toolManager, "PROJECT_ROOT", str(tmp_path))
        saveAfcToolsConfig({"mydraft": {"enabled": False}})

        allTools = getAllAfcTools()
        assert allTools["mydraft"]["enabled"] is False

    def test_custom_overrides_same_name_builtin(self, isolatedState):
        """同名时 custom 条目盖过 builtin"""
        saveBuiltinAfcTools({"weather": {"id": "weather", "source": "builtin"}})
        saveCustomAfcTools({
            "weather": {"enabled": True, "manifest": {"id": "weather", "version": "9.9.9"}, "source": "github:x"}
        })

        allTools = getAllAfcTools()
        assert allTools["weather"]["source"] == "github:x"
        assert allTools["weather"]["manifest"]["version"] == "9.9.9"

    def test_builtin_default_disabled(self, isolatedState):
        """manifest 声明 defaultEnabled false、没写过开关：展示为停用"""
        saveBuiltinAfcTools({
            "web": {"id": "web", "source": "builtin", "defaultEnabled": False},
            "calc": {"id": "calc", "source": "builtin"},
        })

        allTools = getAllAfcTools()
        assert allTools["web"]["enabled"] is False
        assert allTools["calc"]["enabled"] is True

    def test_builtin_default_disabled_overridden_by_switch(self, isolatedState):
        """显式启用后展示为启用"""
        saveBuiltinAfcTools({"web": {"id": "web", "source": "builtin", "defaultEnabled": False}})
        saveAfcToolsConfig({"web": {"enabled": True}})

        allTools = getAllAfcTools()
        assert allTools["web"]["enabled"] is True

    @pytest.mark.parametrize("value", [None, 0, "false"])
    def test_builtin_invalid_default_shown_enabled(self, isolatedState, value):
        """defaultEnabled 不是 JSON 的 false 时展示为启用"""
        saveBuiltinAfcTools({"web": {"id": "web", "source": "builtin", "defaultEnabled": value}})

        allTools = getAllAfcTools()
        assert allTools["web"]["enabled"] is True

    @pytest.mark.parametrize("manifest", [
        {"id": "web"},
        {"id": "web", "defaultEnabled": True},
        {"id": "web", "defaultEnabled": False},
        {"id": "web", "defaultEnabled": "false"},
    ])
    @pytest.mark.parametrize("switches", [
        {},
        {"web": {"enabled": True}},
        {"web": {"enabled": False}},
    ])
    def test_builtin_enabled_matches_isAfcToolEnabled(self, isolatedState, manifest, switches):
        """展示用的 enabled 和实际门控 isAfcToolEnabled 对内置工具给出同一个结果"""
        saveBuiltinAfcTools({"web": manifest})
        saveAfcToolsConfig(switches)

        assert getAllAfcTools()["web"]["enabled"] is isAfcToolEnabled("web")


class TestRegistryRespectsEnabled:
    """集成测试：registry 交出的 schema 跟随 isAfcToolEnabled() 的启用状态。

    registry 的扫描结果覆盖全部已安装工具且只扫一次，启用状态在取 schema 时
    才查，所以这里只改状态文件，不重置扫描缓存。isolatedState 的内置 manifest
    路径指向不存在的文件，没写过开关的工具一律默认启用。
    """

    def test_disabled_tool_not_in_enabled_schema(self, isolatedState):
        """禁用 datetime 后，取到的 schema 里没有它，但它仍是已安装工具"""
        saveAfcToolsConfig({"datetime": {"enabled": False}})

        enabledNames = {toolSchema["tool"] for toolSchema in registry.getEnabledToolsSchema()}
        assert "datetime" not in enabledNames
        assert "datetime" in registry.getAllToolNames()

    def test_enabled_tool_in_schema(self, isolatedState):
        """默认启用时（无条目），datetime 在 schema 里"""
        # 空配置 = 默认全部启用
        saveAfcToolsConfig({})

        enabledNames = {toolSchema["tool"] for toolSchema in registry.getEnabledToolsSchema()}
        assert "datetime" in enabledNames

    def test_other_tools_unaffected_when_one_disabled(self, isolatedState):
        """禁用 datetime 不影响 weather / calc"""
        saveAfcToolsConfig({"datetime": {"enabled": False}})

        enabledNames = {toolSchema["tool"] for toolSchema in registry.getEnabledToolsSchema()}
        assert "weather" in enabledNames
        assert "calc" in enabledNames
        assert "datetime" not in enabledNames

    def test_reenable_takes_effect_after_save(self, isolatedState):
        """重新启用后保存即生效"""
        saveAfcToolsConfig({"datetime": {"enabled": False}})
        assert "datetime" not in {t["tool"] for t in registry.getEnabledToolsSchema()}

        saveAfcToolsConfig({"datetime": {"enabled": True}})
        assert "datetime" in {t["tool"] for t in registry.getEnabledToolsSchema()}

    def test_manifest_default_disabled_tool_needs_switch(self, isolatedState):
        """manifest 默认停用的工具要显式启用才进 schema"""
        saveBuiltinAfcTools({"web": {"id": "web", "defaultEnabled": False}})
        invalidateAfcToolsCache()
        assert "web" not in {t["tool"] for t in registry.getEnabledToolsSchema()}

        saveAfcToolsConfig({"web": {"enabled": True}})
        assert "web" in {t["tool"] for t in registry.getEnabledToolsSchema()}