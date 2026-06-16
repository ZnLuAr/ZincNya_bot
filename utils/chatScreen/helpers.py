"""
utils/chatScreen/helpers.py

chatScreen 辅助函数。

提供聊天对象导航、辅助计算等通用功能。
"""


def getNextChatID(currentChatID: str, direction: str) -> str | None:
    """
    获取 whitelist 中相对于当前 chatID 的下一个/上一个 chatID。

    参数:
        currentChatID: 当前聊天对象的 chatID
        direction: "next" 或 "prev"

    返回:
        下一个/上一个 chatID,如果到达边界则循环;
        whitelist 为空或只有一个用户时返回 None。

    边界情况:
        - whitelist 只有 0 或 1 个用户 → 返回 None (无法切换)
        - currentChatID 不在 whitelist 中 → 返回列表第一个 (容错)
        - 到达列表末尾/开头 → 取模循环
    """
    from utils.whitelistManager.data import getAllowedUserIDs

    allowedIDs = getAllowedUserIDs()

    if len(allowedIDs) <= 1:
        return None  # 只有 0 或 1 个用户,无法切换

    try:
        currentIndex = allowedIDs.index(currentChatID)
    except ValueError:
        # 当前 chatID 不在 whitelist 中(被移除?),回到列表第一个
        return allowedIDs[0] if allowedIDs else None

    if direction == "next":
        nextIndex = (currentIndex + 1) % len(allowedIDs)
    elif direction == "prev":
        nextIndex = (currentIndex - 1) % len(allowedIDs)
    else:
        return None

    return allowedIDs[nextIndex]