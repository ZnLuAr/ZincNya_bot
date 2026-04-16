"""
handlers/start.py

处理 /start 命令，用于白名单鉴权和欢迎消息。
"""

from telegram.ext import CommandHandler

from utils.whitelistManager import handleStart


def register():
    """注册 /start handler"""

    return {
        "handlers": [
            CommandHandler("start", handleStart),
        ],
        "name": "/start 命令",
        "description": "处理 /start 命令，白名单鉴权和欢迎消息",
        "auth": False,  # /start 自己处理鉴权逻辑
    }
