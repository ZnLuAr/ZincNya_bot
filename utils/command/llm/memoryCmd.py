"""
utils/command/llm/memoryCmd.py

/llm memory 子命令处理：模式开关、列表、增删改、管理界面。
"""

from handlers.cli import parseArgsTokens

from utils.core.logger import logAction, LogLevel, LogChildType
from utils.core.stateManager import getStateManager
from utils.llm import (
    addMemory,
    deleteMemory,
    getMemoryCounts,
    getMemories,
    getMemoryAutoApprove,
    getMemoryByID,
    getMemoryEnabled,
    getMemoryRetrievalMode,
    isContextOnceSet,
    loadCalibratedThresholds,
    MEMORY_MODE_CONTEXTUAL,
    MEMORY_SCOPE_GLOBAL,
    setContextOnce,
    setMemoryAutoApprove,
    setMemoryEnabled,
    setMemoryRetrievalMode,
    updateMemory,
)

from .._helpRender import renderSubcommands


# 子命令速查表（case _ 提示的数据源；新增子命令时同步此处与 match 分支）
_MEMORY_SUBCOMMANDS = {
    "-on | -off | -once": "开启 / 关闭记忆模式，或仅下一次带入历史",
    "-autoapprove": "切换记忆自动批准（跳过审核）",
    "list": "列出记忆条目及 mode/hint",
    "add": "新增记忆条目（支持 -mode / -hint）",
    "edit": "编辑记忆条目（支持 -mode / -hint / -clearhint）",
    "del <id>": "删除一条记忆",
    "retrieval <legacy|hybrid>": "切换检索模式（不会安装模型）",
    "status": "显示检索校准、运行时与缓存状态",
    "ui": "打开 Memory 管理界面",
}

_MEMORY_LIST_DEFAULTS = {
    "scope": None, "id": None, "all": None, "limit": None,
}
_MEMORY_LIST_ALIASES = {"s": "scope", "i": "id", "a": "all", "l": "limit"}
_MEMORY_ADD_DEFAULTS = {
    "scope": None, "id": None, "text": None, "tags": [],
    "priority": None, "source": None, "off": None,
    "mode": None, "hint": None,
}
_MEMORY_ADD_ALIASES = {
    "s": "scope", "i": "id", "t": "text", "g": "tags",
    "p": "priority", "o": "off", "m": "mode", "h": "hint",
}
_MEMORY_EDIT_DEFAULTS = {
    "mid": None, "text": None, "tags": [], "priority": None,
    "enabled": None, "source": None, "mode": None,
    "hint": None, "clearhint": None,
}
_MEMORY_EDIT_ALIASES = {
    "m": "mid", "t": "text", "g": "tags", "p": "priority",
    "e": "enabled", "s": "source", "h": "hint",
}

# 每个 schema 都会在调用时复制后交给 parseArgsTokens，避免解析器改写模块级默认值。




def _parseMemoryOptions(defaults, aliases, tokens):
    """
    按命令自己的 schema 解析参数，并避免复用可变的默认列表。

    `parseArgsTokens` 会原地填充传入的字典；而 `tags` 这类默认值是
    列表，所以这里为每次命令调用复制列表，避免一次调用的解析结果
    泄漏到下一次调用。
    """
    parsed = {
        key: (list(value) if isinstance(value, list) else value)
        for key, value in defaults.items()
    }
    return parseArgsTokens(parsed, tokens, aliases)


def _hasMemoryValue(value):
    """判断参数是否提供了具体值，而不是未出现或光杆 flag。"""
    return value is not None and value is not True


def _optionalMemoryValue(value, default=None):
    """取出带值参数；参数未出现或光杆出现时返回 `default`。"""
    return value if _hasMemoryValue(value) else default


async def _handleMemoryFlags(action):
    """处理 `-on`、`-off`、`-once` 和 `-autoapprove` 开关。"""
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


async def _handleMemoryList(args):
    """解析列表筛选条件，读取并渲染管理员可见的 memory 条目。"""
    parsed = _parseMemoryOptions(_MEMORY_LIST_DEFAULTS, _MEMORY_LIST_ALIASES, args)
    # `-all` 是光杆 flag；未出现时只显示启用中的条目。
    enabledOnly = parsed["all"] is None
    limit = int(parsed["limit"]) if _hasMemoryValue(parsed["limit"]) else 0
    scopeType = _optionalMemoryValue(parsed["scope"])
    scopeID = _optionalMemoryValue(parsed["id"])
    if scopeType == MEMORY_SCOPE_GLOBAL:
        # global scope 的数据库记录使用固定的 scope ID。
        scopeID = "global"
    items = await getMemories(
        scopeType=scopeType, scopeID=scopeID, enabledOnly=enabledOnly, limit=limit,
    )
    if not items:
        print("[memory] 没有找到条目\n")
        return
    print("[memory] 条目列表：")
    for item in items:
        tags = ", ".join(item["tags"]) if item["tags"] else "-"
        print(f"  #{item['id']} [{item['scope_type']}:{item['scope_id']}] {'ON' if item['enabled'] else 'OFF'} p={item['priority']} src={item['source']}")
        print(f"     {item['content']}")
        print(f"     tags: {tags}")
        print(f"     mode: {item.get('mode', MEMORY_MODE_CONTEXTUAL)}")
        print(f"     hint: {item.get('retrievalHint') or '-'}")
        print("---\n")
    print("\n\n")


async def _handleMemoryAdd(args):
    """解析新增命令，并将管理员输入交给 memory 数据库接口。"""
    parsed = _parseMemoryOptions(_MEMORY_ADD_DEFAULTS, _MEMORY_ADD_ALIASES, args)
    scopeType = parsed["scope"]
    content = parsed["text"]
    if not scopeType or scopeType is True or not content or content is True:
        print("memory add 的用法应该是：\n    /llm memory add -scope <{global|chat|user|session}> [-id <scopeID>] -text <content>")
        return
    if parsed["mode"] is True or parsed["hint"] is True:
        print("❌ -mode 与 -hint 都必须提供具体值的说\n")
        return
    scopeID = _optionalMemoryValue(parsed["id"])
    memoryID = await addMemory(
        scopeType,
        scopeID,
        # `-tags` 光杆时不创建布尔标签，仍按空标签列表处理。
        content,
        tags=[] if parsed["tags"] == [True] else parsed["tags"],
        priority=int(parsed["priority"]) if _hasMemoryValue(parsed["priority"]) else 0,
        source=_optionalMemoryValue(parsed["source"], "manual"),
        # add 命令使用 `-off` 表示创建后禁用，默认保持启用。
        enabled=parsed["off"] is None,
        mode=_optionalMemoryValue(parsed["mode"], MEMORY_MODE_CONTEXTUAL),
        retrievalHint=_optionalMemoryValue(parsed["hint"]),
    )
    if memoryID is None:
        print("❌ memory 添加失败\n")
        return
    print(f"memory #{memoryID} 已添加\n")


async def _handleMemoryEdit(args):
    """解析编辑命令，检查互斥选项后更新指定的 memory。"""
    parsed = _parseMemoryOptions(_MEMORY_EDIT_DEFAULTS, _MEMORY_EDIT_ALIASES, args)
    memoryID = parsed["mid"]
    if not memoryID or memoryID is True:
        print("memory edit 的用法应该是：\n    /llm memory edit -mid <id> [-text <内容>] [-tags <标签...>] [-priority <n>] [-enabled <on|off>]")
        return
    if parsed["hint"] is not None and parsed["clearhint"] is not None:
        print("❌ -hint 与 -clearhint 不能同时使用的说\n")
        return
    if parsed["hint"] is True:
        print("❌ -hint 必须提供非空说明；清空应使用 -clearhint\n")
        return
    if parsed["mode"] is True:
        print("❌ -mode 必须提供 contextual 或 pinned\n")
        return
    enabled = None
    if _hasMemoryValue(parsed["enabled"]):
        # 光杆和未出现都表示不修改；带值时沿用现有约定，仅 on 映射为启用。
        enabled = parsed["enabled"].lower() == "on"
    if not await getMemoryByID(int(memoryID)):
        print("❌ 记忆不存在喵\n")
        return
    ok = await updateMemory(
        int(memoryID),
        content=_optionalMemoryValue(parsed["text"]),
        tags=None if not parsed["tags"] or parsed["tags"] == [True] else parsed["tags"],
        priority=int(parsed["priority"]) if _hasMemoryValue(parsed["priority"]) else None,
        enabled=enabled,
        source=_optionalMemoryValue(parsed["source"]),
        mode=_optionalMemoryValue(parsed["mode"]),
        # 空字符串是 database.updateMemory 约定的“清空 hint”信号。
        retrievalHint="" if parsed["clearhint"] is not None else parsed["hint"],
    )
    print("memory 已更新\n" if ok else "❌ memory 更新失败\n")


async def _handleMemoryRetrieval(args):
    """查询或切换检索模式；切换配置不会自动安装模型。"""
    if not args:
        print(f"当前 memory 检索模式：{getMemoryRetrievalMode()}\n")
        return
    mode = args[0].lower()
    try:
        setMemoryRetrievalMode(mode)
        await logAction(
            "System",
            "LLM memory 检索模式切换",
            f"已切换为 {mode}（不自动安装模型）",
            LogLevel.INFO,
            LogChildType.WITH_ONE_CHILD,
        )
    except ValueError as exc:
        print(f"❌ {exc}\n")


async def _handleMemoryDelete(args):
    """校验目标条目存在后删除指定 memory。"""
    target = args[0] if args else None
    if not target:
        print("memory del 的用法应该是：\n    /llm memory del <id>")
        return
    if not await getMemoryByID(int(target)):
        print("❌ 记忆不存在喵\n")
        return
    ok = await deleteMemory(int(target))
    print("memory 已删除\n" if ok else "❌ memory 删除失败\n")




async def _handleMemoryCommand(args, app=None):
    """处理 `/llm memory` 的一级路由，不在此处承载具体业务逻辑。"""
    if not args:
        print(f"记忆模式：{'开启' if getMemoryEnabled() else '关闭'}")
        print(f"One-shot：{'已设置' if isContextOnceSet() else '未设置'}\n")
        return

    action = args[0].lower()
    rest = args[1:]

    if action.startswith("-"):
        await _handleMemoryFlags(action)
        return

    # 各子命令自行负责参数解析和业务错误提示；这里仅负责分派。
    match action:
        case "list":
            await _handleMemoryList(rest)
        case "add":
            await _handleMemoryAdd(rest)
        case "edit":
            await _handleMemoryEdit(rest)
        case "retrieval":
            await _handleMemoryRetrieval(rest)
        case "status":
            await _printMemoryStatus()
        case "del":
            await _handleMemoryDelete(rest)
        case "ui":
            from utils.llm.memory.ui import memoryMenuController
            await memoryMenuController(app)
        case _:
            print(renderSubcommands("/llm memory 可用的子命令有", _MEMORY_SUBCOMMANDS))




async def _printMemoryStatus():
    """汇总配置、校准、数据库计数和 runtime 状态，供管理员只读查看。"""
    mode = getMemoryRetrievalMode()
    thresholds, calibrationReason = loadCalibratedThresholds()
    counts = await getMemoryCounts()
    runtime = getStateManager().getMemoryRuntime()
    runtimeStatus = runtime.getStatus() if runtime is not None else None

    enabledCount = counts.get("enabled", 0)
    totalCount = counts.get("total", 0)
    cacheEntries = runtimeStatus.get("cacheEntries", 0) if runtimeStatus else 0
    coverage = (cacheEntries / enabledCount * 100) if enabledCount else 100.0

    print("[memory] 检索状态：")
    print(f"  配置模式：{mode}")
    if calibrationReason:
        print(f"  校准：不可用（{calibrationReason}）")
    else:
        thresholdText = ", ".join(
            f"{name}={value}" for name, value in thresholds.items()
        )
        print(f"  校准：可用（{thresholdText}）")
    print(f"  记忆条目：启用 {enabledCount} / 总计 {totalCount}")

    if runtimeStatus is None:
        print("  Runtime：未注册")
        print("  向量缓存：0 条（未启动编码器）")
    else:
        runtimeState = "运行中" if runtimeStatus.get("running") else "已停止"
        if runtimeStatus.get("closing"):
            runtimeState = "关闭中"
        print(
            f"  Runtime：{runtimeState}；编码器"
            f"{'已就绪' if runtimeStatus.get('encoderReady') else '未就绪'}"
        )
        print(
            f"  向量缓存：{cacheEntries}/{enabledCount} "
            f"({coverage:.1f}%)，{runtimeStatus.get('cacheBytes', 0)} bytes"
        )
        print(
            f"  队列：query={runtimeStatus.get('queryQueued', 0)}，"
            f"index={runtimeStatus.get('indexPending', 0)}，"
            f"oldest={runtimeStatus.get('oldestIndexAgeMs', 0.0):.1f} ms"
        )
        print(f"  最近运行时降级：{runtimeStatus.get('lastReason') or '-'}")

    if mode == "hybrid" and calibrationReason:
        print(f"  当前效果：hybrid 已配置，但检索会降级（{calibrationReason}）")
    elif mode == "hybrid" and (runtimeStatus is None or not runtimeStatus.get("encoderReady")):
        print("  当前效果：hybrid 已配置，但语义编码器尚未就绪")
    else:
        print(f"  当前效果：{mode}")
    print()
