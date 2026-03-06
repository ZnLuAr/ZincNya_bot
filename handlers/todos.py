"""
handlers/todos.py

待办事项 Telegram Handler，提供交互式待办管理体验。

本模块作为表示层，负责：
    - 处理 /todos [内容] 命令
    - 管理 InlineKeyboard 交互（分页、查看详情、编辑优先级、完成/删除）
    - 渲染列表视图和详情视图
    - 解析自然语言时间表达式

设计原则：
    - 单消息编辑模式：所有交互在同一条消息上进行，避免刷屏
    - 权限隔离：通过 chat_id + user_id 确保用户只能看到自己的待办


================================================================================
命令格式
================================================================================

    /todos                      显示待办列表
    /todos 买牛奶               快速添加（无时间，P_）
    /todos 2h 吃饭              2 小时后提醒
    /todos 明天 交报告           明天同一时间提醒
    /todos 明天9:00 开会         明天 9 点提醒
    /todos P1 买牛奶             添加 P1 优先级（无提醒）
    /todos P0 2h 紧急开会        P0 + 2小时后提醒


================================================================================
Callback Data 格式
================================================================================

    todos:list:{page}              列表视图（翻页）
    todos:detail:{id}              详情视图
    todos:pri:{id}:{priority}      修改优先级
    todos:done:{id}                标记完成
    todos:delc:{id}                删除确认
    todos:del:{id}                 确认后删除
    todos:close                    关闭消息


================================================================================
注册的 Handlers
================================================================================

    CommandHandler("todos", handleTodosCommand)
    CallbackQueryHandler(handleTodosCallback, pattern=r'^todos:')

"""


import re
import sys
import html
from io import StringIO
from datetime import datetime, timedelta


from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes

from utils.todoDB import (
    addTodo, getTodos, getTodoByID,
    updateTodo, deleteTodo, markDone, getTodosCount,
)
from utils.logger import logAction, LogLevel, LogChildType
from config import TODOS_ITEMS_PER_PAGE, TODOS_CONTENT_PREVIEW_LENGTH, TODOS_CONTENT_MAX_LENGTH

# jionlp 在 import 时会 print 广告，临时压制
# 广告的格式不太对，在一连串加载信息中会有些突兀…… 申し訳ございません🙇
_stdout = sys.stdout
sys.stdout = StringIO()
import jionlp as jio
sys.stdout = _stdout
del _stdout




# 记录每个用户最后一条 /todos 列表消息，用于防刷屏
# key: (chat_id, user_id), value: message_id
_lastQueryMessages: dict[tuple[str, str], int] = {}


# ============================================================================
# 时间解析
# ============================================================================


def _parseTime(text: str) -> datetime | None:
    """
    从文本中识别时间表达式，仅返回解析出的时间，不修改原文。

    支持：
        2h / 30m / 1d           英文简写（需在开头）
        一分钟后 / 两小时后      中文相对时间（jionlp）
        明天 / 后天              自然语言（jionlp）
        明天9:00 / 下午3点       自然语言+时刻（jionlp）
    """
    # 1. 英文简写相对时间：2h, 30m, 1d（仅开头）
    relMatch = re.match(r'^(\d+)([mhd])\b', text)
    if relMatch:
        value, unit = relMatch.groups()
        delta = {
            'm': timedelta(minutes=int(value)),
            'h': timedelta(hours=int(value)),
            'd': timedelta(days=int(value)),
        }[unit]
        return datetime.now() + delta

    # 2. 自然语言（jionlp）
    base = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        result = jio.parse_time(text, time_base=base)
        parsed = datetime.strptime(result['time'][0], '%Y-%m-%d %H:%M:%S')
        if parsed > datetime.now():
            return parsed
    except (ValueError, KeyError):
        pass

    return None


def _parsePriority(text: str) -> tuple[str, str]:
    """
    从文本中提取优先级标记，返回 (优先级, 剩余文本)。
    优先级必须是独立的空格分隔 token。

    示例：
        "P0 买牛奶"    → ("P0", "买牛奶")
        "买牛奶 P0"    → ("P0", "买牛奶")
        "买牛奶"       → ("P_", "买牛奶")
    """
    tokens = text.split()
    for i, token in enumerate(tokens):
        if re.fullmatch(r'P[0-3_]', token):
            remaining = ' '.join(tokens[:i] + tokens[i+1:])
            return (token, remaining)
    return ('P_', text)




# ============================================================================
# 渲染工具
# ============================================================================

PRIORITY_EMOJI = {
    'P0': '🔴', 'P1': '🟠', 'P2': '🟡', 'P3': '🟢', 'P_': '⚪',
}


def _formatRemindTime(dt: datetime) -> str:
    """格式化提醒时间为友好显示"""
    now = datetime.now()
    today = now.date()
    tomorrow = today + timedelta(days=1)

    if dt.date() == today:
        return f"今天 {dt.strftime('%H:%M')}"
    elif dt.date() == tomorrow:
        return f"明天 {dt.strftime('%H:%M')}"
    else:
        return dt.strftime("%Y/%m/%d %H:%M")


def _preview(content: str) -> str:
    """截断内容用于列表预览"""
    if len(content) <= TODOS_CONTENT_PREVIEW_LENGTH:
        return content
    return content[:TODOS_CONTENT_PREVIEW_LENGTH] + "…"


async def _safeEdit(message, text: str, **kwargs) -> bool:
    """安全地编辑消息，忽略"消息未修改"错误"""
    try:
        await message.edit_text(text, **kwargs)
        return True
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return False
        raise




# ============================================================================
# 渲染视图
# ============================================================================

def _renderListView(
    todos: list,
    total: int,
    page: int,
    userName: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """渲染待办列表视图"""

    totalPages = max(1, (total + TODOS_ITEMS_PER_PAGE - 1) // TODOS_ITEMS_PER_PAGE)

    # 空列表
    if not todos:
        text = (
            f"@{html.escape(userName)} ……？喵（疑惑）\n\n"
            "你目前还没有交代给咱要提醒的事情哦……\n"
            "还是说想看看命令怎么用吗？\n"
            "那就键入 /todos help 吧——"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✖ 关闭", callback_data="todos:close")
        ]])
        return text, keyboard

    # 构建列表文本
    lines = [f"@{html.escape(userName)} 你有以下 todos 哦——\n"]
    offset = (page - 1) * TODOS_ITEMS_PER_PAGE

    for i, todo in enumerate(todos):
        num = offset + i + 1
        pri = PRIORITY_EMOJI.get(todo['priority'], '⚪')
        preview = html.escape(_preview(todo['content']))
        timeStr = f"    {_formatRemindTime(todo['remind_time'])}" if todo['remind_time'] else ""
        line = f"{pri} {num}. {preview}{timeStr}    - {todo['priority']}"

        # 过时/已提醒 → 斜体
        if todo['remind_time'] and todo['remind_time'] <= datetime.now():
            line = f"<i>{line}</i>"

        lines.append(line)

    lines.append(f"\n（第 {page}/{totalPages} 页，共 {total} 条喵）")
    lines.append("键入 /todos help 就能获取有关于该命令的帮助啦")
    text = "\n".join(lines)

    # ── 键盘 ──
    # 数字按钮（每行最多 6 个）
    numButtons = [
        InlineKeyboardButton(str(offset + i + 1), callback_data=f"todos:detail:{todo['id']}")
        for i, todo in enumerate(todos)
    ]

    rows = []
    for j in range(0, len(numButtons), 6):
        rows.append(numButtons[j:j + 6])

    # 翻页按钮
    navRow = []
    if page > 1:
        navRow.append(InlineKeyboardButton("<<", callback_data=f"todos:list:{page - 1}"))
    if page < totalPages:
        navRow.append(InlineKeyboardButton(">>", callback_data=f"todos:list:{page + 1}"))
    if navRow:
        rows.append(navRow)

    # 关闭按钮
    rows.append([InlineKeyboardButton("✖ 关闭", callback_data="todos:close")])

    return text, InlineKeyboardMarkup(rows)




def _renderDetailView(todo: dict) -> tuple[str, InlineKeyboardMarkup]:
    """渲染待办详情视图"""

    pri = todo['priority']
    priEmoji = PRIORITY_EMOJI.get(pri, '⚪')

    remindLine = (
        f"提醒：是 — {_formatRemindTime(todo['remind_time'])}"
        if todo['remind_time']
        else "提醒：无"
    )

    text = (
        f"{html.escape(todo['content'])}\n\n"
        f"{remindLine}\n"
        f"优先级：{priEmoji} {pri} 喵\n\n"
        "按下面的按钮可以编辑哦——"
    )

    todoID = todo['id']

    # 优先级按钮行
    priButtons = [
        InlineKeyboardButton(
            f"> {p} <" if pri == p else p,
            callback_data=f"todos:pri:{todoID}:{p}"
        )
        for p in ['P0', 'P1', 'P2', 'P_']
    ]

    # 操作按钮行
    actionButtons = [
        InlineKeyboardButton("✅ 完成", callback_data=f"todos:done:{todoID}"),
        InlineKeyboardButton("❌ 删除", callback_data=f"todos:delc:{todoID}"),
        InlineKeyboardButton("↩️ 返回", callback_data="todos:list:1"),
    ]

    return text, InlineKeyboardMarkup([priButtons, actionButtons])




# ============================================================================
# 命令处理
# ============================================================================

async def handleTodosCommand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /todos 命令"""

    user = update.effective_user
    chatID = str(update.effective_chat.id)
    userID = str(user.id)
    userName = user.username or user.first_name or str(user.id)

    args = context.args or []
    rawText = ' '.join(args).strip()

    # /todos help
    if rawText in ['help', '-help', '--help', '-h']:
        await update.message.reply_text(
            "📋 todos 命令用法\n\n"
            "<pre>"
            "/todos                            显示待办列表\n"
            "/todos 买牛奶                      快速添加\n"
            "/todos 2h 吃饭                     2 小时后提醒\n"
            "/todos 明天 交报告                 明天同一时间\n"
            "/todos 明天 9:00 开会              明天 9 点\n"
            "/todos P1 买牛奶                   指定优先级\n"
            "/todos P0 20时26分 关大门          优先级 + 时间"
            "</pre>\n\n"
            "ご主人说咱不像是很聪明的样子，\n"
            "如果优先级和时间没说清楚的话，咱可能会读错喵……",
            parse_mode="HTML",
        )
        return

    # /todos（无参数）→ 显示列表
    if not rawText:
        # 删除上一条查询消息，防刷屏
        key = (chatID, userID)
        oldMsgID = _lastQueryMessages.pop(key, None)
        if oldMsgID is not None:
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=oldMsgID)
            except Exception:
                pass

        total = getTodosCount(chatID, userID)
        todos = await getTodos(chatID, userID, limit=TODOS_ITEMS_PER_PAGE, offset=0)
        text, keyboard = _renderListView(todos, total, 1, userName)
        msg = await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")

        _lastQueryMessages[key] = msg.message_id
        return

    # /todos <内容> → 添加待办
    # 1. 提取优先级（任意位置）
    priority, content = _parsePriority(rawText)

    # 2. 识别时间（不修改内容）
    remindTime = _parseTime(content)

    # 3. 验证
    if not content.strip():
        await update.message.reply_text("……待办内容不能为空喵", parse_mode="HTML")
        return

    if len(content) > TODOS_CONTENT_MAX_LENGTH:
        await update.message.reply_text(
            f"……内容太长了喵，最多 {TODOS_CONTENT_MAX_LENGTH} 字哦",
            parse_mode="HTML",
        )
        return

    # 4. 写入数据库
    todoID = await addTodo(chatID, userID, content, remindTime, priority)
    if todoID is None:
        await update.message.reply_text("添加失败了喵，请稍后再试……", parse_mode="HTML")
        return

    # 5. 回复确认
    pri = PRIORITY_EMOJI.get(priority, '⚪')
    remindStr = f"\n提醒：{_formatRemindTime(remindTime)}" if remindTime else ""

    await update.message.reply_text(
        f"好、记下来了喵——\n\n"
        f"{html.escape(content)}{remindStr}\n\n"
        f"优先级：{pri} {priority}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("查看全部 todos", callback_data="todos:list:1")
        ]]),
        parse_mode="HTML",
    )

    await logAction(
        update.effective_user,
        "/todos 添加待办",
        f"ID {todoID}: {_preview(content)}",
        LogLevel.INFO,
        LogChildType.WITH_ONE_CHILD,
    )




# ============================================================================
# Callback 处理
# ============================================================================
#
# 回调路由表（前缀 todos:）：
#
#   todos:close                     关闭（删除 bot 消息）
#   todos:list:{page}               列表视图 / 翻页 (“返回” 按钮也取这个)
#   todos:detail:{todoID}           详情视图
#   todos:pri:{todoID}:{priority}   修改优先级
#   todos:done:{todoID}             标记完成
#   todos:delc:{todoID}             删除确认
#   todos:del:{todoID}              确认后删除
#

async def handleTodosCallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 InlineKeyboard 回调，路由表见上方注释"""

    query = update.callback_query
    await query.answer()

    user = update.effective_user
    chatID = str(update.effective_chat.id)
    userID = str(user.id)
    userName = user.username or user.first_name or str(user.id)
    userTag = f"@{html.escape(userName)}"

    parts = query.data.split(':')
    action = parts[1]

    try:
        # ── 关闭（删除消息） ──
        if action == 'close':
            await query.message.delete()
            return

        # ── 列表视图 / 翻页 ──
        if action == 'list':
            page = int(parts[2]) if len(parts) > 2 else 1
            total = getTodosCount(chatID, userID)
            offset = (page - 1) * TODOS_ITEMS_PER_PAGE
            todos = await getTodos(chatID, userID, limit=TODOS_ITEMS_PER_PAGE, offset=offset)

            text, keyboard = _renderListView(todos, total, page, userName)
            await _safeEdit(query.message, text, reply_markup=keyboard, parse_mode="HTML")
            return

        # ── 详情视图 ──
        if action == 'detail':
            todoID = int(parts[2])
            todo = await getTodoByID(todoID)

            if not todo or todo['user_id'] != userID:
                await _safeEdit(
                    query.message,
                    f"{userTag}  👀\n你在尝试点击是叭——\n……这条待办不存在或不属于你喵 (・ω・`)",
                    parse_mode="HTML",
                )
                return

            text, keyboard = _renderDetailView(todo)
            await _safeEdit(query.message, text, reply_markup=keyboard, parse_mode="HTML")
            return

        # ── 修改优先级 ──
        if action == 'pri':
            todoID = int(parts[2])
            newPriority = parts[3]
            todo = await getTodoByID(todoID)

            if not todo or todo['user_id'] != userID:
                await _safeEdit(
                    query.message,
                    f"呜……{userTag} 👀\n你在尝试点击是叭——\n……这条待办不存在或不属于你喵 (・ω・`)",
                    parse_mode="HTML",
                )
                return

            await updateTodo(todoID, priority=newPriority)
            todo['priority'] = newPriority

            text, keyboard = _renderDetailView(todo)
            await _safeEdit(query.message, text, reply_markup=keyboard, parse_mode="HTML")
            return

        # ── 标记完成 ──
        if action == 'done':
            todoID = int(parts[2])
            todo = await getTodoByID(todoID)

            if not todo or todo['user_id'] != userID:
                await _safeEdit(
                    query.message,
                    f"{userTag}……这条待办不存在或不属于你喵 (・ω・`)",
                    parse_mode="HTML",
                )
                return

            await markDone(todoID)
            await _safeEdit(
                query.message,
                f"✅ 完成了喵——\n\n{html.escape(todo['content'])}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ 返回列表", callback_data="todos:list:1")
                ]]),
                parse_mode="HTML",
            )
            return

        # ── 删除确认 ──
        if action == 'delc':
            todoID = int(parts[2])
            todo = await getTodoByID(todoID)

            if not todo or todo['user_id'] != userID:
                await _safeEdit(
                    query.message,
                    f"{userTag}……这条待办不存在或不属于你喵 (・ω・`)",
                    parse_mode="HTML",
                )
                return

            await _safeEdit(
                query.message,
                f"确定要删除这条待办吗？\n\n{html.escape(todo['content'])}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("确认删除", callback_data=f"todos:del:{todoID}"),
                        InlineKeyboardButton("↩️ 返回", callback_data=f"todos:detail:{todoID}"),
                    ]
                ]),
                parse_mode="HTML",
            )
            return

        # ── 确认后删除 ──
        if action == 'del':
            todoID = int(parts[2])
            todo = await getTodoByID(todoID)

            if not todo or todo['user_id'] != userID:
                await _safeEdit(
                    query.message,
                    f"{userTag}……这条待办不存在或不属于你喵 (・ω・`)",
                    parse_mode="HTML",
                )
                return

            await deleteTodo(todoID)

            # 删除后回到列表
            total = getTodosCount(chatID, userID)
            todos = await getTodos(chatID, userID, limit=TODOS_ITEMS_PER_PAGE, offset=0)
            text, keyboard = _renderListView(todos, total, 1, userName)
            await _safeEdit(query.message, text, reply_markup=keyboard, parse_mode="HTML")
            return

    except Exception as e:
        await logAction(
            user, "/todos callback 异常",
            f"{action}: {e}", LogLevel.ERROR,
        )
        await _safeEdit(
            query.message,
            f"出错了喵……\n{html.escape(str(e))}",
            parse_mode="HTML",
        )




# ============================================================================
# 注册
# ============================================================================

def register():
    return {
        "handlers": [
            CommandHandler("todos", handleTodosCommand),
            CallbackQueryHandler(handleTodosCallback, pattern=r'^todos:'),
        ],
        "name": "待办事项",
        "description": "管理个人待办事项，支持优先级和定时提醒",
    }
