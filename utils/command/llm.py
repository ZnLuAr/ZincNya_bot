"""
utils/command/llm.py

LLM 功能控制台命令：
    /llm on | off
    /llm auto -on | -off | -console
    /llm model [switch <model>]
    /llm memory -on | -off | -once | -autoapprove
    /llm memory list | add | edit | del | ui
    /llm status
    /llm review

控制台审核（/llm review）与 chatScreen 审核命令（:ra/:re/:rr/:rc/:rq）
均支持两类审核项：
    - reply：LLM 回复审核
    - memory：LLM 自主记忆操作审核（add/update/delete）
"""




import sys

from handlers.cli import parseArgsTokens
from utils.logger import logAction, LogLevel, LogChildType
from utils.llm import (
    getModel,
    setModel,
    addMemory,
    getAutoMode,
    setAutoMode,
    getMemories,
    deleteMemory,
    updateMemory,
    getLLMEnabled,
    getMemoryByID,
    setLLMEnabled,
    setContextOnce,
    getMemoryEnabled,
    isContextOnceSet,
    setMemoryEnabled,
    MEMORY_SCOPE_GLOBAL,
    getMemoryAutoApprove,
    setMemoryAutoApprove,
)
from config import LLM_RATE_LIMIT_SECONDS
from utils.llm.review import (
    reviewSend,
    reviewRetry,
    reviewCancel,
    reviewEditSubmit,
    canEditReviewItem,
    canRetryReviewItem,
    formatReviewItemText,
    getReviewItemActions,
)
from utils.llm.state import getReviewQueue




def _printStatus():
    """打印当前 LLM 配置状态"""
    print(f"  LLM 功能：{'开启' if getLLMEnabled() else '关闭'}")
    auto = getAutoMode()
    modeMap = {"on": "直接发送", "off": "Telegram 审核", "console": "控制台审核"}
    print(f"  审核模式：{modeMap.get(auto, auto)}")
    print(f"  当前模型：{getModel()}")
    print(f"  记忆模式：{'开启' if getMemoryEnabled() else '关闭'}")
    print(f"  记忆自动批准：{'开启' if getMemoryAutoApprove() else '关闭'}")
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

        case "memory":
            await _handleMemoryCommand(rest, app)

        case "status":
            _printStatus()

        case "review":
            await handleConsoleReview(app.bot)

        case _:
            print(f"❌ 是未知的子命令 {cmd} 喵")
            print("用法：/llm [on|off|auto|model|memory|status|review]\n")




async def _handleMemoryCommand(args, app=None):
    """处理 /llm memory 子命令。"""
    if not args:
        print(f"记忆模式：{'开启' if getMemoryEnabled() else '关闭'}")
        print(f"One-shot：{'已设置' if isContextOnceSet() else '未设置'}\n")
        return

    action = args[0].lower()
    rest = args[1:]

    if action.startswith("-"):
        val = action.lstrip("-")
        if val == "on":
            setMemoryEnabled(True)
            await logAction("System", "LLM 记忆模式开启", "OK", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
        elif val == "off":
            setMemoryEnabled(False)
            await logAction("System", "LLM 记忆模式关闭", "OK", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
        elif val == "once":
            setContextOnce()
            await logAction("System", "LLM one-shot 记忆已设置", "下一次调用将带入历史上下文", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
        elif val == "autoapprove":
            current = getMemoryAutoApprove()
            setMemoryAutoApprove(not current)
            newState = "开启" if not current else "关闭"
            await logAction("System", f"LLM 记忆自动批准{newState}", "OK", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
        else:
            print(f"❌ 无效的参数 {val} 喵（-on | -off | -once | -autoapprove）\n")
        return

    match action:
        case "list":
            parsed = parseArgsTokens(
                {"scope": None, "id": None, "all": None, "limit": None},
                rest,
                {"s": "scope", "i": "id", "a": "all", "l": "limit"}
            )
            enabledOnly = parsed["all"] == None
            limit = int(parsed["limit"]) if parsed["limit"] and parsed["limit"] != "NoValue" else 0
            scopeType = parsed["scope"]
            scopeID = parsed["id"]
            if scopeType == "NoValue":
                scopeType = None
            if scopeID == "NoValue":
                scopeID = None
            if scopeType == MEMORY_SCOPE_GLOBAL:
                scopeID = "global"
            items = await getMemories(scopeType=scopeType, scopeID=scopeID, enabledOnly=enabledOnly, limit=limit)
            if not items:
                print("[memory] 没有找到条目喵\n")
                return
            print("[memory] 条目列表：")
            for item in items:
                tags = ", ".join(item["tags"]) if item["tags"] else "-"
                print(f"  #{item['id']} [{item['scope_type']}:{item['scope_id']}] {'ON' if item['enabled'] else 'OFF'} p={item['priority']} src={item['source']}")
                print(f"     {item['content']}")
                print(f"     tags: {tags}")
                print("---\n")
            print("\n\n")

        case "add":
            parsed = parseArgsTokens(
                {"scope": None, "id": None, "text": None, "tags": [], "priority": None, "source": None, "off": None},
                rest,
                {"s": "scope", "i": "id", "t": "text", "g": "tags", "p": "priority", "o": "off"}
            )
            scopeType = parsed["scope"]
            content = parsed["text"]
            if not scopeType or scopeType == "NoValue" or not content or content == "NoValue":
                print("❌ 用法：/llm memory add -scope <global|chat|user|session> [-id <scopeID>] -text <content>\n")
                return
            scopeID = parsed["id"]
            if scopeID == "NoValue":
                scopeID = None
            memoryID = await addMemory(
                scopeType,
                scopeID,
                content,
                tags=[] if parsed["tags"] == ["NoValue"] else parsed["tags"],
                priority=int(parsed["priority"]) if parsed["priority"] and parsed["priority"] != "NoValue" else 0,
                source=parsed["source"] if parsed["source"] and parsed["source"] != "NoValue" else "manual",
                enabled=parsed["off"] == None,
            )
            if memoryID is None:
                print("❌ memory 添加失败喵\n")
                return
            print(f"✅ memory #{memoryID} 已添加\n")

        case "edit":
            parsed = parseArgsTokens(
                {"mid": None, "text": None, "tags": [], "priority": None, "enabled": None, "source": None},
                rest,
                {"m": "mid", "t": "text", "g": "tags", "p": "priority", "e": "enabled", "s": "source"}
            )
            memoryID = parsed["mid"]
            if not memoryID or memoryID == "NoValue":
                print("❌ 用法：/llm memory edit -mid <id> [-text ...] [-tags ...] [-priority n] [-enabled on|off]\n")
                return
            enabled = None
            if parsed["enabled"] and parsed["enabled"] != "NoValue":
                enabled = parsed["enabled"].lower() == "on"
            if not await getMemoryByID(int(memoryID)):
                print("❌ memory 不存在喵\n")
                return
            ok = await updateMemory(
                int(memoryID),
                content=parsed["text"] if parsed["text"] and parsed["text"] != "NoValue" else None,
                tags=None if not parsed["tags"] or parsed["tags"] == ["NoValue"] else parsed["tags"],
                priority=int(parsed["priority"]) if parsed["priority"] and parsed["priority"] != "NoValue" else None,
                enabled=enabled,
                source=parsed["source"] if parsed["source"] and parsed["source"] != "NoValue" else None,
            )
            print("✅ memory 已更新\n" if ok else "❌ memory 更新失败喵\n")

        case "del":
            target = rest[0] if rest else None
            if not target:
                print("❌ 用法：/llm memory del <id>\n")
                return
            if not await getMemoryByID(int(target)):
                print("❌ memory 不存在喵\n")
                return
            ok = await deleteMemory(int(target))
            print("✅ memory 已删除\n" if ok else "❌ memory 删除失败喵\n")

        case "ui":
            from utils.llm.memory.ui import memoryMenuController
            await memoryMenuController(app)

        case _:
            print("❌ 用法：/llm memory [-on|-off|-once|list|add|edit|del|ui]\n")




# ============================================================================
# 控制台审核处理
# ============================================================================

async def handleConsoleReview(bot):
    """
    独立 CLI 模式的控制台审核（/llm review）。

    从审核队列取一条消息，通过 asyncInput 交互后执行操作。
    支持 reply 和 memory 两类审核项。
    """
    from utils.inputHelper import asyncInput, asyncMultilineInput

    queue = getReviewQueue()

    if queue.empty():
        print("\n[审核队列] 目前没有待审核的消息喵~\n")
        return

    item = queue.get_nowait()
    kind = item.get("kind", "reply")
    actionsHint = getReviewItemActions(item)

    if kind == "memory":
        print(f"\n{formatReviewItemText(item)}\n")
        print(f"操作：{actionsHint} > ", end="")
    else:
        print(
            f"""
[消息待审核喵]
原始消息：{item['originalMsg']}

ZincNya 的回复：
{'-' * 30}

{item['reply']}

{'-' * 30}

操作：{actionsHint} > """,
end=""
        )

    sys.stdout.flush()

    try:
        choice = (await asyncInput("")).strip().lower()
    except Exception:
        queue.put_nowait(item)
        return

    if choice in ("a", ""):
        try:
            await reviewSend(bot, item)
        except Exception as e:
            print(f"[审核] 操作失败：{e}\n\n")
            queue.put_nowait(item)

    elif choice in ("e", "edit"):
        if not canEditReviewItem(item):
            print("[审核] 当前审核项不可编辑喵")
            queue.put_nowait(item)
            return

        if kind == "memory":
            currentContent = item.get("action", {}).get("content") or ""
            print(f"编辑记忆内容（当前：{currentContent[:100]}）：")
        else:
            print("编辑回复（Alt+Enter 提交，:q 取消）：")

        newText = await asyncMultilineInput(prompt=">> ", continuation_prompt=".. ")
        if newText.strip() and newText.strip() != ":q":
            try:
                editedItem = await reviewEditSubmit(item, newText.rstrip('\n'))
                if kind == "memory":
                    # console memory 编辑后立即批准
                    await reviewSend(bot, editedItem)
                    print("[审核] 记忆操作编辑后已执行")
                else:
                    await reviewSend(bot, editedItem)
                    print(f"[审核] 已发送编辑内容至 {item['chatID']}")
            except Exception as e:
                print(f"[审核] 操作失败：{e}")
                queue.put_nowait(item)
        else:
            print("[审核] 取消编辑喵")
            queue.put_nowait(item)

    elif choice in ("r", "retry"):
        if not canRetryReviewItem(item):
            print("[审核] 当前审核项不支持重试喵")
            queue.put_nowait(item)
            return

        print("[审核] 重新生成喵……")
        try:
            newQueueItem = await reviewRetry(item)
            queue.put_nowait(newQueueItem)
            print("[审核] 已重新生成喵，再次输入 :review 就可以查看了")
        except Exception as e:
            print(f"[审核] 重试失败喵：{e}\n\n")
            queue.put_nowait(item)

    elif choice in ("c", "cancel"):
        await reviewCancel(item)

    else:
        print(f"[审核] 无效选项：{choice!r}")
        queue.put_nowait(item)




async def handleChatScreenReviewCommand(command: str, bot, ui) -> dict | None:
    """
    chatScreen 内审核命令处理（:ra/:re/:rr/:rc/:rq）。

    参数:
        command: 用户输入的命令（已 strip）
        bot: Telegram Bot 实例
        ui: ChatScreenApp 实例

    返回:
        进入编辑模式时返回审核项 dict，否则返回 None。
    """
    queue = getReviewQueue()

    if command == ":rq":
        ui.showStatus(f" 待审核队列：{queue.qsize()} 条" if queue.qsize() > 0 else "审核队列为空")
        return None

    if queue.empty():
        ui.showStatus(" 审核队列空了喵")
        return None

    item = queue.get_nowait()
    kind = item.get("kind", "reply")

    if command == ":ra":
        try:
            await reviewSend(bot, item)
        except Exception as e:
            queue.put_nowait(item)
            ui.showStatus(f" 操作失败喵：{e}")

    elif command == ":re":
        if not canEditReviewItem(item):
            queue.put_nowait(item)
            ui.showStatus(" 当前审核项不可编辑喵")
            return None

        if kind == "memory":
            ui._composerArea.text = item.get("action", {}).get("content", "")
            ui.showStatus(" 记忆内容编辑审核中 | Ctrl+S 提交 | Esc 取消编辑")
        else:
            ui._composerArea.text = item["reply"]
            ui.showStatus(" LLM 生成消息编辑审核中 | Ctrl+S 提交 | Esc 取消编辑")
        return item

    elif command == ":rr":
        if not canRetryReviewItem(item):
            queue.put_nowait(item)
            ui.showStatus(" 当前审核项不支持重试喵")
            return None

        try:
            newQueueItem = await reviewRetry(item)
            queue.put_nowait(newQueueItem)
        except Exception as e:
            queue.put_nowait(item)
            ui.showStatus(f" LLM 消息生成重试失败喵：{e}")

    elif command == ":rc":
        await reviewCancel(item)

    return None


async def handleChatScreenEditSubmit(editItem: dict, editedText: str, ui) -> None:
    """
    chatScreen 编辑模式提交处理。

    参数:
        editItem: 正在编辑的审核项
        editedText: 用户编辑后的文本
        ui: ChatScreenApp 实例
    """
    queue = getReviewQueue()
    result = await reviewEditSubmit(editItem, editedText)
    queue.put_nowait(result)




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
            "/llm memory -on|-off|-once           开启/关闭记忆模式，或仅下一次带入历史\n"
            "/llm memory -autoapprove             切换记忆自动批准（跳过审核）\n"
            "/llm memory list [-all] [-scope <type> -id <id>] [-limit n]\n"
            "/llm memory add -scope <type> [-id <id>] -text <content> [-tags ...] [-priority n] [-off]\n"
            "/llm memory edit -mid <id> [-text ...] [-tags ...] [-priority n] [-enabled on|off]\n"
            "/llm memory del <id>                 删除一条 memory\n"
            "/llm memory ui                        打开 Memory 管理界面\n"
        ),
        "example": (
            "/llm status                          查看当前配置\n"
            "/llm on                              开启 LLM\n"
            "/llm auto -console                   切换到控制台审核模式\n"
            "/llm memory -on                      开启全局记忆模式\n"
            "/llm memory -autoapprove             切换记忆自动批准\n"
            "/llm memory list                     列出所有启用的 memory\n"
            "/llm memory add -scope global -text '偏好简体中文'\n"
            "/llm memory edit -mid 1 -priority 10\n"
            "/llm memory del 3                    删除 memory #3\n"
        ),
    }
