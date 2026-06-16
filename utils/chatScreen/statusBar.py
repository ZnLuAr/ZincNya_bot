"""
utils/chatScreen/statusBar.py

状态栏文本常量与工具函数。

集中管理 chatScreen 界面的状态栏文本，避免在 ui.py / mainLoop.py /
receiver.py / command/llm.py 等多处硬编码同一串文本导致漂移。
"""




def getDefaultStatus(targetChatID: str) -> str:
    """默认状态栏：显示所有快捷键提示。"""
    return (
        "Ctrl+S 发送 | Esc 退出 | Ctrl+X 清空 | Alt+↑↓ 滚动 | Alt+←→ 切换"
        f" | 聊天: {targetChatID}"
    )




def getHistoryBrowsingStatus(offset: int, pendingCount: int, targetChatID: str) -> str:
    """历史浏览模式状态栏。"""
    pendingText = f"  ▼ 有 {pendingCount} 条新消息喵" if pendingCount else ""
    return (
        f"[历史浏览] PgDn/Alt+↓ 向下 | PgUp/Alt+↑ 向上 | 偏移 {offset} 行{pendingText}"
        f"  |  Esc 退出 | 聊天对象: {targetChatID}"
    )




def getReviewQueueStatus(queueSize: int, hint: str = "") -> str:
    """审核队列状态栏（receiverLoop 超时轮询时更新）。"""
    hintText = f" {hint} " if hint else " "
    return f" 待审核: ({queueSize} 条) | :ra 通过 :re 编辑 :rr 重试 :rc 取消 |{hintText}"




def getEditModeStatus(itemType: str) -> str:
    """编辑模式状态栏。itemType: "memory" 或 "llm"。"""
    typeText = "记忆内容" if itemType == "memory" else "LLM 生成消息"
    return f" {typeText}编辑审核中 | Ctrl+S 提交 | Esc 取消编辑"
