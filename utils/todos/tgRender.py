"""
utils/todos/tgRender.py

Telegram 侧渲染模块。

提供列表视图和详情视图的文本与键盘布局生成，
供 handlers/todos.py 调用。纯渲染逻辑，不含 Telegram API 调用。

主要接口：
    preview(content)                             截断内容用于列表预览
    renderListView(todos, total, page, userName)  列表视图（文本 + 键盘）
    renderDetailView(todo)                        详情视图（文本 + 键盘）
"""


import html
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import TODOS_ITEMS_PER_PAGE, TODOS_CONTENT_PREVIEW_LENGTH

from utils.todos.utils import PRIORITY_EMOJI, formatRemindTime




def preview(content: str) -> str:
    """截断内容用于列表预览"""
    if len(content) <= TODOS_CONTENT_PREVIEW_LENGTH:
        return content
    return content[:TODOS_CONTENT_PREVIEW_LENGTH] + "…"




def renderListView(
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
        previewText = html.escape(preview(todo['content']))
        timeStr = f"    {formatRemindTime(todo['remind_time'])}" if todo['remind_time'] else ""
        line = f"{pri} {num}. {previewText}{timeStr}    - {todo['priority']}"

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
        navRow.append(InlineKeyboardButton("<< 上一页喵", callback_data=f"todos:list:{page - 1}"))
    if page < totalPages:
        navRow.append(InlineKeyboardButton("下一页喵 >>", callback_data=f"todos:list:{page + 1}"))
    if navRow:
        rows.append(navRow)

    # 关闭按钮
    rows.append([InlineKeyboardButton("✖ 关闭", callback_data="todos:close")])

    return text, InlineKeyboardMarkup(rows)




def renderDetailView(todo: dict) -> tuple[str, InlineKeyboardMarkup]:
    """渲染待办详情视图"""

    pri = todo['priority']
    priEmoji = PRIORITY_EMOJI.get(pri, '⚪')

    remindLine = (
        f"提醒：是 — {formatRemindTime(todo['remind_time'])}"
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
        for p in ['P0', 'P1', 'P2', 'P3', 'P_']
    ]

    # 操作按钮行（已完成时显示"重新打开"替代"完成"）
    if todo['status'] == 'done':
        statusButton = InlineKeyboardButton("🔄 重新打开", callback_data=f"todos:reopen:{todoID}")
    else:
        statusButton = InlineKeyboardButton("✅ 完成", callback_data=f"todos:done:{todoID}")

    actionButtons = [
        statusButton,
        InlineKeyboardButton("❌ 删除", callback_data=f"todos:delc:{todoID}"),
        InlineKeyboardButton("↩️ 返回", callback_data="todos:list:1"),
    ]

    return text, InlineKeyboardMarkup([priButtons, actionButtons])
