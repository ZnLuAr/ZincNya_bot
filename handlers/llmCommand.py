"""
handlers/llmCommand.py

Telegram 端 /llm 控制命令（ops 专用）。

支持：
    /llm                       查看 LLM 开关状态
    /llm -on | -off            开启/关闭 LLM
    /llm status                显示完整配置（开关、审核模式、模型、记忆）
    /llm model                 查看当前模型
    /llm model switch <name>   切换模型
    /llm memory                查看记忆模式
    /llm memory -on | -off     开启/关闭记忆模式

安全设计：
    - 所有 reply 均不传 parse_mode（plain text，规避 Markdown 注入）
    - 模型名经正则校验后才回显
    - 鉴权统一由 hasPermission(userID, Permission.LLM) 把关
"""




import re

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from config import Permission

from utils.core.errorDecorators import handleTelegramErrors
from utils.llm import (
    getAutoMode,
    getLLMEnabled, setLLMEnabled,
    getGroupTriggerKeywords, getGroupTriggerMode, setGroupTriggerMode,
    addGroupTriggerKeyword, removeGroupTriggerKeyword,
    getMemoryEnabled, setMemoryEnabled,
    getModel, setModel,
    getVisionModel, setVisionModel
)
from utils.logger import logAction, LogLevel, LogChildType
from utils.operators import hasPermission


# 模型名白名单正则：只允许安全字符集，防止 Markdown 注入并限制长度
# 覆盖 Anthropic 的实际命名规范（如 claude-sonnet-4-6、claude-haiku-4-5-20251001）
_MODEL_RE = re.compile(r'^[a-zA-Z0-9._\-]{1,64}$')

# autoMode 值到可读名称的映射（与 utils/llm/config.py 中的合法值对应）
_MODE_NAMES = {"on": "直接发送", "off": "Telegram 审核", "console": "控制台审核"}




@handleTelegramErrors(errorReply="呜哇……是奇奇怪怪的指令结果，朝咱冲过来喵……！")
async def handleLLMCommand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/llm [子命令] — LLM 控制（仅 ops）"""
    userID = update.effective_user.id

    if not hasPermission(userID, Permission.LLM):
        await update.message.reply_text("❌ もー、只有ご主人才有权限执行这个操作喵")
        return

    args = context.args or []
    operatorName = update.effective_user.full_name
    cmd = args[0].lower() if args else ""

    match cmd:
        case "":
            state = "开启" if getLLMEnabled() else "关闭"
            await update.message.reply_text(f"LLM 功能目前是 {state} 的喵")

        case "-on" | "on":
            setLLMEnabled(True)
            await update.message.reply_text("LLM 已开启喵")
            await logAction("System", "Telegram 端开启 LLM", f"操作者：{operatorName}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)

        case "-off" | "off":
            setLLMEnabled(False)
            await update.message.reply_text("LLM 已关闭喵")
            await logAction("System", "Telegram 端关闭 LLM", f"操作者：{operatorName}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)

        case "status":
            autoMode = getAutoMode()
            vm = getVisionModel()
            m = getModel()
            vmLabel = f"{vm}（双调用喵）" if vm != m else "与主模型一致（单调用喵）"
            triggerMode = getGroupTriggerMode()
            triggerModeNames = {"mention": "群聊触发需 @", "keyword": "群聊触发需要 @ 或关键词"}
            keywords = getGroupTriggerKeywords()
            text = (
                f"LLM 功能：{'开启喵' if getLLMEnabled() else '关闭喵'}\n"
                f"审核模式：{_MODE_NAMES.get(autoMode, autoMode)}\n"
                f"当前模型：{m}\n"
                f"视觉模型：{vmLabel}\n"
                f"群聊触发：{triggerModeNames.get(triggerMode, triggerMode)}\n"
                f"触发关键词：{', '.join(keywords) if keywords else '……ないですニャー（目移'}\n"
                f"记忆模式：{'开启喵' if getMemoryEnabled() else '关闭喵'}"
            )
            await update.message.reply_text(text)

        case "model":
            if len(args) == 1:
                await update.message.reply_text(f"当前模型：{getModel()}")
            elif len(args) >= 3 and args[1].lower() == "switch":
                newModel = args[2]
                if not _MODEL_RE.match(newModel):
                    await update.message.reply_text("❌ 模型名称格式无效喵、\n只允许字母、数字、. - _，且最长最长就只能到 64 字符了……")
                    return
                setModel(newModel)
                await update.message.reply_text(f"模型已切换为：{newModel}")
                await logAction("System", "Telegram 端切换 LLM 模型", f"操作者：{operatorName}，模型：{newModel}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            else:
                await update.message.reply_text("用法：/llm model 或 /llm model switch <模型名称>")

        case "visionmodel":
            if len(args) == 1:
                await update.message.reply_text(f"当前的视觉模型是：{getVisionModel()}")
            elif len(args) >= 3 and args[1].lower() == "switch":
                newVisionModel = args[2]
                if not _MODEL_RE.match(newVisionModel):
                    await update.message.reply_text("❌ 模型名称格式无效喵、\n只允许字母、数字、. - _，最长最长就只能到 64 字符了……")
                    return
                setVisionModel(newVisionModel)
                await update.message.reply_text(f"视觉模型已切换为：{newVisionModel}")
                await logAction("System", "Telegram 端切换 LLM 视觉模型", f"操作者：{operatorName}，模型：{newVisionModel}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            elif len(args) == 2 and args[1].lower() == "reset":
                setVisionModel(getModel())
                await update.message.reply_text(f"视觉模型已和主模型 {getModel()} 同步喵")
                await logAction("System", "Telegram 端同步 LLM 视觉模型", f"操作者：{operatorName}，模型：{getModel()}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            else:
                await update.message.reply_text("应该是 /llm visionmodel (reset) 或 /llm visionmodel switch <模型名称> 哦……")

        case "trigger":
            modeNames = {"mention": "群聊触发需要 @", "keyword": "群聊触发需要 @ 或关键词"}
            if len(args) == 1:
                mode = getGroupTriggerMode()
                await update.message.reply_text(f"当前群聊触发模式是 {mode}（{modeNames.get(mode, mode)}） 喵")
            elif len(args) == 2:
                mode = args[1].lower()
                try:
                    setGroupTriggerMode(mode)
                except ValueError:
                    await update.message.reply_text("应该是 /llm trigger mention 或 /llm trigger keyword 的……")
                    return
                await update.message.reply_text(f"群聊触发模式已切换为：{modeNames.get(mode, mode)}")
                await logAction("System", "Telegram 端切换 LLM 群聊触发模式", f"操作者：{operatorName}，模式：{mode}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            else:
                await update.message.reply_text("应该是 /llm trigger 或 /llm trigger mention|keyword 的……")

        case "keyword":
            keywords = getGroupTriggerKeywords()
            if len(args) == 1:
                await update.message.reply_text(f"当前群聊触发关键词：{', '.join(keywords) if keywords else '-'}")
            elif len(args) >= 3 and args[1].lower() in ("add", "del"):
                action = args[1].lower()
                keyword = args[2]
                try:
                    if action == "add":
                        addGroupTriggerKeyword(keyword)
                        await update.message.reply_text(f"已添加触发关键词：{keyword.strip().lower()}")
                        await logAction("System", "Telegram 端添加 LLM 群聊触发关键词", f"操作者：{operatorName}，关键词：{keyword}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
                    else:
                        removeGroupTriggerKeyword(keyword)
                        await update.message.reply_text(f"已删除触发关键词：{keyword.strip().lower()}")
                        await logAction("System", "Telegram 端删除 LLM 群聊触发关键词", f"操作者：{operatorName}，关键词：{keyword}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
                except ValueError as e:
                    await update.message.reply_text(f"❌ {e}")
            else:
                await update.message.reply_text("应该是 /llm keyword 或 /llm keyword add <关键词> 或 /llm keyword del <关键词> 的喵")

        case "memory":
            if len(args) == 1:
                state = "开启" if getMemoryEnabled() else "关闭"
                await update.message.reply_text(f"记忆模式目前是 {state} 的喵")
            else:
                flag = args[1].lower()
                if flag in ("-on", "on"):
                    setMemoryEnabled(True)
                    await update.message.reply_text("记忆模式已开启喵")
                    await logAction("System", "Telegram 端开启记忆模式", f"操作者：{operatorName}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
                elif flag in ("-off", "off"):
                    setMemoryEnabled(False)
                    await update.message.reply_text("记忆模式已关闭喵")
                    await logAction("System", "Telegram 端关闭记忆模式", f"操作者：{operatorName}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
                else:
                    await update.message.reply_text("用法喵——\n/llm memory -on 或 /llm memory -off")

        case _:
            await update.message.reply_text(
                "/llm 指令有以下的用法喵——\n"
                "<pre>\n"
                "/llm                         查看开关状态\n"
                "/llm -on | -off              开启/关闭 LLM\n"
                "/llm status                  显示完整配置\n"
                "/llm model                   查看当前模型\n"
                "/llm model switch &lt;名称&gt;      切换模型\n"
                "/llm trigger                 查看群聊触发模式\n"
                "/llm trigger mention|keyword 切换群聊触发模式\n"
                "/llm keyword                 查看群聊触发关键词\n"
                "/llm keyword add|del &lt;词&gt;    管理群聊触发关键词\n"
                "/llm memory                  查看记忆模式\n"
                "/llm memory -on | -off       开启/关闭记忆\n"
                "</pre>"
                "…… 果然你又忘了吧 0 w0",
                parse_mode="HTML",
            )




def register():
    return {
        "handlers": [CommandHandler("llm", handleLLMCommand)],
        "name": "LLM 命令",
        "description": "LLM 控制命令（ops 专用）",
        "auth": False,  # 内部自行检查 Permission.LLM
    }
