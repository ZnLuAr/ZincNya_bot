"""
loader.py

动态加载 handlers 文件夹中的所有 Telegram 处理器喵

本模块会扫描 handlers/ 文件夹，自动导入所有有 register() 函数的模块，
并将它们注册到 Telegram Application 中。

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
import logging
import pkgutil
import functools
import importlib

from telegram.ext import Application

from config import PROJECT_ROOT, AUTH_ENABLED

from utils.logger import sanitizeForLog
from utils.whitelistManager.data import whetherAuthorizedUser


# 明确跳过的模块（不是 Telegram handler 的模块）
SKIP_MODULES = ["cli"]

# handlers 目录的绝对路径（用于 pkgutil 扫描）
_HANDLERS_DIR = os.path.join(PROJECT_ROOT, "handlers")

_logger = logging.getLogger(__name__)




def _wrapWithAuth(handlerFunc):
    """包装 handler，在执行前检查白名单"""
    @functools.wraps(handlerFunc)
    async def wrapper(update, context, *args, **kwargs):
        user = update.effective_user
        if user and not whetherAuthorizedUser(user.id):
            _logger.warning(
                "未授权访问: %s (@%s / %s)",
                sanitizeForLog(user.full_name),
                sanitizeForLog(user.username or "N/A"),
                user.id
            )
            return
        return await handlerFunc(update, context, *args, **kwargs)
    return wrapper




def loadHandlers(app: Application):
    """动态加载 handlers 文件夹中的所有 Telegram 处理器喵"""

    package = "handlers"
    loadedModules = []
    skippedModules = []
    totalHandlers = 0

    print("正在加载 Telegram 处理器喵……\n")

    for _ , moduleName , _ in pkgutil.iter_modules([_HANDLERS_DIR]):

        # 跳过明确排除的模块
        if moduleName in SKIP_MODULES:
            skippedModules.append(f"{moduleName} (非 Telegram handler)")
            continue


        try:
            module = importlib.import_module(f"{package}.{moduleName}")

            if not hasattr(module, "register"):
                skippedModules.append(f"{moduleName} (无 register 函数)")
                continue

            # 调用 register() 获取 handlers
            result = module.register()

            # 支持两种返回格式
            if isinstance(result , dict):
                # 元数据格式
                handlers = result.get("handlers" , [])
                name = result.get("name" , moduleName)
                description = result.get("description" , "")
                requireAuth = result.get("auth" , True)  # 默认需要白名单鉴权
            else:
                # 简单格式（向后兼容）
                handlers = result
                name = moduleName
                description = ""
                requireAuth = True

            # 注册 handlers（按需包装白名单鉴权）
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
            handlerCount = 0
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
                "name": name,
                "module": moduleName,
                "description": description,
                "handlerCount": handlerCount,
            })

            # 输出加载信息
            if description:
                print(f"  {name} ({handlerCount} 个) - {description}")
            else:
                print(f"  {name} ({handlerCount} 个)")

        except Exception as e:
            print(f"  加载 {moduleName} 失败喵：{e}")

    # 输出统计信息
    print()
    print(f"已加载 {len(loadedModules)} 个模块，共 {totalHandlers} 个处理器——")

    if skippedModules:
        print(f"跳过了 {len(skippedModules)} 个模块：{', '.join(skippedModules)}")

    print()

    return loadedModules
