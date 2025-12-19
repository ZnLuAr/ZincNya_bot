import os



BOT_TOKEN = os.getenv("BOT_TOKEN" , "7770221224:AAGvij0WepiklXbqXDqB7RHa7k-9Y6nyzJs")



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



# Sudoers 列表
SUDOER_LIST = ["ZincPhos"]
SUDOER_ID = ["7767386015"]
WHITELIST_DIR = os.path.join(os.path.dirname(__file__) , "data" , "whitelist.json")