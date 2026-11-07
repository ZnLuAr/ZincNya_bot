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


class TestLLMCommandRegister:
    """测试 handler 注册"""

    def test_register_returns_correct_metadata(self):
        """register() 返回正确的元数据结构"""
        result = register()

        assert isinstance(result, dict)
        assert "handlers" in result
        assert "name" in result
        assert len(result["handlers"]) == 1  # 只有一个 CommandHandler




@pytest.mark.parametrize("section, setter, model", [
    ("model", "setModel", "tenant/kimi-k2.5:preview"),
    ("visionmodel", "setVisionModel", "组织/视觉:latest"),
])
async def test_modelSwitchAcceptsOpaqueIdentifiers(mockUpdate, mockContext, section, setter, model):
    mockContext.args = [section, "switch", model]
    with (
        patch("handlers.llmCommand.hasPermission", return_value=True),
        patch(f"handlers.llmCommand.{setter}") as save,
        patch("handlers.llmCommand.logAction", new_callable=AsyncMock),
    ):
        await handleLLMCommand(mockUpdate, mockContext)
    save.assert_called_once_with(model)
    assert model in mockUpdate.message.reply_text.await_args.args[0]


@pytest.mark.parametrize("section, setter", [("model", "setModelProvider"), ("visionmodel", "setVisionProvider")])
async def test_providerCommandChangesCorrectProfile(mockUpdate, mockContext, section, setter):
    mockContext.args = [section, "provider", "openai"]
    with (
        patch("handlers.llmCommand.hasPermission", return_value=True),
        patch(f"handlers.llmCommand.{setter}") as save,
        patch("handlers.llmCommand.logAction", new_callable=AsyncMock),
    ):
        await handleLLMCommand(mockUpdate, mockContext)
    save.assert_called_once_with("openai")


async def test_visionResetUsesAtomicModelAndProviderSync(mockUpdate, mockContext):
    mockContext.args = ["visionmodel", "reset"]
    with (
        patch("handlers.llmCommand.hasPermission", return_value=True),
        patch("handlers.llmCommand.resetVisionModel") as reset,
        patch("handlers.llmCommand.logAction", new_callable=AsyncMock),
    ):
        await handleLLMCommand(mockUpdate, mockContext)
    reset.assert_called_once_with()
    assert "名称和调用配置组" in mockUpdate.message.reply_text.await_args.args[0]


async def test_invalidProviderGivesChineseError(mockUpdate, mockContext):
    mockContext.args = ["model", "provider", "bad-provider"]
    with (
        patch("handlers.llmCommand.hasPermission", return_value=True),
        patch("utils.llm.config._setConfig") as save,
    ):
        await handleLLMCommand(mockUpdate, mockContext)
    assert "无效" in mockUpdate.message.reply_text.await_args.args[0]
    save.assert_not_called()


async def test_statusExplainsRoutesWithoutSDKOrCredentialLeak(mockUpdate, mockContext, monkeypatch):
    import config
    mockContext.args = ["status"]
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "https://private-user:private-pwd@relay.example/v1?key=private-key#private-fragment")
    with (
        patch("handlers.llmCommand.hasPermission", return_value=True),
        patch("handlers.llmCommand.getModel", return_value="kimi-k2.5"),
        patch("handlers.llmCommand.getModelProvider", return_value="openai"),
        patch("handlers.llmCommand.getVisionModel", return_value="vision:custom"),
        patch("handlers.llmCommand.getVisionProvider", return_value="openai"),
        patch("utils.llm.client._router._buildProviders", side_effect=AssertionError("不能初始化 SDK")),
    ):
        await handleLLMCommand(mockUpdate, mockContext)
    text = mockUpdate.message.reply_text.await_args.args[0]
    assert "OpenAI Chat Completions" in text
    assert "https://relay.example/v1" in text
    assert "显式配置" in text
    assert "private-" not in text




@pytest.mark.parametrize("section", ["model", "visionmodel"])
async def test_modelCommandReportsSetterValidationWithoutSaving(mockUpdate, mockContext, section):
    """命令把非法名称交给配置入口校验，提示错误且不落盘。"""
    mockContext.args = [section, "switch", "invalid\nmodel"]
    with (
        patch("handlers.llmCommand.hasPermission", return_value=True),
        patch("utils.llm.config._setConfig") as save,
    ):
        await handleLLMCommand(mockUpdate, mockContext)
    save.assert_not_called()
    assert "空白或控制字符" in mockUpdate.message.reply_text.await_args.args[0]
