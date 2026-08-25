"""
tests/utils/core/test_stateManager.py

interactiveChatID 追踪（receiver 活跃聊天登记）：
    - 默认 None；set/get；int 入参 str() 归一（whitelist uid 兼容）；None 清除

每用例新实例（fixture 构造 StateManager()，不走 getStateManager 单例）——
构造无副作用：仅建 asyncio.Event（3.10+ 不绑运行循环）+ RLock，生产 3.11 安全。
"""

import pytest

from utils.core.stateManager import StateManager


@pytest.fixture
def sm():
    return StateManager()


class TestInteractiveChatID:

    def test_default_none(self, sm):
        assert sm.getInteractiveChatID() is None

    def test_set_and_get(self, sm):
        sm.setInteractiveChatID("123")
        assert sm.getInteractiveChatID() == "123"

    def test_int_normalized_to_str(self, sm):
        """int 入参被归一为 str——whitelist uid 与 llm 侧 str 比较端零心智"""
        sm.setInteractiveChatID(123)
        assert sm.getInteractiveChatID() == "123"

    def test_clear_with_none(self, sm):
        sm.setInteractiveChatID("123")
        sm.setInteractiveChatID(None)
        assert sm.getInteractiveChatID() is None
