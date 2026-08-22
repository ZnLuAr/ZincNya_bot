"""
utils/llm/review.py

LLM 审核域唯一归属。

三个审核入口（Telegram 按钮、chatScreen TUI、独立 CLI）
的 send/retry/cancel/feedback 逻辑统一在此，各自只负责 UI 交互和结果展示。

首生成分发也在此：dispatchTextReply / dispatchGeneratedOutput 按 autoMode
三分流文字回复与记忆操作（TG 依赖经回调注入，本模块不直接持有 PTB 对象）。

TG 审核卡片渲染（render*/format*/build*Keyboard）也在此——与
formatReviewItemText（console / chatScreen 侧）构成三端展示格式的统一归属；
渲染是纯函数，仅构造 PTB 数据对象（InlineKeyboardMarkup），不依赖上下文实例。

审核队列 item 契约（make*/add*/peek 系列）在此定义；队列容器本体在
utils/llm/state.py（getReviewQueue）——状态容器留 state，领域契约随域走。

支持两类审核项：
    - kind == "reply"：回复审核（支持编辑、重试、补充反馈重试）
    - kind == "memory"：LLM 自主记忆操作审核
"""

from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    LLM_REVIEW_FEEDBACK_MAX_LENGTH,
    LLM_MEMORY_MAX_ACTIONS,
    TG_MESSAGE_MAX_LEN,
)

from utils.core.logger import logAction, LogLevel, LogChildType, logSystemEvent
from utils.llm.client import generateReply
from utils.llm.config import getMemoryAutoApprove
from utils.llm.memory.action import (
    MemoryAction,
    buildMemoryActionReviewPayload,
    executeAction,
    formatActionDetail,
    parseMemoryActions,
    validateAction,
)
from utils.llm.state import getReviewQueue
from utils.telegramHelpers import escapeHtml, sendLLMReply, truncateText


_DISPLAY_LIMIT = 200  # 展示用文本截断长度（memory 内容 / reply / 编辑后预览）
_LOG_FEEDBACK_LEN = 100  # 日志中反馈补充文本的预览长度
_LOG_CONTENT_LEN = 80  # 日志中记忆操作 content 的预览长度
_HINT_LEN = 16  # chatScreen 状态栏预览截断长度（随队列 item 契约从 state.py 迁入）

_NO_OPS_HINT = "诶——等等……管理员配置貌似有缺位……💦\n得有人为锌酱说的话负责，锌酱才可以畅所欲言不逾矩的喵……"




# ---------------------------------------------------------------------------
# 队列 item 契约（make*/add*/peek——容器本体在 state.py，item 格式是审核域契约）
# ---------------------------------------------------------------------------

def _formatReviewHint(item: dict) -> str:
    kind = item.get("kind", "reply")
    if kind == "memory":
        action = item.get("action", {})
        actionType = action.get("action", "?")
        content = action.get("content") or action.get("originalContent") or ""
        memoryID = action.get("memoryID")
        if content:
            # 转义换行符，避免底边栏被截断
            escaped = content.replace('\n', '\\n')
            preview = escaped[:_HINT_LEN] + "…" if len(escaped) > _HINT_LEN else escaped
        elif memoryID is not None:
            preview = f"#{memoryID}"
        else:
            preview = ""
        return f"当前操作的是：[记忆:{actionType}] {preview}" if preview else f"当前操作的是：[记忆:{actionType}]"

    reply = item.get("reply") or ""
    # 转义换行符，避免底边栏被截断
    escaped = reply.replace('\n', '\\n')
    preview = escaped[:_HINT_LEN] + "…" if len(escaped) > _HINT_LEN else escaped
    return f"当前操作的是：[回复] {preview}" if preview else "当前操作的是：[回复]"


def peekReviewHint() -> str | None:
    """
    窥视队首审核项，返回类型+内容预览字符串。
    队列为空时返回 None。不消费队列项。
    """
    # asyncio.Queue 内部使用 collections.deque
    if not getReviewQueue()._queue:
        return None
    return _formatReviewHint(getReviewQueue()._queue[0])


def makeReplyReviewItem(
    *,
    chatID: str,
    messageID: int,
    originalMsg: str,
    reply: str,
    opsID: str,
    userID: str | int | None = None,
    includeContext: bool = False,
    urlContexts: list[dict] | None = None,
    displayBlocks: object | None = None,
) -> dict:
    return {
        "kind": "reply",
        "chatID": chatID,
        "messageID": messageID,
        "originalMsg": originalMsg,
        "reply": reply,
        "opsID": opsID,
        "userID": str(userID) if userID is not None else None,
        "includeContext": includeContext,
        "urlContexts": urlContexts or [],
        "displayBlocks": displayBlocks,
    }


def addReviewItem(
    chatID: str,
    messageID: int,
    originalMsg: str,
    reply: str,
    opsID: str,
    userID: str | int | None = None,
    includeContext: bool = False,
    urlContexts: list[dict] | None = None,
    displayBlocks: object | None = None,
):
    """
    将待审核消息加入队列

    参数:
        chatID: 原始聊天 ID
        messageID: 原始消息 ID
        originalMsg: 用户发送的原始消息
        reply: LLM 生成的回复
        opsID: 审核通知发送给哪个 ops
        urlContexts: URL 抓取结果列表
        displayBlocks: 审核卡结构化展示块（None 时渲染层走退化路径）
    """
    getReviewQueue().put_nowait(makeReplyReviewItem(
        chatID=chatID,
        messageID=messageID,
        originalMsg=originalMsg,
        reply=reply,
        opsID=opsID,
        userID=userID,
        includeContext=includeContext,
        urlContexts=urlContexts,
        displayBlocks=displayBlocks,
    ))


def makeMemoryReviewItem(
    *,
    action: dict,
    chatID: str,
    originalMsg: str,
    opsID: str,
    userID: str | int | None = None,
    displayBlocks: object | None = None,
) -> dict:
    return {
        "kind": "memory",
        "action": action,
        "chatID": chatID,
        "originalMsg": originalMsg,
        "opsID": opsID,
        "userID": str(userID) if userID is not None else None,
        "displayBlocks": displayBlocks,
    }


def addMemoryReviewItem(
    *,
    action: dict,
    chatID: str,
    originalMsg: str,
    opsID: str,
    userID: str | int | None = None,
    displayBlocks: object | None = None,
):
    """
    将 LLM 记忆操作加入审核队列。

    参数:
        action: MemoryAction 的 dict 形式
        chatID: 原始聊天 ID
        originalMsg: 触发该操作的用户消息
        opsID: 审核通知发送给哪个 ops
        userID: 触发用户 ID
        displayBlocks: 审核卡结构化展示块（console/chatScreen 路径恒为 None，见 dispatchMemoryActions 降级边界）
    """
    getReviewQueue().put_nowait(makeMemoryReviewItem(
        action=action,
        chatID=chatID,
        originalMsg=originalMsg,
        opsID=opsID,
        userID=userID,
        displayBlocks=displayBlocks,
    ))




# ---------------------------------------------------------------------------
# 共享字段提取
# ---------------------------------------------------------------------------

def extractMemoryActionFields(action: dict) -> dict:
    """
    从 action dict 中提取标准化展示字段。
    处理 None 安全（content/originalContent fallback），供格式化函数共用。
    """
    return {
        "actionType": action.get("action", "?"),
        "scopeType": action.get("scopeType", "?"),
        "scopeID": action.get("scopeID", ""),
        "content": action.get("content") or action.get("originalContent") or "",
        "tags": action.get("tags") or [],
        "priority": action.get("priority", 0),
        "reason": action.get("reason", ""),
        "memoryID": action.get("memoryID"),
    }




# ---------------------------------------------------------------------------
# 能力判断辅助函数
# ---------------------------------------------------------------------------

def canEditReviewItem(item: dict) -> bool:
    """判断审核项是否可编辑。"""
    kind = item.get("kind", "reply")
    if kind == "reply":
        return True
    if kind == "memory":
        actionType = item.get("action", {}).get("action", "")
        return actionType in {"add", "update"}
    return False


def canRetryReviewItem(item: dict) -> bool:
    """判断审核项是否可重试。"""
    return item.get("kind", "reply") == "reply"




# ---------------------------------------------------------------------------
# 格式化辅助函数
# ---------------------------------------------------------------------------

def formatReviewItemText(item: dict) -> str:
    """将审核项格式化为可读文本，用于 console / chatScreen 展示。"""
    kind = item.get("kind", "reply")

    if kind == "memory":
        f = extractMemoryActionFields(item.get("action", {}))

        lines = [
            f"[记忆操作审核] {f['actionType'].upper()}",
            f"  范围: {f['scopeType']}:{f['scopeID'] or 'global'}",
        ]
        if f["memoryID"] is not None:
            lines.append(f"  目标 ID: #{f['memoryID']}")
        if f["content"]:
            displayContent = f["content"] if len(f["content"]) <= _DISPLAY_LIMIT else f["content"][:_DISPLAY_LIMIT] + "..."
            lines.append(f"  内容: {displayContent}")
        if f["tags"]:
            lines.append(f"  标签: {', '.join(f['tags'])}")
        if f["priority"]:
            lines.append(f"  优先级: {f['priority']}")
        if f["reason"]:
            lines.append(f"  理由: {f['reason']}")
        lines.append(f"  触发消息: {item.get('originalMsg', '?')}")
        return "\n".join(lines)

    # kind == "reply"
    reply = item.get("reply", "")
    displayReply = reply if len(reply) <= _DISPLAY_LIMIT else reply[:_DISPLAY_LIMIT] + "..."
    return (
        f"[回复审核]\n"
        f"  原始消息: {item.get('originalMsg', '?')}\n"
        f"  回复内容: {displayReply}"
    )


def getReviewItemActions(item: dict) -> str:
    """对控制台审核，返回审核项的可用操作提示"""
    kind = item.get("kind", "reply")

    if kind == "memory":
        actionType = item.get("action", {}).get("action", "")
        if actionType in {"add", "update"}:
            return "[A]pprove / [E]dit / [C]ancel"
        return "[A]pprove / [C]ancel"

    # kind == "reply"
    return "[A]ccept / [E]dit / [R]etry / [F]eedback / [C]ancel"




# ---------------------------------------------------------------------------
# 记忆操作解析 / 校验 / 分发（审核层共享）
# ---------------------------------------------------------------------------

async def extractValidatedMemoryActions(reply: str, *, logLabel: str) -> tuple[str, list, int]:
    """
    清理 reply 中的 <MEMORY_ACTION> 块、截断超限操作、逐个校验。

    这是 action.py 原语（parse/validate）之上的审核层编排：
    截断阈值与"丢弃失败项并计数"的语义都为审核流程服务，故放在 review 层。

    参数:
        reply: LLM 原始回复（可能含 <MEMORY_ACTION> 块）
        logLabel: 日志来源标签，如 'retry' / 'feedback retry' / 'console retry'

    返回:
        清理后的 reply, 校验通过的 action 列表, 校验失败数
    """
    cleanedReply, actions = parseMemoryActions(reply)

    # 截断超限操作
    if len(actions) > LLM_MEMORY_MAX_ACTIONS:
        await logSystemEvent(
            f"LLM 记忆操作数量超限（{logLabel}）",
            f"请求 {len(actions)} 个，上限 {LLM_MEMORY_MAX_ACTIONS}，截断",
            LogLevel.WARNING,
        )
        actions = actions[:LLM_MEMORY_MAX_ACTIONS]

    # 逐个校验，失败项丢弃并计数
    validated = []
    failed = 0
    for act in actions:
        err = await validateAction(act)
        if err:
            failed += 1
            await logSystemEvent(
                f"LLM 记忆操作校验失败（{logLabel}）",
                f"{act.action} | {err}",
                LogLevel.WARNING,
            )
        else:
            validated.append(act)

    return cleanedReply, validated, failed


def queueMemoryActionsToConsole(actions: list, *, chatID, originalMsg, opsID, userID) -> None:
    """将校验通过的记忆操作加入 console 审核队列。"""
    for act in actions:
        addMemoryReviewItem(
            action=act.toDict(),
            chatID=chatID,
            originalMsg=originalMsg,
            opsID=opsID,
            userID=userID,
        )


async def dispatchMemoryActionsToConsole(actions, *, chatID, originalMsg, opsID, userID, logLabel):
    """
    memoryDispatcher 默认实现：async 包装转发到 sync queueMemoryActionsToConsole。

    契约：retry/feedback 路径有意不读 memoryAutoApprove（与首生成路径不同——首生成经
    dispatchMemoryActions 的 respectAutoApprove=True 读 memoryAutoApprove）——
    retry 产出的记忆操作本就要 ops 看过新回复才能定夺，故始终走审核。logLabel 仅用于汇总日志。
    """
    queueMemoryActionsToConsole(
        actions, chatID=chatID, originalMsg=originalMsg, opsID=opsID, userID=userID,
    )
    if actions:
        await logSystemEvent(
            f"LLM {logLabel} 生成记忆操作",
            f"{len(actions)} 个操作已加入 console 审核队列",
            LogLevel.INFO,
        )


async def dispatchMemoryActions(
    actions: list,
    *,
    autoMode: str,
    opsList: list,
    chatID,
    originalMsg: str,
    userID,
    respectAutoApprove: bool = True,
    sendTGMemoryReview=None,
) -> None:
    """
    按 autoMode 分流校验通过的记忆操作（首生成 + retry/feedback 共用）。

    与 dispatchMemoryActionsToConsole 的关系：本函数是 autoMode 总分流入口；
    console 分支直接调 addMemoryReviewItem，TG 分支委托调用方注入的 sendTGMemoryReview 回调
    （review.py 不依赖 handlers/PTB，TG 的 int() 类型转换在回调内完成）。

    respectAutoApprove：
        - True（首生成）：读 memoryAutoApprove，开启时直接 executeAction 自动执行
        - False（retry/feedback）：有意不读——产出的操作需 ops 看过新回复才能定夺

    opsList：审核人列表；首生成传完整列表（空则丢弃），retry/feedback 传 [opsID]。
    console 分支强制 str()，TG 分支的 int() 在 sendTGMemoryReview 回调内。
    """
    if not actions:
        return

    if respectAutoApprove and getMemoryAutoApprove():
        for act in actions:
            success = await executeAction(act)
            status = "成功" if success else "失败"
            await logAction(
                "System", f"LLM 记忆操作自动执行 ({status})",
                formatActionDetail(act),
                LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
            )
        return

    if not opsList:
        await logSystemEvent(
            "LLM 记忆操作无审核人",
            f"有 {len(actions)} 个操作被丢弃（无 LLM ops）",
            LogLevel.WARNING,
        )
        return

    opsID = opsList[0]
    for act in actions:
        actDict = await buildMemoryActionReviewPayload(act)
        if autoMode == "console":
            addMemoryReviewItem(
                action=actDict,
                chatID=str(chatID),
                originalMsg=originalMsg,
                opsID=str(opsID),
                userID=userID,
            )
        else:
            if sendTGMemoryReview is None:
                raise RuntimeError(
                    "dispatchMemoryActions: TG 分支需要 sendTGMemoryReview 回调"
                )
            await sendTGMemoryReview(actDict)




# ---------------------------------------------------------------------------
# 首生成分发（dispatchTextReply / dispatchGeneratedOutput）
# ---------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class DispatchTarget:
    """
    一次生成分发的目标定位。纯数据载体，不涉及 Python-Telegram-Bot。

    handleLLMMessage 入口构造、全程透传（update 对象不跨任务——防抖窗口后
    可能已过期，定位信息经它存活）。消费：防抖键、typing、生成、分发闭包、日志。

    字段:
        chatID:       聊天 ID（str 形式；TG API 调用处由 handler 闭包 int() 转换）
        userID:       触发用户 ID（白名单校验通过后的 int）
        username:     展示用 @username / first_name / str(userID)（getSenderDisplayName）
        triggerMsgID: 触发消息 ID——发送回复的 replyTo 目标、reaction 挂载目标
    """
    chatID: str
    userID: int
    username: str
    triggerMsgID: int


@dataclass(frozen=True, kw_only=True)
class GeneratedOutput:
    """
    一次生成的完整产出。纯数据载体，不涉及 Python-Telegram-Bot。

    _runLLMPipeline 生成完成后组装（唯一生产点）；dispatchGeneratedOutput
    按内容分流（空输出丢弃 / 文字回复 / 记忆操作），prompt 线与展示线在此汇合。

    字段:
        reply:              LLM 生成回复。includeContext=True 时已由组装方剥离
                            <MEMORY_ACTION> 块并逐条校验（失败项丢弃；首生成路径
                            失败计数不透出，retry/feedback 路径经 memoryFailedCount
                            写回 item）；False 时记忆块保留在原文
        memoryActions:      校验通过的记忆操作列表（MemoryAction），
                            includeContext=False 时恒 []
        displayOriginalMsg: 展示用原始消息（字符串线）——"@username：[附带 N 张图片]
                            \n combinedText" + 可选 URL 摘要。消费：审核卡退化路径、
                            console/chatScreen item、日志、记忆审核的触发消息
        includeContext:     本次生成是否带上下文（retry 复用、记忆门禁依赖）
        urlContexts:        URL 抓取结果（默认 None；唯一生产点实传空时为 []）；
                            retry 复用不重抓
        displayBlocks:      审核卡结构化展示块（DisplayBlocks：引用/当前配对 +
                            标注前缀）；None 时渲染层走退化路径（对 displayOriginalMsg
                            做 marker 解析）。降级边界：console/chatScreen 的记忆审核项
                            永远无 displayBlocks——这两端本就不渲染 blockquote，
                            属可接受降级
    """
    reply: str
    memoryActions: list
    displayOriginalMsg: str
    includeContext: bool
    urlContexts: list[dict] | None = None
    displayBlocks: object | None = None


async def _safeReaction(sendReaction, emoji: str) -> None:
    """reaction 失败记 WARNING 后继续（不中断分发）；sendReaction 为 None 时跳过。"""
    if sendReaction is None:
        return
    try:
        await sendReaction(emoji)
    except Exception as e:
        await logSystemEvent(
            "LLM reaction 发送失败",
            f"emoji={emoji} | {e}",
            LogLevel.WARNING,
        )


async def dispatchTextReply(
    generated: GeneratedOutput,
    *,
    target: DispatchTarget,
    autoMode: str,
    opsList: list,
    bot=None,
    sendReview=None,
    sendReaction=None,
) -> None:
    """
    按 autoMode 分流文字回复（首生成路径）。

    on：sendLLMReply 直接发送（bot 为 duck-typed Telegram Bot，TG_MESSAGE_MAX_LEN 截断）；
    off：逐个 ops 调注入的 sendReview(opsID)（int 转换在回调内）后加 👀 reaction；
    console：addReviewItem 入队后加 👀 reaction。
    opsList 为空时发 _NO_OPS_HINT（off/console 共同），不发 reaction、不记审核日志。
    off 分支缺 sendReview 回调抛 RuntimeError（与 dispatchMemoryActions 对称）。
    """
    if autoMode == "on":
        await sendLLMReply(
            bot=bot,
            chatID=target.chatID,
            reply=generated.reply,
            replyToMessageID=target.triggerMsgID,
            maxLength=TG_MESSAGE_MAX_LEN,
        )
        await logAction("System", f"LLM 生成内容直接发送至 @{target.username}（{target.chatID}）", f"原文：{generated.displayOriginalMsg}", LogLevel.INFO, LogChildType.WITH_CHILD)
        await logAction("System", "", f"生成的消息：{generated.reply}", LogLevel.INFO, LogChildType.LAST_CHILD)

    elif autoMode == "off":
        if not opsList:
            await bot.send_message(chat_id=target.chatID, text=_NO_OPS_HINT)
        else:
            if sendReview is None:
                raise RuntimeError("dispatchTextReply: off 分支需要 sendReview 回调")
            for opsID in opsList:
                await sendReview(opsID)
            await _safeReaction(sendReaction, "👀")
            await logAction("System", f"LLM 生成内容待审核：@{target.username}（{target.chatID}）", f"原文：{generated.displayOriginalMsg}", LogLevel.INFO, LogChildType.WITH_CHILD)
            await logAction("System", "", f"生成的消息：{generated.reply}", LogLevel.INFO, LogChildType.LAST_CHILD)

    else:
        if not opsList:
            await bot.send_message(chat_id=target.chatID, text=_NO_OPS_HINT)
        else:
            addReviewItem(
                chatID=target.chatID,
                messageID=target.triggerMsgID,
                originalMsg=generated.displayOriginalMsg,
                reply=generated.reply,
                opsID=opsList[0],
                userID=target.userID,
                includeContext=generated.includeContext,
                urlContexts=generated.urlContexts,
                displayBlocks=generated.displayBlocks,
            )
            await _safeReaction(sendReaction, "👀")
            await logAction("System", f"LLM 生成内容等待控制台审核：@{target.username}（{target.chatID}）", f"原文：{generated.displayOriginalMsg}", LogLevel.INFO, LogChildType.WITH_CHILD)
            await logAction("System", "", f"生成的消息：{generated.reply}", LogLevel.INFO, LogChildType.LAST_CHILD)


async def dispatchGeneratedOutput(
    generated: GeneratedOutput,
    *,
    target: DispatchTarget,
    autoMode: str,
    opsList: list,
    bot=None,
    sendTGReview=None,
    sendTGMemoryReview=None,
    sendReaction=None,
) -> None:
    """
    首生成路径总分发：空输出检测（🤔）→ dispatchTextReply → dispatchMemoryActions。

    TG 依赖全部注入回调，本模块不碰 PTB（与 dispatchMemoryActions 同构）。
    sendReaction(emoji) 用于 🤔/👀；sendTGReview 传给 dispatchTextReply 的 sendReview；
    sendTGMemoryReview 直通 dispatchMemoryActions（respectAutoApprove 保持默认 True）。
    """
    if not generated.reply.strip() and not generated.memoryActions:
        await _safeReaction(sendReaction, "🤔")
        await logSystemEvent(
            "LLM 回复为空",
            f"chatID={target.chatID}, user=@{target.username} | 回复为空且无有效记忆操作",
            LogLevel.WARNING,
            LogChildType.WITH_ONE_CHILD,
        )
        return

    if generated.reply.strip():
        await dispatchTextReply(
            generated,
            target=target,
            autoMode=autoMode,
            opsList=opsList,
            bot=bot,
            sendReview=sendTGReview,
            sendReaction=sendReaction,
        )

    await dispatchMemoryActions(
        generated.memoryActions,
        autoMode=autoMode,
        opsList=opsList,
        chatID=target.chatID,
        originalMsg=generated.displayOriginalMsg,
        userID=target.userID,
        sendTGMemoryReview=sendTGMemoryReview,
    )




# ---------------------------------------------------------------------------
# TG 审核卡片渲染
# 与 formatReviewItemText（console/chatScreen 侧）同族；纯函数，仅构造 PTB 数据对象
# ---------------------------------------------------------------------------

# 审核卡片截断长度（按显示需要设定，非业务阈值——改动触发源是卡片排版而非业务需求，
# 且无跨模块共享；曾短暂放根 config 的 reply 预览长度按常量归属判据撤回至此）
_CARD_CONTENT_LEN = 500  # 记忆审核卡片中 content 的截断长度
_ORIGINAL_MSG_LEN = 1800  # 原始消息区截断长度（与 reply 预览同量级，控制卡片总长 < TG 上限）
_REPLY_PREVIEW_LEN = 1800  # 审核卡片 reply 预览长度（正文 + 框架后留余量）

# 重复使用的动态后缀模板（记忆操作校验失败警告）
MEMORY_FAILED_WARNING = "\n\n⚠️ {} 个记忆操作校验失败，已丢弃"


def truncateCardText(text: str, limit: int) -> str:
    """审核卡片截断：委托 truncateText 并固定「内容过长」提示（保持既有文案）。"""
    return truncateText(text, limit, suffix="……[内容过长，已截断]")


def renderOriginalMsgBlock(originalMsg: str, displayBlocks=None) -> str:
    """
    渲染原始消息区为 HTML blockquote。

    视觉设计思路：blockquote 自带缩进与竖线，是主要的视觉锚点。引用与当前消息
    合并在同一个块里、以粗体小节区分——TG 块的宽度由块内最长行决定（HTML 无法
    设置块宽），分块在内容长短悬殊时两条右边缘参差不齐；合块只留一条边缘。

    displayBlocks 非 None（数据源结构化，messagePrep.DisplayBlocks）时走结构化渲染：
    prefix 标注行 + 每 pair 一个合并 blockquote（多消息批次每条消息一块，配对保真）；
    None 时走退化路径：originalMsg 可能是 injectReplyTextContext 的输出
    （含 [引用的消息]/[当前用户消息] 标记 + <@sender>），按标记切分后同样
    合并为一个双小节块；无标记则整块一个 blockquote。
    两条路径都遵守铁律：先截断原始文本再 escape（截断 → 转义 → 拼），避免断实体。
    """
    if displayBlocks is not None:
        blocks = []
        prefix = (displayBlocks.prefix or "").strip()
        if prefix:
            blocks.append(escapeHtml(truncateCardText(prefix, _ORIGINAL_MSG_LEN)))
        for pair in displayBlocks.pairs:
            replyLine = (pair.get("replyLine") or "").strip()
            currentText = (pair.get("currentText") or "").strip()
            sections = []
            if replyLine:
                sections.append(f"<b>引用消息</b>\n{escapeHtml(truncateCardText(replyLine, _ORIGINAL_MSG_LEN))}")
                sections.append("\n")
            if currentText:
                sections.append(f"<b>当前消息</b>\n{escapeHtml(truncateCardText(currentText, _ORIGINAL_MSG_LEN))}")
            if sections:
                blocks.append(f"<blockquote>{chr(10).join(sections)}</blockquote>")
        return "\n".join(blocks) if blocks else f"<blockquote>{escapeHtml(truncateCardText(originalMsg, _ORIGINAL_MSG_LEN))}</blockquote>"

    truncated = truncateCardText(originalMsg, _ORIGINAL_MSG_LEN)
    replyMark = "[引用的消息]"
    currentMark = "[当前用户消息]"
    if replyMark in truncated and currentMark in truncated:
        replyPart, _, afterCurrent = truncated.partition(currentMark)
        return (
            f"<blockquote><b>引用消息</b>\n{escapeHtml(replyPart.replace(replyMark, '').strip())}"
            "\n\n"
            f"<b>当前消息</b>\n{escapeHtml(afterCurrent.strip())}</blockquote>"
        )
    return f"<blockquote>{escapeHtml(truncated)}</blockquote>"


def formatReviewText(originalMsg: str, reply: str, displayBlocks=None) -> str:
    """
    构造回复审核卡片 HTML（blockquote 原始消息区 + <pre> Monospace 回复块）。

    回复展示生成物原文（等宽），不做 Markdown 富文本渲染——富文本在发送路径
    sendLLMReply → prepareMarkdownReply 才生效。
    各动态段先截断原始再转义（铁律），整体不裸截断 escape 文本。
    保留为纯文本入口（返回 HTML 字符串），供 renderReviewCard 加 suffix 后发给 ops。
    displayBlocks 透传 renderOriginalMsgBlock（结构化展示，None 走退化路径）。
    """
    # 回复以 <pre> Monospace 块展示生成物原文：审核视角看的就是将要发送的内容，
    # 不做 Markdown 富文本渲染（富文本在发送路径 sendLLMReply → prepareMarkdownReply 才生效）
    replyBlock = f"<pre>{escapeHtml(truncateCardText(reply, _REPLY_PREVIEW_LEN))}</pre>"
    originalBlock = renderOriginalMsgBlock(originalMsg, displayBlocks)
    return (
        "[生成回复待审核]\n\n"
        f"{originalBlock}\n\n"
        f"<b>回复</b>\n{replyBlock}\n\n"
        f"<i>💡 回复此消息以 :edit 修改 / :fb 补充反馈喵</i>"
    )


def buildReviewKeyboard(chatID) -> InlineKeyboardMarkup:
    """构造回复审核 inline keyboard（callback_data 不含 msgID，由 query.message.message_id 取）"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 发送", callback_data=f"llm:review:send:{chatID}"),
            InlineKeyboardButton("🔄 重试", callback_data=f"llm:review:retry:{chatID}"),
            InlineKeyboardButton("❌ 取消", callback_data=f"llm:review:cancel:{chatID}"),
        ],
    ])


def renderReviewCard(originalMsg: str, reply: str, chatID, suffix: str = "", displayBlocks=None) -> tuple[str, InlineKeyboardMarkup]:
    """
    构造回复审核卡片 (text, markup)。
    suffix 接 retry/feedback 的动态后缀（warningText / 正在生成 / 失败恢复），整体再截断到 TG 上限。
    displayBlocks 透传 formatReviewText（结构化展示，None 走退化路径）。
    """
    text = formatReviewText(originalMsg, reply, displayBlocks) + suffix
    return truncateCardText(text, TG_MESSAGE_MAX_LEN), buildReviewKeyboard(chatID)


def formatMemoryReviewText(action: dict, originalMsg: str, displayBlocks=None) -> str:
    """
    构造记忆审核卡片 HTML（普通文本标题 + 字段行 + 触发消息 blockquote）。

    与回复卡同风格：标题用 [xxx] 普通文本（不用粗体大标题）；字段名加粗作行内标签。
    各动态段先截断原始再 escape（铁律）。触发消息走 renderOriginalMsgBlock
    （用户 reply 触发记忆操作时同样可能含引用；displayBlocks 非 None 时走结构化渲染）。
    """
    f = extractMemoryActionFields(action)

    lines = [
        f"[记忆操作待审核 · {escapeHtml(f['actionType'].upper())}]",
        "",
        f"<b>范围</b>：{escapeHtml(f['scopeType'])}:{escapeHtml(f['scopeID'] or 'global')}",
    ]
    if f["memoryID"] is not None:
        lines.append(f"<b>目标 ID</b>：#{f['memoryID']}")
    if f["content"]:
        lines.append(f"<b>内容</b>：{escapeHtml(truncateCardText(f['content'], _CARD_CONTENT_LEN))}")
    if f["tags"]:
        lines.append(f"<b>标签</b>：{escapeHtml(', '.join(f['tags']))}")
    if f["priority"]:
        lines.append(f"<b>优先级</b>：{f['priority']}")
    if f["reason"]:
        lines.append(f"<b>理由</b>：{escapeHtml(truncateCardText(f['reason'], _CARD_CONTENT_LEN))}")
    lines.append("")
    lines.append("<b>触发消息</b>")
    lines.append(renderOriginalMsgBlock(originalMsg, displayBlocks))

    if f["actionType"] in ("add", "update"):
        lines.append("\n<i>💡 回复此消息并以 :edit 开头修改记忆内容</i>")

    return "\n".join(lines)


def buildMemoryReviewKeyboard(chatID) -> InlineKeyboardMarkup:
    """构造记忆审核 inline keyboard（callback_data 不含 msgID，由 query.message.message_id 取）"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 批准", callback_data=f"llm:memreview:approve:{chatID}"),
            InlineKeyboardButton("❌ 取消", callback_data=f"llm:memreview:cancel:{chatID}"),
        ],
    ])


def renderMemoryReviewCard(action: dict, originalMsg: str, chatID, suffix: str = "", displayBlocks=None) -> tuple[str, InlineKeyboardMarkup]:
    """
    构造记忆审核卡片 (text, markup)。
    memory 审核不支持 :fb（handleFeedbackRetry 拒绝 llm_memreview_），故提示只有 :edit。
    displayBlocks 透传 formatMemoryReviewText（结构化展示，None 走退化路径）。
    """
    text = formatMemoryReviewText(action, originalMsg, displayBlocks) + suffix
    return truncateCardText(text, TG_MESSAGE_MAX_LEN), buildMemoryReviewKeyboard(chatID)




# ---------------------------------------------------------------------------
# 审核动作
# ---------------------------------------------------------------------------

async def _retryReplyReview(
    item: dict,
    *,
    memoryDispatcher=dispatchMemoryActionsToConsole,
    logLabel: str = "console retry",
) -> dict:
    newReply = await generateReply(
        item["originalMsg"],
        item["chatID"],
        includeContext=bool(item.get("includeContext")),
        userID=item.get("userID"),
        urlContexts=item.get("urlContexts"),
    )

    # 清理 <MEMORY_ACTION> 块、校验并按 memoryDispatcher 分发（默认入 console 队列）
    failed = 0
    if item.get("includeContext"):
        newReply, validated, failed = await extractValidatedMemoryActions(newReply, logLabel=logLabel)
        await memoryDispatcher(
            validated,
            chatID=item["chatID"],
            originalMsg=item["originalMsg"],
            opsID=item["opsID"],
            userID=item.get("userID"),
            logLabel=logLabel,
        )

    await logAction(
        "System", "LLM 审核：重新生成",
        f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD,
    )
    await logAction(
        "System", "",
        f"生成的消息：{newReply}", LogLevel.INFO, LogChildType.LAST_CHILD,
    )
    return {**item, "reply": newReply, "memoryFailedCount": failed}


async def reviewRetryWithFeedback(
    item: dict,
    feedback: str,
    *,
    memoryDispatcher=dispatchMemoryActionsToConsole,
    logLabel: str = "feedback retry",
) -> dict:
    """
    Ops 补充反馈后打回去重试生成。

    将 ops 的补充要求追加到 originalMsg 后，作为 [背景信息补充：...] 块。
    LLM 会将其理解为可信的背景信息。

    参数:
        item: 原始审核项
        feedback: ops 输入的补充要求（超 LLM_REVIEW_FEEDBACK_MAX_LENGTH 抛 ValueError）

    返回:
        更新后的审核项（reply 已替换，含 memoryFailedCount）

    抛出:
        ValueError: feedback 超过 LLM_REVIEW_FEEDBACK_MAX_LENGTH（调用方负责提示用户精简）
    """
    trimmed = feedback.strip()
    # 长度检查置于 enhancedMsg 构造之前：超长直接拒绝，不白调用 generateReply
    if len(trimmed) > LLM_REVIEW_FEEDBACK_MAX_LENGTH:
        raise ValueError(
            f"反馈过长喵（{len(trimmed)}/{LLM_REVIEW_FEEDBACK_MAX_LENGTH} 字），请精简后重试"
        )

    # 格式化并追加。
    # trimmed 的分隔符中和由下游统一处理：
    # enhancedMsg 作为 userMessage 流经 generateReply → buildConversationContext，
    # 在进 <CURRENT_USER_MESSAGE> 前被 neutralizePromptDelimiters 整体中和，
    # 故此处不再各自转义（见 utils/llm/promptSafety.py）。
    enhancedMsg = f"{item['originalMsg']}\n\n[背景信息补充：{trimmed}]"

    newReply = await generateReply(
        enhancedMsg,
        item["chatID"],
        includeContext=bool(item.get("includeContext")),
        userID=item.get("userID"),
        urlContexts=item.get("urlContexts"),
    )

    # 清理 <MEMORY_ACTION> 块、校验并按 memoryDispatcher 分发（默认入 console 队列）
    failed = 0
    if item.get("includeContext"):
        newReply, validated, failed = await extractValidatedMemoryActions(
            newReply, logLabel=logLabel,
        )
        await memoryDispatcher(
            validated,
            chatID=item["chatID"],
            originalMsg=item["originalMsg"],
            opsID=item["opsID"],
            userID=item.get("userID"),
            logLabel=logLabel,
        )

    await logAction(
        "System",
        "LLM 审核：补充反馈重试",
        f"补充：{trimmed[:_LOG_FEEDBACK_LEN]}", LogLevel.INFO, LogChildType.WITH_CHILD,
    )
    await logAction(
        "System", "",
        f"生成的消息：{newReply}", LogLevel.INFO, LogChildType.LAST_CHILD,
    )
    return {**item, "reply": newReply, "memoryFailedCount": failed}


async def _approveMemoryReview(item: dict) -> bool:
    actionData = item["action"]
    action = MemoryAction.fromDict(actionData)
    success = await executeAction(action)
    status = "成功" if success else "失败"
    detail = f"scope={action.scopeType}:{action.scopeID}"
    if action.memoryID is not None:
        detail += f", id=#{action.memoryID}"
    if action.content:
        detail += f", content={action.content[:_LOG_CONTENT_LEN]}"
    await logAction(
        "System", f"LLM 审核：记忆操作 {action.action} {status}",
        detail,
        LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
    )
    return success


def _updateReviewItemText(item: dict, editedText: str) -> dict:
    kind = item.get("kind", "reply")
    if kind == "memory":
        return {**item, "action": {**item["action"], "content": editedText}}
    return {**item, "reply": editedText}


async def reviewSend(bot, item: dict) -> None:
    """
    发送审核项的 reply 至目标聊天，或执行记忆操作。

    服务 console / chatScreen 两端（TG 按钮走 llmReview.py 自己的 send 分支，
    不经此函数）。

    参数:
        bot: Telegram Bot 实例
        item: 审核项
    """
    kind = item.get("kind", "reply")

    if kind == "memory":
        await _approveMemoryReview(item)
        return

    # kind == "reply"
    await sendLLMReply(
        bot=bot,
        chatID=item["chatID"],
        reply=item["reply"],
        replyToMessageID=item.get("messageID"),
    )
    await logAction(
        "System", "LLM 审核：发送",
        f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
    )


async def reviewRetry(
    item: dict,
    *,
    memoryDispatcher=dispatchMemoryActionsToConsole,
    logLabel: str = "console retry",
) -> dict:
    """
    重新生成审核项的 reply，返回更新后的审核项。
    仅支持 kind == "reply"。

    参数:
        item: 审核项
        memoryDispatcher: 记忆操作分发回调（默认入 console 队列；TG 端注入 autoMode 分流闭包）
        logLabel: extractValidatedMemoryActions 与 dispatcher 共用的日志标签

    返回:
        更新后的审核项（reply 已替换，含 memoryFailedCount）
    """
    if item.get("kind", "reply") == "memory":
        raise ValueError("memory 审核项暂不支持重试")

    return await _retryReplyReview(item, memoryDispatcher=memoryDispatcher, logLabel=logLabel)


async def reviewCancel(item: dict) -> None:
    """
    取消审核项，记录日志。

    参数:
        item: 审核项
    """
    kind = item.get("kind", "reply")
    if kind == "memory":
        action = item.get("action", {})
        await logAction(
            "System", "LLM 审核：记忆操作取消",
            f"{action.get('action', '?')} | "
            f"scope={action.get('scopeType', '?')}:{action.get('scopeID', '')}",
            LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
        )
        return

    await logAction(
        "System", "LLM 审核：取消",
        f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
    )


async def reviewEditSubmit(item: dict, editedText: str) -> dict:
    """
    提交编辑后的审核项，记录日志，返回更新后的审核项。
    若编辑内容为空则返回原始 item（不修改）。

    参数:
        item: 原始审核项
        editedText: 编辑后的文本

    返回:
        更新后的审核项
    """
    if not editedText.strip():
        return item

    kind = item.get("kind", "reply")
    editedItem = _updateReviewItemText(item, editedText)

    if kind == "memory":
        await logAction(
            "System",
            "LLM 审核：记忆操作编辑完成",
            f"编辑后：{editedText[:_DISPLAY_LIMIT]}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD,
        )
        return editedItem

    # kind == "reply"
    await logAction(
        "System",
        "LLM 审核：编辑完成",
        f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD,
    )
    await logAction(
        "System", "",
        f"编辑后：{editedText[:_DISPLAY_LIMIT]}", LogLevel.INFO, LogChildType.LAST_CHILD,
    )
    return editedItem
