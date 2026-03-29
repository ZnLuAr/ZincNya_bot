"""
utils/command/send.py

用于实现 /send 命令的逻辑模块，负责从控制台发送消息，或进入与某个用户的实时本地聊天界面。

本模块大致可分为三个部分：
    - 参数解析与入口函数（execute）
    - 普通消息发送逻辑（sendMsg）
    - 本地交互式聊天界面（chatScreen）


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
    · 底部：固定多行输入区（Enter 换行，Ctrl+S / Esc+Enter 发送）
    · 收发的消息会自动保存到加密数据库（chatHistory 模块）
    · 按 Esc 或 Ctrl+C 退出界面

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
异步输入通过 inputHelper 模块的 asyncInput() 实现（基于 run_in_executor）。
聊天界面使用 ChatScreenApp (utils/chatUI.py) 管理，基于 prompt_toolkit 全屏 Application。

"""




import asyncio
from telegram import Bot
from datetime import datetime
from telegram.error import Forbidden

from handlers.cli import parseArgsTokens

from utils.core.stateManager import getStateManager
from utils.logger import logAction, LogLevel, LogChildType
from utils.whitelistManager.ui import whitelistMenuController
from utils.chatHistory import saveMessage, loadHistory, iterMessagesWithDateMarkers




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
        await chatScreen(app , bot , screenChatID)
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




def _getSenderName(message) -> str:
    """提取消息发送者名称，缺失时回退为 Unknown。"""
    from_user = getattr(message, "from_user", None)
    if from_user:
        return from_user.username or from_user.first_name or "Unknown"

    chat = getattr(message, "chat", None)
    if chat:
        return getattr(chat, "title", None) or getattr(chat, "full_name", None) or "Unknown"

    return "Unknown"



def _formatMessageLines(timestamp: str, sender: str, text: str) -> list[str]:
    """按统一规则将单条消息格式化为行列表，支持多行内容。"""
    sender = sender or "Unknown"
    text = text or ""

    if '\n' in text:
        lines = text.split('\n')
        result = [f"[{timestamp}] <{sender}> {lines[0]}"]
        indent = "          | "
        for line in lines[1:]:
            result.append(f"{indent}{line}")
        return result
    else:
        return [f"[{timestamp}] <{sender}> {text}"]


def _printFormattedMessage(timestamp: str, sender: str, text: str):
    """按统一规则打印单条消息（兼容现有调用点）。"""
    for line in _formatMessageLines(timestamp, sender, text):
        print(line)



def _formatMessage(mode, content) -> tuple[str, str, str]:
    """
    格式化单条消息，返回 (timestamp, sender, text)。
    供 UI 层与 print 层共用。

    参数:
        mode: "incomingMessage" 或 "selfMessage"
        content: incomingMessage 时为 Telegram Message 对象，selfMessage 时为字符串
    """
    match mode:
        case "incomingMessage":
            sender = _getSenderName(content)
            text = content.text or ""
        case "selfMessage":
            sender = "ZincNya~"
            text = content

    timestamp = datetime.now().strftime("%H:%M:%S")
    return timestamp, sender, text


def printMessage(mode, content):
    """打印消息到屏幕（兼容现有调用点）。"""
    timestamp, sender, text = _formatMessage(mode, content)
    _printFormattedMessage(timestamp, sender, text)




async def buildHistoryLines(targetChatID) -> list[str]:
    """将历史聊天记录格式化为行列表，不打印。"""
    history = await loadHistory(targetChatID)
    lines: list[str] = []
    if history:
        lines.append("─────── 历史记录 ───────")
        lines.append("")
        for item_type, item_data in iterMessagesWithDateMarkers(history):
            if item_type == "date":
                lines.append(f"[{item_data}]")
            else:
                msg = item_data
                ts = msg["timestamp"].strftime("%H:%M:%S") if msg["timestamp"] else "??:??:??"
                sender = msg["sender"] or "Unknown"
                content = msg["content"]
                lines.extend(_formatMessageLines(ts, sender, content))
        lines.append(f"<以上 {len(history)} 条>")
        lines.append("")
        lines.append("─────── 实时聊天 ───────")
        lines.append("")
    return lines


async def displayHistory(targetChatID):
    """显示历史聊天记录（兼容现有调用点）。"""
    for line in await buildHistoryLines(targetChatID):
        print(line)




async def chatScreen(app , bot: Bot , targetChatID: str):
    """
    进入与指定 chatID 的用户/群聊的本地交互聊天界面。

    业务编排入口，负责：
        - interactiveMode 管理
        - 目标聊天选择
        - 后台 receiverLoop 启动与清理
        - 将 UI 交互委托给 ChatScreenApp
    """
    from utils.chatUI import ChatScreenApp

    state = getStateManager()

    # 设置交互模式，暂停外层 CLI 的输入读取
    state.setInteractiveMode(True)
    queue: asyncio.Queue = state.getMessageQueue()

    # 如果用户仅输入 -c（未指定 ID），弹出白名单列表供选择
    if targetChatID == "NoValue":
        targetChatID = await chatIDList(app , bot)

    if not targetChatID:
        state.setInteractiveMode(False)
        return

    # chatIDList 会把 interactiveMode 设为 False，需要重新设为 True
    state.setInteractiveMode(True)

    shutdownEvent = state.getShutdownEvent()

    # ── 初始化 UI ──
    # 先构建初始内容（历史记录 + 欢迎信息）
    initialLines = await buildHistoryLines(targetChatID)
    initialLines.extend([
        "",
        f"已进入聊天界面喵",
        f"与 {targetChatID} 的实时聊天已连接",
        "=" * 64,
    ])
    ui = ChatScreenApp(targetChatID, initialLines=initialLines)

    # 注册控制台输出回调，让 logger 输出路由到 UI transcript
    def _consoleOutputHandler(text: str):
        lines = text.rstrip('\n').split('\n')
        ui.appendLines(lines)

    state.setConsoleOutputCallback(_consoleOutputHandler)


    # ========================================================================
    async def receiverLoop():
        """后台协程：持续监听对方发来的消息并展示到 UI"""
        nonTargetMessages = []

        try:
            while state.isInteractive():
                try:
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        # 检测外部关机信号，强制退出 UI
                        if shutdownEvent.is_set():
                            ui.requestExit()
                            break
                        continue

                    if not msg:
                        continue

                    if str(msg.chat.id) != str(targetChatID):
                        nonTargetMessages.append(msg)
                        continue

                    sender = _getSenderName(msg)
                    content = msg.text or ""
                    await saveMessage(targetChatID, "incoming", sender, content)

                    ts, sndr, txt = _formatMessage("incomingMessage", msg)
                    ui.appendIncomingMessage(ts, sndr, txt)

                except asyncio.CancelledError:
                    break
                except Exception:
                    pass
        finally:
            for msg in nonTargetMessages:
                await queue.put(msg)
    # ========================================================================


    receiverTask = asyncio.create_task(receiverLoop())
    await asyncio.sleep(0.1)


    # ── 主循环：UI 事件驱动 ──
    try:
        while not shutdownEvent.is_set():
            # 运行 UI，等待用户提交
            userInput = await ui.run()

            # 用户 Ctrl+C 退出或外部请求退出
            if userInput is None:
                break

            # 清空 composer 以准备下一轮输入
            ui.clearComposer()

            stripped = userInput.strip()

            # LLM 审核队列查看命令（兼容模式：临时退出 UI）
            if stripped == ":review":
                from utils.command.llm import handleConsoleReview
                # 临时退出全屏 UI
                ui.showStatus("进入审核模式...")
                await handleConsoleReview(bot)
                ui.showStatus(
                    "Enter 换行 | Alt+Enter / Ctrl+S 发送 | Esc 退出 | Ctrl+X 清空"
                    f" | 聊天对象: {targetChatID}"
                )
                continue

            # 空消息不发送
            if not stripped:
                continue

            textToSend = userInput.rstrip('\n')

            try:
                ts, sndr, txt = _formatMessage("selfMessage", textToSend)
                ui.appendSelfMessage(ts, sndr, txt)
                await bot.send_message(chat_id=targetChatID , text=textToSend)
                await saveMessage(targetChatID, "outgoing", "ZincNya~", textToSend)
            except Forbidden:
                ui.showStatus("被 Forbidden 了……对方可能还没跟咱开始聊天")
            except Exception as e:
                ui.showStatus(f"发送失败: {e}")

    finally:
        # 注销控制台输出回调
        state.setConsoleOutputCallback(None)
        state.setInteractiveMode(False)
        receiverTask.cancel()
        try:
            await receiverTask
        except asyncio.CancelledError:
            pass

        if not shutdownEvent.is_set():
            print("退出聊天界面喵——\n\n")




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
