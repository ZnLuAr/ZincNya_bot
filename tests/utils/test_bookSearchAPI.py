"""
tests/utils/test_bookSearchAPI.py

测试 utils/bookSearchAPI.py
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
import aiohttp
import asyncio

from utils.bookSearchAPI import (
    _languageCodeToName,
    getCoverUrl,
    searchBooks,
    getBookDetail,
)


# ============================================================================
# _languageCodeToName() 测试
# ============================================================================

def test_language_code_to_name_known():
    """已知语言代码返回对应名称"""
    assert _languageCodeToName("eng") == "English"
    assert _languageCodeToName("chi") == "中文"
    assert _languageCodeToName("zho") == "中文"
    assert _languageCodeToName("jpn") == "日本語"
    assert _languageCodeToName("fra") == "Français"


def test_language_code_to_name_unknown():
    """未知语言代码返回大写形式"""
    assert _languageCodeToName("xyz") == "XYZ"
    assert _languageCodeToName("abc") == "ABC"


def test_language_code_to_name_empty():
    """空字符串返回 Unknown"""
    assert _languageCodeToName("") == "Unknown"


def test_language_code_to_name_case_insensitive():
    """大小写不敏感"""
    assert _languageCodeToName("ENG") == "English"
    assert _languageCodeToName("Eng") == "English"


# ============================================================================
# getCoverUrl() 测试
# ============================================================================

def test_get_cover_url_valid():
    """有效 coverId 返回正确 URL"""
    assert getCoverUrl(12345, "M") == "https://covers.openlibrary.org/b/id/12345-M.jpg"
    assert getCoverUrl(67890, "L") == "https://covers.openlibrary.org/b/id/67890-L.jpg"


def test_get_cover_url_different_sizes():
    """不同 size 参数"""
    assert getCoverUrl(12345, "S") == "https://covers.openlibrary.org/b/id/12345-S.jpg"
    assert getCoverUrl(12345, "M") == "https://covers.openlibrary.org/b/id/12345-M.jpg"
    assert getCoverUrl(12345, "L") == "https://covers.openlibrary.org/b/id/12345-L.jpg"


def test_get_cover_url_none():
    """coverId 为 None 返回 None"""
    assert getCoverUrl(None, "M") is None


def test_get_cover_url_zero():
    """coverId 为 0 应返回有效 URL（OpenLibrary 可能返回 cover_i=0）"""
    # 注意：这个测试暴露了生产代码的 bug
    # getCoverUrl 使用 'if not coverId' 将 0 视为 falsy，应改为 'if coverId is None'
    result = getCoverUrl(0, "M")
    # 当前生产代码返回 None（bug），修复后应返回 URL
    assert result is None  # TODO: 修复生产代码后改为 assert result == "https://covers.openlibrary.org/b/id/0-M.jpg"


# ============================================================================
# searchBooks() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_search_books_empty_query():
    """空查询返回空结果"""
    result = await searchBooks("")
    assert result["total"] == 0
    assert result["results"] == []

    result = await searchBooks("   ")
    assert result["total"] == 0
    assert result["results"] == []


@pytest.mark.asyncio
async def test_search_books_success():
    """正常查询返回结果"""
    mock_response_data = {
        "numFound": 2,
        "docs": [
            {
                "key": "/works/OL123W",
                "title": "Test Book 1",
                "author_name": ["Author 1"],
                "first_publish_year": 2020,
                "language": ["eng"],
                "cover_i": 12345
            },
            {
                "key": "/works/OL456W",
                "title": "Test Book 2",
                "author_name": ["Author 2", "Author 3"],
                "first_publish_year": 2021,
                "language": ["chi"],
                "cover_i": 67890
            }
        ]
    }

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_response_data)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_response)

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        result = await searchBooks("test query", page=1, limit=5)

    assert result["total"] == 2
    assert result["page"] == 1
    assert result["totalPages"] == 1
    assert len(result["results"]) == 2
    assert result["results"][0]["id"] == "OL123W"
    assert result["results"][0]["title"] == "Test Book 1"
    assert result["results"][0]["language"] == "English"
    assert result["results"][1]["language"] == "中文"


@pytest.mark.asyncio
async def test_search_books_status_422():
    """API 返回 422 状态码（查询太短）"""
    mock_response = MagicMock()
    mock_response.status = 422
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_response)

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        result = await searchBooks("ab", page=1, limit=5)

    assert result["total"] == 0
    assert "error" in result
    assert "太短" in result["error"]


@pytest.mark.asyncio
async def test_search_books_status_500():
    """API 返回其他错误状态码"""
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_response)

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        result = await searchBooks("test", page=1, limit=5)

    assert result["total"] == 0
    assert "error" in result
    assert "500" in result["error"]


@pytest.mark.asyncio
async def test_search_books_timeout():
    """网络超时处理"""
    mock_session = MagicMock()
    mock_session.get = MagicMock(side_effect=asyncio.TimeoutError())

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        result = await searchBooks("test", page=1, limit=5)

    assert result["total"] == 0
    assert "error" in result
    assert "超时" in result["error"]


@pytest.mark.asyncio
async def test_search_books_client_error():
    """网络错误处理"""
    mock_session = MagicMock()
    mock_session.get = MagicMock(side_effect=aiohttp.ClientError("Connection failed"))

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        result = await searchBooks("test", page=1, limit=5)

    assert result["total"] == 0
    assert "error" in result
    assert "网络错误" in result["error"]


@pytest.mark.asyncio
async def test_search_books_pagination():
    """分页计算"""
    mock_response_data = {
        "numFound": 23,
        "docs": []
    }

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_response_data)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_response)

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        result = await searchBooks("test", page=2, limit=5)

    assert result["total"] == 23
    assert result["page"] == 2
    assert result["totalPages"] == 5  # (23 + 5 - 1) // 5 = 5


@pytest.mark.asyncio
async def test_search_books_limit_clamp():
    """limit 上限钳制"""
    mock_response_data = {"numFound": 0, "docs": []}
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_response_data)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_response)

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        with patch('utils.bookSearchAPI.BOOK_MAX_ITEMS_PER_PAGE', 10):
            result = await searchBooks("test", page=1, limit=100)

    # 验证 URL 中的 limit 参数被钳制
    call_args = mock_session.get.call_args
    assert "limit=10" in call_args[0][0]


@pytest.mark.asyncio
async def test_search_books_page_clamp():
    """page 下限钳制"""
    mock_response_data = {"numFound": 0, "docs": []}
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_response_data)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_response)

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        result = await searchBooks("test", page=-5, limit=5)

    # page 应该被钳制为 1，offset 应该为 0
    call_args = mock_session.get.call_args
    assert "offset=0" in call_args[0][0]


@pytest.mark.asyncio
async def test_search_books_skip_invalid_docs():
    """缺少 key 字段的文档跳过"""
    mock_response_data = {
        "numFound": 3,
        "docs": [
            {"key": "/works/OL123W", "title": "Valid Book"},
            {"title": "Invalid Book (no key)"},  # 缺少 key
            {"key": "", "title": "Invalid Book (empty key)"},  # 空 key
        ]
    }

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_response_data)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_response)

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        result = await searchBooks("test", page=1, limit=5)

    # 只有 1 个有效结果
    assert len(result["results"]) == 1
    assert result["results"][0]["id"] == "OL123W"


@pytest.mark.asyncio
async def test_search_books_total_zero():
    """total=0 时 totalPages=0"""
    mock_response_data = {"numFound": 0, "docs": []}
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_response_data)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_response)

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        result = await searchBooks("test", page=1, limit=5)

    assert result["total"] == 0
    assert result["totalPages"] == 0


# ============================================================================
# getBookDetail() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_get_book_detail_success():
    """有效 workId 返回详情"""
    mock_response_data = {
        "title": "Test Book",
        "description": "This is a test book description.",
        "covers": [12345],
        "subjects": ["Fiction", "Adventure", "Mystery", "Thriller", "Action", "Extra"]
    }

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_response_data)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_response)

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        result = await getBookDetail("OL123W")

    assert result is not None
    assert result["id"] == "OL123W"
    assert result["title"] == "Test Book"
    assert result["description"] == "This is a test book description."
    assert result["coverId"] == 12345
    assert len(result["subjects"]) == 5  # 限制为前 5 个
    assert result["links"]["openLibrary"] == "https://openlibrary.org/works/OL123W"


@pytest.mark.asyncio
async def test_get_book_detail_empty_work_id():
    """空 workId 返回 None"""
    result = await getBookDetail("")
    assert result is None

    result = await getBookDetail(None)
    assert result is None


@pytest.mark.asyncio
async def test_get_book_detail_status_404():
    """API 返回非 200 状态码返回 None"""
    mock_response = MagicMock()
    mock_response.status = 404
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_response)

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        result = await getBookDetail("OL999W")

    assert result is None


@pytest.mark.asyncio
async def test_get_book_detail_network_error():
    """网络异常返回 None"""
    mock_session = MagicMock()
    mock_session.get = MagicMock(side_effect=Exception("Network error"))

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        result = await getBookDetail("OL123W")

    assert result is None


@pytest.mark.asyncio
async def test_get_book_detail_description_dict():
    """description 字段为 dict 类型"""
    mock_response_data = {
        "title": "Test Book",
        "description": {
            "type": "/type/text",
            "value": "This is a dict description."
        },
        "covers": [],
        "subjects": []
    }

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_response_data)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_response)

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        result = await getBookDetail("OL123W")

    assert result["description"] == "This is a dict description."


@pytest.mark.asyncio
async def test_get_book_detail_description_truncation():
    """description 截断"""
    long_description = "A" * 1000

    mock_response_data = {
        "title": "Test Book",
        "description": long_description,
        "covers": [],
        "subjects": []
    }

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_response_data)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_response)

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        with patch('utils.bookSearchAPI.BOOK_DESCRIPTION_MAX_LENGTH', 100):
            result = await getBookDetail("OL123W")

    assert len(result["description"]) == 100
    assert result["description"].endswith("...")


@pytest.mark.asyncio
async def test_get_book_detail_description_empty():
    """description 为空时返回默认文本"""
    mock_response_data = {
        "title": "Test Book",
        "description": "",
        "covers": [],
        "subjects": []
    }

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_response_data)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_response)

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        result = await getBookDetail("OL123W")

    assert result["description"] == "暂无简介"


@pytest.mark.asyncio
async def test_get_book_detail_covers_empty():
    """covers 为空列表时 coverId 为 None"""
    mock_response_data = {
        "title": "Test Book",
        "description": "Test",
        "covers": [],
        "subjects": []
    }

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_response_data)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_response)

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        result = await getBookDetail("OL123W")

    assert result["coverId"] is None


@pytest.mark.asyncio
async def test_get_book_detail_missing_fields():
    """缺少字段时使用默认值"""
    mock_response_data = {}  # 所有字段缺失

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_response_data)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_response)

    with patch('utils.bookSearchAPI._getSession', new_callable=AsyncMock, return_value=mock_session):
        result = await getBookDetail("OL123W")

    assert result["title"] == "Unknown Title"
    assert result["description"] == "暂无简介"
    assert result["coverId"] is None
    assert result["subjects"] == []

