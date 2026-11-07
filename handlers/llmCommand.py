"""
handlers/llmCommand.py

Telegram 端 /llm 控制命令（ops 专用）。

支持：
    /llm                            查看 LLM 开关状态
    /llm -on | -off                 开启/关闭 LLM
    /llm status                     显示完整配置（开关、审核模式、模型、群聊触发、记忆、URL 读取）
    /llm model                      查看当前模型
    /llm model switch <name>        切换模型
    /llm model provider <配置组>     指定协议、凭据和端点
    /llm visionmodel ...            视觉模型配置
    /llm trigger mention|keyword    切换群聊触发模式
    /llm keyword [add|del <词>]     管理群聊触发关键词
    /llm memory                     查看记忆模式
    /llm memory -on | -off          开启/关闭记忆模式
    /llm url                        查看 URL 读取开关
    /llm url -on | -off             开启/关闭 URL 读取（详细数值 limits 仅控制台可改）

安全设计：
    - 所有 reply 均不传 parse_mode（plain text，规避 Markdown 注入）
    - 模型标识拒绝空白和控制字符，原样传递给上游
    - 鉴权统一由 hasPermission(userID, Permission.LLM) 把关
"""




from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from config import Permission

from utils.core.errorDecorators import handleTelegramErrors
from utils.llm import (
    getAutoMode,
    getLLMEnabled, setLLMEnabled,
    getForceFallbackPrompt, setForceFallbackPrompt,
    getGroupTriggerKeywords, getGroupTriggerMode, setGroupTriggerMode,
    addGroupTriggerKeyword, removeGroupTriggerKeyword,
    getMemoryEnabled, setMemoryEnabled,
    getModel, setModel,
    getVisionModel, setVisionModel,
    getModelProvider, setModelProvider,
    getVisionProvider, setVisionProvider, resetVisionModel,
    getURLReadEnabled, setURLReadEnabled,
)
from utils.core.logger import logAction, LogLevel, LogChildType
from utils.llm.client._router import describeRoute
from utils.operators import hasPermission


# autoMode 值到可读名称的映射（与 utils/llm/config.py 中的合法值对应）
_MODE_NAMES = {"on": "直接发送", "off": "Telegram 审核", "console": "控制台审核"}




@handleTelegramErrors(errorReply="呜哇……是奇奇怪怪的指令结果，正朝咱冲过来喵……！")
async def handleLLMCommand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/llm [子命令] — LLM 控制（仅 ops）"""
    userID = update.effective_user.id

    if not hasPermission(userID, Permission.LLM):
        await update.message.reply_text("👀 你在动，对吧 w……\n只有管理员才有权限执行这个操作哦……")
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
            await update.message.reply_text("LLM 、启动——！")
            await logAction("System", "Telegram 端开启 LLM", f"操作者：{operatorName}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)

        case "-off" | "off":
            setLLMEnabled(False)
            await update.message.reply_text("LLM 、关闭……！")
            await logAction("System", "Telegram 端关闭 LLM", f"操作者：{operatorName}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)

        case "fade":
            if len(args) == 1:
                await update.message.reply_text("……" if getForceFallbackPrompt() else "喵？")
            elif len(args) == 2:
                flag = args[1].lower()
                if flag in ("-on", "on"):
                    setForceFallbackPrompt(True)
                    await update.message.reply_text("……")
                    await logAction("System", "Telegram 端开启强制提示词回退", f"操作者：{operatorName}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
                elif flag in ("-off", "off"):
                    setForceFallbackPrompt(False)
                    await update.message.reply_text("……嗯？这是……没见过的参数喵……？")
                    await logAction("System", "Telegram 端关闭强制提示词回退", f"操作者：{operatorName}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
                else:
                    await update.message.reply_text("……嗯？这是……没见过的参数喵……？")

        case "status":
            autoMode = getAutoMode()
            vm = getVisionModel()
            m = getModel()
            triggerMode = getGroupTriggerMode()
            triggerModeNames = {"mention": "群聊触发需 @", "keyword": "群聊触发需要 @ 或关键词"}
            keywords = getGroupTriggerKeywords()
            text = (
                f"LLM 功能：{'开启喵' if getLLMEnabled() else '关闭喵'}\n"
                f"审核模式：{_MODE_NAMES.get(autoMode, autoMode)}\n"
                f"当前模型：{m}\n"
                f"主模型调用：{describeRoute(m, getModelProvider())}\n"
                f"视觉模型：{vm}\n"
                f"视觉模型调用：{describeRoute(vm, getVisionProvider())}\n"
                f"群聊触发：{triggerModeNames.get(triggerMode, triggerMode)}\n"
                f"触发关键词：{', '.join(keywords) if keywords else '……ないですニャー（目移'}\n"
                f"记忆模式：{'开启喵' if getMemoryEnabled() else '关闭喵'}\n"
                f"URL 读取：{'开启喵' if getURLReadEnabled() else '关闭喵'}"
            )
            await update.message.reply_text(text)

        case "model":
            if len(args) == 1 or (len(args) == 2 and args[1].lower() == "provider"):
                model = getModel()
                await update.message.reply_text(
                    f"当前模型：{model}\n调用配置组：{describeRoute(model, getModelProvider())}"
                )
            elif len(args) == 3 and args[1].lower() == "switch":
                try:
                    newModel = args[2]
                    setModel(newModel)
                except ValueError as error:
                    await update.message.reply_text(f"❌ {error}")
                    return
                await update.message.reply_text(f"模型已切换为：{newModel}")
                await logAction("System", "Telegram 端切换 LLM 模型", f"操作者：{operatorName}，模型：{newModel}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            elif len(args) == 3 and args[1].lower() == "provider":
                try:
                    setModelProvider(args[2].lower())
                except ValueError as error:
                    await update.message.reply_text(f"❌ {error}")
                    return
                await update.message.reply_text(
                    f"主模型调用配置组已更新：{describeRoute(getModel(), getModelProvider())}"
                )
                await logAction("System", "Telegram 端切换 LLM 调用配置组", f"操作者：{operatorName}，配置组：{args[2].lower()}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            else:
                await update.message.reply_text("用法：/llm model [switch <模型名称>|provider [配置组]]")

        case "visionmodel":
            if len(args) == 1 or (len(args) == 2 and args[1].lower() == "provider"):
                model = getVisionModel()
                await update.message.reply_text(
                    f"当前视觉模型：{model}\n调用配置组：{describeRoute(model, getVisionProvider())}"
                )
            elif len(args) == 3 and args[1].lower() == "switch":
                try:
                    newModel = args[2]
                    setVisionModel(newModel)
                except ValueError as error:
                    await update.message.reply_text(f"❌ {error}")
                    return
                await update.message.reply_text(f"视觉模型已切换为：{newModel}")
                await logAction("System", "Telegram 端切换 LLM 视觉模型", f"操作者：{operatorName}，模型：{newModel}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            elif len(args) == 3 and args[1].lower() == "provider":
                try:
                    setVisionProvider(args[2].lower())
                except ValueError as error:
                    await update.message.reply_text(f"❌ {error}")
                    return
                await update.message.reply_text(
                    f"视觉模型调用配置组已更新：{describeRoute(getVisionModel(), getVisionProvider())}"
                )
                await logAction("System", "Telegram 端切换 LLM 视觉调用配置组", f"操作者：{operatorName}，配置组：{args[2].lower()}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            elif len(args) == 2 and args[1].lower() == "reset":
                resetVisionModel()
                await update.message.reply_text("视觉模型名称和调用配置组已与主模型同步")
                await logAction("System", "Telegram 端同步 LLM 视觉模型", f"操作者：{operatorName}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            else:
                await update.message.reply_text("用法：/llm visionmodel [switch <模型名称>|provider [配置组]|reset]")

        case "trigger":
            modeNames = {"mention": "群聊触发需要 @", "keyword": "群聊触发需要 @ 或关键词"}
            if len(args) == 1:
                mode = getGroupTriggerMode()
                await update.message.reply_text(f"当前群聊触发模式是 {mode}（{modeNames.get(mode, mode)}）")
            elif len(args) == 2:
                mode = args[1].lower()
                try:
                    setGroupTriggerMode(mode)
                except ValueError:
                    await update.message.reply_text("trigger 的用法应该是：/llm trigger [{mention|keyword}]")
                    return
                await update.message.reply_text(f"群聊触发模式已切换为：{modeNames.get(mode, mode)}")
                await logAction("System", "Telegram 端切换 LLM 群聊触发模式", f"操作者：{operatorName}，模式：{mode}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            else:
                await update.message.reply_text("trigger 的用法应该是：/llm trigger [{mention|keyword}]")

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
                await update.message.reply_text("keyword 的用法应该是：/llm keyword [{add <关键词>|del <关键词>}]")

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
                    await update.message.reply_text("memory 的用法应该是：/llm memory [{-on|-off}]")

        case "url":
            if len(args) == 1:
                state = "开启" if getURLReadEnabled() else "关闭"
                await update.message.reply_text(f"URL 读取功能目前是 {state} 的喵")
            else:
                flag = args[1].lower()
                if flag in ("-on", "on"):
                    setURLReadEnabled(True)
                    await update.message.reply_text("URL 读取功能已开启喵")
                    await logAction("System", "Telegram 端开启 URL 读取", f"操作者：{operatorName}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
                elif flag in ("-off", "off"):
                    setURLReadEnabled(False)
                    await update.message.reply_text("URL 读取功能已关闭喵")
                    await logAction("System", "Telegram 端关闭 URL 读取", f"操作者：{operatorName}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
                else:
                    await update.message.reply_text("url 的用法应该是：/llm url [{-on|-off}]")

        case _:
            await update.message.reply_text(
                "/llm 指令有以下的用法喵——\n"
                "<pre>\n"
                "/llm                         查看开关状态\n"
                "/llm -on | -off              开启/关闭 LLM\n"
                "/llm status                  显示完整配置\n"
                "/llm model                   查看当前模型\n"
                "/llm model switch <名称>      切换模型\n"
                "/llm model provider <配置组>  指定调用配置组\n"
                "/llm visionmodel provider <配置组>  指定视觉调用配置组\n"
                "/llm fade -on | -off         开启/关闭“褪色”\n" 
                "/llm trigger                 查看群聊触发模式\n"
                "/llm trigger mention|keyword 切换群聊触发模式\n"
                "/llm keyword                 查看群聊触发关键词\n"
                "/llm keyword add|del &lt;词&gt;    管理群聊触发关键词\n"
                "/llm memory                  查看记忆模式\n"
                "/llm memory -on | -off       开启/关闭记忆\n"
                "/llm url                     查看 URL 读取功能\n"
                "/llm url -on | -off          开启/关闭 URL 读取\n"
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
