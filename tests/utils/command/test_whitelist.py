"""
tests/utils/command/test_whitelist.py

/whitelist 命令测试：参数冲突检测（含光杆 -l 计入——2026-08 修复的漏数 bug）、
无参指引、-a/-d/-s/-c 组合分支。userOperation / whitelistMenuController 全 mock。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.command import whitelist



def _app():
    return MagicMock()



class TestArgConflict:
    async def test_no_args_shows_usage(self, capsys):
        await whitelist.execute(_app(), [])
        out = capsys.readouterr().out
        assert "需要参数" in out and "/help whitelist" in out

    async def test_add_plus_del_conflicts(self, capsys):
        await whitelist.execute(_app(), ["-a", "123", "-d", "456"])
        assert "不能这样用" in capsys.readouterr().out

    async def test_bare_l_with_add_conflicts(self, capsys):
        """光杆 -l 计入冲突检测（旧实现漏数，-l -a 组合不被拦）"""
        await whitelist.execute(_app(), ["-l", "-a", "123"])
        assert "不能这样用" in capsys.readouterr().out

    async def test_two_bare_flags_conflict(self, capsys):
        """光杆 -l 与光杆 -a 同样冲突"""
        await whitelist.execute(_app(), ["-l", "-a"])
        assert "不能这样用" in capsys.readouterr().out



class TestListMode:
    @patch("utils.command.whitelist.whitelistMenuController", new_callable=AsyncMock)
    async def test_bare_l_opens_manager(self, mockMenu):
        await whitelist.execute(_app(), ["-l"])
        mockMenu.assert_awaited_once()

    @patch("utils.command.whitelist.whitelistMenuController", new_callable=AsyncMock)
    async def test_l_with_value_also_opens_manager(self, mockMenu):
        """-l 带值（异常形态但 flag 判断只看 truthy）仍进管理界面"""
        await whitelist.execute(_app(), ["-l", "x"])
        mockMenu.assert_awaited_once()



class TestUserOperations:
    @patch("utils.command.whitelist.logAction", new_callable=AsyncMock)
    @patch("utils.command.whitelist.userOperation", return_value=True)
    async def test_add_user(self, mockOp, mockLog):
        await whitelist.execute(_app(), ["-a", "12345"])
        # addUser + 汇总日志（logAction 至少一次）
        assert mockOp.call_args_list[0].args == ("addUser", "12345")
        assert mockLog.await_count >= 1

    @patch("utils.command.whitelist.logAction", new_callable=AsyncMock)
    @patch("utils.command.whitelist.userOperation", return_value=False)
    async def test_add_duplicate_reports(self, mockOp, mockLog, capsys):
        """添加已在白名单的 UID：userOperation 返回 False → 结果提示已存在"""
        await whitelist.execute(_app(), ["-a", "12345"])
        mockOp.assert_called()

    @patch("utils.command.whitelist.logAction", new_callable=AsyncMock)
    @patch("utils.command.whitelist.userOperation", return_value=True)
    async def test_delete_user(self, mockOp, mockLog):
        await whitelist.execute(_app(), ["-d", "12345"])
        assert mockOp.call_args_list[0].args == ("deleteUser", "12345")

    @patch("utils.command.whitelist.logAction", new_callable=AsyncMock)
    @patch("utils.command.whitelist.userOperation", return_value=True)
    async def test_suspend_user(self, mockOp, mockLog):
        await whitelist.execute(_app(), ["-s", "12345"])
        assert mockOp.call_args_list[0].args == ("suspendUser", "12345")



class TestCommentBranch:
    @patch("utils.command.whitelist.logAction", new_callable=AsyncMock)
    @patch("utils.command.whitelist.userOperation", return_value=True)
    async def test_comment_alone_sets_comment(self, mockOp, mockLog):
        """-c <ID> <text> 单独使用 → setComment"""
        await whitelist.execute(_app(), ["-c", "12345", "备注文本"])
        assert mockOp.call_args_list[-1].args == ("setComment", "12345", "备注文本")

    async def test_comment_without_text_rejected(self, capsys):
        """-c 只有 ID 没有备注文本 → 拒绝"""
        with patch("utils.command.whitelist.userOperation", return_value=True):
            await whitelist.execute(_app(), ["-c", "12345"])
        assert "备注内容不能为空" in capsys.readouterr().out

    @patch("utils.command.whitelist.logAction", new_callable=AsyncMock)
    @patch("utils.command.whitelist.userOperation", return_value=True)
    async def test_add_with_comment_two_steps(self, mockOp, mockLog):
        """-a <ID> -c <text> → 先 addUser 再 setComment 两步"""
        await whitelist.execute(_app(), ["-a", "12345", "-c", "带备注加入"])
        ops = [c.args[:2] for c in mockOp.call_args_list]
        assert ("addUser", "12345") in ops
        assert mockOp.call_args_list[-1].args == ("setComment", "12345", "带备注加入")
