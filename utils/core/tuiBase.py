"""
utils/core/tuiBase.py

终端用户界面（TUI）控制器基类，用于减少 whitelistManager 和 nyaQuoteManager 的代码重复。

提供：
- 备用屏幕管理（smcup/rmcup）
- 键盘事件绑定框架
- 可视窗口计算（长列表滚动）
- 异步刷新/重绘协调
- 交互模式状态管理
"""

import sys
import shutil
from abc import ABC, abstractmethod
from typing import List, Tuple, Optional, Any

from rich.table import Table
from rich.console import Console

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.widgets import TextArea

from utils.terminalUI import cls, smcup, rmcup
from utils.core.stateManager import getStateManager




class BaseTUIController(ABC):
    """
    TUI 控制器基类

    使用示例：
        class MyMenuController(BaseTUIController):
            async def collectViewModel(self, selectedIndex):
                # 返回 (entries, meta)
                pass

            def renderUI(self, entries, selectedIndex):
                # 渲染界面，返回渲染高度
                pass

        controller = MyMenuController(bot=bot, app=app, mode="select")
        result = await controller.run()
    """

    def __init__(self, bot=None, app=None, mode: str = "select"):
        """
        参数:
            bot: Telegram Bot 实例（用于数据获取）
            app: Application 实例（用于设置交互模式标志）
            mode:
                - "select": 选择模式，Enter 返回选中项
                - "manage": 管理模式，支持增删改操作
        """
        self.bot = bot
        self.app = app
        self.mode = mode

        self.selected = 0
        self.prevHeight = 0
        self.entries: List[dict] = []
        self.pendingAction: Optional[tuple] = None


    # ========================================================================
    # 抽象方法
    # ========================================================================

    @abstractmethod
    async def collectViewModel(self, selectedIndex: int) -> Tuple[List[dict], dict]:
        """
        收集要显示的条目列表

        返回:
            (entries, meta)
            - entries: 条目列表，每个条目是一个字典
            - meta: 元数据，包含 {"selected": int, "count": int}
        """
        pass


    @abstractmethod
    def renderUI(self, entries: List[dict], selectedIndex: int) -> int:
        """
        渲染界面

        参数:
            entries: 条目列表
            selectedIndex: 当前选中的索引

        返回:
            渲染的行数（用于下次清屏）
        """
        pass


    # ========================================================================
    # 可选重写方法
    # ========================================================================

    async def handlePendingAction(self) -> bool:
        """
        处理待执行的操作（管理模式下由子类实现）

        返回:
            True 表示继续循环，False 表示退出
        """
        return True


    def setupExtraKeyBindings(self, kb: KeyBindings):
        """
        设置额外的键盘绑定（子类可重写以添加自定义按键）

        参数:
            kb: KeyBindings 实例
        """
        pass


    def getEmptyMessage(self) -> str:
        """返回列表为空时的提示消息"""
        return "列表为空喵……"


    def getExitMessage(self) -> str:
        """返回退出时的提示消息"""
        return "退出菜单喵——\n"


    # ========================================================================
    # 公共工具方法
    # ========================================================================

    @staticmethod
    def calculateVisibleWindow(
        entries: List[dict],
        selectedIndex: int,
        terminalHeight: int,
        reservedLines: int = 8
    ) -> Tuple[List[dict], int, dict]:
        """
        计算在终端高度限制下应该显示的条目窗口

        参数:
            entries: 完整条目列表
            selectedIndex: 当前选中索引
            terminalHeight: 终端高度
            reservedLines: 预留行数（标题、表头、提示等）

        返回:
            (visibleEntries, windowStart, hasMore)
            - visibleEntries: 应该显示的条目列表
            - windowStart: 窗口起始索引
            - hasMore: {"up": bool, "down": bool} 是否有更多项
        """
        maxVisibleRows = max(5, terminalHeight - reservedLines)

        # 如果条目少于可见行数，全部显示
        if len(entries) <= maxVisibleRows:
            return entries, 0, {"up": False, "down": False}

        # 让选中项居中显示
        halfWindow = maxVisibleRows // 2
        windowStart = max(0, selectedIndex - halfWindow)
        windowEnd = min(len(entries), windowStart + maxVisibleRows)

        # 调整窗口确保填满可见区域
        if windowEnd - windowStart < maxVisibleRows:
            windowStart = max(0, windowEnd - maxVisibleRows)

        visibleEntries = entries[windowStart:windowEnd]
        hasMore = {
            "up": windowStart > 0,
            "down": windowEnd < len(entries)
        }

        return visibleEntries, windowStart, hasMore


    def getTerminalHeight(self) -> int:
        """获取终端高度"""
        try:
            return shutil.get_terminal_size().lines
        except:
            return 24


    def renderMoreIndicators(self, console: Console, hasMore: dict, windowStart: int, totalCount: int, visibleCount: int) -> List[str]:
        """
        渲染 ↑/↓ 更多提示

        返回:
            额外的行列表
        """
        extraLines = []

        if hasMore["up"]:
            extraLines.append(f"[dim]↑ 更多 {windowStart} 项[/dim]")

        if hasMore["down"]:
            remainingDown = totalCount - (windowStart + visibleCount)
            extraLines.append(f"[dim]↓ 更多 {remainingDown} 项[/dim]")

        if not extraLines:
            return []

        lines = []
        with console.capture() as capture:
            for line in extraLines:
                console.print(line)
        lines.extend(capture.get().splitlines())

        return lines


    # ========================================================================
    # 核心运行逻辑
    # ========================================================================

    async def refreshEntries(self):
        """刷新数据"""
        self.entries, _ = await self.collectViewModel(self.selected)


    def redraw(self):
        """重新渲染界面"""
        self.prevHeight = self.renderUI(self.entries, self.selected)


    async def run(self) -> Optional[Any]:
        """
        运行交互式菜单

        返回:
            - 选择模式：返回选中的条目或 None
            - 管理模式：始终返回 None
        """
        # 留一个空行占位
        print()

        # 切换到备用屏幕缓冲区
        smcup()
        sys.stdout.flush()

        # 设置交互模式标志
        getStateManager().setInteractiveMode(True)

        isManageMode = (self.mode == "manage")

        try:
            # 初始化数据
            await self.refreshEntries()

            # 检查是否为空
            if not self.entries:
                if not isManageMode:
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
                    full_screen=False
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

        finally:
            # 切回主屏幕缓冲区
            rmcup()
            sys.stdout.flush()

            # 恢复交互模式状态
            getStateManager().setInteractiveMode(False)


    def _setupBaseKeyBindings(self, kb: KeyBindings, isManageMode: bool):
        """设置基础键盘绑定"""

        @kb.add("up")
        def _up(event):
            self.selected = max(0, self.selected - 1)
            self.redraw()

        @kb.add("down")
        def _down(event):
            self.selected = min(len(self.entries) - 1, self.selected + 1)
            self.redraw()

        @kb.add("escape")
        def _esc(event):
            self.selected = -1
            print(self.getExitMessage())
            event.app.exit()

        # 选择模式的 Enter
        if not isManageMode:
            @kb.add("enter")
            def _enter(event):
                event.app.exit()


    def getSelectedEntry(self) -> Optional[dict]:
        """获取当前选中的条目"""
        if self.entries and 0 <= self.selected < len(self.entries):
            return self.entries[self.selected]
        return None
