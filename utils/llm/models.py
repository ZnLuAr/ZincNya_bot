"""LLM 消息、生成请求和结果共用的数据对象。"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

from config import LLM_REPLY_CONTEXT_LIMIT
from utils.llm.config import ContextTier
from utils.llm.memory.types import MemoryQuery, MemoryTurn

if TYPE_CHECKING:
    from utils.llm.memory.action import MemoryAction


class DisplayPair(TypedDict):
    """审核卡中属于同一条消息的引用和正文。"""

    replyLine: str
    currentText: str


@dataclass(frozen=True, kw_only=True)
class DisplayBlocks:
    """审核展示快照：图片、URL 标注前缀，以及按消息顺序排列的引用与正文。"""

    prefix: str = ""
    pairs: list[DisplayPair] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class MessageOrigin:
    """Telegram 消息来源：稳定 ID 与收到消息时的发送者显示名。"""

    chatID: str
    messageID: int
    userID: int | str
    sender: str = ""


@dataclass(frozen=True, kw_only=True)
class MessageInput:
    """一条已接收的用户消息；来源、正文和引用关系随防抖批次保留。"""

    origin: MessageOrigin
    text: str
    replyText: str = ""
    replySender: str = ""
    includeContext: bool = False
    images: tuple[dict, ...] = ()
    imageNotes: tuple[str, ...] = ()
    extraBlocks: tuple[tuple[ContextTier, str, str], ...] = ()
    memoryTurn: MemoryTurn | None = None


@dataclass(frozen=True, kw_only=True)
class GenerationRequest:
    """首次生成或审核重试使用的完整输入。"""

    messages: tuple[MessageInput, ...]
    includeContext: bool = False
    urlContexts: tuple[dict, ...] = ()

    @property
    def origin(self) -> MessageOrigin:
        """整批回复沿用最后一条消息的来源。"""
        return self.messages[-1].origin

    @property
    def chatID(self) -> str:
        return self.origin.chatID

    @property
    def userID(self) -> int | str:
        return self.origin.userID

    @property
    def triggerMessageID(self) -> int:
        return self.origin.messageID

    @property
    def userMessage(self) -> str:
        return "\n".join(
            text for message in self.messages
            if (text := _renderPromptMessage(message))
        )

    @property
    def images(self) -> list[dict]:
        return [image for message in self.messages for image in message.images]

    @property
    def extraBlocks(self) -> tuple[tuple[ContextTier, str, str], ...]:
        """合并批次内各条消息携带的扩展上下文块。"""
        return tuple(block for message in self.messages for block in message.extraBlocks)

    @property
    def memoryQuery(self) -> MemoryQuery | None:
        turns = tuple(message.memoryTurn for message in self.messages if message.memoryTurn is not None)
        return MemoryQuery(turns=turns) if turns else None

    @property
    def username(self) -> str:
        return self.origin.sender or str(self.userID)

    @property
    def urlIntentText(self) -> str:
        """用于判断 URL 读取意图的当前正文；引用内容不参与意图判断。"""
        return "\n".join(message.text for message in self.messages if message.text)

    @property
    def urlCandidateText(self) -> str:
        """返回 URL 提取候选文本，包含正文和引用正文。"""
        return "\n".join(
            text for message in self.messages
            if (text := "\n".join(part for part in (message.text, message.replyText) if part))
        )

    @property
    def displayOriginalMsg(self) -> str:
        images = self.images
        heading = f"@{self.username}："
        if images:
            heading += f"[附带 {len(images)} 张图片]\n"
        body = "\n".join(_renderDisplayMessage(message) for message in self.messages)
        return heading + body

    @property
    def displayPairs(self) -> list[DisplayPair]:
        return [
            {
                "replyLine": _replyLine(message),
                "currentText": _renderCurrentMessage(message),
            }
            for message in self.messages
        ]


@dataclass(frozen=True, kw_only=True)
class GenerationResult:
    """生成后的正文、校验通过的记忆操作及失败数。"""

    reply: str
    memoryActions: list["MemoryAction"] = field(default_factory=list)
    memoryFailedCount: int = 0


@dataclass(frozen=True, kw_only=True)
class GeneratedOutput:
    """提交分发的回复、记忆操作及审核快照；generationRequest 供重试复用。"""

    reply: str
    memoryActions: list["MemoryAction"]
    displayOriginalMsg: str
    includeContext: bool
    urlContexts: list[dict] | None = None
    displayBlocks: DisplayBlocks | None = None
    memoryQuery: MemoryQuery | None = None
    generationRequest: GenerationRequest | None = None


def _replyLine(message: MessageInput) -> str:
    if not message.replyText:
        return ""
    reply = message.replyText
    if len(reply) > LLM_REPLY_CONTEXT_LIMIT:
        reply = reply[:LLM_REPLY_CONTEXT_LIMIT] + "……"
    sender = message.replySender or "未知用户"
    return f"<{_displaySender(sender)}> {reply}"


def _renderPromptMessage(message: MessageInput) -> str:
    imageNotes = "\n".join(message.imageNotes)
    currentText = message.text
    if not message.replyText:
        return "\n".join(part for part in (imageNotes, currentText) if part)
    replyContext = (
        f"[引用的消息]\n{_replyLine(message)}\n\n"
        f"[当前用户消息]\n<{_displaySender(message.origin.sender)}> {currentText}"
    )
    return f"{imageNotes}\n{replyContext}" if imageNotes else replyContext


def _renderCurrentMessage(message: MessageInput) -> str:
    return "\n".join((*message.imageNotes, message.text)).strip()


def _renderDisplayMessage(message: MessageInput) -> str:
    replyLine = _replyLine(message)
    currentText = _renderCurrentMessage(message)
    if replyLine:
        return f"[引用的消息]\n{replyLine}\n\n[当前用户消息]\n<{_displaySender(message.origin.sender)}> {currentText}"
    return currentText


def _displaySender(sender: str) -> str:
    if not sender or sender == "未知用户":
        return "未知用户"
    return sender if sender.startswith("@") else f"@{sender}"
