"""
tests/handlers/test_afc.py

测试 handlers/afc.py（AFC 意图检测 handler）
"""

from unittest.mock import patch, MagicMock

import pytest

from handlers.afc import afcIntentDetector, register
from utils.llm.contextBuilder import ContextTier


# ====================================================================================================
# Fixtures
# ====================================================================================================

def _makeUpdate(text="今天天气怎么样", chatID=12345):
    """构造带文本消息的 Update mock"""
    message = MagicMock()
    message.text = text
    message.chat_id = chatID

    update = MagicMock()
    update.message = message
    update.edited_message = None
    return update


def _makeContext():
    """构造带真实 bot_data dict 的 context mock"""
    context = MagicMock()
    context.bot_data = {}
    return context


# ====================================================================================================
# 意图命中：写入 bot_data 槽位
# ====================================================================================================

@pytest.mark.asyncio
async def test_intent_hit_writes_slot():
    """检测到意图时，工具上下文块写入 afc 槽位"""
    update = _makeUpdate()
    context = _makeContext()

    with patch("handlers.afc.detectTools", return_value={"weather"}), \
         patch("handlers.afc.buildAFCContextBlock", return_value="<TOOLS>...</TOOLS>"):
        await afcIntentDetector(update, context)

    key = "llm_extra_blocks_12345"
    assert key in context.bot_data
    assert "afc" in context.bot_data[key]

    tier, label, content = context.bot_data[key]["afc"]
    assert tier == ContextTier.TOOLS
    assert label == "工具"
    assert content == "<TOOLS>...</TOOLS>"


@pytest.mark.asyncio
async def test_intent_hit_preserves_other_slots():
    """写入 afc 槽位时不覆盖其它模块的槽位"""
    update = _makeUpdate()
    context = _makeContext()
    key = "llm_extra_blocks_12345"
    # 预置其它模块的槽位
    context.bot_data[key] = {"other": ("tierX", "标签X", "内容X")}

    with patch("handlers.afc.detectTools", return_value={"weather"}), \
         patch("handlers.afc.buildAFCContextBlock", return_value="block"):
        await afcIntentDetector(update, context)

    # 其它模块槽位保留，afc 槽位新增
    assert context.bot_data[key]["other"] == ("tierX", "标签X", "内容X")
    assert "afc" in context.bot_data[key]


# ====================================================================================================
# 无意图：不写入
# ====================================================================================================

@pytest.mark.asyncio
async def test_no_intent_no_write():
    """无意图时不写入任何槽位"""
    update = _makeUpdate(text="随便聊聊")
    context = _makeContext()

    with patch("handlers.afc.detectTools", return_value=set()), \
         patch("handlers.afc.buildAFCContextBlock") as mockBuild:
        await afcIntentDetector(update, context)

    mockBuild.assert_not_called()
    assert context.bot_data == {}


@pytest.mark.asyncio
async def test_no_intent_clears_stale_own_slot():
    """无意图时清除本模块上一条消息遗留的槽位，但保留其它模块槽位"""
    update = _makeUpdate(text="随便聊聊")
    context = _makeContext()
    key = "llm_extra_blocks_12345"
    context.bot_data[key] = {
        "afc": (ContextTier.TOOLS, "工具", "旧块"),
        "other": ("tierX", "标签X", "内容X"),
    }

    with patch("handlers.afc.detectTools", return_value=set()):
        await afcIntentDetector(update, context)

    # afc 槽位被清，other 保留
    assert "afc" not in context.bot_data[key]
    assert context.bot_data[key]["other"] == ("tierX", "标签X", "内容X")


@pytest.mark.asyncio
async def test_empty_block_not_written():
    """buildAFCContextBlock 返回空时不写入槽位"""
    update = _makeUpdate()
    context = _makeContext()

    with patch("handlers.afc.detectTools", return_value={"weather"}), \
         patch("handlers.afc.buildAFCContextBlock", return_value=""):
        await afcIntentDetector(update, context)

    key = "llm_extra_blocks_12345"
    # 未写入 afc 槽位（空块）
    assert key not in context.bot_data or "afc" not in context.bot_data.get(key, {})


# ====================================================================================================
# 边界：非文本 / 空消息
# ====================================================================================================

@pytest.mark.asyncio
async def test_no_message_returns_early():
    """既无 message 也无 edited_message 时直接返回"""
    update = MagicMock()
    update.message = None
    update.edited_message = None
    context = _makeContext()

    with patch("handlers.afc.detectTools") as mockDetect:
        await afcIntentDetector(update, context)

    mockDetect.assert_not_called()
    assert context.bot_data == {}


@pytest.mark.asyncio
async def test_non_text_message_returns_early():
    """消息无 text（如图片）时直接返回"""
    message = MagicMock()
    message.text = None
    message.chat_id = 12345

    update = MagicMock()
    update.message = message
    update.edited_message = None
    context = _makeContext()

    with patch("handlers.afc.detectTools") as mockDetect:
        await afcIntentDetector(update, context)

    mockDetect.assert_not_called()
    assert context.bot_data == {}


@pytest.mark.asyncio
async def test_edited_message_used_as_fallback():
    """message 为空时回退到 edited_message"""
    edited = MagicMock()
    edited.text = "改成查天气"
    edited.chat_id = 999

    update = MagicMock()
    update.message = None
    update.edited_message = edited
    context = _makeContext()

    with patch("handlers.afc.detectTools", return_value={"weather"}) as mockDetect, \
         patch("handlers.afc.buildAFCContextBlock", return_value="block"):
        await afcIntentDetector(update, context)

    mockDetect.assert_called_once_with("改成查天气", "999")
    assert "afc" in context.bot_data["llm_extra_blocks_999"]


# ====================================================================================================
# register()
# ====================================================================================================

def test_register_structure():
    """register() 返回结构正确"""
    result = register()

    # group 在每个 handler 项里（loader 契约：handlers 列表项可为 {"handler", "group"}）
    assert result["handlers"][0]["group"] == 1
    assert result["name"] == "AFC 意图检测"
    assert "description" in result
    assert len(result["handlers"]) == 1
