"""
白名单 UI 层

ViewModel、渲染、TUI 控制器
"""

import sys
import asyncio
import shutil
from typing import List, Tuple, Optional

from rich.table import Table
from rich.console import Console
from telegram import Bot
from telegram.error import Forbidden , BadRequest

from utils.inputHelper import asyncInput
from utils.core.tuiBase import BaseTUIController
from utils.terminalUI import cls , smcup , rmcup

from .data import loadWhitelistFile , userOperation




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

    for uid , obj in whitelistData.get("allowed" , {}).items():
        raw.append((uid , "Allowed" , obj.get("comment" , "")))

    for uid , obj in whitelistData.get("suspended" , {}).items():
        raw.append((uid , "Suspended" , obj.get("comment" , "")))

    uids = [x[0] for x in raw]
    results = await asyncio.gather(*[checkChatAvailable(bot , uid) for uid in uids])

    for (uid , listStatus , comment) , available in zip(raw , results):
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

    meta = {"selected": max(0 , min(selectedIndex , len(entries) - 1)) if entries else 0 , "count": len(entries)}

    return entries, meta




def whitelistUIRenderer(entries: list , selectedIndex: int = -1 , prevHeight: int = 0 , showHelpLine: bool = False , addRowOffset: int = 0) -> int:
    console = Console()

    try:
        terminalHeight = shutil.get_terminal_size().lines
    except:
        terminalHeight = 24

    visibleEntries , windowStart , hasMore = BaseTUIController.calculateVisibleWindow(entries , selectedIndex , terminalHeight)

    table = Table(title="\n正在查看白名单喵——\n")
    table.add_column("No." , justify="right")
    table.add_column("UID" , justify="left")
    table.add_column("状态" , justify="left")
    table.add_column("备注" , justify="left")

    for localIdx , e in enumerate(visibleEntries):
        globalIdx = windowStart + localIdx
        isSelected = (globalIdx == selectedIndex)
        uid = e["uid"]
        colour = e["colour"]
        displayStatus = e["displayStatus"]
        comment = e["comment"] or ""
        isAddRow = e.get("isAddRow", False)

        if isAddRow:
            if isSelected:
                table.add_row("[bold yellow]>[/]" , "[bold yellow](+)[/]" , "[bold yellow]添加新用户[/]" , "")
            else:
                table.add_row("" , "[cyan](+)[/]" , "[dim]添加新用户[/]" , "")
        else:
            commentPreview = "" if comment.strip() == "" else comment[:15] + ("..." if len(comment) > 15 else "")
            # 序号显示规则：普通条目从 1 开始连续编号，(+) 行不占序号
            # displayNo = globalIdx - addRowOffset + 1
            #   manage 模式（addRowOffset=1）：index 1 → 显示 1，index 2 → 显示 2
            #   select 模式（addRowOffset=0）：index 0 → 显示 1，index 1 → 显示 2
            displayNo = globalIdx - addRowOffset + 1
            if isSelected:
                table.add_row(f"[bold yellow]> {displayNo}[/]" , f"[bold yellow]{uid}[/]" , f"[bold yellow]{displayStatus}[/]" , f"[bold yellow]{commentPreview}[/]")
            else:
                uidRendered = f"[{colour}]{uid}[/]"
                table.add_row(str(displayNo) , uidRendered , displayStatus , commentPreview)

    with console.capture() as capture:
        console.print(table)

    rendered = capture.get()
    lines = rendered.splitlines()

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
        lines.extend(capture.get().splitlines())

    if showHelpLine:
        with console.capture() as capture:
            console.print("\n[dim]←→ 切换状态 | Enter 编辑备注 | Del 删除 | Esc 退出[/dim]")
        lines.extend(capture.get().splitlines())

    cls()

    for ln in lines:
        print(ln)
    sys.stdout.flush()

    return len(lines)




class WhitelistTUIController(BaseTUIController):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # manage 模式下 index 0 为 (+) 行，数字跳转需偏移 1
        if self.mode == "manage":
            self.addRowOffset = 1

    async def collectViewModel(self, selectedIndex: int):
        isManageMode = (self.mode == "manage")
        return await collectWhitelistViewModel(self.bot , selectedIndex=selectedIndex , includeAddRow=isManageMode)


    def renderUI(self, entries, selectedIndex):
        isManageMode = (self.mode == "manage")
        return whitelistUIRenderer(entries , selectedIndex=selectedIndex , showHelpLine=isManageMode , addRowOffset=self.addRowOffset)


    def getEmptyMessage(self):
        return "白名单为空喵……"


    def getExitMessage(self):
        return "退出白名单列表喵——\n\n"


    def getSelectedEntry(self):
        entry = super().getSelectedEntry()
        if entry:
            return entry["uid"]
        return None
    

    def setupExtraKeyBindings(self, kb):
        if self.mode != "manage":
            return

        @kb.add("left")
        def _left(event):
            if self.selected < len(self.entries) and not self.entries[self.selected].get("isAddRow"):
                self.pendingAction = ("toggle", "left")
                event.app.exit()

        @kb.add("right")
        def _right(event):
            if self.selected < len(self.entries) and not self.entries[self.selected].get("isAddRow"):
                self.pendingAction = ("toggle", "right")
                event.app.exit()

        @kb.add("delete")
        def _del(event):
            if self.selected < len(self.entries) and not self.entries[self.selected].get("isAddRow"):
                self.pendingAction = ("delete",)
                event.app.exit()

        @kb.add("enter")
        def _enter(event):
            if self.selected < len(self.entries):
                if self.entries[self.selected].get("isAddRow"):
                    self.pendingAction = ("add",)
                else:
                    self.pendingAction = ("edit_comment",)
                event.app.exit()


    async def handlePendingAction(self):
        actionType = self.pendingAction[0]

        if actionType == "toggle":
            entry = self.entries[self.selected]
            uid = entry["uid"]
            currentStatus = entry["listStatus"]

            if currentStatus == "Allowed":
                userOperation("suspendUser" , uid)
            elif currentStatus == "Suspended":
                userOperation("unsuspendUser" , uid)

            await self.refreshEntries()

        elif actionType == "delete":
            entry = self.entries[self.selected]
            uid = entry["uid"]

            rmcup()
            sys.stdout.flush()

            try:
                confirm = await asyncInput(f"确认删除 {uid} 吗？(y/N): ")
                if confirm.lower() == "y":
                    userOperation("deleteUser" , uid)
                    await self.refreshEntries()
                    self.selected = min(self.selected , len(self.entries) - 1)
            finally:
                smcup()
                sys.stdout.flush()

        elif actionType == "add":
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
                    await self.refreshEntries()
            finally:
                smcup()
                sys.stdout.flush()

        elif actionType == "edit_comment":
            entry = self.entries[self.selected]
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
                userOperation("setComment" , uid , newComment)
                await self.refreshEntries()
            finally:
                smcup()
                sys.stdout.flush()

        return True




async def whitelistMenuController(bot: Bot , app=None , mode: str = "select") -> Optional[str]:
    controller = WhitelistTUIController(bot=bot , app=app , mode=mode)
    return await controller.run()