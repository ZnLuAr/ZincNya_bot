"""
utils/llm/messagePrep.py

LLM 消息文本准备（「当下」侧）：

与 contextBuilder 分工：contextBuilder 组装「背景」（memory / history / knowledge / URL 块），
本模块组装「当下」——单条用户消息的清洗、reply 上下文注入、展示格式化。

安全契约（改动时勿破坏）：
    - injectReplyTextContext 的标记（[引用的消息]/<sender>）随整段 pureText 流经
      contextBuilder，进 <CURRENT_USER_MESSAGE> 前被 neutralizePromptDelimiters 折成全角——
      有意且安全：不可信 replyText 无法伪造高信任块，标记折全角后语义仍可读
    - preparePurePromptText 的 urlIntentText 必须在 reply 注入之前取值，
      否则 reply-to 消息里的"帮我总结"等文本会被误判为当前用户的意图
"""

import re
from dataclasses import dataclass, field

from config import LLM_REPLY_CONTEXT_LIMIT

from utils.llm.config import getMemoryEnabled
from utils.telegramHelpers import removeMention




@dataclass(frozen=True, kw_only=True)
class PromptPayload:
    """
    LLM prompt 文本准备结果（纯数据载体，PTB-free）。

    单条消息经 preparePurePromptText 清洗后的全部产出——prompt 线与展示线
    在此分岔，四类文本用途隔离（urlIntentText 是安全边界，见模块 docstring）。

    字段（生产：preparePurePromptText；消费：_enqueueLLMDebounce 拆字段入防抖缓冲）:
        pureText:         prompt 主文本。去 @bot / #context 后的当前消息，
                          有引用时已注入引用标记（[引用的消息]/[当前用户消息]，
                          由 _assembleReplyContext 拼接）；
                          图片下载说明行由 handler 侧 replace() 前置进来
        includeContext:   是否启用 memory/history 上下文。
                          #context 标记 or 全局 memoryEnabled；one-shot 标记
                          （consumeContextOnce）在防抖聚合时才叠加取或
        urlIntentText:    reply 注入**前**的当前消息纯文本，仅用于 URL 意图判定
                          （防被回复消息文本伪造意图，必须先于注入取值）
        urlCandidateText: 当前消息纯文本 + 被回复消息 text/caption（换行拼接），
                          仅用于 URL 提取——意图与候选分离，reply 只贡献候选
        replyLine:        展示线。"<@发送者> 引用文本"，无引用/引用为空时 ""，
                          已含 LLM_REPLY_CONTEXT_LIMIT（300）截断
        currentText:      展示线。当前消息纯文本（含图片 notes 注入，不含 reply 注入）
    """
    pureText: str
    includeContext: bool
    urlIntentText: str
    urlCandidateText: str
    replyLine: str = ""
    currentText: str = ""


@dataclass(frozen=True, kw_only=True)
class ReplyContext:
    """
    引用/当前的结构化切分（extractReplyTextContext 的产出，模块内部中间载体）。

    一次切分、两用：_assembleReplyContext 拼回带引用标记的 prompt 字符串 +
    preparePurePromptText 填 PromptPayload 展示字段（防双调用漂移）。

    TODO（Design A 预留）：prompt 组装未来改用它——injectReplyTextContext 的标记字符串
    届时改为结构化注入（引用可独立进上下文 tier / 针对性指令 / 多消息配对保真），
    retry 契约同步改造。收益与改点见 docs/llm-handler.md「四个数据载体」。

    字段:
        replyLine:     "<@发送者> 引用文本"；无引用或引用文本为空时 ""；
                       已含 LLM_REPLY_CONTEXT_LIMIT（300）截断 + "……" 后缀
        currentText:   当前消息纯文本（原样，无 reply 注入）
        currentSender: "@username" 或 "@first_name"；匿名群/频道（from_user=None）
                       时兜底 "未知用户"

    规范（pin 死，防双处组装漂移）：
        replyLine 含尖括号（"<@someone> 文本"）；currentSender 为裸值（"@curuser"）。
    """
    replyLine: str = ""
    currentText: str = ""
    currentSender: str = ""


@dataclass(frozen=True, kw_only=True)
class DisplayBlocks:
    """
    审核卡结构化展示块（展示线的终点载体，prompt 线不使用）。

    生产：_runLLMPipeline 由 DebouncedBatch.displayPairs 组装；
    消费：utils/llm/review.renderOriginalMsgBlock 走结构化渲染
    （每条消息一个 blockquote，引用/当前合并同块、粗体小节区分）。
    None（整个字段缺省或显式 None）时渲染层退化为对 originalMsg 按引用标记切分。

    字段:
        prefix: 标注前缀行——图片标注（"[附带 N 张图片]"）与 URL 摘要（urlSummary）
                换行拼接；无标注时 ""。渲染为普通文本行（不加粗，视觉弱化）
        pairs:  每元素 {"replyLine": str, "currentText": str}，与防抖批次逐条对应
                （双空条目已过滤）——多消息批次配对保真：有引用的消息，
                其引用块紧邻自己的当前消息块，不会与别的消息错配
    """
    prefix: str = ""
    pairs: list = field(default_factory=list)




def getRawMessageText(message) -> str:
    return message.text or message.caption or ""




def extractPureMessage(text: str, botUsername: str) -> tuple[str, bool]:
    """
    去除 @bot 并检查是否包含 #context 标记

    参数:
        text: 原始消息文本
        botUsername: bot 用户名

    返回:
        (纯文本, 是否需要上下文)
        第二个值：#context 标记存在时为 True；否则跟随全局 memoryEnabled 配置
        调用方还需将此值与 consumeContextOnce() 取逻辑或，以叠加 one-shot 标记
    """
    text = removeMention(text, botUsername)

    # 检查 #context 标记（必须在消息开头，去除 @bot 后的第一个词）
    contextMatch = re.match(r"#context\b\s*(.*)", text, re.DOTALL)
    if contextMatch:
        pureText = contextMatch.group(1).strip()
        return pureText, True

    return text, getMemoryEnabled()


def extractReplyTextContext(message, pureText: str) -> ReplyContext:
    """
    切分引用/当前消息为结构化字段（不拼 prompt 字符串）。

    返回:
        replyLine:    "<@发送者> 引用文本"（无引用或引用文本为空时为 ""），已含 300 字截断
        currentText:  当前消息纯文本（原样）
        currentSender: 当前发送者（"@username" 或 "未知用户"；匿名群/频道 from_user=None 时兜底）
    """
    replyMsg = message.reply_to_message
    if not replyMsg:
        return ReplyContext(currentText=pureText, currentSender=_currentSenderOf(message))

    replyText = replyMsg.text or replyMsg.caption or ""
    if not replyText:
        return ReplyContext(currentText=pureText, currentSender=_currentSenderOf(message))

    # 截断过长的 reply 文本
    if len(replyText) > LLM_REPLY_CONTEXT_LIMIT:
        replyText = replyText[:LLM_REPLY_CONTEXT_LIMIT] + "……"

    replyUser = ""
    if replyMsg.from_user:
        replyUser = replyMsg.from_user.username or replyMsg.from_user.first_name or ""
    replySender = f"@{replyUser}" if replyUser else "未知用户"

    return ReplyContext(
        replyLine=f"<{replySender}> {replyText}",
        currentText=pureText,
        currentSender=_currentSenderOf(message),
    )


def _assembleReplyContext(ctx: ReplyContext, currentText: str) -> str:
    """将结构化切分拼回带引用标记的字符串（与 injectReplyTextContext 的历史格式逐字节同构）。

    TODO（Design A 预留）：prompt 结构化后此函数退役——标记拼接被结构化注入取代。
    """
    if not ctx.replyLine:
        return currentText
    return f"[引用的消息]\n{ctx.replyLine}\n\n[当前用户消息]\n<{ctx.currentSender}> {currentText}"




def _currentSenderOf(message) -> str:
    """获取当前消息发送者（匿名群/频道 from_user 可能为 None）"""
    currentUser = ""
    if message.from_user:
        currentUser = message.from_user.username or message.from_user.first_name or ""
    return f"@{currentUser}" if currentUser else "未知用户"


def getSenderDisplayName(message, userID: int) -> str:
    return message.from_user.username or message.from_user.first_name or str(userID)


def getReplyURLCandidateText(message) -> str:
    """获取被回复消息的文本，用于 URL 候选提取"""
    replyMsg = message.reply_to_message
    if not replyMsg:
        return ""
    return replyMsg.text or replyMsg.caption or ""





def injectReplyTextContext(message, pureText: str) -> str:
    """
    将 reply-to 消息的文本注入到 prompt 中，格式上明确标注这是「引用别人说过的话」。

    参数:
        message: 当前用户消息
        pureText: 已去除 @bot / #context 的用户消息

    返回:
        注入 reply 上下文后的 prompt text

    设计思路:
        - 格式上接近 history 块的 `<sender> xxx`，让 LLM 识别这是「别人说过的话」而非「用户现在说的话」

    分隔符安全:
        这里用朴素括号写 [引用的消息] / <sender> / [当前用户消息] 标记。整段 pureText
        作为 userMessage 流经 contextBuilder，进 <CURRENT_USER_MESSAGE> 前会被
        neutralizePromptDelimiters 整体折成全角——包括这里的标记和 replyText / pureText。
        这是有意且安全的：
        - replyText（不可信叶子）里的 <...> tag 一并折叠，如此则无法伪造 <TRUSTED_KNOWLEDGE> 等高信任块跨层越权；
        - 本函数的标记折成全角后仍语义可读，「引用 vs 当前」的角色区分照样生效，
          且与 replyText 内可能预置的全角标记同处 CURRENT_USER_MESSAGE 信任层，
          不会造成跨层混淆。
    """
    ctx = extractReplyTextContext(message, pureText)
    return _assembleReplyContext(ctx, pureText)




def preparePurePromptText(message, rawText: str, botUsername: str) -> PromptPayload:
    """
    准备 LLM 提示词文本

    返回:
        PromptPayload:
            pureText:           给 LLM 的 prompt text（可包含 reply 文本注入）
            includeContext:     是否包含 memory/history
            urlIntentText:      当前用户消息去除 mention/#context 后的文本，用于判断 URL 读取意图
            urlCandidateText:   当前用户消息文本 + 被回复消息文本，用于提取 URL
            replyLine:          引用行（"<@发送者> 文本"，无引用为空串）——审核卡结构化展示用
            currentText:        当前消息纯文本（含图片 notes，不含 reply 注入）——同上

    安全提醒：
        urlIntentText 必须在 injectReplyTextContext 调用之前取值，
        否则 reply-to 消息里的"帮我总结"等文本会被误判为当前用户的意图，
        或可能变成第三方无声触发 URL 抓取的攻击面
    """
    pureText, includeContext = extractPureMessage(rawText, botUsername)
    if not pureText:
        return PromptPayload(pureText="", includeContext=includeContext, urlIntentText="", urlCandidateText="")

    # 先取意图（仅当前消息），再做 reply 注入（包含 reply-to 文本）
    urlIntentText = pureText
    urlCandidateText = pureText + "\n" + getReplyURLCandidateText(message)

    # 结构化切分一次、两用：拼 prompt 字符串 + 填展示字段（防双调用漂移）
    ctx = extractReplyTextContext(message, pureText)
    pureText = _assembleReplyContext(ctx, pureText)

    return PromptPayload(
        pureText=pureText,
        includeContext=includeContext,
        urlIntentText=urlIntentText,
        urlCandidateText=urlCandidateText,
        replyLine=ctx.replyLine,
        currentText=ctx.currentText,
    )



def formatDisplayOriginalMsg(username: str, combinedText: str, allImages: list[dict]) -> str:
    if allImages:
        return f"@{username}：[附带 {len(allImages)} 张图片]\n{combinedText}"
    return f"@{username}：{combinedText}"