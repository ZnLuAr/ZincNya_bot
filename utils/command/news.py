"""
utils/command/news.py

用于实现 /news 命令的逻辑模块，负责从外部网站抓取新闻并推送到 Telegram。

命令用法：
    /news -fetch [-id <chatID>]     抓取并推送新闻到指定群聊
    /news -test                     测试抓取（不推送，仅显示结果）
    /news -list                     查看已推送记录
    /news -clear                    清空已推送记录

注意：
    - 解析逻辑需要根据实际网页结构调整（见 newsAPI.py）
    - 推送需要配置 NEWS_TARGET_CHAT_ID 或使用 -id 参数指定
"""


from telegram import Bot
from handlers.cli import parseArgsTokens
from utils.newsAPI import (
    fetchLatestNews,
    pushToTelegram,
    loadPushedRecord,
    savePushedRecord,
    isAlreadyPushed,
    markAsPushed,
)
from config import NEWS_TARGET_CHAT_ID, NEWS_MAX_ARTICLES
from utils.logger import logSystemEvent, LogLevel




async def execute(app, args: list[str]):

    bot: Bot = app.bot

    parsed = {
        "fetch": None,
        "test": None,
        "list": None,
        "clear": None,
        "id": None,
    }

    argAlias = {
        "f": "fetch",
        "t": "test",
        "l": "list",
        "c": "clear",
        "i": "id",
    }

    parsed = parseArgsTokens(parsed, args, argAlias)

    # /news -test: 测试抓取（不推送）
    if parsed["test"] is not None:
        await testFetch()
        return

    # /news -list: 查看已推送记录
    if parsed["list"] is not None:
        showPushedList()
        return

    # /news -clear: 清空已推送记录
    if parsed["clear"] is not None:
        clearPushedList()
        return

    # /news -fetch: 抓取并推送
    if parsed["fetch"] is not None:
        targetChatID = parsed["id"] if parsed["id"] and parsed["id"] != "NoValue" else NEWS_TARGET_CHAT_ID
        await fetchAndPush(bot, targetChatID)
        return

    # 无参数时显示帮助
    print("使用 /news -fetch 抓取并推送新闻")
    print("使用 /news -test 测试抓取（不推送）")
    print("使用 /news -list 查看已推送记录")
    print("使用 /news -help 查看详细帮助\n")




async def testFetch():
    """测试抓取，仅显示结果不推送"""
    print("正在测试抓取……\n")

    try:
        articles = await fetchLatestNews(limit=NEWS_MAX_ARTICLES)

        if not articles:
            print("❌ 没有抓取到任何文章喵\n")
            print("可能原因：")
            print("  1. 网页结构与解析逻辑不匹配")
            print("  2. 网络连接问题")
            print("  3. 需要配置代理 NEWS_HTTP_PROXY\n")
            return

        print(f"✅ 成功抓取到 {len(articles)} 篇文章：\n")

        for i, article in enumerate(articles, 1):
            print(f"  [{i}] {article.title}")
            print(f"      链接: {article.url}")
            if article.cover_url:
                print(f"      封面: {article.cover_url}")
            if article.summary:
                print(f"      摘要: {article.summary[:50]}...")
            print()

    except Exception as e:
        await logSystemEvent(
            "抓取新闻失败喵……",
            str(e),
            LogLevel.ERROR,
            exception=e
        )




async def fetchAndPush(bot: Bot, targetChatID: str):
    """抓取并推送新闻"""

    if not targetChatID:
        print("❌ 未指定推送目标喵！")
        print("请使用 -id 参数指定，或在 .env 中配置 NEWS_TARGET_CHAT_ID\n")
        return

    print(f"正在抓取新闻并推送到 {targetChatID}……\n")

    try:
        articles = await fetchLatestNews(limit=NEWS_MAX_ARTICLES)

        if not articles:
            print("❌ 没有抓取到任何文章喵\n")
            return

        record = loadPushedRecord()
        pushed_count = 0
        skipped_count = 0

        for article in articles:
            if isAlreadyPushed(article.url, record):
                skipped_count += 1
                continue

            success = await pushToTelegram(bot, targetChatID, article)
            if success:
                markAsPushed(article.url, record)
                pushed_count += 1
                print(f"  ✅ 已推送: {article.title}")
            else:
                print(f"  ❌ 推送失败: {article.title}")

        savePushedRecord(record)

        print()
        print(f"推送完成喵！新推送 {pushed_count} 篇，跳过 {skipped_count} 篇（已推送过）\n")

    except Exception as e:
        await logSystemEvent(
            "抓取或推送新闻失败喵",
            str(e),
            LogLevel.ERROR,
            exception=e
        )




def showPushedList():
    """显示已推送记录"""
    record = loadPushedRecord()
    urls = record.get("pushed_urls", [])
    last_check = record.get("last_check")

    if not urls:
        print("还没有推送过任何文章喵\n")
        return

    print(f"已推送 {len(urls)} 篇文章")
    if last_check:
        print(f"最后检查时间: {last_check}")
    print()

    # 显示最近 10 条
    recent = urls[-10:]
    print("最近推送的文章：")
    for url in reversed(recent):
        print(f"  • {url}")
    print()




def clearPushedList():
    """清空已推送记录"""
    record = {
        "pushed_urls": [],
        "last_check": None
    }
    savePushedRecord(record)
    print("✅ 已清空推送记录喵\n")




def getHelp():
    return {

        "name": "/news",

        "description": "从外部网站抓取新闻并推送到 Telegram",

        "usage": (
            "/news -fetch [-id <chatID>]   抓取并推送新闻\n"
            "/news -test                   测试抓取（不推送）\n"
            "/news -list                   查看已推送记录\n"
            "/news -clear                  清空已推送记录"
        ),

        "example": (
            "测试抓取功能：/news -test\n"
            "推送到默认群聊：/news -fetch\n"
            "推送到指定群聊：/news -fetch -id '-1001234567890'"
        ),

    }