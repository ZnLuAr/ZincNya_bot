import os
import re
import sys
import json
import time
import random
import keyboard
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from config import QUOTES_DIR
from utils.logger import logAction




class JsonOperation:

    # 读取 json 文件
    def loadQuotesFromJson():
        if not os.path.exists(QUOTES_DIR):
            return []
        try:
            with open(QUOTES_DIR , "r" , encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
        
    
    def saveQuotesToJson(data):
        try:
            with open(QUOTES_DIR , "w" , encoding="utf-8") as f:
                json.dump(data , f , ensure_ascii=False , indent=2)
            return True
        except:
            return False
        



class WeitghtedRandom:
    def cdfCalc(quotes):
        if not quotes:
            return None
        weights = [max(1 , int(q.get("weight" , 1))) for q in quotes]
        total = sum(weights)
        r = random.randint(1 , total)
        c = 0
        for q , w in zip(quotes , weights):
            c += w
            if r <= c:
                return q
            return quotes[-1]
        

        

class ZincNyaQuotesUIManager:
    def editQuoteUI(quoteObject):
        os.system("cls" if os.name == "nt" else "clear")
        console = Console()

        bottomBar = Panel("————编辑中喵————\n" , "^S Save    ^X Exit")

        buf = quoteObject["text"].replace("\\n" , "\n")

        console.print(buf)
        console.print(bottomBar)
        
        lines = buf.split("\n")

        while True:
            key = keyboard.read_key()
        
            match key:
                case "ctrl+s":
                    final = "\n".join(lines)
                    quoteObject["text"] = final.replace("\\n" , "\n")
                    return True
                case "ctrl+q":
                    return False
                case "enter":
                    lines.append("")
                    os.system("cls" if os.name == "nt" else "clear")
                    console.print(bottomBar)
                    console.print("\n".join(lines))
                case "backspace":
                    if lines[-1]:
                        lines[-1] = lines[-1][0:-1]
                    else:
                        if len(lines > 1):
                            lines.pop()
                    os.system("cls" if os.name == "nt" else "clear")
                    console.print(bottomBar)
                    console.print("\n".join(lines))
                case _:
                    if len(key) == 1:
                        lines[-1] += key
                        os.system("cls" if os.name == "nt" else "clear")
                        console.print(bottomBar)
                        console.print("\n".join(lines))
                    else:
                        continue



    def listQuoteUI(quotes):
        console = Console()
        selected = 0

        for q in quotes:
            q.setdefault("collapsed" , True)

        def renderer():
            os.system("cls" if os.name == "nt" else "clear")

            table = Table(title="ZincNya Quotes")
            table.add_column("No.")
            table.add_column("Weight")
            table.add_column("Quote")

            for i , q in enumerate(quotes):
                text = q["text"]
                if q["collapsed"]:
                    if len(text) > 20:
                        text = text[:20] + ">"
                text = text.replace("\\n" , "\n")

            if i == selected:
                table.add_row(
                    f"[bold yellow]{i+1}[/]",
                    f"[bold yellow]{text}[/]",
                    f"[bold yellow]{q['weight']}[/]"
                    )
            else:
                table.add_row(str(i+1) , text , str(q["weight"]))

            console.print(table , "\n")
        
        renderer()

        while True:
            if  keyboard.is_pressed("down"):
                selected = min(selected + 1 , len(quotes) - 1)
                renderer()
                time.sleep(0.05)
            elif keyboard.is_pressed("up"):
                selected = max(selected + 1 , len(quotes) - 1)
                renderer()
                time.sleep(0.05)
            elif keyboard.is_pressed("enter"):
                quotes[selected]["collapsed"] = not quotes[selected]["conllapsed"]
                renderer()
                time.sleep(0.05)
            elif keyboard.is_pressed("delete"):
                os.system("cls" if os.name == "nt" else "clear")
                console.print(Panel(f"咱的第 {selected+1} 条语录……ご主人真的要删除吗喵……？ (（)y/n)"))
                while True:
                    k = keyboard.read_key()
                    if k == "y":
                        quotes.pop(selected)
                        return quotes
                    elif k == "n":
                        renderer()
                        break
                time.sleep(0.1)
            elif keyboard.is_pressed("ctrl+a"):
                newQuote = {"text": "" , "weight": 1}
                ok = ZincNyaQuotesUIManager.editQuoteUI(newQuote)
                if ok:
                    quotes.append(newQuote)
                renderer()
                time.sleep(0.05)
            elif keyboard.is_pressed("ctrl+x"):
                return quotes




# 为响应来自 ../cli.py 与 help.py 的常规函数
async def execute(app , args):

    editMatch = any(arg in ("-e" , "--edit") for arg in args)

    quotes = JsonOperation.loadQuotesFromJson()

    if editMatch:
        newList = ZincNyaQuotesUIManager.listQuoteUI(quotes)
        JsonOperation.saveQuotesToJson(newList)
        await logAction("Console" , "/nya -e" , "语录已更新喵——" , "withOneChild")
        return
        
    if not quotes:
        print("呜喵……？说不出话来……\nご主人様——快来修修你的群猫……")
        return
        
    text = WeitghtedRandom.cdfCalc(quotes)
    text = text.replace("\\n" , "\n")

    print(text , "\n")


def getHelp():
    return {

        "name": "/nya",
    
        "description": "对锌喵语录进行操作",
    
        "usage": (
            "/send (-e/--edit)\n"
        ),

        "example": (
            "让锌喵喵一句：/nya\n"
            "对锌喵的语录进行编辑：/nya --edit\n"
        ),

    }