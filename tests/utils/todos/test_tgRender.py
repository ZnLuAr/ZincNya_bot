"""
tests/utils/todos/test_tgRender.py

测试 utils/todos/tgRender.py
"""

import pytest
from datetime import datetime, timedelta
from utils.todos.tgRender import (
    preview,
    renderListView,
    renderDetailView,
)


# ============================================================================
# preview() 测试
# ============================================================================

def test_preview_short_content():
    """短内容不截断"""
    content = "买牛奶"
    result = preview(content)
    assert result == "买牛奶"


def test_preview_long_content():
    """长内容截断并添加省略号"""
    # TODOS_CONTENT_PREVIEW_LENGTH = 15
    content = "这是一个非常非常非常非常非常非常非常长的待办事项内容"
    result = preview(content)
    assert len(result) <= 16  # 15 + "…"
    assert result.endswith("…")


def test_preview_exact_length():
    """恰好等于限制长度"""
    content = "a" * 15
    result = preview(content)
    assert result == content


# ============================================================================
# renderListView() 测试
# ============================================================================

def test_render_list_view_empty():
    """空列表"""
    text, keyboard = renderListView(
        todos=[],
        total=0,
        page=1,
        userName="test_user"
    )

    assert "test_user" in text
    assert "还没有交代给咱要提醒的事情" in text
    assert "/todos help" in text

    # 键盘只有关闭按钮
    assert len(keyboard.inline_keyboard) == 1
    assert keyboard.inline_keyboard[0][0].text == "✖ 关闭"


def test_render_list_view_single_page():
    """单页列表"""
    todos = [
        {
            'id': 1,
            'content': '买牛奶',
            'priority': 'P0',
            'remind_time': None,
        },
        {
            'id': 2,
            'content': '开会',
            'priority': 'P1',
            'remind_time': datetime.now() + timedelta(hours=2),
        },
    ]

    text, keyboard = renderListView(
        todos=todos,
        total=2,
        page=1,
        userName="test_user"
    )

    assert "test_user" in text
    assert "买牛奶" in text
    assert "开会" in text
    assert "P0" in text
    assert "P1" in text
    assert "第 1/1 页" in text
    assert "共 2 条" in text

    # 键盘：数字按钮 + 关闭按钮
    assert len(keyboard.inline_keyboard) >= 2


def test_render_list_view_pagination():
    """多页列表"""
    # TODOS_ITEMS_PER_PAGE = 6，总共 25 条 = 5 页
    todos = [
        {'id': i, 'content': f'Todo {i}', 'priority': 'P_', 'remind_time': None}
        for i in range(1, 7)  # 第一页 6 条
    ]

    # 第 1 页
    text, keyboard = renderListView(
        todos=todos,
        total=25,
        page=1,
        userName="test_user"
    )

    assert "第 1/5 页" in text
    assert "共 25 条" in text

    # 应该有"下一页"按钮
    buttons_text = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert any("下一页" in btn for btn in buttons_text)

    # 第 2 页
    text, keyboard = renderListView(
        todos=todos,
        total=25,
        page=2,
        userName="test_user"
    )

    assert "第 2/5 页" in text

    # 应该有"上一页"和"下一页"按钮
    buttons_text = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert any("上一页" in btn for btn in buttons_text)
    assert any("下一页" in btn for btn in buttons_text)


def test_render_list_view_overdue_italic():
    """过期待办显示为斜体"""
    past_time = datetime.now() - timedelta(hours=1)

    todos = [
        {
            'id': 1,
            'content': '过期任务',
            'priority': 'P0',
            'remind_time': past_time,
        },
    ]

    text, keyboard = renderListView(
        todos=todos,
        total=1,
        page=1,
        userName="test_user"
    )

    # 应该包含斜体标记
    assert "<i>" in text
    assert "</i>" in text


def test_render_list_view_priority_emoji():
    """优先级 emoji 显示"""
    todos = [
        {'id': 1, 'content': 'P0 任务', 'priority': 'P0', 'remind_time': None},
        {'id': 2, 'content': 'P1 任务', 'priority': 'P1', 'remind_time': None},
        {'id': 3, 'content': 'P2 任务', 'priority': 'P2', 'remind_time': None},
        {'id': 4, 'content': 'P3 任务', 'priority': 'P3', 'remind_time': None},
        {'id': 5, 'content': 'P_ 任务', 'priority': 'P_', 'remind_time': None},
    ]

    text, keyboard = renderListView(
        todos=todos,
        total=5,
        page=1,
        userName="test_user"
    )

    # 应该包含 emoji（🔴🟠🟡🟢⚪）
    assert "🔴" in text or "🟠" in text or "🟡" in text or "🟢" in text or "⚪" in text


# ============================================================================
# renderDetailView() 测试
# ============================================================================

def test_render_detail_view_basic():
    """基本详情视图"""
    todo = {
        'id': 1,
        'content': '买牛奶',
        'priority': 'P1',
        'remind_time': None,
        'status': 'pending',
    }

    text, keyboard = renderDetailView(todo)

    assert "买牛奶" in text
    assert "P1" in text
    assert "提醒：无" in text

    # 键盘应该包含优先级按钮和操作按钮
    assert len(keyboard.inline_keyboard) >= 2


def test_render_detail_view_with_remind_time():
    """带提醒时间的详情"""
    remind_time = datetime.now() + timedelta(hours=2)

    todo = {
        'id': 1,
        'content': '开会',
        'priority': 'P0',
        'remind_time': remind_time,
        'status': 'pending',
    }

    text, keyboard = renderDetailView(todo)

    assert "开会" in text
    assert "提醒：是" in text


def test_render_detail_view_priority_buttons():
    """优先级按钮（当前优先级高亮）"""
    todo = {
        'id': 1,
        'content': '任务',
        'priority': 'P1',
        'remind_time': None,
        'status': 'pending',
    }

    text, keyboard = renderDetailView(todo)

    # 第一行应该是优先级按钮
    priority_row = keyboard.inline_keyboard[0]
    assert len(priority_row) == 5  # P0, P1, P2, P3, P_

    # P1 应该被高亮（包含 > <）
    p1_button = [btn for btn in priority_row if 'P1' in btn.text][0]
    assert '>' in p1_button.text and '<' in p1_button.text


def test_render_detail_view_pending_status():
    """pending 状态显示"完成"按钮"""
    todo = {
        'id': 1,
        'content': '任务',
        'priority': 'P_',
        'remind_time': None,
        'status': 'pending',
    }

    text, keyboard = renderDetailView(todo)

    # 操作按钮行
    action_row = keyboard.inline_keyboard[1]
    buttons_text = [btn.text for btn in action_row]

    assert any("完成" in btn for btn in buttons_text)


def test_render_detail_view_done_status():
    """done 状态显示"重新打开"按钮"""
    todo = {
        'id': 1,
        'content': '已完成任务',
        'priority': 'P_',
        'remind_time': None,
        'status': 'done',
    }

    text, keyboard = renderDetailView(todo)

    # 操作按钮行
    action_row = keyboard.inline_keyboard[1]
    buttons_text = [btn.text for btn in action_row]

    assert any("重新打开" in btn for btn in buttons_text)


def test_render_detail_view_action_buttons():
    """操作按钮（完成/删除/返回）"""
    todo = {
        'id': 1,
        'content': '任务',
        'priority': 'P_',
        'remind_time': None,
        'status': 'pending',
    }

    text, keyboard = renderDetailView(todo)

    # 操作按钮行
    action_row = keyboard.inline_keyboard[1]
    buttons_text = [btn.text for btn in action_row]

    assert len(action_row) == 3
    assert any("删除" in btn for btn in buttons_text)
    assert any("返回" in btn for btn in buttons_text)