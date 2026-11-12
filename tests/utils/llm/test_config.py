"""
tests/utils/llm/test_config.py

测试 utils/llm/config.py
"""

import pytest
import json
from unittest.mock import patch, mock_open
from utils.llm.config import (
    getMemorySelectorSettings,
    loadLLMConfig,
    saveLLMConfig,
    _normalizeGroupTriggerKeyword,
    setAutoMode,
    setGroupTriggerKeywords,
    addGroupTriggerKeyword,
    removeGroupTriggerKeyword,
    setKnowledgeMaxResults,
    setKnowledgeMinScore,
)


@pytest.mark.parametrize("key,value", [
    ("memoryHybridSelector", "typo"), ("memorySelectorModel", ""),
    ("memorySelectorProtocol", "message"), ("memorySelectorProtocol", "MESSAGES"),
    ("memorySelectorProtocol", None), ("memorySelectorProtocol", True),
    ("memorySelectorEffort", "invalid"), ("memorySelectorTimeoutSeconds", float("nan")),
    ("memorySelectorTimeoutSeconds", float("inf")), ("memorySelectorTimeoutSeconds", 31),
    ("memorySelectorTimeoutSeconds", 0), ("memorySelectorTimeoutSeconds", True),
    ("memorySelectorTimeoutSeconds", None),
])
def test_memorySelectorRejectsInvalidSettings(key, value):
    """非法配置必须在发送前失败，不能悄悄扩大选择期限。"""
    with pytest.raises(ValueError, match="^selectorConfig$"):
        getMemorySelectorSettings({key: value})


def test_memorySelectorDefaultsStayDisabledAndThirtySeconds():
    """默认 local，显式 llm 才能使用已批准的独立远程预算。"""
    from utils.llm.config import _DEFAULT_CONFIG
    assert _DEFAULT_CONFIG["memoryRetrievalMode"] == "legacy"
    assert _DEFAULT_CONFIG["memoryEnabled"] is False
    assert getMemorySelectorSettings({}) == {
        "backend": "local", "protocol": "responses", "model": "gpt-5.6-terra",
        "effort": "high", "timeoutSeconds": 30.0,
    }
    supplied = {"memoryHybridSelector": "llm", "memorySelectorTimeoutSeconds": "15"}
    assert getMemorySelectorSettings(supplied)["timeoutSeconds"] == 15
    assert supplied["memorySelectorTimeoutSeconds"] == "15"


def test_memorySelectorMessagesRequiresExplicitProtocolWithoutChangingModelDefault():
    """协议显式选择，配置读取不替用户换模型或修改原始快照。"""
    supplied = {"memoryHybridSelector": "llm", "memorySelectorProtocol": "messages",
                "memorySelectorModel": " claude-test ", "memorySelectorEffort": "medium"}
    result = getMemorySelectorSettings(supplied)
    assert result == {"backend": "llm", "protocol": "messages", "model": "claude-test",
                      "effort": "medium", "timeoutSeconds": 30.0}
    assert supplied["memorySelectorModel"] == " claude-test "
    assert getMemorySelectorSettings({"memorySelectorProtocol": "messages"})["model"] == "gpt-5.6-terra"


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
