"""
utils/core/crypto.py

本地数据加密公共模块，使用 Fernet（对称加密）实现敏感字段的加解密。

================================================================================
设计背景
================================================================================

项目中多个 SQLite 库存放用户隐私数据，需要对「内容正文」做静态加密（at-rest）：
    - chatHistory.db  聊天记录原文（utils/chatHistory.py）
    - llmMemory.db    LLM 对用户的长期记忆（utils/llm/memory/database.py）
    - todos.db        用户的待办事项内容（utils/todos/database.py）

这三个库**共用同一把密钥**（data/.chatKey），由本模块统一管理。
加密口径：只加密承载用户隐私的「内容正文列」（content），
不加密用于检索 / 排序的元数据列（scope/chat_id/timestamp/priority 等），
因为加密列无法进入 WHERE / ORDER BY。

knowledge.db 不在加密范围内——它存的是 bot 自己的人设/知识，
源文件本就是明文 markdown 进 git，加密它反而自相矛盾。

================================================================================
密钥管理
================================================================================

密钥文件：data/.chatKey（由 config.KEY_PATH 指定）
    - 首次调用自动生成，请勿删除或泄露。
    - 非 Windows 系统会将权限收紧为 0o600（仅属主可读写）。

⚠️ 密钥与密文同处 data/ 目录，因此本方案防护的是「冷数据泄露」场景
   （磁盘镜像、误提交进 git、备份外泄），而非已取得服务器文件读取权的攻击者。

================================================================================
调用点
================================================================================

encrypt(text) / decrypt(token) 为底层接口（bytes 进出）。
encryptText(str) -> bytes / decryptText(bytes) -> str 为业务侧便捷封装：
    - 写库：存储 encryptText(content) 到 BLOB 列。
    - 读库：用 decryptText(row["content"]) 还原，失败时按调用方策略兜底。

历史明文数据的一次性迁移见 scripts/migrate_encrypt.py。
"""


import os
import sys
from typing import Optional

from cryptography.fernet import Fernet

from config import KEY_PATH




# 缓存 Fernet 实例，避免重复读取密钥文件
_fernetCache: Optional[Fernet] = None


def loadOrCreateKey() -> bytes:
    """
    加载或创建加密密钥。

    密钥存储在 config.KEY_PATH（data/.chatKey）。
    如果文件不存在，会自动生成新密钥并（在非 Windows 上）收紧权限为 0o600。
    """
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, "rb") as f:
            return f.read()

    # 生成新密钥
    key = Fernet.generate_key()
    with open(KEY_PATH, "wb") as f:
        f.write(key)

    # 限制文件权限为仅属主可读写（非 Windows 系统）
    if sys.platform != "win32":
        os.chmod(KEY_PATH, 0o600)

    return key




def getFernet() -> Fernet:
    """
    获取 Fernet 加密器实例（带缓存）。

    首次调用时从磁盘加载密钥，后续调用直接返回缓存的实例。
    三个加密库（chatHistory / llmMemory / todos）共用同一实例。
    """
    global _fernetCache

    if _fernetCache is None:
        key = loadOrCreateKey()
        _fernetCache = Fernet(key)

    return _fernetCache




def encrypt(data: bytes) -> bytes:
    """加密字节数据，返回 Fernet token（bytes）。"""
    return getFernet().encrypt(data)


def decrypt(token: bytes) -> bytes:
    """解密 Fernet token，返回原始字节。失败抛 cryptography.fernet.InvalidToken。"""
    return getFernet().decrypt(token)


def encryptText(text: str) -> bytes:
    """加密 UTF-8 文本，返回密文 bytes（供存入 BLOB 列）。"""
    return getFernet().encrypt(text.encode("utf-8"))


def decryptText(token: bytes) -> str:
    """解密密文 bytes，返回 UTF-8 文本。失败抛 cryptography.fernet.InvalidToken。"""
    return getFernet().decrypt(token).decode("utf-8")