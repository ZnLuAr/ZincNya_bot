"""验证防抖缓冲保留完整消息、顺序和数量上限。"""

from utils.llm.models import MessageInput, MessageOrigin
from utils.llm.state import appendPendingMessage, popPendingMessages


def _message(messageID: int, text: str) -> MessageInput:
    return MessageInput(
        origin=MessageOrigin(chatID="chat", userID=1, messageID=messageID),
        text=text,
    )


def test_pop_pending_messages_preserves_order_and_objects():
    key = "test:ordered"
    first = _message(1, "第一条")
    second = _message(2, "第二条")
    try:
        assert appendPendingMessage(key, first)
        assert appendPendingMessage(key, second)
        assert popPendingMessages(key) == (first, second)
        assert popPendingMessages(key) == ()
    finally:
        popPendingMessages(key)

def test_pending_buffer_enforces_configured_limit(monkeypatch):
    key = "test:limit"
    monkeypatch.setattr("utils.llm.state.LLM_PENDING_MSG_LIMIT", 2)
    try:
        assert appendPendingMessage(key, _message(1, "一"))
        assert appendPendingMessage(key, _message(2, "二"))
        assert not appendPendingMessage(key, _message(3, "三"))
        assert len(popPendingMessages(key)) == 2
    finally:
        popPendingMessages(key)
