"""
handlers/book.py

书籍搜索 Telegram Handler，提供交互式书籍搜索体验。

本模块作为表示层，负责：
    - 处理 /book <query> 命令
    - 处理 "找书 xxx" / "搜书 xxx" 触发词
    - 管理 InlineKeyboard 交互（翻页、关闭）
    - 渲染列表视图

设计原则：
    - 单消息编辑模式：所有交互在同一条消息上进行，避免刷屏
    - 状态存储在 context.user_data 中，支持多用户并发
    - 与 bookSearchAPI 解耦，本模块不直接调用网络请求


================================================================================
交互流程
================================================================================

1. 用户发送 `/book 线性代数` 或 `找书 线性代数`
2. Bot 返回搜索结果列表（带分页按钮）
3. 列表中的书名为超链接，点击可跳转 Open Library 查看详情
4. 用户可通过翻页按钮浏览更多结果
5. 点击关闭按钮删除消息


================================================================================
Callback Data 格式
================================================================================

由于 Telegram callback_data 限制 64 字节，使用以下紧凑编码：

    book:l:{hash}:{page}        列表视图/翻页 (list)
        - hash: 搜索词的 MD5 短哈希（BOOK_QUERY_HASH_LENGTH 位）
        - page: 页码

    book:c                      关闭/删除消息 (close)

搜索词哈希机制：
    由于 callback_data 长度限制，无法存储完整搜索词。
    使用 MD5 哈希的前 N 位作为键，完整搜索词存储在 context.user_data 中：
        context.user_data[f"book_query_{hash}"] = "完整搜索词"


================================================================================
用户数据结构 (context.user_data)
================================================================================

book_query_{hash}: str
    存储搜索词哈希对应的完整搜索词


================================================================================
注册的 Handlers
================================================================================

CommandHandler("book" , handleBookCommand)
    处理 /book <query> 命令

MessageHandler(filters.Regex(...) , handleBookTrigger)
    处理 "找书 xxx" / "搜书 xxx" 触发词
    正则: r'^(找书|搜书)\s+.+'

CallbackQueryHandler(handleBookCallback , pattern=r'^book:')
    处理所有 book: 开头的回调


================================================================================
主要函数
================================================================================

handleBookCommand(update , context) -> None
────────────────────────────────────────
    处理 /book 命令。

    流程:
        1. 从 context.args 提取搜索词
        2. 生成搜索词哈希并存储
        3. 调用 searchBooks API
        4. 渲染列表视图并发送

    无参数时显示用法提示。


handleBookTrigger(update , context) -> None
────────────────────────────────────────
    处理触发词消息（找书/搜书）。

    从消息文本中提取搜索词，然后复用 handleBookCommand 逻辑。


handleBookCallback(update , context) -> None
────────────────────────────────────────
    处理按钮回调。

    根据 callback_data 前缀分发到对应处理逻辑：
        - "c": 关闭（删除消息）
        - "l": 列表视图（翻页）


================================================================================
渲染函数
================================================================================

_renderListView(query , searchResult) -> tuple[str , InlineKeyboardMarkup]
────────────────────────────────────────
    渲染搜索结果列表视图。

    参数:
        query: str              搜索词（用于显示）
        searchResult: dict      searchBooks 返回的结果

    返回:
        (消息文本 , 键盘布局)

    消息格式:
        使用 HTML 格式，书名为可点击的超链接（跳转 Open Library）

    键盘布局:
        [« 上页] [下页 »]       导航按钮（仅在有多页时显示）
        [✖ 关闭]               关闭按钮


================================================================================
辅助函数
================================================================================

_hashQuery(query) -> str
    生成搜索词的 MD5 短哈希（BOOK_QUERY_HASH_LENGTH 位）

_escapeHtml(text) -> str
    转义 HTML 特殊字符（& < >）

_truncate(text , maxLen) -> str
    截断文本，超长时末尾加 "…"

_safeEditText(message , text , **kwargs) -> bool
    安全地编辑消息，忽略"消息未修改"错误

"""


import hashlib
from telegram.error import BadRequest
from telegram import Update , InlineKeyboardButton , InlineKeyboardMarkup
from telegram.ext import CommandHandler , MessageHandler , CallbackQueryHandler , ContextTypes , filters

from utils.bookSearchAPI import searchBooks
from config import BOOK_ITEMS_PER_PAGE , BOOK_QUERY_HASH_LENGTH




def _hashQuery(query: str) -> str:
    """生成搜索词的短哈希"""
    return hashlib.md5(query.encode()).hexdigest()[:BOOK_QUERY_HASH_LENGTH]


async def _safeEditText(message , text: str , **kwargs) -> bool:
    """
    安全地编辑消息，忽略"消息未修改"错误。

    当用户快速点击按钮时，可能会触发多次编辑请求，
    如果内容相同，Telegram 会抛出 BadRequest。
    """
    try:
        await message.edit_text(text , **kwargs)
        return True
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return False  # 静默忽略
        raise  # 其他错误继续抛出


def _truncate(text: str , maxLen: int) -> str:
    """截断文本"""
    if len(text) <= maxLen:
        return text
    return text[:maxLen - 1] + "…"




def _escapeHtml(text: str) -> str:
    """转义 HTML 特殊字符"""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def _renderListView(query: str , searchResult: dict) -> tuple[str , InlineKeyboardMarkup]:
    """
    渲染搜索结果列表视图

    返回: (消息文本 , 键盘)

    注意: 返回的文本使用 HTML 格式，发送时需要 parse_mode="HTML"
    """

    total = searchResult.get("total" , 0)
    page = searchResult.get("page" , 1)
    totalPages = searchResult.get("totalPages" , 0)
    results = searchResult.get("results" , [])
    error = searchResult.get("error")

    # 处理错误
    if error:
        text = f"⚠️ 搜索出错了喵：{_escapeHtml(error)}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✖ 关闭" , callback_data="book:c")
        ]])
        return text , keyboard

    # 处理无结果
    if total == 0:
        text = f"📭 没有找到「{_escapeHtml(query)}」相关的书籍喵……\n\n试试换个关键词？"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✖ 关闭" , callback_data="book:c")
        ]])
        return text , keyboard

    # 构建列表文本（HTML 格式）
    lines = [f"📚 搜索「{_escapeHtml(_truncate(query , 20))}」- 找到 {total} 本喵\n"]

    for book in results:
        title = _truncate(book["title"] , 40)
        authors = " , ".join(book["authors"][:2]) if book["authors"] else "Unknown"
        authors = _truncate(authors , 25)
        year = book["year"] or "?"
        lang = book["language"] or "?"

        # 生成 Open Library 链接
        bookUrl = f"https://openlibrary.org/works/{book['id']}"

        # 使用 HTML 超链接格式
        lines.append(f"📖 <a href=\"{bookUrl}\">{_escapeHtml(title)}</a>")
        lines.append(f"   {_escapeHtml(authors)} · {year} · {lang}\n")

    lines.append(f"第 {page}/{totalPages} 页")

    text = "\n".join(lines)

    # 构建键盘（只有翻页和关闭按钮）
    queryHash = _hashQuery(query)

    # 导航按钮行
    navButtons = []
    if page > 1:
        navButtons.append(
            InlineKeyboardButton(f"<< 上页 ({page - 1})" , callback_data=f"book:l:{queryHash}:{page - 1}")
        )
    if page < totalPages:
        navButtons.append(
            InlineKeyboardButton(f"下页 ({page + 1}) >>" , callback_data=f"book:l:{queryHash}:{page + 1}")
        )

    # 关闭按钮
    closeButton = [InlineKeyboardButton("✖ 取消搜索喵" , callback_data="book:c")]

    # 组装键盘
    keyboard_rows = []
    if navButtons:
        keyboard_rows.append(navButtons)
    keyboard_rows.append(closeButton)

    keyboard = InlineKeyboardMarkup(keyboard_rows)

    return text , keyboard




async def handleBookCommand(update: Update , context: ContextTypes.DEFAULT_TYPE):
    """处理 /book <query> 命令"""

    # 提取搜索词
    if context.args:
        query = " ".join(context.args)
    else:
        await update.message.reply_text(
            "📚 用法：/book <书名或作者>\n\n"
            "例如：\n"
            "  /book 堂吉诃德 \n"
            "  /book Marcel Proust \n"
            "  /bookerta Python"
            "\n\n"
            "Open Library 作为以英文为主的数据库，\n"
            "用英文作者名/书名搜索效果可能会更好哦——"
        )
        return

    # 存储搜索词
    queryHash = _hashQuery(query)
    context.user_data[f"book_query_{queryHash}"] = query

    # 搜索
    result = await searchBooks(query , page=1 , limit=BOOK_ITEMS_PER_PAGE)

    # 渲染并发送
    text , keyboard = _renderListView(query , result)
    await update.message.reply_text(text , reply_markup=keyboard , parse_mode="HTML")




async def handleBookTrigger(update: Update , context: ContextTypes.DEFAULT_TYPE):
    """处理 "找书 xxx" / "搜书 xxx" 触发词"""

    message = update.message.text.strip()

    # 提取搜索词（去掉触发词）
    for trigger in ["找书" , "搜书"]:
        if message.startswith(trigger):
            query = message[len(trigger):].strip()
            break
    else:
        return

    if not query:
        await update.message.reply_text("在找什么书呢……？\n告诉咱书名或作者吧……")
        return

    # 复用命令处理逻辑
    context.args = query.split()
    await handleBookCommand(update , context)




async def handleBookCallback(update: Update , context: ContextTypes.DEFAULT_TYPE):
    """处理按钮回调"""

    query = update.callback_query
    await query.answer()

    data = query.data

    # 解析 callback_data
    parts = data.split(":")
    if len(parts) < 2:
        return

    action = parts[1]

    # ========== 关闭 ==========
    if action == "c":
        await query.message.delete()
        return

    # ========== 列表视图 (翻页) ==========
    if action == "l" and len(parts) >= 4:
        queryHash = parts[2]
        page = int(parts[3])

        # 从 user_data 获取搜索词
        query_text = context.user_data.get(f"book_query_{queryHash}" , "")

        if not query_text:
            await _safeEditText(query.message , "⚠️ 搜索已过期，请重新搜索喵")
            return

        # 重新搜索
        result = await searchBooks(query_text , page=page , limit=BOOK_ITEMS_PER_PAGE)

        # 渲染并更新消息
        text , keyboard = _renderListView(query_text , result)
        await _safeEditText(query.message , text , reply_markup=keyboard , parse_mode="HTML")
        return




def register():
    """注册 handlers"""
    return {
        "handlers": [
            CommandHandler("book" , handleBookCommand),
            MessageHandler(
                filters.Regex(r'^(找书|搜书)\s+.+') & ~filters.COMMAND,
                handleBookTrigger
            ),
            CallbackQueryHandler(handleBookCallback , pattern=r'^book:'),
        ],
        "name": "书籍搜索",
        "description": "搜索 Open Library 书籍数据库",
    }
