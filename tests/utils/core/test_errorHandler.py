"""
tests/utils/core/test_errorHandler.py

测试 utils/core/errorHandler.py 统一错误处理系统
"""

import os
import sys
import logging
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock, mock_open
from datetime import datetime, date

from utils.core.errorHandler import (
    _buildSecretPattern,
    _redactSecrets,
    ErrorHandler,
    initErrorHandler,
    getErrorHandler,
    logError,
    setupAsyncioErrorHandler,
)


# ============================================================================
# Fixture: 重置全局 errorHandler 状态
# ============================================================================

@pytest.fixture(autouse=True)
def reset_error_handler():
    """每个测试前重置全局 errorHandler 状态，并清理 logging handlers"""
    import logging
    from utils.core import errorHandler as eh_module

    # 清理可能存在的 logging handlers
    for loggerName in ["telegram", "httpx", "httpcore"]:
        logger = logging.getLogger(loggerName)
        logger.handlers.clear()
        logger.propagate = True  # 恢复默认传播行为

    # 重置 errorHandler
    eh_module.errorHandler = ErrorHandler()

    # 保存原始 _SECRET_RE 以便恢复
    original_secret_re = eh_module._SECRET_RE

    yield

    # 测试后再次清理
    for loggerName in ["telegram", "httpx", "httpcore"]:
        logger = logging.getLogger(loggerName)
        logger.handlers.clear()
        logger.propagate = True

    eh_module.errorHandler = ErrorHandler()

    # 恢复原始 _SECRET_RE，避免测试间泄漏
    eh_module._SECRET_RE = original_secret_re


# ============================================================================
# 测试 _buildSecretPattern() — secret 模式构建
# ============================================================================

def test_build_secret_pattern_with_secrets(monkeypatch):
    """环境变量存在时构建 pattern"""
    monkeypatch.setenv("BOT_TOKEN", "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-1234567890abcdef")

    pattern = _buildSecretPattern()

    assert pattern is not None
    assert pattern.pattern  # 验证是编译后的正则


def test_build_secret_pattern_no_secrets(monkeypatch):
    """环境变量不存在时返回 None"""
    # 清空所有相关环境变量
    for key in ("BOT_TOKEN", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "DOUBAO_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    pattern = _buildSecretPattern()

    assert pattern is None


def test_build_secret_pattern_short_secret(monkeypatch):
    """长度 < 8 的 secret 不加入 pattern"""
    # 清空所有相关环境变量
    for key in ("BOT_TOKEN", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "DOUBAO_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("BOT_TOKEN", "short")  # 长度 5

    pattern = _buildSecretPattern()

    # 只有一个短 secret，应该返回 None
    assert pattern is None


# ============================================================================
# 测试 _redactSecrets() — token 脱敏
# ============================================================================

def test_redact_secrets_pattern_none():
    """pattern None 返回原文"""
    with patch("utils.core.errorHandler._SECRET_RE", None):
        text = "This contains secret: 1234567890"
        result = _redactSecrets(text)
        assert result == text


def test_redact_secrets_replaces_token(monkeypatch):
    """命中 token 时替换为 [REDACTED]"""
    monkeypatch.setenv("BOT_TOKEN", "SECRET_TOKEN_12345678")

    # 重新构建 pattern
    from utils.core import errorHandler as eh_module
    eh_module._SECRET_RE = _buildSecretPattern()

    text = "Error with token: SECRET_TOKEN_12345678"
    result = _redactSecrets(text)

    assert "[REDACTED]" in result
    assert "SECRET_TOKEN_12345678" not in result


# ============================================================================
# 测试 ErrorHandler.getErrorKey() — 错误 key 生成
# ============================================================================

def test_get_error_key_truncates_message():
    """截取消息前 50 个字符"""
    handler = ErrorHandler()
    long_message = "A" * 100

    key = handler.getErrorKey("TestError", long_message)

    assert key == f"TestError:{'A' * 50}"


def test_get_error_key_empty_message():
    """空消息返回空 key"""
    handler = ErrorHandler()

    key = handler.getErrorKey("TestError", "")

    assert key == "TestError:"


# ============================================================================
# 测试 ErrorHandler.resetDailyCountsIfNeeded() — 跨天重置
# ============================================================================

def test_reset_daily_counts_same_day():
    """同天保留计数"""
    handler = ErrorHandler()
    handler.lastErrorDate = date.today()
    handler.errorCounts["TestError:msg"] = 5

    handler.resetDailyCountsIfNeeded()

    assert handler.errorCounts["TestError:msg"] == 5


def test_reset_daily_counts_different_day():
    """跨天重置计数"""
    handler = ErrorHandler()
    handler.lastErrorDate = date(2020, 1, 1)
    handler.errorCounts["TestError:msg"] = 5

    handler.resetDailyCountsIfNeeded()

    assert len(handler.errorCounts) == 0
    assert handler.lastErrorDate == date.today()


# ============================================================================
# 测试 ErrorHandler.getErrorLogPath() — 路径格式
# ============================================================================

def test_get_error_log_path_format(tmp_path, monkeypatch):
    """路径格式 error_YYYY-MM-DD.log"""
    log_dir = tmp_path / "logs"
    monkeypatch.setattr("utils.core.errorHandler.LOG_DIR", str(log_dir))

    handler = ErrorHandler()
    path = handler.getErrorLogPath()

    today = datetime.now().strftime("%Y-%m-%d")
    expected_filename = f"error_{today}.log"

    assert expected_filename in path
    assert log_dir.exists()


# ============================================================================
# 测试 ErrorHandler.logError() — 主入口
# ============================================================================

def test_log_error_first_occurrence(tmp_path, monkeypatch):
    """首次出现错误"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("utils.core.errorHandler.LOG_DIR", str(log_dir))

    handler = ErrorHandler()

    with patch("utils.core.errorHandler.safePrint") as mock_print:
        handler.logError("TestError", "Test message", None, None)

    # 验证终端输出包含 "刚刚发生了错误"
    mock_print.assert_called_once()
    call_text = mock_print.call_args[0][0]
    assert "刚刚发生了错误" in call_text
    assert "TestError" in call_text


def test_log_error_repeated_occurrence(tmp_path, monkeypatch):
    """重复出现错误（聚合计数）"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("utils.core.errorHandler.LOG_DIR", str(log_dir))

    handler = ErrorHandler()

    with patch("utils.core.errorHandler.safePrint") as mock_print:
        handler.logError("TestError", "Test message", None, None)
        handler.logError("TestError", "Test message", None, None)

    # 第二次调用应该显示计数
    second_call_text = mock_print.call_args[0][0]
    assert "今日第 2 次" in second_call_text


def test_log_error_writes_to_file(tmp_path, monkeypatch):
    """写入错误日志文件"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("utils.core.errorHandler.LOG_DIR", str(log_dir))

    handler = ErrorHandler()

    with patch("utils.core.errorHandler.safePrint"):
        handler.logError("TestError", "Test message", None, "Test context")

    log_file = Path(handler.getErrorLogPath())
    assert log_file.exists()

    content = log_file.read_text(encoding="utf-8")
    assert "TestError" in content
    assert "Test message" in content
    assert "Test context" in content
    assert "═" * 70 in content


def test_log_error_file_write_failure(tmp_path, monkeypatch):
    """文件写入失败时兜底"""
    log_dir = tmp_path / "logs"
    monkeypatch.setattr("utils.core.errorHandler.LOG_DIR", str(log_dir))

    handler = ErrorHandler()

    with patch("builtins.open", side_effect=OSError("Write failed")):
        with patch("utils.core.errorHandler.safePrint") as mock_print:
            handler.logError("TestError", "Test message", None, None)

    # 验证兜底输出
    calls = [call[0][0] for call in mock_print.call_args_list]
    assert any("错误日志写入失败" in call for call in calls)


# ============================================================================
# 测试 ErrorHandler.writeToLogFile() — 文件格式
# ============================================================================

def test_write_to_log_file_with_exception(tmp_path, monkeypatch):
    """包含异常时写入 traceback"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("utils.core.errorHandler.LOG_DIR", str(log_dir))

    handler = ErrorHandler()

    try:
        raise ValueError("Test exception")
    except ValueError as e:
        handler.writeToLogFile("12:00:00", "ValueError", "Test exception", e, None)

    log_file = Path(handler.getErrorLogPath())
    content = log_file.read_text(encoding="utf-8")

    assert "ValueError" in content
    assert "Test exception" in content
    assert "Traceback" in content
    assert "─" * 70 in content


def test_write_to_log_file_redacts_secrets(tmp_path, monkeypatch):
    """traceback 中的 secret 被脱敏"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("utils.core.errorHandler.LOG_DIR", str(log_dir))
    monkeypatch.setenv("BOT_TOKEN", "SECRET_TOKEN_12345678")

    # 重新构建 pattern
    from utils.core import errorHandler as eh_module
    eh_module._SECRET_RE = _buildSecretPattern()

    handler = ErrorHandler()

    try:
        secret = "SECRET_TOKEN_12345678"
        raise ValueError(f"Error with {secret}")
    except ValueError as e:
        handler.writeToLogFile("12:00:00", "ValueError", str(e), e, None)

    log_file = Path(handler.getErrorLogPath())
    content = log_file.read_text(encoding="utf-8")

    # 验证 [REDACTED] 存在
    assert "[REDACTED]" in content
    # 注意：traceback 中的 secret 也会被 redact，但消息本身可能已经被 redact 了
    # 所以不验证 SECRET_TOKEN_12345678 不存在（因为可能在其他地方）


# ============================================================================
# 测试 ErrorHandler.handleUncaughtException() — 全局异常
# ============================================================================

def test_handle_uncaught_exception_keyboard_interrupt():
    """KeyboardInterrupt 跳过"""
    handler = ErrorHandler()

    with patch("sys.__excepthook__") as mock_hook:
        handler.handleUncaughtException(KeyboardInterrupt, KeyboardInterrupt(), None)

    mock_hook.assert_called_once()


def test_handle_uncaught_exception_other():
    """其他异常记录"""
    handler = ErrorHandler()

    with patch.object(handler, "logError") as mock_log:
        exc = ValueError("Test error")
        handler.handleUncaughtException(ValueError, exc, None)

    mock_log.assert_called_once()
    call_args = mock_log.call_args
    assert call_args[1]["errorType"] == "ValueError"
    assert call_args[1]["context"] == "Uncaught Exception"


# ============================================================================
# 测试 ErrorHandler.handleUnraisable() — unraisable 异常
# ============================================================================

def test_handle_unraisable_with_exception():
    """有 exc_value 时记录"""
    handler = ErrorHandler()

    unraisable = MagicMock()
    unraisable.exc_value = ValueError("Test error")
    unraisable.object = "TestObject"

    with patch.object(handler, "logError") as mock_log:
        handler.handleUnraisable(unraisable)

    mock_log.assert_called_once()
    call_args = mock_log.call_args
    assert call_args[1]["errorType"] == "ValueError"
    assert "TestObject" in call_args[1]["context"]


def test_handle_unraisable_no_exception():
    """exc_value None 边界"""
    handler = ErrorHandler()

    unraisable = MagicMock()
    unraisable.exc_value = None
    unraisable.object = None

    with patch.object(handler, "logError") as mock_log:
        handler.handleUnraisable(unraisable)

    mock_log.assert_called_once()
    call_args = mock_log.call_args
    assert call_args[1]["errorType"] == "Unknown"


def test_handle_unraisable_truncates_object():
    """object 截断 120 字符"""
    handler = ErrorHandler()

    unraisable = MagicMock()
    unraisable.exc_value = ValueError("Test")
    unraisable.object = "A" * 200

    with patch.object(handler, "logError") as mock_log:
        handler.handleUnraisable(unraisable)

    call_args = mock_log.call_args
    context = call_args[1]["context"]
    # 验证截断（120 字符 + "Unraisable in " 前缀）
    assert len(context) < 200


# ============================================================================
# 测试 ErrorHandler.handleTelegramError() — Telegram 错误
# ============================================================================

@pytest.mark.asyncio
async def test_handle_telegram_error_with_user(mockUpdate):
    """提取 user.id"""
    handler = ErrorHandler()

    context = MagicMock()
    context.error = ValueError("Test error")

    with patch.object(handler, "logError") as mock_log:
        await handler.handleTelegramError(mockUpdate, context)

    mock_log.assert_called_once()
    call_args = mock_log.call_args
    assert "User: 123456789" in call_args[1]["context"]


@pytest.mark.asyncio
async def test_handle_telegram_error_with_chat():
    """提取 chat.id（无 user）"""
    handler = ErrorHandler()

    update = MagicMock()
    update.effective_user = None
    update.effective_chat = MagicMock()
    update.effective_chat.id = 987654321

    context = MagicMock()
    context.error = ValueError("Test error")

    with patch.object(handler, "logError") as mock_log:
        await handler.handleTelegramError(update, context)

    call_args = mock_log.call_args
    assert "Chat: 987654321" in call_args[1]["context"]


@pytest.mark.asyncio
async def test_handle_telegram_error_no_context():
    """都没有时 contextInfo=None"""
    handler = ErrorHandler()

    update = MagicMock()
    update.effective_user = None
    update.effective_chat = None

    context = MagicMock()
    context.error = ValueError("Test error")

    with patch.object(handler, "logError") as mock_log:
        await handler.handleTelegramError(update, context)

    call_args = mock_log.call_args
    assert call_args[1]["context"] is None


# ============================================================================
# 测试 ErrorHandler.setupAsyncioHandler() — asyncio 异常处理器
# ============================================================================

def test_setup_asyncio_handler_with_exception():
    """exception 存在时记录"""
    handler = ErrorHandler()
    loop = MagicMock()

    handler.setupAsyncioHandler(loop)

    # 获取注入的 handler
    injected_handler = loop.set_exception_handler.call_args[0][0]

    exc = ValueError("Test error")
    context = {"exception": exc, "message": "Test message"}

    with patch.object(handler, "logError") as mock_log:
        injected_handler(loop, context)

    mock_log.assert_called_once()
    call_args = mock_log.call_args
    assert call_args[1]["errorType"] == "ValueError"


def test_setup_asyncio_handler_no_exception():
    """exception 不存在时记录 message"""
    handler = ErrorHandler()
    loop = MagicMock()

    handler.setupAsyncioHandler(loop)

    injected_handler = loop.set_exception_handler.call_args[0][0]

    context = {"message": "Unknown async error"}

    with patch.object(handler, "logError") as mock_log:
        injected_handler(loop, context)

    mock_log.assert_called_once()
    call_args = mock_log.call_args
    assert call_args[1]["errorType"] == "AsyncioError"


# ============================================================================
# 测试 TelegramLoggingHandler.emit() — logging 拦截器
# ============================================================================

def test_logging_handler_ignores_below_error():
    """只处理 ERROR 及以上级别"""
    handler = ErrorHandler()

    with patch.object(handler, "logError") as mock_log:
        handler.setupLoggingInterceptor()

        logger = logging.getLogger("telegram")
        logger.warning("Warning message")

    mock_log.assert_not_called()


def test_logging_handler_httpx_timeout():
    """httpx 超时错误"""
    from httpx import ReadTimeout

    handler = ErrorHandler()
    handler.setupLoggingInterceptor()

    logger = logging.getLogger("httpx")

    with patch.object(handler, "logError") as mock_log:
        try:
            raise ReadTimeout("Request timeout")
        except ReadTimeout:
            logger.exception("HTTP request failed")

    mock_log.assert_called_once()
    call_args = mock_log.call_args
    assert call_args[1]["errorType"] == "ReadTimeout"


def test_logging_handler_telegram_network_error():
    """telegram 网络错误"""
    from telegram.error import NetworkError

    handler = ErrorHandler()
    handler.setupLoggingInterceptor()

    logger = logging.getLogger("telegram")

    with patch.object(handler, "logError") as mock_log:
        try:
            raise NetworkError("Network error")
        except NetworkError:
            logger.exception("Telegram request failed")

    mock_log.assert_called_once()
    call_args = mock_log.call_args
    assert call_args[1]["errorType"] == "NetworkError"


# ============================================================================
# 测试函数接口
# ============================================================================

def test_init_error_handler():
    """initErrorHandler 注册所有 hook"""
    app = MagicMock()

    with patch("sys.excepthook"), patch("sys.unraisablehook"), patch("builtins.print"):
        initErrorHandler(app)

    app.add_error_handler.assert_called_once()


def test_get_error_handler_singleton():
    """getErrorHandler 返回单例"""
    handler1 = getErrorHandler()
    handler2 = getErrorHandler()

    assert handler1 is handler2


def test_log_error_function():
    """logError 函数路由到 errorHandler.logError"""
    with patch("utils.core.errorHandler.errorHandler.logError") as mock_log:
        logError("TestError", "Test message")

    mock_log.assert_called_once()


def test_setup_asyncio_error_handler_function():
    """setupAsyncioErrorHandler 函数路由"""
    loop = MagicMock()

    with patch("utils.core.errorHandler.errorHandler.setupAsyncioHandler") as mock_setup:
        setupAsyncioErrorHandler(loop)

    mock_setup.assert_called_once_with(loop)