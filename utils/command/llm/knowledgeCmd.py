"""
utils/command/llm/knowledgeCmd.py

/llm knowledge 子命令处理：开关、重建索引、列表、统计、检索、参数调整。
"""




from utils.llm import (
    getKnowledgeEnabled,
    getKnowledgeEntries,
    getKnowledgeMaxResults,
    getKnowledgeMinScore,
    getKnowledgeStats,
    reindexKnowledgeBase,
    retrieveKnowledge,
    setKnowledgeEnabled,
    setKnowledgeMaxResults,
    setKnowledgeMinScore,
)
from utils.core.logger import logAction, LogLevel, LogChildType




async def _handleKnowledgeCommand(args):
    """处理 /llm knowledge 子命令。"""
    if not args:
        print(f"知识库：{'开启' if getKnowledgeEnabled() else '关闭'}")
        print(f"召回数：{getKnowledgeMaxResults()}")
        print(f"最低分：{getKnowledgeMinScore()}\n")
        return

    action = args[0].lower()
    rest = args[1:]

    match action:
        case "on":
            setKnowledgeEnabled(True)
            await logAction("System", "LLM 知识库开启", "OK", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)

        case "off":
            setKnowledgeEnabled(False)
            await logAction("System", "LLM 知识库关闭", "OK", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)

        case "reindex":
            force = "--force" in rest or "-f" in rest
            print(f"[Knowledge] 重建索引中{'（强制模式）' if force else ''}……")
            try:
                result = await reindexKnowledgeBase(force=force)
                print(f"✅ 索引重建完成：")
                print(f"   新增：{result['added']}")
                print(f"   更新：{result['updated']}")
                print(f"   删除：{result['removed']}")
                print(f"   跳过：{result['skipped']}\n")
                await logAction("System", "LLM 知识库重建索引", f"新增 {result['added']}，更新 {result['updated']}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            except Exception as e:
                print(f"❌ 索引重建失败：{e}\n")

        case "list":
            category = rest[0] if rest else None
            try:
                entries = await getKnowledgeEntries(category=category)
                if not entries:
                    print("[Knowledge] 没有找到条目喵……\n")
                    return
                print(f"[Knowledge] 条目列表{f'（分类：{category}）' if category else ''}：")
                for entry in entries:
                    tags = ", ".join(entry["tags"][:5]) if entry["tags"] else "-"
                    if len(entry["tags"]) > 5:
                        tags += f" +{len(entry['tags']) - 5}"
                    print(f"  #{entry['id']} [{entry['category']}] p={entry['priority']} {'ON' if entry['enabled'] else 'OFF'}")
                    print(f"     {entry['title']}")
                    print(f"     tags: {tags}")
                    print(f"     来源：{entry['source_file']}")
                    print("---\n")
                print("\n")
            except Exception as e:
                print(f"❌ 列表获取失败：{e}\n")

        case "stats":
            try:
                stats = await getKnowledgeStats()
                print("[Knowledge] 统计信息：")
                print(f"  总条目数：{stats['total']}")
                print(f"  启用条目：{stats['enabled']}")
                print(f"  分类分布：")
                for cat, count in stats["byCategory"].items():
                    print(f"    {cat}: {count}")
                print(f"  平均 tags 数：{stats['avgTags']:.1f}")
                print(f"  来源文件数：{stats['source_files']}\n")
            except Exception as e:
                print(f"❌ 统计获取失败：{e}\n")

        case "search":
            if not rest:
                print("❌ 用法：/llm knowledge search <查询内容>\n")
                return
            query = " ".join(rest)
            try:
                limit = getKnowledgeMaxResults()
                minScore = getKnowledgeMinScore()
                results = await retrieveKnowledge(query, limit=limit, minScore=minScore)
                if not results:
                    print(f"[Knowledge] 未找到相关条目（查询：{query}）\n")
                    return
                print(f"[Knowledge] 检索结果（查询：{query}）：\n")
                for i, entry in enumerate(results, 1):
                    print(f"{i}. [{entry['category']}] {entry['title']} (分数: {entry['score']:.2f})")
                    content = entry["content"][:100].replace("\n", " ")
                    if len(entry["content"]) > 100:
                        content += "..."
                    print(f"   {content}")
                    print()
                print()
            except Exception as e:
                print(f"❌ 检索失败：{e}\n")

        case "maxresults":
            if not rest:
                print(f"当前召回数：{getKnowledgeMaxResults()}\n")
                return
            try:
                value = int(rest[0])
                setKnowledgeMaxResults(value)
                await logAction("System", "LLM 知识库召回数调整", f"已设置为 {value}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            except ValueError as e:
                print(f"❌ {e}\n")

        case "minscore":
            if not rest:
                print(f"当前最低分：{getKnowledgeMinScore()}\n")
                return
            try:
                value = float(rest[0])
                setKnowledgeMinScore(value)
                await logAction("System", "LLM 知识库最低分调整", f"已设置为 {value}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            except ValueError as e:
                print(f"❌ {e}\n")

        case _:
            print("❌ 用法：/llm knowledge [on|off|reindex|list|stats|search|maxresults|minscore]\n")
