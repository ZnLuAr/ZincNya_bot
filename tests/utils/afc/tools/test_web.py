"""
tests/utils/afc/tools/test_web.py

测试网页读取工具 utils/afc/tools/web
"""

from unittest.mock import AsyncMock, patch

import pytest

from utils.afc.errors import ToolError
from utils.afc.tools.web import readURL, _formatResult
from utils.afc.tools.web.config import WEB_FETCH_MAX_CHARS, WEB_FETCH_BLOCKED_HOSTS
from utils.afc.tools.web.fetcher import (
    _isSafeIPAddress,
    _validateURL,
    _makeResult,
    _isTextContentType,
    _isBinaryContent,
    _normalizeText,
    _extractHTML,
)


# ============================================================================
# _isSafeIPAddress() 测试 — SSRF 防护
# ============================================================================

def test_is_safe_ip_public():
    """公网 IP 通过"""
    assert _isSafeIPAddress("8.8.8.8") is True
    assert _isSafeIPAddress("1.1.1.1") is True
    assert _isSafeIPAddress("2606:4700:4700::1111") is True


def test_is_safe_ip_private():
    """私有 IP 拒绝"""
    assert _isSafeIPAddress("192.168.1.1") is False
    assert _isSafeIPAddress("10.0.0.1") is False
    assert _isSafeIPAddress("172.16.0.1") is False
    assert _isSafeIPAddress("fd00::1") is False


def test_is_safe_ip_loopback():
    """回环地址拒绝"""
    assert _isSafeIPAddress("127.0.0.1") is False
    assert _isSafeIPAddress("::1") is False


def test_is_safe_ip_link_local():
    """链路本地地址拒绝"""
    assert _isSafeIPAddress("169.254.1.1") is False
    assert _isSafeIPAddress("fe80::1") is False


def test_is_safe_ip_multicast():
    """组播地址拒绝"""
    assert _isSafeIPAddress("224.0.0.1") is False
    assert _isSafeIPAddress("ff02::1") is False


def test_is_safe_ip_unspecified():
    """未指定地址拒绝"""
    assert _isSafeIPAddress("0.0.0.0") is False
    assert _isSafeIPAddress("::") is False


def test_is_safe_ip_invalid():
    """无效 IP 字符串拒绝"""
    assert _isSafeIPAddress("not-an-ip") is False
    assert _isSafeIPAddress("999.999.999.999") is False
    assert _isSafeIPAddress("") is False


# ============================================================================
# _validateURL() 测试
# ============================================================================

def test_validate_url_valid():
    """有效 URL 通过"""
    ok, err = _validateURL("https://example.com/path", blockedHosts=[])
    assert ok is True
    assert err == ""


def test_validate_url_http():
    """HTTP 协议通过"""
    ok, err = _validateURL("http://example.com", blockedHosts=[])
    assert ok is True


def test_validate_url_invalid_scheme():
    """不支持的协议拒绝"""
    ok, err = _validateURL("ftp://example.com", blockedHosts=[])
    assert ok is False
    assert "不支持的协议" in err


def test_validate_url_no_scheme():
    """缺少协议拒绝"""
    ok, err = _validateURL("example.com", blockedHosts=[])
    assert ok is False


def test_validate_url_no_hostname():
    """缺少 hostname 拒绝"""
    ok, err = _validateURL("https://", blockedHosts=[])
    assert ok is False
    assert "hostname" in err.lower()


def test_validate_url_with_userinfo():
    """包含 userinfo 拒绝"""
    ok, err = _validateURL("https://user:pass@example.com", blockedHosts=[])
    assert ok is False
    assert "userinfo" in err


def test_validate_url_blocked_host():
    """黑名单 host 拒绝"""
    ok, err = _validateURL("https://evil.com/path", blockedHosts=["evil.com"])
    assert ok is False
    assert "blocked host" in err


def test_validate_url_blocked_subdomain():
    """黑名单子域名拒绝"""
    ok, err = _validateURL("https://sub.evil.com/path", blockedHosts=["evil.com"])
    assert ok is False
    assert "blocked host" in err


def test_validate_url_ip_literal_private():
    """私有 IP literal 拒绝"""
    ok, err = _validateURL("http://192.168.1.1/", blockedHosts=[])
    assert ok is False
    assert "不是可路由公网地址" in err


def test_validate_url_ip_literal_public():
    """公网 IP literal 通过"""
    ok, err = _validateURL("http://8.8.8.8/", blockedHosts=[])
    assert ok is True


def test_validate_url_localhost():
    """localhost 拒绝"""
    ok, err = _validateURL("http://127.0.0.1/", blockedHosts=[])
    assert ok is False


def test_validate_url_default_blocked_hosts():
    """不传 blockedHosts 时使用工具配置里的黑名单"""
    assert "localhost" in WEB_FETCH_BLOCKED_HOSTS

    ok, err = _validateURL("http://localhost/")
    assert ok is False
    assert "blocked host" in err

    ok, err = _validateURL("http://metadata.google.internal/computeMetadata/v1/")
    assert ok is False


# ============================================================================
# _isTextContentType() 测试
# ============================================================================

def test_is_text_content_type_html():
    """HTML 类型识别"""
    assert _isTextContentType("text/html", "http://example.com") is True
    assert _isTextContentType("text/html; charset=utf-8", "http://example.com") is True


def test_is_text_content_type_plain():
    """纯文本类型识别"""
    assert _isTextContentType("text/plain", "http://example.com") is True


def test_is_text_content_type_json():
    """JSON 类型识别"""
    assert _isTextContentType("application/json", "http://example.com") is True


def test_is_text_content_type_xml():
    """XML 类型识别"""
    assert _isTextContentType("application/xml", "http://example.com") is True
    assert _isTextContentType("application/xhtml+xml", "http://example.com") is True


def test_is_text_content_type_markdown():
    """Markdown 类型识别"""
    assert _isTextContentType("text/markdown", "http://example.com") is True


def test_is_text_content_type_octet_stream_with_extension():
    """octet-stream 但 URL 有文本扩展名"""
    assert _isTextContentType("application/octet-stream", "http://example.com/file.md") is True
    assert _isTextContentType("application/octet-stream", "http://example.com/file.txt") is True
    assert _isTextContentType("application/octet-stream", "http://example.com/file.json") is True


def test_is_text_content_type_octet_stream_no_extension():
    """octet-stream 且无文本扩展名"""
    assert _isTextContentType("application/octet-stream", "http://example.com/file.bin") is False


def test_is_text_content_type_binary():
    """二进制类型拒绝"""
    assert _isTextContentType("image/png", "http://example.com") is False
    assert _isTextContentType("video/mp4", "http://example.com") is False
    assert _isTextContentType("application/pdf", "http://example.com") is False


# ============================================================================
# _isBinaryContent() 测试
# ============================================================================

def test_is_binary_content_with_null():
    """包含 NUL 字节视为二进制"""
    assert _isBinaryContent(b"Hello\x00World") is True


def test_is_binary_content_high_control_chars():
    """控制字符比例过高视为二进制"""
    data = bytes([1, 2, 3, 4, 5] * 100)  # 全是控制字符
    assert _isBinaryContent(data) is True


def test_is_binary_content_text():
    """纯文本不视为二进制"""
    assert _isBinaryContent(b"Hello World") is False
    assert _isBinaryContent("你好世界".encode("utf-8")) is False


def test_is_binary_content_with_newlines():
    """包含换行符的文本不视为二进制"""
    data = b"Line 1\nLine 2\nLine 3"
    assert _isBinaryContent(data) is False


def test_is_binary_content_empty():
    """空数据不视为二进制"""
    assert _isBinaryContent(b"") is False


# ============================================================================
# _normalizeText() 测试
# ============================================================================

def test_normalize_text_basic():
    """基本文本规范化"""
    result = _normalizeText("  Hello  \n  World  ")
    # 两行非空内容之间没有空行
    assert result == "Hello\nWorld"


def test_normalize_text_multiple_empty_lines():
    """合并过多空行为最多两行"""
    text = "Line 1\n\n\n\n\nLine 2"
    result = _normalizeText(text)
    # 最多保留两个空行（即三个换行符）
    assert result == "Line 1\n\n\nLine 2"


def test_normalize_text_trailing_whitespace():
    """去除行尾空白"""
    text = "Line 1   \nLine 2\t\n"
    result = _normalizeText(text)
    # 两行非空内容之间没有空行
    assert result == "Line 1\nLine 2"


def test_normalize_text_empty():
    """空文本返回空字符串"""
    assert _normalizeText("") == ""
    assert _normalizeText("   \n\n   ") == ""


# ============================================================================
# _extractHTML() 测试
# ============================================================================

def test_extract_html_basic():
    """基本 HTML 提取"""
    html = "<html><head><title>Test Page</title></head><body><p>Hello World</p></body></html>"
    title, body = _extractHTML(html)
    assert title == "Test Page"
    assert "Hello World" in body


def test_extract_html_remove_script():
    """移除 script 标签"""
    html = "<html><body><p>Content</p><script>alert('xss')</script></body></html>"
    title, body = _extractHTML(html)
    assert "alert" not in body
    assert "Content" in body


def test_extract_html_remove_style():
    """移除 style 标签"""
    html = "<html><body><p>Content</p><style>body { color: red; }</style></body></html>"
    title, body = _extractHTML(html)
    assert "color" not in body
    assert "Content" in body


def test_extract_html_markdown_body():
    """优先提取 markdown-body 类"""
    html = """
    <html><body>
        <div>Sidebar</div>
        <article class="markdown-body">Main Content</article>
    </body></html>
    """
    title, body = _extractHTML(html)
    assert "Main Content" in body
    assert "Sidebar" not in body


def test_extract_html_article():
    """提取 article 标签"""
    html = """
    <html><body>
        <div>Header</div>
        <article>Article Content</article>
        <div>Footer</div>
    </body></html>
    """
    title, body = _extractHTML(html)
    assert "Article Content" in body


def test_extract_html_main():
    """提取 main 标签"""
    html = """
    <html><body>
        <nav>Navigation</nav>
        <main>Main Content</main>
        <footer>Footer</footer>
    </body></html>
    """
    title, body = _extractHTML(html)
    assert "Main Content" in body


def test_extract_html_no_title():
    """无 title 标签"""
    html = "<html><body><p>Content</p></body></html>"
    title, body = _extractHTML(html)
    assert title == ""
    assert "Content" in body


def test_extract_html_fallback_to_body():
    """无特殊容器时回退到 body"""
    html = "<html><body><p>Paragraph 1</p><p>Paragraph 2</p></body></html>"
    title, body = _extractHTML(html)
    assert "Paragraph 1" in body
    assert "Paragraph 2" in body


def test_extract_html_invalid():
    """无效 HTML 回退到纯文本"""
    html = "Not really HTML <unclosed tag"
    title, body = _extractHTML(html)
    # 应该不抛异常，返回某种文本
    assert isinstance(title, str)
    assert isinstance(body, str)


# ============================================================================
# readURL() 工具入口测试
# ============================================================================

def _okResult(text="正文", title="标题", truncatedBytes=False):
    result = _makeResult("https://example.com/a")
    result.update({
        "ok": True,
        "finalUrl": "https://example.com/b",
        "status": 200,
        "contentType": "text/html",
        "title": title,
        "text": text,
        "truncatedBytes": truncatedBytes,
    })
    return result


async def test_read_url_success():
    """成功时交回地址、标题和正文，并去掉参数两端空白"""
    fetch = AsyncMock(return_value=_okResult())
    with patch("utils.afc.tools.web.fetcher.fetchURL", fetch):
        output = await readURL("  https://example.com/a  ")

    fetch.assert_awaited_once_with("https://example.com/a")
    assert output == "地址：https://example.com/b\n标题：标题\n\n正文"


@pytest.mark.parametrize("url", ["", "   ", None, 123])
async def test_read_url_empty_raises(url):
    """空地址或非字符串直接报告失败，不发起抓取"""
    fetch = AsyncMock()
    with patch("utils.afc.tools.web.fetcher.fetchURL", fetch):
        with pytest.raises(ToolError):
            await readURL(url)

    fetch.assert_not_awaited()


async def test_read_url_fetch_failure_raises():
    """抓取失败时抛 ToolError，原因取自结果的 error 字段"""
    failed = _makeResult("https://example.com/a")
    failed["error"] = "HTTP 404"

    with patch("utils.afc.tools.web.fetcher.fetchURL", AsyncMock(return_value=failed)):
        with pytest.raises(ToolError, match="HTTP 404"):
            await readURL("https://example.com/a")


def test_format_result_truncates_long_text():
    """正文超过上限时截断并标注"""
    output = _formatResult(_okResult(text="x" * (WEB_FETCH_MAX_CHARS + 50), title=""))

    assert "只保留了开头部分" in output
    assert "标题：" not in output
    assert output.endswith("x" * WEB_FETCH_MAX_CHARS)
    assert "x" * (WEB_FETCH_MAX_CHARS + 1) not in output


def test_format_result_marks_byte_truncation():
    """抓取阶段被字节上限截断时同样标注"""
    output = _formatResult(_okResult(truncatedBytes=True))
    assert "只保留了开头部分" in output


def test_format_result_falls_back_to_requested_url():
    """没有最终地址时用请求地址"""
    result = _okResult()
    result["finalUrl"] = None
    assert _formatResult(result).startswith("地址：https://example.com/a\n")
