"""
utils/chatHistory.py

加密聊天记录存储模块，使用 SQLite + Fernet 实现本地加密存储。

主要功能：
    - 自动生成并管理加密密钥
    - 加密存储聊天消息
    - 按 chatID 查询历史记录
    - 清空聊天记录
    - 自动清理超出限制的旧消息


================================================================================
数据存储
================================================================================

数据库文件：data/chatHistory.db
密钥文件：data/.chatKey（自动生成，请勿删除或泄露）

数据库表结构 messages：
    - id:           INTEGER PRIMARY KEY
    - chat_id:      TEXT        聊天对象 ID
    - direction:    TEXT        消息方向 ('incoming' / 'outgoing')
    - sender:       TEXT        发送者名称
    - content:      BLOB        加密后的消息内容
    - timestamp:    DATETIME    消息时间戳


================================================================================
存储限制
================================================================================

每个聊天的消息数量上限由 config.py 中的 CHAT_HISTORY_LIMIT 控制。
当某个聊天的消息数量超过此限制时，saveMessage 会先将最旧的消息归档到
data/chatBackup/，然后删除。

默认值：131072 条

归档文件：
    - 格式：archive_<chatID>_<YYYYMMDD>.db
    - 位置：data/chatBackup/
    - 特性：同一天的多次溢出会追加到同一个归档文件中
    - 内容：保持加密状态，使用相同的 SQLite 表结构


================================================================================
主要接口
================================================================================

saveMessage(chatID, direction, sender, content)
    保存一条消息到数据库（自动加密）
    当消息数量超过 CHAT_HISTORY_LIMIT 时，自动删除最旧的消息

loadHistory(chatID, limit=0, offset=0)
    加载指定聊天的历史记录（自动解密）

    参数：
        - chatID:   聊天对象 ID
        - limit:    返回的最大条数（0 表示不限制，加载全部）
        - offset:   跳过的条数（用于分页）

    返回 List[dict]，每个 dict 包含:
        - direction: 'incoming' / 'outgoing'
        - sender: str
        - content: str
        - timestamp: datetime

getChatList()
    获取所有有记录的 chatID 列表

clearHistory(chatID=None)
    清空指定聊天的记录，若 chatID 为 None 则清空全部

getMessageCount(chatID=None)
    获取消息数量

"""




import os
import sqlite3
from datetime import datetime
from typing import List, Optional
from cryptography.fernet import Fernet

from config import (
    DB_PATH,
    KEY_PATH,
    CHAT_DATA_DIR,
    CHAT_BACKUP_DIR,
    CHAT_HISTORY_LIMIT,
)
from utils.logger import logSystemEvent, LogLevel




# ============================================================================
# 时间戳格式常量
# ============================================================================

TIMESTAMP_FORMAT_DATE = "%Y%m%d"                # 归档文件名日期格式
TIMESTAMP_FORMAT_DATETIME = "%Y-%m-%d %H:%M:%S" # 数据库时间戳格式
TIMESTAMP_FORMAT_DISPLAY = "%Y/%m/%d"           # 日期分隔符显示格式




# ============================================================================
# 密钥管理
# ============================================================================

def ensureDataDir():
    """确保 data 目录存在"""
    os.makedirs(CHAT_DATA_DIR, exist_ok=True)


def loadOrCreateKey() -> bytes:
    """
    加载或创建加密密钥。

    密钥存储在 data/.chat_key 文件中。
    如果文件不存在，会自动生成新密钥。
    """
    ensureDataDir()

    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, "rb") as f:
            return f.read()

    # 生成新密钥
    key = Fernet.generate_key()
    with open(KEY_PATH, "wb") as f:
        f.write(key)

    return key


def getFernet() -> Fernet:
    """获取 Fernet 加密器实例"""
    key = loadOrCreateKey()
    return Fernet(key)




# ============================================================================
# 数据库初始化
# ============================================================================

def initDB():
    """初始化数据库表结构"""
    ensureDataDir()

    conn = sqlite3.connect(DB_PATH)
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
initDB()




# ============================================================================
# 消息存储与读取
# ============================================================================

async def _archiveOverflow(chatID: str, overflowCount: int) -> bool:
    """
    将即将被删除的溢出消息归档到独立的 SQLite 数据库。

    参数：
        chatID:         聊天对象 ID
        overflowCount:  需要归档的消息数量

    返回：
        成功返回 True，失败返回 False

    归档文件：
        data/chatBackup/archive_<chatID>_<YYYYMMDD>.db
        同一天的多次溢出会追加到同一个归档文件中。
    """
    try:
        os.makedirs(CHAT_BACKUP_DIR, exist_ok=True)

        dateStr = datetime.now().strftime(TIMESTAMP_FORMAT_DATE)
        archiveFilename = f"archive_{chatID}_{dateStr}.db"
        archivePath = os.path.join(CHAT_BACKUP_DIR, archiveFilename)

        # 使用 context manager 确保连接正确关闭
        with sqlite3.connect(DB_PATH) as mainConn:
            mainCursor = mainConn.cursor()

            mainCursor.execute(
                """
                SELECT id, chat_id, direction, sender, content, timestamp
                FROM messages
                WHERE chat_id = ?
                ORDER BY timestamp ASC
                LIMIT ?
                """,
                (str(chatID), overflowCount)
            )
            overflowMessages = mainCursor.fetchall()

            if not overflowMessages:
                return True

            # 归档数据库使用与主数据库相同的表结构
            with sqlite3.connect(archivePath) as archiveConn:
                archiveCursor = archiveConn.cursor()

                archiveCursor.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        sender TEXT,
                        content BLOB NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                archiveCursor.executemany(
                    """
                    INSERT INTO messages (chat_id, direction, sender, content, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [(msg[1], msg[2], msg[3], msg[4], msg[5]) for msg in overflowMessages]
                )

                archiveConn.commit()

        await logSystemEvent(
            "聊天记录归档成功喵",
            f"Chat {chatID}: {overflowCount} 条 → {archiveFilename}",
            LogLevel.INFO
        )
        return True

    except Exception as e:
        await logSystemEvent(
            "聊天记录归档*失败*喵……",
            f"Chat {chatID}: {str(e)}",
            LogLevel.ERROR,
            exception=e
        )
        return False


async def saveMessage(chatID: str, direction: str, sender: str, content: str) -> bool:
    """
    保存一条消息到数据库（自动加密）。

    参数：
        chatID:     聊天对象 ID
        direction:  消息方向，'incoming' 或 'outgoing'
        sender:     发送者名称
        content:    消息内容（明文）

    返回：
        成功返回 True，失败返回 False

    注意：
        当某个聊天的消息数量超过 CHAT_HISTORY_LIMIT 时，会先将最旧的消息归档到
        独立的 SQLite 数据库（data/chatBackup/archive_<chatID>_<YYYYMMDD>.db），
        然后删除。归档文件保持加密状态，使用相同的表结构。
    """
    try:
        fernet = getFernet()
        encryptedContent = fernet.encrypt(content.encode("utf-8"))

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # 使用本地时间而非 UTC
        localTimestamp = datetime.now().strftime(TIMESTAMP_FORMAT_DATETIME)

        cursor.execute(
            "INSERT INTO messages (chat_id, direction, sender, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            (str(chatID), direction, sender, encryptedContent, localTimestamp)
        )

        # 检查是否超出限制，若超出则先归档再删除
        cursor.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (str(chatID),))
        currentCount = cursor.fetchone()[0]

        if currentCount > CHAT_HISTORY_LIMIT:
            overflowCount = currentCount - CHAT_HISTORY_LIMIT

            # 提交当前事务，确保新消息已保存
            conn.commit()

            # 仅当归档成功时才删除旧消息，防止数据丢失
            if await _archiveOverflow(chatID, overflowCount):
                cursor.execute(
                    """
                    DELETE FROM messages
                    WHERE chat_id = ? AND id NOT IN (
                        SELECT id FROM messages
                        WHERE chat_id = ?
                        ORDER BY timestamp DESC
                        LIMIT ?
                    )
                    """,
                    (str(chatID), str(chatID), CHAT_HISTORY_LIMIT)
                )
            else:
                await logSystemEvent(
                    "归档失败喵，先保留旧消息……",
                    f"Chat {chatID}",
                    LogLevel.WARNING
                )

        conn.commit()
        conn.close()
        return True

    except Exception as e:
        await logSystemEvent(
            "消息保存失败喵……",
            f"Chat {chatID}: {str(e)}",
            LogLevel.ERROR,
            exception=e
        )
        return False


async def loadHistory(chatID: str, limit: int = 0, offset: int = 0) -> List[dict]:
    """
    加载指定聊天的历史记录（自动解密）。

    参数：
        chatID:     聊天对象 ID
        limit:      返回的最大条数（0 表示不限制，加载全部）
        offset:     跳过的条数（用于分页）

    返回：
        消息列表，每条消息包含：
            - direction: 'incoming' / 'outgoing'
            - sender: str
            - content: str（解密后的明文）
            - timestamp: datetime
    """
    try:
        fernet = getFernet()

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        if limit > 0:
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
        else:
            cursor.execute(
                """
                SELECT direction, sender, content, timestamp
                FROM messages
                WHERE chat_id = ?
                ORDER BY timestamp DESC
                """,
                (str(chatID),)
            )

        rows = cursor.fetchall()
        conn.close()

        messages = []
        skippedCount = 0
        for row in rows:
            try:
                decryptedContent = fernet.decrypt(row["content"]).decode("utf-8")
                messages.append({
                    "direction": row["direction"],
                    "sender": row["sender"],
                    "content": decryptedContent,
                    "timestamp": datetime.fromisoformat(row["timestamp"]) if row["timestamp"] else None
                })
            except Exception as e:
                # 解密失败的消息跳过（可能是密钥已更改）
                skippedCount += 1
                continue

        if skippedCount > 0:
            await logSystemEvent(
                "消息解密失败喵……",
                f"ChatID {chatID}: 有 {skippedCount} 条消息读取失败",
                LogLevel.WARNING
            )

        # 反转列表，让最旧的消息在前面
        messages.reverse()
        return messages

    except Exception as e:
        await logSystemEvent(
            "历史记录加载失败喵……",
            f"Chat {chatID}: {str(e)}",
            LogLevel.ERROR,
            exception=e
        )
        return []


async def getChatList() -> List[dict]:
    """
    获取所有有记录的聊天列表。

    返回：
        聊天列表，每项包含：
            - chat_id: str
            - message_count: int
            - last_message_time: datetime
    """
    try:
        conn = sqlite3.connect(DB_PATH)
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
        await logSystemEvent(
            "聊天列表获取失败喵……",
            str(e),
            LogLevel.ERROR,
            exception=e
        )
        return []


async def clearHistory(chatID: Optional[str] = None) -> bool:
    """
    清空聊天记录。

    参数：
        chatID:     指定要清空的聊天 ID，若为 None 则清空全部

    返回：
        成功返回 True，失败返回 False
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        if chatID:
            cursor.execute("DELETE FROM messages WHERE chat_id = ?", (str(chatID),))
        else:
            cursor.execute("DELETE FROM messages")

        conn.commit()
        conn.close()
        return True

    except Exception as e:
        await logSystemEvent(
            "记录清空失败喵……",
            str(e),
            LogLevel.ERROR,
            exception=e
        )
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
        conn = sqlite3.connect(DB_PATH)
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

# ============================================================================




def iterMessagesWithDateMarkers(messages: List[dict]):
    """
    遍历消息列表，在日期变化时 yield 日期标记。

    用于在显示/导出聊天记录时自动插入日期分隔符，提升长时间跨度记录的可读性。

    参数：
        messages: 消息列表（通常来自 loadHistory）

    Yields：
        ("date", date_str):    日期标记，格式 "YYYY/MM/DD"
        ("message", msg_dict): 消息对象

    示例：
        for item_type, item_data in iterMessagesWithDateMarkers(messages):
            if item_type == "date":
                print(f"[{item_data}]")
            else:
                print(f"  {item_data['content']}")
    """
    lastDate = None

    for msg in messages:
        if msg.get("timestamp"):
            currentDate = msg["timestamp"].date()

            # 日期变化时，先 yield 日期标记
            if currentDate != lastDate:
                yield ("date", currentDate.strftime(f"\n{TIMESTAMP_FORMAT_DISPLAY}"))
                lastDate = currentDate

        # 然后 yield 消息本身
        yield ("message", msg)
