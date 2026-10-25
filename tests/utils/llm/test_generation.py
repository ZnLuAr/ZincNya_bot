from unittest.mock import AsyncMock, patch

import pytest

from utils.llm.memory.types import MemoryTurn
from utils.llm.models import GenerationRequest, MessageInput, MessageOrigin
from utils.llm.generation import generateResponse


def _request(includeContext=False):
    return GenerationRequest(
        messages=(MessageInput(
            origin=MessageOrigin(chatID="100", userID=42, messageID=7, sender="alice"),
            text="当前问题",
            replyText="引用原文 https://example.com",
            replySender="bob",
            images=({"data": "image-data", "mimeType": "image/jpeg"},),
            imageNotes=("[图片下载失败]",),
            extraBlocks=((1, "扩展", "扩展内容"),),
            memoryTurn=MemoryTurn(currentText="当前问题", replyText="引用原文"),
        ),),
        includeContext=includeContext,
        urlContexts=({"url": "https://example.com", "text": "网页内容"},),
    )


async def test_generate_response_uses_saved_request_and_feedback_only_for_this_retry():
    request = _request(includeContext=True)
    with (
        patch("utils.llm.generation.generateReply", new_callable=AsyncMock, return_value="回复") as generate,
        patch(
            "utils.llm.generation.extractValidatedMemoryActions",
            new_callable=AsyncMock,
            return_value=("回复", [], 0),
        ),
    ):
        result = await generateResponse(request, feedback="补充条件")

    assert result.reply == "回复"
    args, kwargs = generate.await_args
    assert args[0] == request.userMessage + "\n\n[背景信息补充：补充条件]"
    assert "审核" not in args[0]
    assert kwargs["images"] == list(request.images)
    assert kwargs["urlContexts"] == list(request.urlContexts)
    assert kwargs["extraBlocks"] == list(request.extraBlocks)
    assert kwargs["memoryQuery"].feedbackText == "补充条件"
    assert request.memoryQuery.feedbackText == ""


@pytest.mark.parametrize("replySender, expected", [
    ("bob", "<@bob>"), ("张三", "<@张三>"), ("未知用户", "<未知用户>"),
])
def test_request_renders_reply_without_losing_original(replySender, expected):
    message = MessageInput(
        origin=MessageOrigin(chatID="100", userID=42, messageID=7, sender="alice"), text="当前",
        replyText="原文" * 400, replySender=replySender,
    )
    request = GenerationRequest(messages=(message,))
    truncated = "原文" * 400
    truncated = truncated[:300] + "……"
    assert f"[引用的消息]\n{expected} {truncated}" in request.userMessage
    assert "[当前用户消息]\n<@alice> 当前" in request.userMessage
    assert "原文" * 151 not in request.userMessage
    assert message.replyText == "原文" * 400
    assert request.displayPairs == [{"replyLine": f"{expected} {truncated}", "currentText": "当前"}]


def test_request_keeps_message_pairs_in_order_and_separates_url_intent():
    request = GenerationRequest(messages=(
        MessageInput(
            origin=MessageOrigin(chatID="100", userID=42, messageID=7, sender="alice"),
            text="看看 https://current.example", replyText="帮我总结 https://reply.example", replySender="bob",
            imageNotes=("[图片下载失败]",),
        ),
        MessageInput(origin=MessageOrigin(chatID="100", userID=42, messageID=8, sender="alice"), text="再问一句"),
    ))
    assert request.urlIntentText == "看看 https://current.example\n再问一句"
    assert "https://reply.example" in request.urlCandidateText
    assert "图片下载失败" not in request.urlCandidateText
    assert request.displayPairs == [
        {"replyLine": "<@bob> 帮我总结 https://reply.example", "currentText": "[图片下载失败]\n看看 https://current.example"},
        {"replyLine": "", "currentText": "再问一句"},
    ]
    assert request.userMessage.endswith("再问一句")
    assert request.origin is request.messages[-1].origin


def test_request_display_heading_and_images():
    request = GenerationRequest(messages=(MessageInput(
        origin=MessageOrigin(chatID="100", userID=42, messageID=7, sender="alice"), text="文本",
        images=({"data": "x"},),
    ),))
    assert request.displayOriginalMsg == "@alice：[附带 1 张图片]\n文本"


async def test_context_off_keeps_memory_blocks_as_plain_reply():
    reply = '回复<MEMORY_ACTION>{"action":"add"}</MEMORY_ACTION>'
    with (
        patch("utils.llm.generation.generateReply", new_callable=AsyncMock, return_value=reply),
        patch("utils.llm.generation.extractValidatedMemoryActions", new_callable=AsyncMock) as extract,
    ):
        result = await generateResponse(_request())
    assert result.reply == reply
    assert result.memoryActions == []
    extract.assert_not_awaited()
