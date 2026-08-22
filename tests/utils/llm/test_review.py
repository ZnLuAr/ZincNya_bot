"""
tests/utils/llm/test_review.py

测试 LLM 审核共享操作（utils/llm/review.py）：
    - extractValidatedMemoryActions：解析 + 截断 + 逐个校验编排
    - queueMemoryActionsToConsole：转发到 console 审核队列
    - reviewRetryWithFeedback：补充反馈拼接 enhancedMsg 并重试
    - dispatchTextReply / dispatchGeneratedOutput：首生成 autoMode 三分流（原 handlers/llm.py 下沉）
    - _formatReviewHint：队列 item 预览格式化（随队列契约自 state.py 迁入）
    - renderReviewCard / renderMemoryReviewCard：TG 审核卡片渲染（随渲染函数自 handlers/llmReview.py 迁入）

约定：
    - parseMemoryActions / validateAction 用真实实现（add 路径不查 DB）
    - logSystemEvent / logAction / generateReply 用 AsyncMock 隔离副作用
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import InlineKeyboardMarkup

from config import LLM_REVIEW_FEEDBACK_MAX_LENGTH, LLM_MEMORY_MAX_ACTIONS, TG_MESSAGE_MAX_LEN

from utils.core.logger import LogLevel
from utils.llm.memory.action import MemoryAction
from utils.llm.messagePrep import DisplayBlocks
from utils.llm.review import (
    DispatchTarget,
    renderMemoryReviewCard,
    renderReviewCard,
    GeneratedOutput,
    _NO_OPS_HINT,
    _formatReviewHint,
    dispatchGeneratedOutput,
    dispatchMemoryActions,
    dispatchMemoryActionsToConsole,
    dispatchTextReply,
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




# ===========================================================================
# dispatchTextReply（autoMode 三分流——原 handlers/llm.py 下沉）
# ===========================================================================

def _target():
    return DispatchTarget(chatID="100", userID=42, username="cur", triggerMsgID=77)


def _generated(reply="回复内容", memoryActions=None):
    return GeneratedOutput(
        reply=reply,
        memoryActions=memoryActions or [],
        displayOriginalMsg="原始消息",
        includeContext=False,
        urlContexts=None,
    )


class TestDispatchTextReply:
    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    @patch("utils.llm.review.sendLLMReply", new_callable=AsyncMock)
    async def test_on_mode_sends_directly(self, mockSend, mockLog):
        bot = MagicMock()
        await dispatchTextReply(
            _generated(), target=_target(), autoMode="on", opsList=["1"], bot=bot,
        )

        kwargs = mockSend.await_args.kwargs
        assert kwargs["chatID"] == "100"
        assert kwargs["reply"] == "回复内容"
        assert kwargs["replyToMessageID"] == 77
        assert kwargs["maxLength"] == TG_MESSAGE_MAX_LEN

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    @patch("utils.llm.review.sendLLMReply", new_callable=AsyncMock)
    async def test_on_mode_no_review_no_reaction(self, mockSend, mockLog):
        """on 分支：sendReview / sendReaction 均不调用"""
        sendReview = AsyncMock()
        sendReaction = AsyncMock()
        await dispatchTextReply(
            _generated(), target=_target(), autoMode="on", opsList=["1"], bot=MagicMock(),
            sendReview=sendReview, sendReaction=sendReaction,
        )

        sendReview.assert_not_awaited()
        sendReaction.assert_not_awaited()

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    async def test_off_mode_sends_review_per_ops_then_reaction(self, mockLog):
        """off 分支：逐 ops 调 sendReview（按列表顺序）→ 👀 恰一次"""
        sendReview = AsyncMock()
        sendReaction = AsyncMock()
        await dispatchTextReply(
            _generated(), target=_target(), autoMode="off", opsList=["1", "2"], bot=MagicMock(),
            sendReview=sendReview, sendReaction=sendReaction,
        )

        assert [c.args[0] for c in sendReview.await_args_list] == ["1", "2"]
        sendReaction.assert_awaited_once_with("👀")

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    async def test_off_mode_empty_ops_sends_hint(self, mockLog):
        """off + 空 opsList：发 _NO_OPS_HINT，无 reaction、无审核日志"""
        bot = AsyncMock()
        sendReview = AsyncMock()
        sendReaction = AsyncMock()
        await dispatchTextReply(
            _generated(), target=_target(), autoMode="off", opsList=[], bot=bot,
            sendReview=sendReview, sendReaction=sendReaction,
        )

        assert bot.send_message.await_args.kwargs["text"] == _NO_OPS_HINT
        sendReview.assert_not_awaited()
        sendReaction.assert_not_awaited()

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    async def test_off_mode_missing_review_callback_raises(self, mockLog):
        with pytest.raises(RuntimeError, match="sendReview"):
            await dispatchTextReply(
                _generated(), target=_target(), autoMode="off", opsList=["1"], bot=MagicMock(),
            )

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    @patch("utils.llm.review.addReviewItem")
    async def test_console_mode_queues_item(self, mockAdd, mockLog):
        """console 分支：addReviewItem 透传 + 👀"""
        sendReaction = AsyncMock()
        await dispatchTextReply(
            _generated(), target=_target(), autoMode="console", opsList=["9", "2"], bot=MagicMock(),
            sendReaction=sendReaction,
        )

        kwargs = mockAdd.call_args.kwargs
        assert kwargs["chatID"] == "100"
        assert kwargs["messageID"] == 77
        assert kwargs["opsID"] == "9"                 # 首个 ops
        assert kwargs["userID"] == 42
        assert kwargs["includeContext"] is False
        sendReaction.assert_awaited_once_with("👀")

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    async def test_console_mode_empty_ops_sends_hint(self, mockLog):
        bot = AsyncMock()
        await dispatchTextReply(
            _generated(), target=_target(), autoMode="console", opsList=[], bot=bot,
        )

        assert bot.send_message.await_args.kwargs["text"] == _NO_OPS_HINT

    @patch("utils.llm.review.logSystemEvent", new_callable=AsyncMock)
    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    async def test_reaction_failure_logged_and_continues(self, mockLog, mockSysLog):
        """reaction 抛异常：记 WARNING、分发继续（_safeReaction 不中断）"""
        sendReaction = AsyncMock(side_effect=RuntimeError("boom"))
        bot = AsyncMock()
        await dispatchTextReply(
            _generated(), target=_target(), autoMode="console", opsList=["9"], bot=bot,
            sendReaction=sendReaction,
        )

        assert mockSysLog.await_count == 1
        assert mockSysLog.await_args.args[0] == "LLM reaction 发送失败"
        assert mockSysLog.await_args.args[2] == LogLevel.WARNING

    @patch("utils.llm.review.logAction", new_callable=AsyncMock)
    async def test_no_reaction_callback_skips_silently(self, mockLog):
        """sendReaction=None：跳过不报错"""
        await dispatchTextReply(
            _generated(), target=_target(), autoMode="console", opsList=["9"], bot=MagicMock(),
            sendReaction=None,
        )




# ===========================================================================
# dispatchGeneratedOutput（空输出检测 → 文字分发 → 记忆分发）
# ===========================================================================

class TestDispatchGeneratedOutput:
    @patch("utils.llm.review.logSystemEvent", new_callable=AsyncMock)
    @patch("utils.llm.review.dispatchMemoryActions", new_callable=AsyncMock)
    @patch("utils.llm.review.dispatchTextReply", new_callable=AsyncMock)
    async def test_empty_reply_no_memory_thinking_reaction(self, mockText, mockMem, mockSysLog):
        """空回复 + 无记忆：🤔 恰一次 + 日志，不进文字/记忆分发"""
        sendReaction = AsyncMock()
        await dispatchGeneratedOutput(
            _generated(reply="  "), target=_target(), autoMode="on", opsList=["1"],
            sendReaction=sendReaction,
        )

        sendReaction.assert_awaited_once_with("🤔")
        mockText.assert_not_awaited()
        mockMem.assert_not_awaited()

    @patch("utils.llm.review.logSystemEvent", new_callable=AsyncMock)
    @patch("utils.llm.review.dispatchMemoryActions", new_callable=AsyncMock)
    @patch("utils.llm.review.dispatchTextReply", new_callable=AsyncMock)
    async def test_memory_only_skips_thinking(self, mockText, mockMem, mockSysLog):
        """reply 空但有记忆操作：无 🤔，继续记忆分发；respectAutoApprove 不传（默认 True）"""
        sendReaction = AsyncMock()
        actions = [MemoryAction(action="add", scopeType="global", scopeID="global", content="A")]
        await dispatchGeneratedOutput(
            _generated(reply="", memoryActions=actions), target=_target(),
            autoMode="on", opsList=["1"], sendReaction=sendReaction,
        )

        sendReaction.assert_not_awaited()
        mockText.assert_not_awaited()
        mockMem.assert_awaited_once()
        assert "respectAutoApprove" not in mockMem.await_args.kwargs

    @patch("utils.llm.review.dispatchMemoryActions", new_callable=AsyncMock)
    @patch("utils.llm.review.dispatchTextReply", new_callable=AsyncMock)
    async def test_reply_present_passes_through(self, mockText, mockMem):
        """有回复：dispatchTextReply 收到 autoMode/opsList/sendReview/sendReaction 透传"""
        sendReaction = AsyncMock()
        sendTGReview = AsyncMock()
        await dispatchGeneratedOutput(
            _generated(), target=_target(), autoMode="off", opsList=["1", "2"],
            bot=MagicMock(), sendTGReview=sendTGReview, sendReaction=sendReaction,
        )

        kwargs = mockText.await_args.kwargs
        assert kwargs["autoMode"] == "off"
        assert kwargs["opsList"] == ["1", "2"]
        assert kwargs["sendReview"] is sendTGReview
        assert kwargs["sendReaction"] is sendReaction



# ===========================================================================
# 回复审核卡片渲染
# ===========================================================================

class TestRenderReviewCard:
    def test_returns_text_and_markup(self):
        text, markup = renderReviewCard("原始消息", "回复内容", 12345)
        assert isinstance(text, str)
        assert isinstance(markup, InlineKeyboardMarkup)

    def test_card_contains_label_and_content(self):
        text, _ = renderReviewCard("用户的提问", "锌酱的回答", 1)
        assert "待审核" in text
        assert "<b>" in text  # HTML 化：标题加粗
        assert "<blockquote>" in text  # 原始消息走 blockquote
        assert "用户的提问" in text
        assert "锌酱的回答" in text

    def test_card_contains_edit_and_fb_hints(self):
        """同时含 :edit 与 :fb 提示；:fb 带空格（与 startswith(':fb ') 对齐）"""
        text, _ = renderReviewCard("msg", "reply", 1)
        assert ":edit" in text
        assert ":fb " in text

    def test_suffix_appended(self):
        text, _ = renderReviewCard("msg", "reply", 1, suffix="\n\n⚠️ 警告")
        assert text.endswith("⚠️ 警告")

    def test_overlong_reply_within_tg_limit(self):
        text, _ = renderReviewCard("msg", "x" * 10000, 1)
        assert len(text) <= 4096

    def test_original_msg_merges_into_single_blockquote_when_reply_to(self):
        """originalMsg 含 [引用的消息]/[当前用户消息] 标记（injectReplyTextContext 输出）→ 合并为一个双小节 blockquote"""
        originalMsg = (
            "[引用的消息]\n<@zincnya_test_bot> 原引用内容\n\n"
            "[当前用户消息]\n<@ZincPhos> 当前用户说的"
        )
        text, _ = renderReviewCard(originalMsg, "回复", 1)
        assert text.count("<blockquote>") == 1  # 引用与当前同块（宽度不齐问题）
        assert "<b>引用消息</b>" in text
        assert "<b>当前消息</b>" in text
        assert "原引用内容" in text
        assert "当前用户说的" in text

    def test_original_msg_single_blockquote_when_plain(self):
        """普通消息（无引用标记）→ 单个 blockquote"""
        text, _ = renderReviewCard("普通用户消息", "回复", 1)
        assert text.count("<blockquote>") == 1

    def test_dynamic_content_escaped(self):
        """originalMsg/reply 含 < >& → 转义为 HTML 实体，防 BotBadRequest"""
        text, _ = renderReviewCard("<script>x</script>", "a & b < c", 1)
        assert "<script>" not in text  # 原始 < 已转义
        assert "&lt;script&gt;" in text
        assert "&amp;" in text  # & 转义

    def test_keyboard_callback_data_is_4_segments(self):
        """callback_data 4 段 llm:review:{action}:{chatID}（msgID 不编码进 data）"""
        _, markup = renderReviewCard("msg", "reply", 999)
        for row in markup.inline_keyboard:
            for btn in row:
                parts = btn.callback_data.split(":")
                assert len(parts) == 4
                assert parts[0] == "llm"
                assert parts[1] == "review"
                assert parts[3] == "999"




# ===========================================================================
# 记忆审核卡片渲染
# ===========================================================================

class TestRenderMemoryReviewCard:
    def _addAction(self):
        return {
            "action": "add",
            "scopeType": "global",
            "scopeID": "global",
            "content": "记住这件事",
            "tags": [],
            "priority": 0,
        }

    def test_returns_text_and_markup(self):
        text, markup = renderMemoryReviewCard(self._addAction(), "触发消息", 123)
        assert isinstance(text, str)
        assert isinstance(markup, InlineKeyboardMarkup)

    def test_memory_card_contains_fields(self):
        text, _ = renderMemoryReviewCard(self._addAction(), "触发", 1)
        assert "ADD" in text
        assert "global" in text
        assert "记住这件事" in text

    def test_memory_card_has_edit_but_no_fb(self):
        """记忆卡片只有 :edit 提示，无 :fb（memory 不支持 :fb）"""
        text, _ = renderMemoryReviewCard(self._addAction(), "触发", 1)
        assert ":edit" in text
        assert ":fb" not in text

    def test_suffix_appended(self):
        text, _ = renderMemoryReviewCard(self._addAction(), "触发", 1, suffix="\n\n尾注")
        assert text.endswith("尾注")

    def test_keyboard_callback_data_is_4_segments(self):
        """记忆 callback_data 4 段 llm:memreview:{action}:{chatID}"""
        _, markup = renderMemoryReviewCard(self._addAction(), "触发", 888)
        for row in markup.inline_keyboard:
            for btn in row:
                parts = btn.callback_data.split(":")
                assert len(parts) == 4
                assert parts[0] == "llm"
                assert parts[1] == "memreview"
                assert parts[3] == "888"



# ===========================================================================
# displayBlocks 结构化渲染（数据源拆分——引用/当前配对 + 小标题）
# ===========================================================================

def _blocks(prefix="", pairs=None):
    return DisplayBlocks(prefix=prefix, pairs=pairs or [])


class TestDisplayBlocksRendering:
    def test_structured_single_pair_merged_block(self):
        """单消息带引用：合并为一个双小节 blockquote（宽度不齐问题的解法）"""
        blocks = _blocks(pairs=[{"replyLine": "<@rep> 被引用的话", "currentText": "当前说的"}])
        text, _ = renderReviewCard("原始", "回复", 1, displayBlocks=blocks)

        assert text.count("<blockquote>") == 1
        assert "<b>引用消息</b>" in text
        assert "<b>当前消息</b>" in text
        assert "被引用的话" in text
        assert "当前说的" in text

    def test_structured_multi_pair_pairing_preserved(self):
        """多消息批次：每条消息一个块、块内引用/当前配对（旧 partition 只切第一个的 bug 修复）"""
        blocks = _blocks(pairs=[
            {"replyLine": "<@rep> 引用一", "currentText": "消息一"},
            {"replyLine": "", "currentText": "消息二"},
            {"replyLine": "<@rep> 引用三", "currentText": "消息三"},
        ])
        text, _ = renderReviewCard("原始", "回复", 1, displayBlocks=blocks)

        assert text.count("<blockquote>") == 3          # 每条消息一块
        assert text.count("<b>引用消息</b>") == 2
        assert text.count("<b>当前消息</b>") == 3

    def test_structured_no_reply_single_block_with_heading(self):
        """无引用消息：单个当前块（带小节标题）"""
        blocks = _blocks(pairs=[{"replyLine": "", "currentText": "纯消息"}])
        text, _ = renderReviewCard("原始", "回复", 1, displayBlocks=blocks)

        assert text.count("<blockquote>") == 1
        assert "<b>引用</b>" not in text
        assert "<b>当前消息</b>" in text

    def test_structured_prefix_line(self):
        """prefix（图片标注/URL 摘要）渲染为普通文本行（不加粗，视觉弱化）"""
        blocks = _blocks(prefix="[附带 2 张图片]", pairs=[{"replyLine": "", "currentText": "看图"}])
        text, _ = renderReviewCard("原始", "回复", 1, displayBlocks=blocks)

        assert "[附带 2 张图片]" in text
        assert "<b>[附带 2 张图片]</b>" not in text

    def test_structured_dynamic_escaped(self):
        """结构化路径同样转义动态内容（截断→转义→拼铁律）"""
        blocks = _blocks(pairs=[{"replyLine": "<@rep> <script>alert(1)</script>", "currentText": "a & b < c"}])
        text, _ = renderReviewCard("原始", "回复", 1, displayBlocks=blocks)

        assert "<script>" not in text
        assert "&lt;script&gt;" in text
        assert "&amp;" in text

    def test_structured_empty_pairs_falls_back_to_original(self):
        """pairs 为空：回退原始字符串单 blockquote"""
        blocks = _blocks(pairs=[])
        text, _ = renderReviewCard("原始消息", "回复", 1, displayBlocks=blocks)

        assert text.count("<blockquote>") == 1
        assert "原始消息" in text

    def test_memory_card_uses_structured_blocks(self):
        """记忆审核卡同样走结构化合并渲染"""
        blocks = _blocks(pairs=[{"replyLine": "<@rep> 引用", "currentText": "触发记忆的消息"}])
        action = {"action": "add", "scopeType": "global", "scopeID": "global", "content": "内容"}
        text, _ = renderMemoryReviewCard(action, "原始", 1, displayBlocks=blocks)

        assert text.count("<blockquote>") == 1
        assert "<b>引用消息</b>" in text
        assert "触发记忆的消息" in text
