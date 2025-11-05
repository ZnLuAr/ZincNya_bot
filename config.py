import os



BOT_TOKEN = os.getenv("BOT_TOKEN" , "7770221224:AAGvij0WepiklXbqXDqB7RHa7k-9Y6nyzJs")




# /findsticker 功能相关常量
DATA_DIR = "data"
DELETE_DELAY = 180  # 秒
MAX_CONCURRENT_DOWNLOADS = 5  # 最大并发下载 Stickers 数量
MAX_DOWNLOADS_ATTEMPTS = 3  # 最大尝试下载次数




# /nya 功能相关常量
QUOTES_PATH = os.path.join(os.path.dirname(__file__) , "data" , "ZincNyaQuotes.csv")




# 日志相关常量
LOG_DIR = os.path.join(os.path.dirname(__file__) , "log")
LOG_FILE_TEMPLATE = "log_{date}_{index:02d}.log"  # 日志文件名模板




# cli 相关常量
COMMAND_DIR = "utils/command"
HELP_LIST_DIR = os.path.join(os.path.dirname(__file__) , "data" , "helpList.csv")



# Sudoers 列表
SUDOER_LIST = ["ZincPhos"]