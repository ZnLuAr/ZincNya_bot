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