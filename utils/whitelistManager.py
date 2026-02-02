"""
utils/whitelistManager.py

是用于响应外部对 data/whitelist.json 的操作（如调用、添加项）并对后者进行管理的模块

模块大致可分为两部分——内部函数与外部函数


================================================================================
内部函数，面向 data/whitelist.json 的操作
包含 ensureWhitelistFile、loadWhitelistFile、saveWhitelistFile 三个功能


当涉及到对 whitelist.json 的操作时，先调用 ensureWhitelistFile 检查文件是否存在。
  若路径不存在则初始化为默认格式：
    {
        "allowed": {},
        "suspended": {}
    }

loadWhitelistFile() 用于读取并返回白名单文件的  *完整字典结构* ，形如：

    {
        "allowed": {
            "123": {"comment": "..."}
        },
        "suspended": {}
    }

saveWhitelistFile() 用于保存完整的白名单结构到 whitelist.json
    ** data 必须是一个包含 allowed / suspended 字段的字典 **


================================================================================
外部函数，面向命令模块和 Bot
包含：
    whetherAuthorizedUser、userOperation、
    checkChatAvailable、collectWhitelistViewModel、whitelistUIRenderer、
    whitelistMenuController
六个功能。
其中，后三个函数主要面向外部对 whitelist.json 的可视化操作，即 UI 相关操作。


whetherAuthorizedUser() 接受输入 userID: int | str ，**并返回一个 bool 值**
    输入函数的参数，无论是 int 还是 str，最终都按照字符串进行比较。


userOperation() 是统一的白名单操作入口。
    函数分别接受
        - 操作的类型（operation: str）
        - 对象 uuid（userID: str | None），默认为 None
        - 对象备注（comment: str），默认为 None

    operation 可能的取值有：
        - "addUser":        添加用户到 allowed
        - "deleteUser":     将用户从 allowed 中移除
        - "suspendUser":    将用户从 allowed 移入 suspended，体现为用户权限被挂起
        - "unsuspendUser":  将用户从 suspended 移回 allowed，恢复用户权限
        - "listUsers":      返回完整的白名单结构（ -> dict ）
        - "setComment":     添加 / 修改用户备注

    函数的返回值视操作而定：
        - bool: True / False    表示操作是否成功
        - dict                  在 listUsers 时返回白名单完整结构


UI 相关的函数——

checkChatAvailable() 用于检测白名单中的用户是否可以进行聊天。
    它接受 bot: Bot 和内部或外部输入的 uid: str，
    通常被 collectWhitelistViewModel() 调用并异步运行。
    函数视情况返回两种类型的值：
        - 当可以向用户发送信息时，返回布尔值 True
        - 当遇见 Forbidden、BadRequest 及其它原因时返回元组 (errorType, exception)
    返回值通常被 collectWhitelistViewModel() 调用，来判断与用户的聊天界面是否可用


collectWhitelistViewModel() 一般用于构建 whitelistUIRenderer 所需的数据类型，
    接受：
        - bot: Bot
        - selectedIndex: int = -1  被选中的项的序号
    返回：
        - entries: 格式化的列表
        - meta: 包含选中项序号和列表长度的字典
            {
                "selected": int,
                "count": int
            }

    entries 列表中的每一项形如：
    {
        "uid":           "12345",
        "listStatus":    "Allowed" 或 "Suspended"（原始列表状态）,
        "displayStatus": "Allowed" / "Suspended" / "Forbidden" / "Not Found" / "Error",
        "colour":        "wheat1" 或 "grey70"（Allowed 且可用为小麦色，其他为灰色）,
        "comment":       "备注文本",
        "available":     True / False
    }


whitelistUIRenderer() 将 collectWhitelistViewModel 生成的 entries 渲染为 Rich 表格

    函数接受：
        - entries: List[dict]            格式化的表格信息
        - selectedIndex: int = -1        被选中的数字，即应当在渲染时高亮的项的序号
        - prevHeight: int = 0            上一次渲染的表格的高度，用于精准地局部擦除

    并输出当前渲染的表格的长度 len(lines)

    显示的内容包括：
        - 序号
        - UID          Allowed 且可用时为小麦色，其他情况为灰色；选中项以黄色高亮
        - 状态          Allowed / Suspended / Forbidden / Not Found 等
        - 备注预览

    当表格高度超过终端高度时，只显示选中项周围的窗口，并添加 ↑/↓ 更多提示


whitelistMenuController() 交互式白名单控制器

    在控制台使用 /whitelist --list 或 /send --chat 时进入函数
    使用 terminalUI 的备用屏幕缓冲区（smcup/rmcup），不污染主终端

    支持两种模式（通过 mode 参数控制）：

    选择模式（mode="select"，默认）：
        用于 /send --chat，选择聊天对象
        - ↑/↓ 键：      移动选中项
        - Enter键：     确认选中并返回选中的 UID
        - Esc键：       取消选择，返回 None

    管理模式（mode="manage"）：
        用于 /whitelist --list，管理白名单
        - ↑/↓ 键：      移动选中项
        - ←/→ 键：      切换 Allowed/Suspended 状态
        - Enter键：     在 (+) 行添加新用户，在普通行编辑备注
        - Del键：       删除用户（需要确认）
        - Esc键：       退出管理界面

    返回：
        - 选择模式：返回选中的 UID (str) 或 None
        - 管理模式：始终返回 None

"""




import os
import sys
import json
import asyncio
import shutil
from typing import List, Tuple, Optional
from rich.table import Table
from rich.console import Console
from telegram import Update , Bot
from telegram.ext import CommandHandler, ContextTypes
from telegram.error import Forbidden , BadRequest

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.widgets import TextArea

from config import WHITELIST_DIR
from utils.logger import logAction
from utils.terminalUI import cls , smcup , rmcup
from utils.inputHelper import asyncInput




# 内部函数
def ensureWhitelistFile():
    os.makedirs(os.path.dirname(WHITELIST_DIR) , exist_ok=True)
    if not os.path.exists(WHITELIST_DIR):
        saveWhitelistFile({"allowed": {} , "suspended": {}})


def loadWhitelistFile():
    ensureWhitelistFile()
    with open(WHITELIST_DIR , "r" , encoding="utf-8") as f:
        return json.load(f)


def saveWhitelistFile(data):
    with open(WHITELIST_DIR , "w" , encoding="utf-8") as f:
        json.dump(data , f , ensure_ascii=False , indent=2)


def whetherAuthorizedUser(userID: int | str) -> bool:
    data = loadWhitelistFile()
    userID = str(userID)
    return userID in data.get("allowed" , {}) and userID not in data.get("suspended" , {})


def userOperation(operation , userID:str|None=None , comment=None) -> bool | dict:

    data = loadWhitelistFile()
    userID = str(userID) if userID else None

    match operation:
        case "addUser":
            if userID not in data["allowed"]:
                data["allowed"][userID] = {"comment": ""}
                saveWhitelistFile(data)
                return True
            return False

        case "deleteUser":
            if userID in data["allowed"]:
                data["allowed"].pop(userID)
                saveWhitelistFile(data)
                return True
            return False

        case "suspendUser":
            if userID in data["allowed"] and userID not in data["suspended"]:
                data["suspended"][userID] = data["allowed"].pop(userID)
                saveWhitelistFile(data)
                return True
            return False

        case "unsuspendUser":
            if userID in data["suspended"]:
                data["allowed"][userID] = data["suspended"].pop(userID)
                saveWhitelistFile(data)
                return True
            return False

        case "listUsers":
            return dict(data)

        case "setComment":
            if userID in data["allowed"]:
                data["allowed"][userID]["comment"] = comment
            elif userID in data["suspended"]:
                data["suspended"][userID]["comment"] = comment
            else:
                return False
            saveWhitelistFile(data)
            return True

        case _:
            raise ValueError(f"未知的操作类型喵：{operation}")




async def checkChatAvailable(bot: Bot , uid: str):
    """
    检查是否能与用户通信。

    使用 send_chat_action 而非 get_chat，
    因为 get_chat 即使用户屏蔽了 Bot 也可能成功。
    """
    try:
        await bot.send_chat_action(chat_id=uid, action="typing")
        return True
    except Forbidden as e:
        return ("Forbidden", e)
    except BadRequest as e:
        return ("NotFound", e)
    except Exception as e:
        return ("Error", e)




async def collectWhitelistViewModel(bot: Bot , selectedIndex: int = -1 , includeAddRow: bool = False) -> Tuple[List[dict], dict]:

    whitelistData = loadWhitelistFile()
    entries = []
    raw = []

    # 如果是管理模式，添加 (+) 行
    if includeAddRow:
        entries.append({
            "uid": "(+)",
            "listStatus": None,
            "displayStatus": "添加新用户",
            "colour": "cyan",
            "comment": "",
            "available": None,
            "isAddRow": True
        })

    # 先收集所有 allowed 和 suspended 的用户
    for uid , obj in whitelistData.get("allowed" , {}).items():
        raw.append((uid , "Allowed" , obj.get("comment" , "")))

    for uid , obj in whitelistData.get("suspended" , {}).items():
        raw.append((uid , "Suspended" , obj.get("comment" , "")))

    # 并发检查所有用户的聊天可用性（避免逐个等待）
    uids = [x[0] for x in raw]
    results = await asyncio.gather(*[checkChatAvailable(bot , uid) for uid in uids])

    # 组装为带有可用性标记的完整条目
    for (uid , listStatus , comment) , available in zip(raw , results):
        # 确定显示状态：Suspended 优先显示，Allowed 用户显示实际可用性
        if listStatus == "Suspended":
            displayStatus = "Suspended"
        elif available is True:
            displayStatus = "Allowed"
        elif isinstance(available, tuple):
            errorType, _ = available
            match errorType:
                case "Forbidden":
                    displayStatus = "Forbidden"
                case "NotFound":
                    displayStatus = "Not Found"
                case _:
                    displayStatus = "Error"
        else:
            displayStatus = "Unknown"

        # 颜色：可用为小麦色，不可用为灰色
        colour = "wheat1" if (available is True) else "grey70"

        entries.append({
            "uid": uid,
            "listStatus": listStatus,
            "displayStatus": displayStatus,
            "colour": colour,
            "comment": comment,
            "available": available is True,
            "isAddRow": False
        })

    # 确保选中索引在有效范围内
    meta = {"selected": max(0 , min(selectedIndex , len(entries) - 1)) if entries else 0 , "count": len(entries)}

    return entries, meta


def _calculateVisibleWindow(entries: List[dict] , selectedIndex: int , terminalHeight: int) -> Tuple[List[dict] , int , dict]:
    # 预留空间给标题、表头、提示行等
    RESERVED_LINES = 8
    maxVisibleRows = max(5 , terminalHeight - RESERVED_LINES)

    # 如果条目少于可见行数，全部显示
    if len(entries) <= maxVisibleRows:
        return entries , 0 , {"up": False, "down": False}

    # 让选中项居中显示
    halfWindow = maxVisibleRows // 2
    windowStart = max(0 , selectedIndex - halfWindow)
    windowEnd = min(len(entries) , windowStart + maxVisibleRows)

    # 调整窗口确保填满可见区域
    if windowEnd - windowStart < maxVisibleRows:
        windowStart = max(0 , windowEnd - maxVisibleRows)

    visibleEntries = entries[windowStart:windowEnd]
    hasMore = {"up": windowStart > 0 , "down": windowEnd < len(entries)}

    return visibleEntries , windowStart , hasMore


def whitelistUIRenderer(entries: list , selectedIndex: int = -1 , prevHeight: int = 0 , showHelpLine: bool = False) -> int:
    console = Console()

    # 获取终端高度以计算可见窗口
    try:
        terminalSize = shutil.get_terminal_size()
        terminalHeight = terminalSize.lines
    except:
        terminalHeight = 24

    # 计算本次应显示的窗口范围
    visibleEntries , windowStart , hasMore = _calculateVisibleWindow(entries , selectedIndex , terminalHeight)

    table = Table(title="\n正在查看白名单喵——\n")
    table.add_column("No." , justify="right")
    table.add_column("UID" , justify="left")
    table.add_column("状态" , justify="left")
    table.add_column("备注" , justify="left")

    for localIdx , e in enumerate(visibleEntries):
        # 使用全局索引来保持序号连贯
        globalIdx = windowStart + localIdx
        isSelected = (globalIdx == selectedIndex)
        uid = e["uid"]
        colour = e["colour"]
        displayStatus = e["displayStatus"]
        comment = e["comment"] or ""
        isAddRow = e.get("isAddRow", False)

        # "(+)" 行特殊处理
        if isAddRow:
            if isSelected:
                table.add_row("[bold yellow]>[/]" , "[bold yellow](+)[/]" , "[bold yellow]添加新用户[/]" , "")
            else:
                table.add_row("" , "[cyan](+)[/]" , "[dim]添加新用户[/]" , "")
        else:
            commentPreview = "" if comment.strip() == "" else comment[:15] + ("..." if len(comment) > 15 else "")
            if isSelected:
                table.add_row(f"[bold yellow]> {globalIdx}[/]" , f"[bold yellow]{uid}[/]" , f"[bold yellow]{displayStatus}[/]" , f"[bold yellow]{commentPreview}[/]")
            else:
                uidRendered = f"[{colour}]{uid}[/]"
                table.add_row(str(globalIdx) , uidRendered , displayStatus , commentPreview)

    with console.capture() as capture:
        console.print(table)

    rendered = capture.get()
    lines = rendered.splitlines()

    # 添加 ↑/↓ 提示，告知用户还有更多项
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

    # 添加快捷键帮助行
    if showHelpLine:
        with console.capture() as capture:
            console.print("\n[dim]←→ 切换状态 | Enter 编辑备注 | Del 删除 | Esc 退出[/dim]")
        helpRendered = capture.get()
        lines.extend(helpRendered.splitlines())

    # 清屏重绘
    cls()

    # 输出新的表格
    for ln in lines:
        print(ln)
    sys.stdout.flush()

    return len(lines)




async def whitelistMenuController(bot: Bot , app=None , mode: str = "select") -> Optional[str]:
    """
    交互式白名单控制器

    参数：
        bot: Bot 实例
        app: Application 实例（用于设置交互模式）
        mode: 模式
            - "select": 选择模式，用于 /send --chat，Enter 返回选中的 UID
            - "manage": 管理模式，用于 /whitelist -l，支持增删改操作

    返回：
        - 选择模式：返回选中的 UID 或 None
        - 管理模式：始终返回 None
    """

    # 先留一个空行，防止覆盖用户输入
    print()

    # 切换到备用屏幕缓冲区
    smcup()
    sys.stdout.flush()

    # 如果传入了 app，设置交互模式标志，让 consoleListener 让步
    if app:
        app.bot_data["state"]["interactiveMode"] = True

    isManageMode = (mode == "manage")

    try:
        # 预先获取白名单数据，避免在键盘回调中调用异步函数
        entries , _ = await collectWhitelistViewModel(bot , selectedIndex=0 , includeAddRow=isManageMode)

        # 管理模式下即使为空也显示 (+) 行；选择模式下为空则退出
        if not entries:
            if not isManageMode:
                print("白名单为空喵……")
                return None

        selected = 0
        prevHeight = 0
        pendingAction = None  # 用于存储需要在事件循环外执行的异步操作

        async def refreshEntries():
            nonlocal entries
            entries , _ = await collectWhitelistViewModel(bot , selectedIndex=selected , includeAddRow=isManageMode)

        def redraw():
            nonlocal selected , prevHeight
            prevHeight = whitelistUIRenderer(entries , selectedIndex=selected , prevHeight=prevHeight , showHelpLine=isManageMode)

        # 初次绘制
        redraw()

        # 绑定键盘事件
        kb = KeyBindings()

        @kb.add("up")
        def _up(event):
            nonlocal selected
            selected = max(0 , selected - 1)
            redraw()

        @kb.add("down")
        def _down(event):
            nonlocal selected
            selected = min(len(entries) - 1 , selected + 1)
            redraw()

        @kb.add("escape")
        def _esc(event):
            nonlocal selected
            selected = -1
            print("退出白名单列表喵——\n\n")
            event.app.exit()

        if isManageMode:
            # 管理模式的键绑定

            @kb.add("left")
            def _left(event):
                nonlocal pendingAction
                if selected < len(entries) and not entries[selected].get("isAddRow"):
                    pendingAction = ("toggle", "left")
                    event.app.exit()

            @kb.add("right")
            def _right(event):
                nonlocal pendingAction
                if selected < len(entries) and not entries[selected].get("isAddRow"):
                    pendingAction = ("toggle", "right")
                    event.app.exit()

            @kb.add("delete")
            def _del(event):
                nonlocal pendingAction
                if selected < len(entries) and not entries[selected].get("isAddRow"):
                    pendingAction = ("delete",)
                    event.app.exit()

            @kb.add("enter")
            def _enter(event):
                nonlocal pendingAction
                if selected < len(entries):
                    if entries[selected].get("isAddRow"):
                        pendingAction = ("add",)
                    else:
                        pendingAction = ("edit_comment",)
                    event.app.exit()

        else:
            # 选择模式：Enter 直接返回
            @kb.add("enter")
            def _enter(event):
                event.app.exit()

        # 主循环
        while True:
            ptApp = Application(layout=Layout(TextArea(text="" , focus_on_click=False)) , key_bindings=kb , full_screen=False)
            await ptApp.run_async()

            # 用户按 Esc 取消
            if selected == -1:
                return None

            # 选择模式：直接返回 UID
            if not isManageMode:
                if entries and 0 <= selected < len(entries):
                    return entries[selected]["uid"]
                return None

            # 管理模式：处理操作
            if pendingAction is None:
                continue

            actionType = pendingAction[0]

            if actionType == "toggle":
                # 切换 Allowed/Suspended 状态
                entry = entries[selected]
                uid = entry["uid"]
                currentStatus = entry["listStatus"]

                if currentStatus == "Allowed":
                    userOperation("suspendUser" , uid)
                elif currentStatus == "Suspended":
                    userOperation("unsuspendUser" , uid)

                await refreshEntries()
                pendingAction = None
                redraw()

            elif actionType == "delete":
                # 删除用户（需要确认）
                entry = entries[selected]
                uid = entry["uid"]

                # 切回主屏幕显示确认提示
                rmcup()
                sys.stdout.flush()

                try:
                    confirm = await asyncInput(f"确认删除 {uid} 吗？(y/N): ")
                    if confirm.lower() == "y":
                        userOperation("deleteUser" , uid)
                        # 调整选中位置
                        await refreshEntries()
                        selected = min(selected , len(entries) - 1)
                finally:
                    # 切回备用屏幕
                    smcup()
                    sys.stdout.flush()

                pendingAction = None
                redraw()

            elif actionType == "add":
                # 添加新用户
                rmcup()
                sys.stdout.flush()

                try:
                    newUid = await asyncInput("输入新用户的 Chat ID: ")
                    newUid = newUid.strip()
                    if newUid:
                        ok = userOperation("addUser" , newUid)
                        if ok:
                            print(f"已添加 {newUid} 到白名单")
                        else:
                            print(f"{newUid} 已在白名单中")
                        await asyncio.sleep(0.5)
                        await refreshEntries()
                finally:
                    smcup()
                    sys.stdout.flush()

                pendingAction = None
                redraw()

            elif actionType == "edit_comment":
                # 编辑备注
                entry = entries[selected]
                uid = entry["uid"]
                currentComment = entry.get("comment" , "")

                rmcup()
                sys.stdout.flush()

                try:
                    prompt = f"编辑 {uid} 的备注"
                    if currentComment:
                        prompt += f" (当前: {currentComment})"
                    prompt += ": "
                    newComment = await asyncInput(prompt)
                    # 允许清空备注
                    userOperation("setComment" , uid , newComment)
                    await refreshEntries()
                finally:
                    smcup()
                    sys.stdout.flush()

                pendingAction = None
                redraw()

    finally:
        # 切回主屏幕缓冲区
        rmcup()
        sys.stdout.flush()

        # 恢复交互模式状态
        if app:
            app.bot_data["state"]["interactiveMode"] = False




async def handleStart(update , context):
    user = update.effective_user
    userID = str(user.id)
    userName = user.username or "Unknown"
    name = f"{user.first_name} {user.last_name or ''}".strip()

    if not whetherAuthorizedUser(userID):
        print ("ご、ご主人様——")
        await logAction(None , f"有不认识的人尝试访问咱了……" , f"直接拒绝喵：{name}(@{userName} / ID：{userID})" , "withOneChild")
        return

    await update.message.reply_text("欢迎回来喵——")