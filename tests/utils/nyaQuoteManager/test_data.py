"""
tests/utils/nyaQuoteManager/test_data.py

测试 utils/nyaQuoteManager/data.py
"""

import pytest
from unittest.mock import patch, MagicMock

from utils.nyaQuoteManager.data import (
    getRandomQuote,
    userOperation,
)


# ============================================================================
# getRandomQuote() 测试
# ============================================================================

def test_get_random_quote_empty():
    """空语录库返回空列表"""
    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=[]):
        result = getRandomQuote()
        assert result == []


def test_get_random_quote_single():
    """单条语录"""
    quotes = [
        {"text": "喵~", "weight": 1.0}
    ]

    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=quotes):
        with patch('utils.nyaQuoteManager.data.random.choices', return_value=[quotes[0]]):
            result = getRandomQuote()
            assert result == ["喵~"]


def test_get_random_quote_multiple():
    """多条语录的权重选择"""
    quotes = [
        {"text": "喵~", "weight": 1.0},
        {"text": "呜呜", "weight": 2.0},
    ]

    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=quotes):
        with patch('utils.nyaQuoteManager.data.random.choices', return_value=[quotes[1]]):
            result = getRandomQuote()
            assert result == ["呜呜"]


def test_get_random_quote_multi_message_no_weight():
    """多消息格式（无条件概率）"""
    quotes = [
        {"text": "第一条|||第二条|||第三条", "weight": 1.0}
    ]

    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=quotes):
        with patch('utils.nyaQuoteManager.data.random.choices', return_value=[quotes[0]]):
            result = getRandomQuote()
            # 无条件概率时全部发送
            assert result == ["第一条", "第二条", "第三条"]


def test_get_random_quote_multi_message_with_weight():
    """多消息格式（带条件概率）"""
    quotes = [
        {"text": "第一条|||第二条|||第三条", "weight": [1.0, 0.8, 0.5]}
    ]

    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=quotes):
        with patch('utils.nyaQuoteManager.data.random.choices', return_value=[quotes[0]]):
            # Mock random.random() 返回值控制条件概率
            with patch('utils.nyaQuoteManager.data.random.random', side_effect=[0.7, 0.4]):
                # 0.7 < 0.8 → 第二条发送
                # 0.4 < 0.5 → 第三条发送
                result = getRandomQuote()
                assert result == ["第一条", "第二条", "第三条"]


def test_get_random_quote_multi_message_break_chain():
    """多消息格式（条件概率链中断）"""
    quotes = [
        {"text": "第一条|||第二条|||第三条", "weight": [1.0, 0.8, 0.5]}
    ]

    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=quotes):
        with patch('utils.nyaQuoteManager.data.random.choices', return_value=[quotes[0]]):
            # Mock random.random() 返回值控制条件概率
            with patch('utils.nyaQuoteManager.data.random.random', side_effect=[0.9, 0.4]):
                # 0.9 > 0.8 → 第二条不发送，链中断
                result = getRandomQuote()
                assert result == ["第一条"]


def test_get_random_quote_decay_factor():
    """多消息格式（衰减因子）"""
    quotes = [
        # 只指定前两条的权重，第三条使用衰减
        {"text": "第一条|||第二条|||第三条", "weight": [1.0, 0.8]}
    ]

    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=quotes):
        with patch('utils.nyaQuoteManager.data.random.choices', return_value=[quotes[0]]):
            # 第三条的概率 = 0.8 * 0.64 = 0.512
            with patch('utils.nyaQuoteManager.data.random.random', side_effect=[0.7, 0.5]):
                # 0.7 < 0.8 → 第二条发送
                # 0.5 < 0.512 → 第三条发送
                result = getRandomQuote()
                assert result == ["第一条", "第二条", "第三条"]


def test_get_random_quote_newline_escape():
    """\\n 转义处理"""
    quotes = [
        {"text": "第一行\\n第二行", "weight": 1.0}
    ]

    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=quotes):
        with patch('utils.nyaQuoteManager.data.random.choices', return_value=[quotes[0]]):
            result = getRandomQuote()
            assert result == ["第一行\n第二行"]


def test_get_random_quote_list_weight_extraction():
    """list 格式权重提取（用于选择语录）"""
    quotes = [
        {"text": "A", "weight": [2.0, 0.5]},
        {"text": "B", "weight": 1.0},
    ]

    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=quotes):
        # 验证 random.choices 被调用时使用的权重
        with patch('utils.nyaQuoteManager.data.random.choices') as mock_choices:
            mock_choices.return_value = [quotes[0]]
            getRandomQuote()

            # 第一个参数是 quotes，第二个参数是 weights
            call_args = mock_choices.call_args
            assert call_args[1]['weights'] == [2.0, 1.0]


def test_get_random_quote_empty_weight_list():
    """空 weight 列表边界"""
    quotes = [
        {"text": "喵~", "weight": []}
    ]

    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=quotes):
        with patch('utils.nyaQuoteManager.data.random.choices', return_value=[quotes[0]]):
            result = getRandomQuote()
            # 空列表时使用默认权重 1.0
            assert result == ["喵~"]


# ============================================================================
# userOperation() 测试
# ============================================================================

def test_user_operation_add():
    """添加语录"""
    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=[]):
        with patch('utils.nyaQuoteManager.data.saveQuoteFile') as mock_save:
            result = userOperation("add", payload={"text": "新语录", "weight": 1.5})
            assert result is True

            # 验证保存的数据
            saved_quotes = mock_save.call_args[0][0]
            assert len(saved_quotes) == 1
            assert saved_quotes[0]['text'] == "新语录"
            assert saved_quotes[0]['weight'] == 1.5


def test_user_operation_add_list_weight():
    """添加语录（list 权重）"""
    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=[]):
        with patch('utils.nyaQuoteManager.data.saveQuoteFile') as mock_save:
            result = userOperation("add", payload={"text": "新语录", "weight": [1.0, 0.8]})
            assert result is True

            saved_quotes = mock_save.call_args[0][0]
            assert saved_quotes[0]['weight'] == [1.0, 0.8]


def test_user_operation_add_default_weight():
    """添加语录（默认权重）"""
    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=[]):
        with patch('utils.nyaQuoteManager.data.saveQuoteFile') as mock_save:
            result = userOperation("add", payload={"text": "新语录"})
            assert result is True

            saved_quotes = mock_save.call_args[0][0]
            assert saved_quotes[0]['weight'] == 1.0


def test_user_operation_add_invalid_payload():
    """添加语录（无效 payload）"""
    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=[]):
        # 缺少 text 字段
        result = userOperation("add", payload={"weight": 1.0})
        assert result is False

        # payload 不是 dict
        result = userOperation("add", payload="invalid")
        assert result is False


def test_user_operation_delete():
    """删除语录"""
    quotes = [
        {"text": "语录1", "weight": 1.0},
        {"text": "语录2", "weight": 2.0},
    ]

    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=quotes):
        with patch('utils.nyaQuoteManager.data.saveQuoteFile') as mock_save:
            result = userOperation("delete", index=0)
            assert result is True

            saved_quotes = mock_save.call_args[0][0]
            assert len(saved_quotes) == 1
            assert saved_quotes[0]['text'] == "语录2"


def test_user_operation_delete_invalid_index():
    """删除语录（无效索引）"""
    quotes = [{"text": "语录1", "weight": 1.0}]

    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=quotes):
        # 索引超出范围
        result = userOperation("delete", index=10)
        assert result is False

        # 负数索引
        result = userOperation("delete", index=-1)
        assert result is False

        # 缺少索引
        result = userOperation("delete")
        assert result is False


def test_user_operation_set():
    """更新语录"""
    quotes = [
        {"text": "旧文本", "weight": 1.0},
    ]

    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=quotes):
        with patch('utils.nyaQuoteManager.data.saveQuoteFile') as mock_save:
            result = userOperation("set", index=0, payload={"text": "新文本", "weight": 2.0})
            assert result is True

            saved_quotes = mock_save.call_args[0][0]
            assert saved_quotes[0]['text'] == "新文本"
            assert saved_quotes[0]['weight'] == 2.0


def test_user_operation_set_partial_update():
    """更新语录（部分字段）"""
    quotes = [
        {"text": "文本", "weight": 1.0},
    ]

    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=quotes):
        with patch('utils.nyaQuoteManager.data.saveQuoteFile') as mock_save:
            # 只更新 weight
            result = userOperation("set", index=0, payload={"weight": 3.0})
            assert result is True

            saved_quotes = mock_save.call_args[0][0]
            assert saved_quotes[0]['text'] == "文本"  # 保持不变
            assert saved_quotes[0]['weight'] == 3.0


def test_user_operation_set_invalid():
    """更新语录（无效参数）"""
    quotes = [{"text": "文本", "weight": 1.0}]

    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=quotes):
        # 无效索引
        result = userOperation("set", index=10, payload={"text": "新文本"})
        assert result is False

        # 缺少 payload
        result = userOperation("set", index=0)
        assert result is False

        # payload 不是 dict
        result = userOperation("set", index=0, payload="invalid")
        assert result is False


def test_user_operation_list():
    """列出所有语录"""
    quotes = [
        {"text": "语录1", "weight": 1.0},
        {"text": "语录2", "weight": 2.0},
    ]

    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=quotes):
        result = userOperation("list")
        assert result == quotes


def test_user_operation_unknown():
    """未知操作"""
    with patch('utils.nyaQuoteManager.data.loadQuoteFile', return_value=[]):
        result = userOperation("unknown_operation")
        assert result is False