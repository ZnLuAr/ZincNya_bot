"""
tests/utils/llm/review/test_items.py

测试审核项的创建与存取（utils/llm/review/items.py）：
    - _formatReviewHint / peekReviewHint：控制台队列队首的预览文字
    - makeReplyReviewItem / addReviewItem / addMemoryReviewItem：控制台队列里的审核项
    - extractMemoryActionFields / canEditReviewItem / canRetryReviewItem
    - bot_data 里的审核项：key 格式、putReplyReview / putMemoryReview 写入的条目与
      反向索引、deleteReviewEntry、cleanupExpiredReviews

约定：
    - 用到控制台队列的用例经 reviewQueue fixture 换成一条空队列，不碰进程里真正的队列
"""

import asyncio
import time

import pytest

from config import LLM_REVIEW_TTL_SECONDS

from utils.llm.memory.types import MemoryQuery, MemoryTurn
from utils.llm.models import DisplayBlocks, MessageInput, MessageOrigin, TaskRequest
from utils.llm.review.items import (
    _formatReviewHint,
    addMemoryReviewItem,
    addReviewItem,
    canEditReviewItem,
    canRetryReviewItem,
    cleanupExpiredReviews,
    deleteReviewEntry,
    editIndexKey,
    extractMemoryActionFields,
    isMemoryReviewKey,
    makeReplyReviewItem,
    memoryReviewKey,
    peekReviewHint,
    putMemoryReview,
    putReplyReview,
    replyReviewKey,
)


# 过期判定两侧各留的余量（秒），避免用例卡在 TTL 边界上
_TTL_MARGIN_SECONDS = 60




# ===========================================================================
# 测试数据构造
# ===========================================================================

@pytest.fixture
def reviewQueue(monkeypatch):
    """把 items.py 用的控制台审核队列换成一条空队列。"""
    queue = asyncio.Queue()
    monkeypatch.setattr("utils.llm.review.items.getReviewQueue", lambda: queue)
    return queue


def _request() -> TaskRequest:
    return TaskRequest(
        messages=(MessageInput(
            origin=MessageOrigin(chatID="456", userID=42, messageID=9),
            text="问题",
            memoryTurn=MemoryTurn(currentText="问题"),
        ),),
        includeContext=True,
    )


def _seedReply(botData: dict, *, chatID, reviewMsgID, createdAt: float) -> str:
    """经 putReplyReview 写入一条回复审核项，再把 createdAt 改成指定时间。"""
    key = putReplyReview(
        botData, chatID=chatID, reviewMsgID=reviewMsgID, reply="回复", originalMsg="原文",
        opsID=123, triggerMsgID=9, userID=42, includeContext=False,
    )
    botData[key]["createdAt"] = createdAt
    return key


def _seedMemory(botData: dict, *, chatID, reviewMsgID, createdAt: float) -> str:
    """经 putMemoryReview 写入一条记忆审核项，再把 createdAt 改成指定时间。"""
    key = putMemoryReview(
        botData, chatID=chatID, reviewMsgID=reviewMsgID,
        action={"action": "add", "content": "事实"}, originalMsg="原文", opsID=123, userID=42,
    )
    botData[key]["createdAt"] = createdAt
    return key




# ===========================================================================
# 队列 item 契约（_formatReviewHint——随队列契约自 state.py 迁入）
# ===========================================================================

class TestFormatReviewHintNewlineEscape:
    """_formatReviewHint：换行转义 + 截断"""

    def test_reply_newline_escaped(self):
        """回复含换行：预览里换行被转义为字面 \\n"""
        hint = _formatReviewHint({"kind": "reply", "reply": "第一行\n第二行"})

        assert "\n第二行" not in hint           # 不含真实换行
        assert "第一行\\n第二行" in hint          # 含转义后的字面 \n

    def test_reply_no_newline_unchanged(self):
        """回复无换行：原样展示"""
        hint = _formatReviewHint({"kind": "reply", "reply": "单行回复"})
        assert "单行回复" in hint

    def test_reply_truncated_on_escaped_length(self):
        """截断基于转义后字符串：>16 字符附加省略号"""
        hint = _formatReviewHint({"kind": "reply", "reply": "一二三四五六七八九十一二三四五六七"})
        assert "…" in hint

    def test_memory_content_newline_escaped(self):
        """记忆操作 content 含换行：同样被转义"""
        item = {"kind": "memory", "action": {"action": "add", "content": "甲\n乙"}}
        hint = _formatReviewHint(item)

        assert "\n乙" not in hint
        assert "甲\\n乙" in hint

    def test_memory_fallback_to_memory_id(self):
        """记忆操作无 content 时回退到 #memoryID"""
        item = {"kind": "memory", "action": {"action": "delete", "memoryID": 42}}
        hint = _formatReviewHint(item)
        assert "#42" in hint


class TestPeekReviewHint:
    """peekReviewHint：只看队首，不消费"""

    def test_empty_queue_returns_none(self, reviewQueue):
        assert peekReviewHint() is None

    def test_shows_head_item_without_consuming(self, reviewQueue):
        reviewQueue.put_nowait({"kind": "reply", "reply": "队首回复"})
        reviewQueue.put_nowait({"kind": "memory", "action": {"action": "add", "content": "队尾"}})

        hint = peekReviewHint()

        assert hint == "当前操作的是：[回复] 队首回复"
        assert reviewQueue.qsize() == 2




# ===========================================================================
# 控制台队列里的审核项
# ===========================================================================

class TestConsoleReviewItems:
    """makeReplyReviewItem / addReviewItem / addMemoryReviewItem"""

    def test_reply_item_fields(self):
        request = _request()
        memoryQuery = request.memoryQuery
        blocks = DisplayBlocks(pairs=[{"replyLine": "", "currentText": "问题"}])

        item = makeReplyReviewItem(
            chatID="456", messageID=9, originalMsg="原文", reply="回复", opsID="123",
            userID=42, includeContext=True, displayBlocks=blocks,
            memoryQuery=memoryQuery, request=request,
        )

        assert item == {
            "kind": "reply",
            "chatID": "456",
            "messageID": 9,
            "originalMsg": "原文",
            "reply": "回复",
            "opsID": "123",
            "userID": "42",          # 入队时转成字符串
            "includeContext": True,
            "displayBlocks": blocks,
            "memoryQuery": memoryQuery,
            "request": request,
        }
        assert item["request"] is request

    def test_reply_item_without_user_keeps_none(self):
        item = makeReplyReviewItem(
            chatID="456", messageID=9, originalMsg="原文", reply="回复", opsID="123",
        )

        assert item["userID"] is None
        assert item["includeContext"] is False
        assert item["displayBlocks"] is None
        assert item["memoryQuery"] is None
        assert item["request"] is None

    def test_add_review_item_enqueues_reply_item(self, reviewQueue):
        request = _request()

        addReviewItem(
            chatID="456", messageID=9, originalMsg="原文", reply="回复", opsID="123",
            userID=42, includeContext=True, request=request,
        )

        assert reviewQueue.qsize() == 1
        item = reviewQueue.get_nowait()
        assert item == makeReplyReviewItem(
            chatID="456", messageID=9, originalMsg="原文", reply="回复", opsID="123",
            userID=42, includeContext=True, request=request,
        )
        assert item["request"] is request

    def test_add_memory_review_item_enqueues_memory_item(self, reviewQueue):
        action = {"action": "update", "memoryID": 7, "content": "事实", "targetState": "state"}

        addMemoryReviewItem(
            action=action, chatID="456", originalMsg="触发消息", opsID="123", userID=42,
        )

        assert reviewQueue.qsize() == 1
        assert reviewQueue.get_nowait() == {
            "kind": "memory",
            "action": action,
            "chatID": "456",
            "originalMsg": "触发消息",
            "opsID": "123",
            "userID": "42",
            "displayBlocks": None,
        }




# ===========================================================================
# 共享字段提取与能力判断
# ===========================================================================

class TestExtractMemoryActionFields:

    def test_empty_action_uses_defaults(self):
        assert extractMemoryActionFields({}) == {
            "actionType": "?",
            "scopeType": "?",
            "scopeID": "",
            "content": None,
            "originalContent": "",
            "tags": [],
            "priority": None,
            "mode": None,
            "originalMode": None,
            "retrievalHint": None,
            "reason": "",
            "memoryID": None,
        }

    def test_none_values_fall_back_for_display(self):
        """originalContent / tags 显式为 None 时换成空值，格式化函数不用再判 None"""
        fields = extractMemoryActionFields({"action": "update", "originalContent": None, "tags": None})

        assert fields["originalContent"] == ""
        assert fields["tags"] == []

    def test_given_values_pass_through(self):
        fields = extractMemoryActionFields({
            "action": "update",
            "scopeType": "user",
            "scopeID": "42",
            "content": "拟议事实",
            "originalContent": "当前事实",
            "tags": ["事实"],
            "priority": 0,
            "mode": "pinned",
            "originalMode": "contextual",
            "retrievalHint": "",
            "reason": "用户更正",
            "memoryID": 7,
        })

        assert fields == {
            "actionType": "update",
            "scopeType": "user",
            "scopeID": "42",
            "content": "拟议事实",
            "originalContent": "当前事实",
            "tags": ["事实"],
            "priority": 0,           # 0 是有效优先级，不当成缺省
            "mode": "pinned",
            "originalMode": "contextual",
            "retrievalHint": "",     # 空串表示清空，与缺省的 None 不同
            "reason": "用户更正",
            "memoryID": 7,
        }


class TestReviewItemAbilities:
    """canEditReviewItem / canRetryReviewItem"""

    @pytest.mark.parametrize("item, expected", [
        ({"kind": "reply"}, True),
        ({}, True),                                               # 缺 kind 视为回复审核
        ({"kind": "memory", "action": {"action": "add"}}, True),
        ({"kind": "memory", "action": {"action": "update"}}, True),
        ({"kind": "memory", "action": {"action": "delete"}}, False),
        ({"kind": "memory"}, False),
        ({"kind": "other"}, False),
    ])
    def test_can_edit(self, item, expected):
        assert canEditReviewItem(item) is expected

    @pytest.mark.parametrize("item, expected", [
        ({"kind": "reply"}, True),
        ({}, True),
        ({"kind": "memory", "action": {"action": "add"}}, False),
        ({"kind": "other"}, False),
    ])
    def test_can_retry(self, item, expected):
        assert canRetryReviewItem(item) is expected




# ===========================================================================
# bot_data 里的审核项（Telegram 审核卡）
# ===========================================================================

class TestReviewKeys:

    def test_key_formats(self):
        assert replyReviewKey(456, 321) == "llm_review_456_321"
        assert memoryReviewKey(456, 321) == "llm_memreview_456_321"
        assert editIndexKey(321) == "llm_editidx_321"

    def test_is_memory_review_key(self):
        assert isMemoryReviewKey(memoryReviewKey(456, 321)) is True
        assert isMemoryReviewKey(replyReviewKey(456, 321)) is False
        assert isMemoryReviewKey(editIndexKey(321)) is False


class TestPutReplyReview:

    def test_stores_entry_and_reverse_index(self):
        botData = {}
        request = _request()
        memoryQuery = MemoryQuery(turns=(MemoryTurn(currentText="问题"),))
        blocks = DisplayBlocks(pairs=[{"replyLine": "", "currentText": "问题"}])

        before = time.time()
        key = putReplyReview(
            botData, chatID="456", reviewMsgID=321, reply="回复", originalMsg="原文",
            opsID=123, triggerMsgID=9, userID=42, includeContext=True, autoMode="off",
            displayBlocks=blocks, memoryQuery=memoryQuery, request=request,
        )
        after = time.time()

        assert key == "llm_review_456_321"
        assert set(botData) == {key, "llm_editidx_321"}
        assert botData["llm_editidx_321"] == key

        entry = dict(botData[key])
        assert before <= entry.pop("createdAt") <= after
        assert entry == {
            "reply": "回复",
            "originalMsg": "原文",
            "chatID": "456",
            "opsID": 123,
            "triggerMsgID": 9,
            "userID": 42,
            "includeContext": True,
            "autoMode": "off",
            "displayBlocks": blocks,
            "memoryQuery": memoryQuery,
            "request": request,
        }
        assert botData[key]["request"] is request
        assert botData[key]["memoryQuery"] is memoryQuery

    def test_optional_fields_default_to_none(self):
        botData = {}

        key = putReplyReview(
            botData, chatID=456, reviewMsgID=321, reply="回复", originalMsg="原文",
            opsID=123, triggerMsgID=9, userID=42, includeContext=False,
        )

        entry = botData[key]
        assert entry["autoMode"] is None
        assert entry["displayBlocks"] is None
        assert entry["memoryQuery"] is None
        assert entry["request"] is None


class TestPutMemoryReview:

    def test_stores_entry_and_reverse_index(self):
        botData = {}
        action = {"action": "update", "memoryID": 7, "content": "事实", "targetState": "state"}
        blocks = DisplayBlocks(pairs=[{"replyLine": "", "currentText": "触发消息"}])

        before = time.time()
        key = putMemoryReview(
            botData, chatID=456, reviewMsgID=322, action=action, originalMsg="触发消息",
            opsID=123, userID=42, displayBlocks=blocks,
        )
        after = time.time()

        assert key == "llm_memreview_456_322"
        assert set(botData) == {key, "llm_editidx_322"}
        assert botData["llm_editidx_322"] == key

        entry = dict(botData[key])
        assert before <= entry.pop("createdAt") <= after
        assert entry == {
            "action": action,
            "originalMsg": "触发消息",
            "chatID": 456,
            "opsID": 123,
            "userID": 42,
            "displayBlocks": blocks,
        }
        assert botData[key]["action"] is action

    def test_reply_and_memory_cards_of_one_chat_do_not_collide(self):
        """同一聊天的回复审核卡与记忆审核卡各有自己的条目和反向索引"""
        botData = {}

        replyKey = _seedReply(botData, chatID=456, reviewMsgID=321, createdAt=time.time())
        memoryKey = _seedMemory(botData, chatID=456, reviewMsgID=322, createdAt=time.time())

        assert replyKey != memoryKey
        assert botData[editIndexKey(321)] == replyKey
        assert botData[editIndexKey(322)] == memoryKey


class TestDeleteReviewEntry:

    def test_removes_entry_with_reverse_index(self):
        botData = {}
        replyKey = _seedReply(botData, chatID=456, reviewMsgID=321, createdAt=time.time())
        memoryKey = _seedMemory(botData, chatID=456, reviewMsgID=322, createdAt=time.time())

        deleteReviewEntry(botData, replyKey)

        assert set(botData) == {memoryKey, editIndexKey(322)}

        deleteReviewEntry(botData, memoryKey)

        assert botData == {}

    def test_negative_chat_id_still_finds_reverse_index(self):
        """群聊的 chatID 是负数，反向索引按 key 的最后一段（审核卡 message ID）找"""
        botData = {}
        key = _seedReply(botData, chatID=-1001234567890, reviewMsgID=321, createdAt=time.time())

        deleteReviewEntry(botData, key)

        assert botData == {}

    def test_missing_key_is_ignored(self):
        botData = {}
        key = _seedReply(botData, chatID=456, reviewMsgID=321, createdAt=time.time())

        deleteReviewEntry(botData, replyReviewKey(456, 999))

        assert set(botData) == {key, editIndexKey(321)}


class TestCleanupExpiredReviews:

    def test_removes_only_expired_reply_and_memory_reviews(self):
        now = time.time()
        expiredAt = now - LLM_REVIEW_TTL_SECONDS - _TTL_MARGIN_SECONDS
        freshAt = now - LLM_REVIEW_TTL_SECONDS + _TTL_MARGIN_SECONDS
        botData = {}
        _seedReply(botData, chatID=456, reviewMsgID=1, createdAt=expiredAt)
        _seedMemory(botData, chatID=456, reviewMsgID=2, createdAt=expiredAt)
        freshReply = _seedReply(botData, chatID=456, reviewMsgID=3, createdAt=freshAt)
        freshMemory = _seedMemory(botData, chatID=456, reviewMsgID=4, createdAt=freshAt)

        cleanupExpiredReviews(botData)

        assert set(botData) == {
            freshReply, editIndexKey(3),
            freshMemory, editIndexKey(4),
        }

    def test_leaves_other_bot_data_alone(self):
        """只认两种审核项前缀下的 dict；别的键即使带着很早的 createdAt 也不动"""
        botData = {
            "other_feature": {"createdAt": 0},
            "llm_review_note": "不是审核项",
            editIndexKey(77): "llm_review_456_77",
        }
        snapshot = dict(botData)

        cleanupExpiredReviews(botData)

        assert botData == snapshot

    def test_entry_without_created_at_is_treated_as_expired(self):
        botData = {}
        key = _seedReply(botData, chatID=456, reviewMsgID=5, createdAt=time.time())
        del botData[key]["createdAt"]

        cleanupExpiredReviews(botData)

        assert botData == {}
