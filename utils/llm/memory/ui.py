"""
utils/llm/memory/ui.py

Memory TUI 管理界面。

提供：
    - editMemoryViaEditor: 编辑器 + 头部元数据解析
    - MemoryTUIController: 交互式 TUI 控制器
    - memoryMenuController: 工厂函数
"""




import os
import sys
import asyncio
import tempfile
from typing import Optional

from rich.table import Table
from rich.console import Console

from utils.fileEditor import editFile
from utils.inputHelper import asyncInput
from utils.terminalUI import cls, smcup, rmcup
from utils.core.tuiBase import BaseTUIController
from .database import (
    addMemory, getMemories, updateMemory, deleteMemory,
    MEMORY_SCOPE_GLOBAL, VALID_SCOPE_TYPES,
)




async def editMemoryViaEditor(
    initialContent: str = "",
    initialTags: Optional[list] = None,
    initialPriority: int = 0,
) -> Optional[tuple]:
    """
    通过编辑器编辑 memory 内容和元数据。

    文件格式：
        # tags: tag1, tag2
        # priority: 5
        ---
        content here

    返回:
        (content, tags, priority) 或 None（取消/内容为空）
    """
    tagsStr = ", ".join(initialTags) if initialTags else ""

    template = (
        f"# tags: {tagsStr}\n"
        f"# priority: {initialPriority}\n"
        "---\n"
        f"{initialContent}"
    )

    tempPath = None
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".tmp", encoding="utf-8") as tf:
            tempPath = tf.name
            tf.write(template)

        saved = await editFile(tempPath)
        if not saved:
            return None

        with open(tempPath, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    finally:
        if tempPath and os.path.exists(tempPath):
            os.unlink(tempPath)

    if not lines:
        return None

    # 解析头部
    tags = list(initialTags) if initialTags else []
    priority = initialPriority
    separatorIdx = None

    for i, line in enumerate(lines):
        stripped = line.strip()

        if stripped == "---":
            separatorIdx = i
            break

        if stripped.startswith("# tags:"):
            raw = stripped[len("# tags:"):].strip()
            tags = [t.strip() for t in raw.split(",") if t.strip()] if raw else []

        elif stripped.startswith("# priority:"):
            raw = stripped[len("# priority:"):].strip()
            try:
                priority = int(raw)
            except ValueError:
                pass

    # 分隔符之后为 content
    if separatorIdx is not None:
        contentLines = lines[separatorIdx + 1:]
    else:
        contentLines = lines

    content = "\n".join(contentLines).strip()
    if not content:
        return None

    return content, tags, priority




class MemoryTUIController(BaseTUIController):
    """
    Structured memory 的交互式 TUI 管理控制器。

    继承 BaseTUIController，支持：
        - 浏览全部 memory 条目（含已禁用）
        - Enter 添加/编辑（通过 editMemoryViaEditor 在编辑器中填写内容和元数据）
        - ←/→ 快速切换 enabled 状态
        - Delete 删除（需确认）

    入口：memoryMenuController()
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # manage 模式下 index 0 固定为 (+) 添加行，数字跳转需偏移 1
        # （输入数字 1 → 跳到第一条真实 memory，即 entries[1]）
        if self.mode == "manage":
            self.addRowOffset = 1


    async def collectViewModel(self, selectedIndex: int):
        """
        从数据库拉取全部 memory，构造 entries 列表。

        entries[0] 始终为 {"isAddRow": True}（(+) 添加行）；
        后续条目包含展示所需的扁平化字段和截断后的 preview。
        """
        rows = await getMemories(enabledOnly=False)

        _SCOPE_ORDER = {"global": 0, "chat": 1, "user": 2, "session": 3}
        rows = sorted(
            rows,
            key=lambda r: (
                _SCOPE_ORDER.get(r["scope_type"], 99),
                -r["priority"],
                -r["id"],
            ),
        )

        entries = []
        entries.append({"isAddRow": True})

        for row in rows:
            preview = row["content"].replace("\n", " ")
            if len(preview) > 40:
                preview = preview[:40] + "…"

            entries.append({
                "isAddRow": False,
                "id": row["id"],
                "scope_type": row["scope_type"],
                "scope_id": row["scope_id"],
                "content": row["content"],
                "tags": row["tags"],
                "enabled": row["enabled"],
                "priority": row["priority"],
                "source": row["source"],
                "preview": preview,
            })

        meta = {
            "selected": max(0, min(selectedIndex, len(entries) - 1)) if entries else 0,
        }

        return entries, meta


    def renderUI(self, entries, selectedIndex):
        """渲染 Memory 管理表格到终端（清屏后重绘）。返回渲染行数供下次清屏使用。"""
        console = Console()
        termHeight = self.getTerminalHeight()

        visibleEntries, windowStart, hasMore = self.calculateVisibleWindow(
            entries, selectedIndex, termHeight
        )

        table = Table(title="Memory 管理")
        table.add_column("No.", justify="right")
        table.add_column("ID", justify="right")
        table.add_column("Scope", justify="left")
        table.add_column("P", justify="right")
        table.add_column("Status", justify="center")
        table.add_column("Preview", justify="left")

        for localIdx, e in enumerate(visibleEntries):
            globalIdx = windowStart + localIdx
            isSelected = (globalIdx == selectedIndex)
            isAddRow = e.get("isAddRow", False)
            displayNo = globalIdx - self.addRowOffset + 1

            if isAddRow:
                if isSelected:
                    table.add_row("[bold yellow]>[/]", "", "[bold yellow](+) 添加[/]", "", "", "")
                else:
                    table.add_row("", "", "[cyan](+) 添加[/]", "", "", "")
            else:
                scopeType = e['scope_type']
                scopeID = e['scope_id']
                scopeStr = "global" if scopeType == "global" else f"{scopeType}:{scopeID}"

                preview = e.get('preview', '')

                if isSelected:
                    table.add_row(
                        f"[bold yellow]> {displayNo}[/]",
                        f"[bold yellow]{e['id']}[/]",
                        f"[bold yellow]{scopeStr}[/]",
                        f"[bold yellow]{e['priority']}[/]",
                        f"[bold yellow]{'ON' if e['enabled'] else 'OFF'}[/]",
                        f"[bold yellow]{preview}[/]",
                    )
                elif not e['enabled']:
                    table.add_row(
                        f"[dim]{displayNo}[/]",
                        f"[dim]{e['id']}[/]",
                        f"[dim]{scopeStr}[/]",
                        f"[dim]{e['priority']}[/]",
                        "[red]OFF[/red]",
                        f"[dim]{preview}[/]",
                    )
                else:
                    table.add_row(
                        str(displayNo),
                        str(e['id']),
                        scopeStr,
                        str(e['priority']),
                        "[green]ON[/green]",
                        preview,
                    )

        with console.capture() as capture:
            console.print(table)
        lines = capture.get().splitlines()

        if hasMore["up"] or hasMore["down"]:
            moreLines = self.renderMoreIndicators(
                console, hasMore, windowStart,
                len(entries), len(visibleEntries)
            )
            lines.extend(moreLines)

        with console.capture() as capture:
            console.print("\n[dim]Enter 编辑 | Del 删除 | ←→ 启用/停用 | Esc 退出[/dim]")
        lines.extend(capture.get().splitlines())

        cls()
        for ln in lines:
            print(ln)
        sys.stdout.flush()

        return len(lines)


    def getEmptyMessage(self):
        return "Memory 列表为空喵……\n"


    def getExitMessage(self):
        return "退出 Memory 管理喵——\n"


    def setupExtraKeyBindings(self, kb):
        """
        绑定 manage 模式专属按键，均设置 pendingAction 后 exit，
        实际操作在 handlePendingAction() 的异步上下文中执行。
        """

        @kb.add("enter")
        def _enter(event):
            if self.selected < len(self.entries):
                if self.entries[self.selected].get("isAddRow"):
                    self.pendingAction = ("add",)
                else:
                    self.pendingAction = ("edit",)
                event.app.exit()

        @kb.add("delete")
        def _del(event):
            if self.selected < len(self.entries) and not self.entries[self.selected].get("isAddRow"):
                self.pendingAction = ("delete",)
                event.app.exit()

        @kb.add("left")
        def _left(event):
            if self.selected < len(self.entries) and not self.entries[self.selected].get("isAddRow"):
                self.pendingAction = ("toggle",)
                event.app.exit()

        @kb.add("right")
        def _right(event):
            if self.selected < len(self.entries) and not self.entries[self.selected].get("isAddRow"):
                self.pendingAction = ("toggle",)
                event.app.exit()


    async def handlePendingAction(self):
        actionType = self.pendingAction[0]

        if actionType == "toggle":
            entry = self.entries[self.selected]
            await updateMemory(entry["id"], enabled=not entry["enabled"])
            await self.refreshEntries()

        elif actionType == "delete":
            entry = self.entries[self.selected]

            rmcup()
            sys.stdout.flush()

            try:
                confirm = await asyncInput(f"真的要删除 memory #{entry['id']} 吗？(y/N): ")
                if confirm.strip().lower() == "y":
                    ok = await deleteMemory(entry["id"])
                    print("✅ 已删除喵\n" if ok else "❌ 删除失败喵\n")
                    await asyncio.sleep(0.5)
                    await self.refreshEntries()
                    self.selected = min(self.selected, len(self.entries) - 1)
            finally:
                smcup()
                sys.stdout.flush()

        elif actionType == "add":
            rmcup()
            sys.stdout.flush()

            try:
                scopeInput = await asyncInput("Scope (global/chat/user/session) [global]: ")
                scopeType = scopeInput.strip().lower() or "global"

                if scopeType not in VALID_SCOPE_TYPES:
                    print(f"❌ 这个 scope 是无效的: {scopeType}\n")
                    await asyncio.sleep(0.5)
                    return True

                scopeID = None
                if scopeType != "global":
                    scopeID = (await asyncInput(f"Scope ID ({scopeType}): ")).strip()
                    if not scopeID:
                        print("❌ scope ID 是不能为空的喵\n")
                        await asyncio.sleep(0.5)
                        return True

                result = await editMemoryViaEditor()
                if result is None:
                    return True

                content, tags, priority = result
                memoryID = await addMemory(scopeType, scopeID, content, tags=tags, priority=priority)

                print(f"✅ memory #{memoryID} 成功添加喵\n" if memoryID else "❌ 添加失败喵\n")
                await asyncio.sleep(0.5)
                await self.refreshEntries()
            finally:
                smcup()
                sys.stdout.flush()

        elif actionType == "edit":
            entry = self.entries[self.selected]

            rmcup()
            sys.stdout.flush()

            try:
                result = await editMemoryViaEditor(
                    entry["content"],
                    entry["tags"],
                    entry["priority"],
                )
                if result is None:
                    return True

                newContent, newTags, newPriority = result

                updateKwargs = {}
                if newContent != entry["content"]:
                    updateKwargs["content"] = newContent
                if sorted(newTags) != sorted(entry["tags"] or []):
                    updateKwargs["tags"] = newTags
                if newPriority != entry["priority"]:
                    updateKwargs["priority"] = newPriority

                if updateKwargs:
                    ok = await updateMemory(entry["id"], **updateKwargs)
                    print("✅ 更新成功喵\n" if ok else "❌ 更新失败喵\n")
                    await asyncio.sleep(0.5)

                await self.refreshEntries()
            finally:
                smcup()
                sys.stdout.flush()

        return True




async def memoryMenuController(app=None):
    controller = MemoryTUIController(app=app, mode="manage")
    await controller.run()
