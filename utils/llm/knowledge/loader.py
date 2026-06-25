"""
utils/llm/knowledge/loader.py

知识库 Markdown 文件加载器

提供：
    - YAML frontmatter 解析
    - source_hash 增量更新
    - 批量索引刷新
"""

import os
import hashlib
from typing import Optional

import yaml

from config import LLM_KNOWLEDGE_DIR
from utils.core.logger import logSystemEvent, LogLevel

from .database import upsertKnowledgeEntry, deleteEntriesBySource
from .retriever import rebuildTokenCacheFromDB




def _computeSourceHash(title: str, content: str, tags: list[str]) -> str:
    """计算 source_hash（用于增量更新检测）"""
    combined = f"{title}\n{content}\n{','.join(sorted(tags))}"
    return hashlib.sha256(combined.encode('utf-8')).hexdigest()[:16]




def _parseMarkdownFile(filepath: str) -> Optional[dict]:
    """
    解析单个 Markdown 文件（含 YAML frontmatter）

    返回：
        {
            "category": str,
            "title": str,
            "content": str,
            "tags": list[str],  # 合并 tags + tags_expanded
            "priority": int,
            "source_hash": str,
        }

        或 None（解析失败）
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            raw = f.read()

        # 分离 frontmatter 和 content
        if not raw.startswith('---'):
            return None

        parts = raw.split('---', 2)
        if len(parts) < 3:
            return None

        frontmatterRaw = parts[1]
        content = parts[2].strip()

        # 解析 frontmatter
        frontmatter = yaml.safe_load(frontmatterRaw)
        if not isinstance(frontmatter, dict):
            return None

        category = frontmatter.get('category', 'unknown')
        title = frontmatter.get('title', '')
        tags = frontmatter.get('tags', [])
        tagsExpanded = frontmatter.get('tags_expanded', [])
        priority = frontmatter.get('priority', 0)

        if not title or not content:
            return None

        # 合并 tags + tags_expanded
        allTags = list(dict.fromkeys(tags + tagsExpanded))  # 去重保序

        sourceHash = _computeSourceHash(title, content, tags)  # 只用人工 tags 计算 hash

        return {
            "category": category,
            "title": title,
            "content": content,
            "tags": allTags,
            "priority": priority,
            "source_hash": sourceHash,
        }

    except Exception:
        # 解析失败，返回 None，由调用方记录日志
        return None




async def reindexKnowledgeBase(force: bool = False) -> dict:
    """
    扫描 data/llm/knowledge/*.md，增量更新数据库

    参数：
        force: 强制重建所有条目（忽略 source_hash）

    返回：
        {"added": n, "updated": n, "removed": n, "skipped": n}
    """
    if not os.path.exists(LLM_KNOWLEDGE_DIR):
        os.makedirs(LLM_KNOWLEDGE_DIR, exist_ok=True)
        return {"added": 0, "updated": 0, "removed": 0, "skipped": 0}

    stats = {"added": 0, "updated": 0, "removed": 0, "skipped": 0}

    # 扫描所有 .md 文件
    mdFiles = [f for f in os.listdir(LLM_KNOWLEDGE_DIR) if f.endswith('.md')]

    for filename in mdFiles:
        filepath = os.path.join(LLM_KNOWLEDGE_DIR, filename)
        sourceFile = f"knowledge/{filename}"  # 相对路径

        parsed = _parseMarkdownFile(filepath)
        if not parsed:
            await logSystemEvent(f"跳过无效文件: {filename}", level=LogLevel.WARNING)
            stats["skipped"] += 1
            continue

        # 检查是否需要更新（对比 source_hash）
        if not force:
            from .database import knowledgeDB
            def _checkHash(conn):
                cursor = conn.execute(
                    "SELECT source_hash FROM knowledge_entries WHERE source_file = ? AND title = ?",
                    (sourceFile, parsed["title"])
                )
                row = cursor.fetchone()
                return row["source_hash"] if row else None

            existingHash = await knowledgeDB.run(_checkHash)
            if existingHash == parsed["source_hash"]:
                stats["skipped"] += 1
                continue

        # Upsert
        entryID = await upsertKnowledgeEntry(
            category=parsed["category"],
            title=parsed["title"],
            content=parsed["content"],
            tags=parsed["tags"],
            sourceFile=sourceFile,
            sourceHash=parsed["source_hash"],
            priority=parsed["priority"],
        )

        if entryID:
            # 判断是新增还是更新（简化：有 existingHash 就是更新）
            if force or (not force and existingHash):
                stats["updated"] += 1
            else:
                stats["added"] += 1

    # 删除数据库中存在但文件已删除的条目
    from .database import knowledgeDB
    def _getOrphanedSources(conn):
        cursor = conn.execute("SELECT DISTINCT source_file FROM knowledge_entries")
        return [row["source_file"] for row in cursor.fetchall()]

    orphanedSources = await knowledgeDB.run(_getOrphanedSources)
    currentFiles = {f"knowledge/{f}" for f in mdFiles}

    for sourceFile in orphanedSources:
        if sourceFile not in currentFiles:
            deleted = await deleteEntriesBySource(sourceFile)
            stats["removed"] += deleted

    # 重建 token 缓存
    await rebuildTokenCacheFromDB()

    await logSystemEvent(
        "知识库索引刷新完成",
        f"新增 {stats['added']}, 更新 {stats['updated']}, 删除 {stats['removed']}, 跳过 {stats['skipped']}"
    )

    return stats




async def reindexOnStartup():
    """启动时异步扫描并索引（首次或增量）"""
    await reindexKnowledgeBase(force=False)
