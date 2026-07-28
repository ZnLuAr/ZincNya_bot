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




def _patches(reviewEditItem, historyLines=None):
    """构造一组 patch：ChatScreenApp 替身为 mock ui（runSession 返回 reviewEditItem）。"""
    mockUi = MagicMock()
    mockUi.runSession = AsyncMock(return_value=reviewEditItem)
    mockQueue = MagicMock()
    patches = [
        patch('utils.chatScreen.session.ChatScreenApp', return_value=mockUi),
        patch('utils.chatScreen.session.buildHistoryLines', new_callable=AsyncMock, return_value=historyLines or []),
        patch('utils.chatScreen.session.getReviewQueue', return_value=mockQueue),
    ]
    return mockUi, mockQueue, patches


@pytest.mark.asyncio
async def test_chatScreen_noValue_raises():
    """NoValue 守卫：targetChatID='NoValue' 抛 ValueError"""
    with pytest.raises(ValueError, match="NoValue"):
        await chatScreen(MagicMock(), MagicMock(), "NoValue")


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
async def test_chatScreen_constructs_app_with_bot_and_history():
    """构造 ChatScreenApp：传入 bot + initialLines（历史 + 欢迎行）"""
    mockUi = MagicMock()
    mockUi.runSession = AsyncMock(return_value=None)
    with patch('utils.chatScreen.session.ChatScreenApp', return_value=mockUi) as mockCtor, \
         patch('utils.chatScreen.session.buildHistoryLines', new_callable=AsyncMock, return_value=["历史行1"]), \
         patch('utils.chatScreen.session.getReviewQueue', return_value=MagicMock()):
        await chatScreen(MagicMock(), "BOT_INSTANCE", "chat_999")

    mockCtor.assert_called_once()
    _args, kwargs = mockCtor.call_args
    # targetChatID 位置参数
    assert mockCtor.call_args.args[0] == "chat_999"
    # bot + initialLines 经 kwargs 传
    assert kwargs.get("bot") == "BOT_INSTANCE"
    initialLines = kwargs.get("initialLines") or []
    assert "历史行1" in initialLines                  # buildHistoryLines 结果
    assert "已进入聊天界面喵" in initialLines          # 欢迎行
    assert any(set(line) == {"="} and len(line) >= 60 for line in initialLines)  # 分隔线