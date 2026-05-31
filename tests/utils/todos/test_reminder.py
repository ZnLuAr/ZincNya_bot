"""
tests/utils/todos/test_reminder.py

测试 utils/todos/reminder.py
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch, AsyncMock, MagicMock

from utils.todos.reminder import todoReminderLoop


# ============================================================================
# todoReminderLoop() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_reminder_loop_no_pending():
    """无待提醒的待办"""
    mock_app = MagicMock()
    mock_app.bot = MagicMock()

    with patch('utils.todos.reminder.getPendingReminders', new_callable=AsyncMock) as mock_get:
        with patch('utils.todos.reminder.asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            mock_get.return_value = []

            # 第一次 sleep 正常返回，第二次抛出 CancelledError
            sleep_call = 0

            async def _sleep_side_effect(*args):
                nonlocal sleep_call
                sleep_call += 1
                if sleep_call >= 2:
                    raise asyncio.CancelledError()

            mock_sleep.side_effect = _sleep_side_effect

            # 应该正常退出，不抛异常
            await todoReminderLoop(mock_app)

            # 应该调用了 getPendingReminders
            mock_get.assert_called_once()


@pytest.mark.asyncio
async def test_reminder_loop_send_telegram_reminder():
    """发送 Telegram 提醒"""
    mock_app = MagicMock()
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_app.bot = mock_bot

    pending_todo = {
        'id': 1,
        'chat_id': 'chat123',
        'user_id': 'user456',
        'content': '开会',
        'priority': 'P0',
        'remind_time': datetime.now() - timedelta(minutes=5),
    }

    with patch('utils.todos.reminder.getPendingReminders', new_callable=AsyncMock) as mock_get:
        with patch('utils.todos.reminder.updateTodo', new_callable=AsyncMock) as mockUpdate:
            with patch('utils.todos.reminder.logSystemEvent', new_callable=AsyncMock):
                with patch('utils.todos.reminder.asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
                    # 第一次返回待办，第二次取消循环
                    call_count = 0

                    async def _get_side_effect():
                        nonlocal call_count
                        call_count += 1
                        if call_count == 1:
                            return [pending_todo]
                        raise asyncio.CancelledError()

                    mock_get.side_effect = _get_side_effect

                    await todoReminderLoop(mock_app)

                    # 应该发送了消息
                    mock_bot.send_message.assert_called_once()
                    call_args = mock_bot.send_message.call_args
                    assert call_args[1]['chat_id'] == 'chat123'
                    assert '开会' in call_args[1]['text']
                    assert 'P0' in call_args[1]['text']

                    # 应该标记为已提醒
                    mockUpdate.assert_called_once_with(1, reminded=1)


@pytest.mark.asyncio
async def test_reminder_loop_console_todo():
    """控制台待办提醒"""
    mock_app = MagicMock()
    mock_app.bot = MagicMock()

    console_todo = {
        'id': 1,
        'chat_id': 'console',
        'user_id': 'console',
        'content': '控制台任务',
        'priority': 'P1',
        'remind_time': datetime.now() - timedelta(minutes=5),
    }

    with patch('utils.todos.reminder.getPendingReminders', new_callable=AsyncMock) as mock_get:
        with patch('utils.todos.reminder.updateTodo', new_callable=AsyncMock) as mockUpdate:
            with patch('utils.todos.reminder.logSystemEvent', new_callable=AsyncMock):
                with patch('utils.todos.reminder.getStateManager') as mock_state:
                    with patch('utils.todos.reminder.asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
                        mock_state.return_value.isInteractive.return_value = False

                        call_count = 0

                        async def _get_side_effect():
                            nonlocal call_count
                            call_count += 1
                            if call_count == 1:
                                return [console_todo]
                            raise asyncio.CancelledError()

                        mock_get.side_effect = _get_side_effect

                        await todoReminderLoop(mock_app)

                        # 应该标记为已提醒
                        mockUpdate.assert_called_once_with(1, reminded=1)


@pytest.mark.asyncio
async def test_reminder_loop_send_message_error():
    """发送消息失败时继续处理"""
    mock_app = MagicMock()
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock(side_effect=Exception("Network error"))
    mock_app.bot = mock_bot

    pending_todo = {
        'id': 1,
        'chat_id': 'chat123',
        'user_id': 'user456',
        'content': '任务',
        'priority': 'P_',
        'remind_time': datetime.now() - timedelta(minutes=5),
    }

    with patch('utils.todos.reminder.getPendingReminders', new_callable=AsyncMock) as mock_get:
        with patch('utils.todos.reminder.updateTodo', new_callable=AsyncMock) as mockUpdate:
            with patch('utils.todos.reminder.logSystemEvent', new_callable=AsyncMock) as mock_log:
                with patch('utils.todos.reminder.asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
                    call_count = 0

                    async def _get_side_effect():
                        nonlocal call_count
                        call_count += 1
                        if call_count == 1:
                            return [pending_todo]
                        raise asyncio.CancelledError()

                    mock_get.side_effect = _get_side_effect

                    await todoReminderLoop(mock_app)

                    # 应该记录了错误日志
                    assert any('失败' in str(call) for call in mock_log.call_args_list)

                    # 不应该标记为已提醒（因为发送失败）
                    mockUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_reminder_loop_multiple_todos():
    """处理多个待办"""
    mock_app = MagicMock()
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_app.bot = mock_bot

    todos = [
        {
            'id': 1,
            'chat_id': 'chat1',
            'user_id': 'user1',
            'content': '任务1',
            'priority': 'P0',
            'remind_time': datetime.now() - timedelta(minutes=10),
        },
        {
            'id': 2,
            'chat_id': 'chat2',
            'user_id': 'user2',
            'content': '任务2',
            'priority': 'P1',
            'remind_time': datetime.now() - timedelta(minutes=5),
        },
    ]

    with patch('utils.todos.reminder.getPendingReminders', new_callable=AsyncMock) as mock_get:
        with patch('utils.todos.reminder.updateTodo', new_callable=AsyncMock) as mockUpdate:
            with patch('utils.todos.reminder.logSystemEvent', new_callable=AsyncMock):
                with patch('utils.todos.reminder.asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
                    call_count = 0

                    async def _get_side_effect():
                        nonlocal call_count
                        call_count += 1
                        if call_count == 1:
                            return todos
                        raise asyncio.CancelledError()

                    mock_get.side_effect = _get_side_effect

                    await todoReminderLoop(mock_app)

                    # 应该发送了 2 条消息
                    assert mock_bot.send_message.call_count == 2

                    # 应该标记了 2 个待办为已提醒
                    assert mockUpdate.call_count == 2


@pytest.mark.asyncio
async def test_reminder_loop_exception_handling():
    """循环异常处理（不崩溃）"""
    mock_app = MagicMock()

    with patch('utils.todos.reminder.getPendingReminders', new_callable=AsyncMock) as mock_get:
        with patch('utils.todos.reminder.logSystemEvent', new_callable=AsyncMock) as mock_log:
            with patch('utils.todos.reminder.asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
                call_count = 0

                async def _get_side_effect():
                    nonlocal call_count
                    call_count += 1
                    if call_count == 1:
                        raise Exception("Database error")
                    raise asyncio.CancelledError()

                mock_get.side_effect = _get_side_effect

                await todoReminderLoop(mock_app)

                # 应该记录了错误日志
                assert any('出错' in str(call) for call in mock_log.call_args_list)