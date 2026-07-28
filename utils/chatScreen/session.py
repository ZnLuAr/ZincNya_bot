"""
utils/chatScreen/session.py

chatScreen 入口与生命周期编排。

业务编排层：构造 ChatScreenApp（灌入历史记录）→ 跑 runSession（onEnter/mainLoop/收尾接管
console 回调 + receiver + interactiveMode flag）→ 处理 reviewEditItem 三态（放回队列 / switch 透传）。
console 回调注册、receiver 启动/清理、flag 管理已下沉到 ChatScreenApp.onEnter/onExit + TUISession.runSession，
本文件不再手工管理。目标聊天的选择（含 "-c" 未指定 ID 时弹列表）由调用方（command/send.py）负责。
"""

from utils.llm.state import getReviewQueue

from .ui import ChatScreenApp
from .history import buildHistoryLines




async def chatScreen(app, bot, targetChatID: str):
    """
    进入与指定 chatID 的用户/群聊的本地交互聊天界面。

    参数:
        targetChatID: 有效的 Telegram chat ID（不接受 "NoValue"，
                      调用方需先处理列表选择）。

    返回:
        - None：正常退出（Esc / Ctrl+C）
        - {"action": "switch", "direction": "next"/"prev"}：切换聊天对象信号，透传给 send.py
    """
    # 参数校验（确保调用方已处理 "NoValue"）
    if not targetChatID or targetChatID == "NoValue":
        raise ValueError("targetChatID 必须是有效的 chat ID，不能是 'NoValue'")

    # 历史记录构建仍由 session 负责（构造前 buildHistoryLines + 欢迎行 → initialLines 传构造器）
    initialLines = await buildHistoryLines(targetChatID)
    initialLines.extend([
        "",
        f"已进入聊天界面喵",
        f"与 {targetChatID} 的实时聊天已连接",
        "=" * 64,
    ])

    ui = ChatScreenApp(targetChatID, bot=bot, shutdownEvent=None, initialLines=initialLines)

    # runSession：onEnter（注册 console 回调 + 启动 receiver + 初次刷新）→ mainLoop（= runMainLoop）
    # → 收尾（onExit 注销回调 + 恢复 flag + cancel/await receiver）。
    # reviewEditItem 三态由 runMainLoop 返回（None / 审核项 dict / switch dict）。
    reviewEditItem = await ui.runSession()

    # 非 switch 的审核项：编辑模式中退出 → 放回审核队列（session 编排自有，不进基类）
    if reviewEditItem is not None and not (
        isinstance(reviewEditItem, dict) and reviewEditItem.get("action") == "switch"
    ):
        getReviewQueue().put_nowait(reviewEditItem)

    # 切换信号透传给调用方（send.py 的 while 循环据此获取下一个 chatID 再调 chatScreen）
    if isinstance(reviewEditItem, dict) and reviewEditItem.get("action") == "switch":
        return reviewEditItem

    return None
