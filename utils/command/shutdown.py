from utils.core.logger import logAction, LogLevel, LogChildType

async def execute(app, args):
    await logAction(
        "System",
        "/shutdown",
        "收到关机指令",
        LogLevel.INFO,
        LogChildType.NONE
    )
    print("\n     · 现在、就关机了喵——\n     · 期待和你的下一次见面——\n")
    print(".\n.\n.\n    *……瞼を閉じました……= =\n")
    return "SHUTDOWN"




def getHelp():
    return {

    "name": "/shutdown",
    
    "description": "正确且安全地关闭客户端",
    
    "usage": "直接输入 /shutdown 就好了哦",

    "example": "👀"

    }
