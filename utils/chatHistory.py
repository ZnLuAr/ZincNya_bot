"""
utils/chatHistory.py

加密聊天记录存储模块，使用 SQLite + Fernet 实现本地加密存储。

主要功能：
    - 自动生成并管理加密密钥
    - 加密存储聊天消息
    - 按 chatID 查询历史记录
    - 导出聊天记录


================================================================================
数据存储
================================================================================

数据库文件：data/chat_history.db
密钥文件：data/.chat_key（自动生成，请勿删除或泄露）

数据库表结构 messages：
    - id:           INTEGER PRIMARY KEY
    - chat_id:      TEXT        聊天对象 ID
    - direction:    TEXT        消息方向 ('incoming' / 'outgoing')
    - sender:       TEXT        发送者名称
    - content:      BLOB        加密后的消息内容
    - timestamp:    DATETIME    消息时间戳


================================================================================
主要接口
================================================================================

saveMessage(chatID, direction, sender, content)
    保存一条消息到数据库（自动加密）

loadHistory(chatID, limit=50, offset=0)
    加载指定聊天的历史记录（自动解密）
    返回 List[dict]，每个 dict 包含:
        - direction: 'incoming' / 'outgoing'
        - sender: str
        - content: str
        - timestamp: datetime

getChatList()
    获取所有有记录的 chatID 列表

clearHistory(chatID=None)
    清空指定聊天的记录，若 chatID 为 None 则清空全部

"""




import os
import sqlite3
from datetime import datetime
from typing import List, Optional
from cryptography.fernet import Fernet


# ============================================================================
# 路径配置
# ============================================================================

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DB_PATH = os.path.join(_DATA_DIR, "chat_history.db")
_KEY_PATH = os.path.join(_DATA_DIR, ".chat_key")


# ============================================================================
# 密钥管理
# ============================================================================

def _ensureDataDir():
    """确保 data 目录存在"""
    os.makedirs(_DATA_DIR, exist_ok=True)


def _loadOrCreateKey() -> bytes:
    """
    加载或创建加密密钥。

    密钥存储在 data/.chat_key 文件中。
    如果文件不存在，会自动生成新密钥。
    """
    _ensureDataDir()

    if os.path.exists(_KEY_PATH):
        with open(_KEY_PATH, "rb") as f:
            return f.read()

    # 生成新密钥
    key = Fernet.generate_key()
    with open(_KEY_PATH, "wb") as f:
        f.write(key)

    return key


def _getFernet() -> Fernet:
    """获取 Fernet 加密器实例"""
    key = _loadOrCreateKey()
    return Fernet(key)


# ============================================================================
# 数据库初始化
# ============================================================================

def _initDB():
    """初始化数据库表结构"""
    _ensureDataDir()

    conn = sqlite3.connect(_DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            direction TEXT NOT NULL,
            sender TEXT,
            content BLOB NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_id ON messages(chat_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON messages(timestamp)")

    conn.commit()
    conn.close()


# 模块导入时初始化数据库
_initDB()


# ============================================================================
# 消息存储与读取
# ============================================================================

def saveMessage(chatID: str, direction: str, sender: str, content: str) -> bool:
    """
    保存一条消息到数据库（自动加密）。

    参数：
        chatID:     聊天对象 ID
        direction:  消息方向，'incoming' 或 'outgoing'
        sender:     发送者名称
        content:    消息内容（明文）

    返回：
        成功返回 True，失败返回 False
    """
    try:
        fernet = _getFernet()
        encryptedContent = fernet.encrypt(content.encode("utf-8"))

        conn = sqlite3.connect(_DB_PATH)
        cursor = conn.cursor()

        cursor.execute(
            "INSERT INTO messages (chat_id, direction, sender, content) VALUES (?, ?, ?, ?)",
            (str(chatID), direction, sender, encryptedContent)
        )

        conn.commit()
        conn.close()
        return True

    except Exception as e:
        print(f"保存消息失败: {e}")
        return False


def loadHistory(chatID: str, limit: int = 50, offset: int = 0) -> List[dict]:
    """
    加载指定聊天的历史记录（自动解密）。

    参数：
        chatID:     聊天对象 ID
        limit:      返回的最大条数（默认 50）
        offset:     跳过的条数（用于分页）

    返回：
        消息列表，每条消息包含：
            - direction: 'incoming' / 'outgoing'
            - sender: str
            - content: str（解密后的明文）
            - timestamp: datetime
    """
    try:
        fernet = _getFernet()

        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT direction, sender, content, timestamp
            FROM messages
            WHERE chat_id = ?
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
            """,
            (str(chatID), limit, offset)
        )

        rows = cursor.fetchall()
        conn.close()

        messages = []
        for row in rows:
            try:
                decryptedContent = fernet.decrypt(row["content"]).decode("utf-8")
                messages.append({
                    "direction": row["direction"],
                    "sender": row["sender"],
                    "content": decryptedContent,
                    "timestamp": datetime.fromisoformat(row["timestamp"]) if row["timestamp"] else None
                })
            except Exception:
                # 解密失败的消息跳过
                continue

        # 反转列表，让最旧的消息在前面
        messages.reverse()
        return messages

    except Exception as e:
        print(f"加载历史记录失败: {e}")
        return []


def getChatList() -> List[dict]:
    """
    获取所有有记录的聊天列表。

    返回：
        聊天列表，每项包含：
            - chat_id: str
            - message_count: int
            - last_message_time: datetime
    """
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                chat_id,
                COUNT(*) as message_count,
                MAX(timestamp) as last_message_time
            FROM messages
            GROUP BY chat_id
            ORDER BY last_message_time DESC
            """
        )

        rows = cursor.fetchall()
        conn.close()

        return [
            {
                "chat_id": row["chat_id"],
                "message_count": row["message_count"],
                "last_message_time": datetime.fromisoformat(row["last_message_time"]) if row["last_message_time"] else None
            }
            for row in rows
        ]

    except Exception as e:
        print(f"获取聊天列表失败: {e}")
        return []


def clearHistory(chatID: Optional[str] = None) -> bool:
    """
    清空聊天记录。

    参数：
        chatID:     指定要清空的聊天 ID，若为 None 则清空全部

    返回：
        成功返回 True，失败返回 False
    """
    try:
        conn = sqlite3.connect(_DB_PATH)
        cursor = conn.cursor()

        if chatID:
            cursor.execute("DELETE FROM messages WHERE chat_id = ?", (str(chatID),))
        else:
            cursor.execute("DELETE FROM messages")

        conn.commit()
        conn.close()
        return True

    except Exception as e:
        print(f"清空记录失败: {e}")
        return False


def getMessageCount(chatID: Optional[str] = None) -> int:
    """
    获取消息数量。

    参数：
        chatID:     指定聊天 ID，若为 None 则返回全部消息数量

    返回：
        消息数量
    """
    try:
        conn = sqlite3.connect(_DB_PATH)
        cursor = conn.cursor()

        if chatID:
            cursor.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (str(chatID),))
        else:
            cursor.execute("SELECT COUNT(*) FROM messages")

        count = cursor.fetchone()[0]
        conn.close()
        return count

    except Exception:
        return 0
