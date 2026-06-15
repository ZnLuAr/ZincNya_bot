"""
chatScreen 模块 — 控制台全屏聊天界面

提供与 Telegram 用户/群组的实时聊天功能。

主要组件:
    - session.py: chatScreen 入口与生命周期管理
    - receiver.py: 消息接收后台协程
    - mainLoop.py: 主循环输入处理
    - formatter.py: 消息格式化工具
    - history.py: 历史记录加载与显示
    - statusBar.py: 状态栏文本常量
    - ui.py: ChatScreenApp UI 控制层

详细文档: docs/chatScreen.md
"""

from .session import chatScreen
from .formatter import (
    getSenderName,
    extractDisplayText,
    formatMessageLines,
    formatMessage,
)
from .history import buildHistoryLines, displayHistory, printMessage
from .statusBar import (
    getDefaultStatus,
    getReviewQueueStatus,
    getHistoryBrowsingStatus,
    getEditModeStatus,
)
from .ui import ChatScreenApp

__all__ = [
    "chatScreen",
    "getSenderName",
    "extractDisplayText",
    "formatMessageLines",
    "formatMessage",
    "buildHistoryLines",
    "displayHistory",
    "printMessage",
    "getDefaultStatus",
    "getReviewQueueStatus",
    "getHistoryBrowsingStatus",
    "getEditModeStatus",
    "ChatScreenApp",
]
