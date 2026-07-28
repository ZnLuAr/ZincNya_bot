"""
tests/utils/llm/memory/test_ui.py

测试 utils/llm/memory/ui.py：
    - editMemoryViaEditor: 编辑器 + 头部元数据解析（分支覆盖）
    - MemoryTUIController.collectViewModel: 排序 / preview 截断 / (+) 行 / selected 钳制
    - MemoryTUIController.buildTable: 渲染钉扎
"""

import re

import pytest
from unittest.mock import patch, AsyncMock

from utils.llm.memory.ui import editMemoryViaEditor, MemoryTUIController




# ============================================================================
# 辅助：patch editFile 为"写入给定内容后返回 True"，以测真实解析逻辑
# ============================================================================

def _editFileWriting(content):
    """返回一个 async editFile 替身：把 content 写到临时文件、返回 True（模拟用户保存）。"""
    async def _impl(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    return _impl


def _editFileReturningFalse():
    """返回一个 async editFile 替身：模拟用户取消（返回 False）。"""
    async def _impl(path):
        return False
    return _impl


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]|\x1b[=>]")


def _stripAnsi(text):
    return _ANSI_ESCAPE.sub("", text)




# ============================================================================
# editMemoryViaEditor() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_edit_memory_multi_tags_and_priority():
    """# tags: 多标签 + # priority: 正常解析"""
    content = "# tags: a, b, c\n# priority: 5\n---\nbody text"
    with patch('utils.llm.memory.ui.editFile', new=_editFileWriting(content)):
        result = await editMemoryViaEditor()

    assert result is not None
    body, tags, priority = result
    assert body == "body text"
    assert tags == ["a", "b", "c"]
    assert priority == 5


@pytest.mark.asyncio
async def test_edit_memory_illegal_priority_keeps_old():
    """# priority: 非法值 → 保留旧值"""
    content = "# priority: abc\n---\nbody"
    with patch('utils.llm.memory.ui.editFile', new=_editFileWriting(content)):
        result = await editMemoryViaEditor(initialPriority=3)

    assert result is not None
    body, tags, priority = result
    assert body == "body"
    assert priority == 3  # 非法值，保留传入的旧值


@pytest.mark.asyncio
async def test_edit_memory_no_tags_line():
    """无 # tags 行 → tags 为空"""
    content = "# priority: 5\n---\nbody"
    with patch('utils.llm.memory.ui.editFile', new=_editFileWriting(content)):
        result = await editMemoryViaEditor()

    assert result is not None
    body, tags, priority = result
    assert body == "body"
    assert tags == []
    assert priority == 5


@pytest.mark.asyncio
async def test_edit_memory_no_priority_line():
    """无 # priority 行 → priority 为默认（初始）值"""
    content = "# tags: x\n---\nbody"
    with patch('utils.llm.memory.ui.editFile', new=_editFileWriting(content)):
        result = await editMemoryViaEditor(initialPriority=7)

    assert result is not None
    body, tags, priority = result
    assert tags == ["x"]
    assert priority == 7  # 未提供，保留初始值


@pytest.mark.asyncio
async def test_edit_memory_no_separator_all_content():
    """无 --- 分隔符 → 全部行当 content（头部字段仍被解析）"""
    content = "# tags: a\nbody line 1\nbody line 2"
    with patch('utils.llm.memory.ui.editFile', new=_editFileWriting(content)):
        result = await editMemoryViaEditor()

    assert result is not None
    body, tags, priority = result
    assert tags == ["a"]
    # 无分隔符：所有行（含 # tags 行）都进 content
    assert "# tags: a" in body
    assert "body line 1" in body
    assert "body line 2" in body


@pytest.mark.asyncio
async def test_edit_memory_separator_boundary():
    """分隔符顺序边界：--- 之后的 # tags: 被当正文（遇首个 --- 即 break）"""
    content = "# tags: a, b\n---\n# tags: c\n正文"
    with patch('utils.llm.memory.ui.editFile', new=_editFileWriting(content)):
        result = await editMemoryViaEditor()

    assert result is not None
    body, tags, priority = result
    assert tags == ["a", "b"]  # 只解析分隔符之前的
    assert "# tags: c" in body  # 分隔符之后的 # tags 当正文
    assert "正文" in body


@pytest.mark.asyncio
async def test_edit_memory_empty_content_returns_none():
    """正文为空 → 返回 None"""
    content = "# tags: a\n---\n"  # 分隔符后无内容
    with patch('utils.llm.memory.ui.editFile', new=_editFileWriting(content)):
        result = await editMemoryViaEditor()

    assert result is None


@pytest.mark.asyncio
async def test_edit_memory_whitespace_only_content_returns_none():
    """正文只有空白 → 返回 None"""
    content = "# tags: a\n---\n   "
    with patch('utils.llm.memory.ui.editFile', new=_editFileWriting(content)):
        result = await editMemoryViaEditor()

    assert result is None


@pytest.mark.asyncio
async def test_edit_memory_cancel_returns_none():
    """编辑器取消（editFile 返 False）→ 返回 None"""
    with patch('utils.llm.memory.ui.editFile', new=_editFileReturningFalse()):
        result = await editMemoryViaEditor(initialContent="原内容", initialTags=["x"], initialPriority=2)

    assert result is None


@pytest.mark.asyncio
async def test_edit_memory_template_prefill():
    """initialTags / initialPriority / initialContent 正确回填进编辑器模板"""
    captured = {}

    async def capture(path):
        with open(path, "r", encoding="utf-8") as f:
            captured["template"] = f.read()
        return True  # 不改动，直接保存

    with patch('utils.llm.memory.ui.editFile', new=capture):
        await editMemoryViaEditor(initialContent="正文的喵", initialTags=["x", "y"], initialPriority=7)

    template = captured["template"]
    assert "# tags: x, y" in template
    assert "# priority: 7" in template
    assert "---" in template
    assert "正文的喵" in template




# ============================================================================
# MemoryTUIController.collectViewModel() 测试
# ============================================================================

def _memRow(id_, scopeType, scopeId, content, priority=0, enabled=True, tags=None):
    """构造一条 memory row（mock getMemories 返回元素）。"""
    return {
        "id": id_,
        "scope_type": scopeType,
        "scope_id": scopeId,
        "content": content,
        "tags": tags or [],
        "enabled": enabled,
        "priority": priority,
        "source": None,
    }


@pytest.mark.asyncio
async def test_collect_view_model_sorting_and_addrow_and_preview():
    """(+ ) 行在 index 0；排序 scope_type 顺序 + priority 降序 + id 降序；preview 截断"""
    rows = [
        _memRow(2, "global", None, "short", priority=1),
        _memRow(5, "chat", "123", "x" * 50, priority=5),
        _memRow(1, "global", None, "first", priority=10),
        _memRow(3, "user", "u1", "u content", priority=10),
    ]
    controller = MemoryTUIController(mode="manage")

    with patch('utils.llm.memory.ui.getMemories', new_callable=AsyncMock, return_value=rows):
        entries, meta = await controller.collectViewModel(selectedIndex=0)

    # index 0 是 (+) 添加行
    assert entries[0] == {"isAddRow": True}

    # 排序：global(chat=1, user=2, session=3 都在 global 后)；global 内 priority 降序，再 id 降序
    #   global prio10: id1；global prio1: id2；chat prio5: id5；user prio10: id3
    assert [e["id"] for e in entries[1:]] == [1, 2, 5, 3]

    # preview 截断：50 字 → 40 字 + "…"
    chatEntry = entries[3]
    assert chatEntry["preview"].endswith("…")
    assert len(chatEntry["preview"]) == 41


@pytest.mark.asyncio
async def test_collect_view_model_selected_clamp():
    """selectedIndex 超界钳制"""
    rows = [_memRow(1, "global", None, "only", priority=0)]
    controller = MemoryTUIController(mode="manage")

    with patch('utils.llm.memory.ui.getMemories', new_callable=AsyncMock, return_value=rows):
        entries, meta = await controller.collectViewModel(selectedIndex=99)

    # entries = [(+), row] → 2 项，max index 1
    assert len(entries) == 2
    assert meta["selected"] == 1




# ============================================================================
# MemoryTUIController.buildTable 渲染钉扎
# ============================================================================

def test_buildTable_memory_renders_rows_status_scope_help(capsys):
    """buildTable 渲染：标题 + (+) 行 + scope + ON/OFF 状态 + 帮助行"""
    entries = [
        {"isAddRow": True},
        {"id": 1, "scope_type": "global", "scope_id": None, "enabled": True, "priority": 5, "preview": "hello"},
        {"id": 2, "scope_type": "chat", "scope_id": "123", "enabled": False, "priority": 3, "preview": "world"},
    ]
    controller = MemoryTUIController(mode="manage")
    controller.entries = entries
    controller.selected = 1

    rowCount = controller.renderUI(entries, selectedIndex=1)
    out = _stripAnsi(capsys.readouterr().out)

    assert rowCount > 0
    assert "Memory 管理" in out                 # 标题
    assert "(+) 添加" in out                    # (+) 行
    assert "hello" in out and "world" in out    # preview
    assert "global" in out and "chat:123" in out  # scope 渲染（global 直显，非 global 拼 type:id）
    assert "ON" in out and "OFF" in out         # enabled 状态标记
    # memory 恒显示帮助行
    assert "Enter 编辑" in out and "Esc 退出" in out