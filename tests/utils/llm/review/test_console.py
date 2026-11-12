"""
tests/utils/llm/review/test_console.py

测试控制台 / chatScreen 一侧的审核（utils/llm/review/console.py）：
    - formatReviewItemText：回复与记忆操作审核项的展示文字
    - getReviewItemActions：审核项的可用操作提示
    - dispatchMemoryActionsToConsole：重试产生的记忆操作带着目标快照进 console 队列

约定：
    - 入队用例把 utils.llm.review.console.addMemoryReviewItem 换成 mock，不碰进程里真正的队列
    - logSystemEvent / logAction 用 AsyncMock 隔离副作用
"""

from unittest.mock import AsyncMock, patch

import pytest

from utils.llm.memory.action import MemoryAction
from utils.llm.review.actions import _approveMemoryReview
from utils.llm.review.console import (
    dispatchMemoryActionsToConsole,
    formatReviewItemText,
    getReviewItemActions,
)


# 与 console.py 的展示截断长度一致；用例按它构造恰好不截断与刚好截断的文本
_DISPLAY_LIMIT = 200




# ===========================================================================
# 展示文字
# ===========================================================================

class TestFormatReviewItemText:
    """formatReviewItemText：console / chatScreen 的审核项展示"""

    def _addAction(self):
        return {
            "action": "add",
            "scopeType": "global",
            "scopeID": "global",
            "content": "记住这件事",
            "tags": [],
            "priority": 0,
        }

    def test_console_memory_text_shows_priority_zero(self):
        text = formatReviewItemText({
            "kind": "memory",
            "action": self._addAction(),
            "originalMsg": "触发",
        })
        assert "优先级: 0" in text

    def test_reply_text_shows_original_and_reply(self):
        text = formatReviewItemText({"kind": "reply", "originalMsg": "原文", "reply": "回复"})

        assert text == "[回复审核]\n  原始消息: 原文\n  回复内容: 回复"

    def test_item_without_kind_is_shown_as_reply(self):
        text = formatReviewItemText({"originalMsg": "原文", "reply": "回复"})

        assert text.startswith("[回复审核]")

    def test_reply_is_truncated_only_beyond_display_limit(self):
        atLimit = "字" * _DISPLAY_LIMIT
        overLimit = "字" * (_DISPLAY_LIMIT + 1)

        atLimitText = formatReviewItemText({"kind": "reply", "originalMsg": "原文", "reply": atLimit})
        overLimitText = formatReviewItemText({"kind": "reply", "originalMsg": "原文", "reply": overLimit})

        assert atLimitText.endswith(f"回复内容: {atLimit}")
        assert overLimitText.endswith(f"回复内容: {atLimit}...")

    def test_update_text_shows_current_and_proposed_values(self):
        text = formatReviewItemText({
            "kind": "memory",
            "action": {
                "action": "update",
                "scopeType": "user",
                "scopeID": "42",
                "memoryID": 9,
                "originalContent": "当前事实",
                "content": "拟议事实",
                "tags": ["事实", "偏好"],
                "originalMode": "contextual",
                "mode": "pinned",
                "retrievalHint": "",
                "reason": "用户更正",
            },
            "originalMsg": "触发消息",
        })

        assert text.split("\n") == [
            "[记忆操作审核] UPDATE",
            "  范围: user:42",
            "  目标 ID: #9",
            "  当前内容: 当前事实",
            "  新内容: 拟议事实",
            "  标签: 事实, 偏好",
            "  当前模式: contextual",
            "  新模式: pinned",
            "  检索说明: (清空)",
            "  理由: 用户更正",
            "  触发消息: 触发消息",
        ]

    def test_add_text_omits_fields_the_action_does_not_carry(self):
        """add 没有目标记录：不出现目标 ID / 当前内容 / 模式 / 检索说明，正文标签是「内容」"""
        text = formatReviewItemText({
            "kind": "memory",
            "action": {"action": "add", "scopeType": "global", "scopeID": "", "content": "记住这件事"},
            "originalMsg": "触发消息",
        })

        assert text.split("\n") == [
            "[记忆操作审核] ADD",
            "  范围: global:global",
            "  内容: 记住这件事",
            "  触发消息: 触发消息",
        ]


class TestGetReviewItemActions:
    """getReviewItemActions：按审核项种类给出可用操作"""

    @pytest.mark.parametrize("item, expected", [
        ({"kind": "reply"}, "[A]ccept / [E]dit / [R]etry / [F]eedback / [C]ancel"),
        ({}, "[A]ccept / [E]dit / [R]etry / [F]eedback / [C]ancel"),
        ({"kind": "memory", "action": {"action": "add"}}, "[A]pprove / [E]dit / [C]ancel"),
        ({"kind": "memory", "action": {"action": "update"}}, "[A]pprove / [E]dit / [C]ancel"),
        ({"kind": "memory", "action": {"action": "delete"}}, "[A]pprove / [C]ancel"),
        ({"kind": "memory"}, "[A]pprove / [C]ancel"),
    ])
    def test_actions_by_kind(self, item, expected):
        assert getReviewItemActions(item) == expected




# ===========================================================================
# 重试产生的记忆操作
# ===========================================================================

@pytest.mark.asyncio
@patch("utils.llm.review.console.logSystemEvent", new_callable=AsyncMock)
@patch("utils.llm.review.actions.logAction", new_callable=AsyncMock)
@patch("utils.llm.review.actions.executeAction", new_callable=AsyncMock, return_value=True)
@patch("utils.llm.review.console.addMemoryReviewItem")
@patch("utils.llm.memory.action.getMemoryByID", new_callable=AsyncMock)
async def test_consoleRetrySnapshotsUpdateAndDeleteTargets(
    mockGetMemory, mockAdd, mockExecute, mockLogAction, mockLogSystemEvent,
):
    targets = {
        11: {
            "id": 11,
            "scope_type": "user",
            "scope_id": "42",
            "content": "用户事实",
            "tags": ["事实"],
            "retrieval_hint": None,
            "enabled": True,
            "priority": 1,
            "mode": "contextual",
            "source": "inferred",
        },
        12: {
            "id": 12,
            "scope_type": "global",
            "scope_id": "global",
            "content": "全局事实",
            "tags": [],
            "retrieval_hint": None,
            "enabled": True,
            "priority": 0,
            "mode": "pinned",
            "source": "inferred",
        },
    }
    mockGetMemory.side_effect = lambda memoryID: targets[memoryID]
    actions = [
        MemoryAction(
            action="update", scopeType="user", scopeID="42", memoryID=11,
            content="更新后的事实",
        ),
        MemoryAction(
            action="delete", scopeType="global", scopeID="global", memoryID=12,
        ),
    ]

    await dispatchMemoryActionsToConsole(
        actions, chatID="chat", originalMsg="触发消息", opsID="ops", userID="user",
        logLabel="console retry",
    )

    assert mockAdd.call_count == 2
    payloads = [call.kwargs["action"] for call in mockAdd.call_args_list]
    assert all(payload.get("targetState") for payload in payloads)

    for payload in payloads:
        assert await _approveMemoryReview({"kind": "memory", "action": payload}) is True

    assert mockExecute.await_count == 2
    for call, payload in zip(mockExecute.await_args_list, payloads):
        assert call.kwargs["humanApproved"] is True
        assert call.kwargs["expectedState"] == payload["targetState"]


class TestDispatchMemoryActionsToConsole:
    """dispatchMemoryActionsToConsole：逐个入队，汇总日志只在有操作时记一条"""

    async def test_empty_list_queues_nothing_and_logs_nothing(self):
        with (
            patch("utils.llm.review.console.addMemoryReviewItem") as mockAdd,
            patch("utils.llm.review.console.logSystemEvent", new_callable=AsyncMock) as mockLog,
        ):
            await dispatchMemoryActionsToConsole(
                [], chatID="123", originalMsg="msg", opsID="456", userID="789",
                logLabel="console retry",
            )

        mockAdd.assert_not_called()
        mockLog.assert_not_awaited()

    async def test_actions_are_queued_in_order_with_request_identity(self):
        """add 没有目标记录，入队的 action 就是 toDict 的结果；身份字段原样带上"""
        actions = [
            MemoryAction(action="add", scopeType="global", scopeID="global", content="A"),
            MemoryAction(action="add", scopeType="global", scopeID="global", content="B"),
        ]

        with (
            patch("utils.llm.review.console.addMemoryReviewItem") as mockAdd,
            patch("utils.llm.review.console.logSystemEvent", new_callable=AsyncMock) as mockLog,
        ):
            await dispatchMemoryActionsToConsole(
                actions, chatID="123", originalMsg="原始消息", opsID="456", userID="789",
                logLabel="console retry",
            )

        assert [call.kwargs for call in mockAdd.call_args_list] == [
            {
                "action": action.toDict(),
                "chatID": "123",
                "originalMsg": "原始消息",
                "opsID": "456",
                "userID": "789",
            }
            for action in actions
        ]
        mockLog.assert_awaited_once()
        assert "console retry" in mockLog.await_args.args[0]
        assert "2 个操作" in mockLog.await_args.args[1]
