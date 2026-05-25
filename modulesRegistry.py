"""
modulesRegistry.py

ZincNya Bot 模块注册表。

模块定义：
    - 模块是逻辑概念，可以包含 handlers/ 和 utils/ 下的多个文件
    - 一个模块可以有多个 handler 文件（需要调用 register()）
    - 一个模块可以有多个 utils 文件（工具函数、API 封装等）
    - 模块可以定义初始化函数和后台任务

字段说明：
    - id: 模块唯一标识符（字典的 key，只允许小写字母、数字、下划线）
    - name: 模块显示名称
    - description: 模块功能描述
    - version: 版本号（语义化版本）
    - author: 作者（可选）
    - files: 模块包含的所有文件（相对项目根目录）
    - handlers: 需要调用 register() 的 handler 文件列表
    - initFunctions: 启动时调用的初始化函数（格式："module.path:function_name"）
    - backgroundTasks: 后台任务函数（格式同上）
    - dependencies: 依赖的其他模块（可选，第一版不检查）
    - githubRepo: GitHub 仓库 URL（可选，用于标记来源）
"""

MODULES = {
    "llm": {
        "id": "llm",
        "name": "LLM 对话",
        "description": "处理群聊 @ / 关键词触发 / 私聊消息，支持图片、URL 读取、记忆和知识库",
        "version": "1.0.0",
        "author": "ZincPhos",
        "files": [
            "handlers/llm.py",
            "handlers/llmCommand.py",
            "handlers/llmReview.py",
            "utils/chatHistory.py",
            "utils/operators.py",
            "utils/telegramHelpers.py",
            "utils/llm/__init__.py",
            "utils/llm/config.py",
            "utils/llm/state.py",
            "utils/llm/contextBuilder.py",
            "utils/llm/review.py",
            "utils/llm/vision.py",
            "utils/llm/urlIntent.py",
            "utils/llm/urlReader.py",
            "utils/llm/client/__init__.py",
            "utils/llm/client/_base.py",
            "utils/llm/client/_generate.py",
            "utils/llm/client/_guardrails.py",
            "utils/llm/client/_request.py",
            "utils/llm/client/_router.py",
            "utils/llm/client/anthropic.py",
            "utils/llm/client/gemini.py",
            "utils/llm/client/openaiCompat.py",
            "utils/llm/memory/__init__.py",
            "utils/llm/memory/database.py",
            "utils/llm/memory/action.py",
            "utils/llm/memory/ui.py",
            "utils/llm/knowledge/__init__.py",
            "utils/llm/knowledge/database.py",
            "utils/llm/knowledge/loader.py",
            "utils/llm/knowledge/tokenizer.py",
            "utils/fileEditor.py",
            "utils/inputHelper.py",
            "utils/command/llm.py",
        ],
        "handlers": ["handlers/llm.py", "handlers/llmCommand.py", "handlers/llmReview.py"],
        "initFunctions": [
            "utils.chatHistory:initDatabase",
            "utils.llm.memory:initDatabase",
            "utils.llm.knowledge:initDatabase",
            "utils.llm.urlReader:registerResources",
        ],
        "backgroundTasks": [
            "utils.llm.knowledge:reindexOnStartup",
        ],
        "dependencies": [],
        "githubRepo": None,
    },

    "todos": {
        "id": "todos",
        "name": "待办事项",
        "description": "管理个人待办事项，支持优先级和定时提醒",
        "version": "1.0.0",
        "author": "ZincPhos",
        "files": [
            "handlers/todos.py",
            "utils/telegramHelpers.py",
            "utils/todos/__init__.py",
            "utils/todos/database.py",
            "utils/todos/reminder.py",
            "utils/todos/tgRender.py",
            "utils/todos/cliRender.py",
            "utils/todos/utils.py",
            "utils/command/todos.py",
        ],
        "handlers": ["handlers/todos.py"],
        "initFunctions": [
            "utils.todos.database:initDatabase",
        ],
        "backgroundTasks": [
            "utils.todos.reminder:todoReminderLoop",
        ],
        "dependencies": [],
        "githubRepo": None,
    },

    "nya": {
        "id": "nya",
        "name": "Nya 语录",
        "description": "随机发送 ZincNya 语录，支持在线编辑",
        "version": "1.0.0",
        "author": "ZincPhos",
        "files": [
            "handlers/nya.py",
            "utils/nyaQuoteManager/__init__.py",
            "utils/nyaQuoteManager/data.py",
            "utils/nyaQuoteManager/ui.py",
            "utils/fileEditor.py",
            "utils/command/nya.py",
        ],
        "handlers": ["handlers/nya.py"],
        "initFunctions": [],
        "backgroundTasks": [],
        "dependencies": [],
        "githubRepo": None,
    },

    "whitelist": {
        "id": "whitelist",
        "name": "白名单管理",
        "description": "用户白名单与权限管理，包含 /start 命令",
        "version": "1.0.0",
        "author": "ZincPhos",
        "files": [
            "handlers/start.py",
            "utils/whitelistManager/__init__.py",
            "utils/whitelistManager/data.py",
            "utils/whitelistManager/ui.py",
            "utils/inputHelper.py",
            "utils/command/whitelist.py",
        ],
        "handlers": ["handlers/start.py"],
        "initFunctions": [],
        "backgroundTasks": [],
        "dependencies": [],
        "githubRepo": None,
    },

    "book": {
        "id": "book",
        "name": "书籍搜索",
        "description": "Open Library 书籍搜索，支持交互式翻页",
        "version": "1.0.0",
        "author": "ZincPhos",
        "files": [
            "handlers/book.py",
            "utils/bookSearchAPI.py",
            "utils/telegramHelpers.py",
        ],
        "handlers": ["handlers/book.py"],
        "initFunctions": [
            "utils.bookSearchAPI:registerResources",
        ],
        "backgroundTasks": [],
        "dependencies": [],
        "githubRepo": None,
    },

    "stickers": {
        "id": "stickers",
        "name": "表情包下载",
        "description": "下载 Telegram 表情包为图片或 GIF",
        "version": "1.0.0",
        "author": "ZincPhos",
        "files": [
            "handlers/stickers.py",
            "utils/downloader.py",
            "utils/operators.py",
        ],
        "handlers": ["handlers/stickers.py"],
        "initFunctions": [],
        "backgroundTasks": [],
        "dependencies": [],
        "githubRepo": None,
    },

    "reaction": {
        "id": "reaction",
        "name": "消息反应记录",
        "description": "记录消息 reaction 到聊天历史",
        "version": "1.0.0",
        "author": "ZincPhos",
        "files": [
            "handlers/reaction.py",
            "utils/chatHistory.py",
        ],
        "handlers": ["handlers/reaction.py"],
        "initFunctions": [
            "utils.chatHistory:initDatabase",
        ],
        "backgroundTasks": [],
        "dependencies": [],
        "githubRepo": None,
    },

    "shutdown": {
        "id": "shutdown",
        "name": "系统管理",
        "description": "关机、重启、状态查询（ops 专用）",
        "version": "1.0.0",
        "author": "ZincPhos",
        "files": [
            "handlers/shutdown.py",
            "utils/operators.py",
            "utils/command/shutdown.py",
        ],
        "handlers": ["handlers/shutdown.py"],
        "initFunctions": [],
        "backgroundTasks": [],
        "dependencies": [],
        "githubRepo": None,
    },

    "send": {
        "id": "send",
        "name": "控制台聊天",
        "description": "控制台交互式聊天界面（CLI 专用）",
        "version": "1.0.0",
        "author": "ZincPhos",
        "files": [
            "utils/command/send.py",
            "utils/chatUI.py",
            "utils/chatHistory.py",
            "utils/inputHelper.py",
        ],
        "handlers": [],
        "initFunctions": [
            "utils.chatHistory:initDatabase",
        ],
        "backgroundTasks": [],
        "dependencies": [],
        "githubRepo": None,
    },

    "news": {
        "id": "news",
        "name": "新闻推送",
        "description": "新闻 API 封装（未完成）",
        "version": "0.1.0",
        "author": "ZincPhos",
        "files": [
            "utils/newsAPI.py",
            "utils/command/news.py",
        ],
        "handlers": [],
        "initFunctions": [
            "utils.newsAPI:registerResources",
        ],
        "backgroundTasks": [],
        "dependencies": [],
        "githubRepo": None,
    },
}