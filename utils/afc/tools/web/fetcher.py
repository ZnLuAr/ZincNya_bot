"""
utils/afc/tools/web/fetcher.py

网页抓取：
    - SSRF 防护（blocked hosts、private IP、DNS 校验）
    - 手动 redirect 处理，逐跳校验
    - streaming byte cap + 二进制嗅探
    - HTML / 纯文本提取

对外只有 fetchURL：抓取一个地址，交回全字段的结果 dict，失败原因写在 error 里。
"""

import asyncio
import ipaddress
import socket
from typing import Optional
from urllib.parse import urlsplit, urljoin

import aiohttp
from bs4 import BeautifulSoup

from utils.core.logger import logSystemEvent, LogLevel, LogChildType
from utils.core.resourceManager import getResourceManager

from .config import (
    WEB_FETCH_TIMEOUT_SECONDS,
    WEB_FETCH_MAX_RETRIES,
    WEB_FETCH_RETRY_BACKOFF_SECONDS,
    WEB_FETCH_REDIRECT_LIMIT,
    WEB_FETCH_MAX_BYTES,
    WEB_FETCH_BLOCKED_HOSTS,
)


_USER_AGENT = "Mozilla/5.0 (compatible; ZincNyaBot/1.0; Telegram link preview)"
_ACCEPT = "text/html,text/plain,text/markdown,application/json,application/xml,application/xhtml+xml,*/*;q=0.1"
_CHUNK_SIZE = 8192
_BINARY_SNIFF_BYTES = 4096
_BINARY_CONTROL_RATIO = 0.3




# ============================================================================
# 全局 aiohttp Session 复用连接池
# ============================================================================

_session: Optional[aiohttp.ClientSession] = None
_sessionLock = asyncio.Lock()
_cleanupRegistered = False


async def _getSession() -> aiohttp.ClientSession:
    """
    获取全局 aiohttp Session 单例。

    使用 asyncio.Lock 防止并发创建多个 Session。首次创建时向资源管理器登记关闭
    回调：工具可以单独安装和卸载，所以不依赖模块的 initFunctions。
    """
    global _session, _cleanupRegistered

    if _session is None or _session.closed:
        async with _sessionLock:
            if _session is None or _session.closed:
                _session = aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(
                        resolver=_SafeResolver(),
                        use_dns_cache=False,
                    ),
                    cookie_jar=aiohttp.DummyCookieJar(),
                    trust_env=False,
                    timeout=aiohttp.ClientTimeout(total=WEB_FETCH_TIMEOUT_SECONDS),
                    headers={
                        "User-Agent": _USER_AGENT,
                        "Accept": _ACCEPT,
                    },
                )
                if not _cleanupRegistered:
                    getResourceManager().register("Web Tool Session", _closeSession, priority=10)
                    _cleanupRegistered = True

    return _session


async def _closeSession():
    """关闭全局 Session（由资源管理器自动调用）"""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None




# ============================================================================
# SSRF 防护，拒绝解析到私有 / 回环 / 链路本地地址
# ============================================================================

class _SafeResolver(aiohttp.abc.AbstractResolver):
    """
    DNS resolver wrapper：拒绝解析到 private / loopback / link-local 等非公网地址。

    这样避免"校验时一个 DNS 结果、连接时另一个 DNS 结果"的 TOCTOU 问题。
    解析结果不安全时抛 OSError，aiohttp 会包成 ClientConnectorError。
    """

    def __init__(self):
        self._resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC):
        result = await self._resolver.resolve(host, port, family)

        for addrInfo in result:
            ip = addrInfo["host"]
            if not _isSafeIPAddress(ip):
                raise OSError(f"DNS 解析结果 {ip}（host={host}）不是可路由公网地址")

        return result

    async def close(self):
        await self._resolver.close()


def _isSafeIPAddress(ipStr: str) -> bool:
    """判断 IP 是否可路由公网地址（拒绝 private / loopback / link-local / multicast / reserved / unspecified）"""
    try:
        addr = ipaddress.ip_address(ipStr)
    except ValueError:
        return False

    # IPv4-mapped IPv6 → 按 IPv4 判断
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped

    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return False
    if addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        return False
    if not addr.is_global:
        return False

    return True




# ============================================================================
# URL 安全校验
# ============================================================================

def _validateURL(url: str, *, blockedHosts=WEB_FETCH_BLOCKED_HOSTS) -> tuple[bool, str]:
    """
    校验 URL 的基本安全性。

    参数:
        blockedHosts: 拒绝访问的 hostname（含其子域），默认取工具配置

    返回:
        (True, "") 通过校验
        (False, 中文错误说明) 被拒绝
    """
    try:
        parsed = urlsplit(url)
    except Exception as e:
        return False, f"URL 解析失败：{e}"

    if parsed.scheme not in ("http", "https"):
        return False, f"不支持的协议：{parsed.scheme or '(空)'}"

    if not parsed.netloc:
        return False, "URL 缺少 hostname"

    if "@" in parsed.netloc:
        return False, "URL 中不允许包含 userinfo"

    host = parsed.hostname
    if not host:
        return False, "URL hostname 无效"

    host = host.lower().rstrip(".")

    for bh in blockedHosts:
        if host == bh or host.endswith(f".{bh}"):
            return False, f"命中 blocked host：{host}"

    # 直接 IP literal 判断是否公网
    try:
        addr = ipaddress.ip_address(host)
        if not _isSafeIPAddress(str(addr)):
            return False, f"IP {host} 不是可路由公网地址"
    except ValueError:
        pass

    return True, ""




# ============================================================================
# Content-Type 与二进制嗅探
# ============================================================================

_TEXT_CONTENT_TYPES = {
    "text/html", "text/plain", "text/markdown", "text/csv", "text/css",
    "application/json", "application/xml", "application/xhtml+xml",
    "application/rss+xml", "application/atom+xml",
    "application/yaml", "application/x-yaml",
}

_TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".xml",
    ".html", ".htm", ".css", ".go", ".rs", ".java", ".kt", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".php", ".rb", ".sh", ".ps1", ".sql", ".log",
}


def _isTextContentType(contentType: str, url: str) -> bool:
    """判断响应 Content-Type 是否可视为文本。"""
    ct = contentType.lower().split(";")[0].strip()

    if ct.startswith("text/"):
        return True
    if ct in _TEXT_CONTENT_TYPES:
        return True

    # 考虑到有些服务器会返回 application/octet-stream，此时按 URL 扩展名兜底
    if ct == "application/octet-stream":
        path = urlsplit(url).path.lower()
        for ext in _TEXT_EXTENSIONS:
            if path.endswith(ext):
                return True

    return False


def _isBinaryContent(data: bytes) -> bool:
    """二进制嗅探：前几 KB 有 NUL 字节或控制字符比例过高则视为二进制。"""
    sample = data[:_BINARY_SNIFF_BYTES]
    if b"\x00" in sample:
        return True

    controlCount = sum(1 for b in sample if b < 32 and b not in (9, 10, 13))
    if len(sample) > 0 and controlCount / len(sample) > _BINARY_CONTROL_RATIO:
        return True

    return False




# ============================================================================
# 抓取结果工厂
# ============================================================================

def _makeResult(requestedUrl: str) -> dict:
    """构造一个全字段初始化的抓取结果，避免后续 key 漂移。"""
    return {
        "requestedUrl": requestedUrl,
        "finalUrl": None,
        "ok": False,
        "status": None,
        "contentType": "",
        "title": "",
        "text": "",
        "error": "",
        "bytesRead": 0,
        "truncatedBytes": False,
        "redirectChain": [],
    }




# ============================================================================
# HTTP 抓取：手动 redirect + streaming byte cap + 重试
# ============================================================================

async def fetchURL(url: str) -> dict:
    """
    抓取单个 URL，返回抓取结果 dict（字段见 _makeResult）。

    失败也返回 result，原因写在 error 字段，不对外抛异常（取消除外）。
    结束前会打一条 ops 日志。
    """
    result = _makeResult(url)

    for attempt in range(WEB_FETCH_MAX_RETRIES + 1):
        try:
            await _fetchWithRedirect(url, WEB_FETCH_REDIRECT_LIMIT, result)
            break
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            if attempt < WEB_FETCH_MAX_RETRIES:
                await asyncio.sleep(WEB_FETCH_RETRY_BACKOFF_SECONDS)
                continue
            result["error"] = f"抓取失败（{attempt + 1} 次尝试均出错）：{type(e).__name__} {e}"
            break
        except Exception as e:
            result["error"] = f"抓取时出现意外异常：{type(e).__name__} {e}"
            break

    await _logFetchResult(result)
    return result


async def _fetchWithRedirect(startUrl: str, redirectLimit: int, result: dict) -> None:
    """
    手动处理 redirect，每一跳前都执行完整 URL 安全校验。

    结果通过传入的 result dict 原地填充；失败会写入 error 字段。
    """
    currentUrl = startUrl
    redirectChain: list[str] = result["redirectChain"]
    session = await _getSession()
    maxBytes = WEB_FETCH_MAX_BYTES

    for hop in range(redirectLimit + 1):
        ok, err = _validateURL(currentUrl)
        if not ok:
            result["finalUrl"] = currentUrl
            result["error"] = err
            return

        async with session.get(currentUrl, allow_redirects=False) as resp:
            status = resp.status
            result["status"] = status
            result["finalUrl"] = currentUrl

            # 3xx：手动处理 redirect
            if status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if not location:
                    result["error"] = f"HTTP {status} redirect 但缺少 Location header"
                    return

                nextUrl = urljoin(currentUrl, location)
                redirectChain.append(currentUrl)

                if hop >= redirectLimit:
                    result["error"] = f"redirect 跳数超过上限 {redirectLimit}"
                    result["finalUrl"] = nextUrl
                    return

                currentUrl = nextUrl
                continue

            # 4xx / 5xx
            if status >= 400:
                result["error"] = f"HTTP {status}"
                return

            contentType = resp.headers.get("Content-Type", "")
            result["contentType"] = contentType

            if not _isTextContentType(contentType, currentUrl):
                result["error"] = f"非文本 Content-Type：{contentType or '(空)'}"
                return

            # 流式读取，超过 maxBytes 立即停止
            chunks: list[bytes] = []
            bytesRead = 0
            truncatedBytes = False

            async for chunk in resp.content.iter_chunked(_CHUNK_SIZE):
                if bytesRead + len(chunk) > maxBytes:
                    remaining = maxBytes - bytesRead
                    if remaining > 0:
                        chunks.append(chunk[:remaining])
                        bytesRead += remaining
                    truncatedBytes = True
                    break
                chunks.append(chunk)
                bytesRead += len(chunk)

            rawData = b"".join(chunks)
            result["bytesRead"] = bytesRead
            result["truncatedBytes"] = truncatedBytes

            if _isBinaryContent(rawData):
                result["error"] = "响应疑似二进制数据"
                return

            # 解码
            charset = "utf-8"
            if "charset=" in contentType:
                try:
                    charset = contentType.split("charset=")[-1].split(";")[0].strip()
                except Exception:
                    pass

            try:
                decoded = rawData.decode(charset, errors="replace")
            except Exception:
                decoded = rawData.decode("utf-8", errors="replace")

            title, body = _extractContent(decoded, contentType)
            result["title"] = title
            result["text"] = body
            result["ok"] = True
            return

    # 理论上不会到这里（循环总会走到 return）
    result["error"] = "抓取循环异常退出"


async def _logFetchResult(result: dict) -> None:
    """把抓取结果写进 ops 日志，失败单独用 WARNING 级别便于排查。"""
    url = result.get("requestedUrl", "?")

    if result.get("ok"):
        status = result.get("status")
        ctype = result.get("contentType", "")
        bytesRead = result.get("bytesRead", 0)
        truncatedBytes = result.get("truncatedBytes", False)
        truncatedNote = "，内容被字节上限截断" if truncatedBytes else ""
        await logSystemEvent(
            "网页读取完成",
            f"{url} | HTTP {status} | {ctype} | {bytesRead} bytes{truncatedNote}",
            LogLevel.INFO,
            LogChildType.WITH_ONE_CHILD,
        )
        return

    err = result.get("error") or "未知错误"
    status = result.get("status")
    statusPart = f"HTTP {status} | " if status else ""
    await logSystemEvent(
        "网页读取失败",
        f"{url} | {statusPart}{err}",
        LogLevel.WARNING,
        LogChildType.WITH_ONE_CHILD,
    )




# ============================================================================
# 内容提取
# ============================================================================

def _extractContent(text: str, contentType: str) -> tuple[str, str]:
    """根据 Content-Type 提取 (title, body)。非 HTML 走纯文本 normalize。"""
    if "html" in contentType.lower():
        return _extractHTML(text)
    return "", _normalizeText(text)


def _extractHTML(html: str) -> tuple[str, str]:
    """从 HTML 提取 title 和正文，优先 markdown-body → article → main → body。"""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return "", _normalizeText(html)

    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()

    titleTag = soup.find("title")
    title = titleTag.get_text(strip=True) if titleTag else ""

    container = (
        soup.find("article", class_="markdown-body")
        or soup.find(class_="markdown-body")
        or soup.find("article")
        or soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.find("body")
    )

    if container:
        body = container.get_text(separator="\n", strip=True)
    else:
        body = soup.get_text(separator="\n", strip=True)

    return title, _normalizeText(body)


def _normalizeText(text: str) -> str:
    """行尾空白清理，合并过多空行为最多两行。"""
    lines = text.splitlines()
    result: list[str] = []
    emptyCount = 0

    for line in lines:
        line = line.strip()
        if not line:
            emptyCount += 1
            if emptyCount <= 2:
                result.append("")
        else:
            emptyCount = 0
            result.append(line)

    return "\n".join(result).strip()
