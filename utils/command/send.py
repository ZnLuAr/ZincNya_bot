import asyncio
from telegram import Bot
from datetime import datetime
from aioconsole import ainput
from telegram.ext import ApplicationBuilder , MessageHandler , filters
from telegram.error import Forbidden , BadRequest

from handlers.cli import parseArgsTokens
from utils.logger import logAction
from utils.whitelistManager import (
    loadWhitelistFile,
    whitelistUIRenderer,
    collectWhitelistViewModel,
)




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

    entries = await collectWhitelistViewModel(bot)
    whitelistUIRenderer(entries)

    print("输入编号来选择目标喵……或者直接输入ID也行——\n")

    while True:
        userInput = await asyncio.to_thread(input , ">> ")

        i = userInput.strip()
        if not i:
            print("退出喵——\n")
            return None
    
        # 若输入数字编号
        if i.isdigit():
            idx = int(i)
            if 1 <= idx <= len(entries):
                return str(entries[idx-1]["uid"])
            else:
                print("呜喵……？没有这个编号哦……\n")
                continue

        if i.lstrip("-").isdigit():
            return str(i)
        
        print("ムリー！这样输入是不行的喵……再试试吧——\n")




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

        timestamp = await getTimestamp()
        while app.bot_data["state"]["interactiveMode"] == "SendChatScreenMode":

            try:
                msg = await queue.get()

                if not msg:
                    continue

                if str(msg.chat.id) != str(targetChatID):
                    continue

                if msg.from_user and not msg.from_user.is_bot:
                    print(
                        f"\n{timestamp}\n",
                        f" {msg.from_user.username} >> {msg.text}",
                        "\n"
                    )
            
            except asyncio.CancelledError:
                break

            except Exception:
                # 忽略零星的单次读取异常，继续循环
                pass

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
            userInput = await ainput()

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
                await bot.send_message(chat_id=targetChatID , text=userInput)
            except Forbidden:
                print(f"被 Forbidden 了……这可能是对方还没有跟咱开始聊天的缘故哦")
            except Exception as e:
                print(f"呜喵……发送失败了喵…… | {e}\n")

    finally:
        app.bot_data["state"]["interactiveMode"] = False
        receiverTask.cancel()
        try:
            await receiverTask
        except Exception:
            pass




async def getTimestamp():
    timestamp = datetime.now().strftime("%H:%M:%S")
    return timestamp




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