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


================================================================================
普通消息发送逻辑
包含 sendMsg() 一个功能，处理在 cli 输入的指令

sendMsg(bot, idList, atUser, text)
    - 遍历 idList，将文字逐一发送给对应 ChatID
        - idList 为在 cli 输入的 "-id" 后的一个或若干个 ChatID
    - 当指定 -a/--at <userName> 时，会自动在消息开头加上 "@userName"

================================================================================
本地交互式聊天界面（核心功能）
包含 chatScreen() 及其内部的 receiverLoop() / inputLoop() / printMessage()

chatScreen(app , bot: Bot , targetChatID: str) 
    用于进入与某一 chatID 的实时聊天界面。

其操作模式为 CLI 聊天窗口：
    · 上方自动展示来自对方的消息（telegram -> bot -> 全局 messageQueue）
    · 底部由用户以 ">> " 输入并发送消息
    · 输入 ":q" 退出界面

内部结构：
    - 设置 app.bot_data["state"]["interactiveMode"] 为 "SendChatScreen"，暂停外层 CLI
        （"interactiveMode" 一般情况下为 False）
    - receiverLoop() 后台读取 messageQueue 中的消息，并筛选属于当前 chatID 的部分打印到屏幕
    - inputLoop() 等待用户输入并自动清理输入行的回显，使界面更简洁
    - printMessage() 用于统一格式化输出聊天内容

    正常情况下，用户输入 ":q" 退出函数

最终会恢复 interactiveMode 并关闭内部协程。


================================================================================

本模块不直接与 whitelist.json 交互，
仅通过 whitelistManager 提供的 collectWhitelistViewModel() 与 whitelistUIRenderer()
来显示 UID 列表（用于选择聊天对象）。

"""




import sys
import asyncio
from telegram import Bot
from datetime import datetime
from aioconsole import ainput
from telegram.error import Forbidden , BadRequest


from handlers.cli import parseArgsTokens
from utils.logger import logAction
from utils.whitelistManager import whitelistMenuController




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
    print("\n存下来的 UID 列表喵——\n")

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


    # ========================================================================
    async def receiverLoop(bot: Bot , chatID: str):
    # 后台协程：持续监听对方发来的消息并展示，形成一个类聊天窗口的界面
    # （其实就是不断从全局队列中读取消息，
    # 若消息属于当前聊天对象，则打印出来

        while app.bot_data["state"]["interactiveMode"] == "SendChatScreenMode":

            try:
                msg = await queue.get()
                if not msg:
                    continue

                if str(msg.chat.id) != str(targetChatID):
                    continue

                printMessage("incomingMessage" , msg)
            
            except asyncio.CancelledError:
                break

            except Exception:
                # 忽略零星的单次读取异常，继续循环
                pass    


    async def inputLoop(queue):
        while True:
            userInput = await ainput(">> ")

            # 清除用户的原始输入，即不显示命令行的回显“>> text”
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


    receiverTask = asyncio.create_task(receiverLoop(bot , targetChatID))

    # 主循环，从控制台读取输入并发送消息
    try:

        print(
            "\n已进入聊天界面喵",
            f"\n与 {targetChatID} 的实时聊天已连接",
            "\n发送文字即可直接发出；输入 ':q' 退出\n",
            "=" * 64,
            "\n\n"
        )

        while True:
            userInput = await inputLoop(queue)

            if userInput is None:
                continue

            if userInput.strip() == ":q":
                print(
                    "\n\n",
                    "=" * 64,
                    "\n退出聊天界面喵——\n\n"
                )
                break

            try:
                printMessage("selfMessage" , userInput)
                await bot.send_message(chat_id=targetChatID , text=userInput)
            except Forbidden:
                print(f"    被 Forbidden 了……这可能是对方还没有跟咱开始聊天的缘故哦")
            except Exception as e:
                print(f"    呜喵……发送失败了喵…… | {e}\n")

    finally:
        app.bot_data["state"]["interactiveMode"] = False
        receiverTask.cancel()
        try:
            await receiverTask
        except Exception:
            pass




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