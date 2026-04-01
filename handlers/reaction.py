"""
handlers/reaction.py

记录消息 reaction 到聊天历史。
当用户对消息添加 reaction（如 ❤、👍、😂）时，
将 reaction 作为独立消息行存入 chatHistory，
供日后回顾和 LLM 上下文使用。
"""


from telegram import Update
from telegram.ext import ContextTypes, MessageReactionHandler

from utils.chatHistory import saveMessage




async def handleReaction(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """处理 reaction 变化事件。"""
    reaction = update.message_reaction
    if not reaction:
        return

    chatID = str(reaction.chat.id)
    user = reaction.user

    if not user:
        return

    # 计算新增的 reaction（忽略移除）
    oldEmojis = {r.emoji for r in reaction.old_reaction} if reaction.old_reaction else set()
    newEmojis = {r.emoji for r in reaction.new_reaction} if reaction.new_reaction else set()
    addedEmojis = newEmojis - oldEmojis

    if not addedEmojis:
        return

    username = user.username or user.first_name or str(user.id)

    for emoji in addedEmojis:
        await saveMessage(chatID, "reaction", username, f"回应了 {emoji}")




def register():
    return {
        "handlers": [
            MessageReactionHandler(handleReaction),
        ],
        "name": "Reaction 记录",
        "description": "记录消息 reaction 到聊天历史",
        "auth": False,
    }