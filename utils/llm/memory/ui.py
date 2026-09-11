"""
utils/llm/memory/ui.py

Memory TUI 管理界面。

提供：
    - editMemoryViaEditor: 编辑器 + 头部元数据解析
    - MemoryTUIController: 交互式 TUI 控制器
    - memoryMenuController: 工厂函数
"""

import os
import asyncio
import tempfile
from typing import Optional

from rich.table import Table

from utils.core.tui import ListMenuController, editFile
from utils.inputHelper import asyncInput

from .database import (
    addMemory, getMemories, updateMemory, deleteMemory,
    MEMORY_MODE_CONTEXTUAL, MEMORY_SCOPE_GLOBAL, MEMORY_SCOPE_RANK,
    VALID_MEMORY_MODES, VALID_SCOPE_TYPES,
)


# memory 列表预览截断长度
_LIST_PREVIEW_LEN = 40

# 操作结果提示在屏幕上的停留时间（秒）
_ACTION_NOTICE_DELAY = 0.5




async def editMemoryViaEditor(
    initialContent: str = "",
    initialTags: Optional[list] = None,
    initialPriority: int = 0,
    initialMode: str = MEMORY_MODE_CONTEXTUAL,
    initialRetrievalHint: Optional[str] = None,
) -> Optional[tuple]:
    """
    通过编辑器编辑 memory 内容和元数据。

    文件格式：
        # tags: tag1, tag2
        # priority: 5
        # mode: contextual
        # hint: related wording
        ---
        content here

    返回:
        (content, tags, priority, mode, retrievalHint) 或 None（取消/内容为空）
    """
    tagsStr = ", ".join(initialTags) if initialTags else ""

    template = (
        f"# tags: {tagsStr}\n"
        f"# priority: {initialPriority}\n"
        f"# mode: {initialMode}\n"
        f"# hint: {initialRetrievalHint or ''}\n"
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
    mode = initialMode
    retrievalHint = initialRetrievalHint
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

        elif stripped.startswith("# mode:"):
            raw = stripped[len("# mode:"):].strip().lower()
            if raw in VALID_MEMORY_MODES:
                mode = raw

        elif stripped.startswith("# hint:"):
            retrievalHint = stripped[len("# hint:"):].strip()

    # 分隔符之后为 content
    if separatorIdx is not None:
        contentLines = lines[separatorIdx + 1:]
    else:
        contentLines = lines

    content = "\n".join(contentLines).strip()
    if not content:
        return None

    return content, tags, priority, mode, retrievalHint




class MemoryTUIController(ListMenuController):
    """
    Structured memory 的交互式 TUI 管理控制器。

    继承 ListMenuController，支持：
        - 浏览全部 memory 条目（含已禁用）
        - Enter 添加/编辑（通过 editMemoryViaEditor 在编辑器中填写内容和元数据）
        - ←/→ 快速切换 enabled 状态
        - Delete 删除（需确认）

    入口：memoryMenuController()
    """

    def __init__(self, **kwargs):
        """初始化管理列表，并为固定的“添加”行设置数字跳转偏移。"""
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

        rows = sorted(
            rows,
            key=lambda r: (
                MEMORY_SCOPE_RANK.get(r["scope_type"], 99),
                -r["priority"],
                -r["id"],
            ),
        )

        entries = []
        entries.append({"isAddRow": True})

        for row in rows:
            preview = row["content"].replace("\n", " ")
            if len(preview) > _LIST_PREVIEW_LEN:
                preview = preview[:_LIST_PREVIEW_LEN] + "…"

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
                "mode": row.get("mode", MEMORY_MODE_CONTEXTUAL),
                "retrievalHint": row.get("retrievalHint"),
                "preview": preview,
            })

        meta = {
            "selected": max(0, min(selectedIndex, len(entries) - 1)) if entries else 0,
        }

        return entries, meta


    def buildTable(self, visibleEntries, selectedIndex, windowStart):
        """渲染包含 scope、priority、mode、hint 与启用状态的管理表格。"""
        table = Table(title="Memory 管理")
        table.add_column("No.", justify="right")
        table.add_column("ID", justify="right")
        table.add_column("Scope", justify="left")
        table.add_column("P", justify="right")
        table.add_column("Mode", justify="left")
        table.add_column("Hint", justify="left")
        table.add_column("Status", justify="center")
        table.add_column("Preview", justify="left")

        for localIdx, e in enumerate(visibleEntries):
            globalIdx = windowStart + localIdx
            isSelected = (globalIdx == selectedIndex)
            isAddRow = e.get("isAddRow", False)
            displayNo = globalIdx - self.addRowOffset + 1

            if isAddRow:
                if isSelected:
                    table.add_row("[bold yellow]>[/]", "", "[bold yellow](+) 添加[/]", "", "", "", "", "")
                else:
                    table.add_row("", "", "[cyan](+) 添加[/]", "", "", "", "", "")
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
                        f"[bold yellow]{e.get('mode', MEMORY_MODE_CONTEXTUAL)}[/]",
                        f"[bold yellow]{e.get('retrievalHint') or '-'}[/]",
                        f"[bold yellow]{'ON' if e['enabled'] else 'OFF'}[/]",
                        f"[bold yellow]{preview}[/]",
                    )
                elif not e['enabled']:
                    table.add_row(
                        f"[dim]{displayNo}[/]",
                        f"[dim]{e['id']}[/]",
                        f"[dim]{scopeStr}[/]",
                        f"[dim]{e['priority']}[/]",
                        f"[dim]{e.get('mode', MEMORY_MODE_CONTEXTUAL)}[/]",
                        f"[dim]{e.get('retrievalHint') or '-'}[/]",
                        "[red]OFF[/red]",
                        f"[dim]{preview}[/]",
                    )
                else:
                    table.add_row(
                        str(displayNo),
                        str(e['id']),
                        scopeStr,
                        str(e['priority']),
                        e.get('mode', MEMORY_MODE_CONTEXTUAL),
                        e.get('retrievalHint') or "-",
                        "[green]ON[/green]",
                        preview,
                    )

        return table


    def getHelpLine(self):
        """返回管理模式底部快捷键提示。"""
        return "\n[dim]Enter 编辑 | Del 删除 | ←→ 启用/停用 | Esc 退出[/dim]"


    def getEmptyMessage(self):
        """返回无 memory 条目时的占位文本。"""
        return "Memory 列表为空喵……\n"


    def getExitMessage(self):
        """返回退出管理界面后的提示文本。"""
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
                self.safeAppExit(event.app)

        @kb.add("delete")
        def _del(event):
            if self.selected < len(self.entries) and not self.entries[self.selected].get("isAddRow"):
                self.pendingAction = ("delete",)
                self.safeAppExit(event.app)

        @kb.add("left")
        def _left(event):
            if self.selected < len(self.entries) and not self.entries[self.selected].get("isAddRow"):
                self.pendingAction = ("toggle",)
                self.safeAppExit(event.app)

        @kb.add("right")
        def _right(event):
            if self.selected < len(self.entries) and not self.entries[self.selected].get("isAddRow"):
                self.pendingAction = ("toggle",)
                self.safeAppExit(event.app)


    async def handlePendingAction(self):
        """执行按键阶段登记的增删改或启停动作，并刷新列表快照。"""
        actionType = self.pendingAction[0]

        if actionType == "toggle":
            entry = self.entries[self.selected]
            await updateMemory(entry["id"], enabled=not entry["enabled"])
            await self.refreshEntries()

        elif actionType == "delete":
            entry = self.entries[self.selected]

            confirm = await self.runChildSession(
                asyncInput(f"真的要删除 memory #{entry['id']} 吗？(y/N): ")
            )
            if confirm.strip().lower() == "y":
                ok = await deleteMemory(entry["id"])
                print("已删除\n" if ok else "❌ 删除失败\n")
                await asyncio.sleep(_ACTION_NOTICE_DELAY)
                await self.refreshEntries()
                self.selected = min(self.selected, len(self.entries) - 1)

        elif actionType == "add":
            scopeInput = await self.runChildSession(
                asyncInput("Scope (global/chat/user/session) [global]: ")
            )
            scopeType = scopeInput.strip().lower() or "global"

            if scopeType not in VALID_SCOPE_TYPES:
                print(f"❌ 这个 {scopeType} 的 scope 是无效的说\n")
                await asyncio.sleep(_ACTION_NOTICE_DELAY)
                return True

            scopeID = None
            if scopeType != "global":
                scopeID = (await self.runChildSession(
                    asyncInput(f"Scope ID ({scopeType}): ")
                )).strip()
                if not scopeID:
                    print("❌ scope ID 是不能为空的\n")
                    await asyncio.sleep(_ACTION_NOTICE_DELAY)
                    return True

            result = await self.runChildSession(editMemoryViaEditor())
            if result is None:
                return True

            content, tags, priority, mode, retrievalHint = result
            memoryID = await addMemory(
                scopeType,
                scopeID,
                content,
                tags=tags,
                priority=priority,
                mode=mode,
                retrievalHint=retrievalHint,
            )

            print(f"memory #{memoryID} 成功添加\n" if memoryID else "❌ 添加失败\n")
            await asyncio.sleep(_ACTION_NOTICE_DELAY)
            await self.refreshEntries()

        elif actionType == "edit":
            entry = self.entries[self.selected]

            result = await self.runChildSession(editMemoryViaEditor(
                entry["content"],
                entry["tags"],
                entry["priority"],
                entry.get("mode", MEMORY_MODE_CONTEXTUAL),
                entry.get("retrievalHint"),
            ))
            if result is None:
                return True

            newContent, newTags, newPriority, newMode, newRetrievalHint = result

            # 只传递真实变化的字段：未传 hint 表示保留，空字符串才表示清空。
            updateKwargs = {}
            if newContent != entry["content"]:
                updateKwargs["content"] = newContent
            if sorted(newTags) != sorted(entry["tags"] or []):
                updateKwargs["tags"] = newTags
            if newPriority != entry["priority"]:
                updateKwargs["priority"] = newPriority
            if newMode != entry.get("mode", MEMORY_MODE_CONTEXTUAL):
                updateKwargs["mode"] = newMode
            if (newRetrievalHint or None) != entry.get("retrievalHint"):
                updateKwargs["retrievalHint"] = newRetrievalHint

            if updateKwargs:
                ok = await updateMemory(entry["id"], **updateKwargs)
                print("更新成功\n" if ok else "❌ 更新失败\n")
                await asyncio.sleep(_ACTION_NOTICE_DELAY)

            await self.refreshEntries()

        return True




async def memoryMenuController(app=None):
    """创建并运行 Structured Memory 的交互式管理会话。"""
    controller = MemoryTUIController(app=app, mode="manage")
    await controller.runSession()
