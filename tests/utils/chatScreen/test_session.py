"""
tests/utils/chatScreen/test_session.py

测试 utils/chatScreen/session.py 的 chatScreen() 编排逻辑。

钉死 reviewEditItem 三态语义（runMainLoop 返回值的处理）——这是 session.py 最微妙处，
此前零覆盖：
    - None：正常退出 → 不放回队列、返回 None
    - 审核项 dict（非 switch）：编辑模式中退出 → put_nowait 放回审核队列、返回 None
    - {"action":"switch","direction":...}：切换信号 → 不放回队列、透传返回该 dict
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from utils.chatScreen.session import chatScreen




def _patches(reviewEditItem):
    """构造一组 patch：ChatScreenApp 替身为 mock ui（runSession 返回 reviewEditItem）。"""
    mockUi = MagicMock()
    mockUi.runSession = AsyncMock(return_value=reviewEditItem)
    mockQueue = MagicMock()
    patches = [
        patch('utils.chatScreen.session.ChatScreenApp', return_value=mockUi),
        patch('utils.chatScreen.session.getReviewQueue', return_value=mockQueue),
    ]
    return mockUi, mockQueue, patches


@pytest.mark.asyncio
async def test_chatScreen_bare_flag_raises():
    """光杆 flag 守卫：targetChatID=True（/send -c 未选目标）抛 ValueError"""
    with pytest.raises(ValueError, match="光杆"):
        await chatScreen(MagicMock(), MagicMock(), True)


@pytest.mark.asyncio
async def test_chatScreen_empty_id_raises():
    """空 ID 守卫"""
    with pytest.raises(ValueError):
        await chatScreen(MagicMock(), MagicMock(), "")


@pytest.mark.asyncio
async def test_chatScreen_normal_exit_returns_none():
    """runMainLoop 返 None → 不放回队列、返回 None"""
    mockUi, mockQueue, ps = _patches(reviewEditItem=None)
    for p in ps:
        p.start()
    try:
        result = await chatScreen(MagicMock(), MagicMock(), "123")
    finally:
        for p in ps:
            p.stop()

    assert result is None
    mockQueue.put_nowait.assert_not_called()
    mockUi.runSession.assert_awaited_once()


@pytest.mark.asyncio
async def test_chatScreen_review_item_put_back():
    """runMainLoop 返审核项（非 switch）→ put_nowait 放回、返回 None"""
    reviewItem = {"id": 42, "content": "待审核内容"}
    mockUi, mockQueue, ps = _patches(reviewEditItem=reviewItem)
    for p in ps:
        p.start()
    try:
        result = await chatScreen(MagicMock(), MagicMock(), "123")
    finally:
        for p in ps:
            p.stop()

    assert result is None
    mockQueue.put_nowait.assert_called_once_with(reviewItem)


@pytest.mark.asyncio
async def test_chatScreen_switch_passthrough():
    """runMainLoop 返 switch 信号 → 不放回队列、透传返回该 dict"""
    switchItem = {"action": "switch", "direction": "next"}
    mockUi, mockQueue, ps = _patches(reviewEditItem=switchItem)
    for p in ps:
        p.start()
    try:
        result = await chatScreen(MagicMock(), MagicMock(), "123")
    finally:
        for p in ps:
            p.stop()

    assert result == switchItem
    mockQueue.put_nowait.assert_not_called()


@pytest.mark.asyncio
async def test_chatScreen_constructs_app_with_bot():
    """session 只构造界面和运行会话，历史加载属于订阅后的 onEnter。"""
    mockUi = MagicMock()
    mockUi.runSession = AsyncMock(return_value=None)
    with patch('utils.chatScreen.session.ChatScreenApp', return_value=mockUi) as mockCtor:
        await chatScreen(MagicMock(), "BOT_INSTANCE", "chat_999")
    mockCtor.assert_called_once_with("chat_999", bot="BOT_INSTANCE", shutdownEvent=None)
