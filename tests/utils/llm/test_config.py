"""
tests/utils/llm/test_config.py

测试 utils/llm/config.py
"""

import pytest
import json
from unittest.mock import patch, mock_open
from utils.llm.config import (
    loadLLMConfig,
    saveLLMConfig,
    _boundedInt,
    _boundedFloat,
    _normalizeBlockedHost,
    _normalizeGroupTriggerKeyword,
    setAutoMode,
    setGroupTriggerKeywords,
    addGroupTriggerKeyword,
    removeGroupTriggerKeyword,
    setURLReadBlockedHosts,
    addURLReadBlockedHost,
    removeURLReadBlockedHost,
    setKnowledgeMaxResults,
    setKnowledgeMinScore,
)


# ============================================================================
# loadLLMConfig() 测试
# ============================================================================

def test_load_llm_config_file_exists(tmp_path):
    """配置文件存在时加载"""
    config_file = tmp_path / "llmConfig.json"
    config_data = {"enabled": True, "model": "test-model"}
    config_file.write_text(json.dumps(config_data), encoding="utf-8")

    with patch("utils.llm.config.LLM_CONFIG_PATH", str(config_file)):
        result = loadLLMConfig()
        assert result["enabled"] is True
        assert result["model"] == "test-model"


def test_load_llm_config_file_not_exists(tmp_path):
    """配置文件不存在时返回默认值"""
    config_file = tmp_path / "nonexistent.json"

    with patch("utils.llm.config.LLM_CONFIG_PATH", str(config_file)):
        result = loadLLMConfig()
        assert result["enabled"] is False  # 默认值
        assert "model" in result


def test_load_llm_config_invalid_json(tmp_path):
    """配置文件损坏时返回默认值"""
    config_file = tmp_path / "llmConfig.json"
    config_file.write_text("invalid json {", encoding="utf-8")

    with patch("utils.llm.config.LLM_CONFIG_PATH", str(config_file)):
        result = loadLLMConfig()
        assert result["enabled"] is False  # 默认值


def test_load_llm_config_merge_defaults(tmp_path):
    """部分配置时与默认值合并"""
    config_file = tmp_path / "llmConfig.json"
    config_data = {"enabled": True}  # 只有一个字段
    config_file.write_text(json.dumps(config_data), encoding="utf-8")

    with patch("utils.llm.config.LLM_CONFIG_PATH", str(config_file)):
        result = loadLLMConfig()
        assert result["enabled"] is True
        assert "model" in result  # 默认值存在


# ============================================================================
# saveLLMConfig() 测试
# ============================================================================

def test_save_llm_config_success(tmp_path):
    """成功保存配置"""
    config_file = tmp_path / "llmConfig.json"
    config_data = {"enabled": True, "model": "test-model"}

    with patch("utils.llm.config.LLM_CONFIG_PATH", str(config_file)):
        result = saveLLMConfig(config_data)
        assert result is True
        assert config_file.exists()

        # 验证内容
        saved = json.loads(config_file.read_text(encoding="utf-8"))
        assert saved["enabled"] is True
        assert saved["model"] == "test-model"


def test_save_llm_config_atomic_write(tmp_path):
    """原子写（tmpfile + replace）"""
    config_file = tmp_path / "llmConfig.json"
    config_data = {"enabled": True}

    with patch("utils.llm.config.LLM_CONFIG_PATH", str(config_file)):
        saveLLMConfig(config_data)

        # 临时文件应该被清理
        tmp_file = tmp_path / "llmConfig.json.tmp"
        assert not tmp_file.exists()


# ============================================================================
# _boundedInt() 测试
# ============================================================================

def test_bounded_int_within_range():
    """值在范围内"""
    assert _boundedInt(5, default=10, minValue=1, maxValue=10) == 5


def test_bounded_int_below_min():
    """值低于最小值"""
    assert _boundedInt(0, default=10, minValue=1, maxValue=10) == 1


def test_bounded_int_above_max():
    """值高于最大值"""
    assert _boundedInt(20, default=10, minValue=1, maxValue=10) == 10


def test_bounded_int_invalid_type():
    """无效类型返回默认值"""
    assert _boundedInt("invalid", default=10, minValue=1, maxValue=10) == 10
    assert _boundedInt(None, default=10, minValue=1, maxValue=10) == 10


def test_bounded_int_string_number():
    """字符串数字可转换"""
    assert _boundedInt("5", default=10, minValue=1, maxValue=10) == 5


# ============================================================================
# _boundedFloat() 测试
# ============================================================================

def test_bounded_float_within_range():
    """值在范围内"""
    assert _boundedFloat(5.5, default=10.0, minValue=1.0, maxValue=10.0) == 5.5


def test_bounded_float_below_min():
    """值低于最小值"""
    assert _boundedFloat(0.5, default=10.0, minValue=1.0, maxValue=10.0) == 1.0


def test_bounded_float_above_max():
    """值高于最大值"""
    assert _boundedFloat(20.0, default=10.0, minValue=1.0, maxValue=10.0) == 10.0


def test_bounded_float_invalid_type():
    """无效类型返回默认值"""
    assert _boundedFloat("invalid", default=10.0, minValue=1.0, maxValue=10.0) == 10.0


# ============================================================================
# _normalizeBlockedHost() 测试
# ============================================================================

def test_normalize_blocked_host_basic():
    """基本域名规范化"""
    assert _normalizeBlockedHost("Example.COM") == "example.com"


def test_normalize_blocked_host_with_scheme():
    """包含 scheme 时提取 host"""
    assert _normalizeBlockedHost("https://example.com/path") == "example.com"
    assert _normalizeBlockedHost("http://example.com") == "example.com"


def test_normalize_blocked_host_with_port():
    """包含端口时去除"""
    assert _normalizeBlockedHost("example.com:8080") == "example.com"


def test_normalize_blocked_host_trailing_dot():
    """去除尾随点"""
    assert _normalizeBlockedHost("example.com.") == "example.com"


def test_normalize_blocked_host_ipv6():
    """IPv6 地址处理（方括号会被去除）"""
    assert _normalizeBlockedHost("[::1]") == "::1"
    assert _normalizeBlockedHost("[::1]:8080") == "::1"


def test_normalize_blocked_host_empty():
    """空字符串抛出异常"""
    with pytest.raises(ValueError, match="不能为空"):
        _normalizeBlockedHost("")


def test_normalize_blocked_host_forbidden_tld():
    """禁止单独使用常见 TLD"""
    with pytest.raises(ValueError, match="常见公共后缀"):
        _normalizeBlockedHost("com")

    with pytest.raises(ValueError, match="常见公共后缀"):
        _normalizeBlockedHost("org")


def test_normalize_blocked_host_subdomain_allowed():
    """子域名允许"""
    assert _normalizeBlockedHost("evil.com") == "evil.com"
    assert _normalizeBlockedHost("sub.example.org") == "sub.example.org"


# ============================================================================
# _normalizeGroupTriggerKeyword() 测试
# ============================================================================

def test_normalize_group_trigger_keyword_basic():
    """基本关键词规范化"""
    assert _normalizeGroupTriggerKeyword("  Hello  ") == "hello"


def test_normalize_group_trigger_keyword_empty():
    """空关键词抛出异常"""
    with pytest.raises(ValueError, match="不能为空"):
        _normalizeGroupTriggerKeyword("")

    with pytest.raises(ValueError, match="不能为空"):
        _normalizeGroupTriggerKeyword("   ")


def test_normalize_group_trigger_keyword_with_whitespace():
    """包含空白字符抛出异常"""
    with pytest.raises(ValueError, match="不能包含空白字符"):
        _normalizeGroupTriggerKeyword("hello world")


def test_normalize_group_trigger_keyword_too_long():
    """超长关键词抛出异常"""
    long_keyword = "a" * 33
    with pytest.raises(ValueError, match="最长"):
        _normalizeGroupTriggerKeyword(long_keyword)


def test_normalize_group_trigger_keyword_max_length():
    """最大长度边界"""
    keyword = "a" * 32
    assert _normalizeGroupTriggerKeyword(keyword) == keyword


# ============================================================================
# setAutoMode() 测试
# ============================================================================

def test_set_auto_mode_valid():
    """有效的 autoMode"""
    with patch("utils.llm.config._setConfig") as mock_set:
        setAutoMode("on")
        mock_set.assert_called_once_with(autoMode="on")


def test_set_auto_mode_invalid():
    """无效的 autoMode 抛出异常"""
    with pytest.raises(ValueError, match="无效的 autoMode"):
        setAutoMode("invalid")


# ============================================================================
# Group Trigger Keywords 测试
# ============================================================================

def test_set_group_trigger_keywords_deduplication():
    """关键词去重"""
    with patch("utils.llm.config._setConfig") as mock_set:
        setGroupTriggerKeywords(["hello", "HELLO", "world"])
        # 应该去重并规范化
        call_args = mock_set.call_args[1]
        assert call_args["groupTriggerKeywords"] == ["hello", "world"]


def test_add_group_trigger_keyword():
    """添加关键词"""
    with patch("utils.llm.config.getGroupTriggerKeywords", return_value=["hello"]):
        with patch("utils.llm.config._setConfig") as mock_set:
            addGroupTriggerKeyword("world")
            call_args = mock_set.call_args[1]
            assert "world" in call_args["groupTriggerKeywords"]


def test_add_group_trigger_keyword_duplicate():
    """添加重复关键词不重复添加"""
    with patch("utils.llm.config.getGroupTriggerKeywords", return_value=["hello"]):
        with patch("utils.llm.config._setConfig") as mock_set:
            addGroupTriggerKeyword("hello")
            mock_set.assert_not_called()


def test_remove_group_trigger_keyword():
    """移除关键词"""
    with patch("utils.llm.config.getGroupTriggerKeywords", return_value=["hello", "world"]):
        with patch("utils.llm.config._setConfig") as mock_set:
            removeGroupTriggerKeyword("hello")
            call_args = mock_set.call_args[1]
            assert "hello" not in call_args["groupTriggerKeywords"]
            assert "world" in call_args["groupTriggerKeywords"]


# ============================================================================
# URL Blocked Hosts 测试
# ============================================================================

def test_set_url_read_blocked_hosts_deduplication():
    """blocked hosts 去重"""
    with patch("utils.llm.config._setConfig") as mock_set:
        setURLReadBlockedHosts(["evil.com", "EVIL.COM", "bad.org"])
        call_args = mock_set.call_args[1]
        assert call_args["urlReadBlockedHosts"] == ["evil.com", "bad.org"]


def test_add_url_read_blocked_host():
    """添加 blocked host"""
    with patch("utils.llm.config.getURLReadBlockedHosts", return_value=["evil.com"]):
        with patch("utils.llm.config._setConfig") as mock_set:
            addURLReadBlockedHost("bad.org")
            call_args = mock_set.call_args[1]
            assert "bad.org" in call_args["urlReadBlockedHosts"]


def test_remove_url_read_blocked_host():
    """移除 blocked host"""
    with patch("utils.llm.config.getURLReadBlockedHosts", return_value=["evil.com", "bad.org"]):
        with patch("utils.llm.config._setConfig") as mock_set:
            removeURLReadBlockedHost("evil.com")
            call_args = mock_set.call_args[1]
            assert "evil.com" not in call_args["urlReadBlockedHosts"]


# ============================================================================
# Knowledge 配置测试
# ============================================================================

def test_set_knowledge_max_results_valid():
    """有效的 maxResults"""
    with patch("utils.llm.config._setConfig") as mock_set:
        setKnowledgeMaxResults(5)
        mock_set.assert_called_once_with(knowledgeMaxResults=5)


def test_set_knowledge_max_results_invalid():
    """无效的 maxResults 抛出异常"""
    with pytest.raises(ValueError, match="必须在 1-10 之间"):
        setKnowledgeMaxResults(0)

    with pytest.raises(ValueError, match="必须在 1-10 之间"):
        setKnowledgeMaxResults(11)


def test_set_knowledge_min_score_valid():
    """有效的 minScore"""
    with patch("utils.llm.config._setConfig") as mock_set:
        setKnowledgeMinScore(0.5)
        mock_set.assert_called_once_with(knowledgeMinScore=0.5)


def test_set_knowledge_min_score_invalid():
    """无效的 minScore 抛出异常"""
    with pytest.raises(ValueError, match="必须是大于等于 0 的数"):
        setKnowledgeMinScore(-1)