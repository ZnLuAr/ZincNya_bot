"""
tests/utils/command/test_send.py

/send 命令测试：无参指引、-c 三态（无/光杆/带值）、-t/-id 校验、sendMsg 发送循环。
chatScreen 全屏路径不在本文件测（TUI 交互脆弱，见 tests/utils/chatScreen/）。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.command import send



def _app():
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    return app



class TestNoArgs:
    async def test_no_args_shows_usage(self, capsys):
        await send.execute(_app(), [])
        out = capsys.readouterr().out
        assert "用法" in out and "/send" in out

    async def test_only_at_shows_usage(self, capsys):
        """-a 单独出现（无 -t/-id/-c）同样指引"""
        await send.execute(_app(), ["-a", "someone"])
        assert "用法" in capsys.readouterr().out



class TestChatModeThreeStates:
    @patch("utils.command.send.chatIDList", new_callable=AsyncMock, return_value="12345")
    @patch("utils.chatScreen.chatScreen", new_callable=AsyncMock, return_value=None)
    async def test_bare_c_picks_then_enters(self, mockScreen, mockPick, capsys):
        """-c 光杆 → 弹选择 → 拿到 ID 进 chatScreen"""
        await send.execute(_app(), ["-c"])
        mockPick.assert_awaited_once()
        mockScreen.assert_awaited()

    @patch("utils.command.send.chatIDList", new_callable=AsyncMock, return_value=None)
    async def test_bare_c_no_selection(self, mockPick, capsys):
        """-c 光杆 → 选择落空 → 兜底提示"""
        await send.execute(_app(), ["-c"])
        assert "没有选择聊天对象" in capsys.readouterr().out

    @patch("utils.chatScreen.chatScreen", new_callable=AsyncMock, return_value=None)
    async def test_c_with_id_enters_directly(self, mockScreen):
        await send.execute(_app(), ["-c", "12345"])
        mockScreen.assert_awaited_once()



class TestSendValidation:
    async def test_missing_text(self, capsys):
        await send.execute(_app(), ["-id", "12345"])
        assert "-t" in capsys.readouterr().out

    async def test_missing_id(self, capsys):
        await send.execute(_app(), ["-t", "hi"])
        assert "-id" in capsys.readouterr().out

    @patch("utils.command.send.sendMsg", new_callable=AsyncMock)
    async def test_valid_send_invokes(self, mockSend):
        await send.execute(_app(), ["-id", "12345", "-t", "hello"])
        mockSend.assert_awaited_once()



class TestSendMsg:
    async def test_send_to_multiple_ids(self):
        app = _app()
        with patch("utils.command.send.logAction", new_callable=AsyncMock):
            await send.sendMsg(app.bot, ["1", "2"], None, "hi")
        assert app.bot.send_message.await_count == 2

    async def test_send_with_at_prefix(self):
        app = _app()
        with patch("utils.command.send.logAction", new_callable=AsyncMock):
            await send.sendMsg(app.bot, ["1"], "user", "hi")
        sentText = app.bot.send_message.await_args.kwargs["text"]
        assert sentText == "@user hi"

    async def test_send_failure_logged_not_raised(self):
        """发送异常不冒泡（循环内逐个兜底）"""
        app = _app()
        app.bot.send_message = AsyncMock(side_effect=Exception("network"))
        with patch("utils.command.send.logAction", new_callable=AsyncMock) as mockLog:
            await send.sendMsg(app.bot, ["1"], None, "hi")   # 不抛即通过
        assert mockLog.await_count == 1
