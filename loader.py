"""
loader.py

按模块注册表加载 Telegram 处理器喵

本模块从 utils.moduleManager.getAllModules() 读取已启用的模块列表，
逐个导入其声明的 handler 文件并调用 register() 注册到 Telegram Application。
已禁用的模块会被整体跳过；模块定义参见 modulesRegistry.py。

register() 函数可以返回两种格式：
    1. 简单格式（向后兼容）：直接返回 handlers 列表
       def register():
           return [CommandHandler("nya", sendNya)]

    2. 元数据格式（推荐）：返回包含元数据的字典
       def register():
           return {
               "handlers": [CommandHandler("nya", sendNya)],
               "name": "Nya 语录",
               "description": "随机发送 ZincNya 语录喵",
           }
"""

import os
import functools
import importlib

from telegram.ext import Application

from config import PROJECT_ROOT, AUTH_ENABLED

from utils.core.stateManager import safePrint
from utils.core.logger import sanitizeForLog
from utils.whitelistManager.data import whetherAuthorizedUser


# 明确跳过的模块（不是 Telegram handler 的模块）
SKIP_MODULES = ["cli"]




def _wrapWithAuth(handlerFunc):
    """包装 handler，在执行前检查白名单"""
    @functools.wraps(handlerFunc)
    async def wrapper(update, context, *args, **kwargs):
        user = update.effective_user
        if user and not whetherAuthorizedUser(user.id):
            safePrint(
                f"[WARNING] 未授权访问: {sanitizeForLog(user.full_name)} "
                f"(@{sanitizeForLog(user.username or 'N/A')} / {user.id})"
            )
            return
        return await handlerFunc(update, context, *args, **kwargs)
    return wrapper




def loadHandlers(app: Application):
    """按模块注册表加载已启用模块的 Telegram 处理器"""
    from utils.moduleManager import getAllModules

    loadedModules = []
    skippedModules = []
    totalHandlers = 0

    print("正在加载 Telegram 处理器喵……\n")

    allModules = getAllModules()

    for moduleName, config in allModules.items():
        # 跳过已禁用的模块
        if not config.get("enabled", True):
            skippedModules.append(f"{moduleName} (已禁用)")
            continue

        metadata = config["metadata"]
        handlerFiles = metadata.get("handlers", [])

        # 没有 handler 的模块（如纯 utils 模块）
        if not handlerFiles:
            continue

        try:
            handlerCount = 0

            # 遍历所有 handler 文件
            for handlerFile in handlerFiles:
                # 从 "handlers/llm.py" 提取 "llm"
                handlerModuleName = os.path.splitext(os.path.basename(handlerFile))[0]

                # 跳过明确排除的模块（如 cli）
                if handlerModuleName in SKIP_MODULES:
                    continue

                module = importlib.import_module(f"handlers.{handlerModuleName}")

                if not hasattr(module, "register"):
                    print(f"  [WARNING] {handlerFile} 无 register 函数")
                    continue

                # 调用 register() 获取 handlers
                result = module.register()

                # 支持两种返回格式（保持向后兼容）
                if isinstance(result, dict):
                    handlers = result.get("handlers", [])
                    requireAuth = result.get("auth", True)
                else:
                    handlers = result
                    requireAuth = True

                # 注册 handlers（原有逻辑）
                #
                # handlers 列表中的每一项可以是：
                #   - 普通 Handler 对象（注册到默认 group 0）
                #   - {"handler": Handler, "group": int}（注册到指定 group）
                #
                # PTB group 机制：
                #   同一 group 内只有第一个匹配的 handler 执行；
                #   不同 group 各自独立处理同一条消息，按 group 编号从小到大依次执行。
                #   在某 group 的 handler 中抛出 ApplicationHandlerStop 可阻止所有后续 group 处理该消息。
                #
                for item in handlers:
                    if isinstance(item, dict) and "handler" in item:
                        handler = item["handler"]
                        group = item.get("group", 0)
                    else:
                        handler = item
                        group = 0

                    if requireAuth and AUTH_ENABLED and hasattr(handler, "callback"):
                        handler.callback = _wrapWithAuth(handler.callback)

                    app.add_handler(handler, group)
                    handlerCount += 1
                    totalHandlers += 1

            # 记录加载信息
            loadedModules.append({
                "name": metadata["name"],
                "module": moduleName,
                "description": metadata.get("description", ""),
                "handlerCount": handlerCount,
            })

            # 输出加载信息
            if metadata.get("description"):
                print(f"  {metadata['name']} ({handlerCount} 个) - {metadata['description']}")
            else:
                print(f"  {metadata['name']} ({handlerCount} 个)")

        except Exception as e:
            print(f"  加载 {moduleName} 失败喵：{e}")

    # 输出统计信息
    print()
    print(f"已加载 {len(loadedModules)} 个模块，共 {totalHandlers} 个处理器——")

    if skippedModules:
        print(f"跳过了 {len(skippedModules)} 个模块：{', '.join(skippedModules)}")

    print()

    return loadedModules
