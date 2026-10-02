"""
utils/llm/client/memorySelector.py

记忆选择的单次异步传输；请求与清理分别持有，不复用主生成重试链

是 LLM Selector 的网络请求层。负责：
  - 发送 HTTP 请求到独立的 LLM 端点（用于 memory 混合检索的选择器）
  - 超时控制：在指定时间内完成请求，否则取消
  - 资源管理：确保 HTTP 连接、流、transport 都被正确关闭
  - 错误处理：统一的错误码（selectorTimeout, selectorUnconfigured 等）
  - 生命周期追踪：记录每个阶段的时间和状态到 receipt 字典

因为是 LLM HTTP 客户端，所以放在 client 下。
"""

import asyncio
import json
import math
import time
from urllib.parse import urlsplit

import httpx

from config import (
    LLM_MEMORY_SELECTOR_MAX_REQUEST_BYTES,
    LLM_MEMORY_SELECTOR_MAX_RESPONSE_BYTES,
    LLM_MEMORY_SELECTOR_MAX_SECONDS,
    LLM_MEMORY_SELECTOR_CLOSE_RESERVE_SECONDS,
)

from utils.llm.memory.selector import decodeSelectorJson




class MemorySelectionError(ValueError):
    """只携带静态原因码，避免异常正文泄露请求内容或凭据"""


class _DeferredStream(httpx.AsyncByteStream):
    """保留HTTPX解码迭代，将真实流关闭交给独立清理任务"""

    def __init__(self, inner):
        """只保存底层流，不缓冲正文"""
        self.inner = inner

    async def __aiter__(self):
        """HTTPX继续负责解压；本层原样转发底层字节"""
        async for chunk in self.inner:
            yield chunk

    async def aclose(self):
        """HTTPX自动关闭只结束逻辑读取，实际资源由cleanup持有"""


class _OwnedTransport(httpx.AsyncBaseTransport):
    """保存响应原始流和HTTP状态，保证底层资源只有一个清理所有者"""

    def __init__(self, inner, receipt):
        """绑定同次请求诊断，不保存请求正文或认证头"""
        self.inner, self.receipt = inner, receipt
        self.stream = None

    async def handle_async_request(self, request):
        """最多一次发送，先保存状态及资源再交给读取器"""
        if self.receipt["sent"]:
            raise MemorySelectionError("selectorDuplicateSend")
        self.receipt["sent"] = True
        response = await self.inner.handle_async_request(request)
        self.receipt.update(responseReceived=True, httpStatus=response.status_code)
        self.stream = response.stream
        response.stream = _DeferredStream(self.stream)
        return response

    async def aclose(self):
        """client逻辑退出不重复关闭底层；cleanup显式执行真正的关闭"""


async def _closeResource(resource, phase, receipt):
    """实际aclose成功才标完成，错误只留下静态类型并继续尝试其他资源"""
    receipt[phase + "CloseStarted"] = True
    try:
        await resource.aclose()
    except asyncio.CancelledError:
        receipt[phase + "CloseError"] = "CancelledError"
        raise
    except Exception as error:
        receipt[phase + "CloseError"] = type(error).__name__
    else:
        receipt[phase + "Closed"] = True


async def requestMemorySelection(
    body: dict,
    *,
    baseURL: str | None,
    apiKey: str | None,
    proxy: str | None = None,
    timeoutSeconds: float,
    lease,
    owner,
    protocol: str = "responses",
    lifecycle: dict | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """在选择期限内决定结果；请求和清理由同次 lease/runtime 持有至实际终止

    transport 仅供 mock 注入端点/密钥须由调用方明确提供，绝不读取
    默认代理、研究环境变量或 Codex 凭据独立清理不受调用方取消，
    慢关闭时本次选择失败并保留名额；超时后远端执行和计费状态未知
    """
    started = time.monotonic()
    if not isinstance(baseURL, str) or not isinstance(apiKey, str) or not apiKey.strip():
        raise MemorySelectionError("selectorUnconfigured")
    try:
        parsed = urlsplit(baseURL)
        validURL = (
            parsed.scheme == "https" and bool(parsed.hostname)
            and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
        )
    except ValueError:
        validURL = False
    if not validURL:
        raise MemorySelectionError("selectorEndpoint")
    if protocol not in ("responses", "messages"):
        raise MemorySelectionError("selectorProtocol")
    if (
        type(timeoutSeconds) not in (int, float)
        or not math.isfinite(timeoutSeconds)
        or not 0 < timeoutSeconds <= LLM_MEMORY_SELECTOR_MAX_SECONDS
    ):
        raise MemorySelectionError("selectorTimeoutConfig")
    try:
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError):
        raise MemorySelectionError("selectorRequest") from None
    if len(encoded) > LLM_MEMORY_SELECTOR_MAX_REQUEST_BYTES:
        raise MemorySelectionError("selectorRequestTooLarge")
    if not owner.selectorAccepting():
        raise MemorySelectionError("selectorStopping")
    deadline = started + timeoutSeconds
    reserve = min(LLM_MEMORY_SELECTOR_CLOSE_RESERVE_SECONDS, timeoutSeconds / 5)
    requestDeadline = deadline - reserve
    receipt = lifecycle if lifecycle is not None else {}
    receipt.update(sent=False, responseReceived=False, responseClosed=False, transportClosed=False,
                   requestDone=False, cleanupDone=False, requestDeadlineExpired=False, cancelRequests=0,
                   requestFailure=None, totalBudgetSeconds=timeoutSeconds, closeReserveSeconds=reserve)
    owned = _OwnedTransport(transport or httpx.AsyncHTTPTransport(proxy=proxy, retries=0), receipt)
    client = None


    async def sendRequest():
        """请求任务只读取及解析，错误不被后续资源关闭覆盖"""
        nonlocal client
        try:
            if not owner.selectorAccepting():
                raise MemorySelectionError("selectorStopping")
            if time.monotonic() >= requestDeadline:
                receipt["requestDeadlineExpired"] = True
                raise MemorySelectionError("selectorTimeout")
            client = httpx.AsyncClient(transport=owned, trust_env=False, follow_redirects=False,
                                       timeout=httpx.Timeout(timeoutSeconds))
            headers = {"Authorization": "Bearer " + apiKey, "Content-Type": "application/json"}
            if protocol == "messages":
                headers["anthropic-version"] = "2023-06-01"
            request = client.build_request("POST", baseURL.rstrip("/") + "/" + protocol,
                                           headers=headers, content=encoded)
            response = await client.send(request, stream=True)
            if response.status_code != 200:
                raise MemorySelectionError("selectorHTTP")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                if len(chunks) + len(chunk) > LLM_MEMORY_SELECTOR_MAX_RESPONSE_BYTES:
                    raise MemorySelectionError("selectorResponseTooLarge")
                chunks.extend(chunk)
            result = decodeSelectorJson(chunks.decode("utf-8"))
            receipt["requestOutcome"] = "success"
            return result
        except asyncio.CancelledError:
            receipt["requestOutcome"] = "cancelled"
            raise
        except MemorySelectionError as error:
            receipt["requestFailure"] = str(error)
            raise
        except (TimeoutError, httpx.TimeoutException):
            receipt["requestFailure"] = "selectorTransportTimeout"
            raise MemorySelectionError("selectorTransportTimeout") from None
        except Exception:
            receipt["requestFailure"] = "selectorTransport"
            raise MemorySelectionError("selectorTransport") from None
        finally:
            receipt["requestDone"] = True
            receipt["requestDoneSeconds"] = time.monotonic() - started


    async def cleanup(requestTask):
        """请求真正终止后释放资源，重复调用方取消不能中断此独立任务"""
        try:
            await asyncio.wait({requestTask})
            if not requestTask.cancelled():
                requestTask.exception()
            if owned.stream is not None:
                await _closeResource(owned.stream, "response", receipt)
            try:
                if client is not None:
                    await _closeResource(client, "client", receipt)
            finally:
                await _closeResource(owned.inner, "transport", receipt)
        except asyncio.CancelledError:
            receipt["cleanupCancelled"] = True
            raise
        except Exception as error:
            receipt["cleanupError"] = type(error).__name__
        finally:
            receipt["cleanupDone"] = True
            receipt["cleanupDoneSeconds"] = time.monotonic() - started

    # 两个任务在首次await之前登记，调用方退出后runtime和lease继续拥有它们
    requestTask = asyncio.create_task(sendRequest(), name="memory-selector-request")
    cleanupTask = asyncio.create_task(cleanup(requestTask), name="memory-selector-cleanup")
    lease.deferUntil(requestTask)
    lease.deferUntil(cleanupTask)
    owner.trackSelectorTask(lease, requestTask, receipt=receipt)
    owner.trackSelectorTask(lease, cleanupTask, cleanup=True, receipt=receipt)


    def cancelRequestOnce():
        """不向正在取消或已终止的请求重复发送取消"""
        if not requestTask.done() and not requestTask.cancelling():
            receipt["cancelRequests"] += 1
            requestTask.cancel()

    try:
        done, _ = await asyncio.wait({requestTask}, timeout=max(0, requestDeadline - time.monotonic()))
        if not done or receipt["requestDoneSeconds"] > timeoutSeconds - reserve:
            receipt["requestDeadlineExpired"] = True
            cancelRequestOnce()
        done, _ = await asyncio.wait({cleanupTask}, timeout=max(0, deadline - time.monotonic()))
        clean = bool(done and receipt["cleanupDoneSeconds"] <= timeoutSeconds and not cleanupTask.cancelled()
                     and receipt.get("transportClosed")
                     and (not receipt["responseReceived"] or receipt["responseClosed"])
                     and not any(receipt.get(key) for key in (
                         "responseCloseError", "clientCloseError", "transportCloseError", "cleanupError")))
        receipt["cleanupVerifiedAtDecision"] = clean
        failure = receipt["requestFailure"]
        if failure is None and receipt["requestDeadlineExpired"]:
            failure = "selectorTimeout"
        if failure is None and not clean:
            failure = "selectorCleanupFailed" if done else "selectorCleanupPending"
        if failure:
            receipt["decision"] = failure
            raise MemorySelectionError(failure)
        receipt["decision"] = "success"
        return requestTask.result()
    except asyncio.CancelledError:
        receipt.update(externalCancelled=True, decision="externalCancelled")
        cancelRequestOnce()
        raise
    finally:
        receipt["callerSeconds"] = time.monotonic() - started
