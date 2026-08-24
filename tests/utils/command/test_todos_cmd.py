"""
tests/utils/command/test_todos_cmd.py

控制台 /todos 命令测试：add（优先级/时间解析后的内容校验）、done/reopen/del
的 ID 校验与属主检查、list 状态筛选、无参默认分支。数据库层全 mock
（database 行为由 tests/utils/todos/ 覆盖，这里只测命令分支路由）。

命名 test_todos_cmd 避免与 tests/handlers/test_todos.py（Telegram 端）混淆。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.command import todos



def _todo(id=1, content="测试条目", userID="console"):
    """命令层控制台虚拟用户是 'console'（todos.CONSOLE_ID，小写字符串）"""
    return {"id": id, "content": content, "user_id": userID, "status": "pending", "priority": 0, "remind_at": None}



class TestAddBranch:
    @patch("utils.command.todos.addTodo", new_callable=AsyncMock, return_value=7)
    async def test_add_prints_confirmation(self, mockAdd, capsys):
        await todos.execute(MagicMock(), ["-a", "去买猫粮"])
        out = capsys.readouterr().out
        assert "[ID 7]" in out and "去买猫粮" in out
        mockAdd.assert_awaited_once()

    async def test_add_empty_content_rejected(self, capsys):
        """内容剥掉优先级/时间标记后为空 → 拒绝"""
        await todos.execute(MagicMock(), ["-a", "   "])
        assert "不能为空" in capsys.readouterr().out

    @patch("utils.command.todos.addTodo", new_callable=AsyncMock, return_value=None)
    async def test_add_db_failure(self, mockAdd, capsys):
        await todos.execute(MagicMock(), ["-a", "内容"])
        assert "数据库写入出错" in capsys.readouterr().out



class TestDoneBranch:
    @patch("utils.command.todos.markDone", new_callable=AsyncMock, return_value=True)
    @patch("utils.command.todos.getTodoByID", new_callable=AsyncMock, return_value=_todo())
    async def test_done_marks(self, mockGet, mockDone, capsys):
        await todos.execute(MagicMock(), ["-done", "1"])
        assert "标记完成" in capsys.readouterr().out
        mockDone.assert_awaited_once_with(1)

    async def test_done_non_numeric(self, capsys):
        await todos.execute(MagicMock(), ["-done", "abc"])
        assert "必须是数字" in capsys.readouterr().out

    @patch("utils.command.todos.getTodoByID", new_callable=AsyncMock, return_value=None)
    async def test_done_not_found(self, mockGet, capsys):
        await todos.execute(MagicMock(), ["-done", "99"])
        assert "找不到" in capsys.readouterr().out

    @patch("utils.command.todos.getTodoByID", new_callable=AsyncMock, return_value=_todo(userID="777"))
    async def test_done_owner_mismatch(self, mockGet, capsys):
        """条目不属于控制台虚拟用户 → 拒绝"""
        await todos.execute(MagicMock(), ["-done", "1"])
        assert "并不属于" in capsys.readouterr().out

    @patch("utils.command.todos.markDone", new_callable=AsyncMock, return_value=False)
    @patch("utils.command.todos.getTodoByID", new_callable=AsyncMock, return_value=_todo())
    async def test_done_db_error(self, mockGet, mockDone, capsys):
        """markDone 返回 False → 数据库错误提示"""
        await todos.execute(MagicMock(), ["-done", "1"])
        assert "数据库出错" in capsys.readouterr().out



class TestReopenAndDel:
    @patch("utils.command.todos.reopenTodo", new_callable=AsyncMock, return_value=True)
    @patch("utils.command.todos.getTodoByID", new_callable=AsyncMock, return_value=_todo())
    async def test_reopen(self, mockGet, mockRe, capsys):
        await todos.execute(MagicMock(), ["-reopen", "1"])
        assert "已重新打开" in capsys.readouterr().out

    @patch("utils.command.todos.deleteTodo", new_callable=AsyncMock, return_value=True)
    @patch("utils.command.todos.getTodoByID", new_callable=AsyncMock, return_value=_todo())
    async def test_del(self, mockGet, mockDel, capsys):
        await todos.execute(MagicMock(), ["-d", "1"])
        assert "已删除" in capsys.readouterr().out
        mockDel.assert_awaited_once_with(1)

    async def test_del_non_numeric(self, capsys):
        await todos.execute(MagicMock(), ["-d", "xyz"])
        assert "必须是数字" in capsys.readouterr().out



class TestListAndDefault:
    @patch("utils.command.todos.printTodoList")
    @patch("utils.command.todos.getTodos", new_callable=AsyncMock, return_value=[])
    async def test_list_renders(self, mockGet, mockPrint):
        await todos.execute(MagicMock(), ["-l", "all"])
        mockGet.assert_awaited_once()
        mockPrint.assert_called_once()

    async def test_list_invalid_status(self, capsys):
        await todos.execute(MagicMock(), ["-l", "bogus"])
        assert "无效状态" in capsys.readouterr().out

    @patch("utils.command.todos.printTodoList")
    @patch("utils.command.todos.getTodos", new_callable=AsyncMock, return_value=[])
    async def test_no_args_defaults_pending(self, mockGet, mockPrint):
        await todos.execute(MagicMock(), [])
        # 无参走 list 且默认 pending
        kwargs = mockGet.await_args.kwargs
        assert kwargs.get("status") == "pending"

    @patch("utils.command.todos.printOverview")
    @patch("utils.command.todos.getUsersTodosSummary", new_callable=AsyncMock, return_value=[])
    async def test_overview(self, mockSummary, mockPrint):
        await todos.execute(MagicMock(), ["-ov"])
        mockPrint.assert_called_once()
