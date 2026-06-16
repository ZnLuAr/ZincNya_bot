"""
utils/chatScreen/session.py

chatScreen 入口与生命周期管理。

业务编排层：管理 interactiveMode、初始化 UI、启动/清理 receiverLoop、
委托主循环处理用户输入。目标聊天的选择（含 "-c" 未指定 ID 时弹列表）
由调用方（command/send.py）负责，本函数只接受有效的 chatID。
"""

import asyncio

from utils.core.stateManager import getStateManager
from utils.llm.state import getReviewQueue

from .ui import ChatScreenApp
from .history import buildHistoryLines
from .receiver import startReceiver
from .mainLoop import runMainLoop




async def chatScreen(app, bot, targetChatID: str):
    """
    进入与指定 chatID 的用户/群聊的本地交互聊天界面。

    参数:
        targetChatID: 有效的 Telegram chat ID（不接受 "NoValue"，
                      调用方需先处理列表选择）。

    业务编排入口，负责：
        - interactiveMode 管理
        - 后台 receiverLoop 启动与清理
        - 将 UI 交互委托给 ChatScreenApp
    """
    # 参数校验（确保调用方已处理 "NoValue"）
    if not targetChatID or targetChatID == "NoValue":
        raise ValueError("targetChatID 必须是有效的 chat ID，不能是 'NoValue'")

    state = getStateManager()

    # 设置交互模式，暂停外层 CLI 的输入读取
    state.setInteractiveMode(True)
    queue: asyncio.Queue = state.getMessageQueue()
    shutdownEvent = state.getShutdownEvent()

    # ── 初始化 UI ──
    # 先构建初始内容（历史记录 + 欢迎信息）
    initialLines = await buildHistoryLines(targetChatID)
    initialLines.extend([
        "",
        f"已进入聊天界面喵",
        f"与 {targetChatID} 的实时聊天已连接",
        "=" * 64,
    ])
    ui = ChatScreenApp(targetChatID, initialLines=initialLines)

    # 注册控制台输出回调，让 logger 输出路由到 UI transcript
    def _consoleOutputHandler(text: str):
        lines = text.rstrip('\n').split('\n')
        ui.appendLines(lines)

    state.setConsoleOutputCallback(_consoleOutputHandler)

    # ── 启动 receiverLoop ──
    receiverTask = await startReceiver(state, targetChatID, ui, queue, shutdownEvent)
    await asyncio.sleep(0.1)

    # ── 主循环 ──
    reviewEditItem = None
    try:
        reviewEditItem = await runMainLoop(bot, targetChatID, ui, shutdownEvent)
    finally:
        # 编辑模式中退出 → 放回队列
        # 切换信号 ({"action": "switch", ...}) 不是审核项,不放回队列
        if reviewEditItem is not None:
            if not (isinstance(reviewEditItem, dict) and reviewEditItem.get("action") == "switch"):
                getReviewQueue().put_nowait(reviewEditItem)

        # 注销控制台输出回调
        state.setConsoleOutputCallback(None)
        state.setInteractiveMode(False)
        receiverTask.cancel()
        try:
            await receiverTask
        except asyncio.CancelledError:
            pass

    # ── 透传切换信号给调用方 (send.py) ──
    # 返回 {"action": "switch", "direction": "next"/"prev"} 时,
    # send.py 的 while 循环会获取下一个 chatID 并再次调用 chatScreen
    if isinstance(reviewEditItem, dict) and reviewEditItem.get("action") == "switch":
        return reviewEditItem

    return None
