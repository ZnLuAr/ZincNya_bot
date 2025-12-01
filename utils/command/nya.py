import random
from typing import Optional

from utils.nyaQuoteManager import (
    loadQuoteFile,
    userOperation,
    collectQuoteViewModel,
    quoteUIRenderer,
    editQuoteViaEditor
)




def getRandomQuote() -> Optional[str]:

    quotes = loadQuoteFile()
    if not quotes:
        return None
    weights = [float(q.get("weight" , 1.0)) for q in quotes]
    picked = random.choice(quotes , weights=weights , k=1)[0]

    # 存储中用的是 "\n" 作为换行标记，返回给 telegram 时转换为真实换行
    return picked.get("text", "").replace("\\n", "\n")




async def execute(app , args: list[str]):
    
    if any(a in ("-e" , "--edit") for a in args):
        return  # 这里亟待添加代码
    
    selectedQuote = getRandomQuote()
    if selectedQuote is None:
        print("………………\n呜喵……？说不出话来……\n")
        return
    return selectedQuote




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