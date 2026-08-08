"""
utils/command/llm/_dispatch.py

/llm 主入口分发 + 状态汇总 + 帮助文本。

execute() 用 match 分发所有子命令：内联 case 直接操作 utils.llm 的 getter/setter，
memory / knowledge / review 三个 case 委托给同包的子处理器。
"""

from config import LLM_RATE_LIMIT_SECONDS

from utils.core.logger import logAction, LogLevel, LogChildType
from utils.llm import (
    addGroupTriggerKeyword,
    getAutoMode,
    getGroupTriggerKeywords,
    getGroupTriggerMode,
    getKnowledgeEnabled,
    getLLMEnabled,
    getMemoryAutoApprove,
    getMemoryEnabled,
    getModel,
    getVisionModel,
    isContextOnceSet,
    removeGroupTriggerKeyword,
    setAutoMode,
    setGroupTriggerMode,
    setLLMEnabled,
    setModel,
    setVisionModel,
)
from utils.llm.state import getReviewQueue

from .knowledgeCmd import _handleKnowledgeCommand
from .memoryCmd import _handleMemoryCommand
from .review.consoleReview import handleConsoleReview




def _printStatus():
    """打印当前 LLM 配置状态"""
    print(f"  LLM 功能：{'开启' if getLLMEnabled() else '关闭'}")
    auto = getAutoMode()
    modeMap = {"on": "直接发送", "off": "Telegram 审核", "console": "控制台审核"}
    print(f"  审核模式：{modeMap.get(auto, auto)}")
    print(f"  当前模型：{getModel()}")
    visionModel = getVisionModel()
    if visionModel != getModel():
        print(f"  视觉模型：{visionModel}（双调用）")
    else:
        print(f"  视觉模型：与主模型一致（单调用）")
    triggerMode = getGroupTriggerMode()
    triggerModeMap = {"mention": "群聊需 @", "keyword": "群聊 @ 或关键词"}
    keywords = getGroupTriggerKeywords()
    print(f"  群聊触发：{triggerModeMap.get(triggerMode, triggerMode)}")
    print(f"  触发关键词：{', '.join(keywords) if keywords else '-'}")
    print(f"  记忆模式：{'开启' if getMemoryEnabled() else '关闭'}")
    print(f"  记忆自动批准：{'开启' if getMemoryAutoApprove() else '关闭'}")
    print(f"  知识库：{'开启' if getKnowledgeEnabled() else '关闭'}")
    print(f"  One-shot：{'已设置（下次调用生效）' if isContextOnceSet() else '未设置'}")
    print(f"  速率限制：{LLM_RATE_LIMIT_SECONDS} 秒")
    qsize = getReviewQueue().qsize()
    print(f"  待审核队列：{qsize} 条\n")




async def execute(app, args):
    if not args:
        _printStatus()
        return

    cmd = args[0].lower()
    rest = args[1:]

    match cmd:
        case "on":
            setLLMEnabled(True)
            await logAction("System", "开启 LLM 功能", "", LogLevel.INFO, LogChildType.NONE)

        case "off":
            setLLMEnabled(False)
            await logAction("System", "关闭 LLM 功能", "", LogLevel.INFO, LogChildType.NONE)

        case "auto":
            if not rest:
                print(f"❌ 需要子参数哦：-on | -off | -console（当前：{getAutoMode()}）\n")
                return
            mode = rest[0].lstrip("-").lower()
            try:
                setAutoMode(mode)
                modeNames = {"on": "直接发送", "off": "Telegram 审核", "console": "控制台审核"}
                await logAction("System", f"LLM 审核模式切换", f"已切换为 {modeNames.get(mode, mode)}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            except ValueError:
                print(f"❌ 无效的审核模式：{mode}（可选：on / off / console）\n\n")

        case "model":
            if not rest:
                print(f"当前模型：{getModel()}")
                return
            if rest[0].lower() == "switch" and len(rest) > 1:
                newModel = rest[1]
                setModel(newModel)
                await logAction("System", f"LLM 模型切换", f"已切换为：{newModel}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            else:
                print(f"当前模型：{getModel()}\n\n")

        case "visionmodel":
            if not rest:
                vm = getVisionModel()
                m = getModel()
                mode = "双调用喵" if vm != m else "单调用喵"
                print(f"视觉模型：{vm}（{mode}）")
                return
            if rest[0].lower() == "switch" and len(rest) > 1:
                newModel = rest[1]
                setVisionModel(newModel)
                mode = "双调用喵" if newModel != getModel() else "单调用喵"
                await logAction("System", f"LLM 视觉模型切换", f"已切换为：{newModel}（{mode}）", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            elif rest[0].lower() == "reset":
                setVisionModel(getModel())
                await logAction("System", f"LLM 视觉模型重置", f"已重置为主模型（单调用）", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            else:
                print(f"视觉模型：{getVisionModel()}\n\n")

        case "trigger":
            modeNames = {"mention": "群聊触发需要 @", "keyword": "群聊触发需要 @ 或关键词"}
            if not rest:
                mode = getGroupTriggerMode()
                print(f"群聊触发模式：{mode}（{modeNames.get(mode, mode)}）\n")
                return
            mode = rest[0].lower()
            try:
                setGroupTriggerMode(mode)
                await logAction("System", "LLM 群聊触发模式切换", f"已切换为：{modeNames.get(mode, mode)}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
            except ValueError:
                print("❌ 无效的触发模式喵……目前只支持 mention / keyword\n")

        case "keyword":
            keywords = getGroupTriggerKeywords()
            if not rest:
                print(f"群聊触发关键词：{', '.join(keywords) if keywords else '(……ないですニャー w)'}\n")
                return
            action = rest[0].lower()
            if action in ("add", "del") and len(rest) > 1:
                keyword = rest[1]
                try:
                    if action == "add":
                        addGroupTriggerKeyword(keyword)
                        await logAction("System", "LLM 群聊触发关键词添加", keyword, LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
                    else:
                        removeGroupTriggerKeyword(keyword)
                        await logAction("System", "LLM 群聊触发关键词删除", keyword, LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
                except ValueError as e:
                    print(f"❌ {e}\n")
            else:
                print("用法：/llm keyword | /llm keyword add <关键词> | /llm keyword del <关键词>\n")

        case "memory":
            await _handleMemoryCommand(rest, app)

        case "status":
            _printStatus()

        case "review":
            await handleConsoleReview(app.bot)

        case "knowledge":
            await _handleKnowledgeCommand(rest)

        case _:
            print(f"❌ 是未知的子命令 {cmd} 喵")
            print("用法：/llm [on|off|auto|model|visionmodel|trigger|keyword|memory|knowledge|status|review]\n")




def getHelp():
    return {
        "name": "/llm",
        "description": "控制 LLM 功能开关、审核模式和模型选择",
        "usage": (
            "/llm on                              开启 LLM\n"
            "/llm off                             关闭 LLM\n"
            "/llm status                          显示当前配置\n"
            "/llm auto -on|-off|-console          切换审核模式\n"
            "/llm model [switch <model>]          显示或切换模型\n"
            "/llm visionmodel [switch <model>]    显示或切换视觉模型\n"
            "/llm visionmodel reset               重置为主模型（单调用）\n"
            "/llm trigger [mention|keyword]       显示或切换群聊触发模式\n"
            "/llm keyword [add|del <关键词>]       管理群聊触发关键词\n"
            "/llm memory -on|-off|-once           开启/关闭记忆模式，或仅下一次带入历史\n"
            "/llm memory -autoapprove             切换记忆自动批准（跳过审核）\n"
            "/llm memory del <id>                 删除一条 memory\n"
            "/llm memory ui                       打开 Memory 管理界面\n"
            "/llm memory list [-all] [-scope <type> -id <id>] [-limit n]\n"
            "/llm memory add -scope <type> [-id <id>] -text <content> [-tags ...] [-priority n] [-off]\n"
            "/llm memory edit -mid <id> [-text ...] [-tags ...] [-priority n] [-enabled on|off]\n"
            "/llm knowledge on|off                开启/关闭知识库\n"
            "/llm knowledge reindex [--force]     重建知识库索引\n"
            "/llm knowledge list [category]       列出知识库条目\n"
            "/llm knowledge stats                 显示知识库统计\n"
            "/llm knowledge search <query>        测试检索（显示评分）\n"
            "/llm knowledge maxresults <n>        设置召回数（1-10）\n"
            "/llm knowledge minscore <float>      设置最低分数阈值\n"
        ),
        "example": (
            "/llm status                          查看当前配置\n"
            "/llm on                              开启 LLM\n"
            "/llm auto -console                   切换到控制台审核模式\n"
            "/llm visionmodel reset               重置为主模型（单调用）\n"
            "/llm trigger keyword                 使用 @ 或关键词触发群聊 LLM\n"
            "/llm keyword add 锌酱                添加群聊触发关键词\n"
            "/llm memory -autoapprove             切换记忆自动批准\n"
            "/llm memory list                     列出所有启用的 memory\n"
            "/llm memory del 3                    删除 memory #3\n"
            "/llm memory edit -mid 1 -priority 10\n"
            "/llm visionmodel switch claude-sonnet-4-6\n"
            "/llm memory add -scope global -text '偏好简体中文'\n"
            "/llm knowledge reindex --force       强制重建知识库索引\n"
            "/llm knowledge search 编程语言       测试检索相关条目\n"
            "/llm knowledge maxresults 5          设置召回 5 条\n"
        ),
    }
