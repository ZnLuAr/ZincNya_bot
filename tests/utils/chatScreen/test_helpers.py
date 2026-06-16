"""
测试 utils/chatScreen/helpers.py
"""

from unittest.mock import patch

from utils.chatScreen.helpers import getNextChatID


def test_get_next_chat_id_next_direction():
    """测试 next 方向切换"""
    with patch('utils.whitelistManager.data.getAllowedUserIDs', return_value=['111', '222', '333']):
        assert getNextChatID('111', 'next') == '222'
        assert getNextChatID('222', 'next') == '333'
        assert getNextChatID('333', 'next') == '111'  # 循环到开头


def test_get_next_chat_id_prev_direction():
    """测试 prev 方向切换"""
    with patch('utils.whitelistManager.data.getAllowedUserIDs', return_value=['111', '222', '333']):
        assert getNextChatID('111', 'prev') == '333'  # 循环到末尾
        assert getNextChatID('222', 'prev') == '111'
        assert getNextChatID('333', 'prev') == '222'


def test_get_next_chat_id_single_user():
    """测试只有一个用户时返回 None"""
    with patch('utils.whitelistManager.data.getAllowedUserIDs', return_value=['111']):
        assert getNextChatID('111', 'next') is None
        assert getNextChatID('111', 'prev') is None


def test_get_next_chat_id_empty_list():
    """测试空列表时返回 None"""
    with patch('utils.whitelistManager.data.getAllowedUserIDs', return_value=[]):
        assert getNextChatID('111', 'next') is None
        assert getNextChatID('111', 'prev') is None


def test_get_next_chat_id_current_not_in_list():
    """测试当前 chatID 不在列表中时,回退到第一个用户"""
    with patch('utils.whitelistManager.data.getAllowedUserIDs', return_value=['111', '222']):
        assert getNextChatID('999', 'next') == '111'
        assert getNextChatID('999', 'prev') == '111'


def test_get_next_chat_id_invalid_direction():
    """测试无效的 direction 参数"""
    with patch('utils.whitelistManager.data.getAllowedUserIDs', return_value=['111', '222']):
        assert getNextChatID('111', 'invalid') is None
        assert getNextChatID('111', '') is None