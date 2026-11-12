"""
tests/handlers/test_llmReview.py

handleFeedbackRetry / handleReviewCallback 的重试测试：

    - :fb 成功但生成为空 → 恢复旧 reply 的卡片 + ⚠️ 后缀、bot_data 的 reply 不被
      空串覆盖、:fb 消息删除、记 WARNING 日志
    - retry 按钮成功但生成为空 → 同款断言（卡片恢复 + 不写回）
    - 重试 / :fb 经真实的 reviewRetry / reviewRetryWithFeedback 复用审核项保存的
      原始请求，新产生的记忆操作逐个交给这张卡的审核人，且不走自动批准
    - off 模式送审（deliverMessage 发卡）存下的审核项带着原始请求，之后的重试用的就是它

背景：思考模型经中转可能思考吃光 max_tokens 预算，响应无 text block → 空串
（provider 层已加截断提额重试兜底；此处覆盖的是「重试也救不回、生成结果为空」
的最后一道展示层兜底）。原同名测试文件在 00a4ddb 重构时删除，本文件为恢复性重建。

构造要点（与 handlers/llmReview.py 的门禁对应）：
    - hasPermission patch 到 handlers.llmReview.hasPermission 返回 True
    - bot_data 预置 llm_editidx_{msgID} → llm_review_{chatID}_{msgID} 反向索引
    - 审核条目 opsID 与发送者 id 一致（conftest mockUser.id = 123456789）
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

from handlers.llmReview import (
    handleFeedbackRetry,
    handleMemoryReviewCallback,
    handleReviewCallback,
)
from utils.llm.capability.message import deliverMessage
from utils.llm.memory.action import MemoryAction, MemoryActionContext
from utils.llm.memory.types import MemoryQuery, MemoryTurn
from utils.llm.models import CollectedOutput, MessageInput, MessageOrigin, TaskRequest
from utils.llm.operation.types import OperationStatus


_OPS_ID = "123456789"
_CHAT_ID = 987654321
_REVIEW_MSG_ID = 500
_TRIGGER_USER_ID = 42


def _seedReviewEntry(
    bot_data: dict,
    *,
    reply: str = "旧回复",
    originalMsg: str = "原文",
    memoryQuery=None,
    request=None,
    autoMode: str = "off",
):
    """预置一条回复审核条目 + 反向索引（与 putReplyReview 同构）"""
    key = f"llm_review_{_CHAT_ID}_{_REVIEW_MSG_ID}"
    bot_data[key] = {
        "reply": reply,
        "originalMsg": originalMsg,
        "chatID": _CHAT_ID,
        "opsID": _OPS_ID,
        "triggerMsgID": 100,
        "userID": 123456789,
        "includeContext": True,
        "autoMode": autoMode,
        "displayBlocks": None,
        "memoryQuery": memoryQuery,
        "request": request,
        "createdAt": 9999999999.0,   # 远期时间戳，防 TTL 清理误删
    }
    bot_data[f"llm_editidx_{_REVIEW_MSG_ID}"] = key
    return key


def _makeRequest(*, includeContext: bool = True) -> TaskRequest:
    """审核项里保存的原始生成请求。"""
    return TaskRequest(
        messages=(MessageInput(
            origin=MessageOrigin(
                chatID=str(_CHAT_ID), userID=_TRIGGER_USER_ID, messageID=100, sender="alice",
            ),
            text="问题",
            includeContext=includeContext,
            memoryTurn=MemoryTurn(currentText="问题"),
        ),),
        includeContext=includeContext,
    )


@contextmanager
def _realRetryChain(generated: CollectedOutput):
    """
    重试走真实的 reviewRetry / reviewRetryWithFeedback 与 Telegram 记忆分发闭包；
    只替换模型生成、记忆操作的最终提交和日志。
    """
    with (
        patch("handlers.llmReview.hasPermission", return_value=True),
        patch("utils.llm.review.actions.collectOutput", new_callable=AsyncMock, return_value=generated) as mockGenerate,
        patch("handlers.llmReview.submitMemoryAction", new_callable=AsyncMock) as mockSubmit,
        patch("utils.llm.review.actions.logAction", new_callable=AsyncMock),
        patch("handlers.llmReview.logSystemEvent", new_callable=AsyncMock),
        patch("handlers.llmReview.logAction", new_callable=AsyncMock),
    ):
        yield mockGenerate, mockSubmit


@contextmanager
def _quietDelivery():
    """deliverMessage 走真实的发卡与存审核项，只隔离它的日志。"""
    with (
        patch("utils.llm.capability.message.logAction", new_callable=AsyncMock),
        patch("utils.llm.capability.message.logSystemEvent", new_callable=AsyncMock),
    ):
        yield


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


def _seedMemoryReviewEntry(bot_data: dict, *, targetState="old-state"):
    key = f"llm_memreview_{_CHAT_ID}_{_REVIEW_MSG_ID}"
    bot_data[key] = {
        "action": {
            "action": "update",
            "scopeType": "user",
            "scopeID": _OPS_ID,
            "memoryID": 7,
            "content": "拟议事实",
            "targetState": targetState,
        },
        "originalMsg": "触发消息",
        "chatID": _CHAT_ID,
        "opsID": _OPS_ID,
        "userID": _OPS_ID,
        "displayBlocks": None,
        "createdAt": 9999999999.0,
    }
    bot_data[f"llm_editidx_{_REVIEW_MSG_ID}"] = key
    return key


def _makeMemoryReviewQuery(action: str):
    query = MagicMock()
    query.data = f"llm:memreview:{action}:{_CHAT_ID}"
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

        retryItem = {"reply": "   "}

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
        oldQuery = MemoryQuery(turns=(MemoryTurn(currentText="旧问题"),))
        newQuery = MemoryQuery(
            turns=oldQuery.turns,
            feedbackText="收敛一点",
        )
        key = _seedReviewEntry(
            mockContext.bot_data,
            reply="旧回复",
            memoryQuery=oldQuery,
        )
        message = _makeFbMessage()

        retryItem = {
            "reply": "新回复喵",
            "memoryQuery": newQuery,
        }

        with patch("handlers.llmReview.hasPermission", return_value=True), \
             patch("handlers.llmReview.reviewRetryWithFeedback", new_callable=AsyncMock, return_value=retryItem), \
             patch("handlers.llmReview.logSystemEvent", new_callable=AsyncMock) as mockSysLog, \
             patch("handlers.llmReview.logAction", new_callable=AsyncMock):
            handled = await handleFeedbackRetry(message, mockContext)

        assert handled is True
        assert mockContext.bot_data[key]["reply"] == "新回复喵"
        assert mockContext.bot_data[key]["memoryQuery"] is newQuery
        message.delete.assert_awaited_once()
        mockSysLog.assert_not_awaited()


class TestReviewCallbackRetryEmptyGeneration:

    async def test_empty_reply_restores_card_keeps_bot_data(self, mockContext, mockUpdate):
        """retry 按钮成功但生成为空 → 恢复旧卡片 + ⚠️ 后缀、bot_data reply 不变"""
        key = _seedReviewEntry(mockContext.bot_data, reply="旧回复")
        query = _makeRetryQuery("retry")
        mockUpdate.callback_query = query

        retryItem = {"reply": ""}

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
        oldQuery = MemoryQuery(turns=(MemoryTurn(currentText="问题"),))
        key = _seedReviewEntry(
            mockContext.bot_data,
            reply="旧回复",
            memoryQuery=oldQuery,
        )
        query = _makeRetryQuery("retry")
        mockUpdate.callback_query = query

        retryItem = {
            "reply": "新回复喵",
            "memoryQuery": oldQuery,
        }

        with patch("handlers.llmReview.hasPermission", return_value=True), \
             patch("handlers.llmReview.reviewRetry", new_callable=AsyncMock, return_value=retryItem), \
             patch("handlers.llmReview.logSystemEvent", new_callable=AsyncMock) as mockSysLog, \
             patch("handlers.llmReview.logAction", new_callable=AsyncMock):
            await handleReviewCallback(mockUpdate, mockContext)

        assert mockContext.bot_data[key]["reply"] == "新回复喵"
        assert mockContext.bot_data[key]["memoryQuery"] is oldQuery
        mockSysLog.assert_not_awaited()


class TestRetryThroughReviewChain:
    """重试按钮与 :fb 不替换 reviewRetry / reviewRetryWithFeedback，检查它们收到和传出的内容。"""

    async def test_retry_button_reuses_request_and_dispatches_memory_to_reviewer(self, mockContext, mockUpdate):
        request = _makeRequest()
        key = _seedReviewEntry(mockContext.bot_data, request=request, autoMode="off")
        query = _makeRetryQuery("retry")
        mockUpdate.callback_query = query
        actions = [
            MemoryAction(action="add", scopeType="global", scopeID="global", content="喜欢乌龙茶"),
            MemoryAction(action="add", scopeType="global", scopeID="global", content="不喝咖啡"),
        ]
        generated = CollectedOutput(reply="新回复喵", memoryActions=actions)

        with _realRetryChain(generated) as (mockGenerate, mockSubmit):
            await handleReviewCallback(mockUpdate, mockContext)

        mockGenerate.assert_awaited_once_with(request, feedback="", logLabel="retry")
        # 记忆操作逐个提交，每次只带一个 action，顺序与生成结果一致
        assert [call.args for call in mockSubmit.await_args_list] == [(action,) for action in actions]
        for call in mockSubmit.await_args_list:
            assert call.kwargs["bot"] is mockContext.bot
            assert call.kwargs["botData"] is mockContext.bot_data
            assert call.kwargs["autoMode"] == "off"
            assert call.kwargs["opsList"] == [_OPS_ID]
            assert call.kwargs["chatID"] == str(_CHAT_ID)
            assert call.kwargs["userID"] == _TRIGGER_USER_ID
            assert call.kwargs["originalMsg"] == "原文"
            assert call.kwargs["respectAutoApprove"] is False
        assert mockContext.bot_data[key]["reply"] == "新回复喵"
        assert mockContext.bot_data[key]["memoryQuery"] == request.memoryQuery
        assert mockContext.bot_data[key]["request"] is request
        assert "新回复喵" in query.message.edit_text.await_args.args[0]

    async def test_feedback_retry_passes_feedback_without_changing_request(self, mockContext):
        request = _makeRequest()
        key = _seedReviewEntry(mockContext.bot_data, request=request, autoMode="console")
        message = _makeFbMessage(":fb 收敛一点")
        action = MemoryAction(action="add", scopeType="global", scopeID="global", content="喜欢乌龙茶")
        generated = CollectedOutput(reply="收敛后的回复", memoryActions=[action])

        with _realRetryChain(generated) as (mockGenerate, mockSubmit):
            handled = await handleFeedbackRetry(message, mockContext)

        assert handled is True
        mockGenerate.assert_awaited_once_with(request, feedback="收敛一点", logLabel="feedback retry")
        mockSubmit.assert_awaited_once()
        assert mockSubmit.await_args.args == (action,)
        assert mockSubmit.await_args.kwargs["autoMode"] == "console"
        assert mockSubmit.await_args.kwargs["opsList"] == [_OPS_ID]
        assert mockSubmit.await_args.kwargs["respectAutoApprove"] is False
        assert mockContext.bot_data[key]["reply"] == "收敛后的回复"
        assert mockContext.bot_data[key]["request"] is request
        assert mockContext.bot_data[key]["memoryQuery"] == request.memoryQuery
        message.delete.assert_awaited_once()

    async def test_retry_without_context_does_not_dispatch_memory(self, mockContext, mockUpdate):
        request = _makeRequest(includeContext=False)
        key = _seedReviewEntry(mockContext.bot_data, request=request)
        mockUpdate.callback_query = _makeRetryQuery("retry")
        generated = CollectedOutput(reply="新回复喵", memoryActions=[])

        with _realRetryChain(generated) as (_, mockSubmit):
            await handleReviewCallback(mockUpdate, mockContext)

        mockSubmit.assert_not_awaited()
        assert mockContext.bot_data[key]["reply"] == "新回复喵"

    async def test_retry_without_saved_request_restores_card(self, mockContext, mockUpdate):
        """审核项没有保存原始请求时不调用模型，卡片带着旧回复和按钮恢复。"""
        key = _seedReviewEntry(mockContext.bot_data, reply="旧回复", request=None)
        query = _makeRetryQuery("retry")
        mockUpdate.callback_query = query

        with _realRetryChain(CollectedOutput(reply="不应出现")) as (mockGenerate, mockSubmit):
            await handleReviewCallback(mockUpdate, mockContext)

        mockGenerate.assert_not_awaited()
        mockSubmit.assert_not_awaited()
        editCall = query.message.edit_text.await_args
        assert "旧回复" in editCall.args[0]
        assert "无法重试" in editCall.args[0]
        assert editCall.kwargs.get("reply_markup") is not None
        assert mockContext.bot_data[key]["reply"] == "旧回复"

    async def test_feedback_generation_error_restores_card_and_keeps_tag_message(self, mockContext):
        """生成抛出非 ValueError 时卡片恢复并标注失败；:fb 消息留着，审核人可以直接再发一次。"""
        key = _seedReviewEntry(mockContext.bot_data, reply="旧回复", request=_makeRequest())
        message = _makeFbMessage()

        with _realRetryChain(CollectedOutput(reply="不应出现")) as (mockGenerate, mockSubmit):
            mockGenerate.side_effect = RuntimeError("模型请求失败")
            handled = await handleFeedbackRetry(message, mockContext)

        assert handled is True
        mockSubmit.assert_not_awaited()
        editKwargs = mockContext.bot.edit_message_text.await_args.kwargs
        assert "旧回复" in editKwargs["text"]
        assert "生成失败喵" in editKwargs["text"]
        assert editKwargs["reply_markup"] is not None
        assert mockContext.bot_data[key]["reply"] == "旧回复"
        message.delete.assert_not_awaited()


class TestInitialReviewMemoryQuery:
    """off 模式送审：审核项由 deliverMessage 真实发卡时写进 bot_data。"""

    async def test_send_review_message_saves_memory_query(self):
        memoryQuery = MemoryQuery(
            turns=(MemoryTurn(currentText="当前", replyText="引用"),),
        )
        bot = AsyncMock()
        bot.send_message.return_value = SimpleNamespace(message_id=321)
        botData = {}
        request = TaskRequest(messages=(MessageInput(
            origin=MessageOrigin(chatID="456", userID=42, messageID=9),
            text="当前", memoryTurn=memoryQuery.turns[0],
        ),), includeContext=True)

        with _quietDelivery():
            result = await deliverMessage(
                "回复", request=request, bot=bot, botData=botData, autoMode="off", opsList=[123],
            )

        key = "llm_review_456_321"
        assert result.status == OperationStatus.SUBMITTED
        assert bot.send_message.await_args.kwargs["chat_id"] == 123
        # memoryQuery 取自 request.memoryQuery，每次读取都是新对象，按值比较
        assert botData[key]["memoryQuery"] == memoryQuery
        assert botData[key]["request"] is request
        assert botData[key]["reply"] == "回复"
        assert botData[key]["autoMode"] == "off"
        assert botData["llm_editidx_321"] == key

    async def test_retry_button_reuses_request_saved_by_review_card(self, mockContext, mockUpdate):
        """发卡时存下的审核项不经改动交给重试按钮：重新生成用的是同一个原始请求。"""
        request = _makeRequest()
        mockContext.bot.send_message.return_value = SimpleNamespace(message_id=_REVIEW_MSG_ID)

        with _quietDelivery():
            await deliverMessage(
                "旧回复", request=request, bot=mockContext.bot, botData=mockContext.bot_data,
                autoMode="off", opsList=[_OPS_ID],
            )

        key = f"llm_review_{_CHAT_ID}_{_REVIEW_MSG_ID}"
        assert mockContext.bot_data[key]["request"] is request
        query = _makeRetryQuery("retry")
        mockUpdate.callback_query = query

        with _realRetryChain(CollectedOutput(reply="新回复喵", memoryActions=[])) as (mockGenerate, _):
            await handleReviewCallback(mockUpdate, mockContext)

        mockGenerate.assert_awaited_once_with(request, feedback="", logLabel="retry")
        assert mockContext.bot_data[key]["reply"] == "新回复喵"
        assert mockContext.bot_data[key]["request"] is request
        assert "新回复喵" in query.message.edit_text.await_args.args[0]


class TestMemoryReviewConflict:

    async def test_failed_approval_refreshes_and_keeps_review(self, mockContext, mockUpdate):
        key = _seedMemoryReviewEntry(mockContext.bot_data)
        query = _makeMemoryReviewQuery("approve")
        mockUpdate.callback_query = query
        refreshedAction = {
            **mockContext.bot_data[key]["action"],
            "originalContent": "当前事实",
            "targetState": "new-state",
        }

        with (
            patch("handlers.llmReview.hasPermission", return_value=True),
            patch(
                "handlers.llmReview.executeAction",
                new_callable=AsyncMock,
                return_value=False,
            ) as mockExecute,
            patch(
                "handlers.llmReview.refreshMemoryReviewItem",
                new_callable=AsyncMock,
                return_value=True,
            ) as mockRefresh,
            patch("handlers.llmReview.logAction", new_callable=AsyncMock),
        ):
            async def _refresh(reviewData):
                reviewData["action"] = refreshedAction
                return True

            mockRefresh.side_effect = _refresh
            await handleMemoryReviewCallback(mockUpdate, mockContext)

        assert mockExecute.await_args.kwargs["actionContext"] == MemoryActionContext(
            chatID=_CHAT_ID,
            userID=_OPS_ID,
        )

        assert key in mockContext.bot_data
        assert mockContext.bot_data[key]["action"]["targetState"] == "new-state"
        assert f"llm_editidx_{_REVIEW_MSG_ID}" in mockContext.bot_data
        editCall = query.message.edit_text.await_args
        assert "再次批准" in editCall.args[0]
        assert editCall.kwargs.get("reply_markup") is not None

    async def test_successful_approval_removes_review(self, mockContext, mockUpdate):
        key = _seedMemoryReviewEntry(mockContext.bot_data, targetState="current-state")
        query = _makeMemoryReviewQuery("approve")
        mockUpdate.callback_query = query

        with (
            patch("handlers.llmReview.hasPermission", return_value=True),
            patch(
                "handlers.llmReview.executeAction",
                new_callable=AsyncMock,
                return_value=True,
            ) as mockExecute,
            patch("handlers.llmReview.logAction", new_callable=AsyncMock),
        ):
            await handleMemoryReviewCallback(mockUpdate, mockContext)

        assert mockExecute.await_args.kwargs["actionContext"] == MemoryActionContext(
            chatID=_CHAT_ID,
            userID=_OPS_ID,
        )

        assert key not in mockContext.bot_data
        assert f"llm_editidx_{_REVIEW_MSG_ID}" not in mockContext.bot_data
        assert "已批准 · 成功" in query.message.edit_text.await_args.args[0]
