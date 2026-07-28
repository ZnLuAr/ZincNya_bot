"""
utils/chatScreen/ui.py

固定底部输入的聊天界面控制器（fullScreen 范式的复杂实例）。

布局：
    - 上方：聊天记录区（TextArea，read_only，自动滚动到底部）
    - 中间：分隔线
    - 下方：多行输入区（TextArea，可编辑）
    - 最底：状态栏

关键设计：
    - 聊天记录区用 TextArea + read_only=True，通过 buffer.set_document() 更新
    - _scrollOffset 控制滚动：0 = 跟随最新，>0 = 向上偏移
    - 用 buffer.cursor_position 控制显示位置实现滚动
    - 消息到来时延迟重绘，避免打断输入框渲染

继承 FullScreenTUIApp：app 在 __init__ 构造（createApplication 覆写留本文件，保 patch 目标
utils.chatScreen.ui.Application）；单轮入口 runOnce（保留 _exitRequested 守卫）；mainLoop
委托聊天主循环；onEnter/onExit 自管 console 回调 + receiver（chatScreen 专属，不进基类）。
"""

import asyncio
import sys

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.widgets import TextArea

from utils.core.stateManager import getStateManager
from utils.core.tui.paradigms.fullScreen import FullScreenTUIApp




class ChatScreenApp(FullScreenTUIApp):
    """固定底部输入的全屏聊天界面。"""

    def __init__(self, targetChatID: str, bot=None, shutdownEvent=None, initialLines: list[str] | None = None):
        # statusBar 函数（lazy import，避免每次 _defaultStatus/_updateStatus 重复导入）
        from .statusBar import getDefaultStatus, getHistoryBrowsingStatus
        self._getDefaultStatusFn = getDefaultStatus
        self._getHistoryBrowsingStatusFn = getHistoryBrowsingStatus

        # 存构造参数（bot/shutdownEvent 带默认值，保 30 个单参构造测试兼容）
        self._targetChatID = targetChatID
        self._bot = bot
        self._shutdownEvent = shutdownEvent
        self._exitRequested = False
        self._pendingNewMessages = 0
        self._switchDirection: str | None = None  # "prev" / "next" / None

        # ── 布局组件（buildLayout / createApplication 要用，须在 super().__init__() 前就位）──
        self._transcriptArea = TextArea(
            text="",
            multiline=True,
            scrollbar=True,
            wrap_lines=True,
            focus_on_click=False,
            read_only=True,
        )
        self._transcriptWindow = Window(
            content=self._transcriptArea.control,
            height=Dimension(min=5),
            wrap_lines=True,
        )

        self._composerArea = TextArea(
            text="",
            multiline=True,
            wrap_lines=True,
            focus_on_click=True,
        )
        self._composerWindow = Window(
            content=self._composerArea.control,
            height=Dimension(min=3, max=10, preferred=5),
        )

        self._statusText = self._defaultStatus()
        self._statusBar = Window(
            content=FormattedTextControl(
                text=lambda: [("reverse", self._statusText.ljust(self._getTermWidth()))],
                focusable=False,
            ),
            height=1,
        )

        # ── 历史记录（__init__ 灌入 _allLines；onEnter 时初次刷新）──
        self._allLines: list[str] = []
        self._scrollOffset: int = 0  # 0 = 底部（最新），>0 = 向上偏移行数
        if initialLines:
            for line in initialLines:
                self._allLines.extend(line.split('\n'))

        # super 链：FullScreenTUIApp.__init__ → 调 self.createApplication() 构造 _app（组件已就位）
        super().__init__()


    # ========================================================================
    # FullScreenTUIApp 抽象方法 / 覆写
    # ========================================================================

    def buildLayout(self):
        return HSplit([
            self._transcriptWindow,
            Window(height=1, char="─"),
            self._composerWindow,
            self._statusBar,
        ])


    def setupKeyBindings(self, kb: KeyBindings):
        @kb.add("c-s")
        @kb.add("escape", "enter")
        def _submit(event):
            event.app.exit(result=self._composerArea.text)

        @kb.add("c-c")
        @kb.add("escape")
        def _cancel(event):
            self._exitRequested = True
            event.app.exit(result=None)

        @kb.add("c-x", eager=True)
        def _clear(event):
            self._composerArea.text = ""

        @kb.add("pageup")
        def _pageUp(event):
            self._scrollUp(self._getWindowHeight())

        @kb.add("pagedown")
        def _pageDown(event):
            self._scrollDown(self._getWindowHeight())

        @kb.add("c-up")
        @kb.add("escape", "up")
        def _lineUp(event):
            self._scrollUp(1)

        @kb.add("c-down")
        @kb.add("escape", "down")
        def _lineDown(event):
            self._scrollDown(1)

        @kb.add("escape", "left")
        def _switchPrev(event):
            """Alt+← 切换到上一个聊天对象"""
            self._switchDirection = "prev"
            self._exitRequested = True
            event.app.exit(result=None)

        @kb.add("escape", "right")
        def _switchNext(event):
            """Alt+→ 切换到下一个聊天对象"""
            self._switchDirection = "next"
            self._exitRequested = True
            event.app.exit(result=None)


    def createApplication(self) -> Application:
        """覆写基类默认模板：保 patch 目标 utils.chatScreen.ui.Application + 用 Vt100_Output。

        非 @abstractmethod（基类已有默认模板）；此处覆写以引用本文件的 Application 符号
        （测试 patch 命中）+ 装配 focused_element / mouse_support / Vt100_Output。

        注意 output= 是显式指定的，绕过了 pt 的平台探测（create_output 在 Windows 上会按
        VT 是否开启选 Windows10_Output / ConEmuOutput / Win32Output）。代价是 Windows 10
        及以下的传统 PowerShell / conhost 里滚动会整屏错位——已知局限，刻意不修（换现代终端
        即可，详见 docs/tui.md）。要修的话去掉 output= 这一行交还给 pt 探测即可。
        """
        kb = KeyBindings()
        self.setupKeyBindings(kb)
        self._app = Application(
            layout=Layout(self.buildLayout(), focused_element=self._composerWindow),
            key_bindings=kb,
            full_screen=True,
            mouse_support=False,
            output=Vt100_Output.from_pty(sys.stdout),
        )
        return self._app


    async def onEnter(self):
        # ① 注册控制台输出回调（chatScreen 专属：把 logger 输出路由到 UI transcript）
        self._stateManager.setConsoleOutputCallback(self._appendConsoleOutput)
        # ② 启动 receiver 并登记（必须在①之后，保 callback-before-receiver）
        from .receiver import startReceiver
        queue = self._stateManager.getMessageQueue()
        shutdownEvent = self._shutdownEvent or getStateManager().getShutdownEvent()
        task = await startReceiver(self._stateManager, self._targetChatID, self, queue, shutdownEvent)
        self.addBackgroundTask(task)
        await asyncio.sleep(0.1)
        # ③ 初次刷新（画 __init__ 已灌入 _allLines 的内容）
        self._refreshTranscript()


    async def onExit(self):
        # 注销控制台输出回调（与 listMenu.onExit=rmcup 对称，各范式收自己的尾）
        self._stateManager.setConsoleOutputCallback(None)


    async def runOnce(self):
        # 覆写基类 runOnce：保留 _exitRequested 守卫（不能照搬基类直接 return result，
        # 否则 exit 信号丢失，主循环的 if userInput is None 永不触发，聊天退不出）
        result = await self._app.run_async()
        # 退出全屏后重置终端状态，清除残留的状态栏
        self._app.output.reset_attributes()
        self._app.output.flush()
        return None if self._exitRequested else result


    async def mainLoop(self):
        # 委托聊天主循环（while 循环 + 审核命令分发 + 发送留在 mainLoop.py 不动）
        from .mainLoop import runMainLoop
        return await runMainLoop(
            self._bot,
            self._targetChatID,
            self,
            self._shutdownEvent or getStateManager().getShutdownEvent(),
        )


    def _appendConsoleOutput(self, text: str):
        """控制台输出回调：把 logger 文本拆行追加到 transcript。"""
        lines = text.rstrip('\n').split('\n')
        self.appendLines(lines)


    # ========================================================================
    # 对外 API
    # ========================================================================

    def appendLines(self, lines: list[str]):
        """追加多行文本到聊天记录区。"""
        for line in lines:
            self._allLines.extend(line.split('\n'))

        if self._scrollOffset == 0:
            self._pendingNewMessages = 0
        else:
            self._pendingNewMessages += 1

        self._updateStatus()
        self._refreshTranscript()


    def appendIncomingMessage(self, timestamp: str, sender: str, text: str):
        self.appendLines(self._formatMessageLines(timestamp, sender, text))


    def appendSelfMessage(self, timestamp: str, sender: str, text: str):
        self.appendLines(self._formatMessageLines(timestamp, sender, text))


    def clearComposer(self):
        self._composerArea.text = ""


    def showStatus(self, text: str):
        self._statusText = text
        self._app.invalidate()


    def requestExit(self):
        self._exitRequested = True
        try:
            self._app.exit(result=None)
        except Exception:
            pass


    def resetExitFlag(self):
        """重置退出标记（编辑模式中 Esc 取消编辑时调用，避免误退出聊天界面）。"""
        self._exitRequested = False


    # ========================================================================
    # 内部方法
    # ========================================================================

    _fmtLinesFn = None

    @classmethod
    def _formatMessageLines(cls, timestamp: str, sender: str, text: str) -> list[str]:
        if cls._fmtLinesFn is None:
            from .formatter import formatMessageLines
            cls._fmtLinesFn = formatMessageLines
        return cls._fmtLinesFn(timestamp, sender, text)


    def _getTermWidth(self) -> int:
        try:
            return self._app.output.get_size().columns
        except Exception:
            return 80


    def _defaultStatus(self) -> str:
        return self._getDefaultStatusFn(self._targetChatID)


    def _updateStatus(self):
        if self._scrollOffset == 0:
            self._statusText = self._getDefaultStatusFn(self._targetChatID)
        else:
            self._statusText = self._getHistoryBrowsingStatusFn(
                self._scrollOffset,
                self._pendingNewMessages,
                self._targetChatID
            )
        self._app.invalidate()


    def _getWindowHeight(self) -> int:
        """获取 transcript 区的实际渲染高度（行数）。"""
        render_info = self._transcriptWindow.render_info
        if render_info is not None:
            return max(1, render_info.window_height)
        try:
            size = self._app.output.get_size()
            return max(5, size.rows - 7)
        except Exception:
            return 20


    def _scrollUp(self, lines: int):
        """向上滚动（查看历史）。"""
        self._scrollOffset += lines
        self._clampOffset()
        self._updateStatus()
        self._refreshTranscript()


    def _scrollDown(self, lines: int):
        """向下滚动（回到最新）。"""
        self._scrollOffset = max(0, self._scrollOffset - lines)
        self._updateStatus()
        self._refreshTranscript()


    def _clampOffset(self):
        """确保 offset 不超过可滚动范围。"""
        windowHeight = self._getWindowHeight()
        total = len(self._allLines)
        max_offset = max(0, total - windowHeight)
        self._scrollOffset = min(self._scrollOffset, max_offset)


    def _refreshTranscript(self):
        """
        刷新聊天记录显示。
        根据 _scrollOffset 计算应显示的内容，通过 buffer.set_document 更新。
        """
        windowHeight = self._getWindowHeight()
        total = len(self._allLines)

        # 计算可见范围
        if self._scrollOffset == 0:
            # 跟随最新：显示最后 windowHeight 行
            start = max(0, total - windowHeight)
            visibleLines = self._allLines[start:]
        else:
            # 历史浏览：从底部往上偏移 _scrollOffset
            end = max(0, total - self._scrollOffset)
            start = max(0, end - windowHeight)
            visibleLines = self._allLines[start:end]

        # 确保显示固定行数，顶部不足补空行
        while len(visibleLines) < windowHeight:
            visibleLines.insert(0, "")

        text = "\n".join(visibleLines)

        # 更新 Buffer（read_only=True 时需要用 bypass_readonly）
        buf = self._transcriptArea.buffer
        buf.set_document(Document(text=text), bypass_readonly=True)

        # 设置光标位置到底部（让视图显示最下方）
        buf.cursor_position = len(text)

        self._app.invalidate()
