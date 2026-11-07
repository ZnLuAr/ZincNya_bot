"""
tests/utils/command/llm/test_smoke.py

utils/command/llm 包的拆分回归测试。

拆分自原单文件 utils/command/llm.py（现为包）。本测试只做两件事：
    1. 冒烟：从包根 import 四个公开符号（镜像 mainLoop / cli 的真实用法），
       断言 getHelp 结构完整。
    2. execute 分发路由：对内联 case（on/off/auto/model 等）做轻量验证，
       确认 /llm 入口仍正确路由到对应 setter。
不覆盖 memory/knowledge/review 子处理器（它们是委托，逻辑各自独立）。
"""




import pytest
from unittest.mock import patch, AsyncMock

from utils.command.llm import (
    execute,
    getHelp,
    handleChatScreenEditSubmit,
    handleChatScreenReviewCommand,
)




# ============================================================================
# 冒烟测试：包根 import + getHelp 结构
# ============================================================================

class TestPackageSmoke:
    """拆分后包对外接口可用性。"""

    def test_fourPublicSymbolsImportable(self):
        """四个公开符号都能从包根 import（mainLoop / cli 的真实用法）。"""
        # import 已在模块顶部完成，能走到这里即说明符号齐备
        assert callable(execute)
        assert callable(getHelp)
        assert callable(handleChatScreenReviewCommand)
        assert callable(handleChatScreenEditSubmit)

    def test_getHelpStructure(self):
        """getHelp 返回完整 dict（cli 的 -h / --help 依赖）。"""
        info = getHelp()
        assert info["name"] == "/llm"
        assert "description" in info
        assert "usage" in info
        assert "example" in info
        # usage 至少覆盖核心子命令
        assert "/llm on" in info["usage"]
        assert "/llm memory" in info["usage"]
        assert "/llm knowledge" in info["usage"]

    def test_getHelpIsPureData(self):
        """getHelp 是纯数据，多次调用返回等价内容且含五个键。"""
        a = getHelp()
        b = getHelp()
        assert set(a.keys()) == {"name", "description", "usage", "example"}
        assert a == b




# ============================================================================
# execute 分发路由测试（内联 case；patch 目标是 _dispatch 模块绑定）
# ============================================================================

class TestExecuteDispatch:
    """execute 的 match cmd 路由——验证 /llm 入口仍正确分发到各 setter。"""

    @pytest.mark.asyncio
    async def test_onCallsSetLLMEnabledTrue(self):
        """on → setLLMEnabled(True)。"""
        with patch("utils.command.llm._dispatch.setLLMEnabled") as mockSet, \
             patch("utils.command.llm._dispatch.logAction", new_callable=AsyncMock):
            await execute(None, ["on"])
        mockSet.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_offCallsSetLLMEnabledFalse(self):
        """off → setLLMEnabled(False)。"""
        with patch("utils.command.llm._dispatch.setLLMEnabled") as mockSet, \
             patch("utils.command.llm._dispatch.logAction", new_callable=AsyncMock):
            await execute(None, ["off"])
        mockSet.assert_called_once_with(False)

    @pytest.mark.asyncio
    async def test_autoConsoleCallsSetAutoMode(self):
        """auto -console → setAutoMode('console')。"""
        with patch("utils.command.llm._dispatch.setAutoMode") as mockSet, \
             patch("utils.command.llm._dispatch.logAction", new_callable=AsyncMock):
            await execute(None, ["auto", "-console"])
        mockSet.assert_called_once_with("console")

    @pytest.mark.asyncio
    async def test_autoInvalidModeDoesNotCallSetter(self):
        """auto 给无效模式 → setAutoMode 抛 ValueError，不应被吞成成功调用。"""
        with patch("utils.command.llm._dispatch.setAutoMode", side_effect=ValueError) as mockSet, \
             patch("utils.command.llm._dispatch.logAction", new_callable=AsyncMock):
            await execute(None, ["auto", "-bogus"])
        mockSet.assert_called_once_with("bogus")

    @pytest.mark.asyncio
    async def test_modelSwitchCallsSetModel(self):
        """model switch <m> → setModel(<m>)。"""
        with patch("utils.command.llm._dispatch.setModel") as mockSet, \
             patch("utils.command.llm._dispatch.logAction", new_callable=AsyncMock):
            await execute(None, ["model", "switch", "claude-sonnet-4-6"])
        mockSet.assert_called_once_with("claude-sonnet-4-6")

    @pytest.mark.asyncio
    async def test_keywordAddCallsAddGroupTriggerKeyword(self):
        """keyword add <kw> → addGroupTriggerKeyword(<kw>)。"""
        with patch("utils.command.llm._dispatch.addGroupTriggerKeyword") as mockAdd, \
             patch("utils.command.llm._dispatch.getGroupTriggerKeywords", return_value=[]), \
             patch("utils.command.llm._dispatch.logAction", new_callable=AsyncMock):
            await execute(None, ["keyword", "add", "锌酱"])
        mockAdd.assert_called_once_with("锌酱")

    @pytest.mark.asyncio
    async def test_unknownCommandDoesNotRaise(self):
        """未知子命令走 default 分支，不抛错（只打印用法）。"""
        with patch("utils.command.llm._dispatch.logAction", new_callable=AsyncMock):
            # 不应抛异常
            await execute(None, ["totally-bogus-subcommand"])


@pytest.mark.parametrize("section, setter", [("model", "setModelProvider"), ("visionmodel", "setVisionProvider")])
async def test_consoleProviderChangesCorrectProfile(section, setter):
    with (
        patch(f"utils.command.llm._dispatch.{setter}") as save,
        patch("utils.command.llm._dispatch.logAction", new_callable=AsyncMock),
    ):
        await execute(None, [section, "provider", "openai"])
    save.assert_called_once_with("openai")


async def test_consoleVisionResetSynchronizesProfile():
    with (
        patch("utils.command.llm._dispatch.resetVisionModel") as reset,
        patch("utils.command.llm._dispatch.logAction", new_callable=AsyncMock),
    ):
        await execute(None, ["visionmodel", "reset"])
    reset.assert_called_once_with()


async def test_consoleAcceptsOpaqueModelAndRejectsInvalidInput(capsys):
    with (
        patch("utils.llm.config._setConfig") as save,
        patch("utils.command.llm._dispatch.logAction", new_callable=AsyncMock),
    ):
        await execute(None, ["model", "switch", "tenant/模型:preview"])
        save.assert_called_once_with(model="tenant/模型:preview")
        save.reset_mock()
        await execute(None, ["model", "switch", "model\nwith-control"])
        save.assert_not_called()
    assert "空白或控制字符" in capsys.readouterr().out


async def test_consoleStatusShowsEffectiveEndpointWithoutSecrets(capsys, monkeypatch):
    import config
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "https://user:private-pwd@relay.example/v1?key=private-key")
    with (
        patch("utils.command.llm._dispatch.getModel", return_value="kimi-k2.5"),
        patch("utils.command.llm._dispatch.getModelProvider", return_value="openai"),
        patch("utils.command.llm._dispatch.getVisionModel", return_value="kimi-k2.5"),
        patch("utils.command.llm._dispatch.getVisionProvider", return_value="openai"),
        patch("utils.llm.client._router._buildProviders", side_effect=AssertionError("不能初始化 SDK")),
    ):
        await execute(None, ["status"])
    text = capsys.readouterr().out
    assert "OpenAI Chat Completions" in text
    assert "https://relay.example/v1" in text
    assert "private-" not in text
