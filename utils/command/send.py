import re
import asyncio
from telegram import Bot
from telegram.error import Forbidden , BadRequest

from handlers.cli import parseArgsTokens
from utils.logger import logAction
from utils.whitelistManager import loadWhitelistFile , whitelistUIRenderer , checkChatAvailable , collectWhitelistViewModel




async def execute(app , args):

    bot: Bot = app.bot

    # 开始参数解析——
    parsed = {
        "at": None,
        "text": None,
        "id": [],
        "chat": None
    }
    
    parsed: dict = parseArgsTokens(parsed , args)

    atUser = parsed["at"]
    text = parsed["text"]
    idList = parsed["id"]
    screenChatID = parsed["chat"]

    # 参数验证
    if screenChatID is not None:
        await chatScreen(bot , screenChatID)
        return

    # 发送单条信息
    if not text or text == "NoValue":
        print("❌ もー、参数 [-t/--text <text>] 要加上才可以发得出文字啦——！\n")
        return
    
    if not idList or idList == "NoValue":
        print("❌ もー、参数 [-id/--id <chatID>] 得加上才对啦——！\n")
        return
    
    await sendMsg(bot , idList , atUser , text)




# 执行发送信息给<chatID>
async def sendMsg(bot , idList , atUser , text):
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




async def chatScreen(bot: Bot , screenChatID: str):
    if screenChatID == "NoValue":
        screenChatID = await chatIDList(bot)

    if not screenChatID:
        return
    
    print(
        "\n已成功进入——\n",
        f"\n与 {screenChatID} 的聊天界面喵",
        "输入文字就可以直接发送啦；键入“:q”退出聊天界面喵\n",
        "=" * 64
    )


    # 后台轮询监听消息（先定义，以便后面直接调用）
    async def chatListener(bot: Bot , chatID: str , stopFlag: asyncio.Event):
        lastMessageID = None

        while not stopFlag.is_set():
            try:
                updates = await bot.get_updates()

                for u in updates:
                    msg = u.message
                    if not msg:
                        continue
                    if str(msg.chat.id) != str(chatID):
                        continue

                    if lastMessageID is None or msg.message_id > lastMessageID:
                        lastMessageID = msg.message_id
                        if msg.from_user and not msg.from_user.is_bot:
                            print(f"[{msg.from_user.first_name}]：{msg.text}")

            except Exception:
                pass

            # 每 1 秒检查一次最新消息
            await asyncio.sleep(1)


    # 启动后台监听协程
    stopFlag = asyncio.Event()
    listenTask = asyncio.create_task(chatListener(bot , screenChatID , stopFlag))

    try:
        while True:
            userInput = await asyncio.to_thread(input)

            if userInput.strip() == ":q":
                print (
                    "=" * 64,
                    "\n退出聊天界面喵——\n"
                )
                stopFlag.set()
                break

            try:
                await bot.send_message(chat_id=screenChatID , text=userInput)
                print(f"锌酱：{userInput}")
            except Forbidden:
                print(f"被 Forbidden 了……这可能是对方还没有跟咱开始聊天的缘故哦")
            except Exception as e:
                print(f"呜喵……发送失败了喵…… | {e}")
        
    finally:
        stopFlag.set()
        await listenTask




async def chatIDList(bot):
    whitelist = loadWhitelistFile()
    print("\n存下来的ChatID喵——")

    available = []
    whitelistData = await collectWhitelistViewModel(bot)
    whitelistUIRenderer(whitelistData)
    
    for i , (chatID , name) in enumerate(whitelist.items() , 1):
        status = await checkChatAvailable(bot , chatID)
        available.append((chatID , status))
    
    print ("\nご主人请在这里输入序号喵——")
    print("或者直接按回车取消喵\n" , end= "")
    selection = input().strip()

    # 用户取消选择
    if not selection:
        print("^C喵——\n")
        return None
    
    if selection.isdigit():
        num = int(selection)

        if 1 <= num <= len(available):
            chatID , status = available[num - 1]
            if not status is True:
                print(
                    f"{chatID} 喵……？\n",
                    f"因为 {status}，{chatID}好像还不能够接收到来自咱的消息\n",
                    "但咱还是可以尝试进入聊天界面的——\n"  
                )
            return chatID
        
        print("もー、这样选是不行的哦——\n再选一次吧……")
        return None
    
    # 兼容用户直接输入 chatID 的情形
    if selection in whitelist:
        status = await checkChatAvailable(bot , selection)
        if status is not True:
            print(
                f"说是 {status} 喵？……"
                f"总之，{selection} 好像还不能够接收到来自咱的消息\n",
                "但咱还是可以尝试进入聊天界面的——\n"    
            )
        return selection




def getHelp():
    return {

        "name": "/send",
    
        "description": "向一个或多个会话发送文本消息喵",
    
        "usage": (
            "/send [-c/--chat (userID)] [-a/--at <userName>] [-id/--id <id1 , id2 ,...>] [-t/--text <text>]\n"
            "用户或群聊的ID需要在 Telegram 的 @myidbot 中获取哦。\n"
        ),

        "example": (
            "向一个用户发送消息：/send -id '1234567' -t 'Hello world'\n"
            "向聊天中发送消息并@用户：/send -id '-1234567' -a 'userName' -t 'Do you know I'm a bot?'\n"
            "向多个用户发送消息：/send -id '1234567' '1234568' -t '👀'\n"
            "进入与指定用户的聊天界面：/send -c '1234567'\n"
            "进入与用户的聊天界面，但弹出列表以供选择：/send -c"
        ),

    }