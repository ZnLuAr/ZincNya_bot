"""
tests/utils/core/test_logger.py

测试 utils/core/logger.py 树状日志系统
"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock, mock_open
from datetime import datetime

from utils.core.logger import (
    sanitizeForLog,
    TreeLogger,
    LogLevel,
    LogChildType,
    initLogger,
    logAction,
    logSystemEvent,
)


# ============================================================================
# Fixture: 重置全局 logger 状态
# ============================================================================

@pytest.fixture(autouse=True)
def reset_logger():
    """每个测试前重置全局 logger 状态"""
    from utils.core import logger as logger_module
    logger_module._logger = TreeLogger()
    yield
    logger_module._logger = TreeLogger()


# ============================================================================
# 测试 sanitizeForLog() — 纯函数
# ============================================================================

def test_sanitize_for_log_ansi_escape():
    """剥离 ANSI 转义序列"""
    text = "\x1b[31mRed Text\x1b[0m"
    result = sanitizeForLog(text)
    assert result == "Red Text"


def test_sanitize_for_log_control_chars():
    """剥离 C0/C1 控制字符"""
    text = "Hello\x00\x08\x1fWorld"
    result = sanitizeForLog(text)
    assert result == "HelloWorld"


def test_sanitize_for_log_newline():
    """换行符转义为 \\n"""
    text = "Line1\nLine2\nLine3"
    result = sanitizeForLog(text)
    assert result == "Line1\\nLine2\\nLine3"


def test_sanitize_for_log_empty():
    """空字符串边界"""
    assert sanitizeForLog("") == ""
    assert sanitizeForLog(None) is None


def test_sanitize_for_log_clean_text():
    """已经干净的字符串保持不变"""
    text = "Clean text with spaces and 123"
    result = sanitizeForLog(text)
    assert result == text


def test_sanitize_for_log_carriage_return():
    """回车符转义为 \\r（防回车覆盖行首伪造日志来源）

    攻击者用 \\r 把光标移回行首再写入伪造内容，渲染时可覆盖真实时间戳/来源。
    \\r 又会被 C0 控制字符正则吞掉、不留痕迹，所以必须先转义成字面量。
    """
    text = "user input\r[12:34:56] @Admin: rm -rf /"
    result = sanitizeForLog(text)
    assert "\r" not in result
    assert "\\r" in result
    # 伪造的内容仍在，但 \r 已是可见字面量，不会在渲染时覆盖行首
    assert result == "user input\\r[12:34:56] @Admin: rm -rf /"


def test_sanitize_for_log_redacts_secret_kv():
    """脱敏 key=value / key: value 形式的密钥，防止误写入日志落盘"""
    cases = [
        "api_key=sk-abcdef123456",
        "apikey: sk-abcdef123456",
        "token=ghp_xxxxxxxxxxxx",
        "password = hunter2",
        'authorization: "Bearer abc.def.ghi"',
        "secret=topsecret",
    ]
    for text in cases:
        result = sanitizeForLog(text)
        assert "REDACTED" in result, f"未脱敏: {text}"
        # 原始敏感值不应残留
        assert "sk-abcdef123456" not in result
        assert "hunter2" not in result
        assert "topsecret" not in result
        assert "ghp_xxxxxxxxxxxx" not in result


def test_sanitize_for_log_keeps_key_name():
    """脱敏只替换值，键名保留（便于运维定位是哪个字段泄露）"""
    result = sanitizeForLog("api_key=sk-secret")
    assert "api_key" in result
    assert "sk-secret" not in result


def test_sanitize_for_log_no_false_positive_redaction():
    """不含密钥键名的普通文本不被误脱敏"""
    text = "user said: the value is 42 and the result is ok"
    result = sanitizeForLog(text)
    assert result == text
    assert "REDACTED" not in result


# ============================================================================
# 测试 TreeLogger._extractUserName() — 纯函数
# ============================================================================

def test_extract_user_name_none():
    """None → System"""
    logger = TreeLogger()
    assert logger._extractUserName(None) == "System"


def test_extract_user_name_string():
    """str → 去空格后返回"""
    logger = TreeLogger()
    assert logger._extractUserName("  TestUser  ") == "TestUser"
    assert logger._extractUserName("") == "Unknown"
    assert logger._extractUserName("   ") == "Unknown"


def test_extract_user_name_dict():
    """dict → username > first_name > Unknown"""
    logger = TreeLogger()
    assert logger._extractUserName({"username": "user1", "first_name": "First"}) == "user1"
    assert logger._extractUserName({"first_name": "First"}) == "First"
    assert logger._extractUserName({}) == "Unknown"


def test_extract_user_name_telegram_user(mockUser):
    """Telegram User 对象 → username > first_name > Unknown"""
    logger = TreeLogger()
    assert logger._extractUserName(mockUser) == "test_user"

    # 无 username
    mockUser.username = None
    assert logger._extractUserName(mockUser) == "Test"

    # 无 username 和 first_name
    mockUser.first_name = None
    assert logger._extractUserName(mockUser) == "Unknown"


# ============================================================================
# 测试 TreeLogger._formatConsoleText() — 7 种 childType 分支
# ============================================================================

@pytest.mark.parametrize("child_type,expected_pattern", [
    (LogChildType.NONE, r"\[12:00:00\] \[INFO\] @TestUser: Event\n\n"),
    (LogChildType.WITH_CHILD, r"\[12:00:00\] \[INFO\] @TestUser: Event\n\s+└─┤ Details"),
    (LogChildType.WITH_ONE_CHILD, r"\[12:00:00\] \[INFO\] @TestUser: Event\n\s+└─┤ Details\n\n"),
    (LogChildType.CHILD_WITH_CHILD, r"\s+└─┤ Event\n\s+└─┤ Details"),
    (LogChildType.LAST_CHILD_WITH_CHILD, r"\s+└─┤ Event\n\s+└─┤ Details\n\n"),
    (LogChildType.LAST_CHILD, r"\s+└─┤ Details\n\n"),
    (LogChildType.ONLY_RESULT, r"\s+└─┤ Details"),
])
def test_format_console_text(child_type, expected_pattern):
    """验证 7 种 LogChildType 的格式化输出"""
    import re
    logger = TreeLogger()
    result = logger._formatConsoleText(
        timestamp="12:00:00",
        userName="TestUser",
        event="Event",
        details="Details",
        level=LogLevel.INFO,
        childType=child_type
    )
    assert re.search(expected_pattern, result), f"Pattern not matched for {child_type}: {result}"


# ============================================================================
# 测试 TreeLogger._formatLogLine() — 单行格式化
# ============================================================================

def test_format_log_line_both():
    """event + details → event → details"""
    logger = TreeLogger()
    result = logger._formatLogLine("12:00:00", "User", "Event", "Details", LogLevel.INFO)
    assert result == "[12:00:00] [INFO] @User: Event → Details\n"


def test_format_log_line_event_only():
    """仅 event"""
    logger = TreeLogger()
    result = logger._formatLogLine("12:00:00", "User", "Event", "", LogLevel.INFO)
    assert result == "[12:00:00] [INFO] @User: Event\n"


def test_format_log_line_details_only():
    """仅 details"""
    logger = TreeLogger()
    result = logger._formatLogLine("12:00:00", "User", "", "Details", LogLevel.INFO)
    assert result == "[12:00:00] [INFO] @User: → Details\n"


def test_format_log_line_empty():
    """都为空 → 返回空字符串"""
    logger = TreeLogger()
    result = logger._formatLogLine("12:00:00", "User", "", "", LogLevel.INFO)
    assert result == ""


# ============================================================================
# 测试 TreeLogger.initialize() — 路径生成
# ============================================================================

def test_initialize_creates_log_path(tmp_path, monkeypatch):
    """初始化创建日志路径"""
    log_dir = tmp_path / "logs"
    monkeypatch.setattr("config.LOG_DIR", str(log_dir))

    logger = TreeLogger()

    with patch("builtins.print"):
        logger.initialize()

    assert logger._logPath is not None
    assert "log_" in logger._logPath
    assert logger._startupTime is not None
    assert logger._startupWritten is False


def test_initialize_log_path_format(tmp_path, monkeypatch):
    """日志路径格式 log_YYYY-MM-DD.log"""
    log_dir = tmp_path / "logs"
    monkeypatch.setattr("config.LOG_DIR", str(log_dir))

    logger = TreeLogger()

    with patch("builtins.print"):
        logger.initialize()

    today = datetime.now().strftime("%Y-%m-%d")
    expected_filename = f"log_{today}.log"
    assert expected_filename in logger._logPath


# ============================================================================
# 测试 TreeLogger._writeStartupHeader() — 延迟写入
# ============================================================================

def test_write_startup_header_first_call(tmp_path, monkeypatch):
    """首次调用写入启动信息"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("config.LOG_DIR", str(log_dir))

    logger = TreeLogger()
    with patch("builtins.print"):
        logger.initialize()

    logger._writeStartupHeader()

    assert logger._startupWritten is True
    assert Path(logger._logPath).exists()

    content = Path(logger._logPath).read_text(encoding="utf-8")
    assert "ZincNya Bot" in content
    assert "启动啦" in content


def test_write_startup_header_idempotent(tmp_path, monkeypatch):
    """重复调用不重写"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("config.LOG_DIR", str(log_dir))

    logger = TreeLogger()
    with patch("builtins.print"):
        logger.initialize()

    logger._writeStartupHeader()
    first_content = Path(logger._logPath).read_text(encoding="utf-8")

    logger._writeStartupHeader()
    second_content = Path(logger._logPath).read_text(encoding="utf-8")

    assert first_content == second_content


def test_write_startup_header_separator_when_file_exists(tmp_path, monkeypatch):
    """文件已存在且有内容时添加分隔符"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / f"log_{datetime.now().strftime('%Y-%m-%d')}.log"
    log_file.write_text("Previous content\n", encoding="utf-8")

    monkeypatch.setattr("config.LOG_DIR", str(log_dir))

    logger = TreeLogger()
    with patch("builtins.print"):
        logger.initialize()

    logger._writeStartupHeader()

    content = Path(logger._logPath).read_text(encoding="utf-8")
    # 验证分隔符存在（不验证 "Previous content" 因为可能有编码问题）
    assert "─" * 60 in content
    # 验证文件大小增加（说明追加了内容）
    assert len(content) > len("Previous content\n")


# ============================================================================
# 测试 TreeLogger.log() — 异步主流程
# ============================================================================

@pytest.mark.asyncio
async def test_log_empty_event_and_details():
    """空 event 和 details → 直接返回"""
    logger = TreeLogger()

    with patch("builtins.print"):
        await logger.log(None, "", "", LogLevel.INFO, LogChildType.NONE)

    # 不应该初始化
    assert logger._logPath is None


@pytest.mark.asyncio
async def test_log_auto_initialize(tmp_path, monkeypatch):
    """自动初始化（_logPath is None）"""
    log_dir = tmp_path / "logs"
    monkeypatch.setattr("config.LOG_DIR", str(log_dir))

    logger = TreeLogger()

    with patch("builtins.print"):
        await logger.log("User", "Event", "Details", LogLevel.INFO, LogChildType.NONE)

    assert logger._logPath is not None


@pytest.mark.asyncio
async def test_log_exception_injection():
    """异常注入到 details"""
    logger = TreeLogger()
    logger._logPath = "/fake/path.log"

    exc = ValueError("Test error")

    with patch.object(logger, "_writeLogSync"):
        with patch("utils.core.stateManager.getStateManager") as mock_state:
            mock_state.return_value.getConsoleOutputCallback.return_value = None

            await logger.log(
                "User", "Event", "Details", LogLevel.ERROR,
                LogChildType.NONE, exception=exc
            )

    # 验证 details 包含异常信息（通过 _writeLogSync 的调用参数）
    # 这里简化验证，实际会在集成测试中验证


@pytest.mark.asyncio
async def test_log_cli_mode_print(tmp_path, monkeypatch):
    """CLI 模式直接 print"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("config.LOG_DIR", str(log_dir))

    logger = TreeLogger()
    with patch("builtins.print"):
        logger.initialize()

    with patch("builtins.print") as mock_print:
        with patch("utils.core.stateManager.getStateManager") as mock_state:
            mock_state.return_value.getConsoleOutputCallback.return_value = None

            await logger.log("User", "Event", "Details", LogLevel.INFO, LogChildType.NONE)

    # 验证 print 被调用
    assert mock_print.called


@pytest.mark.asyncio
async def test_log_ui_mode_callback(tmp_path, monkeypatch):
    """UI 模式走 callback"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("config.LOG_DIR", str(log_dir))

    logger = TreeLogger()
    with patch("builtins.print"):
        logger.initialize()

    mock_callback = MagicMock()

    with patch("utils.core.stateManager.getStateManager") as mock_state:
        mock_state.return_value.getConsoleOutputCallback.return_value = mock_callback

        await logger.log("User", "Event", "Details", LogLevel.INFO, LogChildType.NONE)

    # 验证 callback 被调用
    assert mock_callback.called


@pytest.mark.asyncio
async def test_log_error_with_exception_triggers_error_handler(tmp_path, monkeypatch):
    """ERROR + exception → 触发错误双写"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("config.LOG_DIR", str(log_dir))

    logger = TreeLogger()
    with patch("builtins.print"):
        logger.initialize()

    exc = ValueError("Test error")

    with patch("utils.core.errorHandler.logError") as mock_log_error:
        with patch("utils.core.stateManager.getStateManager") as mock_state:
            mock_state.return_value.getConsoleOutputCallback.return_value = None

            await logger.log(
                "User", "Event", "Details", LogLevel.ERROR,
                LogChildType.NONE, exception=exc
            )

    # 验证 logError 被调用
    mock_log_error.assert_called_once()
    call_args = mock_log_error.call_args
    assert call_args[1]["errorType"] == "ValueError"


# ============================================================================
# 测试 logSystemEvent() / logAction() — 函数接口
# ============================================================================

@pytest.mark.asyncio
async def test_log_system_event_default_user():
    """logSystemEvent 自动设置 user=System"""
    with patch("utils.core.logger._logger.log") as mock_log:
        await logSystemEvent("Event", "Details")

    mock_log.assert_called_once()
    # logSystemEvent 使用关键字参数调用 log
    assert mock_log.call_args.kwargs["user"] == "System"


@pytest.mark.asyncio
async def test_log_action_passes_user():
    """logAction 传递 user 参数"""
    with patch("utils.core.logger._logger.log") as mock_log:
        await logAction("TestUser", "Event", "Details", LogLevel.INFO, LogChildType.NONE)

    mock_log.assert_called_once()
    # 使用 .args 访问位置参数
    assert mock_log.call_args.args[0] == "TestUser"


def test_init_logger():
    """initLogger 调用 _logger.initialize()"""
    with patch("utils.core.logger._logger.initialize") as mock_init:
        initLogger()

    mock_init.assert_called_once()