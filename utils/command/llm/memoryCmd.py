"""
utils/command/llm/memoryCmd.py

/llm memory 子命令处理：模式开关、列表、增删改、管理界面。
"""

from handlers.cli import parseArgsTokens

from utils.core.logger import logAction, LogLevel, LogChildType
from utils.llm import (
    addMemory,
    deleteMemory,
    getMemories,
    getMemoryAutoApprove,
    getMemoryByID,
    getMemoryEnabled,
    isContextOnceSet,
    MEMORY_SCOPE_GLOBAL,
    setContextOnce,
    setMemoryAutoApprove,
    setMemoryEnabled,
    updateMemory,
)

from .._helpRender import renderSubcommands

# 子命令速查表（case _ 提示的数据源；新增子命令时同步此处与 match 分支）
_MEMORY_SUBCOMMANDS = {
    "-on | -off | -once": "开启 / 关闭记忆模式，或仅下一次带入历史",
    "-autoapprove": "切换记忆自动批准（跳过审核）",
    "list": "列出记忆条目",
    "add": "新增记忆条目",
    "edit": "编辑记忆条目",
    "del <id>": "删除一条记忆",
    "ui": "打开 Memory 管理界面",
}




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
            print(f"❌ 给出的参数 {val} 是无效的（{{-on|-off|-once|-autoapprove}}）\n")
        return

    match action:
        case "list":
            parsed = parseArgsTokens(
                {"scope": None, "id": None, "all": None, "limit": None},
                rest,
                {"s": "scope", "i": "id", "a": "all", "l": "limit"}
            )
            enabledOnly = parsed["all"] == None
            limit = int(parsed["limit"]) if parsed["limit"] and parsed["limit"] is not True else 0
            scopeType = parsed["scope"]
            scopeID = parsed["id"]
            if scopeType is True:
                scopeType = None
            if scopeID is True:
                scopeID = None
            if scopeType == MEMORY_SCOPE_GLOBAL:
                scopeID = "global"
            items = await getMemories(scopeType=scopeType, scopeID=scopeID, enabledOnly=enabledOnly, limit=limit)
            if not items:
                print("[memory] 没有找到条目\n")
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
            if not scopeType or scopeType is True or not content or content is True:
                print("memory add 的用法应该是：\n    /llm memory add -scope <{global|chat|user|session}> [-id <scopeID>] -text <content>")
                return
            scopeID = parsed["id"]
            if scopeID is True:
                scopeID = None
            memoryID = await addMemory(
                scopeType,
                scopeID,
                content,
                tags=[] if parsed["tags"] == [True] else parsed["tags"],
                priority=int(parsed["priority"]) if parsed["priority"] and parsed["priority"] is not True else 0,
                source=parsed["source"] if parsed["source"] and parsed["source"] is not True else "manual",
                enabled=parsed["off"] == None,
            )
            if memoryID is None:
                print("❌ memory 添加失败\n")
                return
            print(f"memory #{memoryID} 已添加\n")

        case "edit":
            parsed = parseArgsTokens(
                {"mid": None, "text": None, "tags": [], "priority": None, "enabled": None, "source": None},
                rest,
                {"m": "mid", "t": "text", "g": "tags", "p": "priority", "e": "enabled", "s": "source"}
            )
            memoryID = parsed["mid"]
            if not memoryID or memoryID is True:
                print("memory edit 的用法应该是：\n    /llm memory edit -mid <id> [-text <内容>] [-tags <标签...>] [-priority <n>] [-enabled <on|off>]")
                return
            enabled = None
            if parsed["enabled"] and parsed["enabled"] is not True:
                enabled = parsed["enabled"].lower() == "on"
            if not await getMemoryByID(int(memoryID)):
                print("❌ 记忆不存在喵\n")
                return
            ok = await updateMemory(
                int(memoryID),
                content=parsed["text"] if parsed["text"] and parsed["text"] is not True else None,
                tags=None if not parsed["tags"] or parsed["tags"] == [True] else parsed["tags"],
                priority=int(parsed["priority"]) if parsed["priority"] and parsed["priority"] is not True else None,
                enabled=enabled,
                source=parsed["source"] if parsed["source"] and parsed["source"] is not True else None,
            )
            print("memory 已更新\n" if ok else "❌ memory 更新失败\n")

        case "del":
            target = rest[0] if rest else None
            if not target:
                print("memory del 的用法应该是：\n    /llm memory del <id>")
                return
            if not await getMemoryByID(int(target)):
                print("❌ 记忆不存在喵\n")
                return
            ok = await deleteMemory(int(target))
            print("memory 已删除\n" if ok else "❌ memory 删除失败\n")

        case "ui":
            from utils.llm.memory.ui import memoryMenuController
            await memoryMenuController(app)

        case _:
            print(renderSubcommands("/llm memory 可用的子命令有", _MEMORY_SUBCOMMANDS))
