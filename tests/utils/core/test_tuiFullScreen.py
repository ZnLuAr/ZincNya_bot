"""
tests/utils/core/test_tuiFullScreen.py

FullScreenTUIApp（utils/core/tui/paradigms/fullScreen.py）+ TextEditorApp
（utils/core/tui/paradigms/textEditor.py）的单元测试。

被测源码已验证正确，本文件只写测试，不改被测源码。
"""

import inspect

import pytest

from unittest.mock import MagicMock, patch, AsyncMock

from prompt_toolkit.layout import Layout

from utils.core.tui.paradigms import fullScreen as fullScreenModule
from utils.core.tui.paradigms.fullScreen import FullScreenTUIApp
from utils.core.tui.paradigms.textEditor import TextEditorApp
from utils.core.tui.session import TUISession




# ============================================================================
# 测试用 FakeFullScreen 子类（实现 buildLayout + setupKeyBindings + mainLoop）
# ============================================================================

class _FakeFullScreen(FullScreenTUIApp):
    """最小可实例化的 FullScreenTUIApp 子类，覆写 getExtraApplicationKwargs。"""

    def buildLayout(self):
        # 任意一个 pt 可布局控件即可（createApplication 会包成 Layout）
        from prompt_toolkit.widgets import TextArea
        return TextArea(text="fake")


    def setupKeyBindings(self, kb):
        @kb.add("c-q")
        def _quit(event):
            event.app.exit(result="quit")


    async def mainLoop(self):
        return await self.runOnce()


    def getExtraApplicationKwargs(self):
        return {"mouse_support": False}




class _FakeFullScreenNoExtra(FullScreenTUIApp):
    """不覆写 getExtraApplicationKwargs（走默认空 dict）的子类。"""

    def buildLayout(self):
        from prompt_toolkit.widgets import TextArea
        return TextArea(text="fake2")


    def setupKeyBindings(self, kb):
        @kb.add("c-q")
        def _quit(event):
            event.app.exit(result="quit")


    async def mainLoop(self):
        return await self.runOnce()




# ============================================================================
# 用例 1：createApplication 默认模板（非 abstract）
# ============================================================================

@pytest.mark.asyncio
async def test_createApplication_defaultTemplateNotAbstract():
    """createApplication 是具体方法（非 @abstractmethod），默认模板装配 pt Application。

    断言：①Application 被调一次；②kwargs 含 full_screen=True；③含 layout（Layout 实例）；
    ④含 key_bindings（含注册的 c-q）；⑤createApplication 不在 __abstractmethods__。
    """
    # ⑤createApplication 不是 abstract
    assert "createApplication" not in getattr(FullScreenTUIApp, "__abstractmethods__", set())

    with patch.object(fullScreenModule, "Application") as mockApplication:
        # 构造时 super().__init__ 会调 createApplication → 触发被 patch 的 Application
        app = _FakeFullScreenNoExtra()

    # ①只调一次
    assert mockApplication.call_count == 1
    kwargs = mockApplication.call_args.kwargs

    # ②full_screen=True
    assert kwargs["full_screen"] is True

    # ③layout 是 Layout 实例
    assert isinstance(kwargs["layout"], Layout)

    # ④key_bindings 存在，含我们注册的 c-q
    kb = kwargs["key_bindings"]
    assert kb is not None
    registered = set()
    for binding in kb.bindings:
        for key in binding.keys:
            registered.add(key.value)
    assert "c-q" in registered

    # _ptApp 就是 createApplication 的返回值（Application mock）
    assert app._ptApp is mockApplication.return_value




# ============================================================================
# 用例 2：getExtraApplicationKwargs 钩子
# ============================================================================

@pytest.mark.asyncio
async def test_getExtraApplicationKwargs_mergedIntoApplication():
    """子类覆写 getExtraApplicationKwargs 的返回值应并入 Application 构造 kwargs。"""
    with patch.object(fullScreenModule, "Application") as mockApplication:
        _FakeFullScreen()  # getExtraApplicationKwargs → {"mouse_support": False}

    assert mockApplication.call_count == 1
    kwargs = mockApplication.call_args.kwargs

    # 来自 extra kwargs
    assert kwargs["mouse_support"] is False
    # 默认模板项仍在
    assert kwargs["full_screen"] is True
    assert isinstance(kwargs["layout"], Layout)
    assert kwargs["key_bindings"] is not None




# ============================================================================
# 用例 3：模块依赖方向（防循环 import）——无对 chatScreen 的代码级依赖
# ============================================================================

def test_fullScreenModule_doesNotImportChatScreen():
    """fullScreen 基类模块不得 import 具体子类 chatScreen（防循环 import）。

    docstring / 注释里出现的描述性 'chatScreen' 措辞允许（不是依赖）；只断言无代码级
    import 依赖——剥离 docstring 与注释后，源码不得含 'import chatScreen' 或
    'from ... chatScreen'。
    """
    source = inspect.getsource(fullScreenModule)

    # 剥离 docstring（模块 + 函数/类）和注释后剩下的「真代码」
    realCodeLines = []
    inDocstring = False
    docstringToken = None
    for line in source.splitlines():
        stripped = line.strip()

        # 处理多行 docstring 区间
        if inDocstring:
            if docstringToken in stripped:
                inDocstring = False
            continue

        # 进入 docstring：行首以 """ 或 ''' 开头
        if stripped.startswith('"""') or stripped.startswith("'''"):
            token = '"""' if stripped.startswith('"""') else "'''"
            # 单行 docstring（开闭在同一行）不算进入区间
            if stripped.count(token) >= 2:
                continue
            inDocstring = True
            docstringToken = token
            continue

        # 剥离行内注释（粗暴：'#' 之后当注释；字符串里的 '#' 在本模块源码里不存在）
        codePart = line.split("#", 1)[0]
        if codePart.strip():
            realCodeLines.append(codePart)

    realCode = "\n".join(realCodeLines)

    assert "import chatScreen" not in realCode, (
        "fullScreen 基类模块不得 import chatScreen——会引入对具体子类的依赖，形成循环 import。"
    )
    assert "chatScreen" not in realCode.replace(" ", ""), (
        "fullScreen 基类模块的代码区不得出现 chatScreen 标识符（docstring/注释里的描述性"
        "措辞已剥离；若仍有出现说明是代码级引用，禁止）。"
    )




# ============================================================================
# 用例 4：runOnce 的 reset_attributes + flush
# ============================================================================

@pytest.mark.asyncio
async def test_runOnce_resetsAttributesAndFlushesAndReturnsRunAsyncResult():
    """runOnce：await run_async → output.reset_attributes → output.flush，并透传返回值。"""
    with patch.object(fullScreenModule, "Application"):
        # 构造期 createApplication 调一次，随后我们换掉 _ptApp
        app = _FakeFullScreen()

    # 替换 _ptApp 为受控 mock：run_async 是 AsyncMock、output.reset_attributes/flush 是 MagicMock
    fakeApp = MagicMock()
    fakeApp.run_async = AsyncMock(return_value="run-result")
    fakeApp.output.reset_attributes = MagicMock()
    fakeApp.output.flush = MagicMock()
    app._ptApp = fakeApp

    # 基类 runOnce（FakeFullScreen 不覆写）
    result = await app.runOnce()

    # run_async 被 await（一次）
    assert fakeApp.run_async.await_count == 1
    # reset_attributes + flush 各一次
    assert fakeApp.output.reset_attributes.call_count == 1
    assert fakeApp.output.flush.call_count == 1

    # 顺序：reset 在 flush 前
    resetIdx = next(i for i, c in enumerate(fakeApp.output.mock_calls) if c[0] == "reset_attributes")
    flushIdx = next(i for i, c in enumerate(fakeApp.output.mock_calls) if c[0] == "flush")
    assert resetIdx < flushIdx

    # 返回值 == run_async 的返回值
    assert result == "run-result"




# ============================================================================
# 用例 5：TextEditorApp 构造 + 保存/放弃分支透传
# ============================================================================

@pytest.mark.asyncio
async def test_textEditorApp_saveBranch_writesInitialContentAndReturnsTrue(tmp_path):
    """run_async 返回 True（保存）→ runSession 返回 True；构造时读到的初始内容仍写入文件。

    注：TextEditorApp 没自己 import Application——createApplication 走基类，引用的是
    fullScreen 模块里的 Application，所以 patch 点在 fullScreenModule.Application。
    mock 掉 Application/run_async 后按键处理器不会触发——这里验证构造（读初始内容）
    + runSession 单发生命周期走通 + 返回值透传，不验证按键写文件。
    """
    filePath = tmp_path / "f.txt"
    filePath.write_text("hello editor", encoding="utf-8")

    with patch.object(fullScreenModule, "Application") as mockApplication:
        # mainLoop → runOnce → run_async，mock 返回 True（保存分支）
        mockApplication.return_value.run_async = AsyncMock(return_value=True)
        mockApplication.return_value.output.reset_attributes = MagicMock()
        mockApplication.return_value.output.flush = MagicMock()

        editor = TextEditorApp(str(filePath))
        # 构造期已读初始内容
        assert editor._editor.text == "hello editor"
        result = await editor.runSession()

    # 返回值透传：True
    assert result is True
    # 文件内容是构造时读的初始内容（未被按键改写，因为 run_async 被 mock）
    assert filePath.read_text(encoding="utf-8") == "hello editor"




@pytest.mark.asyncio
async def test_textEditorApp_cancelBranch_returnsFalse(tmp_path):
    """run_async 返回 False（放弃）→ runSession 返回 False。"""
    filePath = tmp_path / "g.txt"
    filePath.write_text("original", encoding="utf-8")

    with patch.object(fullScreenModule, "Application") as mockApplication:
        mockApplication.return_value.run_async = AsyncMock(return_value=False)
        mockApplication.return_value.output.reset_attributes = MagicMock()
        mockApplication.return_value.output.flush = MagicMock()

        editor = TextEditorApp(str(filePath))
        result = await editor.runSession()

    assert result is False
    assert filePath.read_text(encoding="utf-8") == "original"




def test_textEditorApp_constructionAndType_identity(tmp_path):
    """简化兜底：TextEditorApp 构造不报错 + 类型契约 + 关键方法存在（隔离难度高时的退路）。"""
    filePath = tmp_path / "h.txt"
    filePath.write_text("seed", encoding="utf-8")

    with patch.object(fullScreenModule, "Application"):
        editor = TextEditorApp(str(filePath))

    # 类型契约
    assert isinstance(editor, FullScreenTUIApp)
    assert isinstance(editor, TUISession)
    # 生命周期方法存在（mainLoop 来自子类，runOnce/runSession 来自基类）
    assert callable(getattr(editor, "mainLoop", None))
    assert callable(getattr(editor, "runOnce", None))
    assert callable(getattr(editor, "runSession", None))




def test_textEditorApp_nonExistentFile_constructsWithEmptyText(tmp_path):
    """文件不存在时构造不报错，初始内容为空串。"""
    filePath = tmp_path / "does_not_exist.txt"

    with patch.object(fullScreenModule, "Application"):
        editor = TextEditorApp(str(filePath))

    assert editor._editor.text == ""
    assert editor._filePath == str(filePath)