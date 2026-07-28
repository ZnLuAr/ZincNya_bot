"""
utils/core/tui —— TUI 框架统一出口。

框架 = 一个会话契约 TUISession（utils.core.tui.session）+ 开放范式库 paradigms/。
加新范式 = paradigms/ 加一个文件 + 本文件导出，不碰 session、不碰其它范式。

横切关注点（interactiveMode flag / 后台任务 / 屏幕生命周期钩子）永远只在 session.py 一处。
"""

from utils.core.tui.session import TUISession
from utils.core.tui.paradigms.listMenu import ListMenuController
from utils.core.tui.paradigms.fullScreen import FullScreenTUIApp
from utils.core.tui.paradigms.textEditor import TextEditorApp, editFile




__all__ = [
    "TUISession",
    "ListMenuController",
    "FullScreenTUIApp",
    "TextEditorApp",
    "editFile",
]
