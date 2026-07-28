"""
tests/utils/core/test_tuiListMenu.py

测试 utils/core/tui/paradigms/listMenu.py 的 ListMenuController。

ListMenuController 是抽象基类（collectViewModel + buildTable 未实现）。本文件分两类覆盖：
    - 静态工具方法（calculateVisibleWindow / getTerminalHeight / drawLines 等）——直接调类方法。
    - 渲染管线（renderUI）——用一个最小 FakeController 子类（实现 buildTable 返回固定 rich Table）。
"""

import re

import pytest
from rich.console import Console
from rich.table import Table
from unittest.mock import MagicMock

import utils.core.tui.paradigms.listMenu as listMenuMod
from utils.core.tui.paradigms.listMenu import ListMenuController



# ============================================================================
# 去 ANSI 工具
# ============================================================================

ANSI_RE = re.compile(
    r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]|\x1b[=>]"
)


def stripAnsi(text: str) -> str:
    return ANSI_RE.sub("", text)


def makeEntries(n: int) -> list:
    """构造 n 个 dict 条目（含 name 字段，供 buildTable 取用）。"""
    return [{"name": f"item-{i}", "idx": i} for i in range(n)]



# ============================================================================
# 最小 FakeController（只实现 buildTable；其余走基类默认）
# ============================================================================

class _FakeController(ListMenuController):
    """最小可实例化子类：buildTable 返回固定 rich Table，标题 TESTTITLE。"""

    # 默认不覆写 getHelpLine → 基类返回 None
    # collectViewModel 是抽象方法，必须实现一个最简 stub（renderUI 不调它，给空数据即可）
    async def collectViewModel(self, selectedIndex):
        return [], {"selected": selectedIndex, "count": 0}

    def buildTable(self, visibleEntries, selectedIndex, windowStart):
        table = Table(title="TESTTITLE")
        table.add_column("name")
        table.add_column("seq")
        for entry in visibleEntries:
            table.add_row(entry["name"], str(entry["idx"]))
        return table


class _FakeControllerWithHelp(_FakeController):
    """带 help line 的 FakeController，用于 renderUI 顺序断言。"""

    def getHelpLine(self):
        return "HELPBOTTOMLINE"



# ============================================================================
# 1. calculateVisibleWindow 矩阵
# ============================================================================

class TestCalculateVisibleWindow:

    def test_allVisibleWhenEntriesFit(self):
        """条目数 <= maxVisible → 全显，windowStart=0，两端无 more。"""
        entries = makeEntries(5)
        # terminalHeight=24, reservedLines=8 → maxVisible=16，5 <= 16 全显
        visible, windowStart, hasMore = ListMenuController.calculateVisibleWindow(
            entries, selectedIndex=2, terminalHeight=24
        )

        assert visible == entries
        assert windowStart == 0
        assert hasMore == {"up": False, "down": False}

    def test_allVisibleExactMaxVisible(self):
        """条目数恰等于 maxVisible → 全显、无 more（边界用 <=）。"""
        entries = makeEntries(16)  # terminalHeight=24, reserved=8 → maxVisible=16
        visible, windowStart, hasMore = ListMenuController.calculateVisibleWindow(
            entries, selectedIndex=8, terminalHeight=24
        )

        assert len(visible) == 16
        assert windowStart == 0
        assert hasMore == {"up": False, "down": False}

    def test_windowCentersSelectedWhenTooMany(self):
        """条目数 > maxVisible、selected 居中 → windowStart 使 selected 居中，两端 True。"""
        entries = makeEntries(40)
        # terminalHeight=24, reserved=8 → maxVisible=16，halfWindow=8
        selected = 20
        visible, windowStart, hasMore = ListMenuController.calculateVisibleWindow(
            entries, selectedIndex=selected, terminalHeight=24
        )

        # 居中：windowStart = 20 - 8 = 12，windowEnd = 28
        assert windowStart == 12
        assert len(visible) == 16
        assert visible[0]["idx"] == 12
        assert visible[-1]["idx"] == 27
        assert hasMore == {"up": True, "down": True}

    def test_clampsWindowAtTopEdge(self):
        """selected 靠近开头 → 窗口钳制不越界、仍填满 maxVisible 行。"""
        entries = makeEntries(40)
        # selected=1：自然 windowStart = max(0, 1-8) = 0，windowEnd = 16，无需回拉
        visible, windowStart, hasMore = ListMenuController.calculateVisibleWindow(
            entries, selectedIndex=1, terminalHeight=24
        )

        assert windowStart == 0
        assert len(visible) == 16
        assert hasMore == {"up": False, "down": True}

    def test_clampsWindowAtBottomEdge(self):
        """selected 靠近末尾 → 窗口钳制到末尾、仍填满 maxVisible 行、上端 more=True。"""
        entries = makeEntries(40)
        # selected=39（最后一个）：windowStart = max(0, 39-8) = 31，windowEnd = min(40, 47) = 40
        # windowEnd - windowStart = 9 < 16 → 回拉 windowStart = max(0, 40-16) = 24
        visible, windowStart, hasMore = ListMenuController.calculateVisibleWindow(
            entries, selectedIndex=39, terminalHeight=24
        )

        assert windowStart == 24
        assert len(visible) == 16
        assert visible[0]["idx"] == 24
        assert visible[-1]["idx"] == 39
        assert hasMore == {"up": True, "down": False}

    def test_maxVisibleFloorFive(self):
        """terminalHeight 极小（10）、reservedLines=8 → maxVisible = max(5, 2) = 5（下限保护）。"""
        entries = makeEntries(20)
        visible, windowStart, hasMore = ListMenuController.calculateVisibleWindow(
            entries, selectedIndex=10, terminalHeight=10
        )

        # maxVisible=5，halfWindow=2，windowStart = max(0, 10-2) = 8，windowEnd = min(20, 13) = 13
        assert len(visible) == 5
        assert windowStart == 8
        assert hasMore == {"up": True, "down": True}



# ============================================================================
# 2. getTerminalHeight 委托
# ============================================================================

class TestGetTerminalHeight:

    def test_returnsHeightComponentOfTuple(self, monkeypatch):
        """patch getTerminalSize 返回 (100, 40) → getTerminalHeight() == 40。"""
        monkeypatch.setattr(listMenuMod, "getTerminalSize", lambda: (100, 40))

        assert ListMenuController.getTerminalHeight() == 40



# ============================================================================
# 3. renderUI 管线
# ============================================================================

class TestRenderUI:

    def test_renderUIReturnsPositiveLineCount(self, monkeypatch, capsys):
        """renderUI 返回正整数（行数）。"""
        monkeypatch.setattr(listMenuMod, "getTerminalSize", lambda: (100, 40))
        # 屏蔽真实 cls（写 ANSI 到 stdout），drawLines 仍走真实 print
        monkeypatch.setattr(listMenuMod, "cls", MagicMock())

        ctrl = _FakeController()
        entries = makeEntries(5)

        lineCount = ctrl.renderUI(entries, selectedIndex=0)

        assert isinstance(lineCount, int)
        assert lineCount > 0

    def test_renderUIContainsTableTitleAndContent(self, monkeypatch, capsys):
        """输出含 buildTable 的标题与某行内容。"""
        monkeypatch.setattr(listMenuMod, "getTerminalSize", lambda: (100, 40))
        monkeypatch.setattr(listMenuMod, "cls", MagicMock())

        ctrl = _FakeController()
        entries = makeEntries(3)

        ctrl.renderUI(entries, selectedIndex=0)
        out = stripAnsi(capsys.readouterr().out)

        assert "TESTTITLE" in out
        assert "item-1" in out

    def test_renderUIContainsUpDownIndicatorsWhenHasMore(self, monkeypatch, capsys):
        """条目数超 maxVisible 时输出含 ↑ / ↓ 指示。"""
        monkeypatch.setattr(listMenuMod, "getTerminalSize", lambda: (100, 24))
        # reservedLines 默认 8 → maxVisible=16，30 > 16 两端均 more
        monkeypatch.setattr(listMenuMod, "cls", MagicMock())

        ctrl = _FakeController()
        entries = makeEntries(30)

        ctrl.renderUI(entries, selectedIndex=15)
        out = stripAnsi(capsys.readouterr().out)

        assert "↑" in out
        assert "↓" in out
        assert "更多" in out

    def test_renderUIContainsHelpLineWhenProvided(self, monkeypatch, capsys):
        """FakeControllerWithHelp.getHelpLine 非 None → 输出含 help 文本。"""
        monkeypatch.setattr(listMenuMod, "getTerminalSize", lambda: (100, 40))
        monkeypatch.setattr(listMenuMod, "cls", MagicMock())

        ctrl = _FakeControllerWithHelp()
        entries = makeEntries(3)

        ctrl.renderUI(entries, selectedIndex=0)
        out = stripAnsi(capsys.readouterr().out)

        assert "HELPBOTTOMLINE" in out

    def test_renderUIOrderTableBeforeIndicatorsBeforeHelp(self, monkeypatch, capsys):
        """顺序：表格标题 → ↑↓ 指示 → help line（按出现 index 递增）。"""
        monkeypatch.setattr(listMenuMod, "getTerminalSize", lambda: (100, 24))
        monkeypatch.setattr(listMenuMod, "cls", MagicMock())

        ctrl = _FakeControllerWithHelp()
        entries = makeEntries(30)  # 触发两端 more

        ctrl.renderUI(entries, selectedIndex=15)
        out = stripAnsi(capsys.readouterr().out)

        titleIdx = out.find("TESTTITLE")
        upIdx = out.find("↑")
        downIdx = out.find("↓")
        helpIdx = out.find("HELPBOTTOMLINE")

        # 各自都应出现
        assert titleIdx != -1
        assert upIdx != -1
        assert downIdx != -1
        assert helpIdx != -1

        # 标题在 ↑↓ 之前；↑↓ 在 help 之前
        assert titleIdx < upIdx
        assert downIdx < helpIdx



# ============================================================================
# 4. drawLines 调 cls
# ============================================================================

class TestDrawLines:

    def test_drawLinesCallsClsAndReturnsCount(self, monkeypatch, capsys):
        """drawLines：cls 被调一次、返回行数、capsys 抓到所有行。"""
        clsMock = MagicMock()
        monkeypatch.setattr(listMenuMod, "cls", clsMock)

        result = ListMenuController.drawLines(["a", "b"])

        assert clsMock.call_count == 1
        assert result == 2

        out = capsys.readouterr().out
        assert "a" in out
        assert "b" in out

    def test_drawLinesEmptyList(self, monkeypatch, capsys):
        """空列表：cls 仍调一次、返回 0。"""
        clsMock = MagicMock()
        monkeypatch.setattr(listMenuMod, "cls", clsMock)

        result = ListMenuController.drawLines([])

        assert clsMock.call_count == 1
        assert result == 0



# ============================================================================
# 5. getHelpLine 默认 None
# ============================================================================

class TestGetHelpLine:

    def test_defaultHelpLineIsNone(self):
        """未覆写 getHelpLine 的 FakeController 实例 → 基类返回 None。"""
        ctrl = _FakeController()
        assert ctrl.getHelpLine() is None



# ============================================================================
# 6. onEnter / onExit 调 smcup / rmcup
# ============================================================================

class TestScreenLifecycle:

    @pytest.mark.asyncio
    async def test_onEnterCallsSmcup(self, monkeypatch):
        """onEnter → smcup 被调。"""
        smcupMock = MagicMock()
        rmcupMock = MagicMock()
        monkeypatch.setattr(listMenuMod, "smcup", smcupMock)
        monkeypatch.setattr(listMenuMod, "rmcup", rmcupMock)
        # onEnter 还会调 refreshEntries → collectViewModel；patch 掉避免依赖子类实现
        monkeypatch.setattr(_FakeController, "refreshEntries", _noOpAwaitable)

        ctrl = _FakeController()
        await ctrl.onEnter()

        assert smcupMock.call_count == 1

    @pytest.mark.asyncio
    async def test_onExitCallsRmcup(self, monkeypatch):
        """onExit → rmcup 被调。"""
        smcupMock = MagicMock()
        rmcupMock = MagicMock()
        monkeypatch.setattr(listMenuMod, "smcup", smcupMock)
        monkeypatch.setattr(listMenuMod, "rmcup", rmcupMock)

        ctrl = _FakeController()
        await ctrl.onExit()

        assert rmcupMock.call_count == 1


# ----------------------------------------------------------------------------
# 辅助：refreshEntries 的异步 no-op 替身
# ----------------------------------------------------------------------------

async def _noOpAwaitable(self, *args, **kwargs):
    return None