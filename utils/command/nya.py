from handlers.cli import parseArgsTokens
from utils.nyaQuoteManager import getRandomQuote , quoteMenuController




async def execute(app , args: list[str]):

    parsed = {
        "edit": None,
    }

    # Nya 中将缩写映射到全称的字典
    argAlias = {
        "e": "edit"
    }

    parsed = parseArgsTokens(parsed , args , argAlias)
    
    editMatch = parsed["edit"]
    
    if editMatch == "NoValue":
        await quoteMenuController(app)
        return

    # 当不存在参数 -e/--edit 时，从语录里选一句输出
    selectedQuote = getRandomQuote()
    if selectedQuote is None:
        print("\n…………",
              "\n…………",
              "\n呜喵……？说不出话来……\n")
        return
    print(selectedQuote , "\n")




def getHelp():
    return {

        "name": "/nya",
    
        "description": "模拟 Telegram 端的 /nya 操作与编辑 ZincNyaQuotes",
    
        "usage": (
            "/nya (-e/--edit)"
        ),

        "example": (
            "让锌猫喵一句：/nya\n"
            "进入与编辑界面：/nya --edit"
        ),

    }