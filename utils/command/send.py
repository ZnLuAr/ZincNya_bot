import re
from telegram import Bot
from utils.logger import logAction




async def execute(app , args):
    '''
    /send [-a "@user"] [-id "<id_1>,<id_2>,...,<id_n>"] [-t "text"]
    '''

    bot: Bot = app.bot
    joined = " ".join(args)

    # 开始参数解析——
    atUser = None
    text = None
    idList = []

    # 用正则提取参数（支持 -a 或 --at 等）
    atMatch = re.search(r'--?(?:a|at)\s+(?:"([^"]+)"|(\S+))', joined)
    textMatch = re.search(r'--?(?:t|text)\s+(?:"([^"]+)"|(.+))', joined)
    idMatch = re.search(r'--?id\s+(?:"([^"]+)"|(\S+))', joined)

    def _exetract(match):
        if not match:
            return None
        return match.group(1) or match.group(2)
    
    atUser = _exetract(atMatch)
    text = _exetract(textMatch)
    idRaw = _exetract(idMatch)
    idList = [s.strip() for s in idRaw.split(",")] if idRaw else []

    # 参数验证
    if not text:
        print("❌ もー、参数 [-t/--text <text>] 要加上才可以发得出文字啦——！\n")
        return
    
    if not idList:
        print("❌ もー、参数 [-id/--id <chatID>] 得加上才对啦——！\n")
        return
    

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




def getHelp():
    return {

        "name": "/send",
    
        "description": "向一个或多个会话发送文本消息喵",
    
        "usage": (
            "/send [-a/--at <userName>] [-id/--id <id1 , id2 ,...>] [-t/--text <text>]\n"
            "用户或群聊的ID需要在 Telegram 的 @myidbot 中获取哦。"
        ),

        "example": (
            "向一个用户发送消息：/send -id '1234567' -t 'Hello world'\n"
            "向聊天中发送消息并@用户：/send -id '-1234567' -a 'userName' -t 'Do you know I'm a bot?'\n"
            "向多个用户发送消息：/send -id '1234567' '1234568' -t '👀'"
        ),

    }