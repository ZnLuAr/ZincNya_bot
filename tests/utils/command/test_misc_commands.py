"""
tests/utils/command/test_misc_commands.py

低分支命令的烟测：reboot / shutdown / nya / clear / log / news。
各命令 2-5 例，锁分支路由与关键契约（SHUTDOWN 返回值、无参默认行为），不深入业务。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.command import clear as clearCmd
from utils.command import log as logCmd
from utils.command import news as newsCmd
from utils.command import nya as nyaCmd
from utils.command import reboot as rebootCmd
from utils.command import shutdown as shutdownCmd



class TestReboot:
    @patch("utils.command.reboot.logAction", new_callable=AsyncMock)
    async def test_returns_shutdown_and_requests_reboot(self, mockLog):
        """/reboot 契约：返回 SHUTDOWN 接入退出链 + requestRestart 被调"""
        app = MagicMock()
        app.bot = MagicMock()
        with patch("utils.command.reboot.getStateManager") as mockSm:
            result = await rebootCmd.execute(app, [])
        assert result == "SHUTDOWN"
        mockSm.return_value.requestRestart.assert_called_once()

    def test_get_help_dict(self):
        info = rebootCmd.getHelp()
        assert info["name"] == "/reboot" and "description" in info



class TestShutdown:
    @patch("utils.command.shutdown.logAction", new_callable=AsyncMock)
    async def test_returns_shutdown(self, mockLog, capsys):
        result = await shutdownCmd.execute(MagicMock(), [])
        assert result == "SHUTDOWN"
        assert capsys.readouterr().out  # 退场文案有输出



class TestNya:
    @patch("utils.command.nya.getRandomQuote", return_value=["喵呜~", "语录内容"])
    async def test_no_args_prints_quote(self, mockQuote, capsys):
        await nyaCmd.execute(MagicMock(), [])
        assert "语录内容" in capsys.readouterr().out

    @patch("utils.command.nya.getRandomQuote", return_value=None)
    async def test_no_quotes_hint(self, mockQuote, capsys):
        await nyaCmd.execute(MagicMock(), [])
        assert "说不出话" in capsys.readouterr().out

    @patch("utils.command.nya.quoteMenuController", new_callable=AsyncMock)
    async def test_bare_e_opens_menu(self, mockMenu):
        await nyaCmd.execute(MagicMock(), ["-e"])
        mockMenu.assert_awaited_once()



class TestClear:
    @patch("utils.command.clear.clearLines")
    async def test_n_clears_lines(self, mockClearLines):
        await clearCmd.execute(MagicMock(), ["-n", "5"])
        mockClearLines.assert_called_once_with(5)

    async def test_n_invalid(self, capsys):
        await clearCmd.execute(MagicMock(), ["-n", "abc"])
        assert "无效的行数" in capsys.readouterr().out

    @patch("utils.command.clear.printStartupBanner")
    @patch("utils.command.clear.clearScreen")
    async def test_r_resets(self, mockScreen, mockBanner):
        await clearCmd.execute(MagicMock(), ["-r"])
        mockScreen.assert_called_once()
        mockBanner.assert_called_once()

    @patch("utils.command.clear.clearScreen")
    async def test_no_args_clears_screen(self, mockScreen):
        await clearCmd.execute(MagicMock(), [])
        mockScreen.assert_called_once()



class TestLog:
    @patch("utils.command.log._viewLog")
    async def test_t_views(self, mockView):
        await logCmd.execute(MagicMock(), ["-t", "1"])
        mockView.assert_called_once_with(1)

    async def test_t_invalid_hint(self, capsys):
        await logCmd.execute(MagicMock(), ["-t", "abc"])
        assert "有效的日志序号" in capsys.readouterr().out

    @patch("utils.command.log._deleteLog")
    async def test_d_deletes(self, mockDel):
        await logCmd.execute(MagicMock(), ["-d", "2"])
        mockDel.assert_called_once_with(2)

    @patch("utils.command.log._cleanEmptyLogs")
    async def test_c_cleans(self, mockClean):
        await logCmd.execute(MagicMock(), ["-c"])
        mockClean.assert_called_once()

    @patch("utils.command.log._listLogs")
    async def test_no_args_lists(self, mockList):
        await logCmd.execute(MagicMock(), [])
        mockList.assert_called_once()



class TestNews:
    @patch("utils.command.news.testFetch", new_callable=AsyncMock)
    async def test_test_branch(self, mockTest):
        await newsCmd.execute(MagicMock(), ["-test"])
        mockTest.assert_awaited_once()

    @patch("utils.command.news.showPushedList")
    async def test_list_branch(self, mockList):
        await newsCmd.execute(MagicMock(), ["-list"])
        mockList.assert_called_once()

    async def test_no_args_shows_usage(self, capsys):
        await newsCmd.execute(MagicMock(), [])
        assert "/news" in capsys.readouterr().out
