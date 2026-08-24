"""
tests/utils/command/test_history.py

/history 命令测试：渲染直测（假数据驱动——_listChats 的 f-string 列宽笔误
正是「有真实数据才走到」的路径，2026-08 事故回归）+ execute 参数分支烟测。
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.command import history
from utils.command.history import _listChats, execute



def _fakeChats():
    """构造 _listChats 的假会话数据（含负数群 ID、无备注、空时间三种形态）"""
    return [
        {"chat_id": "-1002581002599", "message_count": 123, "last_message_time": datetime(2026, 8, 24, 0, 30, 0)},
        {"chat_id": "7767386015", "message_count": 45, "last_message_time": None},
    ]



@pytest.fixture
def capRender(capsys):
    return capsys



class TestListChatsRendering:
    """渲染直测：真实假数据跑全渲染路径，抓 f-string/列宽类笔误"""

    @patch("utils.command.history.getChatList", new_callable=AsyncMock, return_value=_fakeChats())
    @patch("utils.command.history.loadWhitelistFile", return_value={"allowed": {}, "suspended": {}})
    async def test_list_renders_all_rows(self, _wl, _chats, capsys):
        """有数据时表头 + 每行数据都渲染（列宽笔误会在此抛 ValueError）"""
        await _listChats()
        out = capsys.readouterr().out
        assert "Chat ID" in out and "备注" in out and "消息数" in out
        assert "-1002581002599" in out
        assert "7767386015" in out
        assert "未知" in out          # last_message_time=None 的兜底
        assert "共 2 个会话" in out

    @patch("utils.command.history.getChatList", new_callable=AsyncMock, return_value=_fakeChats())
    @patch("utils.command.history.loadWhitelistFile")
    async def test_list_shows_comment(self, mockWl, _chats, capsys):
        """白名单备注映射到表格（含截断）"""
        mockWl.return_value = {"allowed": {"-1002581002599": {"comment": "测试群备注"}}, "suspended": {}}
        await _listChats()
        assert "测试群备注" in capsys.readouterr().out

    @patch("utils.command.history.getChatList", new_callable=AsyncMock, return_value=[])
    async def test_list_empty_hint(self, _chats, capsys):
        await _listChats()
        assert "暂无任何聊天记录" in capsys.readouterr().out



class TestPreviewBranch:
    @patch("utils.command.history._previewChat", new_callable=AsyncMock)
    async def test_preview_with_chat(self, mockPreview):
        await execute(MagicMock(), ["-c", "12345"])
        mockPreview.assert_awaited_once_with("12345", 20)   # 默认条数来自 config

    @patch("utils.command.history._previewChat", new_callable=AsyncMock)
    async def test_preview_with_count(self, mockPreview):
        await execute(MagicMock(), ["-c", "12345", "-n", "5"])
        mockPreview.assert_awaited_once_with("12345", 5)

    async def test_preview_invalid_count(self, capsys):
        await execute(MagicMock(), ["-c", "12345", "-n", "abc"])
        assert "无效的条数" in capsys.readouterr().out

    async def test_preview_negative_count(self, capsys):
        await execute(MagicMock(), ["-c", "12345", "-n=-5"])
        assert "正整数" in capsys.readouterr().out



class TestExportBranch:
    @patch("utils.command.history._exportAll", new_callable=AsyncMock)
    @patch("utils.command.history.logAction", new_callable=AsyncMock)
    async def test_export_all(self, mockLog, mockExportAll):
        await execute(MagicMock(), ["--export"])
        mockExportAll.assert_awaited_once()

    @patch("utils.command.history._exportChat", new_callable=AsyncMock)
    @patch("utils.command.history.logAction", new_callable=AsyncMock)
    async def test_export_single_chat(self, mockLog, mockExport):
        await execute(MagicMock(), ["--export", "-c", "12345"])
        mockExport.assert_awaited_once_with("12345")

    @patch("utils.command.history._listChats", new_callable=AsyncMock)
    async def test_no_args_lists(self, mockList):
        await execute(MagicMock(), [])
        mockList.assert_awaited_once()
