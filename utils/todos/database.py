"""
utils/todos/database.py

待办事项数据库模块，使用 SQLite 实现本地明文存储。

主要功能：
    - 添加、查询、更新、删除待办事项
    - 支持优先级（P0/P1/P2/P3/P_）
    - 支持提醒时间设置
    - 按用户和会话隔离数据

================================================================================
数据存储
================================================================================

数据库文件：data/todos.db（明文存储）

数据库表结构 todos：
    - id:           INTEGER PRIMARY KEY
    - chat_id:      TEXT        会话 ID（私聊=用户ID，群组=群ID）
    - user_id:      TEXT        创建者 ID
    - content:      TEXT        待办内容（明文）
    - remind_time:  DATETIME    提醒时间（NULL=无提醒）
    - priority:     TEXT        优先级（P0/P1/P2/P3/P_）
    - status:       TEXT        状态（pending/done）
    - reminded:     INTEGER     是否已发送提醒（0/1，保留 remind_time 用于斜体显示）
    - created_at:   DATETIME    创建时间
    - completed_at: DATETIME    完成时间

================================================================================
主要接口
================================================================================

addTodo(chatID, userID, content, remindTime=None, priority='P_')
    添加一条待办事项

getTodos(chatID, userID, status='pending', limit=0, offset=0)
    获取指定用户的待办列表

getTodoByID(todoID)
    根据 ID 获取单条待办

updateTodo(todoID, **kwargs)
    更新待办（支持 content, remind_time, priority, status, reminded）

deleteTodo(todoID)
    删除待办

markDone(todoID)
    标记待办为已完成

reopenTodo(todoID)
    重新打开已完成的待办

getTodosCount(chatID, userID, status='pending')
    获取待办数量

getPendingReminders()
    获取所有需要提醒的待办（remind_time <= now 且 status = pending 且 reminded = 0）

getUsersTodosSummary()
    按用户聚合统计（pending / done / overdue / 最近活跃时间），供控制台总览使用
"""


import os
import sqlite3
from datetime import datetime
from typing import List, Optional, Dict, Any

from config import TODOS_DB_PATH, CHAT_DATA_DIR
from utils.logger import logSystemEvent, LogLevel




# ============================================================================
# 时间戳格式常量
# ============================================================================

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"  # 数据库时间戳格式




# ============================================================================
# 数据库初始化
# ============================================================================

def ensureDataDir():
    """确保 data 目录存在"""
    os.makedirs(CHAT_DATA_DIR, exist_ok=True)


def initDB():
    """初始化数据库表结构"""
    ensureDataDir()

    conn = sqlite3.connect(TODOS_DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
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
        )
    """)

    # 迁移：为旧数据库添加 reminded 列
    try:
        cursor.execute("ALTER TABLE todos ADD COLUMN reminded INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # 列已存在

    # 创建索引以提高查询效率
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_user ON todos(chat_id, user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_status ON todos(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_remind_time ON todos(remind_time, status)")

    conn.commit()
    conn.close()


# 模块导入时初始化数据库
initDB()


# ============================================================================
# CRUD 操作
# ============================================================================

async def addTodo(
    chatID: str,
    userID: str,
    content: str,
    remindTime: Optional[datetime] = None,
    priority: str = 'P_'
) -> Optional[int]:
    """
    添加一条待办事项。

    参数：
        chatID:      会话 ID
        userID:      创建者 ID
        content:     待办内容
        remindTime:  提醒时间（可选）
        priority:    优先级（P0/P1/P2/P3/P_）

    返回：
        成功返回待办 ID，失败返回 None
    """
    try:
        conn = sqlite3.connect(TODOS_DB_PATH)
        cursor = conn.cursor()

        remindTimeStr = remindTime.strftime(TIMESTAMP_FORMAT) if remindTime else None

        cursor.execute(
            """
            INSERT INTO todos (chat_id, user_id, content, remind_time, priority)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(chatID), str(userID), content, remindTimeStr, priority)
        )

        todoID = cursor.lastrowid
        conn.commit()
        conn.close()

        return todoID

    except Exception as e:
        await logSystemEvent(
            "待办添加失败喵……",
            f"User {userID}: {str(e)}",
            LogLevel.ERROR,
            exception=e
        )
        return None


async def getTodos(
    chatID: str,
    userID: str,
    status: str = 'pending',
    limit: int = 0,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """
    获取指定用户的待办列表。

    参数：
        chatID:  会话 ID
        userID:  用户 ID
        status:  状态筛选（'pending'/'done'/'all'）
        limit:   返回的最大条数（0 表示不限制）
        offset:  跳过的条数（用于分页）

    返回：
        待办列表，每条包含：
            - id, chat_id, user_id, content, remind_time, priority, status, created_at, completed_at
    """
    try:
        conn = sqlite3.connect(TODOS_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        if status == 'all':
            query = """
                SELECT * FROM todos
                WHERE chat_id = ? AND user_id = ?
                ORDER BY created_at DESC
            """
            params = (str(chatID), str(userID))
        else:
            query = """
                SELECT * FROM todos
                WHERE chat_id = ? AND user_id = ? AND status = ?
                ORDER BY created_at DESC
            """
            params = (str(chatID), str(userID), status)

        if limit > 0:
            query += " LIMIT ? OFFSET ?"
            params += (limit, offset)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        todos = []
        for row in rows:
            todos.append({
                "id": row["id"],
                "chat_id": row["chat_id"],
                "user_id": row["user_id"],
                "content": row["content"],
                "remind_time": datetime.fromisoformat(row["remind_time"]) if row["remind_time"] else None,
                "priority": row["priority"],
                "status": row["status"],
                "created_at": datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
                "completed_at": datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
                "reminded": row["reminded"],
            })

        return todos

    except Exception as e:
        await logSystemEvent(
            "待办列表加载失败喵……",
            f"User {userID}: {str(e)}",
            LogLevel.ERROR,
            exception=e
        )
        return []


async def getTodoByID(todoID: int) -> Optional[Dict[str, Any]]:
    """
    根据 ID 获取单条待办。

    返回：
        待办字典，或 None（不存在）
    """
    try:
        conn = sqlite3.connect(TODOS_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM todos WHERE id = ?", (todoID,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        return {
            "id": row["id"],
            "chat_id": row["chat_id"],
            "user_id": row["user_id"],
            "content": row["content"],
            "remind_time": datetime.fromisoformat(row["remind_time"]) if row["remind_time"] else None,
            "priority": row["priority"],
            "status": row["status"],
            "reminded": row["reminded"],
            "created_at": datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            "completed_at": datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
        }

    except Exception as e:
        await logSystemEvent(
            "待办查询失败喵……",
            f"ID {todoID}: {str(e)}",
            LogLevel.ERROR,
            exception=e
        )
        return None


async def updateTodo(todoID: int, **kwargs) -> bool:
    """
    更新待办。

    支持的参数：
        - content: str
        - remind_time: datetime
        - priority: str
        - status: str
        - reminded: int (0/1)

    返回：
        成功返回 True，失败返回 False
    """
    try:
        conn = sqlite3.connect(TODOS_DB_PATH)
        cursor = conn.cursor()

        # 构建 UPDATE 语句
        updates = []
        params = []

        if "content" in kwargs:
            updates.append("content = ?")
            params.append(kwargs["content"])

        if "remind_time" in kwargs:
            updates.append("remind_time = ?")
            remindTime = kwargs["remind_time"]
            params.append(remindTime.strftime(TIMESTAMP_FORMAT) if remindTime else None)

        if "priority" in kwargs:
            updates.append("priority = ?")
            params.append(kwargs["priority"])

        if "status" in kwargs:
            updates.append("status = ?")
            params.append(kwargs["status"])
            if kwargs["status"] == "done":
                # 标记完成 → 记录完成时间
                updates.append("completed_at = ?")
                params.append(datetime.now().strftime(TIMESTAMP_FORMAT))
            elif kwargs["status"] == "pending":
                # 重新打开 → 清除完成时间
                updates.append("completed_at = ?")
                params.append(None)

        if "reminded" in kwargs:
            updates.append("reminded = ?")
            params.append(kwargs["reminded"])

        if not updates:
            return True  # 没有更新内容

        params.append(todoID)
        query = f"UPDATE todos SET {', '.join(updates)} WHERE id = ?"

        cursor.execute(query, params)
        conn.commit()
        conn.close()

        return True

    except Exception as e:
        await logSystemEvent(
            "待办更新失败喵……",
            f"ID {todoID}: {str(e)}",
            LogLevel.ERROR,
            exception=e
        )
        return False


async def deleteTodo(todoID: int) -> bool:
    """
    删除待办。

    返回：
        成功返回 True，失败返回 False
    """
    try:
        conn = sqlite3.connect(TODOS_DB_PATH)
        cursor = conn.cursor()

        cursor.execute("DELETE FROM todos WHERE id = ?", (todoID,))
        conn.commit()
        conn.close()

        return True

    except Exception as e:
        await logSystemEvent(
            "待办删除失败喵……",
            f"ID {todoID}: {str(e)}",
            LogLevel.ERROR,
            exception=e
        )
        return False


async def markDone(todoID: int) -> bool:
    """
    标记待办为已完成。

    返回：
        成功返回 True，失败返回 False
    """
    return await updateTodo(todoID, status="done")


async def reopenTodo(todoID: int) -> bool:
    """
    重新打开已完成的待办（恢复为 pending）。

    返回：
        成功返回 True，失败返回 False
    """
    return await updateTodo(todoID, status="pending", reminded=0)


def getTodosCount(chatID: str, userID: str, status: str = 'pending') -> int:
    """
    获取待办数量。

    参数：
        chatID:  会话 ID
        userID:  用户 ID
        status:  状态筛选（'pending'/'done'/'all'）

    返回：
        待办数量
    """
    try:
        conn = sqlite3.connect(TODOS_DB_PATH)
        cursor = conn.cursor()

        if status == 'all':
            cursor.execute(
                "SELECT COUNT(*) FROM todos WHERE chat_id = ? AND user_id = ?",
                (str(chatID), str(userID))
            )
        else:
            cursor.execute(
                "SELECT COUNT(*) FROM todos WHERE chat_id = ? AND user_id = ? AND status = ?",
                (str(chatID), str(userID), status)
            )

        count = cursor.fetchone()[0]
        conn.close()
        return count

    except Exception:
        return 0


def getUsersTodosSummary() -> List[Dict[str, Any]]:
    """
    按用户聚合统计待办数量，供控制台总览使用。

    返回列表，每条对应一个 user_id，包含：
        - user_id:   str
        - pending:   int    待办中数量
        - done:      int    已完成数量
        - total:     int    合计
        - overdue:   int    已过期未提醒（remind_time <= now 且 pending）
        - last_active: str  最近一条 todo 的创建时间（格式 YYYY-MM-DD HH:MM）
    """
    try:
        conn = sqlite3.connect(TODOS_DB_PATH)
        cursor = conn.cursor()

        now = datetime.now().strftime(TIMESTAMP_FORMAT)

        cursor.execute(
            """
            SELECT
                user_id,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = 'done'    THEN 1 ELSE 0 END) AS done,
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'pending'
                              AND remind_time IS NOT NULL
                              AND remind_time <= ?
                         THEN 1 ELSE 0 END) AS overdue,
                MAX(created_at) AS last_active
            FROM todos
            GROUP BY user_id
            ORDER BY pending DESC, total DESC
            """,
            (now,)
        )

        rows = cursor.fetchall()
        conn.close()

        result = []
        for row in rows:
            user_id, pending, done, total, overdue, last_active = row
            result.append({
                "user_id":     user_id,
                "pending":     pending or 0,
                "done":        done    or 0,
                "total":       total   or 0,
                "overdue":     overdue or 0,
                "last_active": last_active[:16] if last_active else "—",
            })

        return result

    except Exception:
        return []


async def getPendingReminders() -> List[Dict[str, Any]]:
    """
    获取所有需要提醒的待办（remind_time <= now 且 status = pending）。

    返回：
        待办列表
    """
    try:
        conn = sqlite3.connect(TODOS_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        now = datetime.now().strftime(TIMESTAMP_FORMAT)

        cursor.execute(
            """
            SELECT * FROM todos
            WHERE remind_time <= ? AND status = 'pending' AND reminded = 0
            ORDER BY remind_time ASC
            """,
            (now,)
        )

        rows = cursor.fetchall()
        conn.close()

        todos = []
        for row in rows:
            todos.append({
                "id": row["id"],
                "chat_id": row["chat_id"],
                "user_id": row["user_id"],
                "content": row["content"],
                "remind_time": datetime.fromisoformat(row["remind_time"]) if row["remind_time"] else None,
                "priority": row["priority"],
                "status": row["status"],
                "created_at": datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
                "completed_at": datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
                "reminded": row["reminded"],
            })

        return todos

    except Exception as e:
        await logSystemEvent(
            "提醒查询失败喵……",
            str(e),
            LogLevel.ERROR,
            exception=e
        )
        return []
