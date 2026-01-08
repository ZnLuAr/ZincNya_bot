"""
utils/bookSearchAPI.py

Open Library API 封装层，提供书籍搜索和详情获取功能。

本模块作为数据访问层，不涉及任何 Telegram 交互逻辑，仅负责：
    - 与 Open Library API 通信
    - 数据格式化和标准化
    - 错误处理和超时控制

设计原则：
    - 所有函数均为 async，支持异步调用
    - 返回值格式统一，便于上层处理
    - 错误信息包含在返回值中，不抛出异常
    - 配置常量从 config.py 读取，便于统一管理


================================================================================
主要函数
================================================================================

searchBooks(query , page , limit) -> dict
────────────────────────────────────────
    搜索书籍，返回分页结果。

    参数:
        query: str      搜索关键词（书名、作者、ISBN 等）
        page: int       页码，从 1 开始（默认 1）
        limit: int      每页数量（默认 5，最大 10）

    返回值:
        成功时:
        {
            "total": int ,           # 总结果数
            "page": int ,            # 当前页码
            "totalPages": int ,      # 总页数
            "results": [            # 结果列表
                {
                    "id": str ,          # Open Library Work ID (如 "OL123W")
                    "title": str ,       # 书名
                    "authors": list ,    # 作者列表 (可能为空)
                    "year": int|None ,   # 首次出版年份
                    "language": str ,    # 主要语言（已转换为可读名称）
                    "coverId": int|None # 封面 ID（用于生成封面 URL）
                },
                ...
            ]
        }

        失败时:
        {
            "total": 0,
            "page": 1,
            "totalPages": 0,
            "results": [],
            "error": str            # 错误描述
        }

    使用示例:
        result = await searchBooks("线性代数" , page=1 , limit=5)
        if "error" in result:
            print(f"搜索失败: {result['error']}")
        else:
            for book in result["results"]:
                print(f"{book['title']} - {book['authors']}")


getBookDetail(workId) -> dict | None
────────────────────────────────────────
    获取书籍详细信息。

    参数:
        workId: str     Open Library Work ID（如 "OL123W"）

    返回值:
        成功时:
        {
            "id": str ,              # Work ID
            "title": str ,           # 书名
            "authors": list ,        # 作者列表（可能为空，需从搜索结果补充）
            "description": str ,     # 书籍简介（已截断至最大长度）
            "subjects": list ,       # 主题标签（最多 5 个）
            "coverId": int|None ,    # 封面 ID
            "links": {
                "openLibrary": str ,     # Open Library 页面链接
                "read": str|None        # 在线阅读链接（如果有）
            }
        }

        失败时:
        None

    注意:
        - authors 字段可能为空，因为详情 API 返回的是作者引用而非名称
        - 建议从搜索结果中缓存作者信息，在显示详情时补充
        - description 会自动截断至 config.BOOK_DESCRIPTION_MAX_LENGTH


================================================================================
辅助函数
================================================================================

getCoverUrl(coverId , size) -> str | None
────────────────────────────────────────
    根据封面 ID 生成封面图片 URL。

    参数:
        coverId: int    封面 ID（从搜索结果的 coverId 字段获取）
        size: str       尺寸，可选 "S"(小) , "M"(中) , "L"(大)

    返回值:
        封面 URL 字符串，如果 coverId 为空则返回 None

    使用示例:
        url = getCoverUrl(12345 , "M")
        # -> "https://covers.openlibrary.org/b/id/12345-M.jpg"


================================================================================
语言代码映射
================================================================================

Open Library 返回的语言代码（如 "eng" , "chi"）会自动转换为可读名称：

    eng -> English      chi/zho -> 中文       jpn -> 日本語
    kor -> 한국어        fra -> Français      deu -> Deutsch
    spa -> Español      rus -> Русский       por -> Português
    ita -> Italiano

未知语言代码会原样返回（大写形式）。


================================================================================
错误处理
================================================================================

本模块不会抛出异常，所有错误都通过返回值传递：

    - 网络超时：返回 {"error": "请求超时" , ...}
    - API 错误：返回 {"error": "API 返回状态码 XXX" , ...}
    - 其他错误：返回 {"error": "错误描述" , ...}
    - 详情获取失败：返回 None

上层调用时应检查返回值中是否包含 "error" 字段或是否为 None。

"""


import aiohttp
import asyncio
from typing import Optional

from config import (
    BOOK_SEARCH_API,
    BOOK_WORKS_API,
    BOOK_COVERS_API,
    BOOK_MAX_ITEMS_PER_PAGE,
    BOOK_REQUEST_TIMEOUT,
    BOOK_DESCRIPTION_MAX_LENGTH,
    BOOK_HTTP_PROXY,
)


# ============================================================================
# 语言代码映射表
# ============================================================================

LANGUAGE_MAP = {
    "eng": "English",
    "chi": "中文",
    "zho": "中文",
    "jpn": "日本語",
    "kor": "한국어",
    "fra": "Français",
    "deu": "Deutsch",
    "spa": "Español",
    "rus": "Русский",
    "por": "Português",
    "ita": "Italiano",
    "ara": "العربية",
    "hin": "हिन्दी",
    "vie": "Tiếng Việt",
    "tha": "ไทย",
    "nld": "Nederlands",
    "pol": "Polski",
    "tur": "Türkçe",
    "swe": "Svenska",
    "dan": "Dansk",
    "nor": "Norsk",
    "fin": "Suomi",
    "ces": "Čeština",
    "hun": "Magyar",
    "ron": "Română",
    "ukr": "Українська",
    "heb": "עברית",
    "ind": "Bahasa Indonesia",
    "msa": "Bahasa Melayu",
}




# ============================================================================
# 辅助函数
# ============================================================================

def _languageCodeToName(code: str) -> str:
    """
    将 ISO 639-2/3 语言代码转换为可读名称。

    参数:
        code: 语言代码（如 "eng" , "chi" , "jpn"）

    返回:
        可读的语言名称，未知代码返回大写形式
    """
    if not code:
        return "Unknown"
    return LANGUAGE_MAP.get(code.lower() , code.upper())


def getCoverUrl(coverId: Optional[int] , size: str = "M") -> Optional[str]:
    """
    根据封面 ID 生成 Open Library 封面图片 URL。

    参数:
        coverId: 封面 ID（从搜索结果获取）
        size: 图片尺寸
            - "S": 小图 (约 40x60)
            - "M": 中图 (约 180x270)
            - "L": 大图 (约 500x750)

    返回:
        封面图片 URL，如果 coverId 为空则返回 None
    """
    if not coverId:
        return None
    return f"{BOOK_COVERS_API}/{coverId}-{size}.jpg"




# ============================================================================
# 主要 API 函数
# ============================================================================

async def searchBooks(query: str , page: int = 1 , limit: int = 5) -> dict:
    """
    搜索书籍。

    通过 Open Library Search API 搜索书籍，支持按书名、作者、ISBN 等搜索。
    返回标准化的分页结果。

    参数:
        query: 搜索关键词
        page: 页码（从 1 开始）
        limit: 每页数量（最大 BOOK_MAX_ITEMS_PER_PAGE）

    返回:
        标准化的搜索结果字典（详见模块文档）
    """

    # 参数校验
    if not query or not query.strip():
        return {
            "total": 0,
            "page": 1,
            "totalPages": 0,
            "results": []
        }

    # 限制每页数量，防止请求过大
    limit = min(limit , BOOK_MAX_ITEMS_PER_PAGE)
    page = max(1 , page)
    offset = (page - 1) * limit

    # 构建请求 URL
    # 注意：Open Library API 使用 offset 而非 page 进行分页
    from urllib.parse import quote

    # 短查询填充：Open Library 要求至少 3 个字符
    # 使用零宽空格 (U+200B) 填充，不影响搜索结果
    queryText = query.strip()
    while len(queryText) < 3:
        queryText += "\u200b"  # Zero-Width Space

    encodedQuery = quote(queryText)
    url = f"{BOOK_SEARCH_API}?q={encodedQuery}&offset={offset}&limit={limit}"

    try:
        timeout = aiohttp.ClientTimeout(total=BOOK_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession() as session:
            async with session.get(url , timeout=timeout , proxy=BOOK_HTTP_PROXY) as response:
                if response.status == 422:
                    return {
                        "total": 0,
                        "page": page,
                        "totalPages": 0,
                        "results": [],
                        "error": "搜索词太短了喵，试试加更多关键字？\nOpen Library 要求至少 3 个字符的……"
                    }
                if response.status != 200:
                    return {
                        "total": 0,
                        "page": page,
                        "totalPages": 0,
                        "results": [],
                        "error": f"API 返回状态码 {response.status} 喵"
                    }

                data = await response.json()

    except asyncio.TimeoutError:
        return {
            "total": 0,
            "page": page,
            "totalPages": 0,
            "results": [],
            "error": "请求超时，稍后重试喵"
        }
    except aiohttp.ClientError as e:
        return {
            "total": 0,
            "page": page,
            "totalPages": 0,
            "results": [],
            "error": f"网络错误喵  {type(e).__name__}"
        }
    except Exception as e:
        return {
            "total": 0,
            "page": page,
            "totalPages": 0,
            "results": [],
            "error": str(e)
        }

    # 解析 API 响应
    total = data.get("numFound" , 0)
    docs = data.get("docs" , [])

    results = []
    for doc in docs:
        # 提取 Work ID（格式: "/works/OL123W" -> "OL123W"）
        key = doc.get("key" , "")
        workId = key.replace("/works/" , "") if key else None

        if not workId:
            continue

        # 提取主要语言（取列表第一个）
        languages = doc.get("language" , [])
        primaryLanguage = languages[0] if languages else None

        results.append({
            "id": workId,
            "title": doc.get("title" , "Unknown Title"),
            "authors": doc.get("author_name" , []),
            "year": doc.get("first_publish_year"),
            "language": _languageCodeToName(primaryLanguage),
            "coverId": doc.get("cover_i")
        })

    # 计算总页数
    totalPages = (total + limit - 1) // limit if total > 0 else 0

    return {
        "total": total,
        "page": page,
        "totalPages": totalPages,
        "results": results
    }




async def getBookDetail(workId: str) -> Optional[dict]:
    """
    获取书籍详细信息。

    通过 Open Library Works API 获取书籍的详细信息，包括简介、主题等。

    参数:
        workId: Open Library Work ID（如 "OL123W"）

    返回:
        书籍详情字典，失败返回 None

    注意:
        - 详情 API 返回的作者是引用格式，不包含实际名称
        - 建议在调用此函数前缓存搜索结果中的作者信息
    """

    if not workId:
        return None

    url = f"{BOOK_WORKS_API}/{workId}.json"

    try:
        timeout = aiohttp.ClientTimeout(total=BOOK_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession() as session:
            async with session.get(url , timeout=timeout , proxy=BOOK_HTTP_PROXY) as response:
                if response.status != 200:
                    return None

                data = await response.json()

    except Exception:
        return None

    # 解析描述字段
    # Open Library 的 description 可能是字符串或 {"type": "/type/text" , "value": "..."}
    description = data.get("description" , "")
    if isinstance(description , dict):
        description = description.get("value" , "")

    # 截断过长的描述
    if len(description) > BOOK_DESCRIPTION_MAX_LENGTH:
        description = description[:BOOK_DESCRIPTION_MAX_LENGTH - 3] + "..."

    # 如果没有描述，提供默认文本
    if not description:
        description = "暂无简介"

    # 解析封面列表（取第一个）
    covers = data.get("covers" , [])
    coverId = covers[0] if covers else None

    # 解析主题标签（限制数量）
    subjects = data.get("subjects" , [])[:5]

    return {
        "id": workId,
        "title": data.get("title" , "Unknown Title"),
        "authors": [],  # 详情 API 不直接返回作者名，需要额外请求或从搜索结果补充
        "description": description,
        "subjects": subjects,
        "coverId": coverId,
        "links": {
            "openLibrary": f"https://openlibrary.org/works/{workId}",
            "read": None  # 需要查询 editions API 才能获取，暂不实现
        }
    }
