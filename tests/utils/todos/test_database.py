"""
tests/utils/todos/test_database.py

测试 utils/todos/database.py
"""

import pytest
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch, AsyncMock

from utils.todos.database import (
    _rowToTodoDict,
    addTodo,
    getTodos,
    getTodoByID,
    updateTodo,
    deleteTodo,
    markDone,
    reopenTodo,
    getTodosCount,
    getUsersTodosSummary,
    getPendingReminders,
    TIMESTAMP_FORMAT,
)
from utils.core.crypto import decryptText


# ============================================================================
# Fixture: 初始化 todos 表
# ============================================================================

@pytest.fixture
async def todos_db(inMemoryDb):
    """初始化 todos 表结构"""
    conn = inMemoryDb
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            remind_time DATETIME,
            priority TEXT DEFAULT 'P_',
            status TEXT DEFAULT 'pending',
            reminded INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME
        );
    """)
    conn.commit()
    yield conn


# ============================================================================
# _rowToTodoDict() 测试
# ============================================================================

def test_row_to_todo_dict():
    """将 sqlite3.Row 转换为字典"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE todos (
            id INTEGER, chat_id TEXT, user_id TEXT, content TEXT,
            remind_time DATETIME, priority TEXT, status TEXT, reminded INTEGER,
            created_at DATETIME, completed_at DATETIME
        )
    """)

    cursor.execute("""
        INSERT INTO todos VALUES (
            1, 'chat1', 'user1', 'Test todo',
            '2026-05-27 14:00:00', 'P1', 'pending', 0,
            '2026-05-27 12:00:00', NULL
        )
    """)

    cursor.execute("SELECT * FROM todos WHERE id = 1")
    row = cursor.fetchone()

    result = _rowToTodoDict(row)

    assert result['id'] == 1
    assert result['chat_id'] == 'chat1'
    assert result['user_id'] == 'user1'
    assert result['content'] == 'Test todo'
    assert isinstance(result['remind_time'], datetime)
    assert result['priority'] == 'P1'
    assert result['status'] == 'pending'
    assert result['reminded'] == 0
    assert isinstance(result['created_at'], datetime)
    assert result['completed_at'] is None

    conn.close()


def test_row_to_todo_dict_no_remind_time():
    """无提醒时间时 remind_time 为 None"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE todos (
            id INTEGER, chat_id TEXT, user_id TEXT, content TEXT,
            remind_time DATETIME, priority TEXT, status TEXT, reminded INTEGER,
            created_at DATETIME, completed_at DATETIME
        )
    """)

    cursor.execute("""
        INSERT INTO todos VALUES (
            1, 'chat1', 'user1', 'Test todo',
            NULL, 'P_', 'pending', 0,
            '2026-05-27 12:00:00', NULL
        )
    """)

    cursor.execute("SELECT * FROM todos WHERE id = 1")
    row = cursor.fetchone()

    result = _rowToTodoDict(row)

    assert result['remind_time'] is None

    conn.close()


# ============================================================================
# addTodo() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_add_todo_basic(todos_db):
    """添加基本待办"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        # Mock Database.run() 返回 lastrowid
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        todo_id = await addTodo(
            chatID="chat1",
            userID="user1",
            content="Buy milk"
        )

        assert todo_id is not None

        # 验证数据库中的记录
        cursor = todos_db.cursor()
        cursor.execute("SELECT * FROM todos WHERE id = ?", (todo_id,))
        row = cursor.fetchone()

        assert row['chat_id'] == 'chat1'
        assert row['user_id'] == 'user1'
        # content 列已加密存储，需解密后比对
        assert decryptText(row['content']) == 'Buy milk'
        assert row['remind_time'] is None
        assert row['priority'] == 'P_'
        assert row['status'] == 'pending'


@pytest.mark.asyncio
async def test_add_todo_with_remind_time(todos_db):
    """添加带提醒时间的待办"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        remind_time = datetime.now() + timedelta(hours=2)

        todo_id = await addTodo(
            chatID="chat1",
            userID="user1",
            content="Meeting",
            remindTime=remind_time,
            priority="P1"
        )

        assert todo_id is not None

        cursor = todos_db.cursor()
        cursor.execute("SELECT * FROM todos WHERE id = ?", (todo_id,))
        row = cursor.fetchone()

        assert row['remind_time'] is not None
        assert row['priority'] == 'P1'


@pytest.mark.asyncio
async def test_add_todo_exception_handling(todos_db):
    """添加失败时返回 None"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        with patch('utils.todos.database.logSystemEvent', new_callable=AsyncMock):
            mock_run.side_effect = Exception("Database error")

            result = await addTodo(
                chatID="chat1",
                userID="user1",
                content="Test"
            )

            assert result is None


# ============================================================================
# getTodos() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_get_todos_empty(todos_db):
    """空列表"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        result = await getTodos("chat1", "user1")

        assert result == []


@pytest.mark.asyncio
async def test_get_todos_by_status(todos_db):
    """按状态筛选"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        # 插入测试数据
        cursor = todos_db.cursor()
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status)
            VALUES ('chat1', 'user1', 'Todo 1', 'pending')
        """)
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status)
            VALUES ('chat1', 'user1', 'Todo 2', 'done')
        """)
        todos_db.commit()

        # 查询 pending
        pending = await getTodos("chat1", "user1", status="pending")
        assert len(pending) == 1
        assert pending[0]['content'] == 'Todo 1'

        # 查询 done
        done = await getTodos("chat1", "user1", status="done")
        assert len(done) == 1
        assert done[0]['content'] == 'Todo 2'

        # 查询 all
        all_todos = await getTodos("chat1", "user1", status="all")
        assert len(all_todos) == 2


@pytest.mark.asyncio
async def test_get_todos_pagination(todos_db):
    """分页查询"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        # 插入 5 条数据
        cursor = todos_db.cursor()
        for i in range(5):
            cursor.execute("""
                INSERT INTO todos (chat_id, user_id, content)
                VALUES ('chat1', 'user1', ?)
            """, (f'Todo {i}',))
        todos_db.commit()

        # 第一页（limit=2）
        page1 = await getTodos("chat1", "user1", limit=2, offset=0)
        assert len(page1) == 2

        # 第二页
        page2 = await getTodos("chat1", "user1", limit=2, offset=2)
        assert len(page2) == 2

        # 第三页
        page3 = await getTodos("chat1", "user1", limit=2, offset=4)
        assert len(page3) == 1


@pytest.mark.asyncio
async def test_get_todos_by_user(todos_db):
    """按用户隔离"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        cursor = todos_db.cursor()
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content)
            VALUES ('chat1', 'user1', 'User1 Todo')
        """)
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content)
            VALUES ('chat1', 'user2', 'User2 Todo')
        """)
        todos_db.commit()

        user1_todos = await getTodos("chat1", "user1")
        assert len(user1_todos) == 1
        assert user1_todos[0]['content'] == 'User1 Todo'

        user2_todos = await getTodos("chat1", "user2")
        assert len(user2_todos) == 1
        assert user2_todos[0]['content'] == 'User2 Todo'


# ============================================================================
# getTodoByID() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_get_todo_by_id_exists(todos_db):
    """根据 ID 获取待办"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        cursor = todos_db.cursor()
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content)
            VALUES ('chat1', 'user1', 'Test Todo')
        """)
        todo_id = cursor.lastrowid
        todos_db.commit()

        result = await getTodoByID(todo_id)

        assert result is not None
        assert result['id'] == todo_id
        assert result['content'] == 'Test Todo'


@pytest.mark.asyncio
async def test_get_todo_by_id_not_exists(todos_db):
    """ID 不存在返回 None"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        result = await getTodoByID(99999)

        assert result is None


# ============================================================================
# updateTodo() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_update_todo_content(todos_db):
    """更新内容"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        cursor = todos_db.cursor()
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content)
            VALUES ('chat1', 'user1', 'Old content')
        """)
        todo_id = cursor.lastrowid
        todos_db.commit()

        success = await updateTodo(todo_id, content="New content")

        assert success is True

        cursor.execute("SELECT content FROM todos WHERE id = ?", (todo_id,))
        row = cursor.fetchone()
        assert decryptText(row['content']) == 'New content'


@pytest.mark.asyncio
async def test_update_todo_priority(todos_db):
    """更新优先级"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        cursor = todos_db.cursor()
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, priority)
            VALUES ('chat1', 'user1', 'Test', 'P_')
        """)
        todo_id = cursor.lastrowid
        todos_db.commit()

        success = await updateTodo(todo_id, priority="P0")

        assert success is True

        cursor.execute("SELECT priority FROM todos WHERE id = ?", (todo_id,))
        row = cursor.fetchone()
        assert row['priority'] == 'P0'


@pytest.mark.asyncio
async def test_update_todo_status_to_done(todos_db):
    """标记为完成时记录 completed_at"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        cursor = todos_db.cursor()
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status)
            VALUES ('chat1', 'user1', 'Test', 'pending')
        """)
        todo_id = cursor.lastrowid
        todos_db.commit()

        success = await updateTodo(todo_id, status="done")

        assert success is True

        cursor.execute("SELECT status, completed_at FROM todos WHERE id = ?", (todo_id,))
        row = cursor.fetchone()
        assert row['status'] == 'done'
        assert row['completed_at'] is not None


@pytest.mark.asyncio
async def test_update_todo_status_to_pending(todos_db):
    """重新打开时清除 completed_at"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        cursor = todos_db.cursor()
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status, completed_at)
            VALUES ('chat1', 'user1', 'Test', 'done', '2026-05-27 12:00:00')
        """)
        todo_id = cursor.lastrowid
        todos_db.commit()

        success = await updateTodo(todo_id, status="pending")

        assert success is True

        cursor.execute("SELECT status, completed_at FROM todos WHERE id = ?", (todo_id,))
        row = cursor.fetchone()
        assert row['status'] == 'pending'
        assert row['completed_at'] is None


@pytest.mark.asyncio
async def test_update_todo_no_changes(todos_db):
    """无更新内容时返回 True"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        success = await updateTodo(1)

        assert success is True


# ============================================================================
# deleteTodo() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_delete_todo(todos_db):
    """删除待办"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        cursor = todos_db.cursor()
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content)
            VALUES ('chat1', 'user1', 'Test')
        """)
        todo_id = cursor.lastrowid
        todos_db.commit()

        success = await deleteTodo(todo_id)

        assert success is True

        cursor.execute("SELECT * FROM todos WHERE id = ?", (todo_id,))
        row = cursor.fetchone()
        assert row is None


# ============================================================================
# markDone() / reopenTodo() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_mark_done(todos_db):
    """标记为完成"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        cursor = todos_db.cursor()
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status)
            VALUES ('chat1', 'user1', 'Test', 'pending')
        """)
        todo_id = cursor.lastrowid
        todos_db.commit()

        success = await markDone(todo_id)

        assert success is True

        cursor.execute("SELECT status FROM todos WHERE id = ?", (todo_id,))
        row = cursor.fetchone()
        assert row['status'] == 'done'


@pytest.mark.asyncio
async def test_reopen_todo(todos_db):
    """重新打开待办"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        cursor = todos_db.cursor()
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status, reminded)
            VALUES ('chat1', 'user1', 'Test', 'done', 1)
        """)
        todo_id = cursor.lastrowid
        todos_db.commit()

        success = await reopenTodo(todo_id)

        assert success is True

        cursor.execute("SELECT status, reminded FROM todos WHERE id = ?", (todo_id,))
        row = cursor.fetchone()
        assert row['status'] == 'pending'
        assert row['reminded'] == 0


# ============================================================================
# getTodosCount() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_get_todos_count(todos_db):
    """统计待办数量"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        cursor = todos_db.cursor()
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status)
            VALUES ('chat1', 'user1', 'Todo 1', 'pending')
        """)
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status)
            VALUES ('chat1', 'user1', 'Todo 2', 'pending')
        """)
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status)
            VALUES ('chat1', 'user1', 'Todo 3', 'done')
        """)
        todos_db.commit()

        pending_count = await getTodosCount("chat1", "user1", status="pending")
        assert pending_count == 2

        done_count = await getTodosCount("chat1", "user1", status="done")
        assert done_count == 1

        all_count = await getTodosCount("chat1", "user1", status="all")
        assert all_count == 3


# ============================================================================
# getUsersTodosSummary() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_get_users_todos_summary(todos_db):
    """按用户聚合统计"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        cursor = todos_db.cursor()

        # User1: 2 pending, 1 done
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status, created_at)
            VALUES ('chat1', 'user1', 'Todo 1', 'pending', '2026-05-27 10:00:00')
        """)
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status, created_at)
            VALUES ('chat1', 'user1', 'Todo 2', 'pending', '2026-05-27 11:00:00')
        """)
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status, created_at)
            VALUES ('chat1', 'user1', 'Todo 3', 'done', '2026-05-27 12:00:00')
        """)

        # User2: 1 pending
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status, created_at)
            VALUES ('chat2', 'user2', 'Todo 4', 'pending', '2026-05-27 13:00:00')
        """)

        todos_db.commit()

        result = await getUsersTodosSummary()

        assert len(result) == 2

        # User1 应该排在前面（pending 更多）
        user1_summary = result[0]
        assert user1_summary['user_id'] == 'user1'
        assert user1_summary['pending'] == 2
        assert user1_summary['done'] == 1
        assert user1_summary['total'] == 3
        assert user1_summary['last_active'] == '2026-05-27 12:00'

        user2_summary = result[1]
        assert user2_summary['user_id'] == 'user2'
        assert user2_summary['pending'] == 1
        assert user2_summary['done'] == 0
        assert user2_summary['total'] == 1


@pytest.mark.asyncio
async def test_get_users_todos_summary_with_overdue(todos_db):
    """统计过期待办"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        cursor = todos_db.cursor()

        # 过期的待办（remind_time 在过去）
        past_time = (datetime.now() - timedelta(hours=1)).strftime(TIMESTAMP_FORMAT)
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status, remind_time)
            VALUES ('chat1', 'user1', 'Overdue', 'pending', ?)
        """, (past_time,))

        todos_db.commit()

        result = await getUsersTodosSummary()

        assert len(result) == 1
        assert result[0]['overdue'] == 1


# ============================================================================
# getPendingReminders() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_get_pending_reminders(todos_db):
    """获取需要提醒的待办"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        cursor = todos_db.cursor()

        # 需要提醒（时间已到，未提醒）
        past_time = (datetime.now() - timedelta(minutes=5)).strftime(TIMESTAMP_FORMAT)
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status, remind_time, reminded)
            VALUES ('chat1', 'user1', 'Remind me', 'pending', ?, 0)
        """, (past_time,))

        # 未到时间
        future_time = (datetime.now() + timedelta(hours=1)).strftime(TIMESTAMP_FORMAT)
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status, remind_time, reminded)
            VALUES ('chat1', 'user1', 'Not yet', 'pending', ?, 0)
        """, (future_time,))

        # 已提醒
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status, remind_time, reminded)
            VALUES ('chat1', 'user1', 'Already reminded', 'pending', ?, 1)
        """, (past_time,))

        # 已完成
        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status, remind_time, reminded)
            VALUES ('chat1', 'user1', 'Done', 'done', ?, 0)
        """, (past_time,))

        todos_db.commit()

        result = await getPendingReminders()

        # 只有第一条符合条件
        assert len(result) == 1
        assert result[0]['content'] == 'Remind me'


@pytest.mark.asyncio
async def test_get_pending_reminders_order(todos_db):
    """提醒按时间升序排列"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        cursor = todos_db.cursor()

        time1 = (datetime.now() - timedelta(hours=2)).strftime(TIMESTAMP_FORMAT)
        time2 = (datetime.now() - timedelta(hours=1)).strftime(TIMESTAMP_FORMAT)

        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status, remind_time, reminded)
            VALUES ('chat1', 'user1', 'Later', 'pending', ?, 0)
        """, (time2,))

        cursor.execute("""
            INSERT INTO todos (chat_id, user_id, content, status, remind_time, reminded)
            VALUES ('chat1', 'user1', 'Earlier', 'pending', ?, 0)
        """, (time1,))

        todos_db.commit()

        result = await getPendingReminders()

        assert len(result) == 2
        assert result[0]['content'] == 'Earlier'
        assert result[1]['content'] == 'Later'


# ============================================================================
# SQL 注入防护测试
# ============================================================================

@pytest.mark.asyncio
async def test_sql_injection_protection(todos_db):
    """SQL 注入防护（参数化查询）"""
    with patch('utils.todos.database.todosDB.run') as mock_run:
        async def _mock_run(func):
            return func(todos_db)
        mock_run.side_effect = _mock_run

        # 尝试注入恶意 SQL
        malicious_content = "'; DROP TABLE todos; --"

        todo_id = await addTodo(
            chatID="chat1",
            userID="user1",
            content=malicious_content
        )

        assert todo_id is not None

        # 表应该仍然存在
        cursor = todos_db.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='todos'")
        row = cursor.fetchone()
        assert row is not None

        # 内容应该被当作普通字符串存储（加密后），解密可还原
        cursor.execute("SELECT content FROM todos WHERE id = ?", (todo_id,))
        row = cursor.fetchone()
        assert decryptText(row['content']) == malicious_content