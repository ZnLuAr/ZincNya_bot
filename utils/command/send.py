"""
utils/command/send.py

用于实现 /send 命令的逻辑模块，负责从控制台发送消息，或进入与某个用户的实时本地聊天界面。

本模块精简为两个部分：
    - 参数解析与入口函数（execute）
    - 普通消息发送逻辑（sendMsg、chatIDList）

注：chatScreen 聊天界面已重构到 utils/chatScreen/ 模块。


================================================================================
参数解析 / 外层命令入口
包含 execute()、chatIDList() 两个功能

execute() 为统一的命令入口：
    - 接受 app 与命令行参数 tokens（来自 handlers/cli）
    - 解析参数后根据模式执行对应操作：
        · 普通发送模式：/send -id <ID> -t <TEXT>
        · 聊天界面模式：/send -c <ID>

    解析过程中使用 argAlias 将短选项映射到全称字段，并最终构造 parsed 字典。

chatIDList() 用于在 "-c" 未指定 ID 时展示 whitelist 的用户列表，
并允许用户通过编号或直接输入 ID 来选择目标聊天对象。
    注意：whitelistMenuController() 退出时会将 interactiveMode 设为 False，
    因此 chatScreen() 在调用后需要重新设置为 True。


================================================================================
普通消息发送逻辑
包含 sendMsg() 一个功能，处理在 cli 输入的指令

sendMsg(bot, idList, atUser, text)
    - 遍历 idList，将文字逐一发送给对应 ChatID
        - idList 为在 cli 输入的 "-id" 后的一个或若干个 ChatID
    - 当指定 -a/--at <userName> 时，会自动在消息开头加上 "@userName"


================================================================================
本地交互式聊天界面（核心功能）
包含 chatScreen() 及其内部的 receiverLoop() / printMessage() / displayHistory()

chatScreen(app , bot: Bot , targetChatID: str)
    用于进入与某一 chatID 的实时聊天界面。

其操作模式为全屏 CLI 聊天窗口：
    · 使用 prompt_toolkit full_screen Application 管理终端布局
    · 上方：滚动聊天记录区（历史 + 实时消息）
    · 底部：固定多行输入区（Enter 换行，Ctrl+S / Alt+Enter 发送）
    · 收发的消息会自动保存到加密数据库（chatHistory 模块）
    · 按 Esc 或 Ctrl+C 退出界面
    · 支持 LLM 审核命令(:ra/:re/:rr/:rc)、滚动历史(Alt+↑↓ / PgUp/PgDn)

键绑定:
    · Enter          - 换行
    · Ctrl+S / Alt+Enter  - 发送消息
    · Esc / Ctrl+C   - 退出聊天界面
    · Ctrl+X         - 清空输入框
    · Alt+↑ / PgUp   - 向上滚动历史
    · Alt+↓ / PgDn   - 向下滚动历史
    · Alt+← / Alt+→  - 切换聊天对象（上一个/下一个）

详细技术文档请参阅: docs/chatScreen.md

内部结构：
    - 设置 interactiveMode = True，暂停外层 CLI
    - ChatScreenApp (utils/chatUI.py) 管理 prompt_toolkit 全屏布局
    - receiverLoop() 后台协程，持续监听 messageQueue 中的消息
        · 使用 asyncio.wait_for() 带超时读取，避免阻塞
        · 筛选属于当前 chatID 的消息并更新 UI
        · 检测 shutdownEvent 时主动触发 UI 退出
    - 主循环每轮调用 ui.run() 事件驱动，提交后处理发送逻辑

    正常情况下，用户按 Esc 或 Ctrl+C 退出函数

退出时的清理工作（finally 块）：
    1. 恢复 interactiveMode 为 False
    2. 取消 receiverTask 协程
    3. 将非目标消息放回队列


================================================================================

本模块不直接与 whitelist.json 交互，
仅通过 whitelistManager 提供的 whitelistMenuController() 来选择聊天对象。

聊天记录通过 chatHistory 模块进行加密存储和读取。
chatScreen 聊天界面已重构到独立模块 utils/chatScreen/，详见 docs/chatScreen.md。

"""

from telegram import Bot
from telegram.error import Forbidden

from handlers.cli import parseArgsTokens

from utils.core.logger import logAction, LogLevel, LogChildType
from utils.whitelistManager.ui import whitelistMenuController




async def execute(app , args):

    bot: Bot = app.bot
 
    parsed = {
        "at": None,
        "text": None,
        "id": [],
        "chat": None,
    }

    # Send 中将缩写映射到全称的字典
    argAlias = {
        "a": "at",
        "t": "text",
        "i": "id",
        "c": "chat",
    }

    parsed = parseArgsTokens(parsed , args , argAlias)
    
    atUser = parsed["at"]
    text = parsed["text"]
    idList = parsed["id"]
    screenChatID = parsed["chat"]

    # 参数验证
    if screenChatID is not None:
        # 聊天界面模式
        if screenChatID == "NoValue":
            # 未指定 ID，弹出白名单列表供选择
            screenChatID = await chatIDList(app, bot)

            # 用户未选择或 whitelist 为空
            if not screenChatID:
                print("没有可用的聊天对象喵（whitelist 为空或未选择）\n")
                return

        if screenChatID:
            # ── 循环支持切换 ──
            # 用 while 循环实现非递归切换:
            # chatScreen 返回 {"action": "switch", "direction": ...} 时继续循环
            from utils.chatScreen import chatScreen
            from utils.chatScreen.helpers import getNextChatID

            currentChatID = screenChatID
            while currentChatID:
                result = await chatScreen(app, bot, currentChatID)

                # 检测切换信号 (mainLoop → session → 这里)
                if result and result.get("action") == "switch":
                    direction = result["direction"]
                    nextChatID = getNextChatID(currentChatID, direction)

                    if nextChatID and nextChatID != currentChatID:
                        currentChatID = nextChatID
                        continue  # 进入下一个聊天

                # 正常退出,结束循环
                break

            # 退出 chatScreen 循环后打印提示
            print("\n退出聊天界面喵——\n")

        return

    if not text or text == "NoValue":
        print("❌ もー、参数 [-t/--text <text>] 要加上才可以发得出文字啦——！\n")
        return
    
    if not idList or idList == "NoValue":
        print("❌ もー、参数 [-id/--id <chatID>] 得加上才对啦——！\n")
        return
    
    await sendMsg(bot , idList , atUser , text)




async def chatIDList(app , bot):

    # 使用交互式选择器
    selectedUID = await whitelistMenuController(bot , app)

    return selectedUID




async def sendMsg(bot: Bot , idList , atUser , text):
    # 执行发送信息给<chatID>
    for chatID in idList:
        try:
            msg = text
            if atUser:
                msg = f"@{atUser} {text}"
            await bot.send_message(chat_id=chatID , text=msg)
            result = f"✅ 已发送给 {chatID} 喵——"

        except Exception as e:
            result = f"❌ 向 {chatID} 发送失败了喵：{e}"

        finally:
            await logAction(
                "Console",
                f"/send → {chatID} ┃ {text}",
                result,
                LogLevel.INFO,
                LogChildType.WITH_ONE_CHILD
            )




def getHelp():
    return {

        "name": "/send",
    
        "description": "在控制台中向指定对象发送一条消息",
    
        "usage": (
            "/send -id <id> -t <text>                   向指定对象发送消息\n"
            "/send -id <id1> <id2> -t <text>            向多个对象发送消息\n"
            "/send -id <id> -a <userName> -t <text>     发送消息并 @ 用户\n"
            "/send -c <chatID>                          进入与指定用户的聊天界面\n"
            "/send -c                                   弹出列表选择聊天对象\n"
            "\n"
            "注：ID 可通过 Telegram 的 @myidbot 获取哦"
        ),

        "example": (
            "向用户发送消息：/send -id '1234567' -t 'Hello world'\n"
            "发送并 @ 用户：/send -id '-1234567' -a 'userName' -t 'Do you know I'm a bot?'\n"
            "向多个用户发送：/send -id '1234567' '1234568' -t '👀'\n"
            "进入聊天界面：/send -c '1234567'\n"
            "选择聊天对象：/send -c"
        ),

    }
