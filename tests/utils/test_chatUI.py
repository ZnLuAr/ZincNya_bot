"""
tests/utils/test_chatUI.py

测试 utils/chatUI.py
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from utils.chatUI import ChatScreenApp


# ============================================================================
# _formatMessageLines() 测试
# ============================================================================

def test_format_message_lines_lazy_load():
    """懒加载机制（首次调用加载函数）"""
    # 重置类变量
    ChatScreenApp._fmtLinesFn = None

    mock_fn = MagicMock(return_value=["formatted line"])
    mock_module = MagicMock()
    mock_module._formatMessageLines = mock_fn

    with patch.dict('sys.modules', {'utils.command.send': mock_module}):
        result = ChatScreenApp._formatMessageLines("2023-01-01 12:00", "Alice", "Hello")

    assert ChatScreenApp._fmtLinesFn is mock_fn
    mock_fn.assert_called_once_with("2023-01-01 12:00", "Alice", "Hello")
    assert result == ["formatted line"]


def test_format_message_lines_cached():
    """后续调用使用缓存的函数"""
    mock_fn = MagicMock(return_value=["cached line"])
    ChatScreenApp._fmtLinesFn = mock_fn

    result = ChatScreenApp._formatMessageLines("2023-01-01 12:00", "Bob", "Hi")

    mock_fn.assert_called_once_with("2023-01-01 12:00", "Bob", "Hi")
    assert result == ["cached line"]


# ============================================================================
# _getTermWidth() 测试
# ============================================================================

def test_get_term_width_normal():
    """正常获取终端宽度"""
    mock_app = MagicMock()
    mock_size = MagicMock()
    mock_size.columns = 120
    mock_app.output.get_size.return_value = mock_size

    # 创建实例并替换 _app
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._app = mock_app

    assert app._getTermWidth() == 120


def test_get_term_width_exception():
    """异常时返回默认值 80"""
    mock_app = MagicMock()
    mock_app.output.get_size.side_effect = Exception("Terminal error")

    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._app = mock_app

    assert app._getTermWidth() == 80


# ============================================================================
# _defaultStatus() 测试
# ============================================================================

def test_default_status():
    """返回默认状态栏文本"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat_123")

    status = app._defaultStatus()
    assert "Enter 换行" in status
    assert "Ctrl+S" in status
    assert "test_chat_123" in status


# ============================================================================
# _updateStatus() 测试
# ============================================================================

def test_update_status_at_bottom():
    """滚动偏移为 0 时显示默认状态"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._scrollOffset = 0
            app._pendingNewMessages = 5

    app._updateStatus()
    assert "Enter 换行" in app._statusText
    assert "历史浏览" not in app._statusText


def test_update_status_scrolled():
    """滚动偏移 > 0 时显示历史浏览状态"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._scrollOffset = 10
            app._pendingNewMessages = 3

    app._updateStatus()
    assert "历史浏览" in app._statusText
    assert "偏移 10 行" in app._statusText
    assert "有 3 条新消息" in app._statusText


# ============================================================================
# _getWindowHeight() 测试
# ============================================================================

def test_get_window_height_from_render_info():
    """从 render_info 获取高度"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            mock_render_info = MagicMock()
            mock_render_info.window_height = 25
            app._transcriptWindow.render_info = mock_render_info

    assert app._getWindowHeight() == 25


def test_get_window_height_from_output():
    """render_info 为 None 时从 output.get_size() 获取"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._transcriptWindow.render_info = None
            mock_size = MagicMock()
            mock_size.rows = 30
            app._app.output.get_size.return_value = mock_size

    # 30 - 7 = 23
    assert app._getWindowHeight() == 23


def test_get_window_height_exception():
    """异常时返回默认值 20"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._transcriptWindow.render_info = None
            app._app.output.get_size.side_effect = Exception("Terminal error")

    assert app._getWindowHeight() == 20


# ============================================================================
# _clampOffset() 测试
# ============================================================================

def test_clamp_offset_within_range():
    """偏移量在有效范围内"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._allLines = ["line1", "line2", "line3", "line4", "line5"]
            app._scrollOffset = 2

    with patch.object(app, '_getWindowHeight', return_value=3):
        app._clampOffset()

    # max_offset = 5 - 3 = 2
    assert app._scrollOffset == 2


def test_clamp_offset_exceeds_max():
    """偏移量超过最大值时被限制"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._allLines = ["line1", "line2", "line3"]
            app._scrollOffset = 100

    with patch.object(app, '_getWindowHeight', return_value=3):
        app._clampOffset()

    # max_offset = 3 - 3 = 0
    assert app._scrollOffset == 0


def test_clamp_offset_empty_history():
    """空历史时偏移量为 0"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._allLines = []
            app._scrollOffset = 10

    with patch.object(app, '_getWindowHeight', return_value=20):
        app._clampOffset()

    assert app._scrollOffset == 0


# ============================================================================
# _scrollUp() / _scrollDown() 测试
# ============================================================================

def test_scroll_up():
    """向上滚动增加偏移量"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._scrollOffset = 5
            app._allLines = ["line"] * 50

    with patch.object(app, '_getWindowHeight', return_value=20):
        with patch.object(app, '_updateStatus'):
            with patch.object(app, '_refreshTranscript'):
                app._scrollUp(10)

    assert app._scrollOffset == 15


def test_scroll_down():
    """向下滚动减少偏移量（不低于 0）"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._scrollOffset = 5

    with patch.object(app, '_updateStatus'):
        with patch.object(app, '_refreshTranscript'):
            app._scrollDown(10)

    assert app._scrollOffset == 0


def test_scroll_calls_clamp_and_update():
    """滚动后调用 _clampOffset() 和 _updateStatus()"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._scrollOffset = 5
            app._allLines = ["line"] * 50

    with patch.object(app, '_getWindowHeight', return_value=20):
        with patch.object(app, '_clampOffset') as mock_clamp:
            with patch.object(app, '_updateStatus') as mockUpdate:
                with patch.object(app, '_refreshTranscript') as mock_refresh:
                    app._scrollUp(10)

    mock_clamp.assert_called_once()
    mockUpdate.assert_called_once()
    mock_refresh.assert_called_once()


# ============================================================================
# _refreshTranscript() 测试
# ============================================================================

def test_refresh_transcript_at_bottom():
    """滚动偏移为 0 时显示最后 N 行"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._allLines = [f"line{i}" for i in range(10)]
            app._scrollOffset = 0

    with patch.object(app, '_getWindowHeight', return_value=5):
        app._refreshTranscript()

    # 应显示最后 5 行：line5, line6, line7, line8, line9
    buffer_text = app._transcriptArea.buffer.document.text
    assert "line5" in buffer_text
    assert "line9" in buffer_text
    assert "line4" not in buffer_text


def test_refresh_transcript_scrolled():
    """滚动偏移 > 0 时显示历史范围"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._allLines = [f"line{i}" for i in range(20)]
            app._scrollOffset = 5

    with patch.object(app, '_getWindowHeight', return_value=5):
        app._refreshTranscript()

    # end = 20 - 5 = 15, start = 15 - 5 = 10
    # 应显示 line10 ~ line14
    buffer_text = app._transcriptArea.buffer.document.text
    assert "line10" in buffer_text
    assert "line14" in buffer_text
    assert "line15" not in buffer_text


def test_refresh_transcript_pad_empty_lines():
    """不足窗口高度时顶部补空行"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._allLines = ["line0", "line1"]
            app._scrollOffset = 0

    with patch.object(app, '_getWindowHeight', return_value=5):
        app._refreshTranscript()

    # 5 行窗口，只有 2 行内容，顶部补 3 行空行
    buffer_text = app._transcriptArea.buffer.document.text
    lines = buffer_text.split("\n")
    assert len(lines) == 5
    assert lines[0] == ""
    assert lines[1] == ""
    assert lines[2] == ""
    assert lines[3] == "line0"
    assert lines[4] == "line1"


def test_refresh_transcript_cursor_at_end():
    """光标位置设置到底部"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._allLines = ["line0", "line1", "line2"]
            app._scrollOffset = 0

    with patch.object(app, '_getWindowHeight', return_value=3):
        app._refreshTranscript()

    buffer_text = app._transcriptArea.buffer.document.text
    assert app._transcriptArea.buffer.cursor_position == len(buffer_text)


# ============================================================================
# appendLines() 测试
# ============================================================================

def test_append_lines_basic():
    """追加文本到 _allLines"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._allLines = []
            app._scrollOffset = 0

    with patch.object(app, '_updateStatus'):
        with patch.object(app, '_refreshTranscript'):
            app.appendLines(["line1", "line2"])

    assert app._allLines == ["line1", "line2"]


def test_append_lines_split_newlines():
    """文本中的 \\n 被拆分为多行"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._allLines = []
            app._scrollOffset = 0

    with patch.object(app, '_updateStatus'):
        with patch.object(app, '_refreshTranscript'):
            app.appendLines(["line1\nline2", "line3"])

    assert app._allLines == ["line1", "line2", "line3"]


def test_append_lines_at_bottom_resets_pending():
    """滚动偏移为 0 时重置新消息计数"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._allLines = []
            app._scrollOffset = 0
            app._pendingNewMessages = 5

    with patch.object(app, '_updateStatus'):
        with patch.object(app, '_refreshTranscript'):
            app.appendLines(["new line"])

    assert app._pendingNewMessages == 0


def test_append_lines_scrolled_increments_pending():
    """滚动偏移 > 0 时增加新消息计数"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._allLines = []
            app._scrollOffset = 10
            app._pendingNewMessages = 3

    with patch.object(app, '_updateStatus'):
        with patch.object(app, '_refreshTranscript'):
            app.appendLines(["new line"])

    assert app._pendingNewMessages == 4


# ============================================================================
# appendIncomingMessage() / appendSelfMessage() 测试
# ============================================================================

def test_append_incoming_message():
    """追加接收到的消息"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._allLines = []
            app._scrollOffset = 0

    ChatScreenApp._fmtLinesFn = MagicMock(return_value=["[12:00] Alice: Hello"])

    with patch.object(app, '_updateStatus'):
        with patch.object(app, '_refreshTranscript'):
            app.appendIncomingMessage("12:00", "Alice", "Hello")

    ChatScreenApp._fmtLinesFn.assert_called_once_with("12:00", "Alice", "Hello")
    assert "[12:00] Alice: Hello" in app._allLines


def test_append_self_message():
    """追加自己发送的消息"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._allLines = []
            app._scrollOffset = 0

    ChatScreenApp._fmtLinesFn = MagicMock(return_value=["[12:00] Me: Hi"])

    with patch.object(app, '_updateStatus'):
        with patch.object(app, '_refreshTranscript'):
            app.appendSelfMessage("12:00", "Me", "Hi")

    ChatScreenApp._fmtLinesFn.assert_called_once_with("12:00", "Me", "Hi")
    assert "[12:00] Me: Hi" in app._allLines


# ============================================================================
# clearComposer() 测试
# ============================================================================

def test_clear_composer():
    """清空 _composerArea.text"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._composerArea.text = "some text"

    app.clearComposer()
    assert app._composerArea.text == ""


# ============================================================================
# showStatus() 测试
# ============================================================================

def test_show_status():
    """更新 _statusText 并调用 _app.invalidate()"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")

    app.showStatus("New status text")
    assert app._statusText == "New status text"
    app._app.invalidate.assert_called()


# ============================================================================
# requestExit() 测试
# ============================================================================

def test_request_exit_success():
    """设置 _exitRequested = True 并调用 _app.exit()"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._exitRequested = False

    app.requestExit()
    assert app._exitRequested is True
    app._app.exit.assert_called_once_with(result=None)


def test_request_exit_with_exception():
    """异常时不抛出"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._app.exit.side_effect = Exception("Already exited")

    # 不应抛出异常
    app.requestExit()
    assert app._exitRequested is True


# ============================================================================
# resetExitFlag() 测试
# ============================================================================

def test_reset_exit_flag():
    """重置 _exitRequested = False"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat")
            app._exitRequested = True

    app.resetExitFlag()
    assert app._exitRequested is False


# ============================================================================
# 初始化测试
# ============================================================================

def test_init_with_initial_lines():
    """初始化时加载 initialLines"""
    with patch('utils.chatUI.Application'):
        with patch('utils.chatUI.sys.stdout'):
            app = ChatScreenApp("test_chat", initialLines=["line1\nline2", "line3"])

    # initialLines 中的 \n 应被拆分
    assert "line1" in app._allLines
    assert "line2" in app._allLines
    assert "line3" in app._allLines