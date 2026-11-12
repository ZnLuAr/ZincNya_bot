"""
utils/llm/review/cards.py

Telegram 审核卡的文字与键盘。

都是纯函数，只构造字符串和 PTB 的 InlineKeyboardMarkup，不发送也不读写状态。
审核卡由发言、记忆能力（utils/llm/capability/）发出，审核项的存取见 items.py；
控制台 / chatScreen 一侧的展示见 console.py。
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import TG_MESSAGE_MAX_LEN

from utils.llm.models import DisplayBlocks
from utils.telegramHelpers import escapeHtml, truncateText

from .items import extractMemoryActionFields


# 审核卡片截断长度（按显示需要设定，非业务阈值——改动触发源是卡片排版而非业务需求，
# 且无跨模块共享；曾短暂放根 config 的 reply 预览长度按常量归属判据撤回至此）
_CARD_CONTENT_LEN = 500  # 记忆审核卡片中 content 的截断长度
_ORIGINAL_MSG_LEN = 1800  # 原始消息区截断长度（与 reply 预览同量级，控制卡片总长 < TG 上限）
_REPLY_PREVIEW_LEN = 1800  # 审核卡片 reply 预览长度（正文 + 框架后留余量）




def truncateCardText(text: str, limit: int) -> str:
    """审核卡片截断：委托 truncateText 并固定「内容过长」提示（保持既有文案）。"""
    return truncateText(text, limit, suffix="……[内容过长，已截断]")


def renderOriginalMsgBlock(originalMsg: str, displayBlocks: DisplayBlocks | None = None) -> str:
    """
    渲染原始消息区为 HTML blockquote。

    视觉设计思路：blockquote 自带缩进与竖线，是主要的视觉锚点。引用与当前消息
    合并在同一个块里、以粗体小节区分——TG 块的宽度由块内最长行决定（HTML 无法
    设置块宽），分块在内容长短悬殊时两条右边缘参差不齐；合块只留一条边缘。

    displayBlocks 非 None（数据源结构化，models.DisplayBlocks）时走结构化渲染：
    prefix 标注行 + 每 pair 一个合并 blockquote（多消息批次每条消息一块，配对保真）；
    None 时走退化路径：originalMsg 可能是 TaskRequest.displayOriginalMsg 的文本
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


def formatReviewText(originalMsg: str, reply: str, displayBlocks: DisplayBlocks | None = None) -> str:
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


def renderReviewCard(originalMsg: str, reply: str, chatID, suffix: str = "", displayBlocks: DisplayBlocks | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """
    构造回复审核卡片 (text, markup)。
    suffix 接 retry/feedback 的动态后缀（正在生成 / 失败恢复），整体再截断到 TG 上限。
    displayBlocks 透传 formatReviewText（结构化展示，None 走退化路径）。
    """
    text = formatReviewText(originalMsg, reply, displayBlocks) + suffix
    return truncateCardText(text, TG_MESSAGE_MAX_LEN), buildReviewKeyboard(chatID)


def formatMemoryReviewText(action: dict, originalMsg: str, displayBlocks: DisplayBlocks | None = None) -> str:
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
    if f["originalContent"]:
        lines.append(
            f"<b>当前内容</b>："
            f"{escapeHtml(truncateCardText(f['originalContent'], _CARD_CONTENT_LEN))}"
        )
    if f["content"]:
        label = "新内容" if f["originalContent"] else "内容"
        lines.append(
            f"<b>{label}</b>："
            f"{escapeHtml(truncateCardText(f['content'], _CARD_CONTENT_LEN))}"
        )
    if f["tags"]:
        lines.append(f"<b>标签</b>：{escapeHtml(', '.join(f['tags']))}")
    if f["priority"] is not None:
        lines.append(f"<b>优先级</b>：{f['priority']}")
    if f["originalMode"] is not None:
        lines.append(f"<b>当前模式</b>：{escapeHtml(f['originalMode'])}")
    if f["mode"] is not None:
        label = "新模式" if f["originalMode"] is not None else "模式"
        lines.append(f"<b>{label}</b>：{escapeHtml(f['mode'])}")
    if f["retrievalHint"] is not None:
        hintText = f["retrievalHint"] or "(清空)"
        lines.append(f"<b>检索说明</b>：{escapeHtml(hintText)}")
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


def renderMemoryReviewCard(action: dict, originalMsg: str, chatID, suffix: str = "", displayBlocks: DisplayBlocks | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """
    构造记忆审核卡片 (text, markup)。
    memory 审核不支持 :fb（handleFeedbackRetry 拒绝 llm_memreview_），故提示只有 :edit。
    displayBlocks 透传 formatMemoryReviewText（结构化展示，None 走退化路径）。
    """
    text = formatMemoryReviewText(action, originalMsg, displayBlocks) + suffix
    return truncateCardText(text, TG_MESSAGE_MAX_LEN), buildMemoryReviewKeyboard(chatID)
