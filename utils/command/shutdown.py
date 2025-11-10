from utils.logger import logAction

async def execute(app, args):
    await logAction("锌酱" , "/shutdown" , "收到关机指令喵——" , "False")
    print("\n     · 锌酱、要去睡觉了喵——\n     · 期待和ごしゅじん的下一次见面——\n     · 嘿嘿……❤ 喵——\n")
    print(".\n.\n.\n    *……瞼を閉じました……= =")
    return "SHUTDOWN"




def getHelp():
    return {

    "name": "/shudown",
    
    "description": "使咱能够被安全、正确地关机喵",
    
    "usage": "直接输入 /shutdown 就好了哦",

    "example": "もー、ご主人様又在装傻了……"

    }