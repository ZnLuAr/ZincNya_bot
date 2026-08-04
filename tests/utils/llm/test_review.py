"""
tests/utils/llm/test_review.py

测试 LLM 审核共享操作（utils/llm/review.py）：
    - extractValidatedMemoryActions：解析 + 截断 + 逐个校验编排
    - queueMemoryActionsToConsole：转发到 console 审核队列
    - reviewRetryWithFeedback：补充反馈拼接 enhancedMsg 并重试

约定：
    - parseMemoryActions / validateAction 用真实实现（add 路径不查 DB）
    - logSystemEvent / logAction / generateReply 用 AsyncMock 隔离副作用
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import LLM_REVIEW_FEEDBACK_MAX_LENGTH

from utils.llm.memory.action import MemoryAction, LLM_MEMORY_MAX_ACTIONS
from utils.llm.review import (
    dispatchMemoryActions,
    dispatchMemoryActionsToConsole,
    extractValidatedMemoryActions,
    queueMemoryActionsToConsole,
    reviewRetry,
    reviewRetryWithFeedback,
)


# ===========================================================================
# 测试数据构造
# ===========================================================================

def _addBlock(content: str = "测试内容") -> str:
    """构造一个语法合法、校验通过的 add 记忆块。"""
    return (
        '<MEMORY_ACTION>'
        f'{{"action":"add","scope_type":"global","scope_id":"global","content":"{content}"}}'
        '</MEMORY_ACTION>'
    )


def _invalidActionBlock() -> str:
    """构造一个 JSON 合法、但 action 类型非法（校验失败）的块。

    parseMemoryActions 不校验 action 合法性，会成功解析；
    validateAction 才返回 "不支持的 action"。
    """
    return (
        '<MEMORY_ACTION>'
        '{"action":"frobnicate","scope_type":"global","scope_id":"global","content":"X"}'
        '</MEMORY_ACTION>'
    )


# ===========================================================================
# extractValidatedMemoryActions
# ===========================================================================

class TestExtractValidatedMemoryActions:
    """extractValidatedMemoryActions：解析 + 截断 + 校验编排"""

    @patch("utils.llm.review.logSystemEvent", new_callable=AsyncMock)
    async def test_no_memory_block(self, mockLog):
        """无记忆块：返回原文本、空列表、0 失败，不记日志"""
        reply = "这是一条没有任何记忆操作的普通回复"
        cleaned, validated, failed = await extractValidatedMemoryActions(reply, logLabel="test")

        assert cleaned == reply
        assert validated == []
        assert failed == 0
        mockLog.assert_not_called()

    @patch("utils.llm.review.logSystemEvent", new_callable=AsyncMock)
    async def test_single_valid_action(self, mockLog):
        """单个合法 add：块被剥离、validated 含 1 项、0 失败"""
        reply = f"回复正文\n\n{_addBlock('记住这件事')}"
        cleaned, validated, failed = await extractValidatedMemoryActions(reply, logLabel="test")

        assert "<MEMORY_ACTION>" not in cleaned
        assert cleaned.strip() == "回复正文"
        assert len(validated) == 1
        assert validated[0].action == "add"
        assert validated[0].content == "记住这件事"
        assert failed == 0

    @patch("utils.llm.review.logSystemEvent", new_callable=AsyncMock)
    async def test_validation_failure_counted(self, mockLog):
        """校验失败：failed=1、validated 不含该项、记录失败日志"""
        reply = f"回复\n\n{_invalidActionBlock()}"
        cleaned, validated, failed = await extractValidatedMemoryActions(reply, logLabel="test")

        assert "<MEMORY_ACTION>" not in cleaned
        assert validated == []
        assert failed == 1
        # 至少有一次校验失败日志
        assert mockLog.await_count >= 1

    @patch("utils.llm.review.logSystemEvent", new_callable=AsyncMock)
    async def test_valid_and_invalid_mixed(self, mockLog):
        """合法 + 非法混合：validated 只留合法项，failed 计数正确"""
        reply = f"回复\n\n{_addBlock('好的记忆')}\n\n{_invalidActionBlock()}"
        cleaned, validated, failed = await extractValidatedMemoryActions(reply, logLabel="test")

        assert len(validated) == 1
        assert validated[0].content == "好的记忆"
        assert failed == 1

    @patch("utils.llm.review.logSystemEvent", new_callable=AsyncMock)
    async def test_truncation_over_limit(self, mockLog):
        """超过 LLM_MEMORY_MAX_ACTIONS：截断到上限，并记录超限日志"""
        # 构造 上限+2 个合法块
        blocks = "\n\n".join(_addBlock(f"内容{i}") for i in range(LLM_MEMORY_MAX_ACTIONS + 2))
        reply = f"回复\n\n{blocks}"
        cleaned, validated, failed = await extractValidatedMemoryActions(reply, logLabel="test")

        assert len(validated) == LLM_MEMORY_MAX_ACTIONS
        # 截断日志被触发（首条 await 即为超限告警）
        assert mockLog.await_count >= 1
        firstCallEvent = mockLog.await_args_list[0].args[0]
        assert "超限" in firstCallEvent

    @patch("utils.llm.review.logSystemEvent", new_callable=AsyncMock)
    async def test_loglabel_propagated(self, mockLog):
        """logLabel 透传到日志事件文案"""
        reply = f"回复\n\n{_invalidActionBlock()}"
        await extractValidatedMemoryActions(reply, logLabel="feedback retry")

        # 校验失败日志的 event 文案应包含 logLabel
        events = [c.args[0] for c in mockLog.await_args_list]
        assert any("feedback retry" in e for e in events)


# ===========================================================================
# queueMemoryActionsToConsole
# ===========================================================================

class TestQueueMemoryActionsToConsole:
    """queueMemoryActionsToConsole：转发到 console 审核队列"""

    @patch("utils.llm.review.addMemoryReviewItem")
    def test_empty_list_no_call(self, mockAdd):
        """空列表：不调用 addMemoryReviewItem"""
        queueMemoryActionsToConsole(
            [], chatID="123", originalMsg="msg", opsID="456", userID="789",
        )
        mockAdd.assert_not_called()

    @patch("utils.llm.review.addMemoryReviewItem")
    def test_multiple_actions_forwarded(self, mockAdd):
        """多个 action：逐个转发，参数正确"""
        actions = [
            MemoryAction(action="add", scopeType="global", scopeID="global", content="A"),
            MemoryAction(action="add", scopeType="global", scopeID="global", content="B"),
        ]
        queueMemoryActionsToConsole(
            actions, chatID="123", originalMsg="原始消息", opsID="456", userID="789",
        )

        assert mockAdd.call_count == 2
        firstKwargs = mockAdd.call_args_list[0].kwargs
        assert firstKwargs["chatID"] == "123"
        assert firstKwargs["originalMsg"] == "原始消息"
        assert firstKwargs["opsID"] == "456"
        assert firstKwargs["userID"] == "789"
        # action 以 dict 形式传入
        assert firstKwargs["action"]["content"] == "A"


# ===========================================================================
# reviewRetryWithFeedback
# ===========================================================================

class TestReviewRetryWithFeedback:
    """reviewRetryWithFeedback：补充反馈拼接 enhancedMsg 并重试"""

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    @patch("utils.llm.review.generateReply", new_callable=AsyncMock)
    async def test_enhanced_message_format(self, mockGenerate, mockLogAction):
        """enhancedMsg 含 [背景信息补充：...] 且保留 originalMsg"""
        mockGenerate.return_value = "新回复"
        item = {
            "originalMsg": "用户原始消息",
            "chatID": "123",
            "includeContext": False,
        }
        await reviewRetryWithFeedback(item, "假设用户预算 1000 元")

        # generateReply 的第一个位置参数即 enhancedMsg
        enhancedMsg = mockGenerate.await_args.args[0]
        assert "用户原始消息" in enhancedMsg
        assert "[背景信息补充：假设用户预算 1000 元]" in enhancedMsg

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    @patch("utils.llm.review.generateReply", new_callable=AsyncMock)
    async def test_feedback_over_limit_raises(self, mockGenerate, mockLogAction):
        """反馈超 LLM_REVIEW_FEEDBACK_MAX_LENGTH：抛 ValueError，不调 generateReply"""
        mockGenerate.return_value = "新回复"
        longFeedback = "字" * (LLM_REVIEW_FEEDBACK_MAX_LENGTH + 1)
        item = {"originalMsg": "msg", "chatID": "123", "includeContext": False}

        with pytest.raises(ValueError) as excInfo:
            await reviewRetryWithFeedback(item, longFeedback)

        assert "反馈过长" in str(excInfo.value)
        # 长度检查在 generateReply 之前，不应实际调用
        mockGenerate.assert_not_called()

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    @patch("utils.llm.review.generateReply", new_callable=AsyncMock)
    async def test_returns_updated_reply_preserves_original(self, mockGenerate, mockLogAction):
        """返回 {**item, reply}，originalMsg 不变（反馈一次性语义）"""
        mockGenerate.return_value = "重新生成的回复"
        item = {
            "originalMsg": "用户原始消息",
            "chatID": "123",
            "includeContext": False,
            "userID": "u1",
        }
        result = await reviewRetryWithFeedback(item, "补充信息")

        assert result["reply"] == "重新生成的回复"
        # originalMsg 保持干净，不含补充标记
        assert result["originalMsg"] == "用户原始消息"
        assert "背景信息补充" not in result["originalMsg"]
        # 其他字段透传
        assert result["userID"] == "u1"
        # memoryFailedCount 显式写回（includeContext=False 时为 0）
        assert result.get("memoryFailedCount") == 0

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    @patch("utils.llm.review.generateReply", new_callable=AsyncMock)
    async def test_no_memory_processing_when_context_off(self, mockGenerate, mockLogAction):
        """includeContext=False：不调 memoryDispatcher"""
        spy = AsyncMock()
        mockGenerate.return_value = "回复"
        item = {"originalMsg": "msg", "chatID": "123", "includeContext": False}

        await reviewRetryWithFeedback(item, "补充", memoryDispatcher=spy)

        spy.assert_not_called()

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    @patch("utils.llm.review.generateReply", new_callable=AsyncMock)
    async def test_memory_processing_when_context_on(self, mockGenerate, mockLogAction):
        """includeContext=True 且生成含记忆块：剥离块并调 memoryDispatcher 传校验通过的操作"""
        spy = AsyncMock()
        mockGenerate.return_value = f"回复正文\n\n{_addBlock('记住')}"
        item = {"originalMsg": "msg", "chatID": "123", "includeContext": True, "opsID": "o1"}

        result = await reviewRetryWithFeedback(item, "补充", memoryDispatcher=spy)

        # reply 已剥离记忆块
        assert "<MEMORY_ACTION>" not in result["reply"]
        spy.assert_awaited_once()
        # dispatcher 收到校验通过的操作列表（1 个 add）
        dispatchedActions = spy.await_args.args[0]
        assert len(dispatchedActions) == 1
        assert dispatchedActions[0].action == "add"
        # logLabel 透传
        assert spy.await_args.kwargs["logLabel"] == "feedback retry"

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    @patch("utils.llm.review.generateReply", new_callable=AsyncMock)
    async def test_feedback_retry_injects_custom_dispatcher(self, mockGenerate, mockLogAction):
        """feedback 透传自定义 memoryDispatcher 与 logLabel"""
        spy = AsyncMock()
        mockGenerate.return_value = f"回复\n\n{_addBlock('记住')}"
        item = {"originalMsg": "msg", "chatID": "123", "includeContext": True, "opsID": "o1"}

        await reviewRetryWithFeedback(item, "补充", memoryDispatcher=spy, logLabel="custom")

        spy.assert_awaited_once()
        assert spy.await_args.kwargs["logLabel"] == "custom"

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    @patch("utils.llm.review.generateReply", new_callable=AsyncMock)
    async def test_feedback_within_limit_succeeds(self, mockGenerate, mockLogAction):
        """反馈长度等于上限：正常生成不抛异常"""
        mockGenerate.return_value = "新回复"
        item = {"originalMsg": "msg", "chatID": "123", "includeContext": False}
        feedback = "字" * LLM_REVIEW_FEEDBACK_MAX_LENGTH  # 恰好等于上限，不触发 raise

        result = await reviewRetryWithFeedback(item, feedback)

        assert result["reply"] == "新回复"
        mockGenerate.assert_awaited_once()




# ===========================================================================
# reviewRetry
# ===========================================================================

class TestReviewRetry:
    """reviewRetry：透传 memoryDispatcher 与 logLabel，写回 memoryFailedCount"""

    def test_retry_default_dispatcher_is_console(self):
        """默认 memoryDispatcher=dispatchMemoryActionsToConsole, logLabel='console retry'"""
        assert reviewRetry.__kwdefaults__["memoryDispatcher"] is dispatchMemoryActionsToConsole
        assert reviewRetry.__kwdefaults__["logLabel"] == "console retry"

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    @patch("utils.llm.review.generateReply", new_callable=AsyncMock)
    async def test_retry_reply_replaced_and_originalmsg_preserved(self, mockGenerate, mockLogAction):
        """retry 替换 reply，保留 originalMsg，写回 memoryFailedCount=0"""
        mockGenerate.return_value = "新回复"
        item = {"originalMsg": "原文", "chatID": "123", "includeContext": False}

        result = await reviewRetry(item)

        assert result["reply"] == "新回复"
        assert result["originalMsg"] == "原文"
        assert result.get("memoryFailedCount") == 0

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    @patch("utils.llm.review.generateReply", new_callable=AsyncMock)
    async def test_retry_writes_failed_count_to_item(self, mockGenerate, mockLogAction):
        """includeContext=True 含非法块：failedCount 写回 memoryFailedCount，非法项不传 dispatcher"""
        spy = AsyncMock()
        mockGenerate.return_value = f"回复\n\n{_invalidActionBlock()}"
        item = {"originalMsg": "msg", "chatID": "123", "includeContext": True, "opsID": "o1"}

        result = await reviewRetry(item, memoryDispatcher=spy)

        assert result["memoryFailedCount"] == 1
        # 非法操作被丢弃，dispatcher 收到空列表
        assert spy.await_args.args[0] == []

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    @patch("utils.llm.review.generateReply", new_callable=AsyncMock)
    async def test_retry_injects_custom_dispatcher(self, mockGenerate, mockLogAction):
        """retry 透传自定义 memoryDispatcher 与 logLabel"""
        spy = AsyncMock()
        mockGenerate.return_value = f"回复\n\n{_addBlock('记住')}"
        item = {"originalMsg": "msg", "chatID": "123", "includeContext": True, "opsID": "o1"}

        await reviewRetry(item, memoryDispatcher=spy, logLabel="custom")

        spy.assert_awaited_once()
        assert spy.await_args.kwargs["logLabel"] == "custom"

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    @patch("utils.llm.review.generateReply", new_callable=AsyncMock)
    async def test_consecutive_retry_resets_failed_count(self, mockGenerate, mockLogAction):
        """连续 retry：第二次干净回复时 memoryFailedCount 重置为 0（不残留前次）"""
        spy = AsyncMock()
        # 第一次生成含非法块（failed=1）
        mockGenerate.return_value = f"回复\n\n{_invalidActionBlock()}"
        item = {"originalMsg": "msg", "chatID": "123", "includeContext": True, "opsID": "o1"}

        first = await reviewRetry(item, memoryDispatcher=spy)
        assert first["memoryFailedCount"] == 1

        # 第二次生成干净回复（failed=0，不残留 1）
        mockGenerate.return_value = "干净回复"
        second = await reviewRetry(first, memoryDispatcher=spy)
        assert second["memoryFailedCount"] == 0




# ===========================================================================
# dispatchMemoryActions
# ===========================================================================

class TestDispatchMemoryActions:
    """dispatchMemoryActions：autoMode 总分流入口（首生成 + retry/feedback 共用）"""

    @patch("utils.llm.review.logSystemEvent", new_callable=AsyncMock)
    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    async def test_empty_actions_early_return(self, mockLogAction, mockLogEvent):
        """空 actions：直接返回，不调任何下游"""
        spy = AsyncMock()
        await dispatchMemoryActions(
            [], autoMode="on", opsList=["1"], chatID=1,
            originalMsg="msg", userID=1, sendTGMemoryReview=spy,
        )
        spy.assert_not_called()
        mockLogEvent.assert_not_called()

    @patch("utils.llm.review.executeAction", new_callable=AsyncMock)
    @patch("utils.llm.review.getMemoryAutoApprove", return_value=True)
    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    async def test_autoapprove_short_circuits(self, mockLogAction, mockAuto, mockExec):
        """respectAutoApprove=True + memoryAutoApprove 开启：逐个 executeAction，不走审核"""
        spy = AsyncMock()
        actions = [
            MemoryAction(action="add", scopeType="global", scopeID="global", content="A"),
            MemoryAction(action="add", scopeType="global", scopeID="global", content="B"),
        ]
        await dispatchMemoryActions(
            actions, autoMode="on", opsList=["1"], chatID=1, originalMsg="msg", userID=1,
            respectAutoApprove=True, sendTGMemoryReview=spy,
        )
        assert mockExec.await_count == 2
        spy.assert_not_called()

    @patch("utils.llm.review.logSystemEvent", new_callable=AsyncMock)
    @patch("utils.llm.review.getMemoryAutoApprove", return_value=False)
    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    async def test_no_opslist_drops_with_warning(self, mockLogAction, mockAuto, mockLogEvent):
        """opsList 空：丢弃并告警，不调回调"""
        spy = AsyncMock()
        actions = [MemoryAction(action="add", scopeType="global", scopeID="global", content="A")]
        await dispatchMemoryActions(
            actions, autoMode="on", opsList=[], chatID=1,
            originalMsg="msg", userID=1, sendTGMemoryReview=spy,
        )
        spy.assert_not_called()
        events = [c.args[0] for c in mockLogEvent.await_args_list]
        assert any("无审核人" in e for e in events)

    @patch("utils.llm.review.addMemoryReviewItem")
    @patch("utils.llm.review.getMemoryAutoApprove", return_value=False)
    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    async def test_console_branch_uses_str_ids(self, mockLogAction, mockAuto, mockAdd):
        """autoMode=console：addMemoryReviewItem 收到 str(chatID)/str(opsID)"""
        actions = [MemoryAction(action="add", scopeType="global", scopeID="global", content="A")]
        await dispatchMemoryActions(
            actions, autoMode="console", opsList=[42], chatID=99,
            originalMsg="msg", userID=7,
        )
        mockAdd.assert_called_once()
        kwargs = mockAdd.call_args.kwargs
        assert kwargs["chatID"] == "99"
        assert kwargs["opsID"] == "42"

    @patch("utils.llm.review.buildMemoryActionReviewPayload", new_callable=AsyncMock)
    @patch("utils.llm.review.getMemoryAutoApprove", return_value=False)
    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    async def test_tg_branch_calls_callback_with_dict(self, mockLogAction, mockAuto, mockBuild):
        """autoMode 非 console：调 sendTGMemoryReview，传 buildMemoryActionReviewPayload 的 dict"""
        mockBuild.return_value = {"action": "add", "content": "X"}
        spy = AsyncMock()
        actions = [MemoryAction(action="add", scopeType="global", scopeID="global", content="A")]
        await dispatchMemoryActions(
            actions, autoMode="on", opsList=["1"], chatID=1,
            originalMsg="msg", userID=1, sendTGMemoryReview=spy,
        )
        spy.assert_awaited_once_with({"action": "add", "content": "X"})

    @patch("utils.llm.review.getMemoryAutoApprove", return_value=False)
    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    async def test_tg_branch_without_callback_raises(self, mockLogAction, mockAuto):
        """autoMode 非 console 但 sendTGMemoryReview=None：raise RuntimeError"""
        actions = [MemoryAction(action="add", scopeType="global", scopeID="global", content="A")]
        with pytest.raises(RuntimeError):
            await dispatchMemoryActions(
                actions, autoMode="on", opsList=["1"], chatID=1, originalMsg="msg", userID=1,
            )

    @patch("utils.llm.review.executeAction", new_callable=AsyncMock)
    @patch("utils.llm.review.buildMemoryActionReviewPayload", new_callable=AsyncMock)
    @patch("utils.llm.review.getMemoryAutoApprove", return_value=True)
    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    async def test_respect_autoapprove_false_skips_shortcut(self, mockLogAction, mockAuto, mockBuild, mockExec):
        """respectAutoApprove=False：即使 memoryAutoApprove 开启也不自动执行，走审核分流"""
        mockBuild.return_value = {"action": "add", "content": "X"}
        spy = AsyncMock()
        actions = [MemoryAction(action="add", scopeType="global", scopeID="global", content="A")]
        await dispatchMemoryActions(
            actions, autoMode="on", opsList=["1"], chatID=1, originalMsg="msg", userID=1,
            respectAutoApprove=False, sendTGMemoryReview=spy,
        )
        mockExec.assert_not_called()
        spy.assert_awaited_once()
