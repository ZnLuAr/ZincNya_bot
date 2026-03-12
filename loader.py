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
import functools
import importlib
import pkgutil
from telegram.ext import Application

from config import PROJECT_ROOT, AUTH_ENABLED
from utils.whitelistManager.data import whetherAuthorizedUser
from utils.logger import sanitizeForLog


# 明确跳过的模块（不是 Telegram handler 的模块）
SKIP_MODULES = ["cli"]

# handlers 目录的绝对路径（用于 pkgutil 扫描）
_HANDLERS_DIR = os.path.join(PROJECT_ROOT, "handlers")

_logger = logging.getLogger(__name__)




def _wrapWithAuth(handler_func):
    """包装 handler，在执行前检查白名单"""
    @functools.wraps(handler_func)
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
        return await handler_func(update, context, *args, **kwargs)
    return wrapper




def loadHandlers(app: Application):
    """动态加载 handlers 文件夹中的所有 Telegram 处理器喵"""

    package = "handlers"
    loaded_modules = []
    skipped_modules = []
    total_handlers = 0

    print("正在加载 Telegram 处理器喵……\n")

    for _ , module_name , _ in pkgutil.iter_modules([_HANDLERS_DIR]):

        # 跳过明确排除的模块
        if module_name in SKIP_MODULES:
            skipped_modules.append(f"{module_name} (非 Telegram handler)")
            continue


        try:
            module = importlib.import_module(f"{package}.{module_name}")

            if not hasattr(module, "register"):
                skipped_modules.append(f"{module_name} (无 register 函数)")
                continue

            # 调用 register() 获取 handlers
            result = module.register()

            # 支持两种返回格式
            if isinstance(result , dict):
                # 元数据格式
                handlers = result.get("handlers" , [])
                name = result.get("name" , module_name)
                description = result.get("description" , "")
                requireAuth = result.get("auth" , True)  # 默认需要白名单鉴权
            else:
                # 简单格式（向后兼容）
                handlers = result
                name = module_name
                description = ""
                requireAuth = True

            # 注册 handlers（按需包装白名单鉴权）
            handler_count = 0
            for handler in handlers:
                if requireAuth and AUTH_ENABLED and hasattr(handler, "callback"):
                    handler.callback = _wrapWithAuth(handler.callback)
                app.add_handler(handler)
                handler_count += 1
                total_handlers += 1

            # 记录加载信息
            loaded_modules.append({
                "name": name,
                "module": module_name,
                "description": description,
                "handler_count": handler_count,
            })

            # 输出加载信息
            if description:
                print(f"  {name} ({handler_count} 个) - {description}")
            else:
                print(f"  {name} ({handler_count} 个)")

        except Exception as e:
            print(f"  加载 {module_name} 失败喵：{e}")

    # 输出统计信息
    print()
    print(f"已加载 {len(loaded_modules)} 个模块，共 {total_handlers} 个处理器——")

    if skipped_modules:
        print(f"跳过了 {len(skipped_modules)} 个模块：{', '.join(skipped_modules)}")

    print()

    return loaded_modules
