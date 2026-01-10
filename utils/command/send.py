"""
utils/command/send.py

用于实现 /send 命令的逻辑模块，负责从控制台发送消息，或进入与某个用户的实时本地聊天界面。

本模块大致可分为四个部分：
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
    - 设置 app.bot_data["state"]["interactiveMode"] 为 "SendChatScreenMode"，暂停外层 CLI
        （"interactiveMode" 一般情况下为 False）
    - displayHistory() 进入时显示历史聊天记录
    - receiverLoop() 后台读取 messageQueue 中的消息，并筛选属于当前 chatID 的部分打印到屏幕
    - inputLoop() 使用 aioconsole.ainput() 等待用户输入，并自动清理输入行的回显
    - printMessage() 用于统一格式化输出聊天内容

    正常情况下，用户输入 ":q" 退出函数

退出时的清理工作（finally 块）：
    1. 切回主屏幕缓冲区（rmcup）
    2. 恢复 interactiveMode 为 False
    3. 取消 receiverTask 协程
    4. 重置终端状态（resetTerminal）


================================================================================

本模块不直接与 whitelist.json 交互，
仅通过 whitelistManager 提供的 whitelistMenuController() 来选择聊天对象。

聊天记录通过 chatHistory 模块进行加密存储和读取。

"""




import sys
import asyncio
import os
from telegram import Bot
from datetime import datetime
from aioconsole import ainput
from telegram.error import Forbidden


from handlers.cli import parseArgsTokens
from utils.logger import logAction
from utils.whitelistManager import whitelistMenuController
from utils.terminalUI import cls, smcup, rmcup, resetTerminal
from utils.chatHistory import saveMessage, loadHistory




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
            await logAction("Console" , f"/send → {chatID} ┃ {text}" , result , "withOneChild")




async def chatScreen(app , bot: Bot , targetChatID: str):
    # 进入与指定 chatID 的用户/群聊的本地交互聊天界面

    # 设置交互模式，暂停外层 CLI 的输入读取
    app.bot_data["state"]["interactiveMode"] = "SendChatScreenMode"
    queue: asyncio.Queue = app.bot_data["state"]["messageQueue"]

    # 如果用户仅输入 -c（未指定 ID），弹出白名单列表供选择
    if targetChatID == "NoValue":
        targetChatID = await chatIDList(app , bot)

    if not targetChatID:
        # 恢复外层 CLI 的命令读取
        app.bot_data["state"]["interactiveMode"] = False
        return

    # 切换到备用屏幕缓冲区
    smcup()
    sys.stdout.flush()


    # ========================================================================
    def displayHistory():
        """显示历史聊天记录"""
        history = loadHistory(targetChatID, limit=30)
        if history:
            print(f"─────── 历史记录 ───────\n")
            for msg in history:
                ts = msg["timestamp"].strftime("%H:%M:%S") if msg["timestamp"] else "??:??:??"
                sender = msg["sender"] or "Unknown"
                content = msg["content"]
                if msg["direction"] == "outgoing":
                    print(f"[{ts}] <{sender}> {content}")
                else:
                    print(f"[{ts}] <{sender}> {content}")
            print(f"<以上 {len(history)} 条>")
            print("\n─────── 实时聊天 ───────\n")


    async def receiverLoop():
    # 后台协程：持续监听对方发来的消息并展示，形成一个类聊天窗口的界面

        while app.bot_data["state"]["interactiveMode"] == "SendChatScreenMode":

            try:
                msg = await queue.get()
                if not msg:
                    continue

                if str(msg.chat.id) != str(targetChatID):
                    continue

                # 保存消息到加密存储
                sender = msg.from_user.username or msg.from_user.first_name or "Unknown"
                content = msg.text or ""
                saveMessage(targetChatID, "incoming", sender, content)

                printMessage("incomingMessage" , msg)

            except asyncio.CancelledError:
                break

            except Exception:
                # 忽略零星的单次读取异常，继续循环
                pass


    async def inputLoop():
        while True:
            userInput = await ainput(">> ")

            # 清除用户的原始输入，即不显示命令行的回显">> text"
            sys.stdout.write("\033[F")      # 光标上移一行
            sys.stdout.write("\033[K")      # 清除整行
            sys.stdout.flush()

            return userInput


    def printMessage(mode , msg):
        match mode:
            case "incomingMessage":
                sender = msg.from_user.username or msg.from_user.first_name
                text = msg.text or ""
            case "selfMessage":
                sender = "ZincNya~"
                text = msg

        timestamp = datetime.now().strftime("%H:%M:%S")
        sys.stdout.write(f"\r[{timestamp}] <{sender}> {text}\n")
        sys.stdout.write("" if sender == "ZincNya~" else ">> ")
        sys.stdout.flush()
    # ========================================================================


    receiverTask = asyncio.create_task(receiverLoop())

    # 主循环，从控制台读取输入并发送消息
    try:
        cls()  # 清屏
        print(
            "已进入聊天界面喵",
            f"\n与 {targetChatID} 的实时聊天已连接",
            "\n发送文字即可直接发出；输入 ':q' 退出\n",
            "=" * 64,
            "\n"
        )

        # 显示历史记录
        displayHistory()

        while True:
            userInput = await inputLoop()

            if userInput is None:
                continue

            if userInput.strip() == ":q":
                break

            try:
                printMessage("selfMessage" , userInput)
                await bot.send_message(chat_id=targetChatID , text=userInput)
                # 保存发送的消息到加密存储
                saveMessage(targetChatID, "outgoing", "ZincNya~", userInput)
            except Forbidden:
                print(f"    被 Forbidden 了……这可能是对方还没有跟咱开始聊天的缘故哦")
            except Exception as e:
                print(f"    呜喵……发送失败了喵…… | {e}\n")

    finally:
        # 切回主屏幕缓冲区
        rmcup()
        sys.stdout.flush()

        app.bot_data["state"]["interactiveMode"] = False
        receiverTask.cancel()
        try:
            await receiverTask
        except Exception:
            pass

        # 重置终端状态
        resetTerminal()

        print("退出聊天界面喵——\n")




def getHelp():
    return {

        "name": "/send",
    
        "description": "在控制台中向指定对象发送一条消息",
    
        "usage": (
            "/send [-c/--chat (chatID)] [-a/--at <userName>] [-id/--id <id1 , id2 ,...>] [-t/--text <text>]\n"
            "用户或群聊的ID需要在 Telegram 的 @myidbot 中获取哦。"
        ),

        "example": (
            "向一个用户发送消息：/send -id '1234567' -t 'Hello world'\n"
            "向聊天中发送消息并@用户：/send -id '-1234567' -a 'userName' -t 'Do you know I'm a bot?'\n"
            "向多个用户发送消息：/send -id '1234567' '1234568' -t '👀'"
            "进入与指定用户的聊天界面：/send -c '1234567'\n"
            "进入与用户的聊天界面，但弹出列表以供选择：/send -c"
        ),

    }