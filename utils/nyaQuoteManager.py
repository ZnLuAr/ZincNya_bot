"""
utils/nyaQuoteManager.py

用于管理 /nya 的语录存储与在 CLI 中展示用的数据模型。
模块组织与 whitelistManager.py 类似，分为：
  - 文件读写（内部函数）
  - 外部操作入口（userOperation）
  - UI 相关：collectQuoteViewModel、quoteUIRenderer、quoteMenuController
  - 编辑器辅助：edit_quote_via_editor（将 \n <-> 实际换行互转）

设计要点：
  - 存储为列表，每项为 dict: {"text": "...", "weight": float}
  - 展示时按 weight 从小到大排序（A 模式）
  - 编辑时在临时文件中将存储的 "\n" 展开为换行；保存时再转回 "\n"
"""




import os
import sys
import json
import random
import tempfile
from typing import List , Tuple , Optional

from rich.table import Table
from rich.console import Console

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.widgets import TextArea

from config import QUOTES_DIR
from utils.fileEditor import editFile




def ensureQuoteFile():
    if QUOTES_DIR:
        os.makedirs(os.path.dirname(QUOTES_DIR) , exist_ok=True)
    if not os.path.exists(QUOTES_DIR):
        saveQuoteFile([])


def loadQuoteFile() -> List[dict]:
    """
    返回列表形式的语录数据（items: {"text": str, "weight": float}）
    """

    ensureQuoteFile()

    try:
        with open(QUOTES_DIR , "r" , encoding="utf-8") as f:
            quoteData = json.load(f)
            if isinstance(quoteData , list):
                return quoteData
            return[]
    except Exception:
        return []
    

def saveQuoteFile(quotes: List[dict]):
    ensureQuoteFile()
    with open(QUOTES_DIR , "w" , encoding="utf-8") as f:
        json.dump(quotes , f , ensure_ascii=False , indent=2)




def getRandomQuote() -> Optional[str]:

    quotes = loadQuoteFile()
    if not quotes:
        return None
    weights = [float(q.get("weight" , 1.0)) for q in quotes]
    picked = random.choices(quotes , weights=weights , k=1)[0]

    # 存储中用的是 "\n" 作为换行标记，返回给 telegram 时转换为真实换行
    return picked.get("text", "").replace("\\n", "\n")




def userOperation(operation: str , index:Optional[int]=None , payload:Optional[dict]=None):
    """
    统一操作接口（面向 CLI / handlers 调用）
      - operation:
          "add"       : payload -> dict with keys "text"(str) and optional "weight"(float)
          "delete"    : index -> int
          "set"       : (index, payload) 修改指定条目（payload 可包含 text / weight）
          "list"      : 返回完整列表（不排序）
          "get"       : 返回 index 对应条目或 None
    返回：
      - 对于增删改 返回 True/False
      - "list" 返回 list
      - "get" 返回 dict | None
    """

    quotes = loadQuoteFile()

    match operation:
        case "add":
            if not isinstance(payload , dict) or "text" not in payload:
                return False
            quotes.append({
                "text": payload["text"],
                "weight": float(payload.get("weight" , 1.0)),
            })
            saveQuoteFile(quotes)
            return True
        
        case "delete":
            if index is None or not (0 <= index < len(quotes)):
                return False
            quotes.pop(index)
            saveQuoteFile(quotes)
            return True
        
        case "set":
            if index is None or not (0<= index < len(quotes)):
                return False
            quotes[index].update(payload)
            saveQuoteFile(quotes)
            return True
        
        case "list":
            return quotes
        
    return False



def collectQuoteViewModel(selectedIndex: int=-1 , limitPreviewChars: int=15) -> Tuple[List[dict] , int]:
    """
    构建供渲染使用的 entries 列表，并按 weight 从小到大排序（A 模式）。
    返回 (sorted_quotes, new_selected_index)
      - sorted_quotes: 每项包含 {"text", "weight"}
      - new_selected_index: 传入 selected_index 在排序后的新下标（若传入非法则为 -1）

    注意：排序不会修改文件，仅在内存返回排序结果。
    """

    quotes = loadQuoteFile()
    sortedQuotes = sorted(quotes , key=lambda x:float(x.get("weight" , 1.0)))

    if selectedIndex is None or not isinstance(selectedIndex , int):
        selectedIndex = -1

    entries = []
    # 添加 (+) 项
    entries.append({
        "text": "(+)",
        "preview": "添加新语录",
        "weight": None,
        "raw": None,
    })

    for q in sortedQuotes:
        text = q.get("text" , "")
        preview = text[:limitPreviewChars] + ("..." if len(text) > limitPreviewChars else "")
        entries.append({
            "text": text,
            "preview": preview,
            "weight": float(q.get("weight" , 1.0)),
            "raw": q,
        })
    
    meta = {
        "selected": max(0 , min(selectedIndex , (len(entries) - 1)) if entries else 0),
        "count": len(entries)
    }

    return entries , meta


# ─────────────────────────────────────────────────────────────────────────────
# ANSI 工具（相对光标移动）
# ─────────────────────────────────────────────────────────────────────────────

def cuu(n: int):    sys.stdout.write(f"\x1b[{n}A")      # 光标上移
def cud(n: int):    sys.stdout.write(f"\x1b[{n}B")      # 光标下移
def clr():          sys.stdout.write("\x1b[2K")         # 清除本行
def bol():          sys.stdout.write("\x1b[G")          # 返回第一行
def crt():          sys.stdout.write("\r")              # 光标移回句首


def quoteUIRenderer(entries: List[dict] , selectedIndex:int=-1 , prevHeight:int=0) -> int:
    
    console = Console()
    table = Table(title="ZincNya Quotes")
    table.add_column("No." , justify="right")
    table.add_column("Weight" , justify= "right")
    table.add_column("Preview" , justify="left")

    for i , e in enumerate(entries , start=0):
        isSelected: bool = (i == selectedIndex)
        preview = e.get("preview" , "")

        weight = e.get("weight" , None)
        # 确保 weight 不是 None 再格式化
        weightStr = "-" if weight is None else f"{weight:.3g}"

        if isSelected:
            table.add_row(
                f"[bold yellow]> {i}[/]",
                f"[bold yellow]{weightStr}[/]",
                f"[bold yellow]{preview}[/]"
            )
        else:
            table.add_row(str(i) , weightStr , preview)

    with console.capture() as capture:
        console.print(table)
    rendered = capture.get()
    lines = rendered.splitlines()

    if prevHeight and prevHeight > 0:
        cuu(prevHeight)
        for _ in range(prevHeight):
            clr()
            sys.stdout.write("\n")
        # 逐行擦除后再把光标移回块顶
        cuu(prevHeight)

    for ln in lines:
        clr()
        crt()
        print(ln)
    
    sys.stdout.flush()

    return len(lines)




async def editQuoteViaEditor(initialTextEscaped: str , initialWeight: float = 1.0) -> Optional[str]:
    """
    同步版本：在临时文件中打开系统编辑器（$EDITOR 或 vi/nano）。
    编辑器中显示 initial_text_escaped 的真实换行（把 "\\n" -> "\n"）。
    保存后把文件内容读取并把真实换行转换回 "\\n"（用于存储）。
    返回转码后的文本（字符串），或者 None 表示未保存/中断。
    （这是同步阻塞函数；上层会通过 asyncio.to_thread 调用以避免阻塞主循环）
    """

    initialText = initialTextEscaped.replace("\\n", "\n")

    with tempfile.NamedTemporaryFile("w+" , delete=False , suffix=".tmp" , encoding="utf-8") as tf:
        tempPath = tf.name
        tf.write(f"# weight: {initialWeight}\n")
        tf.write(initialText)

    saved = await editFile(tempPath)
    if not saved:
        os.unlink(tempPath)
        return None
    
    with open(tempPath , "r" , encoding="utf-8") as f:
        lines = f.read().splitlines()

    os.unlink(tempPath)

    if not lines:
        return None
    
    firstLine = lines[0].strip()
    weight = initialWeight

    bodyLines = lines
    if firstLine.startswith("# weight:"):
        try:
            weight = float(firstLine[len("# weight:"):].strip())
        except:
            weight = 1.0
        bodyLines = lines[1:]

    body = "\n".join(bodyLines)
    escaped = body.replace("\n" , "\\n")
    
    return escaped , weight



async def quoteMenuController():

    # 先留一个空行占位，防止覆盖用户输入
    print()

    selected = 0
    prevHeight = 0
    

    def redraw():
        nonlocal selected , prevHeight
        entries , meta = collectQuoteViewModel(selectedIndex=selected)
        prevHeight = quoteUIRenderer(entries , selectedIndex=selected , prevHeight=prevHeight)

    # 初次绘制
    redraw()

    kb = KeyBindings()

    @kb.add("up")
    def _up(event):
        nonlocal selected
        selected = max(0, selected - 1)
        redraw()

    @kb.add("down")
    def _down(event):
        nonlocal selected
        entries, meta = collectQuoteViewModel(selectedIndex=selected)
        selected = min(len(entries) - 1, selected + 1)
        redraw()

    @kb.add("delete")
    def _del(event):
        nonlocal selected
        entries, meta = collectQuoteViewModel(selectedIndex=selected)

        if selected == 0:
            return  # 添加项不能删除

        raw = entries[selected].get("raw")
        if raw is None:
            return

        quotes = loadQuoteFile()
        try:
            idx = quotes.index(raw)
            userOperation("delete", index=idx)
        except ValueError:
            return

        # 保持选中位置有效
        entries2, meta2 = collectQuoteViewModel(selectedIndex=selected)
        selected = min(selected, len(entries2) - 1)

        redraw()

    @kb.add("enter")
    async def _enter(event):
        nonlocal selected
        entries, meta = collectQuoteViewModel(selectedIndex=selected)

        # 新建
        if selected == 0:
            res = await editQuoteViaEditor("", 1.0)
            if res:
                t, w = res
                userOperation("add", payload={"text": t, "weight": w})
            redraw()
            return

        # 编辑已有项
        raw = entries[selected].get("raw")
        if not raw:
            return

        quotes = loadQuoteFile()
        try:
            idx = quotes.index(raw)
        except ValueError:
            return

        res = await editQuoteViaEditor(raw.get("text", ""), raw.get("weight", 1.0))
        if res:
            newT, newW = res
            userOperation("set", index=idx, payload={"text": newT, "weight": newW})

        redraw()

    @kb.add("escape")
    def _esc(event):
        event.app.exit()

    app = Application(
        layout=Layout(TextArea(text="", focus_on_click=False)),
        key_bindings=kb,
        full_screen=False,
    )

    await app.run_async()

    # 清理残留 UI
    if prevHeight:
        cuu(prevHeight + 1)
        for _ in range(prevHeight + 1):
            clr()
            print()
        cuu(prevHeight + 1)