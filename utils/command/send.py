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
包含 chatScreen() 及其内部的 receiverLoop() / inputLoop() / printMessage() / displayHistory()

chatScreen(app , bot: Bot , targetChatID: str)
    用于进入与某一 chatID 的实时聊天界面。

其操作模式为 CLI 聊天窗口：
    · 使用 terminalUI 的备用屏幕缓冲区（smcup/rmcup），不污染主终端
    · 进入时显示历史聊天记录（从 chatHistory 模块加载）
    · 上方自动展示来自对方的消息（telegram -> bot -> 全局 messageQueue）
    · 底部由用户以 ">> " 输入并发送消息
    · 收发的消息会自动保存到加密数据库（chatHistory 模块）
    · 输入 ":q" 退出界面

内部结构：
    - 设置 app.bot_data["state"]["interactiveMode"] = True，暂停外层 CLI
    - displayHistory() 进入时显示历史聊天记录
    - receiverLoop() 后台协程，持续监听 messageQueue 中的消息
        · 使用 asyncio.wait_for() 带超时读取，避免阻塞
        · 筛选属于当前 chatID 的消息并打印到屏幕
        · 循环条件依赖 interactiveMode 标志
    - inputLoop() 使用 asyncInput() 等待用户输入，并自动清理输入行的回显
    - printMessage() 用于统一格式化输出聊天内容

    正常情况下，用户输入 ":q" 退出函数

退出时的清理工作（finally 块）：
    1. 切回主屏幕缓冲区（rmcup）
    2. 恢复 interactiveMode 为 False
    3. 取消 receiverTask 协程


================================================================================

本模块不直接与 whitelist.json 交互，
仅通过 whitelistManager 提供的 whitelistMenuController() 来选择聊天对象。

聊天记录通过 chatHistory 模块进行加密存储和读取。
异步输入通过 inputHelper 模块的 asyncInput() 实现（基于 run_in_executor）。

"""




import sys
import asyncio
from telegram import Bot
from datetime import datetime
from telegram.error import Forbidden

from handlers.cli import parseArgsTokens

from utils.terminalUI import cls, smcup, rmcup
from utils.inputHelper import asyncMultilineInput
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



def _printFormattedMessage(timestamp: str, sender: str, text: str):
    """按统一规则打印单条消息，支持多行内容。"""
    sender = sender or "Unknown"
    text = text or ""

    if '\n' in text:
        lines = text.split('\n')
        print(f"[{timestamp}] <{sender}> {lines[0]}")
        # "[HH:MM:SS] <sender> " 的可视前缀长度近似为 len(sender) + 13
        total = len(sender) + 13
        indent = '· ' * (total // 2) + '| '
        for line in lines[1:]:
            print(f"{indent}{line}")
    else:
        print(f"[{timestamp}] <{sender}> {text}")



def printMessage(mode, content):
    """
    打印消息到屏幕

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
    _printFormattedMessage(timestamp, sender, text)




async def displayHistory(targetChatID):
    """显示历史聊天记录"""
    history = await loadHistory(targetChatID)
    if history:
        print(f"─────── 历史记录 ───────\n")
        for item_type, item_data in iterMessagesWithDateMarkers(history):
            if item_type == "date":
                print(f"[{item_data}]")
            else:
                msg = item_data
                ts = msg["timestamp"].strftime("%H:%M:%S") if msg["timestamp"] else "??:??:??"
                sender = msg["sender"] or "Unknown"
                content = msg["content"]
                _printFormattedMessage(ts, sender, content)
        print(f"<以上 {len(history)} 条>")
        print("\n─────── 实时聊天 ───────\n")




async def chatScreen(app , bot: Bot , targetChatID: str):
    # 进入与指定 chatID 的用户/群聊的本地交互聊天界面
    state = getStateManager()

    # 设置交互模式，暂停外层 CLI 的输入读取
    state.setInteractiveMode(True)
    queue: asyncio.Queue = state.getMessageQueue()

    # 如果用户仅输入 -c（未指定 ID），弹出白名单列表供选择
    if targetChatID == "NoValue":
        targetChatID = await chatIDList(app , bot)

    if not targetChatID:
        # 恢复外层 CLI 的命令读取
        state.setInteractiveMode(False)
        return

    # chatIDList 会把 interactiveMode 设为 False，需要重新设为 True
    state.setInteractiveMode(True)

    # 切换到备用屏幕缓冲区
    smcup()
    sys.stdout.flush()


    # ========================================================================
    async def receiverLoop():
        """后台协程：持续监听对方发来的消息并展示"""

        nonTargetMessages = []  # 积攒非目标聊天的消息，退出时回填队列

        try:
            while state.isInteractive():
                try:
                    # 使用 wait_for 添加超时，避免永久阻塞
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue

                    if not msg:
                        continue

                    if str(msg.chat.id) != str(targetChatID):
                        nonTargetMessages.append(msg)
                        continue

                    # 保存消息到加密存储
                    sender = _getSenderName(msg)
                    content = msg.text or ""
                    await saveMessage(targetChatID, "incoming", sender, content)

                    printMessage("incomingMessage" , msg)

                except asyncio.CancelledError:
                    break

                except Exception:
                    # 忽略零星的单次读取异常，继续循环
                    pass
        finally:
            # 将非目标消息回填队列，防止永久丢失
            for msg in nonTargetMessages:
                await queue.put(msg)

    # ========================================================================

    receiverTask = asyncio.create_task(receiverLoop())

    # 等待 receiverLoop 启动
    await asyncio.sleep(0.1)
    print(f"[chatScreen] receiverTask 状态: {'运行中' if not receiverTask.done() else '已结束'}")

    # 主循环，从控制台读取输入并发送消息
    try:
        cls()  # 清屏
        print(
            "已进入聊天界面喵",
            f"\n与 {targetChatID} 的实时聊天已连接",
            "\n直接按 Enter 换行，Alt+Enter / Ctrl+S 发送；输入 ':q' 退出\n",
            "=" * 64,
            "\n"
        )

        # 显示历史记录
        await displayHistory(targetChatID)

        shutdownEvent = state.getShutdownEvent()

        while True:
            # 关机时退出，避免 prompt_async() 永久阻塞
            if shutdownEvent.is_set():
                break

            # 使用 asyncMultilineInput 读取用户输入
            # 支持多行输入，直接按 Enter 换行，Alt+Enter 提交
            # patch_stdout 会在日志输出或聊天窗口中其他人发言时时自动清除并重绘提示符
            userInput = await asyncMultilineInput(
                prompt=">> ",
                continuation_prompt=".. "
            )

            if userInput is None:
                continue

            # 去除首尾空白后检查退出命令（允许像 "  :q  " 这样的输入）
            stripped = userInput.strip()
            if stripped == ":q":
                break

            # LLM 审核队列查看命令
            if stripped == ":review":
                from utils.command.llm import handleConsoleReview
                await handleConsoleReview(bot)
                continue

            # 空消息不发送
            if not stripped:
                continue

            # 发送前去除尾部空行（只保留首部空白，去除尾部）
            textToSend = userInput.rstrip('\n')

            try:
                printMessage("selfMessage" , textToSend)
                await bot.send_message(chat_id=targetChatID , text=textToSend)
                # 保存发送的消息到加密存储
                await saveMessage(targetChatID, "outgoing", "ZincNya~", textToSend)
            except Forbidden:
                print(f"    被 Forbidden 了……这可能是对方还没有跟咱开始聊天的缘故哦")
            except Exception as e:
                print(f"    呜喵……发送失败了喵…… | {e}\n")

    finally:
        # 切回主屏幕缓冲区
        rmcup()
        sys.stdout.flush()

        state.setInteractiveMode(False)
        receiverTask.cancel()
        try:
            await receiverTask
        except asyncio.CancelledError:
            pass

        # 关机时不打印退出消息
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