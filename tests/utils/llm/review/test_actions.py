"""
tests/utils/llm/review/test_actions.py

测试审核动作（utils/llm/review/actions.py）：
    - reviewRetry / reviewRetryWithFeedback：复用原始请求重试，反馈只用于当次
    - reviewSend / reviewCancel：发送回复或执行记忆操作、取消
    - reviewEditSubmit / _updateReviewItemText：编辑后的审核项与日志
    - _approveMemoryReview / refreshMemoryReviewItem：按审核快照批准，冲突时刷新目标

约定：
    - 重试用例只替换 utils.llm.task 里的模型输入准备与模型请求；假的模型输出是
      完整的 <OPERATIONS> 块，解析、校验和操作循环走真实代码（add 路径不查 DB）
    - logSystemEvent / logAction 用 AsyncMock 隔离副作用
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from config import LLM_REVIEW_FEEDBACK_MAX_LENGTH

from utils.llm import task as taskModule
from utils.llm.memory.action import MemoryAction, MemoryActionContext
from utils.llm.memory.types import MemoryQuery, MemoryTurn
from utils.llm.models import CollectedOutput, MessageInput, MessageOrigin, TaskRequest
from utils.llm.review.actions import (
    _approveMemoryReview,
    _updateReviewItemText,
    refreshMemoryReviewItem,
    reviewCancel,
    reviewEditSubmit,
    reviewRetry,
    reviewRetryWithFeedback,
    reviewSend,
)
from utils.llm.review.console import dispatchMemoryActionsToConsole




# ===========================================================================
# 测试数据构造
# ===========================================================================

def _batch(*operations, end=True) -> str:
    """一次合法的模型输出。"""
    payload = json.dumps({"operations": list(operations), "end": end}, ensure_ascii=False)
    return f"<OPERATIONS>\n{payload}\n</OPERATIONS>"


def _message(text: str) -> dict:
    return {"op": "message", "text": text}


def _memoryAdd(content: str = "测试内容", **fields) -> dict:
    """一个能通过校验的 global add 记忆操作；fields 覆盖或补充模型给出的参数。"""
    return {
        "op": "memory_action",
        "action": "add",
        "scope_type": "global",
        "scope_id": "global",
        "content": content,
        **fields,
    }


@pytest.fixture
def modelEnv(monkeypatch, contextStore):
    """
    重试用例的模型替身。

    env.outputs 是按调用顺序给出的模型输出；用到最后一项后一直重复它，
    项是异常实例时该次请求抛出它。默认只有一项：一条发言「新回复」后结束。
    env.prepare / env.request 是模型输入准备与模型请求的替身，供断言。
    """
    env = SimpleNamespace()
    env.outputs = [_batch(_message("新回复"))]
    env.prepare = AsyncMock(return_value=object())

    async def _requestModelOutput(modelInput, *, instructions, appendix=""):
        output = env.outputs.pop(0) if len(env.outputs) > 1 else env.outputs[0]
        if isinstance(output, Exception):
            raise output
        return output

    env.request = AsyncMock(side_effect=_requestModelOutput)

    monkeypatch.setattr(taskModule, "prepareModelInput", env.prepare)
    monkeypatch.setattr(taskModule, "requestModelOutput", env.request)
    monkeypatch.setattr(taskModule, "listCapabilities", lambda: [])
    monkeypatch.setattr(taskModule, "logSystemEvent", AsyncMock())
    monkeypatch.setattr("utils.llm.operation.loop.logSystemEvent", AsyncMock())
    monkeypatch.setattr("utils.llm.review.actions.logAction", AsyncMock())
    return env


def _replyItem(**fields):
    query = fields.get("memoryQuery")
    turns = query.turns if query is not None else (MemoryTurn(currentText="原始问题"),)
    request = TaskRequest(
        messages=tuple(MessageInput(
            origin=MessageOrigin(
                chatID=fields.get("chatID", "123"),
                userID=fields.get("userID", 42),
                messageID=9,
            ),
            text=turn.currentText, replyText=turn.replyText, memoryTurn=turn,
        ) for turn in turns),
        includeContext=fields.get("includeContext", False),
    )
    return {"request": request, **fields}


def _memoryItem(**actionFields) -> dict:
    """一条指向已有记录的记忆审核项；actionFields 覆盖或补充 action 的字段。"""
    return {
        "kind": "memory",
        "action": {
            "action": "update",
            "scopeType": "user",
            "scopeID": "42",
            "memoryID": 7,
            "content": "拟议事实",
            "targetState": "old-state",
            **actionFields,
        },
        "chatID": "123",
        "userID": "42",
        "originalMsg": "触发消息",
    }




# ===========================================================================
# reviewRetryWithFeedback
# ===========================================================================

class TestReviewRetryWithFeedback:
    """reviewRetryWithFeedback：补充反馈拼接 enhancedMsg 并重试"""

    @pytest.mark.parametrize("feedback, expectedFeedback", [
        ("补充条件", "补充条件"),
        ("  补充条件\n", "补充条件"),
        (" \n\t", ""),
        ("", ""),
    ])
    async def test_retry_reuses_generation_request(self, feedback, expectedFeedback):
        request = TaskRequest(messages=(MessageInput(
            origin=MessageOrigin(chatID="123", userID=42, messageID=9), text="原始问题",
            replyText="被引用内容", images=({"data": "image"},),
        ),))
        item = _replyItem(**{
            "originalMsg": "审核展示文字",
            "chatID": "123",
            "includeContext": False,
            "request": request,
        })
        with (
            patch(
                "utils.llm.review.actions.collectOutput",
                new_callable=AsyncMock,
                return_value=CollectedOutput(reply="新回复"),
            ) as generate,
            patch("utils.llm.review.actions.logAction", new_callable=AsyncMock),
        ):
            result = await reviewRetryWithFeedback(item, feedback)

        assert result["reply"] == "新回复"
        generate.assert_awaited_once_with(request, feedback=expectedFeedback, logLabel="feedback retry")

    async def test_enhanced_message_format(self, modelEnv):
        """交给模型的消息来自原始请求，末尾接 [背景信息补充：...]；不使用审核展示文字"""
        item = _replyItem(**{
            "originalMsg": "用户原始消息",
            "chatID": "123",
            "includeContext": False,
        })
        await reviewRetryWithFeedback(item, "假设用户预算 1000 元")

        enhancedMsg = modelEnv.prepare.await_args.args[0]
        assert "原始问题" in enhancedMsg
        assert "用户原始消息" not in enhancedMsg
        assert "[背景信息补充：假设用户预算 1000 元]" in enhancedMsg

    @pytest.mark.parametrize("padding", ["", " \n"])
    async def test_feedback_over_limit_raises(self, padding):
        """审核入口拒绝超长反馈，任务尚未启动。"""
        longFeedback = "字" * (LLM_REVIEW_FEEDBACK_MAX_LENGTH + 1)
        item = _replyItem(**{"originalMsg": "msg", "chatID": "123", "includeContext": False})

        with patch("utils.llm.review.actions.collectOutput", new_callable=AsyncMock) as generate:
            with pytest.raises(ValueError, match="反馈过长喵") as excInfo:
                await reviewRetryWithFeedback(item, padding + longFeedback + padding)

        assert f"{len(longFeedback)}/{LLM_REVIEW_FEEDBACK_MAX_LENGTH}" in str(excInfo.value)
        generate.assert_not_called()

    async def test_returns_updated_reply_preserves_original(self, modelEnv):
        """返回 {**item, reply}，originalMsg 不变（反馈一次性语义）"""
        modelEnv.outputs = [_batch(_message("重新生成的回复"))]
        item = _replyItem(**{
            "originalMsg": "用户原始消息",
            "chatID": "123",
            "includeContext": False,
            "userID": "u1",
        })
        result = await reviewRetryWithFeedback(item, "补充信息")

        assert result["reply"] == "重新生成的回复"
        # originalMsg 保持干净，不含补充标记
        assert result["originalMsg"] == "用户原始消息"
        assert "背景信息补充" not in result["originalMsg"]
        # 其他字段透传
        assert result["userID"] == "u1"
        assert result["request"] is item["request"]

    async def test_no_memory_processing_when_context_off(self, modelEnv):
        """includeContext=False：不调 memoryDispatcher"""
        spy = AsyncMock()
        item = _replyItem(**{"originalMsg": "msg", "chatID": "123", "includeContext": False})

        await reviewRetryWithFeedback(item, "补充", memoryDispatcher=spy)

        spy.assert_not_called()

    async def test_memory_processing_when_context_on(self, modelEnv):
        """includeContext=True：记忆操作不进回复正文，校验通过后连同请求身份交给 memoryDispatcher"""
        spy = AsyncMock()
        modelEnv.outputs = [_batch(_message("回复正文"), _memoryAdd("记住"))]
        item = _replyItem(**{"originalMsg": "msg", "chatID": "123", "includeContext": True, "opsID": "o1"})

        result = await reviewRetryWithFeedback(item, "补充", memoryDispatcher=spy)

        assert result["reply"] == "回复正文"
        spy.assert_awaited_once()
        dispatchedActions = spy.await_args.args[0]
        assert len(dispatchedActions) == 1
        assert isinstance(dispatchedActions[0], MemoryAction)
        assert (dispatchedActions[0].action, dispatchedActions[0].content) == ("add", "记住")
        assert spy.await_args.kwargs == {
            "chatID": "123",
            "originalMsg": "msg",
            "opsID": "o1",
            "userID": 42,
            "logLabel": "feedback retry",
        }

    async def test_feedback_retry_injects_custom_dispatcher(self, modelEnv):
        """feedback 透传自定义 memoryDispatcher 与 logLabel"""
        spy = AsyncMock()
        modelEnv.outputs = [_batch(_message("回复"), _memoryAdd("记住"))]
        item = _replyItem(**{"originalMsg": "msg", "chatID": "123", "includeContext": True, "opsID": "o1"})

        await reviewRetryWithFeedback(item, "补充", memoryDispatcher=spy, logLabel="custom")

        spy.assert_awaited_once()
        assert spy.await_args.kwargs["logLabel"] == "custom"

    @pytest.mark.parametrize("padding", ["", " \n"])
    async def test_feedback_within_limit_succeeds(self, modelEnv, padding):
        """按去除首尾空白后的长度判断，恰好达到上限可以生成。"""
        item = _replyItem(**{"originalMsg": "msg", "chatID": "123", "includeContext": False})
        feedback = "字" * LLM_REVIEW_FEEDBACK_MAX_LENGTH

        result = await reviewRetryWithFeedback(item, padding + feedback + padding)

        assert result["reply"] == "新回复"
        assert modelEnv.prepare.await_args.args[0] == (
            item["request"].userMessage + f"\n\n[背景信息补充：{feedback}]"
        )
        modelEnv.request.assert_awaited_once()

    async def test_feedback_only_updates_query_feedback_text(self, modelEnv):
        originalQuery = MemoryQuery(
            turns=(MemoryTurn(currentText="当前", replyText="引用"),),
        )
        item = _replyItem(**{
            "originalMsg": "msg",
            "chatID": "123",
            "includeContext": False,
            "memoryQuery": originalQuery,
        })

        result = await reviewRetryWithFeedback(item, "  补充条件  ")

        retriedQuery = modelEnv.prepare.await_args.kwargs["memoryQuery"]
        assert retriedQuery.turns == originalQuery.turns
        assert retriedQuery.history == originalQuery.history
        assert retriedQuery.feedbackText == "补充条件"
        assert originalQuery.feedbackText == ""
        assert result["memoryQuery"] is not retriedQuery
        assert result["memoryQuery"].turns == originalQuery.turns
        assert result["memoryQuery"].history == originalQuery.history
        assert result["memoryQuery"].feedbackText == ""

    async def test_memory_review_item_cannot_be_retried_with_feedback(self, modelEnv):
        with pytest.raises(ValueError, match="记忆审核项暂不支持重试"):
            await reviewRetryWithFeedback({"kind": "memory", "action": {"action": "add"}}, "补充")

        modelEnv.prepare.assert_not_awaited()




# ===========================================================================
# reviewRetry
# ===========================================================================

class TestReviewRetry:
    """reviewRetry：复用原始请求重新生成，透传 memoryDispatcher 与 logLabel"""

    async def test_missing_request_does_not_generate_from_display_text(self):
        with patch("utils.llm.review.actions.collectOutput", new_callable=AsyncMock) as generate:
            with pytest.raises(ValueError, match="缺少原始生成请求"):
                await reviewRetry({"originalMsg": "审核展示文字", "chatID": "123"})
        generate.assert_not_awaited()

    async def test_plain_retry_does_not_inherit_feedback(self, modelEnv):
        item = _replyItem(originalMsg="审核展示文字", chatID="123", includeContext=False)
        first = await reviewRetryWithFeedback(item, "补充条件")
        await reviewRetry(first)
        firstInput = modelEnv.prepare.await_args_list[0].args[0]
        secondInput = modelEnv.prepare.await_args_list[1].args[0]
        assert "补充条件" in firstInput
        assert secondInput == "原始问题"
        assert first["request"] is item["request"]

    def test_retry_default_dispatcher_is_console(self):
        """默认 memoryDispatcher=dispatchMemoryActionsToConsole, logLabel='console retry'"""
        assert reviewRetry.__kwdefaults__["memoryDispatcher"] is dispatchMemoryActionsToConsole
        assert reviewRetry.__kwdefaults__["logLabel"] == "console retry"

    async def test_retry_reply_replaced_and_originalmsg_preserved(self, modelEnv):
        """retry 替换 reply，保留 originalMsg；多条发言用空行合并成一条回复"""
        modelEnv.outputs = [_batch(_message("第一句"), _message("第二句"))]
        item = _replyItem(**{"originalMsg": "原文", "chatID": "123", "includeContext": False, "reply": "旧回复"})

        result = await reviewRetry(item)

        assert result["reply"] == "第一句\n\n第二句"
        assert result["originalMsg"] == "原文"
        assert item["reply"] == "旧回复"

    async def test_retry_invalid_memory_action_never_reaches_dispatcher(self, modelEnv):
        """带不合法记忆操作的整批不生效：同批发言不进回复，dispatcher 只收到重新输出后通过校验的操作"""
        spy = AsyncMock()
        modelEnv.outputs = [
            _batch(_message("不该出现"), _memoryAdd("X", action="frobnicate")),
            _batch(_message("回复")),
        ]
        item = _replyItem(**{"originalMsg": "msg", "chatID": "123", "includeContext": True, "opsID": "o1"})

        result = await reviewRetry(item, memoryDispatcher=spy)

        assert result["reply"] == "回复"
        spy.assert_awaited_once()
        assert spy.await_args.args[0] == []
        assert modelEnv.request.await_count == 2

    async def test_retry_memory_scope_is_checked_against_request_identity(self, modelEnv):
        """模型自报的 user scope 与原始请求的用户不一致时不交给 dispatcher；一致的才交"""
        spy = AsyncMock()
        modelEnv.outputs = [
            _batch(_memoryAdd("用户事实", scope_type="user", scope_id="other-user")),
            _batch(_message("回复"), _memoryAdd("用户事实", scope_type="user", scope_id="42")),
        ]
        item = _replyItem(**{"originalMsg": "msg", "chatID": "123", "includeContext": True, "opsID": "o1"})

        await reviewRetry(item, memoryDispatcher=spy)

        dispatchedActions = spy.await_args.args[0]
        assert [(action.scopeType, action.scopeID) for action in dispatchedActions] == [("user", "42")]
        assert spy.await_args.kwargs["userID"] == 42

    async def test_retry_injects_custom_dispatcher(self, modelEnv):
        """retry 透传自定义 memoryDispatcher 与 logLabel"""
        spy = AsyncMock()
        modelEnv.outputs = [_batch(_message("回复"), _memoryAdd("记住"))]
        item = _replyItem(**{"originalMsg": "msg", "chatID": "123", "includeContext": True, "opsID": "o1"})

        await reviewRetry(item, memoryDispatcher=spy, logLabel="custom")

        spy.assert_awaited_once()
        assert spy.await_args.kwargs["logLabel"] == "custom"

    async def test_retry_model_failure_propagates_without_dispatching(self, modelEnv):
        """请求模型失败：抛出原异常，不分发记忆操作，传入的审核项不变"""
        spy = AsyncMock()
        error = TimeoutError("provider timeout")
        modelEnv.outputs = [error]
        item = _replyItem(**{
            "originalMsg": "msg", "chatID": "123", "includeContext": True, "opsID": "o1", "reply": "旧回复",
        })

        with pytest.raises(TimeoutError) as excInfo:
            await reviewRetry(item, memoryDispatcher=spy)

        assert excInfo.value is error
        spy.assert_not_awaited()
        assert item["reply"] == "旧回复"

    async def test_retry_reuses_original_memory_query(self, modelEnv):
        memoryQuery = MemoryQuery(
            turns=(MemoryTurn(currentText="当前", replyText="引用"),),
        )
        item = _replyItem(**{
            "originalMsg": "原文",
            "chatID": "123",
            "includeContext": False,
            "memoryQuery": memoryQuery,
        })

        result = await reviewRetry(item)

        assert modelEnv.prepare.await_args.kwargs["memoryQuery"] == memoryQuery
        assert result["memoryQuery"] == memoryQuery

    async def test_memory_review_item_cannot_be_retried(self, modelEnv):
        with pytest.raises(ValueError, match="记忆审核项暂不支持重试"):
            await reviewRetry({"kind": "memory", "action": {"action": "add"}})

        modelEnv.prepare.assert_not_awaited()




# ===========================================================================
# reviewSend / reviewCancel
# ===========================================================================

class TestReviewSend:
    """reviewSend：回复审核项发到目标聊天，记忆审核项按快照执行"""

    async def test_reply_item_is_sent_as_reply_to_trigger_message(self):
        bot = object()
        item = {"kind": "reply", "chatID": "123", "messageID": 9, "originalMsg": "原文", "reply": "回复"}

        with (
            patch("utils.llm.review.actions.sendLLMReply", new_callable=AsyncMock) as mockSend,
            patch("utils.llm.review.actions.logAction", new_callable=AsyncMock) as mockLog,
        ):
            sent = await reviewSend(bot, item)

        assert sent is True
        mockSend.assert_awaited_once_with(bot=bot, chatID="123", reply="回复", replyToMessageID=9)
        mockLog.assert_awaited_once()
        assert mockLog.await_args.args[1] == "LLM 审核：发送"

    async def test_reply_item_without_trigger_message_is_sent_plain(self):
        """审核项没有 messageID（如控制台主动发起）：不引用任何消息；缺 kind 视为回复审核"""
        item = {"chatID": "123", "originalMsg": "原文", "reply": "回复"}

        with (
            patch("utils.llm.review.actions.sendLLMReply", new_callable=AsyncMock) as mockSend,
            patch("utils.llm.review.actions.logAction", new_callable=AsyncMock),
        ):
            sent = await reviewSend(object(), item)

        assert sent is True
        assert mockSend.await_args.kwargs["replyToMessageID"] is None

    async def test_memory_item_is_executed_with_snapshot_and_card_identity(self):
        """记忆审核项：不发消息；按卡片保存的 chat/user 与 targetState 执行，算人工批准"""
        item = _memoryItem()

        with (
            patch("utils.llm.review.actions.sendLLMReply", new_callable=AsyncMock) as mockSend,
            patch(
                "utils.llm.review.actions.executeAction", new_callable=AsyncMock, return_value=True,
            ) as mockExecute,
            patch("utils.llm.review.actions.logAction", new_callable=AsyncMock) as mockLog,
        ):
            sent = await reviewSend(object(), item)

        assert sent is True
        mockSend.assert_not_awaited()
        mockExecute.assert_awaited_once()
        assert mockExecute.await_args.args == (MemoryAction.fromDict(item["action"]),)
        assert mockExecute.await_args.kwargs == {
            "humanApproved": True,
            "expectedState": "old-state",
            "actionContext": MemoryActionContext(chatID="123", userID="42"),
        }
        assert mockLog.await_args.args[1] == "LLM 审核：记忆操作 update 成功"
        assert mockLog.await_args.args[2] == "scope=user:42, id=#7"


class TestReviewCancel:
    """reviewCancel：只记日志，回复与记忆审核项各有标题"""

    async def test_reply_item_logs_original_message(self):
        with patch("utils.llm.review.actions.logAction", new_callable=AsyncMock) as mockLog:
            await reviewCancel({"kind": "reply", "originalMsg": "原文", "reply": "回复"})

        mockLog.assert_awaited_once()
        assert mockLog.await_args.args[1] == "LLM 审核：取消"
        assert mockLog.await_args.args[2] == "原文：原文"

    async def test_memory_item_logs_action_and_scope_without_content(self):
        with patch("utils.llm.review.actions.logAction", new_callable=AsyncMock) as mockLog:
            await reviewCancel(_memoryItem(content="不该进日志的正文"))

        mockLog.assert_awaited_once()
        assert mockLog.await_args.args[1] == "LLM 审核：记忆操作取消"
        assert mockLog.await_args.args[2] == "update | scope=user:42"




# ===========================================================================
# 编辑
# ===========================================================================

@pytest.mark.asyncio
async def test_editingMemoryReviewClearsOldRetrievalHint():
    item = {
        "kind": "memory",
        "action": {
            "action": "update",
            "content": "旧事实",
            "retrievalHint": "旧检索说明",
            "targetState": "state",
        },
    }

    edited = await reviewEditSubmit(item, "新事实")

    assert edited["action"]["content"] == "新事实"
    assert edited["action"]["retrievalHint"] == ""
    assert edited["action"]["targetState"] == "state"


@pytest.mark.asyncio
async def test_editingMemoryReviewDoesNotLogEditedPrivateText():
    secret = "绝密编辑正文 secret-hint-42"
    item = {
        "kind": "memory",
        "action": {
            "action": "update",
            "scopeType": "user",
            "scopeID": "42",
            "memoryID": 7,
            "content": "旧事实",
            "retrievalHint": "旧检索说明",
            "targetState": "state",
        },
    }

    with patch("utils.llm.review.actions.logAction", new_callable=AsyncMock) as mockLog:
        edited = await reviewEditSubmit(item, secret)

    assert edited["action"]["content"] == secret
    loggedText = " ".join(
        str(argument)
        for call in mockLog.await_args_list
        for argument in (*call.args, *call.kwargs.values())
    )
    assert secret not in loggedText
    assert "secret-hint-42" not in loggedText
    assert "scope=user:42" in loggedText
    assert "id=#7" in loggedText


class TestReviewEditSubmit:
    """reviewEditSubmit / _updateReviewItemText：返回新的审核项，不改传入的那一条"""

    @pytest.mark.parametrize("editedText", ["", "   ", "\n\t"])
    async def test_blank_text_returns_original_item_without_logging(self, editedText):
        item = {"kind": "reply", "originalMsg": "原文", "reply": "旧回复"}

        with patch("utils.llm.review.actions.logAction", new_callable=AsyncMock) as mockLog:
            edited = await reviewEditSubmit(item, editedText)

        assert edited is item
        assert item["reply"] == "旧回复"
        mockLog.assert_not_awaited()

    async def test_reply_edit_replaces_reply_and_keeps_other_fields(self):
        request = _replyItem()["request"]
        item = {"kind": "reply", "originalMsg": "原文", "reply": "旧回复", "request": request}

        with patch("utils.llm.review.actions.logAction", new_callable=AsyncMock) as mockLog:
            edited = await reviewEditSubmit(item, "新回复")

        assert edited == {**item, "reply": "新回复"}
        assert edited["request"] is request
        assert item["reply"] == "旧回复"
        assert [call.args[1] for call in mockLog.await_args_list] == ["LLM 审核：编辑完成", ""]
        assert mockLog.await_args_list[1].args[2] == "编辑后：新回复"

    def test_update_text_of_reply_item(self):
        item = {"originalMsg": "原文", "reply": "旧回复"}

        edited = _updateReviewItemText(item, "新回复")

        assert edited == {"originalMsg": "原文", "reply": "新回复"}
        assert item["reply"] == "旧回复"

    def test_update_text_of_memory_item_clears_hint_and_keeps_snapshot(self):
        item = _memoryItem(retrievalHint="旧检索说明")

        edited = _updateReviewItemText(item, "新事实")

        assert edited["action"] == {
            **item["action"],
            "content": "新事实",
            "retrievalHint": "",
        }
        assert edited["action"]["targetState"] == "old-state"
        assert item["action"]["content"] == "拟议事实"
        assert item["action"]["retrievalHint"] == "旧检索说明"




# ===========================================================================
# 记忆操作的批准与目标刷新
# ===========================================================================

@pytest.mark.asyncio
async def test_failedMemoryApprovalRefreshesTargetSnapshot():
    item = {
        "kind": "memory",
        "action": {
            "action": "update",
            "scopeType": "user",
            "scopeID": "42",
            "memoryID": 7,
            "content": "拟议事实",
            "targetState": "old-state",
        },
    }
    refreshed = {
        **item["action"],
        "originalContent": "当前事实",
        "targetState": "new-state",
    }

    with (
        patch(
            "utils.llm.review.actions.executeAction",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "utils.llm.review.actions.buildMemoryActionReviewPayload",
            new_callable=AsyncMock,
            return_value=refreshed,
        ) as mockBuild,
        patch("utils.llm.review.actions.logAction", new_callable=AsyncMock),
    ):
        success = await _approveMemoryReview(item)

    assert success is False
    assert item["action"] == refreshed
    mockBuild.assert_awaited_once()


class TestRefreshMemoryReviewItem:
    """refreshMemoryReviewItem：只在拿到新的目标快照时写回，其余情况返回 False 且不动审核项"""

    @pytest.mark.parametrize("item", [
        {"kind": "memory"},                                              # 没有 action
        {"kind": "memory", "action": "update"},                          # action 不是 dict
        {"kind": "memory", "action": {"content": "缺 action 与 scopeType"}},
        {"kind": "memory", "action": {"action": "add", "scopeType": "global", "content": "新事实"}},
    ])
    async def test_item_without_refreshable_target_returns_false(self, item):
        """action 缺失、形状不对或没有 memoryID（add）：不查目标记录"""
        snapshot = dict(item)

        with patch(
            "utils.llm.review.actions.buildMemoryActionReviewPayload", new_callable=AsyncMock,
        ) as mockBuild:
            refreshed = await refreshMemoryReviewItem(item)

        assert refreshed is False
        assert item == snapshot
        mockBuild.assert_not_awaited()

    async def test_missing_target_keeps_old_action(self):
        """目标记录已不存在：payload 没有 targetState，审核项保持原样"""
        item = _memoryItem()
        oldAction = item["action"]
        withoutTarget = {key: value for key, value in oldAction.items() if key != "targetState"}

        with patch(
            "utils.llm.review.actions.buildMemoryActionReviewPayload",
            new_callable=AsyncMock,
            return_value=withoutTarget,
        ):
            refreshed = await refreshMemoryReviewItem(item)

        assert refreshed is False
        assert item["action"] is oldAction

    async def test_existing_target_replaces_action_with_fresh_snapshot(self):
        item = _memoryItem()
        fresh = {**item["action"], "originalContent": "当前事实", "targetState": "new-state"}

        with patch(
            "utils.llm.review.actions.buildMemoryActionReviewPayload",
            new_callable=AsyncMock,
            return_value=fresh,
        ) as mockBuild:
            refreshed = await refreshMemoryReviewItem(item)

        assert refreshed is True
        assert item["action"] is fresh
        mockBuild.assert_awaited_once_with(MemoryAction.fromDict(_memoryItem()["action"]))
