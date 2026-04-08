"""
utils/command/llm.py

LLM 功能控制台命令：
    /llm on | off
    /llm auto -on | -off | -console
    /llm model [switch <model>]
    /llm memory -on | -off | -once
    /llm status
    /llm review
"""




import sys

from handlers.cli import parseArgsTokens
from utils.logger import logAction, LogLevel, LogChildType
from utils.llm import (
    getLLMEnabled,
    setLLMEnabled,
    getAutoMode,
    setAutoMode,
    getModel,
    setModel,
    getMemoryEnabled,
    setMemoryEnabled,
    setContextOnce,
    isContextOnceSet,
    addMemory,
    getMemoryByID,
    getMemories,
    updateMemory,
    deleteMemory,
    MEMORY_SCOPE_GLOBAL,
)
from config import LLM_RATE_LIMIT_SECONDS
from utils.llm.state import getReviewQueue




def _printStatus():
    """打印当前 LLM 配置状态"""
    print(f"  LLM 功能：{'开启' if getLLMEnabled() else '关闭'}")
    auto = getAutoMode()
    modeMap = {"on": "直接发送", "off": "Telegram 审核", "console": "控制台审核"}
    print(f"  审核模式：{modeMap.get(auto, auto)}")
    print(f"  当前模型：{getModel()}")
    print(f"  记忆模式：{'开启' if getMemoryEnabled() else '关闭'}")
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
        else:
            print(f"❌ 无效的参数 {val} 喵（-on | -off | -once）\n")
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
# 控制台审核处理（由 chatScreen 调用）
# ============================================================================

async def handleConsoleReview(bot):
    """
    处理控制台审核队列。

    在 chatScreen 中输入 :review 时调用此函数。

    流程：
    1. 从 getReviewQueue() 获取队列
    2. 如果队列为空，打印提示并返回
    3. 弹出一条待审核消息
    4. 等待用户输入 A/E/R/C
    5. 根据选择执行对应操作
    """
    from utils.llm import generateReply
    from utils.inputHelper import asyncInput

    queue = getReviewQueue()

    if queue.empty():
        print("\n[审核队列] 目前没有待审核的消息喵~\n")
        return

    item = queue.get_nowait()

    print(
        f"""
[消息待审核喵]
原始消息：{item['originalMsg']}

ZincNya 的回复：
{'-' * 30}

{item['reply']}

{'-' * 30}

操作：[A]ccept / [E]dit / [R]etry / [C]ancel > """,
end=""
    )
    
    sys.stdout.flush()

    try:
        choice = (await asyncInput("")).strip().lower()
    except Exception:
        # 放回队列
        queue.put_nowait(item)
        return

    if choice in ("a", ""):
        # 发送
        try:
            await bot.send_message(chat_id=item["chatID"], text=item["reply"])
            await logAction("System", "LLM 控制台审核", f"发送：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)
        except Exception as e:
            print(f"[审核] ❌ 发送失败：{e}\n\n")
            queue.put_nowait(item)

    elif choice in ("e", "edit"):
        # 编辑后发送
        print("编辑回复（Alt+Enter 提交，:q 取消）：")
        from utils.inputHelper import asyncMultilineInput
        newText = await asyncMultilineInput(prompt=">> ", continuation_prompt=".. ")
        if newText.strip() and newText.strip() != ":q":
            try:
                await bot.send_message(chat_id=item["chatID"], text=newText.rstrip('\n'))
                print(f"[审核] ✅ 已发送编辑内容至 {item['chatID']}")
                await logAction("System", "LLM 控制台审核：编辑发送", f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD)
                await logAction("System", "", f"发送内容：{newText.rstrip(chr(10))}", LogLevel.INFO, LogChildType.LAST_CHILD)
            except Exception as e:
                print(f"[审核] ❌ 发送失败：{e}")
                queue.put_nowait(item)
        else:
            print("[审核] 取消编辑喵")
            queue.put_nowait(item)

    elif choice in ("r", "retry"):
        # 重新生成
        print("[审核] 重新生成喵……")
        try:
            newReply = await generateReply(
                item["originalMsg"],
                item["chatID"],
                includeContext=bool(item.get("includeContext")),
                userID=item.get("userID"),
            )
            # 放回队列，重新显示
            queue.put_nowait({**item, "reply": newReply})
            print(f"[审核] 🔄 已重新生成喵，再次输入 :review 就可以查看了")
            await logAction("System", "LLM 控制台审核：重新生成", f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_CHILD)
            await logAction("System", "", f"生成的消息：{newReply}", LogLevel.INFO, LogChildType.LAST_CHILD)
        except Exception as e:
            print(f"[审核] ❌ 重试失败喵：{e}\n\n")
            queue.put_nowait(item)

    elif choice in ("c", "cancel"):
        await logAction("System", "LLM 控制台审核：取消", f"原文：{item['originalMsg']}", LogLevel.INFO, LogChildType.WITH_ONE_CHILD)

    else:
        print(f"[审核] ❌ 无效选项：{choice!r}（A/E/R/C）")
        queue.put_nowait(item)




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
            "/llm memory list                     列出所有启用的 memory\n"
            "/llm memory add -scope global -text '偏好简体中文'\n"
            "/llm memory edit -mid 1 -priority 10\n"
            "/llm memory del 3                    删除 memory #3\n"
        ),
    }
