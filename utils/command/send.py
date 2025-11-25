import asyncio
from telegram import Bot
from telegram.ext import ApplicationBuilder
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

    # Send 中将缩写对应全称的字典
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




async def chatScreen(app , bot: Bot , screenChatID: str):
    # 进入与指定 chatID 的用户/群聊的本地交互聊天界面

    # 暂停外层 CLI 的输入读取
    app.bot_data["state"]["interactiveMode"] = True

    # 如果用户仅输入 -c（未指定 ID），弹出白名单列表供选择
    if screenChatID == "NoValue":
        screenChatID = await chatIDList(bot)

    if not screenChatID:
        return
    
    print(
        "\n已进入聊天界面\n",
        f"与 {screenChatID} 的实时聊天已连接",
        "发送文字即可直接发出；输入 ':q' 退出\n",
        "=" * 60,
    )


    async def chatListener(bot: Bot , chatID: str , stopFlag: asyncio.Event):
    # 后台协程：持续监听对方发来的消息并展示，形成一个类聊天窗口的界面

        lastMsgID = None

        while not stopFlag.is_set():
            try:
                updates = await bot.get_updates()

                for u in updates:
                    msg = u.message
                    if not msg:
                        continue
                    if str(msg.chat.id) != str(chatID):
                        continue

                    if lastMsgID is None or msg.message_id > lastMsgID:
                        lastMsgID = msg.message_id
                        if msg.from_user and not msg.from_user.is_bot:
                            print(f"[{msg.from_user.first_name}]: {msg.text}")
        
            except Exception:
                pass

            await asyncio.sleep(0.5)

    # 启动控制台监听
    stopFlag = asyncio.Event()
    listenerTask = asyncio.create_task(chatListener(bot , screenChatID , stopFlag))


    # 主循环，从控制台读取输入并发送消息
    try:
        while True:
            userInput = await asyncio.to_thread(input)

            if userInput.strip() == ":q":
                print("=" * 60, "\n退出聊天界面喵\n")
                stopFlag.set()
                break

        try:
            await bot.send_message(chat_id=screenChatID , text=userInput)
            print(f"> {userInput}")
        except Forbidden:
            print(f"被 Forbidden 了……这可能是对方还没有跟咱开始聊天的缘故哦")
        except Exception as e:
            print(f"呜喵……发送失败了喵…… | {e}")

    finally:
        stopFlag.set()
        
        # 恢复外层 CLI 的命令读取
        app.bot_data["state"]["interactiveMode"] = False

        await listenerTask




async def chatIDList(bot):
    whitelist = loadWhitelistFile()
    print("\n存下来的 UID 列表喵——\n")

    entries = await collectWhitelistViewModel(bot)
    whitelistUIRenderer(entries)



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