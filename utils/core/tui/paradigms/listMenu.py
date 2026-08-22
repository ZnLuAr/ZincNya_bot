"""
utils/core/tui/paradigms/listMenu.py

ListMenuController —— 列表 + 方向键型 TUI 范式。

屏幕模型：跑在 ANSI 备用屏幕（smcup/rmcup），整屏 redraw（用户每次按键后重绘整张表）。
输入模型：方向键上下移动、数字键拼接跳转、Enter 选择 / Esc 退出（manage 模式还支持增删改）。
终端尺寸口径：ANSI（terminalUI.getTerminalSize），不是 prompt_toolkit 的 output.get_size()——
后者是全屏 pt 布局裁剪后的高度，列表菜单没用 pt 全屏布局，必须取 ANSI 口径。

子类职责：实现 collectViewModel（收集条目）+ buildTable（构建表格本体）。渲染管线（窗口计算、
capture、↑↓ 指示、cls/print/flush）由本基类统一负责。
"""

import sys
from abc import abstractmethod
from typing import List, Optional, Tuple

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.widgets import TextArea
from rich.console import Console
from rich.table import Table

from utils.core.terminalUI import cls, smcup, rmcup, getTerminalSize
from utils.core.tui.session import TUISession




class ListMenuController(TUISession):
    """列表 + 方向键型 TUI 控制器。

    使用示例：
        class MyMenuController(ListMenuController):
            async def collectViewModel(self, selectedIndex):
                return entries, meta
            def buildTable(self, visibleEntries, selectedIndex, windowStart):
                ...

        controller = MyMenuController(bot=bot, app=app, mode="select")
        result = await controller.runSession()
    """

    def __init__(self, bot=None, app=None, mode: str = "select"):
        """
        参数:
            bot: Telegram Bot 实例（用于数据获取）
            app: Application 实例
            mode:
                - "select": 选择模式，Enter 返回选中项
                - "manage": 管理模式，支持增删改操作
        """
        super().__init__()  # TUISession：_stateManager + _backgroundTasks

        self.bot = bot
        self.app = app
        self.mode = mode

        self.selected = 0
        self.entries: List[dict] = []
        self.pendingAction: Optional[tuple] = None
        self.numberBuffer = ""

        # 数字跳转偏移量：由各 buildTable 的序号显示规则决定。
        #   - select 模式（无 (+) 行）：addRowOffset = 0，用户输入 1 → index 0
        #   - manage 模式（index 0 为 (+) 行）：子类置 1，用户输入 1 → index 1
        self.addRowOffset = 0


    # ========================================================================
    # 抽象方法（子类必填）
    # ========================================================================

    @abstractmethod
    async def collectViewModel(self, selectedIndex: int) -> Tuple[List[dict], dict]:
        """收集要显示的条目列表。返回 (entries, meta)，meta 含 {"selected": int, "count": int}。"""
        pass


    @abstractmethod
    def buildTable(self, visibleEntries: List[dict], selectedIndex: int, windowStart: int):
        """构建表格本体（标题/列/数据行）。

        只负责表格本身——不含帮助行（由 getHelpLine 钩子单独产）、不含 ↑↓ 指示行（由 renderUI
        管线的 renderMoreIndicators 单独产）。序号显示、选中标记 `>`、(+) 行都由本方法保证。

        参数:
            visibleEntries: 当前窗口可见的条目
            selectedIndex: 全局选中索引（globalIdx = windowStart + localIdx，用于选中标记）
            windowStart: 窗口起始索引
        返回:
            rich Table 对象（由 renderUI 管线 capture + 打印）
        """
        pass


    # ========================================================================
    # 钩子（子类按需覆写）
    # ========================================================================

    def getHelpLine(self):
        """帮助行（默认 None = 不显示）。返回 rich 标记字符串，由 renderUI 管线 capture 打印。"""
        return None


    async def handlePendingAction(self) -> bool:
        """处理待执行的操作（管理模式下由子类实现）。True 继续，False 退出。"""
        return True


    def setupExtraKeyBindings(self, kb: KeyBindings):
        """设置额外的键盘绑定（子类可重写以添加自定义按键）。"""
        pass


    def getEmptyMessage(self) -> str:
        """返回列表为空时的提示消息。"""
        return "列表为空喵……"


    def getExitMessage(self) -> str:
        """返回退出时的提示消息。"""
        return "退出菜单喵——\n"


    # ========================================================================
    # 渲染管线（具体，子类不覆写）
    # ========================================================================

    @staticmethod
    def calculateVisibleWindow(
        entries: List[dict],
        selectedIndex: int,
        terminalHeight: int,
        reservedLines: int = 8,
    ) -> Tuple[List[dict], int, dict]:
        """计算在终端高度限制下应该显示的条目窗口。

        返回 (visibleEntries, windowStart, hasMore)，hasMore = {"up": bool, "down": bool}。
        """
        maxVisibleRows = max(5, terminalHeight - reservedLines)

        if len(entries) <= maxVisibleRows:
            return entries, 0, {"up": False, "down": False}

        halfWindow = maxVisibleRows // 2
        windowStart = max(0, selectedIndex - halfWindow)
        windowEnd = min(len(entries), windowStart + maxVisibleRows)

        if windowEnd - windowStart < maxVisibleRows:
            windowStart = max(0, windowEnd - maxVisibleRows)

        visibleEntries = entries[windowStart:windowEnd]
        hasMore = {
            "up": windowStart > 0,
            "down": windowEnd < len(entries),
        }

        return visibleEntries, windowStart, hasMore


    @staticmethod
    def getTerminalHeight() -> int:
        """获取终端高度（ANSI 口径）。getTerminalSize 自带回退 (80, 24)。"""
        return getTerminalSize()[1]


    @staticmethod
    def renderMoreIndicators(
        console: Console, hasMore: dict, windowStart: int, totalCount: int, visibleCount: int
    ) -> List[str]:
        """渲染 ↑/↓ 更多提示，返回额外的行列表。"""
        extraLines = []

        if hasMore["up"]:
            extraLines.append(f"[dim]↑ 更多 {windowStart} 项[/dim]")

        if hasMore["down"]:
            remainingDown = totalCount - (windowStart + visibleCount)
            extraLines.append(f"[dim]↓ 更多 {remainingDown} 项[/dim]")

        if not extraLines:
            return []

        with console.capture() as capture:
            for line in extraLines:
                console.print(line)
        return capture.get().splitlines()


    @staticmethod
    def _captureLines(console: Console, printFn) -> List[str]:
        """capture 一段 rich 输出，返回逐行列表。"""
        with console.capture() as capture:
            printFn()
        return capture.get().splitlines()


    @staticmethod
    def drawLines(lines: List[str]) -> int:
        """清屏 + 逐行打印 + flush，返回打印行数。"""
        cls()
        for ln in lines:
            print(ln)
        sys.stdout.flush()
        return len(lines)


    def renderUI(self, entries: List[dict], selectedIndex: int) -> int:
        """渲染管线：表格 → ↑↓ 指示 → 帮助行 → 清屏打印。返回打印行数。"""
        terminalHeight = ListMenuController.getTerminalHeight()
        visibleEntries, windowStart, hasMore = ListMenuController.calculateVisibleWindow(
            entries, selectedIndex, terminalHeight
        )

        console = Console()
        table = self.buildTable(visibleEntries, selectedIndex, windowStart)
        lines = ListMenuController._captureLines(console, lambda: console.print(table))

        lines.extend(ListMenuController.renderMoreIndicators(
            console, hasMore, windowStart, len(entries), len(visibleEntries)
        ))

        helpLine = self.getHelpLine()
        if helpLine is not None:
            lines.extend(ListMenuController._captureLines(console, lambda: console.print(helpLine)))

        return ListMenuController.drawLines(lines)


    # ========================================================================
    # 会话生命周期（TUISession 钩子实现）
    # ========================================================================

    async def onEnter(self):
        # 占位空行 + 切换到备用屏幕缓冲区
        print()
        smcup()
        sys.stdout.flush()
        # 初始化数据
        await self.refreshEntries()


    async def mainLoop(self):
        isManageMode = (self.mode == "manage")

        # 空列表短路（选择模式）：打提示直接退。
        # self._skipExitSpacing = True 告知 onExit 跳过装饰空行与退出消息——
        # 空列表场景没进过全屏渲染，onEnter 的占位空行已提供与后续提示的分隔
        if not self.entries and not isManageMode:
            self._skipExitSpacing = True
            print(self.getEmptyMessage())
            return None

        # 初次渲染
        self.redraw()

        # 配置键盘绑定
        kb = KeyBindings()
        self._setupBaseKeyBindings(kb, isManageMode)
        self.setupExtraKeyBindings(kb)

        # 主循环
        while True:
            ptApp = Application(
                layout=Layout(TextArea(text="", focus_on_click=False)),
                key_bindings=kb,
                full_screen=False,
            )
            await ptApp.run_async()

            # 用户按 Esc 取消
            if self.selected == -1:
                return None

            # 选择模式：直接返回
            if not isManageMode:
                return self.getSelectedEntry()

            # 管理模式：处理待执行操作
            if self.pendingAction is not None:
                shouldContinue = await self.handlePendingAction()
                self.pendingAction = None
                if not shouldContinue:
                    return None
                self.redraw()


    async def onExit(self):
        # 切回主屏幕缓冲区（session.runSession 已 try/except 兜住，rmcup 抛 OSError 不阻断收尾）
        rmcup()
        sys.stdout.flush()
        # 空列表短路场景（mainLoop 设置 _skipExitSpacing）没进过全屏，跳过收尾输出；
        # 其余场景在主屏补退出消息 + 空行（manage 操作的输入提示写在主屏会残留，
        # 空行负责与后续控制台输出分隔）。退出消息原先打在 _esc（备用屏上，rmcup 即丢）
        if not getattr(self, "_skipExitSpacing", False):
            print(self.getExitMessage())


    # ========================================================================
    # 内部工具
    # ========================================================================

    async def refreshEntries(self):
        """刷新数据。"""
        self.entries, _ = await self.collectViewModel(self.selected)


    def redraw(self):
        """重新渲染界面。"""
        self.renderUI(self.entries, self.selected)


    def _setupBaseKeyBindings(self, kb: KeyBindings, isManageMode: bool):
        """设置基础键盘绑定（上下/Esc/数字跳转/select 的 Enter）。"""

        @kb.add("up")
        def _up(event):
            self.numberBuffer = ""
            self.selected = max(0, self.selected - 1)
            self.redraw()

        @kb.add("down")
        def _down(event):
            self.numberBuffer = ""
            self.selected = min(len(self.entries) - 1, self.selected + 1)
            self.redraw()

        @kb.add("escape")
        def _esc(event):
            self.selected = -1
            event.app.exit()

        # 数字键 0-9：实时拼接并跳转到对应显示序号。
        # 显示序号由各 buildTable 保证（(+) 行不占序号，普通条目从 1 起连续编号）。
        # 跳转规则：用户输入序号 N → 内部 index = N - 1 + self.addRowOffset。
        for digit in "0123456789":
            @kb.add(digit)
            def _digit(event, d=digit):
                candidate = self.numberBuffer + d
                index = int(candidate) - 1 + self.addRowOffset
                if 0 <= index < len(self.entries):
                    self.numberBuffer = candidate
                    self.selected = index
                    self.redraw()
                else:
                    # 拼接后越界，将新数字单独作为新一轮输入
                    singleIndex = int(d) - 1 + self.addRowOffset
                    if 0 <= singleIndex < len(self.entries):
                        self.numberBuffer = d
                        self.selected = singleIndex
                        self.redraw()

        # 选择模式的 Enter
        if not isManageMode:
            @kb.add("enter")
            def _enter(event):
                event.app.exit()


    def getSelectedEntry(self):
        """获取当前选中的条目。"""
        if self.entries and 0 <= self.selected < len(self.entries):
            return self.entries[self.selected]
        return None
