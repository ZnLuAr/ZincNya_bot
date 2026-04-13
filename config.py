import os
from enum import Enum
from dotenv import load_dotenv

# 项目根目录（所有路径的锚点）
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# 加载 .env 文件中的环境变量（使用绝对路径，确保无论从哪里启动都能找到）
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))




# Telegram Bot Token（从环境变量读取）
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError(
        "❌ BOT_TOKEN 环境变量未设置喵！\n"
        "请创建 .env 文件并添加：BOT_TOKEN=your_token_here\n"
        "参考 .env.example 文件了解配置格式。"
    )

# Telegram Bot 代理配置（可选）
# 用于连接 Telegram 服务器，格式: http://host:port
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", None)




# 数据目录（确保存在）
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)




DEFAULT_FILE_CACHE_TTL = 480  # 秒




# /findsticker 功能相关常量
DOWNLOAD_DIR = os.path.join(PROJECT_ROOT, "download")
CACHE_TTL = 300                 # 表情包缓存 5 分钟过期
DELETE_DELAY = 360              # 表情包相关信息 6 分钟后删除
MAX_CONCURRENT_DOWNLOADS = 5    # 最大并发下载 Stickers 数量
MAX_DOWNLOADS_ATTEMPTS = 3      # 最大尝试下载次数
MAX_GIF_FPS = 24                # 下载的最大 gif 帧数
GIF_QUEUE_ALERT_THRESHOLD = 2   # GIF 任务数达到此值时告警
GIF_ALERT_COOLDOWN = 300        # operator 告警冷却（秒）
DEFAULT_READ_TIMEOUT = 300      # 请求发出后，等待返回响应的超时缓冲区 (秒)
DEFAULT_WRITE_TIMEOUT = 60      # 将请求上传至 Telegram 请求体的超时缓冲区（秒）




# /nya 功能相关常量
QUOTES_PATH = os.path.join(PROJECT_ROOT, "data", "ZincNyaQuotes.json")




# 日志相关常量
LOG_DIR = os.path.join(PROJECT_ROOT, "log")




# cli 相关常量
COMMAND_DIR = os.path.join(PROJECT_ROOT, "utils", "command")     # 文件系统路径
COMMAND_MODULE = "utils.command"                                 # Python 模块路径




# Whitelist 相关常量
WHITELIST_PATH = os.path.join(PROJECT_ROOT, "data", "whitelist.json")
AUTH_ENABLED = True     # 设为 False 则跳过白名单鉴权，允许所有用户使用




# Operators 相关常量
OPERATORS_PATH = os.path.join(PROJECT_ROOT, "data", "operators.json")


class Permission(str, Enum):
    """Operator 权限枚举"""
    SHUTDOWN = "shutdown"
    RESTART = "restart"
    STATUS = "status"
    NOTIFY = "notify"
    LLM = "llm"

    def __str__(self):
        return self.value




# /book 书籍搜索功能相关常量
BOOK_SEARCH_API = "https://openlibrary.org/search.json"     # Open Library 搜索 API
BOOK_WORKS_API = "https://openlibrary.org/works"            # Open Library 作品详情 API
BOOK_COVERS_API = "https://covers.openlibrary.org/b/id"     # Open Library 封面图片 API
BOOK_ITEMS_PER_PAGE = 5                                     # 每页显示书籍数量
BOOK_MAX_ITEMS_PER_PAGE = 10                                # 每页最大书籍数量
BOOK_REQUEST_TIMEOUT = 30                                   # API 请求超时（秒）- 增加以应对慢速网络
BOOK_DESCRIPTION_MAX_LENGTH = 500                           # 书籍简介最大长度
BOOK_QUERY_HASH_LENGTH = 12                                 # 搜索词哈希长度（12 hex = 48 bit，降低碰撞概率）
BOOK_HTTP_PROXY = os.getenv("BOOK_HTTP_PROXY", None)        # HTTP 代理（可选），格式: "http://host:port"




# 聊天记录保存相关常量
CHAT_DATA_DIR = DATA_DIR                                                        # 兼容旧引用
DB_PATH = os.path.join(DATA_DIR, "chatHistory.db")                              # 聊天记录保存文件名
KEY_PATH = os.path.join(DATA_DIR, ".chatKey")                                   # 密钥文件路径
CHAT_HISTORY_LIMIT = 131072                                                     # 每个聊天保存的最大消息条数
CHAT_EXPORT_DIR = os.path.join(CHAT_DATA_DIR, "chatExport")                      # 聊天记录导出目录
CHAT_BACKUP_DIR = os.path.join(CHAT_DATA_DIR, "chatBackup")                      # 聊天记录自动归档目录
CHAT_PREVIEW_LIMIT = 20                                                          # 默认预览条数




# 新闻抓取功能相关常量
# 注意：该站点仅提供 HTTP，无 HTTPS 支持。如更换源站，建议使用 HTTPS
NEWS_SOURCE_URL = os.getenv("NEWS_SOURCE_URL", "http://naenara.com.kp/main/index/ch/first")
NEWS_HTTP_PROXY = os.getenv("NEWS_HTTP_PROXY", None)                            # HTTP 代理（可选），格式: "http://host:port"
NEWS_REQUEST_TIMEOUT = 30                                                       # 请求超时（秒）
NEWS_TARGET_CHAT_ID = os.getenv("NEWS_TARGET_CHAT_ID", None)                    # 推送目标群聊 ID
NEWS_MAX_ARTICLES = 5                                                           # 每次最多推送的文章数
NEWS_DATA_FILE = os.path.join(CHAT_DATA_DIR, "pushedNews.json")                 # 已推送记录保存路径




# TODOS 相关常量
TODOS_DB_PATH = os.path.join(CHAT_DATA_DIR, "todos.db")
TODOS_ITEMS_PER_PAGE = 6                                                        # 每页显示的待办数量
TODOS_REMINDER_CHECK_INTERVAL = 60                                              # 提醒检查间隔（秒）
TODOS_CONTENT_MAX_LENGTH = 200                                                  # 待办内容最大长度
TODOS_CONTENT_PREVIEW_LENGTH = 15                                               # 列表中内容预览长度（字符数）




# LLM 相关常量
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", None)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", None)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", None)
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", None)
DOUBAO_API_KEY = os.getenv("DOUBAO_API_KEY", None)
LLM_OPENAI_BASE_URL = os.getenv("LLM_OPENAI_BASE_URL", None)
LLM_PROXY = os.getenv("LLM_PROXY", None)                        # HTTP/SOCKS 代理（可选），格式: "http://host:port"
LLM_CONFIG_PATH = os.path.join(DATA_DIR, "llmConfig.json")
LLM_PROMPTS_PATH = os.path.join(DATA_DIR, "prompts.json")
LLM_DEFAULT_MODEL = "claude-sonnet-4-6"
LLM_MAX_CONTEXT_MESSAGES = 20                                  # 单次记忆最大读取条数
LLM_RATE_LIMIT_SECONDS = 5
LLM_DEBOUNCE_SECONDS = 1.5                                     # 防抖等待时间（秒）
LLM_PENDING_MSG_LIMIT = 10                                     # 每用户防抖缓冲最大条数
LLM_REVIEW_TTL_SECONDS = 86400                                 # 审核条目 TTL（秒，默认 24h）
LLM_MEMORY_DB_PATH = os.path.join(DATA_DIR, "llmMemory.db")    # structured memory 数据库
LLM_IMAGE_MAX_BYTES = 20 * 1024 * 1024                         # 图片大小上限（20 MB）
LLM_IMAGE_SUPPORTED_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
LLM_REQUEST_MAX_RETRIES = 2                                    # LLM 请求最大重试次数（不含首次）
LLM_REQUEST_RETRY_DELAY = 3                                    # 重试间隔（秒）
