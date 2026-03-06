"""
utils/todoReminder.py

待办事项后台提醒任务。

每隔 TODOS_REMINDER_CHECK_INTERVAL 秒检查一次数据库，
将到期的待办通过 Telegram 发送提醒消息给用户，
提醒后标记 reminded=1（保留 remind_time 用于列表斜体显示）。
"""


import asyncio
from utils.todoDB import getPendingReminders, updateTodo
from utils.logger import logSystemEvent, LogLevel, LogChildType
from config import TODOS_REMINDER_CHECK_INTERVAL


PRIORITY_EMOJI = {
    'P0': '🔴', 'P1': '🟠', 'P2': '🟡', 'P3': '🟢', 'P_': '⚪',
}


async def todoReminderLoop(app):
    """
    后台提醒循环。

    在 bot.py 中通过 asyncio.create_task() 启动。
    检查到期待办 → 发送提醒 → 标记 reminded=1。
    """
    while True:
        try:
            await asyncio.sleep(TODOS_REMINDER_CHECK_INTERVAL)

            todos = await getPendingReminders()
            if not todos:
                continue

            bot = app.bot

            for todo in todos:
                try:
                    pri = PRIORITY_EMOJI.get(todo['priority'], '⚪')

                    # 控制台 todo：直接 print 到终端，不走 Telegram
                    if todo['chat_id'] == "console":
                        print(
                            f"\n⏰ 控制台待办提醒——\n"
                            f"  [{todo['id']}] {todo['content']}\n"
                            f"  优先：{pri} {todo['priority']}\n"
                        )
                        await updateTodo(todo['id'], reminded=1)
                        await logSystemEvent(
                            "控制台待办提醒已输出",
                            f"ID {todo['id']}",
                            LogLevel.INFO,
                            LogChildType.WITH_ONE_CHILD,
                        )
                        continue

                    text = (
                        f"⏰ 待办提醒喵——\n\n"
                        f"{todo['content']}\n"
                        f"优先：{pri} {todo['priority']}"
                    )

                    await bot.send_message(
                        chat_id=todo['chat_id'],
                        text=text,
                    )

                    # 标记已提醒，防止重复提醒（保留 remind_time 用于列表斜体显示）
                    await updateTodo(todo['id'], reminded=1)

                    await logSystemEvent(
                        "待办提醒已发送",
                        f"ID {todo['id']} → Chat {todo['chat_id']}",
                        LogLevel.INFO,
                        LogChildType.WITH_ONE_CHILD,
                    )

                except Exception as e:
                    await logSystemEvent(
                        "待办提醒发送失败喵",
                        f"ID {todo['id']}: {str(e)}",
                        LogLevel.ERROR,
                        exception=e,
                    )

        except asyncio.CancelledError:
            break
        except Exception as e:
            await logSystemEvent(
                "提醒循环出错喵",
                str(e),
                LogLevel.ERROR,
                exception=e,
            )
            await asyncio.sleep(TODOS_REMINDER_CHECK_INTERVAL)
