"""
utils/newsAPI.py

外部新闻抓取模块，用于从指定网站获取新闻文章并推送到 Telegram。

主要功能：
    - fetchLatestNews()   获取最新文章列表
    - downloadCover()     下载封面图片
    - pushToTelegram()    推送文章到 Telegram
    - loadPushedRecord()  加载已推送记录
    - savePushedRecord()  保存已推送记录
    - isAlreadyPushed()   检查文章是否已推送

数据结构：
    NewsArticle 数据类包含：
        - title: 文章标题
        - url: 文章链接
        - cover_url: 封面图片链接（可选）
        - summary: 文章摘要（可选）
        - published_at: 发布时间（可选）
"""


import json
import aiohttp
from io import BytesIO
from datetime import datetime
from telegram import Bot
from dataclasses import dataclass
from typing import Optional
from bs4 import BeautifulSoup

from config import (
    NEWS_SOURCE_URL,
    NEWS_HTTP_PROXY,
    NEWS_REQUEST_TIMEOUT,
    NEWS_DATA_FILE,
)




@dataclass
class NewsArticle:
    """新闻文章数据结构"""
    title: str
    url: str
    cover_url: Optional[str] = None
    summary: Optional[str] = None
    published_at: Optional[str] = None




async def fetchLatestNews(limit: int = 5) -> list[NewsArticle]:
    """
    获取最新文章列表

    参数:
        limit: 最多获取的文章数量

    返回:
        NewsArticle 列表
    """
    timeout = aiohttp.ClientTimeout(total=NEWS_REQUEST_TIMEOUT)

    async with aiohttp.ClientSession() as session:
        async with session.get(
            NEWS_SOURCE_URL,
            timeout=timeout,
            proxy=NEWS_HTTP_PROXY
        ) as response:
            html = await response.text()

    soup = BeautifulSoup(html, "html.parser")
    articles = []

    # ========================================================================
    # TODO: 根据实际网页结构调整解析逻辑
    # 以下是占位代码，需要用户提供网页结构后修改
    # ========================================================================

    # 示例：假设文章在 <article> 标签中
    for item in soup.select("article")[:limit]:
        title_el = item.select_one("h2, h3, .title")
        link_el = item.select_one("a")
        cover_el = item.select_one("img")
        summary_el = item.select_one("p, .summary, .excerpt")

        if not title_el or not link_el:
            continue

        title = title_el.get_text(strip=True)
        url = link_el.get("href", "")

        # 处理相对路径
        if url and not url.startswith("http"):
            base_url = NEWS_SOURCE_URL.rsplit("/", 1)[0]
            url = f"{base_url}/{url.lstrip('/')}"

        cover_url = None
        if cover_el:
            cover_url = cover_el.get("src")
            if cover_url and not cover_url.startswith("http"):
                base_url = NEWS_SOURCE_URL.rsplit("/", 1)[0]
                cover_url = f"{base_url}/{cover_url.lstrip('/')}"

        summary = summary_el.get_text(strip=True) if summary_el else None

        articles.append(NewsArticle(
            title=title,
            url=url,
            cover_url=cover_url,
            summary=summary,
            published_at=None
        ))

    return articles




async def downloadCover(url: str) -> bytes:
    """
    下载封面图片

    参数:
        url: 图片 URL

    返回:
        图片的二进制数据
    """
    timeout = aiohttp.ClientTimeout(total=NEWS_REQUEST_TIMEOUT)

    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            timeout=timeout,
            proxy=NEWS_HTTP_PROXY
        ) as response:
            return await response.read()




async def pushToTelegram(bot: Bot, chat_id: str, article: NewsArticle) -> bool:
    """
    推送文章到 Telegram

    参数:
        bot: Telegram Bot 实例
        chat_id: 目标聊天 ID
        article: 要推送的文章

    返回:
        是否推送成功
    """
    # 构建消息内容
    caption = f"**{article.title}**"
    if article.summary:
        caption += f"\n\n{article.summary}"
    caption += f"\n\n[阅读原文]({article.url})"

    try:
        if article.cover_url:
            # 下载封面图并发送
            cover_data = await downloadCover(article.cover_url)
            await bot.send_photo(
                chat_id=chat_id,
                photo=BytesIO(cover_data),
                caption=caption,
                parse_mode="Markdown"
            )
        else:
            # 无封面图，发送纯文本
            await bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="Markdown"
            )
        return True

    except Exception as e:
        print(f"推送失败: {e}")
        return False




def loadPushedRecord() -> dict:
    """
    加载已推送记录

    返回:
        包含 pushed_urls 列表和 last_check 时间的字典
    """
    try:
        with open(NEWS_DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "pushed_urls": [],
            "last_check": None
        }




def savePushedRecord(record: dict) -> None:
    """
    保存已推送记录

    参数:
        record: 要保存的记录字典
    """
    record["last_check"] = datetime.now().isoformat()

    with open(NEWS_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)




def isAlreadyPushed(url: str, record: dict) -> bool:
    """
    检查文章是否已推送

    参数:
        url: 文章 URL
        record: 已推送记录

    返回:
        是否已推送
    """
    return url in record.get("pushed_urls", [])




def markAsPushed(url: str, record: dict) -> None:
    """
    标记文章为已推送

    参数:
        url: 文章 URL
        record: 已推送记录（会被修改）
    """
    if "pushed_urls" not in record:
        record["pushed_urls"] = []

    if url not in record["pushed_urls"]:
        record["pushed_urls"].append(url)

    # 只保留最近 1000 条记录，避免文件过大
    if len(record["pushed_urls"]) > 1000:
        record["pushed_urls"] = record["pushed_urls"][-1000:]
