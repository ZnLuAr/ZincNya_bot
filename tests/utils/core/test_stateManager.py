"""
tests/utils/core/test_stateManager.py

聊天界面订阅：独立队列、聊天隔离、退出清理，旧会话不能撤销新订阅。

每用例新实例（fixture 构造 StateManager()，不走 getStateManager 单例）——
构造无副作用：仅建 asyncio.Event（3.10+ 不绑运行循环）+ RLock，生产 3.11 安全。
"""

import pytest

from utils.core.stateManager import StateManager


@pytest.fixture
def sm():
    return StateManager()


class TestChatMessageSubscription:

    def test_only_active_chat_receives_notifications(self, sm):
        assert sm.getInteractiveChatID() is None
        queue = sm.subscribeChatMessages(-100)
        sm.notifyChatMessage("other", 1, "其它会话")
        sm.notifyChatMessage("-100", 2, "本会话")
        assert queue.get_nowait() == (2, "本会话")
        assert queue.empty()

    def test_exit_discards_subscription_and_reentry_gets_new_queue(self, sm):
        queue = sm.subscribeChatMessages("123")
        sm.unsubscribeChatMessages(queue)
        sm.notifyChatMessage("123", 1, "离线来信")
        assert sm.getInteractiveChatID() is None
        assert sm.subscribeChatMessages("123").empty()
        assert queue.empty()

    def test_old_session_cannot_unsubscribe_new_session(self, sm):
        oldQueue = sm.subscribeChatMessages("123")
        newQueue = sm.subscribeChatMessages("456")
        sm.unsubscribeChatMessages(oldQueue)
        sm.notifyChatMessage("456", 1, "新会话来信")
        assert newQueue.get_nowait() == (1, "新会话来信")
        assert sm.getInteractiveChatID() == "456"


class TestMemoryRuntime:

    def test_default_set_and_clear(self, sm):
        runtime = object()

        assert sm.getMemoryRuntime() is None
        sm.setMemoryRuntime(runtime)
        assert sm.getMemoryRuntime() is runtime
        sm.setMemoryRuntime(None)
        assert sm.getMemoryRuntime() is None

    def test_stats_only_report_presence(self, sm):
        runtime = object()

        assert sm.getStats()["hasMemoryRuntime"] is False
        sm.setMemoryRuntime(runtime)
        assert sm.getStats()["hasMemoryRuntime"] is True
