"""
utils/chatUI.py

固定底部输入的聊天界面控制器。

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
"""

import sys
from prompt_toolkit.widgets import TextArea
from prompt_toolkit.document import Document
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl




class ChatScreenApp:

    def __init__(self, targetChatID: str, initialLines: list[str] | None = None):
        self._targetChatID = targetChatID
        self._exitRequested = False
        self._pendingNewMessages = 0

        # ── 聊天记录区（只读 TextArea）──
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

        # ── 输入区 ──
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

        # ── 状态栏 ──
        self._statusText = self._defaultStatus()
        self._statusBar = Window(
            content=FormattedTextControl(
                text=lambda: [("reverse", self._statusText.ljust(self._getTermWidth()))],
                focusable=False,
            ),
            height=1,
        )

        # ── 快捷键 ──
        kb = KeyBindings()

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
        def _lineUp(event):
            self._scrollUp(1)

        @kb.add("c-down")
        def _lineDown(event):
            self._scrollDown(1)

        # ── 布局 ──
        body = HSplit([
            self._transcriptWindow,
            Window(height=1, char="─"),
            self._composerWindow,
            self._statusBar,
        ])

        self._app = Application(
            layout=Layout(body, focused_element=self._composerWindow),
            key_bindings=kb,
            full_screen=True,
            mouse_support=False,
            output=Vt100_Output.from_pty(sys.stdout),
        )

        # ── 初始化历史记录 ──
        self._allLines: list[str] = []
        self._scrollOffset: int = 0  # 0 = 底部（最新），>0 = 向上偏移行数
        if initialLines:
            for line in initialLines:
                self._allLines.extend(line.split('\n'))
            self._refreshTranscript()




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


    async def run(self) -> str | None:
        result = await self._app.run_async()
        return None if self._exitRequested else result




    # ========================================================================
    # 内部方法
    # ========================================================================

    _fmtLinesFn = None

    @classmethod
    def _formatMessageLines(cls, timestamp: str, sender: str, text: str) -> list[str]:
        if cls._fmtLinesFn is None:
            from utils.command.send import _formatMessageLines
            cls._fmtLinesFn = _formatMessageLines
        return cls._fmtLinesFn(timestamp, sender, text)


    def _getTermWidth(self) -> int:
        try:
            return self._app.output.get_size().columns
        except Exception:
            return 80


    def _defaultStatus(self) -> str:
        return (
            "Enter 换行 | Ctrl+S / Alt+Enter 发送 | Esc 退出 | Ctrl+X 清空"
            f" | Ctrl+↑↓ / PgUp PgDn 滚动历史 | 聊天对象: {self._targetChatID}"
        )


    def _updateStatus(self):
        if self._scrollOffset == 0:
            self._statusText = self._defaultStatus()
        else:
            pending = f"  ▼ {self._pendingNewMessages} 条新消息" if self._pendingNewMessages else ""
            self._statusText = (
                f"[历史浏览] PgDn/Ctrl+↓ 向下 | PgUp/Ctrl+↑ 向上 | 偏移 {self._scrollOffset} 行{pending}"
                f"  |  Esc 退出 | 聊天对象: {self._targetChatID}"
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
        window_height = self._getWindowHeight()
        total = len(self._allLines)
        max_offset = max(0, total - window_height)
        self._scrollOffset = min(self._scrollOffset, max_offset)


    def _refreshTranscript(self):
        """
        刷新聊天记录显示。
        根据 _scrollOffset 计算应显示的内容，通过 buffer.set_document 更新。
        """
        window_height = self._getWindowHeight()
        total = len(self._allLines)

        # 计算可见范围
        if self._scrollOffset == 0:
            # 跟随最新：显示最后 window_height 行
            start = max(0, total - window_height)
            visible_lines = self._allLines[start:]
        else:
            # 历史浏览：从底部往上偏移 _scrollOffset
            end = max(0, total - self._scrollOffset)
            start = max(0, end - window_height)
            visible_lines = self._allLines[start:end]

        # 确保显示固定行数，顶部不足补空行
        while len(visible_lines) < window_height:
            visible_lines.insert(0, "")

        text = "\n".join(visible_lines)

        # 更新 Buffer（read_only=True 时需要用 bypass_readonly）
        buf = self._transcriptArea.buffer
        buf.set_document(Document(text=text), bypass_readonly=True)

        # 设置光标位置到底部（让视图显示最下方）
        buf.cursor_position = len(text)

        self._app.invalidate()
