"""
handlers/afc.py

AFC 意图检测 handler

group=1 处理器，早于 llm (group=2)，检测用户消息中的 AFC 工具调用意图，
将工具上下文块推入 bot_data 推送层，供后续 LLM 调用时消费。

设计要点：
- 独占 group=1：确保对每条 TEXT 消息都运行（无同 group 竞争）
- 早于 llm：在 LLM 处理前完成工具上下文注入
- 扁平 key + 模块名槽位：bot_data[f"llm_extra_blocks_{chatID}"]["afc"] = (tier, label, content)
- 三层清理：生产端各清各槽 + 写入只动自己槽位 + 消费端整体 pop

handler 执行顺序：
  group -1: 消息收集器（旁路观测）
  group 0:  shutdown mention / 命令 / 回调（命中关键词抛 ApplicationHandlerStop 阻断后续）
  group 1:  本 handler（afc 意图检测，注入工具上下文）
  group 2:  llm 自动回复（消费 bot_data 推送层）
"""

from telegram import Update
from telegram.ext import MessageHandler, filters, ContextTypes

from utils.afc.afcIntent import detectTools
from utils.llm.afcApi import buildAFCContextBlock
from utils.llm.contextBuilder import ContextTier


_BOT_DATA_KEY = "llm_extra_blocks_{}"
_MODULE_SLOT = "afc"   # 本模块在推送层中的槽位名 = 本模块在 modulesRegistry 的 id




async def afcIntentDetector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    检测意图，将工具上下文块推入 bot_data 推送层

    group=1，先于 llm

    错误处理策略：
        AFC 的 handler 不使用 @handleTelegramErrors 装饰器，而采用静默降级策略。
        若 detectTools / buildAFCContextBlock 抛出异常，handler 自然终止，
        工具上下文块不写入 → LLM handler 正常执行，只是没有工具调用能力。

        这种设计避免 AFC 检测失败导致整个消息流中断（用户看到错误回复），
        而是让 LLM 以纯文本模式回复（降级但不失败）。
    """
    message = update.message or update.edited_message
    if not message or not message.text:
        return

    text = message.text
    chatID = str(message.chat_id)
    key = _BOT_DATA_KEY.format(chatID)

    # 清除本模块上一条消息遗留的槽位（只动自己的槽，不影响其它扩展模块）
    # 防止异常情况下未被消费端清理的块泄漏到本条消息
    slots = context.bot_data.get(key)
    if slots is not None:
        slots.pop(_MODULE_SLOT, None)

    # 检测意图（四级召回）
    toolNames = detectTools(text, chatID)
    if not toolNames:
        return

    # 构建工具上下文块
    block = buildAFCContextBlock(toolNames)
    if block:
        # 写入自己的槽位（以模块 id 为 key），不与其它扩展模块冲突
        context.bot_data.setdefault(key, {})[_MODULE_SLOT] = (ContextTier.TOOLS, "工具", block)




def register():
    return {
        "handlers": [
            {"handler": MessageHandler(filters.TEXT & ~filters.COMMAND, afcIntentDetector), "group": 1}
        ],
        "name": "AFC 意图检测",
        "description": "检测 AFC 调用意图，将工具上下文推入 LLM 上下文推送层",
        # group=1：独占，早于 llm (group=2)，晚于命令 /mention (group=0)
    }
