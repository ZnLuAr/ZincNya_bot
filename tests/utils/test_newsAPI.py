"""
tests/utils/test_newsAPI.py

测试 utils/newsAPI.py
"""

import pytest
import json
from unittest.mock import patch, MagicMock, AsyncMock, mock_open
from io import BytesIO

from utils.newsAPI import (
    NewsArticle,
    isAlreadyPushed,
    markAsPushed,
    loadPushedRecord,
    savePushedRecord,
    _getSession,
    _closeSession,
    fetchLatestNews,
    downloadCover,
    pushToTelegram,
    registerResources,
)


# ============================================================================
# isAlreadyPushed() 测试
# ============================================================================

def test_is_already_pushed_true():
    """URL 在记录中返回 True"""
    record = {"pushed_urls": ["https://example.com/article1", "https://example.com/article2"]}
    assert isAlreadyPushed("https://example.com/article1", record) is True


def test_is_already_pushed_false():
    """URL 不在记录中返回 False"""
    record = {"pushed_urls": ["https://example.com/article1"]}
    assert isAlreadyPushed("https://example.com/article2", record) is False


def test_is_already_pushed_empty_record():
    """空记录返回 False"""
    record = {}
    assert isAlreadyPushed("https://example.com/article1", record) is False


# ============================================================================
# markAsPushed() 测试
# ============================================================================

def test_mark_as_pushed_new_url():
    """标记新 URL"""
    record = {"pushed_urls": []}
    markAsPushed("https://example.com/article1", record)
    assert "https://example.com/article1" in record["pushed_urls"]


def test_mark_as_pushed_duplicate():
    """重复标记不重复添加"""
    record = {"pushed_urls": ["https://example.com/article1"]}
    markAsPushed("https://example.com/article1", record)
    assert record["pushed_urls"].count("https://example.com/article1") == 1


def test_mark_as_pushed_truncate():
    """超过 1000 条时截断"""
    record = {"pushed_urls": [f"https://example.com/article{i}" for i in range(1000)]}
    markAsPushed("https://example.com/new", record)
    assert len(record["pushed_urls"]) == 1000
    assert "https://example.com/new" in record["pushed_urls"]
    assert "https://example.com/article0" not in record["pushed_urls"]


def test_mark_as_pushed_init_list():
    """记录中无 pushed_urls 时初始化"""
    record = {}
    markAsPushed("https://example.com/article1", record)
    assert "pushed_urls" in record
    assert "https://example.com/article1" in record["pushed_urls"]


# ============================================================================
# loadPushedRecord() 测试
# ============================================================================

def test_load_pushed_record_success():
    """正常加载 JSON 文件"""
    mock_data = {"pushed_urls": ["url1", "url2"], "last_check": "2023-01-01T12:00:00"}
    m = mock_open(read_data=json.dumps(mock_data))

    with patch("builtins.open", m):
        record = loadPushedRecord()

    assert record == mock_data


def test_load_pushed_record_file_not_found():
    """文件不存在返回默认值"""
    with patch("builtins.open", side_effect=FileNotFoundError):
        record = loadPushedRecord()

    assert record == {"pushed_urls": [], "last_check": None}


def test_load_pushed_record_json_error():
    """JSON 解析错误返回默认值"""
    m = mock_open(read_data="invalid json")

    with patch("builtins.open", m):
        record = loadPushedRecord()

    assert record == {"pushed_urls": [], "last_check": None}


# ============================================================================
# savePushedRecord() 测试
# ============================================================================

def test_save_pushed_record():
    """保存记录并添加时间戳"""
    record = {"pushed_urls": ["url1", "url2"]}
    m = mock_open()

    with patch("builtins.open", m):
        with patch("utils.newsAPI.datetime") as mock_dt:
            mock_dt.now.return_value.isoformat.return_value = "2023-01-01T12:00:00"
            savePushedRecord(record)

    # 验证时间戳被添加
    assert record["last_check"] == "2023-01-01T12:00:00"

    # 验证文件被写入（不验证具体路径，因为是配置常量）
    m.assert_called_once()
    call_args = m.call_args
    assert call_args[0][1] == "w"
    assert call_args[1]["encoding"] == "utf-8"

    handle = m()
    written_data = "".join(call.args[0] for call in handle.write.call_args_list)
    assert "url1" in written_data
    assert "url2" in written_data


def test_save_pushed_record_json_format():
    """验证 JSON 格式"""
    record = {"pushed_urls": ["url1"]}
    m = mock_open()

    with patch("builtins.open", m):
        with patch("utils.newsAPI.datetime") as mock_dt:
            mock_dt.now.return_value.isoformat.return_value = "2023-01-01T12:00:00"
            savePushedRecord(record)

    handle = m()
    written_data = "".join(call.args[0] for call in handle.write.call_args_list)
    # 验证可以解析为 JSON
    parsed = json.loads(written_data)
    assert "pushed_urls" in parsed
    assert "last_check" in parsed


# ============================================================================
# _getSession() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_get_session_first_call():
    """首次调用创建 Session"""
    # 重置全局变量
    import utils.newsAPI
    utils.newsAPI._session = None

    mock_session = MagicMock()
    with patch("utils.newsAPI.aiohttp.ClientSession", return_value=mock_session):
        session = await _getSession()

    assert session is mock_session


@pytest.mark.asyncio
async def test_get_session_reuse():
    """后续调用返回同一 Session"""
    import utils.newsAPI
    mock_session = MagicMock()
    mock_session.closed = False
    utils.newsAPI._session = mock_session

    session = await _getSession()
    assert session is mock_session


@pytest.mark.asyncio
async def test_get_session_recreate_if_closed():
    """Session 关闭后重新创建"""
    import utils.newsAPI
    old_session = MagicMock()
    old_session.closed = True
    utils.newsAPI._session = old_session

    new_session = MagicMock()
    with patch("utils.newsAPI.aiohttp.ClientSession", return_value=new_session):
        session = await _getSession()

    assert session is new_session


# ============================================================================
# _closeSession() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_close_session_success():
    """关闭已打开的 Session"""
    import utils.newsAPI
    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.close = AsyncMock()
    utils.newsAPI._session = mock_session

    await _closeSession()

    mock_session.close.assert_called_once()
    assert utils.newsAPI._session is None


@pytest.mark.asyncio
async def test_close_session_none():
    """Session 为 None 时不抛出异常"""
    import utils.newsAPI
    utils.newsAPI._session = None

    # 不应抛出异常
    await _closeSession()


# ============================================================================
# fetchLatestNews() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_fetch_latest_news_success():
    """正常解析 HTML 返回文章列表"""
    html_content = """
    <article>
        <h2>Article 1</h2>
        <a href="/article1">Link</a>
        <img src="/cover1.jpg">
        <p>Summary 1</p>
    </article>
    <article>
        <h2>Article 2</h2>
        <a href="https://example.com/article2">Link</a>
    </article>
    """

    mock_response = MagicMock()
    mock_response.text = AsyncMock(return_value=html_content)

    # session.get() 返回 async context manager
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_response)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_cm)

    with patch("utils.newsAPI._getSession", new=AsyncMock(return_value=mock_session)):
        articles = await fetchLatestNews(limit=5)

    assert len(articles) == 2
    assert articles[0].title == "Article 1"
    assert articles[0].url.endswith("/article1")
    assert articles[1].title == "Article 2"


@pytest.mark.asyncio
async def test_fetch_latest_news_relative_path():
    """相对路径转换为绝对路径"""
    html_content = """
    <article>
        <h2>Article</h2>
        <a href="/article1">Link</a>
        <img src="/cover.jpg">
    </article>
    """

    mock_response = MagicMock()
    mock_response.text = AsyncMock(return_value=html_content)

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_response)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_cm)

    with patch("utils.newsAPI._getSession", new=AsyncMock(return_value=mock_session)):
        with patch("utils.newsAPI.NEWS_SOURCE_URL", "https://example.com/news"):
            articles = await fetchLatestNews(limit=5)

    assert articles[0].url == "https://example.com/article1"
    assert articles[0].cover_url == "https://example.com/cover.jpg"


@pytest.mark.asyncio
async def test_fetch_latest_news_limit():
    """limit 参数限制返回数量"""
    html_content = """
    <article><h2>Article 1</h2><a href="/1">Link</a></article>
    <article><h2>Article 2</h2><a href="/2">Link</a></article>
    <article><h2>Article 3</h2><a href="/3">Link</a></article>
    """

    mock_response = MagicMock()
    mock_response.text = AsyncMock(return_value=html_content)

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_response)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_cm)

    with patch("utils.newsAPI._getSession", new=AsyncMock(return_value=mock_session)):
        articles = await fetchLatestNews(limit=2)

    assert len(articles) == 2


@pytest.mark.asyncio
async def test_fetch_latest_news_error():
    """异常时返回空列表（@handleErrors）"""
    mock_session = MagicMock()
    mock_session.get = MagicMock(side_effect=Exception("Network error"))

    with patch("utils.newsAPI._getSession", return_value=mock_session):
        articles = await fetchLatestNews(limit=5)

    assert articles == []


# ============================================================================
# downloadCover() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_download_cover_success():
    """正常下载图片返回二进制数据"""
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.read = AsyncMock(return_value=b"fake_image_data")

    # session.get() 返回 async context manager
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_response)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_cm)

    with patch("utils.newsAPI._getSession", new=AsyncMock(return_value=mock_session)):
        data = await downloadCover("https://example.com/cover.jpg")

    assert data == b"fake_image_data"
    # 验证调用（不验证 proxy 参数，因为是实现细节）
    mock_session.get.assert_called_once()
    assert mock_session.get.call_args[0][0] == "https://example.com/cover.jpg"


@pytest.mark.asyncio
async def test_download_cover_error():
    """异常时返回空 bytes（@handleErrors）"""
    mock_session = MagicMock()
    mock_session.get = MagicMock(side_effect=Exception("Network error"))

    with patch("utils.newsAPI._getSession", return_value=mock_session):
        data = await downloadCover("https://example.com/cover.jpg")

    assert data == b""


# ============================================================================
# pushToTelegram() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_push_to_telegram_with_cover():
    """有封面时发送图片"""
    article = NewsArticle(
        title="Test Article",
        url="https://example.com/article",
        cover_url="https://example.com/cover.jpg",
        summary="Test summary"
    )

    mock_bot = MagicMock()
    mock_bot.send_photo = AsyncMock()

    with patch("utils.newsAPI.downloadCover", return_value=b"image data"):
        result = await pushToTelegram(mock_bot, "123", article)

    assert result is True
    mock_bot.send_photo.assert_called_once()
    call_args = mock_bot.send_photo.call_args
    assert call_args[1]["chat_id"] == "123"
    assert "Test Article" in call_args[1]["caption"]
    assert "Test summary" in call_args[1]["caption"]


@pytest.mark.asyncio
async def test_push_to_telegram_without_cover():
    """无封面时发送纯文本"""
    article = NewsArticle(
        title="Test Article",
        url="https://example.com/article",
        cover_url=None,
        summary="Test summary"
    )

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    result = await pushToTelegram(mock_bot, "123", article)

    assert result is True
    mock_bot.send_message.assert_called_once()
    call_args = mock_bot.send_message.call_args
    assert call_args[1]["chat_id"] == "123"
    assert "Test Article" in call_args[1]["text"]


@pytest.mark.asyncio
async def test_push_to_telegram_cover_download_failed():
    """封面下载失败时发送纯文本"""
    article = NewsArticle(
        title="Test Article",
        url="https://example.com/article",
        cover_url="https://example.com/cover.jpg",
        summary="Test summary"
    )

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    with patch("utils.newsAPI.downloadCover", return_value=b""):
        result = await pushToTelegram(mock_bot, "123", article)

    assert result is True
    mock_bot.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_push_to_telegram_failure():
    """推送失败返回 False"""
    article = NewsArticle(
        title="Test Article",
        url="https://example.com/article",
        cover_url=None
    )

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock(side_effect=Exception("Send failed"))

    with patch("utils.newsAPI.logSystemEvent", new_callable=AsyncMock):
        result = await pushToTelegram(mock_bot, "123", article)

    assert result is False


# ============================================================================
# registerResources() 测试
# ============================================================================

def test_register_resources():
    """注册资源清理回调"""
    mock_manager = MagicMock()

    with patch("utils.newsAPI.getResourceManager", return_value=mock_manager):
        registerResources()

    mock_manager.register.assert_called_once()
    call_args = mock_manager.register.call_args
    assert call_args[0][0] == "newsAPI Session"
    assert call_args[1]["priority"] == 10