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
        "❌ BOT_TOKEN 环境变量未设置\n"
        "需要创建 .env 文件并添加：BOT_TOKEN=your_token_here\n"
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
STICKER_MAX_CACHE = 50          # 贴纸集缓存条目上限（模块内 LRU）

# 大文件发送相关常量
TELEGRAM_FILE_SIZE_LIMIT_MB = 48  # Telegram 文件大小限制（MB，留 2MB 裕度）
TG_MESSAGE_MAX_LEN = 4096         # Telegram 单条消息字符上限（平台硬约束，多处复用，禁止重复硬编码）

# 内存监控
MEMORY_MONITOR_INTERVAL = 60          # 监测间隔（秒）
MEMORY_WARNING_THRESHOLD_MB = 300     # 告警阈值（MB）：低于此值通知 OP
MEMORY_ALERT_COOLDOWN = 300           # 告警冷却（秒）
MEMORY_GATE_THRESHOLD_MB = 200        # 拦截阈值（MB）：低于此值拒绝新任务




# /nya 功能相关常量
QUOTES_PATH = os.path.join(PROJECT_ROOT, "data", "ZincNyaQuotes.json")
NYA_MESSAGE_DELAY_MIN = 1           # 多条 nya 语录发送间隔下限（秒）
NYA_MESSAGE_DELAY_MAX = 3           # 多条 nya 语录发送间隔上限（秒）




# 日志相关常量
LOG_DIR = os.path.join(PROJECT_ROOT, "log")




# cli 相关常量
COMMAND_DIR = os.path.join(PROJECT_ROOT, "utils", "command")     # 文件系统路径
COMMAND_MODULE = "utils.command"                                 # Python 模块路径




# Whitelist 相关常量
WHITELIST_PATH = os.path.join(PROJECT_ROOT, "data", "whitelist.json")
AUTH_ENABLED = True     # 设为 False 则跳过白名单鉴权，允许所有用户使用
WHITELIST_NOTIFY_COOLDOWN = 600       # /start 通知冷却（秒），同用户 10 分钟只通知一次
WHITELIST_MAX_NOTIFY_CACHE = 4096     # 通知缓存条目安全上限




# Operators 相关常量
OPERATORS_PATH = os.path.join(PROJECT_ROOT, "data", "operators.json")

class Permission(str, Enum):
    """Operator 权限枚举"""
    SHUTDOWN = "shutdown"
    REBOOT = "reboot"
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




# 数据库共享常量（跨 chatHistory / memory / knowledge / todos）
DB_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"   # 各 db 模块统一的时间戳格式

# 聊天记录保存相关常量
CHAT_DATA_DIR = DATA_DIR                                                        # 兼容旧引用
DB_PATH = os.path.join(DATA_DIR, "chatHistory.db")                              # 聊天记录保存文件名
KEY_PATH = os.path.join(DATA_DIR, ".chatKey")                                   # 密钥文件路径
CHAT_HISTORY_LIMIT = 131072                                                     # 每个聊天保存的最大消息条数
CHAT_EXPORT_DIR = os.path.join(CHAT_DATA_DIR, "chatExport")                      # 聊天记录导出目录
CHAT_BACKUP_DIR = os.path.join(CHAT_DATA_DIR, "chatBackup")                      # 聊天记录自动归档目录
CHAT_PREVIEW_LIMIT = 20                                                          # 默认预览条数




# Bot 身份相关常量
BOT_DISPLAY_NAME = "ZincNya~"                                                   # bot 发言入库 / UI 展示统一署名




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
TODOS_MAX_CACHED_MESSAGES = 1023                                                 # 每用户最后一条 /todos 列表消息缓存上限（防刷屏/内存泄漏）




# LLM 相关常量
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", None)
ANTHROPIC_AUTH_TOKEN = os.getenv("ANTHROPIC_AUTH_TOKEN", None)    # Bearer 认证（自定义端点用，与 API_KEY 二选一）
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", None)       # 自定义 Anthropic 兼容端点（中转/绕过区域限制等）
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", None)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", None)
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", None)
DOUBAO_API_KEY = os.getenv("DOUBAO_API_KEY", None)
LLM_OPENAI_BASE_URL = os.getenv("LLM_OPENAI_BASE_URL", None)
LLM_PROXY = os.getenv("LLM_PROXY", None)                        # HTTP/SOCKS 代理（可选），格式: "http://host:port"
# LLM 记忆选择器必须单独配置，不继承主生成或研究凭据。
LLM_MEMORY_SELECTOR_BASE_URL = os.getenv("LLM_MEMORY_SELECTOR_BASE_URL", None)
LLM_MEMORY_SELECTOR_API_KEY = os.getenv("LLM_MEMORY_SELECTOR_API_KEY", None)
LLM_MEMORY_SELECTOR_PROXY = os.getenv("LLM_MEMORY_SELECTOR_PROXY", None)
LLM_CONFIG_PATH = os.path.join(DATA_DIR, "llm", "llmConfig.json")
LLM_PROMPTS_PATH = os.path.join(DATA_DIR, "llm", "prompts.json")
LLM_DEFAULT_MODEL = "claude-sonnet-4-6"
LLM_MAX_CONTEXT_MESSAGES = 30                                  # 直接拼入 prompt 的近期历史条数上限
LLM_RATE_LIMIT_SECONDS = 5
LLM_DEBOUNCE_SECONDS = 1.5                                     # 防抖等待时间（秒）
LLM_PENDING_MSG_LIMIT = 10                                     # 每用户防抖缓冲最大条数
LLM_REVIEW_TTL_SECONDS = 86400                                 # 审核条目 TTL（秒，默认 24h）
LLM_REVIEW_FEEDBACK_MAX_LENGTH = 2048                          # :fb 补充反馈长度上限（字符）
LLM_MEMORY_DB_PATH = os.path.join(DATA_DIR, "llm", "llmMemory.db")    # structured memory 数据库
LLM_IMAGE_MAX_BYTES = 20 * 1024 * 1024                         # 图片大小上限（20 MB）
LLM_IMAGE_SUPPORTED_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
LLM_REQUEST_MAX_RETRIES = 2                                    # LLM 请求最大重试次数（不含首次）
LLM_REQUEST_RETRY_DELAY = 3                                    # 重试间隔（秒）
LLM_MAX_TOKENS_HARD_CAP = 32768                                # 截断提额重试时 max_tokens 上限（防冲破中转/模型上限）
LLM_KNOWLEDGE_DB_PATH = os.path.join(DATA_DIR, "llm", "knowledge.db")  # 知识库数据库
LLM_KNOWLEDGE_DIR = os.path.join(DATA_DIR, "llm", "knowledge")         # 知识库 Markdown 文件目录
# LLM 记忆操作约束（原散落于 memory/action.py / contextBuilder.py）
LLM_MEMORY_PRIORITY_CAP = 3                # 单条记忆 priority 上限
LLM_MEMORY_MAX_CONTENT_LEN = 500           # 单条记忆 content 最大长度（字符）
LLM_MEMORY_MAX_ACTIONS = 3                 # 单轮回复最多解析的记忆操作数
LLM_MEMORY_MAX_TAGS = 10                   # 单条记忆最多标签数
LLM_MEMORY_HINT_MAX_CHARS = 80             # 单条记忆检索说明最大长度（字符）
LLM_MEMORY_RETRIEVE_PER_SCOPE = 20         # 检索时每作用域候选上限
LLM_MEMORY_RETRIEVE_TOTAL = 10             # 检索时汇池后取前 N 条
# pinned 不参与相关性筛选，会每轮无条件进入候选；单独保留 1000 字符，
# 同时给 contextual 留出约 1500 字符，避免扩大常驻记忆时挤掉全部情境记忆。
LLM_MEMORY_CONTEXT_MAX_CHARS = 2500         # 最终 memory 块硬上限（Unicode 字符）
LLM_MEMORY_PINNED_MAX_CHARS = 1000          # 常驻记忆段字符预算
LLM_MEMORY_QUERY_HISTORY_LIMIT = 20         # memory 语义检索使用的近期历史条数上限
LLM_MEMORY_QUERY_HISTORY_SECONDS = 1800     # 检索历史时间窗口（秒）
LLM_MEMORY_QUERY_HISTORY_MAX_CHARS = 600    # 检索历史总字符上限
LLM_MEMORY_ENCODING_MAX_TOKENS = 256       # 本地语义编码器单次输入上限（含特殊 token）
LLM_MEMORY_CHUNK_OVERLAP = 32              # 长记忆语义分片的重叠 token 数
LLM_MEMORY_VECTOR_CACHE_BYTES = 32 * 1024 * 1024  # 已编码 memory 热缓存字节预算
LLM_MEMORY_RETRIEVAL_TIMEOUT_SECONDS = 2.0         # 单次混合检索墙钟上限（秒）
LLM_MEMORY_FINALIZE_RESERVE_SECONDS = 0.1          # 为最终预算渲染与数据库复核保留的时间
LLM_MEMORY_MAX_ACTIVE_RETRIEVALS = 4               # 允许同时占用检索容量的请求数
LLM_MEMORY_SELECTOR_MAX_SECONDS = 30.0            # 独立远程选择阶段的授权上限
LLM_MEMORY_SELECTOR_CLOSE_RESERVE_SECONDS = 0.1   # 包含在选择预算内的关闭保留时间
LLM_MEMORY_SELECTOR_SHUTDOWN_SECONDS = 5.0        # selector关停宽限；到期如实报告在途资源
LLM_MEMORY_SELECTOR_TOP_K = 32                    # 每路候选上限，三路并集最多 96 条
LLM_MEMORY_SELECTOR_MAX_OPTIONAL = 2              # 校验协议但首版不消费 optional
LLM_MEMORY_SELECTOR_MAX_INPUT_TOKENS = 16384       # 返回 usage 的校验上限，不代替请求前计数
LLM_MEMORY_SELECTOR_MAX_OUTPUT_TOKENS = 8192
LLM_MEMORY_SELECTOR_MAX_REQUEST_BYTES = 65536      # 请求 UTF-8 字节硬上限，不是 token 数
LLM_MEMORY_SELECTOR_MAX_RESPONSE_BYTES = 256 * 1024
LLM_MEMORY_QUERY_QUEUE_LIMIT = 4                   # 等待 native worker 的检索请求上限
LLM_MEMORY_INDEX_QUEUE_LIMIT = 256                 # 在线索引更新队列上限
LLM_MEMORY_INDEX_PAGE_SIZE = 128                   # 后台对账每页读取条数
LLM_MEMORY_RECONCILE_SECONDS = 30                  # 索引对账间隔（秒）
LLM_MEMORY_WORKER_ERROR_BACKOFF_SECONDS = 1.0      # 后台 worker 未预期异常后的退避时间
LLM_MEMORY_QUERY_BURST_LIMIT = 8                   # 连续查询后必须让出一次索引机会
LLM_MEMORY_RRF_K = 60                              # 词法/语义名次融合的 RRF 平滑常量
LLM_MEMORY_BM25_K1 = 1.2                           # BM25 词频饱和参数
LLM_MEMORY_BM25_B = 0.75                           # BM25 文档长度归一化参数
LLM_MEMORY_MODEL_DIR = os.path.join(PROJECT_ROOT, ".cache", "llmMemory", "model")  # 本地模型目录
LLM_MEMORY_MODEL_MANIFEST_PATH = os.path.join(
    PROJECT_ROOT, "utils", "llm", "memory", "modelManifest.json"
)  # 固定模型版本与 artifact 校验信息
LLM_MEMORY_CALIBRATION_PATH = os.path.join(
    PROJECT_ROOT, "utils", "llm", "memory", "retrievalCalibration.json"
)  # 已批准的通道阈值
LLM_MEMORY_REPORT_DIR = os.path.join(PROJECT_ROOT, ".cache", "llmMemory", "reports")  # 评估报告目录
# 视觉描述生成参数（原散落于 client/_generate.py / vision.py）
LLM_VISION_MAX_TOKENS = 4096               # 视觉描述生成 max_tokens
LLM_VISION_TEMPERATURE = 0.2               # 视觉描述生成 temperature
LLM_VISION_MIN_DESCRIPTION_LEN = 150       # 视觉描述最小合法长度，低于则视为异常
LLM_VISION_PREFERRED_MIN_WIDTH = 800       # 选图时偏好的最小宽度阈值
# URL 读取 / 意图判定（原散落于 urlReader.py / urlIntent.py）
LLM_URL_RETRY_BACKOFF_SECONDS = 0.5        # URL 抓取重试退避（秒）
LLM_URL_TOTAL_FETCH_DEADLINE = 15          # 单次 readURLContextsForUserText 总墙钟上限（秒）
LLM_URL_INTENT_NEGATION_WINDOW = 32        # 否定前缀与 intent 词之间允许的字符窗口
LLM_REPLY_CONTEXT_LIMIT = 300              # reply-to 文本注入 prompt 时的截断长度（字符）

# AFC（自主工具调用）相关常量（原散落于 afc/executor.py / afcIntent.py）
AFC_TOOL_TIMEOUT_SECONDS = 15              # 单次工具执行超时（秒），防工具卡死回复链
AFC_MAX_CONTEXTUAL_MESSAGE_LEN = 10        # L4 延续判定：消息不超此长度且含指代词才继承上轮工具集




def migrateLegacyLLMPaths():
    """
    将旧版 data/ 下的 LLM 相关文件迁移到 data/llm/ 子目录。

    必须在任何数据库连接打开之前调用（appLifecycle.initializeApp 最早处）。
    迁移 sqlite 边车文件（-wal / -shm）以避免数据丢失。
    """
    legacy_files = {
        "llmConfig.json": LLM_CONFIG_PATH,
        "prompts.json": LLM_PROMPTS_PATH,
        "llmMemory.db": LLM_MEMORY_DB_PATH,
        "llmMemory.db-wal": LLM_MEMORY_DB_PATH + "-wal",
        "llmMemory.db-shm": LLM_MEMORY_DB_PATH + "-shm",
    }

    for legacy_name, new_path in legacy_files.items():
        legacy_path = os.path.join(DATA_DIR, legacy_name)
        if os.path.exists(legacy_path) and not os.path.exists(new_path):
            os.makedirs(os.path.dirname(new_path), exist_ok=True)
            os.replace(legacy_path, new_path)
