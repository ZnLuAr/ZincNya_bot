"""
tests/handlers/test_llmReview.py

handleFeedbackRetry / handleReviewCallback 的空生成兜底测试：

    - :fb 成功但生成为空 → 恢复旧 reply 的卡片 + ⚠️ 后缀、bot_data 的 reply 不被
      空串覆盖、:fb 消息删除、记 WARNING 日志
    - retry 按钮成功但生成为空 → 同款断言（卡片恢复 + 不写回）

背景：思考模型经中转可能思考吃光 max_tokens 预算，响应无 text block → 空串
（provider 层已加截断提额重试兜底；此处覆盖的是「重试也救不回、生成结果为空」
的最后一道展示层兜底）。原同名测试文件在 00a4ddb 重构时删除，本文件为恢复性重建。

构造要点（与 handlers/llmReview.py 的门禁对应）：
    - hasPermission patch 到 handlers.llmReview.hasPermission 返回 True
    - bot_data 预置 llm_editidx_{msgID} → llm_review_{chatID}_{msgID} 反向索引
    - 审核条目 opsID 与发送者 id 一致（conftest mockUser.id = 123456789）
"""

from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from handlers.llmReview import handleFeedbackRetry, handleReviewCallback


_OPS_ID = "123456789"
_CHAT_ID = 987654321
_REVIEW_MSG_ID = 500


def _seedReviewEntry(bot_data: dict, *, reply: str = "旧回复", originalMsg: str = "原文"):
    """预置一条回复审核条目 + 反向索引（与 _putReplyReview 同构）"""
    key = f"llm_review_{_CHAT_ID}_{_REVIEW_MSG_ID}"
    bot_data[key] = {
        "reply": reply,
        "originalMsg": originalMsg,
        "chatID": _CHAT_ID,
        "opsID": _OPS_ID,
        "triggerMsgID": 100,
        "userID": 123456789,
        "includeContext": True,
        "urlContexts": [],
        "autoMode": "off",
        "displayBlocks": None,
        "createdAt": 9999999999.0,   # 远期时间戳，防 TTL 清理误删
    }
    bot_data[f"llm_editidx_{_REVIEW_MSG_ID}"] = key
    return key


def _makeFbMessage(text: str = ":fb 收敛一点"):
    """构造 ops 的 :fb 消息（reply_to_message 指向审核卡）"""
    message = MagicMock()
    message.text = text
    message.from_user.id = _OPS_ID
    reviewMsg = MagicMock()
    reviewMsg.message_id = _REVIEW_MSG_ID
    message.reply_to_message = reviewMsg
    message.delete = AsyncMock()
    return message


def _makeRetryQuery(action: str):
    """构造 retry 按钮的 callback query（message.edit_text 供 safeEditMessage await）"""
    query = MagicMock()
    query.data = f"llm:review:{action}:{_CHAT_ID}"
    query.from_user.id = _OPS_ID
    query.message.message_id = _REVIEW_MSG_ID
    query.message.edit_text = AsyncMock()
    query.answer = AsyncMock()
    return query



class TestFeedbackRetryEmptyGeneration:

    async def test_empty_reply_restores_card_keeps_bot_data(self, mockContext):
        """:fb 成功但生成为空 → 恢复旧卡片 + ⚠️ 后缀、bot_data reply 不变、删 :fb 消息、记 WARNING"""
        key = _seedReviewEntry(mockContext.bot_data, reply="旧回复")
        message = _makeFbMessage()

        retryItem = {"reply": "   ", "memoryFailedCount": 0}

        with patch("handlers.llmReview.hasPermission", return_value=True), \
             patch("handlers.llmReview.reviewRetryWithFeedback", new_callable=AsyncMock, return_value=retryItem), \
             patch("handlers.llmReview.logSystemEvent", new_callable=AsyncMock) as mockSysLog, \
             patch("handlers.llmReview.logAction", new_callable=AsyncMock):
            handled = await handleFeedbackRetry(message, mockContext)

        assert handled is True
        # 卡片编辑两次：中间态（正在生成）+ 兜底恢复（旧 reply + ⚠️ 提示）
        assert mockContext.bot.edit_message_text.await_count == 2
        editKwargs = mockContext.bot.edit_message_text.await_args.kwargs
        assert "旧回复" in editKwargs["text"]
        assert "重新生成是空的喵" in editKwargs["text"]
        assert editKwargs["reply_markup"] is not None   # 恢复了按钮
        # bot_data 的 reply 不被空串覆盖
        assert mockContext.bot_data[key]["reply"] == "旧回复"
        # :fb 消息已删除（标签已消费）
        message.delete.assert_awaited_once()
        # 记 WARNING 日志
        assert mockSysLog.await_count == 1
        assert "生成为空" in mockSysLog.await_args.args[0]

    async def test_nonempty_reply_updates_normally(self, mockContext):
        """对照组：生成非空 → 正常写回新 reply（不触发兜底）"""
        key = _seedReviewEntry(mockContext.bot_data, reply="旧回复")
        message = _makeFbMessage()

        retryItem = {"reply": "新回复喵", "memoryFailedCount": 0}

        with patch("handlers.llmReview.hasPermission", return_value=True), \
             patch("handlers.llmReview.reviewRetryWithFeedback", new_callable=AsyncMock, return_value=retryItem), \
             patch("handlers.llmReview.logSystemEvent", new_callable=AsyncMock) as mockSysLog, \
             patch("handlers.llmReview.logAction", new_callable=AsyncMock):
            handled = await handleFeedbackRetry(message, mockContext)

        assert handled is True
        assert mockContext.bot_data[key]["reply"] == "新回复喵"
        message.delete.assert_awaited_once()
        mockSysLog.assert_not_awaited()



class TestReviewCallbackRetryEmptyGeneration:

    async def test_empty_reply_restores_card_keeps_bot_data(self, mockContext, mockUpdate):
        """retry 按钮成功但生成为空 → 恢复旧卡片 + ⚠️ 后缀、bot_data reply 不变"""
        key = _seedReviewEntry(mockContext.bot_data, reply="旧回复")
        query = _makeRetryQuery("retry")
        mockUpdate.callback_query = query

        retryItem = {"reply": "", "memoryFailedCount": 0}

        with patch("handlers.llmReview.hasPermission", return_value=True), \
             patch("handlers.llmReview.reviewRetry", new_callable=AsyncMock, return_value=retryItem), \
             patch("handlers.llmReview.logSystemEvent", new_callable=AsyncMock) as mockSysLog, \
             patch("handlers.llmReview.logAction", new_callable=AsyncMock):
            await handleReviewCallback(mockUpdate, mockContext)

        # 卡片经 safeEditMessage（message.edit_text）恢复：旧 reply + ⚠️ 提示
        # （text 为位置参数，其余为 kwargs）
        editCall = query.message.edit_text.await_args
        assert "旧回复" in editCall.args[0]
        assert "重新生成是空的喵" in editCall.args[0]
        assert editCall.kwargs.get("reply_markup") is not None
        # bot_data 的 reply 不被空串覆盖
        assert mockContext.bot_data[key]["reply"] == "旧回复"
        assert mockSysLog.await_count == 1

    async def test_nonempty_reply_updates_normally(self, mockContext, mockUpdate):
        """对照组：生成非空 → 正常写回新 reply"""
        key = _seedReviewEntry(mockContext.bot_data, reply="旧回复")
        query = _makeRetryQuery("retry")
        mockUpdate.callback_query = query

        retryItem = {"reply": "新回复喵", "memoryFailedCount": 0}

        with patch("handlers.llmReview.hasPermission", return_value=True), \
             patch("handlers.llmReview.reviewRetry", new_callable=AsyncMock, return_value=retryItem), \
             patch("handlers.llmReview.logSystemEvent", new_callable=AsyncMock) as mockSysLog, \
             patch("handlers.llmReview.logAction", new_callable=AsyncMock):
            await handleReviewCallback(mockUpdate, mockContext)

        assert mockContext.bot_data[key]["reply"] == "新回复喵"
        mockSysLog.assert_not_awaited()
