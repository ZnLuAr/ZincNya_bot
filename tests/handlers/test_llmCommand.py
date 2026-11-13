"""
tests/handlers/test_llmCommand.py

测试 handlers/llmCommand.py 的 LLM 控制命令
"""

import pytest
from unittest.mock import patch, AsyncMock

from handlers.llmCommand import handleLLMCommand, register


# ============================================================================
# TestLLMCommand - 命令处理测试
# ============================================================================

class TestLLMCommand:
    """测试 /llm 命令处理"""

    @pytest.mark.asyncio
    async def test_no_permission_denied(self, mockUpdate, mockContext):
        """无权限时拒绝"""
        mockContext.args = []

        with patch('handlers.llmCommand.hasPermission', return_value=False):
            await handleLLMCommand(mockUpdate, mockContext)

        call_args = mockUpdate.message.reply_text.call_args[0][0]
        assert "权限" in call_args

    @pytest.mark.asyncio
    async def test_no_args_shows_status(self, mockUpdate, mockContext):
        """无参数显示当前状态"""
        mockContext.args = []

        with patch('handlers.llmCommand.hasPermission', return_value=True), \
             patch('handlers.llmCommand.getLLMEnabled', return_value=True):

            await handleLLMCommand(mockUpdate, mockContext)

            call_args = mockUpdate.message.reply_text.call_args[0][0]
            assert "开启" in call_args or "关闭" in call_args

    @pytest.mark.asyncio
    async def test_on_enables_llm(self, mockUpdate, mockContext):
        """on 参数开启 LLM"""
        mockContext.args = ["on"]

        with patch('handlers.llmCommand.hasPermission', return_value=True), \
             patch('handlers.llmCommand.setLLMEnabled') as mock_set, \
             patch('handlers.llmCommand.logAction', new_callable=AsyncMock):

            await handleLLMCommand(mockUpdate, mockContext)

            mock_set.assert_called_once_with(True)
            call_args = mockUpdate.message.reply_text.call_args[0][0]
            assert "启动" in call_args

    @pytest.mark.asyncio
    async def test_off_disables_llm(self, mockUpdate, mockContext):
        """off 参数关闭 LLM"""
        mockContext.args = ["off"]

        with patch('handlers.llmCommand.hasPermission', return_value=True), \
             patch('handlers.llmCommand.setLLMEnabled') as mock_set, \
             patch('handlers.llmCommand.logAction', new_callable=AsyncMock):

            await handleLLMCommand(mockUpdate, mockContext)

            mock_set.assert_called_once_with(False)
            call_args = mockUpdate.message.reply_text.call_args[0][0]
            assert "关闭" in call_args

    @pytest.mark.asyncio
    async def test_status_shows_config(self, mockUpdate, mockContext):
        """status 参数显示详细配置"""
        mockContext.args = ["status"]

        with patch('handlers.llmCommand.hasPermission', return_value=True), \
             patch('handlers.llmCommand.getLLMEnabled', return_value=True), \
             patch('handlers.llmCommand.getAutoMode', return_value="off"), \
             patch('handlers.llmCommand.getModel', return_value="claude-opus-4-8"), \
             patch('handlers.llmCommand.getVisionModel', return_value="claude-opus-4-8"), \
             patch('handlers.llmCommand.getGroupTriggerMode', return_value="mention"), \
             patch('handlers.llmCommand.getGroupTriggerKeywords', return_value=[]):

            await handleLLMCommand(mockUpdate, mockContext)

            call_args = mockUpdate.message.reply_text.call_args[0][0]
            assert "模型" in call_args

    @pytest.mark.asyncio
    async def test_unknown_command_shows_help(self, mockUpdate, mockContext):
        """未知子命令显示帮助菜单"""
        mockContext.args = ["unknowncmd"]

        with patch('handlers.llmCommand.hasPermission', return_value=True):
            await handleLLMCommand(mockUpdate, mockContext)

            call_args = mockUpdate.message.reply_text.call_args[0][0]
            assert "/llm" in call_args  # 显示用法列表


# ============================================================================
# TestLLMCommandRegister - 注册测试
# ============================================================================

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enabled, expected", [(True, "……"), (False, "喵？")])
    async def test_fade_without_argument_shows_current_state(self, mockUpdate, mockContext, enabled, expected):
        """fade 不带参数时读取当前的提示词回退状态。"""
        mockContext.args = ["fade"]
        with (
            patch("handlers.llmCommand.hasPermission", return_value=True),
            patch("handlers.llmCommand.getForceFallbackPrompt", return_value=enabled) as getFlag,
        ):
            await handleLLMCommand(mockUpdate, mockContext)

        getFlag.assert_called_once_with()
        assert mockUpdate.message.reply_text.call_args.args[0] == expected

    @pytest.mark.asyncio
    async def test_tasks_empty_replies_hint(self, mockUpdate, mockContext):
        """tasks 无运行任务时回复提示。"""
        mockContext.args = ["tasks"]

        with (
            patch('handlers.llmCommand.hasPermission', return_value=True),
            patch('handlers.llmCommand.listRunningTasks', return_value=[]),
        ):
            await handleLLMCommand(mockUpdate, mockContext)

        assert "没有正在运行" in mockUpdate.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_tasks_lists_running_tasks(self, mockUpdate, mockContext):
        """tasks 回复包含 taskID、chat 与预览。"""
        mockContext.args = ["tasks"]

        with (
            patch('handlers.llmCommand.hasPermission', return_value=True),
            patch('handlers.llmCommand.listRunningTasks', return_value=[
                {"taskID": "aaaaaaaa-1111", "chatID": "100", "startedAt": 0.0, "preview": "帮我看代码"},
            ]),
        ):
            await handleLLMCommand(mockUpdate, mockContext)

        call_args = mockUpdate.message.reply_text.call_args[0][0]
        assert "aaaaaaaa-1111" in call_args
        assert "100" in call_args
        assert "帮我看代码" in call_args

    @pytest.mark.asyncio
    async def test_stop_reports_full_task_id_and_logs(self, mockUpdate, mockContext):
        mockContext.args = ["stop", "aaaaaaaa"]

        with (
            patch('handlers.llmCommand.hasPermission', return_value=True),
            patch('handlers.llmCommand.stopTask', return_value="aaaaaaaa-1111") as mockStop,
            patch('handlers.llmCommand.logAction', new_callable=AsyncMock) as mockLog,
        ):
            await handleLLMCommand(mockUpdate, mockContext)

        mockStop.assert_called_once_with("aaaaaaaa")
        assert "aaaaaaaa-1111" in mockUpdate.message.reply_text.call_args[0][0]
        mockLog.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_without_id_shows_usage(self, mockUpdate, mockContext):
        mockContext.args = ["stop"]

        with patch('handlers.llmCommand.hasPermission', return_value=True):
            await handleLLMCommand(mockUpdate, mockContext)

        assert "用法" in mockUpdate.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_stop_error_relays_reason(self, mockUpdate, mockContext):
        mockContext.args = ["stop", "bbbbbbbb"]

        with (
            patch('handlers.llmCommand.hasPermission', return_value=True),
            patch('handlers.llmCommand.stopTask', side_effect=ValueError("没有匹配的运行任务")),
        ):
            await handleLLMCommand(mockUpdate, mockContext)

        assert "没有匹配" in mockUpdate.message.reply_text.call_args[0][0]


class TestLLMCommandRegister:
    """测试 handler 注册"""

    def test_register_returns_correct_metadata(self):
        """register() 返回正确的元数据结构"""
        result = register()

        assert isinstance(result, dict)
        assert "handlers" in result
        assert "name" in result
        assert len(result["handlers"]) == 1  # 只有一个 CommandHandler
