"""
utils/nyaQuoteManager.py

用于管理 /nya 的语录存储与在 CLI 中展示的数据模型。
模块组织与 whitelistManager.py 类似，分为：
  - 文件读写（内部函数）
  - 外部操作入口（userOperation）
  - UI 相关：
            collectQuoteViewModel、quoteUIRenderer、quoteMenuController
            以及一堆 ANSI 工具函数：cuu、cud、ctr 等
  - 编辑器辅助：editQuoteViaEditor（打开外部 TUI 编辑器）
  - 键盘监听及控制函数：quoteMenuController

  
================================================================================
内部函数，面向 data/ZincNyaQuote.json 的操作，与 whiteliseManager.py 的相似，
包含 ensureQuoteFile、loadQuoteFile、saveQuoteFile 三个功能


当涉及到对 ZincNyaQuote.json 的操作时，先调用 ensureQuoteFile 检查文件是否存在。
  若路径不存在则初始化为默认格式（空列表）：
    []

loadQuoteFile() 用于读取并返回列表形式的语录数据，形如：
    [
        {
            "text": str,
            "weight": float
        },
    ]

saveQuoteFile() 接受 quotes: List[dict]
    保存语录到 ZincNyaQuotes.json


================================================================================
外部函数，面向用户操作的函数
包含：
    userOperation、
    collectQuoteViewModel、quoteUIRenderer

三个功能。
其中，第二行的两个函数主要面向外部对 ZincNyaQuotes.json 的可视化操作，即 UI 相关操作。


userOperation()，统一操作接口
    函数分别接受
        - 操作的类型（operation: str）
        - 被使用 Enter 选中的项（index: Optional[int] = None）
        - 载荷（payload: Optional[dict] = None），作为字典，包含语录文本 text: str 和权重(在 editQuoteViaEditor 细说) weight: float

      operation 可能的取值有：
          "add"       : 添加被用户编辑好的 payload 进 json 文件 
          "delete"    : 使用了 index 参数，从 json 中删除指定序号的项
          "set"       : (index, payload) 修改指定条目
          "list"      : 返回完整列表（不排序）
          "get"       : 返回 index 对应条目或 None
    返回：
        - 对于增删改 返回 True/False
        - "list" 返回 list
        - "get" 返回 dict | None


UI 相关的 3 个函数——

collectQuoteViewModel() 一般用于构建 quoteUIRenderer 所需的数据类型，
    一般来说，用户在进行键盘的操作后，会调用函数进行 ZincNyaQuote 表格的重绘
    接受：
        - 被选定的项的序号（selectedIndex: int = -1），
            · 其接受的是经过键控函数操作后的序号
            · 若表格为初次绘制，selectedIndex 默认为 -1，即不选中任何项
    并返回：
        - 被格式化的列表 entries
        - 包含了被选中的（在渲染时应该高亮的）项的序号、列表的长度的 *字典* meta
            meta = {
                        "selected": max(0 , min(selectedIndex , (len(entries) - 1)) if entries else 0),
                        "count": len(entries)
                }

    函数先通过 loadQuoteFile() 从 json 中拿到语录的原始数据，并按 weight 从小到大排序，
    从而得到文本、权重，再将其一一塞入 entries 列表，如：
        entries.append({
                "text": text,
                "preview": preview,                                 # 当对象文本过长时，取前 n 个字 (默认为 15) 作为预览
                "weight": float(q.get("weight" , 1.0)),             # 各条 nyaQuote 被取用的权重，默认为 1.0
                "raw": q,                                           # 正在操作的项的序号
            })

    一般而言，键控函数会先通过该函数获取表格目前的状态，根据键盘操作执行对应逻辑后，把操作后的结果发给该函数进行重绘
    另外，排序不会修改文件，仅在内存返回排序结果。

quoteUIRenderer() 将 collectWhitelistViewModel 生成的 entries 渲染为 Rich 表格，
        并在重绘前使用几个 ANSI 功能函数对屏幕进行擦除等操作

    函数接受：
        - 格式化的表格信息 （entries: List[dict]）
        - 被选中的数字，即应当在渲染时高亮的项的序号（selectedIndex: int = -1）
        - 上一次渲染的表格的高度（prevHeight: int = 0），用于精准地局部擦除

    并输出当前渲染的表格的长度 len(lines)

    表格有 3 列：
        - 序号
        - 权重
        - 文本预览


================================================================================
辅助函数，包含：
    - 编辑器辅助：editQuoteViaEditor（打开外部 TUI 编辑器）
    - 键盘监听及控制函数：quoteMenuController
    - 获取随机语录函数：getRandomQuote
    - ANSI 控制函数。


编辑器辅助 editQuoteViaEditor()，用于编辑选中的 ZincNyaQuote。
    当对项（无论是常规项还是 (+) 项）使用 Enter 键时，函数通过 tmpfile 新建一个临时文件，
    并调用 utils/fileEditor.py 使用 prompt_toolkit 的 TextArea 编辑并保存。

    文件有标准格式：

        # weight: 1.0
        <TEXT>

            其中 weight: float 决定 json 中的各条语录会以多大的权重被取得，
            <TEXT>: str 则是语录的内容

        在按下 ^S 保存并退出界面后，文件头 # weight: 后的数字和下方的文字分别会被以浮点数和字符串类型读取，存入 weight 和 text 中
        若未能检测到合乎格式的权重，将按照默认权重 1.0 保存。
        
        编辑时的换行将被保存为 \n，在读取时替换回换行


quoteMenuController() 用于键盘监听，并执行对应的逻辑。

    在控制台使用 /nya --edit / -e 时进入函数

    它使用 prompt_toolkit 包，支持读取 ↑、↓、Enter、Esc、Delete 键，其中：
        - ↑ 键：     让选中项上移一项，即令 selectedIndex 减去 1
        - ↓ 键：     让选中项下移一项，即令 selectedIndex 加上 1
        - Del键：   删除列表中的选中项，即调用 userOperation() 中的 "delete"
        - Enter键： 确认选中列表中选中项，且有：
            · 当选中的项为第 0 项，即项 (+) 时，在 json 中新建一项，并启用 editQuoteViaEditor 函数进行编辑
            · 当选中的项为常规的语录项时，直接启用 editQuoteViaEditor 进行编辑
    
    进行操作后，调用内嵌的 redraw() 函数进行表格的重绘。
    当退出时，清除残留的 UI。


getRandomQuote() 函数用于从 json 中根据权重随机挑出语录返回。



ANSI 控制函数用于控制光标的相对移动，若：

    def cuu(n: int):    sys.stdout.write(f"\x1b[{n}A")      # 光标上移
    def cud(n: int):    sys.stdout.write(f"\x1b[{n}B")      # 光标下移
    def clr():          sys.stdout.write("\x1b[2K")         # 清除本行
    def bol():          sys.stdout.write("\x1b[G")          # 返回第一行
    def crt():          sys.stdout.write("\r")              # 光标移回句首

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
from utils.terminalUI import cls, smcup, rmcup




def ensureQuoteFile():
    '''
    当涉及到对 ZincNyaQuote.json 的操作时，先调用 ensureQuoteFile 检查文件是否存在。
    若路径不存在则初始化为默认格式（空列表）：
        []
    '''
    if QUOTES_DIR:
        os.makedirs(os.path.dirname(QUOTES_DIR) , exist_ok=True)
    if not os.path.exists(QUOTES_DIR):
        saveQuoteFile([])


def loadQuoteFile() -> List[dict]:
    '''
    loadQuoteFile() 用于读取并返回列表形式的语录数据，形如：

        [
            {
                "text": str,
                "weight": float
            },
        ]
    '''

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
    '''
    saveQuoteFile() 接受 quotes: List[dict]
    
        保存语录到 ZincNyaQuotes.json
    '''
    ensureQuoteFile()
    with open(QUOTES_DIR , "w" , encoding="utf-8") as f:
        json.dump(quotes , f , ensure_ascii=False , indent=2)




def getRandomQuote() -> list[str]:
    """
    从 json 中根据权重随机挑出语录返回

    支持两种 weight 格式：
        1. 传统格式：weight: float（单条消息）
        2. 新格式：weight: [float, float, ...]（多条消息 + 条件概率）

    新格式说明：
        - text 用 "|||" 分隔多条消息
        - weight[0] = P(这条语录被选中)
        - weight[1] = P(发送第二条 | 已发送第一条)
        - weight[n] = P(发送第n+1条 | 已发送第n条)

    返回：
        - 消息列表 List[str]，可能是 1 条或多条
        - 如果语录为空，返回空列表
    """

    quotes = loadQuoteFile()
    if not quotes:
        return []

    # 提取基础权重用于选择语录
    # 如果 weight 是列表，取第一个元素作为基础权重
    baseWeights = []
    for q in quotes:
        w = q.get("weight", 1.0)
        if isinstance(w, list):
            baseWeights.append(float(w[0]) if w else 1.0)
        else:
            baseWeights.append(float(w))

    # 根据基础权重随机选择一条语录
    picked = random.choices(quotes, weights=baseWeights, k=1)[0]

    text = picked.get("text", "")
    weight = picked.get("weight", 1.0)

    # 检查是否是多条消息格式
    if "|||" in text:
        # 分割消息
        parts = text.split("|||")
        messages = []

        # 第一条消息总是发送（因为已经被选中了）
        messages.append(parts[0].replace("\\n", "\n"))

        # 处理后续消息的条件概率
        if isinstance(weight, list) and len(weight) > 1:
            chainWeights = weight[1:]  # 条件概率列表

            # 衰减因子：未指定权重时，每多一条消息概率乘以此值
            DECAY_FACTOR = 0.64

            for i, part in enumerate(parts[1:], start=0):
                if i < len(chainWeights):
                    # 使用指定的权重
                    prob = chainWeights[i]
                else:
                    # 指数衰减：以最后一个指定权重为基准，逐步衰减
                    lastWeight = chainWeights[-1] if chainWeights else 0.8
                    decaySteps = i - len(chainWeights) + 1
                    prob = lastWeight * (DECAY_FACTOR ** decaySteps)

                # 根据条件概率决定是否发送这条消息
                if random.random() < prob:
                    messages.append(part.replace("\\n", "\n"))
                else:
                    # 一旦某条消息没有发送，后续的也不发送
                    break

        else:
            # 如果没有指定条件概率，则全部发送
            for part in parts[1:]:
                messages.append(part.replace("\\n", "\n"))

        return messages

    else:
        # 传统单条消息格式
        return [text.replace("\\n", "\n")]




def userOperation(operation: str , index:Optional[int]=None , payload:Optional[dict]=None):
    '''
    userOperation()，统一操作接口
        函数分别接受：
            - 操作的类型（operation: str）
            - 被使用 Enter 选中的项（index: Optional[int] = None）
            - 载荷（payload: Optional[dict] = None）。作为字典，包含语录文本 text: str 和权重(在 editQuoteViaEditor 细说) weight: float

        operation 可能的取值有：
            "add"       : 添加被用户编辑好的 payload 进 json 文件 
            "delete"    : 使用了 index 参数，从 json 中删除指定序号的项
            "set"       : (index, payload) 修改指定条目
            "list"      : 返回完整列表（不排序）
            "get"       : 返回 index 对应条目或 None
        返回：
            - 对于增删改 返回 True/False
            - "list" 返回 list
            - "get" 返回 dict | None

    '''

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



def collectQuoteViewModel(selectedIndex: int=-1) -> Tuple[List[dict] , int]:
    '''
    collectQuoteViewModel() 一般用于构建 quoteUIRenderer 所需的数据类型，
        一般来说，用户在进行键盘的操作后，会调用函数进行 ZincNyaQuote 表格的重绘
        接受：
            - 被选定的项的序号（selectedIndex: int = -1），
                · 其接受的是经过键控函数操作后的序号
                · 若表格为初次绘制，selectedIndex 默认为 -1，即不选中任何项
        并返回：
            - 被格式化的列表 entries
            - 包含了被选中的（在渲染时应该高亮的）项的序号、列表的长度的 *字典* meta
                meta = {
                            "selected": max(0 , min(selectedIndex , (len(entries) - 1)) if entries else 0),
                            "count": len(entries)
                    }

        函数先通过 loadQuoteFile() 从 json 中拿到语录的原始数据，并按 weight 从小到大排序，
        从而得到文本、权重，再将其一一塞入 entries 列表，如：
            entries.append({
                    "text": text,
                    "preview": preview,                                 # 当对象文本过长时，取前 n 个字 (默认为 15) 作为预览
                    "weight": float(q.get("weight" , 1.0)),             # 各条 nyaQuote 被取用的权重，默认为 1.0
                    "raw": q,                                           # 正在操作的项的序号
                })

    一般而言，键控函数会先通过该函数获取表格目前的状态，根据键盘操作执行对应逻辑后，把操作后的结果发给该函数进行重绘
    另外，排序不会修改文件，仅在内存返回排序结果。
    '''

    limitPreviewChars: int = 15

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


def _calculateVisibleWindow(entries: List[dict], selectedIndex: int, terminalHeight: int) -> Tuple[List[dict], int, dict]:
    """
    计算在终端高度限制下应该显示的条目窗口

    返回：
        - visibleEntries: 应该显示的条目列表
        - windowStart: 窗口起始索引
        - hasMore: {"up": bool, "down": bool} 是否有更多项
    """
    # 预留空间：标题(3) + 表头(2) + 表尾(1) + 提示行(2) = 8 行
    RESERVED_LINES = 8
    maxVisibleRows = max(5, terminalHeight - RESERVED_LINES)

    if len(entries) <= maxVisibleRows:
        # 全部显示
        return entries, 0, {"up": False, "down": False}

    # 居中显示选中项
    halfWindow = maxVisibleRows // 2
    windowStart = max(0, selectedIndex - halfWindow)
    windowEnd = min(len(entries), windowStart + maxVisibleRows)

    # 调整窗口确保填满
    if windowEnd - windowStart < maxVisibleRows:
        windowStart = max(0, windowEnd - maxVisibleRows)

    visibleEntries = entries[windowStart:windowEnd]

    hasMore = {
        "up": windowStart > 0,
        "down": windowEnd < len(entries)
    }

    return visibleEntries, windowStart, hasMore



def quoteUIRenderer(entries: List[dict] , selectedIndex:int=-1) -> int:
    '''
    quoteUIRenderer() 将 collectWhitelistViewModel 生成的 entries 渲染为 Rich 表格，
            并在重绘前使用几个 ANSI 功能函数对屏幕进行擦除等操作

        函数接受：
            - 格式化的表格信息 （entries: List[dict]）
            - 被选中的数字，即应当在渲染时高亮的项的序号（selectedIndex: int = -1）
            - 上一次渲染的表格的高度（prevHeight: int = 0），用于精准地局部擦除

        并输出当前渲染的表格的长度 len(lines)

        表格有 3 列：
            - 序号
            - 权重
            - 文本预览

        当表格高度超过终端高度时，只显示选中项周围的窗口，并添加 ↑/↓ 更多提示
    '''

    console = Console()

    # 获取终端高度
    try:
        import shutil as _shutil
        terminalSize = _shutil.get_terminal_size()
        terminalHeight = terminalSize.lines
    except:
        terminalHeight = 24  # 默认高度

    # 计算可见窗口
    visibleEntries, windowStart, hasMore = _calculateVisibleWindow(entries, selectedIndex, terminalHeight)

    table = Table(title="ZincNya Quotes")
    table.add_column("No." , justify="right")
    table.add_column("Weight" , justify= "right")
    table.add_column("Preview" , justify="left")

    for localIdx , e in enumerate(visibleEntries):
        globalIdx = windowStart + localIdx  # 全局索引
        isSelected: bool = (globalIdx == selectedIndex)
        preview = e.get("preview" , "")

        weight = e.get("weight" , None)
        # 确保 weight 不是 None 再格式化
        weightStr = "-" if weight is None else f"{weight:.3g}"

        if isSelected:
            table.add_row(
                f"[bold yellow]> {globalIdx}[/]",
                f"[bold yellow]{weightStr}[/]",
                f"[bold yellow]{preview}[/]"
            )
        else:
            table.add_row(str(globalIdx) , weightStr , preview)

    with console.capture() as capture:
        console.print(table)
    rendered = capture.get()
    lines = rendered.splitlines()

    # 添加 ↑/↓ 更多提示
    if hasMore["up"] or hasMore["down"]:
        extraLines = []
        if hasMore["up"]:
            extraLines.append(f"[dim]↑ 更多 {windowStart} 项[/dim]")
        if hasMore["down"]:
            remainingDown = len(entries) - (windowStart + len(visibleEntries))
            extraLines.append(f"[dim]↓ 更多 {remainingDown} 项[/dim]")

        with console.capture() as capture:
            for line in extraLines:
                console.print(line)
        extraRendered = capture.get()
        lines.extend(extraRendered.splitlines())

    # 清屏重绘（使用备用屏幕时效果更好）
    cls()

    # 输出新的表格
    for ln in lines:
        print(ln)
    sys.stdout.flush()

    return len(lines)




async def editQuoteViaEditor(initialTextEscaped: str , initialWeight: float = 1.0) -> Optional[str]:
    '''
    编辑器辅助 editQuoteViaEditor()，用于编辑选中的 ZincNyaQuote。
        当对项（无论是常规项还是 (+) 项）使用 Enter 键时，函数通过 tmpfile 新建一个临时文件，
        并调用 utils/fileEditor.py 使用 prompt_toolkit 的 TextArea 编辑并保存。

    文件有标准格式：

        # weight: 1.0
        <TEXT>

            其中 weight: float 决定 json 中的各条语录会以多大的权重被取得，
            <TEXT>: str 则是语录的内容

        在按下^S 保存并退出界面后，文件头 # weight: 后的数字和下方的文字分别会被以浮点数和字符串类型读取，存入 weight 和 text 中
        若未能检测到合乎格式的权重，将按照默认权重 1.0 保存。
        
        编辑时的换行将被保存为 \n，在读取时替换回换行
    '''

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



async def quoteMenuController(app=None):
    '''
    quoteMenuController() 用于键盘监听，并执行对应的逻辑。

    在控制台使用 /nya --edit / -e 时进入函数

    它使用 prompt_toolkit 包，支持读取 ↑、↓、Enter、Esc、Delete 键，其中：
        - ↑ 键：     让选中项上移一项，即令 selectedIndex 减去 1
        - ↓ 键：     让选中项下移一项，即令 selectedIndex 加上 1
        - Del键：   删除列表中的选中项，即调用 userOperation() 中的 "delete"
        - Enter键： 确认选中列表中选中项，且有：
            · 当选中的项为第 0 项，即项 (+) 时，在 json 中新建一项，并启用 editQuoteViaEditor 函数进行编辑
            · 当选中的项为常规的语录项时，直接启用 editQuoteViaEditor 进行编辑

    进行操作后，调用内嵌的 redraw() 函数进行表格的重绘。
    当退出时，清除残留的 UI。
    '''

    # 先留一个空行占位，防止覆盖用户输入
    print()

    # 切换到备用屏幕缓冲区
    smcup()
    sys.stdout.flush()

    # 如果传入了 app，设置交互模式标志，让 consoleListener 让步
    if app:
        app.bot_data["state"]["interactiveMode"] = True

    try:
        selected = 0

        def redraw():
            nonlocal selected
            entries , _ = collectQuoteViewModel(selectedIndex=selected)
            quoteUIRenderer(entries , selectedIndex=selected)

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
            entries, _ = collectQuoteViewModel(selectedIndex=selected)
            selected = min(len(entries) - 1, selected + 1)
            redraw()

        @kb.add("delete")
        def _del(event):
            nonlocal selected
            entries, _ = collectQuoteViewModel(selectedIndex=selected)

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
            entries2, _ = collectQuoteViewModel(selectedIndex=selected)
            selected = min(selected, len(entries2) - 1)

            redraw()

        @kb.add("enter")
        async def _enter(event):
            nonlocal selected
            entries, _ = collectQuoteViewModel(selectedIndex=selected)

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

        ptApp = Application(
            layout=Layout(TextArea(text="", focus_on_click=False)),
            key_bindings=kb,
            full_screen=False,
        )

        await ptApp.run_async()

    finally:
        # 切回主屏幕缓冲区
        rmcup()
        sys.stdout.flush()

        # 恢复交互模式状态
        if app:
            app.bot_data["state"]["interactiveMode"] = False