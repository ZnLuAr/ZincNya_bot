"""
tests/utils/nyaQuoteManager/test_ui.py

测试 utils/nyaQuoteManager/ui.py
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from utils.nyaQuoteManager.ui import (
    _extractBaseWeight,
    collectQuoteViewModel,
    editQuoteViaEditor,
)


# ============================================================================
# _extractBaseWeight() 测试
# ============================================================================

def test_extract_base_weight_float():
    """float 类型直接返回"""
    assert _extractBaseWeight(1.5) == 1.5
    assert _extractBaseWeight(0.0) == 0.0


def test_extract_base_weight_list():
    """list 类型返回第一个元素"""
    assert _extractBaseWeight([2.0, 0.8, 0.5]) == 2.0
    assert _extractBaseWeight([1.0]) == 1.0


def test_extract_base_weight_empty_list():
    """空 list 返回 1.0"""
    assert _extractBaseWeight([]) == 1.0


# ============================================================================
# collectQuoteViewModel() 测试
# ============================================================================

def test_collect_quote_view_model_empty():
    """空语录库返回仅含 (+) 行的列表"""
    with patch('utils.nyaQuoteManager.ui.loadQuoteFile', return_value=[]):
        entries, meta = collectQuoteViewModel()

        assert len(entries) == 1
        assert entries[0]['text'] == "(+)"
        assert entries[0]['preview'] == "添加新语录"
        assert entries[0]['weight'] is None
        assert entries[0]['raw'] is None

        assert meta['selected'] == 0
        assert meta['count'] == 1


def test_collect_quote_view_model_single():
    """单条语录"""
    quotes = [
        {"text": "喵~", "weight": 1.0}
    ]

    with patch('utils.nyaQuoteManager.ui.loadQuoteFile', return_value=quotes):
        entries, meta = collectQuoteViewModel()

        assert len(entries) == 2  # (+) 行 + 1 条语录
        assert entries[0]['text'] == "(+)"
        assert entries[1]['text'] == "喵~"
        assert entries[1]['preview'] == "喵~"
        assert entries[1]['weight'] == 1.0


def test_collect_quote_view_model_sorted_by_weight():
    """多条语录按权重排序"""
    quotes = [
        {"text": "高权重", "weight": 3.0},
        {"text": "低权重", "weight": 1.0},
        {"text": "中权重", "weight": 2.0},
    ]

    with patch('utils.nyaQuoteManager.ui.loadQuoteFile', return_value=quotes):
        entries, meta = collectQuoteViewModel()

        # (+) 行 + 3 条语录（按权重升序）
        assert len(entries) == 4
        assert entries[1]['text'] == "低权重"
        assert entries[2]['text'] == "中权重"
        assert entries[3]['text'] == "高权重"


def test_collect_quote_view_model_preview_truncation():
    """preview 截断（15 字符）"""
    quotes = [
        {"text": "这是一个非常非常非常非常长的语录", "weight": 1.0}
    ]

    with patch('utils.nyaQuoteManager.ui.loadQuoteFile', return_value=quotes):
        entries, meta = collectQuoteViewModel()

        assert len(entries[1]['preview']) <= 18  # 15 + "..."
        assert entries[1]['preview'].endswith("...")


def test_collect_quote_view_model_preview_no_truncation():
    """preview 不截断（短文本）"""
    quotes = [
        {"text": "短文本", "weight": 1.0}
    ]

    with patch('utils.nyaQuoteManager.ui.loadQuoteFile', return_value=quotes):
        entries, meta = collectQuoteViewModel()

        assert entries[1]['preview'] == "短文本"
        assert not entries[1]['preview'].endswith("...")


def test_collect_quote_view_model_selected_index():
    """selectedIndex 边界处理"""
    quotes = [
        {"text": "语录1", "weight": 1.0},
        {"text": "语录2", "weight": 2.0},
    ]

    with patch('utils.nyaQuoteManager.ui.loadQuoteFile', return_value=quotes):
        # 正常索引
        entries, meta = collectQuoteViewModel(selectedIndex=1)
        assert meta['selected'] == 1

        # 超出范围（钳制到最大值）
        entries, meta = collectQuoteViewModel(selectedIndex=10)
        assert meta['selected'] == 2  # 最大索引

        # 负数（钳制到 0）
        entries, meta = collectQuoteViewModel(selectedIndex=-5)
        assert meta['selected'] == 0

        # None（默认 -1 → 钳制到 0）
        entries, meta = collectQuoteViewModel(selectedIndex=None)
        assert meta['selected'] == 0


def test_collect_quote_view_model_list_weight():
    """list 格式权重提取"""
    quotes = [
        {"text": "多消息", "weight": [2.0, 0.8, 0.5]}
    ]

    with patch('utils.nyaQuoteManager.ui.loadQuoteFile', return_value=quotes):
        entries, meta = collectQuoteViewModel()

        # 应该提取第一个元素作为基础权重
        assert entries[1]['weight'] == 2.0


# ============================================================================
# editQuoteViaEditor() 测试
# ============================================================================

@pytest.mark.asyncio
async def test_edit_quote_via_editor_basic():
    """基本编辑"""
    with patch('utils.nyaQuoteManager.ui.editFile', new_callable=AsyncMock) as mock_edit:
        with patch('builtins.open', create=True) as mock_open:
            mock_edit.return_value = True

            # Mock 文件读取
            mock_file = MagicMock()
            mock_file.read.return_value = "# weight: 2.0\n新文本"
            mock_file.__enter__.return_value = mock_file
            mock_open.return_value = mock_file

            result = await editQuoteViaEditor("旧文本", 1.0)

            assert result is not None
            text, weight = result
            assert text == "新文本"
            assert weight == 2.0


@pytest.mark.asyncio
async def test_edit_quote_via_editor_list_weight():
    """list 权重序列化/反序列化"""
    with patch('utils.nyaQuoteManager.ui.editFile', new_callable=AsyncMock) as mock_edit:
        with patch('builtins.open', create=True) as mock_open:
            mock_edit.return_value = True

            # Mock 文件读取
            mock_file = MagicMock()
            mock_file.read.return_value = "# weight: 1.0, 0.8, 0.5\n多条消息"
            mock_file.__enter__.return_value = mock_file
            mock_open.return_value = mock_file

            result = await editQuoteViaEditor("旧文本", [1.0, 0.8])

            assert result is not None
            text, weight = result
            assert text == "多条消息"
            assert weight == [1.0, 0.8, 0.5]


@pytest.mark.asyncio
async def test_edit_quote_via_editor_single_weight_degrades_to_float():
    """单个权重值退化为 float"""
    with patch('utils.nyaQuoteManager.ui.editFile', new_callable=AsyncMock) as mock_edit:
        with patch('builtins.open', create=True) as mock_open:
            mock_edit.return_value = True

            mock_file = MagicMock()
            mock_file.read.return_value = "# weight: 1.5\n文本"
            mock_file.__enter__.return_value = mock_file
            mock_open.return_value = mock_file

            result = await editQuoteViaEditor("旧文本", 1.0)

            assert result is not None
            text, weight = result
            assert weight == 1.5  # float，不是 [1.5]


@pytest.mark.asyncio
async def test_edit_quote_via_editor_newline_escape():
    """\\n 转义处理"""
    with patch('utils.nyaQuoteManager.ui.editFile', new_callable=AsyncMock) as mock_edit:
        with patch('builtins.open', create=True) as mock_open:
            mock_edit.return_value = True

            mock_file = MagicMock()
            # 用户在编辑器中输入真实换行符
            mock_file.read.return_value = "# weight: 1.0\n第一行\n第二行"
            mock_file.__enter__.return_value = mock_file
            mock_open.return_value = mock_file

            result = await editQuoteViaEditor("旧文本\\n旧第二行", 1.0)

            assert result is not None
            text, weight = result
            # 应该转义为 \\n
            assert text == "第一行\\n第二行"


@pytest.mark.asyncio
async def test_edit_quote_via_editor_user_cancel():
    """用户取消编辑"""
    with patch('utils.nyaQuoteManager.ui.editFile', new_callable=AsyncMock) as mock_edit:
        mock_edit.return_value = False

        result = await editQuoteViaEditor("文本", 1.0)

        assert result is None


@pytest.mark.asyncio
async def test_edit_quote_via_editor_empty_file():
    """空文件"""
    with patch('utils.nyaQuoteManager.ui.editFile', new_callable=AsyncMock) as mock_edit:
        with patch('builtins.open', create=True) as mock_open:
            mock_edit.return_value = True

            mock_file = MagicMock()
            mock_file.read.return_value = ""
            mock_file.__enter__.return_value = mock_file
            mock_open.return_value = mock_file

            result = await editQuoteViaEditor("文本", 1.0)

            assert result is None


@pytest.mark.asyncio
async def test_edit_quote_via_editor_invalid_weight():
    """无效权重（使用默认值）"""
    with patch('utils.nyaQuoteManager.ui.editFile', new_callable=AsyncMock) as mock_edit:
        with patch('builtins.open', create=True) as mock_open:
            mock_edit.return_value = True

            mock_file = MagicMock()
            mock_file.read.return_value = "# weight: invalid\n文本"
            mock_file.__enter__.return_value = mock_file
            mock_open.return_value = mock_file

            result = await editQuoteViaEditor("旧文本", 2.0)

            assert result is not None
            text, weight = result
            # 解析失败时使用 [1.0]，单个值退化为 float
            assert weight == 1.0


@pytest.mark.asyncio
async def test_edit_quote_via_editor_no_weight_line():
    """无 weight 行（保持原权重）"""
    with patch('utils.nyaQuoteManager.ui.editFile', new_callable=AsyncMock) as mock_edit:
        with patch('builtins.open', create=True) as mock_open:
            mock_edit.return_value = True

            mock_file = MagicMock()
            mock_file.read.return_value = "新文本\n第二行"
            mock_file.__enter__.return_value = mock_file
            mock_open.return_value = mock_file

            result = await editQuoteViaEditor("旧文本", 3.0)

            assert result is not None
            text, weight = result
            assert text == "新文本\\n第二行"
            assert weight == 3.0  # 保持原权重


@pytest.mark.asyncio
async def test_edit_quote_via_editor_tempfile_cleanup():
    """临时文件清理"""
    temp_path = None

    def capture_temp_path(path, *args, **kwargs):
        nonlocal temp_path
        temp_path = path
        return True

    with patch('utils.nyaQuoteManager.ui.editFile', new_callable=AsyncMock) as mock_edit:
        with patch('builtins.open', create=True) as mock_open:
            with patch('os.unlink') as mock_unlink:
                with patch('os.path.exists', return_value=True):
                    mock_edit.side_effect = capture_temp_path

                    mock_file = MagicMock()
                    mock_file.read.return_value = "# weight: 1.0\n文本"
                    mock_file.__enter__.return_value = mock_file
                    mock_open.return_value = mock_file

                    await editQuoteViaEditor("文本", 1.0)

                    # 应该清理临时文件
                    mock_unlink.assert_called_once()