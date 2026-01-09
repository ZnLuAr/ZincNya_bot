import os
from dotenv import load_dotenv

# 加载 .env 文件中的环境变量
load_dotenv()




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




# /findsticker 功能相关常量
DATA_DIR = "download"
CACHE_TTL = 300                 # 表情包缓存 5 分钟过期
DELETE_DELAY = 180
MAX_CONCURRENT_DOWNLOADS = 5    # 最大并发下载 Stickers 数量
MAX_DOWNLOADS_ATTEMPTS = 3      # 最大尝试下载次数
MAX_GIF_FPS = 24                # 下载的最大 gif 帧数
DEFAULT_READ_TIMEOUT = 300      # 请求发出后，等待返回响应的超时缓冲区 (秒)
DEFAULT_WRITE_TIMEOUT = 60      # 将请求上传至 Telegram 请求体的超时缓冲区（秒）




# /nya 功能相关常量
QUOTES_DIR = os.path.join(os.path.dirname(__file__) , "data" , "ZincNyaQuotes.json")




# 日志相关常量
LOG_DIR = os.path.join(os.path.dirname(__file__) , "log")
LOG_FILE_TEMPLATE = "log_{date}_{index:02d}.log"  # 日志文件名模板




# cli 相关常量
COMMAND_DIR = "utils/command"
HELP_LIST_DIR = os.path.join(os.path.dirname(__file__) , "data" , "helpList.csv")




WHITELIST_DIR = os.path.join(os.path.dirname(__file__) , "data" , "whitelist.json")




# /book 书籍搜索功能相关常量
BOOK_SEARCH_API = "https://openlibrary.org/search.json"     # Open Library 搜索 API
BOOK_WORKS_API = "https://openlibrary.org/works"            # Open Library 作品详情 API
BOOK_COVERS_API = "https://covers.openlibrary.org/b/id"     # Open Library 封面图片 API
BOOK_ITEMS_PER_PAGE = 5                                     # 每页显示书籍数量
BOOK_MAX_ITEMS_PER_PAGE = 10                                # 每页最大书籍数量
BOOK_REQUEST_TIMEOUT = 30                                   # API 请求超时（秒）- 增加以应对慢速网络
BOOK_DESCRIPTION_MAX_LENGTH = 500                           # 书籍简介最大长度
BOOK_QUERY_HASH_LENGTH = 8                                  # 搜索词哈希长度（用于 callback_data）
BOOK_HTTP_PROXY = os.getenv("BOOK_HTTP_PROXY", None)        # HTTP 代理（可选），格式: "http://host:port"




# 聊天记录保存相关常量
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")     # 聊天记录保存路径
DB_PATH = os.path.join(DATA_DIR, "chatHistory.db")                             # 聊天记录保存文件名
KEY_PATH = os.path.join(DATA_DIR, ".chatKey")                                  # 密钥文件路径
