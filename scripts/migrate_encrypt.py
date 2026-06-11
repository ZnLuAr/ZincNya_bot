#!/usr/bin/env python3
"""
scripts/migrate_encrypt.py

一次性迁移脚本：把 llmMemory.db / todos.db 中【历史明文】的 content 列
加密为 Fernet 密文（与 utils/core/crypto.py 共用 data/.chatKey）。

================================================================================
背景
================================================================================

加密改造前，llmMemory.db（长期记忆）和 todos.db（用户待办）的 content 列是明文。
改造后，写入路径已自动加密，但库里已有的历史行仍是明文，读取时会被
_decryptContent 当作"解密失败"走兜底分支——能用但不彻底。本脚本把这些历史明文
就地加密回写，使全库一致。

chatHistory.db 不在此脚本范围内——它一直就是加密的。

================================================================================
幂等性
================================================================================

脚本对每一行先尝试 decrypt：
    - 解密成功 → 该行已是密文，跳过（不会二次加密）。
    - 解密失败 → 视为明文，加密回写。

因此重复运行是安全的。无需额外标记。

================================================================================
用法
================================================================================

    python scripts/migrate_encrypt.py            # 实际执行迁移
    python scripts/migrate_encrypt.py --dry-run  # 只统计，不写入

⚠️ 运行前请先备份 data/（脚本不自动备份）。
迁移完成、确认无误后，本脚本可直接删除。
"""


import os
import sys
import argparse
import sqlite3

# 允许脚本从项目根目录导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import LLM_MEMORY_DB_PATH, TODOS_DB_PATH
from utils.core.crypto import encryptText, decryptText


# (db 路径, 表名, 主键列, content 列) —— content 列已在改造中改为 BLOB
TARGETS = [
    (LLM_MEMORY_DB_PATH, "memory_entries", "id", "content"),
    (TODOS_DB_PATH, "todos", "id", "content"),
]


def _isAlreadyEncrypted(value) -> bool:
    """
    判断 content 值是否已是 Fernet 密文。

    通过尝试解密探测：成功即已加密。空值视为"无需处理"。
    """
    if value is None:
        return True
    try:
        decryptText(value if isinstance(value, bytes) else str(value).encode("utf-8"))
        return True
    except Exception:
        return False


def _toPlaintext(value) -> str:
    """把历史明文 content（bytes 或 str）规整为 str。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def migrateTable(dbPath: str, table: str, pkCol: str, contentCol: str, dryRun: bool) -> tuple[int, int]:
    """
    迁移单个表的 content 列。

    返回 (已加密跳过数, 本次加密数)。
    """
    if not os.path.exists(dbPath):
        print(f"  [跳过] 数据库不存在：{dbPath}")
        return (0, 0)

    conn = sqlite3.connect(dbPath)
    conn.row_factory = sqlite3.Row
    skipped = 0
    encrypted = 0

    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT {pkCol}, {contentCol} FROM {table}")
        rows = cursor.fetchall()

        for row in rows:
            pk = row[pkCol]
            value = row[contentCol]

            if _isAlreadyEncrypted(value):
                skipped += 1
                continue

            plaintext = _toPlaintext(value)
            if not dryRun:
                cursor.execute(
                    f"UPDATE {table} SET {contentCol} = ? WHERE {pkCol} = ?",
                    (encryptText(plaintext), pk),
                )
            encrypted += 1

        if not dryRun:
            conn.commit()

    finally:
        conn.close()

    return (skipped, encrypted)


def main():
    parser = argparse.ArgumentParser(description="加密 llmMemory / todos 的历史明文 content")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    args = parser.parse_args()

    mode = "DRY-RUN（不写入）" if args.dry_run else "实际执行"
    print(f"=== content 加密迁移 [{mode}] ===\n")

    totalEncrypted = 0
    for dbPath, table, pkCol, contentCol in TARGETS:
        print(f"[{table}] {dbPath}")
        skipped, encrypted = migrateTable(dbPath, table, pkCol, contentCol, args.dry_run)
        print(f"  已加密跳过：{skipped} 行；本次加密：{encrypted} 行\n")
        totalEncrypted += encrypted

    if args.dry_run:
        print(f"DRY-RUN 结束：将加密 {totalEncrypted} 行（未写入）。")
    else:
        print(f"迁移完成：共加密 {totalEncrypted} 行。确认无误后可删除本脚本。")


if __name__ == "__main__":
    main()