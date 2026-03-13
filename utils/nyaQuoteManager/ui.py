"""
语录 UI 层

ViewModel、渲染、编辑器、TUI 控制器
"""

import os
import sys
import shutil
import tempfile
from typing import List , Tuple , Optional

from rich.table import Table
from rich.console import Console

from utils.fileEditor import editFile
from utils.terminalUI import cls, smcup, rmcup
from utils.core.tuiBase import BaseTUIController
from .data import loadQuoteFile , saveQuoteFile , userOperation




def _extractBaseWeight(w) -> float:
    """从 weight 字段提取基础权重（兼容 float 和 list 格式）"""
    if isinstance(w, list):
        return float(w[0]) if w else 1.0
    return float(w)




def collectQuoteViewModel(selectedIndex: int=-1) -> Tuple[List[dict] , int]:

    limitPreviewChars: int = 15

    quotes = loadQuoteFile()
    sortedQuotes = sorted(quotes , key=lambda x: _extractBaseWeight(x.get("weight" , 1.0)))

    if selectedIndex is None or not isinstance(selectedIndex , int):
        selectedIndex = -1

    entries = []
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
            "weight": _extractBaseWeight(q.get("weight" , 1.0)),
            "raw": q,
        })

    meta = {
        "selected": max(0 , min(selectedIndex , (len(entries) - 1)) if entries else 0),
        "count": len(entries)
    }

    return entries , meta




def quoteUIRenderer(entries: List[dict] , selectedIndex: int = -1 , addRowOffset: int = 1) -> int:
    console = Console()

    try:
        terminalHeight = shutil.get_terminal_size().lines
    except:
        terminalHeight = 24

    visibleEntries , windowStart , hasMore = BaseTUIController.calculateVisibleWindow(entries , selectedIndex , terminalHeight)

    table = Table(title="ZincNya Quotes")
    table.add_column("No." , justify="right")
    table.add_column("Weight" , justify="right")
    table.add_column("Preview" , justify="left")

    for localIdx , e in enumerate(visibleEntries):
        globalIdx = windowStart + localIdx
        isSelected = (globalIdx == selectedIndex)
        preview = e.get("preview" , "")

        weight = e.get("weight" , None)
        weightStr = "-" if weight is None else f"{weight:.3g}"
        isAddRow = (weight is None)  # (+) 行的 weight 为 None，以此区分

        # 序号显示规则：普通条目从 1 开始连续编号，(+) 行不占序号
        # displayNo = globalIdx - addRowOffset + 1
        # addRowOffset=1（默认）：index 1 → 显示 1，index 2 → 显示 2
        displayNo = globalIdx - addRowOffset + 1

        if isSelected:
            table.add_row(
                "[bold yellow](+)[/]" if isAddRow else f"[bold yellow]> {displayNo}[/]",
                f"[bold yellow]{weightStr}[/]",
                f"[bold yellow]{preview}[/]"
            )
        elif isAddRow:
            table.add_row("[cyan](+)[/]" , f"[dim]{weightStr}[/]" , f"[dim]{preview}[/]")
        else:
            table.add_row(str(displayNo) , weightStr , preview)

    with console.capture() as capture:
        console.print(table)
    lines = capture.get().splitlines()

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

    cls()

    for ln in lines:
        print(ln)
    sys.stdout.flush()

    return len(lines)




async def editQuoteViaEditor(initialTextEscaped: str , initialWeight: float = 1.0) -> Optional[Tuple[str, float]]:
    initialText = initialTextEscaped.replace("\\n", "\n")

    tempPath: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile("w+" , delete=False , suffix=".tmp" , encoding="utf-8") as tf:
            tempPath = tf.name
            tf.write(f"# weight: {initialWeight}\n")
            tf.write(initialText)

        saved = await editFile(tempPath)
        if not saved:
            return None

        with open(tempPath , "r" , encoding="utf-8") as f:
            lines = f.read().splitlines()
    finally:
        if tempPath and os.path.exists(tempPath):
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




class QuoteTUIController(BaseTUIController):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # (+) 行固定在 index 0，数字跳转需偏移 1（输入 1 → index 1）
        self.addRowOffset = 1

    async def collectViewModel(self, selectedIndex: int):
        return collectQuoteViewModel(selectedIndex=selectedIndex)

    def renderUI(self, entries, selectedIndex):
        return quoteUIRenderer(entries , selectedIndex=selectedIndex , addRowOffset=self.addRowOffset)

    def getEmptyMessage(self):
        return "语录列表为空喵……"

    def getExitMessage(self):
        return "退出语录编辑喵——\n"

    def setupExtraKeyBindings(self, kb):
        @kb.add("delete")
        def _del(event):
            if self.selected == 0:
                return  # (+) 行不能删除

            raw = self.entries[self.selected].get("raw")
            if raw is None:
                return

            quotes = loadQuoteFile()
            try:
                idx = quotes.index(raw)
                userOperation("delete", index=idx)
            except ValueError:
                return

            self.entries , _ = collectQuoteViewModel(selectedIndex=self.selected)
            self.selected = min(self.selected, len(self.entries) - 1)
            self.redraw()

        @kb.add("enter")
        def _enter(event):
            if self.selected < len(self.entries):
                if self.selected == 0:
                    self.pendingAction = ("add",)
                else:
                    self.pendingAction = ("edit",)
                event.app.exit()

    async def handlePendingAction(self):
        actionType = self.pendingAction[0]

        if actionType == "add":
            res = await editQuoteViaEditor("", 1.0)
            if res:
                t, w = res
                userOperation("add", payload={"text": t, "weight": w})
            await self.refreshEntries()

        elif actionType == "edit":
            raw = self.entries[self.selected].get("raw")
            if not raw:
                return True

            quotes = loadQuoteFile()
            try:
                idx = quotes.index(raw)
            except ValueError:
                return True

            res = await editQuoteViaEditor(raw.get("text", ""), _extractBaseWeight(raw.get("weight", 1.0)))
            if res:
                newT, newW = res
                userOperation("set", index=idx, payload={"text": newT, "weight": newW})
            await self.refreshEntries()

        return True




async def quoteMenuController(app=None):
    controller = QuoteTUIController(app=app , mode="manage")
    await controller.run()